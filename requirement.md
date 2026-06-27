# 需求：高可用订单服务（advanced 简化版）
2 个文件 ~200 行：
- order_service.py: 订单创建/查询/取消，幂等（同一 order_id 重复请求只处理一次）
- test_order_service.py: 单元 + 并发测试（多线程同 order_id 测试幂等性）
要求：完整错误处理，幂等设计，线程安全。
