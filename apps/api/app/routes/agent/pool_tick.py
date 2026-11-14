"""The node's pool tick: slot roster, known slots, pending intents and port grid (`GET /api/agent/pool-tick`)."""
from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any, Optional, Sequence
from uuid import UUID

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field, model_serializer
from sqlalchemy import false, func, or_, select
from sqlalchemy.orm import Session

from app import (
    carryover,
    fleet_verdicts,
    edition_features,
    forges,
    jaah_repos,
    node_config,
    node_credentials,
    port_grid,
    project_identity,
    project_repos,
    slot_allocation,
)
from app.project_env import port_specs_by_project
from app.operator_settings import reads as settings_reads
# routes need one for a user who is NOT the caller (they carry no session cookie, only the node token).
from app.authz.catalog import Tenancy
from app.authz.declarations import infra
from app.db_async import on_session
from app.node_clone import resolve_auth_dir, slot_path
from app.node_identity import node_id_or_none, node_matches, node_org, node_ref
from app.models.credentials import Account
from app.models.nodes import Node
from app.models.projects import Project
from app.models.worktrees import SlotIntent, Worktree
from app.entity_shape import from_row, iso_or_none
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


NODE_AUTHORED_OUTCOMES = fleet_verdicts.NODE_AUTHORED_OUTCOMES


class PoolTickSlot(BaseModel):
    """One sweepable slot. The swept set comes from here, not a `0..N` range; `path` is the slot's own
    directory (`node_clone.slot_path`), always sent.

    `config_dir` is checked by `fleet_decide.liveness_observed` against `probed_config_dirs`: a slot
    outside probe coverage has unobserved liveness and is never reclassified. `state` is the one occupancy
    field.
    """

    number: int
    path: str
    config_dir: Optional[str] = None
    state: str = "free"
    # Always `{}`: the node picks every slot port itself (`worktree_mechanics.ports.apply_auto_ports`). Still sent
    # only because a node on an older jaahd refuses a slot with no `ports` key; removed with the retired column's drop.
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
    # The slot's own kind (`Worktree.has_repo`), sent only when it differs from its entry's `has_repo`: a folder
    # slot in a git entry on a node that advertises `slot_allocation.FOLDER_SLOT`. Omitted otherwise, so the
    # wire stays what it was; the node reads an absent key as its entry's kind.
    has_repo: Optional[bool] = None

    @model_serializer(mode="wrap")
    def _omit_entry_kind(self, handler):
        data = handler(self)
        if data.get("has_repo") is None:
            data.pop("has_repo", None)
        return data


class PoolTickRepo(BaseModel):
    """One of a git project's repositories, in display order. `id` is `project_repos.id`, the key the
    node's slot manifest and its `repos` observation echo; `base` is that repo's own integration branch, and the
    last three its own Branch policy (`models.ProjectRepo`), which a newly claimed clone's access guard starts with."""

    id: int
    clone_url: str
    repo_path: str
    forge: str
    base: str
    primary: bool
    branch_prefix: str
    push_enabled: bool
    allow_force_push: bool


class PoolTickRosterEntry(BaseModel):
    """One enrolled project's slots plus the configuration the node does not discover — the roster is the
    one configuration source.

    Every entry is usable: a project missing `clone_url` or `base`, or rostered to a node with no recorded
    `jaah_home`, is omitted whole, except a `draining` one, which has no `clone_url`. No `clone`: the node
    derives its anchor from a slot's `path` (`apps/jaahd/worktree_mechanics/core.py::slot_anchor`), else its
    own home. The node reads `clone_url`/`repo_path` (`app/forges.py::RepoRef`). `port_vars` names the vars the node gives a
    free port at lease; the range it picks from rides on `PoolTickOut.grid`.
    """

    # `projects.id`, what `build_pool_roster` keys on: a roster keyed on the display name turns a rename
    # into a zeroed pool. `project` stays the display name the node uses for paths, tmux windows, logs.
    project_id: int
    project: str
    base: str
    # The credential-free https remote, the forge, and the forge-native repo path.
    clone_url: str
    forge: str
    repo_path: str
    # `branch_prefix`, stored WITH its trailing slash (`sn/`) or `""`. Every ref the node creates or
    # pushes is `{branch_prefix}fleet/task-<id>` (`fleet/` once: `app/task_branch.py`) and the push guard is keyed on
    # the same prefix, so a ref
    # rebuilt without it lands on a branch the node cannot push.
    branch_prefix: str = ""
    # The names of the project's Port variables (`app/project_env.py::port_specs`); `port_preferred` the port the
    # node tries first for those that name one (`worktree_mechanics.ports.apply_auto_ports`).
    port_vars: list[str] = []
    port_preferred: dict[str, int] = {}
    slots: list[PoolTickSlot] = []
    # False: a project with no repository, whose slots are empty folders; the node then needs only each
    # slot's `path`, and every git string is `""`. Defaulted true, so an entry that does not set it is a git
    # project — the node reads an absent key the same way.
    has_repo: bool = True
    # True: a git pair whose project's last repository was removed. Its git worktrees are being
    # destroyed (`slot_allocation.converge`), and with no repository there is no remote: `clone_url`,
    # `forge` and `repo_path` are `""`, so the node serves only their teardown.
    draining: bool = False
    # Every repository of a git project, primary flagged; the top-level git fields stay the primary's. `[]` for a
    # folder project or one with no row — the node then converges the single repo above, as before.
    repos: list[PoolTickRepo] = []
    # `projects.stack_down_cmd`, so a node can stop the old primary's stack when the primary changes. `""` for a
    # folder project.
    stack_down_cmd: str = ""
    # `projects.push_enabled`: whether the node may push the integration branch it finds missing on origin
    # (`apps/jaahd/slot_clone.py::_ensure_base`). False for a folder project; absent reads False.
    push_enabled: bool = False
    # `jaah_repos.jaah_repo_prefix` of the project's org: the push guard's `access-guard.jaah-repo-prefix` marker
    # (`""` clears it). None for a folder project; the node reads None or absent as "leave the marker as it is".
    jaah_repo_prefix: Optional[str] = None


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
    # The CP's DECISION — account dir, branch/base, task, plus the `generation` + `dispatch_id`
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
    """The org's slot-port range (`app/port_grid.py`) — the shape `GET /api/agent/port-grid` serves, and the
    range the node picks each slot's ports from (`worktree_mechanics.ports.apply_auto_ports`).

    The last four fields are constant and read by nothing current: a node on an older jaahd refuses a grid
    body without them (an empty `range_start_by_var` reads as "no fixed windows" there). Removed with the
    retired column's drop.
    """

    auto_port_floor: int
    auto_port_ceiling: int
    range_start_by_var: dict[str, int] = {}
    block_size: int = 1
    stride: int = 1
    window: int = 1


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
    if node_row is None:
        node_row = session.execute(
            select(Node.jaah_home, Node.launch_kinds).where(node_matches(node_id))
        ).first() or SimpleNamespace(jaah_home=None, launch_kinds=None)
    slot_home = node_row.jaah_home
    # A node that reads each slot's own kind holds git worktrees and folder slots in one entry.
    mixes_kinds = slot_allocation.FOLDER_SLOT in (getattr(node_row, "launch_kinds", None) or ())
    for row in rows if rows is not None else node_slot_rows(session, node_id):
        slots_by_project.setdefault(row.project_id, []).append(
            PoolTickSlot(
                number=row.number, path=row.path,
                config_dir=resolve_auth_dir(slot_home, row.account_slug) or None, state=row.state,
                last_used=iso_or_none(row.last_used),
                observed_at=iso_or_none(row.observed_at),
                kind=row.kind, retiring=row.retiring_at is not None,
                has_repo=getattr(row, "has_repo", True),
            )
        )
        kinds_by_project.setdefault(row.project_id, []).append(getattr(row, "has_repo", True))

    roster: list[PoolTickRosterEntry] = []
    # THIS node's clone layout, so every entry names a directory that exists ON IT. `slot_home` above is
    # the same value: one read, deliberately not two (this was the CP's highest-volume query).
    jaah_home = slot_home
    # A slot's `path` names its tree and anchors the node's clone; with no home the CP composes no path.
    # True exactly when the home is non-empty after strip/rstrip("/") (`node_clone._project_root`).
    has_home = bool(slot_path(jaah_home, 0, 1))
    # `org` must be this node's. No org → unstamped rows.
    org = org if org is not None else node_org(session, node_id)
    projects = session.scalars(
        select(Project)
        .where(Project.org_id == org)
        .order_by(Project.name)
    ).all()
    # Every project's repositories in one read, not one per project.
    repo_rows = project_repos.repos_by_project(session, [project.id for project in projects])
    # And every project's Port variables (`app/project_env.py::port_specs_by_project`).
    port_specs_by_project_id = port_specs_by_project(session, [project.id for project in projects])
    # One org, so one prefix for every entry; read only when there is a project to send it with.
    jaah_prefix = jaah_repos.jaah_repo_prefix(jaah_repos.org_slug_of(session, org)) if projects else ""
    # A node is rostered its own org's projects only: a cross-org roster would disclose tenants' projects. Not
    # yet enforced for `worktrees` (the unkeyed-reference register). An org-less node gets an empty roster
    # (unreachable: registrations are stamped).
    for project in projects:
        ref = forges.repo_ref_or_none(project)
        # A repo project is a git entry, its folder slots flagged one by one, on a node that mixes kinds — and on
        # any node still holding one of its git worktrees: a node that stopped advertising `folder_slot` (a
        # downgrade, a MicroVM mark) must never read a held git worktree as a folder. Otherwise a node reads ONE kind
        # per entry, so a pair still holding slots of the other kind after the project's repository flipped keeps
        # their kind until they are gone (`slot_allocation.kind_of_pair`); the planner makes none of the new kind
        # meanwhile.
        kinds = kinds_by_project.get(project.id, [])
        entry_has_repo = (
            True if project.has_repo and (mixes_kinds or True in kinds)
            else slot_allocation.kind_of_pair(project.has_repo, kinds)
        )
        # Each slot carries its own kind only where it differs from its entry's: a pure pair's wire is unchanged,
        # and a node that predates per-slot kinds ignores the key.
        for slot in slots_by_project.get(project.id, []):
            slot.has_repo = slot.has_repo if slot.has_repo != entry_has_repo else None
        if not entry_has_repo:
            if not has_home:
                log.warning("pool-tick: project %s has no slot path on node %s (no jaah_home) — omitting its slots",
                            project.name, node_id)
                continue
            # A folder project: each slot's `path` names its directory. Every git
            # string is `""`, and no port vars — a folder runs no stack.
            roster.append(PoolTickRosterEntry(
                project_id=project.id, project=project.name, base="", clone_url="",
                forge="", repo_path="", branch_prefix="",
                port_vars=[], slots=slots_by_project.get(project.id, []),
                has_repo=False,
            ))
            continue
        # A git pair of a project with no repository left: rostered only so its worktrees can be destroyed.
        draining = ref is None and not project.has_repo
        if not has_home or (ref is None and not draining):
            log.warning(
                "pool-tick: project %s is unusable on node %s (jaah_home=%r clone_url=%r) — omitting its slots",
                project.name, node_id, jaah_home, ref.clone_url() if ref is not None else None,
            )
            continue
        # The base is the column. An empty `integration_branch` omits the project WHOLE, like a missing
        # `clone_url`, rather than silently becoming `develop`.
        base = (project.integration_branch or "").strip()
        if not base:
            log.warning(
                "pool-tick: project %s has no integration_branch set — omitting its slots (fail-closed; "
                "the branch runners rebase onto and PR into is never derived)",
                project.name,
            )
            continue
        specs = port_specs_by_project_id.get(project.id, [])
        port_vars = [spec.name for spec in specs]
        port_preferred = {spec.name: spec.preferred for spec in specs if spec.preferred is not None}
        # BY ID — a name lookup here is how a rename ships an empty slot list.
        slots = slots_by_project.get(project.id, [])
        roster.append(
            PoolTickRosterEntry(
                project_id=project.id,
                project=project.name,
                base=base,
                clone_url=ref.clone_url() if ref is not None else "",
                forge=ref.forge if ref is not None else "",
                repo_path=ref.path if ref is not None else "",
                branch_prefix=project.branch_prefix or "",
                port_vars=port_vars,
                port_preferred=port_preferred,
                slots=slots,
                repos=_roster_repos(project, repo_rows.get(project.id, [])),
                stack_down_cmd=project.stack_down_cmd or "",
                push_enabled=bool(project.push_enabled),
                jaah_repo_prefix=jaah_prefix,
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
            base=row.integration_branch, primary=row.is_primary, branch_prefix=row.branch_prefix,
            push_enabled=row.push_enabled, allow_force_push=row.allow_force_push,
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
@on_session
def pool_tick(
    session: Session,
    node_id: str = Query(default="", max_length=200),
    caller: node_credentials.AgentCaller = Depends(_check_token),
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
                    for outcome in fleet_verdicts.SLOT_INTENT_RETIRING_OUTCOMES
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
        from_row(
            PoolTickIntent, row,
            # An intent whose project resolved to nothing is NOT served: it names a directory the node
            # cannot construct, so it stays pending and visible rather than enacted on a guess.
            project=intent_project_names[row.project_id],
            decided_on_observed_at=(
                row.decided_on_observed_at.isoformat()
                if row.decided_on_observed_at is not None
                else None
            ),
            preconditions=row.preconditions or {},
            payload=row.payload or {},
        )
        for row in pending_intents
        if row.project_id in intent_project_names
    ]
    # This tick's org: `caller.org_id` is what the credential proved (`node_id` is unverified). A mismatch
    # yields the caller's own org over the named node's rows → empty slots, never another tenant's.
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
        roster=build_pool_roster(session, node_id, rows=slot_rows, org=org, node_row=node_row),
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
    """`nodes.jaah_home`, `nodes.launch_kinds` and `nodes.microvm` for `?node_id=`, in one read — the row both
    `build_pool_roster` and `_node_microvm` would read, with the same predicate. A row of Nones when no node answers,
    which each reader takes exactly as its own empty read (`jaah_home` None, `microvm` False)."""
    row = session.execute(
        select(Node.jaah_home, Node.launch_kinds, Node.microvm).where(node_matches(node_id))
    ).first()
    return row if row is not None else SimpleNamespace(jaah_home=None, launch_kinds=None, microvm=None)


def _node_microvm(session: Session, node_uuid: Optional[UUID]) -> bool:
    """`nodes.microvm` of the node the credential proved (else the one `?node_id=` names); False for none."""
    return bool(session.scalar(select(Node.microvm).where(Node.id == node_uuid))) if node_uuid is not None else False
