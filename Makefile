# Local development. The default backends need nothing (SQLite + files under work/);
# the compose stack runs the production backends (Postgres, Redis, S3) locally.

COMPOSE := docker compose -f deploy/compose.dev.yaml
PYTHON ?= .venv/bin/python

TEST_ENV := SDE_TEST_DATABASE_URL=postgresql://sde:sde@localhost:$${SDE_PG_PORT:-5432}/sde_test \
            SDE_TEST_REDIS_URL=redis://localhost:$${SDE_REDIS_PORT:-6379}/15 \
            SDE_TEST_S3_ENDPOINT=http://localhost:$${SDE_S3_PORT:-9000} \
            AWS_ACCESS_KEY_ID=sde-dev AWS_SECRET_ACCESS_KEY=sde-dev-secret AWS_DEFAULT_REGION=us-east-1

.PHONY: test test-unit test-e2e test-all deps-up test-infra up up-agent authoring-image down logs ps reconcile

test:            ## infrastructure tests on SQLite + files (no services needed)
	$(PYTHON) -m pytest tests/shared -q

test-unit:       ## every component's unit tests (no services, no model calls)
	$(PYTHON) -m pytest tests/shared api generator evaluation -q

test-e2e:        ## the whole pipeline as processes, on SQLite and (if up) the compose cluster; no model calls
	$(PYTHON) -m pytest tests/e2e -q

test-all: deps-up  ## everything above, with the cluster up
	$(TEST_ENV) $(PYTHON) -m pytest tests/shared api generator evaluation tests/e2e -q

deps-up:         ## Postgres + Redis + S3 (RustFS) only
	$(COMPOSE) up -d --wait postgres redis s3

test-infra: deps-up  ## the same tests on SQLite, Postgres and Postgres+Redis, plus S3
	$(TEST_ENV) $(PYTHON) -m pytest tests/shared -q

up:              ## the whole stack, workers included (oneshot authoring)
	$(COMPOSE) --profile app up -d --build

authoring-image: ## build the agent-mode authoring image into the local Docker
	docker build -t $${SDE_AUTHORING_IMAGE:-sde-authoring:dev} authoring/

up-agent: authoring-image  ## the whole stack with SDE_AUTHOR_MODE=agent (generation on worker-runner)
	SDE_AUTHOR_MODE=agent SDE_GENERATION_CMD="python -m shared.jobs.reconciler" \
	SDE_RUNNER_GENERATION_CMD="python -m generator.service generation" \
	$(COMPOSE) --profile app up -d --build

down:            ## stop everything (data volumes are kept; add -v to drop them)
	$(COMPOSE) --profile app down

logs:
	$(COMPOSE) --profile app logs -f

ps:
	$(COMPOSE) --profile app ps

reconcile:       ## one reconciler pass against the current env's backends
	$(PYTHON) -m shared.jobs.reconciler --once

# --- AWS deployment (deploy/aws_ops.py; account 284460774261, us-east-2) --------
AWS_OPS := $(PYTHON) deploy/aws_ops.py

.PHONY: aws-bootstrap aws-plan aws-apply aws-secrets aws-author-mode aws-deploy aws-smoke aws-status aws-logs aws-stop aws-start aws-destroy aws-up

aws-bootstrap:   ## create the Terraform state bucket (once)
	$(AWS_OPS) bootstrap
aws-plan:
	$(AWS_OPS) plan
aws-apply:       ## VPC, RDS Postgres, ElastiCache Redis, S3, runner EC2
	$(AWS_OPS) apply
aws-secrets:     ## OPENROUTER_API_KEY from .env → SSM SecureString
	$(AWS_OPS) secrets
aws-author-mode: ## make aws-author-mode MODE=agent|oneshot (applies on the next aws-deploy)
	$(AWS_OPS) author-mode $(MODE)
aws-deploy:      ## ship REF (default HEAD) to the runner: make aws-deploy REF=<branch|sha>
	$(AWS_OPS) deploy $(or $(REF),HEAD)
aws-smoke:       ## queue + artifacts round trip on the runner against RDS/Redis/S3
	$(AWS_OPS) smoke
aws-status:
	$(AWS_OPS) status
aws-logs:        ## make aws-logs [SERVICE=worker-runner]
	$(AWS_OPS) logs $(SERVICE)
aws-stop:        ## stop the runner (the expensive part) when nothing is running
	$(AWS_OPS) stop
aws-start:
	$(AWS_OPS) start
aws-destroy:     ## delete everything Terraform created (asks for confirmation)
	$(AWS_OPS) destroy
aws-up: aws-bootstrap aws-apply aws-secrets aws-deploy aws-smoke  ## all of the above, in order
