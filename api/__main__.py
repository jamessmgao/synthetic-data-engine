"""Run the API service: `SDE_API_KEY=... python -m api [--host H] [--port P]`."""

import argparse
import sys

import uvicorn

from .app import create_app
from .config import Settings


def main() -> None:
    parser = argparse.ArgumentParser(prog="python -m api")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()

    settings = Settings.from_env()
    if not settings.api_key:
        sys.exit("Set SDE_API_KEY: requests must send it as the X-Api-Key header.")
    uvicorn.run(create_app(settings), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
