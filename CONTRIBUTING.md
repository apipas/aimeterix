# Contributing to AI Meterix

Thanks for helping. The project is in the planning stage: the best contribution right now is feedback on [docs/PLAN.md](docs/PLAN.md).

## How decisions are made
Open questions are listed per phase in the plan. Once settled, each gets a short record in `docs/decisions/` using the template there. Code follows decisions, not the other way round.

## Once code exists
- `make setup` creates the environment; `make check` runs lint, types and tests exactly like CI.
- Every change needs tests. Storage connectors must pass the shared conformance suite.
- Keep PRs small and focused; update docs when behavior changes.
- Never commit secrets, real usage data, prompts or API keys.
