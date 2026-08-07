"""隐私模块：敏感信息检测（命中则标高隐私/检索过滤）+ 可选 AES-GCM 加密存储。"""

import os
import re

SENSITIVE_PATTERNS = [
    (r"\d{11}", "手机号"),
    (r"\d{15,18}[Xx]?", "身份证"),
    (r"\d{16,19}", "银行卡"),
    (r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}", "邮箱"),
    (r"密码|口令|验证码|密钥|token|Token|PIN码", "凭据"),
    (r"住址|门牌|小区|栋|单元|楼层|几零几", "住址"),
    (r"工资|月薪|年薪|存款|欠款|借款|理财|股票|基金|资产|负债|流水|房产", "财务"),
    (r"微信号|QQ号|支付宝|银行账号|银行卡号|收款码", "账号"),
    (r"护照|驾照|社保|医保|社保卡|工号|工牌", "证件"),
    (r"病历|体检报告|过敏史|处方|住院|手术|诊断", "健康"),
]

SENSITIVE_WORDS = [
    "家庭住址", "身份证", "手机号", "银行卡", "密码", "验证码", "工资", "生病", "住院",
    "微信号", "支付宝", "银行账号", "社保", "医保", "病历", "体检", "过敏", "护照",
    "驾照", "房产", "股票", "理财", "基金", "资产", "邮箱",
]


def detect(text) -> tuple[float, list[str]]:
    """返回 (隐私分 0~1, 命中标签列表)。"""
    text = str(text or "")
    labels = []
    for pat, label in SENSITIVE_PATTERNS:
        if re.search(pat, text):
            labels.append(label)
    for w in SENSITIVE_WORDS:
        if w in text:
            labels.append(w)
    if not labels:
        return 0.0, []
    return min(1.0, 0.6 + 0.1 * len(labels)), list(dict.fromkeys(labels))[:4]


# ===== 可选字段加密：配置 MEMORY_KEY 且安装 cryptography 时对高隐私记忆 AES-GCM 加密 =====
_key = None


def available() -> bool:
    global _key
    if _key is not None:
        return True
    secret = os.getenv("MEMORY_KEY", "")
    if not secret:
        return False
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
        import hashlib
        _key = AESGCM(hashlib.sha256(secret.encode("utf-8")).digest())
        return True
    except Exception:
        return False


def encrypt_text(text) -> str:
    if not available():
        return str(text)
    try:
        nonce = os.urandom(12)
        ct = _key.encrypt(nonce, str(text).encode("utf-8"), None)
        return "enc:" + nonce.hex() + ":" + ct.hex()
    except Exception:
        return str(text)


def decrypt_text(text) -> str:
    if not isinstance(text, str) or not text.startswith("enc:"):
        return text
    if not available():
        return text
    try:
        _, nonce_hex, ct_hex = text.split(":", 2)
        return _key.decrypt(bytes.fromhex(nonce_hex), bytes.fromhex(ct_hex), None).decode("utf-8")
    except Exception:
        return text
