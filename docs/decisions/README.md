# Decision records

One file per decision, numbered in order. A changed decision gets a new record that supersedes the old one.

| # | Decision | Status |
|---|---|---|
| [0001](0001-project-name.md) | Project name: AI Meterix, CLI `aimeterix` | accepted |
| [0002](0002-license.md) | Apache-2.0 with NOTICE crediting Maher Abuthraa, Apipas s.r.o. | accepted |
| [0003](0003-language.md) | Python 3.10+ | accepted |
| [0004](0004-developer-tooling.md) | uv, ruff, mypy, pytest, pre-commit, Makefile, GitHub Actions | accepted |
| [0005](0005-storage-default-and-connectors.md) | Pluggable connectors up to big data (default database superseded by 0008) | accepted |
| [0006](0006-configuration.md) | One YAML file + env overrides; secrets only from env | accepted; partly superseded by 0013 |
| [0007](0007-decision-records.md) | Record decisions as numbered files here | accepted |
| [0008](0008-default-storage-sqlite-wal.md) | Default storage: SQLite in WAL mode; DuckDB optional; Kafka as optional transport | accepted |
| [0009](0009-hardware-support-strategy.md) | Platform probe + supported/unsupported report; Apple Silicon first, NVIDIA next | accepted |
| [0010](0010-core-data-model-and-storage.md) | Core model: hosts, devices, memory pools (unified-aware); dataclasses, psycopg 3, no ORM, UTC, 30-day raw retention | accepted; amended by 0014 |
| [0011](0011-collectors-mvp1.md) | MVP-1 collectors: 10 s default sampling, IOReport power, per-app and per-model energy, proxy with Caller (path + headers) | accepted; amended by 0014 |
| [0012](0012-cost-engine-operational.md) | Cost engine: operational cost only (energy + fixed fees + API tokens); token counts per model (input, output, cache read, cache write); editable price table | accepted |
| [0013](0013-config-in-database-and-api-access.md) | Configuration in the database after init; localhost API with `user` and `admin` roles | accepted |
| [0014](0014-model-storage-collector-amendments.md) | Model and storage changes: CostProfile, ModelPrice, Project, no ownership, rollup sums, StorageReader/StorageWriter, `feature` tag | accepted |
| [0015](0015-advisor-and-forecast.md) | Advisor and forecast: always on demand from the user's filters, separate read-only modules, rules R1–R6, budgets, in MVP-1 | accepted; budget delivery moved to MVP-2 by 0016 |
| [0016](0016-interfaces-mvp1.md) | Interfaces in MVP-1: init from config with confirmation, `start` / `stop` / `health`, read-only dashboard on the API, advice on request; reports, alerts and services in MVP-2 | accepted |
| [0017](0017-footprint-and-safety.md) | Footprint and safety: no sudo, observe only, nothing installed system-wide, localhost only, performance budget, verified `stop`, `uninstall --purge` | accepted |
| [0018](0018-providers-and-models.md) | Providers as records (address, kind, models and prices); ten built in; any provider can be added; no price means cost 0 | accepted |

## Template

```markdown
# NNNN: Title

- Status: proposed | accepted | superseded by NNNN
- Date: YYYY-MM-DD
- Plan ref: D0.2

## Context
What we needed to decide and why it matters.

## Decision
What we chose, in one or two sentences.

## Consequences
What this makes easier, what it makes harder, and what would make us revisit it.
```
