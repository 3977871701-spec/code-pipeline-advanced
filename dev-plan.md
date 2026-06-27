# 开发计划：高可用订单服务

## 项目概述

实现一个支持幂等性、线程安全的高可用订单服务，包含订单创建、查询、取消三大核心功能。同一 `order_id` 的重复请求保证只处理一次，并通过多线程并发测试验证幂等性。

**规模目标**：2 个文件，~200 行
- `order_service.py` (~150 行)
- `test_order_service.py` (~50-100 行)

---

## 模块划分

### 模块 1：数据模型层 (OrderStatus, Order)
- **职责**：定义订单数据结构和状态枚举
- **关键组件**：
  - `OrderStatus(Enum)`: `PENDING` / `CONFIRMED` / `CANCELLED`
  - `Order(@dataclass)`: `order_id`, `user_id`, `items`, `amount`, `status`, `created_at`, `updated_at`
- **文件位置**：`order_service.py`

### 模块 2：异常层 (Custom Exceptions)
- **职责**：定义业务异常类型，统一错误处理入口
- **关键类**：
  - `OrderError(Exception)`: 基础异常
  - `InvalidOrderError(OrderError)`: 参数无效
  - `OrderNotFoundError(OrderError)`: 订单不存在
  - `DuplicateOrderError(OrderError)`: 幂等冲突（同 id 不同 payload）
  - `OrderStateError(OrderError)`: 状态非法转移
- **文件位置**：`order_service.py`

### 模块 3：仓储层 (InMemoryOrderRepository)
- **职责**：线程安全的订单存储抽象
- **关键组件**：
  - `threading.RLock()` 保护内部 dict（允许重入，避免 update→get 死锁）
  - `create(order) -> (bool, Order)`: 原子 check-and-set
  - `get(order_id) -> Order`
  - `update(order_id, **fields) -> Order`
  - `delete(order_id) -> bool`
  - `exists(order_id) -> bool`
  - `count() -> int`
- **文件位置**：`order_service.py`

### 模块 4：幂等性层 (IdempotencyGuard)
- **职责**：per-order-id 细粒度锁，同 order_id 串行化处理
- **关键组件**：
  - `threading.Lock` 字典（按 order_id 维度分配）
  - `threading.Lock` 保护锁字典本身（meta lock）
  - `@contextmanager lock_for(order_id)` 上下文管理器
  - 关键约束：取锁后立即释放 meta_lock，避免死锁
- **文件位置**：`order_service.py`

### 模块 5：服务层 (OrderService)
- **职责**：对外暴露业务 API，编排参数校验→幂等锁→仓储调用
- **关键 API**：
  - `create_order(order_id, user_id, items, amount) -> Order`
  - `get_order(order_id) -> Order`
  - `cancel_order(order_id, reason="") -> Order`
  - 私有：`_validate_order_params(...)`
- **文件位置**：`order_service.py`

### 模块 6：单元测试 (TestOrderServiceUnit)
- **职责**：覆盖正常路径与异常路径
- **关键用例**：
  - 创建订单成功
  - 同 payload 重复创建幂等命中
  - 不同 payload 抛 DuplicateOrderError
  - 查询成功 / 查询不存在抛 OrderNotFoundError
  - 取消成功 / 重复取消幂等 / 取消不存在抛 OrderNotFoundError
  - 参数校验（空 order_id、负 amount、空 items）
- **文件位置**：`test_order_service.py`

### 模块 7：并发测试 (TestOrderServiceConcurrency)
- **职责**：用 `threading.Thread` + `Barrier` 验证幂等性
- **关键用例**：
  - 20 线程同 order_id 并发创建，断言仅 1 条记录、返回结果一致
  - 10 线程并发 create + 10 线程并发 cancel 同一 order_id
  - 50 线程不同 order_id 并发（无死锁）
- **文件位置**：`test_order_service.py`

---

## 依赖关系

```
模块 1 (模型) ← 模块 2 (异常) ← 模块 3 (仓储) ← 模块 4 (幂等) ← 模块 5 (服务)
                                                                         ↑
                                                              模块 6/7 (测试) 依赖全部
```

---

## 开发顺序

| Step | 模块 | 说明 | 预估行数 |
|------|------|------|----------|
| 1 | 异常 + 模型 | `OrderStatus`, `Order`, 所有 Exception | ~30 |
| 2 | 仓储层 | `InMemoryOrderRepository` + RLock | ~50 |
| 3 | 幂等层 | `IdempotencyGuard` per-id Lock + meta Lock | ~20 |
| 4 | 服务层 | `OrderService` 三个 API + 参数校验 + 日志 | ~50 |
| 5 | 单元测试 | `TestOrderServiceUnit`（8+ 用例） | ~50 |
| 6 | 并发测试 | `TestOrderServiceConcurrency`（3+ 用例） | ~30 |
| **合计** | | | **~230 行** |

---

## 关键设计决策

### 1. 幂等性实现（双层防护）
- **Layer 1（仓储）**：`create()` 内一次性 `if order_id in self._orders` 检查 + 比对 payload
  - 已存在 + payload 一致 → 返回 `(False, existing)`，调用方视为命中
  - 已存在 + payload 不一致 → 抛 `DuplicateOrderError`
- **Layer 2（服务）**：`IdempotencyGuard.lock_for(order_id)` per-id 锁，确保 read-check-write 原子
- **取消幂等**：CANCELLED 状态的订单重复取消直接返回，不抛异常
- **禁止行为**：不允许静默覆盖已存在订单的 payload

### 2. 线程安全（两级锁）
- **仓储全局 `RLock`**：保护 `_orders` dict 的复合操作
- **幂等层 per-id `Lock`**：不同 order_id 之间不互斥
- **meta `Lock`**：保护 `_locks` 字典本身
- **锁顺序**：先取 per-id 锁，再在锁内调仓储（仓储内部取 RLock），禁止反向
- **释放保证**：统一用 `with` 上下文管理器，异常路径自动释放

### 3. 错误处理
- 入口参数校验：空字符串/None/负数金额/空 items → `InvalidOrderError`
- 状态机校验：CANCELLED 为终态，禁止任何后续修改
- 所有异常继承自 `OrderError`，便于上层统一捕获
- 仓储/服务层**不吞异常**，只重新抛出或透传

### 4. 可观测性
- 使用 `logging` 模块记录：
  - `INFO`：订单创建/取消成功
  - `WARNING`：幂等命中
  - `ERROR`：业务异常
- 测试运行时不强制要求日志输出，但实现必须包含

---

## 性能与正确性目标

| 指标 | 目标 |
|------|------|
| 单元测试用例数 | ≥ 8 |
| 并发测试用例数 | ≥ 3 |
| 20 线程同 id 创建最终记录数 | 1 |
| 50 线程不同 id 并发完成时间 | < 5s |
| 死锁防护 | 全部 thread.join(timeout=10) |
| 代码总行数 | 200±30 |

---

## 风险与缓解

| 风险 | 缓解措施 |
|------|----------|
| 死锁（per-id lock + repo RLock 顺序） | 严格顺序：per-id 锁 → 仓储 RLock，禁止反向 |
| 重复锁（仓储 update→get 重入） | 仓储使用 `RLock` 而非 `Lock` |
| 异常路径锁未释放 | 全部使用 `with` 上下文管理器 |
| 幂等命中与业务冲突混淆 | 严格 payload 比对（user_id/items/amount），不一致才报错 |
| meta lock 与 per-id lock 嵌套死锁 | 取完 per-id 锁后立即释放 meta lock（yield 前 release） |
| 并发测试假阳性 | 使用 `Barrier(20)` 同步释放，最大化竞争窗口 |

---

## 交付物

- `/Users/xylei/code-pipeline-projects/advanced/order_service.py`（~150 行）
- `/Users/xylei/code-pipeline-projects/advanced/test_order_service.py`（~50-100 行）

**严禁**创建额外文件、引入第三方依赖（仅用 Python 标准库 `unittest` / `threading` / `logging` / `dataclasses` / `enum` / `time`）。
