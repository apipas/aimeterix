# 0013: Configuration in the database, and API access roles

- Status: accepted; supersedes the "one YAML file is the configuration" part of 0006
- Date: 2026-10-11
- Plan ref: C8, C9 (Phase 3 review), D5.3

## Context
MVP-1 gets a small localhost API next to the CLI so the data can be read by other tools and, later, a dashboard. The owner wants authentication with a read-only and an admin role, and wants the admin to change configuration through the API after setup.

## Decision
1. **Localhost API in MVP-1.** Read endpoints for usage, tokens per model, costs, profiles and prices; write endpoints for profiles, prices and configuration. Bound to `127.0.0.1` by default.
2. **Two roles.** `user` (read only, uses `StorageReader`) and `admin` (read and write, uses `StorageWriter`), see record 0014.
3. **Accounts.** `aimeterix init` creates `user/user` and `admin/admin`. Each password **must be changed at first login**, and the API refuses to bind to anything but localhost until both are changed. Passwords are stored as salted `scrypt` hashes (standard library). Login returns a token for later calls. No sign-up or user list in MVP-1; the admin can change both passwords.
4. **Configuration lives in the database after `init`.** `aimeterix init` reads `aimeterix.yaml` once to create the database; from then on the database is the source of truth, edited by the admin via the API or `aimeterix config set`. Only bootstrap settings stay in YAML or environment: database URL, API address and port, and secrets (environment only, as in 0006).
5. **Live reload and audit.** The agent and proxy check a settings version number about every 60 s and apply changes without a restart. Every change is logged with who and when. `aimeterix config export` writes the current settings back to YAML.

## Consequences
- One live source of truth; no drift between a file and the database.
- Default credentials are a known risk; forced change at first login and the localhost-only rule contain it.
- More users, password reset and single sign-on remain after v1.
