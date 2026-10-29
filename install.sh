#!/bin/sh
# install.sh — install or upgrade Jaah Community Edition on Linux or macOS. Windows: install.ps1.
#
#   curl -fsSL https://raw.githubusercontent.com/jaah-ai/jaah-ce/main/install.sh | sh
#   sh install.sh --upgrade        # move an existing install to the latest release
#
# Checks Docker, fetches the latest release into ~/.jaah-ce (its compose.yml checked against the release's
# SHA256SUMS), starts the stack (compose.yml) and the node, and prints the console's address. On Linux the node runs
# natively (node/jaah-ce-node: a systemd user unit), which needs git for the checkout; on macOS, WSL, or Linux without
# git it runs as a container (the `node` profile).
# Nothing to edit: the installer writes the one setting the stack reads, JAAH_VERSION, to ~/.jaah-ce/.env.
set -eu

repo="${JAAH_CE_REPO:-jaah-ai/jaah-ce}"
# Tests point these at a local server; unset, they are GitHub.
release_base="${JAAH_CE_RELEASE_BASE:-https://github.com}"
api_base="${JAAH_CE_API_BASE:-https://api.github.com}"
dir="$HOME/.jaah-ce"
console="http://localhost:3050"
health="http://127.0.0.1:8000/api/health"
unit="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/jaah-ce-node.service"

say() { printf 'jaah-ce: %s\n' "$*"; }
die() { say "$*" >&2; exit 1; }

upgrade=
case "${1:-}" in
  "") ;;
  --upgrade) upgrade=1 ;;
  *) die "usage: install.sh [--upgrade]" ;;
esac

# ── Docker ────────────────────────────────────────────────────────────────────────────────────────────────────
command -v docker >/dev/null 2>&1 || die "Docker is not installed — get it from https://docs.docker.com/get-docker/ and run this again"
docker compose version >/dev/null 2>&1 || die "Docker Compose v2 is missing — install the docker compose plugin and run this again"
docker info >/dev/null 2>&1 || die "Docker is installed but not running (or not usable by $(id -un)) — start it and run this again"
command -v curl >/dev/null 2>&1 || die "curl is not installed"

# ── where the node runs: natively on Linux, as a container on macOS and WSL (which counts as Windows) ──────────
native=
if [ "$(uname -s)" = Linux ] && [ -z "${WSL_DISTRO_NAME:-}" ] && [ ! -e /proc/sys/fs/binfmt_misc/WSLInterop ]; then
  if command -v git >/dev/null 2>&1; then native=1; else say "git is not installed — the node will run as a container"; fi
fi

# ── the latest release ────────────────────────────────────────────────────────────────────────────────────────
# A GitHub Release, made only once every image of its tag is published (.github/workflows/release.yml).
tag="$(curl -fsSL "$api_base/repos/$repo/releases/latest" \
  | sed -n 's/.*"tag_name": *"\(v[0-9][0-9]*\.[0-9][0-9]*\.[0-9][0-9]*\)".*/\1/p' | head -n 1)"
[ -n "$tag" ] || die "could not find a release of $repo on GitHub"
version="${tag#v}"

if [ -n "$upgrade" ] && [ ! -f "$dir/compose.yml" ]; then die "no install at $dir — run install.sh without --upgrade"; fi

# ── the release's files, checked before anything in $dir changes ──────────────────────────────────────────────
# The release's compose.yml pins every image by its signed digest; SHA256SUMS covers every file fetched here.
tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
assets="$release_base/$repo/releases/download/$tag"
curl -fsSL -o "$tmp/SHA256SUMS" "$assets/SHA256SUMS" 2>/dev/null \
  || die "SHA256SUMS: missing from release $tag — refusing to install files it cannot check"
if command -v sha256sum >/dev/null 2>&1; then sha256() { sha256sum "$@"; }; else sha256() { shasum -a 256 "$@"; }; fi
for file in compose.yml; do
  curl -fsSL -o "$tmp/$file" "$assets/$file" 2>/dev/null || die "$file: could not download it from release $tag"
  grep -E "^[0-9a-f]{64} [ *]$file\$" "$tmp/SHA256SUMS" > "$tmp/$file.sum" \
    || die "$file: not listed in the SHA256SUMS of release $tag — not installing it"
  (cd "$tmp" && sha256 -c "$file.sum" >/dev/null 2>&1) \
    || die "$file: checksum mismatch against the SHA256SUMS of release $tag — not installing it"
done

if [ -d "$dir/.git" ] && command -v git >/dev/null 2>&1; then
  git -C "$dir" fetch --quiet --tags origin
  git -C "$dir" -c advice.detachedHead=false checkout --quiet -f --detach "refs/tags/$tag"
elif [ -n "$native" ] && [ ! -e "$dir" ]; then
  git clone --quiet "https://github.com/$repo.git" "$dir"
  git -C "$dir" -c advice.detachedHead=false checkout --quiet -f --detach "refs/tags/$tag"
else
  mkdir -p "$dir"
  native=
fi
# The native node's code is the tag's checkout over HTTPS, so git checks it against the tag's commit; the one file
# it does not cover is the rendered compose.yml, which lands verified over the tag's template here.
cp "$tmp/compose.yml" "$dir/compose.yml.new"
mv "$dir/compose.yml.new" "$dir/compose.yml"
printf 'JAAH_VERSION=%s\n' "$version" > "$dir/.env"

compose() { docker compose --project-directory "$dir" -f "$dir/compose.yml" "$@" </dev/null; }

# ── the control plane ─────────────────────────────────────────────────────────────────────────────────────────
say "starting Jaah Community Edition $version"
profiles=
# An upgrade keeps a node container that already runs.
if [ -n "$upgrade" ] && [ -n "$(compose --profile node ps -a -q node 2>/dev/null)" ]; then profiles="--profile node"; fi
# shellcheck disable=SC2086 # $profiles is empty or two words
compose $profiles pull --quiet
# shellcheck disable=SC2086
compose $profiles up -d
i=0
until curl -fsS -o /dev/null "$health" 2>/dev/null; do
  i=$((i + 1))
  [ "$i" -le 180 ] || die "the control plane did not come up in 3 minutes — see: docker compose -f $dir/compose.yml logs api"
  sleep 1
done

# ── the node ──────────────────────────────────────────────────────────────────────────────────────────────────
if [ -n "$upgrade" ]; then
  if [ -f "$unit" ] && [ -d "$dir/.git" ]; then "$dir/node/jaah-ce-node" install --upgrade "$tag" </dev/null; fi
elif [ -n "$native" ]; then
  # Never the script's own stdin: under `curl | sh` that is the rest of this file.
  "$dir/node/jaah-ce-node" install </dev/null
else
  compose --profile node up -d node
fi

say "ready — open $console"
