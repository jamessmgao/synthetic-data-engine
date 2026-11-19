# Order fulfillment worker

A Python order-fulfillment service accepts orders, processes them with a worker, and reports status. Users see orders stuck in `accepted` after the worker has already reserved a fulfillment package and announced a result; retries can lose that result or reserve extra packages.

## Interface

Run `python3 /app/service.py <command>` from `/app`. Output is one JSON object per line with sorted keys.

Commands:
- `reset` -> `{"state":"ready"}`
- `submit <order_id> <sku> <quantity>` -> `{"order_id":...,"sku":...,"quantity":...,"status":"accepted"}`. Order IDs match `[A-Za-z0-9][A-Za-z0-9_-]{0,63}`; quantities are positive integers. Invalid input exits 1 with `{"error":...}`.
- `worker` -> processes queued orders until none remain, then emits `{"processed":N}`. For an order it may emit `{"order_id":...,"phase":"result_ready","result":...}` and `{"order_id":...,"phase":"settled","result":...}`; cache completions emit `{"order_id":...,"phase":"complete","source":"cache","result":...}`.
- `status <order_id>` -> the cached result with `"source":"cache"`, otherwise the stored order with `"source":"store"`, or `{"error":"not_found","id":...}` with exit 1.
- `report <order_id>` -> `{"order":{...},"queue_waiting":N,"packages":[...],"package_count":N,"cache":...}`. The `order` object has `order_id`, `sku`, `quantity`, `status`, and `result`; `result` is `null` until fulfilled. Each package has `package_id`, `sku`, `quantity`, and `status`.

State files are `/app/state/orders.db`, `/app/state/fulfillment.db`, and `/app/state/responses.db`.

A worker process can be killed at any moment and a new one started. The system must recover correctly from such kills and retries.

## Guarantees

1. Each order reserves at most one fulfillment package.
2. If an order has no queued work, it is fulfilled.
3. A fulfilled order records its reservation result.
4. The order result identifies the reservation package.
5. Cached and stored results agree.

## Must keep working

- `reset` clears all state and returns ready.
- `submit` accepts valid orders and rejects invalid IDs, invalid quantities, and duplicate order IDs.
- `worker` processes all queued orders to completion when not killed, and emits `processed` counts.
- `status` and `report` return the formats above for existing orders and fail for unknown orders.
- Running `worker` again after all work is done emits `{"processed":0}`.
