# hpcusage — run `make help` for the target list.
#
# Targets that only need Python + the app's dependencies work inside the app container
# (`make shell`, or `docker compose exec app make <target>`): test, test-collector,
# test-app, lint, seed, token, vendor, backfill. Targets that need Docker on the host
# (up/down/logs/build/shell) refuse to run elsewhere.
#
# AWS: the synth/diff/deploy/bootstrap targets are `cdk <command>` run in infra/ (needs the CDK CLI,
# the AWS CLI and `pip install -r infra/requirements.txt`); push is scripts/push.sh. See infra/README.md.
.PHONY: help up down logs shell seed test test-collector test-app lint vendor build token backfill \
        bootstrap synth diff deploy push clean need-docker

COMPOSE ?= docker compose
PYTHON  ?= python3
CDK     = cd infra && cdk
STACKS ?= --all
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

# --- AWS (STACKS=--all; e.g. make deploy STACKS=HpcUsage-prod-base) -------------
bootstrap: ## cdk bootstrap (once per account/region)
	$(CDK) bootstrap

synth: ## cdk synth: writes the CloudFormation templates to infra/cdk.out/ for review
	$(CDK) synth $(STACKS)

diff: ## cdk diff: what deploy would change in the account
	$(CDK) diff $(STACKS)

deploy: ## cdk deploy (asks before IAM / security-group changes)
	$(CDK) deploy $(STACKS)

push: need-docker ## Build the app image and push it to ECR; App Runner redeploys :latest
	scripts/push.sh

# --- helpers -------------------------------------------------------------------
.env:
	cp .env.example .env

need-docker:
	@command -v docker >/dev/null 2>&1 || { echo "this target needs Docker: run it on the host, not in the app container"; exit 1; }

clean:
	rm -rf .pytest_cache .ruff_cache infra/cdk.out
	find . -name __pycache__ -type d -prune -exec rm -rf {} +
