# -*- coding: utf-8 -*-
"""端到端一致性测试（v31.2）：模拟多轮交互，检查状态层之间是否一致。
覆盖：生活细节回流 / 礼物隐私 / 嫌烦惩罚 / 久别重逢 / 话题锚点 / 情绪归因。
运行：python e2e_test.py
"""

import json
import os
import sys
import types
from datetime import datetime, timedelta

_openai_stub = types.ModuleType("openai")


class _OpenAI:
    def __init__(self, *args, **kwargs):
        self.chat = types.SimpleNamespace(completions=None)


_openai_stub.OpenAI = _OpenAI
sys.modules["openai"] = _openai_stub

WS = os.path.dirname(os.path.abspath(__file__))
cfg_dir = os.path.join(WS, "data", "_e2e")
os.makedirs(cfg_dir, exist_ok=True)
cfg = {
    "allowed_paths": [cfg_dir],
    "memory": {
        "embedder": {"provider": "none"},
        "core": {
            "enabled": True,
            "schedule": {"enabled": True, "profile": "yuno"},
            "weather": {"enabled": True, "provider": "qweather", "api_key_env": "WEATHER_API_KEY", "location_id": "101010100", "city": "北京", "ttl_s": 1800},
            "environment": {"enabled": True, "ttl_min": 60, "cast": ["小林"]},
            "living": {"enabled": True, "lazy_factor": 1.15, "daily_tick": True, "container_capacity": 10, "home_location": "北京·普通公寓"},
            "sharing": {"enabled": True, "threshold": 0.6, "half_life_hours": 8, "cooldown_hours": 3, "max_per_day": 10, "max_per_week": 20, "residual": 0.2, "check_interval_min": 15, "penalty_hours": 48, "penalty_step": 0.25},
            "sleep": {"enabled": True, "deep_window": [2, 5], "emergency_threshold": 2, "emergency_window_min": 30},
            "space": {"enabled": True, "pair_times": {"排练室:演出场地": 15}},
        },
    },
}
cfg_path = os.path.join(cfg_dir, "config.json")
with open(cfg_path, "w", encoding="utf-8") as f:
    json.dump(cfg, f, ensure_ascii=False, indent=2)
os.environ["CONFIG_PATH"] = cfg_path
sys.path.insert(0, WS)

import agent  # noqa: E402
from memory import emotion, living, relationship, sharing  # noqa: E402
from plugins import _db  # noqa: E402

checks = []


def check(name, cond, extra=""):
    checks.append(bool(cond))
    print(("PASS" if cond else "FAIL"), name, extra if not cond else "")


_db.kv_set("memory", "living_items", None)
_db.kv_set("memory", "sharing_state", None)
_db.kv_set("memory", "sharing_penalty:c2c:e2e", None)
_db.kv_set("memory", "ai_emotion", None)
_db.kv_set("memory", "lastmsg:c2c:e2e", None)
_db.kv_set("memory", "user_return:c2c:e2e", None)
_db.kv_set("memory", "space_position", None)
_db.kv_set("memory", "space_events", None)
_db._connect().execute("DELETE FROM sessions").fetchall()  # 清空会话，保证每次运行确定性
_db._connect().commit()

calls = []


def fake_llm(text, extra_context, history, system):
    calls.append({"text": text, "extra_context": extra_context})
    if "好久不见" in text:
        return "……好久不见。你还活着啊。"
    if "别老发" in text:
        return "……行，知道了。"
    return "我今天顺路买了白巧克力。"


# 1) 生活细节回流：AI 说买了白巧克力 → 物品表 +1
choc0 = living.find("白巧克力")[0]["qty"]
reply, _ = agent.ask("今天心情不错", scopes=["c2c:e2e"], llm=fake_llm, learn=False)
choc1 = living.find("白巧克力")[0]["qty"]
check("E2E-生活回流", choc1 == choc0 + 1, (choc0, choc1))

# 2) 礼物隐私：用户送的礼物记忆 audience=private
living.give("抱枕", 1)
row = next((r for r in _db.memory_rows("ai", "experience") if "抱枕" in r["fact"]), None)
check("E2E-礼物隐私", row is not None and row.get("audience") == "private", row)

# 3) 嫌烦惩罚走对话钩子
agent.ask("别老发消息了，烦", scopes=["c2c:e2e"], llm=fake_llm, learn=False)
check("E2E-嫌烦惩罚", sharing._penalty_mult("c2c:e2e") == 0.75, sharing._penalty_mult("c2c:e2e"))

# 4) 久别重逢：只提醒一次
_db.kv_set("memory", "lastmsg:c2c:e2e", (datetime.now() - timedelta(days=40)).isoformat(timespec="seconds"))
agent.ask("好久不见", scopes=["c2c:e2e"], llm=fake_llm, learn=False)
flag1 = _db.kv_get("memory", "user_return:c2c:e2e")
check("E2E-回归提醒一次", flag1 is not None and flag1.get("announced") is True, flag1)
again = relationship.note_return("c2c:e2e")
check("E2E-回归不重复", again is False)

# 5) 话题锚点注入
calls.clear()
agent.ask("MCP项目进展如何", scopes=["c2c:e2e"], llm=fake_llm, learn=False)
agent.ask("MCP项目呢", scopes=["c2c:e2e"], llm=fake_llm, learn=False)
check("E2E-话题锚点", len(calls) >= 2 and "【当前话题】" in calls[-1]["extra_context"], calls[-1]["extra_context"][:120])

# 6) 情绪归因
emotion.ai_apply({"emotion": "愤怒", "intent": "闲聊"}, "气死我了", scope="c2c:other")
ab_other = emotion.attribution_block("c2c:e2e")
ab_self = emotion.attribution_block("c2c:other")
check("E2E-情绪归因", "情绪归因" in ab_other and ab_self == "", (ab_other, ab_self))

failed = [i for i, c in enumerate(checks) if not c]
print("\nE2E RESULT:", "ALL PASS" if not failed else f"FAILED #{failed}", f"({len(checks)} checks)")
sys.exit(1 if failed else 0)
