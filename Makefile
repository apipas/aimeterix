.PHONY: setup lint format typecheck test check

setup:  ## Create the environment and install pre-commit hooks
	uv sync --all-extras
	uv run pre-commit install

lint:  ## Lint and check formatting
	uv run ruff check .
	uv run ruff format --check .

format:  ## Fix lint and formatting where possible
	uv run ruff check --fix .
	uv run ruff format .

typecheck:  ## Type check (strict on core)
	uv run mypy

test:  ## Run the test suite
	uv run pytest

check: lint typecheck test  ## Everything CI runs
