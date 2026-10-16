# Changelog

## 0.1.0 (preview)

First preview: metering, costs and a dashboard on one machine.

- **macOS installer** with its own Python, for Apple Silicon and Intel (`.pkg`, or `.tar.gz` with `install.sh` and no admin password). It shows the default settings before installing, and the commands, dashboard address and manual when done. Not signed yet. The Python package works on Linux too.
- **Setup:** `aimeterix init` writes the config file with the default settings, shows them, lets you change any, then creates the database after you confirm. `aimeterix start`, `stop` and `health` run and check the agent, proxy and API; `aimeterix uninstall --purge` removes the data.
- **Files in standard places:** the config, database and logs live in the usual folders for macOS (`~/Library/Application Support/aimeterix`, `~/Library/Logs/aimeterix`) and Linux (XDG), the same from any folder; `aimeterix paths` shows them and `AIMETERIX_HOME` puts them in one folder.
- **Hardware (Apple Silicon):** CPU, GPU, memory, power and energy, split by process and by local model (llama.cpp, Ollama). `aimeterix doctor` shows what the machine can measure.
- **LLM calls:** a local proxy records the model and token counts (input, output, cache read and write) of every call, streaming included. Ten cloud providers built in: OpenAI, Anthropic, Google Gemini, Mistral, Groq, Together AI, Fireworks AI, OpenRouter, DeepSeek and xAI; add any other with `aimeterix providers add`.
- **Costs:** energy × PUE × your rate, fixed fees, and tokens × price per model, per day. Prices are versioned; a dated list of public prices for 53 models is included. `aimeterix usage` prints or exports CSV.
- **Storage:** SQLite (default), PostgreSQL or DuckDB, behind one connector interface.
- **API and dashboard:** a localhost API with `user` and `admin` accounts, and a read-only dashboard at http://127.0.0.1:8788/ with a built-in Help page.
- **Settings:** live settings and providers in the database, reloaded within a minute, every change logged.

### Coming soon

- Linux and Windows: installers and hardware metering
- Advisor and forecast: what to change to save money, and next month's cost
- General improvements: NVIDIA GPUs, starting at login, several machines on one dashboard
