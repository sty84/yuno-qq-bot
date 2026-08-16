"""动态数据层门面的类型桩：允许任意属性访问。

实际函数由 _db.py 在运行时按 YUNO_DB_BACKEND 装配。
"""
from typing import Any


def __getattr__(name: str) -> Any: ...
