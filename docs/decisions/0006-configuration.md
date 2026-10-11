# 0006: Configuration

- Status: accepted; "one YAML file is the configuration" superseded by 0013 (YAML is the starting point, the database holds live settings)
- Date: 2026-10-09
- Plan ref: D0.6

## Decision
- One file, `aimeterix.yaml`, safe to commit (no secrets).
- Any setting can be overridden by an environment variable, e.g. `AIMETERIX_STORAGE_URL`.
- Secrets (database passwords, webhook tokens) come only from environment variables or a `.env` file that is never committed.
- `aimeterix init` writes a commented starter file.

## Consequences
- One place to look; works the same on a laptop, in Docker and in CI.
