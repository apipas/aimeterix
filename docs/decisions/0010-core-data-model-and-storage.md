# 0010: Core data model and storage layer

- Status: accepted; amended by 0014 (ownership removed, Project entity, rollup sums, reader/writer roles)
- Date: 2026-10-10
- Plan ref: D1.1, D1.2, D1.3, D1.5, D1.6, D1.7

## Context
Phase 1 builds the data contract every collector, engine and connector depends on. Record 0009 made hardware support platform-aware: Apple Silicon Macs use **unified memory** (CPU, GPU and Neural Engine share one pool), while NVIDIA machines have separate system RAM and per-GPU VRAM. The draft model put `mem_used` on every CPU and GPU sample, which double counts on a Mac and cannot say which pool a number belongs to.

## Decision
1. **Hardware model (D1.6).** Hardware is described as hosts, devices and memory pools:
   - `Host`: one machine (os, arch, chip, ownership, tags). Ownership and price attach here, since a whole machine is bought or rented.
   - `Device`: a compute unit on a host, kind `cpu`, `gpu` or `npu`, with the memory pool it draws from.
   - `MemoryPool`: type `unified`, `system` or `vram`, with total bytes. A Mac has one unified pool shared by all its devices; a Linux + NVIDIA box has one system pool plus one VRAM pool per GPU.
   - `DeviceSample`: ts, device, interval_s, util_pct, power_w, source.
   - `MemorySample`: ts, pool, used_bytes, plus optional wired, compressed, swap, pressure and gpu-allocated bytes.
   - Missing readings are **null, never zero**. Each sample has `source`: `measured`, `estimated` or `mock` (replaces the `mocked` flag).
   - Memory is in **bytes**. Common metrics are typed columns; platform-specific readings go in one small `extra` JSON field.
2. **Timestamps (D1.7).** Always UTC. Timezone-aware datetimes in Python; integer epoch milliseconds in SQLite; native `timestamptz` in Postgres and DuckDB. A sample's timestamp is the end of its interval.
3. **Validation (D1.1).** Standard-library `dataclasses` (frozen, slots) for the core model; **pydantic v2** only at the edges (config, REST API). The base install stays light.
4. **Postgres driver (D1.2).** **psycopg 3**, as the optional extra `ai-meterix[postgres]`.
5. **No ORM (D1.3).** One thin SQL module per connector.
6. **Retention (D1.5).** Raw samples kept 30 days; hourly rollups (min, mean, max, p95) kept 1 year; both configurable.

## Consequences
- Reports can show unified-memory pressure on a Mac and VRAM headroom on NVIDIA without special cases in the engines.
- `StorageBackend` writes device samples and memory samples separately and upserts hosts with their devices and pools.
- A narrow `(ts, device, metric, value)` table was rejected as the main store: flexible, but every report becomes a pivot and SQLite slows down. The `extra` field covers the flexibility need.
- The prototype import (Phase 1 step 8) maps its `cpu` and `gpu:N` rows onto this model, with memory converted from MB to bytes.
