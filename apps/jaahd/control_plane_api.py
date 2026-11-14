"""Control-plane API client for jaahd: the token-carrying `urllib` client with 401 token-rotation
retry, and the per-route wrappers. The daemon's only CP API client.

One base per call (`resolve_control_plane_api_base`): `JAAH_CP_BASE_URL`, else the local loopback — which
is NOT the CP on a remote node, so every node's unit sets the override (an explicit operator opt-in).
STDLIB ONLY, no DB driver. A wire field's reader and writer ship together.

The DAEMON's, not user `jaah`'s: every call presents this node's token (`_agent_headers`). `jaah`-side
callers name a verb on the daemon's socket instead (`agent_socket`/`agent_vocabulary`); jaahd never
routes its OWN calls through its own socket (deadlock, credential loop).
"""
from __future__ import annotations

import http.client
import json
import logging
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from typing import Any

import environment
import node_settings
import node_token_http
import plugin_repair
import runner_env
import token_revocation
from env_helpers import (
    _REPO_ROOT, _dotenv_lines, node_env_path, resolve_node_token,
)

log = logging.getLogger("jaahd.control_plane_api")

# The slot's API_PORT from the launcher env; default 8020.
CONTROL_PLANE_API_PORT = int(os.environ.get("API_PORT", "8020"))
NODE_TOKEN = os.environ.get("NODE_TOKEN", "")


def resolve_control_plane_api_base() -> str:
    """The base URL every call in this module is built on: `JAAH_CP_BASE_URL` override, else
    `http://127.0.0.1:<CONTROL_PLANE_API_PORT>` on a provisioned node — and in SANDBOX posture with neither, no
    base at all.

    Resolved by `environment.control_plane_base` (the single home). `CONTROL_PLANE_API_PORT` is passed
    IN because tests monkeypatch it to aim at a stub. In SANDBOX posture with no override it RAISES
    `environment.SandboxControlPlaneUnset` rather than aim at a live loopback console. Read per call;
    trailing slashes stripped."""
    return environment.control_plane_base(CONTROL_PLANE_API_PORT)


def _json_default(obj: object) -> str:
    if isinstance(obj, datetime):
        return obj.isoformat()
    raise TypeError(f"not JSON serializable: {type(obj)}")


def _agent_headers() -> dict[str, str]:
    # Sent on every call (one builder beats a per-call opt-in); `runner_env.caller_task_id` holds the rationale.
    h: dict[str, str] = {"Content-Type": "application/json", "X-Agent-Task-Id": runner_env.caller_task_id()}
    # Both arms are the daemon's own. The module `NODE_TOKEN` leads because the 401 retry writes a
    # rotated value there. Never add a broker arm: user `jaah` must have no path to the token
    # (`cred_broker`'s module docstring).
    token = NODE_TOKEN or resolve_node_token()
    if token:
        h["X-Agent-Token"] = token
    return h


def _refresh_node_token_from_env() -> str | None:
    """Re-read NODE_TOKEN off disk (os.environ never revisits a set value), so a rotated token self-heals.
    Returns the value, or None when no file declares a NODE_TOKEN line (an empty string IS a value).

    `/etc/jaah/node.env` first; then this daemon's own jaah checkout `.env` (dev only — never a managed
    project's)."""
    for path in (node_env_path(), _REPO_ROOT / ".env"):
        try:
            for key, val in _dotenv_lines(path):
                if key == "NODE_TOKEN":
                    return val
        except FileNotFoundError:
            continue
        except Exception as exc:
            log.warning("failed to re-read NODE_TOKEN from %s for 401 retry: %s", path, exc)
    return None


# The CP's error text, stashed on the re-raised `HTTPError` (an attribute, so callers still catch `HTTPError`).
CP_ERROR_BODY_ATTR = "jaah_cp_error_body"

# Far above the 200 logged chars so the body still parses: `cred_broker._rejected_reason` must drop the
# echoed `input` structurally. Bounded: the peer does not choose the size.
_MAX_CP_ERROR_BODY = 8192


def cp_error_body(exc: BaseException) -> str:
    """The control plane's error text for a raised `HTTPError`, or "" when none was captured.

    Read by `cred_broker` for the `rejected` wire reason."""
    value = getattr(exc, CP_ERROR_BODY_ATTR, "")
    return value if isinstance(value, str) else ""


# The off-host redirect guard (a 307 to presigned S3 must not carry the node's bearer token) lives in the
# stdlib-only leaf `node_token_http`, shared with `tools/control_plane_client.py`; bound under the names
# this module's callers and `microvm_bridge` reach.
_origin = node_token_http.origin
_OFF_HOST_SAFE_OPEN = node_token_http.OFF_HOST_SAFE_OPEN


#: The most bytes one response may move into memory. A ceiling because an unstreamed body is resident in the
#: root daemon, whose OOM takes the fleet down.
MAX_RESPONSE_BYTES = 100 * 1024 * 1024

#: The most bytes one STREAMED response may move (`agent_api_stream_bytes`, peak one chunk): equal to
#: `app/routes/attachments.py::MAX_ATTACHMENT_SIZE` (pinned by `tests/test_control_plane_api.py`), so any
#: file the CP stores this node can fetch.
MAX_STREAM_BYTES = 5 * 1024 * 1024 * 1024


def _drain(response, sink, cap: int) -> bytes:
    """Read one response body: into `sink` in bounded chunks, or into memory when there is no sink.

    The cap is applied BEFORE allocation on both arms: the declared length up front, the running total
    mid-stream (a `Content-Length` is only a claim). With a sink the peak is one chunk."""
    # The declared length is an optional hint; the `cap + 1` read and running total are what bind.
    headers = getattr(response, "headers", None)
    declared = headers.get("content-length", "") if headers else ""
    if declared.isdigit() and int(declared) > cap:
        raise RuntimeError(
            f"the control plane answered {declared} bytes, over this node's {cap} byte ceiling"
        )
    if sink is None:
        payload = response.read(cap + 1)
        if len(payload) > cap:
            raise RuntimeError(f"the response exceeded this node's {cap} byte ceiling")
        return payload
    total = 0
    while chunk := response.read(_RESPONSE_CHUNK_BYTES):
        total += len(chunk)
        if total > cap:
            raise RuntimeError(f"the response exceeded this node's {cap} byte ceiling mid-stream")
        sink.write(chunk)
    return b""


#: One megabyte per read.
_RESPONSE_CHUNK_BYTES = 1024 * 1024

#: The opener every CP call uses instead of urllib's when set (`set_transport`): a Windows MicroVM node's backend,
#: which holds no `NODE_TOKEN`, reaches the CP through the Windows daemon (`microvm_bridge`).
_TRANSPORT_OPEN = None


# Conditional GET (ETag / If-None-Match) for the node's hot rosters. The CP fingerprints these answers
# (`apps/api/app/conditional_get.py::CONDITIONAL_PATHS`, the twin of this set — change both or neither) and
# answers an unchanged one with a body-less 304, which this cache turns back into the body last seen. Keyed by
# the full URL (the query carries the node and project); a handful of entries per node, so the bound is loose.
CONDITIONAL_PATHS = frozenset({"/api/agent/pool-tick", "/api/agent/accounts", "/api/agent/worktrees"})
_CONDITIONAL_MAX_ENTRIES = 64
_conditional_cache: dict[str, tuple[str, bytes]] = {}
_conditional_lock = threading.Lock()


def _conditional_key(req: urllib.request.Request, sink) -> str | None:
    """The cache key for a request this cache serves — a plain GET of a `CONDITIONAL_PATHS` path — else None."""
    if sink is not None or req.get_method() != "GET":
        return None
    if urllib.parse.urlsplit(req.full_url).path not in CONDITIONAL_PATHS:
        return None
    return req.full_url


def _conditional_prepare(key: str | None, req: urllib.request.Request) -> tuple[str, bytes] | None:
    """Add `If-None-Match` for a body this cache holds; returns that entry (None = ask unconditionally)."""
    if key is None:
        return None
    with _conditional_lock:
        cached = _conditional_cache.get(key)
    if cached is not None:
        req.add_header("If-None-Match", cached[0])
    return cached


def _carries_intents(key: str, body: bytes) -> bool:
    """Whether a pool-tick body holds an intent: a `lease_slot` carries the runner's env in the clear, which this
    cache must not keep in memory past the request that brought it. Unparseable counts as carrying one."""
    if urllib.parse.urlsplit(key).path != "/api/agent/pool-tick":
        return False
    try:
        parsed = json.loads(body)
    except ValueError:
        return True
    return not isinstance(parsed, dict) or bool(parsed.get("intents") or parsed.get("microvm_intents"))


def _conditional_remember(key: str | None, response, body: bytes) -> None:
    """Keep a 200's body under its `ETag`, or forget the key when the answer carried none (or carried intents)."""
    if key is None:
        return
    headers = getattr(response, "headers", None)
    etag = headers.get("ETag", "") if headers else ""
    with _conditional_lock:
        _conditional_cache.pop(key, None)
        if not etag or _carries_intents(key, body):
            return
        while len(_conditional_cache) >= _CONDITIONAL_MAX_ENTRIES:
            _conditional_cache.pop(next(iter(_conditional_cache)))
        _conditional_cache[key] = (etag, body)


def set_transport(open_url) -> None:
    """Route every CP call through `open_url(req, timeout=)` (`urlopen`'s shape), or back through urllib on None."""
    global _TRANSPORT_OPEN
    _TRANSPORT_OPEN = open_url


def _agent_urlopen(req: urllib.request.Request, *, expected: tuple[int, ...] = (),
                   timeout: float = 30.0, opener=None, sink=None,
                   cap: int = MAX_RESPONSE_BYTES) -> bytes:
    """urlopen with a distinct log on HTTP errors, so a persistent 401 (token misconfig) is diagnosable.

    `expected` statuses are ones the caller classifies itself: logged at DEBUG, still raised. 401 is
    never `expected` (its self-heal must run). `timeout` is per route (`agent_vocabulary.Verb.timeout_s`).

    A 401 gets one retry with a freshly re-read NODE_TOKEN (an API-stack restart rotates it on disk); if
    the on-disk value is unchanged it is a genuine auth failure and raises immediately."""
    global NODE_TOKEN
    if not token_revocation.WATCH.admit():
        # Backing off a 401 streak: refused HERE, as a transport failure every caller already handles.
        raise urllib.error.URLError("NODE_TOKEN refused by the control plane; backing off (token_revocation)")
    # `opener=_OFF_HOST_SAFE_OPEN` for the one caller that may be redirected off-host.
    open_url = _TRANSPORT_OPEN or opener or urllib.request.urlopen
    conditional = _conditional_key(req, sink)
    cached = _conditional_prepare(conditional, req)
    try:
        response = open_url(req, timeout=timeout)
        body_bytes = _drain(response, sink, cap)
        _conditional_remember(conditional, response, body_bytes)
        token_revocation.WATCH.observe_answer()
        return body_bytes
    except urllib.error.HTTPError as exc:
        if exc.code == 304 and cached is not None:
            # Unchanged since the body this cache holds: the CP answered, so the token is good.
            exc.close()
            token_revocation.WATCH.observe_answer()
            return cached[1]
        detail = ""
        body = ""
        # ONLY A BODY THE CONTROL PLANE AUTHORED IS EVER READ: an S3 error body (after a redirect) echoes
        # the signed request, including the node's STS session token. Decided from `exc.url` (what
        # actually answered), so any future redirecting caller inherits the scrub. Status, method and
        # selector are still logged.
        authored_here = _origin(getattr(exc, "url", "") or req.full_url) == _origin(req.full_url)
        try:
            if authored_here:
                body = exc.read().decode(errors="replace")[:_MAX_CP_ERROR_BODY]
            else:
                exc.read()  # drain it, so the connection is not left holding an unread body
        except Exception:
            pass
        # The log keeps 200 chars; only the stash gets the fuller body.
        detail = body[:200]
        # Stashed because `exc.read()` is single-shot: without it a CP 422 reaches a runner as `unavailable`
        # and it retries a request only it can fix. Set for every arm below.
        setattr(exc, CP_ERROR_BODY_ATTR, body)
        # Any status but 401 that the CP itself authored proves the token is still accepted.
        if authored_here and exc.code != 401:
            token_revocation.WATCH.observe_answer()
        if exc.code == 401:
            fresh = _refresh_node_token_from_env()
            # Compare against the token THIS request sent, not the global a sibling thread may have
            # advanced. urllib normalizes header keys to "X-agent-token".
            sent = req.get_header("X-agent-token")
            if fresh is not None and fresh != sent:
                log.warning("NODE_TOKEN rotated in .env — refreshing and retrying once")
                NODE_TOKEN = fresh
                if fresh:
                    req.add_header("X-Agent-Token", fresh)
                else:
                    # Match `_agent_headers`: no header for a falsy token.
                    req.remove_header("X-agent-token")
                try:
                    # Cannot double-write the sink: `HTTPError` is raised off the status line, before any body.
                    response = open_url(req, timeout=timeout)
                    body_bytes = _drain(response, sink, cap)
                    _conditional_remember(conditional, response, body_bytes)
                    token_revocation.WATCH.observe_answer()
                    return body_bytes
                except urllib.error.HTTPError as retry_exc:
                    if retry_exc.code == 304 and cached is not None:
                        retry_exc.close()
                        token_revocation.WATCH.observe_answer()
                        return cached[1]
                    if _origin(getattr(retry_exc, "url", "") or req.full_url) == _origin(req.full_url):
                        if retry_exc.code == 401:
                            token_revocation.WATCH.observe_401(fresh)
                        else:
                            token_revocation.WATCH.observe_answer()
                    # A different exception object: its own stash and its own origin test.
                    try:
                        if _origin(getattr(retry_exc, "url", "") or req.full_url) == _origin(
                                req.full_url):
                            setattr(retry_exc, CP_ERROR_BODY_ATTR,
                                    retry_exc.read().decode(errors="replace")[:_MAX_CP_ERROR_BODY])
                        else:
                            retry_exc.read()
                            setattr(retry_exc, CP_ERROR_BODY_ATTR, "")
                    except Exception:
                        setattr(retry_exc, CP_ERROR_BODY_ATTR, "")
                    log.error(
                        "API %s %s → HTTP %s after token refresh (retry failed)",
                        req.get_method(), req.selector, retry_exc.code,
                    )
                    raise
            log.error(
                "API %s %s → HTTP 401 %s (NODE_TOKEN unchanged — genuine auth failure)",
                req.get_method(), req.selector, detail,
            )
            if authored_here:
                # A 401 streak backs off, and one that outlasts the platform's window is a deleted node.
                token_revocation.WATCH.observe_401(sent)
            raise
        if exc.code in expected:
            log.debug(
                "API %s %s → HTTP %s (declared expected by the caller, which classifies it) %s",
                req.get_method(), req.selector, exc.code, detail,
            )
            raise
        log.error("API %s %s → HTTP %s %s", req.get_method(), req.selector, exc.code, detail)
        raise


def _agent_json(method: str, path: str, payload: dict | None = None, *, want: type = dict,
                expected: tuple[int, ...] = (), timeout: float | None = None):
    """One JSON exchange with the local control-plane API: the answer when it is a `want` (dict or list),
    else `want()` — so a 204/empty body, a JSON `null` (e.g. an empty claim) or an answer of another shape
    needs no special-casing by a consumer. Raises whatever `_agent_urlopen` raises; `expected` statuses
    still raise but log at DEBUG there, for a caller that classifies them itself."""
    data = json.dumps(payload, default=_json_default).encode() if payload is not None else None
    req = urllib.request.Request(f"{resolve_control_plane_api_base()}{path}", data=data, headers=_agent_headers(),
                                 method=method)
    # Options only when given: the suite's transport fakes stand in for `_agent_urlopen(req)`.
    opts: dict[str, Any] = {**({"expected": expected} if expected else {}), **({"timeout": timeout} if timeout is not None else {})}
    body = _agent_urlopen(req, **opts)
    parsed = json.loads(body) if body else None
    return parsed if isinstance(parsed, want) else want()


def _agent_api_post(path: str, payload: dict, *, expected: tuple[int, ...] = ()) -> dict:
    """POST JSON to the local control-plane API; `_agent_json`'s dict coercion. Ported from the former host-agent."""
    return _agent_json("POST", path, payload, expected=expected)


def _agent_api_get(path: str) -> dict:
    """GET JSON from the local control-plane API; `_agent_json`'s dict coercion."""
    return _agent_json("GET", path)


def _agent_api_get_list(path: str) -> list:
    """GET a JSON array from the local control-plane API; `_agent_json`'s coercion, for a list."""
    return _agent_json("GET", path, want=list)


def _best_effort(
    label: str, method: str, path: str, body: dict | None = None, *, default=None, failed: str | None = None,
):
    """One best-effort call: the endpoint's dict on success, `default` on ANY failure, which is logged as
    a warning (`"<label>: <failed or '<method> failed'>: <exc>"`) and never raised — these wrappers run
    from the daemon's ticks and idle loops, which no unreachable CP may wedge. A caller that must SEE the
    failure (a forwarder keeping its cursor, a claim that fails closed) does not go through here."""
    try:
        return _agent_api_get(path) if method == "GET" else _agent_api_post(path, body or {})
    except Exception as exc:  # noqa: BLE001 — every failure is one sentinel; the caller retries next tick
        log.warning("%s: %s: %s", label, failed or f"{method} failed", exc)
        return default


def agent_api_accounts(include_disabled: bool = False) -> list[dict]:
    """The credential-account set the dispatcher selects from, read from the DB SSOT via
    `GET /api/agent/accounts`. Each element is `{slug, config_dir, enabled, node_id, runners}`; ENABLED
    only unless `include_disabled`. This node's id is always sent: `config_dir` is a per-node answer the CP
    computes. The account's token store is the node's own resolution (`shared_storage.securestorage_dir`).

    No `quota` field: a node publishes only quota it observed itself (carry-forward is `fleet_observe.account_upsert_statement`).

    An unreachable API RAISES; only an empty/204/non-list body yields `[]` ("no accounts").
    `reconcile.accounts_probe.account_roster` depends on that distinction."""
    params = [f"node_id={urllib.parse.quote(this_node_id())}"]
    if include_disabled:
        params.append("include_disabled=1")
    return _agent_api_get_list("/api/agent/accounts?" + "&".join(params))


def account_enabled(config_dir: str) -> bool | None:
    """Whether `config_dir` is a known, ENABLED credential account per the DB SSOT.

    True/False, or None when not a known account dir. A data lookup, not a policy decision. Raises when
    the API is unreachable — never read as "unknown dir"."""
    norm = (config_dir or "").rstrip("/")
    for rec in agent_api_accounts(include_disabled=True):
        if isinstance(rec, dict) and (rec.get("config_dir") or "").rstrip("/") == norm:
            return rec.get("enabled", True) is not False
    return None

def this_node_id() -> str:
    """This daemon's own node id, for per-node pool readers that have no node identity threaded to them.

    The single home (`main.resolve_node_id` delegates here). A node id is ASSIGNED, never derived. From
    `/etc/jaah/node.env` via the unit's `EnvironmentFile`.

    "" when unset, which readers pass through as "no filter" — never an empty pool."""
    return os.environ.get("NODE_ID", "")


def agent_api_pool_worktrees(node_id: str = "") -> list[dict] | None:
    """The WHOLE pool as this node's out-of-process readers need it — `GET /api/agent/worktrees` with no
    `project` filter (spec nats-to-postgres §6.6).

    None on a transport failure, `[]` only on a genuine empty 200. Callers read emptiness incompatibly
    ("nothing stuck", "no live session", "pool empty"), so each keeps its own posture for None."""
    query = urllib.parse.urlencode({"node_id": str(node_id)}) if node_id else ""
    try:
        return _agent_api_get_list(f"/api/agent/worktrees{'?' + query if query else ''}")
    except Exception as exc:  # noqa: BLE001 — every transport failure is one sentinel: "we do not know"
        log.warning("agent_api_pool_worktrees: API unreachable: %s", exc)
        return None


def agent_api_worktree(
    project: str, number: object, node_id: str = "", project_id: int | None = None,
) -> dict | None:
    """ONE slot's record, as a filter over the same endpoint rather than a second endpoint.

    Matched on `project_id` when the caller knows it and the record carries one (a name is display only);
    else on the name, which the node's one org keeps unambiguous.

    None both when absent and when unreachable: a single-slot caller has no posture to choose between."""
    records = agent_api_pool_worktrees(node_id)
    if records is None:
        return None
    wanted = str(number)
    for record in records:
        if str(record.get("number")) != wanted:
            continue
        if project_id is not None and record.get("project_id") is not None:
            if record.get("project_id") == project_id:
                return record
        elif record.get("project") == project:
            return record
    return None


def agent_api_pool_tick(node_id: str) -> dict:
    """One pass's CP instruction bundle for NODE_ID, read from `GET /api/agent/pool-tick`.

    * `roster` — the authoritative pool membership. An entry missing
      `clone_url`, `base` or `clone` is UNUSABLE: skip that slot and publish nothing for it; never
      substitute `""`, which would skip crash triage and fall through to `free`.
    * `intents` — the pending `slot_intent` set, parsed by `reconcile.tick_wire.parse_pool_tick` and acked with
      `ack_slot_intent`. There is no separate fetch for it: the tick IS the work-list.
    * `node_settings` — the CP's node tunables, taken here off every body (`node_settings.update_from_tick`)
      so no caller can fetch a tick without refreshing them.
    * `remote_settings_consent_hash` — this node's org's managed-settings consent, taken off every body the same
      way (`plugin_repair.take_consent_hash_from_tick`).

    Best-effort `{}` on an unreachable API: "no instruction this pass" is safe for roster and intents."""
    query = urllib.parse.urlencode({"node_id": str(node_id)})
    try:
        body = _agent_api_get(f"/api/agent/pool-tick?{query}")
    except urllib.error.URLError as exc:
        log.warning("agent_api_pool_tick: API unreachable for node %s: %s", node_id, exc)
        return {}
    node_settings.update_from_tick(body)
    plugin_repair.take_consent_hash_from_tick(body)
    return body


def post_microvm_facts(vm_id: str, body: dict) -> dict:
    """One MicroVM state transition, `POST /api/agent/microvms/{vm_id}/facts {generation, intent_seq, state,
    reason}` (the MicroVM design's §4.2b). RAISES on any failure: `microvm_rail` keeps an undelivered fact
    and re-sends it, so a lost post must be visible to it."""
    return _agent_api_post(f"/api/agent/microvms/{urllib.parse.quote(str(vm_id), safe='')}/facts", body)


def agent_api_port_grid() -> dict:
    """The pool's node-assigned port range, read from the DB SSOT via `GET /api/agent/port-grid`:
    `{"auto_port_floor", "auto_port_ceiling"}` — fleet-global operator knobs (Settings), for readers with no
    slot in hand (`console_direct.pool_auto_port_range`).

    RAISES on an unreachable API, and on a body missing either knob — `{}` from a 204/empty body must not
    read as "the defaults"."""
    body = _agent_api_get("/api/agent/port-grid")
    missing = [field for field in ("auto_port_floor", "auto_port_ceiling") if field not in body]
    if missing:
        raise RuntimeError(
            f"GET /api/agent/port-grid answered without {missing} — a truncated or empty response. "
            f"Refusing to read it as the default range"
        )
    return body


# NO node-side copy of the slot-intent VERB set (`apps/api/app/fleet_verdicts.py`): a verb missing from a
# copy would be dropped un-acked and re-served forever. The node routes on `reconcile.execute._INTENT_EXECUTORS`
# and answers anything else `unsupported_verb`; `tests/test_slot_intent_vocabulary.py` pins the coupling.

# Ack outcomes this node EMITS: a SUBSET of `fleet_verdicts.SLOT_INTENT_OUTCOMES` (the CP 422s an unknown
# one, and the intent is re-served forever), pinned by the same drift test. `done`, `accepted`,
# `unsupported_verb` and `clone_refused` retire an intent; `refused_stale`, `refused_precondition` and `failed`
# leave it pending.
SLOT_INTENT_DONE = "done"
SLOT_INTENT_UNSUPPORTED_VERB = "unsupported_verb"
SLOT_INTENT_CLONE_REFUSED = "clone_refused"
SLOT_INTENT_OUTCOMES = frozenset({
    SLOT_INTENT_DONE, SLOT_INTENT_UNSUPPORTED_VERB, SLOT_INTENT_CLONE_REFUSED,
    "accepted", "refused_stale", "refused_precondition", "failed",
})


def ack_slot_intent(
    intent_id: object,
    outcome: str,
    reason: str | None = None,
    *,
    generation: int | None = None,
    dispatch_id: str | None = None,
    pid: int | None = None,
    pid_start_time: str | None = None,
    session_id: str | None = None,
    report: dict | None = None,
) -> bool:
    """Ack one enacted `slot_intent` via `POST /api/agent/slot-intents/{id}/ack`.

    Idempotent both sides (an un-acked intent is redelivered). True when the ack was DELIVERED; False on
    a transport failure.

    The reason rides as `detail` (the CP's `SlotIntentAckIn` name; `reason` is silently dropped).
    `generation` + `dispatch_id` are the launch fence echoed from the intent's payload — without them the
    hold stays `dispatching` and is reclaimed under a live runner. Omitted fields are not sent. The pair is
    ALL-OR-NOTHING (raised here): half a pair silently records no launch identity. `report` is
    `destroy_slot`'s outcome (`reconcile.enact_signal._enact_destroy_slot`), read by the CP's `app/slot_destroy.py`."""
    if outcome not in SLOT_INTENT_OUTCOMES:
        raise ValueError(
            f"ack_slot_intent: unknown outcome {outcome!r} — expected one of {sorted(SLOT_INTENT_OUTCOMES)}"
        )
    if (generation is None) != (dispatch_id is None):
        raise ValueError(
            f"ack_slot_intent: the launch fence is a PAIR — got generation={generation!r} "
            f"dispatch_id={dispatch_id!r}. Half of it records no launch identity at all."
        )
    body: dict = {"outcome": outcome, "detail": reason}
    for key, value in (
        ("generation", generation), ("dispatch_id", dispatch_id), ("pid", pid),
        ("pid_start_time", pid_start_time), ("session_id", session_id), ("report", report),
    ):
        if value is not None:
            body[key] = value
    try:
        _agent_api_post(f"/api/agent/slot-intents/{intent_id}/ack", body)
        return True
    except urllib.error.URLError as exc:  # HTTPError too: a refusal is logged, the intent is redelivered
        log.warning("ack_slot_intent: no ack recorded for intent %s: %s", intent_id, exc)
        return False


def claim_task_active(task_id: object, *, session_id: str = "") -> bool:
    """`ready → active` for a task this node just launched — `POST /api/agent/tasks/claim-active`. True iff
    this call won the claim (False when it was already claimed, or on any failure).

    Called right after every launch (`fleet_dispatch.launch_runner`, `_launch_no_model`); a model runner's
    SessionStart hook (`hooks/runner-status.py::rs_start`) repeats it, idempotently. Best-effort, logged. A
    transient failure (`_transient`) is re-sent by `retry_launch_reports` while the runner `session_id` names
    is still live here: without it a CP blip at launch left a working runner's task `ready` — shown Queued —
    until its first turn ended."""
    path, payload = "/api/agent/tasks/claim-active", {"task_id": int(task_id)}
    try:
        body = _agent_api_post(path, payload)
    except Exception as exc:  # noqa: BLE001 — bookkeeping; a transient failure is re-sent (`retry_launch_reports`)
        log.warning("claim_task_active: could not claim task %s active: %s: %s", task_id, type(exc).__name__, exc)
        _remember_launch_report("claim", task_id, session_id, path, payload, exc)
        return False
    _forget_launch_report("claim", task_id, session_id)
    return body.get("claimed") is True


# ── Launch reports a transient CP failure dropped ─────────────────────────────────────────────────────
# The two writes a launch owes the CP — the runner binding (`set_runner_binding`) and the `ready → active`
# claim (`claim_task_active`) — are one-shot. A deploy's few failing minutes (seen: a 500 on every task
# read for 3.5 min) lost both, and the task read Queued for 16 minutes while its runner worked. Each such
# report waits here and `retry_launch_reports` re-sends it every `LAUNCH_REPORT_RETRY_INTERVAL_S` until it
# lands, the CP refuses it, its runner is no longer live on this node, or `LAUNCH_REPORT_RETRY_WINDOW_S`
# passes. A runner not yet registered (the binding is sent before the spawn) is waited for up to
# `LAUNCH_REPORT_SPAWN_GRACE_S`. In memory: a daemon restart drops them, and the runner's own turn-end
# heartbeat still claims.

LAUNCH_REPORT_RETRY_INTERVAL_S = 15.0
LAUNCH_REPORT_RETRY_WINDOW_S = 15 * 60.0
LAUNCH_REPORT_SPAWN_GRACE_S = 120.0
_launch_reports: dict[tuple[str, str], dict[str, Any]] = {}
_launch_reports_lock = threading.Lock()


def _transient(exc: BaseException) -> bool:
    """A failure worth re-sending: no answer, a timeout, a dropped connection, a 5xx or a 429 — and the
    token backoff's `URLError`, which a rotated token heals within the window. Any other HTTP status is the
    CP's answer (a refusal), which a retry would only earn again."""
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code >= 500 or exc.code == 429
    return isinstance(exc, (urllib.error.URLError, OSError, http.client.HTTPException))


def _remember_launch_report(kind: str, task_id: object, session_id: str, path: str, payload: dict,
                            exc: BaseException) -> None:
    """Queue one dropped launch report for `retry_launch_reports`; a newer report of the same kind for the same
    task replaces it. Not queued: a refusal, or a report naming no runner (nothing to check liveness by)."""
    if not session_id or not _transient(exc):
        return
    now = time.monotonic()
    with _launch_reports_lock:
        _launch_reports[(kind, str(task_id))] = {
            "session_id": session_id, "path": path, "payload": payload,
            "give_up_at": now + LAUNCH_REPORT_RETRY_WINDOW_S, "spawn_by": now + LAUNCH_REPORT_SPAWN_GRACE_S,
        }


def _forget_launch_report(kind: str, task_id: object, session_id: str) -> None:
    """Drop the queued report of `kind` for `task_id` — only `session_id`'s, so an older attempt's answer
    never drops a newer attempt's report."""
    key = (kind, str(task_id))
    with _launch_reports_lock:
        if _launch_reports.get(key, {}).get("session_id") == session_id:
            del _launch_reports[key]


def _live_runner_sessions() -> set[tuple[str, str]]:
    """`{(task id, session id)}` of every runner live on this node (`runner_registry.live_runners`)."""
    import runner_registry  # noqa: PLC0415 — the registry pulls the platform layer; only the retry needs it

    return {(str(r.task_id), r.session_id) for r in runner_registry.live_runners()}


def retry_launch_reports(*, live: set[tuple[str, str]] | None = None) -> int:
    """Re-send every queued launch report whose runner is live here; returns how many landed. Binding first,
    so the claim it precedes at launch never lands ahead of it. Never raises."""
    with _launch_reports_lock:
        pending = sorted(_launch_reports.items(), key=lambda item: item[0][0] != "binding")
    if not pending:
        return 0
    if live is None:
        try:
            live = _live_runner_sessions()
        except Exception as exc:  # noqa: BLE001 — an unreadable registry decides nothing; the next step re-reads
            log.warning("launch reports: runner registry unreadable, retrying next step: %s", exc)
            return 0
    now, landed = time.monotonic(), 0
    for (kind, task_id), report in pending:
        session_id = report["session_id"]
        if (task_id, session_id) in live:
            report["seen_live"] = True
        elif report.get("seen_live") or now >= report["spawn_by"]:
            log.info("launch report %s for task %s dropped: its runner is gone", kind, task_id)
            _forget_launch_report(kind, task_id, session_id)
            continue
        else:
            continue  # launched moments ago: its runner is not registered yet
        if now >= report["give_up_at"]:
            log.info("launch report %s for task %s dropped: retry window over", kind, task_id)
            _forget_launch_report(kind, task_id, session_id)
            continue
        try:
            _agent_api_post(report["path"], report["payload"])
        except Exception as exc:  # noqa: BLE001 — a failing CP must never wedge the daemon's step
            if _transient(exc):
                log.warning("launch report %s for task %s still not delivered: %s", kind, task_id, exc)
                continue
            log.warning("launch report %s for task %s refused, not retried: %s", kind, task_id, exc)
        else:
            landed += 1
            log.info("launch report %s for task %s delivered on retry", kind, task_id)
        _forget_launch_report(kind, task_id, session_id)
    return landed


def claim_slot_launch(intent_id: object, *, generation: int, dispatch_id: str) -> tuple[bool, str]:
    """ASK THE CP WHETHER THIS DISPATCH MAY STILL LAUNCH — `POST /api/agent/slot-intents/{id}/launch-claim`.
    Returns `(granted, why_not)`.

    Called immediately before the spawn (the ack comes too late); the CP decides and CONSUMES the
    permission (`app/slot_state.py::claim_launch`), so at most one of N intents for a slot is told yes.

    FAILS CLOSED on every uncertainty: a wrong refusal costs one re-dispatch, a wrong grant two runners in
    one tree. Never degrade to True. Hence DEPLOY THE CP FIRST — a CP without the route stops all launches."""
    try:
        body = _agent_api_post(
            f"/api/agent/slot-intents/{intent_id}/launch-claim",
            {"generation": int(generation), "dispatch_id": dispatch_id},
        )
    except Exception as exc:  # noqa: BLE001 — EVERY failure mode is a refusal; see the docstring
        # Wider than `URLError`: a malformed body raises `ValueError`, and every failure means "no yes".
        why = (
            f"could not ask the CP for launch permission (intent {intent_id}): "
            f"{type(exc).__name__}: {exc}"
        )
        log.warning("claim_slot_launch: %s — REFUSING the launch (fail closed)", why)
        return False, why
    if body.get("granted") is not True:
        # Only the literal `True` is a grant.
        why = str(body.get("reason") or "").strip() or (
            f"the CP did not grant the launch for intent {intent_id} and gave no reason "
            f"(response {body!r})"
        )
        log.warning("claim_slot_launch: %s", why)
        return False, why
    return True, ""


def set_slot_holder(
    node_id: str,
    project: str,
    number: object,
    *,
    last_used: str = "",
    project_id: int | None = None,
) -> dict:
    """Stamp a pool slot's `last_used` via `POST /api/agent/slot-holder`.

    The name and URL are kept for .deb/CP version skew; it no longer declares who holds a slot
    (`slot_state.lease()` does, CP-side).

    `last_used`: "" sends `None` ("not supplied", prior value kept). This is `worktrees.last_used`'s ONLY
    writer. `title` rides as a constant `None` ("leave alone") while the CP still accepts the field.

    Returns `SlotHolderOut` — `{project_id, project, number, mutated, …}`; `mutated == 0`
    means the write was DROPPED (no sensed row). Raises `URLError`/`HTTPError` (404 = unknown project).

    `project_id` is the slot's identity whenever the caller knows it; the name stays for an older CP."""
    payload = {
        "node_id": node_id,
        "project": project,
        "number": int(number),
        "last_used": last_used or None,
        "title": None,
    }
    if project_id is not None:
        payload["project_id"] = int(project_id)
    return _agent_api_post("/api/agent/slot-holder", payload)


# ── The state plane ───────────────────────────────────────────────────────────────────────────────────


# The observation payload's per-slot verdicts (`app/fleet_observe.py`'s `SLOT_STATES`), homed here
# because the node authors them; a test pins the pair.
#
#   present     — the path is a directory on disk. Upserted with this pass's sensed values.
#   gone        — the path is absent AND its container is a readable directory. IN the complement, so the
#                 CP DELETEs the row: this is the SQL successor to the KV prune's positive-observation test.
#   unobserved  — the node could not judge (it never probed the slot, the container was unreadable, the
#                 probe raised). REPORTED, so the row is spared, but NOTHING is written for it — every
#                 sensed field of a slot the node could not judge is None, and writing them would blank the
#                 row it just spared (`fleet_observe.slot_update_rows`).
#   unmaterialised — absent, container readable, never observed present: declared, not yet built. Spared
#                 like `unobserved`; never `gone`, or a just-added slot is deleted.
OBSERVE_SLOT_PRESENT = "present"
OBSERVE_SLOT_GONE = "gone"
OBSERVE_SLOT_UNOBSERVED = "unobserved"
OBSERVE_SLOT_UNMATERIALISED = "unmaterialised"


def agent_api_fleet_observe(payload: dict) -> dict | None:
    """POST one pass's whole observation.

    The caller MUST gate this on `pool_tick.ok`: a snapshot built from no instruction turns the CP's
    complement DELETE into data loss. Returns the CP's counts, or None on a transport failure (never read
    as "accepted an empty snapshot")."""
    return _best_effort("agent_api_fleet_observe", "POST", "/api/agent/fleet/observe", payload)


def agent_api_node_egress(ipv4: str | None) -> bool:
    """POST this node's egress IPv4 (or null) for the headscale edge's allowlist (`egress_report.py`); False
    on any failure, so the caller retries next pass."""
    return _best_effort("agent_api_node_egress", "POST", "/api/agent/nodes/egress", {"ipv4": ipv4}) is not None


def agent_api_disk_reclaim(report: dict) -> bool:
    """POST one disk-reclaim report (`disk_events.post_disk_report`; body pinned in `disk_reclaim.build_report`).

    `reclaim_id` is the CP's idempotency key, so a re-send after a lost reply is harmless. True on a 2xx —
    and on a PERMANENT refusal (400/409/413/422): that report will never be accepted, and kept it would hold
    every newer one behind it. False on anything a retry can fix (unreachable, 5xx, 401/403/404/408/429 —
    a CP without the route yet answers 404), so the report stays spooled for the next drain."""
    try:
        _agent_api_post("/api/agent/disk-reclaims", report)
        return True
    except urllib.error.HTTPError as exc:
        if exc.code in (400, 409, 413, 422):
            log.warning("agent_api_disk_reclaim: the CP refused report %s for good (%s) — dropping it",
                        report.get("reclaim_id"), exc.code)
            return True
        log.warning("agent_api_disk_reclaim: POST failed: %s", exc)
        return False
    except Exception as exc:  # noqa: BLE001 — an unreachable CP must never wedge the daemon's tick
        log.warning("agent_api_disk_reclaim: POST failed: %s", exc)
        return False


def agent_api_node_model_catalogs(catalogs: dict) -> bool:
    """POST this node's sensed vendor-CLI model catalogues (`provider_models.py`); False on any failure, so
    the caller retries next pass."""
    return _best_effort(
        "agent_api_node_model_catalogs", "POST", "/api/agent/nodes/model-catalogs", {"catalogs": catalogs},
    ) is not None


# ── Session transcript capture ─────────────────────────────────────────────────────────────────────────
# The node half of `apps/api/app/routes/agent_sessions.py`: daemon-direct, not socket verbs; sole caller
# `session_sweep.py`. No storage KEYS cross this boundary: the node names a member, gets a presigned URL.

def _session_post(label: str, session_id: str, route: str, body: dict) -> dict | None:
    """One best-effort POST under `/api/agent/sessions/{id}`: the CP's dict, or None on any failure (a miss
    is offered again next tick). `route` is the literal path with `{}` where the URL-quoted id goes — a
    literal at each call site, as `test_fleet_wire_contract` reads them."""
    return _best_effort(label, "POST", route.format(urllib.parse.quote(str(session_id))), body,
                        failed=f"POST failed for {session_id}")


def agent_api_sessions_work(node_id: str) -> dict | None:
    """`GET /api/agent/sessions/work` — what this node still owes, and whether the CP can pay for it.

    No push: the node reads what it owes each tick. None on a transport failure, never `{}` (an empty
    work list is a legitimate answer). Listing leases nothing and counts no attempt."""
    query = urllib.parse.urlencode({"node_id": str(node_id)})
    return _best_effort("agent_api_sessions_work", "GET", f"/api/agent/sessions/work?{query}",
                        failed=f"API unreachable for node {node_id}")


def agent_api_sessions_observed(node_id: str, items: list) -> bool:
    """`POST /api/agent/sessions/observed` — the session ids this node can SEE, so the CP can create
    the rows it is missing (CP-side create signals can drop rows silently). Best-effort; retried next tick."""
    if not items:
        return True
    return _best_effort("agent_api_sessions_observed", "POST", "/api/agent/sessions/observed",
                        {"node_id": str(node_id), "items": items},
                        failed=f"POST failed for {len(items)} item(s)") is not None


def agent_api_session_end(session_id: str, end_reason: str, ended_at: str | None = None) -> bool:
    """This session is over — one the node's own probe found dead (`session_sweep.is_abandoned`);
    `end_reason` is validated CP-side. IDEMPOTENT on the CP side."""
    payload = {"end_reason": end_reason, **({"ended_at": ended_at} if ended_at else {})}
    return _session_post("agent_api_session_end", session_id, "/api/agent/sessions/{}/end", payload) is not None


def agent_api_session_pane_log(session_id: str, tail: str | None, written_at: str | None) -> bool:
    """An unexplained end's raw pane-log tail (`session_sweep`'s pane-log leg); IDEMPOTENT on the CP side,
    first capture wins. `tail` None = no pane log on this node, so the CP records "missing" and stops
    asking. Raw bytes: the CP does all cleaning and redaction."""
    return _session_post("agent_api_session_pane_log", session_id, "/api/agent/sessions/{}/pane-log",
                         {"tail": tail, "written_at": written_at}) is not None


def agent_api_session_usage(session_id: str, payload: dict) -> bool:
    """A STILL-RUNNING session's usage so far (`session_sweep`'s live leg), so the Tasks board prices a
    runner before it ends. `payload` is `{tokens_in, tokens_out, usage_by_model, usage_incomplete,
    explored_targets}`, the same derived values the transcript report carries. 204 also when the CP decides
    it is a no-op."""
    return _session_post("agent_api_session_usage", session_id, "/api/agent/sessions/{}/usage", payload) is not None


def post_session_events(session_id: str, task_id: str, events: list[dict]) -> None:
    """`POST /api/agent/sessions/{id}/events {task_id, events}` — an ACP session's normalised events, each
    carrying the daemon-assigned `seq` (`acp/runtime.py`'s forwarder). IDEMPOTENT on seq CP-side. RAISES on
    any failure: the forwarder keeps its cursor and re-posts, so a lost post must be visible to it."""
    _agent_api_post(f"/api/agent/sessions/{urllib.parse.quote(str(session_id), safe='')}/events",
                    {"task_id": str(task_id), "events": events})


def agent_api_session_upload_ticket(session_id: str, members: list) -> dict | None:
    """**TAKES THE LEASE** and returns one presigned PUT per member: `{"bucket", "expires_at", "urls":
    [{"name", "url"}]}`. Call once per session, immediately before uploading it: lease and URLs expire at
    15 minutes. `members` is `[{"name", "bytes"}]` only. Every returned URL is a BEARER TOKEN — never log or
    persist. None on any failure (a 503 costs the row nothing)."""
    return _session_post("agent_api_session_upload_ticket", session_id, "/api/agent/sessions/{}/upload-ticket",
                         {"members": members})


def agent_api_session_report(session_id: str, payload: dict) -> bool:
    """What became of the upload. Never infer completion from a 204: the CP flips to `uploaded` only after
    its manifest lands. Sent even with no ticket (terminal outcomes). One POST per pass, never retried
    in-pass."""
    return _session_post("agent_api_session_report", session_id, "/api/agent/sessions/{}/transcript",
                         payload) is not None


def agent_api_node_logs(batch: dict) -> dict:
    """`POST /api/agent/node-logs` — one batch of this node's journal (`node_logs.py`). RAISES: the
    forwarder must tell a 503/403 (back off) from a transport failure (retry), so nothing is swallowed."""
    return _agent_api_post("/api/agent/node-logs", batch)


def agent_api_config_snapshot(
    node_id: str, config: dict, reported_at: str | None, microvm: dict | None = None
) -> bool | None:
    """`POST /api/agent/nodes/config-snapshot`. Redaction and publish-on-change stay daemon-side.

    `microvm`: a MicroVM node's §4.3 block (`microvm_facts`), sent beside `config` only when not None.
    True/False = whether it was a real CHANGE; None = the POST did not land. Kept apart so the caller's
    latch neither keeps a snapshot that never arrived nor re-POSTs forever."""
    body: dict = {"node_id": str(node_id), "config": config, "reported_at": reported_at}
    if microvm is not None:
        body["microvm"] = microvm
    answer = _best_effort("agent_api_config_snapshot", "POST", "/api/agent/nodes/config-snapshot", body)
    return None if answer is None else bool(answer.get("changed"))


def agent_api_release_desired() -> str | None:
    """The version the CP says the FLEET should be running, or None when it cannot be read.

    `GET /api/agent/release-desired`, read on every (re)connect by `main` and compared against
    `resolve_daemon_version()` (`upgrade_request.py`). READ ONLY: a node
    states what it IS running, never what the fleet should run; the CP answers its own version.

    None for "nothing published", "no version" AND a transport failure — all mean "do not converge"; an
    unreachable CP is never grounds for a root apt transaction. `str()`-normalized."""
    answer = _best_effort("agent_api_release_desired", "GET", "/api/agent/release-desired")
    version = None if answer is None else answer.get("version")
    if version is None:
        return None
    text = str(version).strip()
    return text or None


def agent_api_autoupdate_enabled() -> bool | None:
    """Whether this node installs new releases on its own, from `GET /api/agent/autoupdate`.

    The Settings knob, overridden by this node's host or by the node itself — the CP resolves it for the
    node the token proves. None on a transport failure or a body whose `enabled` is not a bool: "we could
    not ask", which every caller treats as off, since the answer gates a ROOT apt transaction."""
    answer = _best_effort("agent_api_autoupdate_enabled", "GET", "/api/agent/autoupdate")
    enabled = None if answer is None else answer.get("enabled")
    return enabled if isinstance(enabled, bool) else None


#: The detail a per-project agent route answers 404 with when the project is gone (`routes/agent_guard.py::_NOT_FOUND`).
PROJECT_GONE = "no such project"


def _http_error_detail(exc: urllib.error.HTTPError) -> object:
    """The JSON `detail` of an HTTP error's body; None when it has none or cannot be read."""
    try:
        return json.loads(exc.read(4096) or b"null").get("detail")
    except (OSError, ValueError, AttributeError):
        return None


def _agent_api_bundle(path: str, query: str, *, timeout: float, what: str) -> tuple[int, dict]:
    """A per-project bundle GET: `(200, {...})`, `(204, {})` unchanged since the query's `since`, `(410, {})`
    the feature is off for the project, `(404, {})` the route's own "no such project" (`PROJECT_GONE`: deleted, or
    moved to another org) — any other 404 (a proxy, a CP without the route) is a miss. Any other HTTP status or a
    transport failure RAISES: the caller decides what a miss means. `path` is a literal at each call site (`test_fleet_wire_contract` reads them)."""
    req = urllib.request.Request(f"{resolve_control_plane_api_base()}{path}?{query}", headers=_agent_headers(),
                                 method="GET")
    try:
        body = _agent_urlopen(req, expected=(404, 410), timeout=timeout)
    except urllib.error.HTTPError as exc:
        if exc.code == 410:
            return 410, {}
        if exc.code == 404 and _http_error_detail(exc) == PROJECT_GONE:
            return 404, {}
        raise
    if not body:
        return 204, {}
    parsed = json.loads(body)
    if not isinstance(parsed, dict):
        raise ValueError(f"{what}: the control plane answered a non-object")
    return 200, parsed


def agent_api_memory_bundle(project_id: object, since: object = None, *, timeout: float) -> tuple[int, dict]:
    """`GET /api/agent/memory/bundle` (verb `memory.bundle`; `since` a revision int) for `project_memory_bundle`."""
    query = f"project_id={int(project_id)}" + (f"&since={int(since)}" if since is not None else "")
    return _agent_api_bundle("/api/agent/memory/bundle", query, timeout=timeout, what="memory bundle")


def agent_api_guard_bundle(project_id: object, since: object = None, *, timeout: float) -> tuple[int, dict]:
    """`GET /api/agent/guard/bundle` (verb `guard.bundle`; `since` a revision string) for `guard_bundle`."""
    query = urllib.parse.urlencode({"project_id": int(project_id), **({"since": str(since)} if since else {})})
    return _agent_api_bundle("/api/agent/guard/bundle", query, timeout=timeout, what="guard bundle")


def agent_api_guard_record(project_id: object, decisions: list, *, timeout: float) -> dict:
    """`POST /api/agent/guard/decisions` (verb `guard.record`) for decisions a guard hook spooled and never sent
    (`guard_bundle.ship_spool`): the daemon's own call, declared `host`, so the CP files them under the project
    (bounded to one this node hosts) and under the task its own record ties each line's `session_ref` to on this
    node, or none. Raises on any failure; the caller decides what it means."""
    return _agent_json("POST", "/api/agent/guard/decisions", {"project_id": int(project_id), "decisions": decisions},
                       timeout=timeout)


def agent_api_memory_report(task_id: object, events: list, *, timeout: float) -> dict:
    """`POST /api/agent/tasks/{task_id}/memory/report` (verb `memory.report`) for a RELEASED runner's orphaned
    spool (`project_memory_bundle.ship_orphan_spool`): the daemon's own call, declared `host`, because the runner
    whose identity the verb would carry is gone. Raises on any failure; the caller logs and moves on."""
    return _agent_json("POST", f"/api/agent/tasks/{int(task_id)}/memory/report", {"events": events}, timeout=timeout)


# ── GHA runner pools ──────────────────────────────────────────────────────────────────────────────────
# The node half of `/api/agent/gha-runners/*`, called by the 60s `gha-runners` step. Only the root
# converge asks for tokens (`?tokens=1`), over its own credential; this process never sees one.


def agent_api_gha_desired() -> dict | None:
    """`GET /api/agent/gha-runners/desired` — the pool this node is supposed to be running, WITHOUT the
    tokens that would let anything act on it.

    `{"state", "repo_url", "count", "reader_user", "actionable_slots", "busy_slots", "remove_slots"}`.
    `remove_slots` is the one field taken on the CP's word (only it can ask GitHub whether a runner is busy;
    `gha_runner_ops.removals_pending`); everything else is sensed locally.

    None on a transport failure, never `{}`: a node with no pool row gets a real `absent` answer, and an
    outage must not converge a live pool to ABSENT.

    `?tokens=0` explicitly, and NO option to turn tokens on: minting is a rate-limited mutating call and
    each token a live secret this unprivileged process never spends. Never log the body."""
    return _best_effort("agent_api_gha_desired", "GET", "/api/agent/gha-runners/desired?tokens=0")


def agent_api_gha_report(payload: dict) -> dict | None:
    """`POST /api/agent/gha-runners/report` — what this node actually SEES.

    `{"state", "runners", "error", "runner_prefix"}`; `runner_prefix` (`gha_runner_ops.runner_prefix`)
    lets the CP match GitHub's list to this node's slots. The node is the only writer of observed state.
    Returns the CP's answer (`{"deleted": true}` when the row is removed) or None. Carries no credential."""
    return _best_effort("agent_api_gha_report", "POST", "/api/agent/gha-runners/report", payload)


def post_jaah_session_usage(jaah_session_id: str, calls: list) -> dict:
    """K4: post up to 200 metered calls of one Jaah session (`bedrock_usage_tail`), answered
    `{"accepted": n, "duplicates": m}`. RAISES on any refusal or fault — the caller keeps its offset and
    retries; a 404 (a CP without the route, or a session it no longer takes) is declared expected so the
    retry loop logs it once per attempt at the caller, not twice."""
    return _agent_json("POST", f"/api/agent/jaah-sessions/{urllib.parse.quote(jaah_session_id)}/usage",
                       {"calls": calls}, expected=(404,))


class _CountingSink:
    """A write-through counter, so a streaming caller learns the size without re-`stat`ing anything.

    The sink arm returns no bytes, and the stager's refuse-empty check needs the count."""

    __slots__ = ("_handle", "total")

    def __init__(self, handle) -> None:
        self._handle = handle
        self.total = 0

    def write(self, chunk: bytes) -> int:
        self.total += len(chunk)
        return self._handle.write(chunk)


def agent_api_stream_bytes(path: str, handle, *, cap: int = MAX_STREAM_BYTES) -> int:
    """Stream raw bytes from an agent-token GET into `handle`, returning how many arrived.

    The only raw-bytes reader; the peak is one chunk whatever the file weighs. The route may answer a 307
    to presigned S3, followed via `_OFF_HOST_SAFE_OPEN` so the node token stays home."""
    url = f"{resolve_control_plane_api_base()}{path}"
    req = urllib.request.Request(url, headers=_agent_headers(), method="GET")
    sink = _CountingSink(handle)
    _agent_urlopen(req, opener=_OFF_HOST_SAFE_OPEN, sink=sink, cap=cap)
    return sink.total


def set_runner_binding(
    task_id: object, *, session_id: str | None, worktree_ref: str | None,
    account_dir: str | None = None,
) -> dict:
    """Record WHICH runner is working a Task and WHERE — its `claude --session-id` and its pool worktree
    (e.g. `acme-2`) — keyed on the TASK ID directly.

    Best-effort: bookkeeping never aborts a launch; a dropped binding only leaves the run unattributed.

    `account_dir` is the runner's `CLAUDE_CONFIG_DIR` — the dir, never a slug (the CP derives it).
    Omitted when empty (`""` would read as slug "")."""
    payload: dict = {"runner_session_id": session_id, "worktree_ref": worktree_ref}
    if account_dir:
        payload["account_dir"] = account_dir
    path = f"/api/agent/tasks/{task_id}/runner-session"
    try:
        body = _agent_api_post(path, payload)
    except Exception as exc:  # noqa: BLE001 — bookkeeping; a read timeout or reset arrives unwrapped, not as URLError
        log.warning("set_runner_binding: could not bind task %s: %s: %s", task_id, type(exc).__name__, exc)
        # A transient failure is re-sent while the runner lives (`retry_launch_reports`).
        _remember_launch_report("binding", task_id, session_id or "", path, payload, exc)
        return {}
    _forget_launch_report("binding", task_id, session_id or "")
    return body


# ── a virtual-node HOST's converge loop (`vnode_converge.py`) ────────────────────────────────────────
#
# Five routes, 403 for a non-host (the CP's vnode-host routes); the loop's own
# classifications (403/404/410) are declared `expected`.

def agent_api_vnode_host_desired(*, capabilities: tuple[str, ...] = ()) -> dict:
    """`GET /api/agent/vnode-host/desired` — `{"vnodes": [{id, slot, state, shared}]}`. HTTPError 403 on
    a node that is not a host (404 from a control plane that predates the route), which the loop caches
    for its lifetime (R7). `vnode_tags=1` advertises that this host builds a vnode under the `tag` the CP
    hands it — the CP issues tags only to a host that says so.
    `capabilities` (`nodevm`: this host can run a Whole-node VM, `nodevm.supported`) rides as one more
    parameter, only when there is one — so a host with none asks exactly what it always did."""
    query = "vnode_tags=1"
    if capabilities:
        query += "&" + urllib.parse.urlencode({"capabilities": ",".join(capabilities)})
    return _agent_json("GET", f"/api/agent/vnode-host/desired?{query}", expected=(403, 404))


def agent_api_vnode_enrolment(virtual_node_id: str) -> dict:
    """`GET /api/agent/vnode-host/enrolment/{id}` — READ ONCE, and it carries two live secrets: the caller
    hands it straight to the vnode helper and never logs or keeps it. HTTPError 410 once read."""
    return _agent_json("GET", f"/api/agent/vnode-host/enrolment/{urllib.parse.quote(virtual_node_id)}",
                       expected=(410,))


def agent_api_vnode_enrolment_reissue(virtual_node_id: str) -> None:
    """`POST .../enrolment/{id}/reissue` — a fresh envelope after a 410 (204)."""
    _agent_json("POST", f"/api/agent/vnode-host/enrolment/{urllib.parse.quote(virtual_node_id)}/reissue",
                payload={})


def agent_api_vnode_host_report(entries: list[dict]) -> dict:
    """`POST /api/agent/vnode-host/report` — `{"vnodes": [{id, state, failure_reason?, tailnet_device_id?}]}`."""
    return _agent_json("POST", "/api/agent/vnode-host/report", payload={"vnodes": entries})


def agent_api_vnode_host_usage(entries: list[dict], root: dict | None = None) -> dict:
    """`POST /api/agent/vnode-host/usage` — `{"vnodes": [{id, disk_used_kb, inodes_used}], "root"?: {...}}`,
    `root` being the host's own disk (sent only when given)."""
    payload: dict = {"vnodes": entries}
    if root is not None:
        payload["root"] = root
    return _agent_json("POST", "/api/agent/vnode-host/usage", payload=payload)
