"""Native stack-op handlers (`stack_up`/`stack_down`/`stack_restart`/`stack_rebuild`), run by the node daemon.

The up/down commands are the project's own (`projects.stack_up_cmd`/`stack_down_cmd`, sent by the CP) and
the node has NO default: absent one, the verb needing it FAILS CLOSED. Restart = down then up; rebuild =
`./dc build --no-cache` then up. Which stack a step targets is NOT enforced here — the env names no
compose scoping, but an authored line can re-export it. Every branch is synchronous.

SECURITY: NODE_TOKEN and protected-holder guards run upstream in `commands.py`. The authored lines run
through `bash -lc` UNSANITISED, bounded by the three things `worktree_ops.check` gives `gate_cmd`:
provenance (the CP overwrites both from the project row; only a superadmin authors them), a curated
env allowlist (`_STACK_ENV_KEYS` — `jaah-runner@.service` not listing `node.env` is the other leg), and
a REFUSAL, never a downgrade to the daemon uid (root on a cloud node), when the session-user switch is
needed and unavailable.
"""
from __future__ import annotations

import logging
import os
import shlex
import subprocess
from typing import Callable

import commands as _commands
import runner_env
import session_user
from commands import native_command

log = logging.getLogger("jaahd.stack_ops")

# An ALLOWLIST, never `os.environ` minus a denylist (`worktree_ops._GATE_ENV_KEYS`'s reason: the line is
# operator-authored). The DOCKER_ trio is forwarded only for a host that overrides the endpoint; by default
# the session user's own rootless CLI context decides. HOME/PATH: each branch supplies its own.
_STACK_ENV_KEYS: tuple[str, ...] = (
    "LANG", "LC_ALL", "LC_CTYPE", "TZ",
    "DOCKER_HOST", "DOCKER_TLS_VERIFY", "DOCKER_CERT_PATH",
)


def _stack_env(**overrides: str) -> dict:
    """A stack command's environment, spelled out — `overrides` is the caller's own uid-dependent half.

    Raises if the dict built HERE would carry `COMPOSE_PROJECT_NAME`/`*_PORT`/`*_URL`. It cannot see the
    delegated branch's tmux-server environment, so it is no claim about what the command finally sees."""
    # The node-managed engine first (macOS: `runner_env.docker_env`), so a host override still wins.
    env = runner_env.docker_env()
    env.update({key: os.environ[key] for key in _STACK_ENV_KEYS if key in os.environ})
    env.update(overrides)
    leaked = sorted(k for k in env if k == "COMPOSE_PROJECT_NAME" or k.endswith(("_PORT", "_URL")))
    if leaked:
        raise RuntimeError(f"stack env would carry this process's own compose scoping vars: {leaked}")
    return env


def _own_uid_env() -> dict:
    """`_stack_env` plus THIS process's HOME/PATH, for an in-process step. PATH must be present:
    `subprocess.run(env=…)` resolves argv[0] against the PATH it is handed."""
    return _stack_env(
        HOME=os.environ.get("HOME") or os.path.expanduser("~"),
        PATH=os.environ.get("PATH") or "/usr/local/bin:/usr/bin:/bin",
    )


def _dc_step(run: Callable[..., dict], argv: list[str], cwd: str) -> dict:
    """One `./dc` step, run AS THE SESSION USER where the switch applies, else in-process (dev, CI).

    `jaahd` has no docker access (`session_user.py`, "Running one command as the session user"). Same
    `{exit_code, stdout, stderr}` either way; the delegated path is `session_user.run_capped`'s.
    Switch needed but unavailable is a REFUSAL, as in `worktree_ops.check`."""
    from worktree_mechanics.core import SWITCH_UNAVAILABLE_RC

    if session_user.switch_needed():
        if not session_user.can_run_as_session_user():
            return {"exit_code": SWITCH_UNAVAILABLE_RC, "stdout": "",
                    "stderr": ("stack op: session-user switch required but unavailable — refusing to run "
                               "the project's stack command as the daemon uid; "
                               "check jaah-runner@<node id>.service")}
        # A failed delegation is kept distinct from the command failing, and never retried in-process:
        # that uid is the one the docker socket refuses.
        return session_user.run_capped(argv, cwd=cwd, env=_stack_env(),
                                       timeout=_commands.EXECUTE_TIMEOUT_SECONDS, as_session_user=True)
    try:
        env = _own_uid_env()
    except RuntimeError as exc:
            # Fail closed: `handle_stack` runs in a `to_thread` with no guard, so a raise would kill
            # the handler rather than refuse this op.
        return {"exit_code": -1, "stdout": "", "stderr": f"stack env refused: {exc}"}
    return run(*argv, cwd=cwd, env=env)


@native_command("stack_up", "stack_down", "stack_restart", "stack_rebuild")
def handle_stack(command_type: str, args: dict, run: Callable[..., dict]) -> dict:
    """In-process handler for the four stack ops; every step goes through `_dc_step`, so the CP sees the
    op's real exit code.

    The commands and the path are the CP's (`_resolve_command_args`, from the project row and
    (project, number)); each verb FAILS CLOSED on a missing one — no default, since inventing `./dc up`
    is how a project needing `COMPOSE_PROFILES=web ./dc up` starts the wrong stack."""
    wt_path = str(args.get("path") or "").strip()
    up_cmd = str(args.get("stack_up_cmd") or "").strip()
    down_cmd = str(args.get("stack_down_cmd") or "").strip()

    def _missing(which: str) -> dict:
        return {"exit_code": 1, "stdout": "",
                "stderr": (f"{command_type} needs {which} (the project's stack command, sent by the CP "
                           f"from projects.{which}) — the node has no stack command of its own; "
                           "nothing was run")}

    # A blank path is a refusal: the delegated `cd` is conditional, so the line would run in the tmux
    # server's WorkingDirectory, where a self-contextualised command (`docker compose -p acme-1 down`)
    # SUCCEEDS against whatever it names. Checked before the per-verb checks.
    if not wt_path:
        return {"exit_code": 1, "stdout": "",
                "stderr": (f"{command_type} needs path (the slot's worktree, resolved by the CP from "
                           "(project, number)) — the node will not run a stack command in an unrelated "
                           "directory; nothing was run")}

    # A shell, deliberately: the value is a command LINE (`COMPOSE_PROFILES=web ./dc up`), which `shlex.split`
    # would break; `-l` puts docker on PATH for a systemd-started daemon. Unsanitised — see module docstring.
    if command_type == "stack_up":
        if not up_cmd:
            return _missing("stack_up_cmd")
        return _dc_step(run, ["bash", "-lc", up_cmd], wt_path)

    if command_type == "stack_down":
        # VERBATIM, `-v` included — the asymmetry with `stop_stack`'s volume guard is deliberate: a human
        # pressed a button on one slot, and this is the only way to reset its data on purpose. Pinned:
        # `test_the_operators_stack_down_runs_a_volume_naming_command_verbatim`.
        if not down_cmd:
            return _missing("stack_down_cmd")
        return _dc_step(run, ["bash", "-lc", down_cmd], wt_path)

    if command_type == "stack_restart":
        # Both checked BEFORE the down runs, so a restart never takes the stack down with no way up.
        if not down_cmd:
            return _missing("stack_down_cmd")
        if not up_cmd:
            return _missing("stack_up_cmd")
        down = _dc_step(run, ["bash", "-lc", down_cmd], wt_path)
        if down["exit_code"] != 0:
            return down
        return _dc_step(run, ["bash", "-lc", up_cmd], wt_path)

    if command_type == "stack_rebuild":
        if not up_cmd:
            return _missing("stack_up_cmd")
        # No explicit timeout: `_native_run` clamps any timeout to EXECUTE_TIMEOUT_SECONDS (170s) because
        # a synchronous handler thread cannot be killed; passing one only earns a clamp warning.

        # The build step stays `./dc build --no-cache`: a build is neither the up nor the down column. A
        # project needing a prefix here gets a third column, never a reuse of `stack_up_cmd`.
        build = _dc_step(run, ["./dc", "build", "--no-cache"], wt_path)
        if build["exit_code"] != 0:
            return build
        return _dc_step(run, ["bash", "-lc", up_cmd], wt_path)

    # native_command only routes the four verbs above here; a fifth would be a registry-wiring bug.
    return {"exit_code": -1, "stdout": "", "stderr": f"stack_ops: unhandled command_type {command_type!r}"}


# ── Stack stop — MECHANICS ONLY ───────────────────────────────────────────────────────────────────
# WHEN a stack is idle enough to stop is the CP's call, issued as a `stop_stack` slot intent
# (`app/fleet_verdicts.py::build_stop_stack_intent`, enacted by `reconcile.enact_reset._enact_stop_stack`). This is
# HOW, reached by that intent and by the operator's `stack_down`; both carry the command and fail closed
# without it.


def _names_volume_deletion(tok: str) -> bool:
    """Does this ONE token ask docker to delete volumes? `-v`, `--volumes`, `--volumes=...`, or a
    clustered short group (`-vt 0`).

    A token test, never a substring test: over-refusing (`-f compose-v.yml`) silently disables the idle
    sweep. An accident guard only — `&& docker volume prune -f` or a nested `bash -c` still runs."""
    if tok in ("-v", "--volumes") or tok.startswith("--volumes="):
        return True
    # A clustered short-flag group: ONE leading `-` (not `--`) with `v` among the letters.
    return tok.startswith("-") and not tok.startswith("--") and "v" in tok[1:]


def stop_stack(wt_path: str, down_cmd: str) -> str:
    """Bring a worktree's local stack down WITHOUT deleting data, via the project's own down command.

    This is the UNATTENDED entry point, so a command whose tokens name volume deletion (see
    `_names_volume_deletion`), or that will not tokenise, is REFUSED with nothing run — an accident
    guard, not a sandbox. A blank command or path, or a needed-but-unavailable session-user switch, also
    leaves the stack up (fail closed in the safe direction; the CP re-decides next tick). Best-effort,
    180s, never raises. Returns `"down"` on success, else `"up"`. Env and uid as `_dc_step`."""
    down_cmd = str(down_cmd or "").strip()
    wt_path = str(wt_path or "").strip()
    if not down_cmd:
        log.warning("stop_stack %s: no stack_down_cmd — leaving the stack up rather than guessing one",
                    wt_path)
        return "up"
    if not wt_path:
        # Fail closed as one arm up: the delegated `cd` is conditional, so a blank path runs the line
        # in the tmux server's WorkingDirectory — a stop of somebody else's stack for `-p acme-1 down`.
        log.warning("stop_stack: no worktree path — leaving the stack up rather than running the "
                    "project's down command in an unrelated directory")
        return "up"
    # No volume deletion from the automatic path. An accident guard, not a security control: it restores,
    # for the unattended path only, the data safety `./dc down` gave for free (the wrapper strips `-v`).
    # `app.idle_stacks.stop_idle_stacks` fires this on every idle slot fleet-wide with nobody watching.

    # `comments=True` because `bash -lc` is the tokeniser that matters: `down # -v` is a safe stop.
    try:
        tokens = shlex.split(down_cmd, comments=True)
    except ValueError as exc:
        # "Never raises" (`reconcile.enact_reset._enact_stop_stack` relies on it), and an untokenisable value cannot
        # be vouched for, so it is refused like a volume flag.
        log.error("stop_stack %s: cannot tokenise stack_down_cmd (%s) — leaving the stack up rather than "
                  "running, unchecked for volume deletion, a value the sweep cannot read", wt_path, exc)
        return "up"
    volume_flag = next((tok for tok in tokens if _names_volume_deletion(tok)), None)
    if volume_flag:
        log.error("stop_stack %s: stack_down_cmd names %s — the unattended idle sweep does not delete "
                  "volumes; leaving the stack up (fix projects.stack_down_cmd)", wt_path, volume_flag)
        return "up"
    argv = ["bash", "-lc", down_cmd]
    if session_user.switch_needed():
        if not session_user.can_run_as_session_user():
            log.warning("stop_stack %s: session-user switch required but unavailable — refusing to run "
                        "the project's down command as the daemon uid; leaving the stack up", wt_path)
            return "up"
        try:
            proc = session_user.run_as_session_user(argv, cwd=wt_path, env=_stack_env(), timeout=180)
        except (subprocess.TimeoutExpired, RuntimeError, OSError) as exc:
            log.warning("stop_stack %s (as session user): %s", wt_path, exc)
            return "up"
        return "down" if proc.returncode == 0 else "up"
    try:
        proc = subprocess.run(
            argv, cwd=wt_path, env=_own_uid_env(), timeout=180,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
    except (subprocess.TimeoutExpired, RuntimeError, OSError) as exc:
        # RuntimeError is `_stack_env`'s canary; "never raises" holds, so it fails closed to "up".
        log.warning("stop_stack %s: %s", wt_path, exc)
        return "up"
    return "down" if proc.returncode == 0 else "up"


