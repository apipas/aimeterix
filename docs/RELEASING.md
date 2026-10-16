# Releasing

A release is one tag. GitHub Actions (`.github/workflows/release.yml`) builds the packages on macOS, installs each on a clean Mac of its processor (Apple Silicon and Intel), and only then publishes the GitHub Release. Releases are macOS only for now; `packaging/build.py` also builds the Linux `.deb` and `.tar.gz`, ready for when Linux support ships.

## Steps

1. Make a branch `release/<version>` (e.g. `release/0.1.0`) from `main`.
2. Set the version in `pyproject.toml` and `src/ai_meterix/__init__.py`, run `uv lock`, and add its section to `CHANGELOG.md` (`## 0.1.0 ...`; the release notes are taken from it).
3. `make check` passes; commit and push the branch. Every push runs CI and builds and installs the packages on clean Macs, without publishing anything.
4. When both are green, merge the branch into `main`, then tag and push: `git tag v0.1.0 && git push origin v0.1.0`. Only the tag publishes the GitHub Release; a tag that does not match the package version fails the first job.

`uv.lock` is committed on purpose: CI and every developer install the same dependency versions.

Versions starting with `0.` are published as pre-releases.

## What a release contains

| File | For | Built on |
|---|---|---|
| `ai_meterix-<v>-py3-none-any.whl`, `ai_meterix-<v>.tar.gz` | `pip` / `uv`, any system | macOS |
| `ai-meterix-<v>-macos-arm64.pkg`, `-macos-x86_64.pkg` | macOS installer, Apple Silicon / Intel | macOS |
| `ai-meterix-<v>-macos-<arm64 or x86_64>.tar.gz` | macOS with `install.sh`, no admin password | macOS |
| `install.sh`, `VERSION`, `SHA256SUMS` | the one-line install; it checks the SHA-256 | Linux |

The `.pkg` shows `packaging/macos/welcome.html` (what it is, default settings, coming soon) before installing and `conclusion.html` (commands, dashboard and manual addresses) when done; it puts the README, FAQ, changes and security notes in `/usr/local/ai-meterix/docs`.

Every package except the Python package carries its own Python (`PYTHON` in `packaging/build.py`, from python-build-standalone through `uv`, which checks the download hashes), so it is built per processor. AI Meterix's own code is the same everywhere.

Build locally: `uv build`, then `python packaging/build.py dist/*.whl --target all` (`.deb` needs `dpkg-deb`; add `--pkg` on a Mac for the `.pkg`).

## Where each one installs

| Package | Program | Command | Removed by |
|---|---|---|---|
| `.pkg` | `/usr/local/ai-meterix` | `/usr/local/bin/aimeterix` | `sudo rm -rf /usr/local/ai-meterix /usr/local/bin/aimeterix && sudo pkgutil --forget io.github.mabuthraa.aimeterix` |
| `.deb` | `/opt/ai-meterix` | `/usr/bin/aimeterix` | `sudo apt remove ai-meterix` |
| `install.sh` | `~/.local/lib/ai-meterix` | `~/.local/bin/aimeterix` | `rm -rf ~/.local/lib/ai-meterix ~/.local/bin/aimeterix` |

`aimeterix uninstall` prints the right line; it reads `INSTALLED_BY` in the program folder. Settings and data are per user, in the folders `aimeterix paths` prints (record 0019), and are created by `aimeterix init`, never by an installer: an installer runs as root and must not write into a user's home.

## Not done yet

- **Signing.** The `.pkg` is unsigned, so macOS says the developer cannot be verified (System Settings > Privacy & Security > Open Anyway). Signing and notarizing need an Apple Developer ID: `productsign` with a "Developer ID Installer" certificate, then `xcrun notarytool submit --wait` and `xcrun stapler staple`, with the certificate and an app-specific password as repository secrets.
- **Linux and Windows releases.** Linux packages build and were tried on Ubuntu (x86_64 and ARM64) but wait for Linux hardware metering; Windows needs its own backend and installer.
- **Hardware metering on Linux and Intel Macs** (record 0009): there the proxy, costs and dashboard work, energy does not.
