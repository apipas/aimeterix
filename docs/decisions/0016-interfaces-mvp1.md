# 0016: Interfaces in MVP-1: init from config, dashboard on the API, advice on request

- Status: accepted; refines 0006 (init) and 0015 (budget delivery)
- Date: 2026-10-11
- Plan ref: D5.0 to D5.14 (D5.13 and D5.14 added the same day, round 3)

## Context
Phase 5 planned a full set of interfaces: setup wizard, HTML report sent weekly, local dashboard, alerts by Slack and email, exports and a Grafana dashboard. The owner wants MVP-1 focused on compute data (collection, structure and accuracy) rather than a complete product. Delivery and convenience features can be improved once the data is right.

## Decision
1. **Scope (D5.0).** MVP-1 Phase 5 is: init from the config file, `start` / `stop` / `health` commands, a dashboard built on the API, and advice on request. Everything else moves to MVP-2.
2. **Init (D5.5).** No wizard. `aimeterix init` without `aimeterix.yaml` writes a commented starter file and stops. With the file, it validates it, shows a summary of what it will create, and asks for confirmation (`--yes` for scripts). It then creates and seeds the database, creates the two accounts with forced password change (0013), runs `doctor`, and prints `aimeterix start` and how to point clients at the proxy. Running it on an existing database is refused; changes go through `config set`.
3. **Dashboard (D5.1, D5.10, D5.11).** `aimeterix serve` runs the localhost API and a read-only dashboard in one process. Server-rendered HTML with an inline charts script; no charts library, no build step, no external requests. It reads one endpoint, `GET /api/dashboard`, using the same computation as the CLI. It shows data coverage and gaps first, then usage per model, daily cost, hardware use and data issues, filtered by date range, model and feature. Browser login uses the 0013 accounts with an HttpOnly, SameSite=Strict session cookie.
4. **Advice on request (D5.12).** A "Get advice" action on the dashboard calls the Phase 4 advice and forecast endpoints for the current filters and shows recommendations, forecast and budget status. Computed on demand, nothing stored (0015). Dismissals stay in the CLI and API.
5. **Start, stop and health (D5.13, D5.14).** `aimeterix start` and `aimeterix stop` run or stop `agent`, `proxy` and `serve` in the background (all, or one by name), with PID files and logs in `~/.aimeterix/`; a second copy is refused, and stop lets buffers flush. They survive closing the terminal but not a reboot. `aimeterix health` checks the running setup: each process up or down, age of the last hardware sample and last proxied call, database reachable, size and write delay; exit code 0 when healthy, 1 when not; same data at `GET /api/health`. `doctor` remains the static check of what the machine can measure.
6. **Moved to MVP-2 (D5.2, D5.4, D5.6 to D5.9).** macOS services and scheduled jobs; HTML report file and weekly email; alerts (budget, collector stopped, digest) and channels (macOS notification, Slack, email); secrets file for background jobs; interactive wizard; dashboard admin forms; exports beyond the Phase 3 usage CSV, including Parquet; Grafana through the API. Budget status is shown in the CLI, API and dashboard; its delivery (0015 item 9) moves to MVP-2.

## Consequences
- Much less to build before the first usable version; effort goes into collectors and accuracy.
- The admin starts metering with `aimeterix start`; time when it is not running (stopped, or after a reboot) is not measured. The dashboard's coverage panel makes such gaps visible. Services are the first MVP-2 item.
- One dashboard endpoint shared with the CLI computation keeps all interfaces consistent and testable.
