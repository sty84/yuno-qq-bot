# -*- coding: utf-8 -*-
"""证据门控小型评测集：防止手写正则越改越偏。
每个用例记录：回复 / 证据 / 黑名单 / 用户消息 / 是否应拦截。
"""

from agent import evidence_gate as eg

CASES = [
    # (reply, evidence, banned, user_text, should_block)
    ("阿拉蕾是雪貂", [], ["雪貂"], "", True),
    ("我们约好了明天见面", [], [], "", True),
    ("那明天见，晚安", [], [], "", False),
    ("对，我们约好了明天下午见面", ["约好明天下午见面"], [], "", False),
    ("你说过喜欢蓝色", ["玩过音游"], [], "", True),
    ("你说过玩过音游", ["玩过音游"], [], "", False),
    ("没听你说过这事", ["玩过音游"], [], "", False),
    ("橘色，你自己说的", ["玩过音游"], [], "", True),
    ("你不是说过吗，橘色。跟煤球一个色。", ["我养了只橘猫叫煤球"], [], "", True),
    ("你说的对，我记下了", ["随便什么证据"], [], "", False),
    ("好，约好了", [], [], "我们明天下午三点见吧", False),
    ("我们约好了明天见面", [], [], "我们约了什么", True),
]


def test_gate_eval_set():
    failed = []
    for i, (reply, evidence, banned, user_text, should_block) in enumerate(CASES):
        got = eg.contains_unsupported_claim(
            reply, evidence=evidence, banned=banned, user_text=user_text
        )
        blocked = got is not None
        if blocked != should_block:
            failed.append((i, reply, got, should_block))
    assert not failed, failed
