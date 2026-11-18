#!/bin/bash
# Runs ON the runner (sent through SSM by deploy/aws_ops.py). Unpacks one release,
# writes runtime.env from SSM Parameter Store, and brings the app up.
#
# Usage: remote-deploy.sh <git-sha> <artifacts-bucket> <region> <docker-slots>
set -euo pipefail

sha=$1 bucket=$2 region=$3 slots=$4
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
  # worker-control always runs the one reconciler, with the generation worker if present.
  if [ -e "$release/generator/worker.py" ]; then
    echo "SDE_GENERATE_CMD=python -m generator.service generate --with-reconciler"
  else
    echo "SDE_GENERATE_CMD=python -m shared.jobs.reconciler"
  fi
  cmd job_service SDE_JOB_SERVICE_CMD "python -m job_service run"
  cmd validator/gates.py SDE_VALIDATE_CMD "python -m validator.service validate"
  cmd evaluation/executor.py SDE_EXECUTOR_CMD "python -m evaluation.executor"
  cmd api SDE_API_CMD "uvicorn api.app:app --host 0.0.0.0 --port 8000"
} > /opt/sde/runtime.env.tmp
mv /opt/sde/runtime.env.tmp /opt/sde/runtime.env

cd "$release"
docker compose --env-file /opt/sde/runtime.env -f deploy/compose.prod.yaml up -d --build --remove-orphans
docker compose --env-file /opt/sde/runtime.env -f deploy/compose.prod.yaml ps
echo "components:"; grep '_CMD=' /opt/sde/runtime.env
echo "deployed $sha"
