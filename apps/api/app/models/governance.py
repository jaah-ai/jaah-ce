"""Governance: rule packs, rules, oracles, the entity graph, scans, ledger, exceptions and campaigns."""
from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any, Optional
from uuid import UUID
from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Float,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    Numeric,
    Text,
    text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column
from app.models.base import Base, EntityMixin, _in_check, org_unique, OrgScoped, timestamp_column, UuidPkMixin


# ── Living Governance (revision 0378) ─────────────────────────────────────────────────────────────────
# Rules as data, verdicts by code. The pure logic (cell states, predicates, scoring) is the governance package;
# the domain logic over these tables is `app/governance/`. A MANAGED pack is a global catalogue row
# (`gov_managed_packs`, the `built_in_playbooks` precedent: `org_id` is NOT NULL on every tenant table and the read
# seam fails closed, so a pack every org reads cannot live in a tenant table); an ORG pack (a fork, or one written
# from scratch) is the org's own `gov_packs` row. A `pack_id` held anywhere else names either kind — uuidv7 ids do
# not collide — and is resolved by `app/governance/packs.py`, the one reader of both.

#: `gov_entities.type`: the portfolio graph's ten entity types (AC-6). A new type is one CHECK widening.
GOV_ENTITY_TYPES = (
    "project", "repo", "service", "path", "team", "person", "agent", "model", "data_class", "environment",
)


#: `gov_edges.status`: whether an edge was inferred by a scan or confirmed by a person.
GOV_EDGE_STATUSES = ("inferred", "confirmed")


#: `gov_facts.status`: a user correction is a `rejected` row for the wrong value plus a `confirmed` one.
GOV_FACT_STATUSES = ("inferred", "confirmed", "rejected")


#: `gov_rules.mode`: all five stored so a later epic needs no migration; gate and lock are not offered yet (I6).
GOV_RULE_MODES = ("observe", "advise", "nudge", "gate", "lock")


#: `gov_scans.trigger`.
GOV_SCAN_TRIGGERS = ("manual", "daily", "rescan_repo")


#: `gov_scan_targets.status`.
GOV_SCAN_TARGET_STATUSES = (
    "not_scannable_no_repo", "not_scannable_no_node", "queued", "running", "done", "error",
)


#: `gov_rule_oracles.status`.
GOV_ORACLE_STATUSES = ("pass", "fail")


#: `gov_exceptions.status`.
GOV_EXCEPTION_STATUSES = ("active", "expired", "revoked")


#: `gov_pack_proposals.status`.
GOV_PROPOSAL_STATUSES = ("proposed", "applied", "dismissed")


def _jsonb(default: str) -> Any:
    return mapped_column(JSONB, nullable=False, server_default=text(f"'{default}'::jsonb"))


class _GovPackColumns:
    """What a pack is, whichever table holds it (S3 `[pack]`): its levels, pillars and unlock threshold."""

    slug: Mapped[str] = mapped_column(Text, nullable=False)
    current_version: Mapped[str] = mapped_column(Text, nullable=False)
    levels: Mapped[list[Any]] = _jsonb("[]")
    pillars: Mapped[list[Any]] = _jsonb("[]")
    unlock_threshold: Mapped[Decimal] = mapped_column(Numeric, nullable=False, server_default=text("0.8"))


class GovManagedPack(EntityMixin, _GovPackColumns, Base):
    """A managed pack (origin `managed`): one global copy, written only by the seeder from a TOML in
    `app/governance/packs/`. An org edits one only by forking it into `gov_packs`. `slug` is the TOML's own
    handle; its rules are its releases' snapshots (`gov_managed_pack_releases`), never rows of `gov_rules`."""

    __tablename__ = "gov_managed_packs"
    __table_args__ = (UniqueConstraint("slug", name="uq_gov_managed_packs_slug"),)


class GovManagedPackRelease(UuidPkMixin, Base):
    """One released version of a managed pack: the FULL rule snapshot (S3 rule shape). Immutable — a new
    version is a new row; an org on the managed pack pins one through `gov_org_settings.active_pack_version`."""

    __tablename__ = "gov_managed_pack_releases"
    __table_args__ = (UniqueConstraint("pack_id", "version", name="uq_gov_managed_pack_releases_version"),)

    pack_id: Mapped[UUID] = mapped_column(
        ForeignKey("gov_managed_packs.id", ondelete="CASCADE", name="fk_gov_managed_pack_releases_pack_id"),
        nullable=False,
    )
    version: Mapped[str] = mapped_column(Text, nullable=False)
    rules: Mapped[list[Any]] = _jsonb("[]")
    changelog: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    released_at: Mapped[datetime] = timestamp_column()


class GovPack(EntityMixin, OrgScoped, _GovPackColumns, Base):
    """An org's own pack (origin `org`): a fork of a managed pack (`parent_pack_id`/`parent_version`, and
    `upstream_version` — the managed version it has absorbed) or one written from scratch (no parent). Its
    rules are `gov_rules` rows, edited in place, each edit bumping the rule's `version`."""

    __tablename__ = "gov_packs"
    __table_args__ = (
        org_unique(__tablename__),
        UniqueConstraint("org_id", "slug", name="uq_gov_packs_org_slug"),
    )

    parent_pack_id: Mapped[Optional[UUID]] = mapped_column(
        ForeignKey("gov_managed_packs.id", ondelete="SET NULL", name="fk_gov_packs_parent_pack_id"), nullable=True
    )
    parent_version: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    upstream_version: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


class GovPackRelease(UuidPkMixin, OrgScoped, Base):
    """One snapshot of an org pack, written when it is forked and whenever a proposal is applied, so a
    proposal diffs two releases. Immutable, like a managed release."""

    __tablename__ = "gov_pack_releases"
    __table_args__ = (UniqueConstraint("pack_id", "version", name="uq_gov_pack_releases_version"),)

    pack_id: Mapped[UUID] = mapped_column(
        ForeignKey("gov_packs.id", ondelete="CASCADE", name="fk_gov_pack_releases_pack_id"), nullable=False
    )
    version: Mapped[str] = mapped_column(Text, nullable=False)
    rules: Mapped[list[Any]] = _jsonb("[]")
    changelog: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    released_at: Mapped[datetime] = timestamp_column()


class GovRule(EntityMixin, OrgScoped, Base):
    """A Rule of an org pack, as a first-class object (S3): owner, intent, rationale, scope (an S2 predicate
    over the portfolio graph's facts), projections, check, oracles, mode, exceptions policy, and `version`
    (bumped on every edit; a check or oracle edit leaves the rule `unverified` until oracles run for it).
    No `slug`: `key` is the handle, stable within its pack. `subject_kind` is free text validated in app code
    (`structural`, or `change` for an Architectural Guard rule), so a further subject needs no migration."""

    __tablename__ = "gov_rules"
    __table_args__ = (
        UniqueConstraint("pack_id", "key", name="uq_gov_rules_pack_key"),
        _in_check("mode", GOV_RULE_MODES, "ck_gov_rules_mode"),
        CheckConstraint("level BETWEEN 1 AND 5", name="ck_gov_rules_level"),
        CheckConstraint("version >= 1", name="ck_gov_rules_version"),
    )

    pack_id: Mapped[UUID] = mapped_column(
        ForeignKey("gov_packs.id", ondelete="CASCADE", name="fk_gov_rules_pack_id"), nullable=False
    )
    key: Mapped[str] = mapped_column(Text, nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("1"))
    subject_kind: Mapped[str] = mapped_column(Text, nullable=False, server_default="structural")
    owner_id: Mapped[Optional[UUID]] = mapped_column(
        ForeignKey("admin_users.id", ondelete="SET NULL", name="fk_gov_rules_owner_id"), nullable=True
    )
    intent: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    rationale: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    pillar: Mapped[str] = mapped_column(Text, nullable=False)
    level: Mapped[int] = mapped_column(Integer, nullable=False)
    applies_when: Mapped[dict[str, Any]] = _jsonb('{"all": []}')
    na_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    scope: Mapped[dict[str, Any]] = _jsonb('{"all": []}')
    projections: Mapped[dict[str, Any]] = _jsonb('{"audit": {}}')
    check: Mapped[dict[str, Any]] = _jsonb("{}")
    oracles: Mapped[list[Any]] = _jsonb("[]")
    mode: Mapped[str] = mapped_column(Text, nullable=False, server_default="advise")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    exceptions_policy: Mapped[dict[str, Any]] = _jsonb('{"waivable": true, "max_days": 180}')
    # The approved alternative / fix guidance the Architectural Guard quotes when it blocks ("Do instead: …");
    # required at gate and lock (`app/governance/authoring.py`).
    guidance: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


class GovRuleOracle(UuidPkMixin, OrgScoped, Base):
    """The latest oracle run for one rule version, in one org: ingest upserts it for every rule a scan's
    oracles report. No row for a rule's current version means `unverified`. Per org, because a managed rule
    is run by every org that scans it. `ledger_id` is provenance (no key)."""

    __tablename__ = "gov_rule_oracles"
    __table_args__ = (
        org_unique(__tablename__),
        UniqueConstraint("org_id", "pack_id", "rule_key", "rule_version", name="uq_gov_rule_oracles_rule_version"),
        _in_check("status", GOV_ORACLE_STATUSES, "ck_gov_rule_oracles_status"),
    )

    pack_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    rule_key: Mapped[str] = mapped_column(Text, nullable=False)
    rule_version: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    detail: Mapped[list[Any]] = _jsonb("[]")
    ledger_id: Mapped[Optional[UUID]] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    checked_at: Mapped[datetime] = timestamp_column()


class GovEntity(UuidPkMixin, OrgScoped, Base):
    """A node of the portfolio graph: one of `GOV_ENTITY_TYPES`, keyed naturally per org (`project:<id>`,
    `repo:<repo_url>`). Machinery the scan writes, not user-authored: no entity shape."""

    __tablename__ = "gov_entities"
    __table_args__ = (
        org_unique(__tablename__),
        UniqueConstraint("org_id", "type", "key", name="uq_gov_entities_org_type_key"),
        _in_check("type", GOV_ENTITY_TYPES, "ck_gov_entities_type"),
        Index("ix_gov_entities_project_id", "project_id"),
    )

    type: Mapped[str] = mapped_column(Text, nullable=False)
    key: Mapped[str] = mapped_column(Text, nullable=False)
    name: Mapped[str] = mapped_column(Text, nullable=False)
    project_id: Mapped[Optional[int]] = mapped_column(
        BigInteger, ForeignKey("projects.id", ondelete="CASCADE", name="fk_gov_entities_project_id"), nullable=True
    )
    attrs: Mapped[dict[str, Any]] = _jsonb("{}")
    created_at: Mapped[datetime] = timestamp_column()
    updated_at: Mapped[datetime] = timestamp_column()


class GovEdge(UuidPkMixin, OrgScoped, Base):
    """A typed edge of the portfolio graph (`project_has_repo`, …), `inferred` by a scan or `confirmed` by a
    person. Derives its org through BOTH ends, which must agree."""

    __tablename__ = "gov_edges"
    __table_args__ = (
        UniqueConstraint("src_id", "dst_id", "type", name="uq_gov_edges_src_dst_type"),
        _in_check("status", GOV_EDGE_STATUSES, "ck_gov_edges_status"),
        Index("ix_gov_edges_dst_id", "dst_id"),
    )

    src_id: Mapped[UUID] = mapped_column(
        ForeignKey("gov_entities.id", ondelete="CASCADE", name="fk_gov_edges_src_id"), nullable=False
    )
    dst_id: Mapped[UUID] = mapped_column(
        ForeignKey("gov_entities.id", ondelete="CASCADE", name="fk_gov_edges_dst_id"), nullable=False
    )
    type: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str] = mapped_column(Text, nullable=False)
    observed_at: Mapped[datetime] = timestamp_column()


class GovFact(UuidPkMixin, OrgScoped, Base):
    """One fact about a graph entity (`language=python`, `has_migrations=true`). An inferred fact never
    overwrites a `confirmed` or `rejected` row (`app/governance/facts.py`, the one writer). `source` is
    `ledger:<id>` or `user:<uuid>`."""

    __tablename__ = "gov_facts"
    __table_args__ = (
        UniqueConstraint("entity_id", "predicate", "value", name="uq_gov_facts_entity_predicate_value"),
        _in_check("status", GOV_FACT_STATUSES, "ck_gov_facts_status"),
    )

    entity_id: Mapped[UUID] = mapped_column(
        ForeignKey("gov_entities.id", ondelete="CASCADE", name="fk_gov_facts_entity_id"), nullable=False
    )
    predicate: Mapped[str] = mapped_column(Text, nullable=False)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str] = mapped_column(Text, nullable=False)
    evidence: Mapped[list[Any]] = _jsonb("[]")
    observed_at: Mapped[datetime] = timestamp_column()
    confirmed_by_id: Mapped[Optional[UUID]] = mapped_column(
        ForeignKey("admin_users.id", ondelete="SET NULL", name="fk_gov_facts_confirmed_by_id"), nullable=True
    )
    confirmed_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)


class GovOrgSettings(UuidPkMixin, OrgScoped, Base):
    """One org's Portfolio Compliance settings: the active pack (and, on a managed pack, the release it has
    applied — NULL = the managed pack's current version), the last selection the daily scan repeats, and
    whether the daily scan runs. Singleton per org: no entity shape."""

    __tablename__ = "gov_org_settings"
    __table_args__ = (
        org_unique(__tablename__),
        UniqueConstraint("org_id", name="uq_gov_org_settings_org_id"),
        CheckConstraint("guard_stuck_limit BETWEEN 1 AND 50", name="ck_gov_org_settings_guard_stuck_limit"),
    )

    active_pack_id: Mapped[Optional[UUID]] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    active_pack_version: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    last_selection: Mapped[list[int]] = mapped_column(
        ARRAY(BigInteger), nullable=False, server_default=text("'{}'::bigint[]")
    )
    daily_scan_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))
    #: Consecutive refusals of one guard rule a session takes before it asks a human.
    guard_stuck_limit: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("5"))
    updated_by_id: Mapped[Optional[UUID]] = mapped_column(
        ForeignKey("admin_users.id", ondelete="SET NULL", name="fk_gov_org_settings_updated_by_id"), nullable=True
    )
    updated_at: Mapped[datetime] = timestamp_column()


class GovScan(UuidPkMixin, OrgScoped, Base):
    """One scan of a selection of projects against one pack version. `started_by_id` NULL = the system."""

    __tablename__ = "gov_scans"
    __table_args__ = (
        org_unique(__tablename__),
        _in_check("trigger", GOV_SCAN_TRIGGERS, "ck_gov_scans_trigger"),
    )

    pack_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    pack_version: Mapped[str] = mapped_column(Text, nullable=False)
    trigger: Mapped[str] = mapped_column(Text, nullable=False)
    started_by_id: Mapped[Optional[UUID]] = mapped_column(
        ForeignKey("admin_users.id", ondelete="SET NULL", name="fk_gov_scans_started_by_id"), nullable=True
    )
    created_at: Mapped[datetime] = timestamp_column()
    finished_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)


class GovScanTarget(UuidPkMixin, OrgScoped, Base):
    """One repository of one project of a scan and how far it got. `task_id` is the auditor run's task (one per
    project: every repo target of a project shares it) and `ledger_id` the row its submit wrote; `project_repo_id`
    the `project_repos` row scanned, NULL on a target written before scans went per repo — all provenance, no key
    (a task or repo may be deleted; a ledger row is never)."""

    __tablename__ = "gov_scan_targets"
    __table_args__ = (
        Index("uq_gov_scan_targets_scan_project_repo", "scan_id", "project_id",
              text("COALESCE(project_repo_id, 0)"), unique=True),
        _in_check("status", GOV_SCAN_TARGET_STATUSES, "ck_gov_scan_targets_status"),
        Index("ix_gov_scan_targets_task_id", "task_id"),
    )

    scan_id: Mapped[UUID] = mapped_column(
        ForeignKey("gov_scans.id", ondelete="CASCADE", name="fk_gov_scan_targets_scan_id"), nullable=False
    )
    project_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("projects.id", ondelete="CASCADE", name="fk_gov_scan_targets_project_id"), nullable=False
    )
    project_repo_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    status: Mapped[str] = mapped_column(Text, nullable=False)
    task_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    ledger_id: Mapped[Optional[UUID]] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = timestamp_column()


class GovLedger(UuidPkMixin, OrgScoped, Base):
    """The conformance ledger: one IMMUTABLE row per attestation (I5: unsigned; `signature`/`sig_key_id` are
    reserved for the attestation epic). `subject_kind` + `subject_ref` (`repo` + `<repo_url>@<sha>` now; a
    `pr`/`session`/`deploy` subject later) admit change-level subjects without a migration. A BEFORE UPDATE OR
    DELETE trigger refuses both, except the org purge's delete (`token_billing.purging_org`, the door
    `token_ledger_entries` shares). Every reference is provenance with no foreign key: a key's ON DELETE would
    have to UPDATE or remove an immutable row. One row per scan target — one per repository of a scanned project (a
    retried submit cannot land twice)."""

    __tablename__ = "gov_ledger"
    __table_args__ = (
        org_unique(__tablename__),
        Index("ix_gov_ledger_subject", "subject_project_id", "pack_id", "created_at"),
        Index("uq_gov_ledger_scan_target_id", "scan_target_id", unique=True,
              postgresql_where=text("scan_target_id IS NOT NULL")),
    )

    subject_kind: Mapped[str] = mapped_column(Text, nullable=False)
    subject_ref: Mapped[str] = mapped_column(Text, nullable=False)
    subject_project_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    #: The `project_repos` row scanned; NULL on a row written before scans went per repo (its `subject_ref` URL
    #: names the repo).
    subject_repo_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    sha: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    pack_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    pack_version: Mapped[str] = mapped_column(Text, nullable=False)
    #: `{rule_key: {version, check_hash}}` — what each verdict was judged against.
    rules_snapshot: Mapped[dict[str, Any]] = _jsonb("{}")
    verdicts: Mapped[list[Any]] = _jsonb("[]")
    facts: Mapped[list[Any]] = _jsonb("[]")
    oracles: Mapped[list[Any]] = _jsonb("[]")
    tool_version: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    scan_target_id: Mapped[Optional[UUID]] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    task_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    node_id: Mapped[Optional[UUID]] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    created_at: Mapped[datetime] = timestamp_column()
    signature: Mapped[Optional[bytes]] = mapped_column(LargeBinary, nullable=True)
    sig_key_id: Mapped[Optional[str]] = mapped_column(Text, nullable=True)


#: `gov_guard_decisions.moment` / `.decision` (revision 202610121600).
GOV_GUARD_MOMENTS = ("write", "commit", "push")


GOV_GUARD_DECISIONS = ("denied", "error", "waived", "escalated")


class GovGuardDecision(UuidPkMixin, OrgScoped, Base):
    """One Architectural Guard decision that denied, errored, was waived or escalated (R6.1) — allowed writes are
    never recorded. Kept forever (G-5). Written by the node through `POST /api/agent/guard/decisions` and read by
    the console's Guard activity (`app/governance/guard_runtime.py`, the one home of both). Every reference is
    provenance without a foreign key, as on `gov_ledger`: deleting a project must not rewrite the audit trail.
    `rule_key`/`rule_version` are NULL for a bundle-level `error`; `task_id` for a console session. Machine-written,
    so no entity shape."""

    __tablename__ = "gov_guard_decisions"
    __table_args__ = (
        org_unique(__tablename__),
        _in_check("moment", GOV_GUARD_MOMENTS, "ck_gov_guard_decisions_moment"),
        _in_check("decision", GOV_GUARD_DECISIONS, "ck_gov_guard_decisions_decision"),
        Index("ix_gov_guard_decisions_org", "org_id", text("created_at DESC")),
        Index("ix_gov_guard_decisions_project", "org_id", "project_id", text("created_at DESC")),
        Index("ix_gov_guard_decisions_rule", "org_id", "pack_id", "rule_key", text("created_at DESC")),
    )

    project_id: Mapped[int] = mapped_column(BigInteger, nullable=False)
    task_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    session_ref: Mapped[str] = mapped_column(Text, nullable=False)
    runtime: Mapped[str] = mapped_column(Text, nullable=False)
    moment: Mapped[str] = mapped_column(Text, nullable=False)
    pack_id: Mapped[Optional[UUID]] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    rule_key: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    rule_version: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    mode: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    decision: Mapped[str] = mapped_column(Text, nullable=False)
    path: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    line: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    detail: Mapped[dict[str, Any]] = _jsonb("{}")
    created_at: Mapped[datetime] = timestamp_column()


class GovTwinCell(UuidPkMixin, OrgScoped, Base):
    """The compliance twin's grain: one rule's materialised state on one project under one pack, recomputed
    from the latest ledger row when an attestation lands or a rule, fact or exception changes
    (`app/governance/twin.py`, the one writer). `first_failed_at` anchors drift age; `prev_state` gives trend."""

    __tablename__ = "gov_twin_cells"
    __table_args__ = (
        org_unique(__tablename__),
        UniqueConstraint("project_id", "pack_id", "rule_key", name="uq_gov_twin_cells_project_pack_rule"),
    )

    project_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("projects.id", ondelete="CASCADE", name="fk_gov_twin_cells_project_id"), nullable=False
    )
    pack_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    rule_key: Mapped[str] = mapped_column(Text, nullable=False)
    state: Mapped[str] = mapped_column(Text, nullable=False)
    mode: Mapped[str] = mapped_column(Text, nullable=False)
    verdict: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    confidence: Mapped[Optional[float]] = mapped_column(Float, nullable=True)
    first_failed_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    prev_state: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    ledger_id: Mapped[Optional[UUID]] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    exception_id: Mapped[Optional[UUID]] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    updated_at: Mapped[datetime] = timestamp_column()


class GovTwinRepo(UuidPkMixin, OrgScoped, Base):
    """The twin's per-project summary under one pack: level (0 = below L1), pillar × level tallies (S4),
    open-waiver debt, and the scan it reflects."""

    __tablename__ = "gov_twin_repos"
    __table_args__ = (
        org_unique(__tablename__),
        UniqueConstraint("project_id", "pack_id", name="uq_gov_twin_repos_project_pack"),
    )

    project_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("projects.id", ondelete="CASCADE", name="fk_gov_twin_repos_project_id"), nullable=False
    )
    pack_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    level: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    pillars: Mapped[dict[str, Any]] = _jsonb("{}")
    debt_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default=text("0"))
    ledger_id: Mapped[Optional[UUID]] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    sha: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    scanned_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    updated_at: Mapped[datetime] = timestamp_column()


class GovException(EntityMixin, OrgScoped, Base):
    """A waiver as debt: an owner, a scope (one project, or an S2 predicate), a required expiry, compensating
    control and reason, and an automatic revisit (`app/governance/exceptions.py::expire_due` lapses it,
    recomputes the twin and notifies the owner). Exactly one of `project_id` and `scope`."""

    __tablename__ = "gov_exceptions"
    __table_args__ = (
        org_unique(__tablename__),
        _in_check("status", GOV_EXCEPTION_STATUSES, "ck_gov_exceptions_status"),
        CheckConstraint("num_nonnulls(project_id, scope) = 1", name="ck_gov_exceptions_one_scope"),
        Index("ix_gov_exceptions_due", "expires_at", postgresql_where=text("status = 'active'")),
    )

    pack_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    rule_key: Mapped[str] = mapped_column(Text, nullable=False)
    project_id: Mapped[Optional[int]] = mapped_column(
        BigInteger, ForeignKey("projects.id", ondelete="CASCADE", name="fk_gov_exceptions_project_id"), nullable=True
    )
    scope: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB, nullable=True)
    owner_id: Mapped[UUID] = mapped_column(
        ForeignKey("admin_users.id", ondelete="RESTRICT", name="fk_gov_exceptions_owner_id"), nullable=False
    )
    expires_at: Mapped[datetime] = timestamp_column(server_now=False)
    compensating_control: Mapped[str] = mapped_column(Text, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="active")
    revisit_notified_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)


class GovCampaign(EntityMixin, OrgScoped, Base):
    """A remediation campaign: a set of rules fixed across many projects, one Fix per project."""

    __tablename__ = "gov_campaigns"
    __table_args__ = (org_unique(__tablename__),)

    pack_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    rule_keys: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, server_default=text("'{}'::text[]"))


class GovCampaignItem(UuidPkMixin, OrgScoped, Base):
    """One project of a campaign, in its order, and the Fix task it got (provenance, no key)."""

    __tablename__ = "gov_campaign_items"
    __table_args__ = (UniqueConstraint("campaign_id", "project_id", name="uq_gov_campaign_items_campaign_project"),)

    campaign_id: Mapped[UUID] = mapped_column(
        ForeignKey("gov_campaigns.id", ondelete="CASCADE", name="fk_gov_campaign_items_campaign_id"), nullable=False
    )
    project_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("projects.id", ondelete="CASCADE", name="fk_gov_campaign_items_project_id"), nullable=False
    )
    task_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    ordinal: Mapped[int] = mapped_column(Integer, nullable=False)


class GovRemediation(UuidPkMixin, OrgScoped, Base):
    """One Fix: the rules it addresses on one project and the task it created. The remediation overlay is
    derived at read time from the latest row per (project, rule) — nothing is stored on the cell."""

    __tablename__ = "gov_remediations"
    __table_args__ = (
        org_unique(__tablename__),
        Index("ix_gov_remediations_project", "project_id", "pack_id"),
    )

    project_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("projects.id", ondelete="CASCADE", name="fk_gov_remediations_project_id"), nullable=False
    )
    pack_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    rule_keys: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, server_default=text("'{}'::text[]"))
    task_id: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    campaign_id: Mapped[Optional[UUID]] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    created_by_id: Mapped[Optional[UUID]] = mapped_column(
        ForeignKey("admin_users.id", ondelete="SET NULL", name="fk_gov_remediations_created_by_id"), nullable=True
    )
    created_at: Mapped[datetime] = timestamp_column()


class GovPackProposal(UuidPkMixin, OrgScoped, Base):
    """A managed pack's new version offered to one org: the rule diff, its predicted effect per project, and
    the org's decision. No proposal ever applies itself."""

    __tablename__ = "gov_pack_proposals"
    __table_args__ = (
        org_unique(__tablename__),
        _in_check("status", GOV_PROPOSAL_STATUSES, "ck_gov_pack_proposals_status"),
    )

    target_pack_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    from_version: Mapped[str] = mapped_column(Text, nullable=False)
    to_version: Mapped[str] = mapped_column(Text, nullable=False)
    diff: Mapped[dict[str, Any]] = _jsonb("{}")
    effect: Mapped[list[Any]] = _jsonb("[]")
    status: Mapped[str] = mapped_column(Text, nullable=False, server_default="proposed")
    decided_by_id: Mapped[Optional[UUID]] = mapped_column(
        ForeignKey("admin_users.id", ondelete="SET NULL", name="fk_gov_pack_proposals_decided_by_id"), nullable=True
    )
    decided_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    created_at: Mapped[datetime] = timestamp_column()
