"""Worktree slots, their port grid and slot intents, and the co-located (vnode) hosts they run on."""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional
from uuid import UUID
from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    Text,
    text,
    UniqueConstraint,
)
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.orm import Mapped, mapped_column
from app.entity_shape import SYSTEM_ACTOR_ID
from app.models.base import Base, EntityMixin, org_unique, OrgScoped, timestamp_column, UuidPkMixin


class PortGridVar(EntityMixin, OrgScoped, Base):
    """One tier-1 service var of the deterministic port grid: an env-var name and where its port space begins.

    The single home for range starts; `app/port_grid.py` resolves, and the four grid scalars are operator
    knobs. A row makes a var tier 1 (banded, drift-audited); without one it is tier 2 (auto-assigned
    node-side), so deleting a row is always safe. `name` is resolved exact-match, so uniqueness is
    identity; per org because it is written into each slot's `.env`. No `slug`: the var name is the handle.
    """

    __tablename__ = "port_grid_vars"
    __table_args__ = (
        org_unique(__tablename__),
        # The fleet-wide unique still stands beside the per-org one until a later contraction drops it,
        # so two orgs cannot yet both declare `API_PORT`.
        UniqueConstraint("name", name="uq_port_grid_vars_name"),
        UniqueConstraint("org_id", "name", name="uq_port_grid_vars_org_id_name"),
    )

    # NOT unique: one service spelled two ways (API_PORT and BACKEND_PORT) shares a start on purpose.
    # `app/routes/port_grid.py` rejects values outside 1..65535; overlaps and host-daemon collisions
    # are advisory warnings, not refusals (operator decision).
    range_start: Mapped[int] = mapped_column(Integer, nullable=False)


# ── Console cache tables (written by host agent, read by API) ──────────────


# `worktrees.kind` (`ck_worktrees_kind`): how a numbered slot came to exist and what happens when it is freed.
# `static` is held at the assignment's count and survives a release; `dynamic` is created on demand and
# destroyed once freed (`app/slot_allocation.py` owns the rules). Slot 0, the main clone, is always
# `static`.
WORKTREE_KIND_STATIC = "static"


WORKTREE_KIND_DYNAMIC = "dynamic"


WORKTREE_KINDS: tuple[str, ...] = (WORKTREE_KIND_STATIC, WORKTREE_KIND_DYNAMIC)


class Worktree(EntityMixin, OrgScoped, Base):
    """Host worktree state ingested from the node-local KV worktree/runner store.

    An entity: operators declare and delete pool slots. No `slug` (placement is deterministic).
    The wire field `title` is `app/slot_title.py::resolved_title`'s output, not `name`, which cannot be unique
    (`<project>/<number>` repeats across nodes)."""

    __tablename__ = "worktrees"
    __table_args__ = (
        # The natural key and the OCCUPANCY key: every lifecycle edge resolves this triple to `id`
        # through `resolve_slot`. A slot is a directory on ONE node, so `node_id` is part of it.
        UniqueConstraint("node_id", "project_id", "number", name="uq_worktrees_node_project_number"),
        # The vocabulary (`app/slot_state.py::STATES`) in DDL. Keep this constraint's text to the state
        # literals alone: `tests/test_slot_two_writers.py` parses it.
        CheckConstraint("state IN ('free', 'task', 'user')", name="ck_worktrees_state"),
        # The pairing: `free` => both owners NULL; `task` => `task_id` only; `user` => `owner_user_id`
        # only. Both owners at once would let a dispatch and a human believe the same tree is theirs.
        CheckConstraint(
            "(state = 'free' AND task_id IS NULL AND owner_user_id IS NULL)"
            " OR (state = 'task' AND task_id IS NOT NULL AND owner_user_id IS NULL)"
            " OR (state = 'user' AND owner_user_id IS NOT NULL AND task_id IS NULL)",
            name="ck_worktrees_state_ownership",
        ),
        CheckConstraint("kind IN ('static', 'dynamic')", name="ck_worktrees_kind"),
        # No partial index on `state`: the remaining reads address a slot by the unique triple or by
        # `task_id`, and the pool is tens of rows per node. Do not add one back speculatively.
    )

    # CASCADE reaches live holds: `routes/nodes/delete.py::delete_node` snapshots and ENDS every holder in the
    # same transaction first (`app/slot_teardown.py`), so the runner is killed and its session ledger
    # closed rather than orphaned. No server default — no `nodes` row exists to name.
    node_id: Mapped[UUID] = mapped_column(ForeignKey("nodes.id", ondelete="CASCADE"), nullable=False)
    # CASCADE: an ingest cache, so a deleted project takes its cached slots with it.
    project_id: Mapped[int] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    number: Mapped[int] = mapped_column(Integer, nullable=False)
    path: Mapped[str] = mapped_column(Text, nullable=False)
    branch: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # Ownership is `state` + `task_id` + `owner_user_id` and nothing else: no `holder` column, and
    # `idle_hours` stays a read-time derivation (`app/slot_state.py::idle_hours_expr()`). Defended by
    # `tests/test_slot_two_writers.py`.

    # The task that owns this tree; unbound only by a DECISION (`app/slot_state.py::release` is the
    # single home of the edges — no session or runner is among them). Deliberately NO FK: an ingest
    # cache, and a `tasks` row may be deleted while a retired node's slot row lingers. Indexed: read
    # every dispatch pass. In the `OCCUPANCY` family, not `DECIDED`, because it is written per edge.
    task_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True, index=True)
    # `none_as_null=True` on EVERY nullable jsonb column here: without it psycopg adapts `None` into the
    # jsonb VALUE `null`, so `COALESCE(excluded.x, w.x)` returns it and a carry-forward becomes a
    # blanking write. It reads back as `None` either way, which hides it.
    ports: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB(none_as_null=True), nullable=True)
    last_active: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    # Commits this branch is ahead / behind origin/<integration branch> (NULL = unknown).
    ahead: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    behind: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    # Uncommitted work of ANY kind, untracked files included (`apps/jaahd/reconcile/context.py::wt_dirty` is
    # the rule's single home). NULL = probe could not run, never "clean". The invariant "a slot holding
    # uncommitted work never reaches `free`" is enforced ENTIRELY by reading this; a second, narrower
    # boolean would let the safety consumer destroy work (`WorktreePool.tsx::isSyncEligible`).
    dirty: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    session_status: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    session_started_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    # Which process holds this tree, compared against the launch-ack `pid`/`pid_start_time`.
    # `session_proc_started_at` is opaque, compared for equality only; NULL never matches.
    session_pid: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    session_proc_started_at: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # Live session's current model id (transcript tail). NULL when no live session.
    model: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # What the node observed running here, resolved at ingest from the credential-dir path; ingest fails
    # open (NULL). SET NULL, not CASCADE: `slot_state._slot_occupied` fails open, so a cascaded-away row
    # would be a dispatch into an occupied tree.
    account_id: Mapped[Optional[UUID]] = mapped_column(
        ForeignKey("accounts.id", ondelete="SET NULL"), nullable=True
    )
    # What the CP decided this slot would spend (`app/slot_state.py::bind_account`): the sensor above is
    # too late for `routes/agent/task_secrets.py::get_account_api_key` and never arrives for API-key accounts.
    # OCCUPANCY family (`app/slot_columns.py`). SET NULL as above; NULL authorizes nothing.
    leased_account_id: Mapped[Optional[UUID]] = mapped_column(
        ForeignKey("accounts.id", ondelete="SET NULL", name="fk_worktrees_leased_account_id"),
        nullable=True,
    )
    window_name: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # The sticky label is `EntityMixin.name` (survives session end and release; `window_name` is the LIVE
    # tmux name). NOT NULL, so the observation supplies a derived value on first sight — the INSERT-only
    # carve-out in `app/slot_columns.py::MUTATED`.

    # ── Observation delta ──────────────────────────────────────────────────────────────────
    # `observed_at` is the node's own stamp (vs `updated_at`, the CP write). `console` is tri-state —
    # attached | not_attached | probe_unavailable — so a probe failure is NOT `not_attached`. `probes` is
    # ONE jsonb of per-probe ok flags.
    observed_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    merged: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    session: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    # When the current unbroken run of `session = true` began, on the CP's clock (it feeds the
    # `_slot_occupied` sighting veto). Stamped on the rising edge, left alone while it lasts, cleared when
    # `session` goes false/unknown. CP-owned, so not a `probes` key; own writer family.
    session_since: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    session_id: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    console: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    probes: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB(none_as_null=True), nullable=True)

    # ── State-plane delta ─────────────────────────────────────────────────────────────────────────────
    # Written ONLY by the observation upsert (`app/fleet_observe.py`; columns: `app/slot_columns.py::
    # SENSED`). No `ports_observed` column: it maps onto `ports` above, and `urls` derives from it.
    head_sha: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    probed_config_dirs: Mapped[Optional[list[str]]] = mapped_column(
        JSONB(none_as_null=True), nullable=True
    )
    # NODE-COMPUTED AGES, never absolute timestamps: two processes, two clocks, so the node
    # ships an age and only the CP subtracts. Do not "simplify" these back into timestamps.
    age_last_used_secs: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    age_last_commit_secs: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    age_index_mtime_secs: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    # The slot's last transcript write: the CP's receipt time minus the node's `age_transcript_write_secs`. Kept
    # when a pass carries no age (an old node, nothing resolved), so it only moves forward on evidence; read by the
    # stall detector (`task_pipeline_cp.list_silent_active_tasks`).
    transcript_written_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    # Authored by the operator-mutation statement (`POST /api/agent/slot-holder`), not the observation —
    # the declared carve-out in `slot_columns.MUTATED`.
    last_used: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    tmux_window_id: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    tmux_target: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    compose_project_name: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    stack_present: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    stack_up: Mapped[Optional[bool]] = mapped_column(Boolean, nullable=True)
    default_title: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    host_env: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB(none_as_null=True), nullable=True)
    # The node-sensed slot manifest (`fleet_observe.ObservedSlotRepos`): the slot's repos, kept and failed. NULL = a
    # single-repo slot.
    repos: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB(none_as_null=True), nullable=True)

    # ── Occupancy ────────────────────────────────────────────────────────────────────────────────────
    # Every column below (and `task_id` above) is DECIDED, never sensed (`app/slot_columns.py::
    # OCCUPANCY`): `app/slot_state.py`'s lifecycle statements are the sole writer, disjoint by
    # construction from the observation upsert and the operator-mutation statement.

    # `free` | `task` (`task_id`) | `user` (`owner_user_id`). Occupancy, not liveness: ownership ends
    # only on a recorded decision (task terminal/deleted/reset, or `routes/pool_slots.py::release_slot`);
    # a dead runner is restarted in the same worktree. Edges: `app/slot_state.py::release`.
    state: Mapped[str] = mapped_column(Text, nullable=False, server_default="free")
    # The human who owns this tree; invisible to dispatch and sweeps. SET NULL, and the pairing CHECK
    # then invalidates the row rather than freeing it, so deleting the user cannot hand their tree to a
    # dispatch. No index: tens of rows per node.
    owner_user_id: Mapped[Optional[UUID]] = mapped_column(
        PG_UUID(as_uuid=True), ForeignKey("admin_users.id", ondelete="SET NULL"), nullable=True
    )
    # Fencing token: bumped by every lease and redispatch, carried out on the intent and back on the
    # ack, so a late ack from a superseded dispatch cannot resurrect itself.
    generation: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    # The same fence one step EARLIER, before the spawn.
    launch_claimed_generation: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    # Correlates intent -> ack.
    dispatch_id: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # Read against `acked_at` to recognise an un-acked dispatch for RE-DISPATCH into this same slot.
    # Not a release clock — no age on this row frees anything.
    dispatched_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    # Launch confirmed.
    acked_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    # Launch identity from the node's ack — the CP's own claim, vs `session_pid`/`session_proc_started_at`,
    # which is what the next observation actually finds. Keep both pairs.
    pid: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    # Compared for EQUALITY only, never subtracted; NULL is UNKNOWN, never a match.
    pid_start_time: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # DECIDED (reported on the launch ack); must not share a column with the SENSED `session_id` above —
    # the two-writer contract asserts the families are disjoint.
    launch_session_id: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # Why the last transition happened; operator-visible.
    reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # When the current `state` began, written only by `app/slot_state.py`'s lifecycle edges. Not
    # `updated_at`, which the trigger advances on every ~5s observation.
    state_since: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    # `WORKTREE_KINDS`; the `ALLOCATION` family (`app/slot_columns.py`), written only by
    # `app/slot_allocation.py`. A held static surplus is demoted to `dynamic` rather than destroyed (D12).
    kind: Mapped[str] = mapped_column(
        Text, nullable=False, default=WORKTREE_KIND_STATIC, server_default=WORKTREE_KIND_STATIC
    )
    # Whether this slot is a git worktree (true) or an empty folder (false) — its project's `has_repo` AT
    # DECLARE (`slot_allocation._add_row`), never updated: a held slot keeps the kind its node built after
    # the project's repository flips. The `SLOT_KIND` family (`app/slot_columns.py`). Not a third `kind` value:
    # a folder slot is always `dynamic`.
    has_repo: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True, server_default="true")
    # When the CP ordered this slot destroyed (`slot_allocation.retire` minted its `destroy_slot` intent).
    # The row stays, its index occupied, until the node acks the destroy; then the row is deleted.
    retiring_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    # The node's refusal of that destroy, in its own words. Non-NULL means the index is not re-minted and the
    # console shows "Destroy failed — <detail>" until an operator acts.
    destroy_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # When a live session was last sighted: a liveness clock, never ownership. Read as
    # `COALESCE(last_seen_live, state_since)` by `app/task_pipeline_job.py` before reclaiming the work
    # (not the slot); surfaced via `routes/portfolio.py`.
    last_seen_live: Mapped[Optional[datetime]] = timestamp_column(nullable=True)


class SlotIntent(OrgScoped, Base):
    """One CP-decided action on one pool slot, for the node to execute and ack.

    Verbs are single-homed in `app/fleet_verdicts.py::SLOT_INTENT_VERBS` (runner kills included: the CP
    decides them).

    Two-part staleness guard: `decided_on_observed_at` is the observation stamp decided on, and
    `preconditions` is what the node re-verifies at execution (the node is blind between ticks, so stamp
    equality alone is insufficient).

    Retirement is outcome-gated (`fleet_verdicts.SLOT_INTENT_RETIRING_OUTCOMES`); re-decidable outcomes
    stamp `acked_at` but stay pending. `unsupported_verb` is terminal so an older node cannot refuse
    forever. A pending row is not indefinitely executable: `refusals`/`refusal_key` count
    identical refusals, `next_attempt_at` backs off, and repeated refusals retire it `parked`
    (`fleet_verdicts.REFUSAL_PARK_AFTER`) with the Task put before a human; supersession
    (`fleet_verdicts.issue_intents`) keeps one pending row per slot. The UNIQUE tuple makes
    issuing idempotent."""

    __tablename__ = "slot_intents"
    __table_args__ = (
        UniqueConstraint(
            "node_id", "project_id", "number", "verb", "decided_on_version",
            name="uq_slot_intents_slot_verb_version",
        ),
        # The successor to the tuple above (keyed on the node-authored, monotone `observed_at`); both
        # coexist until `decided_on_version` is dropped.
        UniqueConstraint(
            "node_id", "project_id", "number", "verb", "decided_on_observed_at",
            name="uq_slot_intents_slot_verb_observed_at",
        ),
        # Partial index for the pool-tick drain. Its predicate (`done` only) is a deliberate superset of
        # the drain's filter (`SLOT_INTENT_RETIRING_OUTCOMES`), so it still covers the query.
        Index(
            "ix_slot_intents_pending", "node_id", "created_at",
            postgresql_where=text("outcome IS DISTINCT FROM 'done'"),
        ),
        # A task's pane writes for its page's message list (`routes/task_control_messages.py`): delivered rows are
        # kept, so without it every read scans the fleet's whole intent history.
        Index(
            "ix_slot_intents_pane_writes_task", text("(payload ->> 'task_id')"), "created_at",
            postgresql_where=text("verb IN ('deliver_message', 'deliver_answer')"),
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    # CASCADE: a queued, re-derivable decision dies with its node.
    node_id: Mapped[UUID] = mapped_column(ForeignKey("nodes.id", ondelete="CASCADE"), nullable=False)
    # An FK, not the operator-editable project name, so a rename cannot strand an intent. CASCADE.
    project_id: Mapped[int] = mapped_column(
        ForeignKey("projects.id", ondelete="CASCADE"), nullable=False
    )
    number: Mapped[int] = mapped_column(Integer, nullable=False)
    verb: Mapped[str] = mapped_column(Text, nullable=False)
    decided_on_version: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    # Written by `fleet_verdicts.sight_then_reap` (from `POST /api/agent/fleet/observe`, via
    # `build_reap_release_intent`), read by `jaahd/reconcile/execute.py::observation_is_current`. Deciding the reap
    # on the observation POST uses the freshest stamp, so a 60s decision cannot be `refused_stale` by a
    # ~5s observation.
    decided_on_observed_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    preconditions: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB, nullable=True)
    # The decisions the node receives (account dir, port band, branch/base, generation); not
    # `preconditions`, which the node may refuse on.
    payload: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB, nullable=True)
    reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = timestamp_column()
    acked_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    outcome: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # ── The retry bound: how many times this row has refused identically, and when it may run again ──
    # Counted, not timed: a changing condition gives a different reason and a fresh budget; a repeating
    # one is wedged. `refusal_key` is `fleet_verdicts.refusal_key`'s digest of (outcome, reason); a
    # different key resets the counter.
    refusals: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    refusal_key: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # Withheld-until: at a 5s tick a refusing row would otherwise be re-served ~720 times an hour at full
    # enactment cost. NULL means due now (no sentinel date).
    next_attempt_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)


# ── Multi-Node Hosts (0267) — rows in the host's org ──────────────────────────────────────────────────
# A co-located node is an ordinary `nodes` row in its own org; the classes below hold everything that
# makes it co-located, in the HOST's org — org zero for an operator host, so the read seam withholds
# them from tenants. Rules: `app/vnodes.py`; shape: migration 0267.

#: The converge states a vnode's row moves through. `provisioning`/`failed` admit an enrolment read,
#: `deleting` is set by the tenant node's delete and ends when the host reports `destroyed`.
VNODE_STATES = ("provisioning", "ready", "failed", "deleting")


class VnodeHost(OrgScoped, Base):
    """The marker that makes a `nodes` row a Multi-Node HOST — a machine that runs no work itself and
    carries co-located nodes. In org zero (an operator host, any org's nodes) or a tenant's org (that
    org's nodes only); cloud or self-hosted, an ordinary node in every other respect."""

    __tablename__ = "vnode_hosts"
    __table_args__ = (
        CheckConstraint(
            "shared_capacity IS NULL OR shared_capacity BETWEEN 1 AND 99", name="ck_vnode_hosts_shared_capacity"
        ),
    )

    node_id: Mapped[UUID] = mapped_column(
        ForeignKey("nodes.id", ondelete="RESTRICT", name="fk_vnode_hosts_node_id"), primary_key=True
    )
    # Operator-only: a host-level fault.
    failure_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # The host's DISPLAY name N (0352): its own `nodes.name` is the machine handle `N-host`, so the host's machine
    # and its first node (also N) stay apart on the tailnet (`app/hosts.py`). NULL for a host made before 0352,
    # whose display name is its node name. Not unique: a label.
    name: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # The Shared hosting pool (0356, `app/hosting_provisioning.py`): NULL = not offered; N = at most N live tenant
    # nodes placed here by Shared hostings. Operator (org-zero) hosts only (`routes/vnode_hosts.py`).
    shared_capacity: Mapped[Optional[int]] = mapped_column(SmallInteger, nullable=True)
    # Slot counter (slots are never reused), bumped under this row's lock by
    # `app/vnodes.py::allocate_slot`.
    last_slot: Mapped[int] = mapped_column(
        SmallInteger, nullable=False, default=0, server_default="0"
    )
    # Every slot this host ever handed out (0358): `allocate_slot` picks a random slot outside it and appends,
    # under this row's lock, so a slot is never reused. `last_slot` is kept at least the highest pick, for the
    # release that still counts sequentially.
    used_slots: Mapped[list[int]] = mapped_column(
        ARRAY(SmallInteger), nullable=False, default=list, server_default=text("'{}'::smallint[]")
    )
    # The host's helper builds a vnode's tree from its tag (0358): set on its first desired poll carrying
    # `vnode_tags=1`, never cleared. Until then `allocate_slot` issues no tag, so a helper that ignores one still
    # builds the slot-derived paths the control plane records.
    supports_tags: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default=text("false")
    )
    # The host's own root disk as it last reported it (0425, `POST /api/agent/vnode-host/usage` `root`): its
    # size, filesystem, the last `host.grow_root` failure (NULL after a success) and its boot time — so a
    # storage change can tell grown from stuck, and a reboot from none. All NULL until a host reports.
    root_total_kb: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    root_fs_type: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    root_grow_error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    root_reported_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    booted_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    # When the host's converge loop last asked for its nodes (`GET /api/agent/vnode-host/desired`, at most once
    # per `vnodes.POLL_STAMP_EVERY`). A pass blocked in one vnode's setup asks nothing, so a stale stamp beside a
    # live heartbeat is a stuck converge. NULL until the first poll.
    desired_polled_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    created_at: Mapped[datetime] = timestamp_column()
    created_by_id: Mapped[UUID] = mapped_column(
        ForeignKey("admin_users.id", ondelete="RESTRICT", name="fk_vnode_hosts_created_by_id"),
        nullable=False,
        server_default=text(f"'{SYSTEM_ACTOR_ID}'::uuid"),
    )


class VirtualNode(UuidPkMixin, OrgScoped, Base):
    """One vnode: which host carries it, which tenant `nodes` row it IS, and where its converge stands.

    `node_id` NULL means PENDING TEARDOWN — the tenant node's delete set `state='deleting'` and the FK's
    `SET NULL` cleared the link in the same transaction; the host converges the teardown and reports
    `destroyed`, which deletes this row. `org_id` is the HOST's org — for an operator host (zero), which
    keeps the row out of every tenant's reads; `tenant_org_id` is the org the node belongs to, fixed at
    create (equal to `org_id` on a tenant host)."""

    __tablename__ = "virtual_nodes"
    __table_args__ = (
        org_unique(__tablename__),
        UniqueConstraint("node_id", name="uq_virtual_nodes_node_id"),
        UniqueConstraint("host_node_id", "slot", name="uq_virtual_nodes_host_slot"),
        CheckConstraint("slot BETWEEN 1 AND 99", name="ck_virtual_nodes_slot"),
        CheckConstraint("tag ~ '^[a-z][a-z0-9]{7}$'", name="ck_virtual_nodes_tag"),
        Index(
            "uq_virtual_nodes_host_tag", "host_node_id", "tag", unique=True,
            postgresql_where=text("tag IS NOT NULL"),
        ),
        CheckConstraint(
            "state IN (" + ", ".join(f"'{state}'" for state in VNODE_STATES) + ")",
            name="ck_virtual_nodes_state",
        ),
        Index("ix_virtual_nodes_tenant_org_id", "tenant_org_id"),
    )

    host_node_id: Mapped[UUID] = mapped_column(
        ForeignKey(
            "vnode_hosts.node_id", ondelete="RESTRICT", name="fk_virtual_nodes_host_node_id"
        ),
        nullable=False,
    )
    node_id: Mapped[Optional[UUID]] = mapped_column(
        ForeignKey("nodes.id", ondelete="SET NULL", name="fk_virtual_nodes_node_id"), nullable=True
    )
    # The tenant node's id once `node_id` has been nulled — a copy with no FK (migration 0267).
    former_node_id: Mapped[Optional[UUID]] = mapped_column(PG_UUID(as_uuid=True), nullable=True)
    # No FK (revision 202610130300): a teardown still pending on another org's host outlives a deleted org.
    tenant_org_id: Mapped[UUID] = mapped_column(PG_UUID(as_uuid=True), nullable=False)
    slot: Mapped[int] = mapped_column(SmallInteger, nullable=False)
    # The random handle every name, path and unit of this vnode is derived from on its host (0358,
    # `app/vnodes.py::TAG_PATTERN`). NULL for a vnode made before it, whose names derive from `slot`
    # (`vnodes.token_of`).
    tag: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    state: Mapped[str] = mapped_column(Text, nullable=False)
    failure_reason: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    tailnet_device_id: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # UNWRITTEN and unread: the host mounts the org's LIVE store (`vnodes.shared_config`). Dropped in a later
    # release (additive while two releases overlap).
    access_point_id: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    shared_efs_id: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    shared_efs_region: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    shared_efs_mount_ip: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    # The operator's per-node caps (0358); NULL means the node's Shared tier, else the System setting
    # (`vnodes.effective_caps`).
    disk_cap_gb: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    inode_cap: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    # The host's last measurement of this vnode's tree (0358); NULL until it first reports one
    # (`routes/agent_vnode_host.py::post_usage`).
    disk_used_kb: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    inodes_used: Mapped[Optional[int]] = mapped_column(BigInteger, nullable=True)
    usage_measured_at: Mapped[Optional[datetime]] = timestamp_column(nullable=True)
    created_at: Mapped[datetime] = timestamp_column()
    updated_at: Mapped[datetime] = timestamp_column()
    created_by_id: Mapped[UUID] = mapped_column(
        ForeignKey("admin_users.id", ondelete="RESTRICT", name="fk_virtual_nodes_created_by_id"),
        nullable=False,
        server_default=text(f"'{SYSTEM_ACTOR_ID}'::uuid"),
    )
    updated_by_id: Mapped[UUID] = mapped_column(
        ForeignKey("admin_users.id", ondelete="RESTRICT", name="fk_virtual_nodes_updated_by_id"),
        nullable=False,
        server_default=text(f"'{SYSTEM_ACTOR_ID}'::uuid"),
    )


class VnodeEnrolment(OrgScoped, Base):
    """The SEALED one-time envelope a host reads to enrol one vnode — never a column of `virtual_nodes`,
    so neither secret, sealed or not, sits on that row. Deleted on the read that consumes it."""

    __tablename__ = "vnode_enrolments"

    virtual_node_id: Mapped[UUID] = mapped_column(
        ForeignKey(
            "virtual_nodes.id", ondelete="CASCADE", name="fk_vnode_enrolments_virtual_node_id"
        ),
        primary_key=True,
    )
    sealed_node_token: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    # NULL = no tailnet is configured, which is a fact and not a secret — never sealed.
    sealed_tailnet_key: Mapped[Optional[dict[str, Any]]] = mapped_column(JSONB, nullable=True)
    cp_base_url: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = timestamp_column()
