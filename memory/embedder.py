"""Embedding 提供方（可插拔）+ 向量相似度工具。
当前实现：
- none（关闭）
- openai_compatible（OpenAI 兼容接口，如硅基流动、OpenAI、腾讯云等）
- local（进程内本地计算，sentence-transformers；GPU/CPU 自动选择）
研究新提供方时替换本文件或新增 provider 分支。"""

import os

from plugins import _shared

_client = None
_attempted = False
_local_model = None
_local_attempted = False


def _cfg():
    return _shared.CONFIG.get("memory", {}).get("embedder", {}) or {}


def enabled() -> bool:
    return _cfg().get("provider", "none") not in ("none", "")


def _local_encode(texts) -> list[list[float]] | None:
    """本地向量化：sentence-transformers 进程内加载模型。
    首次运行需联网下载模型到本地缓存，之后完全离线可用。"""
    global _local_model, _local_attempted
    cfg = _cfg()
    if _local_model is None and not _local_attempted:
        _local_attempted = True
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError:
            print("local embedder 需要安装依赖：pip install sentence-transformers")
            return None
        device = str(cfg.get("device") or "auto")
        if device == "auto":
            try:
                import torch
                device = "cuda" if torch.cuda.is_available() else "cpu"
            except Exception:
                device = "cpu"
        try:
            _local_model = SentenceTransformer(
                str(cfg.get("model") or "BAAI/bge-small-zh-v1.5"),
                device=device,
            )
        except Exception as e:
            print(f"local embedder 加载模型失败：{e}")
            return None
    if _local_model is None:
        return None
    try:
        vecs = _local_model.encode(
            [str(t) for t in texts],
            normalize_embeddings=True,
            show_progress_bar=False,
        )
        return [v.tolist() if hasattr(v, "tolist") else list(v) for v in vecs]
    except Exception as e:
        print(f"local embedding 失败：{e}")
        return None


def embed(texts) -> list[list[float]] | None:
    """批量向量化；未配置或失败时返回 None（调用方降级为关键词检索）。"""
    if not texts or not enabled():
        return None
    if _cfg().get("provider") == "local":
        return _local_encode(texts)
    global _client, _attempted
    cfg = _cfg()
    if _client is None and not _attempted:
        _attempted = True
        try:
            from openai import OpenAI as _OpenAI
            _client = _OpenAI(
                api_key=os.getenv(cfg.get("api_key_env", "EMBEDDING_API_KEY"), "") or None,
                base_url=cfg.get("base_url") or None,
            )
        except Exception as e:
            print(f"embedder 初始化失败：{e}")
            return None
    if _client is None:
        return None
    try:
        resp = _client.embeddings.create(
            model=cfg.get("model", "text-embedding-3-small"),
            input=[str(t) for t in texts],
        )
        return [d.embedding for d in resp.data]
    except Exception as e:
        print(f"embedding 失败：{e}")
        return None


def cosine(a, b) -> float:
    """余弦相似度（向量打分与事件建边共用）。"""
    dot = sum(x * y for x, y in zip(a, b))
    na = sum(x * x for x in a) ** 0.5
    nb = sum(x * x for x in b) ** 0.5
    return dot / (na * nb) if na and nb else 0.0
