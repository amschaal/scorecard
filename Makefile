# hpcusage — run `make help` for the target list.
#
# Targets that only need Python + the app's dependencies work inside the app container
# (`make shell`, or `docker compose exec app make <target>`): test, test-collector,
# test-app, lint, seed, token, vendor, backfill. Targets that need Docker on the host
# (up/down/logs/build/shell and the cdk-* / synth / diff / deploy targets) refuse to run elsewhere.
#
# CDK and the AWS CLI never run on the host: they go through the isolated `cdk` compose service
# (infra/Dockerfile), which has no Docker socket. Deployment is audit-then-run: `make synth` writes
# plain CloudFormation to infra/cdk.out/ (offline), you review it, and the deploy targets send
# exactly those files to CloudFormation with your own credentials (AWS_CONFIG_DIR=~/.aws) — they
# never re-synthesize. Day to day only `make push` talks to AWS, with the push user's key, which
# can do nothing but push to the project's ECR repository.
.PHONY: help up down logs shell seed test test-collector test-app lint vendor build token backfill \
        cdk-build cdk-volume cdk-shell synth diff push deploy-base deploy-app deploy clean need-docker

COMPOSE ?= docker compose
PYTHON  ?= python3
ENV_NAME ?= prod
AWS_REGION ?= us-west-2
STACK_BASE = HpcUsage-$(ENV_NAME)-base
STACK_APP  = HpcUsage-$(ENV_NAME)
TOOLBOX ?= $(COMPOSE) --profile cdk run --rm -T cdk
CDK     ?= $(COMPOSE) --profile cdk run --rm cdk cdk -c env_name=$(ENV_NAME)
AWS_CLI ?= $(TOOLBOX) aws
GIT_SHA ?= $(shell git rev-parse --short HEAD 2>/dev/null || echo dev)
stack_output = $(AWS_CLI) cloudformation describe-stacks --stack-name $(1) \
               --query "Stacks[0].Outputs[?OutputKey=='$(2)'].OutputValue" --output text 2>/dev/null
# Deploy a previously synthesized template as-is, printing its checksum so you can confirm it is
# the file you reviewed. CAPABILITY_NAMED_IAM: the base stack names its push user.
cfn_deploy = $(TOOLBOX) sh -c 'set -e; f=cdk.out/$(1).template.json; \
    test -f $$f || { echo "$$f missing: run make synth, review it, then deploy"; exit 1; }; sha256sum $$f; \
    aws cloudformation deploy --stack-name $(1) --template-file $$f --capabilities CAPABILITY_NAMED_IAM --no-fail-on-empty-changeset'
# Inside the container docker-compose.override.yml sets HPCUSAGE_URL=http://app:8000.
SEED_URL ?= $(or $(HPCUSAGE_URL),http://localhost:8000)
SEED_DAYS ?= 45

help:
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

# --- host-only (Docker) --------------------------------------------------------
up: need-docker .env ## Start Postgres + app locally (http://localhost:8000)
	$(COMPOSE) up -d --build
	@echo "app: http://localhost:8000   (AUTH_MODE=dev)"

down: need-docker ## Stop local stack
	$(COMPOSE) down

logs: need-docker ## Tail app logs
	$(COMPOSE) logs -f app

shell: need-docker ## Shell inside the running app container (make targets work there)
	$(COMPOSE) exec app bash

build: need-docker ## Build the production container image
	docker build --target runtime -t hpcusage:local .

# --- portable (host or container) --------------------------------------------
seed: ## Load synthetic multi-day data (SEED_URL, SEED_DAYS)
	$(PYTHON) scripts/seed_local.py --url $(SEED_URL) --days $(SEED_DAYS)

test: test-collector test-app ## Run all tests

test-collector: ## Collector parser tests (stdlib unittest)
	$(PYTHON) -m unittest discover -s collector/tests -v

test-app: ## Backend tests: testcontainers Postgres on the host, HPCUSAGE_TEST_DATABASE_URL in the container
	$(PYTHON) -m pytest app/tests -q -p no:cacheprovider

lint: ## Ruff
	ruff check app infra scripts

vendor: ## Download plotly.min.js for running the app outside Docker
	curl -fsSL https://cdn.plot.ly/plotly-2.35.2.min.js -o app/hpcusage/static/plotly.min.js

token: ## Print a fresh collector token
	$(PYTHON) scripts/gen_token.py

backfill: ## Usage: make backfill CLUSTER=hive FROM=2026-01-01 TO=2026-09-01 (runs on the cluster)
	scripts/backfill.sh $(CLUSTER) $(FROM) $(TO)

# --- AWS deployment (ENV_NAME=prod) ---------------------------------------------
# The external Docker volume hpcusage-cdk-aws holds the push user's key (infra/README.md); the
# deploy targets need your own credentials instead: AWS_CONFIG_DIR=~/.aws make deploy-base.
cdk-build: need-docker ## (Re)build the CDK toolbox image
	$(COMPOSE) --profile cdk build cdk

cdk-volume: need-docker ## Create the credentials volume if missing (idempotent)
	@docker volume create hpcusage-cdk-aws >/dev/null

cdk-shell: cdk-volume ## Shell in the CDK/AWS toolbox (cwd infra/; aws + cdk available)
	$(COMPOSE) --profile cdk run --rm cdk

synth: cdk-volume ## Write plain CloudFormation to infra/cdk.out/ for review (offline; no credentials)
	$(CDK) synth --all --quiet
	@$(TOOLBOX) sha256sum cdk.out/$(STACK_BASE).template.json cdk.out/$(STACK_APP).template.json

diff: cdk-volume ## CDK diff of the synthesized templates against what is deployed (your credentials)
	$(CDK) diff --all

deploy-base: cdk-volume ## Send the reviewed base template to CloudFormation (VPC connector, ECR + push user, secrets)
	$(call cfn_deploy,$(STACK_BASE))

push: cdk-volume ## Build the app image on the host and push it to ECR (App Runner auto-deploys :latest)
	$(eval ACCOUNT_ID := $(shell $(AWS_CLI) sts get-caller-identity --query Account --output text 2>/dev/null))
	@test -n "$(ACCOUNT_ID)" || { echo "no AWS credentials in the toolbox: store the push user's key first (infra/README.md)"; exit 1; }
	$(eval ECR_URI := $(ACCOUNT_ID).dkr.ecr.$(AWS_REGION).amazonaws.com/hpcusage-$(ENV_NAME))
	$(AWS_CLI) ecr get-login-password | docker login --username AWS --password-stdin $(firstword $(subst /, ,$(ECR_URI)))
	docker build --platform linux/amd64 --target runtime -t $(ECR_URI):latest -t $(ECR_URI):$(GIT_SHA) .
	docker push $(ECR_URI):$(GIT_SHA)
	docker push $(ECR_URI):latest
	@echo "pushed $(ECR_URI):latest ($(GIT_SHA)); App Runner redeploys automatically once the service exists"

deploy-app: cdk-volume ## Send the reviewed app template to CloudFormation (needs a pushed image and real secrets)
	@$(AWS_CLI) secretsmanager get-secret-value --secret-id $$($(call stack_output,$(STACK_BASE),DatabaseUrlSecretArn)) \
	    --query SecretString --output text | grep -q CHANGE_ME \
	  && { echo "DATABASE_URL secret is still the placeholder: run scripts/set_secrets.sh $(ENV_NAME) (make cdk-shell)"; exit 1; } || true
	$(call cfn_deploy,$(STACK_APP))

deploy: deploy-base push deploy-app ## All three steps with one set of credentials (after make synth + review)

# --- helpers -------------------------------------------------------------------
.env:
	cp .env.example .env

need-docker:
	@command -v docker >/dev/null 2>&1 || { echo "this target needs Docker: run it on the host (or in the cdk container), not the app container"; exit 1; }

clean:
	rm -rf .pytest_cache .ruff_cache infra/cdk.out
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
