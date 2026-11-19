# Ledger Service: Duplicate Debit Bug

You are maintaining a Go ledger service that processes debit transactions against named accounts. Users report that **re-submitting a transaction with the same key debits the account a second time** — the balance changes more than once per key.

## Symptom

After a successful POST to `/tx` with key `K`, a second POST with the same key `K` sometimes returns `{"key":"K","status":"completed"}` and debits the account again, instead of returning the existing result without modifying the balance.

## Guarantees

1. **Each transaction key changes its account balance at most once**, whichever path processes it (HTTP submission, retry after restart, or startup recovery of a persisted pending record).
2. The `/tx` endpoint returns `{"key":"...","status":"completed"}` on success and the `/account/NAME` endpoint returns `{"account":"NAME","balance":N}`.
3. The `/status/KEY` endpoint returns `{"key":"...","account":"...","amount":N,"status":"completed"}` for a completed key, or 404 if not found.
4. The service persists state to `/app/data/ledger.db` (SQLite with WAL mode). The process can be killed at any moment and restarted; on restart it recovers persisted pending transactions.

## Interface

| Endpoint | Method | Body | Response |
|---|---|---|---|
| `/tx` | POST | `{"key":string,"account":string,"amount":int}` | `{"key":string,"status":string}` (200) |
| `/account/NAME` | GET | — | `{"account":string,"balance":int}` (200) or 404 |
| `/status/KEY` | GET | — | `{"key":string,"account":string,"amount":int,"status":string}` (200) or 404 |

The server binary is `/app/server`. It listens on port 8080. The data directory is `/app/data/`. Build with `CGO_ENABLED=1 go build -o /app/server .` from `/app`.

## What must keep working

- First submission of a unique key debits the account by the given amount and returns `completed`.
- Querying `/account/NAME` after a debit returns the correct balance.
- Querying `/status/KEY` after a debit returns the transaction record with `status: "completed"`.
- The service survives kill and restart: the server process can be killed (SIGKILL) at any moment and restarted; on restart it recovers persisted pending transactions automatically.
