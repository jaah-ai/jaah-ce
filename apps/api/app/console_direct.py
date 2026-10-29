"""Direct-console issuance — the CP half of the browser's optional CP-bypassing console transport.

The browser dials the node's own listener; the CP (1) validates where the node is, (2) mints a bounded
bearer credential, (3) grants it to the node over the node bus and answers only after the node acks
(C-5: `{"status":"ok",direct_url,token,expires_at}`, `{"status":"unavailable",reason}` — a transient
outage the browser retries — or `{"status":"disabled",reason}` — direct console is not configured for this
node, so the browser uses the relay instead of retrying). Frames
(C-6): `console_direct_grant` (request/reply; the ack must echo `read_only`), `console_direct_revoke`
(fire-and-forget, keyed on `session_id` + optional slot/token), `console_direct_used` (node → CP).
No CP-side token store: the node holds grants, `session_id` is `<admin_id>.<epoch>` so a logout can
revoke without state, and the signed token is an attribution envelope, not the authorization. The
token never rides a query string and is logged only by `token_fingerprint()` (AC8.5). Residuals:
revokes are fire-and-forget and reach only nodes whose socket this process holds.
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import logging
import math
import os
import re
import secrets
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from uuid import UUID

from app.dev_bypass import is_local_env

log = logging.getLogger("console-direct")

# The `config_snapshot` key the daemon publishes its listener origin under (no path — see
# `compose_direct_url`); no new `nodes` column.
CONFIG_KEY_DIRECT_URL = "direct_console_url"

# ── Bounds on the credential's lifetime (D12) ────────────────────────────────────────────────────────
# Floor: the browser's reconnect window (~255s), so a reconnect needs no fresh CP round trip.
# Ceiling: 24h — a bearer secret in a browser must never become permanent.
MIN_TOKEN_TTL_SECONDS = 300
MAX_TOKEN_TTL_SECONDS = 24 * 3600
# The 24h default lives on the `direct_console_token_ttl_seconds` catalogue entry
# (`app/operator_settings/`); see `token_ttl_seconds`.

# How stale `nodes.observed_at` may be before issuance is refused: 3x the 30s OFFLINE window
# (`jaah_discovery.PRESENCE_HEARTBEAT_TTL`), so an ONLINE node is never refused. Not
# `pool_state.max_observation_age_secs()`, which is tuned for the pool cap. Value lives on the
# `direct_console_max_node_age_seconds` catalogue entry.

# Bounds the grant round trip. The node only records the grant, so fail fast into `unavailable` rather
# than hold an HTTP request open.
_DEFAULT_GRANT_TIMEOUT_SECONDS = 10.0

# Per-node bound on delivering one revoke frame. Short: the caller is usually a sign-out whose epoch
# bump has already committed, and revoke is fire-and-forget.
_REVOKE_SEND_TIMEOUT = 2.0

# Only `wss` is accepted from a node snapshot; `ws://` would carry the bearer token in cleartext.
_REQUIRED_SCHEME = "wss"

# Public suffixes, not any one tailnet's name — refused by name as `DIRECT_CONSOLE_TAILNET_SUFFIX`, since
# they would match every tailnet on the internet.
_PUBLIC_SUFFIXES = frozenset({"ts.net", "tailscale.net"})

# An explicit unprivileged high port is required: an origin with no port defaults to 443, the public
# ingress, not the console listener.
_MIN_PORT = 1024
_MAX_PORT = 65535

# Domain separation for the attribution MAC.
_MAC_DOMAIN = b"jaah.console.direct.v1"
# Fallback key for a local stack with no SESSION_SECRET: random per process, so tokens become
# unattributable across a restart. Never used when a secret is configured (`app/auth.py`).
_EPHEMERAL_KEY = secrets.token_bytes(32)


class DirectUnavailable(Exception):
    """Direct mode cannot be offered for this (node, slot) right now, with a reason to surface.

    An exception because every refusal point is a mid-computation abort, and the reason must reach
    the user (AC8.0) — the browser retries indefinitely and shows it.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class DirectDisabled(DirectUnavailable):
    """Direct console is not CONFIGURED for this node — a standing state, not an outage.

    Raised where the answer cannot change by retrying: a fresh node that publishes no address (its
    `JAAH_TAILNET_DNS_SUFFIX` is unset) or a CP with no usable `DIRECT_CONSOLE_TAILNET_SUFFIX` to accept
    the one it publishes. `issue` answers `disabled`, and the browser relays rather than re-dialling a
    capability that is off (the report-and-retry path is for a node that is failing, not one that is switched off).
    """


@dataclass(frozen=True)
class NodeFacts:
    """The `nodes` row facts issuance depends on, read by the caller and passed in.

    Keeps this module free of SQLAlchemy so its policy is testable with bare pytest; the DB read lives
    in `app/routes/console.py`.

    `node_id` is `nodes.name` (the tailnet handle, validated against the published host); `bus_key` is
    the socket's registration key (`node_identity.bus_key`). None means "same as `node_id`".
    """

    node_id: str
    config_snapshot: Optional[dict[str, Any]]
    observed_at: Optional[datetime]
    bus_key: Optional[str] = None

    @property
    def bus_target(self) -> str:
        """The key to address this node's socket by — never the raw `node_id` unless they agree."""
        return self.bus_key or self.node_id


@dataclass(frozen=True)
class Claims:
    """The binding a token asserts, recovered from the token itself with no CP-side lookup.

    `tmux_session` is the CP's tmux-session choice (empty: none), distinct from `session_id` (the admin
    browser session). `read_only` is deliberately absent — it rides the grant frame only (`_send_grant`),
    and nothing recovers it from a token."""

    session_id: str
    node_id: str
    project: str
    number: int
    kind: str
    expires_at: datetime
    tmux_session: str = ""


# ── Settings (read at call time, never pinned at import) ─────────────────────────────────────────────


def cp_frame_proof_for_node(node_ref: str) -> str:
    """What the CP puts in a CP→node frame to prove it is the CP, for the node `node_ref` names.

    The derivation is single-homed in `app/node_credentials.py::cp_frame_proof` (node half:
    `apps/jaahd/node_frame_auth.py`).

    `""` on anything unresolvable; never raises. An empty proof is a refused frame on the node, and
    `_send_grant` turns that into a readable `DirectUnavailable`.
    """
    try:
        # Deferred, like every app-level import here: the module's import graph stays stdlib-only so
        # `apps/jaahd` can share its constants.
        from app.db import SessionLocal
        from app.node_credentials import cp_frame_proof_for
        from app.node_identity import node_id_or_none

        # An identity read under `identity_scope()`: callers include unbound DBOS workers, which would
        # otherwise resolve no row and get every frame refused. Cross-org dispatch is guarded upstream by
        # `routes/commands.py::enqueue_command`'s `require_object_access`. Deferred import: this module is
        # also imported stdlib-only, by the node daemon's tests.
        from app.tenancy import seam

        with seam.identity_scope(), SessionLocal() as session:
            return cp_frame_proof_for(session, node_id_or_none(session, node_ref))
    except Exception as exc:  # noqa: BLE001 — see the docstring: degrade to `""`, never raise
        log.warning("console-direct: could not derive a CP frame proof for %r (%s)", node_ref, exc)
        return ""


async def cp_frame_proof_for_node_async(node_ref: str) -> str:
    """`cp_frame_proof_for_node`, off the event loop — the form every async producer must use.

    The sync version blocks on a DB checkout and query; `broadcast_revoke` fans out across every node.
    Uses anyio's threadpool (the one sync FastAPI dependencies use), so one limiter sees all concurrency.

    Never raises, including on the deferred `run_in_threadpool` import failing (reachable under the
    stdlib-only `apps/jaahd` contract test).
    """
    try:
        from starlette.concurrency import run_in_threadpool   # deferred, as every import in this module is

        return await run_in_threadpool(cp_frame_proof_for_node, node_ref)
    except Exception as exc:  # noqa: BLE001 — see the docstring: degrade to `""`, never raise
        log.warning("console-direct: could not derive a CP frame proof for %r off-loop (%s)", node_ref, exc)
        return ""


def _positive_finite(name: str, default: float) -> float:
    """One env float, or `default` for anything that is not a usable positive bound.

    `math.isfinite` matters: `inf` passes `> 0` and would disable the freshness gate. Every bound here
    is fail-closed.
    """
    raw = os.environ.get(name, "")
    try:
        value = float(raw)
    except (TypeError, ValueError, OverflowError):
        return default
    return value if math.isfinite(value) and value > 0 else default


def clamp_ttl_seconds(value: int) -> int:
    """Bring any requested lifetime into [MIN, MAX]. Clamped rather than validated, and applied at both
    the env read and the mint, so no caller can step over the ceiling."""
    return max(MIN_TOKEN_TTL_SECONDS, min(MAX_TOKEN_TTL_SECONDS, value))


def token_ttl_seconds() -> int:
    """The operator `direct_console_token_ttl_seconds` override, else `DIRECT_CONSOLE_TOKEN_TTL_SECONDS`,
    else the 24h default — clamped into [MIN, MAX] either way (defense in depth over the catalogue's
    own bounds)."""
    from app.operator_settings import reads as settings_reads  # deferred: keeps this module's import graph stdlib-only (see NodeFacts)

    return clamp_ttl_seconds(settings_reads.get_int("direct_console_token_ttl_seconds"))


def max_node_age_seconds() -> float:
    """The operator `direct_console_max_node_age_seconds` override, else
    `DIRECT_CONSOLE_MAX_NODE_AGE_SECONDS`, else the default — the `observed_at` freshness bound, read at
    call time."""
    from app.operator_settings import reads as settings_reads  # deferred: keeps this module's import graph stdlib-only (see NodeFacts)

    return settings_reads.get_float("direct_console_max_node_age_seconds")


def grant_timeout_seconds() -> float:
    """`DIRECT_CONSOLE_GRANT_TIMEOUT_SECONDS` — how long to wait for the node's ack. Bounded: an infinite
    wait holds a `MAX_PENDING_PER_NODE` slot and starves that node's command path."""
    return _positive_finite("DIRECT_CONSOLE_GRANT_TIMEOUT_SECONDS", _DEFAULT_GRANT_TIMEOUT_SECONDS)


def tailnet_suffix() -> str:
    """`DIRECT_CONSOLE_TAILNET_SUFFIX` — the full tailnet domain (e.g. `tn.example.com`), never a
    bare public suffix, unset by default. Unset is also the CP-side OFF switch: a node publishing a
    tailnet FQDN then gets `disabled` (see `DirectDisabled`).

    Anyone can create a tailnet with a same-named machine and a valid certificate, so only an exact
    tailnet-domain match is an identity check. Unset: the only accepted host is `nodes.name` itself
    (already the FQDN on this fleet).
    """
    return os.environ.get("DIRECT_CONSOLE_TAILNET_SUFFIX", "").strip().strip(".").lower()


def origin_override(node_id: str) -> Optional[str]:
    """`DIRECT_CONSOLE_ORIGIN_OVERRIDE` — AC8.0's testing override for the URL source.

    Format `node_id=origin[,node_id=origin]...`. An override replaces the snapshot, the identity check
    and the freshness gate, so it requires an affirmative `APP_ENV=local`; absent `APP_ENV` means not
    local (`app/dev_bypass.py::is_local_env`). Ignored loudly otherwise.
    """
    raw = os.environ.get("DIRECT_CONSOLE_ORIGIN_OVERRIDE", "")
    if not raw:
        return None
    if not is_local_env():
        log.error(
            "console-direct: DIRECT_CONSOLE_ORIGIN_OVERRIDE is set but APP_ENV is %r, not 'local' — "
            "IGNORING it. It disables the node-identity and freshness gates, which is acceptable only "
            "on a dev stack that declares itself one. Unset the override, or publish the address "
            "through the node's config snapshot.",
            os.environ.get("APP_ENV") or "<unset>",
        )
        return None
    for pair in raw.split(","):
        name, _, origin = pair.partition("=")
        if name.strip() == node_id and origin.strip():
            return origin.strip()
    return None


# A CP-named tmux session, shape-validated here for both `routes/console.py` paths. The node mirrors the
# regex in its own module (the node does not import the control plane's code).
_SESSION_NAME_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9_-]{0,63}\Z")


def validate_session_name(raw: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    """(session, error) — RAW as an optional CP-named tmux session, or why it is refused.

    `(None, None)` means none named. An invalid value is an error and must never reach the signed token
    or grant frame, which the node trusts."""
    if raw is None:
        return None, None
    if not _SESSION_NAME_RE.match(raw):
        return None, f"invalid session name {raw!r} — expected 1-64 chars of [A-Za-z0-9_-], starting alphanumeric"
    return raw, None


# ── Session identity + token ────────────────────────────────────────────────────────────────────────


def session_id_for(admin_id: UUID, session_epoch: Optional[int]) -> str:
    """The revocation key for one authenticated browser session — see the module docstring.

    Not a secret: it only groups grants for revocation, and revokes arrive only over the node bus,
    authenticated by `cp_frame_proof`. The admin id is a uuid string.
    """
    return f"{admin_id}.{int(session_epoch or 0)}"


def _signing_key() -> bytes:
    secret = os.environ.get("SESSION_SECRET", "")
    if not secret:
        return hmac.new(_EPHEMERAL_KEY, _MAC_DOMAIN, hashlib.sha256).digest()
    return hmac.new(secret.encode(), _MAC_DOMAIN, hashlib.sha256).digest()


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def _unb64(value: str) -> bytes:
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def token_fingerprint(token: str) -> str:
    """A short, non-reversible handle for a token, for logs and events (AC8.5): 12 hex chars of SHA-256,
    enough to correlate issuance with `console_direct_used` events.
    """
    return hashlib.sha256(token.encode()).hexdigest()[:12]


def mint_token(
    *, session_id: str, node_id: str, project: str, number: int, kind: str,
    ttl_seconds: Optional[int] = None, now: Optional[datetime] = None, tmux_session: Optional[str] = None,
) -> tuple[str, datetime]:
    """Mint one direct-console credential bound to (session, node, slot) and expiring at a stated time.

    `jti` makes repeated grants distinct, so a token-narrowed revoke addresses one console.
    `tmux_session` rides the signed claims, so tampering fails the HMAC.
    """
    moment = now or datetime.now(timezone.utc)
    # Clamped here too, so a keyword argument cannot step over the 24h ceiling.
    ttl = token_ttl_seconds() if ttl_seconds is None else clamp_ttl_seconds(ttl_seconds)
    expires_at = moment + timedelta(seconds=ttl)
    claims = {
        "s": session_id, "d": node_id, "p": project, "n": int(number), "k": kind,
        "x": int(expires_at.timestamp()), "j": secrets.token_urlsafe(12), "y": tmux_session or "",
    }
    body = _b64(json.dumps(claims, separators=(",", ":"), sort_keys=True).encode())
    mac = _b64(hmac.new(_signing_key(), body.encode(), hashlib.sha256).digest())
    return f"{body}.{mac}", expires_at


def rejection_reason(
    token: object, *, node_id: str, project: str, number: int, kind: str,
    session_id: Optional[str] = None, tmux_session: Optional[str] = None,
    now: Optional[datetime] = None,
) -> Optional[str]:
    """Why this token does not authorize this (node, slot[, session[, tmux session]]) right now — or None
    if it does.

    AC8.4's binding: expired, wrong node, wrong slot, or (when given) wrong session/tmux session are
    refused. Production caller: `record_used`. The node's enforcement is stricter — it accepts only a
    token it holds a grant for, so revocation is enforced there.
    """
    claims = _parse(token)
    if claims is None:
        return "malformed or unsigned token"
    moment = now or datetime.now(timezone.utc)
    if claims.expires_at <= moment:
        return "expired"
    if claims.node_id != node_id:
        return "bound to a different node"
    if (claims.project, claims.number, claims.kind) != (project, int(number), kind):
        return "bound to a different console slot"
    if session_id is not None and claims.session_id != session_id:
        return "bound to a different session"
    if tmux_session is not None and claims.tmux_session != tmux_session:
        return "bound to a different tmux session"
    return None


def _parse(token: object) -> Optional[Claims]:
    """Recover a token's claims, or None for anything that is not a token this CP signed.

    Fail-closed and never raises: the input is whatever a node put in a frame."""
    if not isinstance(token, str) or "." not in token or not token.isascii():
        # `isascii` matters: `hmac.compare_digest` raises on non-ASCII `str`, and the caller runs in the
        # node bus read loop, where an escape unregisters the node. Signed tokens are always base64url.
        return None
    body, _, mac = token.rpartition(".")
    if not hmac.compare_digest(mac, _b64(hmac.new(_signing_key(), body.encode(), hashlib.sha256).digest())):
        return None
    try:
        claims = json.loads(_unb64(body))
    except (ValueError, TypeError):
        return None
    if not isinstance(claims, dict):
        return None
    try:
        return Claims(
            session_id=str(claims["s"]), node_id=str(claims["d"]), project=str(claims["p"]),
            number=int(claims["n"]), kind=str(claims["k"]),
            expires_at=datetime.fromtimestamp(int(claims["x"]), timezone.utc),
            # `.get`: a token without a `"y"` claim is still handled.
            tmux_session=str(claims.get("y") or ""),
        )
    except (KeyError, TypeError, ValueError, OverflowError, OSError):
        # `fromtimestamp` raises OverflowError/OSError (not ValueError) on an absurd `x`; this runs on the
        # node bus read loop, so "never raises" must be literal.
        return None


# ── The node's address ──────────────────────────────────────────────────────────────────────────────


def resolve_origin(facts: NodeFacts, *, now: Optional[datetime] = None) -> str:
    """The node's validated listener origin, or `DirectUnavailable` with a reason.

    Snapshots are never cleared (`app/node_config.py`), so freshness comes from `observed_at` and a
    stale or wrong host is caught here rather than as a browser TLS error.
    """
    override = origin_override(facts.node_id)
    if override:
        return _validated_override(override, facts.node_id)

    moment = now or datetime.now(timezone.utc)
    observed_at = facts.observed_at
    if observed_at is None:
        raise DirectUnavailable("this node has never reported an observation")
    if observed_at.tzinfo is None:
        observed_at = observed_at.replace(tzinfo=timezone.utc)
    age = (moment - observed_at).total_seconds()
    limit = max_node_age_seconds()
    if age > limit:
        raise DirectUnavailable(
            f"this node's last observation is {int(age)}s old (limit {int(limit)}s) — "
            "it may be offline, and its published console address cannot be trusted"
        )

    snapshot = facts.config_snapshot if isinstance(facts.config_snapshot, dict) else {}
    published = snapshot.get(CONFIG_KEY_DIRECT_URL)
    if not isinstance(published, str) or not published.strip():
        raise DirectDisabled("not configured on this node")
    return _validated_origin(published.strip(), facts.node_id)


def _validated_origin(origin: str, node_id: str) -> str:
    """A node-published origin, or `DirectUnavailable`. Scheme, host and port are checked, the host
    against the node's own tailnet identity."""
    parsed = urllib.parse.urlsplit(origin)
    if parsed.scheme != _REQUIRED_SCHEME:
        raise DirectUnavailable(
            f"this node's published console address is not {_REQUIRED_SCHEME}:// "
            f"(got {parsed.scheme or 'no'} scheme)"
        )
    if parsed.path or parsed.query or parsed.fragment:
        raise DirectUnavailable(
            "this node published a console URL with a path — the CP composes the slot path, so the node "
            "must publish an origin only"
        )
    host = (parsed.hostname or "").lower()
    if not _host_is_this_node(host, node_id):
        if host != node_id.strip().lower() and not _suffix_is_usable(tailnet_suffix()):
            # The CP side switched off: nothing this node publishes can be accepted until one is configured.
            raise DirectDisabled(_host_refusal_reason(host, node_id))
        raise DirectUnavailable(_host_refusal_reason(host, node_id))
    return _with_validated_port(parsed, host)


def _suffix_is_usable(suffix: str) -> bool:
    """The one home of "can this configured suffix identify a node": a full tailnet domain, never a
    bare public suffix and never a dotless label. Both arms are load-bearing (`ts.net` has a dot).

    Mirrored by a plan-time precondition in the control plane's deployment Terraform, and a test keeps
    them in step.
    """
    return bool(suffix) and "." in suffix and suffix not in _PUBLIC_SUFFIXES


def _host_refusal_reason(host: str, node_id: str) -> str:
    """Which side is at fault when the published host is not the node's identity. Diagnostic only,
    reached after `_host_is_this_node` said no.

    An unset or unusable `DIRECT_CONSOLE_TAILNET_SUFFIX` refuses every FQDN-publishing node, and the
    refusal would otherwise blame the node. Not a new refusal condition: a node whose id is its FQDN
    matches the first arm and never gets here.
    """
    # `host !=` guards only the degenerate `host == node_id == ""` (corrupt data).
    if host != node_id.strip().lower() and not _suffix_is_usable(tailnet_suffix()):
        # Not an absolute: a genuinely foreign host is indistinguishable here, so name the likelier fault
        # and the order to check, without clearing the node.
        return (
            "this control plane has no usable DIRECT_CONSOLE_TAILNET_SUFFIX configured — it is unset, "
            "has no dot, or is a bare public suffix, which names no tailnet — so the only console host "
            "it can accept is the node's own node_id. Either this control plane is misconfigured or "
            "this node published an address that is not its own; check DIRECT_CONSOLE_TAILNET_SUFFIX first"
        )
    return "this node's published console address names a host that is not its own tailnet identity"


def warn_if_tailnet_suffix_is_unusable() -> None:
    """The startup half of the diagnosis above: an observable signal, never a refusal.

    Called once from the api's lifespan. A warning, not a failure, because a suffix-less configuration
    is legitimate when node ids are FQDNs. Gated on `_suffix_is_usable`, not emptiness, so `ts.net`
    etc. warn too.
    """
    suffix = tailnet_suffix()
    if not _suffix_is_usable(suffix):
        log.warning(
            "DIRECT_CONSOLE_TAILNET_SUFFIX is not usable (%r): the only console host this control plane "
            "can accept is a node's own node_id, so every node that publishes a tailnet FQDN will be "
            "refused. Set it to the FULL tailnet domain (e.g. tailnet-name.example, not one label) unless this fleet's "
            "node ids are already their tailnet FQDNs.",
            suffix,
        )


def _validated_override(origin: str, node_id: str) -> str:
    """An operator-declared origin (the local-only testing override). Still parsed and port-checked;
    the identity check is skipped — see `origin_override`. Plaintext `ws://` only to loopback.
    """
    parsed = urllib.parse.urlsplit(origin)
    if parsed.scheme not in ("ws", "wss"):
        raise DirectUnavailable(
            f"the configured direct console override for {node_id} is not a ws(s):// URL"
        )
    host = (parsed.hostname or "").lower()
    if not host:
        raise DirectUnavailable(f"the configured direct console override for {node_id} has no host")
    if parsed.scheme == "ws" and host not in ("127.0.0.1", "::1", "localhost"):
        raise DirectUnavailable(
            f"the direct console override for {node_id} is plaintext ws:// to a non-loopback host — "
            "the credential would cross the network in the clear"
        )
    return _with_validated_port(parsed, host)


def _with_validated_port(parsed: urllib.parse.SplitResult, host: str) -> str:
    try:
        port = parsed.port
    except ValueError:
        raise DirectUnavailable("this node's published console address has a malformed port") from None
    if port is None:
        raise DirectUnavailable(
            "this node's published console address carries no port — the console listener is not on the "
            "default port, so an explicit one is required"
        )
    if not (_MIN_PORT <= port <= _MAX_PORT):
        raise DirectUnavailable(f"this node's published console port {port} is outside {_MIN_PORT}-{_MAX_PORT}")
    # Reassembled from validated components, not echoed: drops userinfo, normalises case and ports, and
    # restores IPv6 brackets `urlsplit.hostname` strips.
    literal = f"[{host}]" if ":" in host else host
    return f"{parsed.scheme}://{literal}:{port}"


def _host_is_this_node(host: str, node_id: str) -> bool:
    """Whether `host` is the tailnet identity of `node_id`.

    Accepted: the host is the node id, or its first label is the node id and the rest is exactly the
    configured tailnet domain. A suffix match (e.g. `ts.net`) would accept an attacker's own tailnet
    with a valid certificate.
    """
    if not host:
        return False
    expected = node_id.strip().lower()
    if not expected:
        return False
    if host == expected:
        return True
    suffix = tailnet_suffix()
    if not _suffix_is_usable(suffix):
        # Refuse rather than loosen (fail-closed); see `_suffix_is_usable`.
        return False
    first_label, _, domain = host.partition(".")
    return first_label == expected and domain == suffix


def compose_direct_url(origin: str, project: str, number: int, kind: str) -> str:
    """The one place the direct console's path template lives. The node publishes an origin; the
    browser dials what this returns, verbatim."""
    slot = "/".join(
        urllib.parse.quote(str(part), safe="") for part in ("console", project, number, kind)
    )
    return f"{origin.rstrip('/')}/{slot}"


# ── Issuance + the C-6 frames ───────────────────────────────────────────────────────────────────────


async def issue(
    facts: NodeFacts, *, project: str, number: int, kind: str, session_id: str, registry: Any,
    now: Optional[datetime] = None, view_allowed: bool = False, tmux_session: Optional[str] = None,
    config_dir: Optional[str] = None, securestorage_dir: Optional[str] = None,
    read_only: bool = False, attach_only: bool = False, provider: str,
    api_key_account: Optional[str] = None, jaah: Optional[dict[str, Any]] = None,
    guard: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    """C-5's discriminated union, end to end: freshness, URL validation, mint, grant, ack.

    `registry` is injected (`app/node_bus.py`'s in production) so the ack requirement is testable
    without a node. Nothing is minted before the address validates; nothing returned before the ack.

    Grant-frame fields (not token claims unless noted): `view_allowed` (default False, fail-closed);
    `tmux_session` (pre-validated; also a signed claim); `read_only` (the tier from
    `routes/console.py::_console_read_only`; the node must confirm it, else `unavailable`);
    `attach_only` (the CP requiring "never create" — the node ORs it with the browser's hello, so a
    viewer can still attach read-only); `config_dir` (the create's account, `_console_account()`);
    `securestorage_dir` (where a shared account's credential lives, `app/shared_storage.py`). The last
    two default to None, the fail-closed answer; never pass `""`, which the CLI reads as `~/.claude`.
    `provider` (the picked account's, always sent) and `api_key_account` (the slug of an API-key pick,
    `config_dir` then None) — the relay frame's same pair (`routes/console.py::_picked_account`). `jaah`: K2
    (`account_ref` + `bedrock`) on a Jaah-account create (`routes/console.py::_console_jaah`), sent only then.
    `guard`: the Architectural Guard stanza (`routes/console.py::_console_guard`), sent only where it is available.
    """
    try:
        origin = resolve_origin(facts, now=now)
    except DirectDisabled as exc:
        return {"status": "disabled", "reason": exc.reason}
    except DirectUnavailable as exc:
        return _unavailable(exc.reason)

    if not await registry.is_connected_async(facts.bus_target):
        # Checked before minting: an unreachable node cannot ack (also the answer during a deploy overlap
        # when another process holds the socket).
        return _unavailable("this node is not currently connected to the control plane")

    token, expires_at = mint_token(
        session_id=session_id, node_id=facts.node_id, project=project, number=number, kind=kind,
        now=now, tmux_session=tmux_session,
    )
    try:
        await _send_grant(
            registry, facts.bus_target, token=token, project=project, number=number, kind=kind,
            session_id=session_id, expires_at=expires_at, view_allowed=view_allowed,
            tmux_session=tmux_session, config_dir=config_dir,
            securestorage_dir=securestorage_dir, read_only=read_only,
            attach_only=attach_only, provider=provider, api_key_account=api_key_account, jaah=jaah,
            guard=guard,
        )
    except DirectUnavailable as exc:
        return _unavailable(exc.reason)

    log.info(
        "console-direct: granted %s %s-%s/%s to session %s (token %s, expires %s, view_allowed=%s, "
        "account=%s, read_only=%s)",
        facts.node_id, project, number, kind, session_id, token_fingerprint(token),
        expires_at.isoformat(), view_allowed, config_dir or api_key_account or "-", read_only,
    )
    return {
        "status": "ok",
        "direct_url": compose_direct_url(origin, project, number, kind),
        "token": token,
        "expires_at": expires_at.isoformat(),
    }


def _unavailable(reason: str) -> dict[str, Any]:
    return {"status": "unavailable", "reason": reason}


async def _send_grant(
    registry: Any, node_id: str, *, token: str, project: str, number: int, kind: str,
    session_id: str, expires_at: datetime, view_allowed: bool, tmux_session: Optional[str] = None,
    config_dir: Optional[str] = None, securestorage_dir: Optional[str] = None,
    read_only: bool = False, attach_only: bool = False, provider: str = "claude",
    api_key_account: Optional[str] = None, jaah: Optional[dict[str, Any]] = None,
    guard: Optional[dict[str, Any]] = None,
) -> None:
    """Send one grant and require the node's ack — `FRAME_CONSOLE_OPEN`'s pattern (`app/node_bus.py`).

    Every failure becomes a readable `DirectUnavailable`, including an old daemon silently dropping the
    frame (reported as a timeout). The ack also verifies `read_only`: a read-only grant whose ack lacks
    `read_only: true` is refused, as the relay does for `console_open`.
    """
    from app import node_bus   # deferred: keeps this module's import graph stdlib-only (see NodeFacts)

    try:
        reply = await registry.request(
            node_id,
            {
                # The frame credential (`cp_frame_proof_for_node`), distinct from `token` below.
                "cp_frame_proof": await cp_frame_proof_for_node_async(node_id),
                # The node this grant is for; the node refuses a grant that does not name it.
                "node_id": node_id,
                "token": token, "project": project, "number": number, "kind": kind,
                "session_id": session_id, "expires_at": expires_at.isoformat(),
                # `is True`, matching the node's read, so a merely truthy value is never permissive.
                "view_allowed": view_allowed is True,
                # The CP's tmux-session choice in the clear; the signed copy in the token detects
                # tampering. Sent even when empty.
                "session": tmux_session or "",
                # The account a create runs on. Sent even when None — and None beside an
                # `api_key_account`, which an old node refuses (deploy nodes first).
                "config_dir": config_dir,
                # The API-key account's slug a create runs on instead (its key is fetched by the node
                # under a console key lease, `app/console_key_leases.py`). Sent even when None.
                "api_key_account": api_key_account,
                # The picked account's provider. Always sent.
                "provider": provider,
                # Where that account's token lives when not under `config_dir`. Sent even when None;
                # additive (old nodes ignore it) and not ack-verified — ignoring it degrades to a visible
                # "Not logged in". Never `""` (resolves to `~/.claude` on the node).
                "securestorage_dir": securestorage_dir,
                # The tier; this frame is its only carrier (the node's server-side `DirectGrant`), so
                # the browser has nothing to flip. `is True`; sent even when False so the ack means
                # "applied what you asked".
                "read_only": read_only is True,
                # "This grant may never create" — here, not in the browser's hello. The node ORs it with
                # the hello, so it can only narrow. Sent both ways.
                "attach_only": attach_only is True,
                # K2 on a Jaah-account create; absent otherwise, so an own-accounts grant is unchanged.
                **(dict(jaah) if jaah else {}),
                # The Architectural Guard stanza where it is available; absent otherwise, so the grant is unchanged.
                **({"guard": dict(guard)} if guard else {}),
            },
            timeout=grant_timeout_seconds(),
            frame_type=node_bus.FRAME_CONSOLE_DIRECT_GRANT,
        )
    except node_bus.NodeNotConnected:
        raise DirectUnavailable("this node is not currently connected to the control plane") from None
    except node_bus.NodeRequestTimeout:
        raise DirectUnavailable(
            "this node did not acknowledge the direct console grant — it may be busy, or running a "
            "daemon that does not support direct mode"
        ) from None
    except (OSError, ValueError) as exc:
        log.warning("console-direct: grant to %s failed: %s", node_id, exc)
        raise DirectUnavailable("the direct console grant could not be delivered to this node") from None

    try:
        parsed = json.loads(reply.data or b"{}")
    except ValueError:
        parsed = None
    if not isinstance(parsed, dict) or not parsed.get("ok"):
        # Guarded on every read: `reply.data` is arbitrary node JSON (see `app/routes/console.py`).
        error = parsed.get("error") if isinstance(parsed, dict) else None
        raise DirectUnavailable(str(error or "this node refused the direct console grant")[:160])

    # Fail closed on a node that did not confirm the tier: only `is True` counts, since an old daemon
    # answers bare `{"ok": true}`, which would mint a writable 24h credential for a view principal. The
    # edit tier is unaffected.
    if read_only and parsed.get("read_only") is not True:
        log.warning(
            "console-direct: node %s did not confirm a read-only grant — refusing issuance rather than "
            "handing out a writable credential", node_id,
        )
        # Retract the writable grant the node already stored, by token (this grant only). The token never
        # leaves this process, but a live grant should not be left to the clock. Fire-and-forget.
        await broadcast_revoke(
            registry, session_id=session_id, project=project, number=number, kind=kind, token=token,
        )
        raise DirectUnavailable(
            "this node is running a daemon that does not support read-only consoles"
        )


async def _notify_revoke(registry: Any, node_id: str, frame: dict[str, Any]) -> bool:
    """Deliver one revoke frame to one node, bounded. True if the socket took it.

    Never raises — an undeliverable revoke is a logged residual.
    """
    conn = registry.get(node_id)
    if conn is None:
        return False
    # Per-node: the proof is keyed on each recipient's own credential.
    frame = {**frame, "cp_frame_proof": await cp_frame_proof_for_node_async(node_id)}
    try:
        await asyncio.wait_for(conn.notify(frame), timeout=_REVOKE_SEND_TIMEOUT)
        return True
    except asyncio.TimeoutError:
        log.warning(
            "console-direct: node %s did not take a revoke within %ss — the grant stands until it "
            "expires", node_id, _REVOKE_SEND_TIMEOUT,
        )
        return False
    except Exception as exc:  # noqa: BLE001 — fire-and-forget by contract; see broadcast_revoke
        log.warning("console-direct: could not deliver a revoke to %s: %s", node_id, exc)
        return False


async def broadcast_revoke(
    registry: Any, *, session_id: str, project: Optional[str] = None,
    number: Optional[int] = None, kind: Optional[str] = None, token: Optional[str] = None,
) -> int:
    """Revoke by `session_id` (+ slot, when known) on every node this process holds. Returns the number
    of nodes the frame was delivered to.

    A broadcast keyed on the session, since the CP keeps no token store. Per node fire-and-forget,
    concurrent and individually bounded (`_REVOKE_SEND_TIMEOUT`), so one wedged node cannot stall a
    sign-out; awaited, so every delivery was attempted on return.

    Residual: node grants live for the daemon's lifetime, not the bus connection's, and a node whose
    socket is down is skipped with no retry — its grants survive up to their TTL (a demoted viewer may
    keep a writable console). Closing it needs durable, replayable revocation intent.
    """
    from app import node_bus   # deferred, as in `_send_grant`

    frame = {
        "t": node_bus.FRAME_CONSOLE_DIRECT_REVOKE,
        "session_id": session_id, "project": project, "number": number, "kind": kind,
    }
    if token:
        frame["token"] = token
    node_ids = list(await registry.connected_nodes_async())
    results = await asyncio.gather(
        *(_notify_revoke(registry, node_id, frame) for node_id in node_ids)
    )
    delivered = sum(1 for reached in results if reached)
    slot = "all slots" if project is None else f"{project}-{number}/{kind}"
    log.info(
        "console-direct: revoked session %s (%s%s) on %d node(s)",
        session_id, slot, f", token {token_fingerprint(token)}" if token else "", delivered,
    )
    return delivered


async def revoke_all_on_node(registry: Any, node_id: str) -> bool:
    """Drop EVERY direct grant one node holds (`{"all": true}`) — its org changed (`app/tenancy/transfer_node.py`).
    True if the socket took the frame. Same residual as `broadcast_revoke`: a node whose socket this process does
    not hold keeps its grants until they expire."""
    from app import node_bus   # deferred, as in `_send_grant`

    return await _notify_revoke(registry, node_id, {"t": node_bus.FRAME_CONSOLE_DIRECT_REVOKE, "all": True})


def revoke_all_on_nodes_sync(registry: Any, node_ids: list[str]) -> None:
    """`revoke_all_on_node` for each of `node_ids`, from a synchronous caller (an after-commit hook). Never
    raises: a move must not fail on the bus."""
    import anyio.from_thread   # not stdlib; deferred so this module stays bare-`python3` importable

    async def _all() -> None:
        await asyncio.gather(*(revoke_all_on_node(registry, node_id) for node_id in node_ids))

    try:
        anyio.from_thread.run(_all)
    except Exception:   # noqa: BLE001 — see the docstring
        log.warning("console-direct: could not revoke the direct grants on moved node(s) %s", node_ids,
                    exc_info=True)


async def revoke_for_admin_session(registry: Any, admin_id: UUID, session_epoch: Optional[int]) -> int:
    """Revoke every direct grant issued to one browser session — the session-retirement hook.

    `session_epoch` must be the retired value, not the incremented one. Every `session_epoch` bump must
    call this (`app/routes/auth.py`, `app/routes/users.py`); account deletion is the last chance to
    compute that `session_id`. `revoke_for_permission_change_sync` is the exception: it passes the
    current epoch because the session continues.
    """
    return await broadcast_revoke(registry, session_id=session_id_for(admin_id, session_epoch))


# In-flight cap for the permission-change fan-out (`members x nodes` coroutines otherwise). Above the
# fleet's node count so one member's broadcast is never throttled.
_PERMISSION_REVOKE_CONCURRENCY = 16


async def revoke_for_permission_change(registry: Any, sessions: Any) -> int:
    """Drop every outstanding direct grant held by sessions whose permissions just changed.

    One concurrent fan-out, so latency is bounded by roughly one `_REVOKE_SEND_TIMEOUT`, not one per
    member. Delivery is best-effort: frames to one node serialise on its send lock while every deadline
    is already running, so a large tail can expire (WARNING each; API still 200). The fix is a batched
    `session_ids` frame (follow-up). `return_exceptions=True` so one failure never aborts the rest;
    returns the count of attempts that did not error, not confirmed deliveries.
    """
    # No empty-input guard: `gather()` of nothing returns 0. The useful guards are one layer out.
    listed = list(sessions)
    limit = asyncio.Semaphore(_PERMISSION_REVOKE_CONCURRENCY)

    async def _one(admin_id: Any, session_epoch: Any) -> int:
        async with limit:
            return await broadcast_revoke(
                registry, session_id=session_id_for(admin_id, session_epoch)
            )

    results = await asyncio.gather(
        *(_one(admin_id, epoch) for admin_id, epoch in listed), return_exceptions=True
    )
    for result in results:
        if isinstance(result, BaseException):
            log.warning("console-direct: a permission-change revoke failed: %s", result)
    return sum(1 for result in results if not isinstance(result, BaseException))


def revoke_for_permission_change_sync(registry: Any, sessions: Any) -> None:
    """`revoke_for_permission_change` for a synchronous FastAPI endpoint. Never raises.

    `sessions` is `(admin_id, session_epoch)` pairs with the current epoch — the session continues, its
    privileges changed. One portal hop for the whole set so the coroutine can gather.

    Needed because a grant carries its `read_only` tier into a node-side record for up to 24h with the
    CP out of the path. Revokes rather than re-grants (fail-closed): the browser re-fetches at the new
    tier. Swallows everything; see `broadcast_revoke` for the residual.
    """
    import anyio.from_thread   # not stdlib; deferred so this module stays bare-`python3` importable

    listed = list(sessions)
    if not listed:
        return
    try:
        anyio.from_thread.run(revoke_for_permission_change, registry, listed)
    except Exception:   # noqa: BLE001 — see the docstring: an edit must not fail on the bus
        log.warning(
            "console-direct: could not revoke direct grants after a permission change for %s session(s)",
            len(listed), exc_info=True,
        )


def revoke_for_admin_session_sync(registry: Any, admin_id: UUID, session_epoch: Optional[int]) -> None:
    """`revoke_for_admin_session` for a synchronous FastAPI endpoint. Never raises.

    A bridge rather than `async` routes: those routes do blocking work (bcrypt ~180ms) that must stay
    off the event loop. `anyio` gives the worker thread a portal to run the coroutine on the loop where
    node sockets live. Swallows everything: sign-out must not depend on the node bus; the grant stays
    bounded by its expiry.
    """
    import anyio.from_thread   # not stdlib; deferred so this module stays bare-`python3` importable

    try:
        anyio.from_thread.run(revoke_for_admin_session, registry, admin_id, session_epoch)
    except Exception:   # noqa: BLE001 — see the docstring: a retirement must not fail on the bus
        log.warning(
            "console-direct: could not revoke direct grants for admin %s (session epoch %s)",
            admin_id, session_epoch, exc_info=True,
        )


def record_used(node_id: str, frame: dict[str, Any]) -> None:
    """Consume a node's `console_direct_used` report so the CP keeps a view of who has shell access.

    Attribution comes from the token's claims (no store); only the fingerprint is logged (AC8.5). An
    unattributable report is logged as such rather than dropped.
    """
    event = frame.get("event")
    if event not in ("open", "close"):
        log.warning("console-direct: %s reported a use event with an unknown kind %r", node_id, event)
        return
    token = frame.get("token")
    claims = _parse(token)
    if claims is None:
        log.warning(
            "console-direct: %s reported a direct console %s for a token this control plane cannot "
            "attribute", node_id, event,
        )
        return
    # The full binding, so an expired or mis-slotted token is never recorded as a live shell. `claims`
    # supplies the slot because a `used` frame carries none.
    refusal = rejection_reason(
        token, node_id=node_id, project=claims.project, number=claims.number, kind=claims.kind,
    )
    if refusal is not None:
        log.warning(
            "console-direct: %s reported a direct console %s with a token that does not authorize it "
            "(%s)", node_id, event, refusal,
        )
        return
    log.info(
        "console-direct: %s %s a direct console on %s-%s/%s for session %s (token %s)",
        node_id, event, claims.project, claims.number, claims.kind, claims.session_id,
        token_fingerprint(str(token)),
    )
