# FAQ

**Which machines does it support?**
Hardware metering (energy, CPU, GPU, memory) works on Apple Silicon Macs. On other machines the proxy, cost engine, API and dashboard work, but the agent has no hardware backend yet; `aimeterix doctor` says what your machine can measure.

**Does it slow down my apps?**
The proxy adds a few milliseconds per call and does not slow streaming. The agent stays under 1% of one CPU core at the default 10-second interval and backs off if it goes over.

**Does it see my prompts?**
The proxy forwards them, as it must, but stores only the model name, token counts and timings. Prompts, answers and API keys are never written anywhere.

**Why is my cost $0?**
Either no electricity price is set (`aimeterix profile set --rate 0.30`) or the model has no token price (`aimeterix prices set MODEL --input N --output N`, or `aimeterix prices apply-reference`). The dashboard lists both under Data issues.

**Why are calls or tokens 0?**
Your app calls the provider directly. Change its base URL to the one `aimeterix providers` shows.

**Can I meter a provider that is not built in?**
Yes, if it answers in the OpenAI, Anthropic, Gemini or Ollama format: `aimeterix providers add NAME --base-url URL`. Most providers use the OpenAI format.

**How accurate is it?**
Token counts come from the provider's own response, so they match its bill. Energy totals are measured by the chip; the split between apps and models is an estimate from how much each process used. The dashboard shows how many hours were actually measured each day.

**Where is my data, and how do I remove it?**
In `~/.aimeterix/` (or the database you chose). `aimeterix uninstall --purge` stops everything and deletes it; then `pip uninstall ai-meterix`.

**Does it start when the Mac starts?**
Not yet: run `aimeterix start` after a restart. Services that start at login come in a later version.
