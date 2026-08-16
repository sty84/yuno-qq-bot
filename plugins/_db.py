# -*- coding: utf-8 -*-
"""统一数据层门面：按 YUNO_DB_BACKEND 装配 SQLite 或 PostgreSQL 实现。

外部统一 `from plugins import _db`，接口名由各后端模块保持一致。
"""
import os


def _install(module):
    for _name in dir(module):
        if not _name.startswith("__"):
            globals()[_name] = getattr(module, _name)


if os.getenv("YUNO_DB_BACKEND", "postgresql").strip().lower() == "postgresql":
    from plugins import _db_pg as _impl
else:
    from plugins import _db_sqlite_core as _core
    from plugins import _db_sqlite_memory as _memory
    from plugins import _db_sqlite_ops as _ops

    class _Impl:
        pass

    _impl = _Impl()
    for _mod in (_core, _memory, _ops):
        for _name in dir(_mod):
            if not _name.startswith("__"):
                setattr(_impl, _name, getattr(_mod, _name))

_install(_impl)
