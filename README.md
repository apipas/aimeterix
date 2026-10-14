# AI Meterix

**Know what your AI compute costs, what you waste, and what next month will cost.**

AI Meterix is an open-source tool for teams running AI on their own GPUs, rented cloud GPUs and LLM APIs. It meters usage, prices it with *your* rates, tells you in plain language what to change, and forecasts next month with a range. Store the data in whatever you already use: SQLite, DuckDB, PostgreSQL, or your own connector.

> **Status: early development.** Phases 1 and 2 are in progress: the data model, storage connectors (SQLite default, PostgreSQL, DuckDB), `aimeterix doctor`, the Apple Silicon agent, the LLM proxy and the cost engine (`usage`, `profile`, `prices`) the localhost API (`serve`) and live settings (`config`) exist. Reports and the dashboard come next. Read the plan in [docs/PLAN.md](docs/PLAN.md).

## Try it

```bash
aimeterix doctor                 # what this machine can measure
aimeterix agent                  # sample the hardware until Ctrl-C (--mock for a demo)
aimeterix proxy --upstream http://127.0.0.1:8080   # meter a local llama.cpp server
aimeterix profile set --rate 0.33 --fees 0   # your energy price and fees, in USD
aimeterix usage                  # tokens, Wh and cost per model and day
aimeterix prices                 # price per 1M tokens for each model
aimeterix prices apply-reference # copy the dated OpenAI/Anthropic list (see `prices reference`)
aimeterix serve                  # JSON API on http://127.0.0.1:8788/api with login
aimeterix config                 # live settings; `config set agent.interval_s 5`
```

The first start of the agent, proxy or API copies the live settings (sampling interval, retention, proxy routes and key allowlist) from `aimeterix.yaml` into the database. From then on the database wins: change them with `aimeterix config set KEY VALUE` or `PUT /api/config/<key>`, and the agent and proxy pick the change up within a minute, without a restart. `aimeterix config history` shows who changed what; `aimeterix config export FILE` writes the settings back to YAML. The database URL, addresses and ports, and secrets stay in the file or environment.

`aimeterix serve` creates two accounts, `user` (read) and `admin` (read and write), whose first password is their name and must be changed at first login (`POST /api/password`, or `aimeterix password admin`). Log in with `POST /api/login`, then send `Authorization: Bearer <token>`. Endpoints are listed in `src/ai_meterix/interfaces/api.py`.

Point your app at the proxy instead of the provider: `http://127.0.0.1:8787/openai/v1`, `http://127.0.0.1:8787/anthropic` or, with `--upstream`, `http://127.0.0.1:8787/v1`. Add `X-AIMeterix-App` and `X-AIMeterix-Feature` headers (or `/app/<name>/` in the path) to see who made each call. Only token counts, timings and tags are stored, never prompts, responses or keys.

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
