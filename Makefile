.PHONY: check lint test smoke

check: lint test smoke

lint:
	uv run --all-packages --all-extras ruff check armd/src armd/tests cli/src cli/tests
	uv run --all-packages --all-extras ruff format --check armd/src armd/tests cli/src cli/tests

test:
	uv run --all-packages --all-extras pytest -q armd/tests cli/tests

smoke:
	uv run --package panthera-armd armd --sim --check
