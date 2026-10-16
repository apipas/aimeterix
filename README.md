# AI Meterix

**Know what your AI compute costs, what you waste, and what next month will cost.**

AI Meterix meters the AI work on your machine: the energy the chip uses, the tokens each model reads and writes, local or in the cloud, and what both cost in your currency. It runs on your machine, stores its data in a database you choose, and never stores prompts, answers or API keys.

> **Status: 0.1 preview.** Hardware metering works on Apple Silicon Macs. The LLM proxy, cost engine, API and dashboard work on macOS and Linux. Advice and a monthly forecast come in 0.2. See [CHANGELOG.md](CHANGELOG.md) and the plan in [docs/PLAN.md](docs/PLAN.md).

## Install

Each [release](https://github.com/mabuthraa/gocompute/releases/latest) has a macOS installer that carries its own Python, so nothing else is needed. Pick the one for your Mac's processor (Apple menu > About This Mac shows "Apple M…" or "Intel"):

| Mac | Download | Installs to |
|---|---|---|
| Apple Silicon (M1 and later) | `ai-meterix-<version>-macos-arm64.pkg` | `/usr/local/ai-meterix` |
| Intel | `ai-meterix-<version>-macos-x86_64.pkg` | `/usr/local/ai-meterix` |
| Either, without an admin password | `ai-meterix-<version>-macos-<processor>.tar.gz`, then `./install.sh` | `~/.local` |

The installer shows the default settings first and, when it is done, the commands, the dashboard address and where the manual is. It asks for your password once, to install; AI Meterix itself never runs as root. It is not signed yet: if macOS says the developer cannot be verified, open System Settings > Privacy & Security and choose Open Anyway.

Or in one line, for this user only: `curl -fsSL https://github.com/mabuthraa/gocompute/releases/latest/download/install.sh | sh`

On Linux, or with Python 3.10 or newer anywhere, install the Python package (Linux installers are coming soon):

```bash
pip install ai-meterix                 # or: uv tool install ai-meterix
pip install "ai-meterix[postgres]"     # to store data in PostgreSQL
pip install "ai-meterix[duckdb]"       # to store data in DuckDB
```

Until the package is on PyPI: `pip install git+https://github.com/mabuthraa/gocompute.git`.

## Coming soon

- Linux and Windows installers and hardware metering
- Advice on what to save, and a monthly cost forecast
- General improvements

## Quick start

```bash
aimeterix init       # shows the default settings, lets you change any, creates the database
aimeterix start      # runs the agent, proxy and dashboard in the background
aimeterix health     # is everything measuring? (exit code 0 when healthy)
```

Then:

1. Open http://127.0.0.1:8788/ and log in as `user`, password `user`. You choose a new password the first time.
2. Set your electricity price: `aimeterix profile set --rate 0.30` (USD per kWh).
3. Point your apps at the proxy instead of the provider. `aimeterix providers` shows the address for each one, for example `http://127.0.0.1:8787/openai/v1`. Your app keeps its own API key; it passes through and is never stored.
4. Load public token prices: `aimeterix prices apply-reference --dry-run`, then without `--dry-run`.

`aimeterix stop` stops everything. `aimeterix uninstall --purge` also deletes the config and data, and `aimeterix paths` shows where they are. The dashboard has a **Help** page that explains every number and lists every command with its options.

## What it measures

| | Cloud models | Local models (llama.cpp, Ollama, ...) |
|---|---|---|
| Calls and tokens | Per model: input, output, cache read and write | Same |
| Energy | Not measured (it is spent in the provider's data center) | Measured on this machine, split by process |
| Cost | Tokens × the model's price | The model's share of the energy × your rate |

Ten cloud providers are built in: OpenAI, Anthropic, Google Gemini, Mistral, Groq, Together AI, Fireworks AI, OpenRouter, DeepSeek and xAI. Add any other provider, or a model server on your network, with `aimeterix providers add NAME --base-url URL`, and price its models with `aimeterix prices set MODEL --provider NAME`. A model with no price costs 0 until you set one.

Calls that do not go through the proxy are not seen. AWS Bedrock cannot be metered through a proxy yet; Azure OpenAI works once you add your own address.

## How it works

Three small processes run on your machine, all started by `aimeterix start`:

- **agent** samples the chip every few seconds (power, energy, CPU, GPU, memory) and which processes use it.
- **proxy** sits between your apps and the AI providers. It forwards every call unchanged and records the model and token counts.
- **serve** runs the API and the dashboard. Costs are computed from the measurements each time you look, so a corrected price also corrects past days.

They are separate on purpose: if one stops, the proxy keeps forwarding, so your apps keep working. Each can also run on its own in a terminal (`aimeterix agent`, `aimeterix proxy`, `aimeterix serve`).

Every command finds the same files, whatever folder you run it in:

| | macOS | Linux |
|---|---|---|
| Config (`aimeterix.yaml`, `.env`) | `~/Library/Application Support/aimeterix` | `~/.config/aimeterix` |
| Database (`meter.db`), buffers | `~/Library/Application Support/aimeterix` | `~/.local/share/aimeterix` |
| Logs | `~/Library/Logs/aimeterix` | `~/.local/state/aimeterix/logs` |

Set `AIMETERIX_HOME` to keep everything in one folder instead. Data goes to SQLite by default; PostgreSQL and DuckDB work too. Settings live in the database after `init`; change them with `aimeterix config`, `aimeterix providers` and `aimeterix prices`, or the admin API.

## Privacy and security

- Only counts are stored: model names, token numbers, timings, energy. Never prompts, answers or API keys.
- The proxy, API and dashboard listen on this machine only (127.0.0.1). The only outbound connections are your own calls, forwarded to the provider you chose. No account, no telemetry, no update check.
- No admin rights at run time: nothing runs as root, and nothing is written outside the folders above and the program's own folder. Only the `.pkg` and `.deb` installers ask for a password, once, to install the program.
- Two accounts guard the API and dashboard: `user` reads, `admin` can also change settings. Passwords are salted scrypt hashes.

To report a security problem, see [SECURITY.md](SECURITY.md). More answers in [docs/FAQ.md](docs/FAQ.md).

## Repository map

| Path | What lives there |
|---|---|
| `src/ai_meterix/` | The package: `core/` data model and config, `collectors/` agent and proxy, `storage/` database connectors, `pricing/` cost engine, providers and prices, `interfaces/` command line, API and dashboard |
| `tests/` | Unit, connector conformance and integration tests |
| `docs/PLAN.md` | The phased plan |
| `docs/decisions/` | One short record per settled decision |
| `examples/` | The starter config (`aimeterix.example.yaml`) |

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). License: [Apache-2.0](LICENSE). See [NOTICE](NOTICE) for attribution.
