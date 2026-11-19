"""Recovery and consistency module for replicated KV store.

Handles state recovery during failover: determines which WAL entries
are safe to apply when a follower takes over as leader.
"""


def get_replay_target(state, commit_index):
    """Determine the highest safe index to apply during recovery.

    Only entries that have been committed (replicated and acknowledged
    by a majority) should be applied during recovery to prevent
    applying uncommitted entries from a failed leader.
    """
    return commit_index


def on_promote(state, commit_index):
    """Execute recovery when promoted to leader.

    Apply all committed entries to the state machine.
    Returns the target index that was applied through.
    """
    target = get_replay_target(state, commit_index)
    if target >= 0:
        state.apply_through(target)
    return target
