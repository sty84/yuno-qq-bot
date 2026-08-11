# -*- coding: utf-8 -*-
"""双 Persona Pack 验收：yuno + office 各自跑 pack_core_suite（夹具从 pack 读）。"""

import os
import subprocess
import sys


def test_dual_pack():
    here = os.path.dirname(os.path.abspath(__file__))
    suite = os.path.join(here, "pack_core_suite.py")
    for pack in ("yuno", "office"):
        r = subprocess.run(
            [sys.executable, suite, "--pack", pack],
            capture_output=True, text=True, timeout=180,
        )
        assert r.returncode == 0, f"{pack} FAILED:\n{r.stdout}\n{r.stderr}"
        assert "ALL PASS" in r.stdout, f"{pack}: {r.stdout}"
        print(f"{pack}: ALL PASS")


if __name__ == "__main__":
    test_dual_pack()
    print("dual pack OK")
