#!/bin/bash
# EC2 user data for the read-only showcase (docs/SHOWCASE.md): the API on a SQLite file, the
# dashboard, and one Cloudflare quick tunnel to the dashboard. The API has no public address.
# Fill in the three values, then pass this file as --user-data.
BUCKET="<artifacts-bucket>"            # holds showcase/images.tar.gz, showcase/sde.db.gz and the artifacts
API_KEY="<a random string>"            # shared by the API and the dashboard only
DEV_BATCHES="<comma list, or empty>"   # batches kept out of the Prod numbers (SDE_DEV_BATCHES)
set -x
exec > /var/log/sde-showcase.log 2>&1
fallocate -l 1G /swapfile && chmod 600 /swapfile && mkswap /swapfile && swapon /swapfile && echo '/swapfile none swap sw 0 0' >> /etc/fstab
dnf install -y docker
systemctl enable --now docker
mkdir -p /data /opt/sde
aws s3 cp --only-show-errors "s3://$BUCKET/showcase/images.tar.gz" /opt/sde/images.tar.gz
gunzip -c /opt/sde/images.tar.gz | docker load && rm -f /opt/sde/images.tar.gz
aws s3 cp --only-show-errors "s3://$BUCKET/showcase/sde.db.gz" /opt/sde/sde.db.gz
[ -f /data/sde.db ] || gunzip -c /opt/sde/sde.db.gz > /data/sde.db
chown -R 10001:10001 /data
docker network create sde
docker run -d --name api --restart unless-stopped --network sde --memory 450m \
  -v /data:/data -e SDE_WORK_DIR=/data -e S3_BUCKET="$BUCKET" -e AWS_DEFAULT_REGION=us-east-2 \
  -e SDE_API_KEY="$API_KEY" -e SDE_DEV_BATCHES="$DEV_BATCHES" -e SDE_BUDGET_USD=none \
  sde-pipeline:showcase python -m api --host 0.0.0.0 --port 8000
docker run -d --name dashboard --restart unless-stopped --network sde --memory 300m \
  -e SDE_URL=http://api:8000 -e SDE_API_KEY="$API_KEY" sde-dashboard:showcase
docker run -d --name tunnel --restart unless-stopped --network sde --memory 100m \
  cloudflare/cloudflared:2026.10.7 tunnel --no-autoupdate --url http://dashboard:3100
echo BOOTSTRAP_DONE
