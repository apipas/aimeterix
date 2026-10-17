# Releasing

A release is one tag. GitHub Actions (`.github/workflows/release.yml`) builds the packages on macOS, installs each on a clean Mac of its processor (Apple Silicon and Intel), and only then publishes the GitHub Release. Releases are macOS only for now; `packaging/build.py` also builds the Linux `.deb` and `.tar.gz`, ready for when Linux support ships.

## Steps

1. Update `main` first, then make a branch `release/<version>` (e.g. `release/0.1.3`) from it. Never reuse or merge an old branch: it can bring back commits that were already replaced.
2. Set the version in `pyproject.toml`, `src/ai_meterix/__init__.py` and `uv.lock` (`uv lock`), and in the README's Versions list. Add its section at the top of `CHANGELOG.md` (`## 0.1.3 (preview)`); the release notes are taken from that section only.
3. Website: update the version badge on `site/index.html` and the entry on `site/releases.html`.
4. If the database schema changed, follow [Upgrades and the database](#upgrades-and-the-database) below.
5. `make check` passes; commit and push the branch. Every push runs CI and builds and installs the packages on clean Macs, without publishing anything.
6. When both are green, merge the branch into `main`. Tag the merge commit on `main` and push the tag (`git tag v0.1.3 && git push origin v0.1.3`, or Create Tag in GitHub Desktop). Only the tag publishes the GitHub Release; a tag that does not match the package version fails the first job. Never create the release by hand on GitHub: the workflow could not attach its files to it.
7. Delete the merged branch.

`uv.lock` is committed on purpose: CI and every developer install the same dependency versions.

Every release is published as the latest release, so the one-line install (`releases/latest/download/install.sh`) finds it. A `0.x` version is still a preview.

## Upgrades and the database

Users upgrade by running the one-line install again, or the new `.pkg`. Their settings and data stay where they are (`aimeterix paths`); only the program is replaced. The first `aimeterix start` of the new version upgrades the database schema by itself (`migrate()` in `src/ai_meterix/storage/sqlstore.py`).

Rules for a release that changes the schema:

1. **Append, never edit.** Add the change as a new entry at the end of `MIGRATIONS`. Never change or remove an entry that has been released: users' databases already ran it.
2. **Forward only and safe for existing rows.** New columns get a default or allow NULL; nothing is dropped or renamed in the same release that stops using it.
3. **Test the upgrade.** Create a database with the previous release, then start the new one on it: the schema reaches the new version, `aimeterix health` is healthy, and old usage and costs read the same.
4. **Back up first.** Before migrating, the user's database must be copied next to the original (for SQLite: `meter.db` to `meter.db.v<old>-<date>.bak`). AI Meterix does not make this copy by itself yet: build it before the first release that changes the schema. Say in `CHANGELOG.md` that the release upgrades the database and where the backup is.
5. **Major versions** (`1.x` to `2.x`) may also change settings or file locations: describe each step the user must take in the CHANGELOG and in the release notes.
6. An older AI Meterix refuses a database newer than itself (it says so and stops), so downgrading means restoring the backup.

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
| `.pkg` | `/usr/local/ai-meterix` | `/usr/local/bin/aimeterix` | `sudo rm -rf /usr/local/ai-meterix /usr/local/bin/aimeterix && sudo pkgutil --forget com.apipas.aimeterix` |
| `.deb` | `/opt/ai-meterix` | `/usr/bin/aimeterix` | `sudo apt remove ai-meterix` |
| `install.sh` | `~/.local/lib/ai-meterix` | `~/.local/bin/aimeterix` | `rm -rf ~/.local/lib/ai-meterix ~/.local/bin/aimeterix`, and the PATH line it added to the shell profile |

`aimeterix uninstall` prints the right line; it reads `INSTALLED_BY` in the program folder. Settings and data are per user, in the folders `aimeterix paths` prints (record 0019), and are created by the first `aimeterix start` (or `aimeterix init`) as that user, never by the `.pkg`: an installer runs as root and must not write into a user's home.

## Not done yet

- **Signing.** The `.pkg` is unsigned, so macOS says the developer cannot be verified (System Settings > Privacy & Security > Open Anyway). Signing and notarizing need an Apple Developer ID: `productsign` with a "Developer ID Installer" certificate, then `xcrun notarytool submit --wait` and `xcrun stapler staple`, with the certificate and an app-specific password as repository secrets.
- **Linux and Windows releases.** Linux packages build and were tried on Ubuntu (x86_64 and ARM64) but wait for Linux hardware metering; Windows needs its own backend and installer.
- **Hardware metering on Linux and Intel Macs** (record 0009): there the proxy, costs and dashboard work, energy does not.
