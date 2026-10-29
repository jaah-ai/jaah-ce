"""Agent runs: agent sessions, their fetch stamps and telemetry, Jaah sessions and SDK agents."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional
from uuid import UUID
from sqlalchemy import BigInteger, Boolean, CheckConstraint, ForeignKey, Index, Integer, Text, text, UniqueConstraint
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column
from app.agent_sessions import (
    DEFAULT_AGENT_KIND,
    DEFAULT_STATUS as DEFAULT_AGENT_SESSION_STATUS,
    DEFAULT_TRANSCRIPT_STATUS as DEFAULT_AGENT_SESSION_TRANSCRIPT_STATUS,
    RUNTIME_LEGACY as AGENT_SESSION_RUNTIME_LEGACY,
    RUNTIMES as AGENT_SESSION_RUNTIMES,
    STATUSES as AGENT_SESSION_STATUSES,
    SWEEPABLE_TRANSCRIPT_STATUSES,
    TRANSCRIPT_STATUSES as AGENT_SESSION_TRANSCRIPT_STATUSES,
)
from app.agents import AGENT_STATUSES, AGENTS_TABLE, AGENT_ENABLEMENTS_TABLE
from app.tenancy.ids import ORG_ZERO_ID
from app.models.base import (
    _actor_column,
    Base,
    EntityMixin,
    _in_check,
    org_unique,
    OrgScoped,
    timestamp_column,
    UuidPkMixin,
)


class AgentSession(EntityMixin, OrgScoped, Base):
    """One agentic session (a Claude Code, later Codex, run) and where its transcript went.

    The durable record of the session: who ran it, where, against what, how it ended, and whether its
    transcript reached the bucket. Written by the events it records; nothing re-derives it.

    Named `AgentSession` so it does not shadow `sqlalchemy.orm.Session` in route modules; the table is
    `sessions`. A standard entity (operators manage rows on Audit > Sessions). No `slug`: the
    handle is the CLI's session UUID, in its own unique column. `created_by_id` defaults to the system
    actor for machine writers.

    All four references are nullable `SET NULL`: the row is evidence, the only pointer to a transcript
    that outlives its parents in object storage. The closed vocabularies live in `app/agent_sessions.py`
    (with why only two are DB CHECKs)."""

    __tablename__ = "sessions"
    __table_args__ = (
        org_unique(__tablename__),
        # The database refuses a second row per session id (AC-1.3); a named constraint so upserts can
        # target it. The key is global, so any upsert onto it must carry an ownership predicate, or one
        # node could repoint another node's transcript.
        UniqueConstraint("agent_session_id", name="uq_sessions_agent_session_id"),
        # The CHECKs mirrored from the DDL, rendered from the same constants: `migrations/env.py` targets
        # `Base.metadata`, so anything omitted here autogenerate would propose dropping.
        CheckConstraint(f"status IN {AGENT_SESSION_STATUSES}", name="ck_sessions_status"),
        CheckConstraint(
            f"transcript_status IN {AGENT_SESSION_TRANSCRIPT_STATUSES}",
            name="ck_sessions_transcript_status",
        ),
        CheckConstraint(f"runtime IN {AGENT_SESSION_RUNTIMES}", name="ck_sessions_runtime"),
        CheckConstraint(
            "transcript_status <> 'uploaded' OR transcript_manifest_key IS NOT NULL",
            name="ck_sessions_uploaded_names_a_manifest",
        ),
        CheckConstraint(
            "transcript_status <> 'uploading' OR transcript_leased_at IS NOT NULL",
            name="ck_sessions_uploading_holds_a_lease",
        ),
        # The partial sweep index, declared so autogenerate does not propose dropping it.
        Index(
            "ix_sessions_transcript_sweep", "transcript_status", "transcript_leased_at",
            postgresql_where=text(
                "transcript_status IN ("
                + ", ".join(f"'{state}'" for state in SWEEPABLE_TRANSCRIPT_STATUSES)
                + ")"
            ),
        ),
    )

    # ── Identity ──────────────────────────────────────────────────────────────────────────────────
    # The agent CLI's own session id: the join key to transcript files and logs. Supplied, never minted.
    agent_session_id: Mapped[str] = mapped_column(Text, nullable=False)
    # Defaulted, not nullable, so an unlabelled row cannot be confused with a future agent's.
    agent_kind: Mapped[str] = mapped_column(Text, nullable=False, server_default=DEFAULT_AGENT_KIND)
    # Which agent contract ran it (`agent_sessions.RUNTIMES`): set `acp` by the first event batch ingested
    # (`routes/agent_sessions.py::ingest_events`); it picks the one usage source the session is counted from.
    runtime: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=AGENT_SESSION_RUNTIME_LEGACY
    )
    # The runtime the dispatch CHOSE (`fleet_verdicts.dispatch_runtime_for_task`), for display only: a launch that died
    # before its first event still names the ACP host it was sent to. Never `runtime`'s writer — that column picks
    # the usage source. NULL = not known (a sensed or older row).
    dispatched_runtime: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # A dead run's pane-log tail, cleaned, redacted and capped (`app/pane_logs.py`, the single home of the rule).
    # `pane_log_captured_at` set with a NULL tail: the node had no pane log for the run. Both NULL: never asked.
    pane_log_tail: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    pane_log_redactions: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    pane_log_captured_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    # The pane log file's mtime on the node: a later run of the same task appends to the same file.
    pane_log_written_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    # The engine's own session id as the event stream names it (`native_session_ref`); NULL on legacy rows.
    native_session_ref: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # The highest event `seq` stored for an `acp` session. The stream's S3 chunks are fetched by derived key,
    # never listed (the CP's role has no `s3:ListBucket`), so this is how a reader knows where they end.
    events_last_seq: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    # The stream's `session_end` is stored: its usage was recomputed whole once, and no new event is accepted.
    events_ended: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    # The session this one resumed, by `agent_session_id`. Not an FK: a resume chain can reach sessions
    # this table never recorded.
    parent_session_id: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    # ── What it ran on and against ────────────────────────────────────────────────────────────────
    node_id: Mapped[Optional[UUID]] = mapped_column(
        ForeignKey("nodes.id", ondelete="SET NULL"), nullable=True, index=True
    )
    account_id: Mapped[Optional[UUID]] = mapped_column(
        ForeignKey("accounts.id", ondelete="SET NULL"), nullable=True
    )
    project_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("projects.id", ondelete="SET NULL"), nullable=True
    )
    # A real FK, unlike `worktrees.task_id`: this row is evidence opened from a task, so a dangling id is
    # worse than NULL. Indexed for the task page.
    task_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("tasks.id", ondelete="SET NULL"), nullable=True, index=True
    )
    # The task's project-memory arm (`tasks.memory_arm`) as it stood when this session was recorded, so a
    # session's arm survives the task's next dispatch re-resolving it. NULL: no task, or never resolved.
    memory_arm: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)

    # ── Lifecycle ─────────────────────────────────────────────────────────────────────────────────
    # Distinct from `created_at` (rows can be reconstructed later). Indexed for newest-first listing.
    started_at: Mapped[datetime] = timestamp_column(index=True)
    ended_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    # NOT NULL with a default, like `transcript_status` (AC-2.2), so every row answers "running?" and
    # "bytes arrived?".
    status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=DEFAULT_AGENT_SESSION_STATUS
    )
    # NULL while running. Domain: `agent_sessions.end_reasons()`.
    end_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # The cause SENTENCE behind `end_reason` (a closed word): the attempt's end reason or the runner's own break
    # report, redacted and capped by its writers (`agent_session_lifecycle.end_session`/`stamp_end_detail`). NULL:
    # nothing said why — the page then falls back to the node's journal lines.
    end_detail: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # The last event batch the CP REFUSED with a 4xx (`routes/agent_sessions.py::ingest_events`):
    # `{"status", "detail", "at", "first_seq"}`. The node retries a refusal forever and logs it only on its own
    # host, so without this an `acp` session with no events reads as "never streamed" instead of "streamed, refused".
    events_refused: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB, nullable=True)

    # ── Where it ran ──────────────────────────────────────────────────────────────────────────────
    # `<project>/<number>` as the session saw it; text, not an FK, since slots are re-leased constantly.
    worktree_ref: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    cwd: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    branch: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # What started the session (`fleet`/`user`/`orchestrator`), copied as historical fact.
    holder: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    pid: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    # The process start time makes the pid an identity (pids are reused).
    proc_started_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)

    # ── Analytics ─────────────────────────────────────────────────────────────────────────────────
    # NULL means not measured. `BigInteger`: long sessions pass 2^31 cumulative input tokens.
    model: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    tokens_in: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    tokens_out: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    subagent_count: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    # ── Transcript integrity ──────────────────────────────────────────────────────────────────────
    # These plus `transcript_bucket` are the whole surface the upload lease machine reads and writes.
    transcript_bucket: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # A prefix, not a key: per-file objects plus the manifest describing them.
    transcript_prefix: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    transcript_manifest_key: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    transcript_status: Mapped[str] = mapped_column(
        Text, nullable=False, server_default=DEFAULT_AGENT_SESSION_TRANSCRIPT_STATUS
    )
    transcript_bytes: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    # sha256 of the CP-authored manifest bytes, not the transcript: a transport check that cannot catch
    # a node lying about its files.
    transcript_sha256: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # NOT NULL 0: "no attempt yet" really is zero.
    transcript_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, server_default=text("0")
    )
    # Why a `failed` or `gave_up` row is so (only those two). Redact before writing: S3/STS exceptions
    # carry signatures, presigned URLs and Authorization headers.
    transcript_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # When the current `uploading` lease was taken; without it a dead uploader could never be reclaimed.
    transcript_leased_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    # Which SDK agent ran an `sdk_agent` session: `{"agent_id", "display_name", "version", "commit"}`, a
    # snapshot taken at launch so the row keeps naming the run after the agent changes. NULL otherwise.
    agent_ref: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB, nullable=True)


#: The one picker's exclusivity (DD12): a row names an SDK agent or a built-in provider, never both. Rendered
#: for `tasks` (`bound_provider`) and `projects` (`default_agent_id`/`default_provider`); revision 202610120000
#: carries the same literal.
AGENT_OR_PROVIDER_SQL = "NOT (agent_id IS NOT NULL AND {provider} IS NOT NULL)"


class FetchStamp(OrgScoped, Base):
    """When this node last `git fetch`ed a project's base."""

    __tablename__ = "fetch_stamps"

    # A PK member (0106). CASCADE: a pure cache.
    node_id: Mapped[UUID] = mapped_column(
        ForeignKey("nodes.id", ondelete="CASCADE"), primary_key=True
    )
    # PK `(node_id, project_id)`, never the editable project name. CASCADE: a pure cache.
    project_id: Mapped[int] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), primary_key=True
    )
    fetched_at: Mapped[datetime] = timestamp_column(server_now=False)


class RunTelemetry(OrgScoped, Base):
    """One run's telemetry — the relational projection `GET /api/telemetry/runs` reads.

    `run_id` has no sequence fallback: an INSERT that happens once needs no redelivery key.
    `apps/jaahd/main.py::_telemetry_run_id` is the implementation."""

    __tablename__ = "run_telemetry"
    __table_args__ = (
        org_unique(__tablename__),
        UniqueConstraint("run_id", name="uq_run_telemetry_run_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(Text, nullable=False)
    # SET NULL (0106): NULL already means unattributed.
    node_id: Mapped[Optional[UUID]] = mapped_column(ForeignKey("nodes.id", ondelete="SET NULL"), nullable=True)
    org_repo: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    task_id: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = timestamp_column()


#: `jaah_sessions.kind`: a fleet runner, or a console (incl. the advisor chat) a person opened.
JAAH_SESSION_KINDS = ("runner", "console")


class JaahSession(UuidPkMixin, OrgScoped, Base):
    """One session on the Jaah-provided Bedrock account (`app/jaah_routing/`): a runner dispatch or a
    console create the control plane routed to it. Written only by the CP, when it emits the dispatch plan's or
    the console frame's Jaah block — a node cannot invent one. It is the identity a node's credential request
    names (`bedrock_session:<id>`), the attribution of every call made under it (task, project, user), the
    AWS RoleSessionName (`jaah-<id hex>`), and what is ended to stop issuing credentials. Machinery, not
    user-authored: no entity shape. `task_id`/`project_id`/`user_id`/`node_id` are plain columns (provenance
    that outlives its subject)."""

    __tablename__ = "jaah_sessions"
    __table_args__ = (
        org_unique(__tablename__),
        _in_check("kind", JAAH_SESSION_KINDS, "ck_jaah_sessions_kind"),
        Index("ix_jaah_sessions_open", "started_at", postgresql_where=text("ended_at IS NULL")),
        Index("ix_jaah_sessions_task", "task_id"),
    )

    kind: Mapped[str] = mapped_column(Text, nullable=False)
    task_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    project_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    #: Runner: the task's launcher; console: the signed-in person who opened it.
    user_id: Mapped[Optional[UUID]] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    #: The node the session was routed to: the only node its credentials are served to.
    node_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    #: The runner's `sessions.agent_session_id`, once the node records it.
    agent_session_id: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    #: The last credential issue; a row never asked within `REAP_AFTER` of starting is reaped.
    credentials_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    started_at: Mapped[datetime] = timestamp_column()
    ended_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    #: Why it ended (`app/jaah_routing/sessions.py::END_REASONS`).
    end_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


class Agent(EntityMixin, OrgScoped, Base):
    """One registered SDK agent: a commit-pinned snapshot of a repo that runs through the daemon socket
    (every rule over a row is `app/agents.py`'s, the registration flow `app/agent_bundles.py`'s).

    An ORG agent is usable by its own org only. A CATALOG connector (`is_catalog`) is a row of the operator
    org (org zero, `app/tenancy/ids.py`), published by a superadmin and opted into per org through
    `agent_enablements`; the census requires a NOT NULL `org_id` on every tenant-owned row, so "no org" is
    spelled "the operator org, flagged catalog" rather than NULL.

    `slug` is the manifest's `name`, unique per org. `resolved_commit`/`bundle_*` are NULL until the first
    successful fetch and then only ever replaced by another good one: a failed refresh writes
    `resolve_error` and keeps serving the last snapshot (DD4). `manifest` is the last valid manifest.
    `repo_access_user_id` is whose GitHub connection reads the repo. `status`: `active` | `disabled` |
    `removed` (the catalog's withdrawal; enabled orgs see it marked, never silently gone).
    """

    __tablename__ = AGENTS_TABLE
    __table_args__ = (
        org_unique(__tablename__),
        UniqueConstraint("org_id", "slug", name="uq_agents_org_slug"),
        _in_check("status", AGENT_STATUSES, "ck_agents_status"),
        CheckConstraint(
            f"NOT is_catalog OR org_id = '{ORG_ZERO_ID}'::uuid", name="ck_agents_catalog_in_operator_org"
        ),
        CheckConstraint("resolved_commit IS NULL OR resolved_commit ~ '^[0-9a-f]{40}$'", name="ck_agents_commit"),
    )

    slug: Mapped[str] = mapped_column(Text, nullable=False)
    is_catalog: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default=text("false"))
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    agent_type: Mapped[str] = mapped_column(Text, nullable=False)
    language: Mapped[str] = mapped_column(Text, nullable=False)
    entrypoint: Mapped[str] = mapped_column(Text, nullable=False)
    version: Mapped[str] = mapped_column(Text, nullable=False)
    maintainer: Mapped[str] = mapped_column(Text, nullable=False)
    personas: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False)
    repo_url: Mapped[str] = mapped_column(Text, nullable=False)
    subpath: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default=text("''"))
    ref: Mapped[str] = mapped_column(Text, nullable=False)
    resolved_commit: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    bundle_key: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    bundle_sha256: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    bundle_bytes: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    manifest: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB, nullable=True)
    repo_access_user_id: Mapped[UUID] = mapped_column(
        ForeignKey("admin_users.id", ondelete="RESTRICT"), nullable=False
    )
    status: Mapped[str] = mapped_column(Text, nullable=False, default="active", server_default=text("'active'"))
    resolve_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # When the ref was last resolved, good or bad: the refresh sweep's per-agent throttle.
    resolved_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    # Name → value set on the agent's process at launch (`routes/agents.py::_checked_env`); not secrets.
    env: Mapped[dict[str, str]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )


class AgentEnablement(UuidPkMixin, OrgScoped, Base):
    """One org's opt-in to one catalog connector (`agents.is_catalog`). The writer
    (`routes/agents.py::enable_catalog_agent`) names only a catalog row; a connector withdrawn from the catalog
    keeps its enablements so those orgs see it marked removed rather than vanished (AC-13)."""

    __tablename__ = AGENT_ENABLEMENTS_TABLE
    __table_args__ = (
        org_unique(__tablename__),
        UniqueConstraint("org_id", "agent_id", name="uq_agent_enablements_org_agent"),
    )

    agent_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("agents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    created_at: Mapped[datetime] = timestamp_column()
    created_by_id: Mapped[UUID] = _actor_column()
