FROM python:3.12-slim-bookworm
WORKDIR /app
COPY --chown=10001:10001 parser.py engine.py renderer.py default.json /app/
RUN useradd --uid 10001 agent && chown -R 10001:10001 /app
USER agent
CMD ["python3", "/app/renderer.py", "/app/default.json"]
