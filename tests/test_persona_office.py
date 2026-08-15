# -*- coding: utf-8 -*-
"""第二个 Persona Pack（office 上班族）验收：pack 加载 / 名字解析 / 世界连通 /
日程生成 / 模板渲染 / 检索可用。运行：python tests/test_persona_office.py
"""

import json
import os
import sys
import tempfile
import types


def test_office_pack():
    stub = types.ModuleType("openai")

    class _OpenAI:
        def __init__(self, *a, **k):
            self.chat = types.SimpleNamespace(completions=None)

    stub.OpenAI = _OpenAI
    sys.modules["openai"] = stub

    tmp = tempfile.mkdtemp(prefix="yuno_office_")
    cfg = {
        "memory": {
            "embedder": {"provider": "none"},
            "core": {
                "enabled": True,
                "persona_pack": {"pack": "office"},
                "mind": {"enabled": True},
                "schedule": {"enabled": True, "profile": "office"},
                "weather": {"enabled": False},
                "environment": {"enabled": False},
            },
        }
    }
    cfg_path = os.path.join(tmp, "config.json")
    with open(cfg_path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    os.environ["CONFIG_PATH"] = cfg_path
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    sys.path.insert(0, repo)

    import plugins._shared as _shared_mod
    # 套件顺序下 _shared 可能已被其他测试模块先导入，CONFIG_PATH 在模块级冻结 →
    # 显式重定向并重载，确保 office pack 生效、数据目录落在临时目录（测试隔离）
    _shared_mod.CONFIG_PATH = cfg_path
    _shared_mod.reload_config()

    from agent import persona
    from memory import pack, schedule, living
    from plugins import _db
    _db.init(tmp, force=True)  # 强制绑定本测试临时库

    checks = []

    def check(name, cond, extra=""):
        checks.append(bool(cond))
        print(("PASS" if cond else "FAIL"), name, extra if not cond else "")

    check("office-active", pack.active() == "office", pack.active())
    check("office-name", persona.persona_name() == "李小明", persona.persona_name())
    w = pack.world()
    check("office-world", "layout" in w and "items" in w and w.get("role"), list(w.keys()))
    check("office-role", "上班族" in str(w.get("role")))
    # 世界图连通
    rooms = list((w.get("layout") or {}).keys())
    edges = w.get("edges") or []
    adj = {}
    for a, b in edges:
        adj.setdefault(a, []).append(b)
        adj.setdefault(b, []).append(a)
    seen, q = {rooms[0]}, [rooms[0]]
    while q:
        cur = q.pop()
        for n in adj.get(cur, []):
            if n not in seen:
                seen.add(n)
                q.append(n)
    check("office-connected", len(seen) == len(rooms), (seen, rooms))
    # 日程：上班族周一早晨上班；夜晚槽全部在家
    plan = schedule.generate_week(schedule.profile(), "2026-W34")
    check("office-work-morning", plan[0][0] == "work", plan[0])
    night_out = [
        (wd, plan[wd][3])
        for wd in range(7)
        if not schedule.ACTIVITIES.get(plan[wd][3], {}).get("home", False)
    ]
    check("office-night-home", not night_out, night_out)
    # 模板渲染：换名字不露馅
    tpl = living.INSPECT_PROMPT.format(
        name="李小明", role=w.get("role", ""), room="客厅", container="茶几", items="空的",
    )
    check("office-template", "李小明" in tpl and "千石由乃" not in tpl, tpl[:60])
    # 检索可用（写入一条 office 记忆再召回）
    from memory import reasoning
    from plugins import _db
    _db.memory_add("ai", "test", "李小明上周在公司加班", "2026-08-06T20:00:00", None, 0.7, "test")
    from memory import lexical
    lexical.bm25_upsert("ai", "test", ["李小明上周在公司加班"])
    _db.lexicon_sync("ai", "test")
    hits = reasoning.retrieve("加班", ["ai"], top_k=3, min_score=0.0)
    check("office-retrieve", any("加班" in f for f, _s, _sc in hits), hits[:2])

    failed = [i for i, c in enumerate(checks) if not c]
    print("\nOFFICE RESULT:", "ALL PASS" if not failed else f"FAILED #{failed}", f"({len(checks)} checks)")
    assert not failed, f"failed: {failed}"


if __name__ == "__main__":
    test_office_pack()
    print("office pack OK")
