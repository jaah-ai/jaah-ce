#!/usr/bin/env python3
"""The fleet runner-LAUNCH path: lifecycle-hook settings assembly, the per-dispatch `launch.sh`,
`vis_trust` onboarding seeding, the tmux window spawn, and `launch_refusal` (an occupancy finding,
OBSERVATION ONLY — the veto is `app/slot_state.py::lease`). Every interpolated arg is `printf %q`-quoted.

The hook scripts under `apps/jaahd/hooks/` run in the runner's hook runtime; this module only
assembles the settings that point at them.

All but `vis_trust`, `vis_window`, `launch_refusal` and the terminal pops is pure string assembly, and
`node_naming` is the one rewrite of the composed prompts every launch applies.
`vis_window` spawns tmux with a SANITIZED env (`tmux_safe_env`), never `os.environ`: the shared tmux
server freezes its first client's env into every window."""
from __future__ import annotations

import json
import ntpath
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import threading

import agents  # THE runner-adapter registry: every vendor's argv/env/hook rendering, incl. Claude's
import host_platform  # which OS this node is; Windows-only launch.sh lines hang off `is_windows()`
from host_platform import windows  # Git Bash + bundle paths (importable anywhere: no native code at import)
import runner_clis  # the bundle's runner-CLI dirs a Windows launch.sh puts on PATH (`_windows_path_lines`)
import runner_env  # python_cmd(): every hook line spells the node's interpreter, never a PATH lookup
import session_user  # the uid-split seam: which tmux server a window lands on, and whose uid it runs as
import shared_storage  # the single home of the shared credential store's path + env-var spelling
import agent_providers  # which account layer a provider uses
from agent_providers import DEFAULT_PROVIDER  # what `accounts.provider`'s server_default means
from env_helpers import default_tmux_session, TMUX_SESSION_ENV_VAR  # single-home joins
from jaah_env import env_get  # renamed env vars

# No vendor argv lives here (PRD R2.2): `agents.get_adapter(provider)` owns it and raises when unregistered.

# VISIBLE_LINGER default — inactivity seconds after the last Stop before autoclose may kill the window.
# Must exceed the longest legitimate inter-turn gap (`Stop` is per turn); the cost is cosmetic, since the
# lease is released independently of this timer.
_VISIBLE_LINGER_DEFAULT = 1800

# The set of characters bash 5.2 `printf %q` leaves UNescaped mid-string (empirically derived from
# `printf %q` over printable ASCII 32-126): ASCII letters + digits + this safe punctuation. Everything
# else printable gets a leading backslash; `#`/`~` are additionally escaped ONLY when leading.
_SHQ_SAFE = frozenset(
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789#%+-./:=@_~"
)


# ── the composed prompts, in this node's paths ───────────────────────────────────────────────────────
# The untrusted-data fence the composer wraps the task data in (`dispatch_compose.fencing._FENCE_BEGIN`/
# `_FENCE_END`; a repo test pins the spellings equal). The composer defangs any copy inside
# the data, so the first BEGIN and the last END are the fence.
_FENCE_BEGIN = "--- BEGIN UNTRUSTED TASK DATA ---"
_FENCE_END = "--- END UNTRUSTED TASK DATA ---"


# The installed standard as the control plane names it: the Linux package's root (its `docs/`, `tools/`). A
# macOS or Windows node ships the same files at the same relative paths in its bundle (the platform bundle builder
# stages the jaahd manifest) under its own `lib_root`, where `/usr/lib/jaahd` does not exist.
_LINUX_LIB_ROOT = "/usr/lib/jaahd/"


def respell_lib_root(text: str) -> str:
    """`text` with the Linux package root respelled to this node's bundle; unchanged on Linux. Forward
    slashes on Windows: Git Bash and Claude Code's file tools both read `C:/Program Files/...`."""
    native = host_platform.paths().lib_root.replace("\\", "/").rstrip("/") + "/"
    return text if native == _LINUX_LIB_ROOT else text.replace(_LINUX_LIB_ROOT, native)


def node_naming(prompt: str) -> str:
    """`prompt` with this node's installed standard's root respelled outside the untrusted fence
    (`respell_lib_root`). Idempotent. The fenced task data is a record, left as typed, exactly as the
    composer leaves it; a fence with no END leaves everything after its BEGIN alone."""
    begin = prompt.find(_FENCE_BEGIN)
    if begin < 0:
        return respell_lib_root(prompt)
    end = prompt.rfind(_FENCE_END)
    if end < begin:
        return respell_lib_root(prompt[:begin]) + prompt[begin:]
    return respell_lib_root(prompt[:begin]) + prompt[begin:end] + respell_lib_root(prompt[end:])


# ── printf %q port — the exact shell-quoting applied to every interpolated arg ─────────────────────────

def shq(s: str) -> str:
    """Port of bash 5.2 `printf %q` (a.k.a. `sh_backslash_quote`). Returns `s` quoted so a shell
    re-reads it as the identical single word — byte-parity with the bash launch path, which escapes
    every path/arg it bakes into the `--settings` strings and the launch.sh wrapper.

    Rules, verified against bash 5.2.21:
      * empty string        → `''`
      * any control byte (<0x20 or 0x7f) present → ANSI-C `$'...'` form (\\n/\\t/\\r/\\NNN octal)
      * otherwise           → backslash-escape every char NOT in _SHQ_SAFE; additionally escape a
                              LEADING `#` or `~` (safe mid-string, unsafe at position 0).

    NOT NESTING-SAFE — use `shq_nested` inside a single-quoted host string: there both arms break out
    of the host quote and escaped metacharacters go live (white-box finding H3)."""
    if s == "":
        return "''"
    if any(ord(c) < 0x20 or ord(c) == 0x7f for c in s):
        out = ["$'"]
        for c in s:
            if c == "\n":
                out.append("\\n")
            elif c == "\t":
                out.append("\\t")
            elif c == "\r":
                out.append("\\r")
            elif c == "\\":
                out.append("\\\\")
            elif c == "'":
                out.append("\\'")
            elif ord(c) < 0x20 or ord(c) == 0x7f:
                out.append("\\%03o" % ord(c))
            else:
                out.append(c)
        out.append("'")
        return "".join(out)
    chars = []
    for i, c in enumerate(s):
        if c in _SHQ_SAFE and not (i == 0 and c in "#~"):
            chars.append(c)
        else:
            chars.append("\\" + c)
    return "".join(chars)


def _export(name: str, value: str) -> str:
    """`export NAME=<v>`, the value shell-quoted."""
    return f"export {name}={shq(value)}"


def shq_nested(s: str) -> str:
    """`shq` for the ONE position `shq` cannot serve: interpolated between the quotes of an enclosing
    single-quoted shell string (`bash -lc '… <here> …'`). Returns text containing NO single quote that
    the INNER shell re-reads as the identical word — or raises `ValueError`.

    Raises ValueError on a `'` or any control byte (inside `'...'` nothing can spell them without ending
    the host quote); never silently sanitizes. The caller fails the launch loudly."""
    if "'" in s:
        raise ValueError(
            f"cannot nest {s!r} inside a single-quoted shell string: a single quote would close the "
            f"enclosing quote and hand the rest of the value to the host shell as syntax"
        )
    bad = next((c for c in s if ord(c) < 0x20 or ord(c) == 0x7f), None)
    if bad is not None:
        raise ValueError(
            f"cannot nest {s!r} inside a single-quoted shell string: the control byte "
            f"{bad!r} needs bash's ANSI-C $'...' form, whose leading $' closes the enclosing quote"
        )
    if s == "":
        # `''` would close and reopen the host quote; the caller must omit an empty argument instead.
        raise ValueError("cannot nest an empty value inside a single-quoted shell string — omit the argument")
    # `~` escaped everywhere: in an assignment bash expands `~` after `:`, so `/opt/a:~root/x` would point
    # the session at another account. `shq` keeps the leading-only rule for byte-parity.
    return "".join(
        c if (c in _SHQ_SAFE and c != "~" and not (i == 0 and c == "#")) else "\\" + c
        for i, c in enumerate(s)
    )


# ── Command-string builders ────────────────────────────────────────────────────────────────────────────

def join_cmds(*parts: str) -> str:
    """Join non-empty shell commands with `` ; `` (drops empties cleanly)."""
    out = ""
    for p in parts:
        if not p:
            continue
        if out:
            out += " ; "
        out += p
    return out


# Stripped from a tmux window name, payload and all: a terminal escape sequence (CSI; OSC, DCS, SOS, PM and APC up to
# their BEL/ST terminator, the next ESC or the end, in their 7-bit and C1 forms; any other ESC sequence), then every
# C0/C1 control and bidi override/isolate left (newline/CR split `list-windows` lines; NUL truncates; a bidi
# override makes the shown name read other than it is). The console's tmux pushes the window
# name to the browser's terminal as an OSC title (`console.py` `set-titles`), so a Title carrying `\x1b]0;…\x07` must
# reach no terminal. Stripped, not rejected: the Title is operator free text. Not `accounts._strip_escapes`: that
# scraper keeps an unterminated sequence's text and knows only OSC-8 and SGR.
_WINDOW_NAME_ESCAPES = re.compile(
    r"(?:\x1b\[|\x9b)[0-?]*[ -/]*[@-~]?"
    r"|(?:\x1b[]PX^_]|[\x90\x98\x9d\x9e\x9f])[^\x07\x1b\x9c]*(?:\x07|\x1b\\|\x9c)?"
    r"|\x1b[ -/]*[0-~]?"
    r"|[\x00-\x1f\x7f-\x9f\u202a-\u202e\u2066-\u2069]")


def _window_safe(text: object) -> str:
    """`text` with every terminal escape sequence, control character and bidi override removed
    (`_WINDOW_NAME_ESCAPES`)."""
    return _WINDOW_NAME_ESCAPES.sub("", str(text))


def slot_ref(project: str, number: object) -> str:
    """The `{Project Name}` / `{Project Name}-{Worktree Id}` half of a slot's identity — the main clone
    is a bare `{Project Name}`, never `{Project}-0`.

    The one rendering of that rule. Takes `number`, not the path: under `repos/<project id>/{main,N}` the
    basename carries no project name. A legacy `acme-9`-shaped number passes through unchanged."""
    project = _window_safe(project)
    slot = _window_safe(number if number is not None else "").strip().strip("/")
    if not slot or slot in ("main", "0"):
        return project
    if slot == project or slot.startswith(f"{project}-"):
        return slot
    return f"{project}-{slot}"


def slot_window_name(project: str, number: object, title: str) -> str:
    """THE ONE COMPOSER of a pool slot's tmux window name: `{Project Name}-{Worktree Id}|{Title}`.
    `title` arrives resolved by the CP (`app/slot_title.py::resolved_title`); no node-side fallback. An
    empty title yields the bare slot ref. Untruncated: the bound is tmux's `window-status-format`."""
    ref = slot_ref(project, number)
    title = _window_safe(title)
    # tmux expands `#{…}`, `#(…)` and `#[…]` where a window name is shown (`window-status-format`, the console's
    # `set-titles-string`): `##` is its literal `#`. Here, not in `slot_ref`: that ref is the slot's identity too.
    return (f"{ref}|{title}" if title else ref).replace("#", "##")


def task_slot_title(task_id: object, task_title: str) -> str:
    """The `{TaskId}|{Task Description}` Title a dispatched slot carries — the node-side half of a
    CROSS-BOUNDARY FORMAT CONTRACT.

    The CP writes the same string (`app/slot_title.py::resolved_title` branch 2); the two cannot share
    code, so `tests/test_fleet_launch.py` pins the literal. Empty title degrades to the bare id."""
    task_title = (task_title or "").strip()
    return f"{task_id}|{task_title}" if task_title else str(task_id)


# ── project_name: the resolved `projects.name`, not a path-derived guess ───────────────────────────────
# A path yields the REPO name, wrong whenever `projects.name` differs. Exported at the head of the
# SessionEnd chain for `hooks/user-session-end.py`, which restates the literal (a hook must not import
# daemon code; pinned by tests on both sides). Empty is degraded (path fallback), not a refusal.
PROJECT_NAME_ENV = "JAAH_PROJECT_NAME"
# Its `projects.id` beside it, exported the same way: the identity the hook's report addresses the slot by
# (a name is display only). Empty ⇒ the report carries the name alone and the CP falls back.
PROJECT_ID_ENV = "JAAH_PROJECT_ID"


def vis_user_session_end_cmd(worktree: str, role: str, *, hooks_dir: str) -> str:
    """The SessionEnd teardown hook for an INTERACTIVE `role=user` pool session — the interactive
    counterpart of the runner's on-kill `settle` + role-clear pair.

    `role == "user"` only: a user session gets no on-kill action (`VISIBLE_AUTOCLOSE=0`), so SessionEnd
    is its only terminal signal; a runner's session end is not a decision about its task. `--worktree`
    is explicit so a session started in a subdirectory reports the right slot. Empty when worktree/role
    don't match or the script is missing."""
    if not worktree or role != "user":
        return ""
    use = os.path.join(hooks_dir, "user-session-end.py")
    if not os.path.isfile(use):
        return ""
    return f"{shq(runner_env.python_cmd())} {shq(use)} settle --worktree {shq(worktree)} >/dev/null 2>&1 || true"


def vis_runner_status_cmd(sub: str, *, hooks_dir: str, args: tuple[str, ...] = ()) -> str:
    """The runner-lifecycle hook for one subcommand — `start` (SessionStart claim), `settle` (the
    confirmed-idle terminal ladder), `stopfailure` (the StopFailure API-error-while-alive report), or one
    of the two interactive-chat doors, `console_exit` (SessionEnd) and `console_resume`
    (UserPromptSubmit). Only ever wired for a role=runner session (see build_settings). Empty if
    runner-status.py is missing.

    The redirect leaves stdin alone: `console_exit` and `heartbeat` read the event JSON from it, and only
    `export`s and `autoclose-check.py arm` (which reads none) precede them in their chains
    (`runner-status.py::_hook_payload` is the other half).

    `args` follow the subcommand, each quoted — today only `console_exit`'s quit signature."""
    if not sub:
        return ""
    rs = os.path.join(hooks_dir, "runner-status.py")
    if not os.path.isfile(rs):
        return ""
    tail = "".join(f" {shq(a)}" for a in args)
    return f"{shq(runner_env.python_cmd())} {shq(rs)} {shq(sub)}{tail} >/dev/null 2>&1 || true"


# Wall-clock ceiling on the turn-boundary quick gate, matching the harness hook's `"timeout": 60`.
_QUICK_GATE_TIMEOUT_SECS = 60


def vis_quick_gate_cmd(worktree: str) -> str:
    """The project's QUICK GATE at a turn boundary, for a runner whose CLI does not inherit the fleet's
    account-level harness layer. Empty when there is no worktree to run it in.

    Gated on `AgentAdapter.inherits_harness_hooks()` so no vendor gets it twice. Runs the gate runner
    (`run_gate.py`), the single entry point (the command is the project's `gate_quick_cmd` column).

    Reports, never blocks: how a vendor treats a turn-end hook's exit code or stdout is unmeasured, and a
    wrong guess wedges turns or feeds text to the model. The pre-commit hook still enforces the gate."""
    if not worktree:
        return ""
    # `1>&2`: a hook's stdout may be fed to the model as a message. `|| true`: report, never fail the turn.
    # Path derived from this file, not the worktree: another project has no gate runner.
    gate = Path(__file__).resolve().parents[2] / "tools" / "run_gate.py"
    # 78 is `run_gate.py`'s "gate could not be RESOLVED" code, printed apart so it cannot read as a pass;
    # the timeout keeps a wedged gate from holding a turn boundary open.
    return (
        f"( cd {shq(worktree)} 2>/dev/null || exit 0; "
        f"[ -f {shq(str(gate))} ] || exit 0; "
        f"timeout {_QUICK_GATE_TIMEOUT_SECS} {shq(runner_env.python_cmd())} {shq(str(gate))} --tier quick 1>&2; rc=$?; "
        f'[ "$rc" = 78 ] && echo "quick gate: NOT RUN here (unresolved project gate) — not a failure; '
        f'the pre-commit hook still enforces it" 1>&2; '
        f'[ "$rc" = 124 ] && echo "quick gate: TIMED OUT after {_QUICK_GATE_TIMEOUT_SECS}s — not a '
        f'failure; the pre-commit hook still enforces it" 1>&2 ) || true'
    )


def vis_lane_stop_cmd() -> str:
    """The merge lane's turn-end refusal (`check_lane_stop.py`), in the lane run's chain. Stdout is its
    block JSON (the adapters translate it, as for every Claude-shaped hook); `|| true` keeps a crash from ever
    failing a turn. Empty when the tool is not installed."""
    script = Path(__file__).resolve().parents[2] / "tools" / "check_lane_stop.py"
    if not script.is_file():
        return ""
    return f"{shq(runner_env.python_cmd())} {shq(str(script))} || true"


def vis_guard_cmd(mode: str) -> str:
    """One Architectural Guard hook entry (`architectural_guard.py`'s docstring is its contract): `--pre-tool`
    or `--session-start`. `-I` so the worktree cannot shadow what it imports. `|| true`: the guard fails OPEN (G-1),
    so neither a crash nor a missing script may ever turn into a blocking exit code; a deny is its stdout JSON.
    Empty when the script is not installed."""
    script = Path(__file__).resolve().parents[2] / "tools" / "architectural_guard.py"
    if not script.is_file():
        return ""
    return f"{shq(runner_env.python_cmd())} -I {shq(str(script))} {shq(mode)} || true"


def _lifecycle_chains(
    settings_path: str,
    extra: str = "",
    worktree: str = "",
    role: str = "",
    *,
    hooks_dir: str,
    linger: object = None,
    autoclose: object = None,
    account_dir: str = "",
    project_name: str = "",
    securestorage_dir: str = "",
    provider: str = DEFAULT_PROVIDER,
    guard: bool = False,
    lane: bool = False,
) -> agents.LifecycleChains:
    """WHAT must run at each point of a runner's life, composed as vendor-neutral shell commands.

    Which hooks run, in what order and under which role gate is a fleet fact, here; rendering is the
    adapter's (PRD R2.2). No vendor event name may appear in this function.

    Start = runner-status start. Stop = autoclose arm + heartbeat (so a spurious StopFailure self-heals).
    Every runner-status subcommand is `role == "runner"` STRICTLY (it reports against a TASK). StopFailure
    is wired only here, never in the harness settings (the harness hook-scope check cannot see this
    file). The on-kill action is `extra` + settle — never a slot release. SessionEnd is per role and never
    the other's: user ⇒ user-session-end settle, runner ⇒ `console_exit` (a runner releasing its own slot
    is forbidden). UserPromptSubmit ⇒ `console_resume`, sharing the event with the harness's context
    router (different scripts, both run).

    `linger`/`autoclose`/`project_name` fall back to `VISIBLE_LINGER`/`VISIBLE_AUTOCLOSE`/
    `JAAH_PROJECT_NAME`. `securestorage_dir` has no env fallback: it is resolved per launch (`shared_storage.securestorage_dir`).

    `guard`: whether this session is under the Architectural Guard — the launch ran `guard_bundle.prepare_launch`
    first and says so explicitly, because a chain never reads the daemon env. True adds `vis_guard_cmd`'s two
    entries, for BOTH roles: the per-write check as `on_pre_tool` and the rule brief appended to `on_session_start`.
    False changes nothing, byte for byte (an unavailable org's launch).

    `lane`: this runner is the merge lane's own run — `vis_lane_stop_cmd`, the refusal to end a turn while PRs
    are queued, closes its turn-end chain. False changes nothing, byte for byte."""
    if linger is None:
        linger = os.environ.get("VISIBLE_LINGER", str(_VISIBLE_LINGER_DEFAULT))
    if autoclose is None:
        autoclose = os.environ.get("VISIBLE_AUTOCLOSE", "1") != "0"
    if not project_name:
        project_name = (env_get(PROJECT_NAME_ENV) or "")
    # Sentinel: sibling of the settings file (`${settingsf%.json}.stop_ts`).
    stem = settings_path[:-5] if settings_path.endswith(".json") else settings_path
    sentinel = stem + ".stop_ts"

    rs_start = rs_settle = rs_heartbeat = rs_wallcheck = ""
    rs_console_exit = rs_console_resume = ""
    # `role == "runner"` strictly, not truthy: a `role=user` console tab has a role too, and none of these
    # task reports may ever fire from it.
    if role == "runner":
        rs_start = vis_runner_status_cmd("start", hooks_dir=hooks_dir)
        rs_settle = vis_runner_status_cmd("settle", hooks_dir=hooks_dir)
        rs_heartbeat = vis_runner_status_cmd("heartbeat", hooks_dir=hooks_dir)
        # A usage wall now arrives mid-turn as a grace note, not an error (`break_cause.USAGE_LIMIT_NOTE_KEY`):
        # checked before every tool call so the session is replaced when it lands. Claude Code's alone (it is
        # the CLI behind every "omc" account layer, DeepSeek's included); no other CLI writes the note.
        if agent_providers.account_layer(provider or DEFAULT_PROVIDER) == "omc":
            rs_wallcheck = vis_runner_status_cmd("wallcheck", hooks_dir=hooks_dir)
        # The interactive-chat doors, wired for every runner: the CP knows the persona and refuses the
        # rest (one logged no-op call), so the persona rule is not restated here.
        # `console_exit` is told what an operator quit looks like on THIS CLI (`AgentAdapter.quit_signature`):
        # the vendors' `SessionEnd` reasons do not agree, and two of them do not separate a quit alone.
        quit_sig = agents.get_adapter(provider).quit_signature()
        rs_console_exit = vis_runner_status_cmd(
            "console_exit", hooks_dir=hooks_dir,
            args=("--reason", quit_sig.reason)
            + (("--pane-marker", quit_sig.pane_marker) if quit_sig.pane_marker else ()))
        rs_console_resume = vis_runner_status_cmd("console_resume", hooks_dir=hooks_dir)
    # `role == "runner"` strictly here too.
    rs_stopfailure = vis_runner_status_cmd("stopfailure", hooks_dir=hooks_dir) if role == "runner" else ""
    # on-kill order: caller `extra`, then the terminal `settle`. Nothing releases the slot here: an idle
    # runner is not a decision about its task; release is the CP's alone.
    onkill = join_cmds(extra, rs_settle)

    closecmd = ""
    autoclose_py = os.path.join(hooks_dir, "autoclose-check.py")
    if autoclose and os.path.isfile(autoclose_py):
        closecmd = (
            f"{shq(runner_env.python_cmd())} {shq(autoclose_py)} arm {shq(sentinel)} {linger} {shq(onkill)}"
        )
    # Hook subprocesses do not reliably inherit the account pin (Claude Code drops CLAUDE_CONFIG_DIR), so
    # autoclose would probe the wrong account and false-reap a busy runner. Pinned at the head of each
    # chain; the variable comes from the adapter.
    cfg_export = " ; ".join(
        f"export {name}={shq(value)}"
        for name, value in agents.get_adapter(provider).env(
            config_dir=account_dir, worktree=worktree).items()
    )
    # Travels with `cfg_export`: when the token is in the store the config dir holds none, so without it the idle
    # ladder sees an empty session list and kills a busy runner. No dir ⇒ no export (defined-but-empty
    # resolves to `~/.claude`).
    sec_export = (
        f"export {shared_storage.SECURESTORAGE_ENV}={shq(securestorage_dir)}" if securestorage_dir else ""
    )
    # The hooks locate the transcript by the LEASED WORKTREE ROOT, not `$PWD` (`break_cause.worktree_root()`).
    # No worktree ⇒ no export, leaving the `$PWD` fallback.
    wt_export = _export("JAAH_WORKTREE", worktree) if worktree else ""
    # The turn-boundary gate for a vendor without the harness layer; Claude's True answer keeps this chain
    # byte-identical to the goldens. Runner only.
    gate_cmd = (
        vis_quick_gate_cmd(worktree)
        if role == "runner" and not agents.get_adapter(provider).inherits_harness_hooks() else ""
    )
    lane_stop = vis_lane_stop_cmd() if role == "runner" and lane else ""
    stopcmd = join_cmds(cfg_export, sec_export, wt_export, closecmd, rs_heartbeat, gate_cmd, lane_stop)
    # No chain claims, releases or reconciles a slot: a runner is refused every pool verb, and the
    # daemon's ~5s projection pass already observes every slot.
    start = join_cmds(cfg_export, sec_export, wt_export, rs_start, vis_guard_cmd("--session-start") if guard else "")
    # No `wt_export` in SessionEnd (`user-session-end.py` gets `--worktree`). The project-name pin rides
    # only when that hook is wired.
    use_cmd = vis_user_session_end_cmd(worktree, role, hooks_dir=hooks_dir)
    proj_export = (
        _export(PROJECT_NAME_ENV, project_name) if (project_name and use_cmd) else ""
    )
    project_id = (env_get(PROJECT_ID_ENV) or "").strip()
    proj_id_export = _export(PROJECT_ID_ENV, project_id) if (project_id.isdigit() and use_cmd) else ""
    # One hook per role, mutually exclusive by construction; `join_cmds` drops the empty one.
    # `console_exit` needs none of the pins (stdin + inherited env); the account pair rides regardless.
    endcmd = join_cmds(cfg_export, sec_export, proj_export, proj_id_export, use_cmd, rs_console_exit)
    # UserPromptSubmit un-parks a chat the Stop heartbeat parked; no pins (its hook reads inherited env).
    promptcmd = rs_console_resume
    # StopFailure's break classification needs the account + worktree pins to find the right transcript.
    stopfailurecmd = join_cmds(cfg_export, sec_export, wt_export, rs_stopfailure) if rs_stopfailure else ""
    # The guard reads the tool payload off the shared stdin; `wallcheck` reads none and runs after it.
    guard_pre_tool = vis_guard_cmd("--pre-tool") if guard else ""
    # The account + worktree pins locate the transcript, as for StopFailure; the worktree pin alone serves the guard.
    pre_tool = join_cmds(cfg_export, sec_export, wt_export, guard_pre_tool, rs_wallcheck) if rs_wallcheck else (
        join_cmds(wt_export, guard_pre_tool) if guard_pre_tool else "")
    # Vendor-neutral names; the event mapping is the adapter's (`agents/base.py::LifecycleChains`).
    return agents.LifecycleChains(
        on_session_start=start,
        runner=role == "runner",
        on_turn_end=stopcmd,
        on_session_end=endcmd,
        on_error=stopfailurecmd,
        on_prompt_submit=promptcmd,
        # The worktree pin, so a write is judged against the leased repo even after the agent `cd`s out of it.
        on_pre_tool=pre_tool,
    )


def guard_chains(worktree: str, *, guard: bool) -> "agents.LifecycleChains":
    """A runner's chains under the ACP host: the Architectural Guard's two hooks and nothing else. The settle,
    heartbeat, autoclose and StopFailure chains are the host's jobs there (`acp/host.py`), so wiring them too
    would settle a task twice. `runner=True` all the same: it names the session a fleet runner's (today no
    renderer reads it; the memory tools a runner has are its task's arm's, in `launch.sh`)."""
    wt_export = _export("JAAH_WORKTREE", worktree) if worktree else ""
    start = vis_guard_cmd("--session-start") if guard else ""
    pre_tool = vis_guard_cmd("--pre-tool") if guard else ""
    return agents.LifecycleChains(
        runner=True,
        on_session_start=join_cmds(wt_export, start) if start else "",
        on_pre_tool=join_cmds(wt_export, pre_tool) if pre_tool else "",
    )


def _config_dir(kwargs: dict) -> str:
    """The leased account's config home, read back off the forwarded kwargs (one home for the value).

    `account_dir` is keyword-only in `_lifecycle_chains`; if that changes, this must too. Empty = no
    account was leased."""
    return str(kwargs.get("account_dir") or "")


def build_settings(
    settings_path: str, *args, provider: str = DEFAULT_PROVIDER, **kwargs
) -> tuple[str | None, str]:
    """The hook-settings payload for one dispatch: `(file content or None, the flag that installs it)`.
    PURE — does not write the file (`write_settings` does).

    `_lifecycle_chains` composes what runs; the adapter renders it. `provider` defaults to
    `DEFAULT_PROVIDER` — a data default (`accounts.provider`'s server_default), not a vendor fallback:
    `agents.get_adapter` raises for an unregistered provider."""
    chains = _lifecycle_chains(settings_path, *args, provider=provider, **kwargs)
    return agents.get_adapter(provider).render_hooks(
        settings_path=settings_path, chains=chains, config_dir=_config_dir(kwargs))


def write_settings(settings_path: str, *args, provider: str = DEFAULT_PROVIDER,
                    mode: int | None = None, **kwargs) -> str:
    """Side-effecting counterpart of build_settings: write the payload to `settings_path` and return the
    flag (empty when there is nothing to install). The write is the adapter's (`write_hooks`), via
    `session_user`; a refused write raises, aborting the dispatch. `mode` is the caller's (e.g.
    `fleet_dispatch.META_FILE_MODE`); `None` = the seam's default."""
    chains = _lifecycle_chains(settings_path, *args, provider=provider, **kwargs)
    return agents.get_adapter(provider).write_hooks(
        settings_path=settings_path, chains=chains, mode=mode,
        config_dir=_config_dir(kwargs))


# ── Per-dispatch wrapper text ──────────────────────────────────────────────────────────────────────────

# Where a runner's CLI lives (`$HOME` is the session user's): first on a runner's and a console tab's PATH.
RUNNER_PATH_PREFIX = "$HOME/.local/bin"


def build_launch_sh(
    *,
    auth_line: str,
    hub_root: str,
    worktree: str,
    settings_flag: str,
    sys_file: str,
    task_file: str,
    provider: str = DEFAULT_PROVIDER,
    rc_flag: str = "",
    model_args: tuple[str, ...] = (),
    session_args: tuple[str, ...] = (),
    exec_lines: "tuple[str, ...] | None" = None,
) -> str:
    """The `launch.sh` wrapper `fleet_dispatch.launch_runner` generates. Exports the session env
    (`auth_line`) and NO credential — the file is 0755 on disk. Sources compose-env-guard.sh
    (heals NODE_ID and compose identity against a stale shell/tmux value), then the adapter's exec lines,
    whole (`agents.get_adapter(provider).exec_line`, which raises when unregistered).

    The PATH prefix makes the adapter's bare binary name resolvable in a pane (no trusted ambient PATH;
    never the daemon's own — `session_user.delegated_path`). `$HOME` is the session user's.
    On Windows the PATH line is `_windows_path_lines()` instead.

    `exec_lines` given replaces the adapter's (an SDK agent's `jaah-acp-host`); the agent
    arguments are then unread."""
    guard = os.path.join(hub_root, "tools/compose-env-guard.sh")
    if exec_lines is None:
        exec_lines = tuple(agents.get_adapter(provider).exec_line(
            sys_file=sys_file, task_file=task_file, settings_flag=settings_flag,
            model_args=model_args, session_args=session_args, rc_flag=rc_flag,
        ))
    if host_platform.is_windows():
        path_lines = _windows_path_lines()
    else:
        bundle = bundle_bin()
        path_lines = [f'export PATH="{RUNNER_PATH_PREFIX}{":" + bundle if bundle else ""}:$PATH"']
    return "\n".join(
        [
            "#!/usr/bin/env bash",
            *path_lines,
            auth_line,
            *((f"source {shq(guard)}", f"compose_env_guard {shq(worktree)} NODE_ID")
              if sources_compose_guard(guard) else ()),
            *exec_lines,
        ]
    ) + "\n"


def sources_compose_guard(guard: str) -> bool:
    """Does a pane source the compose env guard at `guard`? Always on a fleet node; on a community-edition node
    only where present, since its bundle ships no `tools/` and a
    missing guard only printed "No such file" and "command not found" in every pane."""
    import edition  # noqa: PLC0415 — read at call time (`apps/jaahd/edition.py`)

    return not edition.IS_CE or os.path.isfile(guard)


def bundle_bin() -> str:
    """The bundle dirs a macOS pane must be handed, `:`-joined, else `""`: its `bin` (docker, jaah-forge, python3,
    jaah-acp-host), then its Node's (`runtime/node/bin`, the interpreter of `claude-agent-acp` and of every Node
    hook a CLI runs — without it a hook printed "node: command not found"); the order
    `jaah_toolchain_env` (the macOS bundle's `jaah-common.sh`) gives the bundle's own launchers. A
    community-edition Linux node is handed its `bin` alone, where the bundle's staging step rendered
    `jaah-acp-host`, `jaah-forge`, `jaah-call` and `jaah-credential`, at the package path too (the container
    node), since a console tab's `bash -lc` resets PATH from `/etc/profile`; its Node is on the system PATH.

    tmux gives every new macOS pane the fixed `/usr/bin:/bin:/usr/sbin:/sbin`, whatever the server's or
    `-e`'s PATH, and a console tab's `bash -lc` adds only what `path_helper` lists — neither has the
    bundle (seen on hardware). Read by `build_launch_sh` and `clauded_wt.py`'s console tab."""
    lib = host_platform.paths().lib_root
    if host_platform.is_macos():
        return f"{lib}/bin:{lib}/runtime/node/bin"
    import edition  # noqa: PLC0415 — read at call time (`apps/jaahd/edition.py`)

    if host_platform.is_linux() and edition.IS_CE:
        return f"{lib}/bin"
    return ""


def msys_path(path: str) -> str:
    """A Windows path in the MSYS spelling Git Bash keeps on its `:`-separated PATH: `C:\\a b\\c` →
    `/c/a b/c`. PURE. A path with no drive letter (already MSYS, or UNC) only has its separators flipped:
    a `C:` left in place would split on the `:` and put two bogus entries on PATH."""
    drive, rest = ntpath.splitdrive(path)
    rest = rest.replace("\\", "/")
    if len(drive) == 2 and drive[1] == ":":
        return f"/{drive[0].lower()}{rest}"
    return drive.replace("\\", "/") + rest


def _dq(text: str) -> str:
    """`text` safe between double quotes (`$`, backtick, `"` and `\\` escaped); `shq` cannot serve there."""
    return re.sub(r'([$`"\\])', r"\\\1", text)


def _windows_path_lines() -> list[str]:
    """launch.sh's PATH line on a Windows node, in place of Linux's one PATH line.

    PATH front, in order: the bundle's `bin` (tmux.exe, docker.exe), Git's dirs (`usr\\bin` first: tmux.exe
    binds to its msys-2.0.dll), the runner account's `.local\\bin` (`claude.exe`, put there by
    `claude_cli_windows.ensure`), the bundle's runner-CLI dirs (`runner_clis.windows_cli_dirs`, the same
    search the node attests from), Node.js (`windows.NODEJS_DIR`: the pinned ACP adapter's shim and every Node
    hook), then the bundled interpreter's own dir (`python3.exe`, which every hook,
    gate and the control MCP server is spelled as). Spelled as MSYS paths because
    bash's PATH is `:`-separated. The runner account's home, not `$HOME`: a service-started bash may not
    have one. Which bash a CLI runs its tools in is the adapter's (`agents.claude.ClaudeAdapter.env`)."""
    paths = windows.paths()
    dirs = [ntpath.join(paths.lib_root, "bin"), *windows.git_path_dirs(),
            ntpath.join(paths.home_root, ".local", "bin"), *runner_clis.windows_cli_dirs(), windows.NODEJS_DIR,
            ntpath.dirname(paths.python)]
    prefix = ":".join(_dq(msys_path(d)) for d in dirs)
    return [f'export PATH="{prefix}:$PATH"']


# ── Onboarding seed ────────────────────────────────────────────────────────────────────────────────────

def _read_seed_config(path: str) -> dict:
    """One `.claude.json` this daemon is about to re-seed, as a dict.

    Not a bare `open()`: claude rewrites the file 0600, so the daemon's group read fails. Re-assert 0660 as
    the owner and read again — swallowing would seed `{}` over a real config, propagating would block
    dispatch on the node. A still-unreadable file after the repair propagates."""
    try:
        with open(path) as f:
            return json.load(f)
    except PermissionError:
        err = session_user.chmod_as_session_user(path, 0o660)
        if err:
            raise RuntimeError(
                f"vis_trust: {path} is unreadable by this daemon and the mode repair failed: {err}"
            ) from None
        with open(path) as f:
            return json.load(f)


def _trust_keys(project: str) -> list[str]:
    """The `projects` keys Claude Code may look `project` up under. On Windows it keys a project by its
    path with forward slashes (`C:/ProgramData/…`), so the as-given spelling alone left the trust dialog
    up and the unattended runner parked on it (a real PC)."""
    # Claude Code keys a project by its RESOLVED path: on macOS /var is /private/var, so seeding only the
    # given path left every runner waiting on the trust dialog (seen on hardware).
    keys = list(dict.fromkeys((project, os.path.realpath(project))))
    if host_platform.is_windows():
        drive, rest = ntpath.splitdrive(ntpath.normpath(project))
        slashed = drive + rest.replace("\\", "/")
        if slashed not in keys:
            keys.append(slashed)
    return keys


# Launches run concurrently (`reconcile.execute.execute_intents`): `vis_trust` is a read-modify-write of shared files,
# and `vis_window`'s has-session/new-session pair a check-then-create on one tmux session.
_TRUST_LOCK = threading.Lock()
_SPAWN_LOCK = threading.Lock()


def vis_trust(project: str, config_dirs: tuple[str, ...] = ()) -> None:
    """Pre-accept Claude Code's workspace-trust + first-run onboarding so an interactive runner doesn't
    block on the trust dialog / theme picker. Seeds `$project` into the SESSION USER's `~/.claude.json`
    plus each pool config dir's `.claude.json`: sets `projects[$project].hasTrustDialogAccepted = true`
    and `hasCompletedOnboarding = true`, preserving any other existing fields.

    The SESSION user's home, not the daemon's (`/var/lib/jaahd`); `expanduser("~")` when there is no
    uid split. Written as the session user, which keeps rewriting it. A refused write raises.

    Serialized in this daemon (`_TRUST_LOCK`) and replaced atomically, so two launches cannot lose each
    other's entry and a reader never sees a half-written file it would rewrite as `{}`."""
    with _TRUST_LOCK:
        _seed_trust(project, config_dirs)


def _seed_trust(project: str, config_dirs: tuple[str, ...]) -> None:
    home = session_user.session_home() or os.path.expanduser("~")
    cfgs = [os.path.join(home, ".claude.json")]
    for d in config_dirs:
        if d:
            cfgs.append(os.path.join(d, ".claude.json"))
    for cj in cfgs:
        try:
            data = _read_seed_config(cj)
        except (FileNotFoundError, ValueError):
            data = {}
        if not isinstance(data, dict):
            data = {}
        projects = data.setdefault("projects", {})
        for key in _trust_keys(project):
            entry = dict(projects.get(key) or {})
            entry["hasTrustDialogAccepted"] = True
            projects[key] = entry
        data["hasCompletedOnboarding"] = True
        err = session_user.replace_file_as_session_user(cj, json.dumps(data), mode=0o660)
        if err:
            raise RuntimeError(f"vis_trust: could not write {cj}: {err}")


# ── tmux spawn — the ONLY side-effecting process entry point ─────────────────────────────────────────────

def tmux_safe_env() -> dict:
    """The sanitized env every tmux-invoking subprocess must use (`env_helpers._tmux_safe_env`, a strict
    allowlist): an auto-started tmux server freezes its first client's env into every window."""
    from env_helpers import _tmux_safe_env

    return _tmux_safe_env()


def env_window_args(env: dict) -> list[str]:
    """The `-e K=V` flags a `new-window`/`new-session` needs so the window's env does not come from the
    SERVER's frozen one (`env_helpers.tmux_window_env_args`)."""
    from env_helpers import tmux_window_env_args

    return tmux_window_env_args(env)


def tmux_session(session: str | None = None) -> str:
    """The tmux session to target: SESSION (the CP's choice) when given, else `$<TMUX_SESSION_ENV_VAR>`,
    else `env_helpers.default_tmux_session()` (`NODE_NAME`). The single home of that chain and the only
    reader of the env var. Raises `env_helpers.NoNodeIdentity` when nothing resolves.

    Resolve once and pass the value down: `_tmux_safe_env()` narrows a launcher's env, so re-deriving
    on each side could disagree."""
    return session or os.environ.get(TMUX_SESSION_ENV_VAR) or default_tmux_session()


# ── The launch-refusal PROBE — observation only, not a veto ────────────────────────────────────────────
# Logged, never gating: a node refusing a CP-assigned launch recreates a live-lock. The veto is
# `app/slot_state.py::lease` (fail-OPEN on an unobserved slot); these probes are the fail-CLOSED read.

# tmux stderr meaning provably no server — zero live panes, not a failed query
# (`console._tmux_query_is_failure`).
_TMUX_NO_SERVER_RUNNING = "no server running on "
_TMUX_NO_CURRENT_TARGET = "no current target"


def _live_pane_refusal(worktree: str, env: dict, socket: str) -> str:
    """"" when no live agent pane is cwd'd inside `worktree`; the reason to REFUSE otherwise — including
    when the tmux query could not be trusted to answer at all.

    `|term`/`|web` windows are skipped (a human's shell or a view); the /proc cwd scan catches a shell."""
    try:
        listing = subprocess.run(
            session_user.tmux_cmd(socket, "list-panes", "-a", "-F",
                                  "#{pane_current_path}\t#{window_name}"),
            env=env, capture_output=True, text=True, timeout=15,
        )
    except Exception as exc:  # noqa: BLE001 — a timeout says nothing about liveness, so it must refuse
        return f"the tmux pane query failed ({exc}) — liveness is UNKNOWN"
    if listing.returncode != 0:
        stderr = (listing.stderr or "").strip()
        if _TMUX_NO_SERVER_RUNNING in stderr or _TMUX_NO_CURRENT_TARGET in stderr:
            return ""
        return (f"the tmux pane query failed (rc={listing.returncode}: {stderr or 'no stderr'}) — "
                f"liveness is UNKNOWN")
    for line in (listing.stdout or "").splitlines():
        pane_cwd, _, name = line.partition("\t")
        if name.endswith("|term") or name.endswith("|web"):
            continue
        # Windows: tmux reports the MSYS spelling (`/c/...`) of a `C:\\...` worktree; `is_under` reads both.
        if host_platform.is_under(pane_cwd, worktree):
            return f"tmux window {name!r} already has a live pane inside this worktree"
    return ""


def launch_refusal(worktree: str, *, env: dict | None = None, cwd_probe=None, pane_probe=None) -> str:
    """"" when `worktree` is demonstrably free to launch a runner into; the reason to REFUSE otherwise.
    OBSERVATION ONLY: the caller (`fleet_dispatch.launch_runner`) logs a non-empty answer and
    launches anyway — the veto is `app/slot_state.py::lease` (fail-OPEN on an unobserved slot), not this.

    Two fail-closed probes: the /proc live-cwd scan (`worktree_mechanics.processes.has_live_cwd_process`;
    PROBE_FAILED refuses) and the tmux pane scan, for a process tree this daemon cannot see into.

    `cwd_probe`/`pane_probe` are test seams; production takes the defaults."""
    if not worktree:
        return "no worktree path — refusing to launch a runner into an unknown tree"
    from worktree_mechanics import processes  # stdlib-only leaf; lazy so the pure builders above stay import-light

    probe = cwd_probe or processes.has_live_cwd_process
    try:
        live_cwd = probe(worktree)
    except Exception as exc:  # noqa: BLE001 — a probe that crashed cannot clear the tree
        return f"the live-cwd probe raised ({exc}) — liveness is UNKNOWN"
    if live_cwd == processes.PROBE_FAILED:
        return "the live-cwd /proc probe could not answer — liveness is UNKNOWN"
    if live_cwd:
        return "a LIVE process has its cwd inside this worktree"
    if env is None:
        env = tmux_safe_env()
    return (pane_probe or _live_pane_refusal)(worktree, env, session_user.fleet_socket(env))


def vis_window(
    label: str,
    cwd: str,
    cmd: str,
    pipe_log: str | None = None,
    *,
    session: str | None = None,
    env: dict | None = None,
    out: dict | None = None,
    extra_env: dict | None = None,
) -> str:
    """Adds a labelled window (tab) running CMD to the shared tmux session (create the session if
    needed), optionally teeing the pane to `pipe_log` via `pipe-pane` for crash diagnosability. Returns
    the new pane's PID (`""` if tmux reported none) — half of the launch ack the CP correlates a live
    runner by; see `fleet_dispatch.proc_started_at` for the other half.

    `out`, when given, is filled with `{"window_id", "pane_pid"}`, for a caller that must reach the window
    later without using its NAME. No occupancy check (the interactive `ctl.py tmux-window` wants a second
    window). Every tmux call uses `tmux_safe_env()`; the window's own env is passed with `-e` because
    `new-window` runs under the SERVER's frozen env. `extra_env` adds per-window `-e K=V` pairs beyond the
    allowlisted keys (a folder slot's git ceiling); `{}`/None adds nothing, so every other window's argv is
    unchanged. Refuses when the uid switch applies and the session user's server is not up."""
    session = tmux_session(session)
    target = f"={session}"
    if env is None:
        env = tmux_safe_env()
    # Per-window env, including the RESOLVED session, so a launcher hand-run in the pane defaults to the
    # session it is in (the worktree `.env` may have replaced `NODE_ID`).
    win_env = env_window_args({**env, TMUX_SESSION_ENV_VAR: session})
    for key, value in (extra_env or {}).items():
        win_env += ["-e", f"{key}={value}"]
    # A community-edition node's CLI off-switches and posture paths (`ce_caps.window_env`; {} on a cloud node, so
    # its argv is unchanged): the server's frozen env is an allowlist, so they ride each window.
    import ce_caps  # noqa: PLC0415 — `ce_caps` imports this module
    for key, value in ce_caps.window_env().items():
        win_env += ["-e", f"{key}={value}"]

    # The socket every call below names. "" only on a host with no session user (dev, CI), where tmux's
    # own default label is correct because there is no second uid to land on.
    socket = session_user.fleet_socket(env)
    if session_user.switch_needed() and not session_user.fleet_socket_ready(env):
        # Refuse loudly: a tmux client with no server STARTS one as itself, i.e. the fleet server as the
        # daemon's uid (the privilege the switch removes). Raised: the dispatch already went `active`.
        msg = (
            f"fleet_launch: REFUSING to open tmux window {label!r} in {cwd} — the fleet tmux server for "
            f"user {session_user.session_user()!r} is not running (no socket at "
            f"{session_user.fleet_socket(env)!r}), and a tmux client with no server would start one as "
            f"THIS daemon's uid ({getattr(os, "geteuid", lambda: "n/a")()}), running the session under the wrong user. No runner "
            f"started, though the lease/worktree/Task state says otherwise. Check: "
            f"systemctl status jaah-runner@{session}.service"
        )
        print(msg, file=sys.stderr)
        raise RuntimeError(msg)

    # The create socket alone, not `manage_sockets()`: this picks `new-window` vs `new-session` on the
    # server that call lands on; a drain-era session on the daemon's server must not answer "yes".
    # Always `-P -F`: the window id feeds `pipe-pane`, the pane pid the launch ack.
    fmt = "#{window_id} #{pane_pid}"
    # tmux on Windows is an MSYS program: handed `C:\\…` it silently starts the pane in $HOME instead, and
    # the runner works (and Claude asks to trust) the wrong directory (a real PC).
    start_dir = msys_path(cwd) if host_platform.is_windows() else cwd
    # Check and create as one step (`_SPAWN_LOCK`): two concurrent launches both seeing no session would both
    # run `new-session`, and the second fails after its launch permission is spent.
    with _SPAWN_LOCK:
        has = subprocess.run(
            session_user.tmux_cmd(socket, "has-session", "-t", target), env=env, capture_output=True
        )
        verb = "new-window" if has.returncode == 0 else "new-session"
        if has.returncode == 0:
            create = session_user.tmux_cmd(socket, "new-window", "-t", target, "-n", label, "-c", start_dir,
                                           *win_env, "-P", "-F", fmt, cmd)
        else:
            create = session_user.tmux_cmd(socket, "new-session", "-d", "-s", session, "-x", "220", "-y", "50",
                                           "-n", label, "-c", start_dir, *win_env, "-P", "-F", fmt, cmd)
        created = subprocess.run(create, env=env, capture_output=True, text=True)
    # `capture_output` swallows stderr, so a failed spawn is printed: silent, it reads as a running runner.
    if created.returncode != 0:
        print(
            f"fleet_launch: tmux {verb} FAILED (rc={created.returncode}) for {label!r} in {cwd} — "
            f"no runner started, though the slot/Task state says otherwise: "
            f"{(created.stderr or '').strip() or '(no stderr)'}",
            file=sys.stderr,
        )
    wid, _, pane_pid = (created.stdout or "").strip().partition(" ")
    # Publish the window id: targeting by NAME breaks on a `:` in a Title (tmux 3.6 resolves it to empty
    # with rc=0, indistinguishable from "gone"). Empty when the spawn failed.
    if out is not None:
        out["window_id"] = wid
        out["pane_pid"] = pane_pid
    if has.returncode != 0:
        # No socket-widening here: a group-usable daemon socket is one `tmux -S` away from a pane as
        # `jaahd`. Humans attach with `jaah tmux`.
        subprocess.run(
            session_user.tmux_cmd(socket, "set", "-t", target, "-g", "mouse", "on"),
            env=env, capture_output=True,
        )

    if pipe_log and wid:
        parent = os.path.dirname(pipe_log)
        if parent:
            os.makedirs(parent, exist_ok=True)
        # The create socket only: window ids collide across servers (`@7` is `@7` everywhere).
        subprocess.run(
            session_user.tmux_cmd(socket, "pipe-pane", "-o", "-t", wid, f"cat >> {shq(pipe_log)}"),
            env=env, capture_output=True,
        )
    return pane_pid


# ── Desktop terminal pop ───────────────────────────────────────────────────────────────────────────────
# Only for interactive launchers (via `ctl.py`); an autonomous dispatch never pops a desktop window.

def vis_terminal() -> str:
    """First available desktop terminal emulator. Honors `VISIBLE_TERMINAL`/`FLEET_TERMINAL`; empty
    when none of the candidates resolve on PATH."""
    override = os.environ.get("VISIBLE_TERMINAL") or os.environ.get("FLEET_TERMINAL") or ""
    for t in (override, "gnome-terminal", "x-terminal-emulator", "konsole", "xterm"):
        if t and shutil.which(t):
            return t
    return ""


def _session_var(name: str) -> str:
    """A desktop-session env var possibly missing from a cron/tmux-server env, read from the systemd
    `--user` manager's tracked environment. The live process env wins if already set; best-effort
    empty on any systemctl failure."""
    cur = os.environ.get(name, "")
    if cur:
        return cur
    try:
        out = subprocess.run(
            ["systemctl", "--user", "show-environment"],
            capture_output=True, text=True, timeout=5,
        ).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""
    prefix = f"{name}="
    for line in out.splitlines():
        if line.startswith(prefix):
            return line[len(prefix):]
    return ""


def pop_terminal(title: str, cmd: str) -> None:
    """Pop ONE desktop terminal running CMD (a string handed to `bash -lc`). Best-effort no-op (never
    raises) without a DISPLAY/terminal, so a cron/headless host never wedges on this.
    `VISIBLE_DESKTOP=0`/`FLEET_DESKTOP=0` disables the pop. Never on Windows: an X11/Wayland pop, by `/run/user`."""
    if host_platform.is_windows():
        return
    if os.environ.get("VISIBLE_DESKTOP", os.environ.get("FLEET_DESKTOP", "1")) == "0":
        return
    if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        return
    term = vis_terminal()
    if not term:
        return
    env = dict(os.environ)
    env.setdefault("DISPLAY", ":0")
    bus_path = f"/run/user/{os.getuid()}/bus"
    if os.path.exists(bus_path):
        env["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path={bus_path}"
    xauth = _session_var("XAUTHORITY")
    if xauth:
        env["XAUTHORITY"] = xauth
    desktop = _session_var("XDG_CURRENT_DESKTOP")
    if desktop:
        env["XDG_CURRENT_DESKTOP"] = desktop
    if term == "gnome-terminal":
        argv = [term, f"--title={title}", "--", "bash", "-lc", cmd]
    else:
        argv = [term, "-e", "bash", "-lc", cmd]
    subprocess.Popen(
        argv, env=env, start_new_session=True,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )


# ── The CONTROL PLANE chooses the account, never the node ──────────────────────────────────────────────
# (`app/fleet_verdicts.build_lease_intent`, `app/routes/console.py::_console_account`).

def ensure_terminal(session: str | None = None) -> None:
    """Pop ONE desktop terminal attached to the shared tmux session, only if none already shows it
    (an exact-match `=session` target avoids tmux's bare-name prefix matching). No-op (via
    `pop_terminal`) when desktop pops are disabled/unavailable.

    Reads the socket SET (a human may be attached on the drain-era server); the attach names the fleet
    socket, where new sessions are."""
    env = tmux_safe_env()
    session = tmux_session(session)
    target = f"={session}"
    for socket in session_user.manage_sockets(env):
        has_client = subprocess.run(
            session_user.tmux_cmd(socket, "list-clients", "-t", target),
            env=env, capture_output=True, text=True,
        )
        if (has_client.stdout or "").strip():
            return
    attach = " ".join(shq(a) for a in session_user.tmux_cmd(session_user.fleet_socket(env), "attach"))
    pop_terminal("fleet console", f"{attach} -t {shq(target)}")
