"""Memory SDK 核心：配置引导 + 统一记忆/纠错/轨迹/评测/导出接口。

设计：复用仓库内的 memory / agent / plugins 三层（数据层收敛在 plugins._db），
SDK 负责配置注入（CONFIG_PATH / LLM / 数据目录），一次进程绑定一个实例。
"""

import json
import os
import pathlib
import tempfile
from datetime import datetime

_DEFAULT_CORE = {
    "enabled": True,
    "top_k": 5,
    "min_score": 0.15,
    "world": {
        "enabled": True, "budget_chars": 400, "cache_ttl_s": 600,
        "llm_investigate": True, "investigate_throttle_s": 600,
    },
    "trace": {"enabled": True, "retention_days": 7},
    "weights": {
        "lexical": 0.6, "vector": 0.7, "graph": 0.4, "structured": 0.3,
        "rules": 0.5, "topics": 0.4, "policy": 0.25, "confidence": 0.2,
    },
    "policy": {
        "decay_days": 90, "prune_importance": 0.15, "ai_experience_min_importance": 0.75,
        "confirm_lr": 2.0, "dispute_lr": 0.3, "conflict_lr": 0.5,
        "forget_days": 120, "fuzzy_strength": 0.15,
        "promote_min_access": 3, "promote_min_importance": 0.6,
    },
}


class Memory:
    """统一记忆系统 SDK：一个进程一个实例（配置在导入时固定）。"""

    def __init__(
        self,
        config=None,
        data_dir=None,
        api_key=None,
        base_url="https://api.deepseek.com",
        model="deepseek-chat",
        embedder=None,
        persona=None,
    ):
        data_dir = pathlib.Path(data_dir or (pathlib.Path.cwd() / "data")).resolve()
        data_dir.mkdir(parents=True, exist_ok=True)
        cfg = self._build_config(config, data_dir, embedder)
        cfg_path = data_dir / "config.sdk.json"
        cfg_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")

        os.environ["CONFIG_PATH"] = str(cfg_path)
        os.environ.setdefault("DEEPSEEK_API_KEY", api_key or "")
        os.environ.setdefault("DEEPSEEK_BASE_URL", base_url)
        os.environ.setdefault("DEEPSEEK_MODEL", model)
        os.environ.setdefault("HF_HOME", str(data_dir / "hf_cache"))
        if persona:
            os.environ.setdefault("SYSTEM_PROMPT", pathlib.Path(persona).read_text(encoding="utf-8"))

        # 触发运行时初始化（plugins._shared 只初始化一次）
        from plugins import _shared  # noqa: F401
        import memory as _memory_mod
        import agent as _agent_mod
        import plugins._db as _db_mod

        self._memory = _memory_mod
        self._agent = _agent_mod
        self._shared = _shared
        self._db = _db_mod
        self.data_dir = data_dir

    @staticmethod
    def _build_config(config, data_dir, embedder):
        if config is None:
            base = {"allowed_paths": [str(data_dir)], "memory": {"embedder": {"provider": "none"}, "core": dict(_DEFAULT_CORE)}}
        elif isinstance(config, (str, os.PathLike)):
            base = json.loads(pathlib.Path(config).read_text(encoding="utf-8"))
        else:
            base = json.loads(json.dumps(config))
        mem = base.setdefault("memory", {})
        core = mem.setdefault("core", {})
        for k, v in _DEFAULT_CORE.items():
            core.setdefault(k, v)
        if embedder == "local":
            mem["embedder"] = {"provider": "local", "model": "BAAI/bge-small-zh-v1.5", "device": "auto"}
        elif isinstance(embedder, dict):
            mem["embedder"] = embedder
        base.setdefault("allowed_paths", [str(data_dir)])
        return base

    # ===== 记忆写入 / 纠错（纠错调查自动发生）=====
    def ingest(self, scope, text, reply="", facts=None, confidence=None):
        return self._memory.ingest(scope, "", text, reply, facts=facts, confidence=confidence)

    def add_fact(self, scope, key, fact, importance=0.5, confidence=0.8, source="sdk"):
        return self._memory.add_fact(scope, key, fact, importance=importance, confidence=confidence, source=source)

    # ===== 检索 =====
    def search(self, query, scopes, top_k=5, min_score=0.25, detailed=False):
        if detailed:
            return self._memory.retrieve_detailed(query, scopes, top_k=top_k, min_score=min_score)
        return self._memory.retrieve(query, scopes, top_k=top_k, min_score=min_score)

    # ===== 轨迹 / 人工评分 / 调整 =====
    def trace(self, scope=None, limit=50):
        return self._memory.trace_rows(scope or None, limit=limit)

    def review(self, trace_id, scores=None, comment="", reviewer="sdk"):
        return self._memory.trace_score(trace_id, scores, comment=comment, reviewer=reviewer)

    def adjustments(self):
        return self._memory.trace_adjustments(force=True)

    # ===== 目标 / 决策顾问 =====
    def goals(self, scope):
        return self._memory.goal_list(scope)

    def goal_add(self, scope, title, priority=3, motivation="", confidence=0.7, current_state=None):
        return self._memory.goal_add(
            scope, title, priority=priority, motivation=motivation,
            confidence=confidence, current_state=current_state,
        )

    def goal_update(self, scope, title, status=None, progress=None, note=None,
                    motivation=None, confidence=None, current_state=None):
        return self._memory.goal_update(
            scope, title, status=status, progress=progress, note=note,
            motivation=motivation, confidence=confidence, current_state=current_state,
        )

    def consult(self, scope, text):
        return self._memory.consult_turn(scope, text)

    # ===== 评测 / 治理 / 导出 =====
    def eval(self, probes, k=5):
        return self._memory.run_eval(probes, k=k)

    def governance(self, scope=None):
        return self._memory.governance_report(scope or None)

    def stats(self):
        return self._memory.stats()

    def export(self, out=None):
        import tarfile
        out = out or str(self.data_dir / f"sdk-export-{datetime.now():%Y%m%d-%H%M%S}.tar.gz")
        with tempfile.TemporaryDirectory() as tmpd:
            tmp = pathlib.Path(tmpd)
            self._db.backup_to(tmp / "bot.db")
            (tmp / "data.json").write_text(
                json.dumps(self._db.dump_all(), ensure_ascii=False), encoding="utf-8"
            )
            meta = {"version": "sdk", "exported_at": datetime.now().isoformat(timespec="seconds")}
            (tmp / "meta.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
            with tarfile.open(out, "w:gz") as tar:
                for f in tmp.iterdir():
                    tar.add(f, arcname=f.name)
        return out
