"""Unit and concurrency tests for order_service per SPEC.md."""

import threading
import unittest

from order_service import (
    DuplicateOrderError,
    IdempotencyGuard,
    InvalidOrderError,
    InMemoryOrderRepository,
    Order,
    OrderError,
    OrderNotFoundError,
    OrderService,
    OrderStateError,
    OrderStatus,
)


# ---------------------------------------------------------------------------
# TestOrderServiceUnit (>= 8 cases)
# ---------------------------------------------------------------------------
class TestOrderServiceUnit(unittest.TestCase):
    def setUp(self) -> None:
        self.svc = OrderService()

    # ---- create_order ----
    def test_create_order_success(self) -> None:
        order = self.svc.create_order("O-1", "u1", ["item1"], 100.0)
        self.assertIsInstance(order, Order)
        self.assertEqual(order.order_id, "O-1")
        self.assertEqual(order.user_id, "u1")
        self.assertEqual(order.items, ["item1"])
        self.assertEqual(order.amount, 100.0)
        self.assertEqual(order.status, OrderStatus.PENDING)
        self.assertGreater(order.created_at, 0.0)
        self.assertEqual(self.svc._repo.count(), 1)

    def test_create_order_idempotent_same_payload(self) -> None:
        a = self.svc.create_order("O-2", "u1", ["item1"], 100.0)
        b = self.svc.create_order("O-2", "u1", ["item1"], 100.0)
        # Same Order instance, same created_at
        self.assertIs(a, b)
        self.assertEqual(a.created_at, b.created_at)
        self.assertEqual(self.svc._repo.count(), 1)

    def test_create_order_duplicate_different_payload(self) -> None:
        self.svc.create_order("O-3", "u1", ["a"], 50.0)
        with self.assertRaises(DuplicateOrderError):
            self.svc.create_order("O-3", "u1", ["a"], 999.0)
        # amount changed
        with self.assertRaises(DuplicateOrderError):
            self.svc.create_order("O-3", "u1", ["a"], 51.0)
        # user_id changed
        with self.assertRaises(DuplicateOrderError):
            self.svc.create_order("O-3", "u2", ["a"], 50.0)
        # items changed
        with self.assertRaises(DuplicateOrderError):
            self.svc.create_order("O-3", "u1", ["b"], 50.0)

    # ---- get_order ----
    def test_get_order_success(self) -> None:
        created = self.svc.create_order("O-4", "u1", ["x"], 10.0)
        fetched = self.svc.get_order("O-4")
        self.assertIs(fetched, created)

    def test_get_order_not_found(self) -> None:
        with self.assertRaises(OrderNotFoundError):
            self.svc.get_order("does-not-exist")

    def test_get_order_invalid_id(self) -> None:
        with self.assertRaises(InvalidOrderError):
            self.svc.get_order("")

    # ---- cancel_order ----
    def test_cancel_order_success(self) -> None:
        self.svc.create_order("O-5", "u1", ["x"], 10.0)
        cancelled = self.svc.cancel_order("O-5", reason="user request")
        self.assertEqual(cancelled.status, OrderStatus.CANCELLED)

    def test_cancel_order_idempotent(self) -> None:
        self.svc.create_order("O-6", "u1", ["x"], 10.0)
        r1 = self.svc.cancel_order("O-6")
        r2 = self.svc.cancel_order("O-6")
        self.assertEqual(r1.status, OrderStatus.CANCELLED)
        self.assertEqual(r2.status, OrderStatus.CANCELLED)
        self.assertEqual(self.svc._repo.count(), 1)

    def test_cancel_order_not_found(self) -> None:
        with self.assertRaises(OrderNotFoundError):
            self.svc.cancel_order("missing")

    def test_cancel_order_invalid_id(self) -> None:
        with self.assertRaises(InvalidOrderError):
            self.svc.cancel_order("")

    # ---- parameter validation ----
    def test_create_order_invalid_params(self) -> None:
        # empty order_id
        with self.assertRaises(InvalidOrderError):
            self.svc.create_order("", "u1", ["x"], 10.0)
        # None order_id
        with self.assertRaises(InvalidOrderError):
            self.svc.create_order(None, "u1", ["x"], 10.0)  # type: ignore[arg-type]
        # empty user_id
        with self.assertRaises(InvalidOrderError):
            self.svc.create_order("O-7", "", ["x"], 10.0)
        # empty items
        with self.assertRaises(InvalidOrderError):
            self.svc.create_order("O-7", "u1", [], 10.0)
        # None items
        with self.assertRaises(InvalidOrderError):
            self.svc.create_order("O-7", "u1", None, 10.0)  # type: ignore[arg-type]
        # negative amount
        with self.assertRaises(InvalidOrderError):
            self.svc.create_order("O-7", "u1", ["x"], -1.0)
        # zero amount
        with self.assertRaises(InvalidOrderError):
            self.svc.create_order("O-7", "u1", ["x"], 0.0)
        # non-numeric amount
        with self.assertRaises(InvalidOrderError):
            self.svc.create_order("O-7", "u1", ["x"], "10")  # type: ignore[arg-type]

    # ---- state machine ----
    def test_repository_state_machine_terminal(self) -> None:
        repo = InMemoryOrderRepository()
        o = Order("O-8", "u1", ["x"], 10.0, OrderStatus.CANCELLED, 0.0, 0.0)
        repo._orders[o.order_id] = o
        with self.assertRaises(OrderStateError):
            repo.update("O-8", status=OrderStatus.PENDING)

    def test_repository_state_machine_confirmed_to_pending_forbidden(self) -> None:
        repo = InMemoryOrderRepository()
        o = Order("O-9", "u1", ["x"], 10.0, OrderStatus.CONFIRMED, 0.0, 0.0)
        repo._orders[o.order_id] = o
        with self.assertRaises(OrderStateError):
            repo.update("O-9", status=OrderStatus.PENDING)

    def test_repository_rejects_unknown_update_fields(self) -> None:
        repo = InMemoryOrderRepository()
        o = Order("O-10", "u1", ["x"], 10.0, OrderStatus.PENDING, 0.0, 0.0)
        repo._orders[o.order_id] = o
        with self.assertRaises(OrderStateError):
            repo.update("O-10", user_id="hacker")

    def test_idempotency_guard_rejects_empty_id(self) -> None:
        guard = IdempotencyGuard()
        with self.assertRaises(InvalidOrderError):
            with guard.lock_for(""):
                pass

    def test_exception_hierarchy(self) -> None:
        # All custom exceptions must derive from OrderError
        for cls in (
            InvalidOrderError,
            OrderNotFoundError,
            DuplicateOrderError,
            OrderStateError,
        ):
            self.assertTrue(issubclass(cls, OrderError))
        self.assertTrue(issubclass(OrderError, Exception))


# ---------------------------------------------------------------------------
# TestOrderServiceConcurrency (>= 3 cases)
# ---------------------------------------------------------------------------
class TestOrderServiceConcurrency(unittest.TestCase):
    def setUp(self) -> None:
        self.svc = OrderService()

    def test_concurrent_create_same_id(self) -> None:
        n = 20
        barrier = threading.Barrier(n)
        results: list[Order] = []
        errors: list[BaseException] = []
        lock = threading.Lock()

        def worker() -> None:
            try:
                barrier.wait(timeout=5)
                o = self.svc.create_order("order-X", "u1", ["item1"], 100.0)
                with lock:
                    results.append(o)
            except BaseException as e:  # noqa: BLE001
                with lock:
                    errors.append(e)

        threads = [threading.Thread(target=worker) for _ in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        # Deadlock detection: if join timed out, the thread is still alive.
        for t in threads:
            self.assertFalse(t.is_alive(), "thread did not finish — deadlock suspected")

        self.assertEqual(errors, [], f"workers raised: {errors}")
        self.assertEqual(len(results), n)
        self.assertEqual(self.svc._repo.count(), 1)
        # All threads see the same Order (same id, same created_at)
        first = results[0]
        for o in results[1:]:
            self.assertIs(o, first)
            self.assertEqual(o.created_at, first.created_at)

    def test_concurrent_create_and_cancel(self) -> None:
        # Pre-create the order so cancellers always find it; the "creators"
        # are now idempotent retries per SPEC, exercising idempotency under
        # concurrency.
        self.svc.create_order("order-Y", "u1", ["item1"], 100.0)
        n_create = 10
        n_cancel = 10
        total = n_create + n_cancel
        barrier = threading.Barrier(total)
        errors: list[BaseException] = []
        lock = threading.Lock()

        def creator() -> None:
            try:
                barrier.wait(timeout=5)
                self.svc.create_order("order-Y", "u1", ["item1"], 100.0)
            except BaseException as e:  # noqa: BLE001
                with lock:
                    errors.append(e)

        def canceller() -> None:
            try:
                barrier.wait(timeout=5)
                self.svc.cancel_order("order-Y")
            except BaseException as e:  # noqa: BLE001
                with lock:
                    errors.append(e)

        threads = []
        for _ in range(n_create):
            threads.append(threading.Thread(target=creator))
        for _ in range(n_cancel):
            threads.append(threading.Thread(target=canceller))
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        for t in threads:
            self.assertFalse(t.is_alive(), "thread did not finish — deadlock suspected")

        self.assertEqual(errors, [], f"workers raised: {errors}")
        final = self.svc.get_order("order-Y")
        self.assertEqual(final.status, OrderStatus.CANCELLED)
        self.assertEqual(self.svc._repo.count(), 1)

    def test_concurrent_different_ids(self) -> None:
        n = 50
        barrier = threading.Barrier(n)
        errors: list[BaseException] = []
        lock = threading.Lock()

        def worker(i: int) -> None:
            try:
                barrier.wait(timeout=5)
                self.svc.create_order(f"order-{i}", "u1", ["item1"], float(i + 1))
            except BaseException as e:  # noqa: BLE001
                with lock:
                    errors.append(e)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10)
        for t in threads:
            self.assertFalse(t.is_alive(), "thread did not finish — deadlock suspected")

        self.assertEqual(errors, [], f"workers raised: {errors}")
        self.assertEqual(self.svc._repo.count(), n)


if __name__ == "__main__":
    unittest.main()
