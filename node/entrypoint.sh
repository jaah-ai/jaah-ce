#!/bin/sh
# entrypoint.sh — start the community edition's node in its container (Dockerfile.node). Starts as root, then drops to
# the two uids a fleet node runs (`apps/jaahd/session_user.py`): the session server as `$JAAH_SESSION_USER`, the
# daemon as `$JAAH_DAEMON_USER`.
#
# The node's identity and control plane come from `node.env` (written by the control plane's bootstrap into the
# identity volume), the CLI off-switches from `cli_env`. The identity is copied to a file only the daemon can read
# and never reaches the session server's environment: an agent session cannot read `NODE_TOKEN`.
# The runtime tree mirrors the fleet daemon unit's ExecStartPre lines under `$JAAH_RUN_ROOT`; the session server is
# the fleet runner unit's loop, restarted here as that unit's `Restart=always` would.
set -eu

die() { echo "jaah-ce node: $*" >&2; exit 1; }
daemon="${JAAH_DAEMON_USER:?the image names the daemon user}"
runner="${JAAH_SESSION_USER:?the image names the session user}"
[ "$(id -u)" = 0 ] || die "the entrypoint must start as root (it drops to $daemon and $runner)"
# Every command root runs after a drop is named by its absolute path, and each PATH is set only after the drop: a
# PATH naming a session-writable directory would let a planted `setpriv` run as root.
drop() { uid="$1" gid="$2"; shift 2; /usr/bin/setpriv --reuid="$uid" --regid="$gid" --init-groups --inh-caps=-all "$@"; }
private=/var/lib/jaahd/node.env
bundle=/usr/lib/jaahd/apps/jaahd
# The daemon tree runs under the venv the image built from its hash lock (Dockerfile.node, the fleet package's
# jaahd-venv): the only interpreter on this node that holds its dependencies. Used for every process below, the
# stdlib-only ones too, so one interpreter runs the tree.
python=/usr/lib/jaahd/venv/bin/python3

# The CLI telemetry off-switches, into both processes' environments; every agent window gets them from the daemon
# (`apps/jaahd/ce_caps.py`, `window_env`).
set -a
. "$JAAH_CLI_ENV_FILE"
if [ -r "$JAAH_NODE_ENV_FILE" ]; then
  . "$JAAH_NODE_ENV_FILE"
fi
set +a
[ -n "${NODE_TOKEN:-}" ] || die "no node identity — $JAAH_NODE_ENV_FILE is missing; start the control plane first"

# The daemon's own copy, laid out as a fleet node's (`apps/jaahd/secret_modes.py`, `node_env_finding`: root's, 0640,
# a group the session user does not hold). `classify()` reads a present node.env as a provisioned node; identity
# passed as env alone still needs the file. The pre-split default lived in the session's volume: remove it.
if [ -s "$JAAH_NODE_ENV_FILE" ] && [ "$JAAH_NODE_ENV_FILE" != "$private" ]; then
  install -o root -g "$daemon" -m 0640 "$JAAH_NODE_ENV_FILE" "$private"
elif [ ! -e "$private" ]; then
  install -o root -g "$daemon" -m 0640 /dev/null "$private"
fi
rm -f -- "$JAAH_HOME/node.env"

# The runtime tree, fresh each start (no server outlives the container). Root-owned at the top, so a session cannot
# plant a path the daemon later trusts.
run="$JAAH_RUN_ROOT"
case "$run" in /run/?*) ;; *) die "JAAH_RUN_ROOT must be a directory under /run, not '$run'" ;; esac
rm -rf -- "$run"
install -d -o root -g root -m 0755 "$run" "$run/exec"
install -d -o "$daemon" -g "$runner" -m 2770 "$TMUX_TMPDIR" "$run/exec/res"
install -d -o "$daemon" -g "$runner" -m 2750 "$run/requests" "$run/exec/req" "$(dirname "$JAAH_CRED_SOCKET")"
install -d -o "$daemon" -g "$daemon" -m 0750 "$run/runners"

# The node home is the session user's, setgid and group-writable, so the daemon (group `$runner`) shares it. A volume
# from a one-uid release was written under umask 022: widen it to the group once — as the tree's owner, so root never
# walks a path a session controls.
install -d -o "$runner" -g "$runner" -m 2770 "$JAAH_HOME"
drop "$runner" "$runner" /usr/bin/env -u NODE_TOKEN /bin/sh -c '
  [ -e "$1/.jaah-uid-split" ] && exit 0
  chmod -R g+rwX "$1" && find "$1" -type d -exec chmod g+s {} + && : > "$1/.jaah-uid-split"' _ "$JAAH_HOME" \
  || echo "jaah-ce node: could not widen $JAAH_HOME to group $runner; the daemon may be refused files in it" >&2

session="$("$python" -I -c "import sys; sys.path.insert(0, '$bundle'); import env_helpers; print(env_helpers.default_tmux_session())")"

# The session server: every agent pane is forked by it, so every session runs as `$runner`. Its environment carries
# no `NODE_TOKEN`; its PATH is the fleet runner unit's with the bundle's root-owned `bin/` (the runner-facing clients)
# ahead, and without `~/.local/bin` (each window adds it, `fleet_launch.build_launch_sh`).
path=/usr/lib/jaahd/bin:/usr/local/bin:/usr/local/sbin:/usr/bin:/usr/sbin:/bin:/sbin
(
  while :; do
    drop "$runner" "$runner" --no-new-privs /usr/bin/env -u NODE_TOKEN HOME="/home/$runner" USER="$runner" LOGNAME="$runner" \
      SHELL=/bin/bash PATH="$path" JAAH_NODE_ENV_FILE="$private" \
      /bin/sh -c 'umask 0002; cd "$HOME"; exec "$3" "$1/fleet_tmux_server.py" --loop "$2"' _ "$bundle" "$session" "$python" \
      || true
    echo "jaah-ce node: the session server exited; restarting it in 5s" >&2
    sleep 5
  done
) &

exec /usr/bin/setpriv --reuid="$daemon" --regid="$runner" --init-groups --inh-caps=-all \
  /usr/bin/env HOME=/var/lib/jaahd USER="$daemon" LOGNAME="$daemon" PATH="$path" \
  JAAH_NODE_ENV_FILE="$private" \
  /bin/sh -c 'umask 0002; cd "$HOME"; tree="$1"; python="$2"; shift 2; exec "$python" "$tree/main.py" "$@"' _ "$bundle" "$python" "$@"
