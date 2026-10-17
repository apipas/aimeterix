"""Build the install packages of a release (docs/RELEASING.md).

Each package carries its own Python, so it runs whatever Python the machine
has, and is built per system and CPU:

    linux-x86_64, linux-arm64   ->  .tar.gz and .deb
    macos-arm64,  macos-x86_64  ->  .tar.gz, and .pkg when run on macOS

Usage, after ``uv build``:

    python packaging/build.py dist/ai_meterix-0.1.0-py3-none-any.whl --target all
    python packaging/build.py dist/ai_meterix-*.whl --target macos-arm64 --pkg

Needs ``uv`` (it downloads the Python builds and checks their hashes) and, for a
.deb, ``dpkg-deb``; a .pkg needs macOS (``pkgbuild``, ``productbuild``).
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path

PYTHON = "3.12.13"
HERE = Path(__file__).resolve().parent
APP_ID = "io.github.mabuthraa.aimeterix"
HOMEPAGE = "https://github.com/mabuthraa/gocompute"
# Shipped in the program folder as the offline manual (macOS .pkg).
DOCS = ("README.md", "CHANGELOG.md", "SECURITY.md", "LICENSE", "docs/FAQ.md")
# Never needed at run time; leaving them out saves about a third of the size.
UNUSED = (
    "include",
    "share",
    "lib/tcl*",
    "lib/tk*",
    "lib/itcl*",
    "lib/thread*",
    "lib/libtcl*",
    "lib/libtk*",
    "lib/python3.*/test",
    "lib/python3.*/idlelib",
    "lib/python3.*/tkinter",
    "lib/python3.*/turtledemo",
    "lib/python3.*/ensurepip",
    "lib/python3.*/lib2to3",
    "lib/python3.*/lib-dynload/_tkinter*",
    "lib/python3.*/config-*",
    "lib/python3.*/site-packages/pip*",
)


@dataclass(frozen=True)
class Target:
    name: str  # in file names: <os>-<cpu>
    python: str  # uv's key for the Python build
    platform: str  # uv's --python-platform for the dependencies
    deb_arch: str = ""

    @property
    def system(self) -> str:
        return self.name.split("-", 1)[0]

    @property
    def cpu(self) -> str:
        return self.name.split("-", 1)[1]


TARGETS = {
    t.name: t
    for t in (
        Target("linux-x86_64", "linux-x86_64-gnu", "x86_64-manylinux_2_28", "amd64"),
        Target("linux-arm64", "linux-aarch64-gnu", "aarch64-manylinux_2_28", "arm64"),
        Target("macos-arm64", "macos-aarch64-none", "aarch64-apple-darwin"),
        Target("macos-x86_64", "macos-x86_64-none", "x86_64-apple-darwin"),
    )
}

LAUNCHER = """#!/bin/sh
# AI Meterix: runs the Python bundled next to this file, never the system's.
p=$0
while [ -h "$p" ]; do
  l=$(readlink "$p")
  case $l in /*) p=$l ;; *) p=$(dirname "$p")/$l ;; esac
done
root=$(cd "$(dirname "$p")/.." && pwd)
# The package ships compiled files; never write new ones into it.
PYTHONDONTWRITEBYTECODE=1 exec "$root/python/bin/python3" -I -m ai_meterix "$@"
"""


def defaults_text(version: str, where: str) -> str:
    """What the package installs and the settings `aimeterix init` starts from."""
    return f"""AI Meterix {version}
Measures what local and cloud AI costs on this machine: energy, tokens and money.

Installed in   {where}
Command        aimeterix   (aimeterix --help lists everything)

Default settings; the first `aimeterix start` uses them, `aimeterix init` changes them first:
  Proxy          http://127.0.0.1:8787   apps send their AI calls here
  Dashboard      http://127.0.0.1:8788   accounts user and admin
  Database       SQLite in the data folder
  Samples        hardware every 10 seconds; raw data kept 30 days, hourly totals 365
  Local server   none; set it to your llama.cpp or Ollama address if you have one
Everything listens on this machine only. Prompts, answers and API keys are never stored.

Files (`aimeterix paths` shows them):
  macOS   ~/Library/Application Support/aimeterix   config and database
          ~/Library/Logs/aimeterix                  logs
  Linux   ~/.config/aimeterix, ~/.local/share/aimeterix, ~/.local/state/aimeterix

Next:
  aimeterix start      run the agent, proxy and dashboard; the first start sets up the defaults
  aimeterix health     check everything is measuring

Hardware energy readings need Apple Silicon for now; on Intel Macs and Linux the
proxy and dashboard work, and token costs are counted.
{HOMEPAGE}
"""


def version_of(wheel: Path) -> str:
    match = re.match(r"ai_meterix-([^-]+)-", wheel.name)
    if not match:
        sys.exit(f"not an ai-meterix wheel: {wheel}")
    return match.group(1)


def run(*command: str | Path, cwd: Path | None = None) -> None:
    subprocess.run([str(c) for c in command], check=True, cwd=cwd)


def bundle(target: Target, wheel: Path, work: Path) -> Path:
    """``<work>/ai-meterix``: bin/aimeterix, python/ with AI Meterix inside."""
    root = work / "ai-meterix"
    pythons = work / "pythons"
    run(
        "uv", "python", "install", f"cpython-{PYTHON}-{target.python}",
        "--install-dir", pythons, "--no-bin",
    )  # fmt: skip
    source = pythons / f"cpython-{PYTHON}-{target.python}"
    shutil.copytree(source, root / "python", symlinks=True)
    for pattern in UNUSED:
        for path in (root / "python").glob(pattern):
            shutil.rmtree(path) if path.is_dir() else path.unlink()
    minor = ".".join(PYTHON.split(".")[:2])
    site = root / "python" / "lib" / f"python{minor}" / "site-packages"
    run(
        "uv", "pip", "install", wheel, "--target", site, "--python-version", minor,
        "--python-platform", target.platform, "--only-binary", ":all:", "--no-cache",
    )  # fmt: skip
    # Compiled files are the same on every CPU, so this machine's Python makes them.
    run(
        "uv", "run", "--no-project", "--python", PYTHON, "python", "-m", "compileall",
        "-q", "-j", "0", root / "python" / "lib" / f"python{minor}",
    )  # fmt: skip
    (root / "bin").mkdir()
    launcher = root / "bin" / "aimeterix"
    launcher.write_text(LAUNCHER)
    launcher.chmod(0o755)
    shutil.copy(HERE / "install.sh", root / "install.sh")
    (root / "install.sh").chmod(0o755)
    return root


def tarball(root: Path, out: Path, name: str, version: str) -> Path:
    (root / "README.txt").write_text(
        defaults_text(version, "where install.sh puts it (default ~/.local/lib/ai-meterix)")
    )
    path = out / f"{name}.tar.gz"
    with tarfile.open(path, "w:gz") as tar:
        tar.add(root, arcname=name)
    (root / "README.txt").unlink()
    return path


def deb(root: Path, target: Target, out: Path, version: str, work: Path) -> Path:
    tree = work / "deb"
    shutil.copytree(root, tree / "opt" / "ai-meterix", symlinks=True)
    (tree / "opt" / "ai-meterix" / "install.sh").unlink()
    (tree / "opt" / "ai-meterix" / "INSTALLED_BY").write_text("deb\n")
    (tree / "opt" / "ai-meterix" / "README.txt").write_text(
        defaults_text(version, "/opt/ai-meterix")
    )
    (tree / "usr" / "bin").mkdir(parents=True)
    (tree / "usr" / "bin" / "aimeterix").symlink_to("/opt/ai-meterix/bin/aimeterix")
    control = tree / "DEBIAN"
    control.mkdir()
    size_kb = sum(f.lstat().st_size for f in tree.rglob("*") if not f.is_dir()) // 1024
    (control / "control").write_text(
        f"Package: ai-meterix\n"
        f"Version: {version}\n"
        f"Architecture: {target.deb_arch}\n"
        f"Maintainer: Maher Abuthraa <noreply@github.com>\n"
        f"Installed-Size: {size_kb}\n"
        f"Section: utils\n"
        f"Priority: optional\n"
        f"Homepage: {HOMEPAGE}\n"
        f"Description: Measure what local and cloud AI costs on this machine\n"
        f" Energy, tokens and money per app and model, with a local dashboard.\n"
        f" Includes its own Python. Run `aimeterix init` to review the settings.\n"
    )
    postinst = control / "postinst"
    postinst.write_text("#!/bin/sh\nset -e\ncat /opt/ai-meterix/README.txt\n")
    postinst.chmod(0o755)
    path = out / f"ai-meterix_{version}_{target.deb_arch}.deb"
    run("dpkg-deb", "--root-owner-group", "--build", tree, path)
    return path


def pkg(root: Path, target: Target, out: Path, version: str, work: Path) -> Path:
    """A macOS installer into /usr/local/ai-meterix; refuses the wrong CPU."""
    payload = work / "payload"
    shutil.copytree(root, payload, symlinks=True)
    (payload / "install.sh").unlink()
    (payload / "INSTALLED_BY").write_text("pkg\n")
    (payload / "README.txt").write_text(defaults_text(version, "/usr/local/ai-meterix"))
    docs = payload / "docs"
    docs.mkdir()
    for name in DOCS:
        shutil.copy(HERE.parent / name, docs / Path(name).name)
    scripts = work / "scripts"
    shutil.copytree(HERE / "macos" / "scripts", scripts)
    resources = work / "resources"
    resources.mkdir()
    for page in ("welcome.html", "conclusion.html"):
        text = (HERE / "macos" / page).read_text()
        (resources / page).write_text(
            text.replace("@VERSION@", version).replace("@HOMEPAGE@", HOMEPAGE)
        )
    shutil.copy(HERE.parent / "LICENSE", resources / "license.txt")
    component = work / "ai-meterix-component.pkg"
    run(
        "pkgbuild", "--root", payload, "--install-location", "/usr/local/ai-meterix",
        "--scripts", scripts, "--identifier", APP_ID, "--version", version, component,
    )  # fmt: skip
    arch = "arm64" if target.cpu == "arm64" else "x86_64"
    distribution = work / "distribution.xml"
    distribution.write_text(
        (HERE / "macos" / "distribution.xml")
        .read_text()
        .replace("@VERSION@", version)
        .replace("@ARCH@", arch)
        .replace("@APP_ID@", APP_ID)
    )
    path = out / f"ai-meterix-{version}-{target.name}.pkg"
    run(
        "productbuild", "--distribution", distribution, "--resources", resources,
        "--package-path", work, path,
    )  # fmt: skip
    return path


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("wheel", type=Path)
    parser.add_argument("--target", action="append", choices=[*TARGETS, "all"], required=True)
    parser.add_argument("--out", type=Path, default=Path("dist"))
    parser.add_argument("--pkg", action="store_true", help="also build the macOS .pkg")
    parser.add_argument("--no-deb", action="store_true", help="skip the Linux .deb")
    args = parser.parse_args(argv)
    wheel = args.wheel.resolve()
    version = version_of(wheel)
    names = list(TARGETS) if "all" in args.target else args.target
    args.out.mkdir(parents=True, exist_ok=True)
    for name in names:
        target = TARGETS[name]
        with tempfile.TemporaryDirectory() as tmp:
            work = Path(tmp)
            root = bundle(target, wheel, work)
            built = [tarball(root, args.out, f"ai-meterix-{version}-{name}", version)]
            if target.system == "linux" and not args.no_deb:
                built.append(deb(root, target, args.out, version, work))
            if target.system == "macos" and args.pkg:
                built.append(pkg(root, target, args.out, version, work))
        for path in built:
            print(f"built {path} ({path.stat().st_size / 1e6:.1f} MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
