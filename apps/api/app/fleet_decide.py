"""The fleet CP decision core — the PURE decision logic apps/api owns.

Rotation/dedup arithmetic, slot selection, and the sensor-only reconcile. No I/O and no git:
inputs come from the DB (`app/pool_state.py`, `app/slot_state.py`) via `app/routes/fleet.py`.
Selection only: the create/reset/live-cwd mechanics stay in jaahd, which fails closed on a stale pick.
"""
from __future__ import annotations

import logging
import re
from datetime import datetime
from typing import List, Optional

from app.task_lifecycle import AWAITING_RESPONSE, PAUSED

log = logging.getLogger("fleet-decide")


# Task states that hold a worktree with nobody running in it until a human acts (`is_active_record`).
_PARKED_TASK_STATES = frozenset({AWAITING_RESPONSE, PAUSED})


# ══ C. Pure pool arithmetic ════════════════════════════════════════════════════════════════════════════

# `record["holder"]` is `Worktree.state` verbatim (`free` | `task` | `user` | `main` display override).
# The concurrency cap counts `task`-held slots only: a human's `user` session consumes no fleet budget.
# `held_slots_from_records` is wider on purpose, since a `user` slot must still refuse a pick.
def is_active_record(record: dict) -> bool:
    """`holder == 'task'`, unless that task is parked with no live runner.

    Not the branch or session: a fresh lease leaves the worktree detached with no session yet,
    so those proxies under-count every in-flight dispatch and the plan over-dispatches. `holder` is the
    CP's own decision, true from the leasing statement onward. A task-held slot whose runner
    died still counts (the conservative direction). A `user` slot never counts.

    The one exception: a task waiting for an answer or stopped (`_PARKED_TASK_STATES`) whose runner is gone
    keeps its tree but no runner place. Nothing relaunches it until a human acts, which returns it to
    `ready`, so counting it would hold the node's cap for days."""
    if record.get("holder") != "task":
        return False
    return not (record.get("_task_state") in _PARKED_TASK_STATES and record.get("session") is not True)


def count_active(records: List[dict]) -> int:
    """The number of pool worktrees a fleet Task currently holds (see `is_active_record`)."""
    return sum(1 for r in records if is_active_record(r))


def inflight_task_ids(records: List[dict]) -> set:
    """Task ids a pool slot currently reports a live session for — the dedup that stops a task whose
    runner is already up from being dispatched twice.

    Requires `task_id` and the same record's sensed `session`: `task_id` stays bound after a runner dies
    (so the task can re-enter its own slot), and counting it alone would wedge that task forever. Never
    inferred from the branch, which outlives the run."""
    return {
        str(record["task_id"])
        for record in records
        if record.get("task_id") is not None and record.get("session") is True
    }


# Gates whether a `task`-held slot with no observed live session may be redispatched into its own slot
# (it never frees anything). Gated on the CP's own dispatch facts rather than the node's sighting, so a
# not-yet-acked launch is not superseded every drain tick. 120s is sized against what an ack costs a node
# (fetch intent, clone/reset, start session), not the drain cadence.
UNACKED_REDISPATCH_DEADLINE_SECONDS = 120.0

# The deadline above is not proof the order was lost: a node runs its intents serially and each lease clones
# or resets before it answers, so a busy node can still be WORKING through a queue that holds this dispatch's
# own order. Superseding it then only adds a dead lease to that queue, and a node slower than the deadline
# never reaches a live one (one slow node took 2-12 min per answer). So while the hold's own order sits
# unanswered on its node, the redispatch waits up to this bound; past it the node is presumed wedged.
UNANSWERED_LEASE_MAX_WAIT_SECONDS = 900.0


# An ack is not an observation: `session` turns true only on the node's next observe pass, so a just-acked
# healthy runner looks dead until then. Measured from the ack on the CP clock (not the node's clock or
# receipt stamp). 60s is above the worst observed node pass (~35s).
ACKED_REDISPATCH_GRACE_SECONDS = 60.0

# Clock-skew tolerance for `launch_never_claimed`: `dispatched_at` is DB `now()` while `github_synced_at`
# is the API host's clock. 2s exceeds NTP drift and is below the fastest possible claim (1-6s).
WATERMARK_SKEW_SECONDS = 2.0


def launch_never_claimed(
    *,
    acked_at: Optional[datetime],
    dispatched_at: Optional[datetime],
    github_synced_at: Optional[datetime],
    skew_seconds: float = WATERMARK_SKEW_SECONDS,
) -> bool:
    """Did the launch this hold records reach the node and then FAIL to claim its task? True when the
    launch was ACKED and the task has not moved since that dispatch began.

    One predicate behind the drain's attempt charge (`app/lease_drain.py`) and the plan's account
    rotation (`routes/fleet.py::build_dispatch_plan`). `github_synced_at` is the task's transition
    watermark: at or before the dispatch (within skew), or absent, means nobody claimed. Requires
    `acked_at` (an un-acked dispatch says nothing about the work) and `dispatched_at`; fails closed."""
    if acked_at is None or dispatched_at is None:
        return False
    if github_synced_at is None:
        return True
    return (github_synced_at - dispatched_at).total_seconds() <= skew_seconds


def dispatch_is_redispatchable(
    *,
    acked_at: Optional[datetime],
    dispatched_at: Optional[datetime],
    now: datetime,
    deadline_seconds: float = UNACKED_REDISPATCH_DEADLINE_SECONDS,
    ack_grace_seconds: float = ACKED_REDISPATCH_GRACE_SECONDS,
    lease_unanswered_since: Optional[datetime] = None,
    unanswered_max_wait_seconds: float = UNANSWERED_LEASE_MAX_WAIT_SECONDS,
    predecessor_gone: bool = True,
) -> bool:
    """May a `task`-held slot with no currently-observed live session be treated as available for
    RE-DISPATCH into the SAME slot — never as freed?

    Acked: only after `ack_grace_seconds` (see `ACKED_REDISPATCH_GRACE_SECONDS`). Never acked: only once
    `dispatched_at` is older than `deadline_seconds`; a fresh un-acked launch may still be in flight — and,
    when `lease_unanswered_since` says this dispatch's own order is still pending and never answered by its
    node, only once that order is older than `unanswered_max_wait_seconds` (see
    `UNANSWERED_LEASE_MAX_WAIT_SECONDS`). `dispatched_at is None` fails closed. `predecessor_gone=False`
    (`fleet_verdicts.predecessor_gone`: the node has not yet reported the stopped runner gone) is never
    redispatchable, whatever the clocks say."""
    if not predecessor_gone:
        return False
    if acked_at is not None:
        return (now - acked_at).total_seconds() >= ack_grace_seconds
    if dispatched_at is None:
        return False
    if (lease_unanswered_since is not None
            and (now - lease_unanswered_since).total_seconds() < unanswered_max_wait_seconds):
        return False
    return (now - dispatched_at).total_seconds() >= deadline_seconds


def enrolled_projects(projects: List[dict]) -> List[dict]:
    """The fleet rotation: every project, sorted by name."""
    return sorted(projects, key=lambda p: p.get("name") or "")


# ══ E. Pure slot-selection core ════════════════════════════════════════════════════════════════════════
# Selection only; a stale mis-selection fails closed at jaahd's execution-time gate.

def select_slot(
    roster: dict[int, dict],
    *,
    exclude: Optional[List[str]] = None,
    force_num: str | int | None = None,
    task_id: object = None,
) -> Optional[int]:
    """Pick the slot to lease over the POOL ROSTER (number → record, each with a `holder`/`task_id` — the
    live `Worktree.state`/`task_id` columns) plus the busy exclude set:
      1. a forced number wins, only if the roster declares it and it is not excluded;
      2. else the slot whose `task_id` is this task; if excluded, None (wait), never another slot;
      3. else the lowest existing non-excluded slot, preferring one no task owns.
    None when nothing is available — fail closed. `user`-held slots are invisible to every rule. It never
    allocates: the CP dictates which slots exist (the roster's own numbers, walked ascending from 1), so an
    empty roster returns None."""
    excluded = {str(e) for e in (exclude or [])}

    def is_excluded(i: object) -> bool:
        return str(i) in excluded

    # User-owned rows are filtered out before every rule.
    pickable = {i: r for i, r in roster.items() if r.get("holder") != "user"}

    if force_num is not None:
        if is_excluded(force_num):
            return None
        try:
            forced = int(force_num)
        except (TypeError, ValueError):
            return None
        return forced if forced in pickable else None

    # (2) A requirement, not a preference: falling through would abandon the tree holding the previous
    # attempt's uncommitted work to `reset_to_base`. Waiting costs one cycle (~10s).
    numbers = sorted(i for i in pickable if i >= 1)
    if task_id is not None:
        for i in numbers:
            if pickable[i].get("task_id") is not None and str(pickable[i]["task_id"]) == str(task_id):
                return None if is_excluded(i) else i
    # (3) Prefer a slot no other task's `task_id` names, so an unbound tree is not taken by a different
    # task. Only a preference: without I/O we cannot tell if that task is still live, and refusing would
    # wedge a small pool.
    fallback: Optional[int] = None
    for i in numbers:
        if is_excluded(i):
            continue
        if pickable[i].get("task_id") is None:
            return i
        if fallback is None:
            fallback = i
    return fallback


def held_slots_from_records(records: List[dict], project_id: int) -> List[str]:
    """The BUSY set — slot numbers for `project_id` whose roster record shows a non-free holder.

    Mirrors jaahd's `CommandBridge._held_slots` (`holder not in (None, "free", "main")`); any divergence
    is a dispatch stall (`ResetRefused`). Kept separate from `is_active_record`: different question
    (pickability vs concurrency). Differences from the mirror resolve safe; non-JSON fails the lease loudly."""
    held: List[str] = []
    for number, record in roster_from_records(records, project_id).items():
        if record.get("holder") not in (None, "free", "main"):
            held.append(str(number))
    return held


def roster_from_records(records: List[dict], project_id: int) -> dict[int, dict]:
    """Build the {slot_number: record} roster `select_slot` reads from the raw pool-worktree KV payloads,
    keyed by the `_project_id`/`_number` the reader injects. Only this project's slots with a positive numeric
    slot are included — the number-0 main clone is never a pool lease slot. Matched on the id, never the
    name: a node may host two orgs' projects that share a name (`uq_projects_org_name`)."""
    roster: dict[int, dict] = {}
    for record in records:
        if record.get("_project_id") != project_id:
            continue
        raw_number = record.get("_number", record.get("number"))
        try:
            number = int(raw_number)
        except (TypeError, ValueError):
            continue
        if number > 0:
            roster[number] = record
    return roster


# ══ F. Sensor-only reconcile — the decided fields ════════════════════════════════

# The node senses; functions below are pure decisions over one Observation + one roster entry. Slot
# ownership is decided in `app/slot_state.py`, never here; these derive cosmetic fields only.
# A failed probe is unknown, never a value: guards test affirmative observation (`is True`).
# Dwell inputs are node-computed ages; the CP never subtracts a node timestamp from its own `now`.
# The Title is CP-owned (`app/slot_title.py`); no sensed tmux field becomes one.

# ── Section-F thresholds (single-homed here, read at call time) ─────────────────────────────────────────

# `secs_since("")` returns this on the node — "so long ago it is past every threshold".
_UNKNOWN_AGE_SENTINEL = 999999

# ── The deterministic port grid — the COMPARISON half ───────────────────────────────────────────────────────
# The grid is resolved from the DB by `app/port_grid.py` and passed in, keeping this module pure.
# `expected_port`'s None is the tier-2 exemption; an empty map means all tier 2.


# ── R1 primitives: "was this observed?" ────────────────────────────────────────────────────────────────

def probe_observed(obs: dict, probe: str) -> bool:
    """The single test for "this probe actually ran and succeeded"; anything but literal `True` is
    unobserved (fail closed)."""
    probes = obs.get("probes")
    if not isinstance(probes, dict):
        return False
    return probes.get(probe) is True


def resolve_stack(obs: dict, prev_stack: object = "down") -> str:
    """`stack`: `none` when the worktree has no compose stack at all, else `up`/`down` from the docker
    snapshot. An unobserved probe or `stack_present` carries the prior value forward."""
    present = obs.get("stack_present")
    if present is False:
        return "none"
    if present is None:
        return str(prev_stack or "down")
    if not probe_observed(obs, "docker_ok"):
        return str(prev_stack or "down")
    return "up" if obs.get("stack_up") is True else "down"


def urls_from_ports(ports: object) -> dict:
    """`urls`: a pure derivation (no file read). Each port key maps to a role (web/api/else the key) and
    a localhost URL; later keys win on collision."""
    urls: dict[str, str] = {}
    if not isinstance(ports, dict):
        return urls
    for key, value in ports.items():
        lowered = str(key).lower()
        if re.search(r"front|web|ui", lowered):
            role = "web"
        elif re.search(r"api|back|server", lowered):
            role = "api"
        else:
            role = str(key)
        urls[role] = f"http://localhost:{value}"
    return urls


def expected_port(
    var: str,
    block: object,
    n: object,
    *,
    range_start: object,
    block_size: object,
    stride: object,
) -> Optional[int]:
    """The port grid's expected port for `var` at slot `n` of `block`:
    `range_start + block*block_size + n*stride`. None when not bandable (an unknown var is not drift).

    No grid input has a default, so an omitted keyword is a `TypeError`; a keyword passed as None
    silently answers None (fail closed)."""
    if range_start is None:
        return None
    try:
        return int(range_start) + int(block) * int(block_size) + int(n) * int(stride)
    except (TypeError, ValueError):
        return None


def port_band(
    block: object,
    n: object,
    port_vars: object = None,
    *,
    range_start_by_var: dict,
    block_size: object,
    stride: object,
) -> dict[str, int]:
    """The port grid BAND for slot `n` of `block`: `{var: port}` for every bandable var.

    Reuses `expected_port`. Empty `port_vars` falls back to every grid var; a var with no range start is
    omitted. The node only writes these values (`apply_port_band`)."""
    names = [str(v) for v in (port_vars or [])] or sorted(range_start_by_var)
    band: dict[str, int] = {}
    for var in names:
        port = expected_port(
            var, block, n,
            range_start=range_start_by_var.get(var), block_size=block_size, stride=stride,
        )
        if port is not None:
            band[var] = port
    return band

