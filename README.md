# AI Meterix

AI Meterix shows what AI costs on your Mac. It measures the energy your chip uses for local models (llama.cpp, Ollama) and counts the tokens every model uses, local or in the cloud. It turns both into money in your currency, in a dashboard on your machine.

It runs only on your machine. It never stores prompts, answers or API keys.

## Versions

| Version | Status | What it has |
|---|---|---|
| 0.1.0 | Preview | Energy metering on Apple Silicon Macs, token counting for 10 cloud providers and local models, daily costs, a local dashboard |

All changes are listed in [CHANGELOG.md](CHANGELOG.md).

### Coming soon

- Linux and Windows support
- Advice on what to change to save money, and a monthly cost forecast
- General improvements

## Install

**One line** (macOS, Apple Silicon or Intel, no admin password):

```bash
curl -fsSL https://github.com/apipas-com/aimeterix/releases/latest/download/install.sh | sh
```

It downloads the right build for your Mac, installs it, and starts AI Meterix.

**Installer package:** download the `.pkg` for your Mac from the [releases page](https://github.com/apipas-com/aimeterix/releases/latest): `macos-arm64` for Apple Silicon (M1 and later), `macos-x86_64` for Intel. Then run `aimeterix start` in Terminal. The package is not signed yet: if macOS blocks it, open System Settings > Privacy & Security and choose Open Anyway.

**From source** (needs [uv](https://docs.astral.sh/uv/)):

```bash
git clone https://github.com/apipas-com/aimeterix.git && cd aimeterix
uv run aimeterix start
```

## Use it

| | Address |
|---|---|
| Dashboard | http://127.0.0.1:8788/ (log in as `user`, password `user`; you choose a new one at first login) |
| Manual | http://127.0.0.1:8788/#help |
| Proxy for your apps | http://127.0.0.1:8787/ (for example `http://127.0.0.1:8787/openai/v1`) |

To have your apps' AI calls counted, point them at the proxy instead of the provider. Your app keeps its own API key. `aimeterix providers` shows the address for each provider.

| Command | What it does |
|---|---|
| `aimeterix start` | Start AI Meterix in the background (again after a restart of the Mac) |
| `aimeterix stop` | Stop it |
| `aimeterix health` | Check that everything is running and measuring |
| `aimeterix profile set --rate 0.30` | Set your electricity price per kWh |
| `aimeterix usage` | Show today's usage and costs in Terminal |
| `aimeterix providers` | List the AI providers and their proxy addresses |
| `aimeterix init` | Optional, before the first start: change the default ports or database |
| `aimeterix paths` | Show where the settings, data and logs are |
| `aimeterix uninstall --purge` | Stop and delete all settings and data |
| `aimeterix --help` | Every command |

## Privacy

Only counts are stored: model names, token numbers, timings and energy. Everything listens on 127.0.0.1 only. There is no account, no telemetry and no update check. See [SECURITY.md](SECURITY.md) to report a problem.

## License

[Apache-2.0](LICENSE). To help, see [CONTRIBUTING.md](CONTRIBUTING.md).
