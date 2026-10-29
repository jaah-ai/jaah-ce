"""Token metering: model prices, Bedrock profiles, the token ledger, wallets and top-ups."""
from __future__ import annotations

from datetime import date, datetime
from typing import Any, Optional
from uuid import UUID
from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    Text,
    text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column
from app.model_prices import PRICE_PRECISION, PRICE_SCALE
from app.models.base import Base, EntityMixin, _in_check, org_unique, OrgScoped, timestamp_column, UuidPkMixin


class ModelPrice(EntityMixin, Base):
    """One model's five token rates, in $/MTok — the operator-owned pricing table behind every console
    dollar figure (Settings).

    `name` is the canonical model id (`haiku`), unique because it is the join key to captured
    `usage_by_model` (`app/model_prices.py::prices_by_model`); the human label is `description`. Every
    spelling is folded onto its family by `app/model_prices.py::canonical_model`, applied to usage by
    `merge_usage` and to input by `routes/model_prices.py::_validated_name`.

    Five rate columns because Anthropic prices input five ways across a 40× spread (see
    `app/model_prices.py`). `NUMERIC` with that module's precision/scale. No `slug`. Seeded by revision
    0139 from `SEED_PRICES`; operator rows override that module's code fallbacks for vendor models.
    """

    __tablename__ = "model_prices"
    __table_args__ = (
        UniqueConstraint("name", name="uq_model_prices_name"),
        # A negative rate would credit a task. In DDL (0139) for writers the route never sees (seed,
        # psql), mirrored here; agreement is asserted by `tests/test_model_prices_schema.py`.
        CheckConstraint(
            "input_per_mtok >= 0 AND cache_write_5m_per_mtok >= 0 AND cache_write_1h_per_mtok >= 0 "
            "AND cache_read_per_mtok >= 0 AND output_per_mtok >= 0",
            name="ck_model_prices_rates_nonnegative",
        ),
    )

    input_per_mtok: Mapped[Any] = mapped_column(
        Numeric(PRICE_PRECISION, PRICE_SCALE), nullable=False
    )
    cache_write_5m_per_mtok: Mapped[Any] = mapped_column(
        Numeric(PRICE_PRECISION, PRICE_SCALE), nullable=False
    )
    cache_write_1h_per_mtok: Mapped[Any] = mapped_column(
        Numeric(PRICE_PRECISION, PRICE_SCALE), nullable=False
    )
    cache_read_per_mtok: Mapped[Any] = mapped_column(
        Numeric(PRICE_PRECISION, PRICE_SCALE), nullable=False
    )
    output_per_mtok: Mapped[Any] = mapped_column(
        Numeric(PRICE_PRECISION, PRICE_SCALE), nullable=False
    )


# ── Platform Bedrock (`app/bedrock/`) ───────────────────────────────────────────────────────────────────
# Machinery rows, not user-authored: written only by `app/bedrock/` on the operator's behalf, so no entity
# shape. Both are org-scoped `Stored` rows (`app/tenancy/manifest.py`): the org IS each row's subject.


class BedrockInferenceProfile(UuidPkMixin, OrgScoped, Base):
    """One tenant's Bedrock application inference profile for one model family — the ARN every Jaah-account
    call that tenant makes goes through (AC1.2). Created by `app/bedrock/inference_profiles.py::ensure_profiles`,
    copied from the newest system cross-region profile of the family (`source_model_id`) and tagged
    `TenantID=<org uuid>` only. One row per (org, family): a newer source replaces the row's ARN."""

    __tablename__ = "bedrock_inference_profiles"
    __table_args__ = (
        org_unique(__tablename__),
        UniqueConstraint("org_id", "model_family", name="uq_bedrock_inference_profiles_org_family"),
    )

    model_family: Mapped[str] = mapped_column(Text, nullable=False)
    source_model_id: Mapped[str] = mapped_column(Text, nullable=False)
    profile_arn: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = timestamp_column()
    updated_at: Mapped[datetime] = timestamp_column()


class BedrockRevocation(UuidPkMixin, OrgScoped, Base):
    """A tenant whose Bedrock access is revoked right now. Its presence puts the org's uuid in the invoke
    role's `jaah-tenant-deny` policy (`app/bedrock/revocation.py::sync_deny_policy`), which refuses even an
    already-minted session within seconds. One row per org; deleting it restores access."""

    __tablename__ = "bedrock_revocations"
    __table_args__ = (
        org_unique(__tablename__),
        UniqueConstraint("org_id", name="uq_bedrock_revocations_org_id"),
    )

    reason: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = timestamp_column()


# ── Jaah token billing (`app/token_billing/`) ──────────────────────────────────────────────────────────
# Machinery rows, not user-authored: written only by `app/token_billing/` (an operator's credit is a ledger
# row whose author is `created_by_id`), so no entity shape. Every one is an org-scoped `Stored` row: the org
# IS each row's subject. Money is integer micro-USD (AS1); `app/money.py` displays it.

#: `token_ledger_entries.kind` (`ck_token_ledger_entries_kind`). Credits add, the rest subtract, and
#: `spend_correction` is signed either way (`app/token_billing/ledger.py`).
LEDGER_CREDIT_KINDS = ("signup_credit", "operator_credit", "topup", "auto_recharge")


LEDGER_DEBIT_KINDS = ("operator_debit", "refund")


LEDGER_SPEND_KINDS = ("spend", "spend_correction")


LEDGER_KINDS = LEDGER_CREDIT_KINDS + LEDGER_DEBIT_KINDS + LEDGER_SPEND_KINDS


#: `token_wallets.floor_state` (`app/token_billing/floor.py`).
FLOOR_STATES = ("ok", "low", "floor")


#: `token_usage_calls.source` and `.flag`.
USAGE_SOURCES = ("node", "aws", "both")


USAGE_FLAGS = ("unattributed", "aws_only", "node_only", "mismatch")


class TokenLedgerEntry(UuidPkMixin, OrgScoped, Base):
    """One immutable change to a tenant's Jaah-account balance (AC4.1). A trigger refuses UPDATE, and DELETE
    except while the org itself is being erased (`app/tenancy/org_lifecycle.py::purge_org_rows`).
    `amount_micros = free_delta_micros + paid_delta_micros` (checked): the split is the draw order (AS3).
    `idempotency_key` is the business key: a retried write lands once."""

    __tablename__ = "token_ledger_entries"
    __table_args__ = (
        org_unique(__tablename__),
        UniqueConstraint("idempotency_key", name="uq_token_ledger_entries_idempotency_key"),
        _in_check("kind", LEDGER_KINDS, "ck_token_ledger_entries_kind"),
        CheckConstraint(
            "amount_micros = free_delta_micros + paid_delta_micros", name="ck_token_ledger_entries_split"
        ),
        Index("ix_token_ledger_entries_org_created", "org_id", "created_at"),
    )

    kind: Mapped[str] = mapped_column(Text, nullable=False)
    amount_micros: Mapped[int] = mapped_column(BigInteger, nullable=False)
    free_delta_micros: Mapped[int] = mapped_column(BigInteger, nullable=False)
    paid_delta_micros: Mapped[int] = mapped_column(BigInteger, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(Text, nullable=False)
    usage_call_id: Mapped[Optional[UUID]] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    #: The `token_topups.id` a top-up, recharge or refund entry is about (no FK: the ledger outlives nothing).
    topup_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    #: The operator or tenant admin who caused it; NULL = the system.
    created_by_id: Mapped[Optional[UUID]] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    created_at: Mapped[datetime] = timestamp_column()


class TokenUsageCall(UuidPkMixin, OrgScoped, Base):
    """One metered model call on the Jaah account (D8), from whichever source reported it first (the node
    live, or AWS's invocation log). `(org_id, call_key)` is the business key (`app/token_billing/call_key.py`).
    `node_tokens`/`aws_tokens` are keyed by `app/model_prices.py::TOKEN_CLASSES`. `charge_micros` is what the
    tenant paid (cost plus `markup_bps`); AWS's figure is authoritative once it arrives (I1)."""

    __tablename__ = "token_usage_calls"
    __table_args__ = (
        org_unique(__tablename__),
        UniqueConstraint("org_id", "call_key", name="uq_token_usage_calls_org_call_key"),
        _in_check("source", USAGE_SOURCES, "ck_token_usage_calls_source"),
        CheckConstraint(
            "flag IS NULL OR flag IN (" + ", ".join(f"'{v}'" for v in USAGE_FLAGS) + ")",
            name="ck_token_usage_calls_flag",
        ),
        Index("ix_token_usage_calls_org_occurred", "org_id", "occurred_at"),
        Index("uq_token_usage_calls_org_node_call_key", "org_id", "node_call_key", unique=True,
              postgresql_where=text("node_call_key IS NOT NULL")),
        Index("uq_token_usage_calls_org_aws_request_id", "org_id", "aws_request_id", unique=True,
              postgresql_where=text("aws_request_id IS NOT NULL")),
        Index("ix_token_usage_calls_session", "jaah_session_id", "occurred_at"),
    )

    call_key: Mapped[str] = mapped_column(Text, nullable=False)
    #: The `jaah_sessions` row the call ran under; SET NULL keeps the call (and its charge) if the row goes.
    jaah_session_id: Mapped[Optional[UUID]] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("jaah_sessions.id", ondelete="SET NULL"), nullable=True
    )
    task_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    project_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    user_id: Mapped[Optional[UUID]] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    model_family: Mapped[str] = mapped_column(Text, nullable=False)
    model_id: Mapped[str] = mapped_column(Text, nullable=False)
    node_tokens: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB, nullable=True)
    aws_tokens: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB, nullable=True)
    cost_micros: Mapped[int] = mapped_column(BigInteger, nullable=False)
    charge_micros: Mapped[int] = mapped_column(BigInteger, nullable=False)
    markup_bps: Mapped[int] = mapped_column(Integer, nullable=False)
    source: Mapped[str] = mapped_column(Text, nullable=False)
    occurred_at: Mapped[datetime] = timestamp_column(server_now=False)
    flag: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    #: Each source's own name for the call: the key the node reported it under, and AWS's `requestId`. They
    #: let a re-report be recognised when the node's key and the key AWS's record computes differ (K5's
    #: fallback hashes a timestamp the two sources stamp at different moments) — `app/token_billing/metering.py`.
    node_call_key: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    aws_request_id: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = timestamp_column()


class TokenWallet(UuidPkMixin, OrgScoped, Base):
    """A tenant's balance — a CACHE of its ledger, one row per org, updated under `SELECT … FOR UPDATE` in the
    same transaction as every ledger insert (`app/token_billing/ledger.py`). Invariant:
    `balance_micros = Σ token_ledger_entries.amount_micros = free_micros + paid_micros`."""

    __tablename__ = "token_wallets"
    __table_args__ = (
        org_unique(__tablename__),
        UniqueConstraint("org_id", name="uq_token_wallets_org_id"),
        _in_check("floor_state", FLOOR_STATES, "ck_token_wallets_floor_state"),
        CheckConstraint("balance_micros = free_micros + paid_micros", name="ck_token_wallets_split"),
    )

    balance_micros: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    free_micros: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    paid_micros: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    floor_state: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'ok'"))
    #: C8's de-duplication of the low-balance alert.
    low_alerted_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    updated_at: Mapped[datetime] = timestamp_column()


#: `token_topups.kind` / `.status` (`app/token_billing/topups.py`).
TOPUP_KINDS = ("topup", "auto_recharge")


TOPUP_STATUSES = ("pending", "paid", "failed")


class TokenTopup(OrgScoped, Base):
    """One Stripe charge that funds a tenant's Jaah AI balance: a top-up a tenant admin paid, or an
    auto-recharge (AC5.1, AC5.2). `pending` until Stripe's `invoice.paid` credits the ledger (`topup:<id>`) and
    marks it `paid`; `failed` keeps Stripe's sentence in `failure_reason`. `refunded_micros` is how much of it
    went back to the card (AC5.6). A failed auto-recharge holds the next one off until `suppressed_until`
    unless the tenant acts. `uq_token_topups_one_pending_recharge` keeps one auto-recharge pending per org.
    Machinery, not user-authored: no entity shape."""

    __tablename__ = "token_topups"
    __table_args__ = (
        org_unique(__tablename__),
        UniqueConstraint("request_id", name="uq_token_topups_request_id"),
        UniqueConstraint("stripe_invoice_id", name="uq_token_topups_stripe_invoice_id"),
        _in_check("kind", TOPUP_KINDS, "ck_token_topups_kind"),
        _in_check("status", TOPUP_STATUSES, "ck_token_topups_status"),
        CheckConstraint("amount_micros > 0", name="ck_token_topups_amount"),
        CheckConstraint("refunded_micros >= 0 AND refunded_micros <= amount_micros", name="ck_token_topups_refunded"),
        Index("ix_token_topups_org_created", "org_id", "created_at"),
        Index("uq_token_topups_one_pending_recharge", "org_id", unique=True,
              postgresql_where=text("kind = 'auto_recharge' AND status = 'pending'")),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    kind: Mapped[str] = mapped_column(Text, nullable=False)
    amount_micros: Mapped[int] = mapped_column(BigInteger, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default=text("'pending'"))
    #: The console's id for a top-up dialog: a resent request is the same top-up.
    request_id: Mapped[Optional[UUID]] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    stripe_invoice_id: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    stripe_payment_method_id: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    refunded_micros: Mapped[int] = mapped_column(BigInteger, nullable=False, server_default=text("0"))
    failure_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    suppressed_until: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    #: The tenant admin who paid; NULL = an auto-recharge.
    created_by_id: Mapped[Optional[UUID]] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    created_at: Mapped[datetime] = timestamp_column()
    updated_at: Mapped[datetime] = timestamp_column()


class BedrockReconciliation(UuidPkMixin, OrgScoped, Base):
    """One tenant's UTC day of Jaah AI usage checked against AWS (AC1.4, `app/token_billing/reconcile.py`):
    what Jaah metered (Σ `token_usage_calls.cost_micros`, Bedrock's price before markup) beside what Cost
    Explorer reports for the tenant's `TenantID` tag. Written by the daily `bedrock-reconcile` cron, re-run in
    place for the same day; it never changes a balance. Machinery, not user-authored: no entity shape."""

    __tablename__ = "bedrock_reconciliations"
    __table_args__ = (
        org_unique(__tablename__),
        UniqueConstraint("org_id", "day", name="uq_bedrock_reconciliations_org_day"),
        Index("ix_bedrock_reconciliations_day", "day"),
    )

    day: Mapped[date] = mapped_column(Date, nullable=False)
    jaah_cost_micros: Mapped[int] = mapped_column(BigInteger, nullable=False)
    aws_cost_micros: Mapped[int] = mapped_column(BigInteger, nullable=False)
    #: The operator's tolerance when the row was written, and whether the difference exceeded it.
    tolerance_bps: Mapped[int] = mapped_column(Integer, nullable=False)
    over_tolerance: Mapped[bool] = mapped_column(Boolean, nullable=False)
    created_at: Mapped[datetime] = timestamp_column()
    updated_at: Mapped[datetime] = timestamp_column()
