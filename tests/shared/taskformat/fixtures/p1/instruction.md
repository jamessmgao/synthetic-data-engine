## Durable reservation service

You are working in `/app` on the `reservation` command-line service. For a selected data directory, it stores durable orders and inventory in two SQLite databases: `orders.db` and `inventory.db`.

Clients submit stable request IDs such as `order-1`. Delivery can be retried, and a worker process can be interrupted before it finishes all of its work. The service has a logical clock: time advances only through the `tick` command, and a worker lease lasts `30000` logical milliseconds. An expired lease must be retried without losing the order.

Users have reported that stock can fall by more than a submitted request should consume after delivery is retried, and then later requests that should fit are rejected. The service must provide these observable guarantees for every schedule reachable through the command interface:

- A request ID can produce at most one accepted inventory movement, and that movement can debit no more than the quantity submitted for that request.
- Every accepted inventory movement must fit the durable stock at the moment it is accepted. Stock must never become negative.
- Once a request ID has produced an inventory movement, any later delivery of that same request ID must reuse that exact durable result without applying another inventory effect.
- Lease retry must preserve both the order and any prior durable inventory effect. After recovery, the request must remain available and must not consume capacity twice.

### Command interface

Run commands as `python3 -m reservation.cli --data-dir DATA_DIR ...` from `/app`. The service accepts arbitrary valid SKU names, nonempty request IDs, positive quantities, and nonnegative initial stock; examples use `widget`.

- `init --sku SKU --stock COUNT` initializes or replaces stock for `SKU`. On success it prints `initialized SKU with COUNT units`.
- `submit --request-id ID --sku SKU --quantity COUNT` durably enqueues a positive reservation. Order states are `queued`, `running`, `fulfilled`, and `rejected`. On a new ID it prints a JSON object with exactly `request_id`, `sku`, `quantity`, `state`, and `attempts`; `state` is `queued` and `attempts` is `0`. A repeated submission with identical values returns the existing order record without adding another order. A conflicting reuse of the same ID fails without replacing the existing order.
- `worker --id WORKER_ID` claims at most one eligible order. If it claims one, it prints a JSON object containing the order fields and an `inventory` result. An accepted result contains exactly `accepted: true`, `movement_id`, and `remaining`; an unavailable-stock result contains exactly `accepted: false` and `reason: "insufficient_stock"`. If no order is eligible it prints `{"claimed": false}`.
- `tick --milliseconds COUNT` advances logical time and prints `{"now_ms": NEW_TIME}`. Negative values are invalid.
- `inspect --json` prints a JSON report. `orders` contains `now_ms` and `orders`; each order contains `request_id`, `sku`, `quantity`, `state`, `attempts`, `lease_until_ms`, and `last_worker`, ordered by `request_id`. `inventory` contains `stock` and `movements`; each stock row contains `sku` and `quantity`, and each movement contains `movement_id`, `reference`, `sku`, `quantity`, and `remaining`. Stock rows are ordered by `sku`, and movements are ordered by `movement_id`. In a movement, `reference` is the request ID, `quantity` is the amount debited, and `remaining` is that SKU’s durable stock after the accepted movement.

The documented fault-injection hook is `STOP_AFTER=inventory`: when a claimed request can be fulfilled, at that point a worker exits with status `70` after the accepted inventory movement and stock update are durable and before the worker’s remaining completion work. A replacement worker is started as a new process. Logical time is deterministic; use `tick --milliseconds 30001` to expire a lease.

Keep the normal behavior working: valid requests are queued and fulfilled exactly once when stock fits; requests larger than available stock are marked `rejected` with reason `insufficient_stock` and do not change stock; repeated identical submissions return the existing order; conflicting reuse and invalid submissions fail; idle workers report that no order was claimed; the logical clock and inspect output formats continue to work.

Make the guarantees hold under every execution reachable through this interface, including retries and interrupted workers. Do not change the CLI paths, output formats, database file names, `30000`-millisecond lease duration, or documented `STOP_AFTER=inventory` behavior.
