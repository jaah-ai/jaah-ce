#!/usr/bin/env python3
"""provider_health.py — each non-Claude provider's own answer to "is this account's credential usable
right now", and — once it is — the account's identity (email, plan) and quota, so no provider account
borrows Anthropic's probe (`collector._read_account_quota`).

`observe()` keeps `_read_account_quota`'s `(account, quota)` contract. Health first: `connected` is a
conjunction of the vendor CLI's own verdict AND the launch predicate's
(`agents.get_adapter(provider).credential_state`), so a green dot can never be an account that cannot launch.
Three-valued: an uncertain probe yields `(None, None)`, published as nothing, so the CP keeps the row. Only a
`connected` account is then read for identity and quota (`_IDENTITY_QUOTA_READERS`): Codex through its own
`codex app-server` JSON-RPC, Antigravity through the Code Assist API the CLI calls. A reader that could not read
answers `quota=None` ("no reading from me") — never a fabricated block, which would speak about quota
undated. Never logs or returns a credential value: `codex login status` prints a masked-but-partial key to
stderr, which is captured and dropped; the Antigravity token is held in memory for the calls and discarded.
On the vendor-name census's `SEAM_FILES`; `collector.py` is `NEVER_ALLOWLISTABLE`.
"""
from __future__ import annotations

import errno
import json
import logging
import os
import re
import selectors
import shutil
import stat
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

import agent_providers
import google_id_token
from forge_cli.transport import NoRedirect

# 0 where the platform lacks the flag (it is POSIX-only).
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_O_NONBLOCK = getattr(os, "O_NONBLOCK", 0)

log = logging.getLogger("jaahd.provider_health")

# Read server-side by `app/account_fold.NODE_STATUS_NO_CREDENTIALS` and `routes/fleet.py::_account_candidates`.
STATUS_CONNECTED = "connected"
STATUS_NO_CREDENTIALS = "no_credentials"

# Codex's `CLAUDE_CONFIG_DIR`: config and stored login (`auth.json`) live here (verified, codex-cli 0.151.0).
CODEX_HOME_ENV = "CODEX_HOME"

# Exit 0 is logged in, exit 1 is not; the message goes to stderr either way and is never parsed.
_CODEX_STATUS_ARGV = ("login", "status")

# Bound on one probe: `codex login status` is a local read, so this is cold-start headroom, well inside
# the account-probe interval (`node_settings`' `node_account_probe_interval_secs`, floor 120s).
_PROBE_TIMEOUT = 20.0

# Read bound on a vendor credential file (~1 KiB real): the dir is runner-writable, so an unbounded read
# would offer the node's memory to the planter.
_MAX_CREDS_BYTES = 64 * 1024


def _probe_env(auth_dir: Path, home_var: str) -> dict[str, str]:
    """The environment one vendor CLI health probe runs with — SPELLED OUT, never `os.environ`.

    The daemon's environment carries `NODE_TOKEN`, which a vendor CLI must never see. `PATH` and `HOME`
    are carried because the binaries are npm shims that re-exec `node`."""
    env = {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", ""),
        home_var: str(auth_dir),
    }
    return {key: value for key, value in env.items() if value}


def _codex_healthy(auth_dir: Path) -> Optional[bool]:
    """Is this Codex account logged in? `codex login status`, judged on its EXIT CODE alone.

    None when it could not be asked (binary absent, timeout, exec failure) or on an exit code other than
    0/1 — never False, since only a real "not logged in" may exclude the account.

    A missing home is a proven absence, answered False without running the CLI (which might create it).
    Asked with `os.stat`, not `Path.is_dir()`: `is_dir()` turns EACCES/EIO/ESTALE into False, i.e. a
    positive `no_credentials` on a transient error."""
    try:
        mode = os.stat(auth_dir).st_mode
    except (FileNotFoundError, NotADirectoryError):
        return False                        # nothing is at this path: a PROVEN absence of a login
    except OSError as exc:
        log.warning("provider health: %s could not be stat'ed (%s) — unobserved this pass",
                    auth_dir, errno.errorcode.get(exc.errno or 0, exc.errno))
        return None
    if not stat.S_ISDIR(mode):
        return False                        # not the directory a login lives in
    binary = shutil.which(agent_providers.CODEX.binary)
    if not binary:
        log.warning("provider health: %s is not on this node's PATH — %s stays unobserved this pass",
                    agent_providers.CODEX.binary, auth_dir)
        return None
    try:
        proc = subprocess.run(
            [binary, *_CODEX_STATUS_ARGV],
            # BOTH streams captured and BOTH discarded: stderr carries a masked-but-partial credential
            # (see the module docstring), and letting it inherit would print it into the daemon's journal.
            capture_output=True,
            env=_probe_env(auth_dir, CODEX_HOME_ENV),
            timeout=_PROBE_TIMEOUT,
            check=False,
        )
    except Exception as exc:  # noqa: BLE001 — TimeoutExpired, OSError; an unanswered probe, never a verdict
        log.warning("provider health: %s status probe failed for %s (%s) — unobserved this pass",
                    agent_providers.CODEX.binary, auth_dir, type(exc).__name__)
        return None
    if proc.returncode == 0:
        return True
    if proc.returncode == 1:
        return False
    log.warning("provider health: %s status returned %s for %s — unobserved this pass",
                agent_providers.CODEX.binary, proc.returncode, auth_dir)
    return None


def _read_guarded_bytes(path: Path) -> tuple[Optional[bool], Optional[bytes]]:
    """`(present, raw bytes)` for one small file in a runner-writable dir. `present` is three-valued:
    unreadable is None, not absent; absent or not a regular file is False.

    FD-based, because the dir is runner-writable:
      * `O_NONBLOCK` — a planted FIFO would otherwise block forever and freeze every account's probe.
      * `O_NOFOLLOW` — a final-component symlink would read another path with the reader's authority.
        Parent dirs are still followed (1-bit oracle residual; fix is `openat` on `.gemini`), harmless
        while `collector.probe_account` refuses to run in-process on a dir it does not own.
      * `fstat` on the opened fd (no check-then-read window); `st_nlink != 1` refuses a hardlink.
    The read asks one byte over `_MAX_CREDS_BYTES`, measured in BYTES before decode, so our own
    truncation is None, never False. One `os.read` assumes no short read (local, `node_pinned` files)."""
    try:
        fd = os.open(path, os.O_RDONLY | _O_NONBLOCK | _O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0))
    except PermissionError:
        log.warning("provider health: %s is present but unreadable — unobserved this pass", path)
        return None, None
    except (FileNotFoundError, NotADirectoryError):
        # Proven absence. A directory is not caught here: `os.open(O_RDONLY)` on one succeeds on Linux,
        # so `S_ISREG` below answers it.
        return False, None
    except OSError as exc:
        # `ELOOP` (symlink refused) is a definite absence here, so False; every other OSError is "could
        # not tell" — False would exclude a healthy account on a transient error.
        if exc.errno == errno.ELOOP:
            return False, None
        log.warning("provider health: %s could not be read (%s) — unobserved this pass",
                    path, errno.errorcode.get(exc.errno or 0, exc.errno))
        return None, None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            # The one guard refusing a FIFO, directory or device, before any read.
            return False, None
        if info.st_nlink != 1:
            # A second name for this inode means the file may be someone else's, linked in.
            log.warning("provider health: %s has %s links — unobserved this pass", path, info.st_nlink)
            return None, None
        data = os.read(fd, _MAX_CREDS_BYTES + 1)
    except OSError:
        return None, None                   # the read itself failed: nothing proven about the account
    finally:
        os.close(fd)
    if len(data) > _MAX_CREDS_BYTES:
        # Our bound, not a verdict; measured on bytes before decode (see the docstring).
        log.warning("provider health: %s is larger than the %s-byte read bound — unobserved this pass",
                    path, _MAX_CREDS_BYTES)
        return None, None
    return True, data


def _read_guarded_json(path: Path) -> tuple[Optional[bool], object]:
    """`_read_guarded_bytes`, parsed: unparseable is False (no document), never an error to raise."""
    present, data = _read_guarded_bytes(path)
    if present is not True:
        return present, None
    try:
        return True, json.loads(data.decode("utf-8", "replace"))
    except Exception:  # noqa: BLE001 — a truncated/garbage file is no document, not an error to raise
        return False, None


# ── Antigravity: four answers, kept apart (a missing CLI, a broken one, a signed-out account, a live one) ──
ANTIGRAVITY_BINARY_MISSING = "binary_missing"
ANTIGRAVITY_BINARY_BROKEN = "binary_broken"
ANTIGRAVITY_UNAUTHENTICATED = "unauthenticated"
ANTIGRAVITY_AUTHENTICATED = "authenticated"
# `agy` is a native ELF (measured, agy 1.2.11 linux amd64/arm64); anything else at that name cannot run.
_ELF_MAGIC = b"\x7fELF"


def _antigravity_binary_state(path: Optional[str] = None) -> Optional[str]:
    """`ANTIGRAVITY_BINARY_MISSING`, `ANTIGRAVITY_BINARY_BROKEN`, None when fine, or `"unknown"` when this
    process cannot tell. Decided on the runner's PATH (`session_user.delegated_path()`) WITHOUT EXECUTING
    anything there: `~jaah/.local/bin` is runner-writable, so running it would hand a runner the daemon
    uid (`runner_clis.py`). The search skips what `execvp` skips (a dangling link, a non-file, no execute
    bit for the runner); the first runnable entry decides, and is broken unless it is an ELF image (an
    interrupted download, a planted script). Only unrunnable entries and none that runs: broken."""
    import runner_clis  # noqa: PLC0415 — deferred: it imports `session_user`, needed on this arm only
    import session_user  # noqa: PLC0415

    binary = agent_providers.ANTIGRAVITY.binary
    search = path if path is not None else session_user.delegated_path()
    uid, gid = session_user.session_uid(), session_user.session_gid()
    broken = False
    for directory in search.split(os.pathsep):
        if not directory:
            continue
        candidate = os.path.join(directory, binary)
        try:
            os.lstat(candidate)
        except (FileNotFoundError, NotADirectoryError):
            continue                            # nothing by that name here: keep searching
        except OSError:
            return "unknown"
        try:
            st = os.stat(candidate)
        except (FileNotFoundError, NotADirectoryError):
            broken = True                       # a dangling symlink: a PATH search passes over it
            continue
        except OSError:
            return "unknown"
        if not (stat.S_ISREG(st.st_mode) and st.st_mode & runner_clis._exec_bit_for(st, uid, gid)):
            broken = True                       # not runnable here; the search goes on, as `execvp`'s does
            continue
        head = runner_clis._head_bytes(candidate)
        if head is None:
            return "unknown"
        return None if head.startswith(_ELF_MAGIC) else ANTIGRAVITY_BINARY_BROKEN
    return ANTIGRAVITY_BINARY_BROKEN if broken else ANTIGRAVITY_BINARY_MISSING


def antigravity_health(auth_dir: Path, path: Optional[str] = None) -> Optional[str]:
    """One of the four `ANTIGRAVITY_*` answers for this account, or None when it cannot be told.

    The CLI exposes no status verb (measured, agy 1.2.11: `agy models` needs a signed-in account and prints
    prose), so the account half is the credential file's presence and shape: the token the CLI's file store
    writes at `ANTIGRAVITY.credential_file` (a store `agents/antigravity_keyring.py` forces). Present and
    non-empty is signed in; its content is not parsed here (every key is a secret) — only the quota reader,
    `_antigravity_identity_and_quota`, reads it."""
    binary = _antigravity_binary_state(path)
    if binary == "unknown":
        return None
    if binary is not None:
        return binary
    present, data = _read_guarded_bytes(Path(auth_dir) / agent_providers.ANTIGRAVITY.credential_file)
    if present is None:
        return None
    return ANTIGRAVITY_AUTHENTICATED if present and data and data.strip() else ANTIGRAVITY_UNAUTHENTICATED


def _antigravity_healthy(auth_dir: Path) -> Optional[bool]:
    """`antigravity_health` folded to the probe contract, each cause logged in its own words.

    A missing or broken CLI is None ("cannot tell", the row is left alone): the account may be fine and the
    node is what needs repair — `runner_clis` already withholds the binary from dispatch. Only a signed-out
    account is False."""
    state = antigravity_health(auth_dir)
    if state == ANTIGRAVITY_BINARY_MISSING:
        log.warning("provider health: no `%s` on the runner's PATH — %s stays unobserved until the runner-CLI "
                    "floor installs it (packaging/postinst)", agent_providers.ANTIGRAVITY.binary, auth_dir)
        return None
    if state == ANTIGRAVITY_BINARY_BROKEN:
        log.warning("provider health: `%s` on the runner's PATH cannot run (not an executable ELF image) — %s "
                    "stays unobserved until the runner-CLI floor reinstalls it",
                    agent_providers.ANTIGRAVITY.binary, auth_dir)
        return None
    if state == ANTIGRAVITY_UNAUTHENTICATED:
        return False
    if state == ANTIGRAVITY_AUTHENTICATED:
        return True
    return None


# ── Identity and quota: each vendor's own reading, taken only once the account is `connected` ──────────
#
# The block shape is Anthropic's (`collector._live_quota`), so the CP folds it unchanged: a window of at most
# a day fills the `fiveHour*` slot, a longer one the `weekly*` slot. What each slot means for a provider is
# `agent_providers.quota_windows`. A block that asserts a percentage carries `observedAt` (it REPLACES the
# stored reading); a reader that could not read returns None — "no reading from me", never a fabricated
# block.

# Whether this account has a quota to read, sent on every read of a `connected` account — always, because the
# CP merges `extra` and never deletes a key. `none` is STRUCTURAL (a linked Codex account, an API-key login,
# an Antigravity login with no Code Assist project): the dispatch gate then treats the account as having no quota
# (`routes/fleet.py::_account_candidates`) rather than excluding it for lacking a reading. A reader that
# merely could not read this pass says nothing, so the stored value stands.
QUOTA_MODEL_KEY = "quota_model"
QUOTA_MODEL_NONE = "none"
QUOTA_MODEL_READABLE = "readable"

# Identity labels are bounded (an email's own ceiling).
_MAX_LABEL_CHARS = 320
# Bound on an HTTP body a Code Assist reader takes in.
_MAX_HTTP_BYTES = 1024 * 1024

# Windows up to a day fill the short slot; anything longer, the weekly one.
_SHORT_WINDOW_MAX_MINUTES = 24 * 60

# One `codex app-server` exchange, start to finish: a process start plus two vendor round trips. Kept well
# inside `collector._DELEGATED_PROBE_TIMEOUT` beside the `codex login status` probe it follows.
_CODEX_RPC_TIMEOUT = 12.0
# A runaway stdout is not buffered without bound.
_CODEX_RPC_MAX_BYTES = 1024 * 1024
# The CLI's own JSON-RPC surface (`codex app-server generate-json-schema`, codex-cli 0.152): no token is
# touched here — the CLI reads its own `auth.json`. `refreshToken: false` asks it not to refresh on our behalf.
_CODEX_REQUESTS: tuple[dict, ...] = (
    {"id": 1, "method": "initialize", "params": {"clientInfo": {"name": "jaahd", "version": "1"}}},
    {"method": "initialized"},
    {"id": 2, "method": "account/read", "params": {"refreshToken": False}},
    {"id": 3, "method": "account/rateLimits/read"},
)
_CODEX_ACCOUNT_ID = 2
_CODEX_LIMITS_ID = 3
# The metered bucket runners draw on, when the multi-bucket view names it.
_CODEX_LIMIT_ID = "codex"



def _epoch_to_iso(value: object) -> Optional[str]:
    """Unix seconds as ISO-8601 UTC, or None."""
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    try:
        return datetime.fromtimestamp(value, timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


def _plan_label(raw: object) -> Optional[str]:
    """A vendor plan id as a label: `plus` -> `Plus`, `self_serve_business` -> `Self Serve Business`.
    `unknown`/blank is None — no plan is better than a made-up one."""
    if not isinstance(raw, str) or not raw.strip() or raw.strip().lower() == "unknown":
        return None
    return " ".join(word.capitalize() for word in raw.strip().replace("-", "_").split("_") if word)


def _pct(value: object) -> Optional[int]:
    """A percentage clamped to 0..100, or None for anything that is not a number."""
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    return max(0, min(100, round(value)))


def _quota_block(windows: list[tuple[Optional[int], Optional[int], Optional[str]]]) -> Optional[dict]:
    """`[(window_minutes, used_percent, resets_at_iso)]` -> a dated quota block, or None when no window
    carries a percentage. The worst window per slot wins, so the gate conserves the tightest limit."""
    slots: dict[str, tuple[int, Optional[str]]] = {}
    for minutes, used, resets_at in windows:
        if used is None:
            continue
        slot = "weekly" if minutes is not None and minutes > _SHORT_WINDOW_MAX_MINUTES else "fiveHour"
        if slot not in slots or used > slots[slot][0]:
            slots[slot] = (used, resets_at)
    if not slots:
        return None
    import collector  # noqa: PLC0415 — deferred: `collector` imports this module

    block: dict = {"observedAt": collector._now_iso()}
    for slot in ("fiveHour", "weekly"):
        used, resets_at = slots.get(slot, (None, None))
        block[f"{slot}Percent"] = used
        block[f"{slot}ResetsAt"] = resets_at
    return block


def _codex_rpc(auth_dir: Path) -> dict[int, dict]:
    """The `codex app-server` answers to `_CODEX_REQUESTS`, by request id; `{}` when it could not be asked.

    The server answers asynchronously and exits on stdin EOF before answering, so stdin stays open until
    both answers arrive or `_CODEX_RPC_TIMEOUT` passes. Only a RESPONSE is an answer (no `method`, and a
    `result` or `error`): a server-to-client request may reuse an id. stderr is discarded, as in
    `_codex_healthy`."""
    binary = shutil.which(agent_providers.CODEX.binary)
    if not binary:
        return {}
    wanted = {_CODEX_ACCOUNT_ID, _CODEX_LIMITS_ID}
    answers: dict[int, dict] = {}
    try:
        proc = subprocess.Popen(
            [binary, "app-server"], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, env=_probe_env(auth_dir, CODEX_HOME_ENV),
        )
    except OSError as exc:
        log.warning("provider quota: %s app-server could not start for %s (%s)",
                    agent_providers.CODEX.binary, auth_dir, type(exc).__name__)
        return {}
    try:
        assert proc.stdin is not None and proc.stdout is not None
        proc.stdin.write(b"".join(json.dumps(r).encode() + b"\n" for r in _CODEX_REQUESTS))
        proc.stdin.flush()
        deadline = time.monotonic() + _CODEX_RPC_TIMEOUT
        buf = b""
        total = 0
        with selectors.DefaultSelector() as sel:
            sel.register(proc.stdout, selectors.EVENT_READ)
            while wanted - answers.keys():
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not sel.select(remaining):
                    break
                chunk = os.read(proc.stdout.fileno(), 65536)
                total += len(chunk)
                if not chunk or total > _CODEX_RPC_MAX_BYTES:
                    break
                buf += chunk
                while b"\n" in buf:
                    line, buf = buf.split(b"\n", 1)
                    try:
                        message = json.loads(line)
                    except (ValueError, RecursionError):
                        continue
                    if (isinstance(message, dict) and message.get("id") in wanted and "method" not in message
                            and ("result" in message or "error" in message)):
                        answers[message["id"]] = message
    except (OSError, ValueError) as exc:
        log.warning("provider quota: %s app-server exchange failed for %s (%s)",
                    agent_providers.CODEX.binary, auth_dir, type(exc).__name__)
    finally:
        for close in (proc.stdin, proc.stdout):
            try:
                if close is not None:
                    close.close()
            except OSError:
                pass
        proc.terminate()
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
    return answers


def _label(value: object, limit: int = _MAX_LABEL_CHARS) -> Optional[str]:
    """A string the CP stores as an identity label, or None: non-strings refused (the CP's model types
    `email` as a string, and a dict would reject the node's whole observation), length bounded."""
    if not isinstance(value, str) or not value.strip():
        return None
    return value.strip()[:limit]


def _linked_cli_home(store: Path) -> Optional[Path]:
    """A linked account's PER-NODE CLI home — `<jaah_home>/auth/<slug>`, where its runners already run
    the CLI — or None unless that home is provably this account's: a real directory (not a link) whose
    credential file is the link to exactly `store`'s. The CLI never runs with its home in the store."""
    import environment  # noqa: PLC0415 — stdlib-only leaf; `pool_root` is the one home of JAAH_HOME
    import shared_storage  # noqa: PLC0415 — deferred, as in `observe`

    slug, root = shared_storage.account_slug(str(store)), environment.pool_root()
    if not slug or not root:
        return None
    home = Path(root) / "auth" / slug
    credential = agent_providers.CODEX.credential_file
    try:
        if not stat.S_ISDIR(os.lstat(home).st_mode):
            return None
        if os.path.realpath(home / credential) != os.path.realpath(store / credential):
            return None
    except (OSError, RuntimeError):
        return None
    return home


def _codex_identity_and_quota(auth_dir: Path, linked: bool = False) -> tuple[dict, Optional[dict]]:
    """Email and plan from `account/read`; the 5h/weekly windows from `account/rateLimits/read`.

    A linked account (`linked`, `auth_dir` the store) is read in its per-node home (`_linked_cli_home`), as
    its runners run — never with the home in the store, which would load config any mounting node can
    write; with no such home it has no quota this node can read. An API-key login has none either."""
    if linked:
        home = _linked_cli_home(auth_dir)
        if home is None:
            return {QUOTA_MODEL_KEY: QUOTA_MODEL_NONE}, None
        auth_dir = home
    answers = _codex_rpc(auth_dir)
    identity: dict = {}
    account = (answers.get(_CODEX_ACCOUNT_ID, {}).get("result") or {}).get("account")
    if isinstance(account, dict):
        if account.get("type") == "chatgpt":
            identity = {"email": _label(account.get("email")), "tier": _plan_label(account.get("planType"))}
        else:
            return {QUOTA_MODEL_KEY: QUOTA_MODEL_NONE}, None
    result = answers.get(_CODEX_LIMITS_ID, {}).get("result") or {}
    by_id = result.get("rateLimitsByLimitId") if isinstance(result.get("rateLimitsByLimitId"), dict) else {}
    snapshot = by_id.get(_CODEX_LIMIT_ID) or result.get("rateLimits")
    if not isinstance(snapshot, dict):
        return identity, None
    identity[QUOTA_MODEL_KEY] = QUOTA_MODEL_READABLE
    if not identity.get("tier"):
        identity["tier"] = _plan_label(snapshot.get("planType"))
    windows = []
    # Placed by the window's own length; a window that states none is placed by position (primary is the
    # short window, secondary the weekly one), never defaulted into the short slot.
    for key, fallback_minutes in (("primary", None), ("secondary", _SHORT_WINDOW_MAX_MINUTES + 1)):
        window = snapshot.get(key)
        if isinstance(window, dict):
            minutes = window.get("windowDurationMins")
            minutes = minutes if isinstance(minutes, int) and not isinstance(minutes, bool) else fallback_minutes
            windows.append((minutes, _pct(window.get("usedPercent")), _epoch_to_iso(window.get("resetsAt"))))
    return identity, _quota_block(windows)


_HTTP = urllib.request.build_opener(NoRedirect)


def _http_json(url: str, *, data: bytes, headers: dict[str, str]) -> dict:
    """POST and parse, reading at most `_MAX_HTTP_BYTES`; raises `urllib.error.HTTPError` on a non-2xx
    (a redirect included)."""
    req = urllib.request.Request(url, data=data, headers={"User-Agent": "jaahd-collector/1.0", **headers})
    with _HTTP.open(req, timeout=_HTTP_TIMEOUT) as resp:
        body = json.loads(resp.read(_MAX_HTTP_BYTES))
    return body if isinstance(body, dict) else {}


# ── Antigravity's reading: the same Code Assist API family, on the host and in the shape `agy` itself uses ──
#
# Measured against agy 1.2.11 with a signed-in account: the CLI serves every call from
# `daily-cloudcode-pa` (its cli.log), and `retrieveUserQuotaSummary` there answers groups of buckets, each a
# `window` ("5h", "weekly") with a `remainingFraction` and `resetTime` — one Gemini group and one Claude/GPT
# group, each with both windows. `loadCodeAssist` names the project and tier ONLY to a User-Agent that
# says `antigravity` (any other gets "This client is no longer supported" and no project), so ours says so
# after our own name. The token file is `{"token": {"access_token", "refresh_token", "expiry"}, "id_token"}`.
_ANTIGRAVITY_API_URL = "https://daily-cloudcode-pa.googleapis.com/v1internal:{method}"
_GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
_HTTP_TIMEOUT = 8.0
_ANTIGRAVITY_USER_AGENT = "jaahd-collector/1.0 antigravity"
_ANTIGRAVITY_CLIENT_METADATA = {"ideType": "ANTIGRAVITY", "platform": "PLATFORM_UNSPECIFIED", "pluginType": "GEMINI"}
# A bucket's `window` -> its length in minutes; a label not measured is not guessed at.
_ANTIGRAVITY_WINDOWS = {"5h": 5 * 60, "weekly": 7 * 24 * 60}
# The OAuth client secret is read out of the installed binary, never copied here (it moves with the pinned
# build): Google's installed-application secrets all start `GOCSPX-` (the binary holds two; one pairs).
_ANTIGRAVITY_SECRET_RE = re.compile(rb"GOCSPX-[A-Za-z0-9_-]{28}")
_MAX_ANTIGRAVITY_BINARY_BYTES = 1024 * 1024 * 1024
# Candidates kept per build: the measured binary holds two, and each costs one token request carrying the
# refresh token, so a planted binary full of them must not multiply those.
_MAX_ANTIGRAVITY_SECRETS = 4
# realpath -> `((mtime, size), candidate secrets)`, and realpath -> the secret that refreshed. One entry per
# path, so touching the binary replaces the entry rather than growing the cache.
_antigravity_secrets_cache: dict[str, tuple[tuple[float, int], list[str]]] = {}
_antigravity_secret_worked: dict[str, str] = {}


def _antigravity_secrets() -> tuple[Optional[str], list[str]]:
    """The installed `agy`'s candidate client secrets, the one that last worked first; READ, never executed
    (the binary sits in a runner-writable directory — see `_antigravity_binary_state`). Cached per build.

    Read in chunks through a descriptor opened no-follow and non-blocking, never mapped: a runner can
    rewrite the file mid-scan (a shrunk mapping is a SIGBUS in the daemon) or swap in a FIFO (a blocking
    `open` that never returns)."""
    import session_user  # noqa: PLC0415 — deferred, as in `_antigravity_binary_state`

    binary = shutil.which(agent_providers.ANTIGRAVITY.binary, path=session_user.delegated_path())
    if not binary:
        return None, []
    entry = os.path.realpath(binary)
    try:
        fd = os.open(entry, os.O_RDONLY | _O_NOFOLLOW | _O_NONBLOCK | getattr(os, "O_CLOEXEC", 0))
    except OSError:
        return None, []
    with os.fdopen(fd, "rb") as handle:
        try:
            info = os.fstat(handle.fileno())
        except OSError:
            return None, []
        stamp = (info.st_mtime, info.st_size)
        cached = _antigravity_secrets_cache.get(entry)
        if cached is None or cached[0] != stamp:
            found: list[str] = []
            if stat.S_ISREG(info.st_mode) and 0 < info.st_size <= _MAX_ANTIGRAVITY_BINARY_BYTES:
                tail = b""
                try:
                    while len(found) < _MAX_ANTIGRAVITY_SECRETS and (chunk := handle.read(1 << 20)):
                        window = tail + chunk
                        for match in _ANTIGRAVITY_SECRET_RE.finditer(window):
                            secret = match.group().decode()
                            if secret not in found and len(found) < _MAX_ANTIGRAVITY_SECRETS:
                                found.append(secret)
                        # Kept so a secret split across two chunks is still seen whole.
                        tail = window[-40:]
                except OSError:
                    return None, []
            if not found:
                log.warning("provider quota: no OAuth client secret found in the %s binary at %s",
                            agent_providers.ANTIGRAVITY.binary, entry)
            _antigravity_secrets_cache[entry] = (stamp, found)
            _antigravity_secret_worked.pop(entry, None)
    candidates = list(_antigravity_secrets_cache[entry][1])
    worked = _antigravity_secret_worked.get(entry)
    if worked in candidates:
        candidates.remove(worked)
        candidates.insert(0, worked)
    return entry, candidates


#: `_antigravity_refresh_outcome`'s failure words (an access token comes with "").
REFRESH_INVALID_GRANT = "invalid_grant"  # Google refused the refresh token itself
REFRESH_INVALID_CLIENT = "invalid_client"  # no candidate secret pairs with the client, or none was found
REFRESH_UNREACHABLE = "unreachable"  # the token endpoint could not be reached or answered unreadably
REFRESH_REFUSED = "refused"  # any other HTTP refusal


def _antigravity_refresh(token: dict, client_id: str) -> tuple[Optional[str], bool]:
    """`(access_token, rejected)` from a refresh held in memory only — the file is never written back (the
    CLI keeps refreshing its own). `rejected` on `invalid_grant` alone (`_antigravity_refresh_outcome`)."""
    access, outcome = _antigravity_refresh_outcome(token, client_id)
    return access, outcome == REFRESH_INVALID_GRANT


def _antigravity_refresh_outcome(token: dict, client_id: str) -> tuple[Optional[str], str]:
    """`(access_token, "")`, or `(None, <a REFRESH_* word>)` naming why there is none: `invalid_client` moves to the
    next candidate secret and is the verdict only once none pairs, so a network failure is never read as one."""
    refresh_token = token.get("refresh_token")
    key, secrets = _antigravity_secrets()
    if not (isinstance(refresh_token, str) and refresh_token and client_id):
        return None, REFRESH_REFUSED
    if not secrets:
        return None, REFRESH_INVALID_CLIENT
    for secret in secrets:
        form = urllib.parse.urlencode({"client_id": client_id, "client_secret": secret,
                                       "refresh_token": refresh_token, "grant_type": "refresh_token"}).encode()
        try:
            body = _http_json(_GOOGLE_TOKEN_URL, data=form,
                              headers={"Content-Type": "application/x-www-form-urlencoded"})
        except urllib.error.HTTPError as exc:
            try:
                error = json.loads(exc.read(_MAX_HTTP_BYTES)).get("error")
            except Exception:  # noqa: BLE001 — an unreadable error body is not a verdict
                error = None
            if exc.code == 401 and error == "invalid_client":
                continue
            log.warning("provider quota: antigravity token refresh refused (HTTP %s)", exc.code)
            return None, (REFRESH_INVALID_GRANT if exc.code == 400 and error == "invalid_grant" else REFRESH_REFUSED)
        except Exception as exc:  # noqa: BLE001 — network/parse: no reading this pass
            log.warning("provider quota: antigravity token refresh failed (%s)", type(exc).__name__)
            return None, REFRESH_UNREACHABLE
        access = body.get("access_token")
        if isinstance(access, str) and access:
            _antigravity_secret_worked[key] = secret
            return access, ""
        return None, REFRESH_UNREACHABLE
    log.warning("provider quota: no client secret in the installed %s pairs with its OAuth client",
                agent_providers.ANTIGRAVITY.binary)
    return None, REFRESH_INVALID_CLIENT


def _antigravity_stored_token(token: dict) -> Optional[str]:
    """The stored access token while it has a minute left (`expiry` is RFC 3339), else None."""
    access, expiry = token.get("access_token"), token.get("expiry")
    if not (isinstance(access, str) and access and isinstance(expiry, str)):
        return None
    try:
        # Go writes nanoseconds; `fromisoformat` reads at most microseconds.
        stamp = datetime.fromisoformat(re.sub(r"(\.\d{6})\d+", r"\1", expiry))
    except ValueError:
        return None
    return access if stamp.timestamp() > time.time() + 60 else None


def _antigravity_code_assist(token: str) -> tuple[dict, Optional[dict], bool]:
    """`(identity, quota_summary, no_project)` from `loadCodeAssist` then `retrieveUserQuotaSummary`; raises
    `urllib.error.HTTPError` for the caller to judge."""
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json",
               "User-Agent": _ANTIGRAVITY_USER_AGENT}
    load = _http_json(_ANTIGRAVITY_API_URL.format(method="loadCodeAssist"), headers=headers,
                      data=json.dumps({"metadata": _ANTIGRAVITY_CLIENT_METADATA}).encode())
    identity: dict = {}
    # Measured: `paidTier` names the subscription ("Google AI Ultra"), `currentTier` the product.
    for key in ("paidTier", "currentTier"):
        tier = load.get(key)
        name = _label(tier.get("name")) if isinstance(tier, dict) else None
        if name:
            identity["tier"] = name
            break
    project = load.get("cloudaicompanionProject")
    if isinstance(project, dict):
        project = project.get("id")
    if not isinstance(project, str) or not project:
        return identity, None, True
    summary = _http_json(_ANTIGRAVITY_API_URL.format(method="retrieveUserQuotaSummary"), headers=headers,
                         data=json.dumps({"project": project}).encode())
    return identity, summary, False


def _antigravity_identity_and_quota(auth_dir: Path, linked: bool = False) -> tuple[dict, Optional[dict]]:
    """Email from the id_token; plan from `loadCodeAssist`; the 5-hour and weekly windows from
    `retrieveUserQuotaSummary` (every group's buckets, the most-used per slot winning, as `_quota_block`
    conserves). No CLI runs. The OAuth client id is the id_token's own `aud`, so it is never copied here.
    `auth_rejected` only on a refresh refused with `invalid_grant`, or a 401 to a FRESHLY refreshed token: a 401
    to the stored one earns one refresh and retry first, since a revoked access token is not a dead login."""
    del linked  # the store is laid out as a CLI home (`shared_storage.credential_home`), so the path is the same
    present, document = _read_guarded_json(Path(auth_dir) / agent_providers.ANTIGRAVITY.credential_file)
    if present is not True or not isinstance(document, dict) or not isinstance(document.get("token"), dict):
        return {}, None
    id_token = document.get("id_token", "")
    identity: dict = {"email": _label(google_id_token.id_token_claim(id_token, "email"))}
    client_id = google_id_token.id_token_claim(id_token, "aud")
    token, fresh = _antigravity_stored_token(document["token"]), False
    for _attempt in range(2):
        if token is None:
            token, rejected = _antigravity_refresh(document["token"], client_id)
            if token is None:
                return identity, (_rejected_quota() if rejected else None)
            fresh = True
        try:
            found, summary, no_project = _antigravity_code_assist(token)
        except urllib.error.HTTPError as exc:
            log.warning("provider quota: antigravity Code Assist refused (HTTP %s) for %s", exc.code, auth_dir)
            if exc.code == 401 and not fresh:
                token = None
                continue
            return identity, (_rejected_quota() if exc.code == 401 else None)
        except Exception as exc:  # noqa: BLE001 — network/parse: no reading this pass
            log.warning("provider quota: antigravity Code Assist read failed for %s (%s)", auth_dir,
                        type(exc).__name__)
            return identity, None
        break
    else:
        return identity, None
    identity.update(found)
    if no_project:
        log.info("provider quota: antigravity account at %s has no Code Assist project — no quota to read",
                 auth_dir)
        identity[QUOTA_MODEL_KEY] = QUOTA_MODEL_NONE
        return identity, None
    identity[QUOTA_MODEL_KEY] = QUOTA_MODEL_READABLE
    windows = []
    for group in (summary or {}).get("groups") or []:
        for bucket in (group.get("buckets") or []) if isinstance(group, dict) else []:
            minutes = _ANTIGRAVITY_WINDOWS.get(bucket.get("window")) if isinstance(bucket, dict) else None
            fraction = bucket.get("remainingFraction") if minutes else None
            if not isinstance(fraction, (int, float)) or isinstance(fraction, bool):
                continue
            reset = bucket.get("resetTime")
            windows.append((minutes, _pct((1 - fraction) * 100), reset if isinstance(reset, str) else None))
    return identity, _quota_block(windows)


def _rejected_quota() -> dict:
    """`collector._auth_rejected_quota(token)` — the one home of that sentinel. Lazy, like `_quota_block`."""
    import collector  # noqa: PLC0415 — deferred: `collector` imports this module

    return collector._auth_rejected_quota(collector._REJECTED_TOKEN)


# health probe (`Capabilities.health_probe`) -> its identity/quota reader, run only for a `connected` account;
# the second argument says the account's credential is linked into the token store (`auth_dir` is then the store).
_IDENTITY_QUOTA_READERS: dict[str, Callable[[Path, bool], tuple[dict, Optional[dict]]]] = {
    "app_server": _codex_identity_and_quota,
    "code_assist": _antigravity_identity_and_quota,
}


def _pi_healthy(auth_dir: Path) -> Optional[bool]:
    """None, always: a Pi account is an API key the control plane holds, served per run by the credential broker
    (`agents/pi.py`), so nothing on a node says whether it works. Unobserved, never `no_credentials`."""
    del auth_dir
    return None


def _claude_code_account(auth_dir: Path, securestorage_dir: str) -> tuple[Optional[dict], Optional[dict]]:
    """`collector.claude_code_account_quota` — Anthropic's token ladder, its one home. Lazy, like `_quota_block`."""
    import collector  # noqa: PLC0415 — deferred: `collector` imports this module

    return collector.claude_code_account_quota(auth_dir, securestorage_dir)


# account layer (`Capabilities.account_layer`) -> the reader answering for EVERY provider whose homes carry it:
# a CLI that is Claude Code itself is read through its OAuth token, never a per-provider probe.
_ACCOUNT_LAYER_READERS: dict[str, Callable[[Path, str], tuple[Optional[dict], Optional[dict]]]] = {
    "omc": _claude_code_account,
}


def has_account_reader(provider: str) -> bool:
    """Whether `provider`'s account layer is answered by `_ACCOUNT_LAYER_READERS` (no vendor CLI executed)."""
    return _ACCOUNT_LAYER_READERS.get(agent_providers.account_layer(provider)) is not None


# health probe (`Capabilities.health_probe`) -> its probe; a new mechanism is a row here plus its probe.
_HEALTH_PROBES: dict[str, Callable[[Path], Optional[bool]]] = {
    "app_server": _codex_healthy,
    "code_assist": _antigravity_healthy,
    "broker_key": _pi_healthy,
}


def node_can_use_credential(provider: str, auth_dir: Path) -> Optional[bool]:
    """Can this node act on `auth_dir`'s credential for `provider`? — THE LAUNCH PREDICATE'S OWN QUESTION.

    `None` (no adapter registered) is uncertainty, never "no credential". Asked of the adapter, like
    `fleet_dispatch.local_account_dir`, and accepting the same states: `unreadable` is routine on a
    uid-split node (0600, owned by whoever refreshed) and launches fine. No token-store redirect here: a
    linked account's `auth_dir` is already its store (`collector` asks `shared_storage.credential_home`).
    Lazy import: `agents/codex.py` imports this module."""
    import agents  # noqa: PLC0415 — deferred: agents/codex.py imports this module (see the docstring)
    import agent_config_dirs  # noqa: PLC0415 — the three-state vocabulary's single home

    try:
        adapter = agents.get_adapter(provider)
    except agents.UnknownProvider:
        log.warning("provider health: no runner adapter for provider %r — %s stays unobserved",
                    provider, auth_dir)
        return None
    state = adapter.credential_state(config_dir=str(auth_dir))
    return state in (agent_config_dirs.CREDS_TOKEN, agent_config_dirs.CREDS_UNREADABLE)


def observe(provider: str, auth_dir: Path,
            securestorage_dir: str = "") -> tuple[Optional[dict], Optional[dict]]:
    """`(account, quota)` for one account — `collector._read_account_quota`'s contract, for every provider.

    A provider whose account layer has a reader (`_ACCOUNT_LAYER_READERS`: Claude Code's) is answered by it;
    every other by its own probe below.

    `account` carries the health status and, when `connected`, whatever identity the provider's reader
    found; `quota` is that reader's block or None (module docstring). A provider with no probe answers
    `(None, None)`, leaving the stored row alone (older package, newer control plane).

    A `shared_token_link` account whose credential is in the token store (`securestorage_dir` named) is
    judged on its credential file IN THE STORE alone — a no-follow shape check, no vendor CLI exec'd: a CLI
    run with its home in a mounted store would load config any mounting node can write. `auth_dir` is then the store (`collector` asks
    `shared_storage.credential_home`), and the reader is told so (`linked`)."""
    import shared_storage  # noqa: PLC0415 — deferred: a stdlib leaf, only needed on this arm

    layer_reader = _ACCOUNT_LAYER_READERS.get(agent_providers.account_layer(provider))
    if layer_reader is not None:
        return layer_reader(auth_dir, securestorage_dir)
    mechanism = agent_providers.health_probe(provider)
    probe = _HEALTH_PROBES.get(mechanism)
    if probe is None:
        log.warning("provider health: no probe for provider %r — %s stays unobserved", provider, auth_dir)
        return None, None
    row = agent_providers.by_value(provider)
    linked = bool(securestorage_dir) and row is not None and row.shared_token_link
    try:
        if linked:
            if not shared_storage.store_is_on_share(str(auth_dir), row.credential_file):
                log.warning("provider health: %s reads no_credentials — not a real directory in the token "
                            "store (a symlink sits on its path)", auth_dir)
                return {"status": STATUS_NO_CREDENTIALS}, None
            healthy = True
        else:
            healthy = probe(Path(auth_dir))
        # The second conjunct, the launch predicate's own. Inside this `try` on purpose: an adapter's raise
        # must hit the structural catch below.
        usable = node_can_use_credential(provider, Path(auth_dir))
    except Exception as exc:  # noqa: BLE001 — STRUCTURAL, and the reason is below
        # No adapter may take the collect pass down: an escaping raise would publish no observation for
        # ANY account on the node, every pass. Uncertainty is `None`, structurally.
        log.warning("provider health: the %s probe raised for %s (%s) — unobserved this pass",
                    provider, auth_dir, type(exc).__name__)
        return None, None
    # Either conjunct unsure publishes nothing. (`usable is None` is reachable only for an older package
    # meeting a newer catalogue.)
    if healthy is None or usable is None:
        return None, None
    if not (healthy and usable):
        return {"status": STATUS_NO_CREDENTIALS}, None
    account: dict = {"status": STATUS_CONNECTED}
    reader = _IDENTITY_QUOTA_READERS.get(mechanism)
    if reader is None:
        return account, None
    try:
        identity, quota = reader(Path(auth_dir), linked)
    except Exception as exc:  # noqa: BLE001 — the same structural catch: health still publishes
        log.warning("provider quota: the %s reader raised for %s (%s) — no reading this pass",
                    provider, auth_dir, type(exc).__name__)
        return account, None
    # Identity keys only when read: an absent key leaves the CP's stored value alone.
    account.update({key: value for key, value in identity.items() if value})
    return account, quota
