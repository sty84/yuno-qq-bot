#!/usr/bin/env python3
"""PostgreSQL 功能验证：数据量、词法检索、融合检索、向量检索。

用法：
  YUNO_DB_BACKEND=postgresql YUNO_PG_DB=yuno_verify python scripts/verify_pg_functional.py
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from plugins import _db
from memory import lexical, reasoning, vecindex

SCOPE = os.getenv("YUNO_PG_SCOPE", "c2c:B8898E1A9DEA134FBF303C9442E194DB")


def main():
    print("PG DB:", _db.DB_PATH)
    print("schema_version:", _db._schema_version())
    print("memories:", len(_db.memory_rows()))
    print("events:", len(_db.event_rows()))

    hits = lexical.search("猫", [SCOPE], limit=3)
    print("lexical hits:", [h["fact"] for h in hits])
    if not hits:
        print("FAIL lexical")
        return 1

    rhits = reasoning.retrieve("用户养了什么猫", [SCOPE], top_k=3, min_score=0.0)
    print("retrieve hits:", [f for f, _s, _sc in rhits])
    if not rhits:
        print("FAIL retrieve")
        return 1

    print("vec enabled:", vecindex.enabled())
    if vecindex.enabled():
        from memory import embedder
        vecs = embedder.embed(["猫"])
        if vecs:
            vh = vecindex.search(vecs[0], None, top_k=3)
            print("vec hits:", [h["fact"] for h in vh])
            if not vh:
                print("FAIL vec")
                return 1

    print("PG functional verification OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
