#!/bin/sh
# Install AI Meterix for this user, without admin rights.
# Releases have macOS packages; Linux ones are coming (packaging/build.py builds them).
#
# From an unpacked release:   ./install.sh
# From the internet:          curl -fsSL <release>/install.sh | sh
#
# It downloads the release for this Mac's processor from GitHub, checks its
# SHA-256, installs it, adds the command to PATH and starts AI Meterix.
#
# AIMETERIX_VERSION picks a release (default: the latest),
# AIMETERIX_PREFIX the place (default: ~/.local; the program goes to
# <prefix>/lib/ai-meterix and the command to <prefix>/bin/aimeterix).
# AIMETERIX_NO_START=1 only installs; AIMETERIX_NO_MODIFY_PATH=1 leaves the
# shell profile alone.
set -eu

REPO="apipas-com/aimeterix"
PREFIX="${AIMETERIX_PREFIX:-$HOME/.local}"
VERSION="${AIMETERIX_VERSION:-latest}"

fail() { echo "install.sh: $*" >&2; exit 1; }

case $(uname -s) in
  Darwin) os=macos ;;
  Linux) os=linux ;;
  *) fail "AI Meterix runs on macOS and Linux, not $(uname -s)" ;;
esac
case $(uname -m) in
  x86_64 | amd64) cpu=x86_64 ;;
  arm64 | aarch64) cpu=arm64 ;;
  *) fail "no build for the $(uname -m) processor" ;;
esac

here=$(cd "$(dirname "$0")" && pwd)
if [ -x "$here/python/bin/python3" ] && [ -x "$here/bin/aimeterix" ]; then
  src=$here
else
  [ "$os" = macos ] || fail "Linux packages are coming soon; for now: pip install ai-meterix"
  command -v curl > /dev/null || fail "curl is needed to download AI Meterix"
  if [ "$VERSION" = latest ]; then
    base="https://github.com/$REPO/releases/latest/download"
    VERSION=$(curl -fsSL "$base/VERSION") || fail "cannot find the latest release"
  else
    base="https://github.com/$REPO/releases/download/v$VERSION"
  fi
  name="ai-meterix-$VERSION-$os-$cpu"
  tmp=$(mktemp -d)
  trap 'rm -rf "$tmp"' EXIT
  echo "Downloading $name.tar.gz"
  curl -fsSL "$base/$name.tar.gz" -o "$tmp/$name.tar.gz" || fail "cannot download $name.tar.gz"
  curl -fsSL "$base/SHA256SUMS" -o "$tmp/SHA256SUMS" || fail "cannot download SHA256SUMS"
  expected=$(grep " $name.tar.gz\$" "$tmp/SHA256SUMS" | cut -d' ' -f1)
  if command -v sha256sum > /dev/null; then
    actual=$(sha256sum "$tmp/$name.tar.gz" | cut -d' ' -f1)
  else
    actual=$(shasum -a 256 "$tmp/$name.tar.gz" | cut -d' ' -f1)
  fi
  [ -n "$expected" ] && [ "$expected" = "$actual" ] || fail "checksum mismatch for $name.tar.gz"
  tar -xzf "$tmp/$name.tar.gz" -C "$tmp"
  src="$tmp/$name"
fi

# A package built for another system or processor is refused here, not at run time.
"$src/python/bin/python3" -c "import sys" 2> /dev/null ||
  fail "this package is not built for $os-$cpu; download ai-meterix-<version>-$os-$cpu"

dest="$PREFIX/lib/ai-meterix"
mkdir -p "$PREFIX/lib" "$PREFIX/bin"
# An upgrade: stop the running copy first, so the new one is what starts.
if [ -x "$dest/bin/aimeterix" ]; then
  "$dest/bin/aimeterix" stop > /dev/null 2>&1 || true
fi
rm -rf "$dest.new"
cp -R "$src" "$dest.new"
rm -f "$dest.new/install.sh"
echo script > "$dest.new/INSTALLED_BY"
rm -rf "$dest"
mv "$dest.new" "$dest"
ln -sf "$dest/bin/aimeterix" "$PREFIX/bin/aimeterix"

echo "Installed $("$dest/bin/aimeterix" --version) in $dest"

# The command's folder on PATH, for new terminals, like other installers do.
case ":$PATH:" in
  *":$PREFIX/bin:"*) on_path=yes ;;
  *) on_path=no ;;
esac
if [ "$on_path" = no ] && [ "${AIMETERIX_NO_MODIFY_PATH:-}" != 1 ]; then
  case $(basename "${SHELL:-sh}") in
    zsh) profile="$HOME/.zshrc" ;;
    bash) if [ "$os" = macos ]; then profile="$HOME/.bash_profile"; else profile="$HOME/.bashrc"; fi ;;
    *) profile="$HOME/.profile" ;;
  esac
  line="export PATH=\"$PREFIX/bin:\$PATH\"  # AI Meterix"
  if ! grep -qsF "$line" "$profile"; then
    printf '\n%s\n' "$line" >> "$profile"
    echo "Added $PREFIX/bin to PATH in $profile (new terminals have it)."
  fi
elif [ "$on_path" = no ]; then
  echo "Add $PREFIX/bin to your PATH:  export PATH=\"$PREFIX/bin:\$PATH\""
fi

if [ "${AIMETERIX_NO_START:-}" != 1 ]; then
  echo
  "$dest/bin/aimeterix" start ||
    echo "Something did not start; aimeterix health and aimeterix doctor say why."
fi

cat << 'EOF'

Commands (in a new terminal):
  aimeterix start      start again, e.g. after a restart of the Mac
  aimeterix health     check that everything is measuring
  aimeterix stop       stop everything
  aimeterix --help     every command

Online      https://github.com/apipas-com/aimeterix
Remove      aimeterix uninstall --purge
EOF
