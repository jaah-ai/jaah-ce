#!/bin/sh
# install_clis.sh — install every provider CLI a community-edition node runs, as the CURRENT user, into ~/.local.
#
#   install_clis.sh <bundle-root>
#
# The single home of that list for the community edition: the node image (Dockerfile.node) and the native installer
# (jaah-ce-node) both run it. <bundle-root> is the node bundle (`apps/jaahd/runner_cli_pins.py` and the pins and
# locks it reads under `packaging/`). Each pinned arm goes through `runner_cli_pins.py`, the one reader of the pins:
# the tarball checked against its integrity, the dependency tree from the committed lock, never an unpinned latest.
# Claude Code installs with the vendor's own installer, as on a fleet node. No sudo, no apt.
# Needs on PATH: python3, npm, curl, tar, unzip. Antigravity is amd64-only on Linux (no linux arm64 build).
set -eu

bundle="${1:?usage: install_clis.sh <bundle-root>}"
pins="$bundle/apps/jaahd/runner_cli_pins.py"
[ -f "$pins" ] || { echo "install_clis: no $pins — is $bundle a node bundle?" >&2; exit 2; }
prefix="$HOME/.local"
mkdir -p "$prefix/bin"
export PATH="$prefix/bin:$PATH"

# Claude Code (also the DeepSeek CLI): the vendor installer, into ~/.local/bin.
if ! command -v claude >/dev/null 2>&1; then
  curl -fsSL https://claude.ai/install.sh | bash
fi

# The npm arms, each at its pin: Codex, Pi and its ACP agent, the ACP adapters, and OMX beside Codex.
python3 -I "$pins" install --prefix "$prefix" -- \
  @openai/codex pi-acp @earendil-works/pi-coding-agent \
  @agentclientprotocol/codex-acp @agentclientprotocol/claude-agent-acp oh-my-codex

# Antigravity: the pinned release tarball, sha256-checked (the `[agy]` pin; `runner_cli_pins.py agy-shell`).
case "$(uname -m)" in
  x86_64|amd64)
    AGY_VERSION=
    eval "$(python3 -I "$pins" agy-shell)"
    if [ -n "$AGY_VERSION" ]; then
      stage="$(mktemp -d)"
      curl -fsSL --proto =https --proto-redir =https -o "$stage/agy.tar.gz" \
        "$AGY_RELEASE/linux-x64/cli_linux_x64.tar.gz"
      printf '%s  %s\n' "$AGY_SHA256_AMD64" "$stage/agy.tar.gz" | sha256sum -c --quiet -
      tar -xzf "$stage/agy.tar.gz" -C "$stage" antigravity
      printf '%s  %s\n' "$AGY_BIN_SHA256_AMD64" "$stage/antigravity" | sha256sum -c --quiet -
      install -m 0755 "$stage/antigravity" "$prefix/bin/agy"
      rm -rf -- "$stage"
    fi
    # Its ACP server is ~3 GiB: a host short of space keeps Antigravity on the terminal runtime, as a fleet node does.
    python3 -I "$pins" agy-acp-install --home "$HOME" \
      || echo "install_clis: agy-acp-server not installed — Antigravity runs on the terminal runtime" >&2
    ;;
  *)
    echo "install_clis: $(uname -m): Antigravity ships no Linux build for this architecture — skipped" >&2
    ;;
esac
