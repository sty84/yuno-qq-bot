# -*- coding: utf-8 -*-
"""轻量负载测试（v31.2）：模拟并发消息走 分析→情绪判断→观测→分享钩子，检查无异常与耗时。
运行：python load_test.py [并发数，默认 200]
"""

import json
import os
import sys
import threading
import time
import types


def main():

    _openai_stub = types.ModuleType("openai")


    class _OpenAI:
        def __init__(self, *args, **kwargs):
            self.chat = types.SimpleNamespace(completions=None)


    _openai_stub.OpenAI = _OpenAI
    sys.modules["openai"] = _openai_stub

    WS = os.path.dirname(os.path.abspath(__file__))
    cfg_dir = os.path.join(WS, "data", "_load")
    os.makedirs(cfg_dir, exist_ok=True)
    cfg = {
        "allowed_paths": [cfg_dir],
        "memory": {"embedder": {"provider": "none"}, "core": {"enabled": True,
                                                              "schedule": {"enabled": True, "profile": "yuno"},
                                                              "sharing": {"enabled": True, "penalty_hours": 48},
                                                              "living": {"enabled": True}}},
    }
    cfg_path = os.path.join(cfg_dir, "config.json")
    with open(cfg_path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)
    os.environ["CONFIG_PATH"] = cfg_path
    sys.path.insert(0, WS)

    import memory  # noqa: E402
    from memory import analysis, emotion, sharing  # noqa: E402

    N = int(sys.argv[1]) if len(sys.argv) > 1 else 200
    TEXTS = [
        "气死我了", "今天好开心", "有点烦", "哈哈", "最近压力好大", "嗯", "别老发消息了",
        "MCP项目进展如何", "胃好疼", "太棒了", "离谱", "我好难过",
    ]

    errors = []


    def worker(i):
        try:
            t = TEXTS[i % len(TEXTS)]
            an = analysis.analyze(t)
            emotion.user_observe("c2c:load", an, t)
            sharing.on_conversation(an, t, "c2c:load")
            emotion.user_estimate("c2c:load")
        except Exception as e:
            errors.append((i, repr(e)))


    t0 = time.time()
    threads = [threading.Thread(target=worker, args=(i,)) for i in range(N)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()
    elapsed = time.time() - t0

    print(f"并发 {N} 条消息：{elapsed:.2f}s（约 {N / max(elapsed, 0.001):.0f} 条/秒）")
    print("错误数:", len(errors))
    for e in errors[:5]:
        print(" ", e)
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
