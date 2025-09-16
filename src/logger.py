import logging
import os

# 日志目录和文件
LOG_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'logs')
OPERATIONS_LOG = os.path.join(LOG_DIR, 'operations.txt')
ORDERS_LOG = os.path.join(LOG_DIR, 'orders.md')

os.makedirs(LOG_DIR, exist_ok=True)

# 操作日志
operations_logger = logging.getLogger('operations')
operations_logger.setLevel(logging.INFO)
operations_handler = logging.FileHandler(OPERATIONS_LOG, encoding='utf-8')
operations_handler.setFormatter(logging.Formatter('%(message)s'))
operations_logger.addHandler(operations_handler)

# 开仓日志
orders_logger = logging.getLogger('orders')
orders_logger.setLevel(logging.INFO)
orders_handler = logging.FileHandler(ORDERS_LOG, encoding='utf-8')
orders_handler.setFormatter(logging.Formatter('%(message)s'))
orders_logger.addHandler(orders_handler)
