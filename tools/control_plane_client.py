"""control_plane_client.py — shared helper for Python scripts that read project metadata from the
Control-plane API instead of parsing the portfolio's markdown project cards.

API base: see `api_base`. HTTP reads use a PERSISTED session cookie, logging in (POST /api/auth/login)
only when it is missing/expired; a host with no admin credentials (a fleet node's packaged copy) falls
back to NODE_TOKEN as `X-Agent-Token`, within `apps/api/app/auth.py::NODE_TOKEN_API_PATHS`.

TWO TRANSPORTS, AND A THREE-WAY SPLIT BETWEEN THEM. Every `/api/agent/*` call (and the project list)
asks the NODE DAEMON over its unix socket FIRST (`agent_socket.call`, a verb from
`apps/jaahd/agent_vocabulary.py`): user `jaah` holds no credential, so for a runner the socket is the
only door. The same scripts also run as the OPERATOR (cron, by hand), who cannot reach the socket but
has a credential. So each call branches on HOW the socket failed:

  * a result — done; no credential is touched.
  * `AgentSocketUnavailable` — NO ANSWER. Fall through to the NODE_TOKEN leg (the operator's path).
  * `AgentSocketRefused` — the daemon said NO. RAISE (as `ControlPlaneApiError`), NEVER fall back:
    retrying with a credential would make every `agent_vocabulary` rule advisory (peer-derived task id,
    no route that hands out a credential by name, the unforgeable R30 persona guard).

`_via_socket` is the ONE implementation of that split; no function reimplements the branch.

The cookie jar is persisted (keyed by hub-root basename; each worktree has its own session cookie name)
because login shares the PUBLIC_RATE_LIMIT window (apps/api/app/main.py), and a fresh login per
short-lived hook exhausts it and 429s. Hot callers (check_gh_pr_guard.py) add their own data cache.

Raises ControlPlaneApiError on any failure — callers must fail loudly, never skip or fall back to stale
data (spec R5, NFR Consistency). stdlib-only (urllib).
"""
from __future__ import annotations

import http.cookiejar
import json
import os
import re
import urllib.error
import urllib.parse
import sys
import urllib.request
from pathlib import Path
from typing import Any

# Explicit insert for importers; `tools/` is sys.path[0] only when run as a script.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import runner_env  # noqa: E402
import jaah_paths  # noqa: E402
from jaah_env_names import env_get  # noqa: E402

# The daemon socket: the FIRST transport (module docstring). Imported defensively: it reaches a node
# only via the jaahd bundle manifest, and a hard ImportError would
# take down every importer (run_gate.py's Stop gate, check_gh_pr_guard.py, `apps/jaahd/guards/*`).
# `None` = no socket transport; `_SOCKET_IMPORT_ERROR` names the cause in the credential leg's error.
# Tests flip it: a suite pinning the NODE_TOKEN path MUST set `agent_socket` to None (or set
# `JAAH_CRED_SOCKET` — `_socket_path` is the daemon's own and reads the daemon module's `SOCKET_PATH`, not an
# attribute of the facade), since a developer box may run a real daemon.
try:
    import agent_socket  # noqa: E402
except Exception as _socket_import_exc:  # noqa: BLE001 — a bundle missing the manifest line; the facade itself
    # loads the daemon's client on first use, and answers a client that will not load as AgentSocketUnavailable.
    agent_socket = None  # type: ignore[assignment]
    _SOCKET_IMPORT_ERROR = f"tools/agent_socket.py is not importable here ({_socket_import_exc!r})"
else:
    _SOCKET_IMPORT_ERROR = ""


HUB_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_ENV_PATH = HUB_ROOT / ".env"
DEFAULT_TIMEOUT = 10.0


def _default_cookie_jar_path() -> Path:
    return jaah_paths.cache_dir("cookie", f"cockpit-api-{HUB_ROOT.name}.txt")


def _save_jar(cookie_jar: http.cookiejar.MozillaCookieJar) -> None:
    """Persist the session cookie, tolerating a filesystem fault (full/unwritable cache dir, a
    root-owned jar). Persisting is a rate-limit OPTIMISATION, so a save failure must never abort a call
    that succeeded (or spam the sync cron's log with tracebacks)."""
    try:
        cookie_jar.save(ignore_discard=True, ignore_expires=True)
    except OSError:
        pass


_WARNED: set[str] = set()


def _warn_once(message: str) -> None:
    """One diagnostic per distinct message, to stderr, so a frequently-invoked hook does not drown its log."""
    if message in _WARNED:
        return
    _WARNED.add(message)
    print(f"control_plane_client: {message}", file=sys.stderr)


class ControlPlaneApiError(RuntimeError):
    """The control-plane API was unreachable, rejected our credentials, or returned something unparseable.
    Callers must propagate it as a loud failure — never continue with an empty/stale project list."""


def _read_env_value(key: str, env_path: Path = DEFAULT_ENV_PATH) -> str:
    """Read KEY from the hub's own .env (last-wins, inline-comment/quote stripped)."""
    if not env_path.is_file():
        return ""
    value = ""
    pattern = re.compile(rf"^\s*{re.escape(key)}=(.*)$")
    for line in env_path.read_text(encoding="utf-8").splitlines():
        m = pattern.match(line)
        if m:
            raw = m.group(1).split("#", 1)[0].strip()
            value = raw.strip("\"'")
    return value


# No SSM-helper arm: the broker no longer serves `node_token`. Both remaining arms are the
# OPERATOR's.
def _resolve_node_token(env_path: Path = DEFAULT_ENV_PATH) -> str:
    """NODE_TOKEN from the process env (what `compose_env_guard` injects), then the repo `.env`; "" on a
    cloud node, where there is nothing further to ask."""
    return (
        os.environ.get("NODE_TOKEN", "").strip()
        or _read_env_value("NODE_TOKEN", env_path)
    )


def caller_task_id() -> str:
    """The `X-Agent-Task-Id` value for an agent-API call ON THE CREDENTIAL LEG ONLY: this process's own
    console task id inside a fleet runner's session, else the `host` producer sentinel.

    Not consulted on the socket leg, where the daemon DERIVES the header from the calling process
    (`runner_registry.resolve_peer` → `agent_vocabulary.caller_task_header`) — which is what makes
    `apps/api/app/routes/agent/auth.py::_guard_task_creating_caller` (R30) unforgeable.

    The task-creating routes require this header and reject a persona other than `plan`/`finish`.
    `CONTROL_TASK_ID` is exported only into a runner's shell (`fleet_dispatch.build_auth_lines`), so cron
    declares `host` while the same tool inside an implementer's session declares that implementer. Never
    hardcode the sentinel at a call site.

    `host` is NOT an unconditional fallback (an identity never gets a safe-off default): inside a runner
    (`CONTROL_RUNNER_SESSION_ID` set) a missing task id means the id was LOST, so this declares nothing
    and the server 422s. Guards against accidental loss, not forgery. Not `COCKPIT_AGENT_BASE` or
    `JAAH_WORKTREE` as the runner test: operator shells carry those. Mirror:
    `apps/jaahd/control_plane_api.py::_caller_task_id`."""
    task_id = runner_env.task_id().strip()
    if task_id:
        return task_id
    if runner_env.runner_session_id().strip():
        return ""                       # a runner that lost its id: refuse to claim producer status
    return "host"


# ── The socket leg: ONE implementation of the three-way split ─────────────────────────────────────────
# The only place that catches `AgentSocketRefused` and decides a credential fallback is permitted.
# Route through here or do not call the daemon at all.

# "The socket gave no answer": a sentinel, not `None` (a verb may legitimately return None) and not an
# exception (falling through is the NORMAL operator path).
_NO_SOCKET = object()


def _via_socket(verb: str, args: dict[str, Any] | None = None,
                timeout: float | None = None) -> Any:
    """Ask the daemon to perform `verb`: its parsed result, `_NO_SOCKET` when nothing answered, or
    `ControlPlaneApiError` when the daemon answered NO.

    `_NO_SOCKET` covers every non-answer (no socket file, EACCES outside group `jaah`, a timeout, a
    closed connection, no `tools/agent_socket.py`); the caller MAY then use its own credential (the
    operator-cron arm).

    `timeout` is FORWARDED, not defaulted: shortening a caller's budget on the leg with no fallback turns a slow CP into a gate failure.

    A POLICY REFUSAL IS RAISED, NEVER RETRIED (`forbidden_arg`, `not_a_runner`, `unknown_verb`, `busy`,
    `unavailable`). Re-raised as `ControlPlaneApiError` because that is what every caller catches; the
    refusal is kept as `__cause__`."""
    if agent_socket is None:
        return _NO_SOCKET
    try:
        return agent_socket.call(verb, args or {}, timeout=timeout)
    except agent_socket.AgentSocketUnavailable:
        return _NO_SOCKET
    except agent_socket.AgentSocketRefused as exc:
        # `bad_request` = the SHAPE was not understood, which is what a daemon predating this protocol
        # says to every verb during a rollout. Not a policy decision, so a credential holder may fall
        # back; a runner has no credential either way.
        if exc.reason == "bad_request":
            _warn_once(
                f"the node daemon did not understand `{verb}` (bad_request) — it is most likely running a "
                f"bundle that predates the local vocabulary; using the credential leg for this call")
            return _NO_SOCKET
        raise ControlPlaneApiError(
            f"the node daemon refused `{verb}` ({exc.reason}) — this is NOT retried with a credential, by "
            f"design: see control_plane_client.py's module docstring on the three-way split"
        ) from exc


# WHICH ARM OF `api_base` ANSWERED. tools/project_config.py's `api_base_redirect_reason` needs it: it
# refuses to write the governance cache when the base came from the in-checkout `.env` (which the
# guarded repo controls). It asks here rather than re-deriving the order.
API_BASE_FROM_OVERRIDE = "JAAH_API_URL"
API_BASE_FROM_ENV_FILE = "env-file"
API_BASE_FROM_NODE_ENV = "node-env"
API_BASE_FROM_GRID_DEFAULT = "grid-default"


def api_base_source(env_path: Path = DEFAULT_ENV_PATH) -> tuple[str, str]:
    """(which arm answered, the base it produced) — the ONE implementation of `api_base`'s order."""
    override = env_get("JAAH_API_URL")
    if override:
        return API_BASE_FROM_OVERRIDE, override
    port = _read_env_value("API_PORT", env_path)
    if port:
        return API_BASE_FROM_ENV_FILE, f"http://127.0.0.1:{port}"
    node_base = (
        os.environ.get("COCKPIT_AGENT_BASE", "").strip()
        or env_get("JAAH_CP_BASE_URL", "").strip()
    )
    if node_base:
        return API_BASE_FROM_NODE_ENV, node_base.rstrip("/")
    return API_BASE_FROM_GRID_DEFAULT, "http://127.0.0.1:8020"


def api_base(env_path: Path = DEFAULT_ENV_PATH) -> str:
    """`JAAH_API_URL` override, else the loopback stack this checkout's `.env` describes, else the
    control plane this NODE was pointed at, else the port grid's default.

    The third arm serves a host with no `.env`: the jaahd bundle ships `tools/` to /usr/lib/jaahd
    with none, and a loopback default reaches no console on any node but the CP host.
    `COCKPIT_AGENT_BASE` (exported by `fleet_dispatch.build_auth_lines` from
    `control_plane_api.resolve_control_plane_api_base()`, the one home) and `JAAH_CP_BASE_URL` (the node
    unit's) are read where they already exist.

    The `.env` port WINS over `COCKPIT_AGENT_BASE` so a hub checkout keeps dialling its own stack.
    A caller that writes one task row may choose the opposite for its own reason; do not "fix" one to
    match the other without carrying the reason across."""
    return api_base_source(env_path)[1]


def _login(
    opener: urllib.request.OpenerDirector,
    base: str,
    env_path: Path,
    timeout: float,
    also_tried: str = "",
) -> None:
    """POST /api/auth/login (consumes PUBLIC_RATE_LIMIT budget). Only after a request against the
    persisted session proved it missing/expired.

    `also_tried` is appended to the missing-credentials error so it names every door knocked on.

    `.env` admin credentials belong to the stack that same `.env` describes: they are sent only to its
    loopback `API_PORT`, an explicit `JAAH_API_URL`, or the loopback grid default — NEVER to the node's
    control plane (`COCKPIT_AGENT_BASE`/`JAAH_CP_BASE_URL`), where they are a stranger's password and each
    attempt is a refused sign-in on production (and trips its failed-login alarm)."""
    source, _ = api_base_source(env_path)
    if source == API_BASE_FROM_NODE_ENV:
        raise ControlPlaneApiError(
            f"{base} came from the node environment (COCKPIT_AGENT_BASE/JAAH_CP_BASE_URL), not from "
            f"{env_path}'s API_PORT, so no ADMIN_EMAIL/ADMIN_PASSWORD from that file is sent there{also_tried} — set "
            f"API_PORT for a local stack, or JAAH_API_URL to name a console on purpose"
        )
    email = _read_env_value("ADMIN_EMAIL", env_path)
    password = _read_env_value("ADMIN_PASSWORD", env_path)
    if not email or not password:
        raise ControlPlaneApiError(
            f"ADMIN_EMAIL/ADMIN_PASSWORD not set in {env_path} — cannot authenticate to the control-plane "
            f"API{also_tried}"
        )
    login_req = urllib.request.Request(
        f"{base}/api/auth/login",
        data=json.dumps({"email": email, "password": password}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        opener.open(login_req, timeout=timeout).read()
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise ControlPlaneApiError(
            f"login to control-plane API at {base} failed ({exc}) — is the stack up (./dc up -d in {HUB_ROOT})?"
        ) from exc


def _send_once(
    opener: urllib.request.OpenerDirector,
    url: str,
    method: str,
    payload: dict[str, Any] | None,
    timeout: float,
    extra_headers: dict[str, str] | None = None,
) -> tuple[int, bytes]:
    """One HTTP attempt: (status_code, body) for ANY response, 4xx/5xx included — callers decide what a
    status means (a 409 is success for `create_project`). Only a transport failure propagates.

    `extra_headers` (the node-token fallback's `X-Agent-Token`) is merged LAST so the Content-Type default
    can never displace it."""
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {"Content-Type": "application/json"} if data is not None else {}
    if extra_headers:
        headers.update(extra_headers)
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with opener.open(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def api_request(
    method: str,
    path: str,
    payload: dict[str, Any] | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    env_path: Path = DEFAULT_ENV_PATH,
    cookie_jar_path: Path | None = None,
    node_token_ok: bool = False,
) -> tuple[int, bytes]:
    """Authenticated request: the persisted session first, then (on 401/403 or a connection failure)
    `POST /api/auth/login` and exactly one retry. (status_code, body) for ANY final response; raises
    `ControlPlaneApiError` only when the API cannot be reached even after a fresh login.

    `node_token_ok=True` inserts ONE `X-Agent-Token` attempt between the failed session request and the
    login. Opt-in per call site: only path+METHOD pairs in `apps/api/app/auth.py::NODE_TOKEN_API_PATHS`
    accept it (`GET /api/projects` does, via `require_user_or_agent`).

    SESSION-FIRST: the node token is a shared node-wide secret that gets the UNFILTERED project list,
    while a cookie is user-scoped — so the broad credential is reached only where there is no other."""
    base = api_base(env_path)
    jar_path = cookie_jar_path or _default_cookie_jar_path()
    try:
        jar_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError:
        pass  # unwritable jar dir — tolerated for the same reason as the save (see `_save_jar`)
    cookie_jar = http.cookiejar.MozillaCookieJar(str(jar_path))
    if jar_path.is_file():
        try:
            cookie_jar.load(ignore_discard=True, ignore_expires=True)
        except (OSError, http.cookiejar.LoadError):
            pass  # corrupt/partial jar — treated the same as "no session yet", not a fatal error
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cookie_jar))
    url = f"{base}{path}"

    try:
        status, body = _send_once(opener, url, method, payload, timeout)
    except (urllib.error.URLError, OSError, ValueError):
        status, body = None, b""  # fall through to the login-and-retry path below
    else:
        if status not in (401, 403):
            _save_jar(cookie_jar)
            return status, body

    also_tried = ""
    if node_token_ok:
        token = _resolve_node_token(env_path)
        if not token:
            also_tried = (
                f" (no NODE_TOKEN in the environment or {env_path}, so "
                f"the node-token route was not available)"
            )
        else:
            also_tried = " (the node-token route was tried first and the API refused it)"
            try:
                status, body = _send_once(
                    opener, url, method, payload, timeout, {"X-Agent-Token": token},
                )
            except (urllib.error.URLError, OSError, ValueError):
                also_tried = " (the node-token route was tried first and the API was unreachable)"
            else:
                if status not in (401, 403):
                    # No `_save_jar`: these routes never mint a session, so there is nothing to persist
                    # and a token-authenticated call must not look like a logged-in one on disk.
                    return status, body

    _login(opener, base, env_path, timeout, also_tried)
    _save_jar(cookie_jar)
    try:
        return _send_once(opener, url, method, payload, timeout)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise ControlPlaneApiError(
            f"{method} {url} failed even after a fresh login: {exc}"
        ) from exc


def fetch_projects(
    timeout: float = DEFAULT_TIMEOUT,
    env_path: Path = DEFAULT_ENV_PATH,
    cookie_jar_path: Path | None = None,
) -> list[dict[str, Any]]:
    """The project list (ProjectOut shape — apps/api/app/routes/projects.py), from the daemon first
    (`projects.list`), then the API. Raises `ControlPlaneApiError` on any failure.

    The socket leg serves runner sessions (project_config.py → run_gate.py's Stop hook, check_gh_pr_guard.py,
    the access guards); `env_path`/`cookie_jar_path` only steer the HTTP leg. The HTTP leg is the
    operator's; `node_token_ok=True` covers a bundled node with no `.env` and no reachable daemon."""
    data = _via_socket("projects.list", timeout=timeout)
    source = "projects.list via the node daemon"
    if data is _NO_SOCKET:
        base = api_base(env_path)
        source = f"GET {base}/api/projects"
        status, body = api_request(
            "GET", "/api/projects", timeout=timeout, env_path=env_path,
            cookie_jar_path=cookie_jar_path, node_token_ok=True,
        )
        if status >= 400:
            raise ControlPlaneApiError(
                f"{source} -> HTTP {status}: {body.decode(errors='replace')[:200]}"
            )
        try:
            data = json.loads(body)
        except json.JSONDecodeError as exc:
            raise ControlPlaneApiError(f"{source} returned unparseable JSON: {exc}") from exc
    # ONE shape check for both legs: a daemon reply is the same CP JSON as an HTTP body.
    if not isinstance(data, list):
        raise ControlPlaneApiError(f"{source} returned a non-list body: {type(data)!r}")
    return data


def fetch_port_grid(
    timeout: float = DEFAULT_TIMEOUT,
    env_path: Path = DEFAULT_ENV_PATH,
) -> dict[str, Any]:
    """The deterministic port grid's INPUTS (DB rows edited in Settings), daemon `port_grid.get` first,
    else `GET /api/agent/port-grid` with this node's token. Returns the raw body: `range_start_by_var`
    plus `block_size`/`stride`/`window`/`auto_port_floor`/`auto_port_ceiling`.

    Two legs because `check_pool_ports.py` runs in runner sessions and operator shells.

    Raises on any unreachable/refused/unparseable/wrong-typed field. NO cache, NO code-copy fallback: a
    stale grid makes `--fix` rewrite live `.env`s onto ports nothing agrees with. An EMPTY
    `range_start_by_var` is LEGITIMATE (every var tier 2)."""
    data = _via_socket("port_grid.get", timeout=timeout)
    source = "port_grid.get via the node daemon"
    if data is _NO_SOCKET:
        base = api_base(env_path)
        path = "/api/agent/port-grid"
        source = f"GET {base}{path}"
        status, body = _token_api_request("GET", path, timeout=timeout, env_path=env_path)
        if status >= 400:
            raise ControlPlaneApiError(
                f"{source} -> HTTP {status}: {body.decode(errors='replace')[:200]}"
            )
        try:
            data = json.loads(body)
        except json.JSONDecodeError as exc:
            raise ControlPlaneApiError(f"{source} returned unparseable JSON: {exc}") from exc
    # Every field check runs on BOTH legs: these numbers are written into live `.env`s under `--fix`, so
    # a partly-understood body must stop the run.
    if not isinstance(data, dict):
        raise ControlPlaneApiError(f"{source} returned a non-dict body: {type(data)!r}")

    starts = data.get("range_start_by_var")
    if not isinstance(starts, dict):
        raise ControlPlaneApiError(
            f"{source}: range_start_by_var is {type(starts)!r}, expected an object"
        )
    for var, start in starts.items():
        # `bool` is an `int` subclass; reject explicitly so JSON `true` is not banded as port 1.
        if not isinstance(var, str) or isinstance(start, bool) or not isinstance(start, int):
            raise ControlPlaneApiError(
                f"{source}: range_start_by_var[{var!r}] is {start!r}, expected an int"
            )
    for field in ("block_size", "stride", "window", "auto_port_floor", "auto_port_ceiling"):
        value = data.get(field)
        if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
            raise ControlPlaneApiError(
                f"{source}: {field} is {value!r}, expected a positive int"
            )
    return data


def create_project(
    fields: dict[str, Any],
    timeout: float = DEFAULT_TIMEOUT,
    env_path: Path = DEFAULT_ENV_PATH,
    cookie_jar_path: Path | None = None,
) -> tuple[int, dict[str, Any]]:
    """POST /api/projects (Connect Existing — `apps/api/app/routes/projects.py`). (status_code,
    parsed_body) for ANY final response, including 409 (a seeding caller treats a
    duplicate as "already seeded", making re-runs idempotent). A non-JSON body is `{}`. Raises
    `ControlPlaneApiError` only when the API cannot be reached even after a fresh login."""
    status, body = api_request(
        "POST", "/api/projects", payload=fields, timeout=timeout, env_path=env_path,
        cookie_jar_path=cookie_jar_path,
    )
    try:
        parsed = json.loads(body) if body else {}
    except json.JSONDecodeError:
        parsed = {}
    return status, parsed if isinstance(parsed, dict) else {}


# ── Local clone-path resolution ───────────────────────────────────────────────────────────────────────
# A project's main clone is COMPUTED per node as `<JAAH_HOME>/repos/<project_id>/main` (one stored path
# could be right on only one node). Reference implementation (do not diverge):
# apps/api/app/node_clone.py::resolve_clone; this stdlib half keeps `tools/` free of app imports.

def resolve_clone_root(project_id: int | None) -> str:
    """THIS node's main-clone path for a project: <jaah_home>/repos/<project_id>/main (`projects.id`).
    "" when either input is missing (no JAAH_HOME set here, or no id) — no implicit default."""
    # An id is a non-negative int — a `bool` is an `int`, and a string could carry a path segment.
    if not isinstance(project_id, int) or isinstance(project_id, bool) or project_id < 0:
        return ""
    value = env_get("JAAH_HOME", "").strip()
    home = value.rstrip("/")
    # Checked AFTER the rstrip: "/" strips to nothing and would anchor the path at filesystem root.
    # Matches apps/jaahd/env_helpers.py's `jaah_home()`.
    if not home:
        return ""
    return f"{home}/repos/{project_id}/main"


# ── Agent-token requests to /api/agent/* ──────────────────────────────────────────────────────────────
# Those routes are `_check_token`-gated (X-Agent-Token), not session-cookie. `_token_api_request` is the
# credential leg on its own, so socket-first callers can make the second attempt without re-entering
# the socket branch.
def _token_api_request(
    method: str,
    path: str,
    payload: dict[str, Any] | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    env_path: Path = DEFAULT_ENV_PATH,
) -> tuple[int, bytes]:
    """One NODE_TOKEN-authenticated request against an `/api/agent/*` endpoint: `X-Agent-Token`, no login,
    no cookie jar. (status_code, body) for ANY HTTP response. Raises `ControlPlaneApiError` when NODE_TOKEN
    is unset or the API is unreachable.

    THE OPERATOR'S LEG. Inside a runner there is no NODE_TOKEN and this raises — correctly; reaching it
    means the socket gave no answer, so the error names the socket transport too."""
    token = _resolve_node_token(env_path)
    if not token:
        socket_note = (
            f"; the node daemon's socket was not usable either ({_SOCKET_IMPORT_ERROR})"
            if _SOCKET_IMPORT_ERROR else
            f"; the node daemon's socket at {getattr(agent_socket, 'SOCKET_PATH', '<none>')} gave no "
            f"answer either"
        )
        raise ControlPlaneApiError(
            f"NODE_TOKEN not set in {env_path} or the environment "
            f"— cannot authenticate to the agent-token API ({path}){socket_note}"
        )
    base = api_base(env_path)
    url = f"{base}{path}"
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {"X-Agent-Token": token, "X-Agent-Task-Id": caller_task_id()}
    if data is not None:
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise ControlPlaneApiError(f"{method} {url} failed: {exc}") from exc


# (method, path) → verb for `agent_api_request`, a TABLE on purpose: a generic path passthrough is what
# `agent_vocabulary` refuses to be. Only pairs in-repo callers pass; an unmapped pair is a loud error.
# Adding a row means adding the matching `Verb` there first. Keyed on the path WITHOUT its query string
# (the query becomes `args`, allowlisted CP-side).
#
# `_CREDENTIAL_ONLY_PATHS`: vetted AS operator-only, so they go straight to the credential leg (unlike
# "unmapped", which raises). `purge-stale` archives the escalation trail fleet-wide; its only caller is
# a cron script with a credential.
_CREDENTIAL_ONLY_PATHS: frozenset[tuple[str, str]] = frozenset({
    ("POST", "/api/agent/runner-breaks/purge-stale"),
})

_AGENT_PATH_VERBS: dict[tuple[str, str], str] = {
    # The broken-sessions tool — `list` only; see `_CREDENTIAL_ONLY_PATHS` for `purge`.
    ("GET", "/api/agent/runner-breaks"): "runner_breaks.list",
}


def agent_api_request(
    method: str,
    path: str,
    payload: dict[str, Any] | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    env_path: Path = DEFAULT_ENV_PATH,
) -> tuple[int, bytes]:
    """One request against an `/api/agent/*` endpoint — daemon first, NODE_TOKEN second — returning
    `(status_code, body)` so no caller has to know which leg answered.

    The pair maps to a verb through `_AGENT_PATH_VERBS`; UNMAPPED raises (read that table's comment
    before adding a row).

    THE SOCKET LEG SYNTHESISES `200`: the daemon returns a result only on an acceptable CP answer; a CP
    4xx becomes a raised refusal instead. Callers branching on `status >= 400` keep working.

    Three-way split as everywhere: result returned, refusal raised, only "no answer" reaches
    `_token_api_request`."""
    bare_path, _, query = path.partition("?")
    pair = (method.upper(), bare_path)
    # Vetted operator-only: skip the socket.
    if pair in _CREDENTIAL_ONLY_PATHS:
        return _token_api_request(method, path, payload, timeout=timeout, env_path=env_path)
    verb = _AGENT_PATH_VERBS.get(pair)
    if verb is None:
        raise ControlPlaneApiError(
            f"{method} {bare_path} has no verb in control_plane_client._AGENT_PATH_VERBS — this "
            f"helper cannot forward an arbitrary agent path (that would be the passthrough "
            f"apps/jaahd/agent_vocabulary.py refuses to be). Add a Verb there and a row here, or call the "
            f"function that owns this route."
        )
    # Query parameters become `args` (last value wins, as urllib reads them); a body `payload` merges on
    # top. The verb's `query` tuple splits them CP-side.
    args: dict[str, Any] = {
        key: values[-1]
        for key, values in urllib.parse.parse_qs(query, keep_blank_values=True).items()
    } if query else {}
    if payload:
        args.update(payload)
    result = _via_socket(verb, args, timeout=timeout)
    if result is not _NO_SOCKET:
        return 200, json.dumps(result).encode("utf-8")
    return _token_api_request(method, path, payload, timeout=timeout, env_path=env_path)


def create_system_task(
    persona: str,
    project: str,
    title: str,
    body: str,
    state: str = "ready",
    dedup_open: bool = True,
    dedup_key: str | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    env_path: Path = DEFAULT_ENV_PATH,
    project_id: int | None = None,
) -> dict[str, Any]:
    """Create (or dedup-match + body-refresh) a HIDDEN system Task — daemon `system_task.create` first,
    else `POST /api/agent/tasks/system` with this node's token. Returns `{"id": int, "created": bool}`.
    Raises `ControlPlaneApiError` on a refusal or any non-2xx.

    Who may create is decided CP-side (`_guard_task_creating_caller`, R30); on the socket leg the persona
    header is process-derived, and a refusal is RAISED, never retried with a credential.

    `dedup_key` narrows the one-open-per-(persona, project) match to that key. REQUIRED for
    `persona='adhoc'` (422 without — a coarse adhoc match clobbers unrelated rows).

    `project_id` is the project's identity when the caller knows it; the CP prefers it and falls back to
    the `project` name (resolved inside the calling node's org) only when it is absent."""
    payload: dict[str, Any] = {
        "persona": persona, "project": project, "title": title, "body": body,
        "state": state, "dedup_open": dedup_open,
    }
    if project_id is not None:
        payload["project_id"] = int(project_id)
    if dedup_key is not None:
        payload["dedup_key"] = dedup_key
    result = _via_socket("system_task.create", payload)
    if result is not _NO_SOCKET:
        # As the token leg: an unexpected body shape is handed back as `{}`.
        return result if isinstance(result, dict) else {}
    status, raw = _token_api_request(
        "POST", "/api/agent/tasks/system", payload, timeout=timeout, env_path=env_path,
    )
    if status >= 400:
        raise ControlPlaneApiError(
            f"POST /api/agent/tasks/system (persona={persona} project={project}) -> HTTP {status}: "
            f"{raw.decode(errors='replace')[:200]}"
        )
    return json.loads(raw) if raw else {}


def find_open_system_task(
    persona: str,
    project: str,
    dedup_key: str | None = None,
    timeout: float = DEFAULT_TIMEOUT,
    env_path: Path = DEFAULT_ENV_PATH,
    project_id: int | None = None,
) -> int | None:
    """The id of the one open system Task for (persona, project) — daemon `system_task.open` first, else
    `GET /api/agent/tasks/system/open`. None when nothing is open; raises `ControlPlaneApiError` on a
    refusal or non-2xx. Nothing-open and a failed lookup must not converge.

    `dedup_key` narrows to the row THAT key opened, for a producer with several rows per (persona,
    project). `project_id`, when known, is the identity the CP matches on (as `create_system_task`)."""
    params: dict[str, str] = {"persona": persona, "project": project}
    if project_id is not None:
        params["project_id"] = str(int(project_id))
    if dedup_key:
        params["dedup_key"] = dedup_key
    data = _via_socket("system_task.open", dict(params), timeout=timeout)
    if data is _NO_SOCKET:
        query = urllib.parse.urlencode(params)
        status, raw = _token_api_request(
            "GET", f"/api/agent/tasks/system/open?{query}", timeout=timeout, env_path=env_path
        )
        if status >= 400:
            raise ControlPlaneApiError(
                f"GET /api/agent/tasks/system/open (persona={persona} project={project}) -> HTTP "
                f"{status}: {raw.decode(errors='replace')[:200]}"
            )
        data = json.loads(raw) if raw else {}
    task_id = data.get("id") if isinstance(data, dict) else None
    return task_id if isinstance(task_id, int) else None


def resolve_system_task(
    task_id: int,
    outcome_summary: str,
    timeout: float = DEFAULT_TIMEOUT,
    env_path: Path = DEFAULT_ENV_PATH,
) -> dict[str, Any]:
    """Resolve an OUTCOME-criterion system Task to `done` (states owned by
    `task_lifecycle._RESOLVABLE_SYSTEM_TASK_STATES`) via `POST /api/agent/tasks/system/{id}/resolve`.
    Returns `{"resolved": bool}`; raises `ControlPlaneApiError` on a refusal or any non-2xx.

    `task_id` names SOMEONE ELSE'S row (an alarm this producer opened), not the caller's own task, which
    `agent_vocabulary._TASK_ID_KEYS` refuses to let any caller name."""
    # CREDENTIAL LEG ONLY, BY DESIGN — no `system_task.resolve` verb: ids are enumerable via
    # `system_task.open` and the CP handler has no caller predicate, so any local caller could close any
    # alarm. Every real caller runs as the operator or the daemon.
    status, raw = _token_api_request(
        "POST", f"/api/agent/tasks/system/{task_id}/resolve",
        {"outcome_summary": outcome_summary}, timeout=timeout, env_path=env_path,
    )
    if status >= 400:
        raise ControlPlaneApiError(
            f"POST /api/agent/tasks/system/{task_id}/resolve -> HTTP {status}: "
            f"{raw.decode(errors='replace')[:200]}"
        )
    return json.loads(raw) if raw else {}


if __name__ == "__main__":
    print(__doc__)
