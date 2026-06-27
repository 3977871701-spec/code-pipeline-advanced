# 高可用订单服务（Advanced）

## 项目简介

这是一个高可用的内存订单服务，属于代码流水线（Code Pipeline）的 **Advanced 难度** 级别。相比 Standard 难度，本项目在架构上引入了更细粒度的分层设计：

- **独立仓储层**（`InMemoryOrderRepository`）：线程安全的订单存储抽象，支持 CRUD + 状态机校验
- **独立幂等层**（`IdempotencyGuard`）：per-order-id 细粒度锁，不同订单 ID 之间不互斥，最大化并发吞吐
- **日志可观测性**：集成 `logging` 模块，记录 INFO/WARNING/ERROR 级别的业务事件

服务提供订单的创建、查询、取消三大核心能力，所有操作均保证幂等性和线程安全。测试报告显示 19 个用例全部通过（16 个单测 + 3 个并发），包括 20 线程同 ID 竞争创建、20 线程混合创建/取消、50 线程不同 ID 并发等高压力场景。

## 功能特性

### 订单管理
- **创建订单** `create_order(order_id, user_id, items, amount)`：幂等创建，同 ID 同参数返回同一实例
- **查询订单** `get_order(order_id)`：按 ID 查询，不加幂等锁（读可并发）
- **取消订单** `cancel_order(order_id, reason="")`：幂等取消，支持附带取消原因

### 幂等设计（双层防护）

**Layer 1 — 仓储层**：`InMemoryOrderRepository.create()` 内部原子 check-and-set
- 已存在 + payload 一致 → 返回 `(False, existing)`
- 已存在 + payload 不一致 → 抛 `DuplicateOrderError`

**Layer 2 — 幂等层**：`IdempotencyGuard.lock_for(order_id)` per-id 细粒度锁
- 不同 `order_id` 之间不互斥，最大化并发
- 同一 `order_id` 的所有操作严格串行化
- meta lock 仅保护锁字典的分配，取完 per-id 锁后立即释放

### 线程安全（两级锁）

```
请求 → IdempotencyGuard (per-id Lock) → InMemoryOrderRepository (RLock) → 内存 dict
```

- **per-id Lock**：不同订单 ID 可并行处理
- **仓储 RLock**：允许重入（`update` 内部调用 `get` 不死锁）
- **锁顺序**：先 per-id 锁 → 再仓储 RLock，严格单向，禁止反向嵌套

### 状态机

```
PENDING   ──cancel──▶ CANCELLED（终态）
PENDING   ──confirm─▶ CONFIRMED
CONFIRMED ──cancel──▶ CANCELLED（终态）
CANCELLED ──✗──▶ 禁止任何变更
```

### 异常体系

| 异常类 | 触发场景 |
|--------|----------|
| `InvalidOrderError` | 参数无效（空 ID、负金额、空 items 等） |
| `OrderNotFoundError` | 订单不存在 |
| `DuplicateOrderError` | 同 ID 不同 payload 的幂等冲突 |
| `OrderStateError` | 状态非法转移（如 CANCELLED → PENDING） |

所有异常均继承自 `OrderError(Exception)`，便于上层统一捕获。

### 可观测性

- `logging.INFO`：订单创建/取消成功
- `logging.WARNING`：幂等命中（重复请求）
- `logging.ERROR`：业务异常

## 技术栈

| 技术 | 说明 |
|------|------|
| **语言** | Python 3.11+ |
| **依赖** | 无第三方依赖，仅使用标准库 |
| **并发原语** | `threading.RLock`（仓储层）+ `threading.Lock`（幂等层） |
| **设计模式** | Repository Pattern + Idempotency Guard + Context Manager |
| **数据结构** | `dataclasses`、`enum.Enum` |
| **测试框架** | `unittest` + `threading.Barrier` |
| **测试规模** | 19 个用例（16 单测 + 3 并发），0.004s 完成 |

### 项目结构

```
advanced/
├── requirement.md              # 需求文档
├── SPEC.md                     # 详细技术规格说明（406 行）
├── dev-plan.md                 # 开发计划
├── order_service.py            # 业务实现（178 行，含 5 个组件）
├── test_order_service.py       # 单元 + 并发测试（278 行）
├── test_report.md              # 测试报告
└── README.md                   # 本文件
```

### 架构分层

```
┌─────────────────────────────────────────────────────┐
│                   OrderService                       │
│         （对外 API：create/get/cancel）                │
├──────────────────┬──────────────────────────────────┤
│ IdempotencyGuard │   InMemoryOrderRepository        │
│  per-id Lock     │   RLock + dict                   │
│  meta Lock       │   CRUD + 状态机校验               │
├──────────────────┴──────────────────────────────────┤
│              数据模型 + 异常体系                       │
│   OrderStatus / Order / OrderError 子类              │
└─────────────────────────────────────────────────────┘
```

## 使用方法

### 运行测试

```bash
cd /Users/xylei/code-pipeline-projects/advanced
python3 -m unittest test_order_service.py -v
```

预期输出：
```
test_concurrent_create_same_id ... ok
test_concurrent_create_and_cancel ... ok
test_concurrent_different_ids ... ok
test_create_order_duplicate_different_payload ... ok
test_create_order_idempotent_same_payload ... ok
test_create_order_invalid_params ... ok
test_create_order_success ... ok
test_cancel_order_idempotent ... ok
test_cancel_order_invalid_id ... ok
test_cancel_order_not_found ... ok
test_cancel_order_success ... ok
test_exception_hierarchy ... ok
test_get_order_invalid_id ... ok
test_get_order_not_found ... ok
test_get_order_success ... ok
test_idempotency_guard_rejects_empty_id ... ok
test_repository_rejects_unknown_update_fields ... ok
test_repository_state_machine_confirmed_to_pending_forbidden ... ok
test_repository_state_machine_terminal ... ok

----------------------------------------------------------------------
Ran 19 tests in 0.004s

OK
```

### 作为模块调用

```python
from order_service import (
    OrderService, Order, OrderStatus,
    InvalidOrderError, OrderNotFoundError,
    DuplicateOrderError, OrderStateError,
)

# 创建服务
svc = OrderService()

# 创建订单（幂等）
order1 = svc.create_order("ORD-001", "user-1", ["item-A", "item-B"], 99.99)
order2 = svc.create_order("ORD-001", "user-1", ["item-A", "item-B"], 99.99)
assert order1 is order2  # 幂等命中，同一实例

# 同 ID 不同参数 → 冲突
try:
    svc.create_order("ORD-001", "user-1", ["item-A"], 50.0)
except DuplicateOrderError as e:
    print(f"冲突: {e}")

# 查询
order = svc.get_order("ORD-001")

# 取消（幂等）
cancelled = svc.cancel_order("ORD-001", reason="用户取消")
cancelled_again = svc.cancel_order("ORD-001")  # 不报错

# 参数校验
try:
    svc.create_order("", "user-1", ["item"], 10.0)
except InvalidOrderError as e:
    print(f"参数错误: {e}")
```
