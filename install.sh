#!/bin/sh
# Install the latest GitHub Release executable without requiring Python or uv.
set -eu

REPOSITORY="mmcs-work/git-explain-tui"
INSTALL_DIR="${GIT_EXPLAIN_TUI_INSTALL_DIR:-$HOME/.local/bin}"
VERSION="${GIT_EXPLAIN_TUI_VERSION:-latest}"

case "$(uname -s)" in
  Darwin) platform="macos" ;;
  Linux) platform="linux" ;;
  *) echo "git-explain-tui: only macOS and Linux are supported." >&2; exit 1 ;;
esac

case "$(uname -m)" in
  x86_64|amd64) architecture="x86_64" ;;
  arm64|aarch64)
    if [ "$platform" = "macos" ]; then
      architecture="arm64"
    else
      echo "git-explain-tui: Linux ARM releases are not available yet." >&2
      exit 1
    fi
    ;;
  *) echo "git-explain-tui: unsupported CPU architecture: $(uname -m)" >&2; exit 1 ;;
esac

asset="git-explain-tui-${platform}-${architecture}"
if [ "$VERSION" = "latest" ]; then
  url="https://github.com/$REPOSITORY/releases/latest/download/$asset"
else
  url="https://github.com/$REPOSITORY/releases/download/$VERSION/$asset"
fi

mkdir -p "$INSTALL_DIR"
target="$INSTALL_DIR/git-explain-tui"
temporary="$target.download"
checksums="$target.checksums"
trap 'rm -f "$temporary" "$checksums"' 0 HUP INT TERM

if command -v curl >/dev/null 2>&1; then
  curl -fL --retry 3 "$url" -o "$temporary"
  curl -fL --retry 3 "${url%/$asset}/SHA256SUMS" -o "$checksums"
elif command -v wget >/dev/null 2>&1; then
  wget -O "$temporary" "$url"
  wget -O "$checksums" "${url%/$asset}/SHA256SUMS"
else
  echo "git-explain-tui: install curl or wget, then try again." >&2
  exit 1
fi

expected="$(awk -v name="$asset" '$2 == name { print $1 }' "$checksums")"
if [ -z "$expected" ]; then
  echo "git-explain-tui: release checksum for $asset is missing." >&2
  exit 1
fi

if command -v shasum >/dev/null 2>&1; then
  actual="$(shasum -a 256 "$temporary" | awk '{ print $1 }')"
else
  actual="$(sha256sum "$temporary" | awk '{ print $1 }')"
fi
if [ "$actual" != "$expected" ]; then
  echo "git-explain-tui: downloaded file failed checksum verification." >&2
  exit 1
fi

chmod +x "$temporary"
mv "$temporary" "$target"
echo "Installed git-explain-tui to $target"

case ":$PATH:" in
  *":$INSTALL_DIR:"*) echo "Run: git-explain-tui" ;;
  *)
    echo "Add this directory to PATH, then reopen your terminal:"
    echo "  export PATH=\"$INSTALL_DIR:\$PATH\""
    echo "Or run it now: $target"
    ;;
esac
