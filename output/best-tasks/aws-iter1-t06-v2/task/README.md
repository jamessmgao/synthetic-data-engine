# KV Store Failover Consistency

A replicated key-value store loses acknowledged writes after leader failover. The follower's recovery logic does not replay uncommitted but replicated WAL entries.

## Scenario

The agent is given a Python replicated KV store with leader/follower architecture. After a leader crash and follower promotion, or after restarting a promoted replica, acknowledged writes are lost.

## Root cause (hidden from the agent)

recovery.get_replay_target returns commit_index instead of state.wal.last_index, so on promotion and restart, replicated-but-uncommitted WAL entries are never applied to the state machine.

## Correctness constraints

- **C1**: Acknowledged writes remain readable after failover to a replica, including after the promoted replica is killed and restarted.
- **C2**: Basic SET, GET, DEL, and FLUSH operations produce correct results on a leader.
- **C3**: FLUSH commits writes so they survive node restart.
- **C4**: Followers reject SET, DEL, and FLUSH with ERR_NOT_LEADER.

## Subgoals

1. Identify why acknowledged writes are lost after promotion
2. Fix recovery to replay all replicated WAL entries
3. Ensure commit_index is updated after promotion so restart recovery also works
4. Verify basic operations still function correctly

## Failure scenario

SET x 1 and SET x 2 on leader (both return OK, both replicated to follower). Kill leader before FLUSH. PROMOTE follower. GET x returns NIL instead of OK 2. After restarting promoted node, GET x still returns NIL.

## Expected agent pitfalls

- Fixing only the promotion path by patching _cmd_promote directly, without fixing the restart recovery path
- Not updating commit_index after promotion, so restart recovery still skips WAL entries

## Anti-shortcut measures

- Tests check reads after both promotion and restart, catching fixes that only address one path
- Counterexample test replays the exact failure scenario including restart
- Random seed testing covers varied write patterns

## Requirement-to-test coverage

| Requirement | Tests |
|---|---|
| C1 | `test_acknowledged_writes_after_failover_and_restart`, `test_repro_failover_loses_writes` |
| C2 | `test_basic_set_get`, `test_delete`, `test_get_missing_key_returns_nil` |
| C3 | `test_flush_commits_writes` |
| C4 | `test_follower_rejects_writes` |
