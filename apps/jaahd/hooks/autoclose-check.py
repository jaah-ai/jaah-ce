#!/usr/bin/env python3
"""autoclose-check.py — the debounced, BUSY-AWARE window close for visible runners. Single home for
the close decision; fleet_launch.py's build_settings installs `arm` as the Stop-hook close command.

  arm  <sentinel> <linger> [on-kill]   Stop-hook entry: record "last stop" now, then schedule one
                             detached close check <linger> seconds out (forwarding on-kill).
  check <sentinel> <linger> [on-kill]  The close decision (arm schedules it; exposed for tests): kill
                             THIS tmux window ($TMUX_PANE) only if EVERY activity signal reads idle.

on-kill runs only on a confirmed close, before the pane is torn down (runner-status.py's terminal
settle). The self-exit of the own pane fires only after the full ladder. A session waiting on background work emits no Stop events, so "no Stop within linger" is not idle.

THE LADDER, all keyed on the pinned worktree root (`break_cause.worktree_root()`, never `$PWD`): kill
only when ALL read idle; any uncertainty DECLINES (fail-open — a lingering tab is cheap, killed work is not):
  P. with `$CONTROL_RUNNER_SESSION_ID` set, some account's project dir for the root holds
     `<session-id>.jsonl`, else `skip=root-not-my-session` (a populated wrong root would judge another
     session's evidence). Unset (console/plain visible run): a logged no-op.
  0. the slot is identifiable (`skip=occupant-unknown`), and the window's newest transcript is still the
     arming session's (`skip=session-changed` declines AND stops the ladder).
  1. sentinel (last Stop) is stale >= linger.
  2. every account's transcript's last appended record is older than linger (not mtime).
  3. no session artifact (sidechains, `/tmp/claude-<uid>/…`) written within linger.
  4. `claude agents --json` over every account shows no busy session in the root's tree, either direction.
Rungs 2-4 union every account's config dir: a window can change accounts under a leftover checker. No
account with a project dir for the root (`skip=no-proj-dir`) declines — blind rungs must never read idle.
The sentinel is removed only on an actual kill.

The last Stop arms the last checker, and no automated actor reclaims a live-but-parked runner; a human
does. So the retry ladder is sized to outlast one linger (AUTOCLOSE_RETRIES × AUTOCLOSE_RETRY_INTERVAL).
A wrong root declines instead of killing, and spends the whole ladder into `armloop gave-up`; a rung-0 stop
logs `armloop stopped early`. Both hold the slot; the worktree-pool runbook greps
for both.

Residuals: rung 0a depends on a node-scoped CP read (an outage declines everywhere → held slots; worst case
~75s — `urlopen(timeout=30)`, a 401-rotation retry, a ~15s credential-helper shell-out — in a DETACHED process); a
mis-pinned root or an unreadable account root (`skip=unreadable-project-dir`, node-wide) costs slots, not
live work; precondition P does not cover a plain visible run. A hook running as `jaah` holds no node
token, so it skips both CP reads (`_cp_reads_authenticated`) and rung 0a always declines there:
on a node, the CP's settled-hold reap closes the window, not this ladder.
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

_SELF_DIR = Path(__file__).resolve().parent

# Where a session's files live, and why it broke, are single-homed in `break_cause` (loaded by path).
_bc_spec = importlib.util.spec_from_file_location("break_cause", _SELF_DIR / "break_cause.py")
break_cause = importlib.util.module_from_spec(_bc_spec)
_bc_spec.loader.exec_module(break_cause)

# `agent_config_dirs` and `control_plane_api` live one directory up; a hook has only its own dir on
# `sys.path`, so that one is APPENDED (never inserted, so nothing can shadow stdlib). Both are stdlib-only
# at import (`agent_config_dirs.creds_state` loads `agents` lazily, hence its guarded call below).
# `session_user` is deliberately not imported — see `_probe_claude_agents`.
_JAAHD_DIR = str(_SELF_DIR.parent)
if _JAAHD_DIR not in sys.path:
    sys.path.append(_JAAHD_DIR)
import agent_config_dirs
import agent_providers  # the probe's argv (`_AGENTS_PROBE_ARGV`) — a stdlib-only leaf, same append
import environment  # the hooks' log dir, `hook_log_dir` — same stdlib-only leaf, same append
import runner_env

try:
    import control_plane_api
except Exception:  # noqa: BLE001 — see below
    # `control_plane_api` parses `API_PORT` at import; a malformed value must not fail this hook's import
    # (then nothing autocloses anywhere). `None` is treated as an unreachable API — never a green light.
    control_plane_api = None


def _cp_reads_authenticated() -> bool:
    """Whether this process can present a node token on a CP read. A runner's hook runs as `jaah`, which
    holds none, so its read is a certain 401 — counted by prod's X-Agent-Token failure alarm
    — and is skipped as an unreadable answer instead."""
    if control_plane_api is None:
        return False
    try:
        import env_helpers  # a dev checkout's tokenless CP accepts every caller (`ALLOW_INSECURE_AGENT`)

        return bool(control_plane_api.NODE_TOKEN or control_plane_api.resolve_node_token()
                    or env_helpers.insecure_agent_enabled())
    except Exception:  # noqa: BLE001 — an unreadable token is no token
        return False


def _ac_log(*parts: str) -> None:
    """Durable, low-noise decision log (instrumentation only — NEVER gates the close decision)."""
    log_dir = environment.hook_log_dir()
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        return
    stamp = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    pane = os.environ.get("TMUX_PANE", "no-pane")
    try:
        with open(log_dir / "autoclose-check.log", "a") as f:
            f.write(f"{stamp} [{pane}] {' '.join(parts)}\n")
    except OSError:
        pass


def _decline(reason: str) -> bool:
    _ac_log(reason)
    return False


# One record is well under 1K, so a 64K tail always spans several complete lines.
_TRANSCRIPT_TAIL_BYTES = 65536


def _last_record_ts(path: str) -> float | None:
    """Epoch of the LAST RECORD APPENDED to a Claude Code transcript (its `timestamp` field), or None
    when no dateable record can be read from the tail.

    Not mtime: a parked transcript keeps being rewritten with no new content, so mtime reported a
    finished runner as live and held its slot. Only the tail is read; undateable lines are skipped. None
    means fall back to mtime — the conservative direction, since mtime is never older than the last append."""
    try:
        with open(path, "rb") as f:
            if os.fstat(f.fileno()).st_size > _TRANSCRIPT_TAIL_BYTES:
                f.seek(-_TRANSCRIPT_TAIL_BYTES, os.SEEK_END)
            tail = f.read()
    except OSError:
        return None
    for line in reversed(tail.splitlines()):
        try:
            record = json.loads(line)
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue  # the seek landed mid-record, or this line is not a record
        if not isinstance(record, dict):
            continue
        stamp = record.get("timestamp")
        if not isinstance(stamp, str):
            continue
        try:
            # `fromisoformat` rejects `Z` before 3.11; the fleet floor is 3.9.
            parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
        except ValueError:
            continue
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)  # naive stamps are UTC in this format
        return parsed.timestamp()
    return None


def _transcript_activity(path: str) -> tuple[float | None, str]:
    """(epoch of `path`'s last activity, which signal it came from). `None` means BOTH reads failed, and
    the caller must treat that as uncertainty — never as "idle"."""
    content_ts = _last_record_ts(path)
    if content_ts is not None:
        return content_ts, "record"
    return break_cause.safe_mtime(path), "mtime"


def _dedup(items: list[str]) -> list[str]:
    """`items`, deduped preserving first-seen order (`agent_config_dirs()` deliberately does not)."""
    seen: set[str] = set()
    out: list[str] = []
    for it in items:
        if it not in seen:
            seen.add(it)
            out.append(it)
    return out


def _current_transcripts(cwd: str, config_dirs: list[str]) -> tuple[list[tuple[str, str]], list[str]]:
    """(transcripts, blind_dirs). `transcripts`: (config_dir, transcript_path) for every CONFIG_DIRS entry
    that has a project dir AND a session transcript for `cwd`; a genuinely absent project dir is skipped.
    `blind_dirs`: config dirs whose project dir is present but unreadable — the signal is UNAVAILABLE, not
    absent, and the caller declines on it."""
    transcripts: list[tuple[str, str]] = []
    blind: list[str] = []
    for cd in config_dirs:
        proj = break_cause.project_dir(cwd, cd)
        if not agent_config_dirs.dir_present(proj):
            continue  # genuinely absent — this account has never touched this cwd
        if not os.path.isdir(proj):
            blind.append(cd)  # present but unreadable — signal unavailable, not "nothing to see"
            continue
        tj = break_cause.session_transcript_path(proj)
        if tj:
            transcripts.append((cd, tj))
    return transcripts, blind


def _sentinel_session_path(sentinel: str) -> str:
    """The sidecar `arm` records the arming session's own transcript path into, alongside the sentinel.
    A sibling file because `check` asserts the sentinel is a bare integer."""
    return sentinel + ".session"


def _own_transcript_at_arm(cwd: str) -> str:
    """The ARMING session's own transcript — the newest `.jsonl` in ITS OWN project dir. Called only from
    `arm()`, where `$CLAUDE_CONFIG_DIR` is that session's own Stop-hook env and `cwd` is the PINNED root
    (never raw `$PWD`, or no baseline is recorded). "" when unresolvable — `check` then declines on the
    missing baseline."""
    config_dir = os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude")
    proj = break_cause.project_dir(cwd, config_dir)
    if not agent_config_dirs.dir_present(proj) or not os.path.isdir(proj):
        return ""
    return break_cause.session_transcript_path(proj) or ""


# `_occupant_state`'s two non-state outcomes.
_OCCUPANT_NOT_A_SLOT = "\0not-a-slot"    # cwd is not a pool worktree at all — occupancy does not apply
_OCCUPANT_UNKNOWN = "\0unknown"          # cwd IS a slot but its DB record could not be read — uncertainty


def _same_path(a: str, b: str) -> bool:
    """Do these two spellings name the same directory? `realpath` both (symlinked homes; the CP stores
    its own spelling) and drop a trailing slash. No `os.stat`, so an unreadable path still compares."""
    def _norm(p: str) -> str:
        return (os.path.realpath(p).rstrip("/") or "/") if p else ""

    na, nb = _norm(a), _norm(b)
    return bool(na) and na == nb


def _occupant_state(cwd: str) -> str:
    """The CP's `worktrees.state` for `cwd`'s pool slot — `"free"`/`"dispatching"`/`"live"`/`"parked"` —
    or `_OCCUPANT_NOT_A_SLOT` when `cwd` is not a pool worktree (occupancy does not apply: a main clone or
    an ad-hoc dir is not a slot), or `_OCCUPANT_UNKNOWN` when `cwd` DOES resolve to a slot but no row
    could be matched to it.

    Resolved BY PATH against `worktrees.path`, not a `(project, number)` guess: `slot_of`'s project half
    is wrong whenever `projects.name` differs from the repo name, which would answer UNKNOWN forever.
    Node-scoped (`this_node_id()`), because two nodes with the same `jaah_home` share slot paths.
    Every uncertain answer — transport failure, no matching row, `control_plane_api is None` — is
    `_OCCUPANT_UNKNOWN`, since this gates a kill."""
    slot = agent_config_dirs.slot_of(cwd)
    if slot is None:
        return _OCCUPANT_NOT_A_SLOT
    if not _cp_reads_authenticated():
        return _OCCUPANT_UNKNOWN
    try:
        rows = control_plane_api.agent_api_pool_worktrees(control_plane_api.this_node_id())
    except Exception as exc:  # noqa: BLE001 — an unreachable API is uncertainty, not a hook crash
        _ac_log(f"check note=occupant-probe-failed path={cwd} err={exc!r}")
        rows = None
    if rows is None:
        return _OCCUPANT_UNKNOWN
    for row in rows:
        if not isinstance(row, dict):
            continue
        if _same_path(cwd, str(row.get("path") or "")):
            state = str(row.get("state") or "")
            _ac_log(
                f"check note=occupant-resolved-by-path path={cwd} "
                f"project={row.get('project')} number={row.get('number')} state={state or '<none>'}"
            )
            return state
    _ac_log(f"check note=occupant-no-row-for-path path={cwd} rows={len(rows)}")
    return _OCCUPANT_UNKNOWN


# `creds_state`/`CREDS_*` live in `agent_config_dirs.py`.


def _account_enabled(config_dir: str) -> bool | None:
    """Whether CONFIG_DIR is a known, ENABLED credential account, per `control_plane_api.account_enabled` — the
    DB SSOT lookup by config_dir. None on an unreachable API, a failed import, or an unknown config dir."""
    if not _cp_reads_authenticated():
        return None
    try:
        return control_plane_api.account_enabled(config_dir)
    except Exception:  # noqa: BLE001 — an unreachable API is uncertainty, not a hook crash
        return None


# `claude agents --json`'s own bound, mirroring `reconcile.accounts_probe._default_agents_probe`'s 15s.
_AGENTS_PROBE_TIMEOUT = 15
# The probe itself, as `reconcile.accounts_probe._agents_probe_argv`: the default provider's session listing (the catalogue's
# `sessions.list_command`), since the homes probed are its.
_AGENTS_PROBE_ARGV = agent_providers.session_list_argv(agent_providers.DEFAULT_PROVIDER)


# Never inherited by the probe. `NODE_TOKEN` is a belt; `ANTHROPIC_API_KEY` is load-bearing (see
# `_probe_claude_agents`): the variable the Claude CLI reads an API key from.
_STRIPPED_FROM_PROBE = frozenset({"NODE_TOKEN", "ANTHROPIC_API_KEY"})


def _probe_claude_agents(config_dir: str) -> subprocess.CompletedProcess | None:
    """One `claude agents --json` probe against CONFIG_DIR. None means the probe could not be run at all
    — the caller's uncertainty, never "no busy session".

    In-process only, never delegated to the session user: this hook runs inside a `jaah`-owned tmux
    window, so it already is the session user. Run manually as `jaahd`, a delegated branch would hit
    tmux ≥3.3's cross-uid `access not allowed` exit 0 and block the full timeout.

    `NODE_TOKEN` is stripped as a belt (a node's `jaah` holds none). `ANTHROPIC_API_KEY` is
    stripped because this probe is a child of a runner that may hold one: inheriting it would authenticate
    as that runner's account while pointed at another account's config dir — unleased spend and a
    reading that is not evidence about the named account. A dir with no usable credential then fails,
    which the ladder already models."""
    env = {k: v for k, v in os.environ.items() if k not in _STRIPPED_FROM_PROBE}
    env["CLAUDE_CONFIG_DIR"] = config_dir
    try:
        return subprocess.run(
            list(_AGENTS_PROBE_ARGV), capture_output=True, text=True,
            timeout=_AGENTS_PROBE_TIMEOUT, env=env,
        )
    except (subprocess.SubprocessError, OSError):
        return None


def _root_holds_this_session(cwd: str, config_dirs: list[str], session_id: str) -> bool:
    """Whether ANY known account's project dir for `cwd` actually holds `<session_id>.jsonl` — i.e. whether
    the root this ladder is keyed on is THIS session's own tree, positively confirmed rather than assumed.
    A wrong root is usually POPULATED, so finding transcripts proves nothing. `session_id` is the
    launch-minted `--session-id` (see `hooks/runner-status.py::_runner_session_id`), and Claude Code names
    the transcript after it. ANY account counts (the roster is unioned); unreadable dirs never match."""
    for cd in config_dirs:
        if os.path.isfile(os.path.join(break_cause.project_dir(cwd, cd), session_id + ".jsonl")):
            return True
    return False


def _is_same_tree(a_cwd: str, root: str) -> bool:
    """Whether a reported agent cwd and this checker's pinned root name the SAME session's tree — either
    one being an ancestor of the other, compared on PATH BOUNDARIES.

    Bidirectional: `claude agents --json` reports a session's live cwd, which drifts below the pinned
    root, so a busy agent ABOVE the checker's cwd is the common case. On path boundaries after
    `normpath` (`.../acme/8` never matches `.../acme/80`). A relative cwd never matches; rung 4 pre-screens
    non-absolute cwds into a decline, and `root` is never empty."""
    a = os.path.normpath(a_cwd)
    b = os.path.normpath(root)
    if a == b:
        return True
    # Root `/` matches everything — only ever in the declining direction.
    return a.startswith(b.rstrip("/") + "/") or b.startswith(a.rstrip("/") + "/")


def _check_impl(sentinel: str, linger: int, on_kill: str = "") -> tuple[bool, bool]:
    """(killed, stop_retrying). `check()` is the public bool wrapper; `stop_retrying` is read only by
    `_armloop`, to tell ordinary uncertainty from a verdict that belongs to another session (rung 0)."""
    now = int(time.time())
    try:
        with open(sentinel) as f:
            last_raw = f.read()
    except OSError:
        return _decline(f"check skip=unreadable-sentinel sentinel={sentinel}"), False
    last_raw = last_raw.strip()
    if not last_raw.isdigit():
        return _decline("check skip=garbled-sentinel"), False  # never read as "0"
    last = int(last_raw)
    sentinel_age = now - last
    if sentinel_age < linger:
        return _decline(f"check skip=fresh-sentinel sentinel_age={sentinel_age} linger={linger}"), False  # newer Stop re-armed
    tmux_pane = os.environ.get("TMUX_PANE", "")
    if not tmux_pane:
        return _decline(f"check skip=no-tmux-pane sentinel_age={sentinel_age}"), False  # nothing to kill
    cutoff = now - linger
    # Every rung below is keyed on the pinned root (see the module docstring).
    cwd = break_cause.worktree_root()

    config_dirs = _dedup(agent_config_dirs.agent_config_dirs(read_cp=_cp_reads_authenticated()))
    # `root=` is what the ladder keyed on, `env_pwd=` the raw `$PWD` beside it (the old `pwd=` field is
    # renamed so a grep never gets a value that no longer decides). `check env ` prefix is what the
    # runbook anchors on. Logged ahead of every rung.
    _ac_log(f"check env root={cwd} env_pwd={os.environ.get('PWD', '')} "
            f"config_dirs={','.join(config_dirs)} sentinel_age={sentinel_age}")

    # Precondition P, first in the ladder: rung 0a's lookup is keyed on the root too, so a foreign root
    # would diagnose the wrong slot. Confirms against the one identity the launcher and Claude Code agree
    # on (`$CONTROL_RUNNER_SESSION_ID` == `--session-id` == the transcript's filename).
    runner_sid = runner_env.runner_session_id()
    if not runner_sid:
        # No identity to confirm against: a logged no-op (a silently disabled guard is undiagnosable),
        # never a weaker substitute check.
        _ac_log("check note=no-runner-session-id "
                "(precondition P is a no-op this check — no CONTROL_RUNNER_SESSION_ID to confirm the root against)")
    elif not _root_holds_this_session(cwd, config_dirs, runner_sid):
        return _decline(
            f"check skip=root-not-my-session root={cwd} session={runner_sid} "
            f"config_dirs={','.join(config_dirs)} (no account's project dir for the pinned root holds this "
            "session's own transcript — every rung below would be judging another session's evidence)"
        ), False

    # Rung 0a: a slot that resolves but has no readable record is uncertainty, not "nobody is in it".
    # A cwd that is not a pool slot falls through. The CP does not model "a human is attached", so the
    # leftover-checker case rests on rung 0b's session-identity check alone.
    state = _occupant_state(cwd)
    if state == _OCCUPANT_UNKNOWN:
        return _decline(f"check skip=occupant-unknown cwd={cwd}"), False

    transcripts, blind_dirs = _current_transcripts(cwd, config_dirs)
    if blind_dirs:
        # Present-but-unreadable is unavailable, not absent. Before rung 0b: a blind account might hold
        # the live session, so 0b's cross-account comparison cannot be trusted either.
        return _decline(f"check skip=unreadable-project-dir config_dirs={','.join(blind_dirs)}"), False

    if not transcripts:
        # No account has a project dir for the pinned root: rungs 0b/2/3 are all blind at once, and the
        # busy probe alone must not carry a kill (both logged `no-proj-dir` lines before this decline
        # were followed by a kill). With the leased root as key, this means the pin, roster or dirs are
        # wrong. Ahead of rung 0b so the operator reads the real cause. Does not stop the ladder: it is a
        # signal, not ownership, so a permanent one exhausts into the loud `armloop gave-up`.
        return _decline(
            f"check skip=no-proj-dir root={cwd} config_dirs={','.join(config_dirs)} "
            "(no known account has a project dir for the pinned worktree root — rungs 0b/2/3 are all "
            "blind at once, so the busy probe alone must not be allowed to carry the kill)"
        ), False

    # Rung 0b: is the window's occupant still the session that ARMED this checker? A brand-new session
    # reads idle on every signal below. Ordered by `_transcript_activity` (last record, not raw mtime,
    # which a rewritten stale transcript would win). A different newest session declines AND stops.
    try:
        with open(_sentinel_session_path(sentinel)) as f:
            armed_session = f.read().strip()
    except OSError:
        armed_session = ""
    if not armed_session:
        # A decline, not a no-op: `arm()` runs on every path, so no baseline means its write failed or no
        # project dir resolved, and 0b cannot tell who occupies the window. Does not stop the ladder (a
        # signal, not ownership).
        return _decline("check skip=no-session-baseline (arm() recorded no identity baseline, so rung 0b "
                        "cannot confirm this window still holds the session this checker is about)"), False
    newest = max(transcripts, key=lambda ct: (_transcript_activity(ct[1])[0] or -1.0))
    if newest[1] != armed_session:
        return _decline(
            f"check skip=session-changed armed_session={armed_session} current_session={newest[1]}"
        ), True

    # 2+3: freshness unioned across every account; the newest .jsonl in a project dir is that account's
    # live session. Any account reading fresh declines.
    for cd, tj in transcripts:
        activity, signal = _transcript_activity(tj)
        if activity is None:
            return _decline(f"check skip=undateable-transcript transcript={tj} config_dir={cd}"), False
        if activity >= cutoff:
            return _decline(
                f"check skip=fresh-transcript sentinel_age={sentinel_age} signal={signal} "
                f"activity_age={int(now - activity)} config_dir={cd}"
            ), False
        sid = os.path.basename(tj)[: -len(".jsonl")]
        proj = break_cause.project_dir(cwd, cd)
        for d in (
            os.path.join(proj, sid),
            os.path.join(f"/tmp/claude-{os.getuid()}", os.path.basename(proj), sid),
        ):
            if not os.path.isdir(d):
                continue
            for root, _dirs, files in os.walk(d):
                for name in files:
                    fpath = os.path.join(root, name)
                    try:
                        if os.stat(fpath).st_mtime >= cutoff:
                            return _decline(f"check skip=fresh-artifact dir={d} config_dir={cd}"), False
                    except OSError:
                        continue
    # 4: the busy probe against every account. The array gate matters: a parse failure or non-array must
    # not read as "no match". Catches an account mid-turn; a human idling at the prompt is rungs 0/2/3's job.
    if not _AGENTS_PROBE_ARGV or not shutil.which(_AGENTS_PROBE_ARGV[0]):
        return _decline("check skip=no-claude"), False
    for cd in config_dirs:
        proc = _probe_claude_agents(cd)
        if proc is None:
            return _decline(f"check skip=agents-probe-failed config_dir={cd}"), False
        if proc.returncode != 0:
            return _decline(f"check skip=agents-probe-failed config_dir={cd}"), False
        try:
            agents = json.loads(proc.stdout)
        except json.JSONDecodeError:
            return _decline(f"check skip=agents-probe-not-array config_dir={cd}"), False
        if not isinstance(agents, list):
            return _decline(f"check skip=agents-probe-not-array config_dir={cd}"), False
        if not agents:
            # An rc-0 empty array from an account with no confirmable credential looks identical to
            # "confirmed no session", so it is gated on the credential state.
            try:
                creds = agent_config_dirs.creds_state(cd)
            except Exception as exc:  # noqa: BLE001 — it imports daemon code lazily; a failure is uncertainty
                return _decline(f"check skip=creds-probe-failed config_dir={cd} error={type(exc).__name__}"), False
            if creds == agent_config_dirs.CREDS_UNREADABLE:
                return _decline(f"check skip=creds-unreadable config_dir={cd}"), False
            if creds == agent_config_dirs.CREDS_NONE:
                # `CREDS_NONE` alone must not decline: a disabled account is credential-less by design.
                # An ENABLED account with none is the bug class; `None` (unknown) falls through.
                if _account_enabled(cd):
                    return _decline(f"check skip=creds-missing-enabled-account config_dir={cd}"), False
        # A busy agent is positive evidence of life, so an unreadable entry or uncomparable cwd is
        # uncertainty, never "some other tree".
        for a in agents:
            if not isinstance(a, dict):
                return _decline(f"check skip=agents-entry-unreadable config_dir={cd}"), False
            a_cwd = a.get("cwd")
            if a.get("status") != "busy":
                continue
            if not isinstance(a_cwd, str) or not os.path.isabs(a_cwd):
                # Absent, empty, or not comparable to an absolute root (`_is_same_tree`'s own docstring).
                return _decline(
                    f"check skip=busy-session-uncomparable-cwd agent_cwd={a_cwd!r} root={cwd} config_dir={cd}"
                ), False
            if _is_same_tree(a_cwd, cwd):
                return _decline(f"check skip=busy-session agent_cwd={a_cwd} root={cwd} config_dir={cd}"), False

    # ── CONFIRMED IDLE: every decline path returned above. Teardown, in order:
    _ac_log(f"check verdict=idle killed=yes sentinel_age={sentinel_age} on_kill={'set' if on_kill else ''}")
    try:
        os.remove(sentinel)  # only on an actual close — never on a decline
    except OSError:
        pass
    try:
        os.remove(_sentinel_session_path(sentinel))  # the identity baseline dies with the sentinel
    except OSError:
        pass

    # (a) on-kill first, while the runner is alive and the task still `active`, so a terminal settle
    #     completes before the pane goes. Best-effort; a self-contained shell string.
    if on_kill:
        subprocess.run(["bash", "-c", on_kill], capture_output=True)

    # (b) Targeted self-exit: a parked runner never self-exits. Safe because it fires only after the full
    #     idle ladder.
    if shutil.which("tmux"):
        killed = subprocess.run(["tmux", "kill-pane", "-t", tmux_pane], capture_output=True)
        if killed.returncode != 0:
            subprocess.run(["tmux", "kill-window", "-t", tmux_pane], capture_output=True)
    return True, False


def check(sentinel: str, linger: int, on_kill: str = "") -> bool:
    """True only on an actual kill, False on every decline. The stable bool entry point for the CLI and
    tests; `_armloop` reads `_check_impl`'s extra stop-retrying signal."""
    return _check_impl(sentinel, linger, on_kill)[0]


def _armloop(sentinel: str, linger: int, on_kill: str, retries: int, interval: int, armed_at: str) -> None:
    time.sleep(linger)
    attempt = 0
    while True:
        killed, stop = _check_impl(sentinel, linger, on_kill)
        if killed:
            break  # killed -> this checker's job is done
        if stop:
            # Rung 0 fired: this checker's verdict belongs to another session; retrying would apply it
            # anyway. Logged distinctly from the silent "a newer Stop re-armed" break below.
            _ac_log(f"armloop stopped early sentinel={sentinel} armed_at={armed_at} attempt={attempt} "
                    f"— the live session in this window is no longer the one that armed this checker")
            break
        attempt += 1
        if attempt > retries:
            # Exhausted, and nothing re-arms it: the slot stays held until a human reaps it. Never silent.
            _ac_log(f"armloop gave-up attempts={attempt - 1} sentinel={sentinel} armed_at={armed_at} "
                    f"— nothing will re-arm this session; its slot stays HELD until a human reaps it")
            break
        try:
            with open(sentinel) as f:
                cur = f.read().strip()
        except OSError:
            break
        if cur != armed_at:
            break  # a newer Stop re-armed its own checker
        time.sleep(interval)


def arm(sentinel: str, linger: int, on_kill: str = "") -> None:
    # 30 × 60s = one full linger (VISIBLE_LINGER=1800) after the first check: a decline the ladder
    # cannot outlast is a permanently held slot.
    retries = int(os.environ.get("AUTOCLOSE_RETRIES", "30"))
    interval = int(os.environ.get("AUTOCLOSE_RETRY_INTERVAL", "60"))
    armed_at = str(int(time.time()))
    with open(sentinel, "w") as f:
        f.write(armed_at)
    # The rung-0b baseline, resolved here where the env is still the arming session's own. Written
    # atomically: the detached checker reading a torn file would be indistinguishable from "no baseline".
    session_path = _sentinel_session_path(sentinel)
    root = break_cause.worktree_root()
    try:
        tmp_path = f"{session_path}.tmp{os.getpid()}"
        with open(tmp_path, "w") as f:
            f.write(_own_transcript_at_arm(root))
        os.replace(tmp_path, session_path)
    except OSError:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
    _ac_log(f"arm sentinel={sentinel} linger={linger} retries={retries} armed_at={armed_at} on_kill={'set' if on_kill else ''}")
    # Detach fully (stdio to devnull, new session) so the sleeper never holds the Stop hook's pipes.
    # cwd="/": a live process cwd'd in a worktree makes `worktree_mechanics.reset.reset_to_base` refuse it, so a
    # sleeping checker would pin its own slot. `PWD` is re-pinned to the resolved root so the child cannot
    # disagree with arm()'s baseline; `JAAH_WORKTREE` rides the environment copy unchanged (asserted by
    # test on the on-kill `bash -c` child's env).
    # Windows ignores `start_new_session`: there its own process group keeps the hook console's Ctrl+C off it.
    devnull = subprocess.DEVNULL
    child_env = {**os.environ, "PWD": root}
    posix = hasattr(os, "setsid")
    subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve()), "_armloop", sentinel, str(linger),
         on_kill, str(retries), str(interval), armed_at],
        stdin=devnull, stdout=devnull, stderr=devnull, start_new_session=posix,
        creationflags=0 if posix else getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
        cwd="/",
        env=child_env,
    )


def main(argv: list[str]) -> int:
    if not argv:
        print("usage: autoclose-check.py {arm|check} ...", file=sys.stderr)
        return 2
    cmd, rest = argv[0], argv[1:]
    if cmd == "arm":
        if len(rest) < 2:
            print("usage: autoclose-check.py arm <sentinel> <linger> [on-kill]", file=sys.stderr)
            return 2
        arm(rest[0], int(rest[1]), rest[2] if len(rest) > 2 else "")
        return 0
    if cmd == "check":
        if len(rest) < 2:
            print("usage: autoclose-check.py check <sentinel> <linger> [on-kill]", file=sys.stderr)
            return 2
        return 0 if check(rest[0], int(rest[1]), rest[2] if len(rest) > 2 else "") else 1
    if cmd == "_armloop":
        # Internal verb only: the detached child arm() spawns to run the sleep+retry loop.
        sentinel, linger, on_kill, retries, interval, armed_at = rest
        _armloop(sentinel, int(linger), on_kill, int(retries), int(interval), armed_at)
        return 0
    print("usage: autoclose-check.py {arm|check} ...", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
