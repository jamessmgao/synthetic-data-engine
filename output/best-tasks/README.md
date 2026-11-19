# The night's best tasks (closest to the band)

None is accepted yet: each scored **4/5** with terminus-2 + glm-5.3-flash, one pass above the
1–3/5 band. Each folder holds:
- `task/`: the bundle (`instruction.md`, `environment/`, `tests/`, `solution/`, `task.toml`);
- `rollouts/<job>/`: each solver run's `trajectory.json`, the verifier's `test-stdout.txt`,
  `reward.txt` and `result.json`.

| Task | Family | Passes | What it is | Why it wasn't harder |
|---|---|---|---|---|
| [aws-iter1-t01-v1](aws-iter1-t01-v1/) | cross-component / misleading symptom (Go + SQLite) | ✓✓✗✓✓ | a ledger service that must dedupe and recover pending transactions after a crash | most solvers found the dedup-and-recovery fix; run 3 missed crash-time PENDING recovery (a genuine miss), but its tests also insert raw SQL into an unstated table (a hidden requirement) |
| [aws-iter1-t06-v2](aws-iter1-t06-v2/) | distributed / linearizability (Python) | ✓✗✓✓✓ | a replicated KV store with a WAL; v2 (a mutation) adds a separate sync point, so acknowledged writes lost on restart need a two-path fix | 3 small files; the one failure broke its own server startup, not the restart path |
| [aws-agent-b1-t09-v2](aws-agent-b1-t09-v2/) | performance / memory bound (Java) | ✗✓✓✓✓ | a 6M-event reconciler in 128 MiB; v2 (a mutation) requires per-account checksums in timestamp order, which isn't the stream order, so it needs an external sort | the one failure had the right design but a broken merge; the stated order still points at an external sort |

Exported from S3 on 2026-11-18. The full analysis is in [docs/ITERATION_LOG.md on main](https://github.com/jamessmgao/synthetic-data-engine/blob/main/docs/ITERATION_LOG.md).
