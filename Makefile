.PHONY: test check e2e

# 运行全部 pytest 回归（自动只收集 tests/）
test:
	./venv/bin/python -m pytest -q

# 提交前检查：配置校验 + diff 空白检查 + 回归测试
check:
	./venv/bin/python tools.py config-validate
	git diff --check
	./venv/bin/python -m pytest -q

# 端到端脚本（独立运行，不走 pytest）
e2e:
	./venv/bin/python e2e_test.py
	./venv/bin/python load_test.py
	./venv/bin/python v29_test.py
