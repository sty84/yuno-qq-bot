# -*- coding: utf-8 -*-
"""Persona Pack 通用性核心套件：夹具全部从当前 pack 读取（物品/房间/名字/cast）。
运行：python tests/pack_core_suite.py --pack <yuno|office>"""

import argparse
import json
import os
import sys
import tempfile
import types


def build_config(pack):
    return {
        "memory": {
            "embedder": {"provider": "none"},
            "core": {
                "enabled": True,
                "persona_pack": {"pack": pack},
                "mind": {"enabled": True},
                "schedule": {"enabled": True},
                "weather": {"enabled": False},
                "environment": {"enabled": False},
            },
        }
    }


def run_for_pack(pack_name):
    stub = types.ModuleType("openai")

    class _OpenAI:
        def __init__(self, *a, **k):
            self.chat = types.SimpleNamespace(completions=None)

    stub.OpenAI = _OpenAI
    sys.modules["openai"] = stub

    tmp = tempfile.mkdtemp(prefix=f"yuno_pack_{pack_name}_")
    cfg_path = os.path.join(tmp, "config.json")
    with open(cfg_path, "w", encoding="utf-8") as f:
        json.dump(build_config(pack_name), f, ensure_ascii=False, indent=2)
    os.environ["CONFIG_PATH"] = cfg_path
    repo = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    sys.path.insert(0, repo)

    from agent import persona
    from memory import lexical, living, reasoning, schedule
    from memory import pack as pack_mod
    from plugins import _db

    checks = []

    def check(name, cond, extra=""):
        checks.append(bool(cond))
        print(("PASS" if cond else "FAIL"), name, extra if not cond else "")

    w = pack_mod.world()
    layout = w.get("layout") or {}
    rooms = list(layout.keys())
    items = w.get("items") or []
    name = persona.persona_name()
    check("pack-active", pack_mod.active() == pack_name, pack_mod.active())
    check("name", bool(name), name)
    check("world", bool(rooms and items and w.get("role")), (rooms, w.get("role")))
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
    check("connected", len(seen) == len(rooms), (seen, rooms))
    if items:
        it = items[0]
        r = living.move_item(it["name"], it["room"], it["container"])
        check("item-move", r.get("ok"), r)
        check("position", living.position_at(it["name"]).get("known"))
    plan = schedule.generate_week(schedule.profile(), "2026-W34")
    night_out = [
        (wd, plan[wd][3])
        for wd in range(7)
        if not schedule.ACTIVITIES.get(plan[wd][3], {}).get("home", False)
    ]
    check("night-home", not night_out, night_out)
    tpl = living.INSPECT_PROMPT.format(
        name=name, role=w.get("role", ""),
        room=rooms[0], container=(layout.get(rooms[0]) or {}).get("furniture", ["茶几"])[0],
        items="空的",
    )
    check("template", name in tpl, tpl[:60])
    fact = f"{name}上周在公司加班"
    _db.memory_add("ai", "test", fact, "2026-08-06T20:00:00", None, 0.7, "test")
    lexical.bm25_upsert("ai", "test", [fact])
    _db.lexicon_sync("ai", "test")
    hits = reasoning.retrieve("加班", ["ai"], top_k=3, min_score=0.0)
    check("retrieve", any("加班" in f for f, _s, _sc in hits), hits[:2])

    failed = [i for i, c in enumerate(checks) if not c]
    print(f"\nPACK[{pack_name}] RESULT:", "ALL PASS" if not failed else f"FAILED #{failed}", f"({len(checks)} checks)")
    return not failed


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--pack", default="yuno")
    args = parser.parse_args()
    ok = run_for_pack(args.pack)
    sys.exit(0 if ok else 1)
