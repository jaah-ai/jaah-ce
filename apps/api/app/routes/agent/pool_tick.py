"""The node's pool tick: slot roster, known slots, pending intents and port grid (`GET /api/agent/pool-tick`)."""
from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any, Optional, Sequence
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field
from sqlalchemy import false, func, or_, select
from sqlalchemy.orm import Session

from app import (
    carryover,
    fleet_decide,
    fleet_verdicts,
    edition_features,
    forges,
    node_config,
    node_credentials,
    port_grid,
    project_identity,
    project_repos,
    slot_allocation,
)
from app.operator_settings import reads as settings_reads
# routes need one for a user who is NOT the caller (they carry no session cookie, only the node token).
from app.authz.catalog import Tenancy
from app.authz.declarations import infra
from app.db import get_session
from app.node_clone import resolve_auth_dir, resolve_clone
from app.node_identity import node_id_or_none, node_matches, node_org, node_ref
from app.models.agent_sessions import FetchStamp
from app.models.credentials import Account
from app.models.nodes import Node
from app.models.projects import Project
from app.models.worktrees import SlotIntent, Worktree
from app.entity_shape import iso_or_none
from app.routes.agent.auth import _check_token

router = APIRouter(prefix="/api/agent", tags=["agent"])
log = logging.getLogger("agent-routes")


# ── Sensor-only reconcile: the per-tick roster + intent read ──────
# Roster and intents ride THIS response; VERDICTS do not — `stack_state`/`urls` are derived at read time
# (`app/slot_stack.py`) and served by `GET /api/agent/worktrees`. The node never transports a verdict.

# The intent vocabulary single-homes in `app/fleet_verdicts.py`; this module is only the wire.
SLOT_INTENT_VERBS = fleet_verdicts.SLOT_INTENT_VERBS


# Three outcomes retire an intent. `accepted` must retire a `lease_slot` even though the work is only
# beginning: re-served next tick it is a SECOND launch into a slot holding a live session.
# `unsupported_verb` retires an intent nothing happened for. Everything else stays PENDING.
SLOT_INTENT_OUTCOMES = fleet_verdicts.SLOT_INTENT_OUTCOMES


SLOT_INTENT_RETIRING_OUTCOMES = fleet_verdicts.SLOT_INTENT_RETIRING_OUTCOMES


NODE_AUTHORED_OUTCOMES = fleet_verdicts.NODE_AUTHORED_OUTCOMES


class PoolTickSlot(BaseModel):
    """One sweepable slot. The swept set comes from here, not a `0..N` range; `is_main` is a
    roster attribute, not a `number == 0` test.

    `config_dir` is checked by `fleet_decide.liveness_observed` against `probed_config_dirs`: a slot
    outside probe coverage has unobserved liveness and is never reclassified. `state` is the one occupancy
    field (`slot_state.lease` refuses slot 0).
    """

    number: int
    path: str
    is_main: bool
    config_dir: Optional[str] = None
    state: str = "free"
    # The port grid's band `{VAR: port}` (`fleet_decide.port_band`; parsed by
    # `reconcile.tick_wire.port_band_from_wire`), the node's only grid-port source: `apply_port_band` refuses an absent
    # band. A declared var the band omits is tier 2.
    ports: dict[str, int] = {}
    # Raw timestamp rather than an age: authored by the mutation verbs, not sensed, and the CP never
    # receives a node timestamp to subtract.
    last_used: Optional[str] = None
    # The staleness token the node compares an intent's `decided_on_observed_at` against.
    observed_at: Optional[str] = None
    # `worktrees.kind` and `retiring_at IS NOT NULL`: a retiring row stays rostered so the node can
    # resolve the `destroy_slot` intent addressed to it (`reconcile.enact_signal._enact_destroy_slot`).
    kind: str = "static"
    retiring: bool = False


class PoolTickRepo(BaseModel):
    """One of a git project's repositories, in display order. `id` is `project_repos.id`, the key the
    node's slot manifest and its `repos` observation echo; `base` is that repo's own integration branch."""

    id: int
    clone_url: str
    repo_path: str
    forge: str
    base: str
    primary: bool


class PoolTickRosterEntry(BaseModel):
    """One enrolled project's slots plus the configuration the node does not discover — the roster is the
    one configuration source.

    Every entry is usable: a project missing `clone_url`, `base` or `clone` is omitted whole, except a `draining`
    one, which has no `clone_url`. The node reads
    `clone_url`/`repo_path` (`app/forges.py::RepoRef`). `port_block`/`port_vars`/`range_start_by_var` are this
    project's slice of the grid (`app/port_grid.py`); global scalars ride on `PoolTickOut.grid`.
    """

    # `projects.id`, what `build_pool_roster` keys on: a roster keyed on the display name turns a rename
    # into a zeroed pool. `project` stays the display name the node uses for paths, tmux windows, logs.
    project_id: int
    project: str
    clone: str
    base: str
    # The credential-free https remote, the forge, and the forge-native repo path.
    clone_url: str
    forge: str
    repo_path: str
    # `branch_prefix`, stored WITH its trailing slash (`sn/`) or `""`. Every ref the node creates or
    # pushes is `{branch_prefix}fleet/task-<id>` and the push guard is keyed on the same prefix, so a ref
    # rebuilt without it lands on a branch the node cannot push.
    branch_prefix: str = ""
    port_block: Optional[int] = None
    port_vars: list[str] = []
    range_start_by_var: dict[str, int] = {}
    slots: list[PoolTickSlot] = []
    # Seconds since this node last fetched this project's base (write half: the observation POST's
    # `fetched_projects`). Per PROJECT, not per slot. None = never fetched, read as "fetch now".
    fetch_stamp_age_secs: Optional[int] = None
    # False: a project with no repository, whose slots are empty folders; the node then needs only
    # `clone`, and every git string is `""`. Defaulted true, so an entry that does not set it is a git
    # project — the node reads an absent key the same way.
    has_repo: bool = True
    # True: a git pair whose project's last repository was removed. Its git worktrees are being
    # destroyed (`slot_allocation._converge_assigned`), and with no repository there is no remote: `clone_url`,
    # `forge` and `repo_path` are `""`, so the node serves only their teardown.
    draining: bool = False
    # Every repository of a git project, primary flagged; the top-level git fields stay the primary's. `[]` for a
    # folder project or one with no row — the node then converges the single repo above, as before.
    repos: list[PoolTickRepo] = []
    # `projects.stack_down_cmd`, so a node can stop the old primary's stack when the primary changes. `""` for a
    # folder project.
    stack_down_cmd: str = ""
    # `projects.push_enabled`: whether the node may push the integration branch it finds missing on origin
    # (`apps/jaahd/worktree_mechanics/core.py::_create_missing_base`). False for a folder project; absent reads False.
    push_enabled: bool = False


class PoolTickIntent(BaseModel):
    """One pending slot intent for this node. `decided_on_observed_at` is the observation stamp the CP decided
    on; `preconditions` is what the node re-verifies before acting. Both required: the node is blind between
    ticks. `decided_on_version` is never populated.
    """

    id: int
    # The name is not redundant: the intent is enacted against a DIRECTORY and a tmux window named from it.
    project_id: int
    project: str
    number: int
    verb: str
    decided_on_version: Optional[int] = None
    # The SQL-native staleness token, compared against `PoolTickSlot.observed_at`.
    decided_on_observed_at: Optional[str] = None
    preconditions: dict[str, Any] = {}
    # The CP's DECISION — account dir, port band, branch/base, task, plus the `generation` + `dispatch_id`
    # the ack must echo. `preconditions` is what the node may REFUSE on; this is what it must APPLY
    # (`app/fleet_verdicts.py::build_slot_intent`).
    payload: dict[str, Any] = {}
    # Bounded: `reason` is relayed into the operator's channel, and Slack (>40_000) RAISES in
    # `alert_relay` — a withdrawn dedup row and an alert no human sees. An alert-suppression lever.
    reason: Optional[str] = Field(default=None, max_length=8_000)
    created_at: Optional[str] = None


class PoolTickKnownSlot(BaseModel):
    """One slot this node has a `worktrees` row for, unfiltered by the roster's usability gate.

    `build_pool_roster` omits unresolvable projects; reporting only roster slots would let the complement
    DELETE remove those rows irreversibly. Node contract: report a `state` for every known and roster slot;
    a known slot outside the roster, absent with a readable container → `gone`, else `unobserved`, never
    `present`. Every field here can only protect a row.
    """

    # Neither half can license a deletion.
    project_id: int
    project: str
    number: int
    path: str
    observed_at: Optional[str] = None


class PoolTickGrid(BaseModel):
    """The port grid, whole and fleet-global — the shape `GET /api/agent/port-grid` serves.

    Rides the tick for `auto_port_floor`/`auto_port_ceiling` (the tier-2 range,
    `worktree_mechanics.ports.apply_auto_ports`). `range_start_by_var` is unfiltered.
    """

    range_start_by_var: dict[str, int] = {}
    block_size: int
    stride: int
    window: int
    auto_port_floor: int
    auto_port_ceiling: int


class PoolTickOut(BaseModel):
    node_id: str
    roster: list[PoolTickRosterEntry]
    intents: list[PoolTickIntent]
    # OPTIONAL, unlike `known_slots`: absent means "this CP did not say", and the node keeps its own
    # documented tier-2 defaults — a knob default is safe to read where inventing a range START is not.
    grid: Optional[PoolTickGrid] = None
    # Required: a `[]` default would widen the complement DELETE. `reconcile.tick_wire.parse_pool_tick` treats absence
    # as `ok=False`, so an older API freezes rather than prunes.
    known_slots: list[PoolTickKnownSlot]
    # The "Infrastructure" > "Daemon" knobs (`operator_settings.catalog.NODE_SETTING_KEYS`), re-read per request; the node
    # keeps the last values it received (`apps/jaahd/node_settings.py`). Absent (the empty tick, an older
    # API) means the node keeps what it has.
    node_settings: Optional[dict[str, int]] = None
    # This node's org's managed-settings consent hash (`node_config.org_consent_hash`), per request, so
    # EVERY node — self-hosted, a Multi-Node Host's vnodes — gets what a cloud node is only seeded with at
    # launch, and a managed-settings change reaches it next tick. `None` (none learned for this org, an
    # older API) changes nothing
    # on the node (`apps/jaahd/plugin_repair.py::take_consent_hash_from_tick`).
    remote_settings_consent_hash: Optional[str] = None
    # The named node's MicroVM mark (`nodes.microvm`, `app/microvm_mode.py`): the node runs each task in its own
    # VM while it is true. Absent (an older API, the empty tick) means false.
    microvm: bool = False
    # The MicroVM rail's pending orders for the proved node, oldest first, each in its pushed frame's shape
    # (`app/microvm_rail.py`). `[]` and absent both mean no orders.
    microvm_intents: list[dict[str, Any]] = []


def node_slot_rows(session: Session, node_id: str) -> Sequence[Any]:
    """One read of every `worktrees` row for this node, the union of the columns `pool-tick`'s two builders
    need.

    Invariant: FROM is the single `worktrees` table, the name is a correlated subquery and the only join is
    LEFT, so no `projects`/`accounts` shape can remove a row (see `PoolTickKnownSlot`).
    """
    project_name = (
        project_identity.name_expr().where(Project.id == Worktree.project_id).scalar_subquery()
    )
    return session.execute(
        select(
            Worktree.project_id,
            project_name.label("project_name"),
            Worktree.number,
            Worktree.path,
            Account.slug.label("account_slug"),
            Worktree.last_used,
            Worktree.observed_at,
            Worktree.state,
            Worktree.kind,
            Worktree.retiring_at,
            Worktree.has_repo,
        )
        .outerjoin(Account, Account.id == Worktree.account_id)
        # `node_ref` resolves the wire value in-statement: an unknown name yields NULL and matches nothing
        # (the callers' "roster unavailable" freeze). A Python `None` would render `IS NULL` and serve
        # one node every other node's slots.
        .where(Worktree.node_id == node_ref(node_id))
        .order_by(Worktree.project_id, Worktree.number)
    ).all()


def build_pool_roster(
    session: Session,
    node_id: str,
    grid: Optional[port_grid.Grid] = None,
    rows: Optional[Sequence[Any]] = None,
    org: Optional[UUID] = None,
    node_row: Optional[Any] = None,
) -> list[PoolTickRosterEntry]:
    """The one configuration source, shared by the pool-tick wire and `app/fleet_verdicts.py` so both agree
    on which slots exist and are usable.

    An empty list is an answer only beside `known_slots` (`reconcile.tick_wire.parse_pool_tick`). Every per-project
    map is keyed on `project.id`, never the name (a rename would zero the pool). `rows` are the caller's
    `node_slot_rows`; omitted, read here. `node_row` is the caller's read of this node's `jaah_home`
    (`_tick_node_row`); omitted, read here.
    """
    # Slots come from the DB projection of the node's KV ledger — "which slots exist", observed, not a
    # max-derived range. Keyed on `worktrees.project_id`.
    slots_by_project: dict[int, list[PoolTickSlot]] = {}
    # Each project's numbered rows' kinds (`Worktree.has_repo`), for `slot_allocation.kind_of_pair`. A caller's
    # rows predating the column read as git worktrees.
    kinds_by_project: dict[int, list[bool]] = {}
    # The wire field is a PATH: the node's `reconcile.accounts_probe._agent_config_dirs()` builds `probed_config_dirs`
    # from it. Derived for THIS node from the slug; `""` maps to `None`.
    slot_home = (
        session.scalar(select(Node.jaah_home).where(node_matches(node_id))) if node_row is None
        else node_row.jaah_home
    )
    for row in rows if rows is not None else node_slot_rows(session, node_id):
        slots_by_project.setdefault(row.project_id, []).append(
            PoolTickSlot(
                number=row.number, path=row.path, is_main=row.number == 0,
                config_dir=resolve_auth_dir(slot_home, row.account_slug) or None, state=row.state,
                last_used=iso_or_none(row.last_used),
                observed_at=iso_or_none(row.observed_at),
                kind=row.kind, retiring=row.retiring_at is not None,
            )
        )
        if row.number >= 1:
            kinds_by_project.setdefault(row.project_id, []).append(getattr(row, "has_repo", True))

    # Per-project fetch-stamp ages, one read. Keyed on id: a missed lookup reads as "never fetched" —
    # safe, but a per-tick `git fetch` on every slot forever.
    fetch_ages: dict[int, int] = {
        row.project_id: int(row.age_secs)
        for row in session.execute(
            select(
                FetchStamp.project_id,
                func.floor(
                    func.extract("epoch", func.now() - FetchStamp.fetched_at)
                ).label("age_secs"),
            ).where(FetchStamp.node_id == node_ref(node_id))
        ).all()
    }

    roster: list[PoolTickRosterEntry] = []
    # THIS node's clone layout, so every entry names a directory that exists ON IT. `slot_home` above is
    # the same value: one read, deliberately not two (this was the CP's highest-volume query).
    jaah_home = slot_home
    # One grid for the whole roster, passed in so roster and `grid` block are one snapshot. `org` feeds only
    # the fallback arm and must be this node's (the grid is per org). No grid and no org → unstamped rows.
    org = org if org is not None else node_org(session, node_id)
    grid = grid or port_grid.grid(session, org)
    projects = session.scalars(
        select(Project)
        .where(Project.org_id == org)
        .order_by(Project.name)
    ).all()
    # Every project's repositories in one read, not one per project.
    repo_rows = project_repos.repos_by_project(session, [project.id for project in projects])
    # A node is rostered its own org's projects only: `port_block` is unique per org, so cross-org rosters
    # would band two block-0 projects onto one host and disclose tenants' projects. Not yet enforced for
    # `worktrees` (the unkeyed-reference register); `apply_port_band` keeps it fail closed. An org-less
    # node gets an empty roster (unreachable: registrations are stamped).
    for project in projects:
        ref = forges.repo_ref_or_none(project)
        clone = resolve_clone(jaah_home, project.id)
        # The node reads ONE kind per entry, so a pair still holding slots of the other kind keeps their kind
        # until they are gone (`slot_allocation.kind_of_pair`); the planner makes none of the new kind meanwhile.
        if not slot_allocation.kind_of_pair(project.has_repo, kinds_by_project.get(project.id, [])):
            if not clone:
                log.warning("pool-tick: project %s has no clone path on node %s — omitting its slots",
                            project.name, node_id)
                continue
            # A folder project: the node needs only `clone` to name each slot's directory. Every git
            # string is `""`, and no port band — a folder runs no stack.
            slots = slots_by_project.get(project.id, [])
            for slot in slots:
                slot.ports = {}
            roster.append(PoolTickRosterEntry(
                project_id=project.id, project=project.name, clone=clone, base="", clone_url="",
                forge="", repo_path="", branch_prefix="",
                port_block=project.port_block, port_vars=[], range_start_by_var={}, slots=slots,
                fetch_stamp_age_secs=fetch_ages.get(project.id), has_repo=False,
            ))
            continue
        # A git pair of a project with no repository left: rostered only so its worktrees can be destroyed.
        draining = ref is None and not project.has_repo
        if not clone or (ref is None and not draining):
            log.warning(
                "pool-tick: project %s is unusable on node %s (clone=%r clone_url=%r) — omitting its slots",
                project.name, node_id, clone, ref.clone_url() if ref is not None else None,
            )
            continue
        # The base is the column. An empty `integration_branch` omits the project WHOLE, like a missing
        # `clone`/`clone_url`, rather than silently becoming `develop`.
        base = (project.integration_branch or "").strip()
        if not base:
            log.warning(
                "pool-tick: project %s has no integration_branch set — omitting its slots (fail-closed; "
                "the branch runners rebase onto and PR into is never derived)",
                project.name,
            )
            continue
        port_vars = list(project.port_vars or [])
        # Only the vars this project declares — a start for an undeclared var would band a port nobody
        # promised. Hoisted because the per-slot band uses the same starts and the two must not disagree.
        range_start_by_var = {
            var: grid.range_start_by_var[var]
            for var in port_vars
            if var in grid.range_start_by_var
        }
        # BY ID — a name lookup here is how a rename ships an empty slot list.
        slots = slots_by_project.get(project.id, [])
        # Stamp the band here: the slot rows come from a `worktrees` query with no project configuration
        # in scope. `PoolTickSlot.ports` covers why an absent band is a node-side refusal.
        for slot in slots:
            slot.ports = fleet_decide.port_band(
                project.port_block, slot.number, port_vars,
                range_start_by_var=range_start_by_var,
                block_size=grid.block_size,
                stride=grid.stride,
            )
        roster.append(
            PoolTickRosterEntry(
                project_id=project.id,
                project=project.name,
                clone=clone,
                base=base,
                clone_url=ref.clone_url() if ref is not None else "",
                forge=ref.forge if ref is not None else "",
                repo_path=ref.path if ref is not None else "",
                branch_prefix=project.branch_prefix or "",
                port_block=project.port_block,
                port_vars=port_vars,
                range_start_by_var=range_start_by_var,
                slots=slots,
                fetch_stamp_age_secs=fetch_ages.get(project.id),
                repos=_roster_repos(project, repo_rows.get(project.id, [])),
                stack_down_cmd=project.stack_down_cmd or "",
                push_enabled=bool(project.push_enabled),
                draining=draining,
            )
        )
    return roster


def _roster_repos(project: Project, rows: Sequence[Any]) -> list[PoolTickRepo]:
    """`project`'s rows as wire entries. A row whose URL does not parse is skipped with a warning: the node
    cannot clone it, and refusing the whole entry would stop every other repo's slot too."""
    out: list[PoolTickRepo] = []
    for row in rows:
        try:
            ref = forges.parse_repo_url(row.repo_url, row.forge, row.forge_base_url)
        except forges.ForgeUrlError:
            log.warning("pool-tick: project %s repo %s has an unparseable URL — omitting it", project.name, row.id)
            continue
        out.append(PoolTickRepo(
            id=row.id, clone_url=ref.clone_url(), repo_path=ref.path, forge=ref.forge,
            base=row.integration_branch, primary=row.is_primary,
        ))
    return out


def build_known_slots(
    session: Session, node_id: str, rows: Optional[Sequence[Any]] = None
) -> list[PoolTickKnownSlot]:
    """Every `worktrees` row for this node, unfiltered — see `PoolTickKnownSlot` for the hazard.

    No `Project` join or pool/branch filters: none is evidence the directory is gone. An empty result is
    legitimate, never a licence to widen the DELETE.
    """
    rows = rows if rows is not None else node_slot_rows(session, node_id)
    # A carried-over slot's project moved out of this node's org (`app/carryover.py`), so the name read above is
    # NULL for it: named by its carry-over, else by id, rather than failing the whole tick.
    carried = (
        carryover.carried_project_ids(session, node_id)
        if any(row.project_name is None for row in rows) else {}
    )
    return [
        PoolTickKnownSlot(
            project_id=row.project_id,
            project=row.project_name or carried.get(row.project_id) or f"project#{row.project_id}",
            number=row.number, path=row.path, observed_at=iso_or_none(row.observed_at),
        )
        for row in rows
    ]


@router.get("/pool-tick", response_model=PoolTickOut)
@infra(Tenancy.ORG_SCOPED)
def pool_tick(
    # Bounded: a mismatch writes a WARNING carrying this value on every request, so an unbounded parameter
    # is an unbounded log line a token holder can emit at will.
    node_id: str = Query(default="", max_length=200),
    caller: node_credentials.AgentCaller = Depends(_check_token),
    session: Session = Depends(get_session),
) -> PoolTickOut:
    """Everything the node needs for one reconcile pass, except the verdicts (`GET /api/agent/worktrees`).

    Per-tick order is fetch → execute → probe → publish. `node_id` is the name, echoed on
    `PoolTickOut.node_id`; it does not select the intents (fenced to the credential's node) and does not
    default to this process's node. The empty tick posts nothing.
    """
    if not node_id:
        log.warning("pool-tick: no ?node_id= on the request — returning an empty tick")
        return PoolTickOut(node_id="", roster=[], intents=[], known_slots=[])

    # The intent select is fenced to the node the credential proved, never `?node_id=`: a `lease_slot`
    # payload carries resolved env vars in plaintext (`fleet_verdicts._env_vars_payload`). A caller proving no
    # node gets no intents (roster/`known_slots` still answer). Both narrowing branches warn, since a node
    # whose intents stop freezes behind a 200.
    if caller.node_id is None:
        log.warning(
            "pool-tick: the caller's credential proves no node (arm %s) — serving NO intents for the "
            "named node %r. An intent is addressed to one node and carries its secrets, so no "
            "fleet-wide reader is served one; set this host's NODE_TOKEN if its drain has stopped",
            caller.arm, node_id,
        )
        intent_node_is_the_caller = false()
        microvm_intent_node = None
    elif node_id != str(caller.node_id) and node_id_or_none(session, node_id) != caller.node_id:
        # Not a 403: other fields answer about the named node, but the proved node's intents beside another
        # node's roster would be unresolvable orders.
        log.warning(
            "pool-tick: credential proves node %s but the request names %r — serving NO intents rather "
            "than one node's orders beside another's roster. A real jaahd never sends a mismatch",
            caller.node_id, node_id,
        )
        intent_node_is_the_caller = false()
        microvm_intent_node = None
    else:
        intent_node_is_the_caller = SlotIntent.node_id == caller.node_id
        # The MicroVM rail's orders go only to the node the credential proved (`microvm_rail.pending_frames`).
        microvm_intent_node = caller.node_id

    # Two statements: the pending-intent query takes no join so its WHERE matches the
    # `ix_slot_intents_pending` partial index; names resolve afterwards in ONE bulk read.
    pending_intents = list(
        session.scalars(
            select(SlotIntent)
            # IS DISTINCT FROM, not `!=`: a never-acked outcome is NULL and `!= 'done'` is NULL in SQL.
            # Every retiring outcome is a strict SUBSET of the index predicate, so the scan uses it.
            .where(
                # The PROVED node's uuid as a bound parameter, not a correlated `node_ref` subquery, so
                # this clause still matches `ix_slot_intents_pending`. `false()` on either narrowing
                # branch: fail-CLOSED, and it plans as a Result node with no scan.
                intent_node_is_the_caller,
                *[
                    SlotIntent.outcome.is_distinct_from(outcome)
                    for outcome in SLOT_INTENT_RETIRING_OUTCOMES
                ],
                # THE BACKOFF: a refused row is withheld until `next_attempt_at`; NULL is "due now". Not
                # in the partial index — it narrows a scan already reduced to one node's pending rows.
                or_(
                    SlotIntent.next_attempt_at.is_(None),
                    SlotIntent.next_attempt_at <= func.now(),
                ),
            )
            .order_by(SlotIntent.created_at, SlotIntent.id)
        ).all()
    )
    intent_project_names = project_identity.names_by_id(
        session, [row.project_id for row in pending_intents]
    )
    # A payload-addressed erase names a project that left this node's org (`app/carryover.py`), which the read
    # above cannot see: its name rides its payload.
    for row in pending_intents:
        if row.verb == fleet_verdicts.VERB_ERASE_TREE and row.project_id not in intent_project_names:
            name = (row.payload or {}).get("project")
            if name:
                intent_project_names[row.project_id] = name
    unresolved = [row.id for row in pending_intents if row.project_id not in intent_project_names]
    if unresolved:
        # Unreachable through the FK, hence ERROR: a `slot_intents` row references a missing project.
        log.error(
            "pool-tick: NOT serving intent(s) %s to node %s — their project_id resolves to no project "
            "row, so the node could not name the directory the intent is about. The rows stay pending",
            unresolved, node_id,
        )
    intents = [
        PoolTickIntent(
            id=row.id,
            project_id=row.project_id,
            # An intent whose project resolved to nothing is NOT served: it names a directory the node
            # cannot construct, so it stays pending and visible rather than enacted on a guess.
            project=intent_project_names[row.project_id],
            number=row.number,
            verb=row.verb,
            decided_on_version=row.decided_on_version,
            decided_on_observed_at=(
                row.decided_on_observed_at.isoformat()
                if row.decided_on_observed_at is not None
                else None
            ),
            preconditions=row.preconditions or {},
            payload=row.payload or {},
            reason=row.reason,
            created_at=iso_or_none(row.created_at),
        )
        for row in pending_intents
        if row.project_id in intent_project_names
    ]
    # This tick's org: `caller.org_id` is what the credential proved (`node_id` is unverified). A mismatch
    # yields the caller's own grid over the named node's rows → empty slots, never another tenant's bands.
    # The node join and `known_slots` still trust `node_id` (debt recorded in `app/node_credentials.py`).
    org = caller.org_id if caller.org_id is not None else node_org(session, node_id)
    grid = port_grid.grid(session, org)
    # ONE `worktrees` read for BOTH builders (`node_slot_rows`).
    slot_rows = node_slot_rows(session, node_id)
    # ONE `nodes` read for the roster's `jaah_home` and the `microvm` flag, when both name the same node: the
    # credential's, and `?node_id=` resolves to it (the intent branch above). Otherwise each reads its own.
    node_row = _tick_node_row(session, node_id) if microvm_intent_node is not None else None
    microvm_intents: list[dict[str, Any]] = []
    if edition_features.enabled("microvm"):  # The community edition has no MicroVMs.
        from app import microvm_rail

        microvm_intents = microvm_rail.pending_frames(session, microvm_intent_node)
    return PoolTickOut(
        node_id=node_id,
        roster=build_pool_roster(session, node_id, grid, rows=slot_rows, org=org, node_row=node_row),
        intents=intents,
        known_slots=build_known_slots(session, node_id, rows=slot_rows),
        grid=PoolTickGrid(**grid.wire()),
        node_settings=settings_reads.node_settings(),
        # This tick's org only, never another's; `""` (none learned for it yet) sends nothing.
        remote_settings_consent_hash=node_config.org_consent_hash(session, org) or None,
        microvm=(
            bool(node_row.microvm) if node_row is not None
            else _node_microvm(session, caller.node_id or node_id_or_none(session, node_id))
        ),
        microvm_intents=microvm_intents,
    )


def _tick_node_row(session: Session, node_id: str) -> Any:
    """`nodes.jaah_home` and `nodes.microvm` for `?node_id=`, in one read — the row both `build_pool_roster`
    and `_node_microvm` would read, with the same predicate. A row of Nones when no node answers, which each
    reader takes exactly as its own empty read (`jaah_home` None, `microvm` False)."""
    row = session.execute(select(Node.jaah_home, Node.microvm).where(node_matches(node_id))).first()
    return row if row is not None else SimpleNamespace(jaah_home=None, microvm=None)


def _node_microvm(session: Session, node_uuid: Optional[UUID]) -> bool:
    """`nodes.microvm` of the node the credential proved (else the one `?node_id=` names); False for none."""
    return bool(session.scalar(select(Node.microvm).where(Node.id == node_uuid))) if node_uuid is not None else False
