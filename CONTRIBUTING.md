# Contributing to AI Meterix

Thanks for helping. The project is in early development: feedback on [docs/PLAN.md](docs/PLAN.md) is still the most useful contribution.

## How decisions are made
Open questions are listed per phase in the plan. Once settled, each gets a short record in `docs/decisions/` using the template there. Code follows decisions, not the other way round.

## Working on the code
You need Python 3.10+ and [uv](https://docs.astral.sh/uv/).

- `make setup` creates the environment and installs the pre-commit hooks.
- `make check` runs lint, types and tests exactly like CI; it must pass before a change is ready.
- Every change needs tests. Storage connectors must pass the shared conformance suite.
- Keep PRs small and focused; update docs when behavior changes.
- Never commit secrets, real usage data, prompts or API keys.
