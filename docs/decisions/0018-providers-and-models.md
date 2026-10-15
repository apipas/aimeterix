# 0018: Providers and their models

- Status: accepted
- Date: 2026-10-15
- Plan ref: Phase 2 (proxy), Phase 3 (prices); dashboard forms in MVP-2

## Context
The proxy knew two cloud routes (OpenAI, Anthropic), set as the live setting `proxy.upstreams`, and prices were a flat list of models. The owner wants each provider to be one record that holds its address and its models with their token prices, local and cloud alike, so a user can add any AI provider or model, and a model without a price simply costs 0. A management screen for this comes in MVP-2.

## Decision
1. **A provider is a record in the database** (`providers`, schema v3): name, label, base URL, kind (`cloud` or `local`), the path an app adds (`app_path`), response format, enabled. Its name is its proxy route: apps call `http://127.0.0.1:<port>/<name><app_path>`.
2. **Ten providers are built in** (`pricing/providers.json`): OpenAI, Anthropic, Google Gemini API, Mistral, Groq, Together AI, Fireworks AI, OpenRouter, DeepSeek and xAI. Their addresses were checked on the providers' own docs on 2026-10-15. All but Anthropic and Gemini use the OpenAI response format, which the proxy already reads; DeepSeek's `prompt_cache_hit_tokens` is now read as cached input.
3. **Seeding:** the first start of the proxy, API or `config` adds the built-in providers and any `proxy.upstreams` from the config file (or from the old live setting) that the database lacks. After that the database wins. `proxy.upstreams` is no longer a live setting.
4. **Editing:** `aimeterix providers add|set|enable|disable` and `PUT /api/providers/<name>` (admin). Built-in providers can be changed or disabled, not deleted. Each changed field is logged as `provider.<name>.<field>` in the settings change log. The proxy reloads routes within a minute.
5. **Models and prices:** each row in `model_prices` names its provider (`prices set MODEL --provider NAME`). A price still belongs to a model name, so two providers serving the same model name share one price; the shipped list keeps one (Groq's `openai/gpt-oss-120b`). A model with no price costs 0 and is marked "price not set".
6. **Local providers** (kind `local`, e.g. a llama.cpp server added by name) are treated like the default local route: their models cost their energy share, never tokens.
7. **Reference prices** now cover the main models of nine providers (OpenRouter is left out: it passes other providers' prices through). Prices with conditions the table cannot express are noted in the list: Gemini promotional prices ending on a date, DeepSeek off-peak half price, xAI double price above 200K prompt tokens.

## Consequences
- Any OpenAI-compatible provider, cloud or local, can be metered by adding one record, without a code change.
- Not supported: AWS Bedrock (requests are signed for its own host, which a proxy in between breaks) and per-hour or per-context-size prices. Azure OpenAI works once the user adds their own address.
- If two providers ever sell the same model name at different prices, prices move to (provider, model); the provider column already stored makes that a data migration, not a redesign.
