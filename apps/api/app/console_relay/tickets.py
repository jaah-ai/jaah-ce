"""The `console_tickets` row — the ONE piece of state the API and the relay share (package docstring).

The API mints and sweeps; the relay claims, touches and finishes. A ticket is a capability, so only its
sha256 is stored, and no function here logs one.
"""
from __future__ import annotations

import hashlib
import logging
import secrets
from dataclasses import dataclass
from datetime import timedelta
from typing import Iterable, Optional
from uuid import UUID

from sqlalchemy import and_, delete, func, or_, select, update
from sqlalchemy.orm import Session

from app import hipaa
from app.db import SessionLocal
from app.models.control_plane import ConsoleTicket
from app.models.identity import AdminUser
from app.tenancy import seam

log = logging.getLogger("app.console_relay")

# How long both legs have to arrive, from the mint. It spans the node's `console_open` handling (up to
# `routes/console.py::CONSOLE_CONNECT_TIMEOUT`, 40s, for a create) plus the browser's dial after the API
# answers. An unredeemed ticket is a capability, so it expires; a PAIRED session outlives it.
TICKET_TTL_SECONDS = 90.0
# How often the owning relay touches a live session's row and reads `revoked` — the bound on how long a
# revoked viewer keeps an open console.
SEEN_INTERVAL_SECONDS = 30.0
# A paired row unseen this long belongs to a relay task that died; the API's sweep deletes it.
STALE_AFTER_SECONDS = 4 * SEEN_INTERVAL_SECONDS

# The paths, single-homed here: the relay serves them, the ALB rules route them, the pane and the node
# dial them.
BROWSER_PATH = "/api/console-relay"
NODE_PATH = "/api/console-stream"
# Relay-to-relay only. Under `/api/agent/` so the public ALB's machine-surface deny already covers it.
OWNER_LEG_PATH = "/api/agent/console-relay/owner-leg"
HEALTH_PATH = "/api/console-relay/health"

LEG_BROWSER = "browser"
LEG_NODE = "node"
LEGS = (LEG_BROWSER, LEG_NODE)

# The header a dialling node names itself in. Must match the daemon's `_NODE_ID_HEADER`
# (`apps/jaahd/console.py`; a test pins the two — a drift silently refuses every dial).
NODE_ID_HEADER = "X-Node-Id"
# WS close codes the node uses to say why a session ended. Which reason maps to which code is single-homed
# in the node's `_CLOSE_REASON_RETRYABLE` (apps/jaahd/console.py).
NODE_CLOSE_SESSION_ENDED = 4000     # the session itself is over (e.g. PTY EOF) — terminal
NODE_CLOSE_RECOVERABLE = 4001       # the VIEWER was dropped, the session outlives it — re-attachable
# `ConsoleTicket.revoked_reason` of a HIPAA org's console the sweep closed for inactivity.
REVOKED_IDLE = "idle"
# `ConsoleTicket.revoked_reason` of a console whose viewer's address left the org's IP allowlist.
REVOKED_IP = "ip_not_allowed"


def ticket_hash(ticket: str) -> str:
    """The stored spelling of a ticket: the row must not itself be the capability."""
    return hashlib.sha256(ticket.encode()).hexdigest()


@dataclass(frozen=True)
class Minted:
    """The two tickets of one open. `node` rides the `console_open` frame; `browser` the API's answer."""

    browser: str
    node: str


@dataclass(frozen=True)
class Claim:
    """A leg's successful claim: which session (`key`, the node ticket's hash), which relay task holds
    it, the viewer tier, and how long the other leg still has to arrive."""

    key: str
    owner_address: str
    read_only: bool
    seconds_left: float


def mint(
    *, node_id: str, node_name: str, read_only: bool,
    session_ref: tuple[UUID, int, int] | None, client_ip: Optional[str] = None,
) -> Minted:
    """Insert one session's row and return its two tickets. Committed; raises on a DB fault, which the
    caller refuses the open on. `client_ip` is the viewer's address: the browser leg must be claimed from it
    (`claim`), and the sweep ends the console once it leaves the org's IP allowlist."""
    minted = Minted(browser=secrets.token_urlsafe(32), node=secrets.token_urlsafe(32))
    user_id, epoch, iepoch = session_ref if session_ref is not None else (None, None, None)
    with SessionLocal() as session:
        session.add(ConsoleTicket(
            ticket_hash=ticket_hash(minted.node), browser_ticket_hash=ticket_hash(minted.browser),
            node_id=node_id, node_name=node_name, read_only=read_only,
            user_id=user_id, session_epoch=epoch, session_iepoch=iepoch, client_ip=client_ip,
            expires_at=func.now() + timedelta(seconds=TICKET_TTL_SECONDS),
        ))
        session.commit()
    return minted


def discard(minted: Minted) -> None:
    """Delete an open's row — the API's cleanup on every path that fails after the mint, so an abandoned
    open leaves no redeemable capability. Best effort: an undeleted row expires on its own."""
    try:
        with SessionLocal() as session:
            session.execute(delete(ConsoleTicket).where(ConsoleTicket.ticket_hash == ticket_hash(minted.node)))
            session.commit()
    except Exception as exc:  # noqa: BLE001 — see the docstring; the sweep prunes it at expiry
        log.warning("console-relay: could not discard a minted ticket pair (%s)", exc)


def claim(
    leg: str, ticket: object, *, self_address: str, node_id: object = None, client_ip: Optional[str] = None,
) -> Optional[Claim]:
    """Claim `leg` of the session `ticket` opens, naming this relay task its owner unless one already is.
    None for every refusal — unknown, expired, already claimed (single-use), (node leg) a dial from a
    node the ticket was not minted for, or (browser leg) a dial from an address other than the one that
    minted it (`client_ip`, the relay's `server.client_address`) or outside the viewer's org's IP allowlist.
    A refused claim consumes nothing.

    One UPDATE, so two relay tasks claiming the two legs at once agree on one owner (the row lock orders
    them; `coalesce` keeps the first)."""
    if leg not in LEGS or not isinstance(ticket, str) or not ticket:
        return None
    hashed = ticket_hash(ticket)
    if leg == LEG_NODE:
        if not isinstance(node_id, str) or not node_id:
            return None
        where = and_(
            ConsoleTicket.ticket_hash == hashed,
            ConsoleTicket.node_redeemed_at.is_(None),
            # The uuid only: names repeat (`app/hosts.py` NAMING), so a name would widen the binding.
            ConsoleTicket.node_id == node_id,
        )
        redeemed = {"node_redeemed_at": func.now()}
    else:
        if _browser_address_refused(hashed, client_ip):
            return None
        where = and_(
            ConsoleTicket.browser_ticket_hash == hashed,
            ConsoleTicket.browser_redeemed_at.is_(None),
            # A bearer token no more: only the minting address may redeem it (NULL: a row the outgoing release
            # minted). Re-asked here, so the read above cannot be raced.
            or_(ConsoleTicket.client_ip.is_(None), ConsoleTicket.client_ip == client_ip),
        )
        redeemed = {"browser_redeemed_at": func.now()}
    statement = (
        update(ConsoleTicket)
        .where(where, ConsoleTicket.expires_at > func.now(), ConsoleTicket.revoked.is_(False))
        .values(
            owner_address=func.coalesce(ConsoleTicket.owner_address, self_address),
            seen_at=func.now(), **redeemed,
        )
        .returning(
            ConsoleTicket.ticket_hash, ConsoleTicket.owner_address, ConsoleTicket.read_only,
            func.extract("epoch", ConsoleTicket.expires_at - func.now()),
        )
        .execution_options(synchronize_session=False)
    )
    with SessionLocal() as session:
        row = session.execute(statement).first()
        session.commit()
    if row is None:
        return None
    return Claim(key=row[0], owner_address=row[1], read_only=bool(row[2]), seconds_left=max(float(row[3]), 0.0))


def _browser_address_refused(hashed: str, client_ip: Optional[str]) -> bool:
    """Whether the browser leg of the row `hashed` names is dialled from an address its viewer's org's IP
    allowlist leaves out (`ip_allowlist.session_refusal`). An unknown ticket is not judged here: the claim
    refuses it."""
    from app import ip_allowlist  # lazy: it imports the login funnel

    with SessionLocal() as session:
        user_id = session.scalar(
            select(ConsoleTicket.user_id).where(ConsoleTicket.browser_ticket_hash == hashed)
        )
    return user_id is not None and ip_allowlist.session_refusal(user_id, client_ip) is not None


@dataclass(frozen=True)
class Touched:
    """What one `touch` learned: sessions whose viewer was revoked (closed for good), the subset of those
    the sweep closed for inactivity (`REVOKED_IDLE`) or because the viewer's address left the org's IP allowlist
    (`REVOKED_IP`, `blocked`), and sessions whose row is gone (closed for a
    reconnect — the pane re-opens through the API, which decides afresh)."""

    revoked: frozenset[str]
    vanished: frozenset[str]
    idled: frozenset[str] = frozenset()
    blocked: frozenset[str] = frozenset()


def touch(keys: Iterable[str], typed: Iterable[str] = ()) -> Touched:
    """Mark the owner's live sessions seen — and `typed`, those whose viewer sent a keystroke since the last
    touch, as typed in now (`last_input_at`) — and report the revoked and the vanished among them. A
    vanished row is not a verdict on the viewer (an outgoing API release prunes rows by their redeem
    deadline), so its session reconnects rather than ending; it must not keep running unseen either."""
    keys = list(keys)
    if not keys:
        return Touched(frozenset(), frozenset())
    typed = set(typed)
    with SessionLocal() as session:
        if typed:
            session.execute(
                update(ConsoleTicket).where(ConsoleTicket.ticket_hash.in_(typed)).values(last_input_at=func.now())
                .execution_options(synchronize_session=False)
            )
        rows = session.execute(
            update(ConsoleTicket)
            .where(ConsoleTicket.ticket_hash.in_(keys))
            .values(seen_at=func.now())
            .returning(ConsoleTicket.ticket_hash, ConsoleTicket.revoked, ConsoleTicket.revoked_reason)
            .execution_options(synchronize_session=False)
        ).all()
        session.commit()
    present = {key for key, _, _ in rows}
    return Touched(
        revoked=frozenset(key for key, revoked, _ in rows if revoked),
        vanished=frozenset(set(keys) - present),
        idled=frozenset(key for key, revoked, reason in rows if revoked and reason == REVOKED_IDLE),
        blocked=frozenset(key for key, revoked, reason in rows if revoked and reason == REVOKED_IP),
    )


def finish(key: str) -> None:
    """Delete a session's row once its relay is done with it. Best effort: the sweep reaps it when stale."""
    try:
        with SessionLocal() as session:
            session.execute(delete(ConsoleTicket).where(ConsoleTicket.ticket_hash == key))
            session.commit()
    except Exception as exc:  # noqa: BLE001 — see the docstring
        log.warning("console-relay: could not delete a finished session's row (%s)", exc)


def sweep(session: Session) -> int:
    """The API's half of the row's life, run on its peer heartbeat (`app/cp_peers.py::housekeeping`).
    Does not commit. Returns the number of sessions newly revoked.

    PRUNE: a row whose deadline passed with a leg still unclaimed (an abandoned open), and a paired row
    its relay has stopped touching (a relay task that died). REVOKE: a paired row whose viewer's login
    session is no longer live (disabled, logged out, password changed) — the relay reads the flag on its
    next touch and closes the browser with 4401 —, as `REVOKED_IP`, a row whose minting address its
    viewer's org's IP allowlist now leaves out (its own close code, 4403), and, as `REVOKED_IDLE`, a row whose viewer's org
    is HIPAA ON and whose last keystroke (or, with none, the browser's arrival) is more than
    `hipaa.IDLE_TIMEOUT` ago, which the relay closes with its idle code."""
    from app import ip_allowlist  # lazy: it imports the login funnel
    from app.auth import session_row_is_live  # lazy: app.auth imports the request machinery

    session.execute(delete(ConsoleTicket).where(or_(
        and_(
            ConsoleTicket.expires_at < func.now(),
            or_(ConsoleTicket.node_redeemed_at.is_(None), ConsoleTicket.browser_redeemed_at.is_(None)),
        ),
        ConsoleTicket.seen_at < func.now() - timedelta(seconds=STALE_AFTER_SECONDS),
    )))
    is_quiet = func.coalesce(ConsoleTicket.last_input_at, ConsoleTicket.browser_redeemed_at) < (
        func.now() - hipaa.IDLE_TIMEOUT
    )
    live = session.execute(
        select(ConsoleTicket.ticket_hash, ConsoleTicket.user_id, ConsoleTicket.session_epoch,
               ConsoleTicket.session_iepoch, is_quiet, ConsoleTicket.client_ip)
        .where(ConsoleTicket.revoked.is_(False), ConsoleTicket.user_id.is_not(None))
    ).all()
    if not live:
        return 0
    # Inside the identity escape: `admin_users` is tenant-owned and the heartbeat binds nothing, so a filtered
    # read would find no viewer and revoke every live console on every tick.
    with seam.identity_scope():
        users = {
            user.id: user
            for user in session.scalars(select(AdminUser).where(AdminUser.id.in_({row[1] for row in live})))
        }
        revoked = [
            key for key, user_id, epoch, iepoch, _, _ in live
            if not session_row_is_live(users.get(user_id), epoch, iepoch)
        ]
    if revoked:
        session.execute(
            update(ConsoleTicket).where(ConsoleTicket.ticket_hash.in_(revoked)).values(revoked=True)
            .execution_options(synchronize_session=False)
        )
    # The org's list as it holds now (cached `ip_allowlist.CACHE_TTL_SECONDS`), so a range removed after the open
    # ends the console within a heartbeat or two. A NULL address (the outgoing release's row) is not judged.
    blocked = [
        key for key, user_id, _, _, _, client_ip in live
        if key not in revoked and client_ip is not None and user_id in users
        and not ip_allowlist.exempt(users[user_id])
        and ip_allowlist.refusal(session, users[user_id].org_id, client_ip) is not None
    ]
    if blocked:
        session.execute(
            update(ConsoleTicket).where(ConsoleTicket.ticket_hash.in_(blocked))
            .values(revoked=True, revoked_reason=REVOKED_IP)
            .execution_options(synchronize_session=False)
        )
    revoked += blocked
    quiet = {key: users[user_id].org_id for key, user_id, _, _, quiet_now, _ in live
             if quiet_now and key not in revoked and user_id in users}
    hipaa_orgs = {org_id for org_id in set(quiet.values()) if hipaa.org_is_on(session, org_id)}
    idled = [key for key, org_id in quiet.items() if org_id in hipaa_orgs]
    if idled:
        session.execute(
            # Re-asked in the write: a keystroke the relay recorded since the read keeps its console.
            update(ConsoleTicket).where(ConsoleTicket.ticket_hash.in_(idled), is_quiet)
            .values(revoked=True, revoked_reason=REVOKED_IDLE)
            .execution_options(synchronize_session=False)
        )
    return len(revoked) + len(idled)
