"""The control plane's own runtime: peer processes, ECS tasks, console tickets, rate limits, idempotency keys
and flags."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional
from uuid import UUID
from sqlalchemy import BigInteger, Boolean, Identity, Index, Integer, LargeBinary, SmallInteger, String, Text, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column
from app.models.base import Base, timestamp_column


class SystemState(Base):
    """Tiny key-value store for app-level flags."""

    __tablename__ = "system_state"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict, nullable=False)
    updated_at: Mapped[datetime] = timestamp_column(onupdate=True)


class CpPeer(Base):
    """One running control-plane API process (an ECS task, a local api container) — the peer registry.

    Fleet-plane runtime state, no tenant: `app/cp_peers.py` is the single home (registration, heartbeat,
    prune, forwarding). `address` is the private `http://<ip>:<port>` other peers forward to; `heartbeat_at`
    is liveness (`PEER_STALE_SECONDS`). Deleting a peer cascades its sockets and console tickets."""

    __tablename__ = "cp_peers"

    id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    address: Mapped[str] = mapped_column(Text, nullable=False)
    started_at: Mapped[datetime] = timestamp_column(server_now=False)
    heartbeat_at: Mapped[datetime] = timestamp_column(server_now=False)


class CpEcsTask(Base):
    """One ECS task of the control plane's OWN service, as ECS last described it — kept past ECS's ~1 h
    memory of a stopped task, so a replaced task's stop reason is still readable days later.

    Fleet-plane runtime state, no tenant: `app/cp_ecs.py` is the single home (the recorder, its prune, the
    read). Upserted on `task_arn` each time the recorder sees the task; a stopped row stops changing once ECS
    forgets it. `containers` is each container's `{name, last_status, exit_code, reason, health_status}`;
    `deploy_run_id` is the GitHub Actions run that rolled the task's revision out (its `JaahDeployRun` tag)."""

    __tablename__ = "cp_ecs_tasks"

    task_arn: Mapped[str] = mapped_column(Text, primary_key=True)
    task_definition_arn: Mapped[str] = mapped_column(Text, nullable=False)
    availability_zone: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    private_ip: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    last_status: Mapped[str] = mapped_column(Text, nullable=False)
    desired_status: Mapped[str] = mapped_column(Text, nullable=False)
    health_status: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    started_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    stopping_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    stopped_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    stopped_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    stop_code: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    containers: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    deploy_run_id: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    first_seen_at: Mapped[datetime] = timestamp_column(server_now=False)
    last_seen_at: Mapped[datetime] = timestamp_column(server_now=False, index=True)


class ConsoleTicket(Base):
    """One console session's rendezvous, shared by the API that sets it up and the relay that carries it
    (`app/console_relay/`). The API mints two single-use tickets per open — one for the browser's leg,
    one for the node's dial-back — and stores only their sha256 hashes here; the first relay task a leg
    reaches claims the row (`owner_address`) and every later leg is proxied to that task. Pruned and
    revoked by the API's peer heartbeat (`app/console_relay/tickets.py::sweep`)."""

    __tablename__ = "console_tickets"
    __table_args__ = (
        Index("uq_console_tickets_browser_ticket_hash", "browser_ticket_hash", unique=True),
    )

    # The NODE leg's ticket hash (the row's original meaning, kept as the key).
    ticket_hash: Mapped[str] = mapped_column(Text, primary_key=True)
    browser_ticket_hash: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # The node the dial-back must come from, in both spellings: the bus key and the `nodes.name`.
    node_id: Mapped[str] = mapped_column(Text, nullable=False)
    node_name: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # The relay discards every browser frame when set — the viewer tier, decided by the API at open.
    read_only: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    # The viewer's session (`auth.session_row_is_live` inputs), re-checked by the API's sweep; the browser leg must
    # carry it (`console_relay/tickets.py::claim`).
    user_id: Mapped[Optional[UUID]] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    session_epoch: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    session_iepoch: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    # The viewer's address: the mint's, then the browser leg's own once claimed. The sweep ends the console once it
    # leaves the org's IP allowlist (`app/ip_allowlist.py`); a row minted without a session must be claimed from it.
    # NULL on a row of the outgoing release.
    client_ip: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # The relay task holding the rendezvous (`http://ip:port`), set by the first leg to arrive.
    owner_address: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    node_redeemed_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    browser_redeemed_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    # Set by the API's sweep when the viewer's session ends; the relay closes the browser with 4401.
    revoked: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    # Why the sweep set `revoked`: NULL for an ended login session, `idle` for a HIPAA org's console with no
    # keystroke for `hipaa.IDLE_TIMEOUT`, `ip_not_allowed` for a viewer whose address left the org's IP allowlist
    # (the relay closes each of those two with its own code).
    revoked_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # The viewer's last keystroke as the owning relay's touch saw it (to `SEEN_INTERVAL_SECONDS`); NULL is
    # none since the browser leg arrived.
    last_input_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    # The owning relay's last touch of a live session; a row unseen for long is a dead relay's.
    seen_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    # Redeem-by deadline for both legs; a paired session outlives it.
    expires_at: Mapped[datetime] = timestamp_column(server_now=False)


class RateLimitHit(Base):
    """One counted request for a rate-limit `bucket` and `key`, shared across peers so a limit holds
    fleet-wide rather than per process. The peer heartbeat prunes rows past their
    bucket's window, and every row after a day."""

    __tablename__ = "rate_limit_hits"
    __table_args__ = (Index("ix_rate_limit_hits_bucket_key_hit_at", "bucket", "key", "hit_at"),)

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    bucket: Mapped[str] = mapped_column(Text, nullable=False)
    key: Mapped[str] = mapped_column(Text, nullable=False)
    hit_at: Mapped[datetime] = timestamp_column(server_now=False)


class RequestIdempotency(Base):
    """One console write's idempotency key and the answer it got (`app/idempotency.py`, its single home).

    Keyed by principal (`admin_id` from the session) and the client's key; never a tenant object. `state` is
    `running` from the claim (committed before the route runs) to `done` (answer stored). `body` NULL with a
    `status` is an answer too large to keep. Rows past `idempotency.RETENTION` are ignored and pruned by the
    `request-idempotency-prune` cron."""

    __tablename__ = "request_idempotency"
    __table_args__ = (Index("ix_request_idempotency_created_at", "created_at"),)

    admin_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), primary_key=True)
    key: Mapped[str] = mapped_column(Text, primary_key=True)
    method: Mapped[str] = mapped_column(Text, nullable=False)
    path: Mapped[str] = mapped_column(Text, nullable=False)
    body_sha256: Mapped[str] = mapped_column(Text, nullable=False)
    state: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[Optional[int]] = mapped_column(SmallInteger, nullable=True)
    headers: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    body: Mapped[Optional[bytes]] = mapped_column(LargeBinary, nullable=True)
    created_at: Mapped[datetime] = timestamp_column()
    heartbeat_at: Mapped[datetime] = timestamp_column()
    completed_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
