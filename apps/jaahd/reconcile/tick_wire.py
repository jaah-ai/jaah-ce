"""The pool-tick wire model: roster, known-slot and intent records, their parsers, and the tick fetch."""
from __future__ import annotations

import asyncio
import dataclasses
import logging
from dataclasses import dataclass, field

import cloud_modules
import control_plane_api
import host_platform
import slot_repos
from worktree_mechanics import core, ports as wm_ports

log = logging.getLogger(__name__)


# The CP owns slot configuration; the node reads the worktree's own files (`.env` ports, compose name, stack
# presence) because that is sensing — roster values would blind the CP's drift verdict.
_ROSTER_GATED_FIELDS = ("clone", "base", "clone_url")


# `branch_prefix` is gated on presence: `""` is legitimate, but absent-read-as-`""` would tell the
# branch-shape guard a prefixed project is unprefixed.
_ROSTER_PRESENCE_GATED_FIELDS = ("branch_prefix",)


# A project with no repository (`has_repo: false`) has nothing to clone or fetch: every git string
# on its entry is `""` by contract, so only the clone path — which `wt_path` derives its folders from — is
# gated.
_FOLDER_ROSTER_GATED_FIELDS = ("clone",)


# A git entry the CP marks `draining: true`: its project's last repository was removed, so the CP has no remote
# to send (`clone_url` `""`) and serves only the teardown of the git worktrees still on this node. The clone and
# its base are still gated; nothing is cloned or fetched for it (`worktree_mechanics` refuses an empty remote).
_DRAINING_ROSTER_GATED_FIELDS = ("clone", "base")


def wire_has_repo(raw: object) -> bool:
    """The CP's `has_repo` as the node reads it, on a roster entry or an intent payload: only an explicit
    `false` is a folder project. ABSENT MEANS TRUE, so a CP that predates folder slots keeps every slot on
    today's git path byte for byte."""
    return raw is not False


def wire_push_enabled(raw: object) -> bool:
    """The CP's `push_enabled` on a roster entry: only an explicit `true` lets the node push on its own
    (`worktree_mechanics.core._create_missing_base`). Absent — an older CP — is False, the side that pushes nothing."""
    return raw is True


def _mechanics_config(
    *, name: str, clone: str, base: str, clone_url: str, repo_path: str, branch_prefix: str,
    port_vars: tuple[str, ...], has_repo: bool, project_id: int | None = None,
    repos: tuple = (), push_enabled: bool = False,
) -> "core.PoolConfig":
    """The one roster → `worktree_mechanics.core.PoolConfig` conversion; a second construction is how a field (e.g.
    `clone_url`, read empty as "no remote") silently starts arriving empty.

    `port_vars` is still needed: the observation reads those vars from the worktree's `.env`.
    `branch_prefix` is required keyword-only so a caller cannot forget it."""
    return core.PoolConfig(
        name=name, clone=clone, base=base, clone_url=clone_url, repo_path=repo_path,
        branch_prefix=branch_prefix,
        port_vars=list(port_vars), has_repo=has_repo, project_id=project_id, repos=repos,
        push_enabled=push_enabled,
    )


def _pool_config(wire) -> "core.PoolConfig":
    """`RosterProject` and `RosterSlot` carry the same mechanics fields, so one conversion serves both."""
    return _mechanics_config(
        name=wire.project, clone=wire.clone, base=wire.base, clone_url=wire.clone_url,
        repo_path=wire.repo_path, branch_prefix=wire.branch_prefix, port_vars=wire.port_vars,
        has_repo=wire.has_repo, project_id=wire.project_id, repos=wire.repos,
        push_enabled=wire.push_enabled,
    )


@dataclass(frozen=True)
class RosterProject:
    """One enrolled project's CP-supplied configuration, independent of its current slots.

    Needed where no slot can supply it (`create`/`lease` pick a slot that does not exist yet). The CP
    declares which slot rows exist; the node carries no ceiling of its own."""

    project: str
    clone: str
    base: str
    # The CP's credential-free remote (gated) and forge path (`o/r`, `grp/sub/repo`; `""` = unknown).
    clone_url: str
    repo_path: str
    # With its trailing slash, or `""`; the CP's, never derived (the `_roster_entry` presence gate).
    branch_prefix: str = ""
    port_vars: tuple[str, ...] = ()
    # Per project: one fetch refreshes `origin/<base>` for every slot. Written by the observation POST, so
    # `None` while that cannot land. `None` is not `0`: `None` means "fetch now" (`fetch_base_if_stale`'s gate
    # 3).
    fetch_stamp_age_secs: int | None = None
    # False: a project with no repository, whose slots are empty folders (`wire_has_repo`).
    has_repo: bool = True
    # The entry's `projects.id` — the identity every daemon→CP call sends beside the display name. None =
    # the CP did not say (an older CP), and callers then send the name alone.
    project_id: int | None = None
    # Every repository of the project (`slot_repos.RepoSpec`, the primary flagged). `()` = an older
    # CP, a folder project, or an unusable list: today's single-repo path, byte for byte.
    repos: tuple = ()
    # `projects.stack_down_cmd`: what stops the old primary's stack when a primary swap moves it out.
    stack_down_cmd: str = ""
    # `projects.push_enabled` (`wire_push_enabled`).
    push_enabled: bool = False

    def pool_config(self) -> "core.PoolConfig":
        return _pool_config(self)


# `worktrees.state`: a worktree belongs to a task, a user, or is free. Mirrored from
# `apps/api/app/slot_state.py`, held together by `tests/test_fleet_wire_contract.py`. Whether a
# runner is breathing is a comparison of launch vs sensed columns, never a state. Single home for the node;
# `commands.py` reads it here.
SLOT_FREE = "free"


# A fleet Task's hold — the one occupied state `_enact_kill_runner` may act on, named so a fence can require
# it specifically.
SLOT_TASK = "task"


# A human's hold, singled out by the orphan reap (`_observe_one`) and the kill-runner sweep.
SLOT_USER = "user"


# The positive test, never `!= SLOT_FREE`: an undescribed slot parses as `""`, which must read neither busy
# nor free — callers that must not guess also handle `""` explicitly.
OCCUPIED_SLOT_STATES = frozenset({SLOT_TASK, SLOT_USER})


@dataclass(frozen=True)
class RosterSlot:
    """One CP-supplied pool slot. `is_main` is a roster attribute, not an `n == "0"` test.

    `state` is the only occupancy fact the CP serves; `tests/test_fleet_wire_contract.py` refuses a parser
    reading the retired `holder` keys."""

    project: str
    number: str
    path: str
    clone: str
    base: str
    clone_url: str
    repo_path: str
    # The project's prefix, so a slot-scoped `reset_to_base` sees the same config as a project-scoped one.
    branch_prefix: str = ""
    is_main: bool = False
    port_vars: tuple[str, ...] = ()
    # The CP-decided port band (pairs, so the frozen dataclass stays hashable). `()` = no declared ports;
    # `apply_port_band` refuses an absent band. A declared var the band omits is tier 2
    # (`worktree_mechanics.ports.apply_auto_ports`).
    ports: tuple[tuple[str, int], ...] = ()
    # `worktrees.state` (`free`/`task`/`user`), decided by `slot_state`. `""` = the roster said nothing, not
    # `free`: test membership in `OCCUPIED_SLOT_STATES`, never `!= "free"`.
    state: str = ""
    # The CP's stored copy of the timestamp the mutation verbs author; the observation ships
    # `age_last_used_secs` instead. "" yields None ("unknown", not "just used").
    last_used: str = ""
    # R3's staleness token vs `SlotIntent.decided_on_observed_at`; "" refuses every intent (fail-closed).
    observed_at: str = ""
    # `worktrees.kind` (`static`/`dynamic`) and whether the CP has ordered it destroyed: the row a
    # `destroy_slot` intent resolves against stays in the roster until the ack deletes it. "" = not said.
    kind: str = ""
    retiring: bool = False
    # The entry's `projects.id`, which `destroy_slot`'s compose project is checked against. None = not said.
    project_id: int | None = None
    # False: an empty FOLDER slot of a project with no repository (`wire_has_repo`) — never a git tree, so
    # no git runs against it, ever.
    has_repo: bool = True
    # The project's `RosterProject.repos` / `stack_down_cmd`, so a session-start converge needs no second read.
    repos: tuple = ()
    stack_down_cmd: str = ""
    push_enabled: bool = False

    @property
    def ref(self) -> tuple[str, str]:
        return (self.project, self.number)

    def pool_config(self) -> "core.PoolConfig":
        """This slot's config for the mechanics (`reset_to_base` is the only slot-scoped call)."""
        return _pool_config(self)


@dataclass(frozen=True)
class KnownSlot:
    """One slot the CP has a `worktrees` row for, unfiltered by the roster's usability gate.

    The node's accounting set, which makes the CP's complement DELETE safe: report a state for every
    element of `known_slots` plus every roster slot. `known_slots \\ roster_slots` reach the disk verdict
    labelled from their stored `path` alone. `number` is a `str`, as in `RosterSlot`."""

    project: str
    number: str
    path: str
    # The one discriminator between "declared, never built" and "built, then deleted"
    # (`_unmaterialised_slot_entry`); `""` spares the row. Not fatal if missing.
    observed_at: str = ""

    @property
    def ref(self) -> tuple[str, str]:
        return (self.project, self.number)


@dataclass(frozen=True)
class SlotIntent:
    """A CP-decided action for one slot, carrying R3's two-part staleness guard: the observation
    revision it was decided on, and the preconditions the node re-verifies against a fresh probe."""

    id: object
    verb: str
    project: str
    number: str
    # The staleness token, compared against `RosterSlot.observed_at`; "" refuses the intent (see
    # `observation_is_current`). `decided_on_version` is deliberately NOT read node-side.
    decided_on_observed_at: str = ""
    preconditions: dict = field(default_factory=dict)
    # The CP's decision, applied verbatim (vs `preconditions`, the doubt the node may refuse on) — including
    # the `generation`/`dispatch_id` pair the ack must echo. Per-verb shape, CP-authored.
    payload: dict = field(default_factory=dict)
    reason: str = ""
    # The CP's mint stamp, read only for `_merge_intents` ordering (ISO-8601 sorts chronologically), never as
    # a clock.
    created_at: str = ""

    @property
    def ref(self) -> tuple[str, str]:
        return (self.project, self.number)


@dataclass(frozen=True)
class PoolTick:
    """One `GET /api/agent/pool-tick` response. `ok` False = the CP was unreachable or sent something
    unusable: sweep, publish and execute nothing — the pool freezes for the outage."""

    ok: bool = False
    slots: tuple[RosterSlot, ...] = ()
    intents: tuple[SlotIntent, ...] = ()
    # Per-project configuration from the same roster entries; the mutation verbs address a project.
    projects: dict[str, RosterProject] = field(default_factory=dict)
    # Every slot the CP has a row for. Empty is legitimate; absent or unparseable degrades the tick to
    # `ok=False`.
    known_slots: tuple[KnownSlot, ...] = ()
    # The tier-2 range off the tick's fleet-global `grid` block
    # (`worktree_mechanics.ports.auto_port_range_from_grid`): one pair, not a per-slot copy of a global fact. None =
    # no block, not a degrade — the default lives in `worktree_mechanics`.
    auto_ports: tuple[int, int] | None = None
    # The node's MicroVM mark (top-level `microvm`, absent = False). None = the CP did not answer, which
    # must never unmark a node (`microvm_mark.sync`).
    microvm: bool | None = None
    # The `microvm` rail's orders (`microvm_rail.intents_from_tick`), parsed on every tick like the mark: a
    # MicroVM node has no roster. Enacted only on a marked node.
    microvm_intents: tuple[dict, ...] = ()
    # Every roster entry parsed into `projects` (none dropped as unusable): the only roster the pool-mirror
    # sweep may read as "a project absent here is gone" (`sweep_pool_mirrors`).
    roster_complete: bool = False


def port_band_from_wire(raw: object) -> tuple[tuple[str, int], ...]:
    """The CP's port band for one slot → a sorted, hashable `(VAR, port)` tuple. The single parse.

    A non-integer value is dropped, never defaulted (the node holds no grid and must not decide a port).
    Sorted so a reordered dict is not a changed slot."""
    if not isinstance(raw, dict):
        return ()
    band: dict[str, int] = {}
    for var, port in raw.items():
        if isinstance(port, bool):
            continue
        try:
            band[str(var)] = int(port)
        except (TypeError, ValueError):
            log.warning("reconcile: ignoring non-numeric port %r for %r in the CP's band", port, var)
    return tuple(sorted(band.items()))


def _roster_entry(entry: dict) -> tuple[RosterProject | None, list[RosterSlot]]:
    """One project's roster entry → its `RosterProject` plus one `RosterSlot` per sweepable slot.

    `(None, [])` when unusable; the gate is applied once for both, so neither survives a gate the other
    failed. A missing gated field is never substituted with "" — the contract is enforced on both sides."""
    project = str(entry.get("project") or "")
    if not project:
        log.warning("reconcile: roster entry without a project name, skipping: %r", entry)
        return None, []
    has_repo = wire_has_repo(entry.get("has_repo"))
    if has_repo:
        gated = _DRAINING_ROSTER_GATED_FIELDS if entry.get("draining") is True else _ROSTER_GATED_FIELDS
        missing = [f for f in gated if not entry.get(f)]
        missing += [f for f in _ROSTER_PRESENCE_GATED_FIELDS if f not in entry]
    else:
        missing = [f for f in _FOLDER_ROSTER_GATED_FIELDS if not entry.get(f)]
    if missing:
        log.warning(
            "reconcile: roster entry %s is missing %s — its slots are skipped, nothing published (R4)",
            project, ", ".join(missing),
        )
        return None, []
    clone = str(entry["clone"])
    base = str(entry.get("base") or "")
    clone_url = str(entry.get("clone_url") or "")
    # Not gated: the stack identity falls back to the project name without it (`PoolConfig.repo`).
    repo_path = str(entry.get("repo_path") or "")
    branch_prefix = str(entry.get("branch_prefix") or "")
    raw_port_vars = entry.get("port_vars")
    port_vars = tuple(str(v) for v in raw_port_vars if v) if isinstance(raw_port_vars, list) else ()
    # Non-int = "never fetched" (None). `bool` excluded: `True` would become age 1.
    raw_fetch_age = entry.get("fetch_stamp_age_secs")
    fetch_stamp_age_secs = (
        raw_fetch_age if isinstance(raw_fetch_age, int) and not isinstance(raw_fetch_age, bool) else None
    )
    raw_project_id = entry.get("project_id")
    project_id = (
        raw_project_id if isinstance(raw_project_id, int) and not isinstance(raw_project_id, bool) else None
    )
    # Absent/empty (an older CP, a folder project) is `()`, today's path (S4).
    repos = slot_repos.repos_from_wire(entry.get("repos")) if has_repo else ()
    stack_down_cmd = str(entry.get("stack_down_cmd") or "")
    push_enabled = wire_push_enabled(entry.get("push_enabled"))
    roster_project = RosterProject(
        project=project, clone=clone, base=base, clone_url=clone_url, repo_path=repo_path,
        branch_prefix=branch_prefix, port_vars=port_vars,
        fetch_stamp_age_secs=fetch_stamp_age_secs, has_repo=has_repo, project_id=project_id,
        repos=repos, stack_down_cmd=stack_down_cmd, push_enabled=push_enabled,
    )

    out: list[RosterSlot] = []
    raw_slots = entry.get("slots")
    if not isinstance(raw_slots, list):
        return roster_project, out
    for raw in raw_slots:
        if not isinstance(raw, dict):
            continue
        number = raw.get("number")
        if number is None:
            log.warning("reconcile: roster slot without a number in %s, skipping: %r", project, raw)
            continue
        number = str(number)
        is_main = bool(raw.get("is_main"))
        # The default path is keyed on `is_main`, never the slot index.
        path = str(raw.get("path") or (clone if is_main else f"{clone}-{number}"))
        out.append(
            RosterSlot(
                project=project,
                number=number,
                path=path,
                clone=clone,
                base=base,
                clone_url=clone_url,
                repo_path=repo_path,
                branch_prefix=branch_prefix,
                is_main=is_main,
                port_vars=port_vars,
                ports=port_band_from_wire(raw.get("ports")),
                state=str(raw.get("state") or ""),
                last_used=str(raw.get("last_used") or ""),
                observed_at=str(raw.get("observed_at") or ""),
                kind=str(raw.get("kind") or ""),
                retiring=raw.get("retiring") is True,
                project_id=project_id,
                has_repo=has_repo,
                repos=repos,
                stack_down_cmd=stack_down_cmd,
                push_enabled=push_enabled,
            )
        )
    return roster_project, out


def _slot_intent_from_entry(entry: dict) -> SlotIntent | None:
    """One wire intent, parsed permissively on the verb.

    An unrecognised verb is carried through so `execute_intent` can answer `unsupported_verb` and retire it;
    dropped here it would re-serve forever. Only fields with no honest default (id, verb, slot) are fatal.

    Rejections log the id and key names only: this also parses pushed frames, and `payload` can hold a
    human's gate answer or a prompt bundle."""
    def _reject(why: str) -> None:
        log.warning(
            "reconcile: ignoring intent %r — %s (keys: %s)",
            entry.get("id"), why, ",".join(sorted(str(k) for k in entry)) or "none",
        )

    # No id means no ack: an unretirable destructive instruction re-run every pass. It must also be an integer
    # — `_merge_intents` sorts on it, and one string id would raise and abort the whole pass. Unreachable from
    # a real mint; the per-intent drop is the bounded failure for an executable frame.
    raw_id = entry.get("id")
    if raw_id is None or isinstance(raw_id, bool) or not isinstance(raw_id, int):
        _reject(
            "it carries no usable id, so no outcome could ever be recorded for it"
            if raw_id is None else
            f"its id is a {type(raw_id).__name__}, not an integer — it could neither be acked nor ordered"
        )
        return None
    verb = str(entry.get("verb") or "")
    if not verb:
        _reject("it names no verb")
        return None
    project = str(entry.get("project") or "")
    number = entry.get("number")
    if not project or number is None:
        _reject("it names no slot")
        return None
    preconditions = entry.get("preconditions")
    if not isinstance(preconditions, dict):
        preconditions = {}
    # A non-object payload is `{}`: every executor key is absent-checked, so it refuses on its own terms.
    payload = entry.get("payload")
    if not isinstance(payload, dict):
        payload = {}
    return SlotIntent(
        id=entry.get("id"),
        verb=verb,
        project=project,
        number=str(number),
        decided_on_observed_at=str(entry.get("decided_on_observed_at") or ""),
        preconditions=preconditions,
        payload=payload,
        reason=str(entry.get("reason") or ""),
        created_at=str(entry.get("created_at") or ""),
    )


def _slot_intents_from_entries(entries: "list[dict] | tuple[dict, ...] | None") -> tuple[SlotIntent, ...]:
    """Parse raw wire intent entries into `SlotIntent`s, dropping the ones `_slot_intent_from_entry` refuses.

    The one parser: a pushed intent (`send_intent`) carries the same object as a polled one."""
    if not entries:
        return ()
    parsed: list[SlotIntent] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        intent = _slot_intent_from_entry(entry)
        if intent is not None:
            parsed.append(intent)
    return tuple(parsed)


def _known_slots(body: dict) -> tuple[KnownSlot, ...] | None:
    """The tick's `known_slots`, or None meaning "this tick is unusable".

    Unlike `_roster_entry`, one bad element (or an absent field) degrades the whole tick: this is the
    accounting set, and the CP's complement DELETE removes every row not reported, so a shrunken set widens
    the delete. An empty list is fine (a node with no rows yet)."""
    raw = body.get("known_slots")
    if not isinstance(raw, list):
        log.warning(
            "reconcile: pool-tick carried no known_slots (got %s) — treating the tick as unavailable. "
            "Without the node's full accounting set the complement delete would widen to every slot the "
            "roster omitted, so this fails closed rather than pruning.",
            type(raw).__name__,
        )
        return None
    out: list[KnownSlot] = []
    for entry in raw:
        if not isinstance(entry, dict):
            log.warning("reconcile: known_slots element is not an object (%r) — tick unavailable", entry)
            return None
        project = str(entry.get("project") or "")
        number = entry.get("number")
        path = str(entry.get("path") or "")
        if not project or number is None or not path:
            log.warning(
                "reconcile: known_slots element missing project/number/path (%r) — tick unavailable. "
                "`path` is the disk verdict's only input besides the syscall pair, so a slot without one "
                "cannot be labelled at all.",
                entry,
            )
            return None
        out.append(KnownSlot(
            project=project, number=str(number), path=path,
            observed_at=str(entry.get("observed_at") or ""),
        ))
    return tuple(out)


def _microvm_rail():
    """`microvm_rail`, imported where it is used: its supervisor modules need POSIX-only stdlib the
    macOS/Windows daemon bundles do not carry (as `cloud_node.microvm_facts`). None on a community-edition node."""
    return cloud_modules.load("microvm_rail")


def parse_pool_tick(body: object) -> PoolTick:
    """Parse a pool-tick body into the roster + intent set (`_parse_roster_tick`), plus the `microvm` rail's
    orders (`microvm_rail.intents_from_tick`), carried whatever the roster said like the mark: a MicroVM node
    has no roster."""
    tick = _parse_roster_tick(body)
    orders = body.get("microvm_intents") if isinstance(body, dict) else None
    mark = cloud_modules.load("microvm_mark") if orders else None
    rail = _microvm_rail() if mark is not None and mark.host_supported() else None
    if rail is not None:
        tick = dataclasses.replace(tick, microvm_intents=tuple(rail.intents_from_tick(body)))
    elif orders and host_platform.is_windows() and isinstance(orders, list):
        # Verbatim to the WSL2 backend, whose own `intents_from_tick` parses them (`microvm_bridge`).
        tick = dataclasses.replace(tick, microvm_intents=tuple(e for e in orders if isinstance(e, dict)))
    return tick


def _parse_roster_tick(body: object) -> PoolTick:
    """Parse a pool-tick body into the roster + intent set.

    An unusable project entry is dropped on its own; a missing roster, or a non-empty one with no usable
    project, fails the tick ("no instruction", never "every slot is gone"). Gated on `projects`, not
    `slots`: zero slots is normal for a just-enrolled project, which must still be able to publish."""
    if not isinstance(body, dict):
        log.warning("reconcile: pool-tick body is not an object (%s) — treating as unavailable", type(body))
        return PoolTick(ok=False)
    # Before the roster gate: a MicroVM node has no roster to gate on. Absent = False, but only in a REAL
    # answer: a transport failure arrives as `{}` (`agent_api_pool_tick`), and reading that as "unmarked"
    # would tear a live node's pool down on every CP outage. A real answer carries `roster`.
    microvm = (body.get("microvm") is True) if ("microvm" in body or "roster" in body) else None
    raw_roster = body.get("roster")
    if not isinstance(raw_roster, list):
        log.warning("reconcile: pool-tick carried no usable roster — treating as unavailable")
        return PoolTick(ok=False, microvm=microvm)
    slots: list[RosterSlot] = []
    projects: dict[str, RosterProject] = {}
    dropped = False
    for entry in raw_roster:
        if not isinstance(entry, dict):
            dropped = True
            continue
        roster_project, entry_slots = _roster_entry(entry)
        if roster_project is not None:
            projects[roster_project.project] = roster_project
        else:
            dropped = True
        slots.extend(entry_slots)
    # An empty roster is an answer: a node with no enrolled project has no pool, and freezing would
    # withhold its host metrics. Safe because `known_slots` is still the full accounting
    # set, so the complement DELETE cannot widen.
    if raw_roster and not projects:
        log.warning("reconcile: pool-tick roster yielded no usable project — treating as unavailable")
        return PoolTick(ok=False, microvm=microvm)
    known_slots = _known_slots(body)
    if known_slots is None:
        return PoolTick(ok=False, microvm=microvm)
    raw_intents = body.get("intents")
    intents = _slot_intents_from_entries(raw_intents if isinstance(raw_intents, list) else None)
    # The only ungated field: operator settings with a module-constant default, so garbage leaves `auto_ports`
    # None rather than freezing a node's pool over a knob.
    raw_grid = body.get("grid")
    auto_ports = (
        wm_ports.auto_port_range_from_grid(raw_grid) if isinstance(raw_grid, dict) else None
    )
    return PoolTick(
        ok=True, slots=tuple(slots), intents=intents, projects=projects, known_slots=known_slots,
        auto_ports=auto_ports,
        microvm=microvm, roster_complete=not dropped,
    )


async def fetch_pool_tick(node_id: str | None) -> PoolTick:
    """The roster and intents, off `GET /api/agent/pool-tick` (verdicts are on the `worktrees` rows instead).

    Blocking urllib, run off the loop. Any failure degrades to `ok=False`, freezing the pool for the
    outage."""
    try:
        body = await asyncio.to_thread(control_plane_api.agent_api_pool_tick, node_id or "")
    except Exception as exc:  # noqa: BLE001 — an unreachable CP must never wedge the daemon's tick
        log.warning("reconcile: pool-tick unavailable (%s) — sweeping nothing this pass", exc)
        return PoolTick(ok=False)
    return parse_pool_tick(body)


def slot_kind_mismatch(slot: RosterSlot, intent: SlotIntent) -> str:
    """`""` when the intent and the roster agree on what kind of slot this is, else the refusal.

    The roster's `has_repo` decides how the node treats a slot; an intent that carries its own (`lease_slot`,
    `destroy_slot`) must say the same thing, or the two halves of the CP disagree about whether this path is
    a git tree or an empty folder — and one of the two readings is destructive. Absent reads true on both
    sides (`wire_has_repo`)."""
    said = wire_has_repo(intent.payload.get("has_repo"))
    if said == slot.has_repo:
        return ""
    kind = {True: "a git worktree", False: "a folder slot"}
    return (
        f"the intent says {slot.project} #{slot.number} is {kind[said]} but the roster says it is "
        f"{kind[slot.has_repo]} — refusing to act on a slot the two disagree about"
    )
