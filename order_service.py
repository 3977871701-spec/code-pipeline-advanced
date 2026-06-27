"""High-availability order service per SPEC.md."""

from __future__ import annotations

import logging
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
from typing import Iterator

logger = logging.getLogger(__name__)


class OrderStatus(Enum):
    PENDING = "PENDING"
    CONFIRMED = "CONFIRMED"
    CANCELLED = "CANCELLED"


@dataclass
class Order:
    order_id: str
    user_id: str
    items: list
    amount: float
    status: OrderStatus = OrderStatus.PENDING
    created_at: float = 0.0
    updated_at: float = 0.0


# --- Exception hierarchy (Section 3) --------------------------------------
class OrderError(Exception):
    """Base for all order-related errors."""


class InvalidOrderError(OrderError):
    """Parameter validation failed."""


class OrderNotFoundError(OrderError):
    """Requested order does not exist."""


class DuplicateOrderError(OrderError):
    """Idempotency conflict: same order_id with different payload."""


class OrderStateError(OrderError):
    """Illegal state transition."""


# --- InMemoryOrderRepository (Section 4) ----------------------------------
class InMemoryOrderRepository:
    _TERMINAL = OrderStatus.CANCELLED

    def __init__(self) -> None:
        self._orders: dict[str, Order] = {}
        self._lock = threading.RLock()

    @staticmethod
    def _is_same_payload(a: Order, b: Order) -> bool:
        return a.user_id == b.user_id and a.amount == b.amount and list(a.items) == list(b.items)

    def create(self, order: Order) -> tuple[bool, Order]:
        with self._lock:
            existing = self._orders.get(order.order_id)
            if existing is None:
                self._orders[order.order_id] = order
                return (True, order)
            if self._is_same_payload(existing, order):
                return (False, existing)
            raise DuplicateOrderError(f"order_id {order.order_id!r} payload mismatch")

    def get(self, order_id: str) -> Order:
        with self._lock:
            order = self._orders.get(order_id)
            if order is None:
                raise OrderNotFoundError(f"order {order_id!r} not found")
            return order

    def update(self, order_id: str, **fields) -> Order:
        unknown = set(fields) - {"status", "updated_at"}
        if unknown:
            raise OrderStateError(f"unsupported update fields: {sorted(unknown)}")
        with self._lock:
            order = self._orders.get(order_id)
            if order is None:
                raise OrderNotFoundError(f"order {order_id!r} not found")
            new_status = fields.get("status", order.status)
            if order.status is self._TERMINAL and new_status is not self._TERMINAL:
                raise OrderStateError(f"cannot leave terminal state {order.status.value}")
            if order.status is OrderStatus.CONFIRMED and new_status is OrderStatus.PENDING:
                raise OrderStateError("cannot revert CONFIRMED to PENDING")
            order.status = new_status
            order.updated_at = fields.get("updated_at", time.time())
            return order

    def delete(self, order_id: str) -> bool:
        with self._lock:
            return self._orders.pop(order_id, None) is not None

    def exists(self, order_id: str) -> bool:
        with self._lock:
            return order_id in self._orders

    def count(self) -> int:
        with self._lock:
            return len(self._orders)


# --- IdempotencyGuard (Section 5) -----------------------------------------
class IdempotencyGuard:
    def __init__(self) -> None:
        self._locks: dict[str, threading.Lock] = {}
        self._meta_lock = threading.Lock()

    @contextmanager
    def lock_for(self, order_id: str) -> Iterator[threading.Lock]:
        if not isinstance(order_id, str) or order_id == "":
            raise InvalidOrderError("order_id must be a non-empty string")
        with self._meta_lock:
            lock = self._locks.get(order_id) or self._locks.setdefault(order_id, threading.Lock())
        with lock:
            yield lock


# --- OrderService (Section 6) ---------------------------------------------
class OrderService:
    _CANCELLABLE = (OrderStatus.PENDING, OrderStatus.CONFIRMED)

    def __init__(self) -> None:
        self._repo = InMemoryOrderRepository()
        self._idempotency = IdempotencyGuard()

    @staticmethod
    def _validate(order_id: str, user_id: str, items: list, amount: float) -> None:
        if not isinstance(order_id, str) or order_id == "":
            raise InvalidOrderError("order_id must be non-empty string")
        if not isinstance(user_id, str) or user_id == "":
            raise InvalidOrderError("user_id must be non-empty string")
        if not isinstance(items, list) or len(items) == 0:
            raise InvalidOrderError("items must be a non-empty list")
        if isinstance(amount, bool) or not isinstance(amount, (int, float)) or amount <= 0:
            raise InvalidOrderError("amount must be positive number")

    def create_order(self, order_id: str, user_id: str, items: list, amount: float) -> Order:
        self._validate(order_id, user_id, items, amount)
        with self._idempotency.lock_for(order_id):
            now = time.time()
            candidate = Order(order_id, user_id, list(items), float(amount),
                              OrderStatus.PENDING, now, now)
            created, order = self._repo.create(candidate)
        if created:
            logger.info("order %r created for user %r", order_id, user_id)
        else:
            logger.warning("idempotent hit for order %r", order_id)
        return order

    def get_order(self, order_id: str) -> Order:
        if not isinstance(order_id, str) or order_id == "":
            raise InvalidOrderError("order_id must be non-empty string")
        return self._repo.get(order_id)

    def cancel_order(self, order_id: str, reason: str = "") -> Order:
        if not isinstance(order_id, str) or order_id == "":
            raise InvalidOrderError("order_id must be non-empty string")
        with self._idempotency.lock_for(order_id):
            order = self._repo.get(order_id)
            if order.status is OrderStatus.CANCELLED:
                logger.warning("idempotent cancel for order %r", order_id)
                return order
            if order.status not in self._CANCELLABLE:
                raise OrderStateError(f"cannot cancel order in status {order.status.value}")
            updated = self._repo.update(order_id, status=OrderStatus.CANCELLED, updated_at=time.time())
            logger.info("order %r cancelled (reason=%r)", order_id, reason)
            return updated
