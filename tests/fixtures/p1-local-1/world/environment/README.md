# Reservation

`reservation` is a command-line service for durable stock reservations. It keeps
order leases and inventory in separate SQLite databases under one data
directory. Clients submit a stable `request_id`; delivery may be retried after a
lease expires, so externally visible inventory effects must be tied to that ID.

## Examples

```sh
python -m reservation.cli --data-dir ./state init --sku widget --stock 10
python -m reservation.cli --data-dir ./state submit --request-id order-1 --sku widget --quantity 3
python -m reservation.cli --data-dir ./state worker
python -m reservation.cli --data-dir ./state inspect --json
```

Logical time is advanced only by the `tick` command. A lease is 30000 logical
milliseconds. `STOP_AFTER=inventory` is the supported fault-injection point for
recovery tests.
