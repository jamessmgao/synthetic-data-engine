FROM python:3.12-slim-bookworm

ENV PYTHONUNBUFFERED=1
WORKDIR /app
COPY pyproject.toml pyproject.toml
COPY reservation reservation

RUN useradd --create-home --shell /bin/bash agent \
    && chown -R agent:agent /app

USER agent
CMD ["python", "-m", "reservation.cli", "--help"]
