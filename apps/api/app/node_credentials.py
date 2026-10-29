"""The single home of a node's own credential — minting one, storing it, and resolving a presented
token back to the node that holds it.

The credential names its holder, so a compromise is not fleet-wide, one node can be revoked, and
`node_id` is derived, not self-reported. Do not reintroduce a fleet-wide shared credential.
`nodes.node_token_hash` holds `sha256(plaintext)` (a 256-bit machine token gains nothing from a work
factor); the plaintext is returned once, so the CP cannot recover it — hence `cp_frame_proof`.
`resolve` returns the authenticating arm: `routes/agent/auth.py::_read_scope_for` grants the operator plane
to an enumerated set of arms, so an arm must never answer silently.
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
import secrets
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional
from uuid import UUID

from sqlalchemy import or_, select, update
from sqlalchemy.orm import Session

from app.dev_bypass import insecure_agent_enabled, is_local_env
from app.models.nodes import Node
from app.tenancy import seam

log = logging.getLogger("node-credentials")

# The minted token's visible prefix, so a secret scanner or an operator can recognise the credential.
# `app/node_bootstrap.py::_NODE_TOKEN_RE` spells it again; changing this alone makes every bootstrap
# render reject fresh tokens (asserted by `tests/test_node_credentials.py`). A reader outside `apps/api`
# belongs in the cross-tree credential parity test.
TOKEN_PREFIX = "snt_"

# 256 bits, urlsafe-base64'd to 43 characters: charset-restricted so it survives a shell heredoc, a
# `KEY=value` env line, an HTTP header and a JSON body.
_TOKEN_ENTROPY_BYTES = 32

# The ways a caller can be authenticated. Strings, not an enum: they are a wire and telemetry vocabulary
# (log lines) and must render one way everywhere.
#
# Only `ARM_NODE` authenticates a node and yields a `node_id`; the others are the control plane reaching
# its own agent surface. A presented fleet-wide shared token falls through to a plain 401.
ARM_NODE = "node"          # the caller presented its OWN per-node token: the destination state
ARM_CP_SELF = "cp_self"    # this control plane calling its own loopback API (see `cp_self_token`)
ARM_INSECURE = "insecure"  # `ALLOW_INSECURE_AGENT` on a local dev stack; never reachable in deployment

# How often one node's `node_token_used_at` may be re-stamped: operators need minute resolution, and
# per-request would put a write on the hottest read path.
_USED_AT_THROTTLE_SECONDS = 60.0

# Process-local throttle memory: `{node_id: monotonic seconds at last stamp}`. Lock-guarded, since the
# agent surface is served concurrently.
_used_at_seen: dict[UUID, float] = {}
_used_at_lock = threading.Lock()

@dataclass(frozen=True)
class AgentCaller:
    """Who just authenticated on the agent surface, and by which arm.

    `node_id` is derived — the row whose `node_token_hash` matched. Only `ARM_NODE` yields one; `None`
    means the caller is this control plane (`ARM_CP_SELF`, or `ARM_INSECURE` locally). What those arms
    are entitled to is decided only at `routes/agent/auth.py::_FLEET_WIDE_ARMS`; a route must never read
    `None` as "no node" and widen on its own.
    """

    node_id: Optional[UUID]
    arm: str
    org_id: Optional[UUID] = None
    """THE ORG THIS CREDENTIAL BELONGS TO — the read scope `routes/agent/auth.py::_check_token` binds.

    IT COMES OFF THE SAME ROW AND THE SAME QUERY AS `node_id`, never a second lookup: the agent surface
    is the hottest path in this process (every node heartbeats on it), and resolving the org separately
    would double its query count to answer a question the row already answered.

    `None` NOW HAS ONE CAUSE AND USED TO HAVE TWO. On an arm that derives no node (`cp_self`,
    `insecure`) there is no row to read an org from, and the caller's scope is decided by the ARM
    instead — that is the live one. On the `node` arm it would mean the node's own `org_id` had not been
    stamped, which `nodes.org_id` being NOT NULL (`app/models/base.py::OrgScoped`) has made unconstructible;
    the field keeps its `Optional` because the first cause is real and because that mixin leaves the
    ANNOTATION wide on purpose. Either way the caller is left UNBOUND, which `app/tenancy/seam.py`
    renders as `false()` and logs. Neither is ever widened to a default org. What the node arm's cause
    being unconstructible BUYS is argued where the value is consumed rather than restated here:
    `routes/agent/auth.py::_read_scope_for` is that consumer and is the single home for it."""

    @property
    def is_derived(self) -> bool:
        """Did the credential name this node, rather than the caller naming itself? Ask before trusting
        `node_id`."""
        return self.node_id is not None


def mint() -> str:
    """A fresh node token in plaintext. The only producer — never build one at a call site.

    Returned once and never recoverable: only `digest(...)` is persisted, so a lost token is re-issued.
    """
    return f"{TOKEN_PREFIX}{secrets.token_urlsafe(_TOKEN_ENTROPY_BYTES)}"


def digest(plaintext: str) -> str:
    """The stored form of one token: lowercase hex sha256 over its UTF-8 bytes.

    Hex because the column is `text` and must be greppable; the encoding is not a security property.
    """
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


# ── The control plane's own loopback identity ────────────────────────────────────────────────────────


def cp_self_token() -> str:
    """A token this control plane mints for itself, accepted on its own `/api/agent/*` surface.

    A domain-separated subkey of `SESSION_SECRET` (refused at boot when blank or published), so it can
    never substitute for a session cookie signature and nothing outside this process can produce it.

    Two callers present it: the api test harness (`tests/conftest.py`), wanting an authenticated,
    node-less, operator-plane caller, and one control-plane task forwarding a node-bus
    command or a console stream to the peer task holding the socket (`app/cp_peers.py`, the
    `/api/agent/cp-peer/*` routes). Every task derives the same value because they share `SESSION_SECRET`,
    which is what makes the CP its own credential across tasks. `ARM_CP_SELF` still binds `OPERATOR`.

    Falls back to a process-ephemeral random key when no session secret is configured (a process that
    skipped the boot check);
    producer and consumer are then the same process, and it beats a constant — but two local processes
    cannot authenticate to each other (`cp_self_key_is_ephemeral`).
    """
    return _cp_self_token_for(_session_secret_or_ephemeral())


def cp_self_key_is_ephemeral() -> bool:
    """Whether `cp_self_token` derives from the per-process random key rather than a configured secret —
    i.e. whether ANOTHER process would mint a different token. `app/cp_peers.py` refuses to forward on it."""
    return _session_secret_or_ephemeral() == _ephemeral_self_key


# The loopback token's label.
_CP_SELF_LABEL = b"jaah-cp-self-loopback-v1"


def _cp_self_token_for(key: str) -> str:
    return f"{TOKEN_PREFIX}cp{hmac.new(key.encode('utf-8'), _CP_SELF_LABEL, hashlib.sha256).hexdigest()}"


def _is_cp_self_token(presented: str) -> bool:
    """Whether `presented` is this CP's loopback token; compared constant-time."""
    return hmac.compare_digest(presented.encode(), _cp_self_token_for(_session_secret_or_ephemeral()).encode())


# The ephemeral fallback, minted once per process; `secrets` so the token cannot be derived from this
# repository. Limit: on a multi-worker process without `SESSION_SECRET`, workers disagree and the
# loopback call may be refused — configure a secret.
_ephemeral_self_key = secrets.token_urlsafe(32)


# The session secret this repository once published for local stacks; treated as unset, as
# `app/auth.py::resolve_session_secret` does, or the loopback token would be computable from a checkout.
# Spelled here because `app.auth` imports this module.
_PUBLISHED_DEV_SESSION_SECRET = "dev-session-secret-change-me"


def _session_secret_or_ephemeral() -> str:
    """The key `cp_self_token` derives from — a real configured secret, or a per-process random one.

    The published dev constant counts as unset, as in `resolve_session_secret`.
    """
    configured = os.environ.get("SESSION_SECRET", "")
    if not configured or configured == _PUBLISHED_DEV_SESSION_SECRET:
        return _ephemeral_self_key
    return configured


# ── The CP→node direction: proving the CP is the CP, without holding the node's token ───────────────


# Domain-separation label for `cp_frame_proof`, versioned so a derivation change yields a different value.
# The node spells it identically (`apps/jaahd/node_frame_auth.py`); a mismatch must read as refused.
_CP_FRAME_LABEL = b"jaah-cp-frame-v1"


def cp_frame_proof(token_hash: str) -> str:
    """What the CP puts in a CP→node frame to prove it is the control plane, given only the node's hash.

    Two frame families (`app/console_direct.py`, `app/routes/console.py`) need a double-guard the node
    enforces before recording a grant. The CP holds only `sha256(token)`; storing the token reversibly or
    keeping a fleet-wide secret would both undo per-node credentials.

    `HMAC-SHA256(key=sha256(token), msg="jaah-cp-frame-v1")`: the CP has the hash, the node derives it
    from its token, and a captured frame does not authenticate as the node.

    It proves the sender knows the node's hash (is the CP, or holds its database). It is not a channel
    binding or a replay guard; it rides the node's own authenticated socket as a second factor.
    """
    return hmac.new(token_hash.encode("utf-8"), _CP_FRAME_LABEL, hashlib.sha256).hexdigest()


def cp_frame_proof_for(session: Session, node_id: Optional[UUID]) -> str:
    """`cp_frame_proof` for one node, or `""` when that node has no credential yet.

    `POST /api/nodes` mints in the INSERT's transaction, so the roster no longer produces this;
    returning `""` keeps a console grant against an older row working instead of a 500.
    """
    if node_id is None:
        return ""
    token_hash = session.scalar(select(Node.node_token_hash).where(Node.id == node_id))
    return cp_frame_proof(token_hash) if token_hash else ""


# ── Issuing ─────────────────────────────────────────────────────────────────────────────────────────


def issue(session: Session, node: Node) -> str:
    """Mint a fresh credential for `node`, store its hash, and return the plaintext — once.

    Revocation is this function: overwriting `node_token_hash` makes the previous token stop resolving
    on the next request, with no list to propagate and no overlap window; no other row changes.

    Does not commit. On the creation path it must land in the `nodes` INSERT's transaction, so no row
    ever commits without a credential.
    """
    plaintext = mint()
    node.node_token_hash = digest(plaintext)
    # A rotation in flight dies with the credential it was rotating.
    node.pending_node_token_hash = None
    node.node_token_issued_at = datetime.now(timezone.utc)
    # Cleared: the new token must not claim the old one's last-used instant, which operators read to confirm
    # a rotation reached the node.
    node.node_token_used_at = None
    # And forgotten by the throttle: the new token's first use is its install starting, which the console shows
    # (`_write_used_at`), so an old token's recent stamp must not swallow it.
    with _used_at_lock:
        _used_at_seen.pop(node.id, None)
    return plaintext


def revoke(node: Node) -> None:
    """Clear `node`'s credential: no token resolves to it until `issue` mints one (the console's re-issue). For a
    node row kept while the machine goes (`routes/nodes/delete.py::_keep_row`). Does not commit."""
    node.node_token_hash = None
    node.pending_node_token_hash = None
    node.node_token_issued_at = None
    node.node_token_used_at = None


# ── Rotating: the node replaces its own credential ──────────────────────────────────────────────────
#
# The token an install starts with travelled in cleartext (the fetched bootstrap body, IMDS-readable cloud
# user-data), so each installer's last step (`jaah auth rotate`) trades it for one only node.env holds. Two
# steps, so a lost answer never strands the node: `rotate` stores the next token as PENDING — both resolve —
# and only `confirm_rotation`, presented the NEW token, retires the old one. Lose the rotate answer and the old
# token still works (a later rotate replaces the pending one); lose the confirm answer and the new one does.
# Callers lock the row (`SELECT ... FOR UPDATE`) and re-prove the presented token against it, as
# `routes/agent_bootstrap.py::_reenrolment_is_open` does.

ROTATION_PRESENTED_CURRENT = "current"
ROTATION_PRESENTED_PENDING = "pending"


def presented_as(node: Node, presented: str) -> Optional[str]:
    """Which of `node`'s credentials `presented` is: `ROTATION_PRESENTED_CURRENT`, `..._PENDING`, or `None`."""
    presented_digest = digest(presented)
    if node.node_token_hash and hmac.compare_digest(node.node_token_hash, presented_digest):
        return ROTATION_PRESENTED_CURRENT
    if node.pending_node_token_hash and hmac.compare_digest(node.pending_node_token_hash, presented_digest):
        return ROTATION_PRESENTED_PENDING
    return None


def rotate(node: Node) -> str:
    """Mint `node`'s next credential as pending and return its plaintext — once. The current token keeps
    resolving until `confirm_rotation`. Overwrites an unconfirmed earlier pending one, whose answer was lost.
    Does not commit."""
    plaintext = mint()
    node.pending_node_token_hash = digest(plaintext)
    return plaintext


def confirm_rotation(node: Node) -> None:
    """Promote `node`'s pending credential, which the caller just presented: the previous token stops resolving.
    Does not commit."""
    node.node_token_hash = node.pending_node_token_hash
    node.pending_node_token_hash = None
    now = datetime.now(timezone.utc)
    node.node_token_issued_at = now
    # It just authenticated this very call; NULL would replay the install-started event (`_write_used_at`).
    node.node_token_used_at = now


# ── Resolving ───────────────────────────────────────────────────────────────────────────────────────


def resolve(session: Session, presented: object) -> Optional[AgentCaller]:
    """Authenticate one presented `X-Agent-Token`, or `None`.

    The per-node lookup is tried first: a single indexed equality on the hottest path. Do not add an
    accepting branch for a credential several nodes hold identically.

    Fail-closed: an empty token never matches, an unrecognised value is refused, and the
    `ALLOW_INSECURE_AGENT` bypass is consulted only when nothing else answered (its local-only posture
    is asserted at boot: `app/dev_bypass.py::assert_agent_auth_posture`).
    """
    if not isinstance(presented, str) or not presented:
        # An absent token can still be the dev bypass, which depends on the server's posture, not the request.
        return _unconfigured_bypass()

    row = _node_for_token(session, presented)
    if row is not None:
        node_id, org_id = row
        _stamp_used(node_id)
        return AgentCaller(node_id=node_id, arm=ARM_NODE, org_id=org_id)

    # Bytes, not str: `hmac.compare_digest` raises TypeError on a non-ASCII str, which would be a 500.
    if _is_cp_self_token(presented):
        return AgentCaller(node_id=None, arm=ARM_CP_SELF)

    return _unconfigured_bypass()


def _unconfigured_bypass() -> Optional[AgentCaller]:
    """The local-dev opt-in, and nothing else.

    Refused whenever the environment is deployed; `is_local_env()` is the whole guard here, paired with
    `app/dev_bypass.py`'s boot refusal. An absent `APP_ENV` counts as not-local.

    Not tightened to also require an empty roster: that would make the bypass depend on DB state, so a
    developer who registered one node locally would find their tokenless stack silently refusing.
    """
    if not insecure_agent_enabled():
        return None
    if not is_local_env():
        return None
    return AgentCaller(node_id=None, arm=ARM_INSECURE)


def auth_unconfigured(session: Session) -> bool:
    """Is there nothing on this deployment that could authenticate an agent caller?

    Drives the agent surface's `503` (misconfiguration), kept distinct from the `401` a wrong token earns
    because they ask for different fixes. Consulted on the failure path only. An empty roster is the
    whole condition.
    """
    return not _any_credentialed_node(session)


def _any_credentialed_node(session: Session) -> bool:
    # Identity escape: a fleet-wide question with no org to ask it under, reached only on a refused request.
    with seam.identity_scope():
        return (
            session.scalar(select(Node.id).where(Node.node_token_hash.is_not(None)).limit(1))
            is not None
        )


def _node_for_token(session: Session, presented: str) -> Optional[tuple[UUID, Optional[UUID]]]:
    """The `(nodes.id, nodes.org_id)` whose stored hash — current or a rotation's pending one — matches, or
    `None`. Two indexed equalities (both columns carry a partial unique index), and no scan.

    Both columns come from the one row, sparing a second query per agent request. The digest is compared
    inside the database; a hash comparison needs no constant-time care, since the attacker controls the
    input, not the stored 256-bit digest.
    """
    presented_digest = digest(presented)
    # Under the identity escape: `nodes` is tenant-owned and this read is how the org is discovered.
    # See `app/tenancy/seam.py::identity_scope`.
    with seam.identity_scope():
        return session.execute(
            select(Node.id, Node.org_id).where(
                or_(Node.node_token_hash == presented_digest, Node.pending_node_token_hash == presented_digest)
            )
        ).first()


def _stamp_used(node_id: UUID) -> None:
    """Advance `node_token_used_at`, at most once per node per `_USED_AT_THROTTLE_SECONDS`.

    Handed to one background-lane thread (`app/db.py`), never written here: the caller is still holding the
    session it resolved the token on (an identity-lane connection), and a second checkout while holding one
    is the hold-and-wait that starved the pool. Never the request's session either: committing
    inside it would flush whatever the route had staged. Best-effort: a cosmetic clock never fails a request.
    """
    now = time.monotonic()
    with _used_at_lock:
        if now - _used_at_seen.get(node_id, 0.0) < _USED_AT_THROTTLE_SECONDS:
            return
        _used_at_seen[node_id] = now
    try:
        _stamper().submit(_write_used_at, node_id)
    except RuntimeError as exc:  # the interpreter is shutting down
        log.debug("node-auth: could not queue the last-used stamp for node %s (%s)", node_id, exc)


_stamper_executor = None


def _stamper():  # type: ignore[no-untyped-def]
    global _stamper_executor
    with _used_at_lock:
        if _stamper_executor is None:
            # Local import: `app.db` builds the engine at import, and this module is imported by `app.auth`.
            from app.db import background_executor

            _stamper_executor = background_executor(1, "node-token-stamp")
        return _stamper_executor


def _write_used_at(node_id: UUID) -> None:
    try:
        from app.db import SessionLocal
        from app.events import emit_console_event

        with SessionLocal() as stamp_session:
            now = datetime.now(timezone.utc)
            # The FIRST use of a token is its install command fetching the bootstrap: the console's roster shows
            # the node deploying from then, so it hears it live. Every later stamp is a cosmetic clock, silent.
            first = stamp_session.execute(
                update(Node)
                .where(Node.id == node_id, Node.node_token_used_at.is_(None))
                .values(node_token_used_at=now)
            ).rowcount
            if first:
                emit_console_event(stamp_session, entity="node", id=node_id, action="upserted")
            else:
                stamp_session.execute(update(Node).where(Node.id == node_id).values(node_token_used_at=now))
            stamp_session.commit()
    except Exception as exc:  # noqa: BLE001 — see `_stamp_used`: a cosmetic clock never fails a request
        log.debug("node-auth: could not stamp last-used for node %s (%s)", node_id, exc)

