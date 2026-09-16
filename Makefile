# hpcusage developer entry points.
#
# Every target that only needs Python/bash also works INSIDE the app container
# (`make shell`, or `docker compose exec app make <target>`): test, test-collector,
# test-app, lint, seed, token, vendor, backfill. Targets that need Docker on the host
# (up/down/logs/build/shell and the cdk-* / synth / diff / deploy targets) refuse to run elsewhere.
#
# CDK and the AWS CLI never run on the host: synth/diff/deploy/bootstrap go through the
# isolated `cdk` compose service (infra/Dockerfile), which has no Docker socket. The app image
# is built and pushed by `make push` with the host's Docker; App Runner deploys from ECR.
# Inside the cdk container CDK=cdk and AWS_CLI=aws, so the same targets call the CLIs directly.
.PHONY: help up down logs shell seed test test-collector test-app lint vendor build token backfill \
        cdk-build cdk-shell bootstrap synth diff push deploy-base deploy-app deploy clean need-docker

COMPOSE ?= docker compose
PYTHON  ?= python3
ENV_NAME ?= prod
STACK_BASE = HpcUsage-$(ENV_NAME)-base
STACK_APP  = HpcUsage-$(ENV_NAME)
CDK     ?= $(COMPOSE) --profile cdk run --rm cdk cdk -c env_name=$(ENV_NAME)
AWS_CLI ?= $(COMPOSE) --profile cdk run --rm -T cdk aws
GIT_SHA ?= $(shell git rev-parse --short HEAD 2>/dev/null || echo dev)
stack_output = $(AWS_CLI) cloudformation describe-stacks --stack-name $(1) \
               --query "Stacks[0].Outputs[?OutputKey=='$(2)'].OutputValue" --output text 2>/dev/null
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
cdk-build: need-docker ## (Re)build the CDK toolbox image
	$(COMPOSE) --profile cdk build cdk

cdk-shell: need-docker ## Shell in the CDK/AWS toolbox (cwd infra/; aws + cdk available)
	$(COMPOSE) --profile cdk run --rm cdk

bootstrap: need-docker ## CDK bootstrap (once per AWS account/region)
	$(CDK) bootstrap

synth: need-docker ## CDK synth (both stacks)
	$(CDK) synth --all

diff: need-docker ## CDK diff against what is deployed
	$(CDK) diff --all

deploy-base: need-docker ## Deploy VPC connector, ECR repo, secrets (then run scripts/set_secrets.sh)
	$(CDK) deploy $(STACK_BASE)

push: need-docker ## Build the app image on the host and push it to ECR (App Runner auto-deploys :latest)
	$(eval ECR_URI := $(shell $(call stack_output,$(STACK_BASE),EcrRepositoryUri)))
	@test -n "$(ECR_URI)" || { echo "no ECR repository yet: run 'make deploy-base' first"; exit 1; }
	$(AWS_CLI) ecr get-login-password | docker login --username AWS --password-stdin $(firstword $(subst /, ,$(ECR_URI)))
	docker build --platform linux/amd64 --target runtime -t $(ECR_URI):latest -t $(ECR_URI):$(GIT_SHA) .
	docker push $(ECR_URI):$(GIT_SHA)
	docker push $(ECR_URI):latest
	@echo "pushed $(ECR_URI):latest ($(GIT_SHA)); App Runner redeploys automatically once the service exists"

deploy-app: need-docker ## Deploy the App Runner service (needs a pushed image and real secrets)
	@$(AWS_CLI) secretsmanager get-secret-value --secret-id $$($(call stack_output,$(STACK_BASE),DatabaseUrlSecretArn)) \
	    --query SecretString --output text | grep -q CHANGE_ME \
	  && { echo "DATABASE_URL secret is still the placeholder: run scripts/set_secrets.sh $(ENV_NAME) (make cdk-shell)"; exit 1; } || true
	$(CDK) deploy $(STACK_APP)

deploy: deploy-base push deploy-app ## Full deployment: base stack -> image push -> App Runner service

# --- helpers -------------------------------------------------------------------
.env:
	cp .env.example .env

need-docker:
	@command -v docker >/dev/null 2>&1 || { echo "this target needs Docker: run it on the host (or in the cdk container), not the app container"; exit 1; }

clean:
	rm -rf .pytest_cache .ruff_cache infra/cdk.out
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
