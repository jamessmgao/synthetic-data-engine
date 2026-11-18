# World scaffolds (`/opt/sde/scaffold` in the authoring image)

Small, tested, stdlib-only helpers for the parts every world needs and nobody should
rewrite under a time budget. Copy the ones you use into the world, so it does not
depend on the authoring image:

    cp /opt/sde/scaffold/sde_sched.py /opt/sde/scaffold/sde_repro.py /logs/artifacts/world/environment/app/

They are plumbing for the counterexample, not the system. The difficulty must stay in
the system's own components and their interactions. Name the vendored files as they
are. They are generic and give nothing away.

| file | what it does |
|---|---|
| `sde_sched.py` | Deterministic interleavings. Actors are generators that `yield "step"` at every point another actor may run. `Scheduler(seed=…)` picks the order from a seed; `Scheduler(replay=schedule)` replays a recorded order exactly. `explore(build, check, seeds)` finds the first seed whose run violates an invariant and returns its schedule; `save_schedule` stores it for repro/. |
| `sde_fault.py` | Planned faults. `Faults(plan)` / `Faults.from_env()` (`SDE_FAULTS`, inline JSON or a file). `point("after_commit")` raises `Crash` on a planned hit (`"after_commit#2"` = the 2nd hit); `deliver("ack", m)` returns `[]` (dropped), `[m]` or `[m, m]` (duplicated); `write(f, data, at="wal")` writes a prefix and crashes (a torn write). `fired` lists what happened. Restarting is yours: catch `Crash`, rebuild from persisted state. |
| `sde_repro.py` | The repro harness. `repro_main(run, INVARIANTS)` reads `--sequence` (default `repro/sequence.json`), replays it with your `run`, checks each invariant (True / False / a message), prints the verdict and exits **1 violated, 0 holds, 2 harness broken**. |

A typical Python world: find the counterexample with `explore`, save its schedule to
`repro/sequence.json`, and make `repro.sh` one line:

    cd /app && python3 repro_main.py --sequence /world/repro/sequence.json

## C and Go worlds: the same patterns, written inline

These are short enough to write directly. Keep the shape:

**Crash points (C).** The plan comes from the environment, so the repro sets it:

```c
static int crash_hit(const char *point) {            /* SDE_CRASH=after_commit#2 */
    static int hits[64]; static const char *names[64]; static int n;
    const char *plan = getenv("SDE_CRASH"); if (!plan) return 0;
    int i = 0; while (i < n && strcmp(names[i], point)) i++;
    if (i == n && n < 64) names[n++] = point;
    int hit = ++hits[i]; char want[128]; snprintf(want, sizeof want, "%s#%d", point, hit);
    return strcmp(plan, want) == 0 || (hit == 1 && strcmp(plan, point) == 0);
}
#define FAULT_POINT(p) do { if (crash_hit(p)) _exit(137); } while (0)
```

**Deterministic interleavings (Go).** Run actors as goroutines that block on a
per-actor channel at each step; a driver releases one actor per step, in the order of
a recorded schedule or a seeded `rand.New(rand.NewSource(seed))`:

```go
type Step struct{ actor string; release chan struct{}; done chan struct{} }
// actor side, at every interleaving point:   <-me.release; ...work...; me.done <- struct{}{}
// driver:   for _, name := range schedule { a := actors[name]; a.release <- struct{}{}; <-a.done }
```

**Repro exit codes (any language).** 1 when an invariant is violated, 0 when every one
holds, anything else (2) when the harness itself failed.
