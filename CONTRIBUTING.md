# Contributing to AI Meterix

Thanks for helping. The project is in early development: bug reports and feedback in [issues](https://github.com/apipas/aimeterix/issues) are the most useful contribution.

## How decisions are made
Open questions are listed per phase in the plan. Once settled, each gets a short record in `docs/decisions/` using the template there. Code follows decisions, not the other way round.

## Working on the code
You need Python 3.10+ and [uv](https://docs.astral.sh/uv/).

- `make setup` creates the environment and installs the pre-commit hooks.
- `make check` runs lint, types and tests exactly like CI; it must pass before a change is ready.
- Every change needs tests. Storage connectors must pass the shared conformance suite in `tests/conformance`.
- The suite runs on SQLite and DuckDB everywhere. To include PostgreSQL, point it at a server where you may create databases, e.g. `AIMETERIX_TEST_POSTGRES_URL=postgresql://postgres@localhost:5432/postgres make test` (`docker run -e POSTGRES_HOST_AUTH_METHOD=trust -p 5432:5432 postgres:16` works).
- Keep PRs small and focused; update docs when behavior changes.
- Never commit secrets, real usage data, prompts or API keys.
