.PHONY: help install check test lint fmt types eval

help:
	@echo "make install  📦 sync dependencies (all extras)"
	@echo "make check    ✅ lint + format + types + tests, with a summary"
	@echo "make test     🧪 pytest (needs Docker)"
	@echo "make lint     🧹 ruff check"
	@echo "make fmt      🎨 ruff format"
	@echo "make types    🔎 pyright (strict)"

install:
	uv sync --all-extras

check:
	@./scripts/check.sh

test:
	uv run pytest -q

lint:
	uv run ruff check .

fmt:
	uv run ruff format .

types:
	uv run pyright
