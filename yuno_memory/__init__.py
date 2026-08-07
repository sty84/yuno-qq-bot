"""yuno-memory SDK：统一记忆系统对外接口。

QQ 机器人（bot.py）是内置客户端；本 SDK 供任意 Python 程序 / Agent / 平台接入。
"""

from .core import Memory

__all__ = ["Memory"]
__version__ = "1.0.0"
