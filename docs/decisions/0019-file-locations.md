# 0019: Where AI Meterix keeps its files

- Status: accepted
- Date: 2026-10-16
- Plan ref: Phase 6 (release); amends 0008, 0016 and 0017

## Context
Before 0.1.0 the config file and its `.env` were read from the folder a command was run in (`./aimeterix.yaml`), while the database, logs and PID files lived in `~/.aimeterix`. Running a command from another folder silently used other settings, and `init` left a config file in whatever folder the user happened to be in. The owner asked for the standard locations of each system instead.

## Decision
1. **One module decides every path** (`core/paths.py`), read at call time, so every command finds the same files from any folder.
2. **Each system's own conventions, no admin rights:**

   | | macOS | Linux and others |
   |---|---|---|
   | config (`aimeterix.yaml`, `.env`) | `~/Library/Application Support/aimeterix` | `$XDG_CONFIG_HOME/aimeterix` (`~/.config`) |
   | data (`meter.db`, buffers, agent status) | `~/Library/Application Support/aimeterix` | `$XDG_DATA_HOME/aimeterix` (`~/.local/share`) |
   | process IDs | `.../aimeterix/run` | `$XDG_STATE_HOME/aimeterix/run` (`~/.local/state`) |
   | logs | `~/Library/Logs/aimeterix` | `$XDG_STATE_HOME/aimeterix/logs` |

   On macOS, Console.app shows the logs and Time Machine backs up the data. The folder name is lowercase with no space, so it is easy to type.
3. **`AIMETERIX_HOME`** puts everything in one folder (config and data at its top, `run/` and `logs/` inside). `--config` and `AIMETERIX_CONFIG` still name another config file; its `.env` is read next to it.
4. **`aimeterix paths`** prints every location and says which ones do not exist yet.
5. **`aimeterix uninstall --purge`** lists the folders, asks, then deletes them. It refuses a folder that holds the home folder (a mistaken `AIMETERIX_HOME=~`).
6. **No automatic move** of an old `~/.aimeterix`. `init`, `paths` and `uninstall` name it, and the user deletes it.

## Consequences
- The tests give every test its own home folder, so none can read or write the real one.
- A sqlite URL with a relative path in the config file is still relative to the current folder; the default is absolute.
- Revisit if a system-wide install (a launch daemon or a Linux service) is added in MVP-2: that would use `/Library` or `/var/lib` and need admin rights.
