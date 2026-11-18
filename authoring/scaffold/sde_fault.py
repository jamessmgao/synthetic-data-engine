"""Planned faults for a world (stdlib only; vendor it into environment/).

Put a named fault point wherever the system could crash, and route messages and
writes through the injector. Which faults fire is a plan (a dict, a JSON file, or the
SDE_FAULTS environment variable), so a repro replays the same failure every time:

    faults = Faults.from_env()   # SDE_FAULTS='{"crash": ["after_commit"], "drop": {"ack": [2]}}'

    db.commit()
    faults.point("after_commit")               # raises Crash on the planned hit
    for m in faults.deliver("ack", ack):       # [] dropped, [ack, ack] duplicated
        send(m)
    faults.write(f, record, at="wal_append")   # a partial write, then Crash

Plan keys (all optional):
    crash:     ["point", "point#3", ...]  crash at a point (on its Nth hit with #N, else the 1st)
    drop:      {"kind": [n, ...]}          drop the nth message of that kind (1-based)
    duplicate: {"kind": [n, ...]}          deliver the nth message of that kind twice
    partial:   {"point": bytes_kept}       a write at that point keeps only the first bytes, then crashes

`faults.fired` lists what happened, in order, for the repro's log. A restart is the
caller's: catch Crash, rebuild the component from its persisted state, carry on.
"""

import json
import os
from collections import Counter
from pathlib import Path
from typing import IO, Any


class Crash(Exception):
    """A planned crash at a named point."""

    def __init__(self, point: str, hit: int):
        super().__init__(f"planned crash at {point}#{hit}")
        self.point, self.hit = point, hit


class Faults:
    def __init__(self, plan: dict[str, Any] | None = None):
        plan = plan or {}
        unknown = set(plan) - {"crash", "drop", "duplicate", "partial"}
        if unknown:
            raise ValueError(f"unknown fault plan keys: {sorted(unknown)}")
        self.crash: set[tuple[str, int]] = set()
        for spec in plan.get("crash", []):
            point, _, n = spec.partition("#")
            self.crash.add((point, int(n or 1)))
        self.drop = {k: set(v) for k, v in plan.get("drop", {}).items()}
        self.duplicate = {k: set(v) for k, v in plan.get("duplicate", {}).items()}
        self.partial: dict[str, int] = dict(plan.get("partial", {}))
        self._hits: Counter = Counter()
        self._msgs: Counter = Counter()
        self.fired: list[str] = []

    @classmethod
    def from_env(cls, var: str = "SDE_FAULTS") -> "Faults":
        """The plan from $SDE_FAULTS: inline JSON, or a path to a JSON file. Unset: no faults."""
        raw = os.environ.get(var, "").strip()
        if not raw:
            return cls()
        return cls(json.loads(raw if raw.startswith("{") else Path(raw).read_text()))

    def point(self, name: str) -> None:
        """A crash point; raises Crash if the plan says so for this hit."""
        self._hits[name] += 1
        hit = self._hits[name]
        if (name, hit) in self.crash:
            self.fired.append(f"crash {name}#{hit}")
            raise Crash(name, hit)

    def deliver(self, kind: str, msg: Any) -> list[Any]:
        """The copies of the nth `kind` message that arrive: [], [msg] or [msg, msg]."""
        self._msgs[kind] += 1
        n = self._msgs[kind]
        if n in self.drop.get(kind, ()):
            self.fired.append(f"drop {kind}#{n}")
            return []
        if n in self.duplicate.get(kind, ()):
            self.fired.append(f"duplicate {kind}#{n}")
            return [msg, msg]
        return [msg]

    def write(self, f: IO, data: str | bytes, at: str) -> None:
        """Write `data` to `f` and flush; at a planned partial point, only a prefix, then Crash."""
        self._hits[at] += 1
        if at in self.partial:
            keep = self.partial.pop(at)
            f.write(data[:keep])
            f.flush()
            self.fired.append(f"partial {at}: {keep} of {len(data)}")
            raise Crash(at, self._hits[at])
        f.write(data)
        f.flush()
        self.point(at)
