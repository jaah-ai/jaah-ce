"""Support: site enquiries and chat, the support knowledge base, tickets and support conversations."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional
from uuid import UUID
from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Float,
    ForeignKey,
    Identity,
    Index,
    Integer,
    String,
    Text,
    desc,
    text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from pgvector.sqlalchemy import VECTOR
from sqlalchemy.orm import Mapped, mapped_column
# Stdlib-only leaves: the ticket and email-template CHECKs render from their vocabularies.
from app import support_lifecycle
from app.entity_shape import SYSTEM_ACTOR_ID
from app.models.base import Base, hnsw_cosine_index, _in_check, org_unique, OrgScoped, timestamp_column, UuidPkMixin


#: `site_enquiries.source`.
SITE_ENQUIRY_SOURCE_FORM = "form"


SITE_ENQUIRY_SOURCE_JARVIS = "jarvis"


SITE_ENQUIRY_SOURCES = (SITE_ENQUIRY_SOURCE_FORM, SITE_ENQUIRY_SOURCE_JARVIS)


#: `site_chat_conversations.site`: which public site the widget ran on.
SITE_CHAT_SITES = ("docs", "www")


SITE_CHAT_ROLES = ("user", "assistant")


class SiteEnquiry(UuidPkMixin, OrgScoped, Base):
    """One contact-us or waitlist submission from the marketing site that passed validation and the
    reCAPTCHA check (`app/site_enquiry.py`, which owns every column's meaning).

    Always org zero: the site's visitors are the operator's leads, not any tenant's. Append-only apart
    from the two forward outcomes written once after the form is answered, so no entity shape
    (`tests/test_entity_shape.py` exempt list). `slack_posted_at`/`hubspot_contact_id` NULL with no error
    means the forward has not finished (or the process died before it did)."""

    __tablename__ = "site_enquiries"
    __table_args__ = (
        org_unique(__tablename__),
        Index("ix_site_enquiries_received_at", text("received_at DESC")),
        _in_check("source", SITE_ENQUIRY_SOURCES, "ck_site_enquiries_source"),
    )

    received_at: Mapped[datetime] = timestamp_column()
    name: Mapped[str] = mapped_column(Text, nullable=False)
    email: Mapped[str] = mapped_column(Text, nullable=False)
    company: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    role: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    offer: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    note: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    note_truncated: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    recaptcha_score: Mapped[float] = mapped_column(Float, nullable=False)
    slack_posted_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    slack_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    hubspot_contact_id: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    hubspot_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    #: Where the lead came from: the site's form, or website Jarvis's in-chat handoff (`app/site_chat`).
    source: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'form'"))
    #: The website chat the lead was handed off from — set only when the visitor's chat token proved it.
    site_chat_conversation_id: Mapped[Optional[UUID]] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey(
            "site_chat_conversations.id", ondelete="SET NULL", name="fk_site_enquiries_site_chat_conversation_id"
        ),
        nullable=True,
    )
    #: Who sent it, for tracing abuse after the fact (`site_enquiry.store`): the visitor's address, its peppered hash
    #: (comparable with `SiteChatConversation.ip_hash`), the user agent, and reCAPTCHA's `riskAnalysis.reasons`.
    #: NULL on rows stored before these were kept.
    visitor_ip: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    visitor_ip_hash: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    user_agent: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    recaptcha_reasons: Mapped[Optional[list]] = mapped_column(JSONB, nullable=True)


#: `trust_document_requests.state` (`ck_trust_document_requests_state`): awaiting an operator, or decided.
TRUST_REQUEST_PENDING, TRUST_REQUEST_APPROVED, TRUST_REQUEST_DECLINED = "pending", "approved", "declined"


TRUST_REQUEST_STATES = (TRUST_REQUEST_PENDING, TRUST_REQUEST_APPROVED, TRUST_REQUEST_DECLINED)


class TrustDocumentRequest(UuidPkMixin, OrgScoped, Base):
    """One anonymous visitor's request for trust documents (a SOC 2 report, a pentest summary, …) from the Trust
    page, and the operator's decision on it (`app/trust_requests.py`, which owns every column's meaning).

    Always org zero, like `SiteEnquiry`: the requester is a prospect, not any tenant's member. Not an entity (a
    request and its decision, like `access_requests`; `tests/test_entity_shape.py` exempt list): nobody creates
    one in the console, so there is no `created_by`; `decided_by_id` and `documents_sent_by_id` are the only actors
    that ever write it."""

    __tablename__ = "trust_document_requests"
    __table_args__ = (
        org_unique(__tablename__),
        _in_check("state", TRUST_REQUEST_STATES, "ck_trust_document_requests_state"),
        Index("ix_trust_document_requests_state_created_at", "state", text("created_at DESC")),
    )

    created_at: Mapped[datetime] = timestamp_column()
    #: Set by the API on each write (no `set_updated_at` trigger: not an entity).
    updated_at: Mapped[datetime] = timestamp_column()
    name: Mapped[str] = mapped_column(Text, nullable=False)
    company: Mapped[str] = mapped_column(Text, nullable=False)
    email: Mapped[str] = mapped_column(Text, nullable=False)
    #: `trust_requests.DOCUMENTS` keys, in the order the form offers them.
    documents: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False)
    purpose: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    #: The NDA the requester accepted: the operator's `legal_version` when they submitted, and when.
    nda_version: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    nda_accepted_at: Mapped[datetime] = timestamp_column(server_now=False)
    state: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'pending'"))
    decided_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    decided_by_id: Mapped[Optional[UUID]] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("admin_users.id", ondelete="SET NULL"), nullable=True
    )
    decline_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    #: The requester's decision email: when it was sent, or why not (`onboarding_mail.Outcome`, in words).
    notified_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    notify_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    #: The operators' Slack post on submit (the site-enquiry channel): when it went, or why not.
    slack_posted_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    slack_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    #: An approved request's files went out (an operator sends them outside the console): when, and who said so.
    #: NULL on an approved request means it still owes documents (`trust_requests.mark_sent`).
    documents_sent_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    documents_sent_by_id: Mapped[Optional[UUID]] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("admin_users.id", ondelete="SET NULL"), nullable=True
    )
    #: The sender's address as website Jarvis's peppered hash (`site_chat.access.ip_hash`); never the raw IP.
    ip_hash: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


class SiteChatConversation(UuidPkMixin, OrgScoped, Base):
    """One anonymous visitor's chat with website Jarvis (`app/site_chat`, which owns every column's meaning).

    Always org zero, like `SiteEnquiry`: an anonymous visitor belongs to no tenant. Holds no identity beyond
    what abuse control needs (AS3): a peppered hash of the IP, the user agent, the site and the page. The
    session token is stored only as its sha256. Kept indefinitely (D8) — but a chat handed off to an enquiry is erased with
    that prospect's data on their request (`routes/prospect_data.py`); no entity shape (exempt list)."""

    __tablename__ = "site_chat_conversations"
    __table_args__ = (
        org_unique(__tablename__),
        _in_check("site", SITE_CHAT_SITES, "ck_site_chat_conversations_site"),
        Index("ix_site_chat_conversations_created_at", text("created_at DESC")),
    )

    site: Mapped[str] = mapped_column(Text, nullable=False)
    page_url: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    locale: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    ip_hash: Mapped[str] = mapped_column(Text, nullable=False)
    user_agent: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("''"))
    token_hash: Mapped[str] = mapped_column(Text, nullable=False)
    token_expires_at: Mapped[datetime] = timestamp_column(server_now=False)
    turn_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=text("0"))
    created_at: Mapped[datetime] = timestamp_column()
    updated_at: Mapped[datetime] = timestamp_column()


class ChatTurnColumns:
    """What one stored turn records, on both chat tables (`SiteChatMessage`, `SupportMessage`): the message as
    stored (secrets redacted), the agent's own record of the reply — `tool_calls`, provider/model/token/latency —
    and the turn's failure code. Each table adds its conversation key."""

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    role: Mapped[str] = mapped_column(Text, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    tool_calls: Mapped[list[Any]] = mapped_column(
        JSONB, nullable=False, default=list, server_default=text("'[]'::jsonb")
    )
    #: The reply's answered tool rounds, replayed into the next turns (`support_agent/turn.py::replayed`); never
    #: served. NULL: no tool ran, or a reply from before it was kept.
    tool_exchange: Mapped[Optional[list[Any]]] = mapped_column(JSONB, nullable=True)
    provider: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    model: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    latency_ms: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    input_tokens: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    output_tokens: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    redaction_hits: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=text("0"))
    created_at: Mapped[datetime] = timestamp_column()


class SiteChatMessage(ChatTurnColumns, OrgScoped, Base):
    """One turn of a website chat, as stored (secrets redacted). `error` is the turn's failure code, or
    `refused` for a user message a later guard turned away — which the daily cap does not count."""

    __tablename__ = "site_chat_messages"
    __table_args__ = (
        _in_check("role", SITE_CHAT_ROLES, "ck_site_chat_messages_role"),
        Index("ix_site_chat_messages_conversation_created", "conversation_id", "created_at"),
        Index("ix_site_chat_messages_created_at", "created_at"),
    )

    conversation_id: Mapped[UUID] = mapped_column(
        PG_UUID(as_uuid=True),
        ForeignKey("site_chat_conversations.id", ondelete="CASCADE", name="fk_site_chat_messages_conversation_id"),
        nullable=False,
    )
    miss_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


# ── User support (epic "User Support Subsystem v1"; `app/support_lifecycle.py`, `app/support_access.py`) ─
#
# A ticket is its own entity, never a `Task` row (NC4): nothing here is dispatched, leased or scheduled.
# Int-keyed (shown `#<id>`) with a `title`, so it carries the entity shape's timestamps and actor pair but
# not its uuid/name/description (`tests/test_entity_shape.py` exempt list says why).


#: `support_kb_chunks.audience`: who may retrieve a chunk (`app/support_kb/search.py`).
SUPPORT_KB_AUDIENCES = ("everyone", "operator")


#: The embedding model's width, 768 (`app/support_kb/embeddings.py::DIMENSIONS`, spelled here as a literal so
#: the model needs no import from the package it stores for; `tests/support_kb` holds the two equal).
SUPPORT_KB_DIMENSIONS = 768


class SupportKbChunk(Base):
    """One embedded passage of the product's documentation — the support agent's knowledge base
    (`app/support_kb/__init__.py` states the design; `index.py` writes, `search.py` reads).

    Fleet-global reference data, NOT a tenant's: the corpus is the shipped image's own docs, identical
    for every org, and no org_id (the tenancy verdict is `Shared`, `app/tenancy/manifest.py`). Who may
    read a row is `audience`, decided per caller in `search.py`, never a scope. Written only by the
    `support-kb-index` and `support-kb-site` crons: rows are inserted and deleted by `content_hash` (only `url` is ever rewritten in place), so no entity
    shape (`tests/test_entity_shape.py`). `model` names the embedder a vector came from — a row of another
    model is stale, whatever its hash, because vectors of two models do not share a space.
    """

    __tablename__ = "support_kb_chunks"
    __table_args__ = (
        _in_check("audience", SUPPORT_KB_AUDIENCES, "ck_support_kb_chunks_audience"),
        UniqueConstraint("content_hash", name="uq_support_kb_chunks_content_hash"),
        Index("ix_support_kb_chunks_source_path", "source", "path"),
        hnsw_cosine_index("ix_support_kb_chunks_embedding", "embedding"),
    )

    id: Mapped[int] = mapped_column(BigInteger, Identity(), primary_key=True)
    source: Mapped[str] = mapped_column(Text, nullable=False)
    path: Mapped[str] = mapped_column(Text, nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    audience: Mapped[str] = mapped_column(Text, nullable=False)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)
    content_hash: Mapped[str] = mapped_column(Text, nullable=False)
    model: Mapped[str] = mapped_column(Text, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    # The docs portal page (and section anchor) a guide passage is on, relative (`/docs/...`); NULL for
    # every other source. Not in `content_hash`: the reconcile pass rewrites it in place, never re-embeds.
    url: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)
    embedding: Mapped[list[float]] = mapped_column(VECTOR(SUPPORT_KB_DIMENSIONS), nullable=False)
    created_at: Mapped[datetime] = timestamp_column()


class Ticket(OrgScoped, Base):
    """One support request. Its org is the REQUESTER'S; `app/support_access.py` decides who reads it.

    The four `last_*` stamps are written only by `app/support_activity.py::record_event`, inside the
    writing transaction, and feed the list's "last reply side" and every reader's unread marker:
    `last_public_at`/`last_reply_side` the latest public post, `last_customer_activity_at` the latest by
    the customer side (requester or org admin), `last_responder_public_at` the latest public post by
    anyone but the requester. `context` is captured at create (`{page_url, app_version, user_agent,
    requester_role}`) and served to operators only. `conversation_id` references the chat the ticket
    came from; the transcript is never copied here."""

    __tablename__ = "tickets"
    __table_args__ = (
        org_unique(__tablename__),
        CheckConstraint(support_lifecycle.TICKET_TITLE_CHECK_SQL, name=support_lifecycle.TICKET_TITLE_CHECK),
        CheckConstraint(support_lifecycle.TICKET_STATUS_CHECK_SQL, name=support_lifecycle.TICKET_STATUS_CHECK),
        CheckConstraint(
            support_lifecycle.TICKET_PRIORITY_CHECK_SQL, name=support_lifecycle.TICKET_PRIORITY_CHECK
        ),
        CheckConstraint(
            support_lifecycle.TICKET_REPLY_SIDE_CHECK_SQL, name=support_lifecycle.TICKET_REPLY_SIDE_CHECK
        ),
        CheckConstraint(support_lifecycle.TICKET_LESSON_CHECK_SQL, name=support_lifecycle.TICKET_LESSON_CHECK),
        Index("ix_tickets_org_status_updated", "org_id", "status", desc("updated_at")),
        Index("ix_tickets_requester_updated", "requester_id", desc("updated_at")),
        # One ticket per chat (and `support_conversations`' mirror index below).
        Index(
            "uq_tickets_conversation_id", "conversation_id", unique=True,
            postgresql_where=text("conversation_id IS NOT NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(
        Text, nullable=False, default=support_lifecycle.OPEN, server_default=support_lifecycle.OPEN
    )
    priority: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        default=support_lifecycle.DEFAULT_PRIORITY,
        server_default=support_lifecycle.DEFAULT_PRIORITY,
    )
    # RESTRICT, per the actor-column standard: a person with tickets is not silently erased from them.
    requester_id: Mapped[UUID] = mapped_column(
        ForeignKey("admin_users.id", ondelete="RESTRICT", name="fk_tickets_requester_id"), nullable=False
    )
    # `use_alter`: the two support tables reference each other.
    conversation_id: Mapped[Optional[int]] = mapped_column(
        BigInteger,
        ForeignKey(
            "support_conversations.id", ondelete="SET NULL", use_alter=True, name="fk_tickets_conversation_id"
        ),
        nullable=True,
    )
    context: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    last_public_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    last_reply_side: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    last_customer_activity_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    last_responder_public_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    # The lesson Jarvis learns from this ticket (`app/support_kb/lessons.py`): a `support_lifecycle.LESSON_*` status
    # (NULL: none owed) and its two halves, generic product knowledge with nothing of this org in it. Operator-only
    # on the wire; served to every tenant's Jarvis only once `approved`.
    lesson_status: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    lesson_problem: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    lesson_solution: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = timestamp_column()
    # Stamped by the writers that are a person's act (a post, a status or priority write) and by nothing
    # else — no `set_updated_at` trigger, since a read stamp or a link acknowledgement is not an update.
    updated_at: Mapped[datetime] = timestamp_column()
    created_by_id: Mapped[UUID] = mapped_column(
        ForeignKey("admin_users.id", ondelete="RESTRICT", name="fk_tickets_created_by_id"),
        nullable=False,
        server_default=text(f"'{SYSTEM_ACTOR_ID}'::uuid"),
    )
    updated_by_id: Mapped[UUID] = mapped_column(
        ForeignKey("admin_users.id", ondelete="RESTRICT", name="fk_tickets_updated_by_id"),
        nullable=False,
        server_default=text(f"'{SYSTEM_ACTOR_ID}'::uuid"),
    )


class TicketTaskLink(OrgScoped, Base):
    """One Task an operator linked to a ticket. The TASK is usually in another org (the operator's), so
    `task_id` is a cross-org reference held by no key, and only the task's STATE is ever read back
    (`app/support_access.py::linked_task_states`); `task_title_snapshot` is what an operator sees.

    `task_id` NULL means the task was deleted (the wire says `removed`). `done_at` is stamped when the
    task reaches done, `done_acked_at` when an operator next replies or writes the status
    (`app/support_activity.py`); a link with the first and not the second is the "task done" marker."""

    __tablename__ = "ticket_task_links"
    __table_args__ = (
        UniqueConstraint("ticket_id", "task_id", name="uq_ticket_task_links_ticket_task"),
        Index("ix_ticket_task_links_task_id", "task_id"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    ticket_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("tickets.id", ondelete="CASCADE", name="fk_ticket_task_links_ticket_id"),
        nullable=False,
    )
    task_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("tasks.id", ondelete="SET NULL", name="fk_ticket_task_links_task_id"), nullable=True
    )
    task_title_snapshot: Mapped[str] = mapped_column(Text, nullable=False)
    created_by_id: Mapped[UUID] = mapped_column(
        ForeignKey("admin_users.id", ondelete="RESTRICT", name="fk_ticket_task_links_created_by_id"),
        nullable=False,
        server_default=text(f"'{SYSTEM_ACTOR_ID}'::uuid"),
    )
    created_at: Mapped[datetime] = timestamp_column()
    done_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    done_acked_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)


class TicketRead(OrgScoped, Base):
    """When one person last read one ticket — the per-reader half of the unread marker. A pure edge.

    The reader may be an operator in another org than the ticket, so `user_id` is a reference and not
    the derivation (the ticket is)."""

    __tablename__ = "ticket_reads"

    ticket_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("tickets.id", ondelete="CASCADE", name="fk_ticket_reads_ticket_id"),
        primary_key=True,
    )
    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("admin_users.id", ondelete="CASCADE", name="fk_ticket_reads_user_id"), primary_key=True
    )
    last_read_at: Mapped[datetime] = timestamp_column()


class SupportConversation(OrgScoped, Base):
    """One person's chat with the support agent (behavior: the agent child). Its org is the user's.

    `ticket_id` is set, once, in the transaction that raises a ticket from it (`POST
    /api/support/tickets`), which is what refuses a second ticket from the same chat."""

    __tablename__ = "support_conversations"
    __table_args__ = (
        org_unique(__tablename__),
        Index("ix_support_conversations_user_updated", "user_id", desc("updated_at")),
        Index(
            "uq_support_conversations_ticket_id", "ticket_id", unique=True,
            postgresql_where=text("ticket_id IS NOT NULL"),
        ),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("admin_users.id", ondelete="CASCADE", name="fk_support_conversations_user_id"),
        nullable=False,
    )
    ticket_id: Mapped[Optional[int]] = mapped_column(
        BigInteger,
        ForeignKey("tickets.id", ondelete="SET NULL", name="fk_support_conversations_ticket_id"),
        nullable=True,
    )
    created_at: Mapped[datetime] = timestamp_column()
    updated_at: Mapped[datetime] = timestamp_column()
    turn_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default=text("0"))


class SupportMessage(ChatTurnColumns, OrgScoped, Base):
    """One turn of a support conversation, as stored (secrets already redacted). Immutable: no route
    updates it. `tool_calls` and the provider/model/token/latency columns are the agent's own record."""

    __tablename__ = "support_messages"
    __table_args__ = (
        CheckConstraint(support_lifecycle.MESSAGE_ROLE_CHECK_SQL, name=support_lifecycle.MESSAGE_ROLE_CHECK),
        Index("ix_support_messages_conversation_created", "conversation_id", "created_at"),
    )

    conversation_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("support_conversations.id", ondelete="CASCADE", name="fk_support_messages_conversation_id"),
        nullable=False,
    )


#: Why a turn is a docs miss: `search_docs` found nothing, or the model reported the passages did not cover
#: the question (`report_docs_gap`). Which turn is which: `app/support_agent/docs_misses.py::reason_for`.
DOCS_MISS_NO_PASSAGES = "no_passages"


DOCS_MISS_UNCOVERED = "uncovered"


DOCS_MISS_REASONS: tuple[str, ...] = (DOCS_MISS_NO_PASSAGES, DOCS_MISS_UNCOVERED)


class SupportDocsMiss(OrgScoped, Base):
    """One support-agent turn the documentation did not answer — a log row, read by the operator's
    "Unanswered questions" list (`GET /api/support/docs-misses`). At most one per assistant turn.

    Holds nothing a ticket does not: `question` is the user turn's stored (already redacted) content,
    `requester_id` the asker, `context` the ticket's `{page_url, app_version, requester_role}` keys.
    `question_key` is what the list groups by (`docs_misses.question_key`)."""

    __tablename__ = "support_docs_misses"
    __table_args__ = (
        _in_check("reason", DOCS_MISS_REASONS, "ck_support_docs_misses_reason"),
        # At most one miss per assistant turn.
        UniqueConstraint("message_id", name="uq_support_docs_misses_message_id"),
        Index("ix_support_docs_misses_question_key", "question_key"),
        Index("ix_support_docs_misses_created_at", "created_at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    conversation_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("support_conversations.id", ondelete="CASCADE", name="fk_support_docs_misses_conversation_id"),
        nullable=False,
    )
    #: The assistant turn that missed.
    message_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("support_messages.id", ondelete="CASCADE", name="fk_support_docs_misses_message_id"),
        nullable=False,
    )
    question: Mapped[str] = mapped_column(Text, nullable=False)
    question_key: Mapped[str] = mapped_column(Text, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    # CASCADE, not the ticket's RESTRICT: the row lives inside the asker's conversation, which already
    # goes with them, so a RESTRICT here could only ever block that delete.
    requester_id: Mapped[UUID] = mapped_column(
        ForeignKey("admin_users.id", ondelete="CASCADE", name="fk_support_docs_misses_requester_id"),
        nullable=False,
    )
    context: Mapped[dict[str, Any]] = mapped_column(
        JSONB, nullable=False, default=dict, server_default=text("'{}'::jsonb")
    )
    created_at: Mapped[datetime] = timestamp_column()
