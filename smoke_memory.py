#!/usr/bin/env python3
"""一键冒烟：验证「核心记忆层（差距一）+ 主动自我编辑（差距二）」两条新链路。

用法（在项目根目录）：
    ./venv/bin/python smoke_memory.py

隔离性：
    - 用临时 data 目录初始化 DB，不碰生产 bot.db；
    - 只对真实 DeepSeek 做 1~2 次小调用（1 次连通性 ping + 1 次 active_edit 决策）；
    - 不改 config.json（active_edit 开关只在当前进程内临时打开）。

退出码：全部通过 0，有失败 1。
"""
import sys
import tempfile
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from plugins import _db  # noqa: E402  （先只导 _db：_shared 在模块级会 init 生产库，必须等 _db.init(tmp) 之后再导）


def main() -> int:
    results = []

    def check(name, ok, detail=""):
        results.append((name, ok))
        line = f"  {'✅' if ok else '❌'} {name}"
        if detail:
            line += f"  → {detail}"
        print(line)

    # ---- 隔离 DB ----
    tmp = tempfile.mkdtemp(prefix="smoke_mem_")
    # 关键顺序：必须先 _db.init(tmp)，再 import _shared。
    # _shared 模块级会执行 _db.init(DATA_DIR)；若此时 DB_PATH 已指向临时库，该 init 变 no-op，
    # 否则会误连生产 bot.db（之前冒烟脚本「读写了生产库」的 bug 根因就在这里）。
    _db.init(tmp)
    print(f"[db] 临时库（不碰生产数据）：{tmp}\n")

    from plugins import _shared  # noqa: E402

    # ---- 开关 active_edit（仅本进程）----
    _shared.CONFIG.setdefault("memory", {}).setdefault("core", {})["active_edit"] = {
        "enabled": True,
        "min_gain": 0.0,
    }

    # ============================================================
    print("=== 差距一：核心记忆层（热/温/冷）===")
    from memory import policy, context

    scope = "c2c:smoke"
    _db.memory_add(scope, "", "生日是八月十二号", confidence=0.8, mclass="short", source="user")
    _db.memory_add(scope, "", "今天吃了便当", confidence=0.7, mclass="short", source="user")

    n = policy.promote_core(scope)
    rows = {r["fact"]: r["mclass"] for r in _db.memory_rows(scope)}
    check("稳定事实「生日」升为核心", n >= 1 and rows.get("生日是八月十二号") == "core", f"升 {n} 条")
    check("过程事实「便当」不升核心", rows.get("今天吃了便当") == "short")

    block = context.core_memory_block(scope)
    check(
        "core_memory_block 常驻注入",
        "生日是八月十二号" in block and "便当" not in block,
    )
    if block:
        print("      └─ 注入片段：")
        for ln in block.splitlines():
            print(f"         {ln}")

    check("group scope 守卫（群聊不升核心）", policy.promote_core("group:123") == 0)
    check("重复 promote 幂等（不再升迁）", policy.promote_core(scope) == 0)

    # ============================================================
    print("\n=== 差距二：主动自我编辑（active_edit）===")
    from memory import controller

    # 2.1 确定性：_apply_ops 的 remember/forget（不依赖 LLM 判断）
    _db.memory_add("c2c:apply", "", "用户住在上海", confidence=0.8, mclass="core", source="user")
    applied = controller._apply_ops(
        "c2c:apply", "",
        [
            {"op": "remember", "fact": "用户住在深圳", "mclass": "core"},
            {"op": "forget", "fact": "用户住在上海"},
        ],
    )
    check(
        "_apply_ops remember+forget 正确应用",
        applied == {"remember": 1, "forget": 1},
        str(applied),
    )

    # 2.2 门控（确定性，不花 LLM）
    _shared.CONFIG["memory"]["core"]["active_edit"]["min_gain"] = 0.9
    low = controller.active_edit("c2c:gate", "", "哈哈", "哈哈")
    check("低信息消息被门槛拦截（省调用）", low.get("skipped") == "low_gain", str(low))
    _shared.CONFIG["memory"]["core"]["active_edit"]["min_gain"] = 0.0

    check("非私聊 scope 跳过", controller.active_edit("group:1", "", "x", "y").get("skipped") == "not_c2c")

    _shared.CONFIG["memory"]["core"]["active_edit"]["enabled"] = False
    check("默认关闭 → 直接返回 enabled=False", controller.active_edit("c2c:x", "", "x", "y") == {"enabled": False})
    _shared.CONFIG["memory"]["core"]["active_edit"]["enabled"] = True

    # 2.3 真实 LLM 往返
    print()
    if not _shared.DEEPSEEK_API_KEY:
        print("  ⚠️  未检测到 DEEPSEEK_API_KEY，跳过真实 LLM 检查（不影响确定性用例）")
    else:
        alive = False
        try:
            resp = _shared.deepseek_chat(
                messages=[{"role": "user", "content": "只回复两个字：正常"}],
                max_tokens=8, temperature=0, module="smoke", detail="ping",
            )
            alive = bool(resp.choices[0].message.content)
        except Exception as e:
            print(f"  ⚠️  LLM 连通失败：{e}")
        check("真实 DeepSeek 连通", alive)
        if alive:
            msg = "我下个月要搬到深圳去工作了"
            r = controller.active_edit("c2c:llm", "", msg, "那祝你一切顺利呀")
            ok = r.get("enabled") is True and isinstance(r.get("ops"), list)
            check("active_edit 真实 LLM 决策→应用", ok, f"applied={r.get('applied')}")
            ops = r.get("ops") or []
            if ops:
                print("      └─ LLM 决定的编辑：")
                for op in ops:
                    print(f"         - {op.get('op')}: {op.get('fact')}")
                written = _db.memory_rows("c2c:llm")
                if written:
                    print("      └─ 落库结果：")
                    for w in written:
                        print(f"         - {w['fact']!r} mclass={w['mclass']} src={w['source']}")
            else:
                print("      ⚠️  LLM 返回了空决策（可能模型没识别出可记信息，或见上方报错）")

    # ============================================================
    print()
    passed = sum(1 for _, ok in results if ok)
    total = len(results)
    print(f"===== 结果：{passed}/{total} 通过 =====")
    if passed == total:
        print("全部通过。可以在 config.json 的 memory.core 下加 active_edit.enabled=true 正式开启。")
        return 0
    print("有失败项，请核对上方 ❌ 行。")
    return 1


if __name__ == "__main__":
    sys.exit(main())
