# AI Meterix

**Know what your AI compute costs, what you waste, and what next month will cost.**

AI Meterix is an open-source tool for teams running AI on their own GPUs, rented cloud GPUs and LLM APIs. It meters usage, prices it with *your* rates, tells you in plain language what to change, and forecasts next month with a range. Store the data in whatever you already use: SQLite, DuckDB, PostgreSQL, or your own connector.

> **Status: early development.** Phase 1 is in progress: the core data model, configuration loader, storage interface and the SQLite (default), PostgreSQL and DuckDB connectors exist, plus an import for the prototype's data; collectors and interfaces do not yet. Read the plan in [docs/PLAN.md](docs/PLAN.md).

## What it will answer

| You ask | AI Meterix tells you |
|---|---|
| What did we spend? | One total across cloud GPUs, owned GPUs (with power) and LLM tokens |
| What did we waste? | Idle GPU time priced in dollars, and when it happens |
| What should we change? | Actions with an estimated saving |
| What will next month cost? | A forecast with a likely range and a budget warning |

## Principles

- **Advice first.** Every report leads with actions, not charts.
- **Bring your own database.** Storage is a pluggable connector.
- **Local first and private.** No account, no telemetry, prompts never stored.

## Repository map

| Path | What lives there |
|---|---|
| `docs/PLAN.md` | The phased plan and open decisions |
| `docs/decisions/` | One short record per settled decision |
| `src/ai_meterix/` | The package; `core/` holds the data model, config and plugin registry |
| `tests/` | Unit, connector conformance and integration tests |
| `examples/` | Sample config (`aimeterix.example.yaml`); showcase data later |
| `deploy/` | Docker and docker-compose |

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). License: [Apache-2.0](LICENSE). See [NOTICE](NOTICE) for attribution.

Decisions so far: [docs/decisions](docs/decisions/README.md).
