"""Deterministic interleavings for a world (stdlib only; vendor it into environment/).

Write each concurrent actor as a generator function that yields a step name at every
point where another actor may run in between. The scheduler runs one step at a time,
choosing which actor goes next from a seed, or from a recorded schedule:

    def client(name, store):
        v = store.read("x"); yield f"{name}: read"
        store.write("x", v + 1); yield f"{name}: write"

    s = Scheduler(seed=7)
    s.spawn("a", client("a", store))
    s.spawn("b", client("b", store))
    trace = s.run()                       # [("a", "a: read"), ("b", "b: read"), ...]

    Scheduler(replay=[a for a, _ in trace])  # the same interleaving, step for step

`explore(build, check, seeds)` runs a fresh world per seed until `check` reports a
violated invariant, and returns the seed and its schedule: execute the system to find
the counterexample, then save the schedule (`save_schedule`) for repro/.
"""

import json
import random
from collections.abc import Callable, Generator, Iterable
from pathlib import Path
from typing import Any

Actor = Generator[str, None, Any]


class ScheduleError(Exception):
    """A replayed schedule names an actor that is finished or unknown."""


class Scheduler:
    def __init__(self, seed: int | None = None, replay: Iterable[str] | None = None):
        if (seed is None) == (replay is None):
            raise ValueError("give exactly one of seed or replay")
        self._rng = random.Random(seed) if seed is not None else None
        self._replay = list(replay) if replay is not None else None
        self._actors: dict[str, Actor] = {}
        self.trace: list[tuple[str, str]] = []
        self.results: dict[str, Any] = {}

    def spawn(self, name: str, actor: Actor) -> None:
        if name in self._actors or name in self.results:
            raise ValueError(f"actor {name!r} already exists")
        self._actors[name] = actor

    def _next(self, i: int) -> str:
        if self._replay is not None:
            if i >= len(self._replay):  # the recording ran out: finish in spawn order
                return next(iter(self._actors))
            name = self._replay[i]
            if name not in self._actors:
                raise ScheduleError(f"step {i}: actor {name!r} is not runnable (runnable: {sorted(self._actors)})")
            return name
        return self._rng.choice(sorted(self._actors))

    def run(self, max_steps: int = 100_000) -> list[tuple[str, str]]:
        """Run every actor to completion; returns the trace of (actor, step) pairs."""
        i = 0
        while self._actors:
            if i >= max_steps:
                raise RuntimeError(f"no completion after {max_steps} steps (a livelock?)")
            name = self._next(i)
            try:
                step = next(self._actors[name])
                self.trace.append((name, str(step)))
            except StopIteration as done:
                del self._actors[name]
                self.results[name] = done.value
                self.trace.append((name, "<done>"))
            i += 1
        return self.trace

    @property
    def schedule(self) -> list[str]:
        """The actor order, which replays this run exactly."""
        return [name for name, _ in self.trace]


def explore(build: Callable[[Scheduler], Any], check: Callable[[Any], str | None],
            seeds: Iterable[int] = range(1000)) -> dict | None:
    """For each seed: a fresh world (`build(scheduler)` spawns the actors and returns the
    state), run it, and `check(state)` returns a violation message or None. The first
    violation found: {seed, schedule, trace, violation}; None if every seed held."""
    for seed in seeds:
        s = Scheduler(seed=seed)
        state = build(s)
        s.run()
        if (violation := check(state)) is not None:
            return {"seed": seed, "schedule": s.schedule, "trace": s.trace, "violation": violation}
    return None


def save_schedule(path: str | Path, schedule: list[str]) -> None:
    Path(path).write_text(json.dumps(schedule, indent=0) + "\n")


def load_schedule(path: str | Path) -> list[str]:
    return json.loads(Path(path).read_text())
