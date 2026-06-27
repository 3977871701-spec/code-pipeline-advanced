# 详细技术规格：高可用订单服务

> 本文档为实现 Agent 的依据。所有 API 签名、数据结构、行为契约均已明确。

---

## 1. 文件清单

| 文件路径 | 行数目标 | 用途 |
|----------|----------|------|
| `order_service.py` | ~150 行 | 服务实现 |
| `test_order_service.py` | ~50-100 行 | 单元 + 并发测试 |

**严禁**创建额外文件。**严禁**引入第三方依赖（仅 Python 标准库）。

---

## 2. 数据结构

### 2.1 OrderStatus（Enum）

```python
class OrderStatus(Enum):
    PENDING = "PENDING"      # 已创建未支付
    CONFIRMED = "CONFIRMED"  # 已支付/已确认
    CANCELLED = "CANCELLED"  # 已取消（终态）
```

**状态机**：
```
PENDING   ──cancel──▶ CANCELLED
PENDING   ──confirm─▶ CONFIRMED
CONFIRMED ──cancel──▶ CANCELLED
CANCELLED ──✗──▶ (终态，禁止任何变更)
```

### 2.2 Order（@dataclass）

```python
@dataclass
class Order:
    order_id: str         # 全局唯一，客户端提供
    user_id: str          # 下单用户
    items: list           # 商品列表，list[dict|str]
    amount: float         # 总金额，必须 > 0
    status: OrderStatus   # 默认 PENDING
    created_at: float     # time.time()
    updated_at: float     # time.time()
```

**不变量**：
- `order_id` 非空字符串
- `user_id` 非空字符串
- `items` 非空 list
- `amount > 0`
- `created_at <= updated_at`

---

## 3. 异常层级

```python
class OrderError(Exception):
    """所有订单相关异常的基类"""

class InvalidOrderError(OrderError):
    """参数无效（空 id、负金额、空 items 等）"""

class OrderNotFoundError(OrderError):
    """订单不存在"""

class DuplicateOrderError(OrderError):
    """幂等冲突：order_id 已存在但 payload 不一致"""

class OrderStateError(OrderError):
    """状态非法：例如对 CANCELLED 订单再次操作"""
```

**契约**：
- 所有异常必须携带 `message: str`
- 不直接抛出 `OrderError`（太宽泛）
- 仓储/服务层不静默吞异常

---

## 4. InMemoryOrderRepository

### 4.1 内部状态

```python
class InMemoryOrderRepository:
    def __init__(self):
        self._orders: dict[str, Order] = {}
        self._lock = threading.RLock()  # 允许重入
```

### 4.2 API 签名

```python
def create(self, order: Order) -> tuple[bool, Order]:
    """
    原子 check-and-set 创建订单。
    Returns:
        (True, order)  - 新建成功
        (False, existing) - 已存在且 payload 一致
    Raises:
        DuplicateOrderError - order_id 已存在但 payload 不一致
    """

def get(self, order_id: str) -> Order:
    """
    获取订单。
    Raises:
        OrderNotFoundError - 不存在
    """

def update(self, order_id: str, **fields) -> Order:
    """
    字段更新（仅允许 status, updated_at）。
    Raises:
        OrderNotFoundError
        OrderStateError - 状态非法转移
    """

def delete(self, order_id: str) -> bool:
    """
    物理删除（测试用）。
    Returns: True 删除成功 / False 不存在
    """

def exists(self, order_id: str) -> bool:
    """检查订单是否存在"""

def count(self) -> int:
    """返回订单总数（并发测试断言用）"""
```

### 4.3 线程安全保证
- 所有方法在 `with self._lock:` 内执行
- 使用 `RLock` 允许重入（`update` 内部调用 `get` 不死锁）
- 复合操作（`exists` + `create`）必须在同一锁内完成

### 4.4 payload 一致性判定
私有方法 `_is_same_payload(a: Order, b: Order) -> bool`：
- 比较 `user_id`, `items`, `amount` 三个字段
- `items` 使用 `==` 深比较

---

## 5. IdempotencyGuard

```python
class IdempotencyGuard:
    def __init__(self):
        self._locks: dict[str, threading.Lock] = {}
        self._meta_lock = threading.Lock()

    @contextmanager
    def lock_for(self, order_id: str):
        """
        为指定 order_id 获取细粒度锁。
        - 不同 order_id 之间不互斥
        - 同一 order_id 的所有操作串行化
        """
```

**实现要点**：
- 用 `self._meta_lock` 保护 `_locks` 字典的读写
- 逻辑：
  ```python
  with self._meta_lock:
      if order_id not in self._locks:
          self._locks[order_id] = threading.Lock()
      lock = self._locks[order_id]
  with lock:        # meta_lock 已释放
      yield
  ```
- 锁用完不主动删除（内存可接受；可选用 `weakref` 实现 LRU 优化）
- 严禁在持有 per-id 锁时再次获取 meta lock

---

## 6. OrderService

### 6.1 构造函数

```python
def __init__(self):
    self._repo = InMemoryOrderRepository()
    self._idempotency = IdempotencyGuard()
```

### 6.2 公开 API

#### create_order

```python
def create_order(
    self,
    order_id: str,
    user_id: str,
    items: list,
    amount: float,
) -> Order:
    """
    创建订单（幂等）。

    流程：
    1. 参数校验
    2. with idempotency.lock_for(order_id)
    3. 委托 repo.create()
    4. 返回 Order（新建 or 幂等命中）

    Returns: Order
    Raises:
        InvalidOrderError
        DuplicateOrderError
    """
```

#### get_order

```python
def get_order(self, order_id: str) -> Order:
    """
    查询订单（不加幂等锁，读可并发）。
    Raises:
        OrderNotFoundError
        InvalidOrderError - order_id 为空
    """
```

#### cancel_order

```python
def cancel_order(self, order_id: str, reason: str = "") -> Order:
    """
    取消订单（幂等：重复取消同 id 返回相同结果）。

    流程：
    1. 参数校验（order_id 非空）
    2. with idempotency.lock_for(order_id)
    3. repo.get(order_id) → 不存在抛 OrderNotFoundError
    4. status == CANCELLED → 直接返回（幂等命中）
    5. status in (PENDING, CONFIRMED) → update status=CANCELLED, updated_at=now
    6. 记录日志

    Returns: Order (status == CANCELLED)
    Raises:
        OrderNotFoundError
        InvalidOrderError
    """
```

### 6.3 参数校验

私有方法 `_validate_order_params(order_id, user_id, items, amount)`：
- `order_id` 为空/None → `InvalidOrderError("order_id must be non-empty")`
- `user_id` 为空/None → `InvalidOrderError("user_id must be non-empty")`
- `items` 为空 list / None → `InvalidOrderError("items must be non-empty")`
- `amount` 不是数字或 `<= 0` → `InvalidOrderError("amount must be positive")`

### 6.4 日志

```python
import logging
logger = logging.getLogger(__name__)
```

- `INFO`：订单创建/取消成功
- `WARNING`：幂等命中
- `ERROR`：业务异常

测试运行时不强制要求日志输出。

---

## 7. 测试规格

### 7.1 测试框架
- 使用 `unittest`（标准库，零外部依赖）
- 命名：`TestOrderServiceUnit` / `TestOrderServiceConcurrency`

### 7.2 TestOrderServiceUnit（≥ 8 个用例）

| 用例 | 断言 |
|------|------|
| `test_create_order_success` | 返回 Order，status=PENDING，count=1 |
| `test_create_order_idempotent_same_payload` | 同 id 同 payload 二次创建返回同一订单，不抛异常 |
| `test_create_order_duplicate_different_payload` | 同 id 不同 amount 抛 DuplicateOrderError |
| `test_get_order_success` | get_order 返回创建的订单 |
| `test_get_order_not_found` | 不存在 id 抛 OrderNotFoundError |
| `test_cancel_order_success` | PENDING → CANCELLED 成功 |
| `test_cancel_order_idempotent` | 重复取消同 id 不抛异常，返回 CANCELLED 订单 |
| `test_cancel_order_not_found` | 抛 OrderNotFoundError |
| `test_create_order_invalid_params` | 空 order_id、负 amount、空 items 各抛 InvalidOrderError |

### 7.3 TestOrderServiceConcurrency（≥ 3 个用例）

#### test_concurrent_create_same_id
```
- 启动 20 个线程
- 用 threading.Barrier(20) 同步释放
- 所有线程调用 service.create_order("order-X", "u1", ["item1"], 100.0)
- 断言：
    1. 无线程抛异常
    2. service._repo.count() == 1
    3. 所有线程返回的 Order.order_id 一致
```

#### test_concurrent_create_and_cancel
```
- 10 线程并发 create_order("order-Y", ...)
- 10 线程并发 cancel_order("order-Y")
- 用 Barrier 同步（20 个 worker）
- 断言：
    1. 无异常
    2. 最终 status == CANCELLED
    3. 仓储 count == 1
```

#### test_concurrent_different_ids
```
- 50 线程并发创建不同 order_id
- 断言：
    1. 无异常
    2. count == 50
    3. 全部 thread.join(timeout=10) 内完成（无死锁）
```

### 7.4 测试基础设施

```python
import unittest
import threading
import time
from order_service import (
    OrderService, Order, OrderStatus,
    OrderError, InvalidOrderError, OrderNotFoundError,
    DuplicateOrderError, OrderStateError,
)
```

### 7.5 断言风格
- 优先 `assertEqual`, `assertRaises`, `assertIsInstance`
- 不依赖具体时间戳值
- 每个测试 `setUp` 创建新 `OrderService` 实例
- 并发测试用 `Barrier` 同步而非 `sleep`

---

## 8. 行为契约示例

### 8.1 幂等命中
```python
svc = OrderService()
a = svc.create_order("O1", "u1", ["item1"], 100.0)
b = svc.create_order("O1", "u1", ["item1"], 100.0)
assert a.order_id == b.order_id
assert a.created_at == b.created_at
assert svc._repo.count() == 1
```

### 8.2 幂等冲突
```python
svc = OrderService()
svc.create_order("O2", "u1", ["a"], 50.0)
with self.assertRaises(DuplicateOrderError):
    svc.create_order("O2", "u1", ["a"], 999.0)  # amount 不同
```

### 8.3 取消幂等
```python
svc = OrderService()
svc.create_order("O3", "u1", ["x"], 10.0)
r1 = svc.cancel_order("O3")
r2 = svc.cancel_order("O3")
assert r1.status == OrderStatus.CANCELLED
assert r2.status == OrderStatus.CANCELLED
```

---

## 9. 实施检查清单

- [ ] `order_service.py` 包含所有 5 个组件（模型/异常/仓储/幂等/服务）
- [ ] 所有公开方法有 docstring
- [ ] 仓储使用 `RLock`
- [ ] 幂等层使用 per-id `Lock` + meta `Lock`
- [ ] 异常层级完整且无未捕获
- [ ] `test_order_service.py` 覆盖 8+ 单元用例 + 3+ 并发用例
- [ ] 并发测试使用 `Barrier` 同步
- [ ] 所有测试通过
- [ ] 代码总行数 200±30

---

## 10. 反模式（禁止）

- 全局单例锁（粒度太粗，违反 per-id 串行化目标）
- 锁内调用 logging 之外的 I/O
- 异常吞并（`except: pass`）
- 静默修改已存在订单的 payload
- 在 `__del__` 中释放锁（不可靠）
- 使用 `time.sleep` 模拟并发（用 `Barrier` 替代）
- 持有 per-id 锁时再次获取 meta lock（嵌套死锁）
