#!/usr/bin/env python3
"""Delete old sde-pipeline release images from this node's K3s containerd (run by the deploy
after each import, as root, stdlib only).

Why: each release adds a ~1.1 GB image to every node and nothing removed them; at 85% disk the
kubelet's image GC deletes the least recently used images no container runs, and on
2026-11-19 that was the release just imported (no pod had started on it yet), so its pods
failed with ErrImageNeverPull while ten old releases stayed.

Kept: the images the KEEP tags (argv: the current and the previous release) point at, and every
image a container uses (running or exited). Only images all of whose tags are
docker.io/library/sde-pipeline:* are considered: never sde-authoring, sde-dashboard or base
images. Deletion is by image ID (CRI removes every tag of an ID, so a kept ID keeps them all).
"""
import json
import os
import subprocess
import sys

REPO = "docker.io/library/sde-pipeline:"


def to_drop(images: list[dict], containers: list[dict], keep_tags: set[str]) -> list[dict]:
    """The sde-pipeline images to delete, from `crictl images -o json` / `crictl ps -a -o json`."""
    strip = lambda i: (i or "").removeprefix("sha256:")
    used = {strip(c.get("imageRef")) for c in containers} | {strip((c.get("image") or {}).get("image"))
                                                            for c in containers}
    ours = [i for i in images if i.get("repoTags") and all(t.startswith(REPO) for t in i["repoTags"])]
    keep = {strip(i["id"]) for i in ours if any(t[len(REPO):] in keep_tags for t in i["repoTags"])}
    return [i for i in ours if strip(i["id"]) not in keep | used]


def main(keep_tags: list[str]) -> None:
    env = {**os.environ, "HOME": os.environ.get("HOME") or "/root"}  # crictl warns (and exits 1) without one

    def crictl(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(["k3s", "crictl", *args], capture_output=True, text=True, env=env)

    images = json.loads(crictl("images", "-o", "json").stdout or "{}").get("images", [])
    containers = json.loads(crictl("ps", "-a", "-o", "json").stdout or "{}").get("containers", [])
    drop = to_drop(images, containers, set(keep_tags))
    for image in drop:
        crictl("rmi", image["id"])  # its exit code is unreliable; the listing below is the truth
    left = {i["id"] for i in json.loads(crictl("images", "-o", "json").stdout or "{}").get("images", [])}
    gone = [i for i in drop if i["id"] not in left]
    gb = sum(int(i.get("size", 0)) for i in gone) / 1e9
    print(f"pruned {len(gone)} of {len(drop)} old sde-pipeline images ({gb:.1f} GB); kept {', '.join(keep_tags)}"
          " and every image a container uses")


if __name__ == "__main__":
    main([t for t in sys.argv[1:] if t])
