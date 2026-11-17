"""Run the API service: `SDE_API_KEY=... python -m api [--host H] [--port P]`.

Backends come from the environment (DATABASE_URL, REDIS_URL, S3_*, SDE_WORK_DIR);
see shared/storage/__init__.py.
"""

import argparse
import os
import sys

import uvicorn

from .app import create_app


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m api")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    if not os.environ.get("SDE_API_KEY"):
        sys.exit("Set SDE_API_KEY: requests must send it as the X-Api-Key header.")
    uvicorn.run(create_app(), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
