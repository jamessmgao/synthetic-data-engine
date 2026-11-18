# The authoring image (docs/AGENTIC_AUTHORING_PLAN.md §3.4): the container a
# world / create agent works in. Build context = the repo root:
#
#   docker build -f authoring/Dockerfile -t sde-authoring:dev .
#
# The agent builds and runs worlds in their own containers through the host's
# Docker socket (mounted by the authoring task's docker-compose.yaml). Nothing
# here may rely on host bind mounts: through the socket, a path in this
# container is not a path on the host. Builds upload their context, and files
# move in and out of containers with `docker cp`.

FROM python:3.12-slim-bookworm@sha256:935c3c5b12357a7e10caa9c1e6ca5692344d97cf229832ef584be2cec9fc03df

ARG TARGETARCH
ARG GO_VERSION=1.22.12
ARG JDK_VERSION=21.0.5_11
ARG JDK_TAG=jdk-21.0.5%2B11

ENV DEBIAN_FRONTEND=noninteractive PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1

# C toolchain, SQLite, a Postgres client, git and the Docker CLI.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      build-essential make git curl ca-certificates gnupg jq sqlite3 postgresql-client procps \
 && install -m 0755 -d /etc/apt/keyrings \
 && curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc \
 && echo "deb [signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian bookworm stable" \
      > /etc/apt/sources.list.d/docker.list \
 && apt-get update \
 && apt-get install -y --no-install-recommends docker-ce-cli docker-buildx-plugin \
 && rm -rf /var/lib/apt/lists/*

# Go and a JDK, pinned, for the agent's own builds and experiments (each world
# still builds from its own pinned base image).
RUN case "$TARGETARCH" in arm64) goarch=arm64; jdkarch=aarch64 ;; *) goarch=amd64; jdkarch=x64 ;; esac \
 && curl -fsSL "https://go.dev/dl/go${GO_VERSION}.linux-${goarch}.tar.gz" | tar -xz -C /usr/local \
 && mkdir -p /opt/jdk \
 && curl -fsSL "https://github.com/adoptium/temurin21-binaries/releases/download/${JDK_TAG}/OpenJDK21U-jdk_${jdkarch}_linux_hotspot_${JDK_VERSION}.tar.gz" \
      | tar -xz -C /opt/jdk --strip-components=1
ENV PATH=/usr/local/go/bin:/opt/jdk/bin:$PATH JAVA_HOME=/opt/jdk

# harbor: materialize builds task.toml through Harbor's own TaskConfig (and brings pydantic).
RUN pip install "pytest==8.4.1" "harbor==0.26.0" "toml==0.10.2"

# Our generator and validator, for the self-check CLIs (sde-world, sde-task).
COPY generator/ /opt/sde/generator/
COPY validator/ /opt/sde/validator/
COPY authoring/ /opt/sde/authoring/
RUN chmod +x /opt/sde/authoring/bin/* && ln -s /opt/sde/authoring/bin/* /usr/local/bin/

WORKDIR /work
