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
data (spec R5, NFR Consistency). stdlib-only (urllib) plus two daemon leaves loaded by path through
`jaahd_module` (never copied here): `env_helpers` for the `.env` rule and the clone-root rule, and
`node_token_http` for the one opener that may carry `X-Agent-Token`.
"""
from __future__ import annotations

import http.cookiejar
import json
import os
import urllib.error
import sys
import urllib.request
from pathlib import Path
from typing import Any

# Explicit insert for importers; `tools/` is sys.path[0] only when run as a script.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import runner_env  # noqa: E402
import jaah_paths  # noqa: E402
import jaahd_module  # noqa: E402
from jaah_env_names import env_get  # noqa: E402

# The daemon's leaves, by path (`tools/jaahd_module.py`): `env_helpers.dotenv_value` is THE `.env` reading
# rule (last `KEY=` wins, inline `#` cut, quotes and spaces dropped) and `env_helpers.resolve_clone_root` THE
# per-node clone-root rule; `node_token_http.OFF_HOST_SAFE_OPEN` is the one opener a node-token request may use.
_env_helpers = jaahd_module.load("env_helpers.py", "env_helpers", siblings=True)
_node_token_http = jaahd_module.load("node_token_http.py", "node_token_http")

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
    """KEY from the hub's own .env by the daemon's `dotenv_value` rule; "" when the file is absent."""
    if not env_path.is_file():
        return ""
    return _env_helpers.dotenv_value(env_path.read_text(encoding="utf-8"), key)


# No SSM-helper arm: the broker no longer serves `node_token`. Both remaining arms are the
# OPERATOR's.
def _resolve_node_token(env_path: Path = DEFAULT_ENV_PATH) -> str:
    """NODE_TOKEN from the process env (what `compose_env_guard` injects), then the repo `.env`; "" on a
    cloud node, where there is nothing further to ask."""
    return (
        os.environ.get("NODE_TOKEN", "").strip()
        or _read_env_value("NODE_TOKEN", env_path)
    )


# The `X-Agent-Task-Id` rule lives with the identity it reads (`runner_env.caller_task_id`); bound here for its callers.
caller_task_id = runner_env.caller_task_id


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
    status means. Only a transport failure propagates.

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


# ── Local clone-path resolution ───────────────────────────────────────────────────────────────────────
# A project's repos root is COMPUTED per node as `<JAAH_HOME>/repos/<project_id>` (one stored path could be
# right on only one node); its slots are `<N>` beneath it. The node-side rule is the daemon's
# `env_helpers.resolve_clone_root` (its server twin: `apps/api/app/node_clone.py`), bound here for its
# importers: "" when the id is missing or JAAH_HOME is unset/`/` — no implicit default, never a raise.
resolve_clone_root = _env_helpers.resolve_clone_root


def _digit_dirs(parent: str) -> list[str]:
    """`parent`'s digit-named children, numerically ordered (`_repos`, `main` and scratch are not slots)."""
    try:
        names = os.listdir(parent)
    except OSError:
        return []
    return [os.path.join(parent, n) for n in sorted((n for n in names if n.isascii() and n.isdigit()), key=int)]


def resolve_slot_clones(project_id: int | None) -> list[str]:
    """Every clone of a project's PRIMARY repository THIS node holds, in a stable order: a legacy `main` anchor
    if one is still on disk, then each slot `<jaah_home>/repos/<id>/<N>` numerically. `[]` when
    `resolve_clone_root` cannot place the project.

    The pool has no slot 0 and `apps/jaahd/legacy_layout.py` erases `main` clones, so on a migrated node
    `resolve_clone_root`'s path does not exist — a per-clone audit walks this list instead. Non-primary
    repositories (`_repos/<rid>/<N>`) are left out: each may declare its own branch policy and base
    (`apps/jaahd/slot_repos.py`), so the project row's values are not theirs to judge or install."""
    anchor = resolve_clone_root(project_id)
    if not anchor:
        return []
    candidates = [anchor, *_digit_dirs(os.path.dirname(anchor))]
    return [c for c in candidates if os.path.exists(os.path.join(c, ".git"))]


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
        # Never plain `urlopen`: a 307 off the control plane would carry the token to the other host.
        with _node_token_http.OFF_HOST_SAFE_OPEN(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise ControlPlaneApiError(f"{method} {url} failed: {exc}") from exc


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


if __name__ == "__main__":
    print(__doc__)
