# -*- coding: utf-8 -*-
"""测试默认使用 SQLite 隔离库，避免误连 PostgreSQL 生产库。
如需跑 PG 后端测试，可显式设置 YUNO_DB_BACKEND=postgresql 并配合独立测试库。
"""
import os

os.environ.setdefault("YUNO_DB_BACKEND", "sqlite")
