# 0015: Advisor and forecast: on demand, separate from tracking

- Status: accepted; budget delivery (item 9) moved to MVP-2 by 0016
- Date: 2026-10-11
- Plan ref: D4.0 to D4.11

## Context
Phase 4 adds advice with estimated savings, a forecast with a range, and budgets. Both depend on prices and cost profiles, which an admin can change at any time, including for past periods (record 0012). The owner wants this simple, part of MVP-1, and built so that changing it can never break metering.

## Decision
1. **In MVP-1 (D4.0).** The advisor, forecast and one budget ship in MVP-1, then get iterated.
2. **Always on demand (D4.11).** Advice, forecasts, backtests and budget status are computed per request from the hourly sums, using the same filters as `aimeterix usage` (time range, host, model, feature). Nothing is stored or cached, so a price or profile edit shows up on the next request. `Recommendation` and `Forecast` are result types, not tables.
3. **Separate modules (D4.11).** `advisor/` and `forecast/` depend only on `core` types, the read-only `StorageReader` and the cost engine's public functions in `pricing/`. Collectors, storage and core never import them, and the agent and proxy processes never load them. An import contract checked in `make check` enforces this. Each rule runs in isolation: one failing rule is reported as "rule failed" and the others still return.
4. **Starter rules (D4.5).** R1 expensive model for short calls; R2 prompt caching not used; R3 cache written but not reused; R4 local vs API cost (always low confidence); R5 paid idle capacity (only with fixed fees); R6 data gaps (untagged calls, price not set, reference list older than 90 days). The old "idle rented GPUs" rule is replaced by R5; "over-provisioned GPUs" moves to the backlog. Thresholds are settings, scaled to the selected time range.
5. **Tone (D4.2).** Plain language, one action each, saving per month as a range unless confidence is high, evidence traceable to `aimeterix usage`. Models with no price show the saving in tokens and "price not set", never 0.
6. **Dismissals (D4.6).** An admin can dismiss a recommendation (rule + feature or model, until a date or for good, with a note). Dismissals are stored as settings with the usual change log. A dismissed item returns if its saving grows past 2×.
7. **Forecast (D4.1, D4.4, D4.7).** Our own simple model, no heavy dependencies. Fixed fees are taken exactly from the profile; energy is a run-rate with weekly pattern; API token counts are forecast per model and type, then priced with current prices. Default horizon: rest of the current month and the next calendar month. History comes from the selected range: under 7 days, no forecast; 7 to 20 days, run-rate marked "early"; 21 days or more, trend plus weekly pattern. The range targets 8 of 10 days inside.
8. **Backtesting (D4.8).** Rolling backtest over 7 and 30 days, reporting error and range coverage, on demand (`aimeterix forecast --backtest`). The showcase dataset is regenerated for the operational scope.
9. **Budgets (D4.9).** One monthly money budget per project plus optional token budgets per model, stored as settings. Status "at risk" when the forecast's high end crosses it, "over" when the expected value or actual spend does. Slack and email delivery stay in Phase 5.
10. **Interfaces (D4.10).** `aimeterix advise` and `aimeterix forecast` in the CLI, and matching API endpoints (read for `user`; dismissals and budgets for `admin`), built in Phase 4.
11. **LLM summaries (D4.3).** Optional and off by default; backlog.

## Consequences
- No stale advice or forecasts, and no background jobs; each request costs a few hourly-sum queries.
- Advisor or forecast bugs can't stop metering; the import contract makes the boundary visible in review.
- Budgets and dismissals reuse the settings table, so the storage interface doesn't change.
- Forecasts for a single developer will have wide ranges; they become more useful with a team (MVP-2).
