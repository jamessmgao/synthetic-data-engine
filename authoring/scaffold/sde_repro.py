"""The repro harness for a world (stdlib only; vendor it into environment/ or repro/).

repro/repro.sh must exit 1 when an invariant is violated and 0 when every one holds,
the same way every run. This turns that into three pieces you write:

    from sde_repro import repro_main

    def run(sequence):           # replay the recorded steps against the real system
        ...; return state        # whatever the invariants need to look at

    INVARIANTS = {
        "acknowledged writes survive a restart": lambda s: s.acked <= s.durable,
        "no order is charged twice": lambda s: all(n <= 1 for n in s.charges.values()),
    }

    if __name__ == "__main__":
        repro_main(run, INVARIANTS)   # reads --sequence (default repro/sequence.json)

An invariant returns True (holds), False, or a string describing the violation. Exit
codes: 1 if any invariant is violated, 0 if all hold, 2 if the harness itself fails
(an exception in `run` or an invariant, or an unreadable sequence). The pipeline treats
anything but 0/1 as a broken repro, so a crash never passes for "the bug is fixed".
"""

import argparse
import json
import sys
import traceback
from collections.abc import Callable
from pathlib import Path
from typing import Any

Invariant = Callable[[Any], bool | str | None]


def check(state: Any, invariants: dict[str, Invariant]) -> list[str]:
    """The violated invariants, each with its message."""
    violated = []
    for name, inv in invariants.items():
        verdict = inv(state)
        if verdict is True or verdict is None:
            continue
        violated.append(name if verdict is False else f"{name}: {verdict}")
    return violated


def run_repro(sequence: Any, run: Callable[[Any], Any], invariants: dict[str, Invariant]) -> int:
    """Replay, check, print the verdict; returns the exit code (0 holds, 1 violated, 2 broken)."""
    try:
        state = run(sequence)
        violated = check(state, invariants)
    except Exception:
        traceback.print_exc()
        print("REPRO BROKEN: the harness raised (exit 2)")
        return 2
    for v in violated:
        print(f"VIOLATED: {v}")
    if not violated:
        print(f"OK: {len(invariants)} invariant(s) hold")
    return 1 if violated else 0


def repro_main(run: Callable[[Any], Any], invariants: dict[str, Invariant],
               default: str = "repro/sequence.json", argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(description="replay a recorded sequence and check the invariants")
    p.add_argument("--sequence", default=default, help="JSON file with the recorded sequence")
    args = p.parse_args(argv)
    try:
        sequence = json.loads(Path(args.sequence).read_text())
    except (OSError, json.JSONDecodeError) as e:
        print(f"REPRO BROKEN: cannot read the sequence {args.sequence}: {e}")
        sys.exit(2)
    sys.exit(run_repro(sequence, run, invariants))
