# AI Meterix: planning guide

> **Status:** draft for review. Nothing here is built yet. We go through it one phase at a time, settle the open decisions, and only then start building that phase.
>
> **Prototype:** an earlier prototype proved the idea (hardware meter, LLM proxy, mocked showcase report). Its code gets ported into this repo where the plan says so; it is not the final design.

---

## 1. What we are building

**One sentence:** AI Meterix tells a team what their AI compute costs, what they waste, what to change, and what next month will cost, across their own GPUs, rented cloud GPUs and LLM APIs.

**Who uses it first:** small to mid-size teams (2 to 50 engineers) running AI locally, in the cloud, or both, who have no FinOps team and get surprised by the bill.

**What makes it different:**
1. **One view of hardware and LLM spend.** Most tools do one or the other.
2. **Advice, not charts.** Every report leads with actions and an estimated saving.
3. **Forecast with a range,** plus a budget warning.
4. **Bring your own database.** Storage is a connector: SQLite on a laptop (default, WAL mode), DuckDB for local analytics, Postgres for a team, big-data stores (ClickHouse, Parquet/Iceberg, BigQuery, Snowflake) at scale.
5. **Local first and private.** No account, no telemetry, prompts are never stored.

**Out of scope for v1:** a hosted SaaS, user accounts beyond two fixed roles (`user`, `admin`), billing or chargeback invoices, accelerators beyond Apple Silicon and NVIDIA (AMD, Intel come later), Windows, Kubernetes operators.

**v1 is "ready to go" when:** a team can `pip install ai-meterix` or `docker compose up`, point it at their machines and LLM traffic, pick their database, and see their usage, actions and a forecast in the dashboard. CI is green, docs exist, and a showcase dataset demos it in one command.

---

## 2. Architecture at a glance

```
 SOURCES (collectors)            CORE                         STORAGE (connectors)
 ───────────────────            ────                         ────────────────────
 GPU / CPU agent  ─┐                                      ┌─▶ SQLite WAL  (default, zero install)
 LLM proxy        ─┼─▶ canonical events ─▶ StorageBackend ┼─▶ DuckDB      (local analytics)
 SDK wrapper      ─┤   (validated, typed)   interface     ├─▶ PostgreSQL / TimescaleDB (teams)
 OTLP receiver    ─┤                                      ├─▶ big data    (ClickHouse, Iceberg, BigQuery... later)
 Bill importers   ─┘                                      └─▶ your own    (plugin)
                                     │
                          ENGINES    ▼                         INTERFACES
                          profile + prices ─▶ costs ─▶ advisor ─────▶  CLI · local dashboard · HTML report (MVP-2)
                                          └─▶ forecaster ──▶  REST API · alerts (MVP-2)
```

**The rule that keeps it simple:** collectors and interfaces never talk to a database directly. Everything goes through the `StorageBackend` interface, so any connector works with every feature.

### 2.1 Canonical data model (the contract)

| Entity | What it holds | Key fields |
|---|---|---|
| `Project` | a group of machines, e.g. a small data center | id, name |
| `Host` | one machine we meter; one agent and one cost profile | id, project_id, hostname, os, arch, chip, tags |
| `Device` | a compute unit on a host | id, host_id, kind (cpu/gpu/npu), model, memory_pool_id |
| `MemoryPool` | memory devices draw from | id, host_id, type (unified/system/vram), total_bytes |
| `DeviceSample` | one reading of a device | ts, device_id, interval_s, util_pct, power_w, source, extra |
| `MemorySample` | one reading of a memory pool | ts, pool_id, interval_s, used_bytes, wired / compressed / swap / gpu_allocated bytes, pressure, source, extra |
| `ProcessSample` | one app's share of a host in an interval | ts, host_id, process name, pid, cpu_time_s, gpu_time_s, attributed_energy_j, local_model_id, source |
| `LocalModel` | a local LLM model loaded by a runtime | id, host_id, runtime (ollama/llama.cpp/...), model name, size_bytes, pid, first_seen, last_seen |
| `Caller` | who made an LLM call | id, app name, feature, tags (MVP-1: from URL path and headers) |
| `LlmCall` | one model API call | ts, provider, request id, model, call type (normal / batch / file), input, output, reasoning, cache read, cache write tokens, raw usage, latency, status, caller_id |
| `CostProfile` | a host's operational cost parameters, versioned on save | host_id, name, region, currency, energy rate per kWh, PUE, fixed fees per month, fee spread |
| `ModelPrice` | price per 1M tokens per model, editable history | model, source (local/api), input, output, cache read, cache write, valid_from, valid_to, price_set |
| `PriceChange` / `SettingChange` | audit log of edits | who, when, what, old → new |
| `Setting` | live configuration after `init` | key, value, version |
| `User` | API account | name, role (`user` / `admin`), password hash, must_change |
| `CostRecord` | derived cost per period (cache, recomputable) | period, host, model, feature, energy cost, fixed cost, token cost, idle overhead |
| `Recommendation` | one piece of advice; computed on demand, not stored | rule id, title, why, action, est_saving, confidence, evidence |
| `Forecast` | projection; computed on demand, not stored | horizon, low / expected / high, method |

On an Apple Silicon Mac, CPU, GPU and Neural Engine share one `unified` pool, so memory is never counted per device; an NVIDIA machine has a `system` pool plus one `vram` pool per GPU. Missing readings are null, never zero; `source` says measured, estimated or mock. Timestamps are UTC; memory is in bytes. See record 0010.

Raw events (samples, calls) are stored. Costs, recommendations and forecasts are **derived** and can always be recomputed when prices change; recommendations and forecasts are never stored (record 0015). Budgets and dismissed advice are settings. Structure: project → host (one agent, one cost profile) → devices (record 0014).

### 2.2 Storage connector interface (draft)

```python
class StorageReader(Protocol):                 # read only: reports, API `user` role
    name: str                                   # "sqlite", "postgres", ...
    def connect(self, url: str, **options) -> None: ...   # SQLite mode=ro, read-only DB user
    def health(self) -> HealthStatus: ...
    def query(self, q: UsageQuery) -> Table: ...  # time range, group by, filters, bucket size
    def get_profile(self, host_id: str) -> CostProfile: ...
    def get_prices(self) -> list[ModelPrice]: ...
    def get_settings(self) -> Settings: ...
    def close(self) -> None: ...

class StorageWriter(StorageReader, Protocol):  # read + write: agent, proxy, API `admin` role
    def migrate(self) -> None: ...              # create or upgrade schema
    def write_device_samples(self, samples: Iterable[DeviceSample]) -> int: ...
    def write_memory_samples(self, samples: Iterable[MemorySample]) -> int: ...
    def write_calls(self, calls: Iterable[LlmCall]) -> int: ...
    def upsert_hosts(self, hosts: Iterable[Host]) -> None: ...  # with their devices and memory pools
    def save_profile(self, profile: CostProfile, by: str) -> None: ...   # new version + log
    def save_price(self, price: ModelPrice, by: str) -> None: ...        # + PriceChange log
    def save_setting(self, key: str, value: Any, by: str) -> None: ...   # + SettingChange log
    def retention(self, policy: RetentionPolicy) -> int: ...
```

- **Selected by URL** in config: `sqlite:///~/.aimeterix/meter.db` (default), `duckdb:///meter.duckdb`, `postgresql://user@host/db`, `clickhouse://...`.
- **Plugins:** third parties register a connector through the Python entry-point group `ai_meterix.storage`. No fork needed.
- **Conformance suite:** one shared test suite (`tests/conformance`) that every connector must pass. That is the definition of "supported".
- **Queries stay small:** a typed `UsageQuery` (range, group-by, bucket, filters), not raw SQL, so engines are database-independent.

### 2.3 Repository layout (created, empty for now)

```
ai-meterix/
├── README.md            what it is, quick start, status
├── CONTRIBUTING.md      how humans work in this repo
├── docs/
│   ├── PLAN.md          this guide
│   └── decisions/       one short record per settled decision (ADR)
├── src/ai_meterix/
│   ├── core/            data model, config, plugin registry, errors
│   ├── collectors/      hardware agent, LLM proxy, SDK wrapper, OTLP, importers
│   ├── storage/         StorageBackend interface + sqlite, postgres, duckdb connectors
│   ├── pricing/         price book loading, cost calculation, allocation
│   ├── advisor/         rules that produce recommendations
│   ├── forecast/        forecasting methods + backtesting
│   └── interfaces/      CLI, report renderer, REST API, dashboard, alerts
├── tests/
│   ├── unit/            fast, no network, no database server
│   ├── conformance/     shared suite every storage connector must pass
│   └── integration/     end-to-end with real Postgres etc. (Docker)
├── examples/            showcase dataset and report, sample configs
└── deploy/              Dockerfile, docker-compose (app + Postgres)
```

---

## 3. Phases

Each phase lists its goal, the steps, the decisions to settle **before** we build it (with my recommendation), and what "done" means. Sizes are relative: **S** small, **M** medium, **L** large.

### Phase 0: Foundations (decide, don't code)

**Goal:** settle the choices every later phase depends on.

| # | Decision | Options | My recommendation |
|---|---|---|---|
| # | Decision | Outcome | Record |
|---|---|---|---|
| D0.1 | Name and package | ✅ **AI Meterix**; PyPI `ai-meterix`, import `ai_meterix`, CLI `aimeterix` | [0001](decisions/0001-project-name.md) |
| D0.2 | License | ✅ **Apache-2.0 + NOTICE** crediting the original author; free for commercial use | [0002](decisions/0002-license.md) |
| D0.3 | Language | ✅ **Python 3.10+** | [0003](decisions/0003-language.md) |
| D0.4 | Tooling | ✅ uv, ruff, mypy, pytest, pre-commit, Makefile, GitHub Actions | [0004](decisions/0004-developer-tooling.md) |
| D0.5 | Default database | ✅ **SQLite (WAL mode)** default; any database via connectors, including big data; Kafka as optional transport | [0005](decisions/0005-storage-default-and-connectors.md), [0008](decisions/0008-default-storage-sqlite-wal.md) |
| D0.6 | Config format | ✅ one `aimeterix.yaml` + env overrides, secrets only from env | [0006](decisions/0006-configuration.md) |
| D0.7 | How we record decisions | ✅ numbered records in `docs/decisions/` | [0007](decisions/0007-decision-records.md) |

**Steps:** review this guide → settle D0.x → write records 0001 to 0007 (done; two still proposed) → set up CI skeleton. **Size:** S.
**Done when:** every D0 decision has an ADR and `make check` runs (even with no code).

---

### Phase 1: Core and storage connectors

**Goal:** the data contract and the pluggable storage layer, proven by tests, before any collector depends on it.

**Steps**
1. Data model in `core/`: hosts, devices, memory pools, samples (record 0010); stdlib dataclasses; units fixed: seconds, watts, bytes, tokens, and the profile's currency (record 0012). **S**
2. Config loader (`aimeterix.yaml` + env) and plugin registry via entry points. **S**
3. `StorageBackend` interface, `UsageQuery`, retention policy. **M**
4. Conformance test suite (write, query, group-by, time buckets, retention, concurrency, migration from empty and from previous version). **M**
5. Connectors: **SQLite in WAL mode** (default, port from prototype), **PostgreSQL** (Timescale optional), **DuckDB** (analytics). **L**
6. **Write path:** batched writes with busy timeout so the agent, proxy and reports share one SQLite file safely (see record 0008). **S**
7. Schema migrations per connector (versioned, forward-only). **S**
8. Import the prototype's SQLite file so early users keep their data. **S**

**Decisions** (all settled)
| # | Decision | Outcome | Record |
|---|---|---|---|
| D1.1 | Validation library | ✅ stdlib dataclasses in the core; **pydantic v2** only for config and API | [0010](decisions/0010-core-data-model-and-storage.md) |
| D1.2 | Postgres driver | ✅ **psycopg 3**, optional extra `[postgres]` | [0010](decisions/0010-core-data-model-and-storage.md) |
| D1.3 | Use an ORM? | ✅ **No.** A thin SQL module per connector | [0010](decisions/0010-core-data-model-and-storage.md) |
| D1.4 | Which connectors ship in v1 | ✅ **SQLite (default), Postgres, DuckDB**; big data after v1 | [0005](decisions/0005-storage-default-and-connectors.md), [0008](decisions/0008-default-storage-sqlite-wal.md) |
| D1.5 | Resolution and retention | ✅ raw 30 days; hourly rollups (min, mean, max, p95) 1 year; configurable | [0010](decisions/0010-core-data-model-and-storage.md) |
| D1.6 | Hardware data model | ✅ hosts, devices and memory pools (unified / system / vram); null never zero | [0010](decisions/0010-core-data-model-and-storage.md) |
| D1.7 | Timestamps | ✅ UTC; epoch ms in SQLite, `timestamptz` elsewhere | [0010](decisions/0010-core-data-model-and-storage.md) |

**Done when:** all three connectors pass the same conformance suite in CI (Postgres runs in a Docker service).

---

### Phase 2: Collectors

**Goal:** get real usage in, from hardware and from LLM traffic, with as little setup as possible.

**Steps**
1. **Platform probe and `aimeterix doctor`:** detect OS, architecture and accelerators; report each metric as supported, partial or unsupported, with the reason. Unsupported metrics are stored as missing, never zero (record 0009). **M**
1b. **Apple Silicon backend (tier 1):** CPU per P/E cluster, unified memory (used, wired, compressed, pressure), GPU utilization via IOKit, thermal state; power and Neural Engine via IOReport or `powermetrics` (D2.5). **L**
1c. **NVIDIA backend (tier 2):** NVML via `nvidia-ml-py` with `nvidia-smi` fallback; per-process memory, clocks, power, throttling. psutil + RAPL for Linux CPUs. **M**
1d. **Agent loop:** batches writes, keeps a local buffer if the database is down, mock backend for demos. **S**
1e. **Footprint guarantees (record 0017):** the agent measures its own CPU, memory and energy against the budget and backs off when over; tests prove no sudo, no writes outside `~/.aimeterix/` and localhost-only servers. **S**
2. **LLM proxy:** port from prototype; OpenAI-compatible and Anthropic, streaming, app and feature tags by header (`X-AIMeterix-Feature`, record 0014), cached-token fields, provider request id, never stores prompts. Add an optional key allowlist so it is not an open relay. **M**
3. **SDK wrapper:** `with aimeterix.track(project="x"):` and a decorator for teams who don't want a proxy. **S**
4. **OTLP receiver (stretch):** accept OpenTelemetry GenAI spans so teams already using OTel need no new client. **M**
5. **Host registry:** each machine belongs to a project and carries tags (team, environment); no owned/rented flag (record 0014). **S**

**Decisions** (all settled; record [0011](decisions/0011-collectors-mvp1.md))
| # | Decision | Status | Proposal |
|---|---|---|---|
| D2.1 | Sampling interval | ✅ agreed | Default 10 s, configurable 1 to 300 s, 1 s burst mode; counters make totals exact at any interval; the agent reports its own overhead |
| D2.2 | Proxy server | ✅ agreed | Port the standard-library prototype for MVP-1; httpx later if needed |
| D2.2b | Identifying callers | ✅ agreed | `Caller` entity; MVP-1 uses app name in the URL path and tag headers only |
| D2.3 | Several machines | ✅ agreed | MVP-2 |
| D2.4 | OpenTelemetry receiver | ✅ agreed | Later |
| D2.5 | Mac power readings | ✅ agreed | IOReport and direct IOKit/libproc calls (no sudo); `powermetrics` opt-in cross-check only |
| D2.6 | Test hardware | ✅ agreed | Owner's M4 (64 GB); `doctor` records the exact chip |
| D2.7 | NVIDIA | ✅ agreed | Not in MVP-1; small experimental module tested on recorded `nvidia-smi` output |
| D2.8 | SDK wrapper | ✅ agreed | MVP-2, with D2.3 |
| D2.9 | Per-app and per-model energy | ✅ agreed | MVP-1: per-app CPU/GPU time, attributed energy, model detection for Ollama and llama.cpp |
| D2.10 | Footprint and safety | ✅ agreed | No sudo, observe only, localhost only, budget < 1% CPU / 100 MB / 0.1 W, verified `stop`; record [0017](decisions/0017-footprint-and-safety.md) |

**Release stages:** MVP-1 is one Mac (probe, agent, per-app and per-model energy, proxy, SQLite, cost engine with token counts per model, localhost API with login, advisor with R1–R6, forecast and one budget, `init` from the config file, `start` / `stop` / `health`, read-only dashboard with advice on request). MVP-2 adds services, reports and alerts (record 0016), several machines, the SDK wrapper, NVIDIA on real hardware, and the host registry with tags.

**Done when:** on an Apple Silicon Mac, `aimeterix doctor` reports coverage correctly, and `aimeterix agent` and `aimeterix proxy` run for 24h without loss, and the LLM numbers match the provider's usage page within 1%.

---

### Phase 3: Cost engine

**Goal:** turn measured usage into operational cost and token counts, shown like a cloud LLM provider's usage page, so a client can check measured usage against the provider (record 0012).

**Steps**
1. **Cost profile per host** (energy rate, PUE, region label, currency, fixed fees per month, fee spread), seeded by `init`, stored in the database, versioned on save. **S**
2. **Model price table** (input, output, cache read, cache write per 1M tokens) with editable history, `price_set` flag and change log; dated OpenAI/Anthropic reference list applied by the admin. **M**
3. **Token normalization** across providers (cached tokens subtracted where included in input; "—" when a runtime reports none). **S**
4. **Cost calculation:** energy × PUE × rate + fixed fees over the month's real hours; split to active models by energy share; idle overhead line; API tokens × prices. From hourly rollup sums, exact decimals, local-time days, cache dropped on profile/price change. **M**
5. **Usage statement:** per model and day: requests, input, output, cache read, cache write, totals, kWh, cost. CLI (`aimeterix usage`), CSV, localhost API. **M**
6. **Localhost API with login** (`user` read, `admin` read+write; record 0013) and configuration editable in the database. **M** Live settings: interval, retention, proxy routes and key allowlist; reloaded about every 60 s; CLI flags pin a setting for that run.
7. Batch jobs and file uploads recorded (tokens "not metered yet"). **S**

**Decisions** (all settled; records [0012](decisions/0012-cost-engine-operational.md), [0013](decisions/0013-config-in-database-and-api-access.md), [0014](decisions/0014-model-storage-collector-amendments.md))
| # | Decision | Outcome |
|---|---|---|
| D3.1 | Scope | ✅ Operational only: energy + fixed fees + API tokens; no hardware cost |
| D3.2 | Cost profile | ✅ Per host; rate/kWh, PUE (1.0), region, currency, fixed fees, fee spread (`calendar` default, or `uptime`); no FX; USD only in MVP-1 (custom currency later); automatic version history, no user-entered dates |
| D3.3 | Where it lives | ✅ Database, per host; seeded from `init` |
| D3.4 | Fixed fees and idle | ✅ Spread over real month hours; active hours to models by energy share; idle overhead shown |
| D3.5 | Energy counted | ✅ Whole-device power if readable, else chip + overhead; × PUE |
| D3.6 | Output | ✅ Token counts per model (input, output, cache read, cache write) + costs; CLI, CSV, localhost API |
| D3.7 | Prices | ✅ Separate editable table with history and change log; local default 0; API default 0 + "price not set"; reference list applied by admin |
| D3.8 | Provider check | ✅ Same columns + request id stored; export import MVP-2 |
| D3.9 | Computation | ✅ On demand from hourly sums; exact decimals; local-time days |
| D3.10 | Batch and Files | ✅ Batch unsupported for tokens in MVP-1 (jobs recorded); file uploads counted with size |
| C1–C7 | Model, storage, collector changes | ✅ Record 0014 |
| C8–C9 | API roles; config in DB | ✅ Record 0013 |

**Done when:** for one recorded day on the owner's M4, per-model token counts match the provider's usage page within 1%, a hand-checked spreadsheet matches the costs to the cent, changing a price or profile recomputes past days, and the localhost API returns the same numbers as the CLI.

---

### Phase 4: Advisor and forecast

**Goal:** the part that makes this a helpful tool: actions with savings, and next month's cost with a range. Always computed on demand from the user's filters, in modules separate from tracking (record 0015).

**Steps**
1. **Module boundary:** `advisor/` and `forecast/` use only `core` types, `StorageReader` and the cost engine's public functions; an import contract in `make check` stops collectors, storage and core from importing them. **S**
2. **Rule framework:** each rule reads usage and costs for the requested range and filters and returns `Recommendation`s with saving, confidence and evidence; rules run isolated (one failure doesn't stop the others); thresholds are settings scaled to the range. **M**
3. **Starter rules:** R1 expensive model for short calls · R2 prompt caching not used · R3 cache written but not reused · R4 local vs API cost · R5 paid idle capacity (with fixed fees) · R6 data gaps. Savings in money, or in tokens when a price is not set. **M**
4. **Dismissals:** admin dismisses advice until a date or for good; stored as settings with change log; returns at 2× saving. **S**
5. **Forecaster:** fixed fees exact; energy run-rate with weekly pattern; API tokens per model and type with trend and weekly pattern, priced with current prices; range; rest of month + next month. **M**
6. **Backtesting:** rolling 7- and 30-day backtest on demand, with error and range coverage; regenerate the showcase dataset for the operational scope. **M**
7. **Budgets:** one monthly money budget per project + optional token budgets per model; "at risk" / "over". **S**
8. **CLI and API:** `aimeterix advise`, `aimeterix forecast [--backtest]`, matching API endpoints (`user` read, `admin` dismissals and budgets). **S**

**Decisions** (all settled; record [0015](decisions/0015-advisor-and-forecast.md))
| # | Decision | Outcome |
|---|---|---|
| D4.0 | In MVP-1? | ✅ Yes, then iterate |
| D4.1 | Forecast library | ✅ Own simple model; statsmodels only if backtests show a gain |
| D4.2 | Recommendation tone | ✅ Plain language, one action, monthly saving as a range, traceable evidence |
| D4.3 | LLM summaries | ✅ Off by default; backlog |
| D4.4 | Minimum history | ✅ < 7 days none; 7–20 run-rate "early"; ≥ 21 trend + weekly pattern |
| D4.5 | Starter rules | ✅ R1–R6; idle rented GPUs replaced by R5; over-provisioned GPUs to backlog |
| D4.6 | Dismissals | ✅ Admin, until a date or for good, logged; return at 2× saving |
| D4.7 | Forecast method | ✅ Fees exact, energy run-rate, tokens forecast then priced; this month + next |
| D4.8 | Backtesting | ✅ Rolling, on demand; showcase regenerated |
| D4.9 | Budgets | ✅ One money budget + optional token budgets per model; delivery in Phase 5 |
| D4.10 | Endpoints | ✅ CLI and API in Phase 4 |
| D4.11 | Computation and modules | ✅ Always on demand from the user's range and filters, nothing stored; separate modules, read only, isolated from tracking |

**Done when:** each rule has tests with a hand-checked saving (money and token versions), the regenerated showcase backtest reports its error and range coverage, the API returns the same advice and forecast as the CLI, an import-contract check proves tracking code doesn't depend on Phase 4 modules, and after 30 days of the owner's M4 data the forecast error is reported on real usage.

---

### Phase 5: Interfaces

**Goal:** see the collected data and get advice without learning commands. MVP-1 keeps this slim and focused on compute data: collection, structure and accuracy (record 0016).

**Steps (MVP-1)**
1. **`aimeterix init` from the config file:** without `aimeterix.yaml` it writes a commented starter file; with it, it shows a summary, the user confirms, then it seeds the database (record 0013), runs `doctor` and prints `aimeterix start` and the proxy lines for clients. **S**
1b. **`aimeterix start` / `stop`:** start or stop `agent`, `proxy` and `serve` in the background (all by default, or one by name). PID files and logs in `~/.aimeterix/`; `start` refuses to run a second copy; `stop` lets the agent and proxy flush their buffers before exiting. Keeps running after the terminal closes; not after a reboot (auto-start is MVP-2). **S** `stop` verifies nothing is left running (record 0017); `stop --keep-proxy` and `uninstall --purge`.
1b/1c done early (2026-10-15, owner's request): three background processes under one command, so a crash in one never stops the proxy.
1c. **`aimeterix health`:** live check of the running setup: each process up or down (with uptime), last hardware sample and last proxied call (with age), database reachable, size and write delay. Exit code 0 when healthy, 1 when not, so scripts can use it. Same data from `GET /api/health`. `doctor` stays the static check (what this Mac can measure); `health` is the runtime check (is it measuring now). **S**
2. **`aimeterix serve`:** the localhost API (Phase 3) and the dashboard in one process. **S**
3. **Dashboard endpoint:** `GET /api/dashboard` (range, model, feature), same computation as the CLI. **S**
4. **Dashboard:** read-only, server-rendered, inline charts, no external requests. Panels: data coverage and gaps, usage per model, daily cost, hardware use, data issues. Browser login with a session cookie. **M**
5. **Advice on request:** "Get advice" calls the Phase 4 advice and forecast endpoints for the current filters; shows recommendations, forecast and budget status; nothing stored. **S**

**Decisions** (all settled; record [0016](decisions/0016-interfaces-mvp1.md))
| # | Decision | Outcome |
|---|---|---|
| D5.0 | Scope in MVP-1 | ✅ Init from config, dashboard on the API, advice on request; rest to MVP-2 |
| D5.1 | Dashboard tech | ✅ Server-rendered + inline charts; no library, build step or external files |
| D5.2 | Grafana | ✅ MVP-2 or later, through our API |
| D5.3 | Auth | ✅ Record 0013; browser uses an HttpOnly session cookie |
| D5.4 | Services and scheduled jobs | ✅ MVP-2 (first item); in MVP-1 started with `aimeterix start` |
| D5.5 | Init | ✅ From `aimeterix.yaml`, summary, user confirms; no wizard |
| D5.6 | HTML report and weekly send | ✅ MVP-2 |
| D5.7 | Alerts and channels | ✅ MVP-2 |
| D5.8 | Secrets for background jobs | ✅ MVP-2 |
| D5.9 | Exports | ✅ Phase 3 usage CSV only; rest MVP-2 |
| D5.10 | Dashboard editing | ✅ Read-only; edits by CLI and API |
| D5.11 | Dashboard API | ✅ One endpoint, same computation as the CLI |
| D5.12 | Advice | ✅ On request, on demand, nothing stored |
| D5.13 | Start and stop | ✅ `aimeterix start` / `stop` for agent, proxy, serve, in the background; no auto-start (MVP-2) |
| D5.14 | Health | ✅ `aimeterix health` and `GET /api/health`: runtime check with exit code; `doctor` stays the static check |

**Done when:** on a fresh Mac, `pip install` → edit `aimeterix.yaml` → `aimeterix init` → `aimeterix start` → `aimeterix health` reports healthy → the dashboard shows real hardware data and proxied calls within 1 hour, with coverage and gaps, without reading docs; one test proves the dashboard endpoint, CLI and API return the same numbers; the page makes no external requests.

---

### Phase 6: Release v0.1.0 ("ready to go")

**Goal:** someone outside the project can install, run and trust it.

**Steps**
1. **Packaging:** PyPI package with extras (`ai-meterix[postgres]`, `[duckdb]`, `[proxy]`); Docker image; `deploy/docker-compose.yml` (agent + proxy + `serve` for API and dashboard + Postgres). **M**
2. **CI/CD:** GitHub Actions for lint, types, unit, conformance (all connectors), integration, build; release on tag. **M**
3. **Docs:** quick start, concepts, connector guide ("write your own in an afternoon"), configuration reference, FAQ, privacy statement. **M**
4. **Security pass:** proxy is not an open relay, secrets only from env, no prompts stored, dependency audit, `SECURITY.md`. **S**
5. **Showcase:** `aimeterix demo` loads the showcase data and opens the dashboard. **S**
6. **Launch material:** README with screenshot, a short demo video, a blog post: "What does your AI team's compute really cost?". **S**

**Done when:** a fresh machine goes from `pip install` to report with no errors, on Linux and macOS, with each of the three databases.

---

### Improvement backlog (discussed, deferred)

Items we discussed and agreed to postpone. Each is reviewed again when its stage comes up.

| Item | Target | From |
|---|---|---|
| Several machines writing to one database; collector API | MVP-2 | D2.3 |
| Custom currency per profile (MVP-1 is USD only) | Later | D3.2 |
| SDK wrapper (Python, then JS) | MVP-2 | D2.8 |
| NVIDIA verified on real hardware; deep NVML metrics | MVP-2 / tier 2 | D2.7, record 0009 |
| Host registry with tags; cloud instance tags | MVP-2 | Phase 2 review |
| OpenTelemetry (OTLP) receiver | Later | D2.4 |
| LM Studio and MLX model detection | Later | D2.9 |
| More caller signals: API-key fingerprint, local process lookup, user-agent | Later | D2.2b |
| Per-caller proxy tokens for teams | Later | D2.2b |
| Proxy budgets that warn or block | Later | Phase 2 guide |
| Import provider usage export / usage API; automatic match report (flag > 1%) | MVP-2 | D3.8 |
| Batch API token metering from results files; batch pricing | MVP-2 | D3.10 |
| Several hosts with own profiles; project-level default profile; site-level fees | MVP-2 | C7 |
| API access from other machines (token) | MVP-2 | record 0013 |
| Reference price list refresh command | MVP-2 | D3.7 |
| Time-of-day energy rates; CO₂ per kWh | Later | D3.5 |
| Files API storage fees; tool fees; batch, priority and long-context price tiers | Later | D3.7, D3.10 |
| More users, password reset, single sign-on | Later | record 0013 |
| Hardware amortization (only if users ask) | Later | D3.1 |
| Over-provisioned GPU rule (needs fees tied to a GPU) | MVP-2 | D4.5 |
| Budgets per feature, host, team | MVP-2 | D4.9 |
| macOS services (agent, proxy, serve) and scheduled jobs; `.env` secrets file for them (first MVP-2 item) | MVP-2 | D5.4, D5.8 |
| HTML report file and weekly email | MVP-2 | D5.6 |
| Alerts (budget at risk / over, collector stopped, weekly digest) via macOS notification, Slack, email | MVP-2 | D5.7 |
| Interactive setup wizard | MVP-2 | D5.5 |
| Dashboard admin forms (price, budget, dismiss) | MVP-2 | D5.10 |
| Exports beyond usage CSV; Parquet | MVP-2 | D5.9 |
| Grafana through our API | MVP-2 or later | D5.2 |
| Rules: reasoning effort, batch candidates, error and retry waste | Later | D4.5 |
| statsmodels forecast as optional extra (only if backtests show a gain) | Later | D4.1 |
| LLM-written summaries, off by default | Later | D4.3 |

### After v1 (not planned in detail yet)

- Cloud bill import: AWS CUR, GCP billing export, Azure cost export (reconcile metered vs billed)
- Kubernetes: read from DCGM exporter / Prometheus, cost per namespace and pod
- AMD (ROCm), Windows and Intel collectors
- Big-data connectors (ClickHouse, Parquet/Iceberg, BigQuery, Snowflake); multi-node collector API
- Team features: users, roles, SSO
- Market data: public GPU rental price index (ties back to the compute-as-a-commodity story)

---

## 4. Cross-cutting rules

**Developer experience**
- `make setup` gives a working environment in one command; `make check` runs everything CI runs.
- Devcontainer and docker-compose for Postgres so contributors need nothing installed but Docker.
- Every PR: tests, types, lint, and an updated doc if behavior changed.

**Testing strategy**
- Unit tests for all engines with small hand-checked fixtures.
- Conformance suite is the gate for every connector.
- Integration tests run the agent, proxy and a real database together.
- The showcase dataset doubles as a regression test (numbers must not drift silently).

**Privacy and security**
- Never store prompts, responses or API keys. Only counts, model names, timings, tags.
- Secrets only from environment variables; config files are safe to commit.
- Proxy and dashboard bind to localhost by default.

**Versioning**
- Semantic versioning. The data model and `StorageBackend` interface are public APIs: breaking them needs a major version and a migration.

---

## 5. How we use this guide

1. We review one phase at a time, starting with Phase 0.
2. For each decision, you pick or change my recommendation; I write a short ADR in `docs/decisions/`.
3. When a phase has no open decisions, we build it, then mark it done here.

### Progress

| Phase | Status |
|---|---|
| 0. Foundations | ✅ done: records 0001 to 0009 accepted |
| 1. Core and storage connectors | 🟢 decisions settled (record 0010); ready to build |
| 2. Collectors | 🟢 decisions settled (record 0011); ready to build after Phase 1 |
| 3. Cost engine | 🟢 decisions settled (records 0012 to 0014); ready to build after Phase 2 |
| 4. Advisor and forecast | 🟢 decisions settled (record 0015); ready to build after Phase 3 |
| 5. Interfaces | 🟢 decisions settled (record 0016); slim MVP-1, ready to build after Phase 4 |
| 6. Release v0.1.0 | ⚪ not started |
