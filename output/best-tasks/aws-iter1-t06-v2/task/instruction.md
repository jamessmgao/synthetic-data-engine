# Key-Value Store: Acknowledged Writes Lost

## Symptom

Keys that were successfully written return NIL instead of their values. The SET command
returned OK, confirming the write was acknowledged by the leader, but later GET
commands return NIL for those keys.

## Guarantees

Every write acknowledged by the leader (SET or DEL returning an OK response) is
durable. Once a write has been acknowledged, its effect must be visible in every
subsequent read, through any combination of process failures, restarts, and role
changes. A node that starts or restarts on a given port must serve every write
that was acknowledged while that port was in use.

## What Must Keep Working

- SET key value on a leader returns OK index and stores the value
- GET key returns OK value for existing keys, NIL for missing keys
- DEL key on a leader removes the key; subsequent GET returns NIL
- FLUSH on a leader durably persists all acknowledged writes; after FLUSH, writes survive a node restart
- SET, DEL, and FLUSH on a follower return ERR_NOT_LEADER
- PROMOTE on a follower changes its role to leader and returns OK

## Interface

Start a leader node (with replication to a peer):
  python3 /app/kvstore.py leader PORT PEER_HOST PEER_PORT

Start a follower node:
  python3 /app/kvstore.py follower PORT

Send a command to a node:
  python3 /app/client.py HOST PORT COMMAND

Commands and their responses:
  SET key value   ->  OK index   (leader only; replicates to follower)
  GET key         ->  OK value   or  NIL
  DEL key         ->  OK index   (leader only; replicates to follower)
  FLUSH           ->  OK commit=INDEX   (leader only)
  PROMOTE         ->  OK   (on a follower; promotes it to leader)

A node process can be killed at any moment and a new one started on the same port.
