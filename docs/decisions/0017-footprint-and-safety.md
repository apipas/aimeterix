# 0017: Footprint and safety on the user's machine

- Status: accepted
- Date: 2026-10-12
- Plan ref: Phase 2 (collectors), Phase 5 (`start` / `stop`)

## Context
AI Meterix runs on the machine it measures. The owner wants to be sure it cannot harm the Mac's security or performance, and that `aimeterix stop` really turns everything off.

## Decision
1. **No admin rights.** No code path uses sudo. `powermetrics` stays an opt-in cross-check, off by default.
2. **Observe only.** It reads counters (IOReport, IOKit, libproc, host statistics). It never changes process priorities, power settings, other apps or system settings.
3. **Nothing installed system-wide in MVP-1.** No launch agents or daemons, kernel or system extensions, login items or shell-profile edits. Its files live in `~/.aimeterix/` (database, logs, PID files) plus the Python package. Background services in MVP-2 will be opt-in.
4. **Network.** It listens only on `127.0.0.1` (proxy 8787, API 8788). The only outbound connections are the proxy forwarding the user's own LLM calls to the provider they chose. There is no telemetry and no update check.
5. **Data.** Prompts, responses and API keys are never stored (unchanged from the plan's rules).
6. **Performance budget**, measured by the agent itself and shown by `aimeterix health`:

   | Measure | Budget |
   |---|---|
   | Agent CPU | under 1% of one core on average at 10 s sampling |
   | Agent memory | under 100 MB |
   | Agent energy | under 0.1 W attributed |
   | Proxy delay | under 5 ms added per call; streaming not slowed |

   Over budget, `health` reports a problem and the agent backs off to a slower interval rather than compete with the user's work.
7. **`aimeterix stop`** lets buffers flush (at most about 2 s), stops `agent`, `proxy` and `serve`, then verifies that no AI Meterix process is left, ports 8787 and 8788 are free and PID files are removed; otherwise it names what is left and exits non-zero. Nothing restarts after a reboot. `aimeterix stop --keep-proxy` pauses metering of hardware while apps that use the proxy keep working.
8. **Removal.** `aimeterix uninstall --purge` stops everything, deletes `~/.aimeterix/` and prints the `pip uninstall` line. Nothing else on the machine needs cleaning.

## Consequences
- Every guarantee gets a test in Phase 2 or Phase 5: no sudo or writes outside `~/.aimeterix/`, servers bound to 127.0.0.1 only, the agent overhead benchmark on the owner's M4 Max, and start then stop leaving no process, port or PID file.
- Apps pointed at the proxy fail while it is stopped; the docs and `stop` output say so.
- Some readings that need admin rights (e.g. `powermetrics`) stay off unless the user opts in.
