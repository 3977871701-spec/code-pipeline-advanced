# Test Report: order_service

- **Target**: `/Users/xylei/code-pipeline-projects/advanced/order_service.py`
- **Test file**: `/Users/xylei/code-pipeline-projects/advanced/test_order_service.py`
- **Framework**: `unittest` (Python 3.11.9 standard library)
- **Date**: 2026-06-02
- **Difficulty**: advanced (unit + concurrency)

## Result

**PASS** — 19 / 19 tests passed in 0.004s.

```
Ran 19 tests in 0.004s
OK
```

## Coverage

### TestOrderServiceUnit (16 cases)

| # | Test | Verifies |
|---|------|----------|
| 1 | `test_create_order_success` | Create returns `Order`, status `PENDING`, `count == 1` |
| 2 | `test_create_order_idempotent_same_payload` | Same id + same payload returns the same instance, no exception, `count == 1` |
| 3 | `test_create_order_duplicate_different_payload` | Different `amount` / `user_id` / `items` all raise `DuplicateOrderError` |
| 4 | `test_get_order_success` | `get_order` returns the same object that was created |
| 5 | `test_get_order_not_found` | Missing id raises `OrderNotFoundError` |
| 6 | `test_get_order_invalid_id` | Empty id raises `InvalidOrderError` |
| 7 | `test_cancel_order_success` | `PENDING → CANCELLED` transition |
| 8 | `test_cancel_order_idempotent` | Second `cancel_order` returns the same `CANCELLED` record, no exception |
| 9 | `test_cancel_order_not_found` | Missing id raises `OrderNotFoundError` |
| 10 | `test_cancel_order_invalid_id` | Empty id raises `InvalidOrderError` |
| 11 | `test_create_order_invalid_params` | Empty / `None` order_id, empty user_id, empty / `None` items, negative / zero / non-numeric amount each raise `InvalidOrderError` |
| 12 | `test_repository_state_machine_terminal` | `CANCELLED` is terminal — `update(..., status=PENDING)` raises `OrderStateError` |
| 13 | `test_repository_state_machine_confirmed_to_pending_forbidden` | `CONFIRMED → PENDING` is forbidden |
| 14 | `test_repository_rejects_unknown_update_fields` | Only `status` / `updated_at` accepted; other fields raise `OrderStateError` |
| 15 | `test_idempotency_guard_rejects_empty_id` | `IdempotencyGuard.lock_for("")` raises `InvalidOrderError` |
| 16 | `test_exception_hierarchy` | All four subclasses inherit from `OrderError` → `Exception` |

### TestOrderServiceConcurrency (3 cases — all use `threading.Barrier` to synchronize release)

| # | Test | Verifies |
|---|------|----------|
| 1 | `test_concurrent_create_same_id` | 20 threads race to create `order-X`; no exceptions, all 20 receive the **same** `Order` instance (same `created_at`), `count == 1` |
| 2 | `test_concurrent_create_and_cancel` | 10 creators + 10 cancellers (20-thread `Barrier`); final state is `CANCELLED`, `count == 1`, no exceptions |
| 3 | `test_concurrent_different_ids` | 50 threads create 50 distinct ids; all complete (no deadlock), `count == 50`, no exceptions |

## Concurrency / Idempotency Findings

- `IdempotencyGuard` plus `InMemoryOrderRepository`'s `RLock` correctly serialize per-id operations, so 20 racing creates collapse to a single record with a single `created_at`.
- The cancel path is also idempotent under contention: regardless of how many threads call `cancel_order` on the same id, every caller observes `CANCELLED` and no exception is raised.
- Mixed create/cancel races terminate with a consistent final state (exactly one record, status `CANCELLED`).
- No thread was observed alive after `join(timeout=10)`, so no deadlock on the per-id / meta-lock pair.

## Run Command

```bash
cd /Users/xylei/code-pipeline-projects/advanced
python3 -m unittest test_order_service -v
```
