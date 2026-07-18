.PHONY: check lint test smoke

check: lint test smoke

lint:
	uv run --package panthera-armd --extra dev ruff check armd/src armd/tests
	uv run --package panthera-armd --extra dev ruff format --check armd/src armd/tests

test:
	uv run --package panthera-armd --extra dev pytest -q armd/tests

smoke:
	uv run --package panthera-armd armd --sim --check
