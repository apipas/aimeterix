# 0005: Default database and storage connectors

- Status: accepted; default database superseded by 0008 (connector approach still applies)
- Date: 2026-10-09
- Plan ref: D0.5, D1.4

## Context
Teams already have a database they trust, from a laptop to a data warehouse. The tool must work out of the box and also scale to large data.

## Decision
- **DuckDB is the default** storage: one local file, no server, fast analytics over months of samples.
- All storage goes through the `StorageBackend` interface, chosen by URL (`duckdb:///...`, `postgresql://...`). Any database can be added as a connector plugin without changing the core.
- Connector roadmap:
  1. **v1:** DuckDB (default), PostgreSQL, SQLite (lightweight fallback and prototype import).
  2. **After v1, big data:** ClickHouse, Parquet/Iceberg files on object storage (S3, GCS, Azure), BigQuery, Snowflake.
- A connector is "supported" only when it passes the shared conformance test suite.

## Consequences
- DuckDB allows **one writing process at a time**. The agent and the LLM proxy must not open the file separately. Design consequence: a single local AI Meterix service owns the database, and the agent and proxy send it events (or write to a small buffer it ingests). This is now a Phase 1 requirement.
- DuckDB is an extra dependency (not in Python's standard library); it is small and ships wheels for Linux, macOS and Windows.
- Teams with several machines writing at once should use PostgreSQL or a big-data connector.
