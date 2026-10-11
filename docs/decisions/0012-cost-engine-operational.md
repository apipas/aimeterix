# 0012: Cost engine: operational cost and token counts

- Status: accepted
- Date: 2026-10-11
- Plan ref: D3.1 to D3.10

## Context
Phase 3 turns usage into money. The owner wants the output to read like a cloud LLM provider's usage page (Claude, ChatGPT and others), so a client can check that measured usage matches what the provider reports. Hardware purchase cost would complicate the project and is not needed. The first release (MVP-1) is one project, one machine, one agent.

## Decision
1. **Scope (D3.1).** Operational cost only: measured energy, configured fixed fees, and API token cost. No purchase price, lifetime or amortization.
2. **Cost profile (D3.2, D3.3).** One profile per machine (host), stored in the database: name, region (label), currency, energy rate per kWh, PUE (default 1.0), fixed fees per month (e.g. reserved capacity, maintenance), fee spread (`calendar` default, or `uptime`). No FX conversion: all prices use the profile's currency. No user-entered effective dates: every change is saved as a new version with its save time, and days before a change keep the old values.
3. **Fixed fees and idle (D3.4).** Fees are spread over the real hours of each month (672 to 744 with `calendar`; hours the machine was on with `uptime`). Each active hour's share goes to the active models by energy share; hours with nothing active appear as one **idle overhead** line. Active means GPU ≥ 10% or CPU ≥ 10% (configurable).
4. **Energy (D3.5).** Whole-device power when macOS exposes it, otherwise chip energy plus a configurable overhead in watts (default 0, noted in reports); multiplied by PUE.
5. **Output (D3.6).** Token counts **per model**, split into input (uncached), output (including reasoning tokens), cache read and cache write, with requests, totals per model and per day, kWh and cost. Filters by machine and feature. Delivered by the CLI (`aimeterix usage`), CSV export, and a localhost API (record 0013). Local per-request cost, where shown, is an estimate: the hour's model cost split by tokens.
6. **Token normalization.** Cached counts come from the provider's response, not estimation. Providers that include cached tokens in input (OpenAI, Gemini) have them subtracted; Anthropic's input already excludes them. Runtimes that report no cache count (Ollama) show "—", never 0. The raw usage fields are stored for checking.
7. **Prices (D3.7).** A separate `model_price` table: per model, prices per 1M tokens for input, output, cache read and cache write, with a validity period. The admin can edit prices, including past periods; costs are derived, so reports follow at once. Every edit goes to a `price_change` log (who, when, old → new), and affected periods show an "edited" badge. New local models default to 0. New API models default to 0 with `price_set = false`, shown as "price not set". A dated reference list of public OpenAI and Anthropic prices ships with the project and is applied only by an admin action (`aimeterix prices apply-reference`).
8. **Provider check (D3.8).** Columns match provider usage pages, and the proxy stores the provider's request id. Importing the provider's usage export with an automatic match report (flag > 1%) is MVP-2.
9. **Computation (D3.9).** On demand from hourly rollup sums, with a cache dropped when a profile or price changes; exact decimal arithmetic; tokens summed per hour before pricing; days in the local time zone.
10. **Batch and Files APIs (D3.10).** MVP-1 records batch jobs (count, model, status) with tokens marked "not metered yet", and file uploads (count, size). Batch token metering and batch pricing come later.

## Consequences
- The cost engine needs no background job and adds no sampling load.
- With fixed fees, the fee share usually outweighs energy on a single small device; the idle overhead line shows what unused time costs.
- Editable price history is flexible but can change past figures; the change log keeps this traceable.
- Hardware amortization, provider export import, batch metering, time-of-day rates and more are in the PLAN backlog.
- Related changes to the data model, storage and collectors are in record 0014; configuration and API access in record 0013.
