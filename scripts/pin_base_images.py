"""
Resolve the digest of every allowed base image and write config/base_images.json.

Generated Dockerfiles may only start FROM an image in this table; materialize.py
rewrites `FROM <tag>` to `FROM <tag>@<digest>` so builds are reproducible and
`harbor lint` (unpinned-image) passes. The model never writes a digest itself.

Usage: python scripts/pin_base_images.py   (needs docker buildx and network)
"""

import json
import subprocess
from pathlib import Path

IMAGES = [
    "python:3.12-slim-bookworm",
    "python:3.11-slim-bookworm",
    "python:3.13-slim-bookworm",
    "debian:bookworm-slim",
    "ubuntu:24.04",
    "node:22-bookworm-slim",
    "golang:1.23-bookworm",
    "rust:1.83-slim-bookworm",
    "eclipse-temurin:21-jdk-jammy",
    "gcc:14-bookworm",
    "postgres:16-bookworm",
]

OUT = Path(__file__).resolve().parent.parent / "config" / "base_images.json"


def digest(image: str) -> str:
    out = subprocess.run(
        ["docker", "buildx", "imagetools", "inspect", image, "--format", "{{json .Manifest.Digest}}"],
        capture_output=True, text=True, check=True, timeout=120,
    ).stdout
    return json.loads(out)


if __name__ == "__main__":
    table = {image: digest(image) for image in IMAGES}
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text(json.dumps(table, indent=2, sort_keys=True) + "\n")
    for image, d in sorted(table.items()):
        print(f"{image:32} {d}")
