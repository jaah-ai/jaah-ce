"""Account family — per-account OMC/HUD/notify/MCP setup and the two-phase Add-Account / Re-auth OAuth
state machine, served in-process by `handle_account_command` (`@native_command`).

Detached login: its own session, stdin a per-slug FIFO opened O_RDWR (never EOF), stdout a log. Polling
windows stay under the 170s bus budget (a native handler in `asyncio.to_thread` cannot be killed). PASTE
feeds the operator's code over the FIFO (`*_submit_code`); AWAIT_CREDENTIALS waits for a device-auth
login's credentials to land on disk (`*_await_login`); the vendor's declared `login_completion` decides,
Claude's included (`paste_code`). The login runs AS THE SESSION USER: the CLI writes credentials owned by
whoever ran it. A uid-split host with no working seam is refused.

SEALED HANDOFF (`login_seal`): the control plane sends a `peer_key` (required), the authorize
URL leaves this node sealed to the browser's key and the pasted code arrives sealed to a per-login key this
node minted (`seal.pem`, 0600, beside the other handover files: the runtime dir, or the daemon-only
`state\\account-add` on Windows, `_account_add_handover_root`) — the control plane relays ciphertext only.
The private key never leaves the node and is destroyed with that dir. Neither the URL nor the code ever
travels in the clear.

SECURITY: never logs/returns a token value, a seal key or a sealed value's plaintext; `_creds_have_token`
checks shape only; `_OAUTH_URL_RE` anchors the brand host for the URL reader; `_account_delete`
containment-checks the path before any rmtree.
"""
from __future__ import annotations

import errno
import contextlib
import json
import logging
import ntpath
import os
import re
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import time
from pathlib import Path

import agent_config_dirs
import agent_providers
import login_seal
import session_user
import environment
import host_platform
import ce_caps  # the community edition's account cap (a no-op on a cloud node)
from host_platform import windows  # importable everywhere: no native call at import
from jaah_env import env_get  # renamed env vars
import runner_credentials
import shared_storage
from agents import base as agent_base, claude_login, login as agent_login
from commands import EXECUTE_TIMEOUT_SECONDS, native_command
import env_helpers
from env_helpers import _norm_dir, jaah_dir, jaah_home, now_iso as _now_iso
import notification_policy
import plugin_repair
from plugin_repair import MARKETPLACES, MCP_PLUGINS, OMC_MARKETPLACE_URL
from reconcile.accounts_probe import _fleet_auth_dirs, account_roster, invalidate_account_roster

log = logging.getLogger(__name__)

# OAuth-wizard bookkeeping lives in the CP (apps/api/app/durable/accounts.py); this node only runs the
# OAuth process and reports its outcome in each command's reply.


# OMC is recorded under a key starting with "oh-my-claudecode@" in <config_dir>/plugins/installed_plugins.json.
# The fleet's plugin set single-homes in `plugin_repair`.
_OMC_MARKETPLACE_URL = OMC_MARKETPLACE_URL


# WHERE THIS ACCOUNT'S TOKEN IS: resolved on this node (`shared_storage.token_dir`) — the token store
# `{jaah-home}/auth/.shared/<slug>` if it holds a credential, else its config dir. A token in the store is not
# under the config dir, so every `claude` invocation must carry `CLAUDE_SECURESTORAGE_CONFIG_DIR` or it opens
# a different, empty store. Every login mints into the store (`_login_store_for`).


def _account_securestorage_dir(config_dir: str) -> str:
    """The token store to pin for an admin verb on CONFIG_DIR, or `""` when its token is in the config dir
    (`shared_storage.securestorage_dir`). Also `provision_accounts`' "token is in the store" test."""
    return shared_storage.securestorage_dir(config_dir)


def _securestorage_env(securestorage_dir: str) -> dict:
    """`{CLAUDE_SECURESTORAGE_CONFIG_DIR: dir}`, or `{}` — the ONE composer for this pin in this module.

    OMITTED, never empty: the CLI resolves a DEFINED-but-empty variable to `~/.claude`, a third store
    (`fleet_dispatch.build_auth_lines`)."""
    return {shared_storage.SECURESTORAGE_ENV: securestorage_dir} if securestorage_dir else {}


def _claude_cli(run, *argv: str, config_dir: str) -> dict:
    """Run one `claude` CLI verb against ONE account's config dir, AS THE UID THAT OWNS IT.

    Every `CLAUDE_CONFIG_DIR`-scoped invocation rewrites `.credentials.json` 0600 owned by whoever ran it,
    so a daemon-forked verb takes the account away from the sessions that use it
    (`env_helpers.atomic_write_text`: the replaced file takes the writer's uid). Returns `_native_run`'s
    `{"exit_code", "stdout", "stderr"}` on both paths, same caps and bus-budget timeout. The delegated env
    is spelled out, never inherited: the daemon's carries `NODE_TOKEN`, which user `jaah` must never
    receive. A delegation that could not run is a FAILED verb (`exit_code` -1), never an in-process retry
    — that retry is the write this stops.
    The credential store is pinned where the token is (`_account_securestorage_dir`): with the token in the
    store the config dir holds none, so an unpinned `claude plugin install` runs UNAUTHENTICATED."""
    store_env = _securestorage_env(_account_securestorage_dir(config_dir))
    # OPEN OBLIGATION: with `store_env` empty, the caller's own `CLAUDE_SECURESTORAGE_CONFIG_DIR` rides in
    # through this spread — another account's store lent to this one. `_login_env` POPS it (the
    # shape to copy); this credential-WRITE site, `reconcile/accounts_probe.py`'s in-process agents probe and
    # `plugin_repair.py`'s install env still omit rather than clear. Each needs its own verification.
    if not session_user.can_run_as_session_user():
        return run(*argv, env={**os.environ, "CLAUDE_CONFIG_DIR": config_dir, **store_env})
    try:
        proc = session_user.run_as_session_user(
            list(argv), env={"CLAUDE_CONFIG_DIR": config_dir, **store_env},
            timeout=EXECUTE_TIMEOUT_SECONDS)
    except Exception as exc:  # noqa: BLE001 — RuntimeError (switch unavailable), TimeoutExpired, OSError
        return session_user.delegation_failure(list(argv), exc, would_own=config_dir)
    return session_user.capped_result(proc)


def _installed_plugin_keys(config_dir: str) -> set[str]:
    """Plugin keys in <config_dir>/plugins/installed_plugins.json's "plugins" dict; empty set on any
    missing/malformed file."""
    plugins_file = Path(config_dir).expanduser() / "plugins" / "installed_plugins.json"
    try:
        data = json.loads(_read_runner_text(plugins_file))
    except Exception:
        return set()
    plugins = data.get("plugins") if isinstance(data, dict) else None
    if not isinstance(plugins, dict):
        return set()
    return {str(k) for k in plugins}


def _omc_present(config_dir: str) -> bool:
    """True iff an OMC plugin (key starting 'oh-my-claudecode@') is recorded in installed_plugins.json."""
    return any(k.startswith("oh-my-claudecode@") for k in _installed_plugin_keys(config_dir))


def _omc_check(config_dir: str, install: bool, run) -> dict:
    """Presence check + optional idempotent install via `_claude_cli`. Returns
    {present, installed, exit_code, stderr}."""
    if install and not _omc_present(config_dir):
        add = _claude_cli(run, "claude", "plugin", "marketplace", "add", _OMC_MARKETPLACE_URL,
                          config_dir=config_dir)
        inst = _claude_cli(run, "claude", "plugin", "install", "oh-my-claudecode@omc", "--scope", "user",
                           config_dir=config_dir)
        present = _omc_present(config_dir)
        err = "\n".join(s for s in (add.get("stderr"), inst.get("stderr")) if s).strip()
        code = 0 if present else (inst.get("exit_code") or add.get("exit_code") or 1)
        return {"present": present, "installed": present, "exit_code": code, "stderr": err}
    present = _omc_present(config_dir)
    return {"present": present, "installed": False, "exit_code": 0, "stderr": ""}


def _hud_setup(config_dir: str) -> dict:
    """Best-effort HUD/statusline setup: copy the OMC HUD wrapper + config-dir helper from the installed
    OMC plugin's cache into <config_dir>/hud and point settings.json's statusLine at it.

    Never blocks account_add on failure — no statusline is not an unusable account."""
    base = Path(config_dir).expanduser()
    cache_root = base / "plugins" / "cache" / "omc" / "oh-my-claudecode"
    if not cache_root.is_dir():
        return {"applied": False, "error": "omc_plugin_not_found"}
    try:
        # Segments tagged (0, int) / (1, str) so a stray non-numeric segment cannot raise TypeError.
        versions = sorted(
            (p for p in cache_root.iterdir() if p.is_dir() and re.match(r"^\d", p.name)),
            key=lambda p: [(0, int(t)) if t.isdigit() else (1, t) for t in re.split(r"(\d+)", p.name)],
        )
        if not versions:
            return {"applied": False, "error": "omc_plugin_not_found"}
        scripts_lib = versions[-1] / "scripts" / "lib"
        wrapper_src = scripts_lib / "hud-wrapper-template.txt"
        config_dir_src = scripts_lib / "config-dir.mjs"
        if not wrapper_src.is_file() or not config_dir_src.is_file():
            return {"applied": False, "error": "hud_template_missing"}

        hud_dir = base / "hud"
        _ensure_runner_dir(hud_dir / "lib")
        # WRITE ONLY WHAT DIFFERS: a daemon-run wizard leaves these `jaahd`-owned 0644, and a later
        # converge as `jaah` would fail EACCES rewriting identical content — a permanent false alarm.
        _write_text_if_changed(hud_dir / "omc-hud.mjs", _read_plugin_file(wrapper_src, cache_root), mode=0o755)
        _write_text_if_changed(hud_dir / "lib" / "config-dir.mjs", _read_plugin_file(config_dir_src, cache_root))

        settings_path = base / "settings.json"
        try:
            settings = json.loads(_read_runner_text(settings_path)) if settings_path.exists() else {}
        except Exception:
            settings = {}
        settings["statusLine"] = {
            "type": "command",
            "command": "node ${CLAUDE_CONFIG_DIR:-$HOME/.claude}/hud/omc-hud.mjs",
        }
        _write_text_if_changed(settings_path, json.dumps(settings, indent=2) + "\n")
        return {"applied": True, "error": None}
    except Exception as exc:
        return {"applied": False, "error": str(exc)}


def _write_text_if_changed(path: Path, content: str, *, mode: int | None = None) -> bool:
    """Write `content` only when `path` does not already hold exactly it. Returns whether it wrote.

    Ownership, not IO cost: rewriting an identical file another uid wrote fails EACCES (`_hud_setup`).
    On Windows every caller's target is in an account config dir, a runner-writable tree, so the write is
    the runner account's (`_write_runner_file`)."""
    try:
        if path.exists() and _read_runner_text(path) == content:
            return False
    except OSError:
        pass  # unreadable → fall through and try the write, which reports its own failure
    if host_platform.is_windows():
        _write_runner_file(path, content, mode=mode)
        return True
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    if mode is not None:
        with contextlib.suppress(OSError):
            path.chmod(mode)
    return True


# WINDOWS: the account config dirs and `run\` are runner-writable, `_O_NOFOLLOW` is 0 and the runner account
# holds the link privilege, so every daemon open BY PATH there follows a link the runner planted. Writes go
# through the session-user seam (the runner writes as itself, no further than it could already); reads are
# refused BEFORE the open when a link leads off the disk or out of the tree (`windows.local_path_under`: an
# open through a link to `\\host\share` authenticates the daemon to that host), and check the opened
# handle's final path after. Off Windows each helper is the plain call it replaces.

def _final_path_in(fd: int, directory: Path) -> bool:
    """Whether the handle FD's final path names a file DIRECTLY in DIRECTORY as laid out on disk: the real
    `<jaah_home>` plus DIRECTORY's spelling below it — not DIRECTORY's realpath, which follows the same swapped
    junction the create did. False when it cannot be read (fail closed)."""
    home = jaah_home()
    try:
        rel = os.path.relpath(directory, home)
    except ValueError:  # another drive
        rel = os.pardir
    if rel == os.pardir or rel.startswith(os.pardir + os.sep):
        expected = os.path.realpath(directory)  # outside the home: the daemon's own `state\`
    else:
        expected = os.path.normpath(os.path.join(os.path.realpath(home), rel))
    try:
        return ntpath.normcase(ntpath.dirname(host_platform.fd_path(fd))) == ntpath.normcase(expected)
    except OSError:
        return False


class _OutsideRunnerTree(FileNotFoundError):
    """A runner-tree name whose opened handle resolved outside `<jaah_home>` (Windows only). A
    `FileNotFoundError`, so every caller's missing-file arm answers it: no file of this login's is there."""


# What `_read_runner_text` reads of a file on Windows: the runner writes those files, so their size is its.
_RUNNER_TEXT_CAP = 1 << 20


def _read_runner_text(path: Path, errors: str = "strict") -> str:
    """`path.read_text()` for a file in a runner-writable tree. On Windows the opened handle must stay under
    the real `<jaah_home>` — everything there the runner can read already — so a planted link cannot make the
    daemon read a daemon-only file (`etc\\node.env`) on its behalf, and at most `_RUNNER_TEXT_CAP` bytes
    are read. Raises `_OutsideRunnerTree` otherwise."""
    if not host_platform.is_windows():
        return path.read_text(errors=errors)
    if not windows.local_path_under(str(path), jaah_home()):
        raise _OutsideRunnerTree(errno.ENOENT, f"{path} resolves outside {jaah_home()}")
    fd = os.open(path, os.O_RDONLY | _O_BINARY)
    try:
        if not agent_base.final_path_under(fd, jaah_home()):
            raise _OutsideRunnerTree(errno.ENOENT, f"{path} resolves outside {jaah_home()}")
        with os.fdopen(fd, "rb", closefd=False) as fh:
            data = fh.read(_RUNNER_TEXT_CAP)
    finally:
        os.close(fd)
    return data.decode("utf-8", errors)


def _write_runner_file(path: Path, content: str, *, mode: int | None = None) -> None:
    """Replace PATH with CONTENT in a runner-writable tree: as the runner account on Windows
    (`session_user.replace_file_as_session_user`), else in-process. Raises `OSError` on failure."""
    if not host_platform.is_windows():
        path.write_text(content)
        return
    err = session_user.replace_file_as_session_user(str(path), content, mode=0o664 if mode is None else mode)
    if err:
        raise OSError(errno.EIO, f"could not write {path} as {session_user.session_user()!r}: {err}")


def _ensure_runner_dir(path: Path, *, mode: int | None = None) -> None:
    """`mkdir -p PATH` in a runner-writable tree: as the runner account on Windows
    (`session_user.ensure_dir_as_session_user`), else in-process at MODE. Raises `OSError` on failure."""
    if not host_platform.is_windows():
        if mode is None:
            path.mkdir(parents=True, exist_ok=True)
        else:
            os.makedirs(path, mode=mode, exist_ok=True)
        return
    err = session_user.ensure_dir_as_session_user(str(path), mode=mode)
    if err:
        raise OSError(errno.EIO, f"could not create {path} as {session_user.session_user()!r}: {err}")


# The login path's open flags, each 0 where the OS lacks it: Windows has none of the first three, and
# without `O_BINARY` its CRT descriptor is text-mode (CRLF on write, Ctrl-Z as EOF on read). A Windows
# node runs the looser model the operator accepted — `run\` is runner-writable and nothing there refuses
# a link — so these keep its opens from raising, not its paths from being planted.
_O_NOFOLLOW = os.O_NOFOLLOW if hasattr(os, "O_NOFOLLOW") else 0
_O_NONBLOCK = os.O_NONBLOCK if hasattr(os, "O_NONBLOCK") else 0
_O_CLOEXEC = os.O_CLOEXEC if hasattr(os, "O_CLOEXEC") else 0
_O_BINARY = os.O_BINARY if hasattr(os, "O_BINARY") else 0
# Neither flag exists on Windows; a FIFO planted in place of an asset must not block the daemon's open.
_NO_FOLLOW_NONBLOCK = _O_NOFOLLOW | _O_NONBLOCK


def _read_plugin_file(src: Path, cache_root: Path) -> str:
    """A HUD asset's text, read as the daemon from a plugin tree the SESSION USER writes (one shared root per
    node tree, `plugin_repair.share_plugin_root`): only a regular file, never through a final link, and only
    one resolving inside `cache_root` — a planted link must not have the daemon copy a file only it may read
    (the node credential) into a file the session user can. Raises OSError otherwise."""
    # Windows: before `realpath`, which opens through every link on the way (`windows.local_path_under`).
    if host_platform.is_windows() and not windows.local_path_under(str(src), str(cache_root)):
        raise _OutsideRunnerTree(errno.ENOENT, f"{src} resolves outside {cache_root}")
    real_root = os.path.realpath(cache_root)
    if not os.path.realpath(src).startswith(real_root + os.sep):
        raise OSError(f"{src} resolves outside {cache_root}")
    fd = os.open(src, os.O_RDONLY | _NO_FOLLOW_NONBLOCK)
    with os.fdopen(fd, encoding="utf-8") as fh:
        if not stat.S_ISREG(os.fstat(fh.fileno()).st_mode):
            raise OSError(f"{src} is not a regular file")
        # Windows: no `O_NOFOLLOW`, so the realpath above can be raced — the HANDLE's final path decides.
        if host_platform.is_windows() and not agent_base.final_path_under(fh.fileno(), real_root):
            raise OSError(f"{src} resolves outside {cache_root}")
        return fh.read()


_NOTIFICATION_EVENT_POLICY = {
    "session-start": {"enabled": False},
    "session-stop": {"enabled": False},
    "session-end": {"enabled": False},
    "session-idle": {"enabled": False},
    "agent-call": {"enabled": False},
    "ask-user-question": {"enabled": True},
}


def _notification_setup(config_dir: str) -> dict:
    """Best-effort: write `_NOTIFICATION_EVENT_POLICY` into <config_dir>/.omc-config.json — only
    `ask-user-question` enabled, because a headless runner stuck on human input is the one event worth
    paging for and every other event is noise. Unrelated top-level keys are preserved.

    Channel-agnostic: it gates which OMC events fire; it never writes a destination. The merge-and-write
    is `notification_policy.write_notification_policy`, the one writer of this file shape (each vendor's
    policy file is one call of it with that vendor's event set).

    Never raises, never blocks the caller."""
    res = notification_policy.write_notification_policy(
        Path(config_dir).expanduser() / ".omc-config.json", _NOTIFICATION_EVENT_POLICY)
    return {"applied": res["applied"], "error": res["error"] or None}


_MCP_PLUGINS = MCP_PLUGINS


def _known_marketplaces(config_dir: str) -> set[str]:
    """Marketplace names from <config_dir>/plugins/known_marketplaces.json; empty set on any
    missing/malformed file."""
    path = Path(config_dir).expanduser() / "plugins" / "known_marketplaces.json"
    try:
        data = json.loads(_read_runner_text(path))
    except Exception:
        return set()
    return {str(k) for k in data} if isinstance(data, dict) else set()


def _mcp_plugins_setup(config_dir: str, run) -> dict:
    """Best-effort: install + enable every plugin in `_MCP_PLUGINS` for a fleet account.

    A plugin whose marketplace this account does not know gets it ADDED first from
    `plugin_repair.MARKETPLACES`; a fresh account knows only the one `_omc_check` adds, and an install
    into an unknown marketplace fails with a "try `marketplace update`" message that points at a stale
    cache which is not the problem. Re-verifies against installed_plugins.json rather than trusting the
    CLI's exit code; enabledPlugins is set only for plugins CONFIRMED present. Never raises, never blocks
    the caller."""
    try:
        errors: list[str] = []
        present = _installed_plugin_keys(config_dir)
        known = _known_marketplaces(config_dir)
        wanted = [p for p in _MCP_PLUGINS if p not in present]
        attempted = []
        for plugin in wanted:
            _, _, marketplace = plugin.partition("@")
            if marketplace and marketplace not in known:
                source = MARKETPLACES.get(marketplace)
                add_failure = ""
                if source:
                    res = _claude_cli(run, "claude", "plugin", "marketplace", "add", source,
                                      config_dir=config_dir)
                    # The add's own stderr is kept for the error below: a local-directory source absent
                    # on a node mid-upgrade is otherwise reported as a bare "could not be added".
                    if isinstance(res, dict):
                        add_failure = (res.get("stderr") or res.get("stdout") or "").strip()[:400]
                    known = _known_marketplaces(config_dir)
            if marketplace and marketplace not in known:
                if marketplace in MARKETPLACES:
                    cause = f"could not be added from {MARKETPLACES[marketplace]!r}"
                    if add_failure:
                        cause += f": {add_failure}"
                else:
                    cause = "has no declared source"
                errors.append(
                    f"{plugin}: marketplace {marketplace!r} is not configured for this account "
                    f"(known: {', '.join(sorted(known)) or 'none'}) and {cause} — add it with "
                    f"`claude plugin marketplace add <source>` before this plugin can install"
                )
                continue
            attempted.append(plugin)
        for plugin in attempted:
            res = _claude_cli(run, "claude", "plugin", "install", plugin, "--scope", "user",
                              config_dir=config_dir)
            err = res.get("stderr") if isinstance(res, dict) else None
            if err:
                errors.append(err.strip())

        present = _installed_plugin_keys(config_dir)
        confirmed = [p for p in _MCP_PLUGINS if p in present]
        newly_installed = [p for p in attempted if p in present]

        base = Path(config_dir).expanduser()
        settings_path = base / "settings.json"
        try:
            settings = json.loads(_read_runner_text(settings_path)) if settings_path.exists() else {}
            if not isinstance(settings, dict):
                settings = {}
        except Exception:
            settings = {}
        enabled = settings.get("enabledPlugins")
        if not isinstance(enabled, dict):
            enabled = {}
        for plugin in confirmed:
            enabled[plugin] = True
        settings["enabledPlugins"] = enabled
        _ensure_runner_dir(base)
        _write_runner_file(settings_path, json.dumps(settings, indent=2) + "\n")
        return {"applied": len(confirmed) == len(_MCP_PLUGINS), "installed": newly_installed,
                "error": "\n".join(e for e in errors if e).strip() or None}
    except Exception as exc:
        return {"applied": False, "installed": [], "error": str(exc)}


# ── Add-account OAuth-scrape state machine ──────────────
# `\Z`, NOT `$`: `$` also matches before a trailing newline, so `"acct1\n"` would pass and
# `shared_storage.resolve_shared_auth_dir` would derive ANOTHER account's store — where a token is MINTED.
# Spelled identically to `apps/api/app/routes/commands.py::_SLUG_RE` and `credentials.py::_SLUG_RE`.
_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}\Z")
# The pasted OAuth callback code, `<authcode>#<state>`. Shape-only (no control chars, bounded) before it
# is written to the login's stdin; broad on purpose — the CLI rejects a bad code loudly as login_rejected.
_OAUTH_CODE_RE = re.compile(r"^[A-Za-z0-9._~#/+=-]{1,512}$")
# The scraped authorize URL's bound, in UTF-8 bytes, checked before it is sealed: 11 chunks under
# `login_seal.MAX_SEALED_CHARS`, the control plane's wire bound on the blob it relays.
_OAUTH_URL_MAX_BYTES = 2048
# Bounded windows (seconds). Overridable for tests via env.
_OAUTH_SCRAPE_TIMEOUT = 30.0
_LOGIN_POLL_TIMEOUT = 120.0
_LOGIN_POLL_INTERVAL = 3.0
# AWAIT_CREDENTIALS mode's phase-2 window, SHORTER than paste mode's: `_login_await_redirect`'s reply is
# NON-TERMINAL (the caller re-asks on `awaiting_redirect`), so a long window only pins one of
# `commands.CommandBridge._dispatch_bound` slots. Paste mode's window is ONE-SHOT and terminal — never
# unify the two.
_LOGIN_AWAIT_POLL_TIMEOUT = 25.0
# Wall-clock cap on the DETACHED `claude auth login` process. It outlives the command that spawned it, so
# `timeout(1)` bounds the leak of an abandoned login (a later phase 2 finds it gone: login_process_gone).
# Also caps an await-credentials wait that keeps answering `awaiting_redirect`, since that arm reaps nothing.
_LOGIN_PROC_TIMEOUT = 900.0
def _account_add_runtime_root() -> Path:
    """Per-slug runtime dir for the detached login's IPC files (FIFO stdin, stdout log, pid). Kept OUT
    of CLAUDE_CONFIG_DIR so it never confuses the CLI's config scan. Lazy, as `env_helpers.fleet_locks_dir`."""
    return jaah_dir("run", "account-add")


def _strip_escapes(line: str) -> str:
    """Strip OSC-8 hyperlink escapes (\\x1b]...\\x07) and SGR color codes (\\x1b[...m) from a scraped line.

    The OSC class excludes `\\x1b` (a DoS bound): admitting ESC lets unterminated `\\x1b]` starts rescan
    the tail quadratically over a log an untrusted uid appends to (3.22s vs 0.0022s on 64 KiB). `\\n` and
    `{0,4096}` are secondary bounds; past them the escape is left unstripped, which is benign."""
    line = re.sub(r"\x1b\][^\x07\n\x1b]{0,4096}\x07", "", line)
    line = re.sub(r"\x1b\[[0-9;]*m", "", line)
    return line


# THE ONLY TWO READS THIS MODULE MAKES INTO AN ACCOUNT DIR: an mtime and a shape check, never a token
# value. `.credentials.json` is 0600 the session user's: the mtime needs only traversal (dir is 2770, group
# `jaah`), the shape check needs contents, so its `PermissionError` is DELEGATED to the session user,
# never swallowed — swallowing would report a successful exchange as `login_timeout` on every split node.
# Both take `securestorage_dir` EXPLICITLY: they watch the file the login's own env named, and at poll
# time the token store legitimately holds no token yet, so a from-disk answer would watch the wrong file.


def _creds_mtime(config_dir: str, securestorage_dir: str = "") -> float | None:
    """mtime of this account's `.credentials.json`, or None if absent/unreadable.

    A re-auth's config_dir already holds the OLD file, so both phase-2 cores compare this against a
    pre-poll snapshot to wait for an actual rewrite. Goes through `_path_mtime`, which `lstat`s: `stat`
    follows a planted symlink to any file whose mtime moves on its own and forges the freshness conjunct.
    `_creds_have_token`'s O_NOFOLLOW readers are the other half; change the three together."""
    cred = Path(shared_storage.token_dir(config_dir, securestorage_dir)).expanduser() / ".credentials.json"
    return _path_mtime(cred)


def _creds_have_token(config_dir: str, securestorage_dir: str = "") -> bool:
    """True iff this account's `.credentials.json` exists and parses with a claudeAiOauth.accessToken-shaped
    field (`claude_login.has_oauth_token`). NEVER reads/returns/logs the token VALUE — presence + shape only.

    Read through `agent_base.credential_state` + `credential_file_present` (no symlink, FIFO or non-regular
    file is a credential). `PermissionError` is the split host's normal case (0600 the session user's) and is
    delegated; every other read error is "no token", the conservative direction for the poll."""
    cred = Path(shared_storage.token_dir(config_dir, securestorage_dir)).expanduser() / ".credentials.json"
    if runner_credentials.read_through_runner(cred):
        return _delegated_creds_have_token(cred)
    # Windows, past the share (the runner's, delegated above): refused before the open, as `_read_runner_text`.
    if host_platform.is_windows() and not windows.local_path_under(str(cred), jaah_home()):
        return False
    state = agent_base.credential_state(str(cred), lambda: agent_base.credential_file_present(
        str(cred), max_bytes=claude_login.MAX_CREDENTIAL_BYTES, validate=claude_login.has_oauth_token))
    if state == agent_config_dirs.CREDS_UNREADABLE:
        return _delegated_creds_have_token(cred)
    return state == agent_config_dirs.CREDS_TOKEN


# The reader both delegated shape checks open with. Runs AS THE SESSION USER: stdlib only, `O_NOFOLLOW` +
# `S_ISREG` as in `_creds_have_token` (a planted symlink read by its own author forges the shape conjunct);
# `data` is the parsed JSON or None. Each check prints `1`/`0` and NOTHING else — never the file's contents,
# not even in an error (the delegated stdout is spooled and, on failure, logged).
_READ_JSON_AS_USER_PY = (
    "import json, os, stat, sys\n"
    "def _read(path):\n"
    "    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_BINARY', 0))\n"
    "    with os.fdopen(fd, 'rb') as fh:\n"
    "        if not stat.S_ISREG(os.fstat(fh.fileno()).st_mode):\n"
    "            return None\n"
    "        return json.load(fh)\n"
    "try:\n"
    "    data = _read(sys.argv[1])\n"
    "except Exception:\n"
    "    data = None\n"
)

_CREDS_SHAPE_PY = (
    _READ_JSON_AS_USER_PY +
    "oauth = data.get('claudeAiOauth') if isinstance(data, dict) else None\n"
    "print(1 if isinstance(oauth, dict) and oauth.get('accessToken') else 0)\n"
)


def _delegated_creds_have_token(cred: Path) -> bool:
    """`_creds_have_token`'s shape check run as the session user, for a file this uid cannot read. False
    when the seam is unavailable or the check could not run — the poll stays in its "not yet" arm."""
    if not session_user.can_run_as_session_user():
        log.warning("cannot read %s and cannot delegate the check to %r — treating it as no token",
                    cred, session_user.session_user())
        return False
    try:
        proc = session_user.run_as_session_user(
            [sys.executable, "-I", "-c", _CREDS_SHAPE_PY, str(cred)], timeout=_DELEGATED_TIMEOUT)
    except Exception as exc:  # noqa: BLE001 — RuntimeError (switch gone), TimeoutExpired, OSError
        log.warning("delegated credentials-shape check for %s failed (%s: %s)", cred, type(exc).__name__, exc)
        return False
    return (proc.stdout or "").strip() == "1"


# Vendor-neutral "a JSON object with one non-empty string value", sound only after the mtime MOVED.
# `_READ_JSON_AS_USER_PY`'s `O_NOFOLLOW` + `S_ISREG`: a runner that plants `$CODEX_HOME/auth.json` as a link
# to any JSON it owns would otherwise print `1`. Cheap forgery only (the uid may write its own file; a forged
# `res/<id>.out` stays possible); the trust boundary is the node's (`agents/codex_login.py::credential_present`).
_ANY_CREDENTIAL_SHAPE_PY = (
    _READ_JSON_AS_USER_PY +
    "ok = isinstance(data, dict) and any(\n"
    "    isinstance(v, str) and v.strip() for v in data.values())\n"
    "print(1 if ok else 0)\n"
)


def _delegated_credential_written(cred: Path) -> bool:
    """`_delegated_creds_have_token`'s vendor-neutral twin: has the session user written a non-empty JSON
    object at `cred`? False when the seam is unavailable or the check could not run.

    A separate function, not a parameter: collapsing them would let Claude's path reach the weak check
    where the strong one is required."""
    if not session_user.can_run_as_session_user():
        log.warning("cannot read %s and cannot delegate the check to %r — treating it as no credential",
                    cred, session_user.session_user())
        return False
    try:
        proc = session_user.run_as_session_user(
            [sys.executable, "-I", "-c", _ANY_CREDENTIAL_SHAPE_PY, str(cred)], timeout=_DELEGATED_TIMEOUT)
    except Exception as exc:  # noqa: BLE001 — RuntimeError (switch gone), TimeoutExpired, OSError
        log.warning("delegated credential-shape check for %s failed (%s: %s)",
                    cred, type(exc).__name__, exc)
        return False
    return (proc.stdout or "").strip() == "1"


def _path_mtime(path: Path) -> float | None:
    """mtime of `path` ITSELF, or None when absent/unreadable.

    `lstat` needs only a traversable directory, which is what makes the freshness baseline work on a
    uid-split node where the 0600 credential is unreadable. `lstat`, NOT `stat`: both sides of the
    freshness pair (`_login_pre_mtime`, `_login_creds_fresh`) come through here, and `stat` follows a
    planted `auth.json` symlink to any file whose mtime moves on its own. The sibling guard is
    `_ANY_CREDENTIAL_SHAPE_PY`'s `O_NOFOLLOW`; change the two together."""
    if runner_credentials.read_through_runner(path):
        return _delegated_path_mtime(path)
    try:
        return os.lstat(path).st_mtime
    except OSError:
        return None


# Runs AS THE SESSION USER: prints the `lstat` mtime of `argv[1]`, or nothing when it is absent.
_LSTAT_MTIME_PY = (
    "import os, sys\n"
    "try:\n"
    "    print(repr(os.lstat(sys.argv[1]).st_mtime))\n"
    "except OSError:\n"
    "    pass\n"
)


def _delegated_path_mtime(path: Path) -> float | None:
    """`_path_mtime` run as the session user, for a path this daemon cannot see. None when absent, or when
    the check could not run — the poll then stays in its "not yet" arm."""
    try:
        proc = session_user.run_as_session_user(
            [sys.executable, "-I", "-c", _LSTAT_MTIME_PY, str(path)], timeout=_DELEGATED_TIMEOUT)
    except Exception as exc:  # noqa: BLE001 — RuntimeError (switch gone), TimeoutExpired, OSError
        log.warning("delegated mtime check for %s failed (%s: %s)", path, type(exc).__name__, exc)
        return None
    try:
        return float((proc.stdout or "").strip())
    except ValueError:
        return None


def _set_provisioning(config_dir: str, _prov: dict | None, *, slug: str | None = None) -> None:
    """No-op: the CP owns provisioning bookkeeping (apps/api/app/durable/accounts.py), fed from each
    command's reply. Kept callable so the wizard's call sites need no edit."""
    return


def _login_env(config_dir: str, securestorage_dir: str = "", params=None) -> dict:
    """Environment for a login spawned IN THIS PROCESS (one-uid host; the delegated spawn curates its own,
    `_spawn_login_as_session_user`).

    `BROWSER`, not `DISPLAY`, decides whether a window opens: with DISPLAY unset the CLI still runs the
    opener (measured, claude 2.1.247), so `true` makes it a no-op — always; the URL the operator uses is
    the MANUAL one the CLI prints to its stdout log (`_scrape_oauth_url`).
    `securestorage_dir` is where the token lands (the token store; `""` only for a node-pinned provider).
    The ambient `CLAUDE_SECURESTORAGE_CONFIG_DIR` is popped because `os.environ` is spread first: a caller
    running under another account carries it, and this login would mint into the CALLER's store. The vendor overlay (`params.login_env`) is applied LAST.
    SECRET-BEARING BY CONSTRUCTION: never logged, echoed into a reply, or put in telemetry."""
    env = {**os.environ, "BROWSER": "true"}
    env.pop("DISPLAY", None)
    env.pop("WAYLAND_DISPLAY", None)
    env.pop(shared_storage.SECURESTORAGE_ENV, None)
    env.update(_securestorage_env(securestorage_dir))
    env.update((_CLAUDE_LOGIN if params is None else params).login_env(config_dir=config_dir))
    return env


def _login_uid_refusal(config_dir: str, params=None) -> str | None:
    """The refusal that stops a login from splitting an account by uid, or None when it can run as the
    uid that must own the credentials (one-uid host, or the split with a working seam).

    The split with the seam DOWN stays a refusal: proceeding as the daemon "succeeds" and leaves an
    account no runner can open. Re-checked in phase 2 — the halves are minutes apart. The manual-repair
    hint renders a value only where it is `config_dir`; every other overlay variable is named with its
    value replaced, because `login_env` is secret-bearing and this string is stored and shown."""
    if not session_user.switch_needed() or session_user.can_run_as_session_user():
        return None
    params = _CLAUDE_LOGIN if params is None else params
    pins = " ".join(f"{k}={v}" if v == config_dir else f"{k}=<this account's value>"
                    for k, v in params.login_env(config_dir=config_dir).items())
    cmd = " ".join(x for x in (pins, shlex.join(params.login_argv())) if x)
    return (
        f"login_wrong_uid: refusing to run `{shlex.join(params.login_argv())}` for {config_dir} as this "
        f"uid — it would write the credential file owned by it and lock out "
        f"{session_user.session_user()!r}, the user "
        f"every runner and console session runs as, leaving the account unusable. Running it as that "
        f"user needs the fleet tmux server (jaah-runner@<node name>.service) and {session_user.exec_spool()}, and one "
        f"of them is not up — repair that and retry. Meanwhile you can log in by hand: attach with "
        f"`jaah tmux`, then run `{cmd}` in the "
        f"pane (that pane already runs as {session_user.session_user()!r}, so the credentials land with "
        f"the right owner)."
    )


def _login_provider_refusal(provider: str) -> str | None:
    """The refusal that stops a login being run for a provider this node cannot drive, or None when it can.

    Otherwise `claude auth login` would mint a real Claude token into an account recorded (immutably) as
    another vendor's. Asks only whether this daemon carries a login adapter (`agents.login.login_adapter_for`),
    Claude Code's included. An unknown value is refused too, and so is a provider with no OAuth kind at all
    (DeepSeek): it runs the `claude` binary, so a login here would be a Claude login."""
    row = agent_providers.by_value(provider)
    if row is not None and "oauth" not in row.auth_kinds:
        return (
            f"login_provider_unsupported: {row.label} accounts have no login — they are API-key accounts "
            f"({', '.join(row.auth_kinds)}). Add the key as a credential in the console and point the account "
            f"at it instead; running a login here would mint a Claude credential for a {row.label} account."
        )
    if agent_login.login_adapter_for(provider) is not None:
        return None
    return (
        f"login_provider_unsupported: this node cannot run a login for provider {provider!r} — it carries "
        f"no login adapter for it, and the only login it can run would mint a Claude credential for an "
        f"account recorded as {provider!r}, which cannot be corrected afterwards. Register this account on "
        f"a node whose jaahd carries that adapter, or wait for the deploy that adds it."
    )


# THE one spelling of an account config dir's mode. `reconcile.accounts_probe.AgentsProbeCache._run_converge` creates
# the dir through the session-user seam and reads this instead of calling `ensure_account_dir`.
ACCOUNT_DIR_MODE = 0o2770


def ensure_account_dir(config_dir: str) -> None:
    """Create this node's config dir for one credential account, at the fleet's mode.

    2770, NOT 0700: the dir is shared between the daemon and the session user, so a 0700 dir made by
    whichever uid got here first is one the other cannot traverse; setgid keeps the group inherited for
    everything created inside. Credentials stay 0600 — the CLI chmods them itself. Also called by
    `provision_accounts.py`'s sweep, which creates the per-node dir of an account whose token is in the
    store. On Windows the runner account creates it (`_ensure_runner_dir`): `home\\` is its tree."""
    _ensure_runner_dir(Path(config_dir), mode=ACCOUNT_DIR_MODE)


def _login_store_for(slug: str, provider: str) -> tuple[str, str | None]:
    """`(store, refusal)` for a login of SLUG: the token store `<root>/<slug>` it mints into
    (`shared_storage.resolve_shared_auth_dir`), or `""` for a `node_pinned` provider, whose
    token stays in its config dir. No store for any other provider is a refusal (no usable JAAH_HOME, or a
    store mount that is not answering, `shared_storage.account_token_root`)."""
    row = agent_providers.by_value(provider or agent_providers.DEFAULT_PROVIDER)
    if row is not None and row.node_pinned:
        return "", None
    store = shared_storage.resolve_shared_auth_dir(slug)
    if store:
        return store, None
    return "", (
        f"login_store_unavailable: this node has no token store for {slug} — "
        f"{shared_storage.accounts_nodelink_root() or 'JAAH_HOME/auth/.shared'} is unusable (no JAAH_HOME, "
        f"or a mount there that is not answering). Refusing rather than minting the token where no read "
        f"looks. Fix the mount (or JAAH_HOME) and retry."
    )


def _login_store_refusal(slug: str, securestorage_dir: str) -> str | None:
    """The refusal that stops a login from minting through an unsafe path, or None. Shape and symlink safety
    only: a store that is not mounted is still the store (the account is then this node's alone). `""`
    (a node-pinned login) has nothing to check. Two arms:
      1. a store path this node composes differently (phase 2 reads phase 1's back);
      2. a symlink (or non-directory) at the store root or at the account's leaf. An absent root passes:
         the login creates it (`shared_storage.ensure_shared_root`)."""
    if not securestorage_dir:
        return None
    expected = shared_storage.resolve_shared_auth_dir(slug)
    if securestorage_dir != expected:
        # The recorded value is NEVER echoed, nor anything derived from it: it was read back from disk, and
        # a planted handover can make that read any file the daemon can open (the node credential).
        return (
            f"login_store_mismatch: the token store this login recorded is not the one this node derives "
            f"({expected or '(none)'}) for {slug} — refusing to act on a path that moved between the two "
            f"halves of the login"
        )
    if os.path.lexists(shared_storage.account_token_root()) and not shared_storage.store_is_on_share(
            securestorage_dir):
        return (
            f"login_store_not_on_share: {securestorage_dir} is not a real directory in the token store — a "
            f"symlink or another filesystem sits at it or at its root. Refusing rather than writing the "
            f"credential through it: it would land outside the store (or in another account's). Remove "
            f"that path and retry."
        )
    return None


# A delegated one-shot (spawn, signal, shape check) is a spool round trip doing no work of its own, so this
# bounds a hung seam, not the operation — far under every window in this module.
_DELEGATED_TIMEOUT = 30.0
# The delegating shell returns as soon as the child is backgrounded; this covers the child's first two lines.
_LOGIN_PID_WAIT = 10.0

# Two scripts so neither quotes the other: `run_as_session_user` shell-quotes each argv element, so the
# child script travels as one argument. `setsid` + `&`: the login must outlive the delegated shell and the
# spool round trip, and as group leader it is what `_kill_login_proc` signals. `"$@"` after a shift: the
# login argv is the vendor's and variadic (`agents/login.py::login_argv`). `{new_session}` is
# `session_user.new_session_cmd()` (`setsid` on Linux; macOS has none).
_LOGIN_SPAWN_SH = (
    'child="$1"\n'
    'shift\n'
    '{new_session} /bin/sh -c "$child" jaahd-login "$@" </dev/null >/dev/null 2>&1 &\n'
)
# $1 fifo, $2 log, $3 pid file, $4 wall-clock cap, $5.. the login argv. `exec` keeps `$$` as the login's
# pid (written via rename, never half-read). `0<>` opens the FIFO READ-WRITE so reads BLOCK for the code
# instead of seeing EOF. The argv is executed as `"$@"`, never interpolated — a shell injection surface.
_LOGIN_CHILD_SH = (
    'echo $$ > "$3.tmp" && mv "$3.tmp" "$3"\n'
    'exec 0<>"$1"\n'
    'log="$2"; cap="$4"\n'
    'shift 4\n'
    'exec timeout "$cap" "$@" >>"$log" 2>&1\n'
)
# Windows' pair, for Git for Windows' MSYS runtime, which ships no `setsid` and has no FIFO. `set -m` does
# `setsid`'s job here: the backgrounded child leads its OWN process group — what `_kill_login_proc` signals,
# and out of the delegating job's. The child runs under `bash` by name: it needs process substitution, which
# a bash older than 5.1 refuses when invoked as `sh`.
_WINDOWS_LOGIN_SPAWN_SH = (
    'child="$1"\n'
    'shift\n'
    'set -m\n'
    '/bin/bash -c "$child" jaahd-login "$@" </dev/null >/dev/null 2>&1 &\n'
)
# $1 code file, $2 log, $3 pid file, $4 wall-clock cap, $5.. the login argv. `exec`, as Linux's: the pid
# file's `$$` becomes `timeout` itself, so a dead CLI reads as a dead login and the group `$$` leads is
# timeout's (GNU timeout's own `setpgid` is a no-op for a leader). The code arrives as a FILE
# (`_deliver_login_code`); the process substitution — the feeder, in the same group — waits for it, feeds it
# once, then holds the pipe open so the CLI never sees EOF (the FIFO's `0<>`). An await-credentials login
# never gets one. The feeder ends at the cap or as soon as `timeout` is gone, whichever is first.
_WINDOWS_LOGIN_CHILD_SH = (
    'echo $$ > "$3.tmp" && mv "$3.tmp" "$3"\n'
    'codef="$1"; log="$2"; cap="$4"\n'
    'shift 4\n'
    'exec timeout "$cap" "$@" >>"$log" 2>&1 < <(\n'
    '  n=0\n'
    '  while [ ! -s "$codef" ] && [ "$n" -lt "$cap" ] && kill -0 $$ 2>/dev/null; do n=$((n+1)); sleep 1; done\n'
    '  if [ -s "$codef" ]; then cat "$codef"; rm -f "$codef"; fi\n'
    '  while [ "$n" -lt "$cap" ] && kill -0 $$ 2>/dev/null; do n=$((n+1)); sleep 1; done)\n'
)


def _login_cli_refusal(params=None) -> str | None:
    """The refusal for a node with no runnable login binary, or None when the CLI is there.

    A preflight, else a detached login's missing CLI surfaces as `oauth_url_scrape_failed` after 30s.
    Resolved on the same PATH the login gets, checking the first element of the argv actually spawned."""
    params = _CLAUDE_LOGIN if params is None else params
    # `cli_binary` when the argv starts with a driver rather than the CLI itself (agents/login.py).
    binary = getattr(params, "cli_binary", "") or params.login_argv()[0]
    hint = getattr(params, "install_hint", "")
    if not _login_runs_delegated():
        if shutil.which(binary):
            return None
        return (f"login_cli_missing: no `{binary}` CLI on this node's PATH ({os.environ.get('PATH', '')}) — "
                f"a login cannot run here until the runner toolchain is provisioned")
    try:
        proc = session_user.run_as_session_user(
            ["/bin/sh", "-c", f'command -v {shlex.quote(binary)}'], timeout=_DELEGATED_TIMEOUT)
    except Exception as exc:  # noqa: BLE001 — RuntimeError (switch gone), TimeoutExpired, OSError
        # Not a verdict on the CLI: the seam failed, and the spawn reports that with its own message.
        log.warning("could not check for `%s` as %r (%s: %s) — letting the spawn report it",
                    binary, session_user.session_user(), type(exc).__name__, exc)
        return None
    if proc.returncode == 0 and (proc.stdout or "").strip():
        return None
    return (
        f"login_cli_missing: user {session_user.session_user()!r} has no `{binary}` CLI on this node "
        f"(PATH {session_user.delegated_path()}) — a login cannot run here until the runner toolchain is "
        f"provisioned. {hint}".rstrip()
    )


# What the log carries when the CLI never ran. Matched as a pair (binary AND complaint) so an unrelated
# line mentioning either one cannot claim this cause.
_CLI_MISSING_COMPLAINTS = ("not found", "no such file")


def _login_log_tail(log_source: Path | _ScrapedLog, limit: int = 400) -> str:
    """The tail of the login's stdout log, escape-stripped and collapsed to one line, for an operator-facing
    error. Carries no secret: the Claude CLI prints a banner and the public URL, never the token.

    Via `_ScrapedLog`, capped at `_LOG_SCRAPE_TAIL_BYTES`: the log is group-writable, its size an
    attacker's choice. A poisoned channel reads as `""`. A reader wrapped here is closed here."""
    reader, owned = _as_scraped_log(log_source)
    try:
        text = reader.read_tail() or ""
    finally:
        if owned:
            reader.close()
    return " ".join(text.split())[-limit:]


def _scrape_failure_error(log_source: Path | _ScrapedLog, params=None) -> str:
    """The terminal error for a scrape window that closed with no URL — named by what the log shows.

    `oauth_url_scrape_failed` claims the CLI's OUTPUT FORMAT changed (the frontend says "retrying won't
    help"); a CLI that never started is a provisioning gap and gets `login_cli_missing`. The BASENAME is
    matched because an adapter's argv may be absolute while the shell's complaint is not.
    ONLY CLAUDE'S LOG TAIL MAY LEAVE THE NODE: this message is stored control-plane side, and a log tail is
    the likeliest place for an arbitrary vendor's token to sit. A provider's tail is not retained anywhere
    else either — `_cleanup_account_add_runtime` rmtrees it — an acknowledged gap; do not close it by
    widening the redaction. On Windows only a PINNED reader's tail is read at all: a by-path read there
    follows whatever link a runner planted at the name."""
    params = _CLAUDE_LOGIN if params is None else params
    binary = os.path.basename(getattr(params, "cli_binary", "") or params.login_argv()[0])
    pinned = isinstance(log_source, _ScrapedLog) and log_source.is_pinned
    readable = pinned or not host_platform.is_windows()
    tail = _login_log_tail(log_source) if readable else ""
    low = tail.lower()
    detail = tail if params is _CLAUDE_LOGIN and readable else "this vendor's CLI output is kept on the node"
    if binary.lower() in low and any(c in low for c in _CLI_MISSING_COMPLAINTS):
        return (f"login_cli_missing: the login process could not run `{binary}` on this node ({detail}) — "
                f"provision the runner toolchain and retry")
    return f"oauth_url_scrape_failed: {detail or '(the login process printed nothing at all)'}"


def _login_runs_delegated() -> bool:
    """Whether the login goes through the session-user seam. The ONE predicate the flow branches on —
    IPC modes, spawn and SIGTERM must agree; `_login_uid_refusal` has turned away "must, but cannot"."""
    return session_user.can_run_as_session_user()


def _share_with_session_user(path: Path, mode: int) -> None:
    """Make sure the delegated login can use a path this process created. Each half is checked before it
    is changed so an already-usable path (e.g. the runtime root the SESSION USER owns) is not chmod'ed —
    an EPERM. Only widens, never narrows; best-effort with a loud log — the delegated spawn fails visibly
    if the sharing did not take."""
    gid = session_user.session_gid()
    try:
        st = path.stat()
    except OSError as exc:
        log.warning("could not stat %s to share it with %r: %s", path, session_user.session_user(), exc)
        return
    # Group bits plus setgid: `os.makedirs` takes setgid from the parent, not its `mode` argument.
    required = mode & 0o2070
    if st.st_mode & required != required:
        try:
            os.chmod(path, st.st_mode & 0o7777 | required)
        except OSError as exc:
            log.warning("could not grant %o on %s (needed by %r): %s",
                        required, path, session_user.session_user(), exc)
    if gid is not None and st.st_gid != gid:
        try:
            os.chown(path, -1, gid)
        except OSError as exc:
            log.warning("could not set %s's group to %s (%r's own): %s",
                        path, gid, session_user.session_user(), exc)


def _share_fifo_with_session_user(path: Path, mode: int) -> None:
    """`_share_with_session_user` for the FIFO, done ON A DESCRIPTOR, not the path.

    A path-based share is a second syscall on the NAME in a runner-writable dir: a swap would get
    `chmod`/`chgrp` applied to a `jaahd`-owned file of its choosing. `O_NONBLOCK`: a writerless FIFO
    open blocks. `S_ISFIFO` on the fd closes the regular-file swap. Only widens; best-effort, logged."""
    gid = session_user.session_gid()
    try:
        fd = os.open(path, os.O_RDONLY | _O_NONBLOCK | _O_NOFOLLOW | _O_CLOEXEC)
    except OSError as exc:
        log.warning("could not open %s to share it with %r: %s", path, session_user.session_user(), exc)
        return
    try:
        st = os.fstat(fd)
        if not stat.S_ISFIFO(st.st_mode):
            log.error("refusing to share %s with %r: it is no longer the FIFO this login created, so it "
                      "was replaced behind the name — the hijack this fd-based share exists to refuse",
                      path, session_user.session_user())
            return
        required = mode & 0o2070
        if st.st_mode & required != required:
            try:
                os.fchmod(fd, st.st_mode & 0o7777 | required)
            except OSError as exc:
                log.warning("could not grant %o on %s (needed by %r): %s",
                            required, path, session_user.session_user(), exc)
        if gid is not None and st.st_gid != gid:
            try:
                os.fchown(fd, -1, gid)
            except OSError as exc:
                log.warning("could not set %s's group to %s (%r's own): %s",
                            path, gid, session_user.session_user(), exc)
    finally:
        os.close(fd)


class _LoginRuntimeUnsafe(RuntimeError):
    """A per-slug runtime path was already occupied at create time. Its own class so `_login_start` reports
    it under its own terminal code instead of labelling an attack `login_spawn_failed`."""


def _create_login_runtime_file(path: Path, mode: int, content: str = "", *, keep_open: bool = False) -> int | None:
    """Create PATH in the per-slug runtime dir at MODE, refusing to follow anything already there, and set
    mode (and the shared group, when delegated) ON THE FD — never on the path.

    The runtime dir is runner-writable, so a planted symlink would take a path-based truncate/chmod/chgrp
    onto a `jaahd`-owned file. `O_EXCL` cannot false-positive: `_login_start` rmtrees the dir first.
    Known gap off Windows: `_pre_creds_mtime_path` and `_login_store_path` are still created path-wise.
    Route any further runtime file through this helper.

    `keep_open`: return the create's own descriptor, opened read-write, for the caller to own — the log
    pin on Windows, where a reopen by name would follow a link planted after the create (no `O_NOFOLLOW`).

    Windows: `windows.create_new_fd` refuses a final link, but a junction swapped in for a parent is followed
    — so the handle's final path must lie directly in PATH's directory, or the file is deleted by handle."""
    on_windows = host_platform.is_windows()
    flags = (os.O_RDWR if keep_open else os.O_WRONLY) | os.O_CREAT | os.O_EXCL | _O_NOFOLLOW | _O_BINARY
    try:
        fd = windows.create_new_fd(str(path)) if on_windows else os.open(path, flags, mode)
    except FileExistsError as exc:
        raise _LoginRuntimeUnsafe(
            f"refusing to create {path}: something already occupies that path, but this attempt "
            f"removed the whole runtime dir first — so it was planted between the two, which is the "
            f"symlink hijack this create is hardened against. Nothing was written. Retry; if it "
            f"recurs, audit who else can write {path.parent} (it is group {session_user.session_user()!r} "
            f"by design, so a compromised fleet runner is the shape to look for)."
        ) from exc
    try:
        if on_windows:
            if not _final_path_in(fd, path.parent):
                try:
                    windows.delete_by_handle(fd)
                except OSError as exc:
                    log.warning("could not delete the create of %s that landed outside %s: %s",
                                path, path.parent, exc)
                raise _LoginRuntimeUnsafe(
                    f"refusing to create {path}: the create landed outside {path.parent}, so a link or "
                    f"junction was swapped in on the way — the hijack this create is hardened against. Nothing "
                    f"was written and the file was deleted. Retry; if it recurs, audit who else can write "
                    f"{path.parent} (the runner account {session_user.session_user()!r} can, by design)."
                )
            # No mode bits or groups there: the `run\` ACLs already grant both accounts Modify, and
            # `state\` (the handover files, `_login_handover_dir`) is the daemon's alone.
            if content:
                os.write(fd, content.encode())
            return fd if keep_open else None
        # On the fd, not the `mode` argument: `os.open` applies the umask.
        os.fchmod(fd, mode)
        if content:
            os.write(fd, content.encode())
        gid = session_user.session_gid()
        if _login_runs_delegated() and gid is not None:
            try:
                os.fchown(fd, -1, gid)
            except OSError as exc:
                log.warning("could not set %s's group to %s (%r's own): %s",
                            path, gid, session_user.session_user(), exc)
        return fd if keep_open else None
    except BaseException:
        keep_open = False
        raise
    finally:
        if not keep_open:
            os.close(fd)


def _spawn_login_process(config_dir: str, fifo_path: Path, log_path: Path, pid_path: Path,
                        securestorage_dir: str = "", params=None) -> None:
    """Start the detached login for CONFIG_DIR, AS THE UID THAT MUST OWN ITS CREDENTIALS, returning once
    its pid is at PID_PATH. Raises `RuntimeError` with an actionable message if it could not start.

    `params` and `securestorage_dir` ride BOTH legs: a leg that dropped one would run a different CLI or
    mint into a store the poll does not watch — on that host alone.

    Windows always delegates (`_windows_sign_in_refusal`): a seam lost since the refusals fails there visibly
    rather than forking a POSIX-shaped login (a FIFO, `start_new_session`) as the daemon."""
    if _login_runs_delegated() or host_platform.is_windows():
        _spawn_login_as_session_user(config_dir, fifo_path, log_path, pid_path, securestorage_dir, params)
    else:
        _spawn_login_in_process(config_dir, fifo_path, log_path, pid_path, securestorage_dir, params)


def _spawn_login_in_process(config_dir: str, fifo_path: Path, log_path: Path, pid_path: Path,
                           securestorage_dir: str = "", params=None) -> None:
    """The one-uid host's spawn: fork it here. Correct only where there is no session user to lock out."""
    # O_RDWR: the child inherits this fd as stdin, and with a writer always present its reads BLOCK for the
    # code instead of seeing EOF, even after this process closes its copy. O_NONBLOCK guards only the open().
    params = _CLAUDE_LOGIN if params is None else params
    fifo_fd = os.open(fifo_path, os.O_RDWR | _O_NONBLOCK)
    log_fd = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    try:
        proc = subprocess.Popen(
            # The `timeout` wrapper is the MACHINE's, not the vendor's — `login_argv` does not carry it.
            ["timeout", str(int(_LOGIN_PROC_TIMEOUT)), *params.login_argv()],
            env=_login_env(config_dir, securestorage_dir, params),
            stdin=fifo_fd, stdout=log_fd, stderr=subprocess.STDOUT,
            start_new_session=True, close_fds=True,
        )
    finally:
        # This process must NOT keep its O_RDWR FIFO writer: the child would never see the code line as a
        # complete read.
        os.close(fifo_fd)
        os.close(log_fd)
    pid_path.write_text(str(proc.pid))


def _spawn_login_as_session_user(config_dir: str, fifo_path: Path, log_path: Path, pid_path: Path,
                                securestorage_dir: str = "", params=None) -> None:
    """The split host's spawn: one delegated shell that backgrounds the login in its own session and
    returns, leaving a process owned by the session user reading the FIFO this process created.

    The env is spelled out, never inherited (the daemon's carries `NODE_TOKEN`). No DISPLAY and
    `BROWSER=true` keep the CLI on its print-the-URL path. The vendor overlay goes last, as in
    `_login_env`, and is never logged (secret-bearing)."""
    import time

    params = _CLAUDE_LOGIN if params is None else params
    login_argv = params.login_argv()
    spawn_sh, child_sh = ((_WINDOWS_LOGIN_SPAWN_SH, _WINDOWS_LOGIN_CHILD_SH) if host_platform.is_windows()
                          else (_LOGIN_SPAWN_SH.format(new_session=session_user.new_session_cmd()), _LOGIN_CHILD_SH))
    # The script reads its paths in the shell's own spelling (MSYS form on Windows, unchanged elsewhere).
    runtime_paths = [session_user._shell_path(str(p)) for p in (fifo_path, log_path, pid_path)]
    argv = ["/bin/sh", "-c", spawn_sh, "jaahd-spawn-login", child_sh,
            *runtime_paths, str(int(_LOGIN_PROC_TIMEOUT)), *login_argv]
    try:
        proc = session_user.run_as_session_user(
            argv,
            env={"BROWSER": "true",
                 **_securestorage_env(securestorage_dir),
                 **params.login_env(config_dir=config_dir)},
            timeout=_DELEGATED_TIMEOUT)
    except Exception as exc:  # noqa: BLE001 — RuntimeError (switch gone), TimeoutExpired, OSError
        raise RuntimeError(
            f"could not start `{shlex.join(login_argv)}` as {session_user.session_user()!r} "
            f"({type(exc).__name__}: {exc}) — the fleet tmux server (jaah-runner@<node name>.service) and "
            f"{session_user.exec_spool()} must both be up; running it as this uid would take ownership of "
            f"{config_dir}") from exc
    if proc.returncode != 0:
        raise RuntimeError(
            f"the delegated `{shlex.join(login_argv)}` spawn for {config_dir} exited {proc.returncode}: "
            f"{(proc.stderr or proc.stdout or '').strip()[-500:]}")

    # Wait for the pid file so "started" is honest: phase 2 finds the process by it.
    deadline = time.monotonic() + _LOGIN_PID_WAIT
    while not pid_path.exists():
        if time.monotonic() > deadline:
            raise RuntimeError(
                f"the delegated `{shlex.join(login_argv)}` for {config_dir} never recorded its pid at "
                f"{pid_path} within {_LOGIN_PID_WAIT:.0f}s — check the fleet tmux server and that user "
                f"{session_user.session_user()!r} can write {pid_path.parent}")
        time.sleep(0.05)


def _account_add_runtime(slug: str) -> tuple[Path, Path, Path, Path]:
    """Deterministic per-slug IPC paths for the detached login process: (dir, stdin FIFO, stdout log,
    pid file). Deterministic (not random) so the second half of the flow — `account_add_submit_code`,
    running in a SEPARATE process — can re-derive them from the slug alone."""
    d = _account_add_runtime_root() / slug
    # Windows has no FIFO: its stdin channel is a regular file the child waits for (`_deliver_login_code`).
    stdin_name = "code" if host_platform.is_windows() else "stdin.fifo"
    return d, d / stdin_name, d / "stdout.log", d / "login.pid"


def _account_add_handover_root() -> Path:
    """Where phase 1 leaves phase 2 its HANDOVER files (store, baseline, vendor, seal key) — not IPC, so
    nothing the login process needs. The runtime root off Windows. On Windows the daemon-only
    `state\\account-add`: `run\\` is runner-writable and nothing there refuses a link, so a handover read
    from it could be redirected onto any file the daemon can open (`etc\\node.env`). Created and removed by
    the daemon (`_login_start`, `_cleanup_account_add_runtime`)."""
    if host_platform.is_windows():
        return Path(host_platform.paths().state_dir, "account-add")
    return _account_add_runtime_root()


def _login_handover_dir(slug: str) -> Path:
    """The per-slug handover dir (`_account_add_handover_root`); the runtime dir itself off Windows."""
    return _account_add_handover_root() / slug


def _pre_creds_mtime_path(slug: str) -> Path:
    """Where phase 1 stashes the pre-exchange credentials-file mtime for phase 2's freshness check — in the
    per-slug handover dir, so the value never crosses the network as an unvalidated inbound argument."""
    return _login_handover_dir(slug) / "pre_creds_mtime"


def _read_pre_creds_mtime(slug: str) -> float | None:
    """Phase 2's read side of `_pre_creds_mtime_path` — missing/empty/malformed reads as None (no
    baseline), matching phase 1's own None-if-absent convention for a fresh account's first login."""
    try:
        if host_platform.is_windows():
            text = _read_own_handover(_pre_creds_mtime_path(slug), "pre-creds-mtime")
        else:
            text = _pre_creds_mtime_path(slug).read_text().strip()
    except OSError:
        return None
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _login_store_path(slug: str) -> Path:
    """Where phase 1 stashes the credential store it gave the login, for phase 2 to poll — beside the
    mtime baseline, since the two are one fact ("which file, and how fresh").

    Handed over, not re-resolved: a store mount that comes or goes during the browser round trip would make
    phase 2 poll the wrong file and report `login_timeout` for a login that succeeded. Never leaves this
    node."""
    return _login_handover_dir(slug) / "login_store"


def _read_login_store(slug: str) -> str:
    """Phase 2's read side of `_login_store_path` — missing/empty reads as `""` (no store), which is both
    phase 1's own spelling for a node-pinned login and the safe answer for a runtime dir that a
    concurrent cleanup has already removed.

    Not `read_text()`, for `_read_login_provider`'s reasons (symlink, planted FIFO, no bound) in the same
    runner-writable dir. Every failure reads as `""`. The value is NEVER echoed (`_login_store_refusal`)."""
    try:
        return _read_own_handover(_login_store_path(slug), "login-store")
    except OSError:
        return ""


def _read_own_handover(path: Path, what: str) -> str:
    """One handover file's stripped text, bounded at 4 KiB: `O_NOFOLLOW` + `O_NONBLOCK` + `S_ISREG`, and on
    Windows also the daemon's own create (`_is_daemons_own`) — owner checked there only, so Linux reads as
    it always has. Raises `OSError` on a missing file; every refusal (logged, `what` names it) reads `""`."""
    fd = os.open(path, os.O_RDONLY | _O_NONBLOCK | _O_NOFOLLOW | _O_BINARY)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or (host_platform.is_windows() and not _is_daemons_own(fd, st)):
            log.warning("refusing a non-regular or foreign %s handover at %s — reading it as absent", what, path)
            return ""
        return os.read(fd, 4096).decode("utf-8", "replace").strip()
    finally:
        os.close(fd)


def _login_seal_key_path(slug: str) -> Path:
    """Where phase 1 stashes this login's PRIVATE seal key (PEM, 0600, via `_create_login_runtime_file`)
    for phase 2 to unseal the pasted code with — beside the store and the vendor, same dir, same
    lifetime, same cleanup. Written by every phase 1.

    The one secret of the sealed handoff that exists anywhere: its public half went out as `seal_key`,
    and the control plane relays what the browser sealed to it without being able to read it. Never
    leaves this node; `_cleanup_account_add_runtime` destroys it with the dir."""
    return _login_handover_dir(slug) / "seal.pem"


def _is_daemons_own(fd: int, st: os.stat_result) -> bool:
    """Whether the handover file open on FD (`st` its `fstat`) is this daemon's own create, not a plant: its
    uid off Windows. On Windows every `st_uid` is 0, so the owner SID decides, and it must be THIS process's
    own (`windows.current_sid`, `NT SERVICE\\jaahd`): the files live in the daemon-only `state\\`, which only
    this daemon writes, so "not the runner's" would also admit an Administrators-owned file put there.
    False when either SID cannot be read (fail closed)."""
    if not host_platform.is_windows():
        return st.st_uid == os.geteuid()
    try:
        return windows.file_owner_sid(fd) == windows.current_sid()
    except OSError:
        return False


def _read_login_seal_key(slug: str) -> bytes | None:
    """Phase 2's read side of `_login_seal_key_path` — None when there is no key (phase 1 ran for an older
    control plane, or the runtime dir is gone), which the caller reports as `login_seal_invalid`.

    Symlink-safe like `_read_login_store` (`O_NOFOLLOW`, `S_ISREG`) and owner-checked like
    `_read_login_provider`: the dir is runner-writable between the halves, and a planted key could only
    make the unseal fail, but a planted FIFO would wedge the read. Bounded: a 2048-bit PEM is ~1.7 KiB."""
    try:
        fd = os.open(_login_seal_key_path(slug), os.O_RDONLY | _O_NONBLOCK | _O_NOFOLLOW | _O_BINARY)
    except OSError:
        return None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or not _is_daemons_own(fd, st):
            log.warning("refusing a non-regular or foreign seal key for %s — reading it as absent", slug)
            return None
        pem = os.read(fd, 16384)
    except OSError:
        return None
    finally:
        os.close(fd)
    return pem or None


# The shape a provider value may take out of the phase-1 handover file (the catalogue's spelling);
# enforced in `_read_login_provider`.
_PROVIDER_VALUE_RE = re.compile(r"[a-z0-9][a-z0-9_-]{0,31}\Z")


def _login_provider_path(slug: str) -> Path:
    """Where phase 1 stashes the PROVIDER it drove this login for, so phase 2 drives the same one.

    Phase 2's verbs carry no `provider` on the wire, so this is the only answer that cannot disagree with
    the running login (else a provider account would be finalized with `claude`). Never leaves this node."""
    return _login_handover_dir(slug) / "login_provider"


def _read_login_provider(slug: str) -> str:
    """Phase 2's read side of `_login_provider_path` — missing/empty reads as `agent_providers
    .DEFAULT_PROVIDER`, i.e. Claude.

    Absent means Claude (`_arg_provider`'s rule); a concurrently-removed dir fails on the pid file anyway."""
    path = _login_provider_path(slug)
    try:
        # This value selects which binary `_finalize_account_add` runs against a fresh credential, and the
        # runtime dir is runner-writable for minutes between phases. `O_NOFOLLOW` + `S_ISREG` refuse a plant;
        # `O_NONBLOCK` is what lets the FIFO arm run at all (a writerless FIFO open never returns). Refusal
        # falls back to the DEFAULT, which never runs another vendor's binary.
        fd = os.open(path, os.O_RDONLY | _O_NONBLOCK | _O_NOFOLLOW | _O_BINARY)
    except OSError:
        return agent_providers.DEFAULT_PROVIDER
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            log.warning("refusing a non-regular login-provider handover at %s — reading it as %s",
                        path, agent_providers.DEFAULT_PROVIDER)
            return agent_providers.DEFAULT_PROVIDER
        # Ownership too: for a Claude login this path is never written, so a runner can simply create it.
        # And `st_nlink == 1`: a runner can hardlink the daemon-owned, group-rw `stdout.log` here and pass
        # every other check. The daemon's own `O_EXCL` create is always owned by it with one link.
        if not _is_daemons_own(fd, st) or st.st_nlink != 1:
            log.warning("refusing a login-provider handover at %s (uid %s, nlink %s) — reading it as %s",
                        path, st.st_uid, st.st_nlink, agent_providers.DEFAULT_PROVIDER)
            return agent_providers.DEFAULT_PROVIDER
        value = os.read(fd, 256).decode(errors="replace").strip()
    except OSError:
        return agent_providers.DEFAULT_PROVIDER
    finally:
        os.close(fd)
    if not value:
        return agent_providers.DEFAULT_PROVIDER
    # Shape-checked before it can be echoed: `_login_provider_refusal` puts it in a stored, rendered reply.
    # Grammar, not membership, so an unknown well-formed value still gets that refusal's operator message.
    if not _PROVIDER_VALUE_RE.match(value):
        log.warning("refusing a malformed login-provider handover at %s (%d chars) — reading it as %s",
                    path, len(value), agent_providers.DEFAULT_PROVIDER)
        return agent_providers.DEFAULT_PROVIDER
    return value


# The shape a `LoginAdapter.refusal` reason may take — a snake_case code only. The enforcement of
# `agents/login.py`'s closed-set rule: a reason built from the log tail would carry a token to the CP.
_REFUSAL_CODE_RE = re.compile(r"[a-z0-9][a-z0-9_]{0,63}\Z")


def _contained_refusal(params, log_tail: str) -> str | None:
    """This vendor's declared refusal, or None — with anything that is not a closed-set CODE discarded.

    Fails closed to None (the scrape diagnosis), never the raw value. The adapter is logged, the value not."""
    try:
        reason = params.refusal(log_tail)
    except Exception:  # noqa: BLE001 — an adapter defect must not turn a scrape miss into a 500
        log.exception("login adapter %r raised from refusal() — falling back to the scrape diagnosis",
                      getattr(params, "provider", "?"))
        return None
    if reason is None:
        return None
    if not isinstance(reason, str) or not _REFUSAL_CODE_RE.match(reason):
        log.error("login adapter %r returned a refusal reason outside the closed set (%d chars, "
                  "non-conforming) — discarding it rather than reporting CLI output to the control plane",
                  getattr(params, "provider", "?"), len(str(reason)))
        return None
    return reason


def _login_pre_mtime(declared, config_dir: str, securestorage_dir: str) -> float | None:
    """The BEFORE side of the freshness pair `_login_creds_fresh` compares against — the mtime of the file
    THIS vendor's login is going to write, taken before the login spawns.

    Both halves must stat the SAME file, or the comparison passes on the first tick. None (nothing there
    yet) is a real answer."""
    if declared is not None:
        return _path_mtime(Path(declared.credential_path(config_dir=config_dir)))
    return _creds_mtime(config_dir, securestorage_dir)

# ───────────────────────────────────────────────────────────────────────────────────────────────────
# THE SCRAPE CHANNEL — the login's stdout log, read through a descriptor rather than through its path
# ───────────────────────────────────────────────────────────────────────────────────────────────────
#
# The authorize URL and one-time code are scraped from a log the session uid (every runner) can append
# to, truncate, or replace. Reads go through the fd `_login_start` pinned at create, so a swapped inode is
# never read; the channel is refused as poisoned when the size SHRINKS or the path stops naming the inode.
# Not closed: appends (partial guard in `_scrape_user_code`), an in-place `pwrite` erase, a truncate-and-
# refill between polls. The real fix is handing the login an inherited `O_APPEND` fd instead of a
# group-writable path. `by_path()` (tests only) re-opens the name each poll: one read implementation.

# How much of the log a scrape may read. A banner is a few hundred bytes (the measured Codex
# transcript is under 500); the bound is for the adversarial, attacker-sized log, on every scrape read.
_LOG_SCRAPE_TAIL_BYTES = 64 * 1024


class _ScrapedLog:
    """The login's stdout log as a READER, pinned to the inode the daemon created when it can be.

    `pinned()` on the login path, `by_path()` otherwise. `read_tail()` returns the escape-stripped tail,
    or None: the channel is untrustworthy and nothing read through it may be published. Owns its fd and
    must be closed (`_login_start`'s `finally`)."""

    __slots__ = ("_path", "_fd", "_high_water", "_broken")

    def __init__(self, path: Path, fd: int | None) -> None:
        self._path = path
        self._fd = fd
        # The largest size ever observed, so a shrink is caught even if the length is restored later.
        self._high_water = -1
        self._broken = False

    @classmethod
    def pinned(cls, path: Path) -> "_ScrapedLog":
        """Hold an O_RDONLY descriptor on `path` — the caller must have just created it.

        A failure to pin is FATAL, never a by-path fallback (which would silence every guard): the
        caller's `O_EXCL` create just proved the path empty, so a failing `O_NOFOLLOW` is a plant in
        progress. Fatal is not hostile: only name-errnos (`ELOOP`/`ENOENT`/`ENOTDIR`) are reported as an
        attack; resource exhaustion is a plain `RuntimeError`. `O_NONBLOCK` + `S_ISREG` on the fd refuse a
        planted FIFO, which would otherwise hang the daemon inside `os.open`."""
        try:
            fd = os.open(path, os.O_RDONLY | _O_NONBLOCK | _O_NOFOLLOW | _O_CLOEXEC | _O_BINARY)
        except OSError as exc:
            # The errno decides the diagnosis, never the outcome (both arms are fatal): only name-errnos
            # are the plant; EMFILE/ENOMEM/ENOSPC are capacity and must not send the operator hunting.
            if exc.errno in (errno.ELOOP, errno.ENOENT, errno.ENOTDIR):
                raise _LoginRuntimeUnsafe(
                    f"refusing to scrape {path}: this attempt created it as a regular file a statement "
                    f"earlier, so a descriptor pin that now fails ({exc}) means the name was redirected in "
                    f"between — the symlink hijack the pin exists to refuse. Nothing was published. Retry; "
                    f"if it recurs, audit who else can write {path.parent} (it is group "
                    f"{session_user.session_user()!r} by design, so a compromised fleet runner is the shape "
                    f"to look for)."
                ) from exc
            raise RuntimeError(
                f"cannot scrape {path}: the login log this attempt just created could not be opened for "
                f"reading ({exc}). That errno is about this HOST, not about the file — a descriptor "
                f"table, a memory or an inode limit reached — so nothing was tampered with and there is "
                f"nothing to audit in {path.parent}. Nothing was published. Retry once the node has room."
            ) from exc
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            os.close(fd)
            raise _LoginRuntimeUnsafe(
                f"refusing to scrape {path}: this attempt created it as a regular file a statement "
                f"earlier and the pin has landed on something else — a FIFO or another special file "
                f"planted in between, which `O_NOFOLLOW` alone does not refuse and which would have hung "
                f"this open had it not been non-blocking. Nothing was published. Retry; if it recurs, "
                f"audit who else can write {path.parent} (it is group "
                f"{session_user.session_user()!r} by design, so a compromised fleet runner is the shape "
                f"to look for)."
            )
        return cls(path, fd)

    @classmethod
    def by_path(cls, path: Path) -> "_ScrapedLog":
        """A reader with no descriptor: re-resolves the name on every read. See the fallback note above.

        Not reachable from the login path; for the scraper tests."""
        return cls(path, None)

    @property
    def is_pinned(self) -> bool:
        """Whether this reader holds a descriptor (`pinned()`, or the create's own) rather than a name."""
        return self._fd is not None

    def close(self) -> None:
        """Release the descriptor. Idempotent, and never raises — it is called from a `finally`."""
        fd, self._fd = self._fd, None
        if fd is None:
            return
        try:
            os.close(fd)
        except OSError:
            pass

    def read_tail(self) -> str | None:
        """The last `_LOG_SCRAPE_TAIL_BYTES` of the log, escape-stripped — or None if the channel is poisoned.

        `""` is "nothing readable this tick" (wait); None is truncated/replaced (stop, publish nothing).
        Poison is sticky, so a shrink-then-restore cannot clean the channel. Lenient decode: the tail cut
        may land mid-codepoint."""
        if self._broken:
            return None
        try:
            observed = self._read_bytes()
        except OSError:
            # One tick, not the window: EACCES during the hand-off, ESTALE, EIO are not tampering.
            return ""
        if observed is None:
            return None
        size, raw = observed
        if size < self._high_water:
            log.error("the login log %s SHRANK during a scrape (%d bytes, was %d) — it is group-writable "
                      "by the session user, so a truncation is an erase of the CLI's own output and "
                      "nothing read through this channel may be published", self._path, size,
                      self._high_water)
            self._broken = True
            return None
        self._high_water = max(self._high_water, size)
        return _strip_escapes(raw.decode("utf-8", errors="replace"))

    def _read_bytes(self) -> tuple[int, bytes] | None:
        """`(size, tail bytes)` for this tick through the pinned fd where there is one, or None if the path
        and the pinned inode have come apart.

        A pinned reader compares `fstat(fd)` with `stat(path)`; a mismatch is returned as None, not
        raised, so `read_tail`'s transient-`OSError` handler cannot absorb it."""
        if self._fd is None:
            # `pinned()`'s flags and refusal: a planted FIFO would hang the polling thread in `os.open`.
            # Kept although off the login path: every scraper accepts a bare `Path` by signature.
            fd = os.open(self._path, os.O_RDONLY | _O_NONBLOCK | _O_NOFOLLOW | _O_CLOEXEC | _O_BINARY)
            try:
                if not stat.S_ISREG(os.fstat(fd).st_mode):
                    log.error("the login log %s is not a regular file — a FIFO or another special file is "
                              "wearing the name, and nothing read through this channel may be published",
                              self._path)
                    self._broken = True
                    return None
                return self._pread(fd)
            finally:
                os.close(fd)
        pinned = os.fstat(self._fd)
        try:
            named = os.stat(self._path)
        except OSError:
            named = None
        if named is None or (named.st_ino, named.st_dev) != (pinned.st_ino, pinned.st_dev):
            log.error("the login log %s no longer names the inode this scrape is pinned to — it was "
                      "replaced or unlinked, and nothing read through this channel may be published",
                      self._path)
            self._broken = True
            return None
        return self._pread(self._fd)

    @staticmethod
    def _pread(fd: int) -> tuple[int, bytes]:
        """The tail of `fd` via `pread`, without moving its file offset — or, where there is no `pread`
        (Windows), a seek and a read: every read seeks first, so the moved offset is never relied on."""
        size = os.fstat(fd).st_size
        start = max(0, size - _LOG_SCRAPE_TAIL_BYTES)
        if not hasattr(os, "pread"):
            os.lseek(fd, start, os.SEEK_SET)
            return size, os.read(fd, size - start)
        return size, os.pread(fd, size - start, start)


def _as_scraped_log(source: "Path | _ScrapedLog") -> tuple[_ScrapedLog, bool]:
    """`(reader, owned)` — accept either a pinned reader or a bare path at the two scrape entry points.

    A pinned reader is the caller's to close; a wrapped `Path` is closed by the scraper."""
    if isinstance(source, _ScrapedLog):
        return source, False
    return _ScrapedLog.by_path(source), True


# The shape a publishable one-time code may take (containment twin of `_REFUSAL_CODE_RE`): anything
# else is an over-matched slice of CLI output. Not `_OAUTH_CODE_RE`, which guards an INBOUND paste.
_USER_CODE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{0,63}\Z")

# How long to look for the one-time code after the URL is found: the CLI prints them on consecutive
# lines, and the URL is published either way.
_USER_CODE_SCRAPE_TIMEOUT = 5.0


def _scrape_user_code(log_source, deadline: float, params=None) -> str | None:
    """This vendor's one-time DEVICE CODE from the login's stdout log; `""` when one was expected and did
    not arrive; None when this vendor's login shows the operator no code at all.

    A code ⇒ link + code; `""` ⇒ a warning (device auth, no code: restart); None ⇒ the ordinary link
    sentence, true only for a link-only login. So every failure (miss, over-match, poisoned log, adapter
    raise) answers `""`; None comes only from an adapter declaring no `user_code_re`, which keeps Claude's
    reply byte-identical. The try covers the whole loop so an adapter defect never throws away a login
    whose URL is already in hand; read errors cost one tick (`_ScrapedLog.read_tail`). Polled: the code
    line can land microseconds after the URL."""
    import time

    pattern_of = getattr(params, "user_code_re", None)
    if pattern_of is None:
        return None
    provider = getattr(params, "provider", "?")
    reader, owned = _as_scraped_log(log_source)
    try:
        pattern = pattern_of()
        # Ambiguity is poison: any runner (the session uid) may append a banner + code of its choosing,
        # and the operator would authorize the runner's device flow. Distinct candidates are accumulated
        # across the WHOLE window (draining the deadline), so a code planted before the CLI's own line
        # still meets the genuine one; two or more publish nothing. Not a boundary: SIGSTOPping the CLI or
        # a slow round trip keeps the genuine banner out. The real remedy is `_ScrapedLog`'s `O_APPEND` fd.
        candidates: set = set()
        while time.monotonic() < deadline:
            raw = reader.read_tail()
            if raw is None:
                # Poisoned channel (logged by `_ScrapedLog`): nothing to publish.
                return ""
            candidates.update(pattern.findall(raw))
            if len(candidates) > 1:
                log.error("login adapter %r saw %d distinct one-time-code candidates in one login log — "
                          "refusing to publish any of them; the log is appendable by the session user, so "
                          "an ambiguous log is a poisoned log", provider, len(candidates))
                return ""
            time.sleep(0.1)
        if candidates:
            code = candidates.pop()
            if isinstance(code, str) and _USER_CODE_RE.match(code):
                return code
            log.error("login adapter %r matched a one-time code outside the publishable shape "
                      "(%d chars, non-conforming) — discarding it rather than putting CLI output on "
                      "the wire", provider, len(str(code)))
            return ""
    except Exception:  # noqa: BLE001 — see the containment paragraph above; the login is already good
        log.exception("login adapter %r failed while scraping its one-time code — the operator is told "
                      "the code is missing rather than losing a login whose URL is already in hand",
                      provider)
        return ""
    finally:
        if owned:
            reader.close()
    log.warning("login adapter %r declares a one-time code but printed none within the scrape window",
                provider)
    return ""


def _login_creds_fresh(declared, config_dir: str, securestorage_dir: str,
                       pre_mtime: float | None) -> bool:
    """Has THIS login's exchange produced a usable credential yet — the one question both phase-2 cores
    poll on, asked once here so the two cannot drift apart.

    A registered adapter answers for its own file (`credential_path`); Claude's answer is a PAIR — mtime
    moved AND token shape — because a re-auth's dir already holds the old file."""
    # `declared` is the adapter or None, never an identity test against the Claude singleton.
    if declared is not None:
        cred = Path(declared.credential_path(config_dir=config_dir))
        # 1) Freshness: an unmoved mtime (vs phase 1's `_login_pre_mtime`) means nothing written yet.
        if _path_mtime(cred) == pre_mtime:
            return False
        # 2) Where the seam exists, the delegated shape check DECIDES — not `credential_present(...) or`:
        #    an `lstat`-only adapter check would let any runner dropping one byte there finalize the
        #    account, and an adapter that reads the 0600 file gets PermissionError on every split node (so
        #    not `and` either). Without the seam the adapter is authoritative.
        if _login_runs_delegated():
            return _delegated_credential_written(cred)
        return declared.credential_present(config_dir=config_dir)
    # Mtime first: a local stat gates the delegated shape check to one round trip per attempt.
    return (_creds_mtime(config_dir, securestorage_dir) != pre_mtime
            and _creds_have_token(config_dir, securestorage_dir))


def _login_exited_error(declared, config_dir: str, securestorage_dir: str) -> str:
    """The error for a login that exited without a fresh credential: `login_rejected`, plus — on macOS, for
    Claude Code's machine-finished login — where the token may have gone instead.

    There the CLI saves its token in the user's login Keychain when it can, and writes `.credentials.json`
    into `CLAUDE_SECURESTORAGE_CONFIG_DIR` only when that fails. The session user is a hidden service account
    with no login Keychain, so the file is the expected outcome; no setting is known that forces it (none is
    invented here). A Keychain that does exist would take the token where no runner or other node can read
    it, and would look exactly like a rejection, so the detail names it. The head stays `login_rejected`:
    the console keys on it."""
    if declared is not None or not host_platform.is_macos():
        return "login_rejected"
    vendor = agent_providers.CLAUDE
    store = Path(shared_storage.token_dir(config_dir, securestorage_dir)) / vendor.credential_file
    return (f"login_rejected: the login ended with no token in {store}. If the code was accepted, {vendor.label} "
            f"saved it in user {session_user.session_user()!r}'s macOS Keychain instead of that file: that "
            f"user must have no login Keychain. Remove it, then sign in again.")


# CLAUDE CODE'S LOGIN: its registry row (`agents/claude_login.py`), resolved once. Its phishing anchor is that
# module's `OAUTH_URL_RE`, aliased here because the machine's readers name it — never a second copy.
_CLAUDE_LOGIN = agent_login.login_adapter_for(agent_providers.CLAUDE.value)
_OAUTH_URL_RE = _CLAUDE_LOGIN.auth_url_re()


def _login_vendor(provider: str = agent_providers.DEFAULT_PROVIDER) -> tuple:
    """Everything the machine needs to know about WHICH login this is, resolved once: `(declared, params,
    vendor_arg)`.

    Every caller must assert `_login_provider_refusal` FIRST; phase 2 re-asserts against phase 1's recorded
    provider, since the daemon may roll back between halves.
      * `declared` — the adapter whose own members finish this login, or None for a row the machine's own
        steps finish (`machine_finished`: Claude Code's). The completion mode turns on it: an adapter's
        `completion()`, paste otherwise.
      * `params` — the registry row: the argv, env pin and phishing anchor.
      * `vendor_arg` — `params` as a splattable tail, empty for a machine-finished row, so Claude's calls are
        unchanged."""
    declared = _declared_login_adapter(provider)
    params = agent_login.login_adapter_for(provider)
    if params is None:
        # Fail closed: Claude's row here would mint a Claude credential into another vendor's account.
        raise ValueError(f"no login adapter for provider {provider!r}")
    return declared, params, (() if declared is None else (params,))


def _declared_login_adapter(provider: str):
    """`provider`'s login adapter when its own `credential_*`/`finalize` members finish the login, else None —
    no row, or a row the machine's own steps finish (the optional `machine_finished`, `agents/login.py`)."""
    adapter = agent_login.login_adapter_for(provider)
    return None if adapter is None or getattr(adapter, "machine_finished", False) else adapter


def _scrape_oauth_url(log_source, deadline: float, params=None) -> str | None:
    """Poll the login process's stdout log for the authorization URL until `deadline` (monotonic).
    Reads the log's tail each pass (`_ScrapedLog`, which also strips OSC-8/SGR escapes so the match runs on
    plain text) and returns the one URL matching this login's own brand anchor, or None if the deadline
    passes without one (a CLI-output-format regression → oauth_url_scrape_failed).

    A poisoned reader (see `_ScrapedLog`) is the same absent-URL outcome: never publish a rewritten log's link.

    Deliberately FIRST-MATCH, no distinct-count guard: a rejected code makes a vendor CLI mint a fresh
    URL into the same log, so two URLs are
    legitimate. The weight rests on the adapter's anchor (a constant device page gives a planted
    `?user_code=` nowhere to go) plus the channel. Residual: a varying-URL anchor (Claude's) and an
    attacker writing first.

    The ONE URL reader, every vendor (for Claude it is the MANUAL URL the CLI prints). `params` supplies
    the anchor and, where present, the banner gate."""
    import time

    params = _CLAUDE_LOGIN if params is None else params
    pattern = params.auth_url_re()
    # Optional (`agents/login.py`; Claude Code's row carries it): no attribute means no banner gate, and the
    # vendor's own anchored pattern is then the whole guard.
    banner_seen = getattr(params, "url_banner_seen", None)
    reader, owned = _as_scraped_log(log_source)
    try:
        while time.monotonic() < deadline:
            clean = reader.read_tail()
            if clean is None:
                # Poisoned channel (logged by `_ScrapedLog`): the ordinary absent-URL outcome.
                return None
            if banner_seen is None or banner_seen(clean):
                m = pattern.search(clean)
                if m:
                    return m.group(0)
            time.sleep(0.1)
        return None
    finally:
        if owned:
            reader.close()


def _login_failed_in_log(log_path: Path, params=None) -> bool:
    """True iff the login process's stdout log shows a rejected-code failure. Needed because the code is
    delivered over a FIFO that never EOFs (its stdin stays open), so on a BAD code the CLI does NOT exit
    — it prints `Login failed: …` and RE-PROMPTS for another code (verified against the live CLI). Without
    scanning the log, a bad code would only surface as a slow `login_timeout` instead of a prompt
    `login_rejected`.

    The wording is the vendor's (`login_rejected_in_log`); a vendor declaring none reports rejection only
    by process exit."""
    params = _CLAUDE_LOGIN if params is None else params
    # No attribute means no gate, as with `url_banner_seen`.
    rejected = getattr(params, "login_rejected_in_log", None)
    if rejected is None:
        return False
    try:
        text = _strip_escapes(_read_runner_text(log_path, errors="replace")).lower()
    except FileNotFoundError:
        return False
    try:
        # Guarded like `_contained_refusal`: the callable sees the whole log (codes, tokens), and an
        # exception carrying it would reach the journal. `bool()` contains the return value.
        return bool(rejected(text))
    except Exception:  # noqa: BLE001 — an adapter defect must not decide a login's verdict
        log.exception("login adapter %r raised from login_rejected_in_log() — reporting no rejection",
                      getattr(params, "provider", "?"))
        return False


def _pid_alive(pid: int) -> bool:
    """True iff a process with this pid exists (signal 0 probes without delivering). Best-effort — a pid
    reused by an unrelated process is indistinguishable, but the login process is short-lived and
    self-terminating, so the window is small and the worst case is a login_rejected the operator retries."""
    if pid <= 0:
        return False
    if host_platform.is_windows():
        # NEVER `os.kill` there: any signal but CTRL_C/CTRL_BREAK is `TerminateProcess`, so a "probe" of a
        # runner-planted pid would kill it with the daemon's rights. The pid is an MSYS one besides.
        return _msys_login_alive(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


# The smallest process group this may ever signal: `killpg(1, sig)` is `kill(-1, sig)`, a UID-WIDE
# broadcast (measured: accepted on fleet nodes) — as `jaah`, the fleet tmux server and every runner.
# No spawned login can be in init's group (each is its own session leader), so the floor is free.
_MIN_SIGNALLABLE_PGID = 2

# Runs AS THE SESSION USER: SIGTERM the login's group. Python, not a shell (dash rejects
# `kill -TERM -- -123`). The floor is re-asserted here: this uid owns every runner, so a broadcast from
# here takes the node down.
_KILLPG_PY = (
    "import os, signal, sys\n"
    "pgid = int(sys.argv[1])\n"
    f"if pgid < {_MIN_SIGNALLABLE_PGID}:\n"
    "    raise SystemExit('refusing to signal process group %d — killpg below the floor is a uid-wide broadcast' % pgid)\n"
    "try:\n"
    "    os.killpg(pgid, signal.SIGTERM)\n"
    "except (ProcessLookupError, PermissionError):\n"
    "    pass\n"
)

# Windows' probe and kill, run AS THE RUNNER ACCOUNT in its MSYS runtime (bash, so `kill -- -<pgid>` is
# accepted): the pid file holds an MSYS pid — `timeout`'s, exec'd in place of `_WINDOWS_LOGIN_CHILD_SH`'s
# shell — and MSYS's own `kill` reaches the native CLI under it. The kill signals the group that pid leads
# (`set -m` made it a leader): `timeout`, the CLI and the code feeder all receive the TERM directly. The pid
# alone when it leads none. The floor is re-asserted here, as in `_KILLPG_PY`: this account owns every runner.
_MSYS_ALIVE_SH = 'kill -0 "$1" 2>/dev/null\n'
_MSYS_KILL_SH = (
    f'[ "$1" -ge {_MIN_SIGNALLABLE_PGID} ] || exit 2\n'
    'kill -TERM -- "-$1" 2>/dev/null || kill -TERM "$1" 2>/dev/null\n'
    'exit 0\n'
)


def _msys_login_alive(pid: int) -> bool:
    """`_pid_alive` on Windows: `kill -0` as the runner account. A seam that cannot answer reads as alive
    (logged), the direction that never fails a working login — the login's own `timeout` still ends it."""
    try:
        proc = session_user.run_as_session_user(
            ["/bin/sh", "-c", _MSYS_ALIVE_SH, "jaahd-login-alive", str(pid)], timeout=_DELEGATED_TIMEOUT)
    except Exception as exc:  # noqa: BLE001 — RuntimeError (switch gone), TimeoutExpired, OSError
        log.warning("could not probe login pid %s as %r (%s: %s) — treating it as alive",
                    pid, session_user.session_user(), type(exc).__name__, exc)
        return True
    return proc.returncode == 0


def _kill_msys_login(pid: int) -> None:
    """`_kill_login_proc` on Windows: SIGTERM the login's group as the runner account. Best-effort with a
    loud log, as `_kill_login_proc_as_session_user`."""
    try:
        session_user.run_as_session_user(
            ["/bin/sh", "-c", _MSYS_KILL_SH, "jaahd-login-kill", str(pid)], timeout=_DELEGATED_TIMEOUT)
    except Exception as exc:  # noqa: BLE001 — RuntimeError (switch gone), TimeoutExpired, OSError
        log.warning("delegated SIGTERM of login pid %s failed (%s: %s) — it will exit on its own `timeout` "
                    "(up to %.0fs)", pid, type(exc).__name__, exc, _LOGIN_PROC_TIMEOUT)


def _kill_login_proc(pid_path: Path) -> None:
    """Best-effort terminate the detached login process recorded at `pid_path` (SIGTERM the whole
    session — it was spawned in its own). Used on failure/cleanup and by account_delete so a torn-down or
    replaced add never leaves a `claude auth login` waiting on a FIFO forever.

    Re-checks liveness and derives the pgid from the pid (narrowing pid reuse). An `EPERM` (a session-user
    login) is delegated, never swallowed, else the old login outlives a retry's replaced FIFO."""
    try:
        pid = int(_read_runner_text(pid_path).strip())
    except (FileNotFoundError, ValueError):
        return
    if pid < _MIN_SIGNALLABLE_PGID or not _pid_alive(pid):
        return
    if host_platform.is_windows():
        _kill_msys_login(pid)
        return
    try:
        pgid = os.getpgid(pid)
    except OSError:
        pgid = pid
    # Not redundant with the pid check: the PGID decides the blast radius (see `_MIN_SIGNALLABLE_PGID`).
    if pgid < _MIN_SIGNALLABLE_PGID:
        log.warning(
            "refusing to SIGTERM login process group %s resolved from pid %s (%s) — killpg(1) is a "
            "uid-wide broadcast that would kill this node's fleet tmux server and every runner in it. "
            "Leaving the login process to its own `timeout` (up to %.0fs).",
            pgid, pid, pid_path, _LOGIN_PROC_TIMEOUT,
        )
        return
    try:
        os.killpg(pgid, signal.SIGTERM)
        return
    except ProcessLookupError:
        return
    except PermissionError:
        pass
    except OSError:
        try:
            os.kill(pid, signal.SIGTERM)
            return
        except PermissionError:
            pass
        except OSError:
            return
    _kill_login_proc_as_session_user(pgid)


def _kill_login_proc_as_session_user(pgid: int) -> None:
    """`_kill_login_proc`'s arm for a login process this uid may not signal. Best-effort with a loud log:
    the caller is already a cleanup path, and the leaked process self-exits on its own `timeout`."""
    if not session_user.can_run_as_session_user():
        log.warning("cannot signal login process group %s and cannot delegate to %r — it will exit on its "
                    "own `timeout` (up to %.0fs)", pgid, session_user.session_user(), _LOGIN_PROC_TIMEOUT)
        return
    try:
        session_user.run_as_session_user(
            [sys.executable, "-I", "-c", _KILLPG_PY, str(pgid)], timeout=_DELEGATED_TIMEOUT)
    except Exception as exc:  # noqa: BLE001 — RuntimeError (switch gone), TimeoutExpired, OSError
        log.warning("delegated SIGTERM of login process group %s failed (%s: %s)",
                    pgid, type(exc).__name__, exc)


def _cleanup_account_add_runtime(slug: str) -> OSError | None:
    """Kill any detached login process for this slug and remove its runtime dir (FIFO/log/pid). Idempotent
    — a missing dir is success. Returns the `OSError` that stopped the removal, or None when the dir is
    gone.

    Reports the failure: `_login_start`'s `O_EXCL` creates treat an occupied path as a planted hijack, a
    premise that is false if this rmtree failed (read-only mount, `chattr +i`) and would recur forever.
    The exception carries the errno the operator repairs by. Tidying callers may ignore it."""
    runtime_dir, _, _, pid_path = _account_add_runtime(slug)
    _kill_login_proc(pid_path)
    if host_platform.is_windows():
        # Both trees: the IPC files as the runner account, the handover files (the daemon's own) here.
        failure = _remove_windows_login_runtime(runtime_dir)
        try:
            shutil.rmtree(_login_handover_dir(slug))
        except FileNotFoundError:
            pass
        except OSError as exc:
            log.warning("account-add handover cleanup failed for %s: %s", slug, exc)
            failure = failure or exc
        return failure
    try:
        shutil.rmtree(runtime_dir)
    except FileNotFoundError:
        return None
    except OSError as exc:
        log.warning("account-add runtime cleanup failed for %s: %s", slug, exc)
        return exc
    return None


def _remove_windows_login_runtime(runtime_dir: Path) -> OSError | None:
    """`_cleanup_account_add_runtime`'s removal on Windows: `rm -rf` AS THE RUNNER ACCOUNT, since the daemon
    deletes nothing in a runner-writable tree itself — a junction there needs no privilege. The
    seam's error string is carried as an `OSError` (EIO) so the caller's report reads as on Linux."""
    if not os.path.lexists(runtime_dir):
        return None
    err = session_user.remove_tree_as_session_user(str(runtime_dir), timeout=_DELEGATED_TIMEOUT)
    if not err:
        return None
    log.warning("account-add runtime cleanup failed for %s: %s", runtime_dir.name, err)
    return OSError(errno.EIO, err)


def _prov_block(step: str, *, status: str = "in_progress", oauth_url: str | None = None,
                error: str | None = None, started_at: str, pre_creds_mtime: float | None = None) -> dict:
    """The `provisioning` block shape shared by both halves of the Add-Account flow. Never carries a
    token — only the public authorization URL (NFR Security).

    `pre_creds_mtime`: the credentials file's mtime at the start of phase 1 (None if absent), so phase 2
    can tell a fresh exchange from a re-auth's pre-existing dead file."""
    return {"step": step, "oauth_url": oauth_url, "status": status,
            "error": error, "started_at": started_at, "pre_creds_mtime": pre_creds_mtime}


def _windows_sign_in_refusal(provider: str) -> str | None:
    """The refusal that stops a sign-in on a Windows node, or None (always None elsewhere). Asked by every
    phase, so a half never runs where its other half could not (PRD R10).

    The provider's catalogue row says whether its runner, and so its login, works on Windows
    (`windows_runner`). There the login always runs as the runner account through the session-user seam
    (the daemon is `NT SERVICE\\jaahd`), so a daemon that IS that account has no spawn to drive it with.
    Its runtime under `run\\` is runner-writable — the looser model the operator accepted."""
    if not host_platform.is_windows():
        return None
    row = agent_providers.by_value(provider or agent_providers.DEFAULT_PROVIDER)
    if row is None or not row.windows_runner:
        # The catalogue's label, never the value: later phases read it back from disk.
        what = f"a {row.label} account" if row is not None else "an account of an unknown provider"
        return (f"login_provider_unsupported: signing {what} in is not supported on Windows "
                f"nodes — use a Linux node")
    if not session_user.switch_needed():
        return ("login_provider_unsupported: on Windows a sign-in runs only as the runner account, "
                "delegated by the jaahd service, and this daemon is not that service — run it as "
                "NT SERVICE\\jaahd")
    return None


def _login_start(slug: str, config_dir: str, run, *, require_existing: bool = False,
                 fail_code: str = "account_add_failed",
                 provider: str = agent_providers.DEFAULT_PROVIDER,
                 peer_key: str | None) -> dict:
    """Shared phase-1 login core for BOTH `account_add` and `account_reauth`, and for EVERY provider.
    Suppresses the host browser, spawns the vendor's DETACHED login, surfaces the authorization URL, and
    returns at `awaiting_code` with the login process still alive.

    `provider` resolves once to the attempt's parameterisation (`_login_vendor`). Registration is a
    parameterisation of this machine, not a second flow: the vendor's DECLARED completion (A13) selects
    paste (`_login_submit_code`) vs wait-for-credentials (`_login_await_redirect`) — an adapter's
    `completion()`, and paste for Claude, whose catalogue row says so. Nothing is inferred from the URL
    and the operator is offered no choice the vendor lacks. One URL source for every vendor: the manual
    URL the CLI prints to its stdout log.

    `peer_key` (the browser's public key) is required: the reply carries `sealed_url` (the URL
    sealed to it) + `seal_key` (this login's own public key, for the code coming back) and NEVER
    `oauth_url`; the private half is `_login_seal_key_path`. A missing or malformed key, or a seal that
    fails, is terminal under `login_seal_invalid`; neither the URL nor key material is ever logged.

    `require_existing` (re-auth) re-checks existence UNDER the flock (TOCTOU with `account_delete`).
    Every login mints into the token store `{jaah-home}/auth/.shared/<slug>` (a `node_pinned` provider
    excepted), created here if missing; the success reply reports the store actually used."""
    windows_refusal = _windows_sign_in_refusal(provider)
    if windows_refusal:
        log.warning("login start refused for %s: %s", slug, windows_refusal)
        return {"exit_code": 1, "stdout": "", "stderr": windows_refusal}
    import time

    # `declared` is kept separate: the completion mode turns on it. A provider's `completion()` decides
    # (A13); Claude (no adapter) is paste, as its catalogue row declares. No checkbox, no URL inference.
    declared, params, vendor_arg = _login_vendor(provider)
    paste_mode = declared is None or declared.completion() == agent_providers.PASTE_CODE

    # Before any mutation or lock, like the refusals below: a key that cannot be sealed to leaves nothing
    # behind. Shape only; whether `openssl` accepts it is the seal's verdict (terminal, further down).
    if not login_seal.is_wire_key(peer_key):
        error = "login_seal_invalid: the wizard's peer key is missing or not a bounded base64 public key"
        log.warning("login start refused for %s: %s", slug, error)
        return {"exit_code": 1, "stdout": "", "stderr": error}

    # Where the token belongs, before anything is created (it decides the login env and the mtime
    # baseline): the token store, resolved on this node — never the CP's say (`_login_store_for`).
    securestorage_dir, store_refusal = _login_store_for(slug, provider)

    # Before any mutation or lock: a refusal leaves nothing behind.
    refusal = (_login_uid_refusal(config_dir, *vendor_arg) or _login_cli_refusal(*vendor_arg)
               or store_refusal or _login_store_refusal(slug, securestorage_dir))
    if refusal:
        log.warning("login start refused for %s: %s", slug, refusal)
        return {"exit_code": 1, "stdout": "", "stderr": refusal}

    runtime_dir, fifo_path, log_path, pid_path = _account_add_runtime(slug)

    def _fail(step: str, error: str, started_at: str) -> dict:
        # The log pin is released before the removal: Windows deletes no file this process holds open.
        if log_pin is not None:
            log_pin.close()
        _cleanup_account_add_runtime(slug)
        _set_provisioning(config_dir, _prov_block(step, status="error", error=error, started_at=started_at),
                          slug=slug)
        return {"exit_code": 1, "stdout": "", "stderr": error}

    started_at = _now_iso()
    # Snapshot before this attempt touches the dir: a re-auth's dir already holds the old credential.
    pre_creds_mtime = _login_pre_mtime(declared, config_dir, securestorage_dir)

    # 1) creating_dir — acquire the per-slug flock (nonblocking). If already held, another login flow is
    #    in flight → distinct in_progress_elsewhere result; do NOT overwrite the in-flight provisioning.
    lock_path = _account_add_lock_path(slug)
    lock_fh = open(lock_path, "a+")
    # The pinned log reader, hoisted so the `finally` closes it on every arm (None = nothing to close).
    log_pin: _ScrapedLog | None = None
    try:
        try:
            host_platform.lock(lock_fh, blocking=False)
        except OSError:
            lock_fh.close()
            return {"exit_code": 1, "stdout": json.dumps({"status": "in_progress_elsewhere"}),
                    "stderr": "another login is in progress for this slug"}

        if require_existing and not Path(config_dir).is_dir():
            # Disk presence, not a registry record (a provisioned-outside-the-wizard account has none).
            # The TOCTOU-safe re-check under the flock; persist nothing, which would create a phantom.
            return {"exit_code": 1, "stdout": "", "stderr": "account_not_found"}
        # A `shared_token_link` login mints in the PER-NODE home — a CLI is never run in a home another node
        # can write — and `_publish_shared_login` copies only the credential into the store. A link left by
        # an earlier login is dropped first (under the flock), so this one writes a fresh regular file.
        _drop_credential_link(config_dir, provider)

        # Clear any prior attempt's login and FIFO. A failed cleanup is terminal here, under its own code,
        # before the first `O_EXCL` create (see `_cleanup_account_add_runtime`).
        cleanup_failure = _cleanup_account_add_runtime(slug)
        if cleanup_failure is not None:
            code = errno.errorcode.get(cleanup_failure.errno, cleanup_failure.errno)
            error = (f"login_runtime_cleanup_failed: the previous attempt's runtime dir {runtime_dir} "
                     f"could not be removed ({code}: {cleanup_failure.strerror or cleanup_failure}), so "
                     f"this attempt cannot start on a clean dir and nothing was created. This is NOT the "
                     f"occupied-path refusal — nothing was planted, the leftovers are this node's own. "
                     f"Repair or free that path and retry.")
            log.error("login start for %s cannot clear its runtime dir: %s", slug, error)
            return _fail("creating_dir", error, started_at)

        _set_provisioning(config_dir,
                          _prov_block("creating_dir", started_at=started_at, pre_creds_mtime=pre_creds_mtime),
                          slug=slug)
        ensure_account_dir(config_dir)
        # The CLI creates neither the store root nor the account's leaf. Through an access point both land
        # uid/gid 3000 whatever the mode.
        if securestorage_dir:
            root_refusal = shared_storage.ensure_shared_root()
            if root_refusal:
                return _fail("creating_dir", f"login_store_not_on_share: {root_refusal}", started_at)
            # Re-asked before and after the create: a path planted since the refusal is never followed.
            if not shared_storage.store_is_on_share(securestorage_dir):
                return _fail("creating_dir", f"login_store_not_on_share: {securestorage_dir} is not a real "
                             f"directory in the token store", started_at)
            if host_platform.is_windows():
                # The runner account's, as everything under its home — and the only one that can create it
                # under a mounted NodeLink link (`\\wsl.localhost` is per-user).
                failure = session_user.ensure_dir_as_session_user(securestorage_dir)
                if failure:
                    return _fail("creating_dir", f"login_store_not_on_share: could not create "
                                 f"{securestorage_dir} ({failure})", started_at)
            else:
                os.makedirs(securestorage_dir, mode=shared_storage.STORE_DIR_MODE, exist_ok=True)
            if not shared_storage.store_is_on_share(securestorage_dir):
                return _fail("creating_dir", f"login_store_not_on_share: {securestorage_dir} is not a real "
                             f"directory in the token store", started_at)
        # Create the parent explicitly (`makedirs` applies `mode` only to the leaf). 2770 when delegated:
        # the session-user login writes its pid file here; 0700 on a one-uid host.
        delegated = _login_runs_delegated()
        runtime_dir_mode = 0o2770 if delegated else 0o700
        # Daemon-owned IPC files: 0660 group `jaah` when delegated (FIFO read, log append), else 0600.
        ipc_mode = 0o660 if delegated else 0o600
        # Windows has no mode bits or groups to share: the `run\` ACLs grant both accounts Modify.
        on_windows = host_platform.is_windows()
        if on_windows:
            # `run\` is the runner account's to create in; the handover dir is the daemon's own (`state\`).
            _ensure_runner_dir(runtime_dir)
            os.makedirs(_login_handover_dir(slug), exist_ok=True)
        else:
            _account_add_runtime_root().mkdir(mode=runtime_dir_mode, parents=True, exist_ok=True)
            os.makedirs(runtime_dir, mode=runtime_dir_mode, exist_ok=True)
        if delegated and not on_windows:
            # `exist_ok=True` does not chmod an existing dir, so state the modes.
            _share_with_session_user(_account_add_runtime_root(), runtime_dir_mode)
            _share_with_session_user(runtime_dir, runtime_dir_mode)
        # The freshness baseline, node-local (`_pre_creds_mtime_path`).
        baseline = "" if pre_creds_mtime is None else repr(pre_creds_mtime)
        if on_windows:
            # O_EXCL there: the daemon-only dir admits no plant, and phase 2 reads only the daemon's own create.
            _create_login_runtime_file(_pre_creds_mtime_path(slug), 0o600, baseline)
            _create_login_runtime_file(_login_store_path(slug), 0o600, securestorage_dir)
        else:
            _pre_creds_mtime_path(slug).write_text(baseline)
            # The store the baseline was taken of, and (below) the vendor, for phase 2.
            _login_store_path(slug).write_text(securestorage_dir)
        # Written only for a provider (absent reads as Claude). Via `_create_login_runtime_file`, and 0600,
        # not `ipc_mode`: it is not IPC, and group write would let any runner pick phase 2's binary.
        if declared is not None:
            _create_login_runtime_file(_login_provider_path(slug), 0o600, provider)
        # This login's seal key, minted here and stored 0600 like the vendor handover: phase 2 unseals
        # the pasted code with it. The PEM never reaches a log or a reply.
        try:
            seal_pem = login_seal.new_private_key()
        except login_seal.SealError as exc:
            error = f"login_seal_invalid: {exc}"
            log.warning("login start for %s could not mint its seal key: %s", slug, error)
            return _fail("creating_dir", error, started_at)
        _create_login_runtime_file(_login_seal_key_path(slug), 0o600, seal_pem.decode())

        # 2) awaiting_oauth — spawn the DETACHED login (no window) and read the manual URL from its
        #    stdout log.
        _set_provisioning(config_dir,
                          _prov_block("awaiting_oauth", started_at=started_at, pre_creds_mtime=pre_creds_mtime),
                          slug=slug)
        # The daemon owns the IPC files whichever uid runs the login (it scrapes the log, writes the FIFO).
        # No FIFO on Windows: the code arrives later as a file (`_deliver_login_code`), so nothing is made now.
        if not on_windows:
            os.mkfifo(fifo_path, mode=ipc_mode)
        # Symlink-safe create (`O_EXCL|O_NOFOLLOW`): a planted link would otherwise be truncated as the daemon.
        if on_windows:
            # Pinned on the create's OWN descriptor: with no `O_NOFOLLOW` a reopen by name would follow a link
            # the runner swapped in between, and the tail read through it goes to the control plane.
            log_pin = _ScrapedLog(log_path, _create_login_runtime_file(log_path, ipc_mode, keep_open=True))
        else:
            _create_login_runtime_file(log_path, ipc_mode)
            # Pin the read side now, before the file is shared or written (see `_ScrapedLog`).
            log_pin = _ScrapedLog.pinned(log_path)
        if delegated and not on_windows:
            # mkfifo applies the umask, so share explicitly — fd-based, since a path-based share could be
            # redirected by a runner re-planting the name (see `_share_fifo_with_session_user`).
            _share_fifo_with_session_user(fifo_path, ipc_mode)
            # The log is deliberately NOT shared here: its create already set mode and group on the fd, so
            # a path-based share could only be a no-op or a hijack.
        _spawn_login_process(config_dir, fifo_path, log_path, pid_path, securestorage_dir, *vendor_arg)

        oauth_url = _scrape_oauth_url(log_pin, time.monotonic() + _OAUTH_SCRAPE_TIMEOUT, *vendor_arg)
        if oauth_url is None:
            # No URL in the window. The vendor is asked first: it may know the login can never succeed
            # here (an admin-gated login, D13). Its answer is a closed-set code, never log tail.
            declared_refusal = _contained_refusal(params, _login_log_tail(log_pin))
            if declared_refusal:
                error = f"login_provider_refused: {declared_refusal}"
            else:
                error = _scrape_failure_error(log_pin, *vendor_arg)
            log.warning("login start for %s scraped no authorization URL: %s", slug, error)
            return _fail("awaiting_oauth", error, started_at)

        # 3) awaiting_code — URL surfaced; `awaiting_code` in both modes (a stored wire value).
        _set_provisioning(config_dir,
                          _prov_block("awaiting_code", oauth_url=oauth_url, started_at=started_at,
                                      pre_creds_mtime=pre_creds_mtime),
                          slug=slug)
        # `securestorage_dir` is an OBSERVATION: the store this login mints into (`""` for a node-pinned
        # provider). Present (not omitted) so absence means only "too old to say"; a path, so the CP can check
        # equality against its own derivation.
        # `login_completion` (provider only) reports the mode actually driven, since the two sides'
        # catalogues may disagree across a deploy; absent for Claude (byte-identical).
        reply = {"status": "awaiting_code", "config_dir": config_dir, "securestorage_dir": securestorage_dir}
        # The sealed handoff: the URL readable only by the browser that sent `peer_key`, and this login's
        # public key for the code coming back. `oauth_url` is deliberately NOT in the reply — the control
        # plane must never hold it.
        if len(oauth_url.encode("utf-8")) > _OAUTH_URL_MAX_BYTES:
            # Bounded here, not by the control plane, which never sees the plaintext: an over-long URL
            # would seal to a blob over its wire bound and fail there as a generic start failure.
            error = ("oauth_url_scrape_failed: the authorize URL is over the "
                     f"{_OAUTH_URL_MAX_BYTES}-byte bound")
            log.warning("login start for %s scraped an over-long authorization URL: %s", slug, error)
            return _fail("awaiting_oauth", error, started_at)
        try:
            reply["sealed_url"] = login_seal.seal(peer_key, oauth_url)
            reply["seal_key"] = login_seal.public_key_b64(seal_pem)
        except login_seal.SealError as exc:
            error = f"login_seal_invalid: {exc}"
            log.warning("login start for %s could not seal its authorization URL: %s", slug, error)
            return _fail("awaiting_oauth", error, started_at)
        if declared is not None:
            reply["login_completion"] = (agent_providers.PASTE_CODE if paste_mode
                                         else agent_providers.AWAIT_CREDENTIALS)
        # `user_code` (provider only), scraped after the URL is in hand. Present — even `""` — whenever the
        # vendor declares one, so absence means only "link-only login" (see `_scrape_user_code`). It is the
        # RFC 8628 USER code, never the `device_code` polling secret; it goes into the reply, no log line.
        user_code = _scrape_user_code(log_pin, time.monotonic() + _USER_CODE_SCRAPE_TIMEOUT, *vendor_arg)
        if user_code is not None:
            reply["user_code"] = user_code
        return {"exit_code": 0, "stdout": json.dumps(reply), "stderr": ""}
    except _LoginRuntimeUnsafe as exc:
        # An occupied runtime path: before the `RuntimeError` arm (it subclasses it), under its own code,
        # since it is a hostile act, not a broken host.
        log.error("login start refused an occupied runtime path for %s: %s", slug, exc)
        return _fail("awaiting_oauth", f"login_runtime_unsafe: {exc}", started_at)
    except RuntimeError as exc:
        # `_spawn_login_process`'s operator-diagnosable failures: the message travels to the console.
        log.warning("login start could not spawn the login process for %s: %s", slug, exc)
        return _fail("awaiting_oauth", f"login_spawn_failed: {exc}", started_at)
    except Exception:
        # Anything unexpected lands on a terminal error with the runtime cleaned up (`_fail` is idempotent).
        log.exception("login start failed unexpectedly for %s", slug)
        return _fail("awaiting_oauth", fail_code, started_at)
    finally:
        # The pinned log fd is released here and nowhere else; `close()` never raises.
        if log_pin is not None:
            log_pin.close()
        try:
            host_platform.unlock(lock_fh)
        except Exception:
            pass
        lock_fh.close()


def _account_add(slug: str, run, *, provider: str = agent_providers.DEFAULT_PROVIDER,
                 peer_key: str | None) -> dict:
    """Add-Account flow, phase 1: validate the slug, then run the shared login-start core. Creates the
    config dir (a new account, or an enrolment onto a new node); the CP tracks wizard state
    (apps/api/app/durable/accounts.py).

    `provider` and `peer_key` are relayed, not judged."""
    if not _SLUG_RE.match(slug or ""):
        return {"exit_code": -1, "stdout": "", "stderr": f"invalid slug: {slug!r}"}
    config_dir = str(Path(jaah_home()) / "auth" / slug)
    return _login_start(slug, config_dir, run, provider=provider, peer_key=peer_key)


def _account_reauth(slug: str, run, *, provider: str = agent_providers.DEFAULT_PROVIDER,
                    peer_key: str | None) -> dict:
    """Re-auth flow, phase 1: refresh the OAuth credentials of an EXISTING account (dead/expired token).
    Mirrors `_account_add`'s login machinery verbatim via `_login_start`, but REQUIRES the account to
    already exist and NEVER creates, enables, or re-stamps it:

    - The config dir must exist on disk (not a registry record), else a terminal `account_not_found`,
      persisting nothing. A cheap pre-flock reject; `_login_start` re-asserts it under the flock.
    - Never creates or re-enables an account (enabled is the DB's)."""
    if not _SLUG_RE.match(slug or ""):
        return {"exit_code": -1, "stdout": "", "stderr": f"invalid slug: {slug!r}"}
    config_dir = str(Path(jaah_home()) / "auth" / slug)
    if not Path(config_dir).is_dir():
        return {"exit_code": 1, "stdout": "", "stderr": "account_not_found"}
    return _login_start(slug, config_dir, run, require_existing=True, fail_code="account_reauth_failed",
                        provider=provider, peer_key=peer_key)


def _finalize_account_add(config_dir: str, slug: str, oauth_url: str | None, started_at: str, run) -> dict:
    """Phase-2 tail (steps 4–9) — THE AGENT LAYER (architecture A14): once credentials exist, make the new
    account's config dir usable by a runner, then report `done` for the CP to clear provisioning from.

    One step list per vendor. A provider runs only the adapter's `finalize` (`agents/agent_layer.py`):
    Claude's steps would run the wrong binary against the wrong config dir (the test asserts ZERO
    `_claude_cli` calls on a provider add). HUD/notify are Claude-only. `run` is Claude's alone."""
    def _fail(step: str, error: str) -> dict:
        _set_provisioning(config_dir, _prov_block(step, status="error", error=error, started_at=started_at),
                          slug=slug)
        return {"exit_code": 1, "stdout": "", "stderr": error}

    # The vendor from phase 1's record: this verb carries no `provider`.
    adapter = _declared_login_adapter(_read_login_provider(slug))
    if adapter is not None:
        # 4) agent_layer — the whole provider sequence, best-effort: a failure leaves a degraded agent
        #    layer, never a stranded credential. `finalize` returns None, so failures are visible only in
        #    `agent_layer`'s node log; there is no provider check verb yet.
        _set_provisioning(config_dir,
                          _prov_block("mcp_setup", oauth_url=oauth_url, started_at=started_at), slug=slug)
        try:
            adapter.finalize(config_dir=config_dir)
        except Exception:  # noqa: BLE001 — see above: a degraded agent layer must not strand a credential
            log.exception("provider agent layer failed for %s — the account is registered but degraded",
                          slug)
        return {"exit_code": 0, "stdout": json.dumps({"status": "done", "config_dir": config_dir}),
                "stderr": ""}

    # 4) omc_check — install/repair OMC into the new account's Claude home. A new account's `plugins` is
    #    first linked at the node tree's shared root, so this installs into the one copy (no-op when OMC is
    #    already there) rather than downloading another (`plugin_repair.link_new_account`).
    plugin_repair.link_new_account(Path(config_dir).expanduser(), jaah_home())
    _set_provisioning(config_dir,
                      _prov_block("omc_check", oauth_url=oauth_url, started_at=started_at), slug=slug)
    omc = _omc_check(config_dir, install=True, run=run)
    if not omc["present"]:
        return _fail("omc_check", "omc_install_failed")

    # 5) hud_setup — best-effort: copy the OMC HUD wrapper and point settings.json's statusLine at it,
    #    mirroring the manual setup on acct1/acct2. Never fails the add.
    _set_provisioning(config_dir,
                      _prov_block("hud_setup", oauth_url=oauth_url, started_at=started_at), slug=slug)
    _hud_setup(config_dir)

    # 6) notify_setup — best-effort: write the canonical ask-user-question-only event policy into this
    #    account's .omc-config.json. Never fails the add.
    _set_provisioning(config_dir,
                      _prov_block("notify_setup", oauth_url=oauth_url, started_at=started_at), slug=slug)
    _notification_setup(config_dir)

    # 7) mcp_setup — best-effort: install + enable the official-marketplace MCP plugins and mirror them
    #    into settings.json's enabledPlugins. Never fails the add.
    _set_provisioning(config_dir,
                      _prov_block("mcp_setup", oauth_url=oauth_url, started_at=started_at), slug=slug)
    _mcp_plugins_setup(config_dir, run)

    # 8) settings_converge — the managed `settings.json` block and managed-settings consent
    #    (`plugin_repair`). Best-effort. Without it a runner has no model/hooks/guards, and an unconsented
    #    account blocks at the approval prompt on every launch, relaunched forever. After
    #    `_mcp_plugins_setup` (which owns `enabledPlugins`). The consent hash comes from peer accounts: a
    #    new account has never consented.
    _set_provisioning(config_dir,
                      _prov_block("settings_converge", oauth_url=oauth_url, started_at=started_at),
                      slug=slug)
    try:
        cfg = Path(config_dir).expanduser()
        plugin_repair.converge_settings(cfg)
        plugin_repair.converge_remote_settings_consent(
            cfg,
            settings_hash=plugin_repair.consent_hash(
                plugin_repair.discover_config_dirs(jaah_home())),
        )
    except Exception as exc:  # noqa: BLE001 — same contract as every other best-effort step above
        log.warning("account_add: settings converge for %s failed (%s: %s) — the account is usable but "
                    "may meet the managed-settings approval prompt on launch",
                    slug, type(exc).__name__, exc)

    # 9) registering / 10) done — the CP clears provisioning to `done` from this reply.
    return {"exit_code": 0, "stdout": json.dumps({"status": "done", "config_dir": config_dir}),
            "stderr": ""}


def _drop_credential_link(config_dir: str, provider: str) -> None:
    """Remove this account's credential LINK from its per-node home, if one is there — never a regular file.

    Before a `shared_token_link` login: the adapters refuse to read a link (their anti-forgery rule), so a
    login polling through one would never see itself complete. The store's copy is untouched."""
    row = agent_providers.by_value(provider or agent_providers.DEFAULT_PROVIDER)
    if row is None or not row.shared_token_link:
        return
    path = os.path.join(config_dir, row.credential_file)
    try:
        if stat.S_ISLNK(os.lstat(path).st_mode):
            # Windows: the runner account's to remove — the daemon deletes nothing in its home.
            if host_platform.is_windows():
                err = session_user.unlink_as_session_user(path, timeout=_DELEGATED_TIMEOUT)
                if err:
                    log.warning("could not drop the credential link %s: %s", path, err)
            else:
                os.unlink(path)
    except OSError:
        pass


def _drop_stale_credential(config_dir: str, provider: str) -> str:
    """After a login minted into the token store: remove a REGULAR credential file left in the config dir, so
    a later read cannot fall back to it (`shared_storage.token_dir` asks the store first, then the config dir:
    a store that loses its token, or a mount that goes away, would expose it). A symlink is left alone — it is
    never followed, so nothing in the store is touched. `""` when nothing stale is left, else the reason."""
    row = agent_providers.by_value(provider or agent_providers.DEFAULT_PROVIDER)
    if row is None or not row.credential_file:
        return ""
    path = os.path.join(config_dir, row.credential_file)
    try:
        if not stat.S_ISREG(os.lstat(path).st_mode):
            return ""
        if host_platform.is_windows():
            # The runner account's to remove, as `_drop_credential_link`; its error is a reason, never raised.
            err = session_user.unlink_as_session_user(path, timeout=_DELEGATED_TIMEOUT)
            if err:
                return f"the old credential {path} could not be removed ({err})"
        else:
            os.unlink(path)
    except FileNotFoundError:
        return ""
    except OSError as exc:
        return f"the old credential {path} could not be removed ({type(exc).__name__})"
    log.info("login: removed the stale credential %s — the new one is in the token store", path)
    return ""


def _publish_shared_login(slug: str, config_dir: str, securestorage_dir: str, provider: str) -> str:
    """After a login minted its credential (`securestorage_dir` = the token store phase 1 named; `""` for a
    node-pinned login, which needs nothing): a `shared_token_link` login landed it in the per-node home, so
    copy ONLY that file into the store (`shared_storage.publish_credential`, as the uid that owns it), then
    replace the local file with the link every launch reads through
    (`fleet_dispatch.ensure_shared_credential_link`); any other login wrote straight into the store, so a
    stale regular credential left in the config dir is removed (`_drop_stale_credential`). `""` on success,
    else the `login_publish_failed` reason — the login is then not reported done."""
    if not securestorage_dir:
        return ""
    row = agent_providers.by_value(provider or agent_providers.DEFAULT_PROVIDER)
    if row is None or not row.shared_token_link:
        reason = _drop_stale_credential(config_dir, provider)
        if reason:
            log.warning("login for %s: %s", slug, reason)
            return f"login_publish_failed: {reason}"
        return ""
    import fleet_dispatch  # noqa: PLC0415 — deferred, as `account_converge` imports it

    src = os.path.join(config_dir, row.credential_file)
    # Windows: the daemon calls the publish, which hands the copy to the runner account itself.
    if _login_runs_delegated() and not host_platform.is_windows():
        # The session user owns the fresh 0600 file; it publishes it. The env is spelled out (never the
        # daemon's, which carries `NODE_TOKEN`): the home, so the store root resolves as it does here, and the
        # legacy-root override, if this node has one.
        root = env_get(environment.SHARED_ACCOUNTS_ROOT_ENV)
        root_env = {environment.SHARED_ACCOUNTS_ROOT_ENV: root} if root is not None else {}
        home = environment.pool_root_raw()
        if home:
            root_env[environment.POOL_ROOT_ENV] = home
        try:
            proc = session_user.run_as_session_user(
                [sys.executable, "-s", "-E", str(Path(shared_storage.__file__).resolve()), "--publish-credential",
                 src, securestorage_dir, row.credential_file],
                env=root_env, timeout=_DELEGATED_TIMEOUT)
            reason = "" if proc.returncode == 0 else ((proc.stderr or "").strip()[-300:] or "the publish failed")
        except Exception as exc:  # noqa: BLE001 — RuntimeError (switch gone), TimeoutExpired, OSError
            reason = f"the session-user publish could not run ({type(exc).__name__})"
    else:
        reason = shared_storage.publish_credential(src, securestorage_dir, row.credential_file)
    if reason:
        log.warning("login for %s: the credential could not be published to the token store — %s", slug,
                    reason)
        return f"login_publish_failed: {reason}"
    # `layer=False`: the login's own `finalize` lays the agent layer next (the preflight already ran).
    refusal = fleet_dispatch.ensure_shared_credential_link(config_dir, securestorage_dir, provider, layer=False)
    if refusal:
        # The credential IS in the store now; this node's next launch re-links it.
        log.warning("login for %s: published, but this node's home could not be linked — %s", slug, refusal)
    return ""


def _login_unreadable_refusal(config_dir: str, provider: str) -> str:
    """`""` when this node can use the credential a login just wrote, else the `credential_unreadable` reason.

    The login's own check reads the file as the account that wrote it; this asks the question dispatch asks
    (`fleet_dispatch.account_dir_usable`, the reader the quota probe resolves through too), so a login never
    reports done for a credential this node cannot read — a Windows NodeLink only the runner account sees was
    one. A provider with no runner adapter here is never dispatched here: nothing to agree with."""
    import agents  # noqa: PLC0415 — the runner-adapter registry; deferred, as `fleet_dispatch`
    import fleet_dispatch  # noqa: PLC0415 — deferred, as `account_converge` imports it

    try:
        agents.get_adapter(provider or agent_providers.DEFAULT_PROVIDER)
    except agents.UnknownProvider:
        return ""
    if fleet_dispatch.account_dir_usable(config_dir, provider=provider):
        return ""
    where = shared_storage.token_dir(config_dir)
    log.warning("login for %s finished, but this node cannot read the credential in %s", config_dir, where)
    return f"credential_unreadable: looked in {where}"


def _account_add_lock_path(slug: str) -> Path:
    """The per-slug login flock (`_login_start`'s, taken again by every phase-2 half and by a delete), under
    the fleet locks dir, created on demand. Shared by add AND re-auth, name kept so a deploy cannot orphan an
    in-flight flow: never two concurrent logins for one slug."""
    locks_dir = env_helpers.fleet_locks_dir()
    locks_dir.mkdir(parents=True, exist_ok=True)
    return locks_dir / f"account-add-{slug}.lock"


class _LoginRefused(Exception):
    """`_login_readback`'s refusal, carrying the verb's answer."""

    def __init__(self, result: dict) -> None:
        super().__init__(result["stderr"])
        self.result = result


def _login_readback(slug: str, half: str) -> tuple[str, str, str, str, tuple]:
    """Phase 2's re-asserted refusals, for both halves (`_login_submit_code`, `_login_await_redirect`): the
    halves are minutes apart and the node (or the daemon) may change between. Returns `(config_dir,
    securestorage_dir, stored_provider, declared, vendor_arg)`; raises `_LoginRefused` with the verb's answer.

    The store is phase 1's, read back (`_login_store_path`) and re-checked; the vendor is read back beside it
    (`_login_provider_path`). The provider refusal FIRST (`_login_vendor` raises for a provider with no row): a
    rollback between halves must answer this refusal, not a raise. Inert for Claude."""
    config_dir = str(Path(jaah_home()) / "auth" / slug)
    securestorage_dir = _read_login_store(slug)
    stored_provider = _read_login_provider(slug)
    declared, vendor_arg = "", ()
    refusal = _login_provider_refusal(stored_provider) or _windows_sign_in_refusal(stored_provider)
    if not refusal:
        declared, _, vendor_arg = _login_vendor(stored_provider)
        refusal = (_login_uid_refusal(config_dir, *vendor_arg)
                   or _login_store_refusal(slug, securestorage_dir))
    if refusal:
        log.warning("login %s refused for %s: %s", half, slug, refusal)
        raise _LoginRefused({"exit_code": 1, "stdout": "", "stderr": refusal})
    return config_dir, securestorage_dir, stored_provider, declared, vendor_arg


def _login_submit_code(slug: str, sealed_code: str | None, finalize, *,
                       fail_code: str = "account_add_failed") -> dict:
    """Shared phase-2 login core for BOTH `account_add` and `account_reauth`: feed the operator's pasted
    authorization code to the detached login process started in phase 1, wait for credentials to land,
    then hand off to `finalize(config_dir, oauth_url, started_at)` for the flow-specific success action
    (add: install OMC + register enabled; re-auth: just clear the provisioning block).

    The code arrives SEALED (`sealed_code`, to this login's `seal_key`) and is unsealed here,
    under the flock and after the pid check, with the key phase 1 stored (`_read_login_seal_key`); a
    missing key or an unsealable blob is terminal under `login_seal_invalid`. No plaintext code is
    accepted. The unsealed code must pass `_OAUTH_CODE_RE` before it touches the FIFO.

    A separate process from phase 1: re-derives the runtime paths from the slug. Every failure cleans up
    and lands a terminal error. `fail_code` names the flow's unexpected-failure code."""
    import time

    if not _SLUG_RE.match(slug or ""):
        return {"exit_code": -1, "stdout": "", "stderr": f"invalid slug: {slug!r}"}
    if not login_seal.is_wire_sealed(sealed_code):
        # Shape only, pre-lock: the login stays alive for a re-paste.
        return {"exit_code": -1, "stdout": "", "stderr": "login_seal_invalid: malformed sealed code"}

    try:
        config_dir, securestorage_dir, stored_provider, declared, vendor_arg = _login_readback(slug, "submit_code")
    except _LoginRefused as refused:
        return refused.result

    _, fifo_path, log_path, pid_path = _account_add_runtime(slug)

    def _fail(step: str, error: str, started_at: str) -> dict:
        _cleanup_account_add_runtime(slug)
        _set_provisioning(config_dir, _prov_block(step, status="error", error=error, started_at=started_at),
                          slug=slug)
        return {"exit_code": 1, "stdout": "", "stderr": error}

    # `_login_start`'s lock, so a phase-2 submit never races a phase-1 start.
    lock_fh = open(_account_add_lock_path(slug), "a+")
    try:
        try:
            host_platform.lock(lock_fh, blocking=False)
        except OSError:
            lock_fh.close()
            return {"exit_code": 1, "stdout": json.dumps({"status": "in_progress_elsewhere"}),
                    "stderr": "another login is in progress for this slug"}

        started_at = _now_iso()
        oauth_url = None
        # Phase 1's pre-attempt snapshot (a re-auth's dir already holds the dead file).
        pre_mtime = _read_pre_creds_mtime(slug)

        # The login must still be alive: its PKCE verifier lives only in its memory.
        try:
            pid = int(_read_runner_text(pid_path).strip())
        except (FileNotFoundError, ValueError):
            return _fail("awaiting_code", "login_process_gone", started_at)
        if not _pid_alive(pid):
            return _fail("awaiting_code", "login_process_gone", started_at)

        # Under the flock and only for a live login: the key is this attempt's, and a stale one is a
        # `login_process_gone` above, never a misleading seal error. Neither the PEM nor the plaintext is
        # logged.
        seal_pem = _read_login_seal_key(slug)
        if seal_pem is None:
            return _fail("awaiting_code", "login_seal_invalid: this login has no seal key on this node "
                         "(its runtime dir is gone)", started_at)
        try:
            code = login_seal.unseal(seal_pem, sealed_code)
        except login_seal.SealError as exc:
            return _fail("awaiting_code", f"login_seal_invalid: {exc}", started_at)
        if not _OAUTH_CODE_RE.match(code):
            # Non-terminal: the login stays alive for a re-paste.
            return {"exit_code": -1, "stdout": "", "stderr": "invalid or missing authorization code"}

        _set_provisioning(config_dir,
                          _prov_block("polling_login", oauth_url=oauth_url, started_at=started_at,
                                      pre_creds_mtime=pre_mtime),
                          slug=slug)

        if not _deliver_login_code(fifo_path, code, pid):
            return _fail("awaiting_code", "login_process_gone", started_at)

        # Poll for a fresh credential, bounded under the command timeout.
        poll_deadline = time.monotonic() + _LOGIN_POLL_TIMEOUT
        while True:
            # Freshness is the vendor's (`_login_creds_fresh`).
            if _login_creds_fresh(declared, config_dir, securestorage_dir, pre_mtime):
                break
            # A bad code makes the CLI print `Login failed: …` and re-prompt WITHOUT exiting (its FIFO
            # stdin never EOFs), so detect rejection from the log — not just from a process exit.
            if _login_failed_in_log(log_path, *vendor_arg):
                return _fail("polling_login", "login_rejected", started_at)
            if not _pid_alive(pid) and not _login_creds_fresh(
                    declared, config_dir, securestorage_dir, pre_mtime):
                # Exited without writing a fresh credential ⇒ rejected. The second `_login_creds_fresh` is a
                # race check, not a repeat: a CLI may write and exit between the two — do not hoist it.
                return _fail("polling_login", _login_exited_error(declared, config_dir, securestorage_dir),
                             started_at)
            if time.monotonic() > poll_deadline:
                return _fail("polling_login", "login_timeout", started_at)
            time.sleep(_LOGIN_POLL_INTERVAL)

        # Reap the login BEFORE finalize: a CLI that stays alive could flush its settings over what
        # finalize just registered. Only the process: the runtime dir holds the provider handover
        # finalize reads, so it is removed after.
        _kill_login_proc(pid_path)
        published = (_publish_shared_login(slug, config_dir, securestorage_dir, stored_provider)
                     or _login_unreadable_refusal(config_dir, stored_provider))
        if published:
            return _fail("polling_login", published, started_at)
        result = finalize(config_dir, oauth_url, started_at)
        # Remove the runtime dir (the repeated kill is idempotent).
        _cleanup_account_add_runtime(slug)
        return result
    except Exception:
        # Same terminal-error guarantee as phase 1.
        log.exception("login submit_code failed unexpectedly for %s", slug)
        return _fail("polling_login", fail_code, started_at)
    finally:
        try:
            host_platform.unlock(lock_fh)
        except Exception:
            pass
        lock_fh.close()


# Runs AS THE RUNNER ACCOUNT: the code arrives on stdin (never argv, env or a log), lands beside the code file
# and is renamed into place, so the child's feeder never reads it half-written.
_DELIVER_CODE_SH = 'umask 077; t="$1.$$.tmp"; cat >"$t" && mv -f -- "$t" "$1"\n'


def _deliver_login_code(stdin_path: Path, code: str, pid: int) -> bool:
    """Hand CODE to the detached login's stdin; False when the login can no longer take it.

    Off Windows, the FIFO — `O_NONBLOCK`, so a racing death (no reader) cannot wedge the open. On Windows a
    regular file the child's feeder waits for (`_WINDOWS_LOGIN_CHILD_SH`), written by the runner account
    (`_DELIVER_CODE_SH`): `run\\` is runner-writable, so this daemon opens nothing there. A file has no "no
    reader" refusal, so PID's liveness is re-checked first, the FIFO's verdict by other means."""
    if host_platform.is_windows():
        if not _pid_alive(pid):
            return False
        try:
            proc = session_user.run_as_session_user(
                ["/bin/sh", "-c", _DELIVER_CODE_SH, "jaahd-login-code", session_user._shell_path(str(stdin_path))],
                input=code + "\n", timeout=_DELEGATED_TIMEOUT)
        except Exception as exc:  # noqa: BLE001 — RuntimeError (switch gone), TimeoutExpired, OSError
            log.warning("could not hand the code to the login at %s as %r (%s: %s)",
                        stdin_path, session_user.session_user(), type(exc).__name__, exc)
            return False
        if proc.returncode != 0:
            log.warning("could not hand the code to the login at %s: rc %s %s", stdin_path, proc.returncode,
                        (proc.stderr or "").strip()[-300:])
            return False
        return True
    try:
        code_fd = os.open(stdin_path, os.O_WRONLY | _O_NONBLOCK)
    except OSError:
        return False
    try:
        os.write(code_fd, (code + "\n").encode())
    finally:
        os.close(code_fd)
    return True


def _add_finalizer(slug: str, run):
    """Phase-2 finalizer of the Add-Account flow: install OMC/HUD/notify/mcp, register the account, clear
    provisioning (`_finalize_account_add`)."""
    def _finalize(config_dir: str, oauth_url: str | None, started_at: str) -> dict:
        return _finalize_account_add(config_dir, slug, oauth_url, started_at, run)
    return _finalize


def _reauth_finalizer(slug: str):
    """Phase-2 finalizer of the Re-auth flow: NO `_finalize_account_add` (provisioned at add time), just
    `done`, plus the provider agent layer repair (`_reassert_provider_agent_layer`) for what the preflight
    strips."""
    def _finalize(config_dir: str, oauth_url: str | None, started_at: str) -> dict:
        _reassert_provider_agent_layer(slug, config_dir)
        return {"exit_code": 0, "stdout": json.dumps({"status": "done", "config_dir": config_dir}),
                "stderr": ""}
    return _finalize


def _account_add_submit_code(slug: str, sealed_code: str | None, run) -> dict:
    """Add-Account flow, phase 2: deliver the pasted code (sealed to this login's `seal_key`), then
    finalize (`_add_finalizer`)."""
    return _login_submit_code(slug, sealed_code, _add_finalizer(slug, run))


def _reassert_provider_agent_layer(slug: str, config_dir: str) -> None:
    """Re-run the provider agent layer after a RE-AUTH, because the preflight just dismantled part of it.

    The login preflight sanitizes the provider's settings file (correctly: it is untrusted before the
    credential exists), dropping our control-MCP registration; without re-asserting it a refreshed account
    comes back mute. `finalize` is idempotent and non-raising. A no-op for the default vendor
    (`login_adapter_for` answers None). Residual: the handover is re-read, so a peer runner unlinking it
    makes this a silent no-op (never the wrong vendor); passing the caller's value is the follow-up."""
    # Inside the `try`: an escape would report a successful re-auth as `account_reauth_failed`.
    try:
        adapter = _declared_login_adapter(_read_login_provider(slug))
        if adapter is None:
            return
        adapter.finalize(config_dir=config_dir)
    except Exception:  # noqa: BLE001 — a degraded agent layer must never strand a refreshed credential
        log.exception("provider agent layer failed on re-auth for %s", slug)


def _account_reauth_submit_code(slug: str, sealed_code: str | None, run) -> dict:
    """Re-auth flow, phase 2: deliver the pasted code (sealed to this login's `seal_key`) to refresh the
    existing account's credentials, then `_reauth_finalizer`. `run` is unused but kept for a uniform
    signature."""
    return _login_submit_code(slug, sealed_code, _reauth_finalizer(slug), fail_code="account_reauth_failed")


def _read_login_pid(pid_path: Path) -> int | None:
    """The pid recorded at PID_PATH, or None when missing/empty/malformed (read twice by the await path)."""
    try:
        return int(_read_runner_text(pid_path).strip())
    except (OSError, ValueError):
        return None


def _login_await_redirect(slug: str, finalize, *, fail_code: str = "account_add_failed") -> dict:
    """Shared phase-2 login core for AWAIT_CREDENTIALS mode (a device-authorization provider,
    `login_completion == await_credentials`) — `_login_submit_code`'s sibling for a login with no code to
    deliver: the operator finishes in their browser and the CLI, polling the vendor, writes the credentials
    itself, so the only thing left to do is WAIT for them to land on disk and then run the flow's
    `finalize`.

    Every check `_login_submit_code` makes, minus the FIFO write. Re-callable: a closing window returns a
    NON-TERMINAL `awaiting_redirect` (exit 0), cleaning up nothing, and the caller calls again.

    Polls LOCK-FREE and takes the flock only to finish: holding it across a re-entered window would keep
    it taken ~98% of the time and block `account_delete` and retries until the 900s cap. The price: the
    pid is captured up front and RE-VERIFIED under the flock; a different pid is `in_progress_elsewhere`,
    never adopted."""
    import time

    if not _SLUG_RE.match(slug or ""):
        return {"exit_code": -1, "stdout": "", "stderr": f"invalid slug: {slug!r}"}

    try:
        config_dir, securestorage_dir, stored_provider, declared, vendor_arg = _login_readback(slug, "await_redirect")
    except _LoginRefused as refused:
        return refused.result

    _, _, log_path, pid_path = _account_add_runtime(slug)
    lock_path = _account_add_lock_path(slug)

    started_at = _now_iso()
    # Phase 1's pre-attempt snapshot (a re-auth's dir already holds the dead file).
    pre_mtime = _read_pre_creds_mtime(slug)

    def _fresh_creds() -> bool:
        """Has this login's exchange landed a usable credential (`_login_creds_fresh`)?"""
        return _login_creds_fresh(declared, config_dir, securestorage_dir, pre_mtime)

    def _fail(step: str, error: str) -> dict:
        """Terminal arm: reap the login and persist the error. Cleans up only under the flock and only for
        this wait's own pid (a phase-1 restart may own the dir now); on contention, touches nothing."""
        lock_fh = open(lock_path, "a+")
        try:
            try:
                host_platform.lock(lock_fh, blocking=False)
            except OSError:
                log.warning("await_redirect for %s reporting %s without cleanup — another login flow "
                            "holds the slug lock", slug, error)
            else:
                try:
                    if _read_login_pid(pid_path) in (None, pid):
                        _cleanup_account_add_runtime(slug)
                finally:
                    host_platform.unlock(lock_fh)
        finally:
            lock_fh.close()
        _set_provisioning(config_dir, _prov_block(step, status="error", error=error, started_at=started_at),
                          slug=slug)
        return {"exit_code": 1, "stdout": "", "stderr": error}

    # ── observe, lock-free ──────────────────────────────────────────────────────────────────────────────
    # The login holds the device-auth poll. Read ONCE: this pid is the wait's identity.
    pid = _read_login_pid(pid_path)
    # Gone, unless already done: a one-shot CLI writes and exits between two polls, and failing then would
    # throw away a good credential. The baseline file must EXIST (phase 1 writes it before the pid file):
    # without it `_login_creds_fresh` degrades to presence-only and a leftover credential would re-enable
    # a disabled account through this `system:create` verb. Kept inside the condition, not hoisted: the
    # short-circuit keeps `_fresh_creds()` (outside the `try`) off a healthy login's path
    # (`test_await_login_unexpected_failure_surfaces_account_add_failed`).
    if (pid is None or not _pid_alive(pid)) and not (
        _pre_creds_mtime_path(slug).exists() and _fresh_creds()
    ):
        return _fail("awaiting_code", "login_process_gone")

    _set_provisioning(config_dir,
                      _prov_block("polling_login", oauth_url=None, started_at=started_at,
                                  pre_creds_mtime=pre_mtime),
                      slug=slug)

    try:
        poll_deadline = time.monotonic() + _LOGIN_AWAIT_POLL_TIMEOUT
        while not _fresh_creds():
            # A refused authorization is read from the log: the CLI stays alive (FIFO stdin never EOFs).
            if _login_failed_in_log(log_path, *vendor_arg):
                return _fail("polling_login", "login_rejected")
            # `pid is not None` first: the gate can admit a None pid, and `_pid_alive(None)` would raise.
            if pid is not None and not _pid_alive(pid) and not _fresh_creds():
                # Gone without a fresh credential: a timeout, not `login_rejected` — no code is ever
                # submitted on this path, and `timeout 900` starts at spawn.
                return _fail("polling_login", "login_timeout")
            if time.monotonic() > poll_deadline:
                # NON-TERMINAL: no cleanup, no SIGTERM — the login is still polling the vendor for its grant.
                return {"exit_code": 0, "stdout": json.dumps({"status": "awaiting_redirect"}), "stderr": ""}
            time.sleep(_LOGIN_POLL_INTERVAL)
    except Exception:
        log.exception("login await_redirect failed unexpectedly while polling for %s", slug)
        return _fail("polling_login", fail_code)

    # ── finish, under the flock ─────────────────────────────────────────────────────────────────────────
    lock_fh = open(lock_path, "a+")
    try:
        try:
            host_platform.lock(lock_fh, blocking=False)
        except OSError:
            lock_fh.close()
            return {"exit_code": 1, "stdout": json.dumps({"status": "in_progress_elsewhere"}),
                    "stderr": "another login is in progress for this slug"}

        # Re-verify the pid: a phase-1 restart may own this slug now; never adopt or reap it.
        current = _read_login_pid(pid_path)
        if current is not None and current != pid:
            log.warning("await_redirect for %s found login pid %s where it was waiting on %s — another "
                        "phase 1 restarted this slug", slug, current, pid)
            return {"exit_code": 1, "stdout": json.dumps({"status": "in_progress_elsewhere"}),
                    "stderr": "another login is in progress for this slug"}

        # Reaped before the finalize, for `_login_submit_code`'s reason.
        _kill_login_proc(pid_path)
        published = (_publish_shared_login(slug, config_dir, securestorage_dir, stored_provider)
                     or _login_unreadable_refusal(config_dir, stored_provider))
        if published:
            return _fail("polling_login", published)
        result = finalize(config_dir, None, started_at)
        # Remove the runtime dir (the repeated kill is idempotent).
        _cleanup_account_add_runtime(slug)
        return result
    except Exception:
        # Same terminal-error guarantee as both sibling phases.
        log.exception("login await_redirect failed unexpectedly for %s", slug)
        _cleanup_account_add_runtime(slug)
        _set_provisioning(config_dir,
                          _prov_block("polling_login", status="error", error=fail_code,
                                      started_at=started_at), slug=slug)
        return {"exit_code": 1, "stdout": "", "stderr": fail_code}
    finally:
        try:
            host_platform.unlock(lock_fh)
        except Exception:
            pass
        lock_fh.close()


def _account_add_await_login(slug: str, run) -> dict:
    """Add-Account flow, phase 2 in AWAIT_CREDENTIALS mode: wait for the device-auth login's credentials
    to land, then finalize exactly as `_account_add_submit_code` does (`_add_finalizer`)."""
    return _login_await_redirect(slug, _add_finalizer(slug, run))


def _account_reauth_await_login(slug: str, run) -> dict:
    """Re-auth flow, phase 2 in AWAIT_CREDENTIALS mode: wait for the device-auth login to refresh the
    existing account's credentials, then `_reauth_finalizer` (mirrors `_account_reauth_submit_code`). `run`
    is unused (no finalize side-effects) but kept for a uniform handler signature."""
    return _login_await_redirect(slug, _reauth_finalizer(slug), fail_code="account_reauth_failed")


def _account_delete(raw_config_dir: str) -> dict:
    """R4: permanently remove an account from this node — delete <jaah_home>/auth/<slug>/ from disk
    (credentials included) and, when the token store is local disk (not a mount), its leaf
    `<jaah_home>/auth/.shared/<slug>` too: a token only this node held goes with the account, while a
    mounted store's leaf is every node's and stays (`account_shared_delete` removes that one). Idempotent
    (a missing dir is success, not an error — a double-click or stale UI must never surface a scary
    failure).

    The incoming `config_dir` is the CP's path, rebased onto this host's auth root first
    (`rebased_account_dir`), since `JAAH_HOME` may differ. Then resolved and
    containment-checked BEFORE any mutation; a `..` tail is still refused. Refuses while a login holds the
    per-slug flock."""
    config_dir = _norm_dir(raw_config_dir)
    if not config_dir:
        return {"exit_code": -1, "stdout": "", "stderr": "account_delete needs config_dir"}

    # Function-local: `fleet_dispatch` pulls heavy imports at module scope.
    from fleet_dispatch import rebased_account_dir

    auth_root = Path(os.path.realpath(Path(jaah_home()) / "auth"))
    resolved = Path(os.path.realpath(rebased_account_dir(config_dir) or config_dir))
    # Auth root itself, or a DIRECT child only (spec R4) — not an arbitrary descendant, so a stale/buggy
    # caller passing a nested path errors instead of recursively deleting a subtree.
    if resolved != auth_root and resolved.parent != auth_root:
        return {"exit_code": -1, "stdout": "",
                "stderr": "account_delete: config_dir must be <jaah_home>/auth/ or a direct child of it"}
    # The token store is `auth/.shared` (`shared_storage.SHARED_ACCOUNTS_SEGMENT`): no slug starts with `.`, and a
    # sweep must never cross into a mount — it would erase every node's credentials in a mounted store.
    def is_mount(path: Path) -> bool:
        # On Windows a NodeLink is a link, never `ismount`: `mounts_under` names ours.
        return bool(shared_storage.mounts_under(str(path))) if host_platform.is_windows() else os.path.ismount(path)

    if resolved != auth_root and (resolved.name.startswith(".") or is_mount(resolved)):
        return {"exit_code": -1, "stdout": "",
                "stderr": "account_delete: config_dir names no account (a dot-dir or a mount point)"}
    if resolved == auth_root:
        try:
            mounted = [child.name for child in auth_root.iterdir() if is_mount(child)]
        except OSError:
            mounted = []
        if mounted:
            return {"exit_code": -1, "stdout": "",
                    "stderr": f"account_delete: refusing to sweep {auth_root} while a share is mounted under it "
                              f"({', '.join(sorted(mounted))}); delete the accounts one by one"}

    slug = resolved.name
    lock_fh = open(_account_add_lock_path(slug), "a+")
    try:
        host_platform.lock(lock_fh, blocking=False)
    except OSError:
        lock_fh.close()
        return {"exit_code": 1, "stdout": "",
                "stderr": "account_add is in progress for this slug — cannot delete"}

    try:
        # An add at `awaiting_code` holds no flock but leaves a detached login; kill it and its runtime.
        _cleanup_account_add_runtime(slug)
        # The CP decided: every process still using the account goes first, or its open files hold the tree.
        _kill_account_users([str(resolved)] if resolved == auth_root else _account_paths(slug, str(resolved)))
        failure = _remove_account_tree(resolved) if resolved.exists() else None
        if failure is None and resolved != auth_root:
            failure = _remove_local_store_leaf(slug)
    finally:
        host_platform.unlock(lock_fh)
        lock_fh.close()

    if failure:
        return {"exit_code": 1, "stdout": "", "stderr": failure}
    # The forget: the account's place in the community edition's ledger is free again (a no-op on a cloud node).
    for forgotten in (ce_caps.admitted_accounts() if resolved == auth_root else [slug]):
        ce_caps.forget_account(api_key_account=forgotten)
    return {"exit_code": 0, "stdout": json.dumps({"config_dir": config_dir, "deleted": True}), "stderr": ""}


def _remove_local_store_leaf(slug: str) -> str | None:
    """Remove `<jaah_home>/auth/.shared/<slug>` when `.shared` is local disk — never on a mount (`ismount`, or
    mountinfo listing one there), whose leaf is every attached node's. Through a descriptor of the root,
    opened no-follow, so a `.shared` or leaf swapped for a link after the checks is never followed; the leaf
    must sit on the root's own device. None when nothing is left to remove, else why part of it survived."""
    root = shared_storage.accounts_nodelink_root()
    if not root or shared_storage.account_token_root() != root or shared_storage.share_mounted():
        return None
    if host_platform.is_windows():
        return _remove_local_store_leaf_windows(root, slug)
    try:
        root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError:
        return None     # absent, or a link: nothing of this node's to remove
    try:
        try:
            info = os.lstat(slug, dir_fd=root_fd)
        except OSError:
            return None
        if not stat.S_ISDIR(info.st_mode) or info.st_dev != os.fstat(root_fd).st_dev:
            return None
        leaf = os.path.join(root, slug)
        crossing = _mount_crossing(leaf)
        if crossing:
            return crossing
        return _force_remove_leaf(root_fd, slug, leaf)
    finally:
        os.close(root_fd)


def _remove_local_store_leaf_windows(root: str, slug: str) -> str | None:
    """`_remove_local_store_leaf` on Windows, where there is no descriptor walk (no `O_DIRECTORY`/`O_NOFOLLOW`)
    and the daemon deletes nothing under the runner's home itself: the leaf must be a real directory
    on the root's own volume (`store_is_on_share`, `lstat` by path), and the runner account removes it."""
    leaf = ntpath.join(root, slug)
    if not os.path.lexists(leaf) or not shared_storage.store_is_on_share(leaf):
        return None
    crossing = _mount_crossing(leaf)
    if crossing:
        return crossing
    err = session_user.remove_tree_as_session_user(leaf, timeout=60)
    if err:
        return f"could not remove {leaf} ({err})"
    return f"{leaf} still exists" if os.path.lexists(leaf) else None


def _mount_crossing(path: str) -> str | None:
    """The refusal for removing PATH while a mount lies at or under it (a recursive removal would erase the
    mounted filesystem's files — another node's, on a share), else None. Logged; the path is left alone."""
    mounted = shared_storage.mounts_under(path)
    if not mounted:
        return None
    log.warning("refusing to remove %s: a filesystem is mounted under it (%s)", path, ", ".join(mounted))
    return f"refusing to remove {path}: a filesystem is mounted under it ({', '.join(mounted)})"


def _remove_account_tree(resolved: Path) -> str | None:
    """Delete one account dir, or return why part of it survived.

    The dir is 2770 and shared, but what a vendor CLI writes inside it is owned by the SESSION USER at its
    own umask — a provider's translated `agents/` (`agents/subagents.py`) lands `0755`, so this
    uid cannot unlink a thing inside them and a plain rmtree dies on EACCES. On a uid-split node the
    remainder is therefore handed to the session user, who owns those subtrees, and whatever is left after
    that (the daemon's own files) is swept here once more. Delegating grants nothing: the session user can
    delete only what it could already delete. The RESOLVED (checked) path is used throughout, closing the
    symlink-swap TOCTOU; neither rmtree nor `rm -rf` follows a symlink.

    On Windows ONLY the runner account removes it: the daemon deletes nothing under the runner-writable home
    itself, and every file in an account dir there is the runner's anyway."""
    crossing = _mount_crossing(str(resolved))
    if crossing:
        return crossing
    if host_platform.is_windows():
        err = session_user.remove_tree_as_session_user(str(resolved), timeout=60)
        if err:
            return f"could not remove {resolved} ({err})"
        return f"{resolved} still exists" if os.path.lexists(resolved) else None
    first = _sweep_retrying(resolved)
    if first is None:
        return None
    if not session_user.can_run_as_session_user():
        return f"could not remove {resolved} ({first}) and no session user can remove the rest"
    try:
        # `chmod` first: `rm` never widens a read-only dir, even its owner's. Exit status ignored: the
        # top-level entry is not the session user's to unlink, so `rm` reports a failure even when it
        # cleared everything it owns. The sweep below is the verdict.
        session_user.run_as_session_user(
            ["/bin/sh", "-c", 'chmod -R u+w -- "$SU_DIR"; rm -rf -- "$SU_DIR"'],
            env={"SU_DIR": str(resolved)}, timeout=60)
    except Exception as exc:  # noqa: BLE001 — TimeoutExpired, RuntimeError (seam down), OSError
        log.warning("account_delete: delegated removal of %s failed (%s: %s)",
                    resolved, type(exc).__name__, exc)
    second = _sweep_retrying(resolved)
    if second is None:
        return None
    return f"could not remove {resolved} ({first}; after the session user's pass: {second})"


def _sweep(path: Path) -> str | None:
    """rmtree PATH as this uid; None when nothing is left, else the error that stopped it. An entry that
    vanished mid-walk (a process still exiting, the delegated pass) is skipped rather than ending the walk,
    and the verdict is whether the path still exists — never whether an exception was raised."""
    def _gone_ok(_fn, _path, exc):
        if not isinstance(exc, FileNotFoundError):
            raise exc

    try:
        shutil.rmtree(path, onexc=_gone_ok)
    except OSError as exc:
        return f"{type(exc).__name__}: {exc}"
    return f"{path} still exists" if os.path.lexists(path) else None


#: The removal's patience: a killed process takes a moment to exit, and until it does an NFS store keeps its open
#: files as `.nfsXXXX` entries, so the leaf's last `rmdir` answers ENOTEMPTY. Attempts, and the pause between them.
_REMOVE_ATTEMPTS = 10
_REMOVE_PAUSE_SECONDS = 0.5

# Run as the uid that owns the sessions (`_kill_account_users`): SIGKILL every process whose cwd or any environment
# value is one of the paths in `$JAAH_KILL_UNDER` (newline-separated) or under one. Linux only (`/proc`); elsewhere it
# finds nothing. Never a tmux server, nor itself or its parent (the daemon, or the delegation's shell). Prints how many
# it signalled.
_KILL_USERS_PY = r"""
import os, signal
roots = [r.rstrip("/") for r in os.environ.get("JAAH_KILL_UNDER", "").split("\n") if r.strip("/")]
def under(value):
    value = value.rstrip("/")
    return any(value == r or value.startswith(r + "/") for r in roots)
skip = {os.getpid(), os.getppid()}
try:
    pids = [int(d) for d in os.listdir("/proc") if d.isdigit()]
except OSError:
    pids = []
killed = 0
for pid in pids:
    if pid in skip or pid <= 1:
        continue
    try:
        with open(f"/proc/{pid}/comm") as fh:
            if fh.read().startswith("tmux"):
                continue  # a tmux server hosts every session: its panes are judged one by one
    except OSError:
        continue
    hit = False
    try:
        hit = under(os.readlink(f"/proc/{pid}/cwd"))
    except OSError:
        pass
    if not hit:
        try:
            with open(f"/proc/{pid}/environ", "rb") as fh:
                env = fh.read().split(b"\0")
        except OSError:
            continue
        for entry in env:
            name, _, value = entry.partition(b"=")
            if name != b"JAAH_KILL_UNDER" and under(value.decode(errors="replace")):
                hit = True
                break
    if hit:
        try:
            os.kill(pid, signal.SIGKILL)
            killed += 1
        except OSError:
            pass
print(killed)
"""


def _account_paths(slug: str, *known: str) -> list[str]:
    """Every path a session of account SLUG runs against on this node: its per-node home and its token-store leaf."""
    paths = [os.path.join(jaah_home(), "auth", slug), *known]
    root = shared_storage.account_token_root()
    if root:
        paths.append(os.path.join(root, slug))
    return sorted({p for p in paths if p})


def _kill_account_users(paths: list[str]) -> None:
    """SIGKILL every process using one of PATHS — a runner, a console tab, a login, whoever started it: the account
    is being deleted, and a live session would keep its files open. As the session user where the node splits
    uids (it owns the sessions), else as this uid. Best-effort and logged: the removal that follows is the verdict."""
    if host_platform.is_windows() or not paths:
        return
    under = "\n".join(paths)
    try:
        if session_user.switch_needed():
            if not session_user.can_run_as_session_user():
                # This uid may not signal the session user's processes anyway, and must never run a binary found
                # on the session user's PATH (`session_user.delegated_path`).
                log.warning("account delete: no session user to stop the processes using %s", ", ".join(paths))
                return
            proc = session_user.run_as_session_user(
                ["python3", "-I", "-c", _KILL_USERS_PY],
                env={"JAAH_KILL_UNDER": under, "PATH": session_user.delegated_path()}, timeout=30)
        else:
            proc = subprocess.run([sys.executable, "-I", "-c", _KILL_USERS_PY],
                                  env={**os.environ, "JAAH_KILL_UNDER": under},
                                  capture_output=True, text=True, timeout=30)
    except Exception as exc:  # noqa: BLE001 — TimeoutExpired, RuntimeError (seam down), OSError
        log.warning("account delete: could not stop the processes using %s (%s: %s)", paths, type(exc).__name__, exc)
        return
    killed = (proc.stdout or "").strip()
    if killed and killed != "0":
        log.info("account delete: killed %s process(es) using %s", killed, ", ".join(paths))


def _sweep_retrying(path: Path) -> str | None:
    """`_sweep` until nothing is left or `_REMOVE_ATTEMPTS` run out (see there); the last error otherwise."""
    failure = None
    for attempt in range(_REMOVE_ATTEMPTS):
        if attempt:
            time.sleep(_REMOVE_PAUSE_SECONDS)
        failure = _sweep(path)
        if failure is None:
            return None
    return failure


def _force_remove_leaf(root_fd: int, slug: str, leaf: str) -> str | None:
    """Remove the token-store leaf SLUG under the store root ROOT_FD (opened no-follow), whatever is in it: retried
    while killed sessions let go of their files, then handed to the session user for what this uid may not unlink.
    None when it is gone, else why part of it survived."""
    def _gone_ok(_fn, _path, exc):
        if not isinstance(exc, FileNotFoundError):
            raise exc

    def _present() -> bool:
        try:
            os.lstat(slug, dir_fd=root_fd)
            return True
        except FileNotFoundError:
            return False

    failure = ""
    delegated = False
    for attempt in range(_REMOVE_ATTEMPTS):
        if attempt:
            time.sleep(_REMOVE_PAUSE_SECONDS)
        try:
            shutil.rmtree(slug, dir_fd=root_fd, onexc=_gone_ok)
        except OSError as exc:
            failure = f"{type(exc).__name__}: {exc}"
        if not _present():
            return None
        if failure.startswith("PermissionError") and not delegated and session_user.can_run_as_session_user():
            delegated = True
            try:
                session_user.run_as_session_user(
                    ["/bin/sh", "-c", 'chmod -R u+w -- "$SU_DIR"; rm -rf -- "$SU_DIR"'],
                    env={"SU_DIR": leaf}, timeout=60)
            except Exception as exc:  # noqa: BLE001 — TimeoutExpired, RuntimeError (seam down), OSError
                log.warning("account delete: delegated removal of %s failed (%s: %s)", leaf, type(exc).__name__, exc)
    return f"could not remove {leaf} ({failure or 'it is still there'})"


def _account_shared_delete(slug: str) -> dict:
    """Remove an account's token-store leaf `<store root>/<slug>` (offboarding, account delete across nodes),
    mounted or not. Idempotent: an absent leaf is success. The path is resolved node-side from the slug only
    — never taken off the wire — and refused unless the leaf is a real (not symlinked) direct child of a
    real store root; the removal runs through a no-follow descriptor of that root."""
    if not _SLUG_RE.match(slug or ""):
        return {"exit_code": -1, "stdout": "", "stderr": f"account_shared_delete: invalid slug: {slug!r}"}
    store = shared_storage.resolve_shared_auth_dir(slug)
    if not store:
        return {"exit_code": 1, "stdout": "",
                "stderr": "account_shared_delete: this node's token store is not usable right now (no "
                          "JAAH_HOME, or a store mount that is not answering)"}
    root = shared_storage.account_token_root()
    # On Windows a mounted store's root IS a link — our NodeLink's (`share_mounted`), the only one accepted.
    # macOS: `.shared` is root's link to its staging mount; opened as that validated target, never followed blind.
    windows = host_platform.is_windows()
    paths = ntpath if windows else os.path
    opened = shared_storage.resolved_store_root(root)
    if not opened or (os.path.islink(opened) and not (windows and shared_storage.share_mounted())) \
            or os.path.islink(store) or paths.dirname(store) != root:
        return {"exit_code": -1, "stdout": "",
                "stderr": "account_shared_delete: the store must be a real direct child of the store root"}

    slug = paths.basename(store)
    lock_fh = open(_account_add_lock_path(slug), "a+")
    try:
        host_platform.lock(lock_fh, blocking=False)
    except OSError:
        lock_fh.close()
        return {"exit_code": 1, "stdout": "",
                "stderr": "account_add is in progress for this slug — cannot delete"}

    root_fd = -1
    try:
        if windows:
            return _remove_shared_leaf_windows(store, slug)
        try:
            root_fd = os.open(opened, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        except FileNotFoundError:
            return {"exit_code": 0, "stdout": json.dumps({"slug": slug, "deleted": False, "absent": True}),
                    "stderr": ""}
        except OSError as exc:
            return {"exit_code": 1, "stdout": "",
                    "stderr": f"account_shared_delete: cannot open the store root ({type(exc).__name__})"}
        try:
            os.lstat(slug, dir_fd=root_fd)
        except FileNotFoundError:
            return {"exit_code": 0, "stdout": json.dumps({"slug": slug, "deleted": False, "absent": True}),
                    "stderr": ""}
        crossing = _mount_crossing(store)
        if crossing:
            return {"exit_code": 1, "stdout": "", "stderr": f"account_shared_delete: {crossing}"}
        _kill_account_users(_account_paths(slug, store))
        failure = _force_remove_leaf(root_fd, slug, store)
        if failure:
            return {"exit_code": 1, "stdout": "", "stderr": f"account_shared_delete: {failure}"}
    finally:
        if root_fd >= 0:
            os.close(root_fd)
        host_platform.unlock(lock_fh)
        lock_fh.close()
    return {"exit_code": 0, "stdout": json.dumps({"slug": slug, "deleted": True}), "stderr": ""}


def _remove_shared_leaf_windows(store: str, slug: str) -> dict:
    """`_account_shared_delete`'s removal on Windows, as the runner account: there is no descriptor walk there,
    and a mounted store sits behind a link this daemon cannot read through (`\\\\wsl.localhost` is per-user).
    So the leaf is checked lexically (`store_is_on_share`) and `rm -rf` runs as the runner; whether a leaf
    behind the link existed cannot be told here, so a mounted store's removal reports `deleted`."""
    if not shared_storage.share_mounted() and not os.path.lexists(store):
        return {"exit_code": 0, "stdout": json.dumps({"slug": slug, "deleted": False, "absent": True}), "stderr": ""}
    if not shared_storage.store_is_on_share(store):
        return {"exit_code": -1, "stdout": "",
                "stderr": "account_shared_delete: the store must be a real direct child of the store root"}
    crossing = _mount_crossing(store)
    if crossing:
        return {"exit_code": 1, "stdout": "", "stderr": f"account_shared_delete: {crossing}"}
    err = session_user.remove_tree_as_session_user(store, timeout=60)
    if err:
        return {"exit_code": 1, "stdout": "", "stderr": f"account_shared_delete: could not remove {store} ({err})"}
    return {"exit_code": 0, "stdout": json.dumps({"slug": slug, "deleted": True}), "stderr": ""}


def _arg_peer_key(args: dict) -> str | None:
    """Phase 1's `peer_key`, the browser's public key for the sealed handoff. Required:
    `_login_start` refuses a missing or malformed one as `login_seal_invalid`. Never coerced: a
    non-string (or None) is handed on as-is so the refusal, not a default, answers it."""
    return args.get("peer_key")


def _arg_provider(args: dict) -> str:
    """Phase 1's `provider` value, as it arrives over the wire — which agent CLI this account is FOR.

    Absent or empty means `DEFAULT_PROVIDER` (a deploy-skew floor: a current CP always sends the row's
    value). A non-string is passed through as `str()`, never defaulted: defaulting would mint a Claude
    token for another vendor's account; the refusal names the odd value instead."""
    value = args.get("provider")
    if value is None or value == "":
        return agent_providers.DEFAULT_PROVIDER
    return value if isinstance(value, str) else str(value)


# ── Native dispatch ───────────────────────────────────────────────────────────────
def _converge_shared_link_account(config_dir: str) -> dict | None:
    """`account_converge` for a `shared_token_link` provider's account, or None for every other account.

    Decided on the roster row's PROVIDER (fixed for the account's life): the Claude-format layer
    (`fleet_dispatch.converge_account_layer`) must never be planted in another vendor's home. What this node
    owes such an account whose credential is in the token store is its credential LINK plus the vendor's
    per-home layer (preflight + `finalize`) in the per-node home; with the credential in the config dir it
    owes nothing. A credential in neither is a failure the durable ladder retries, never a silent success."""
    import fleet_dispatch  # noqa: PLC0415 — deferred, as the caller imports it

    slug = shared_storage.account_slug(config_dir)
    rows, _ok = account_roster()
    rec = next((r for r in rows if isinstance(r, dict) and str(r.get("slug") or "") == slug), {})
    row = agent_providers.by_value(str(rec.get("provider") or "") or agent_providers.DEFAULT_PROVIDER)
    if row is None or not row.shared_token_link:
        return None
    store = shared_storage.securestorage_dir(config_dir)
    if not store:
        if shared_storage.credential_file_present(config_dir, row.credential_file):
            return {"exit_code": 0, "stdout": json.dumps({"linked": False}), "stderr": ""}
        return {"exit_code": 1, "stdout": "",
                "stderr": f"account_converge: {slug} has no credential on this node yet — nothing to link"}
    refusal = fleet_dispatch.ensure_shared_credential_link(config_dir, store, row.value, layer=True)
    if refusal:
        return {"exit_code": 1, "stdout": "", "stderr": f"account_converge: {refusal}"}
    return {"exit_code": 0, "stdout": json.dumps({"linked": True}), "stderr": ""}


# The commands that put an account to use on this node, each with what it asks (the community edition's ledger,
# `ce_caps.admit_account`). The forget is `account_delete`.
_ACCOUNT_ADMITTING_COMMANDS = {"account_converge": "mint", "account_add": "log in", "account_reauth": "log in"}


# The account command_types, served in-process; `run` is the bridge's bounded subprocess closure. The
# mutating ones (add/reauth with both phase-2 completions, delete) trigger a post-exec reconcile
# (`commands._fire_post_reconcile`); `account_converge` does not. Enable/disable is
# `PUT /api/accounts/{id}`.
@native_command(
    # The managed layer for one existing account, on command from the CP: a local-config verb (no
    # credential, so no reconcile). `reconcile.accounts_probe.AgentsProbeCache.converge_unprovisioned` does the same but minutes late, and
    # an account in a mounted store is dispatchable the instant it is minted, so the CP commands it promptly.
    "account_converge",
    "account_add",
    "account_add_submit_code",
    "account_reauth",
    "account_reauth_submit_code",
    # AWAIT_CREDENTIALS mode's phase 2: its own verb because it carries no code and is re-callable.
    "account_add_await_login",
    "account_reauth_await_login",
    "account_delete",
    "account_shared_delete",
)
def handle_account_command(command_type: str, args: dict, run) -> dict:
    # The community edition's account cap (a no-op on a cloud node): a mint or a login with an account past the
    # ledger is refused before anything is created.
    if command_type in _ACCOUNT_ADMITTING_COMMANDS:
        why = ce_caps.admit_account(config_dir=_norm_dir(args.get("config_dir", "")) or "",
                                       api_key_account="" if args.get("config_dir") else args.get("slug", ""),
                                       action=_ACCOUNT_ADMITTING_COMMANDS[command_type])
        if why:
            log.warning("%s refused: %s", command_type, why)
            return {"exit_code": 1, "stdout": "", "stderr": why}
    if command_type == "account_converge":
        # `config_dir` is checked against the roster, never trusted.
        config_dir = _norm_dir(args.get("config_dir", ""))
        if not config_dir:
            return {"exit_code": -1, "stdout": "", "stderr": "account_converge needs config_dir"}
        known = {_norm_dir(str(d)) for d in _fleet_auth_dirs()}
        if config_dir not in known:
            # A miss forces one fresh roster read: this verb arrives right after the account was enabled.
            invalidate_account_roster()
            known = {_norm_dir(str(d)) for d in _fleet_auth_dirs()}
        if config_dir not in known:
            return {"exit_code": -1, "stdout": "", "stderr": f"unknown account config_dir: {config_dir}"}
        # Create the dir first (these nodes never held the account), as the session user at
        # `ACCOUNT_DIR_MODE`, as `reconcile.accounts_probe.AgentsProbeCache._run_converge` does.
        err = session_user.ensure_dir_as_session_user(config_dir, mode=ACCOUNT_DIR_MODE)
        if err:
            return {"exit_code": 1, "stdout": "",
                    "stderr": f"account_converge: could not create {config_dir}: {err}"}
        # The launch path's own entry point (one mechanism). It never raises and reports degraded parts.
        import fleet_dispatch

        linked = _converge_shared_link_account(config_dir)
        if linked is not None:
            return linked
        res = fleet_dispatch.converge_account_layer(config_dir)
        # Phase 1's return code is the verb's; `None` (did not run) is a failure. Phase 2 is detached.
        phase1 = res.get("phase1")
        return {
            "exit_code": 0 if phase1 == 0 else 1,
            "stdout": json.dumps(res),
            "stderr": "" if phase1 == 0 else (
                f"account_converge: settings/consent leg for {config_dir} "
                f"{'did not run' if phase1 is None else f'exited {phase1}'}"
            ),
        }

    if command_type == "account_add":
        # Refused here, before anything runs: a dir left behind would read as a half-registered account.
        provider = _arg_provider(args)
        refusal = _login_provider_refusal(provider)
        if refusal:
            log.warning("account_add refused: %s", refusal)
            return {"exit_code": 1, "stdout": "", "stderr": refusal}
        # An older CP's `shared` flag is unread: every login mints into the token store. `provider` passed
        # only when not Claude's, keeping Claude's call unchanged
        # (`test_the_add_arm_calls_through_unchanged_for_claude`).
        provider_kw = ({} if agent_providers.implied_on_wire(provider) else {"provider": provider})
        return _account_add(args.get("slug", ""), run, peer_key=_arg_peer_key(args), **provider_kw)

    if command_type == "account_add_submit_code":
        return _account_add_submit_code(args.get("slug", ""), args.get("sealed_code"), run)

    if command_type == "account_add_await_login":
        return _account_add_await_login(args.get("slug", ""), run)

    if command_type == "account_reauth":
        # The same gate: a wrong-CLI re-auth would overwrite a live credential.
        provider = _arg_provider(args)
        refusal = _login_provider_refusal(provider)
        if refusal:
            log.warning("account_reauth refused: %s", refusal)
            return {"exit_code": 1, "stdout": "", "stderr": refusal}
        # Omitted for Claude, exactly as on the add arm above and for the same reason.
        provider_kw = ({} if agent_providers.implied_on_wire(provider) else {"provider": provider})
        return _account_reauth(args.get("slug", ""), run, peer_key=_arg_peer_key(args), **provider_kw)

    if command_type == "account_reauth_submit_code":
        return _account_reauth_submit_code(args.get("slug", ""), args.get("sealed_code"), run)

    if command_type == "account_reauth_await_login":
        return _account_reauth_await_login(args.get("slug", ""), run)

    if command_type == "account_delete":
        return _account_delete(args.get("config_dir", ""))

    if command_type == "account_shared_delete":
        return _account_shared_delete(args.get("slug", ""))

    return {"exit_code": -1, "stdout": "", "stderr": f"unknown command_type: {command_type}"}
