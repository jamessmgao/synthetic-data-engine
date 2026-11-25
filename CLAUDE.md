# synthetic-data-engine

## Layout

- `shared/storage/`: the database, artifact files and S3 (`sql`, `queue`, `artifacts`, `budget`, `dbretry`).
- `shared/jobs/`: the queue: claim, lease, settle, hand-off, the reconciler and the generic service loop.
- `shared/contracts/`: shared names and settings: job types, statuses, outcomes, timing, the band rules.
- `shared/taxonomy/`: families, mechanisms, domains, the sampler and world selection.
- `shared/taskformat/`: the task schema, the code that writes a task directory, and the checks on a task or a world.
- `shared/llm/`: the model client and its mock.
- `api/`: the HTTP API and task submission.
- `generator/`: the Generation Service (`python -m generator.service`).
- `validator/`: Build + Validate (`python -m validator.service`).
- `evaluation/`: the Executor and the Trace Parser.
- `authoring/`: the authoring image and the `sde-task` and `sde-world` commands.
- `scripts/`, `experiments/`, `deploy/`, `tests/`: tooling.

## Rules

1. A service (`api`, `generator`, `validator`, `evaluation`, `authoring`) never imports another service. This includes imports inside a function and `importlib.import_module`, `__import__` and `sys.modules[...]`.
2. Nothing under `shared/` imports a service.
3. Inside `shared/`, dependencies go one way: `contracts` imports nothing from the repo; `storage` may import `contracts`; `jobs` may import `contracts` and `storage`; `taxonomy` may import `storage`; `llm` may import `contracts`; `taskformat` may import `contracts` and `llm`.
4. Tooling (`scripts/`, `experiments/`, `deploy/`, `tests/` and `test_*.py` files anywhere) may import anything. Services and `shared/` never import tooling.

`tests/test_boundaries.py` enforces this.

New shared code goes in the closest `shared/` subpackage, not in a new import from one service to another.

## Tests

- `make test-unit`: every unit test, no services needed.
- The full suite: `.venv/bin/python -m pytest -q tests evaluation generator api validator authoring experiments`
