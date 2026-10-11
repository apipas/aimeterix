# 0014: Data model, storage and collector changes from the Phase 3 review

- Status: accepted; amends 0010 and 0011
- Date: 2026-10-11
- Plan ref: C1 to C7 (Phase 3 review)

## Context
Records 0010 and 0011 were accepted before the cost engine was designed. The operational-cost scope (record 0012) and the API roles (record 0013) need a few small changes before Phase 1 code is written.

## Decision
1. **C1. Prices.** `PriceBook` is replaced by `CostProfile` (per host, versioned automatically) and `ModelPrice` (per model and token type, with validity period and change log). No hardware price fields.
2. **C2. Ownership removed.** `Host.ownership` (owned/rented) is removed from the model.
3. **C3. LLM calls.** `LlmCall` adds the provider's request id, cache-write tokens and reasoning tokens, and keeps the provider's raw usage fields (counts only; never prompts or responses). Batch jobs and file uploads are recorded as their own call types (record 0012).
4. **C4. Rollup sums.** Hourly rollups store **sums** (energy, active seconds per device, app and model, tokens per type) in addition to min, mean, max and p95, so costs can be recomputed beyond the 30-day raw window.
5. **C5. Two storage roles.** The storage interface is split into `StorageReader` (read only; SQLite opened with `mode=ro`, a read-only database user on Postgres) and `StorageWriter` (read and write). Both cover profiles, prices, settings and users. Reports and the API's `user` role get the reader; the agent, proxy, profile loader and the API's `admin` role get the writer.
6. **C6. Project entity.** A new `Project` groups hosts, e.g. a small data center. Each host belongs to one project.
7. **C7. Structure.** Project → host (one agent, one cost profile) → devices (CPU, GPUs, NPU). Per-device cost is the host's cost split by energy share. MVP-1: one project, one host. A project-level default profile and site-level fees are in the backlog.
8. **Caller tag renamed.** The per-call "project" tag becomes **feature**: header `X-AIMeterix-Feature` (replacing `X-AIMeterix-Project` from 0011), so "project" always means the group of machines.

## Consequences
- Multi-GPU servers fit as one host with several GPU devices; several servers fit as several hosts in one project (MVP-2).
- The conformance suite (Phase 1) must cover both storage roles and the new tables.
