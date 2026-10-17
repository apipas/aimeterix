# 0011: Collectors for MVP-1

- Status: accepted; amended by 0014 (LlmCall fields, `X-AIMeterix-Feature` replaces `X-AIMeterix-Project`)
- Date: 2026-10-10
- Plan ref: D2.1 to D2.9

## Context
Phase 2 gets usage data in. The first release (MVP-1) targets one Apple Silicon Mac (record 0009). The owner wants a detailed power report, including which app and which local LLM model consumed energy, and the meter's own cost must be known. Release stages: **MVP-1** is one Mac; **MVP-2** adds several machines and wider integrations; **Later** is the backlog.

## Decision
1. **Sampling (D2.1).** Default interval 10 s, configurable from 1 s to 300 s, with a 1 s burst mode for a limited time. Readings that macOS keeps as running totals (CPU time, GPU busy time, energy, per-app CPU and GPU time) are stored as deltas, so totals are exact at any interval. The agent measures and reports its own CPU, memory and energy use ("tracking cost").
2. **Mac power (D2.5).** Chip energy (CPU, GPU, Neural Engine) from IOReport energy counters; IOKit and libproc called directly from Python, no admin rights. `powermetrics` (needs sudo) is an opt-in cross-check only. Spawning command-line tools per sample is avoided because of its overhead.
3. **Per-app and per-model energy (D2.9).** Per-app CPU time (libproc) and GPU time (IOKit GPU clients) are sampled; per-app energy is **attributed** from each app's share and labelled as an estimate. Loaded local models are detected for **Ollama** (its local status API) and **llama.cpp** (process arguments and its `/props` and `/metrics` endpoints) and linked to their process. Full process scans run at most every 60 s; GPU clients and top CPU users every sample.
4. **LLM proxy (D2.2).** Port the standard-library prototype: OpenAI-compatible and Anthropic APIs, streaming, cached tokens, localhost only, optional key allowlist, prompts and responses never stored.
5. **Callers (D2.2b).** A `Caller` entity represents who made an LLM call. MVP-1 identifies callers by **app name in the URL path** (`/app/<name>/v1`) and **tag headers** (`X-AIMeterix-App`, `X-AIMeterix-Project`, `X-AIMeterix-Tag-*`). Header wins over path. Calls with neither go to an `unknown` caller.
6. **Test hardware (D2.6).** The owner's M4 Mac with 64 GB; `doctor` records the exact chip.
7. **Deferred.** Several machines (D2.3) and the SDK wrapper (D2.8): MVP-2. NVIDIA (D2.7): small experimental module tested only on recorded `nvidia-smi` output, not in MVP-1. OpenTelemetry receiver (D2.4): later.

## Consequences
- The data model gains `Caller`, `ProcessSample` and `LocalModel`; `LlmCall` references a caller instead of a free-text project.
- Per-app energy is an estimate and every report says so; per-app Neural Engine use is not available on macOS.
- Token counts per local model need the proxy, except where llama.cpp reports them itself.
- IOReport is undocumented and can change with macOS updates; the probe checks it and `doctor` reports a fallback to estimates.
- More caller signals (API-key fingerprint, local process lookup, user-agent, per-caller proxy tokens) are in the backlog and fit the `Caller` entity without a schema break.
