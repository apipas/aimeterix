# 0009: Hardware support strategy

- Status: accepted
- Date: 2026-10-10
- Plan ref: Phase 2

## Context
What can be measured, and how, depends heavily on the hardware and the operating system. Apple Silicon Macs use **unified memory**: CPU, GPU and Neural Engine share one memory pool, so "GPU memory" and "RAM" are not separate numbers as on a PC with an NVIDIA card. NVIDIA GPUs expose much deeper counters (per-process memory, SM clocks, power, throttling) through NVML. Supporting every platform well from day one is not realistic for an open-source v1.

## Decision
1. **Platform probe first.** Every run starts with a probe that detects OS, CPU architecture and accelerators, and reports for each metric whether it is **supported, partial, or unsupported** on this machine, and why (e.g. "power needs elevated permission"). `aimeterix doctor` prints this as a plain table. Nothing is silently faked: unsupported metrics are stored as missing, never as zero.
2. **Pluggable platform backends** behind one interface, selected by the probe:
   - **Tier 1, first target: macOS on Apple Silicon (M-series).**
   - **Tier 2, next: Linux + NVIDIA** (NVML, deep metrics).
   - Later: Linux CPU-only, Windows, AMD ROCm, Intel.
   - A **mock backend** remains for demos and tests on any machine.
3. **Metric model is platform-aware.** Memory is recorded as a pool with a type (`unified` or `discrete`) instead of assuming separate GPU and system RAM.

## Apple Silicon: what we plan to measure
| Metric | Source (no admin rights unless noted) | Support |
|---|---|---|
| CPU use, per performance / efficiency cluster | host statistics (psutil + sysctl) | supported |
| Unified memory: used, wired, compressed, swap, memory pressure | `vm_stat` / host statistics, `memory_pressure` | supported |
| GPU utilization | IOKit accelerator statistics ("Device Utilization %") | supported, to be verified per chip generation |
| GPU memory in use | IOKit accelerator statistics (in-use system memory) | partial: it is a share of unified memory |
| Power: CPU, GPU, Neural Engine, package | `powermetrics` (needs sudo) or the IOReport energy counters (no sudo, undocumented API) | partial; decided in Phase 2 |
| Neural Engine activity | IOReport / `powermetrics` | partial |
| Thermal state / throttling | thermal pressure level | supported |
| Per-process GPU use | not exposed reliably | unsupported for now |

## Consequences
- The first release is honest and useful on Macs, which many AI developers run models on locally (Ollama, MLX, llama.cpp).
- Reports show coverage ("power not measured on this machine") so costs are never presented as more precise than they are.
- Undocumented macOS APIs can change between OS versions; the probe and per-version tests catch that, and the backend falls back to the documented sources.
- NVIDIA depth moves to the next tier; the prototype's `nvidia-smi` reader is kept as the starting point.
