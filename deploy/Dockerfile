# Pipeline image, two targets (build context: the repo root).
#
#   control  API, Job Service, generation worker, reconciler. No Docker access.
#   runner   build+validate and executor workers: adds the Docker CLI and Harbor.
#            Runs task containers through the host's Docker socket, so it belongs
#            on a dedicated runner host, never next to the API in production.

FROM python:3.12.11-slim-bookworm AS control
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1
WORKDIR /app
COPY shared/requirements.txt shared/requirements.txt
COPY generator/requirements.txt generator/requirements.txt
RUN pip install -r shared/requirements.txt -r generator/requirements.txt
COPY . .
RUN useradd --create-home --uid 10001 sde && chown -R sde /app
USER sde
CMD ["python", "-m", "shared.jobs.reconciler"]

FROM control AS runner
USER root
ARG HARBOR_VERSION=0.26.0
ARG UV_VERSION=0.9.5
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates curl gnupg \
 && install -m 0755 -d /etc/apt/keyrings \
 && curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc \
 && echo "deb [signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian bookworm stable" \
      > /etc/apt/sources.list.d/docker.list \
 && apt-get update \
 && apt-get install -y --no-install-recommends docker-ce-cli docker-compose-plugin \
 && rm -rf /var/lib/apt/lists/*
# Harbor in its own tool environment so its dependencies can't clash with ours.
# --force: the control stage may already have a pip-installed harbor on PATH.
RUN pip install "uv==${UV_VERSION}" \
 && UV_TOOL_DIR=/opt/uv-tools UV_TOOL_BIN_DIR=/usr/local/bin uv tool install --force "harbor==${HARBOR_VERSION}"
# Stays root: the runner talks to the host Docker daemon through its socket.
