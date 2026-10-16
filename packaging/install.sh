#!/bin/sh
# Install AI Meterix for this user, without admin rights.
# Releases have macOS packages; Linux ones are coming (packaging/build.py builds them).
#
# From an unpacked release:   ./install.sh
# From the internet:          curl -fsSL <release>/install.sh | sh
#
# AIMETERIX_VERSION picks a release (default: the latest),
# AIMETERIX_PREFIX the place (default: ~/.local; the program goes to
# <prefix>/lib/ai-meterix and the command to <prefix>/bin/aimeterix).
set -eu

REPO="mabuthraa/gocompute"
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
rm -rf "$dest.new"
cp -R "$src" "$dest.new"
rm -f "$dest.new/install.sh"
echo script > "$dest.new/INSTALLED_BY"
rm -rf "$dest"
mv "$dest.new" "$dest"
ln -sf "$dest/bin/aimeterix" "$PREFIX/bin/aimeterix"

echo "Installed $("$dest/bin/aimeterix" --version) in $dest"
case ":$PATH:" in
  *":$PREFIX/bin:"*) ;;
  *) echo "Add $PREFIX/bin to your PATH, e.g. in ~/.zshrc or ~/.bashrc:"
     echo "  export PATH=\"$PREFIX/bin:\$PATH\"" ;;
esac
cat << 'EOF'

Next:
  1. aimeterix init     shows the default settings, lets you change any, creates the database
  2. aimeterix start    runs the agent, proxy and dashboard in the background
  3. aimeterix health   checks that everything is measuring

Dashboard   http://127.0.0.1:8788/        log in as user (or admin); the first password is the name
Manual      http://127.0.0.1:8788/#help   or: aimeterix --help, aimeterix paths
Online      https://github.com/mabuthraa/gocompute
Remove      aimeterix uninstall --purge
EOF
