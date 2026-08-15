# -*- coding: utf-8 -*-
"""P0-4b: LLM 输出 JSON 解析公共工具（消除各模块重复的 find/rfind + loads）。

用法：data = parse_json_object(raw)；data is None 表示无有效 JSON 对象（原各模块
start<0 / json.loads 失败 / 非 dict 三条路径的统一）。
"""

import json


def parse_json_object(raw) -> dict | None:
    """从 LLM 输出中提取第一个 JSON 对象；无对象/解析失败/非对象返回 None。"""
    if raw is None:
        return None
    s = str(raw).find("{")
    e = str(raw).rfind("}")
    if s < 0 or e <= s:
        return None
    try:
        data = json.loads(str(raw)[s:e + 1])
        return data if isinstance(data, dict) else None
    except Exception:
        return None
