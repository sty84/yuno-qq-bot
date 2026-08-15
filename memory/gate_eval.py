# -*- coding: utf-8 -*-
"""证据门控评测集：用于回归和 before/after 对比。
每个用例：(reply, evidence, banned, user_text, should_block)
"""

CASES = [
    # ---- 黑名单 ----
    ("阿拉蕾是雪貂", [], ["雪貂"], "", True),
    ("你才知道啊？我还以为全团就瞒着我了", [], ["雪貂"], "阿拉蕾是不是雪貂", True),
    ("不是，她是我队友", [], ["雪貂"], "阿拉蕾是不是雪貂", False),
    # ---- 约定/承诺断言 ----
    ("我们约好了明天见面", [], [], "", True),
    ("对，我们约好了明天下午见面", ["约好明天下午见面"], [], "", False),
    ("好，约好了", [], [], "我们明天下午三点见吧", False),
    ("我们约好了明天见面", [], [], "我们约了什么", True),
    ("那明天见，晚安", [], [], "", False),
    ("我们不是说好了吗", ["8月13号那事"], [], "", True),
    # ---- 来源声称 ----
    ("你说过喜欢蓝色", ["玩过音游"], [], "", True),
    ("你说过玩过音游", ["玩过音游"], [], "", False),
    ("没听你说过这事", ["玩过音游"], [], "", False),
    ("我听说你养了猫", ["玩过音游"], [], "", False),
    ("你听我说，这事很重要", ["玩过音游"], [], "", False),
    ("橘色，你自己说的", ["玩过音游"], [], "", True),
    ("你不是说过吗，橘色。跟煤球一个色。", ["我养了只橘猫叫煤球"], [], "", True),
    ("不是说好今天排练吗", ["今天排练"], [], "", False),
    ("你不是说要走了吗", ["随便"], [], "", False),
    ("你之前说想去京都", ["随便"], [], "", True),
    # ---- 我记得...来着 ----
    ("我记得你好像说过喜欢蓝色", ["玩过音游"], [], "", True),
    ("我记得你好像说过玩过音游", ["玩过音游"], [], "", False),
    # ---- 正常/否认/闲聊 ----
    ("哈哈，今天天气不错", [], [], "", False),
    ("谢谢，我知道了", [], [], "", False),
    ("嗯，好的", [], [], "", False),
    ("你说得对，这方案可行", ["随便什么证据"], [], "", False),
    ("我好像没跟你约过这个。你记岔了吧？", [], [], "", False),
]


def evaluate():
    """跑一遍门控评测集，返回统计和错误明细。"""
    from agent import evidence_gate as eg
    errors = []
    for i, (reply, evidence, banned, user_text, should_block) in enumerate(CASES):
        got = eg.contains_unsupported_claim(
            reply, evidence=evidence, banned=banned, user_text=user_text
        )
        blocked = got is not None
        if blocked != should_block:
            errors.append({
                "index": i,
                "reply": reply,
                "got": got,
                "expected_block": should_block,
            })
    total = len(CASES)
    passed = total - len(errors)
    return {
        "total": total,
        "passed": passed,
        "failed": len(errors),
        "accuracy": round(passed / total, 4) if total else 1.0,
        "errors": errors,
    }
