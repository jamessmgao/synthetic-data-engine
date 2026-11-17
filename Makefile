# Local development. The default backends need nothing (SQLite + files under work/);
# the compose stack runs the production backends (Postgres, Redis, S3) locally.

COMPOSE := docker compose -f deploy/compose.dev.yaml
PYTHON ?= .venv/bin/python

TEST_ENV := SDE_TEST_DATABASE_URL=postgresql://sde:sde@localhost:$${SDE_PG_PORT:-5432}/sde_test \
            SDE_TEST_REDIS_URL=redis://localhost:$${SDE_REDIS_PORT:-6379}/15 \
            SDE_TEST_S3_ENDPOINT=http://localhost:$${SDE_S3_PORT:-9000} \
            AWS_ACCESS_KEY_ID=sde-dev AWS_SECRET_ACCESS_KEY=sde-dev-secret AWS_DEFAULT_REGION=us-east-1

.PHONY: test deps-up test-infra up down logs ps reconcile

test:            ## infrastructure tests on SQLite + files (no services needed)
	$(PYTHON) -m pytest tests/shared -q

deps-up:         ## Postgres + Redis + S3 (RustFS) only
	$(COMPOSE) up -d --wait postgres redis s3

test-infra: deps-up  ## the same tests on SQLite, Postgres and Postgres+Redis, plus S3
	$(TEST_ENV) $(PYTHON) -m pytest tests/shared -q

up:              ## the whole stack, workers included
	$(COMPOSE) --profile app up -d --build

down:            ## stop everything (data volumes are kept; add -v to drop them)
	$(COMPOSE) --profile app down

logs:
	$(COMPOSE) --profile app logs -f

ps:
	$(COMPOSE) --profile app ps

reconcile:       ## one reconciler pass against the current env's backends
	$(PYTHON) -m shared.jobs.reconciler --once
