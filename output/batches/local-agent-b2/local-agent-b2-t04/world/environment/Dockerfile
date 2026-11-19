FROM python:3.12-slim-bookworm
WORKDIR /app
COPY service.py store.py provider.py /app/
RUN useradd --system --uid 1000 agent \
    && mkdir -p /app/state \
    && chown -R agent:agent /app
USER agent
CMD ["python3", "/app/service.py"]
