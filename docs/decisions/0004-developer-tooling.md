# 0004: Developer tooling

- Status: accepted
- Date: 2026-10-09
- Plan ref: D0.4

## Context
Contributors need one obvious way to set up, check and test the project, and CI must run exactly the same checks.

## Decision
| Job | Tool | Why |
|---|---|---|
| Environments and dependency lock | **uv** | One fast tool for virtualenvs, installs and a lockfile |
| Lint and formatting | **ruff** | Replaces flake8, isort and black with one fast tool |
| Type checking | **mypy** (strict on `core`) | Catches contract mistakes in the data model and connector interface |
| Tests | **pytest** | The Python standard |
| Pre-commit hooks | **pre-commit** | Runs ruff and basic checks before each commit |
| Entry points | **Makefile**: `make setup`, `make test`, `make lint`, `make check` | One command per job, same locally and in CI |
| CI | **GitHub Actions** | Free for open source, where the repo will live |

## Consequences
- New contributors need only Python, uv and (for Postgres/integration tests) Docker.
- `make check` is the single definition of "ready to merge".
