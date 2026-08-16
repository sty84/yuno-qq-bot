# -*- coding: utf-8 -*-
"""PostgreSQL 数据层门面：统一装配 core + memory + ops，供 plugins._db 使用。

外部可直接 `from plugins import _db_pg`，接口与 SQLite 后端保持一致。
"""
from plugins import _db_pg_core as _core
from plugins import _db_pg_memory as _memory
from plugins import _db_pg_ops as _ops


def _install(module):
    for _name in dir(module):
        if not _name.startswith("__"):
            globals()[_name] = getattr(module, _name)


_install(_core)
_install(_memory)
_install(_ops)
