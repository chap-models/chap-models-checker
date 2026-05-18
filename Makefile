.PHONY: help install lint check test clean run reclassify list render-status

# ==============================================================================
# Venv
# ==============================================================================

UV := $(shell command -v uv 2> /dev/null)

# ==============================================================================
# Targets
# ==============================================================================

help:
	@echo "Usage: make [target]"
	@echo ""
	@echo "Targets:"
	@echo "  install       Install dependencies (incl. dev tools)"
	@echo "  lint          Auto-format and auto-fix lint issues with ruff"
	@echo "  check         Non-modifying checks: ruff + mypy + pyright (CI-like)"
	@echo "  test          Run tests"
	@echo "  run           Run the full chap-models sweep"
	@echo "  reclassify    Re-run failure classification on existing logs"
	@echo "  render-status Refresh the Snapshot block in README.md / STATUS.md"
	@echo "  list          Show repo status from the committed snapshot"
	@echo "  clean         Remove caches and work artifacts"

install:
	@echo ">>> Installing dependencies"
	@$(UV) sync

lint:
	@echo ">>> Formatting"
	@$(UV) run ruff format .
	@echo ">>> Auto-fixing lint"
	@$(UV) run ruff check . --fix

check:
	@echo ">>> ruff check"
	@$(UV) run ruff check .
	@echo ">>> ruff format --check"
	@$(UV) run ruff format --check .
	@echo ">>> mypy"
	@$(UV) run mypy
	@echo ">>> pyright"
	@$(UV) run pyright

test:
	@echo ">>> pytest"
	@$(UV) run pytest -q

run:
	@echo ">>> chap-models-checker run"
	@$(UV) run chap-models-checker run || true
	@$(MAKE) --no-print-directory render-status

reclassify:
	@echo ">>> chap-models-checker reclassify"
	@$(UV) run chap-models-checker reclassify
	@$(MAKE) --no-print-directory render-status

render-status:
	@echo ">>> chap-models-checker render-status"
	@$(UV) run chap-models-checker render-status

list:
	@echo ">>> chap-models-checker list"
	@$(UV) run chap-models-checker list

clean:
	@echo ">>> Cleaning caches"
	@find . -type f -name "*.pyc" -delete
	@find . -type d -name "__pycache__" -exec rm -rf {} + 2>/dev/null || true
	@find . -type d -name ".pytest_cache" -exec rm -rf {} + 2>/dev/null || true
	@find . -type d -name ".ruff_cache" -exec rm -rf {} + 2>/dev/null || true
	@find . -type d -name ".mypy_cache" -exec rm -rf {} + 2>/dev/null || true
	@find . -type d -name ".pyright" -exec rm -rf {} + 2>/dev/null || true
	@rm -rf .coverage htmlcov coverage.xml
	@rm -rf dist build *.egg-info

.DEFAULT_GOAL := help
