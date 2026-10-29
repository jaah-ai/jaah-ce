#!/usr/lib/jaah-python/bin/python3
"""`jaah` — the thin operator CLI the `.deb` installs at /usr/bin/jaah (D11; `ctl.py` stays separate).

    jaah auth login <uuid> --with-token [--no-verify] [--no-restart]
                                                             enrol this node (token on STDIN)
    jaah auth status                                       what credential this node holds
    jaah node status                                       daemon / package / identity one-pager
    jaah node converge-runner                              name the runner tmux unit after NODE_NAME
    jaah tmux [list | <session>[:<window>]] [-r]           attach the node's runner/console tmux
    jaah nodevault fingerprint                             this node's NodeVault identity fingerprint
    jaah nodevault keys                                    the NodeVault keys this node holds, by hash

Installed alone at /usr/bin, so it must not import from apps.jaahd — stdlib only. It therefore carries
sanctioned copies (`_dotenv_kv`, `node_session`, `_SESSION_NAME_RE`, and `environment.py`'s node-env path,
CP URL, credential socket and TMUX_TMPDIR default), each pointing at its single home and pinned by tests
(`tests/test_environment.py`, `tests/test_jaah_cli_tmux.py`).
The token is read from STDIN, never argv (D12: a secret on argv lands in `ps` and shell history).
/etc/jaah/node.env (0640 root:jaahd: NODE_ID, NODE_NAME, NODE_TOKEN) is written by `auth login`;
/etc/jaah/jaahd.env by provisioning. `JAAH_NODE_ENV_FILE`/`JAAH_JAAHD_ENV_FILE` override both
(the test seam). Exit codes: 0 ok, 1 failure, 2 usage.
"""
from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

try:
    import grp
    import pwd
except ImportError:  # Windows: only the tmux and vnode verbs read them, and those are Linux/macOS only.
    grp = pwd = None

# Sanctioned copy of `environment.NODE_ENV_FILE_DEFAULT` (module docstring). Only the roots are literals;
# the files derive from them (`tests/test_environment_vnode.py`'s literal scan).
_ETC_ROOT = "/etc/jaah"
_RUN_ROOT = "/run/jaah"
_NODE_ENV_DEFAULT = f"{_ETC_ROOT}/node.env"
# Sanctioned copy of `environment.credential_socket()` (POSIX): the daemon's local vocabulary socket.
_CRED_SOCKET_DEFAULT = f"{_RUN_ROOT}/creds/sock"
_CRED_SOCKET_ENV = "JAAH_CRED_SOCKET"
# Sanctioned copy of `host_platform.windows`' `creds_socket`: on Windows the endpoint is a named pipe.
_CRED_PIPE_WINDOWS = "\\\\.\\pipe\\jaah-creds"
# A relocated vnode's runtime root, which moves the socket with it (`environment.run_root`).
_RUN_ROOT_ENV = "JAAH_RUN_ROOT"
_SOCKET_TIMEOUT_S = 10.0
_SOCKET_REPLY_CAP = 256 * 1024
_JAAHD_ENV_DEFAULT = f"{_ETC_ROOT}/jaahd.env"

# The verification probe: an authenticated, read-only, parameterless GET behind the same `_check_token`
# gate every `/api/agent/*` route uses.
_VERIFY_PATH = "/api/agent/nodes/liveness"

# The CP resolves node.env's uuid to `nodes.name` over the uuid-only door. BAKED into node.env
# rather than asked at use time: tmux session resolution is on the hot path of every runner launch, and an
# unreachable CP must not take the name away from a working node.
_IDENTITY_PATH = "/api/agent/nodes/identity"
# A sanity bound on a two-field answer, not a tuning knob.
_IDENTITY_BODY_CAP = 64 * 1024

# The two-step credential rotation (`apps/api/app/node_credentials.py::rotate`): the first is presented the
# current token and answers the next one, the second is presented the NEW token and retires the old.
_ROTATE_PATH = "/api/agent/token/rotate"
_ROTATE_CONFIRM_PATH = "/api/agent/token/rotate/confirm"
# Sanctioned copy of `apps/api/app/node_credentials.py::TOKEN_PREFIX` (a parity test holds the two equal):
# a rotated token must look like a minted one before it is written into node.env.
_TOKEN_PREFIX = "snt_"
# A confirm that got no answer is retried this often, this far apart, before the run gives up.
_CONFIRM_TRIES = 3
_CONFIRM_RETRY_S = 2.0
# The package channel's HTTP Basic carries the node token too (`node_bootstrap.py` § the apt channel; dnf's repo file
# on Amazon Linux, `DNF_REPO_FILE`); a rotation that left it behind would 401 every `apt-get update` / dnf run.
_APT_AUTH_FILES = ("/etc/apt/auth.conf.d/jaah.conf", "/etc/yum.repos.d/jaah.repo")
_HTTP_TIMEOUT_S = 5.0

# `is-active` answers from local state; `try-restart` waits for stop + start, so it gets more.
_SYSTEMCTL_TIMEOUT_S = 10.0
_RESTART_TIMEOUT_S = 60.0

# node.env's owner/group/mode — 0640 root:<daemon group> so only root writes it and only the daemon reads
# it. The daemon's group is `jaahd` on Linux and `_jaahd` on macOS (created by the installer,
# `apps/api/app/node_bootstrap.py`); `sys.platform` because this file imports nothing from the daemon.
_NODE_ENV_MODE = 0o640
_NODE_ENV_GROUP = "_jaahd" if sys.platform == "darwin" else "jaahd"


def _err(msg: str) -> None:
    print(f"jaah: {msg}", file=sys.stderr)


# ── file locations + dotenv reading ─────────────────────────────────────────────────────────────────

def _env_get(name: str, environ=None) -> str | None:
    """Inline twin of `apps/jaahd/jaah_env.py::env_get` (this file imports nothing from the daemon)."""
    env = os.environ if environ is None else environ
    return env.get(name)


def node_env_path() -> str:
    """`$JAAH_NODE_ENV_FILE` when set, else /etc/jaah/node.env. Sanctioned copy of
    `environment.node_env_path`."""
    return _env_get("JAAH_NODE_ENV_FILE") or _NODE_ENV_DEFAULT


def jaahd_env_path() -> str:
    """`$JAAH_JAAHD_ENV_FILE` when set, else /etc/jaah/jaahd.env (the test seam)."""
    return _env_get("JAAH_JAAHD_ENV_FILE") or _JAAHD_ENV_DEFAULT


def _dotenv_kv(raw: str) -> tuple[str, str] | None:
    """One dotenv line → `(key, value)`, or None for blank/comment/no-`=`. Sanctioned copy of
    `env_helpers._dotenv_kv`: `export ` ignored, ` #` comment trimmed, one layer of quotes stripped."""
    line = raw.strip()
    if not line or line.startswith("#") or "=" not in line:
        return None
    if line.startswith("export "):
        line = line[len("export "):]
    key, _, val = line.partition("=")
    key = key.strip()
    val = val.split(" #", 1)[0].strip() if " #" in val else val.strip()
    if len(val) >= 2 and val[0] == val[-1] and val[0] in ("'", '"'):
        val = val[1:-1]
    return (key, val) if key else None


def _read_env_file(path: str) -> dict[str, str]:
    """All KEY=VALUE pairs in a dotenv file; `{}` when it does not exist. An UNREADABLE file raises —
    absence and permission-denied must not be conflated."""
    try:
        with open(path, encoding="utf-8") as fh:
            raw_lines = fh.read().splitlines()
    except FileNotFoundError:
        return {}
    pairs = {}
    for raw in raw_lines:
        kv = _dotenv_kv(raw)
        if kv is not None:
            pairs[kv[0]] = kv[1]
    return pairs


def _node_env_label(key: str) -> str:
    """One node.env value used as a SESSION-NAME LABEL, or "" when unreadable or not a legal session name.

    Unreadable is benign here: a label only adds work (carry a name over, retire an outgoing instance) the
    next converge will do, whereas raising could fail a credential write or abort a half-done converge.
    Shape-checked against `_SESSION_NAME_RE` because the value becomes a tmux target, a systemd instance
    name and a written `NODE_NAME=` line."""
    try:
        value = _read_env_file(node_env_path()).get(key, "").strip()
    except OSError:
        return ""
    return value if _SESSION_NAME_RE.match(value) else ""


def cp_base_url() -> str:
    """The control-plane base URL: ambient `$JAAH_CP_BASE_URL`, else `jaahd.env`'s, else "".

    No localhost fallback — a guessed CP is how a credential gets verified against the wrong one. Sanctioned
    copy of `environment.control_plane_base_explicit()`."""
    ambient = (_env_get("JAAH_CP_BASE_URL") or "").strip()
    if ambient:
        return ambient
    return (_env_get("JAAH_CP_BASE_URL", _read_env_file(jaahd_env_path())) or "").strip()


# ── credential verification ─────────────────────────────────────────────────────────────────────────

# TLS is the rule; loopback is the one exemption (a local `./dc` stack has no certificate).
_PLAINTEXT_OK_HOSTS = ("127.0.0.1", "localhost", "::1")


def _unsafe_for_the_credential(base_url: str) -> str:
    """Why BASE_URL must not be sent this node's `NODE_TOKEN`, or "" when it may.

    One home for both places the credential leaves the node (`verify_token`, `resolve_node_name`, the latter
    unattended on every install). Neither TLS nor loopback is refused, never upgraded — rewriting to
    `https` would send the token to a host nobody declared. `--no-verify` is the pre-CP escape."""
    parts = urllib.parse.urlsplit(base_url)
    if parts.scheme == "https":
        return ""
    if parts.scheme == "http" and (parts.hostname or "") in _PLAINTEXT_OK_HOSTS:
        return ""
    return (f"refusing to send this node's credential to {base_url!r} - it is neither https nor "
            f"loopback (fix JAAH_CP_BASE_URL in {jaahd_env_path()})")


def verify_token(base_url: str, token: str) -> tuple[str, str]:
    """Check TOKEN against the control plane: `("ok" | "rejected" | "error", detail)`.

    Only an explicit 401 is "rejected"; every transport failure or other status (including the CP's 503) is
    a don't-know, or a network hiccup would refuse a valid credential."""
    unsafe = _unsafe_for_the_credential(base_url)
    if unsafe:
        # "error", not "rejected": the CP never saw the credential.
        return "error", unsafe
    url = base_url.rstrip("/") + _VERIFY_PATH
    req = urllib.request.Request(url, headers={"X-Agent-Token": token})
    try:
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT_S) as resp:
            status = resp.status
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            return "rejected", f"the control plane at {base_url} rejected this token (HTTP 401)"
        return "error", f"unexpected HTTP {exc.code} from {url}: {exc.reason}"
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        reason = getattr(exc, "reason", None) or exc
        return "error", f"control plane unreachable at {url}: {reason}"
    if 200 <= status < 300:
        return "ok", ""
    return "error", f"unexpected HTTP {status} from {url}"


def resolve_node_name(base_url: str, token: str, node_id: str) -> tuple[str, str]:
    """This node's `nodes.name` per the control plane: `(name, "")`, or `("", why not)`.

    Asked, never guessed from argv or hostname — the CP row is the authority. One failure outcome (the
    caller keeps what node.env holds either way), and it never raises: it must not fail a login whose
    credential is already verified."""
    if not base_url:
        return "", "JAAH_CP_BASE_URL is not set"
    unsafe = _unsafe_for_the_credential(base_url)
    if unsafe:
        return "", unsafe
    url = base_url.rstrip("/") + _IDENTITY_PATH + "?" + urllib.parse.urlencode({"node_id": node_id})
    req = urllib.request.Request(url, headers={"X-Agent-Token": token})
    try:
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT_S) as resp:
            # CAPPED: the timeout bounds time, not size, and this runs as root inside `postinst`, where an
            # unbounded read of a hostile CP's answer could OOM a live apt transaction.
            body = json.loads(resp.read(_IDENTITY_BODY_CAP).decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return "", (f"the control plane at {base_url} does not know node {node_id!r} - register it "
                        f"in the console first, and check NODE_ID is this node's uuid")
        return "", f"unexpected HTTP {exc.code} from {url}: {exc.reason}"
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        reason = getattr(exc, "reason", None) or exc
        return "", f"control plane unreachable at {url}: {reason}"
    except (ValueError, http.client.HTTPException) as exc:
        # `http.client` by name: `BadStatusLine`/`LineTooLong` subclass neither `ValueError` nor `OSError`.
        return "", f"malformed answer from {url}: {exc}"
    if not isinstance(body, dict) or body.get("id") != node_id:
        # The answer must name the row we asked about, or a CP/cache answering for another row would have
        # this node adopt a neighbour's name and rename its live session onto it.
        answered = body.get("id") if isinstance(body, dict) else body
        return "", (f"the control plane answered about {answered!r}, not this node - refusing to take "
                    f"a name from it")
    name = body.get("name", "")
    if not isinstance(name, str) or not _SESSION_NAME_RE.match(name):
        # Refused rather than sanitized (`env_helpers.default_tmux_session`): a mangling would have to be
        # reproduced identically by the daemon and `postinst`.
        return "", (f"the control plane calls this node {name!r}, which cannot be a tmux session name "
                    f"- rename it in the console")
    return name, ""


# ── node.env writing ────────────────────────────────────────────────────────────────────────────────

def _write_node_env(path: str, node_id: str, token: str, name: str = "") -> None:
    """Write PATH with exactly `NODE_ID=`, `NODE_NAME=` and `NODE_TOKEN=`, atomically, mode 0640.

    Temp file + `os.replace`, mode set BEFORE the rename so the path never exists wider. root:jaahd when
    root; chown skipped under the non-root test seam. An unknown name is OMITTED, never written empty:
    `env_helpers.default_tmux_session` distinguishes "no name yet" from "named the empty string"."""
    dirname = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(dir=dirname, prefix=".node.env.")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            name_line = f"NODE_NAME={name}\n" if name else ""
            fh.write(f"NODE_ID={node_id}\n{name_line}NODE_TOKEN={token}\n")
        os.chmod(tmp, _NODE_ENV_MODE)
        # Windows has no owners to set: the file inherits its directory's access list (the installer's,
        # `apps/api/app/node_bootstrap.py::render_windows_body`), and `os.replace` keeps that.
        if hasattr(os, "geteuid") and os.geteuid() == 0:
            # LookupError (no jaahd group) propagates: the package's sysusers step did not run.
            shutil.chown(tmp, user="root", group=_NODE_ENV_GROUP)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ── picking the new credential up ───────────────────────────────────────────────────────────────────

def _reload_daemon_credential() -> str:
    """Restart a RUNNING jaahd so it re-reads node.env, and return the one line to report.

    systemd reads `EnvironmentFile=` only at unit START. `try-restart`, not `restart`: `auth login` must
    never start a daemon an operator deliberately stopped. Every failure is reported, never raised — the
    credential is already written."""
    if not os.path.isdir("/run/systemd/system"):
        return "systemd is not running - restart jaahd yourself to pick the credential up"
    try:
        if subprocess.run(["systemctl", "is-active", "--quiet", "jaahd.service"],
                          timeout=_SYSTEMCTL_TIMEOUT_S).returncode != 0:
            return "jaahd is not running - start it with `systemctl start jaahd`"
        proc = subprocess.run(["systemctl", "try-restart", "jaahd.service"],
                              capture_output=True, text=True, timeout=_RESTART_TIMEOUT_S)
    except FileNotFoundError:
        return "systemctl not found - restart jaahd yourself to pick the credential up"
    except subprocess.TimeoutExpired:
        return "systemctl timed out - restart jaahd yourself with `systemctl restart jaahd`"
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip().splitlines()
        return (f"restarting jaahd failed (exit {proc.returncode}"
                f"{': ' + detail[0] if detail else ''}) - retry with `systemctl restart jaahd`")
    return "restarted jaahd - it is now serving with this credential"


# ── verbs ───────────────────────────────────────────────────────────────────────────────────────────

def cmd_auth_login(args: argparse.Namespace) -> int:
    path = node_env_path()
    if path == _NODE_ENV_DEFAULT and os.geteuid() != 0:
        _err(f"auth login writes {path} (0640 root:{_NODE_ENV_GROUP}) and must run as root - "
             "re-run with sudo")
        return 1
    if not _SESSION_NAME_RE.match(args.node_id):
        # A newline would inject a key into the `EnvironmentFile` (a second `NODE_TOKEN=`), and the id may
        # name a tmux session, so it is held to the session-name shape (a uuid passes).
        _err(f"{args.node_id!r} cannot be a node id - expected the uuid the console shows for this node "
             f"(letters, digits, `_` and `-` only, starting alphanumeric, at most 64 characters)")
        return 1
    token = sys.stdin.readline().strip()
    if not token:
        _err("no token on stdin - pipe the credential in, e.g. "
             f"`jaah auth login {args.node_id} --with-token < token-file`")
        return 1
    name, name_detail = "", ""
    if args.no_verify:
        verified = "unverified (--no-verify)"
        # This run asks nothing; `converge-runner` below still tries, so the report says what THIS step did.
        name_detail = "not asked for (--no-verify)"
    else:
        base = cp_base_url()
        if not base:
            _err(f"cannot verify: JAAH_CP_BASE_URL is not set (looked in the environment and "
                 f"{jaahd_env_path()}) - provision jaahd.env first, or use --no-verify")
            return 1
        state, detail = verify_token(base, token)
        if state == "rejected":
            _err(f"refusing to write {path}: {detail}")
            return 1
        if state == "error":
            _err(f"refusing to write {path}: could not verify the credential - {detail} "
                 "(use --no-verify to write anyway)")
            return 1
        verified = f"verified against {base}"
        # The node learns its name with the token check. A failure never fails the login;
        # `converge-runner` refreshes it later.
        name, name_detail = resolve_node_name(base, token, args.node_id)
    # A name this run could not resolve is KEPT: a briefly unreachable CP must not rename a live session
    # back to its uuid.
    kept = "" if name else _node_env_label("NODE_NAME")
    _write_node_env(path, args.node_id, token, name or kept)
    print(f"logged in as {args.node_id}: wrote {path} ({verified})")
    if name:
        print(f"node name: {name} - this node's tmux session and runner instance are named after it")
    elif kept:
        print(f"node name: kept '{kept}' - not refreshed ({name_detail})")
    else:
        print(f"node name: unresolved ({name_detail}) - this node's tmux session keeps its uuid name "
              f"until `jaah node converge-runner` can read the name from the control plane")
    # Only for the REAL path: under the fixture seam there is no daemon this credential belongs to.
    if path == _NODE_ENV_DEFAULT:
        # Re-point the unit that seeds the (name-named) session before the restart, or the daemon targets
        # a session no unit keeps. Reported, never fatal.
        cmd_node_converge_runner(args)
        if args.no_restart:
            print("daemon: not restarted (--no-restart) - jaahd reads node.env only at start")
        else:
            print(f"daemon: {_reload_daemon_credential()}")
    return 0


def _post_with_token(base_url: str, path: str, token: str) -> tuple[int, bytes, str]:
    """POST an empty body to PATH under TOKEN: `(status, body, "")`, or `(0, b"", why no answer came)`.

    An HTTP error status is an ANSWER (returned, never raised); only a transport failure is a don't-know."""
    unsafe = _unsafe_for_the_credential(base_url)
    if unsafe:
        return 0, b"", unsafe
    url = base_url.rstrip("/") + path
    req = urllib.request.Request(url, data=b"", method="POST", headers={"X-Agent-Token": token})
    try:
        with urllib.request.urlopen(req, timeout=_HTTP_TIMEOUT_S) as resp:
            return resp.status, resp.read(_IDENTITY_BODY_CAP), ""
    except urllib.error.HTTPError as exc:
        return exc.code, b"", ""
    except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException) as exc:
        reason = getattr(exc, "reason", None) or exc
        return 0, b"", f"control plane unreachable at {url}: {reason}"


def _sync_apt_auth(token: str) -> list[str]:
    """Make TOKEN the password of every node token in this node's package channel auth files (apt's and dnf's,
    `_APT_AUTH_FILES`); the files changed, by path.

    Any node-token-shaped password, not only the previous one: a run killed between node.env and here must be
    finished by the re-run, which no longer knows that value. Same file, same owner and mode, atomically (temp +
    `os.replace`)."""
    changed = []
    for path in _APT_AUTH_FILES:
        try:
            with open(path, encoding="utf-8") as fh:
                text = fh.read()
        except FileNotFoundError:
            continue
        # apt's netrc spells it `password <token>`, dnf's ini `password=<token>`.
        rewritten = re.sub(rf"(\bpassword(?:\s+|\s*=\s*)){re.escape(_TOKEN_PREFIX)}\S+",
                           lambda m: m.group(1) + token, text)
        if rewritten == text:
            continue
        st = os.stat(path)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".jaah-auth.")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(rewritten)
            os.chmod(tmp, stat.S_IMODE(st.st_mode))
            if hasattr(os, "geteuid") and os.geteuid() == 0:
                os.chown(tmp, st.st_uid, st.st_gid)
            os.replace(tmp, path)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        changed.append(path)
    return changed


def cmd_auth_rotate(args: argparse.Namespace) -> int:
    """`_rotate` under an exclusive lock on node.env's directory, where there is `fcntl` (not Windows: one
    installer, nothing else rotates). Two runs on one node would otherwise mint over each other's pending token."""
    try:
        import fcntl
    except ImportError:
        return _rotate(args)
    path = node_env_path()
    if path == _NODE_ENV_DEFAULT and os.geteuid() != 0:
        _err(f"auth rotate rewrites {path} (0640 root:{_NODE_ENV_GROUP}) and must run as root - re-run with sudo")
        return 1
    fd = os.open(os.path.dirname(path) or ".", os.O_RDONLY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        return _rotate(args)
    finally:
        os.close(fd)  # releases the lock


def _confirm_rotation(base: str, token: str) -> tuple[int, str]:
    """`/token/rotate/confirm` under TOKEN, retried briefly when no answer came: `(status, why not)`."""
    for attempt in range(_CONFIRM_TRIES):
        status, _, detail = _post_with_token(base, _ROTATE_CONFIRM_PATH, token)
        if status:
            return status, detail
        if attempt + 1 < _CONFIRM_TRIES:
            time.sleep(_CONFIRM_RETRY_S)
    return status, detail


def _write_credential(path: str, node_id: str, token: str, name: str) -> None:
    """node.env, then the apt channel's auth (`_sync_apt_auth`) — only for the real path."""
    _write_node_env(path, node_id, token, name)
    if path == _NODE_ENV_DEFAULT:
        for apt_auth in _sync_apt_auth(token):
            print(f"apt channel: {apt_auth} updated")


def _rotate(args: argparse.Namespace) -> int:
    """Trade this node's token for a fresh one only node.env holds: each installer's last step, so the token
    that travelled in the bootstrap body (or the IMDS-readable user-data) dies once the install has worked.

    rotate (current token) -> write node.env and the apt channel's auth -> confirm (NEW token) -> check the
    previous one is refused -> restart jaahd. Every exit leaves node.env holding a token the CP accepts: the old
    one until the write, the new one (pending, then confirmed) after it, and the old one again when the confirm
    is refused (the pending one was superseded). Re-run to finish."""
    path = node_env_path()
    if path == _NODE_ENV_DEFAULT and os.geteuid() != 0:
        _err(f"auth rotate rewrites {path} (0640 root:{_NODE_ENV_GROUP}) and must run as root - re-run with sudo")
        return 1
    pairs = _read_env_file(path)
    node_id, current, name = pairs.get("NODE_ID", ""), pairs.get("NODE_TOKEN", ""), pairs.get("NODE_NAME", "")
    if not node_id or not current:
        _err(f"nothing to rotate: {path} holds no NODE_ID and NODE_TOKEN (run `jaah auth login` first)")
        return 1
    base = cp_base_url()
    if not base:
        _err(f"cannot rotate: JAAH_CP_BASE_URL is not set (looked in the environment and {jaahd_env_path()})")
        return 1

    status, body, detail = _post_with_token(base, _ROTATE_PATH, current)
    previous = current
    if status == 409:
        # node.env already holds a rotated token a lost confirm never retired: confirm that one, and finish the
        # apt half a killed run may have left behind.
        print("rotation: node.env already holds the rotated credential - confirming it")
        if path == _NODE_ENV_DEFAULT:
            _sync_apt_auth(current)
    elif status == 200:
        try:
            rotated = json.loads(body.decode("utf-8"))["token"]
        except (ValueError, KeyError, TypeError):
            rotated = None
        # The same shape the CP mints (`TOKEN_PREFIX` + urlsafe base64): anything else could inject a node.env key.
        if not isinstance(rotated, str) or not re.fullmatch(re.escape(_TOKEN_PREFIX) + r"[A-Za-z0-9_-]{20,}", rotated):
            _err(f"refusing the rotation: {base} answered no usable credential - this node keeps its current one")
            return 1
        _write_credential(path, node_id, rotated, name)
        current = rotated
    elif status == 401:
        _err(f"the control plane at {base} rejected this node's credential (HTTP 401) - nothing rotated")
        return 1
    else:
        _err(f"could not rotate - {detail or f'unexpected HTTP {status} from {base}{_ROTATE_PATH}'} "
             "(this node keeps its current credential; re-run `jaah auth rotate`)")
        return 1

    status, detail = _confirm_rotation(base, current)
    if status == 401 and previous != current:
        # Superseded before the confirm (another rotate under the old token): the new one is dead, the old one is not.
        _write_credential(path, node_id, previous, name)
        _err(f"the control plane refused the rotated credential (HTTP 401) - restored the previous one to {path}; "
             "re-run `jaah auth rotate`")
        return 1
    if status == 401:
        _err(f"the control plane at {base} refused this node's credential (HTTP 401) - re-issue it in the console")
        return 1
    if status != 204:
        _err(f"wrote the rotated credential to {path}, but the confirm got no answer "
             f"({detail or f'HTTP {status} from {base}{_ROTATE_CONFIRM_PATH}'}) - re-run `jaah auth rotate` to finish")
        return 1
    revoked = "the previous token is revoked"
    if previous != current:
        state, check = verify_token(base, previous)
        if state == "ok":
            _err(f"the control plane at {base} still accepts the previous token after the confirm")
            return 1
        if state == "error":
            revoked = f"the previous token's refusal is unchecked ({check})"
    print(f"rotated the credential of {node_id}: wrote {path}; {revoked}")
    # As `auth login`: only for the REAL path, and only where systemd seeds the daemon's environment from
    # node.env. A macOS or Windows installer restarts its own service.
    if path == _NODE_ENV_DEFAULT and sys.platform.startswith("linux"):
        if args.no_restart:
            print("daemon: not restarted (--no-restart) - jaahd reads node.env only at start")
        else:
            print(f"daemon: {_reload_daemon_credential()}")
    return 0


def cmd_auth_status(_args: argparse.Namespace) -> int:
    path = node_env_path()
    pairs = _read_env_file(path)
    if not pairs and not os.path.exists(path):
        print(f"not logged in: {path} does not exist (run `jaah auth login <uuid> --with-token`)")
        return 1
    node_id = pairs.get("NODE_ID", "")
    token = pairs.get("NODE_TOKEN", "")
    print(f"node.env:   {path}")
    print(f"NODE_ID:    {node_id or '(missing)'}")
    # The human half of the identity pair; `converge-runner` writes it.
    print(f"NODE_NAME:  {pairs.get('NODE_NAME') or '(missing - run `jaah node converge-runner`)'}")
    # The token VALUE never reaches stdout - not even a prefix; presence is the whole report.
    print(f"NODE_TOKEN: {'present' if token else 'missing or empty'}")
    if not token:
        return 1
    base = cp_base_url()
    if not base:
        print("credential: unverified (JAAH_CP_BASE_URL not set)")
        return 0
    state, detail = verify_token(base, token)
    if state == "ok":
        print(f"credential: verifies against {base}")
        return 0
    if state == "rejected":
        print(f"credential: REJECTED - {detail}")
        return 1
    # Offline is a don't-know, not a failure: the credential may be fine (degrade gracefully).
    print(f"credential: unverified ({detail})")
    return 0


def _run_line(argv: list[str]) -> str:
    """First stdout line of ARGV, or a human-readable reason there is none; a missing binary is named."""
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=10)
    except FileNotFoundError:
        return f"unknown ({argv[0]} not found)"
    except subprocess.TimeoutExpired:
        return f"unknown ({argv[0]} timed out)"
    out = proc.stdout.strip().splitlines()
    if out:
        return out[0]
    # e.g. a query for something absent: rc 1, the reason on stderr.
    err = proc.stderr.strip().splitlines()
    return f"unknown (exit {proc.returncode}{': ' + err[0] if err else ''})"


# `infra/jaah/pkgmgr.py`, the one home of "apt or dnf". This file is installed ALONE at /usr/bin on Linux, so it
# loads it from the bundle by absolute path; beside it on Windows and in the dev tree (the relative layout).
_PKGMGR_PATHS = (Path(__file__).resolve().parents[2] / "infra" / "jaah" / "pkgmgr.py",
                 Path("/usr/lib/jaahd/infra/jaah/pkgmgr.py"))


def _installed_jaah_version() -> str:
    """The installed `jaah` package version (apt or dnf), or a human-readable reason there is none."""
    path = next((p for p in _PKGMGR_PATHS if p.is_file()), None)
    if path is None:
        return "unknown (no infra/jaah/pkgmgr.py: the bundle is broken)"
    import importlib.util  # noqa: PLC0415 — only `node status` pays for it
    spec = importlib.util.spec_from_file_location("jaah_pkgmgr", path)
    if spec is None or spec.loader is None:
        return f"unknown ({path} is not loadable)"
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    if module.kind() is None:
        return "unknown (no apt or dnf on this host)"
    return module.installed_version("jaah") or "unknown (not installed)"


def cmd_node_status(_args: argparse.Namespace) -> int:
    pairs = _read_env_file(node_env_path())
    print(f"jaahd:  {_run_line(['systemctl', 'is-active', 'jaahd'])}"
          f" ({_run_line(['systemctl', 'show', 'jaahd', '--property=SubState', '--value'])})")
    print(f"package:  jaah {_installed_jaah_version()}")
    print(f"NODE_ID:  {pairs.get('NODE_ID') or '(not logged in)'}")
    # No space after the colon: `NODE_NAME:` is already the ten-column label width.
    print(f"NODE_NAME:{pairs.get('NODE_NAME') or '(missing)'}")
    print(f"CP URL:   {cp_base_url() or '(unset)'}")
    return 0


# ── tmux: attaching to this node's runner + console sessions ────────────────────────────────────────
#
# The sessions belong to the SYSTEM users (`jaahd`, `jaah`), and their sockets sit under the daemon's
# TMUX_TMPDIR (2770 jaahd:jaah), which the operator cannot traverse — so tmux runs AS the owner.
# Always `-S <socket>`: tmux ignores TMUX_TMPDIR inside a session and would hit the caller's own server
# (a guard hook denies the shape).

# Every uid that can own a fleet tmux server. A `pwd` miss (package not installed) is a non-answer.
_SESSION_OWNERS = ("jaahd", "jaah")
# Fallback only; the `jaahd.service` unit is the authority. Sanctioned copy of
# `environment.TMUX_TMPDIR_DEFAULT`.
_TMUX_TMPDIR_FALLBACK = f"{_RUN_ROOT}/tmux"
# A node's tmux session is named after its NAME. Sanctioned copy of
# `env_helpers.default_tmux_session` and its `SESSION_NAME_RE`, pinned by `tests/test_jaah_cli_tmux.py`.
# The node's uuid (computed per node) is a retired name kept only to feed `_rename_outgoing_sessions`.
_SESSION_NAME_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")
# A wedged tmux server must not hang the CLI.
_TMUX_TIMEOUT_S = 10.0


def node_session() -> str | None:
    """The tmux session name THIS node's daemon targets: its NODE_NAME, or `None` when that is not
    knowable from here.

    Same answer as `env_helpers.default_tmux_session()`. node.env first (the operator is not the daemon),
    then the environment (a runner pane, the test seam); falls back to `NODE_ID` exactly as the daemon's
    resolver does. `None`, never a guess: node.env is 0640, so a plain login shell usually cannot read it,
    and `cmd_tmux` then asks the live tmux server. An illegal name is `None`, not sanitized."""
    try:
        assigned = _read_env_file(node_env_path())
    except OSError:
        assigned = {}
    for key in ("NODE_NAME", "NODE_ID"):
        value = (assigned.get(key, "") or os.environ.get(key, "")).strip()
        if _SESSION_NAME_RE.match(value):
            return value
    return None


def tmux_tmpdir() -> str:
    """The daemon's OWN TMUX_TMPDIR, asked of the running unit (the `jaahd.service` unit is the single
    home), falling back to the default."""
    line = _run_line(["systemctl", "show", "jaahd", "--property=Environment", "--value"])
    for token in line.split():
        if token.startswith("TMUX_TMPDIR="):
            return token.split("=", 1)[1]
    return _TMUX_TMPDIR_FALLBACK


def _owner_record(owner: str) -> tuple[int, str] | None:
    """(uid, home) for OWNER, or None when the user does not exist on this host."""
    try:
        entry = pwd.getpwnam(owner)
    except KeyError:
        return None
    return entry.pw_uid, entry.pw_dir


def _as_owner(owner: str, uid: int, home: str, argv: list[str]) -> list[str]:
    """ARGV, wrapped so it runs as OWNER with OWNER's HOME.

    The owner itself needs no wrapper; root uses `runuser` (works without sudo); anyone else `sudo`. The
    `env` prefix matters: sudo's `env_reset` would hand tmux the CALLER's HOME, and $TMUX must be cleared
    or a caller inside tmux gets a refusal."""
    cmd = ["env", "-u", "TMUX", f"HOME={home}",
           f"TERM={os.environ.get('TERM') or 'xterm-256color'}", *argv]
    if os.geteuid() == uid:
        return cmd
    if os.geteuid() == 0:
        return ["runuser", "-u", owner, "--", *cmd]
    return ["sudo", "-u", owner, "--", *cmd]


# Emits OWNER's socket paths, globbed BY THE SHELL RUNNING AS OWNER (the caller usually cannot read these
# directories). `$1` quoted, `*` not; trailing `exit 0` so a final failed `[ -S ]` is not a failure.
_SOCKET_PROBE = (
    'for s in "$1"/tmux-"$(id -u)"/*; do [ -S "$s" ] && echo "$s"; done\n'
    '[ -n "$2" ] && for s in "$2"/run/tmux-*; do [ -S "$s" ] && echo "$s"; done\n'
    'exit 0\n'
)


def _sockets_for(owner: str, uid: int, home: str, tmpdir: str, jaah_home: str) -> list[str]:
    """Live tmux sockets owned by OWNER: `tmux-<uid>/` under the daemon's TMUX_TMPDIR, plus any `-S`
    socket under $JAAH_HOME/run (the console's adopt-server)."""
    argv = _as_owner(owner, uid, home,
                     ["sh", "-c", _SOCKET_PROBE, "jaah-tmux", tmpdir, jaah_home])
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=_TMUX_TIMEOUT_S)
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return []
    return [line for line in proc.stdout.splitlines() if line]


def _session_owners() -> list[tuple[str, int, str]]:
    """(owner, uid, home) for each session-owning user that exists on this host, daemon first."""
    found = []
    for owner in _SESSION_OWNERS:
        record = _owner_record(owner)
        if record is not None:
            found.append((owner, *record))
    return found


def _preauthorize(owners) -> None:
    """Take sudo's password prompt ONCE, here, on the operator's own terminal.

    The privileged calls below capture stderr, where sudo prompts, so the terminal would look hung and the
    probe report "no sockets". `sudo -v` refreshes the cached credential and runs nothing."""
    if os.geteuid() == 0 or os.geteuid() in {uid for _, uid, _ in owners}:
        return
    if not shutil.which("sudo"):
        return  # nothing to pre-authorize; the calls below will fail with their own named error
    try:
        # No capture: prompt and typing must reach the terminal. Bounded so a lost tty cannot wedge it.
        subprocess.run(["sudo", "-v"], timeout=120)
    except (OSError, subprocess.TimeoutExpired):
        pass


def _discover_sockets(owners) -> list[tuple[str, int, str, str]]:
    """Every (owner, uid, home, socket) on this node, daemon first."""
    tmpdir = tmux_tmpdir()
    jaah_home = _env_get("JAAH_HOME", _read_env_file(jaahd_env_path())) or ""
    found = []
    for owner, uid, home in owners:
        for socket in _sockets_for(owner, uid, home, tmpdir, jaah_home):
            found.append((owner, uid, home, socket))
    return found


def _tmux_capture(owner: str, uid: int, home: str, socket: str, args: list[str]) -> tuple[int, str]:
    """(returncode, stdout) of a tmux command on SOCKET, run as OWNER. `-u`: a locale-less operator shell
    is a non-UTF-8 client, and tmux would rewrite the TABs in a `-F` listing to `_` (`session_user.tmux_cmd`)."""
    argv = _as_owner(owner, uid, home, ["tmux", "-u", "-S", socket, *args])
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=_TMUX_TIMEOUT_S)
    except (FileNotFoundError, subprocess.TimeoutExpired) as exc:
        return 1, f"({exc})"
    return proc.returncode, proc.stdout


_LIST_FORMAT = ("  #{session_name}:#{window_index}  #{window_name}"
                "  [#{pane_current_path}]#{?window_active, *,}")

# Sanctioned copy of `console.EPHEMERAL_VIEW_SESSION_PREFIX` (see its `is_ephemeral_view_session`), pinned
# by `tests/test_jaah_cli_tmux.py`.
_EPHEMERAL_VIEW_SESSION_PREFIX = "_view_"

# The display line keyed by a machine-readable `#{session_name}` field, split off before printing, so the
# session name is never parsed back out of our own formatting.
_LIST_QUERY_FORMAT = "#{session_name}\t" + _LIST_FORMAT


def _visible_window_lines(out: str) -> str:
    """The operator-facing lines of a `_LIST_QUERY_FORMAT` listing, minus the ephemeral `_view_*`
    sessions (one per open browser tile)."""
    lines = []
    for line in out.splitlines():
        session, tab, display = line.partition("\t")
        if not tab:
            # Not our format: print it rather than risk blanking the operator's only listing.
            lines.append(line)
            continue
        if session.startswith(_EPHEMERAL_VIEW_SESSION_PREFIX):
            continue
        lines.append(display)
    return "\n".join(lines)


def _live_session_names(sockets) -> list[str]:
    """Every attachable session name live on `sockets`, in discovery order, deduplicated.

    The authoritative answer to "which session is this node's" when node.env is unreadable. Ephemeral
    `_view_*` sessions are excluded (they vanish with their browser tab)."""
    names: list[str] = []
    for owner, uid, home, socket in sockets:
        rc, out = _tmux_capture(owner, uid, home, socket,
                                ["list-sessions", "-F", "#{session_name}"])
        if rc != 0:
            continue
        for name in out.splitlines():
            name = name.strip()
            if name and not name.startswith(_EPHEMERAL_VIEW_SESSION_PREFIX) and name not in names:
                names.append(name)
    return names


def _attach_hint(sockets) -> str:
    """The `attach:` line under a listing — built from what is LIVE, never from a guessed name.

    One live session is named; several are all listed rather than one picked."""
    live = _live_session_names(sockets)
    if not live:
        return "attach: nothing to attach to on this node — no live tmux session"
    return f"attach: jaah tmux [{' | '.join(live)}]   read-only: jaah tmux -r"


def _cmd_tmux_list(sockets) -> int:
    for owner, uid, home, socket in sockets:
        print(f"{socket}  (user {owner})")
        rc, out = _tmux_capture(owner, uid, home, socket,
                                ["list-windows", "-a", "-F", _LIST_QUERY_FORMAT])
        visible = _visible_window_lines(out) if rc == 0 else ""
        print(visible.rstrip() if visible.strip() else "  (no sessions)")
    print(f"\n{_attach_hint(sockets)}")
    return 0


# ── converge-runner: making the SEEDED session name agree with this node's id ───────────────────────

_RUNNER_UNIT_FMT = "jaah-runner@{}.service"


def _systemctl(*args: str) -> tuple[int, str]:
    """(returncode, first line of output) of a systemctl call. A missing systemctl is a NAMED absence,
    never folded into a generic failure — same rule as `_run_line`."""
    try:
        proc = subprocess.run(["systemctl", *args], capture_output=True, text=True,
                              timeout=_RESTART_TIMEOUT_S)
    except FileNotFoundError:
        return 1, "systemctl not found"
    except subprocess.TimeoutExpired:
        return 1, "systemctl timed out"
    out = (proc.stdout or proc.stderr).strip().splitlines()
    return proc.returncode, out[0] if out else ""


def _rename_outgoing_sessions(session: str, outgoing: list[str]) -> list[str]:
    """Rename any live session on this node's tmux servers that still carries an OUTGOING name to
    SESSION, in place.

    A rename, not a re-create: its windows are live runners and consoles, and `rename-session` keeps panes,
    pids, scrollback and grouped `_view_*` sessions. The caller passes the outgoing names. A server that
    already has SESSION is skipped and reported — an operator decides, never a silent kill."""
    lines = []
    owners = _session_owners()
    if not owners:
        return ["  tmux: no session-owning user on this host — nothing to rename"]
    for owner, uid, home, socket in _discover_sockets(owners):
        for old in outgoing:
            if _tmux_capture(owner, uid, home, socket, ["has-session", "-t", f"={old}"])[0] != 0:
                continue
            if _tmux_capture(owner, uid, home, socket, ["has-session", "-t", f"={session}"])[0] == 0:
                lines.append(f"  tmux: {socket} has BOTH '{old}' and '{session}' — left as "
                             f"is; move the windows you want with `tmux move-window` and kill the "
                             f"empty one")
                continue
            rc, out = _tmux_capture(owner, uid, home, socket,
                                    ["rename-session", "-t", f"={old}", session])
            lines.append(f"  tmux: renamed '{old}' -> '{session}' on {socket}" if rc == 0
                         else f"  tmux: FAILED to rename '{old}' on {socket}: "
                              f"{out.strip() or f'exit {rc}'} — its windows stay reachable as "
                              f"`jaah tmux {old}`, but the daemon now targets '{session}'")
    return lines or [f"  tmux: no live session under an outgoing name ({', '.join(outgoing)}) to rename"]


def _refresh_node_name(path: str) -> list[str]:
    """Re-read this node's name from the CP and write it into node.env when it is new — the report lines.

    Runs from `postinst` on every install/upgrade, so nodes learn (or follow a console rename of) their name
    unattended. NEVER FATAL and never touches the credential: on any failure the node keeps its name and
    session. Reads NODE_ID/NODE_TOKEN from node.env, their only home under `postinst`."""
    try:
        pairs = _read_env_file(path)
    except OSError as exc:
        # Never fatal includes an unreadable node.env: this runs after the credential is written.
        return [f"  name: not refreshed - cannot read {path}: {exc}"]
    node_id, token = pairs.get("NODE_ID", ""), pairs.get("NODE_TOKEN", "")
    current = pairs.get("NODE_NAME", "")
    if not node_id or not token:
        return [f"  name: not refreshed - {path} carries no credential yet "
                f"(run `jaah auth login <uuid> --with-token`)"]
    name, detail = resolve_node_name(cp_base_url(), token, node_id)
    if not name:
        return [f"  name: not refreshed - {detail}"
                + (f" (keeping '{current}')" if current else "")]
    if name == current:
        return [f"  name: {name} (unchanged)"]
    try:
        _write_node_env(path, node_id, token, name)
    except (OSError, LookupError) as exc:
        # Never fatal covers the write too (read-only /etc, ENOSPC, missing `jaahd` group) — inside
        # `postinst` behind `|| true` a traceback would be hidden. Nothing mutated yet; next converge retries.
        return [f"  name: not refreshed - could not write {path}: {exc}"
                + (f" (keeping '{current}')" if current else "")]
    return [f"  name: {name} - wrote it to {path}"
            + (f", replacing '{current}'" if current else " (this node had none)")]


def cmd_node_converge_runner(_args: argparse.Namespace) -> int:
    """Point `jaah-runner@.service` at the instance named by this node's NAME — the unit that seeds the
    tmux session every runner and console window lives in (the instance name IS the session name).

    Run by `postinst` and `auth login`: refresh the name, stop outgoing instances, rename their live
    sessions in place, enable the new one. Stopping BEFORE the rename is safe (`Type=exec`,
    `KillMode=process` — only the converge loop is signalled, never tmux or a pane) and required, or it
    re-creates the empty old session within 30s. `enable --now` starts a stopped instance and never
    restarts a running one, which `postinst` depends on."""
    if os.geteuid() != 0:
        _err("converge-runner enables a systemd unit and renames another user's tmux session - "
             "re-run as root")
        return 1
    # Read BEFORE the refresh overwrites it: the current name is outgoing after a console rename, and
    # missing it strands every live window in a session nothing targets.
    outgoing_name = _node_env_label("NODE_NAME")
    name_lines = _refresh_node_name(node_env_path())
    # File-only, unlike `node_session()`: this runs as root and WRITES units and renames, and an ambient
    # `NODE_NAME` from the invoking shell would converge onto a name the restarted daemon (env = node.env
    # alone) does not resolve.
    current_name = _node_env_label("NODE_NAME")
    session = current_name or _node_env_label("NODE_ID") or None
    if session is None:
        # Nothing to converge onto, so nothing is enabled — seeding a name no node targets is how stray
        # sessions start.
        for line in name_lines:
            print(line)
        _err(f"this node has no usable NODE_NAME or NODE_ID (read from {node_env_path()}), so it has no "
             f"session name to converge onto - run `jaah auth login <uuid> --with-token` first")
        return 1
    unit = _RUNNER_UNIT_FMT.format(session)
    if not os.path.isdir("/run/systemd/system"):
        _err(f"systemd is not running - nothing to converge (this node's session is '{session}')")
        return 1
    for line in name_lines:
        print(line)
    print(f"node: {session} - this node's tmux session and runner instance are named after it")
    # Every outgoing name — the pre-refresh name, the uuid. An enabled old instance re-seeds an
    # empty session every 30s; an unrenamed one strands its windows. `old != session` keeps steady state a
    # no-op.
    outgoing = [old for old in (outgoing_name, _node_env_label("NODE_ID"))
                if old and old != session]
    for old in outgoing:
        legacy = _RUNNER_UNIT_FMT.format(old)
        rc, out = _systemctl("disable", "--now", legacy)
        print(f"  systemd: retired {legacy}" if rc == 0
              else f"  systemd: could not retire {legacy}: {out or f'exit {rc}'}")
    if outgoing:
        for line in _rename_outgoing_sessions(session, outgoing):
            print(line)
    rc, out = _systemctl("enable", "--now", unit)
    if rc != 0:
        _err(f"could not enable {unit}: {out or f'exit {rc}'} - this node has no keeper for its tmux "
             f"session; runners still start (the daemon creates the session on demand) but it dies "
             f"with its last window")
        return 1
    print(f"  systemd: enabled {unit} (seeds and keeps session '{session}')")
    if current_name != outgoing_name:
        # The running daemon still holds the old name (`EnvironmentFile=` is read at start) and would
        # create an unkept session under it. Keyed on node.env's name CHANGING, not on name != session,
        # which would restart the daemon on every upgrade of an unnamed node.
        print(f"  daemon: {_reload_daemon_credential()}")
    return 0


def _current_socket() -> str:
    """The socket of the server the CALLER is already inside, or "". $TMUX is `<socket>,<pid>,<session>`."""
    return (os.environ.get("TMUX") or "").split(",")[0]


def _bare_target(sockets) -> str | None:
    """Which session a bare `jaah tmux` attaches to, or None having already said why it cannot tell.

    The node's own name first (what the daemon targets), else the live server's single session. Nothing is
    guessed when neither answers, and two live sessions are refused rather than picked."""
    assigned = node_session()
    if assigned is not None:
        return assigned
    live = _live_session_names(sockets)
    if len(live) == 1:
        return live[0]
    if not live:
        _err("this node has no live tmux session, and its name is not readable from here "
             f"({node_env_path()} is root-owned) — start a runner, or re-run under sudo")
        return None
    _err(f"this node has {len(live)} live tmux sessions ({', '.join(live)}) and its name is not "
         f"readable from here ({node_env_path()} is root-owned) — name the one you want: "
         f"`jaah tmux {live[0]}`")
    return None


def cmd_tmux(args: argparse.Namespace) -> int:
    owners = _session_owners()
    if not owners:
        _err("neither system user (" + ", ".join(_SESSION_OWNERS) + ") exists here — "
             "this node does not host fleet sessions")
        return 1
    if (os.geteuid() != 0 and os.geteuid() not in {uid for _, uid, _ in owners}
            and not shutil.which("sudo")):
        # Every path below needs to BE one of these users; say so rather than blame jaahd.
        _err(f"must run as root or one of {', '.join(_SESSION_OWNERS)} on a node with no sudo")
        return 1
    _preauthorize(owners)
    sockets = _discover_sockets(owners)
    if not sockets:
        _err(f"no fleet tmux sockets on this node (looked under {tmux_tmpdir()}); "
             "is jaahd running, and does this node host runners?")
        return 1
    if args.target in ("list", "ls"):
        return _cmd_tmux_list(sockets)

    target = args.target or _bare_target(sockets)
    if target is None:
        return 1
    session = target.split(":", 1)[0]
    for owner, uid, home, socket in sockets:
        # `=name` defeats tmux's bare-name PREFIX matching, so `fleet` never resolves to `fleet-old`.
        if _tmux_capture(owner, uid, home, socket, ["has-session", "-t", f"={session}"])[0] != 0:
            continue
        if _current_socket() == socket:
            # Already inside this server (the Web Console's pane): attaching would nest; move the client.
            verb = ["switch-client", "-t", target]
        elif args.read_only:
            # `ignore-size` matters: a plain attach shrinks the session to the smallest client, reflowing
            # the runner's panes.
            verb = ["attach", "-t", target, "-f", "read-only,ignore-size"]
        else:
            verb = ["attach", "-t", target]
        argv = _as_owner(owner, uid, home, ["tmux", "-S", socket, *verb])
        # exec, not run: the terminal must become tmux's, with no process holding the tty in between.
        os.execvp(argv[0], argv)
    _err(f"no session '{session}' on this node — `jaah tmux list` shows what there is")
    return 1


# ── virtual nodes (a vnode HOST only) ──────────────────────────────────────────────────────────────
#
# Sanctioned copies of `vnodehelper/layout.py`'s store path and runtime root (installed alone at
# /usr/bin/jaah); pinned by `tests/test_vnodehelper_layout.py`.
VNODE_STORE = "/var/lib/jaah/vnodehelper/store.json"
# The root vnode helper's in-process entry point, where the package installs it — pinned, never derived
# from this file: /usr/bin/jaah is a COPY in the package, not a link into the bundle. The same path
# as `jaah-vnodehelper.service`'s ExecStart (`tests/test_vnodehelper_layout.py`).
VNODE_HELPER_MAIN = "/usr/lib/jaahd/apps/jaahd/vnodehelper/main.py"
VNODE_RUN_ROOT = "/run/jaah-vn"
MICROVMD_MAIN = "/usr/lib/jaahd/apps/jaahd/microvmd.py"
VNODE_ROOT = "/var/lib/jaah-vn"


def ensure_vnode_var_tmp(token: str) -> None:
    """`layout.var_tmp_dir(v)` for a vnode's TOKEN, `1770 root:vn<t>-jaah`, made BEFORE the restart below: the units bind it
    over /var/tmp, and a missing bind source fails a unit's namespace before any `ExecStartPre=` could make
    it — so a vnode built before it existed would not come back from the upgrade. Opened `O_NOFOLLOW` and
    owned through the descriptor. A vnode whose group is gone is left to the helper."""
    try:
        gid = grp.getgrnam(f"vn{token}-jaah").gr_gid
    except KeyError:
        return
    path = f"{VNODE_ROOT}/{token}/var-tmp"
    try:
        try:
            os.mkdir(path, 0o700)
        except FileExistsError:
            pass
        fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fchown(fd, 0, gid)
            os.fchmod(fd, 0o1770)
        finally:
            os.close(fd)
    except OSError as exc:
        _err(f"vnode {token}: could not make {path} ({exc.strerror or exc}) - its units will not start")


def vnode_units(token: str) -> list[str]:
    """`layout.units` for a vnode's TOKEN — its tag, or its slot for a vnode built before tags."""
    return [_tailscaled_unit(token), f"jaah-microvm@{token}.path", f"jaahd@{token}.service",
            f"jaah-runner-vn@{token}.service", f"jaah-gha-vn@{token}.path"]


def _tailscaled_unit(token: str) -> str:
    return f"jaah-tailscaled-vn@{token}.service"


def _tailscaled_definition_changed(token: str) -> bool:
    """Whether vnode TOKEN's tailscaled must restart onto a changed unit definition.

    Restarting it logs the vnode out of the tailnet and back in. Every upgrade restarted every vnode's
    at once, from one host IP, and headscale's per-IP limits answered 429 — the vnodes then failed their
    rejoin and were rebuilt (prod, 2026-10-14). Ours is its unit (template plus drop-ins, as `systemctl cat`
    renders them): restart when that differs from the definition recorded at its last restart here. No record yet records the running
        one WITHOUT a restart, so introducing the record is not itself a fleet-wide re-login. A replaced binary
    is the other reason (`_tailscaled_binary_replaced`)."""
    shown = subprocess.run(["systemctl", "cat", _tailscaled_unit(token)], capture_output=True, text=True,
                           check=False)
    if shown.returncode != 0:
        return False
    # Directives only: `systemctl cat` interleaves `#` path headers (and a "changed on disk" warning before a
    # reload) whose format is systemd's, and a comment edit is no reason to re-login.
    directives = [ln for ln in shown.stdout.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]
    digest = hashlib.sha256("\n".join(directives).encode()).hexdigest()
    record = os.path.join(VNODE_ROOT, token, "tailscaled-unit.sha256")
    try:
        with open(record, encoding="utf-8") as fh:
            recorded = fh.read().strip()
    except FileNotFoundError:
        recorded = None
    except OSError as exc:
        _err(f"vnode {token}: cannot read {record} ({exc.strerror or exc}) - its tailscaled is left running")
        return False
    if recorded == digest:
        return False
    if not os.path.isdir(os.path.dirname(record)):
        return False  # not a built vnode's tree: the helper owns making it, never this
    try:
        with open(record, "w", encoding="utf-8") as fh:
            fh.write(digest + "\n")
    except OSError as exc:
        _err(f"vnode {token}: cannot write {record} ({exc.strerror or exc}) - its tailscaled is left running")
        return False
    return recorded is not None


def _tailscaled_binary_replaced(token: str) -> bool:
    """Whether vnode TOKEN's running tailscaled is a binary the `tailscale` package has since replaced.

    That package's own upgrade restarts only the host's `tailscaled.service`, so without this a tailscale
    security fix would never reach a running vnode. The kernel marks a replaced executable `(deleted)`."""
    rc, pid = _systemctl("show", "-p", "MainPID", "--value", _tailscaled_unit(token))
    if rc != 0 or not pid.isdigit() or pid == "0":
        return False
    try:
        return os.readlink(f"/proc/{pid}/exe").endswith(" (deleted)")
    except OSError:
        return False


def _tailscaled_must_restart(token: str) -> bool:
    """Only onto a changed unit or a replaced binary: any other restart is a needless tailnet re-login."""
    return _tailscaled_definition_changed(token) or _tailscaled_binary_replaced(token)


def _vnode_token(record: dict) -> str | None:
    """`layout.Vnode.token` of a store record, or None when it is malformed — validated before it becomes
    a unit name, a user name and a path (the twin of `layout.TAG_PATTERN` / `validate_slot`)."""
    tag, slot = record.get("tag"), record.get("slot")
    if tag is not None:
        return tag if isinstance(tag, str) and re.fullmatch(r"[a-z][a-z0-9]{7}", tag) else None
    if isinstance(slot, int) and not isinstance(slot, bool) and 1 <= slot <= 99:
        return str(slot)
    return None


def cmd_vnode_converge_units(_args: argparse.Namespace) -> int:
    """Restart every built vnode's resident units onto the package just installed, and converge each
    vnode's tmux ACL grant — the per-vnode twin of postinst's runner restart and `server-access` grant.

    A no-op without a vnode store (every regular node). `try-restart` + `KillMode=process`: live panes
    survive. A vnode's tailscaled restarts only onto a changed unit or binary (`_tailscaled_must_restart`). A vnode whose listing fence could not be written first is NOT restarted (its units would fail
    closed) and the command exits 1, naming it."""
    try:
        with open(VNODE_STORE, encoding="utf-8") as fh:
            store = json.load(fh)
    except FileNotFoundError:
        return 0
    except (OSError, ValueError) as exc:
        _err(f"cannot read the vnode store {VNODE_STORE} ({exc.__class__.__name__}) - re-run as root")
        return 1
    if os.geteuid() != 0:
        _err("vnode converge-units restarts system units and edits another user's tmux ACL - re-run as root")
        return 1
    tokens = sorted({t for v in store.get("vnodes") or [] if v.get("state") == "built"
                     and (t := _vnode_token(v)) is not None})
    # Before any restart: the templates bind each vnode's own /var/tmp and its listing-fence files, which a
    # vnode built by an older package does not have yet (`vnodehelper/ops.py::fence_all`).
    for token in tokens:
        ensure_vnode_var_tmp(token)
    unfenced = _fence_all_vnodes(tokens) if tokens else set()
    for token in tokens:
        if token in unfenced:
            # A restart would fail its units closed on the missing fence files: leave them running.
            _err(f"vnode {token}: listing fence not written - its units were NOT restarted; fix the helper's "
                 f"error above, then re-run `jaah vnode converge-units`")
        elif os.path.isdir("/run/systemd/system"):
            # A running vnode built before its MicroVM watch existed: `try-restart` never starts a unit, so
            # start the watch itself — BEFORE the daemon's restart rewrites the marker (`layout.units`).
            if _systemctl("is-active", "--quiet", f"jaahd@{token}.service")[0] == 0:
                subprocess.run(["systemctl", "start", f"jaah-microvm@{token}.path"], check=False)
            units = [u for u in vnode_units(token)
                     if u != _tailscaled_unit(token) or _tailscaled_must_restart(token)]
            subprocess.run(["systemctl", "try-restart", *units], check=False)
            # Its MicroVM supervisor too — but by the K4 rule, never under a live VM (`microvmd.py
            # postinst-restart`): a `try-restart` here would kill every session it holds.
            try:
                subprocess.run([sys.executable, MICROVMD_MAIN, "postinst-restart",
                                "--unit", f"jaah-microvmd@{token}.service",
                                "--pending", f"{VNODE_RUN_ROOT}/{token}/microvm/restart-pending"], check=False)
            except OSError as exc:
                _err(f"vnode {token}: cannot run {MICROVMD_MAIN} ({exc.strerror or exc}) - its MicroVM "
                     f"supervisor keeps its old code until it next restarts")
        session, daemon = f"vn{token}-jaah", f"vn{token}-jaahd"
        try:
            uid = pwd.getpwnam(session).pw_uid
        except KeyError:
            continue
        sock = f"{VNODE_RUN_ROOT}/{token}/tmux/tmux-{uid}/default"
        if os.path.exists(sock) and shutil.which("tmux"):
            subprocess.run(["runuser", "-u", session, "--", "tmux", "-S", sock, "server-access", "-aw", daemon],
                           check=False)
    return 1 if unfenced else 0


def _fence_all_vnodes(tokens: list[str]) -> set[str]:
    """Run the helper's `--fence-all` (`vnodehelper/ops.py::fence_all`); the tokens it did NOT fence. A
    helper that failed outright — or whose outcome line is unreadable — fenced none of them."""
    try:
        proc = subprocess.run([sys.executable, VNODE_HELPER_MAIN, "--fence-all"], check=False,
                              stdout=subprocess.PIPE, text=True)
    except OSError as exc:
        _err(f"cannot run the vnode helper {VNODE_HELPER_MAIN} ({exc.strerror or exc}) - is jaahd installed?")
        return set(tokens)
    if proc.returncode != 0:
        _err(f"the vnode helper's --fence-all exited {proc.returncode} - no vnode was fenced")
        return set(tokens)
    try:
        fenced = json.loads((proc.stdout or "").strip().splitlines()[-1])["fenced"]
    except (IndexError, ValueError, KeyError, TypeError):
        _err("the vnode helper's --fence-all printed no outcome - treating every vnode as not fenced")
        return set(tokens)
    return set(tokens) - set(fenced)


# ── entry point ─────────────────────────────────────────────────────────────────────────────────────

def cred_socket_path() -> str:
    """Sanctioned copy of `environment.credential_socket()`: the override, else `<run root>/creds/sock` (the
    named pipe on Windows)."""
    if sys.platform == "win32":
        return _env_get(_CRED_SOCKET_ENV) or _CRED_PIPE_WINDOWS
    run_root = (_env_get(_RUN_ROOT_ENV) or "").strip().rstrip("/")
    return _env_get(_CRED_SOCKET_ENV) or (f"{run_root}/creds/sock" if run_root else _CRED_SOCKET_DEFAULT)


def _connect(path: str):
    """A connected stream to the daemon at `path`. On Windows it is a named pipe, dialled through
    `host_platform.connect_socket`, which also proves the pipe's server IS the daemon; imported lazily and
    only there, since a Windows node runs this file in place from the bundle's `apps/jaahd` while Linux
    installs it alone (`git_credential_helper._connect` does the same)."""
    import socket
    if sys.platform == "win32":
        import host_platform  # noqa: PLC0415 — see the docstring

        return host_platform.connect_socket(path, timeout=_SOCKET_TIMEOUT_S)
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.settimeout(_SOCKET_TIMEOUT_S)
        sock.connect(path)
    except BaseException:
        sock.close()
        raise
    return sock


def _local_call(verb: str) -> dict:
    """Ask the node daemon a daemon-local verb (`agent_vocabulary.LOCAL_VERBS`) over its socket. Raises
    OSError (reported by `main`) when the daemon is unreachable or says no."""
    import socket
    path = cred_socket_path()
    request = json.dumps({"call": verb, "args": {}}).encode() + b"\n"
    try:
        with _connect(path) as sock:
            sock.sendall(request)
            sock.shutdown(socket.SHUT_WR)
            buf = b""
            while b"\n" not in buf and len(buf) < _SOCKET_REPLY_CAP:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                buf += chunk
    except OSError as exc:
        raise OSError(f"the node daemon is unreachable at {path} ({exc}) - is jaahd running? "
                      f"`systemctl status jaahd`") from None
    try:
        reply = json.loads(buf.split(b"\n", 1)[0])
    except ValueError:
        raise OSError("the node daemon sent an unreadable reply") from None
    if not isinstance(reply, dict) or reply.get("ok") is not True or not isinstance(reply.get("result"), dict):
        error = reply.get("error") if isinstance(reply, dict) else None
        raise OSError(f"the node daemon refused {verb}: {error or 'no answer'}"
                      + (" - this daemon predates NodeVault; upgrade the jaah package"
                         if error == "unknown_verb" else ""))
    return reply["result"]


def cmd_nodevault_fingerprint(_args: argparse.Namespace) -> int:
    result = _local_call("nodevault.fingerprint")
    fingerprint = result.get("fingerprint")
    if not result.get("capable"):
        print("NodeVault: not supported on this node (its crypto library does not load here)")
        return 1
    if not fingerprint:
        print("NodeVault identity: not created yet (the daemon creates it at start)")
        return 1
    print(f"NodeVault identity fingerprint: {fingerprint}")
    print(f"Short form (as the console shows it): {fingerprint[:12]}")
    return 0


def cmd_nodevault_keys(_args: argparse.Namespace) -> int:
    keys = _local_call("nodevault.keys").get("keys") or []
    if not keys:
        print("No NodeVault keys on this node.")
        return 0
    for row in keys:
        print(f"{row.get('state', '?'):<8} {row.get('key_hash', '?')}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="jaah", description="jaah node operator CLI")
    top = parser.add_subparsers(dest="group", required=True)

    auth = top.add_parser("auth", help="node credential management").add_subparsers(
        dest="verb", required=True)
    login = auth.add_parser("login", help="enrol this node (token read from stdin, never argv)")
    login.add_argument("node_id", help="this node's assigned uuid, as the console shows it (the uuid "
                                       "is the machine key; its NAME is read from the CP)")
    login.add_argument("--with-token", action="store_true", required=True,
                       help="read NODE_TOKEN from stdin (required; the token is never an argument)")
    login.add_argument("--no-verify", action="store_true",
                       help="skip the control-plane check (pre-CP provisioning escape)")
    login.add_argument("--no-restart", action="store_true",
                       help="write the credential without restarting a running jaahd (it keeps "
                            "serving with the previous one until restarted)")
    login.set_defaults(func=cmd_auth_login)
    auth.add_parser("status", help="report this node's credential state").set_defaults(
        func=cmd_auth_status)
    rotate = auth.add_parser("rotate", help="replace this node's credential with a fresh one only node.env "
                                            "holds (each installer's last step)")
    rotate.add_argument("--no-restart", action="store_true",
                        help="rotate without restarting a running jaahd")
    rotate.set_defaults(func=cmd_auth_rotate)

    node = top.add_parser("node", help="node health").add_subparsers(dest="verb", required=True)
    node.add_parser("status", help="daemon / package / identity summary").set_defaults(
        func=cmd_node_status)
    node.add_parser("converge-runner",
                    help="point the runner tmux unit at this node's name (session = NODE_NAME, "
                         "refreshed from the control plane), renaming a live session that still "
                         "carries an outgoing name in place").set_defaults(
        func=cmd_node_converge_runner)

    vnode = top.add_parser("vnode", help="a virtual-node host's vnodes").add_subparsers(
        dest="verb", required=True)
    vnode.add_parser("converge-units",
                     help="restart every built vnode's units onto this package and converge its tmux "
                          "grant (no-op on a node that is not a vnode host)").set_defaults(
        func=cmd_vnode_converge_units)

    nodevault = top.add_parser("nodevault", help="this node's NodeVault identity and keys (public facts)"
                               ).add_subparsers(dest="verb", required=True)
    nodevault.add_parser("fingerprint", help="the node identity key's fingerprint (compare with the console)"
                         ).set_defaults(func=cmd_nodevault_fingerprint)
    nodevault.add_parser("keys", help="the NodeVault keys this node holds, by key hash").set_defaults(
        func=cmd_nodevault_keys)

    # A bare positional: "put me in this node's session" must stay a two-word command.
    tmux = top.add_parser("tmux", help="attach this terminal to the node's runner/console sessions")
    tmux.add_argument("target", nargs="?", default=None,
                      help=f"session or session:window (default: {node_session()} — this node's "
                           "own session, named after its NODE_NAME); `list` shows every session and "
                           "window instead of attaching")
    tmux.add_argument("-r", "--read-only", action="store_true",
                      help="attach read-only and ignore-size — watch a runner without typing into "
                           "it or reflowing its panes")
    tmux.set_defaults(func=cmd_tmux)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except (OSError, LookupError) as exc:
        _err(str(exc))
        return 1


if __name__ == "__main__":
    sys.exit(main())
