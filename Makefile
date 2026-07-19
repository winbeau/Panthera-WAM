.PHONY: check lint test audit smoke

check: lint test audit smoke

lint:
	uv run --all-packages --all-extras ruff check armd/src armd/tests cli/src cli/tests tools
	uv run --all-packages --all-extras ruff format --check armd/src armd/tests cli/src cli/tests tools

test:
	uv run --all-packages --all-extras pytest -q armd/tests cli/tests

audit:
	uv run --all-packages --all-extras python tools/audit_sdk_contract.py

smoke:
	uv run --package panthera-armd armd --sim --check
