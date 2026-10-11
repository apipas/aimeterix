# 0008: Default storage is SQLite in WAL mode

- Status: accepted; supersedes the default-database part of 0005
- Date: 2026-10-10
- Plan ref: D0.5

## Context
Record 0005 picked DuckDB as the default. DuckDB lets only **one process** open a file for writing, so the hardware agent and the LLM proxy could not write at the same time without an extra service in between. The owner asked about Apache Kafka and about SQLite in WAL mode.

## Options considered
| Option | Fit as the *default* | Notes |
|---|---|---|
| DuckDB | Weak | Fast analytics, but one writing process per file; needs a single-writer service |
| **SQLite (WAL mode)** | **Strong** | In Python's standard library, zero setup. WAL lets readers run while one writer writes, and several processes can take turns writing (short lock + busy timeout). Our write load is a few rows per second, far below its limits |
| Apache Kafka | Not a default | An event stream, not a database: it does not answer "cost per project last month". Needs a broker running (heavy for a laptop). Excellent as an optional **transport** at scale |
| PostgreSQL | Team option | Needs a server; the right choice when many machines write |

## Decision
- **SQLite in WAL mode is the default** (`sqlite:///~/.aimeterix/meter.db`), with `busy_timeout` and batched writes.
- **DuckDB stays as an optional analytics connector.** It can also read the SQLite file directly for heavy reports.
- **Kafka (and similar: Redpanda, NATS) becomes an optional ingest transport** after v1: agents publish events to a topic, and a sink writes them into the chosen database (ClickHouse, Postgres, Iceberg, ...).
- v1 connectors: SQLite (default), PostgreSQL, DuckDB. Big data after v1, unchanged from 0005.

## Consequences
- No extra service or dependency for a first install; the agent and proxy write directly.
- The single-writer service step in Phase 1 is no longer required (kept optional for remote agents).
- Very large or multi-machine setups should move to Postgres, ClickHouse or the Kafka transport; the storage interface makes that a config change.
