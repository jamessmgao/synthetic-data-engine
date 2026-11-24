-include deploy/local.env
# Local development. The default backends need nothing (SQLite + files under work/);
# the compose stack runs the production backends (Postgres, Redis, S3) locally.

COMPOSE := docker compose -f deploy/compose.dev.yaml
PYTHON ?= .venv/bin/python

TEST_ENV := SDE_TEST_DATABASE_URL=postgresql://sde:sde@localhost:$${SDE_PG_PORT:-5432}/sde_test \
            SDE_TEST_REDIS_URL=redis://localhost:$${SDE_REDIS_PORT:-6379}/15 \
            SDE_TEST_S3_ENDPOINT=http://localhost:$${SDE_S3_PORT:-9000} \
            AWS_ACCESS_KEY_ID=sde-dev AWS_SECRET_ACCESS_KEY=sde-dev-secret AWS_DEFAULT_REGION=us-east-1

# Every unit-test suite: the packages' own tests plus tests/shared and tests/deploy.
UNIT_TESTS := tests/shared tests/deploy api generator evaluation validator authoring experiments

.PHONY: test test-unit test-e2e test-all deps-up test-infra up up-agent authoring-image down logs ps reconcile

test:            ## infrastructure tests on SQLite + files (no services needed)
	$(PYTHON) -m pytest tests/shared -q

test-unit:       ## every component's unit tests (no services, no model calls)
	$(PYTHON) -m pytest $(UNIT_TESTS) -q

test-e2e:        ## the whole pipeline as processes, on SQLite and (if up) the compose cluster; no model calls
	$(PYTHON) -m pytest tests/e2e -q

test-all: deps-up  ## everything above, with the cluster up
	$(TEST_ENV) $(PYTHON) -m pytest $(UNIT_TESTS) tests/e2e -q

deps-up:         ## Postgres + Redis + S3 (RustFS) only
	$(COMPOSE) up -d --wait postgres redis s3

test-infra: deps-up  ## the same tests on SQLite, Postgres and Postgres+Redis, plus S3
	$(TEST_ENV) $(PYTHON) -m pytest tests/shared -q

up:              ## the whole stack, workers included (oneshot authoring)
	rm -f deploy/make.env
	$(COMPOSE) --profile app up -d --build

authoring-image: ## build the agent-mode authoring image into the local Docker (context: the repo)
	docker build -f authoring/Dockerfile -t $(or $(SDE_AUTHORING_IMAGE),sde-authoring:dev) .

up-agent: authoring-image  ## the whole stack with SDE_AUTHOR_MODE=agent (generation on worker-runner)
	echo SDE_AUTHOR_MODE=agent > deploy/make.env
	SDE_GENERATION_CMD="python -m shared.jobs.reconciler" \
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

# --- Local Kubernetes (k3d cluster "sde", context k3d-sde; scripts/k8s-local.sh) ---
K8S := bash scripts/k8s-local.sh

.PHONY: k8s-up k8s-deploy k8s-status k8s-logs k8s-down

k8s-up:          ## create (or start) the k3d cluster, build + import sde-pipeline:local
	$(K8S) up
k8s-deploy:      ## rebuild + import the image, apply infra/k8s/overlays/local, wait
	$(K8S) deploy
k8s-status:
	$(K8S) status
k8s-logs:        ## make k8s-logs SVC=api|control|validate|executor|generation|postgres|redis|s3
	SVC=$(SVC) $(K8S) logs
k8s-down:        ## delete the cluster and everything in it
	$(K8S) down

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

# --- Local Kubernetes (k3d cluster "sde", docs/DEPLOY.md §8) ---------------------
.PHONY: k8s-test

k8s-test:        ## chaos test on the k3d cluster (mock mode): kill an executor mid-rollout, restart Postgres
	$(PYTHON) scripts/k8s_test.py $(ARGS)

# K3s across the runners instead of compose (scripts/k8s_aws.py; docs/KUBERNETES.md).
K8S_AWS := $(PYTHON) scripts/k8s_aws.py

.PHONY: aws-k8s-up aws-k8s-deploy aws-k8s-deploy-ui aws-k8s-status aws-k8s-logs aws-k8s-test aws-k8s-test-log aws-k8s-down

aws-k8s-up:      ## stop compose; K3s server + agents; image on every node; KEDA
	$(K8S_AWS) up $(or $(REF),HEAD)
aws-k8s-deploy:  ## make aws-k8s-deploy [REF=<branch|sha>] [OVERLAY=aws|aws-prod] [ONLY=ui,api,…]: ship, rebuild + import, apply (ONLY: just those components)
	$(K8S_AWS) deploy $(or $(REF),HEAD) --overlay $(or $(OVERLAY),aws) $(if $(ONLY),--only $(ONLY))
aws-k8s-deploy-ui: ## make aws-k8s-deploy-ui [REF=…]: only the dashboard (aws-prod); restarts nothing else
	$(K8S_AWS) deploy $(or $(REF),HEAD) --overlay aws-prod --only ui
aws-k8s-status:
	$(K8S_AWS) status
aws-k8s-logs:    ## make aws-k8s-logs SVC=api|control|validate|executor|generation|postgres|redis|s3
	$(K8S_AWS) logs $(or $(SVC),executor)
aws-k8s-public:  ## the public HTTPS links, api: and dashboard: (Cloudflare quick tunnels; aws-prod overlays)
	$(K8S_AWS) public
aws-k8s-test:    ## make aws-k8s-test [ARGS="--tasks 2"]: scripts/k8s_test.py on the server (systemd unit)
	$(K8S_AWS) test $(ARGS)
aws-k8s-test-log:
	$(K8S_AWS) test-log
aws-k8s-down:    ## uninstall K3s on every runner (then `make aws-deploy` restores compose)
	$(K8S_AWS) down

# --- the dashboard (web/): status and results of every task -------------------------------
.PHONY: ui ui-aws

ui:              ## the dashboard on http://localhost:3100, reading web/.env.local (SDE_URL, SDE_API_KEY)
	cd web && npm install --no-audit --no-fund && npm run dev
ui-aws:          ## point the dashboard at the AWS cluster (tunnel URL + SSM key into web/.env.local), then run it
	@url=$$($(K8S_AWS) public | grep '^api:' | grep -o 'https://[a-z0-9-]*\.trycloudflare\.com'); \
	key=$$(aws ssm get-parameter --region us-east-2 --name /sde/api_key --with-decryption --query Parameter.Value --output text); \
	test -n "$$url" -a -n "$$key" || { echo "no tunnel URL or API key"; exit 1; }; \
	printf 'SDE_URL=%s\nAQ_API_KEY=%s\n' "$$url" "$$key" > web/.env.local; echo "web/.env.local -> $$url"
	$(MAKE) ui
