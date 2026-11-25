"""Cleaning up after a Harbor run: the compose networks its trials leave behind."""

import subprocess
from pathlib import Path


def remove_trial_networks(job_dir: Path) -> list[str]:
    """Remove the compose network of every trial under `job_dir`. Harbor names a trial's
    compose project `<trial dir name, lowercased>__env`, and its network
    `<project>_default`. A Harbor that was killed or crashed never runs compose down,
    and the leaked networks exhausted Docker's address pools (local-agent-b2: "all
    predefined address pools have been fully subnetted"). `docker network rm` refuses a
    network still in use, so a live trial's network is left alone. Returns the
    networks it asked to remove."""
    if not job_dir.is_dir():
        return []
    names = [f"{trial.name.lower()}__env_default" for trial in sorted(job_dir.iterdir()) if trial.is_dir()]
    for name in names:
        subprocess.run(["docker", "network", "rm", name], capture_output=True, text=True)
    return names
