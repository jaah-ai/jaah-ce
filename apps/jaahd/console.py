"""Console PTY bridge — one dedicated WebSocket per viewer.

One PTY per (project, number, kind), any number of viewers. Resolve-or-create on first connect, a
fail-closed token gate, and DETACH-NEVER-KILL on disconnect: a viewer leaving closes the PTY master fd;
the tmux session and its process are never signalled. The PTY is this daemon's child, but everything
inside the session runs as user `jaah` (`session_user`), so every tmux call below names its socket with
`-S` — tmux's default label resolves per-euid — and this daemon never widens its own socket.

`console_open` on the node bus is a rendezvous; the node then dials the CP's console relay
(`CONSOLE_STREAM_PATH`, or the frame's `stream_url`) with the single-use ticket, and possession of that
socket IS the viewer's authorization. Teardown is its close; `_CLOSE_CODE_*` (matching
`apps/api/app/console_relay/tickets.py`) says why.
"""
from __future__ import annotations

import asyncio
import contextlib
try:
    import fcntl
except ImportError:  # Windows: the console bridge is ConPTY (console_pty)
    fcntl = None
import functools
import json
import logging
import os
try:
    import pty
except ImportError:  # Windows: the console bridge is ConPTY (console_pty)
    pty = None
import re
import secrets
import shlex
import shutil
import struct
import subprocess
import sys
try:
    import termios
except ImportError:  # Windows: the console bridge is ConPTY (console_pty)
    termios = None
import threading
import time
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path

import cloud_modules
import console_pty    # the Windows console bridge (a tmux relay); stdlib-only at import
import fleet_launch   # tmux_session(): the single home for the node's default-session fallback chain
from jaah_env import env_get
import host_platform
import ce_caps  # the community edition's node-side caps (a no-op on a cloud node)
import node_frame_auth   # single home for "is this CP->node frame authentic" (the cp_frame_proof check)
import env_helpers  # `environ_node_token`: the token the handshake validates against
import session_user
from control_plane_api import agent_api_worktree
# Read from the module that CREATES it, so the view path's denylist cannot drift from it.
from fleet_tmux_server import HOLDER_WINDOW
from env_helpers import (   # single home
    _tmux_safe_env, SESSION_NAME_RE, NoNodeIdentity,
)

log = logging.getLogger("jaahd.console")

# ── The slot read: `None` is not `{}` ──────────────────────────────────────────────────
# `agent_api_worktree` returns None on a transport failure and a record otherwise; both readers below
# treat None as "held / unresolvable". Fail-CLOSED — reading it as "free" releases a live slot's guard.

CONSOLE_ENABLED = env_get("JAAH_CONSOLE_ENABLED", "1") != "0"
# The token the connect handshake validates against — a PIN, not the value; `None` (production) means
# read `os.environ['NODE_TOKEN']` at validation time, so a rotation strands nothing. Fail-closed.
NODE_TOKEN: str | None = None
# A session with no stdin/stdout traffic and no detach message this long is force-closed — bounds an
# abandoned attach whose relay vanished without publishing `.detach`.
CONSOLE_IDLE_TIMEOUT_SECONDS = int(os.environ.get("CONSOLE_IDLE_TIMEOUT_SECONDS", "1800"))
_IDLE_SWEEP_INTERVAL_SECONDS = 30.0
# Bounds one worktree/kind's viewers — caps the resource a NODE_TOKEN holder can grow unbounded.
_MAX_VIEWERS_PER_SESSION = 16

# ── PTY I/O sizing (the latency path) ─────────────────────────────────────────────────
# The master fd is O_NONBLOCK, so both directions batch without ever blocking the event loop.
_STDOUT_READ_SIZE = 65536
# Bounds one drain so a firehose cannot hold the reader callback forever and starve every other task.
_STDOUT_DRAIN_MAX_BYTES = 4 * _STDOUT_READ_SIZE
# Bounds the output HELD for a session's first viewer. A fresh `tmux attach` paints the whole screen within
# ~50ms of the spawn, before the CP dial has landed a viewer; flushed to zero viewers that paint was lost,
# and the console stayed blank until tmux's 5s terminal-query timeout repainted. A paint is tens of KB.
_STDOUT_PRE_VIEWER_MAX_BYTES = 1024 * 1024
# Bounds the un-written stdin backlog. Only reachable when the PTY stops draining while a client keeps
# sending, and dropping the overflow is right — these are keystrokes. Above the browser's max frame.
_STDIN_MAX_PENDING_BYTES = 1024 * 1024

# The CP path this daemon dials per console session, served by the console relay
# (`apps/api/app/console_relay/server.py`; on ECS the ALB routes it there from the CP's own hostname).
CONSOLE_STREAM_PATH = "/api/console-stream"
# Bounds the dial so a black-holed CP cannot hold a `console_open` open past the CP's own wait.
_STREAM_DIAL_TIMEOUT = 10.0
# The header this daemon names ITSELF in when it dials. The CP binds every console ticket to the node it
# was minted for and refuses a dial from any other — or from one that sends no id — so this is not
# optional decoration. Must match `apps/api/app/console_relay/tickets.py`'s NODE_ID_HEADER, asserted in
# test_console.py: a drift is silent on both sides and kills Console fleet-wide with a 4404.
_NODE_ID_HEADER = "X-Node-Id"
# Why a session ended, for the CP relay. Must match `apps/api/app/console_relay/tickets.py`'s NODE_CLOSE_*.
_CLOSE_CODE_SESSION_ENDED = 4000
_CLOSE_CODE_RECOVERABLE = 4001

# The same two verdicts in the BROWSER's vocabulary, for a direct viewer with no relay to translate:
# an untranslated 4000 is absent from the browser's NON_RETRYABLE_CLOSE_CODES, so an ENDED session would
# ride the reconnect ladder. Homed here because `console_direct` imports this module, not vice versa.
_BROWSER_CLOSE_CODE_SESSION_ENDED = 1000
_BROWSER_CLOSE_CODE_RECOVERABLE = 4503

# Which close reasons a viewer may come BACK from. The node decides, not the API: it is the only side
# that knows what ended the session.
#
# The question is "can a viewer RE-ATTACH", not "did we close something": every entry runs through
# `_close_session`, whose contract is DETACH, NEVER KILL, so losing a VIEWER is retryable by
# construction and only losing the SESSION (`pty-eof`, `window-gone`) is terminal.
_CLOSE_REASON_RETRYABLE: dict[str, bool] = {
    "shutdown": True,
    "pty-eof": False,
    # The sweep DETACHES (the agent keeps running, a viewer can re-attach), so it is retryable; marked
    # terminal, a SUSPENDED phone page (no timers, so past CONSOLE_IDLE_TIMEOUT_SECONDS) got close 1000
    # and never retried. A throttled tab stays well inside the timeout.
    "idle timeout": True,
    # Terminal, and unobservable: it fires only after the last viewer is gone, so no code is sent.
    "all viewers detached": False,
    # The tmux window this PTY was attached to no longer exists (`_Session.tmux_window`). TERMINAL:
    # a retry would re-attach to nothing, and reopening the console starts a fresh agent on a click.
    "window-gone": False,
    # Not a session close at all — the viewer-cap eviction closes ONE viewer's socket while the
    # session keeps running. Retryable by construction.
    "viewer evicted": True,
    # A `worktrees:edit` viewer arrived at a PTY a watcher had opened `-r`, so it was rebuilt writable.
    # RETRYABLE by design: the displaced watchers are meant to auto-reconnect to the fresh PTY.
    "read-only rebuild": True,
}

# The human-readable close REASON, where one says something the CODE cannot. Not every reason gets one:
# the browser prefers this string over its own label, so a bare token like `pty-eof` would read worse.
_CLOSE_REASON_TEXT: dict[str, str] = {
    "window-gone": "the tmux window this console was showing was closed — reopen to start a new session",
    # Nothing ended: a user with edit access joined and the console was rebuilt writable under this
    # watcher, who is about to be reconnected. "session ended" would be wrong, not just unhelpful.
    "read-only rebuild": "this console was rebuilt for a user with edit access — reconnecting",
}

_CONSOLE_DIM_MIN, _CONSOLE_DIM_MAX = 1, 1000   # sane bounds for a terminal's cols/rows (winsize is u16)

# The stderr shapes tmux prints when there is no server at all. Verified on tmux 3.4: `no server
# running on` is ECONNREFUSED and definitive alone; `error connecting to <socket> (<strerror>)` is the
# fallback for every other connect errno, so it means "no server" ONLY with the ENOENT suffix.
_TMUX_NO_SERVER_RUNNING = "no server running on"
_TMUX_ERROR_CONNECTING = "error connecting to"
_TMUX_ERROR_CONNECTING_ENOENT_SUFFIX = "(no such file or directory)"
# tmux's answer (exit 1) on a server that is ALIVE with ZERO sessions — reachable under `exit-empty
# off`. Unclassified it hits the fail-closed default, and every console open answers indeterminate.
_TMUX_NO_CURRENT_TARGET = "no current target"

# Client→node stdin wire frame: 1-byte type + 2-byte big-endian length + payload, matching
# ConsoleTerminal.tsx. Each WS message already IS one complete frame, so nothing reassembles here.
_FRAME_HEADER_LEN = 3
_FRAME_TYPE_INPUT = 0
_FRAME_TYPE_RESIZE = 1
# A pasted image, split because a frame's length field caps a payload at 65535: type 2 is a JSON header
# `{"size", "ext"}`, type 3 the bytes. The declared size IS the terminator, so no half-file survives.
_FRAME_TYPE_PASTE_BEGIN = 2
_FRAME_TYPE_PASTE_DATA = 3
# "Copy pane text", empty payload: the daemon captures the pane's history as plain text and has tmux hand it
# to THIS PTY's client as OSC 52 (`_copy_pane_to_client`), which the browser's clipboard addon already reads.
_FRAME_TYPE_COPY_PANE = 4
# How long a PTY's size must hold still before its client is redrawn (`_redraw_client`). The browser
# already debounces its refits (ConsoleTerminal.tsx's PANEL_REFIT_DEBOUNCE_MS); this coalesces what is left.
_RESIZE_SETTLE_SECONDS = 0.3
# The copied text's cap, kept from the END (the newest output). Base64 grows it by 4/3, and xterm.js drops an
# OSC payload past 10 MB, so 4 MiB lands; `history-limit 50000` (the host's tmux.conf) rarely reaches it.
_COPY_PANE_MAX_BYTES = 4 * 1024 * 1024
# One copy per session at a time, at most this often: each frame is 3 bytes in and megabytes of tmux work and
# OSC 52 out, so an unthrottled stream of them would be the cheapest way to stall the node.
_COPY_PANE_MIN_INTERVAL_SECONDS = 1.0

# Pasted images land here, one directory per slot, and enter the session as an absolute PATH. `/tmp` is
# deliberate: they must survive nothing, and the daemon must never write inside a session user's home.
_PASTE_ROOT = "/tmp/console-paste"
if host_platform.is_windows():
    # No shared `/tmp` there: the run root is the one directory both the daemon and the runner account
    # hold Modify on, and the typed path is then one the session can open.
    _PASTE_ROOT = os.path.join(host_platform.paths().run_dir, "console-paste")
# The cap is the browser's too (ConsoleTerminal.tsx's PASTE_MAX_BYTES), enforced again here because a
# declared size is attacker input. Both the declared size and the running total are checked.
_PASTE_MAX_BYTES = 10 * 1024 * 1024
# Allowlist, not a sanitizer: the extension is concatenated into a filename.
_PASTE_EXTS = frozenset({"png", "jpg", "jpeg", "gif", "webp"})
# The staging directory's name comes from `wt_key`, i.e. from DB-sourced fields. Checked rather than
# trusted: a malformed one is a path traversal into a directory owned by the session user.
_PASTE_KEY_SAFE = re.compile(r"^[A-Za-z0-9._-]+$")
# Consumed within seconds of landing; a day is slack for a user who scrolls back to an old path. Swept
# opportunistically on the next paste into the same slot, since a slot nobody pastes into has nothing.
_PASTE_PRUNE_AFTER_SECONDS = 24 * 3600

# The one resolve error meaning "I could not find out", not "there is nothing here". It rides the reply
# as `retryable`, because the browser's auto-reconnect stops permanently on a definitive answer.
INDETERMINATE_ERROR = "could not read this worktree's state"

# `_console_run`'s exit code for "that program is not installed here" (a FileNotFoundError from exec),
# as opposed to its `-1` "the program ran and we could not get an answer".
EXIT_BINARY_MISSING = -2

# A CP-NAMED tmux session — the name travels FROM the control plane; this node only supplies
# `fleet_launch.tmux_session()`'s default. Shape-checked against `env_helpers.SESSION_NAME_RE`.


def _validate_session_name(raw: object) -> tuple[str | None, str | None]:
    """(session, error) — RAW as an optional CP-named tmux session, or why it is refused.

    `(None, None)` is "the CP named none", which is not an error: the caller falls back to this node's
    own default. A PRESENT but invalid value IS an error — it must never reach a tmux argv unvalidated.
    The regex's leading-alnum rule already refuses `EPHEMERAL_VIEW_SESSION_PREFIX` as a side effect."""
    if raw is None:
        return None, None
    if not isinstance(raw, str) or not SESSION_NAME_RE.match(raw):
        return None, f"invalid session name {raw!r} — expected 1-64 chars of [A-Za-z0-9_-], starting alphanumeric"
    return raw, None


def _validate_optional_bool(raw: object, field: str) -> tuple[bool, str | None]:
    """(value, error) — RAW as an optional control-plane boolean, or why it is refused.

    ABSENT MEANS FALSE (a frame predating the flag omits it). A PRESENT NON-BOOLEAN IS REFUSED rather
    than coerced: False would hand a view-only principal a WRITABLE console, True an edit user a terminal
    that silently swallows keystrokes."""
    if raw is None:
        return False, None
    if raw is not True and raw is not False:
        return False, f"invalid {field} {raw!r} — expected a JSON boolean"
    return bool(raw), None


def _expected_token() -> str:
    """The token to validate against, resolved AT CALL TIME: the pin if one is set, else `os.environ`
    read fresh, so a rotation takes effect without restarting the daemon."""
    return NODE_TOKEN if NODE_TOKEN is not None else env_helpers.environ_node_token()


def _clamp_dim(value: object, *, default: int) -> int:
    """A terminal dimension coerced into the safe winsize range, or the default for anything
    non-numeric or out of range."""
    try:
        n = int(value)   # type: ignore[arg-type]
    except (TypeError, ValueError):
        return default
    if n < _CONSOLE_DIM_MIN or n > _CONSOLE_DIM_MAX:
        return default
    return n


def _valid_resize_dim(value: int) -> int | None:
    """A resize-frame dimension in the safe winsize range, or None to DROP the resize (no sane
    fallback for a live resize, unlike the handshake's initial cols/rows)."""
    return value if _CONSOLE_DIM_MIN <= value <= _CONSOLE_DIM_MAX else None


def _set_winsize(fd: int, rows: int, cols: int) -> None:
    winsize = struct.pack("HHHH", rows, cols, 0, 0)
    fcntl.ioctl(fd, termios.TIOCSWINSZ, winsize)


def _console_run(*argv: str, timeout: int = 300, env: dict | None = None,
                 stdout_limit: int | None = session_user.STDOUT_CAP) -> dict:
    """subprocess.run → {"exit_code","stdout","stderr"}, never raising — a creation failure must be a
    fast, clear error, never a hang.

    stdout keeps only the TAIL of `stdout_limit` chars, so a caller that PARSES it must pass
    `stdout_limit=None`: a truncated listing reports live windows as gone. `-1` is "could not find out";
    `EXIT_BINARY_MISSING` is definitive and must never reach the retryable arm."""
    if env is None:
        env = _tmux_safe_env()
    try:
        result = subprocess.run(list(argv), capture_output=True, text=True, timeout=timeout, env=env)
        return {
            "exit_code": result.returncode,
            "stdout": result.stdout if stdout_limit is None else result.stdout[-stdout_limit:],
            "stderr": result.stderr[-session_user.STDERR_CAP:],
        }
    except subprocess.TimeoutExpired as exc:
        return {"exit_code": -1, "stdout": "", "stderr": f"timeout: {exc}"}
    except FileNotFoundError:
        # Separated from the catch-all below, which would report this as indeterminate and hand the
        # browser an endless retry: an ENOENT on exec is definitive, and argv[0] is the diagnostic.
        return {
            "exit_code": EXIT_BINARY_MISSING, "stdout": "",
            "stderr": f"{argv[0]}: not installed on this node",
        }
    except Exception as exc:
        return {"exit_code": -1, "stdout": "", "stderr": str(exc)}


def _tmux_query_is_failure(result: dict) -> bool:
    """Whether a `tmux list-windows`/`list-panes` result is a genuine "I could not look" — the only fact
    `query_failed=True` carries — as opposed to "no tmux server, so nothing live to find".

    Gated on `exit_code > 0`, so `_console_run`'s synthesized `-1` can never reach it: that arm is the
    loaded-host hiccup, and reading it as "nothing live" double-creates an agent. `no server running on`
    is definitive alone; `error connecting to` needs the ENOENT suffix, since `(Permission denied)` can
    sit in front of a live server. Everything else stays fail-closed."""
    exit_code = result["exit_code"]
    if exit_code == 0:
        return False
    if exit_code < 0:
        # _console_run's own timeout/exception arms — never tmux, so no shape below applies.
        # EXIT_BINARY_MISSING included: `_missing_binary_error` keeps a missing tmux out one level up.
        return True
    stderr = result["stderr"].strip().lower()
    if _TMUX_NO_SERVER_RUNNING in stderr:
        return False
    if _TMUX_ERROR_CONNECTING in stderr and stderr.endswith(_TMUX_ERROR_CONNECTING_ENOENT_SUFFIX):
        return False
    if _TMUX_NO_CURRENT_TARGET in stderr:
        # A live server with zero sessions. Trustworthy as "zero windows", and unlike "no server
        # running" it DID answer, so the create path may build onto it — panes fork as the SERVER's uid.
        return False
    return True


def _tmux_server_answered(result: dict) -> bool:
    """Whether this tmux invocation actually REACHED A LIVE SERVER — a different question from
    `_tmux_query_is_failure`'s "may I trust this listing as zero windows?".

    The CREATE path needs this one: `tmux new-session` on a socket with no server STARTS ONE, as
    `jaahd`. So `error connecting to` counts here for EVERY connect errno, `(Permission denied)`
    included, where the classifier above narrows to ENOENT — EACCES must read neither as an empty listing
    there nor as a licence to start our own server here."""
    exit_code = result["exit_code"]
    if exit_code == 0:
        return True
    if exit_code < 0:
        return False
    stderr = result["stderr"].strip().lower()
    if _TMUX_NO_SERVER_RUNNING in stderr or _TMUX_ERROR_CONNECTING in stderr:
        return False
    return True


def _missing_binary_error(*binaries: str) -> str | None:
    """A DEFINITIVE error naming the first of `binaries` this node does not have, or None.

    THE POINT IS WHICH KIND OF ERROR IT IS: FileNotFoundError becomes exit -1, which reads as "could not
    look", which the API maps to RETRYABLE — so an unprovisioned node had a browser reconnecting forever
    and never saying why. No retry installs a missing program."""
    for binary in binaries:
        if shutil.which(binary) is None:
            return (
                f"{binary} is not installed on this node — the runner toolchain layer never landed "
                f"here. On the node itself (Tailscale SSH): sudo REPROVISION_CREDENTIAL_LAYER=0 "
                f"REPROVISION_ACCOUNT_LAYER=0 {host_platform.paths().python} "
                f"/usr/lib/jaahd/infra/jaah/node_reprovision.py $NODE_ID — the same layer "
                f"packaging/postinst runs on every install, which apt-installs {binary}"
            )
    return None


EPHEMERAL_VIEW_SESSION_PREFIX = "_view_"


def is_ephemeral_view_session(name: str) -> bool:
    """Whether `name` names an EPHEMERAL PER-VIEWER tmux session — a `_view_<token>` grouped session a
    browser tile OR A SLOT CONSOLE attaches through, which exists only while that viewer is open.

    THE SINGLE HOME OF THIS RULE; `jaah_cli`'s copy is sanctioned (installed at /usr/bin) and pinned by
    a test. A grouped session duplicates every `list-panes -a` row, so a first-match reader can get a
    target that vanishes with the tile. THE FIX IS A PARSE FILTER, NEVER A NARROWED QUERY:
    `ReconcileContext.tmux`'s contract is every socket or nothing."""
    return name.split(":", 1)[0].startswith(EPHEMERAL_VIEW_SESSION_PREFIX)


def _tmux_listing_by_socket(
    *args: str, sockets: list[str] | None = None,
) -> tuple[list[tuple[str, str]], bool]:
    """Run one tmux LISTING over every socket a read must cover, and return
    ([(socket, stdout) for every socket that ANSWERED], any_query_failed).

    TWO SOCKETS, NOT ONE: an in-place upgrade keeps pre-switch runner windows on the daemon's own server,
    so reading one would report a live session as "nothing here". The FLEET socket is stricter than
    `_tmux_query_is_failure`: this daemon may not bootstrap it, so an unreachable fleet socket is a QUERY
    FAILURE, never a zero."""
    fleet = session_user.fleet_socket()
    fleet_must_be_live = session_user.switch_needed()
    listings: list[tuple[str, str]] = []
    query_failed = False
    for socket in (session_user.manage_sockets() if sockets is None else sockets):
        result = _console_run(*session_user.tmux_cmd(socket, *args), timeout=5, stdout_limit=None)
        if _tmux_query_is_failure(result):
            query_failed = True
            continue
        if fleet_must_be_live and socket == fleet and not _tmux_server_answered(result):
            log.warning(
                "console: the fleet tmux server on %s did not answer (exit=%s): %s — reporting "
                "indeterminate rather than zero windows, because this daemon must not create that "
                "server (it would run as %s, not %r)",
                socket, result["exit_code"], result["stderr"].strip() or "(no stderr)",
                getattr(os, "geteuid", lambda: "n/a")(), session_user.session_user(),
            )
            query_failed = True
            continue
        listings.append((socket, result["stdout"]))
    return listings, query_failed


def _tmux_listing(*args: str) -> tuple[str, bool]:
    """`_tmux_listing_by_socket` for the callers that want only the lines: (concatenated stdout,
    any_query_failed). Every word of that function's contract applies here unchanged."""
    listings, query_failed = _tmux_listing_by_socket(*args)
    return "".join(stdout for _, stdout in listings), query_failed


def _find_live_clauded_window(path: str, fleet_session: str | None = None) -> tuple[str | None, bool]:
    """(target, query_failed): the target window, or None — and whether the tmux QUERY itself failed,
    which is a different fact from "no such window" and must not be collapsed into it.

    Reported as "not found", a hiccup would be the API's terminal 4502 for a live session and, on the
    create path, a second agent in the worktree."""
    fleet_session = fleet_launch.tmux_session(fleet_session)
    stdout, query_failed = _tmux_listing(
        "list-panes", "-a",
        # `#{window_id}` (`@N`), NOT `#{window_index}`: tmux renumbers an index when a lower one is
        # killed; the id lives and dies with the window, so it is a usable liveness key.
        "-F", "#{pane_current_path}\t#{window_name}\t#{session_name}:#{window_id}",
    )
    # `list-panes -a` is socket-wide; restricting to the target session keeps a pane that merely sits in
    # the worktree (a dev server) from being taken for the agent. A SESSION test, not
    # `#{pane_current_command} == "claude"`: an agent running a pager would read as absent.
    exact = prefix = None
    for line in stdout.splitlines():
        parts = line.split("\t", 2)
        if len(parts) != 3:
            continue
        cwd, name, target = parts
        if name.endswith("|term") or name.endswith("|web"):
            continue
        if is_ephemeral_view_session(target):
            continue
        if target.rsplit(":", 1)[0] != fleet_session:
            continue
        if host_platform.path_key(cwd) == host_platform.path_key(path):
            exact = target
            break
        if prefix is None and host_platform.is_under(cwd, path):
            prefix = target
    target = exact or prefix
    # A match is a match even if some OTHER socket's query failed; the flag only speaks for the
    # nothing-found case — see `_tmux_listing`.
    return (target, False) if target else (None, query_failed)


# TWO TMUX TARGET FORMATS COEXIST HERE ON PURPOSE. This module resolves `session:@id`, because a console
# must prove later that the SAME window is still there; `worktrees.tmux_target` stays
# `session:window_index`, because reconcile is a SENSOR and every kill site re-resolves per socket.
def live_window_ids() -> tuple[set[str], bool]:
    """(every `#{window_id}` this node's tmux servers currently hold, query_failed).

    The liveness half of `_Session.tmux_window`. `query_failed` IS THE WHOLE SAFETY OF THIS: a caller must
    treat it as "leave the session alone", or one hiccuping `list-windows` tears down every open console.
    Residual: a `@id` is per-SERVER and both servers mint from `@0`, so during a drain an id held by the
    other reads as alive and that slot keeps serving a drifted PTY until the drain ends."""
    stdout, query_failed = _tmux_listing("list-windows", "-a", "-F", "#{window_id}")
    return {line.strip() for line in stdout.splitlines() if line.strip()}, query_failed


def _fleet_server_refusal(socket: str, detail: str) -> dict:
    """A `_console_run`-shaped result meaning "the fleet tmux server is not reachable, so this create did
    not happen". `exit_code == -1` on purpose: `_create_failure` maps it to INDETERMINATE_ERROR ⇒ the
    API's RETRYABLE close ⇒ the browser asks again. Accurate, not merely kind — the fleet server is a
    `Restart=always` unit, so "not there this second" is a state that fixes itself."""
    log.warning(
        "console: refusing to create a window on %s — %s. Starting that server is what this daemon must "
        "never do: it would run as uid %s (`jaahd`) and every pane on it would too, which is exactly "
        "the uid `jaah-runner@.service` exists to keep out of runner panes.",
        socket or "tmux's default socket", detail, getattr(os, "geteuid", lambda: "n/a")(),
    )
    return {"exit_code": -1, "stdout": "", "stderr": f"fleet tmux server unavailable on {socket}: {detail}"}


def _create_failure(result: dict, label: str, *, what: str) -> str:
    """The error a failed create SUBPROCESS resolves to: INDETERMINATE_ERROR when we never got an answer,
    the short `label` when it definitively failed.

    A create that merely TIMED OUT is not "there is no session here", and reporting it as one reached the
    API's terminal 4502 for a session that came up a second later."""
    log.warning(
        "console: %s failed (exit=%s): %s | %s",
        what, result["exit_code"], result["stderr"].strip(), result["stdout"].strip(),
    )
    return INDETERMINATE_ERROR if result["exit_code"] == -1 else label


# ── The two identities a console has ────────────────────────────────────────────────────────────────

# `ConsoleKey` is the PTY identity: (project, number, kind, view) — two tiles showing different fleet
# windows are two PTYs. `SlotKey` is the WORKTREE identity, `ConsoleKey[:3]`, what `_opening` reserves;
# it carries no `view` because that reservation guards the worktree's window against the reconciler's
# DONE-close. `has_live_session` MUST stay a key[0]/key[1] prefix match, or a tiled-only slot dies.

# TWO SENTINELS FOR "NO VIEW": `key[3]` is `""` (a dict-key element should be a value);
# `_Session.view_socket` is `None`, because `""` is a VALID socket (tmux's default label) — a
# truthiness test on it would skip teardown.

# Liveness attribution is MIS-ATTRIBUTED on purpose: a tile opened from slot A but showing slot B's
# window registers under A, because keying it to the viewed window would let an idle tab pin arbitrary
# slots against reclaim. The cost: a killed viewed window leaves the tile rendering a stale label.
ConsoleKey = tuple[str, str, str, str]
SlotKey = tuple[str, str, str]


def _normalize_view(view: object) -> str:
    """The one spelling this module accepts for "no view" in a `ConsoleKey`'s fourth element — the empty
    string, never `None`. Both transports route every `ConsoleKey` through here, or the two would key the
    same tmux window twice and attach a second PTY."""
    return "" if view is None else str(view)


def console_key_target(view: object, layout: object) -> tuple[str, str | None]:
    """(key[3], error) — the fourth element of a `ConsoleKey` for whatever this open asked to see.

    THE ONE PLACE THE THREE TARGET SHAPES ARE RESOLVED AGAINST EACH OTHER, so the two transports cannot
    disagree: neither → `""`, `view=@7` → `"@7"`, `layout=r50(@3,@7)` → `"layout:r50(@3,@7)"`; a MicroVM
    session's `view=vm:<vm_id>` → `"vm:<vm_id>"` (its own PTY, never tmux: `_open_vm_pty`). BOTH AT
    ONCE IS REFUSED — a transport that picked would show the user the wrong thing and call it working.
    SHAPE ONLY; existence is checked at resolve time against tmux."""
    view_value = _normalize_view(view)
    layout_value = "" if layout is None else str(layout)
    if view_value and layout_value:
        return "", "a console asks for a window or a layout, never both"
    if layout_value:
        tree, error = parse_layout_spec(layout_value)
        if tree is None:
            return "", error or "invalid layout"
        return f"{_LAYOUT_KEY_PREFIX}{layout_value}", None
    if view_value.startswith(_VM_KEY_PREFIX):
        microvmd = cloud_modules.load("microvmd")  # Linux-only machinery, off the macOS/Windows import graph
        if microvmd is None:
            return "", "this node runs no MicroVM"
        if not microvmd.valid_vm_id(view_value[len(_VM_KEY_PREFIX):]):
            return "", f"invalid VM id in {view_value!r}"
        return view_value, None
    if view_value and not _VIEW_ID_RE.match(view_value):
        return "", f"invalid window id {view_value!r} — expected a tmux window id like @7"
    return view_value, None


# THREE elements: this lock is per-WORKTREE-SESSION (the view path never takes it). It serialises
# resolve-or-create across the whole window — the lookups, the create budget's read-then-append, and the
# `clauded_wt.py` spawn — so two concurrent opens cannot both spawn, and the budget needs no lock of its own.
_console_create_locks: dict[SlotKey, threading.Lock] = {}
_console_create_locks_guard = threading.Lock()


def _console_create_lock(project: str, number: object, kind: str) -> threading.Lock:
    key = (project, str(number), kind)
    with _console_create_locks_guard:
        lock = _console_create_locks.get(key)
        if lock is None:
            lock = threading.Lock()
            _console_create_locks[key] = lock
        return lock


# ── The create budget, and it lives HERE because this is where the two transports meet ──────────────
# The relay's per-(principal, slot) budget (`routes/console.py`) is blind to the DIRECT transport,
# so this one sits where both funnel. Keyed on the SLOT: the node cannot see a principal, and
# the property worth holding is "one worktree does not accumulate agents". A second Python rate limiter
# (after `apps/api/app/ratelimit.py`) because the deployables cannot share code; a third is the trigger
# to extract.
_CREATE_BUDGET_PER_HOUR = 12
_CREATE_BUDGET_WINDOW_SECONDS = 3600.0
_create_history: dict[SlotKey, list[float]] = {}


def _create_budget_allows(key: SlotKey, *, now: float | None = None) -> bool:
    """Has this slot spent its hourly create budget? Records the create when it answers True.

    Called under `_console_create_lock` for the same key, held across the whole resolve-then-create
    window, so the read-then-append is safe and two dials cannot both spend the last unit."""
    stamp = time.monotonic() if now is None else now
    recent = [t for t in _create_history.get(key, ()) if stamp - t < _CREATE_BUDGET_WINDOW_SECONDS]
    if len(recent) >= _CREATE_BUDGET_PER_HOUR:
        _create_history[key] = recent
        return False
    recent.append(stamp)
    _create_history[key] = recent
    return True


def _reset_create_budget_for_tests() -> None:
    _create_history.clear()


def console_config_dir(raw: object) -> str | None:
    """THE ONE READING of a control-plane frame's `config_dir`, for both transports: a non-blank stripped
    string, or None. Anything unusable reads as ABSENT rather than being coerced."""
    if not isinstance(raw, str):
        return None
    stripped = raw.strip()
    return stripped or None


def console_securestorage_dir(raw: object) -> str | None:
    """THE ONE READING of a control-plane frame's `securestorage_dir` — the CP's view of the account's token
    store — for both transports. Forwarded to `clauded_wt.py`, which no longer reads it: the node resolves
    the store itself (`shared_storage.securestorage_dir`). Delegates, so the two fields cannot disagree on
    normalisation; blank reads as absent."""
    return console_config_dir(raw)


def console_provider(raw: object) -> str | None:
    """THE ONE READING of a frame's `provider` (`accounts.provider` of the account a create runs on), for
    both transports. Absent (an older CP) is None, and `clauded_wt.py` then opens the catalogue default;
    a value with no adapter is refused there, never opened on another binary."""
    return console_config_dir(raw)


def console_api_key_account(raw: object) -> str | None:
    """THE ONE READING of a frame's `api_key_account` — the SLUG of an API-key account (never its key), sent
    in place of `config_dir`, which an API-key account does not have. Absent is an OAuth account."""
    return console_config_dir(raw)


def console_bedrock(raw: object) -> str | None:
    """THE ONE READING of a frame's `bedrock` — the Jaah-account block (K2) a create on the platform Bedrock
    account carries in place of `config_dir`/`api_key_account`: the validated block as canonical JSON (the
    form `clauded_wt.py --bedrock-plan` takes), or None. Absent is an own-accounts tab; a malformed block is
    None too, logged, and the create is then refused for naming no account — never opened on another one."""
    import bedrock_launch  # noqa: PLC0415 — only a Jaah-account frame needs it

    try:
        plan = bedrock_launch.optional_plan(raw)
    except bedrock_launch.PlanRefused as exc:
        log.warning("console: refusing a create frame's Jaah-account block: %s", exc)
        return None
    return json.dumps(plan.wire(), sort_keys=True) if plan is not None else None


def console_worktree_path(raw: object) -> str | None:
    """THE ONE READING of a frame's `path` — the slot's absolute path as the CP knows it. Anything else
    (absent, empty, relative, non-string) is None, and the node falls back to looking it up."""
    return raw if isinstance(raw, str) and os.path.isabs(raw) else None


def console_guard(raw: object) -> int | None:
    """THE ONE READING of a frame's (or a direct grant's) `guard` — the Architectural Guard stanza the CP sends only
    where the guard is available for the org: its project id, or None. Absent or malformed is an unguarded tab."""
    guard_bundle = cloud_modules.load("guard_bundle")  # the `compliance` feature's; None where it is not
    return guard_bundle.project_id_of(raw) if guard_bundle is not None else None


def _guard_kw(guard_project: int | None) -> dict:
    """`guard_project=` for the next hop only when a stanza arrived, so an unguarded open's calls are unchanged."""
    return {"guard_project": guard_project} if guard_project else {}


def _console_guard_env(project_id: int, path: str, provider: str | None) -> dict[str, str]:
    """A guarded tab's `JAAH_GUARD_*` exports, from `guard_bundle.prepare_launch` run HERE, in the daemon, so the
    60 s refresh is registered where it lives (`clauded_wt.py` is a short-lived child). `{}` when `_meta` cannot
    be made: the tab then opens unguarded, as a fetch miss never would — said in the log."""
    import agent_providers  # noqa: PLC0415 — the lazy siblings `clauded_wt.py` uses too
    import fleet_dispatch  # noqa: PLC0415

    guard_bundle = cloud_modules.load("guard_bundle")
    if guard_bundle is None:
        return {}
    meta = os.path.join(os.path.dirname(path), "_meta")
    err = session_user.ensure_dir_as_session_user(meta, mode=fleet_dispatch.META_DIR_MODE)
    if err:
        log.warning("console: could not create %s (%s) — this tab opens without the Architectural Guard", meta, err)
        return {}
    # `clauded-<slot>`: the tab's own `_meta` stem, as its settings file (`clauded_wt.py`).
    return guard_bundle.prepare_launch(
        {"project_id": project_id}, meta=meta, ref_key=f"clauded-{os.path.basename(path.rstrip('/'))}",
        mode=fleet_dispatch.META_FILE_MODE, runtime=provider or agent_providers.DEFAULT_PROVIDER, worktree=path)


def _post_create_not_found(project: str, number: object, query_failed: bool, *, what: str) -> str:
    """The error a create that SUCCEEDED and then wasn't found resolves to — INDETERMINATE_ERROR.

    Neither outcome is definitive: a failed query says nothing, and an EMPTY listing says almost as
    little, because `_find_live_clauded_window` matches on `pane_current_path`, which can lag."""
    log.warning(
        "console: %s reported success for %s-%s but the confirmation lookup %s",
        what, project, number, "failed" if query_failed else "came back without the window",
    )
    return INDETERMINATE_ERROR


@ce_caps.serialised
def resolve_or_create_console_target(
    repo_root: Path, project: str, number: object, kind: str, *,
    # FAIL CLOSED: True spawns an agent against an account's quota, so a forgetful call site resolves
    # only. Every production caller passes it explicitly.
    create: bool = False,
    node_id: str = "", config_dir: str | None = None, session: str | None = None,
    securestorage_dir: str | None = None,
    provider: str | None = None, api_key_account: str | None = None, bedrock: str | None = None,
    path: str | None = None, guard_project: int | None = None,
) -> tuple[str | None, str | None, bool]:
    """(target, error, created) for a Console open — resolves a worktree's live `clauded` tmux window,
    and CREATES one when there is none and `create` is set.

    IT TAKES NO HOLD, AND THAT IS THE WHOLE INVARIANT: only the CP leases, releases or commands a slot,
    so nothing here writes `worktrees.state`, leases, or resets the tree. Opening a window to look
    through is not an occupancy decision — and without it a slot whose runner died (work on disk,
    nothing in tmux) has no way in.

    `create=False` is the browser's auto-reconnect: attach only, never spawn. Never raises; blocking, so
    call off the event loop. Always a live tmux query (never the cached `tmux_target`), and a FAILED query
    is INDETERMINATE_ERROR, never "no live session". `node_id` scopes the slot lookup to this node.
    `config_dir` is the account a create runs on (the CP names it; refused without one), unread on
    attach. `securestorage_dir` is the CP's view of its token store, forwarded and unread
    (`console_securestorage_dir`). `api_key_account` is the other kind of account: its slug, standing in for
    `config_dir` (an API-key account has none), and `provider` is whose CLI the tab opens; both the CP's.
    `bedrock` is the third kind: the Jaah-account block (`console_bedrock`), standing in for both.
    `path` is the slot's path as the CP sent it in `console_open`; absent, it is looked up from the CP.
    `guard_project` is the Architectural Guard stanza's project (`console_guard`): a create then writes the rule
    bundle first and hands `clauded_wt.py` its exports; None leaves the create's argv byte-identical."""
    # This node either has tmux or has no console: a definitive error, so the browser stops retrying.
    missing = _missing_binary_error("tmux")
    if missing is not None:
        return None, missing, False
    if not path:
        record = agent_api_worktree(project, number, node_id)
        if record is None:
            # COULD NOT LOOK ≠ NOTHING THERE. Fail-closed rather than defaulting to "nothing to find".
            return None, INDETERMINATE_ERROR, False
        path = record.get("path")
    try:
        fleet_session = fleet_launch.tmux_session(session)
    except NoNodeIdentity as exc:
        # Definitive: an unprovisioned node has no session until `jaah auth login` runs.
        return None, str(exc), False

    if kind != "clauded":
        # Console is `clauded`-only.
        return None, "no live session for this worktree", False

    with _console_create_lock(project, number, "clauded"):
        # No path: the record read fine and no query ran, so fall through to the definitive answer.
        target, query_failed = _find_live_clauded_window(path, fleet_session) if path else (None, False)
        if query_failed:
            # A failed query must not read as "nothing live": a terminal 4502, or a second agent.
            return None, INDETERMINATE_ERROR, False
        # This node's OWN default — differs from `fleet_session` exactly when the CP named one. `None`
        # without an identity (no second place to look).
        try:
            default_session = fleet_launch.tmux_session(None)
        except NoNodeIdentity:
            default_session = None

        if target is None and path and default_session and fleet_session != default_session:
            # ONE agent per worktree, whatever the session; a live one usually sits in the node's default,
            # so a CP-named miss checks there too.
            target, query_failed = _find_live_clauded_window(path, default_session)
            if query_failed:
                return None, INDETERMINATE_ERROR, False
        if target is not None:
            return target, None, False
        if not path or not create:
            # A first attach and a re-attach are otherwise identical, so a reconnect that created would
            # spawn a second agent against the quota.
            return None, "no live session for this worktree", False
        # ── CREATE, WITHOUT TAKING A HOLD ──────────────────────────────────────────────────────────
        # Only the CP may lease, release or command a slot. Any slot, free or held — the operator's call. The claim
        # that protects the tree is made CP-side before this frame (`routes/console.py::
        # _claim_slot_for_console_user`); the release is the tab's `role="user"` `/exit` hook
        # (`clauded_wt.py`). ACCEPTED RESIDUAL: an agent whose window is outside the fleet session reads
        # as absent, so a first dial spawns a second; `attach_only` on reconnect keeps it to one click.
        if not config_dir and not api_key_account and not bedrock:
            # The CP named no account: refuse rather than open on the unauthenticated default.
            return None, "no credential account available for this node (control plane)", False
        # The community edition's caps (no-ops on a cloud node): a definitive refusal, never INDETERMINATE_ERROR.
        if refusal := ce_caps.capacity_refusal():
            return None, ce_caps.capacity_text(refusal), False
        if why := ce_caps.admit_account(config_dir=config_dir or "", api_key_account=api_key_account or "",
                                           action="open a console"):
            return None, why, False
        if not _create_budget_allows((project, str(number), kind)):
            # Definitive, never INDETERMINATE_ERROR (retried every 5s, the loop this stops); the message
            # names the cause.
            log.warning(
                "console: create budget spent for %s-%s (%d per %.0fs) — refusing to spawn another",
                project, number, _CREATE_BUDGET_PER_HOUR, _CREATE_BUDGET_WINDOW_SECONDS,
            )
            return None, "too many sessions opened in this worktree recently — try again later", False
        # A new session starts here, so a multi-repo slot converges to the project's repositories first
        # (`slot_repos.converge_for_console`: free or user-held slots only; never raises).
        import slot_repos  # noqa: PLC0415 — deferred: only a create converges

        slot_repos.converge_for_console(project, number, node_id)
        launcher = str(repo_root / "apps" / "jaahd" / "clauded_wt.py")
        # The CP's view of the token store, forwarded and unread (`clauded_wt.py` resolves it); never empty.
        secure_flag = ("--securestorage-dir", securestorage_dir) if securestorage_dir else ()
        # The account: a config dir, or an API-key account's slug (whose key the tab's `apiKeyHelper`
        # fetches per request). The provider rides along only when named, so an older CP's argv is unchanged.
        account_flag = (("--bedrock-plan", bedrock) if bedrock
                        else ("--api-key-account", api_key_account) if api_key_account
                        else ("--config-dir", config_dir))
        provider_flag = ("--provider", provider) if provider else ()
        # Only for a guarded project, so every other create's argv is unchanged.
        guard_env = _console_guard_env(guard_project, path, provider) if guard_project else {}
        guard_flag = ("--guard-env", json.dumps(guard_env, sort_keys=True)) if guard_env else ()
        # `sys.executable`, not a bare "python3", so the launcher cannot land on a different python.
        # `--session` carries the session RESOLVED ABOVE; the launcher must not re-derive it.
        argv = (
            (sys.executable, launcher, *account_flag, *secure_flag, *provider_flag, *guard_flag,
             "--session", fleet_session, project)
            if str(number) == "0" else
            (sys.executable, launcher, *account_flag, *secure_flag, *provider_flag, *guard_flag,
             "--session", fleet_session, project, str(number))
        )
        # A Jaah tab may converge its home's managed layer first (`clauded_wt.py`), whose offline pass has
        # its own longer budget; every other tab keeps the old one.
        created = _console_run(*argv, timeout=90 if bedrock else 20)
        if created["exit_code"] != 0:
            return None, _create_failure(created, "failed to open a session", what="clauded_wt.py"), False
        target, query_failed = _find_live_clauded_window(path, fleet_session)
        if target:
            return target, None, True
        return None, _post_create_not_found(project, number, query_failed, what="clauded_wt.py"), False


# ── Grouped-session attachment: one ephemeral session per viewer ────────────────────────────────────

# `&view=@<window_id>` asks for a PTY showing ONE NAMED WINDOW of this node's shared `fleet` session.
# `fleet` is node-wide, so a view is a PER-VIEWER presentation: a GROUPED session (`new-session -t
# fleet`) shares the windows with its own current-window pointer, where two PTYs on one session are
# coupled. The slot console attaches through one too (`_create_console_session`).

# `prefix None` IS LOAD-BEARING: without it `C-b n` re-points a tile's session at another window and
# resizes fight through TIOCSWINSZ. `prefix`/`prefix2` are SESSION options, so nothing else is affected.

# Deliberately no `resize-window`/`refresh-client -C`/`window-size`: that would resize for EVERY viewer. A
# plain `refresh-client -t <client>` only redraws one client (`_redraw_client`).

# How long a `_view_*` session must have had NO client before the reaper may take it. Longer than the
# browser's 15s reconnect ceiling; the reservation below is the correctness guarantee, this is depth.
_VIEW_SESSION_NO_CLIENT_GRACE_SECONDS = 60.0
_VIEW_SESSION_SWEEP_INTERVAL_SECONDS = 30.0
# tmux's `#{window_id}` shape, validated before any argv. `\A`/`\Z`: Python's `$` also matches before a
# trailing newline.
_VIEW_ID_RE = re.compile(r"\A@[0-9]{1,18}\Z")


def _window_id_of(tmux_target: str) -> str:
    """The `@N` half of a `session:@id` target, or `""` when the target does not name a window id.

    Empty means "do not watch this session's window" — degrades to no liveness key rather than a bogus one."""
    tail = tmux_target.rsplit(":", 1)[-1]
    return tail if _VIEW_ID_RE.match(tail) else ""


# ── The designed layout, drawn BY TMUX rather than by the browser ───────────────────────────────────

# `&layout=<spec>`: ONE PTY on a tmux window whose PANES are the tiles. Nothing is moved (`join-pane`
# would mutate `fleet` and re-mint its `@id`s): each pane runs a NESTED tmux client (`env -u TMUX tmux
# -S <socket> attach -t =_view_<token>`) on a grouped session, so a tile gets tmux's own window list and
# click-to-switch (`mouse on` at both layers). `prefix None` on the outer session too. The spec is a
# STRING (one query parameter), mirroring `apps/web/src/lib/consoleLayout.ts`'s `LayoutModel`:
#   node    := tile | split
#   tile    := '@' <digits>                    a fleet window id, the same shape `view` accepts
#   split   := ('r'|'c') <percent> '(' node ',' node ')'
#   'r'     puts the two children SIDE BY SIDE (tmux `split-window -h`), 'c' STACKS them (`-v`)
#   percent is the FIRST child's share of the rectangle, 10..90
_LAYOUT_KEY_PREFIX = "layout:"
# `view=vm:<vm_id>`: a MicroVM session's console — the VM's own terminal through `jaah-microvmd`, never tmux
# (the MicroVM design's D18). The CP names the VM; `microvmd.valid_vm_id` is the shape check.
_VM_KEY_PREFIX = "vm:"
# Shares the `_view_` prefix so every reader that drops ephemeral rows drops these too.
EPHEMERAL_LAYOUT_SESSION_PREFIX = EPHEMERAL_VIEW_SESSION_PREFIX + "lay"
# Named, not indexed: `base-index` is a user option.
_LAYOUT_WINDOW = "grid"
# Bounds the tmux work and nested clients one open can cost.
_LAYOUT_MAX_TILES = 8
# The extremes are excluded: tmux clamps pane minimums, so the render would not match the model.
_LAYOUT_MIN_PERCENT, _LAYOUT_MAX_PERCENT = 10, 90
# Shape gate before the parser; the length bound stops a pathological nest (8 tiles need ~90 chars).
_LAYOUT_SPEC_RE = re.compile(r"\A[@0-9rc(),]{1,300}\Z")


@dataclass(frozen=True)
class _LayoutTile:
    """One leaf of a designed layout: the fleet window this tile shows, as a tmux `@id`."""

    window: str


@dataclass(frozen=True)
class _LayoutSplit:
    """One division of a rectangle in two. `horizontal` is tmux's `-h` — children SIDE BY SIDE — and
    `percent` is always the FIRST child's share, as `LayoutModel.ratio` reads in the browser; the build
    inverts it for tmux's `-l` in exactly one place."""

    horizontal: bool
    percent: int
    first: "_LayoutNode"
    second: "_LayoutNode"


_LayoutNode = _LayoutTile | _LayoutSplit


def parse_layout_spec(spec: str) -> tuple[_LayoutNode | None, str | None]:
    """(tree, error) — the layout `spec` as a tile tree, or why this daemon will not build it.

    Total; every rejection is user-facing. Pure — whether the windows still EXIST is `_open_layout_pty`'s."""
    if not _LAYOUT_SPEC_RE.match(spec):
        return None, f"invalid layout {spec[:80]!r} — expected a spec like r50(@3,@7)"
    try:
        node, index = _parse_layout_node(spec, 0)
    except ValueError as exc:
        return None, str(exc)
    if index != len(spec):
        return None, f"invalid layout — trailing {spec[index:index + 20]!r} after a complete tree"
    tiles = layout_windows(node)
    if len(tiles) > _LAYOUT_MAX_TILES:
        return None, f"layout has {len(tiles)} tiles — this node builds at most {_LAYOUT_MAX_TILES}"
    if len(set(tiles)) != len(tiles):
        # Two tiles on one window would be shrunk to the smaller pane; the designer cannot author it.
        return None, "layout names the same window twice — one window can only be shown in one tile"
    return node, None


def _parse_layout_node(spec: str, index: int) -> tuple[_LayoutNode, int]:
    """One node of a layout spec starting at `index`, and the index just past it. Raises `ValueError`
    carrying the user-facing message."""
    if index >= len(spec):
        raise ValueError("invalid layout — a tile or split was expected but the spec ended")
    char = spec[index]
    if char == "@":
        end = index + 1
        while end < len(spec) and spec[end].isdigit():
            end += 1
        window = spec[index:end]
        if not _VIEW_ID_RE.match(window):
            raise ValueError(f"invalid layout — {window!r} is not a window id like @7")
        return _LayoutTile(window=window), end
    if char not in ("r", "c"):
        raise ValueError(f"invalid layout — expected a window id or a split at {spec[index:index + 8]!r}")
    end = index + 1
    while end < len(spec) and spec[end].isdigit():
        end += 1
    digits = spec[index + 1:end]
    if not digits:
        raise ValueError(f"invalid layout — the split at {spec[index:index + 8]!r} carries no percentage")
    percent = int(digits)
    if not _LAYOUT_MIN_PERCENT <= percent <= _LAYOUT_MAX_PERCENT:
        raise ValueError(
            f"invalid layout — a split of {percent}% is outside "
            f"{_LAYOUT_MIN_PERCENT}–{_LAYOUT_MAX_PERCENT}%",
        )
    if end >= len(spec) or spec[end] != "(":
        raise ValueError(f"invalid layout — expected '(' after {spec[index:end]!r}")
    first, end = _parse_layout_node(spec, end + 1)
    if end >= len(spec) or spec[end] != ",":
        raise ValueError(f"invalid layout — expected ',' between the two halves of {spec[index:end]!r}")
    second, end = _parse_layout_node(spec, end + 1)
    if end >= len(spec) or spec[end] != ")":
        raise ValueError(f"invalid layout — expected ')' to close {spec[index:end]!r}")
    return _LayoutSplit(horizontal=char == "r", percent=percent, first=first, second=second), end + 1


def layout_windows(node: _LayoutNode) -> list[str]:
    """Every window a layout shows, in TILE ORDER — first-then-second, depth first.

    Load-bearing: the order panes, `_build_layout_panes` and the per-tile grouped sessions follow."""
    if isinstance(node, _LayoutTile):
        return [node.window]
    return layout_windows(node.first) + layout_windows(node.second)


def _fleet_window_socket(view: str) -> tuple[str | None, str | None]:
    """(socket, error) — WHICH tmux socket hosts fleet window `view`, or why we will not attach to it.

    Resolve-only (a missing window is definitive). Reads the FLEET socket only: `@ids` are per-server,
    so during a drain another server's `@7` would be a silent wrong-window attach. `_holder` (the
    `sleep infinity` that holds the server up) is refused. Blocking."""
    return _fleet_windows_socket([view])


def _fleet_windows_socket(views: list[str]) -> tuple[str | None, str | None]:
    """(socket, error) — the socket hosting EVERY window in `views`, or the first reason one is refused.

    ONE LISTING for the whole set, so answers cannot disagree and the build cannot half-succeed;
    all-or-nothing, first failure wins."""
    for view in views:
        if not _VIEW_ID_RE.match(view):
            return None, f"invalid window id {view!r} — expected a tmux window id like @7"
    missing = _missing_binary_error("tmux")
    if missing is not None:
        return None, missing
    fleet_session = fleet_launch.tmux_session()
    listings, query_failed = _tmux_listing_by_socket(
        "list-windows", "-a", "-F", "#{window_id}\t#{session_name}\t#{window_name}",
        sockets=[session_user.fleet_socket()],
    )
    # (socket, window_name) per window id of this node's fleet session, from one instant's listing.
    found: dict[str, tuple[str, str]] = {}
    for socket, stdout in listings:
        for line in stdout.splitlines():
            parts = line.split("\t", 2)
            if len(parts) != 3:
                continue
            window_id, session_name, window_name = parts
            if session_name != fleet_session:
                continue
            found.setdefault(window_id, (socket, window_name))
    resolved: str | None = None
    for view in views:
        match = found.get(view)
        if match is None:
            if query_failed:
                # COULD NOT LOOK ≠ NOT THERE: retryable to the browser.
                return None, INDETERMINATE_ERROR
            return None, f"no window {view} on this node's {fleet_session} session"
        socket, window_name = match
        if window_name == HOLDER_WINDOW:
            return None, f"{view} is the fleet server's holder window and is not attachable"
        resolved = socket
    if resolved is None:
        # Unreachable through either caller; spelled out so a `None` can never escape into an argv.
        return None, "no windows requested"
    return resolved, None


def _console_window_socket(target: str) -> str | None:
    """WHICH tmux socket holds the console target `session:@id`, or None when no answer is safe to act on.

    A GROUPED attach needs the socket (a plain one tries `manage_sockets()` in order): during an
    in-place-upgrade drain both servers host a session named for the node, and grouping on the wrong one
    shows that server's window under this slot's name. Same preference order as the plain `||` chain, so
    isolation never lands on a different window. `None` means attach the plain way — an unnameable socket
    costs the isolation, never the console. Blocking — executor only."""
    base, window = target.rsplit(":", 1)[0], _window_id_of(target)
    if not base or not window:
        return None
    listings, _query_failed = _tmux_listing_by_socket(
        "list-windows", "-a", "-F", "#{session_name}:#{window_id}",
    )
    for socket, stdout in listings:
        if any(line.strip() == target for line in stdout.splitlines()):
            return socket
    return None


def _kill_view_session(socket: str, name: str) -> bool:
    """Kill ONE ephemeral `_view_*` grouped session. True when tmux reported it gone.

    The ONE exemption from detach-never-kill here: a `_view_*` session only points at windows `fleet`
    owns, so killing it costs one viewport. The prefix test is a hard refusal — wrong, it takes the node."""
    if not is_ephemeral_view_session(name):
        log.error("console: refusing to kill %r — the view reaper only ever kills a %s* SESSION",
                  name, EPHEMERAL_VIEW_SESSION_PREFIX)
        return False
    result = _console_run(*session_user.tmux_cmd(socket, "kill-session", "-t", f"={name}"), timeout=10)
    if result["exit_code"] != 0:
        # An already-gone session is the common teardown case; debug only.
        log.debug("console: kill-session %s on %s exited %s: %s",
                  name, socket, result["exit_code"], result["stderr"].strip())
        return False
    return True


def _create_grouped_session(
    socket: str, base: str, name: str, window: str, *, label: str,
    fatal_options: tuple[tuple[str, str], ...] = (),
    best_effort_options: tuple[tuple[str, str], ...] = (),
) -> tuple[str | None, str | None, str | None]:
    """(session_name, attach_target, error) — a second session over `base`'s windows pointed at `window`:
    THE ONE MECHANISM behind every grouped attach, a tile's and a slot console's alike.

    Why isolate: clients of ONE tmux session share a current-window pointer AND (under `window-size
    latest`) the window size, so every keypress in one client resizes the shared window and forces a
    full-screen repaint (measured 30-45 KB per keypress) onto every other viewer.

    `name` is chosen by the caller so it can be reserved against `_ViewSessionReaper` before the first
    tmux command. Every command is additive or session-scoped, and targets are always session-qualified
    (`_view_x:@7`): `@7` is now in TWO sessions. `fatal_options` fail the create; `best_effort_options`
    only warn. Blocking — executor only."""
    created = _console_run(
        # `=base`: tmux falls back to prefix/fnmatch matching, which could group onto a different session.
        *session_user.tmux_cmd(socket, "new-session", "-d", "-t", f"={base}", "-s", name), timeout=10,
    )
    if created["exit_code"] != 0:
        if (
            session_user.switch_needed()
            and socket == session_user.fleet_socket()
            and not _tmux_server_answered(created)
        ):
            # Keep the retryable split: the window resolved, so a runner-unit restart in between exits 1
            # with a real code `_create_failure` would read as definitive, for a `Restart=always` unit.
            created = _fleet_server_refusal(
                socket, f"new-session reached no live server: {created['stderr'].strip() or '(no stderr)'}",
            )
        return None, None, _create_failure(
            created, f"could not open {label}", what="tmux new-session -t",
        )
    for option, value in fatal_options:
        result = _console_run(
            *session_user.tmux_cmd(socket, "set-option", "-t", name, option, value), timeout=10,
        )
        if result["exit_code"] != 0:
            _kill_view_session(socket, name)
            return None, None, _create_failure(
                result, f"could not set {option} for {label}", what=f"tmux set-option {option}",
            )
    for option, value in best_effort_options:
        applied = _console_run(
            *session_user.tmux_cmd(socket, "set-option", "-t", name, option, value), timeout=10,
        )
        if applied["exit_code"] != 0:
            log.warning(
                "console: could not set %s on %s (%s) — the attach proceeds without it; what each one "
                "costs is at `_TILE_OPTIONS` / `_CONSOLE_OPTIONS`",
                option, name, applied["stderr"].strip() or "no stderr",
            )
    target = f"{name}:{window}"
    selected = _console_run(*session_user.tmux_cmd(socket, "select-window", "-t", target), timeout=10)
    if selected["exit_code"] != 0:
        _kill_view_session(socket, name)
        # The error names the window, so a multi-tile apply can report which one vanished.
        return None, None, _create_failure(
            selected, f"could not select window {window} — it may have just closed",
            what="tmux select-window",
        )
    return name, target, None


# A TILE's presentation. The prefix is FATAL: a working `C-b n` would re-point the tile's session.
_TILE_FATAL_OPTIONS = (("prefix", "None"), ("prefix2", "None"))

# THE DRIFT CHANNEL, best-effort: a grouped session's pointer can MOVE (window killed, renamed,
# `switch-client`), and `set-titles` pushes `#{window_id} #{window_name}` as OSC 0 on the PTY stream the
# browser already reads (`@<digits>` first: unambiguous). Not fatal — a missing title costs the signal
# only. `status-left` is blanked: the default prints the meaningless `_view_<hex>` over the window list.
_TILE_OPTIONS = (
    ("set-titles", "on"),
    ("set-titles-string", "#{window_id} #{window_name}"),
    ("status-left", ""),
)

# A SLOT CONSOLE's: the prefix STAYS (a full terminal; on its own session `C-b` moves only this viewer),
# `set-titles` stays off (the browser labels a console from its slot), `status-left` blanked as above.
_CONSOLE_OPTIONS = (("status-left", ""),)


def _create_view_session(
    socket: str, view: str, name: str, *, mouse: bool = False,
) -> tuple[str | None, str | None, str | None]:
    """The grouped session ONE TILE attaches through: the mechanism above plus a tile's own options.

    Always over this node's DEFAULT fleet session. `mouse` is the layout path's only. No
    `resize-window`/`refresh-client`/`window-size`: that would resize for every viewer on `fleet`."""
    return _create_grouped_session(
        socket, fleet_launch.tmux_session(), name, view, label=f"a view of {view}",
        fatal_options=_TILE_FATAL_OPTIONS,
        best_effort_options=_TILE_OPTIONS + ((("mouse", "on"),) if mouse else ()),
    )


def _create_console_session(
    socket: str, base: str, name: str, window: str,
) -> tuple[str | None, str | None, str | None]:
    """The grouped session ONE SLOT CONSOLE attaches through — see `_CONSOLE_OPTIONS` for what it does
    NOT set, and `_create_grouped_session` for what being on its own session buys the viewer.

    `base` is the session the target RESOLVED to, never the node default: a CP-named session can have a
    pre-switch copy on the daemon's own server, and a default base would attach a different window."""
    return _create_grouped_session(
        socket, base, name, window, label=f"an isolated attach to {window}",
        best_effort_options=_CONSOLE_OPTIONS,
    )


def _layout_pane_command(socket: str, tile_session: str) -> str:
    """The shell command one tile's PANE runs: a nested tmux client on that tile's grouped session.

    `env -u TMUX` is not optional: tmux refuses `attach` when `$TMUX` is set, which it is in every pane."""
    return shlex.join(["env", "-u", "TMUX", *session_user.tmux_cmd(socket, "attach", "-t", f"={tile_session}")])


def _create_layout_session(
    socket: str, tree: _LayoutNode, tile_sessions: dict[str, str], name: str, *, rows: int, cols: int,
) -> tuple[str | None, str | None]:
    """(attach_target, error) — build the tmux window whose PANES are this layout's tiles.

    Nothing here touches `fleet`. `-l <n>%` sizes the pane tmux CREATES (the second child), so the model's
    first-child share is inverted exactly here. `remain-on-exit on` guards the layout: an exited pane
    would otherwise be removed and tmux would re-flow the grid. Blocking — executor only."""
    windows = layout_windows(tree)
    first = _console_run(
        *session_user.tmux_cmd(
            socket, "new-session", "-d", "-s", name, "-n", _LAYOUT_WINDOW,
            "-x", str(cols), "-y", str(rows), "-P", "-F", "#{pane_id}",
            _layout_pane_command(socket, tile_sessions[windows[0]]),
        ),
        timeout=10,
    )
    if first["exit_code"] != 0:
        if (
            session_user.switch_needed()
            and socket == session_user.fleet_socket()
            and not _tmux_server_answered(first)
        ):
            # The same retryable split as `_create_grouped_session`: the windows resolved a moment ago.
            first = _fleet_server_refusal(
                socket, f"new-session reached no live server: {first['stderr'].strip() or '(no stderr)'}",
            )
        return None, _create_failure(first, "could not open the layout grid", what="tmux new-session")
    root_pane = first["stdout"].strip()
    if not root_pane:
        # Success with no `-P -F` pane id: nothing can be split, so a create failure.
        _kill_view_session(socket, name)
        return None, "tmux reported the layout grid was created but named no pane"

    for option, value, fatal in (
        # Fatal: a live prefix would let `C-b "` split a tile into something no stored layout describes.
        ("prefix", "None", True),
        ("prefix2", "None", True),
        # The outer status bar would print the session token over the grid; tiles carry their own.
        ("status", "off", False),
        # Mouse at both layers: the outer selects the tile and forwards (`send-keys -M`) to the inner.
        ("mouse", "on", False),
        ("remain-on-exit", "on", False),
    ):
        result = _console_run(
            *session_user.tmux_cmd(socket, "set-option", "-t", name, option, value), timeout=10,
        )
        if result["exit_code"] == 0:
            continue
        if fatal:
            _kill_view_session(socket, name)
            return None, _create_failure(
                result, "could not neutralise the prefix for the layout grid",
                what=f"tmux set-option {option}",
            )
        log.warning(
            "console: could not set %s on layout %s (%s) — the grid renders without it",
            option, name, result["stderr"].strip() or "no stderr",
        )

    error = _build_layout_panes(socket, tree, root_pane, tile_sessions)
    if error is not None:
        _kill_view_session(socket, name)
        return None, error

    # The first tile takes the caret (the browser's default). Best-effort.
    selected = _console_run(
        *session_user.tmux_cmd(socket, "select-pane", "-t", root_pane), timeout=10,
    )
    if selected["exit_code"] != 0:
        log.warning(
            "console: could not select the first tile of layout %s (%s)",
            name, selected["stderr"].strip() or "no stderr",
        )
    return f"{name}:{_LAYOUT_WINDOW}", None


def _build_layout_panes(
    socket: str, node: _LayoutNode, pane: str, tile_sessions: dict[str, str],
) -> str | None:
    """Split `pane` — which currently occupies exactly `node`'s rectangle — until the tree is realized.
    Returns None on success, or the error to report.

    Invariant: on entry `pane` covers `node`'s rectangle and already runs its first tile's client, so
    the split is emitted before either recursion. A half-way failure is not partially applied."""
    if isinstance(node, _LayoutTile):
        return None
    window = layout_windows(node.second)[0]
    created = _console_run(
        *session_user.tmux_cmd(
            socket, "split-window", "-t", pane,
            "-h" if node.horizontal else "-v",
            "-l", f"{100 - node.percent}%", "-d", "-P", "-F", "#{pane_id}",
            _layout_pane_command(socket, tile_sessions[window]),
        ),
        timeout=10,
    )
    if created["exit_code"] != 0:
        return _create_failure(
            created, f"could not place the tile showing {window}", what="tmux split-window",
        )
    new_pane = created["stdout"].strip()
    if not new_pane:
        return f"tmux split the grid for {window} but named no pane"
    error = _build_layout_panes(socket, node.first, pane, tile_sessions)
    if error is not None:
        return error
    return _build_layout_panes(socket, node.second, new_pane, tile_sessions)


class _ViewSessionReaper:
    """Crash recovery for ephemeral `_view_*` sessions: kills the ones no client is attached to.

    IT NEEDS A RESERVATION: a session between its `new-session` and its `tmux attach` HAS ZERO CLIENTS,
    which is the sweep's own kill criterion, so the reaper would otherwise kill a session a dial is still
    setting up. A refcount, so a double reservation survives the first release. `sweep()` BLOCKS —
    executor only, hence the lock on `_reserved`."""

    def __init__(self, grace: float = _VIEW_SESSION_NO_CLIENT_GRACE_SECONDS) -> None:
        self._grace = grace
        self._reserved: dict[str, int] = {}
        self._lock = threading.Lock()
        # session name → when it was first SEEN with no client. No lock: only `sweep` touches it and it
        # never runs concurrently with itself. `_reserved` needs one — loop writer, executor reader.
        self._clientless_since: dict[str, float] = {}

    @contextlib.contextmanager
    def reserved(self, name: str):
        """Hold `name` against the sweep for the duration of the block. See the class docstring."""
        with self._lock:
            self._reserved[name] = self._reserved.get(name, 0) + 1
        try:
            yield
        finally:
            with self._lock:
                count = self._reserved.get(name, 0) - 1
                if count > 0:
                    self._reserved[name] = count
                else:
                    self._reserved.pop(name, None)

    def is_reserved(self, name: str) -> bool:
        with self._lock:
            return name in self._reserved

    def sweep(self, *, ignore_grace: bool = False) -> list[str]:
        """Kill every unreserved, client-less `_view_*` session past the grace floor; return their names.

        BLOCKING — executor only. `ignore_grace` is for the START-UP sweep, where the tiles' sockets died
        with the previous daemon and no reservation can exist yet. Fail-closed: only an
        `#{session_attached}` of exactly "0" counts as client-less."""
        with self._lock:
            reserved = set(self._reserved)
        now = time.monotonic()
        killed: list[str] = []
        seen: set[str] = set()
        for socket in session_user.manage_sockets():
            result = _console_run(
                *session_user.tmux_cmd(socket, "list-sessions", "-F", "#{session_name}\t#{session_attached}"),
                timeout=5, stdout_limit=None,
            )
            if result["exit_code"] != 0:
                # No server, or unreadable: skip, never guess.
                continue
            for line in result["stdout"].splitlines():
                parts = line.split("\t", 1)
                if len(parts) != 2 or not is_ephemeral_view_session(parts[0]):
                    continue
                name, attached = parts[0], parts[1].strip()
                seen.add(name)
                # A console relay (Windows) has no client by design: its own client is nested in its pane.
                if name in reserved or attached != "0" or console_pty.is_live_relay(name):
                    self._clientless_since.pop(name, None)
                    continue
                since = self._clientless_since.setdefault(name, now)
                if not ignore_grace and now - since < self._grace:
                    continue
                # Re-checked at the kill: a dial can reserve after the snapshot.
                if self.is_reserved(name):
                    self._clientless_since.pop(name, None)
                    continue
                if _kill_view_session(socket, name):
                    killed.append(name)
                    self._clientless_since.pop(name, None)
        for gone in set(self._clientless_since) - seen:
            self._clientless_since.pop(gone, None)
        if killed:
            log.info("console: reaped %d abandoned view session(s): %s", len(killed), ", ".join(killed))
        return killed


def _attach_command(tmux_target: str, socket: str | None = None, read_only: bool = False) -> str:
    """The bash the PTY child execs: `tmux attach -t <target>`, with the SOCKET named explicitly, tried
    across `manage_sockets()` in order until one attaches.

    `read_only` adds tmux's own `-r`: that client may not send keys AND is excluded from smallest-client
    sizing. `socket` PINS IT TO ONE, skipping the `||` chain, because the view path already resolved which
    socket holds the window. Resolving it in the daemon would park the event loop in a subprocess."""
    sockets = [socket] if socket is not None else session_user.manage_sockets()
    flags = ("-r",) if read_only else ()
    attaches = [
        shlex.join(session_user.tmux_cmd(each, "attach", *flags, "-t", tmux_target))
        for each in sockets
    ]
    return " || ".join([*attaches[:-1], f"exec {attaches[-1]}"])


def _spawn_console_pty(
    tmux_target: str, rows: int, cols: int, socket: str | None = None, read_only: bool = False,
) -> tuple[int, int]:
    """Fork a child running `tmux attach -t <tmux_target>` on a new PTY. Returns (pid, master_fd).

    `pty.fork` is what gives `_close_session` an fd to close and a pid to `waitpid` — the mechanics
    detach-never-kill is built on — so the attaching CLIENT runs as `jaahd` while everything behind the
    attach was forked by the `jaah`-owned server. `_start_stdout_pump` owns the blocking mode.

    Windows has no pty: `_spawn_console_conpty` returns `(pid, -1)` and parks the attach for `_Session`; the
    attach client itself runs in a relay pane of the fleet server (`console_pty.TmuxRelay`)."""
    if host_platform.is_windows():
        return _spawn_console_conpty(tmux_target, rows, cols, socket, read_only)
    pid, master_fd = pty.fork()
    if pid == 0:   # child
        env = _tmux_safe_env()
        env.setdefault("TERM", "xterm-256color")
        os.execvpe("bash", ["bash", "-lc", _attach_command(tmux_target, socket, read_only)], env)
        os._exit(127)   # pragma: no cover — execvpe only returns on failure
    _set_winsize(master_fd, rows, cols)
    return pid, master_fd


def _spawn_vm_console_pty(vm_id: str, rows: int, cols: int, read_only: bool = False) -> tuple[int, int]:
    """`_spawn_console_pty` for a MicroVM session: the same `pty.fork`, but the child runs the
    `microvm_console` relay instead of `tmux attach`. Everything downstream — the pump, the viewers,
    `_close_session`'s detach-never-kill — is the static path's. `_set_winsize` after the fork is what
    sends the child its first SIGWINCH, which the relay forwards to the guest as microvmd `resize`."""
    microvm_console = cloud_modules.load("microvm_console")  # Linux-only machinery, off the macOS/Windows import graph

    argv = microvm_console.relay_argv(vm_id, read_only)
    pid, master_fd = pty.fork()
    if pid == 0:   # child
        os.execv(argv[0], argv)
        os._exit(127)   # pragma: no cover — execv only returns on failure
    _set_winsize(master_fd, rows, cols)
    return pid, master_fd


async def _spawn_console(
    tmux_target: str, rows: int, cols: int, socket: str | None = None, read_only: bool = False,
) -> tuple[int, int]:
    """`_spawn_console_pty` from async code. POSIX: inline, as ever (`pty.fork`). Windows: in an executor —
    the relay runs several tmux processes to set up, which must not park the loop. A spawn whose
    caller is cancelled meanwhile is discarded once it lands, so no live relay is left parked."""
    if not host_platform.is_windows():
        return _spawn_console_pty(tmux_target, rows, cols, socket, read_only=read_only)
    future = asyncio.get_running_loop().run_in_executor(
        None, functools.partial(_spawn_console_pty, tmux_target, rows, cols, socket, read_only=read_only))
    try:
        return await asyncio.shield(future)
    except asyncio.CancelledError:
        def _discard(done) -> None:
            if not done.cancelled() and done.exception() is None:
                asyncio.ensure_future(_discard_pending_conpty(done.result()[0]))
        future.add_done_callback(_discard)
        raise


# A spawned Windows attach, by pid, until the `_Session` built from that `(pid, -1)` claims it in
# `__post_init__`. A side table rather than a wider return so the four spawn callers (and their Linux
# lines) stay exactly as they are. A spawn whose caller fails before its `_Session` exists is popped and
# closed by `_discard_pending_conpty`, so nothing is left parked here.
_PENDING_CONPTY: dict[int, console_pty.ConPtyAttach] = {}


def _spawn_console_conpty(
    tmux_target: str, rows: int, cols: int, socket: str | None, read_only: bool,
) -> tuple[int, int]:
    """Windows `_spawn_console_pty`: the tmux attach client behind a relay (`console_pty`), `(pid, -1)`.

    argv directly, no `bash -lc … || …` chain: Windows has one fleet socket, so the chain's
    fallback has nothing to fall back to. `_tmux_safe_env` passes PATH through, which is what keeps Git's
    `usr\\bin` (tmux.exe's msys runtime) reachable."""
    sock = socket if socket is not None else session_user.manage_sockets()[0]
    flags = ("-r",) if read_only else ()
    env = _tmux_safe_env()
    env.setdefault("TERM", "xterm-256color")
    attach = console_pty.ConPtyAttach.spawn(
        session_user.tmux_cmd(sock, "attach", *flags, "-t", tmux_target), rows, cols, env,
        max_pending=_STDIN_MAX_PENDING_BYTES,
    )
    _PENDING_CONPTY[attach.pid] = attach
    return attach.pid, -1


def _spawn_windows_vm_console(vm_id: str, rows: int, cols: int, read_only: bool) -> tuple[int, int]:
    """`_spawn_vm_console_pty` on Windows: a relay pane of the fleet server (`console_pty`) — the runner
    account owns the WSL distro — running `microvm_console attach` in the MicroVM backend through `wsl.exe`."""
    from host_platform import windows  # noqa: PLC0415 — only this Windows-gated path needs it
    microvm_bridge = cloud_modules.load("microvm_bridge")  # the cloud edition's; the caller checked it is here

    sock = session_user.manage_sockets()[0]
    env = _tmux_safe_env()
    env.setdefault("TERM", "xterm-256color")
    command = ["env", *(f"{k}={v}" for k, v in windows.WSL_ENV.items()), console_pty._msys(windows.wsl_exe()),
               *microvm_bridge.console_argv(vm_id, read_only)]
    attach = console_pty.ConPtyAttach.spawn(
        [*session_user.tmux_cmd(sock), console_pty.RUN, *command], rows, cols, env,
        max_pending=_STDIN_MAX_PENDING_BYTES,
    )
    _PENDING_CONPTY[attach.pid] = attach
    return attach.pid, -1


async def _discard_pending_conpty(pid: int | None) -> None:
    """A spawn caller's failure path: close the attach still parked under `pid`, if any. Whatever raises
    between the spawn and its `_Session` would otherwise leak the entry AND its live pywinpty client. A
    no-op off Windows (nothing parks) and once a `_Session` claimed it. `close` blocks, so an executor."""
    attach = _PENDING_CONPTY.pop(pid, None) if pid is not None else None
    if attach is not None:
        await asyncio.get_event_loop().run_in_executor(None, attach.close)


# ── One viewer's own tmux client: redraw it, copy its pane ────────────────────────────────────────────

def _pty_client(pid: int, view_socket: str | None) -> tuple[str, str] | None:
    """(socket, client_tty) of the tmux client a console PTY runs, or None when none is found (a MicroVM
    relay, a client that already detached).

    The child's stdin IS the PTY slave, so on Linux its tty names the client exactly; elsewhere the client
    pid does (the attach `exec`s on a one-socket node). Every command aimed at the result is per-CLIENT, so
    no other viewer of the window is touched. Blocking — executor only."""
    tty = host_platform.stdin_tty(pid) or ""
    sockets = [view_socket] if view_socket is not None else session_user.manage_sockets()
    for socket in sockets:
        listing = _console_run(
            *session_user.tmux_cmd(socket, "list-clients", "-F", "#{client_pid} #{client_tty}"),
            timeout=10, stdout_limit=None,
        )
        if listing["exit_code"] != 0:
            continue
        for line in listing["stdout"].splitlines():
            client_pid, _, client_tty = line.partition(" ")
            if client_tty and (client_tty == tty or client_pid == str(pid)):
                return socket, client_tty
    return None


def _redraw_client(pid: int, view_socket: str | None) -> None:
    """Redraw ONE console client's whole screen once a resize has settled.

    tmux already repaints a client it resizes, and a headless replay of a drag (agy and Claude Code panes,
    with relay lag) ends with xterm equal to the pane — so this is the guarantee, not the fix: whatever a torn
    chunk or a renderer left on THIS viewer's screen is overwritten from tmux's own grid. `refresh-client`
    without `-C` resizes nothing. It cannot clear rows the CLI itself left in the pane (an inline redraw
    erasing too few lines after tmux re-wraps it); those are the pane's content. Blocking — executor only."""
    client = _pty_client(pid, view_socket)
    if client is None:
        return
    socket, tty = client
    result = _console_run(*session_user.tmux_cmd(socket, "refresh-client", "-t", tty), timeout=10)
    if result["exit_code"] != 0:
        log.debug("console: refresh-client %s exited %s: %s", tty, result["exit_code"], result["stderr"].strip())


def _tmux_bytes(socket: str, *args: str, stdin: bytes | None = None) -> subprocess.CompletedProcess | None:
    """`tmux` with BYTES in and out — pane text is UTF-8 whatever this daemon's locale says, which
    `_console_run`'s text mode would decode with. None when it could not run at all."""
    try:
        return subprocess.run(
            session_user.tmux_cmd(socket, *args), input=stdin, capture_output=True, timeout=30,
            env=_tmux_safe_env(),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("console: tmux %s could not run: %s", args[0], exc)
        return None


def _capture_pane_tail(socket: str, pane: str) -> bytes | None:
    """The pane's whole history as text, keeping only its last `_COPY_PANE_MAX_BYTES` (plus a line's slack)
    while reading — a wide pane's 50000 lines would otherwise be held whole before the cap. None on failure."""
    keep = _COPY_PANE_MAX_BYTES + 65536
    try:
        proc = subprocess.Popen(
            session_user.tmux_cmd(socket, "capture-pane", "-p", "-J", "-S", "-", "-t", pane),
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, env=_tmux_safe_env(),
        )
    except OSError as exc:
        log.warning("console: tmux capture-pane could not run: %s", exc)
        return None
    stdout = proc.stdout
    assert stdout is not None   # stdout=PIPE
    tail = bytearray()
    try:
        for chunk in iter(lambda: stdout.read(65536), b""):
            tail += chunk
            if len(tail) > 2 * keep:
                del tail[:-keep]
        returncode = proc.wait(timeout=30)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
        return None
    finally:
        stdout.close()
    return bytes(tail[-keep:]) if returncode == 0 else None


def _copy_pane_to_client(pid: int, view_socket: str | None, layout: bool) -> str | None:
    """Put the pane this console shows on its viewer's clipboard as plain text, history included. Returns
    why it could not, or None.

    `capture-pane -J -S -` reads the pane's GRID, joined where tmux wrapped it — readable where copy-mode
    history of an inline-redrawing CLI is not. `load-buffer -w -t <client>` has tmux send the text as OSC 52
    to that one client, in order on the stream the browser already reads (`set-clipboard on`), so nothing
    is spliced into the PTY output mid-sequence. A LAYOUT's active pane is a nested client: one hop down to
    the tile it shows. The buffer is deleted again; the clipboard keeps the copy. Blocking — executor only."""
    client = _pty_client(pid, view_socket)
    if client is None:
        return "no tmux client is attached to this console"
    socket, tty = client
    pane = _console_run(*session_user.tmux_cmd(socket, "display-message", "-p", "-c", tty, "#{pane_id}"),
                        timeout=10)["stdout"].strip()
    if pane and layout:
        tile_tty = _console_run(*session_user.tmux_cmd(socket, "display-message", "-p", "-t", pane, "#{pane_tty}"),
                                timeout=10)["stdout"].strip()
        pane = _console_run(*session_user.tmux_cmd(socket, "display-message", "-p", "-c", tile_tty, "#{pane_id}"),
                            timeout=10)["stdout"].strip() if tile_tty else ""
    if not pane:
        return "could not find the pane this console shows"
    captured = _capture_pane_tail(socket, pane)
    if captured is None:
        return "could not read the pane"
    text = captured.rstrip() + b"\n"
    if len(text) > _COPY_PANE_MAX_BYTES:
        text = text[-_COPY_PANE_MAX_BYTES:]
        text = text[text.find(b"\n") + 1:]   # start on a whole line
    buffer = f"_jaah_copy_{secrets.token_hex(4)}"
    loaded = _tmux_bytes(socket, "load-buffer", "-w", "-b", buffer, "-t", tty, "-", stdin=text)
    _tmux_bytes(socket, "delete-buffer", "-b", buffer)
    if loaded is None or loaded.returncode != 0:
        return "could not hand the text to the console"
    return None


@dataclass
class _Paste:
    """One pasted image being assembled out of a viewer's PASTE_DATA frames. `size` is what the browser
    DECLARED, and is the terminator: assembly completes at exactly that many bytes and fails past it."""

    size: int
    ext: str
    buf: bytearray = field(default_factory=bytearray)


@dataclass
class _Viewer:
    """One browser's leg: the dedicated WebSocket this daemon dialled to the CP for it, plus the task
    reading that socket. Liveness is the SOCKET's, which is why nothing here tracks staleness — a viewer
    that vanishes closes its connection and its read task removes it."""

    ws: object
    # Whether a BROWSER is on the other end of this socket rather than the CP relay. It selects the
    # close-code dialect (`_viewer_close_code`) and nothing else; everything downstream is identical.
    browser_facing: bool = False
    # Whether this viewer is ONE-WAY (`worktrees:view`). Per-VIEWER, because one console is routinely
    # open for a driver who may type and a watcher who may not. `_handle_stdin` enforces it.
    read_only: bool = False
    reader: asyncio.Task | None = None
    # The paste in flight on THIS socket. Per-viewer, because a session's viewers' frames interleave and
    # a shared buffer would splice two people's screenshots into one file.
    paste: _Paste | None = None


def _viewer_close_code(viewer: _Viewer, retryable: bool) -> int:
    """The close code to send THIS viewer for a verdict of `retryable`, in its own transport's dialect.

    One verdict, two vocabularies, computed once here so the two dialects cannot drift into disagreeing
    about whether a session may be retried."""
    if viewer.browser_facing:
        return _BROWSER_CLOSE_CODE_RECOVERABLE if retryable else _BROWSER_CLOSE_CODE_SESSION_ENDED
    return _CLOSE_CODE_RECOVERABLE if retryable else _CLOSE_CODE_SESSION_ENDED


@dataclass
class _Session:
    key: ConsoleKey
    master_fd: int
    pid: int
    # The tmux SESSION this PTY's window lives in — `None` for a view or layout. Recorded at create time
    # so a LATER open naming a DIFFERENT session is refused rather than served this one silently.
    tmux_session: str | None = None
    # The tmux `#{window_id}` this PTY attached to, its liveness key (`""` for a view or layout). Needed
    # because a `tmux attach` client is attached to a SESSION: kill the window and it silently moves to a
    # neighbour, streaming another worktree's terminal under this slot's name.
    tmux_window: str = ""
    # Every ephemeral `_view_*` session this PTY OWNS, and their socket (stored, not re-resolved). One for
    # a view or a grouped slot console (empty if it fell back to a plain attach); for a LAYOUT the grid
    # FIRST — killing it detaches the per-tile sessions — then one per tile. `_close_session` tears down all.
    owned_sessions: tuple[str, ...] = ()
    view_socket: str | None = None
    # Whether this PTY's client attached `-r`. Fixed at attach time and the PTY is shared, so a `-r`
    # session would swallow a later edit user's keys; the reverse is `_handle_stdin`'s drop.
    read_only: bool = False
    # Live viewers, `vid` → socket: output fans out to each; the last leaving detaches. The vid is a
    # handle, not a credential — possession of the CP-authenticated socket is the authorization.
    viewers: dict[str, _Viewer] = field(default_factory=dict)
    last_activity: float = field(default_factory=time.monotonic)
    # PTY output staging. `_on_readable` is a SYNC callback and cannot await, so it only appends here and
    # sets `out_wake`; the one pump task per session owns every send.
    out_buf: bytearray = field(default_factory=bytearray)
    out_wake: asyncio.Event | None = None
    out_pump: asyncio.Task | None = None
    # Stdin the PTY would not accept yet (EAGAIN — its input buffer is full because the app is not
    # reading). Drained by an `add_writer` callback; see `_write_to_pty`.
    in_buf: bytearray = field(default_factory=bytearray)
    in_writer_armed: bool = False
    # Windows only: the ConPTY attach standing in for `master_fd`/`pid` (`master_fd` is -1). None on POSIX.
    conpty: console_pty.ConPtyAttach | None = None
    # The pending post-resize redraw (`_schedule_redraw`); each resize replaces it, so one runs per settle.
    redraw_timer: asyncio.TimerHandle | None = None
    # "Copy pane text" throttle (`_COPY_PANE_MIN_INTERVAL_SECONDS`): one in flight, and when the last began.
    copy_in_flight: bool = False
    last_copy_at: float = 0.0

    def __post_init__(self) -> None:
        if self.master_fd == -1 and self.conpty is None:
            self.conpty = _PENDING_CONPTY.pop(self.pid, None)


async def _close_ws(ws, code: int, reason: str = "") -> None:
    """Close one viewer socket, carrying WHY in the close code — and the REASON when the caller has one.

    RFC 6455 caps the reason at 123 BYTES, and a longer one is a protocol error the library raises on
    rather than truncating, so encoding before slicing is what makes the cap hold for non-ASCII."""
    if reason:
        try:
            await ws.close(code=code, reason=reason.encode("utf-8")[:123].decode("utf-8", "ignore"))
            return
        except Exception as exc:   # noqa: BLE001 — the code still has to be delivered; see below
            # The code matters more than the reason: with no code the browser sees a retryable 1006.
            log.warning("console: could not deliver a close reason (%s); closing with %d alone", exc, code)
    try:
        # The kwarg is omitted, not passed empty: the `except` would swallow a TypeError silently.
        await ws.close(code=code)
    except Exception:
        pass


def console_stream_url(raw: object) -> str | None:
    """The `console_open` frame's `stream_url` as this daemon may dial it: a `ws://`/`wss://` URL, or None
    (absent, or anything else) to dial `resolve_console_stream_url()` instead."""
    if isinstance(raw, str) and raw.startswith(("ws://", "wss://")) and "?" not in raw:
        return raw
    return None


def resolve_console_stream_url() -> str:
    """The CP endpoint this daemon dials for one console PTY. Derived from the SAME vars the node bus
    resolves its own URL from (`node_bus.resolve_cp_ws_url`), so console and the bus can never end up
    pointed at different control planes."""
    import node_bus

    return node_bus.resolve_cp_ws_url().replace(node_bus.WS_PATH, CONSOLE_STREAM_PATH)


def wt_key(project: str, number: object) -> str:
    return f"{project}-{number}"


def _prune_paste_dir(directory: str) -> None:
    """Best-effort sweep of pasted images older than `_PASTE_PRUNE_AFTER_SECONDS`.

    Every failure is swallowed, and the interesting one is EACCES: the directory belongs to the SESSION
    USER while this runs as the daemon, so `/tmp`'s sticky bit denies the unlink and the files age out
    with the host's own cleaning instead.

    NEVER on Windows: the paste root is under `run\\`, where the runner account holds Modify and may shape
    the tree with junctions (no privilege needed), so a daemon-side unlink there would delete with the
    daemon's rights wherever the runner points it. Pastes age out with the run tree instead."""
    if host_platform.is_windows():
        return
    cutoff = time.time() - _PASTE_PRUNE_AFTER_SECONDS
    try:
        entries = list(os.scandir(directory))
    except OSError:
        return
    for entry in entries:
        try:
            if entry.is_file(follow_symlinks=False) and entry.stat().st_mtime < cutoff:
                os.unlink(entry.path)
        except OSError:
            continue


class _ConsoleOpenError(Exception):
    """A failure to establish a console, carrying the exact reply its caller must send back.

    Establishment is split across `_resolve_session` and `_attach_viewer` so a second transport can
    reuse it; `as_reply` rebuilds the reply dict, including whether `retryable` is present at all."""

    def __init__(self, error: str, *, retryable: bool = False) -> None:
        super().__init__(error)
        self.error = error
        self.retryable = retryable

    def as_reply(self) -> dict:
        reply: dict = {"error": self.error}
        if self.retryable:
            reply["retryable"] = True
        return reply


class ConsoleBridge:
    """Bridges each viewer's dedicated CP socket to a per-(project, number, kind) tmux-attach PTY — see
    the module docstring for the wire protocol. Holds no listener: consoles arrive as `console_open`
    frames the node bus routes to `handle_console_open`."""

    def __init__(
        self,
        node_id: str,
        repo_root: Path,
        idle_timeout: float = CONSOLE_IDLE_TIMEOUT_SECONDS,
        stream_url: str | None = None,
    ) -> None:
        self._node_id = node_id
        # Resolved per dial rather than pinned here, so a CP address change takes effect without a
        # restart. An explicit value is for tests pointing at a local stand-in CP.
        self._stream_url = stream_url
        self._repo_root = repo_root
        self._idle_timeout = idle_timeout
        self._sessions: dict[ConsoleKey, _Session] = {}
        # (project, number, kind) → connects mid-handshake, reserved the instant the token gate passes
        # and before the ~35s resolve-or-create, so the reconciler's DONE-close cannot kill a window being
        # created. A refcount, so peers cannot clear each other's.
        self._opening: dict[SlotKey, int] = {}
        self._async_locks: dict[ConsoleKey, asyncio.Lock] = {}
        self._sweep_task: asyncio.Task | None = None
        # Owns the ephemeral `_view_*` grouped sessions: the create→attach reservation, and the crash
        # recovery sweep. See `_ViewSessionReaper` — the normal teardown is `_close_session`, not this.
        self._view_reaper = _ViewSessionReaper()
        self._last_view_sweep = 0.0
        # In-flight paste and per-client tmux tasks: asyncio holds weak refs, and a bare `ensure_future` fails
        # silently at GC.
        self._paste_tasks: set[asyncio.Task] = set()

    async def _dial_cp(self, ticket: str, stream_url: str | None = None):
        """Open this viewer's dedicated socket to the CP, presenting the ticket that pairs it with the
        waiting browser relay. Returns the connection, or raises — the caller turns a raise into a
        `retryable` error, since a CP we could not reach says nothing about whether the PTY is fine."""
        from websockets.asyncio.client import connect

        # Refused HERE, naming the cause: a node id is ASSIGNED, never derived, so `$NODE_ID` unset gives
        # "", and the CP would answer the empty header with the same generic 4404 "unknown ticket" as an
        # expired ticket — sending the operator to the wrong place. This message reaches the operator.
        if not self._node_id:
            raise RuntimeError(
                "this daemon has no node id, so Console cannot dial the control plane: NODE_ID is unset "
                "in jaahd's environment and a node id is assigned, never derived. Set NODE_ID to this "
                "node's assigned id and restart jaahd."
            )

        # A locally configured URL first, then the frame's `stream_url` — a CP whose relay is not at its own
        # hostname names it (a local stack), on the authenticated bus frame from the CP this daemon trusts.
        base = self._stream_url or stream_url or resolve_console_stream_url()
        url = f"{base}?ticket={urllib.parse.quote(ticket)}"
        # Token in a header (query strings land in access logs); the single-use ticket may ride the query.
        # The node id is REQUIRED: the CP binds each ticket to its node and refuses any other (the token
        # proves only "a fleet node"). `_NODE_ID_HEADER` must match `console_relay/tickets.py`; a test pins
        # it. `compression=None`: no console hop negotiates permessage-deflate — PTY bytes barely compress
        # and a stateful deflate context serializes every frame.
        return await asyncio.wait_for(
            connect(url, additional_headers={
                "X-Agent-Token": _expected_token(),
                _NODE_ID_HEADER: self._node_id,
            }, compression=None),
            timeout=_STREAM_DIAL_TIMEOUT,
        )

    async def start(self) -> None:
        """Only the idle sweep. Consoles arrive as `console_open` frames the node bus routes to
        `handle_console_open`, so this bridge holds no listener of its own."""
        # The start-up sweep completes before the idle loop exists, so `_clientless_since` needs no lock.
        # Every `_view_*` left belongs to a tile that died with the previous daemon. Best effort.
        try:
            reaped = await asyncio.get_event_loop().run_in_executor(
                None, functools.partial(self._view_reaper.sweep, ignore_grace=True),
            )
            if reaped:
                log.info("console: cleared %d view session(s) left by a previous daemon", len(reaped))
        except Exception:
            log.exception("console: the start-up view-session sweep failed")
        self._sweep_task = asyncio.ensure_future(self._idle_sweep_loop())
        log.info("console bridge ready (dialling %s per session)", self._stream_url or "<resolved per dial>")

    def has_live_session(self, project: str, number: object) -> bool:
        """Whether any Console/Term session (any `kind`) is attached OR being opened for this worktree.

        reconcile's DONE-close gate calls this: a bare tmux attach has no agent process, so agent
        liveness alone would reclaim a worktree someone is in. A mid-handshake connect counts (tens of s)."""
        number = str(number)
        return any(key[0] == project and key[1] == number for key in self._sessions) or any(
            key[0] == project and key[1] == number for key in self._opening
        )

    async def stop(self) -> None:
        if self._sweep_task is not None:
            self._sweep_task.cancel()
        for task in list(self._paste_tasks):
            task.cancel()
        for key in list(self._sessions):
            # Guarded per key: one failure must not skip the rest of the shutdown or escape into main.py.
            await self._close_session_guarded(key, reason="shutdown")

    async def _close_session_guarded(self, key: ConsoleKey, *, reason: str) -> None:
        """`_close_session`, with its exceptions contained to the ONE session they belong to.

        Both loops ITERATE sessions, so an escape kills the loop; the pty-eof pump's fire-and-forget
        task would fail silently at GC."""
        try:
            await self._close_session(key, reason=reason)
        except Exception:
            log.exception("console: closing %s (%s) failed", key, reason)

    async def _idle_sweep_loop(self) -> None:
        while True:
            await asyncio.sleep(min(self._idle_timeout, _IDLE_SWEEP_INTERVAL_SECONDS))
            # Recheck staleness per key right before closing: a connect can refresh a later key meanwhile.
            now = time.monotonic()
            for key in list(self._sessions):
                session = self._sessions.get(key)
                if session is not None and now - session.last_activity > self._idle_timeout:
                    await self._close_session_guarded(key, reason="idle timeout")
            # A drifted console is BUSY (it streams the neighbour), so idle never ends it; this pass does.
            await self._close_vanished_windows(list(self._sessions))
            await self._sweep_view_sessions()

    async def _close_vanished_windows(self, keys: list[ConsoleKey]) -> None:
        """Close every session among `keys` whose tmux window no longer exists — the acting half of
        `_Session.tmux_window`.

        One tmux read for the set, in an executor. Fails closed: an unanswered read closes nothing, and a
        session replaced meanwhile survives (identity re-check)."""
        watched: list[tuple[ConsoleKey, _Session]] = []
        for key in keys:
            session = self._sessions.get(key)
            if session is not None and session.tmux_window:
                watched.append((key, session))
        if not watched:
            return
        live, query_failed = await asyncio.get_event_loop().run_in_executor(None, live_window_ids)
        if query_failed or not live:
            # `not live` is a second, independent gate: an unresolvable session user collapses to the
            # daemon's own label, whose DEFINITIVE zero would close every console.
            return
        for key, session in watched:
            if session.tmux_window in live or self._sessions.get(key) is not session:
                continue
            log.info(
                "console: tmux window %s for %s is gone — closing the console rather than letting its "
                "attach drift onto a neighbouring window", session.tmux_window, key,
            )
            await self._close_session_guarded(key, reason="window-gone")

    async def _sweep_view_sessions(self) -> None:
        """Run the view-session reaper, IN AN EXECUTOR and on its own slower cadence.

        Not inline: a blocking `subprocess.run` per socket would park the event loop."""
        now = time.monotonic()
        if now - self._last_view_sweep < _VIEW_SESSION_SWEEP_INTERVAL_SECONDS:
            return
        self._last_view_sweep = now
        try:
            await asyncio.get_event_loop().run_in_executor(None, self._view_reaper.sweep)
        except Exception:
            log.exception("console: the view-session sweep failed")

    async def handle_console_open(self, req: dict) -> dict:
        """Serve one `console_open` request from the node bus: attach this worktree's PTY — opening a
        session first when the frame's `attach_only` allows it and nothing is live — dial the CP a
        dedicated socket for the asking viewer, and report the outcome.

        The `read_only` echo is a CAPABILITY REPORT the CP fails closed on (an older daemon ignores the
        flag and answers bare `{"ok": true}`), so it is on EVERY successful reply, True or False."""
        kind = req.get("kind")
        if kind != "clauded":
            # `clauded` is the only kind; `routes/console.py::CONSOLE_KINDS` mirrors this set.
            return {"error": "unknown console kind"}
        # `is None`, not `or ""`: slot 0 arrives as the integer 0. Absent is still refused.
        project = "" if req.get("project") is None else str(req.get("project"))
        number = "" if req.get("number") is None else str(req.get("number"))
        if not project or not number:
            return {"error": "invalid console target"}
        ticket = req.get("ticket")
        if not isinstance(ticket, str) or not ticket:
            return {"error": "missing rendezvous ticket"}
        # Shape validated here; existence at resolve time.
        target, target_error = console_key_target(req.get("view"), req.get("layout"))
        if target_error is not None:
            return {"error": target_error}
        # The CP's tmux-session choice; absent (or `""`) means this node's default.
        session_name, session_error = _validate_session_name(req.get("session") or None)
        if session_error:
            return {"error": session_error}
        # The `worktrees:view` tier's node-side input; absent (older CP) means writable.
        read_only, read_only_error = _validate_optional_bool(req.get("read_only"), "read_only")
        if read_only_error:
            return {"error": read_only_error}
        key = (project, number, kind, target)
        try:
            return await self._open_for_viewer(
                key, req, ticket, session_name=session_name, read_only=read_only,
            )
        except Exception as exc:
            # Never log the ticket: it is a live single-use capability until the CP expires it.
            log.exception("console: console_open failed for %s", key)
            return {"error": f"console open failed: {exc}", "retryable": True}

    async def _open_for_viewer(
        self, key: ConsoleKey, req: dict, ticket: str, *, session_name: str | None = None,
        read_only: bool = False,
    ) -> dict:
        """The CP rendezvous half of an open: authenticate the bus frame, resolve the session, then dial
        this viewer's own socket to the CP and attach it. `_resolve_session` + `_attach_viewer` hold
        everything shared; what stays here is specific to arriving as a `console_open` frame."""
        if not node_frame_auth.frame_auth_ok(req, expected_token=_expected_token):
            # Two booleans, never the values: nothing sent / nothing configured / mismatch (rotation).
            log.warning(
                "console open rejected: invalid/missing cp_frame_proof (proof_provided=%s, token_configured=%s)",
                bool(req.get("cp_frame_proof")), bool(_expected_token()),
            )
            return {"error": "unauthorized"}
        # Passed as sent; `_resolve_session` drops a read-only viewer's dimensions for both transports.
        rows = _clamp_dim(req.get("rows"), default=24)
        cols = _clamp_dim(req.get("cols"), default=80)
        # An open resolves, and creates when the frame allows; it never claims a slot.
        # `attach_only` is a request field: a read-only open must never arrive with it false.
        attach_only = bool(req.get("attach_only"))

        try:
            # Reserve the SLOT (`key[:3]`) for the WHOLE open — resolve, dial, attach — behind the auth
            # check, so no unauthenticated caller can pin a slot. The caller holds it because the dial
            # outlives the resolve.
            with self._reserved(key[:3]):
                session = await self._resolve_session(
                    key, rows=rows, cols=cols, attach_only=attach_only, read_only=read_only,
                    # The CP's account choice, off the frame through the shared reader.
                    config_dir=console_config_dir(req.get("config_dir")),
                    # Its token store when SHARED; an older CP omits it, same as non-shared.
                    securestorage_dir=console_securestorage_dir(req.get("securestorage_dir")),
                    # Whose CLI, and the API-key account standing in for `config_dir`; same readers.
                    provider=console_provider(req.get("provider")),
                    api_key_account=console_api_key_account(req.get("api_key_account")),
                    # The Jaah account (K2) standing in for both, validated by the one reader.
                    bedrock=console_bedrock(req.get("bedrock")),
                    # Already validated in handle_console_open.
                    session_name=session_name,
                    # The CP's own read of the slot's path, saving the node a CP round trip per open.
                    path=console_worktree_path(req.get("path")),
                    # The Architectural Guard stanza, only where it is available for the org; absent, the call is
                    # unchanged.
                    **_guard_kw(console_guard(req.get("guard"))),
                )

                if len(session.viewers) >= _MAX_VIEWERS_PER_SESSION:
                    # Hard bound, no eviction: a viewer releases by closing. `retryable` (transient).
                    # Checked before any dial so hitting the cap costs the CP nothing.
                    return {"error": "too many concurrent viewers", "retryable": True}

                # DIAL BEFORE REGISTERING, reply after both: a failed dial leaves nothing, a mid-dial
                # close finds no half-built viewer, and `_attach_viewer` refuses a session that died.
                ws = await self._dial_cp(ticket, console_stream_url(req.get("stream_url")))
                await self._attach_viewer(session, ws, read_only=read_only)
                # Echoes what was APPLIED; every way of not applying it raises instead of downgrading.
                return {"ok": True, "read_only": read_only}
        except _ConsoleOpenError as exc:
            return exc.as_reply()

    @contextlib.contextmanager
    def _reserved(self, key: SlotKey):
        """Hold `key`'s slot reservation for the duration of the block — the single home for
        `self._opening`'s refcount arithmetic.

        Sync: a suspension point would let a peer interleave and lose a count."""
        self._opening[key] = self._opening.get(key, 0) + 1
        try:
            yield
        finally:
            count = self._opening.get(key, 0) - 1
            if count > 0:
                self._opening[key] = count
            else:
                self._opening.pop(key, None)

    async def _resolve_session(
        self, key: ConsoleKey, *, rows: int, cols: int, attach_only: bool, read_only: bool = False,
        config_dir: str | None = None, session_name: str | None = None,
        securestorage_dir: str | None = None,
        provider: str | None = None, api_key_account: str | None = None, bedrock: str | None = None,
        path: str | None = None, guard_project: int | None = None,
    ) -> _Session:
        """The live `_Session` for `key`, creating the tmux target and its PTY if this is the first viewer.
        Raises `_ConsoleOpenError` when there is nothing to attach to.

        THREE RESOLUTIONS on `key[3]`: empty is this worktree's own window, a `@id` is `_open_view_pty`,
        `layout:<spec>` is `_open_layout_pty`. The single home of the `worktrees:view` tier's two effects
        (dimensions discarded, spawned PTY attaches `-r`). Must be called inside `_reserved(key[:3])` by
        an ALREADY-AUTHENTICATED caller; auth is not enforced here."""
        project, number, kind, view = key
        if read_only:
            # A viewer may not change the terminal size (tmux sizes to the SMALLEST client). `-r` is what
            # protects the driver (tmux sets `CLIENT_IGNORESIZE` with `CLIENT_READONLY`, measured on 3.4);
            # this clamp covers only spawn time, which `-r` cannot. Not permission to drop `-r`.
            rows, cols = 24, 80
        slot_key = key[:3]
        if not self._opening.get(slot_key):
            raise RuntimeError(f"console: _resolve_session({key}) called outside _reserved({slot_key})")
        if view and session_name:
            # Views and layouts attach to EXISTING windows by id; refusing beats silently ignoring
            # `session_name`.
            raise _ConsoleOpenError(
                "a session name cannot be combined with a view or layout attachment", retryable=False,
            )
        # Before handing back a cached session: is its window still there? Otherwise it is returned forever.
        await self._close_vanished_windows([key])
        # Every decision reading `self._sessions[key]` is taken UNDER THE LOCK on one read (a pre-lock read
        # raced the ~35s create). Costs one uncontended acquire; only the read-only rebuild waits, and
        # `_close_session` closes displaced viewers CONCURRENTLY so the lock is never held past the CP's
        # `console_open` timeout. Keyed by the FULL ConsoleKey: one PTY per key, tiles not serialised.
        lock = self._async_locks.setdefault(key, asyncio.Lock())
        async with lock:
            session = self._sessions.get(key)
            if session is not None and session_name and session.tmux_session and session.tmux_session != session_name:
                # A live PTY in a DIFFERENT session: a session hint is create-time only. Refusing beats
                # attaching one while reporting another.
                raise _ConsoleOpenError(
                    # Remedy first: this is also a close REASON, clipped at 123 bytes (RFC 6455).
                    f"reconnect without naming a session to attach to this console: it is already live "
                    f"in tmux session {session.tmux_session!r}, not the requested {session_name!r}",
                    retryable=False,
                )
            if session is not None and session.read_only and not read_only:
                # A writable open is never served a `-r` PTY (read-only-ness is fixed per client and the
                # PTY is shared). Replaced, not refused: the displaced watchers get a RETRYABLE close. The
                # close is under the lock because their read-only reconnects would otherwise rebuild it.
                log.info("console: replacing the read-only PTY for %s — a writable viewer arrived", key)
                await self._close_session_guarded(key, reason="read-only rebuild")
                # `_close_session` popped the key and nobody can install one under this lock; re-read anyway.
                session = self._sessions.get(key)
            if session is None:
                if view.startswith(_VM_KEY_PREFIX):
                    session = await self._open_vm_pty(key, rows=rows, cols=cols, read_only=read_only)
                elif view.startswith(_LAYOUT_KEY_PREFIX):
                    session = await self._open_layout_pty(key, rows=rows, cols=cols, read_only=read_only)
                elif view:
                    session = await self._open_view_pty(key, rows=rows, cols=cols, read_only=read_only)
                else:
                    session = await self._open_worktree_pty(
                        key, rows=rows, cols=cols, attach_only=attach_only, read_only=read_only,
                        config_dir=config_dir, session_name=session_name,
                        securestorage_dir=securestorage_dir,
                        provider=provider, api_key_account=api_key_account, bedrock=bedrock, path=path,
                        **_guard_kw(guard_project),
                    )
                self._sessions[key] = session
                self._start_stdout_pump(key, session)
        return session

    async def _open_worktree_pty(
        self, key: ConsoleKey, *, rows: int, cols: int, attach_only: bool, read_only: bool = False,
        config_dir: str | None = None, session_name: str | None = None,
        securestorage_dir: str | None = None,
        provider: str | None = None, api_key_account: str | None = None, bedrock: str | None = None,
        path: str | None = None, guard_project: int | None = None,
    ) -> _Session:
        """This worktree's own console PTY — resolve-or-create.

        `config_dir`, `session_name` and `securestorage_dir` are the CP's choices (from the frame or the
        direct GRANT), never the browser's."""
        project, number, kind, _ = key
        if read_only and not attach_only:
            # A `worktrees:view` principal never creates. The CP forces `attach_only` for it, but a
            # promise is not a guard. Non-retryable.
            raise _ConsoleOpenError(
                "a read-only console attaches to an existing session, it never creates one",
                retryable=False,
            )
        loop = asyncio.get_event_loop()
        # `attach_only` is the create decision: a reconnect asks attach-only so it cannot spawn a second
        # agent. A first open by an edit principal creates when nothing is live. No hold is taken.
        target, err, created = await loop.run_in_executor(
            None,
            functools.partial(
                resolve_or_create_console_target,
                self._repo_root, project, number, kind,
                create=not attach_only,
                node_id=self._node_id or "", config_dir=config_dir, session=session_name,
                securestorage_dir=securestorage_dir,
                provider=provider, api_key_account=api_key_account, bedrock=bedrock, path=path,
                **_guard_kw(guard_project),
            ),
        )
        if target is None:
            raise _ConsoleOpenError(
                err or "no live session for this worktree",
                retryable=err == INDETERMINATE_ERROR,
            )
        if created:
            # The only node-side record of a spawn against a named account's quota (the CP's audit line
            # is the same for attach and spawn; the principal is only known CP-side).
            log.info(
                "console: CREATED a clauded session for %s-%s in tmux session %s on account %s",
                project, number, session_name or "<node default>",
                config_dir or api_key_account or ("Jaah account" if bedrock else None),
            )
        # A grouped session, not `tmux attach -t <target>` (see `_create_grouped_session` for the
        # coupling cost). `owned_sessions`/`view_socket` let `_close_session` tear it down; empty if unavailable.
        isolated = await self._grouped_console_attach(target, rows=rows, cols=cols, read_only=read_only)
        pid: int | None = None if isolated is None else isolated[0]
        try:
            if isolated is None:
                pid, master_fd = await _spawn_console(target, rows, cols, read_only=read_only)
                owned: tuple[str, ...] = ()
                socket: str | None = None
            else:
                pid, master_fd, grouped, socket = isolated
                owned = (grouped,)
            session = _Session(
                # The RESOLVED session and window, never the grouped session's name: later opens and
                # `_close_vanished_windows` ask about the window the viewer sees.
                key=key, master_fd=master_fd, pid=pid, tmux_session=target.rsplit(":", 1)[0],
                tmux_window=_window_id_of(target), owned_sessions=owned, view_socket=socket,
                read_only=read_only,
            )
        except BaseException:
            await _discard_pending_conpty(pid)
            raise
        log.info("console attached %s (target=%s, isolated=%s)", key, target, isolated is not None)
        return session

    async def _grouped_console_attach(
        self, target: str, *, rows: int, cols: int, read_only: bool,
    ) -> tuple[int, int, str, str] | None:
        """(pid, master_fd, owned session, socket) for an ISOLATED attach to console target `target` — or
        None when this node cannot give this console its own session and the caller must attach plainly.

        DEGRADES, NEVER FAILS: isolation is a latency property, so an unanswering tmux costs the
        coupling back, not the console. The reservation spans create→attach: in between the session has
        zero clients, the reaper's kill criterion."""
        loop = asyncio.get_event_loop()
        base, window = target.rsplit(":", 1)[0], _window_id_of(target)
        if not window:
            # No `@id`: nothing to select and no liveness key; plain attach rather than a guess.
            return None
        socket = await loop.run_in_executor(None, functools.partial(_console_window_socket, target))
        if socket is None:
            log.warning(
                "console: could not name the tmux socket holding %s — attaching as a client of %s "
                "instead, which couples this viewer's window and size to every other one", target, base,
            )
            return None
        name = f"{EPHEMERAL_VIEW_SESSION_PREFIX}{secrets.token_hex(8)}"
        pid: int | None = None
        try:
            with self._view_reaper.reserved(name):
                created, attach_target, err = await loop.run_in_executor(
                    None, functools.partial(_create_console_session, socket, base, name, window),
                )
                if created is None or attach_target is None:
                    log.warning("console: %s — attaching to %s without isolation",
                                err or "no grouped session", target)
                    return None
                try:
                    pid, master_fd = await _spawn_console(
                        attach_target, rows, cols, socket, read_only=read_only)
                except BaseException:   # a cancelled (Windows: awaited) spawn tears it down too
                    # Ours alone, so a failed spawn tears it down now rather than leaving it to the reaper.
                    await loop.run_in_executor(None, functools.partial(_kill_view_session, socket, created))
                    raise
        except BaseException:
            await _discard_pending_conpty(pid)
            raise
        return pid, master_fd, created, socket

    async def _open_view_pty(
        self, key: ConsoleKey, *, rows: int, cols: int, read_only: bool = False,
    ) -> _Session:
        """A PTY showing ONE named window of this node's shared `fleet` session — the per-view path.

        Resolve which socket holds the window, create the grouped session there, attach on that socket.
        The reservation spans create→attach (zero clients = the reaper's criterion). Attach-only."""
        _, _, _, view = key
        loop = asyncio.get_event_loop()
        socket, err = await loop.run_in_executor(None, functools.partial(_fleet_window_socket, view))
        if socket is None:
            raise _ConsoleOpenError(
                err or f"no window {view} on this node", retryable=err == INDETERMINATE_ERROR,
            )
        name = f"{EPHEMERAL_VIEW_SESSION_PREFIX}{secrets.token_hex(8)}"
        pid: int | None = None
        try:
            with self._view_reaper.reserved(name):
                created, target, err = await loop.run_in_executor(
                    None, functools.partial(_create_view_session, socket, view, name),
                )
                if target is None or created is None:
                    raise _ConsoleOpenError(
                        err or f"could not open a view of {view}", retryable=err == INDETERMINATE_ERROR,
                    )
                try:
                    pid, master_fd = await _spawn_console(target, rows, cols, socket, read_only=read_only)
                except BaseException:   # a cancelled (Windows: awaited) spawn tears it down too
                    # Ours alone, so a failed spawn tears it down now rather than leaving it to the reaper.
                    await loop.run_in_executor(None, functools.partial(_kill_view_session, socket, created))
                    raise
            session = _Session(
                key=key, master_fd=master_fd, pid=pid, owned_sessions=(created,), view_socket=socket,
                read_only=read_only,
            )
        except BaseException:
            await _discard_pending_conpty(pid)
            raise
        log.info("console attached %s (target=%s, socket=%s)", key, target, socket)
        return session

    async def _open_vm_pty(
        self, key: ConsoleKey, *, rows: int, cols: int, read_only: bool = False,
    ) -> _Session:
        """A PTY on a MicroVM session's own terminal (D18): `microvm_console`'s relay where a static session
        forks `tmux attach` — the ring replayed, then live output, input written unless `read_only`, and the
        PTY's size forwarded to the guest. Attach-only: the VM exists before any console can see it.

        The VM must be RUNNING on this node's supervisor, checked first so a stopped or unknown VM is a
        clean refusal rather than a PTY that prints an error and closes."""
        vm_id = key[3][len(_VM_KEY_PREFIX):]
        if host_platform.is_windows():
            return await self._open_windows_vm_pty(key, vm_id, rows=rows, cols=cols, read_only=read_only)
        microvm_client = cloud_modules.load("microvm_client")  # Linux-only machinery, off the macOS/Windows import graph

        loop = asyncio.get_event_loop()
        try:
            listed = await loop.run_in_executor(
                None, functools.partial(microvm_client.request, "list", timeout=5.0))
        except (microvm_client.SupervisorError, OSError) as exc:
            raise _ConsoleOpenError(f"this node's VM supervisor did not answer: {exc}", retryable=True)
        state = next((str(row.get("state")) for row in listed.get("vms") or []
                      if isinstance(row, dict) and row.get("vm_id") == vm_id), None)
        if state != "running":
            raise _ConsoleOpenError(
                f"VM {vm_id} is not running on this node" + (f" (it is {state})" if state else ""),
                retryable=state == "starting",
            )
        pid, master_fd = _spawn_vm_console_pty(vm_id, rows, cols, read_only=read_only)
        log.info("console attached %s (vm=%s, read_only=%s)", key, vm_id, read_only)
        return _Session(key=key, master_fd=master_fd, pid=pid, read_only=read_only)

    async def _open_windows_vm_pty(
        self, key: ConsoleKey, vm_id: str, *, rows: int, cols: int, read_only: bool = False,
    ) -> _Session:
        """`_open_vm_pty` on a Windows MicroVM node: the VM runs in the WSL2 backend, so the relay pane runs
        `microvm_console`'s relay there (`_spawn_windows_vm_console`). A VM that is not running ends the relay with
        its own one-line refusal."""
        bridge = cloud_modules.load("microvm_bridge")
        if bridge is None or not bridge.windows_marked():
            raise _ConsoleOpenError("this node is not a MicroVM node", retryable=False)
        loop = asyncio.get_event_loop()
        pid, master_fd = await loop.run_in_executor(
            None, functools.partial(_spawn_windows_vm_console, vm_id, rows, cols, read_only))
        try:
            session = _Session(key=key, master_fd=master_fd, pid=pid, read_only=read_only)
        except BaseException:
            await _discard_pending_conpty(pid)
            raise
        log.info("console attached %s (vm=%s, read_only=%s)", key, vm_id, read_only)
        return session

    async def _open_layout_pty(
        self, key: ConsoleKey, *, rows: int, cols: int, read_only: bool = False,
    ) -> _Session:
        """A PTY showing a whole DESIGNED GRID: one tmux window whose panes are the layout's tiles.

        The spec rides in the key, so identical grids share one PTY and a reconnect rebuilds from the
        string. Every reservation spans the WHOLE build (a tile is client-less until the grid attaches to
        it). A partial build is torn down, never attached to."""
        _, _, _, spec_key = key
        tree, error = parse_layout_spec(spec_key[len(_LAYOUT_KEY_PREFIX):])
        if tree is None:
            # Definitive: the browser re-sends the same string, so a retry only spends the ladder.
            raise _ConsoleOpenError(error or "invalid layout")
        windows = layout_windows(tree)
        loop = asyncio.get_event_loop()
        socket, err = await loop.run_in_executor(None, functools.partial(_fleet_windows_socket, windows))
        if socket is None:
            raise _ConsoleOpenError(
                err or "no such window on this node", retryable=err == INDETERMINATE_ERROR,
            )
        grid_name = f"{EPHEMERAL_LAYOUT_SESSION_PREFIX}{secrets.token_hex(8)}"
        tile_names = {window: f"{EPHEMERAL_VIEW_SESSION_PREFIX}{secrets.token_hex(8)}" for window in windows}
        pid: int | None = None
        try:
            with contextlib.ExitStack() as reservations:
                reservations.enter_context(self._view_reaper.reserved(grid_name))
                for name in tile_names.values():
                    reservations.enter_context(self._view_reaper.reserved(name))
                built: list[str] = []
                try:
                    for window in windows:
                        created, _target, err = await loop.run_in_executor(
                            None,
                            functools.partial(
                                _create_view_session, socket, window, tile_names[window], mouse=True,
                            ),
                        )
                        if created is None:
                            raise _ConsoleOpenError(
                                err or f"could not open a view of {window}",
                                retryable=err == INDETERMINATE_ERROR,
                            )
                        built.append(created)
                    target, err = await loop.run_in_executor(
                        None,
                        functools.partial(
                            _create_layout_session, socket, tree, tile_names, grid_name, rows=rows, cols=cols,
                        ),
                    )
                    if target is None:
                        raise _ConsoleOpenError(
                            err or "could not build the layout grid", retryable=err == INDETERMINATE_ERROR,
                        )
                    built.insert(0, grid_name)   # the grid leads, so teardown kills the panes first
                    pid, master_fd = await _spawn_console(target, rows, cols, socket, read_only=read_only)
                except BaseException:
                    for owned in built:
                        await loop.run_in_executor(
                            None, functools.partial(_kill_view_session, socket, owned),
                        )
                    raise
            session = _Session(
                key=key, master_fd=master_fd, pid=pid, owned_sessions=tuple(built), view_socket=socket,
                read_only=read_only,
            )
        except BaseException:
            await _discard_pending_conpty(pid)
            raise
        log.info(
            "console attached %s (target=%s, socket=%s, tiles=%d)", key, target, socket, len(windows),
        )
        return session

    async def _attach_viewer(
        self, session: _Session, ws, *, browser_facing: bool = False, read_only: bool = False,
    ) -> str:
        """Register `ws` as one more viewer of `session`, start pumping its stdin, and return its `vid`.

        The transport half; incurious about where `ws` came from. Raises `_ConsoleOpenError` (closing
        `ws` RECOVERABLY) when the session died meanwhile. The viewer cap is the CALLER's check."""
        key = session.key
        if self._sessions.get(key) is not session:
            # Dialect matters even before a `_Viewer` exists: a browser has no rule for 4001.
            await _close_ws(ws, _viewer_close_code(_Viewer(ws=ws, browser_facing=browser_facing), True))
            raise _ConsoleOpenError("session closed while opening", retryable=True)

        vid = secrets.token_hex(8)
        # `read_only` rides on the VIEWER, where `_handle_stdin` enforces it.
        viewer = _Viewer(ws=ws, browser_facing=browser_facing, read_only=read_only)
        session.viewers[vid] = viewer
        session.last_activity = time.monotonic()
        viewer.reader = asyncio.ensure_future(self._read_viewer(key, vid, viewer, session))
        if session.out_buf and session.out_wake is not None:
            # Deliver what the PTY painted before any viewer existed (`_flush_stdout` held it).
            session.out_wake.set()
        return vid

    async def attach_direct_viewer(
        self, key: ConsoleKey, ws, *, rows: int, cols: int, attach_only: bool, read_only: bool = False,
        session_name: str | None = None, before_attach=None, config_dir: str | None = None,
        securestorage_dir: str | None = None,
        provider: str | None = None, api_key_account: str | None = None, bedrock: str | None = None,
        guard_project: int | None = None,
    ) -> str:
        """Establish a console for a socket THIS daemon accepted (`console_direct.py`) and return its vid.

        Authentication is the CALLER's: redeem the grant BEFORE calling (this can create a PTY).
        `session_name`, `read_only`, `config_dir`, `securestorage_dir` come from the REDEEMED GRANT, never
        the hello — a client that could set `read_only` could clear it (no CP in the path).
        `before_attach` runs between resolve and attach, so the ack neither trails output nor over-promises."""
        # Normalised here too (`_normalize_view`). Reserves the SLOT, as `_open_for_viewer` does.
        key = key[:3] + (_normalize_view(key[3]),)
        with self._reserved(key[:3]):
            session = await self._resolve_session(
                key, rows=rows, cols=cols, attach_only=attach_only, read_only=read_only,
                session_name=session_name, config_dir=config_dir,
                securestorage_dir=securestorage_dir,
                provider=provider, api_key_account=api_key_account, bedrock=bedrock,
                **_guard_kw(guard_project),
            )
            if len(session.viewers) >= _MAX_VIEWERS_PER_SESSION:
                # The CALLER's check on both transports (see `_open_for_viewer`).
                raise _ConsoleOpenError("too many concurrent viewers", retryable=True)
            if before_attach is not None:
                try:
                    await before_attach()
                except Exception as exc:
                    # The browser vanished before the ack. A session this call created has no viewer for
                    # `_drop_viewer` to detach, so close it now rather than leave it to the idle sweep.
                    if not session.viewers:
                        await self._close_session_guarded(key, reason="all viewers detached")
                    raise _ConsoleOpenError(
                        "the client went away before the session was acknowledged ({})".format(exc),
                        retryable=True,
                    )
            return await self._attach_viewer(session, ws, browser_facing=True, read_only=read_only)

    async def _read_viewer(self, key: ConsoleKey, vid: str, viewer: _Viewer, session: _Session) -> None:
        """Pump one viewer's socket into the PTY until it closes.

        The socket closing IS this viewer's detach; nothing is authorized per message. `session` is the
        one this viewer was REGISTERED on, for `_drop_viewer`."""
        try:
            async for frame in viewer.ws:
                self._handle_stdin(key, frame.encode() if isinstance(frame, str) else frame, viewer)
        except Exception as exc:
            log.debug("console: viewer stream for %s ended (%s)", key, exc)
        finally:
            await self._drop_viewer(key, vid, session)

    async def _drop_viewer(self, key: ConsoleKey, vid: str, session: _Session) -> None:
        """Remove one viewer of `session`, and close it when that was the last — DETACH, never kill.

        Scoped to the session the viewer was REGISTERED on, never to whatever `key` holds now: a
        replacement starts with zero viewers and would read as "the last viewer left". A cancelled
        `_read_viewer` still runs its `finally`. Re-entrant; the identity check stops the recursion."""
        if self._sessions.get(key) is not session:
            return
        viewer = session.viewers.pop(vid, None)
        if viewer is not None:
            await _close_ws(viewer.ws, _viewer_close_code(viewer, False))
        if not session.viewers:
            await self._close_session_guarded(key, reason="all viewers detached")

    def _handle_stdin(self, key: ConsoleKey, data: bytes, viewer: _Viewer) -> None:
        """Parse one or more browser wire frames out of `data` and apply them to the PTY.

        The framing is the browser's own: type 0 input, type 1 resize, types 2/3 a pasted image. `viewer`
        is passed rather than looked up because paste assembly is per SOCKET."""
        if viewer.read_only:
            # THE ENFORCEMENT: a `worktrees:view` socket is output-only, whole-payload (every frame type
            # is a write, and an unknown one defaults to "not from here"). The relay drops them too, but
            # direct mode has no CP in the path, so this is the only drop there. `last_activity` is not
            # touched — keeping a session alive is itself an effect on others.
            return
        session = self._sessions.get(key)
        if session is None:
            return   # session already closed — drop rather than crash
        session.last_activity = time.monotonic()
        offset = 0
        while offset + _FRAME_HEADER_LEN <= len(data):
            frame_type = data[offset]
            length = (data[offset + 1] << 8) | data[offset + 2]
            start = offset + _FRAME_HEADER_LEN
            end = start + length
            if end > len(data):
                break   # incomplete/malformed trailing frame — drop rather than crash
            payload = bytes(data[start:end])
            if frame_type == _FRAME_TYPE_INPUT:
                self._write_to_pty(session, payload)
            elif frame_type == _FRAME_TYPE_RESIZE and len(payload) == 4:
                cols = _valid_resize_dim((payload[0] << 8) | payload[1])
                rows = _valid_resize_dim((payload[2] << 8) | payload[3])
                if cols is not None and rows is not None:
                    try:
                        if session.conpty is not None:
                            session.conpty.resize(rows, cols)
                        else:
                            _set_winsize(session.master_fd, rows, cols)
                            if not session.key[3].startswith(_VM_KEY_PREFIX):   # no tmux client on this side
                                self._schedule_redraw(session)
                    except OSError:
                        pass
            elif frame_type == _FRAME_TYPE_COPY_PANE and not session.key[3].startswith(_VM_KEY_PREFIX):
                now = time.monotonic()
                if not session.copy_in_flight and now - session.last_copy_at >= _COPY_PANE_MIN_INTERVAL_SECONDS:
                    session.copy_in_flight, session.last_copy_at = True, now
                    self._run_client_job(
                        functools.partial(self._copy_pane, session), f"copying the pane of {session.key}")
            elif frame_type == _FRAME_TYPE_PASTE_BEGIN:
                self._begin_paste(session, viewer, payload)
            elif frame_type == _FRAME_TYPE_PASTE_DATA:
                self._collect_paste(session, viewer, payload)
            # else: unrecognized type or malformed resize payload — dropped, never crashes the bridge.
            offset = end

    def _schedule_redraw(self, session: _Session) -> None:
        """(Re)arm the one redraw of this PTY's client for when its size stops changing."""
        if session.redraw_timer is not None:
            session.redraw_timer.cancel()
        session.redraw_timer = asyncio.get_event_loop().call_later(
            _RESIZE_SETTLE_SECONDS, self._run_client_job,
            functools.partial(_redraw_client, session.pid, session.view_socket), f"redrawing {session.key}")

    def _copy_pane(self, session: _Session) -> None:
        try:
            error = _copy_pane_to_client(session.pid, session.view_socket, layout=session.key[3].startswith(_LAYOUT_KEY_PREFIX))
        finally:
            session.copy_in_flight = False   # a plain store, read only on the loop thread's next frame
        if error:
            log.warning("console: copy pane text on %s failed — %s", session.key, error)

    def _run_client_job(self, job, what: str) -> None:
        """Run a blocking per-client tmux job in an executor, tracked like a paste so a failure is a warning."""
        if host_platform.is_windows():
            return   # the attach client sits behind a relay pane there (`console_pty`); nothing to aim at
        task = asyncio.ensure_future(asyncio.get_event_loop().run_in_executor(None, job))
        self._paste_tasks.add(task)
        task.add_done_callback(lambda done: self._reap_client_job(done, what))

    def _reap_client_job(self, task: asyncio.Future, what: str) -> None:
        self._paste_tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            log.warning("console: %s failed: %r", what, task.exception())

    def _begin_paste(self, session: _Session, viewer: _Viewer, payload: bytes) -> None:
        """Accept a PASTE_BEGIN header, replacing whatever this viewer had in flight.

        A new header always abandons the previous paste (the browser sends it only to start over). Every
        rejection is logged — a screenshot that silently never appears is undebuggable."""
        viewer.paste = None
        try:
            header = json.loads(payload.decode())
            size = int(header["size"])
            ext = str(header["ext"]).lower()
        except (UnicodeDecodeError, ValueError, TypeError, KeyError) as exc:
            log.warning("console: dropping a paste on %s — unreadable header (%s)", session.key, exc)
            return
        if ext not in _PASTE_EXTS:
            log.warning("console: dropping a paste on %s — extension %r is not one of %s",
                        session.key, ext, sorted(_PASTE_EXTS))
            return
        if size <= 0 or size > _PASTE_MAX_BYTES:
            log.warning("console: dropping a paste on %s — declared size %d is outside 1..%d bytes",
                        session.key, size, _PASTE_MAX_BYTES)
            return
        viewer.paste = _Paste(size=size, ext=ext)

    def _collect_paste(self, session: _Session, viewer: _Viewer, payload: bytes) -> None:
        """Append one PASTE_DATA frame, and hand the image off once the declared size is reached."""
        paste = viewer.paste
        if paste is None:
            log.warning("console: dropping %d paste bytes on %s — no paste is in flight",
                        len(payload), session.key)
            return
        paste.buf += payload
        if len(paste.buf) < paste.size:
            return
        # Cleared before deciding complete vs overrun, so a peer that keeps sending starts afresh.
        viewer.paste = None
        if len(paste.buf) > paste.size:
            log.warning("console: dropping a paste on %s — %d bytes received against a declared %d",
                        session.key, len(paste.buf), paste.size)
            return
        task = asyncio.ensure_future(self._materialize_paste(session, paste))
        self._paste_tasks.add(task)
        task.add_done_callback(self._reap_paste_task)

    def _reap_paste_task(self, task: asyncio.Task) -> None:
        """Retrieve a finished materialization's outcome so a failure is a warning, not a GC-time trace."""
        self._paste_tasks.discard(task)
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:
            log.warning("console: a pasted image was lost mid-materialization: %r", exc)

    async def _materialize_paste(self, session: _Session, paste: _Paste) -> None:
        """Land a completed paste on disk AS THE SESSION USER and type its path into the PTY.

        The path is the payload (Claude Code reads the image off disk). Written through `session_user`'s
        seam: the agent reading it cannot chmod a daemon-owned file. The seam fails closed and the paste
        is dropped — a path to an unreadable file is worse than none."""
        # Per WORKTREE: only the slot half of the key matters.
        project, number, _kind, _view = session.key
        target = wt_key(project, number)
        if not _PASTE_KEY_SAFE.match(target):
            log.warning("console: dropping a paste — %r is not a usable staging directory name", target)
            return
        directory = os.path.join(_PASTE_ROOT, target)
        path = os.path.join(directory, f"img-{int(time.time())}-{secrets.token_hex(4)}.{paste.ext}")
        loop = asyncio.get_event_loop()
        # The seam creates parents; this call exists so the error names the right fix.
        err = await loop.run_in_executor(
            None, functools.partial(session_user.ensure_dir_as_session_user, directory))
        if not err:
            err = await loop.run_in_executor(
                None, functools.partial(session_user.write_file_as_session_user, path, bytes(paste.buf)))
        if err:
            log.warning("console: could not stage a pasted image at %s (%s) — dropping it", path, err)
            return
        if self._sessions.get(session.key) is not session:
            return   # the session ended while the bytes were landing — there is no prompt to type into
        self._write_to_pty(session, f" {path} ".encode())
        await loop.run_in_executor(None, _prune_paste_dir, directory)

    def _write_to_pty(self, session: _Session, payload: bytes) -> None:
        """Write client input to the PTY master without ever blocking the event loop.

        The fd is O_NONBLOCK, so a full input buffer surfaces as EAGAIN instead of parking the daemon
        inside `os.write`. Ordering is preserved by construction: once anything is pending, later payloads
        APPEND rather than attempting their own write. On Windows the ConPTY's writer thread is the queue."""
        if session.conpty is not None:
            if not session.conpty.write(payload):
                log.warning("console: stdin backlog full for %s — dropping %d bytes", session.key, len(payload))
            return
        if payload and not session.in_buf:
            try:
                written = os.write(session.master_fd, payload)
            except BlockingIOError:
                written = 0
            except OSError:
                return   # fd gone — the EOF path owns teardown, this write has nothing to report
            if written == len(payload):
                return
            payload = payload[written:]
        if not payload:
            return
        if len(session.in_buf) + len(payload) > _STDIN_MAX_PENDING_BYTES:
            # Drop rather than grow without bound (see _STDIN_MAX_PENDING_BYTES).
            log.warning(
                "console: stdin backlog full for %s (%d bytes pending) — dropping %d bytes",
                session.key, len(session.in_buf), len(payload),
            )
            return
        session.in_buf += payload
        self._arm_pty_writer(session)

    def _arm_pty_writer(self, session: _Session) -> None:
        """Register the drain callback for a session with pending stdin, at most once."""
        if session.in_writer_armed:
            return
        loop = asyncio.get_event_loop()

        def _on_writable() -> None:
            while session.in_buf:
                try:
                    written = os.write(session.master_fd, session.in_buf)
                except BlockingIOError:
                    return   # still full — stay armed and wait for the next writable event
                except OSError:
                    session.in_buf.clear()
                    break    # fd gone; the reader's EOF path owns teardown
                if written <= 0:
                    return
                del session.in_buf[:written]
            try:
                loop.remove_writer(session.master_fd)
            except Exception:
                pass
            session.in_writer_armed = False

        loop.add_writer(session.master_fd, _on_writable)
        session.in_writer_armed = True

    def _start_stdout_pump(self, key: ConsoleKey, session: _Session) -> None:
        """PTY output → the viewers, split into a sync reader that DRAINS and one task per session that
        PUBLISHES.

        ORDER: fire-and-forget publishes would interleave at the await and reverse chunks. SPEED: the drain
        merges the dozens of small reads a tmux repaint arrives as. O_NONBLOCK is set HERE because this
        code requires it: the drain must see EAGAIN, and `os.write` to a PTY master blocks once full."""
        loop = asyncio.get_event_loop()
        if session.conpty is None:
            os.set_blocking(session.master_fd, False)
        session.out_wake = asyncio.Event()

        def _on_readable() -> None:
            at_eof = False
            drained = 0
            while drained < _STDOUT_DRAIN_MAX_BYTES:
                try:
                    chunk = os.read(session.master_fd, _STDOUT_READ_SIZE)
                except BlockingIOError:
                    break        # nothing more readable right now — the common exit
                except OSError:
                    at_eof = True   # EIO on a hung-up master is this platform's EOF
                    break
                if not chunk:
                    at_eof = True   # PTY EOF — the attach ended (session detached/closed on its side)
                    break
                session.out_buf += chunk
                drained += len(chunk)
            if drained:
                session.last_activity = time.monotonic()
                session.out_wake.set()
            if at_eof:
                try:
                    loop.remove_reader(session.master_fd)
                except Exception:
                    pass
                # Guarded: this fire-and-forget task has nobody awaiting it.
                asyncio.ensure_future(self._close_session_guarded(key, reason="pty-eof"))

        def _on_conpty_data(chunk: bytes) -> None:
            # The ConPTY reader thread's chunk, delivered on the loop thread: `_on_readable`'s staging.
            session.out_buf += chunk
            session.last_activity = time.monotonic()
            session.out_wake.set()

        def _on_conpty_eof() -> None:
            asyncio.ensure_future(self._close_session_guarded(key, reason="pty-eof"))

        async def _pump() -> None:
            while True:
                await session.out_wake.wait()
                session.out_wake.clear()
                await self._flush_stdout(key, session)

        if session.conpty is not None:
            session.conpty.start(loop, _on_conpty_data, _on_conpty_eof)
        else:
            loop.add_reader(session.master_fd, _on_readable)
        session.out_pump = asyncio.ensure_future(_pump())

    async def _flush_stdout(self, key: ConsoleKey, session: _Session) -> None:
        """Send everything staged in `session.out_buf` to every live viewer.

        Buffer is taken and cleared BEFORE the first await, so bytes the reader stages while these sends
        are in flight land in the next flush rather than being lost or re-sent."""
        if not session.out_buf:
            return
        if not session.viewers:
            # HELD for the first viewer (`_attach_viewer` wakes the pump) — see `_STDOUT_PRE_VIEWER_MAX_BYTES`.
            # Past the bound it is dropped; tmux repaints on its own soon enough.
            if len(session.out_buf) > _STDOUT_PRE_VIEWER_MAX_BYTES:
                session.out_buf.clear()
            return
        data = bytes(session.out_buf)
        session.out_buf.clear()
        # Fan out to each viewer's own socket. `list()` because the loop awaits and `viewers` can change.
        for vid, viewer in list(session.viewers.items()):
            try:
                await viewer.ws.send(data)
            except Exception as exc:
                # Per viewer, non-fatal: one dead socket must not freeze everyone else's terminal.
                log.warning("console: stdout send failed for %s (viewer %s): %s", key, vid, exc)

    async def _close_session(self, key: ConsoleKey, *, reason: str) -> None:
        """DETACH, NEVER KILL: closing our master fd makes `tmux attach` detach; the tmux
        session and its child keep running. We never signal `pid`.

        The one exemption: a per-view PTY's ephemeral `_view_*` GROUPED session holds no agent or process."""
        session = self._sessions.pop(key, None)
        if session is None:
            return
        # Deliver the tail (the final screenful) BEFORE closing sockets; awaited, since cancelling
        # mid-send is the race this avoids.
        if session.out_pump is not None:
            session.out_pump.cancel()
        if session.redraw_timer is not None:
            session.redraw_timer.cancel()
        try:
            await self._flush_stdout(key, session)
        except Exception as exc:
            log.warning("console: final stdout flush failed for %s: %s", key, exc)
        if session.conpty is not None:
            # Windows: end the attach CLIENT (its ConPTY), which is this platform's fd close — still a
            # detach. In an executor because pywinpty's terminate sleeps; bounded inside `close()`.
            try:
                await asyncio.get_event_loop().run_in_executor(None, session.conpty.close)
            except Exception:
                log.exception("console: closing the ConPTY attach for %s failed", key)
        else:
            try:
                # Inside the guard, so a raise cannot leave the fd open and still `add_reader`ed.
                loop = asyncio.get_event_loop()
                loop.remove_reader(session.master_fd)
                # Symmetric: a writer left on a soon-reused fd number would fire against whatever lands there.
                if session.in_writer_armed:
                    loop.remove_writer(session.master_fd)
                    session.in_writer_armed = False
            except Exception:
                pass
            try:
                os.close(session.master_fd)
            except OSError:
                pass
            try:
                os.waitpid(session.pid, os.WNOHANG)
            except ChildProcessError:
                pass
        # `is not None`, not truthiness: `""` (tmux's default label) is a valid socket.
        if session.owned_sessions and session.view_socket is not None:
            # In order — a layout's grid first, so the nested clients die before their tile sessions. In
            # an executor and guarded, so an unanswering tmux cannot block the viewer notices; leaks go
            # to the reaper.
            for owned in session.owned_sessions:
                try:
                    await asyncio.get_event_loop().run_in_executor(
                        None,
                        functools.partial(_kill_view_session, session.view_socket, owned),
                    )
                except Exception:
                    log.exception("console: tearing down view session %s failed", owned)
        # Tell every remaining viewer why, in the close code (a close cannot be lost). An unclassified
        # reason is RECOVERABLE: a needless retry costs one attempt, a needless terminal close a reload.
        retryable = _CLOSE_REASON_RETRYABLE.get(reason)
        if retryable is None:
            log.warning("console: unclassified close reason %r — treating as retryable", reason)
            retryable = True
        # CONCURRENTLY: a half-open socket never answers the close handshake, so serially this would cost
        # `_MAX_VIEWERS_PER_SESSION` × 10s while the read-only rebuild holds the per-key lock. Every
        # viewer is popped and its reader cancelled before the first close is awaited.
        closes = []
        for vid, viewer in list(session.viewers.items()):
            session.viewers.pop(vid, None)
            # Keeps its stdin off a dying PTY; the reader's `finally` still reaches `_drop_viewer`, made
            # harmless by that function's identity check.
            if viewer.reader is not None:
                viewer.reader.cancel()
            # Per viewer: a relayed and a direct viewer each need their own dialect.
            closes.append(_close_ws(viewer.ws, _viewer_close_code(viewer, retryable),
                                    _CLOSE_REASON_TEXT.get(reason, "")))
        if closes:
            # `return_exceptions`: one viewer's socket must never abandon the rest.
            await asyncio.gather(*closes, return_exceptions=True)
        log.info("console session closed (%s): %s", reason, key)
