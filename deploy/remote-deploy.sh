#!/bin/bash
# Runs ON the runner (sent through SSM by deploy/aws_ops.py). Unpacks one release,
# writes runtime.env from SSM Parameter Store, and brings the app up.
#
# Usage: remote-deploy.sh <git-sha> <artifacts-bucket> <region> <docker-slots> <role>
#   role=control  api + worker-control (Generation Service, Trace Parser, the one reconciler)
#                 + worker-runner
#   role=runner   worker-runner only (Docker jobs)
set -euo pipefail

sha=$1 bucket=$2 region=$3 slots=$4 role=$5
release=/opt/sde/releases/$sha

until [ -f /opt/sde/.bootstrapped ]; do echo "waiting for first-boot setup"; sleep 5; done

# Releases are immutable: unpack once, into a temp dir renamed into place.
if [ ! -d "$release" ]; then
  tmp=$(mktemp -d "/opt/sde/releases/.$sha.XXXX")
  aws s3 cp --region "$region" "s3://$bucket/releases/$sha.tar.gz" - | tar -xz -C "$tmp"
  mv "$tmp" "$release"
fi
ln -sfn "$release" /opt/sde/current

param() {
  aws ssm get-parameter --region "$region" --name "$1" --with-decryption \
    --query Parameter.Value --output text
}
# An optional parameter, with a default when it isn't set.
param_or() { param "$1" 2>/dev/null || echo "$2"; }

# Author mode (docs/AGENTIC_AUTHORING_PLAN.md), set with `aws_ops.py author-mode`.
author_mode=$(param_or /sde/author_mode oneshot)

# The authoring image, when this release has one: built into each runner's Docker
# (context = the release root: it bakes in generator/ and validator/), tagged by the
# content of those dirs so a change rebuilds it and an unchanged one is reused.
authoring_image=""
if [ -f "$release/authoring/Dockerfile" ]; then
  tag=$(cd "$release" && find authoring generator validator -type f ! -path '*/__pycache__/*' -print0 \
        | sort -z | xargs -0 sha256sum | sha256sum | cut -c1-12)
  authoring_image="sde-authoring:$tag"
  docker image inspect "$authoring_image" >/dev/null 2>&1 \
    || docker build -q -f "$release/authoring/Dockerfile" -t "$authoring_image" "$release" >/dev/null
fi
# Run a component only if its code is in this release.
cmd() { if [ -e "$release/$1" ]; then echo "$2=$3"; else echo "$2="; fi; }

umask 077
{
  echo "DATABASE_URL=$(param /sde/database_url)"
  echo "REDIS_URL=$(param /sde/redis_url)"
  echo "S3_BUCKET=$(param /sde/s3_bucket)"
  echo "AWS_DEFAULT_REGION=$region"
  echo "OPENROUTER_API_KEY=$(param /sde/openrouter_api_key)"
  echo "OPENROUTER_API_BASE=https://gateway.example.com/openrouter/api/v1"
  echo "SDE_WORK_DIR=/opt/sde/work"
  echo "SDE_DOCKER_SLOTS=$slots"
  echo "SDE_RELEASE=$sha"
  echo "SDE_AUTHOR_MODE=$author_mode"
  echo "SDE_AUTHORING_IMAGE=$authoring_image"
  echo "DOCKER_GID=$(stat -c %g /var/run/docker.sock)"
  # worker-control always runs the one reconciler. The Generation Service runs next
  # to it in oneshot mode, and on every runner's worker-runner in agent mode (each
  # world / create / revise job is a Harbor trial there, holding a Docker slot).
  if [ ! -e "$release/generator/worker.py" ]; then
    echo "SDE_GENERATION_CMD=python -m shared.jobs.reconciler"
    echo "SDE_RUNNER_GENERATION_CMD="
  elif [ "$author_mode" = agent ]; then
    echo "SDE_GENERATION_CMD=python -m shared.jobs.reconciler"
    echo "SDE_RUNNER_GENERATION_CMD=python -m generator.service generation"
  else
    echo "SDE_GENERATION_CMD=python -m generator.service generation --with-reconciler"
    echo "SDE_RUNNER_GENERATION_CMD="
  fi
  cmd evaluation/trace_parser_service.py SDE_TRACE_PARSER_CMD "python -m evaluation.trace_parser_service"
  cmd validator/gates.py SDE_VALIDATE_CMD "python -m validator.service validate"
  cmd evaluation/executor.py SDE_EXECUTOR_CMD "python -m evaluation.executor"
  cmd api SDE_API_CMD "python -m api --host 0.0.0.0 --port 8000"
  echo "SDE_API_KEY=$(param /sde/api_key)"
} > /opt/sde/runtime.env.tmp
mv /opt/sde/runtime.env.tmp /opt/sde/runtime.env

# Every app container runs as uid 10001 (the image's `sde` user): it must own the
# shared dirs. Only entries not yet owned are touched, so this stays cheap.
find /opt/sde/work /opt/sde/output ! -user 10001 -exec chown 10001:10001 {} +

services=""  # all of them
[ "$role" = runner ] && services="worker-runner"

cd "$release"
# Build quietly: SSM keeps only the first 24,000 characters of output, and a full
# build log would push the result lines below out of it.
# shellcheck disable=SC2086  # $services is intentionally split (or empty)
docker compose --env-file /opt/sde/runtime.env -f deploy/compose.prod.yaml build --quiet $services
# shellcheck disable=SC2086
docker compose --env-file /opt/sde/runtime.env -f deploy/compose.prod.yaml up -d --remove-orphans $services
docker compose --env-file /opt/sde/runtime.env -f deploy/compose.prod.yaml ps
echo "components:"; grep -E '_CMD=|SDE_AUTHOR_MODE=|SDE_AUTHORING_IMAGE=' /opt/sde/runtime.env
echo "deployed $sha as $role"
