"""The `node-liveness` scheduled job: `app/node_liveness.py`'s sweep, run in the control plane's process.

An adapter only: every verdict, bound, report body and dedup key lives in `app/node_liveness.py`. The
six I/O seams call `routes/agent/task_reads.py`'s own handlers rather than new SQL, because `create_system_task`'s
dedup (indexed `dedup_key`, uniqueness guard, excluded states incl. `blocked`) must not get a second,
subtly different implementation. Handlers get `_=None` for `Depends` defaults (this caller is inside the
trust boundary) and commit their own transactions.

Runs on the cron wrapper's dedicated executor thread (`app/cron_system_activities.py`), never the event
loop: the sweep is blocking psycopg work. A failing pass is recorded as an `error` run. The rc is
returned, not raised: `EXIT_INVARIANT_VIOLATED` means the sweep worked and the fleet is unhealthy.
"""
from __future__ import annotations

import json
import logging
from typing import Optional

from sqlalchemy import select

from app import cron_alarms, edition_features, node_liveness, platform_release
from app.db import SessionLocal
from app.models.nodes import Node
from app.routes.agent import (
    auth as agent_auth,
    node_config as agent_node_config,
    releases as agent_releases,
    task_reads as agent_task_reads,
)

log = logging.getLogger("cockpit-scheduler")

# Defaults the `node-liveness` cron row's activity registers (`app/cron_system_activities.py`).
# Two passes fit inside the 600s bound the sweep asserts.
INTERVAL_SECONDS = 300.0
# Well clear of a normal pass and well under the interval, so a wedged pass is abandoned before its
# successor is due; the cron run lock never overlaps a firing with itself, so without this one hung DB
# call would end the check for the life of the process.
TIMEOUT_SECONDS = 120.0


def sweep_once(session_factory=SessionLocal) -> int:
    """Read the roster, place generations, assert, deliver. Returns the sweep's exit code.

    One session for the whole pass, so the roster read and the alarm writes see one control plane.
    `session_factory` is the test seam.
    """
    with session_factory() as session:
        seams = _Seams(session)
        try:
            rows = seams.read_nodes()
        except node_liveness.DBUnavailable as exc:
            node_liveness.warn(f"node-liveness: COULD NOT VERIFY — {exc}")
            return node_liveness.EXIT_COULD_NOT_VERIFY
        published = (
            seams.published_release() if node_liveness.any_generation_reported(rows)
            else node_liveness.PublishedRelease(None, None)
        )
        placed = _place_per_platform(rows, published.version, seams.node_os())
        if edition_features.enabled("multi_node_hosts"):  # The community edition has no Multi-Node Hosts.
            from app import vnodes

            # A lagging vnode makes its host the target (one package version per host) — `app/vnodes.py`.
            placed = vnodes.retarget_rollout(placed, vnodes.host_id_by_vnode_id(session))
        return node_liveness.sweep(
            placed,
            create_task=seams.create_task,
            find_task=seams.find_task,
            resolve_task=seams.resolve_task,
            recent_settled_task=seams.recent_settled_task,
            release_grace_active=node_liveness.release_desired_in_grace(published.updated_age),
        )


def _place_per_platform(
    rows: list[node_liveness.NodeRow], published: Optional[str], os_by_uuid: dict[str, str]
) -> list[node_liveness.NodeRow]:
    """`node_liveness.place_generations`, but each row against what its platform can install
    (`app/platform_release.py`): a Windows node is never STALE for a release with no Windows bundle."""
    return [
        row._replace(placement=node_liveness.place_generation(
            row.generation, platform_release.for_os(os_by_uuid.get(row.node_uuid or ""), published).version
        ))
        for row in rows
    ]


class _Seams:
    """The sweep's six I/O seams — two reads, four alarm calls — bound to one session.

    Grouped so a caller cannot bind some and forget others; `sweep_once` passes every one.
    """

    def __init__(self, session) -> None:
        self.session = session

    def _project_id(self) -> Optional[int]:
        """Where this pass's alarms file: the "Alarm project" setting (`cron_alarms.alarm_project_id`), read per
        call so a pass that sets it mid-way is not held to a stale answer."""
        return cron_alarms.alarm_project_id(self.session)

    # ── Reads ────────────────────────────────────────────────────────────────────

    def read_nodes(self) -> list:
        """The roster, via the route handler's own SQL, parsed by the sweep's own parser.

        Round-trips through JSON so `parse_rows` stays the one parser ("a malformed answer is no reading,
        never an empty fleet")."""
        rows = agent_node_config.node_liveness(_=None, session=self.session)
        return node_liveness.parse_rows(
            json.dumps([row.model_dump() for row in rows], default=str)
        )

    def node_os(self) -> dict[str, str]:
        """Every node's declared `os` by uuid text, so each placement is against its own platform's release."""
        return {str(node_id): os for node_id, os in self.session.execute(select(Node.id, Node.os)).all()}

    def published_release(self) -> node_liveness.PublishedRelease:
        """The release every node should run (the CP's own version) plus how long it has been served.

        A null version makes every placement `unknown` (reported, never alarmed); the age feeds the
        STALE_RELEASE grace window. Process state, not a row, so this seam touches no DB."""
        desired = agent_releases.cp_release_desired()
        version = getattr(desired, "version", None) or None
        if version is None:
            return node_liveness.PublishedRelease(None, None)
        return node_liveness.PublishedRelease(version, getattr(desired, "updated_age", None))

    # ── Alarm delivery ───────────────────────────────────────────────────────────

    def create_task(self, **kwargs) -> dict:
        """`POST /api/agent/tasks/system`, in-process. Returns the `{id, created}` shape the sweep reads.

        `SystemTaskIn(**kwargs)` so a key rename on either side is a pydantic error rather than a
        silently dropped `dedup_key`. `host` is the affirmative producer declaration the task-creation
        guard requires (`routes/agent/auth.py::_guard_task_creating_caller`).

        The row is the Alarm project's org's, whichever tenant's nodes it names
        (`routes/agent/task_reads.py::create_system_task`)."""
        project_id = self._project_id()
        if project_id is None:
            # Raised into the sweep's delivery guard, which prints the whole report as ALARM NOT DELIVERED.
            log.error("node-liveness: alarm %s NOT filed — %s", kwargs.get("dedup_key"), cron_alarms.unset_reason())
            raise RuntimeError(cron_alarms.unset_reason())
        out = agent_task_reads.create_system_task(
            body=agent_task_reads.SystemTaskIn(**kwargs, project_id=project_id),
            x_agent_task_id=agent_auth.HOST_PRODUCER_CALLER,
            _=None,
            session=self.session,
        )
        return {"id": out.id, "created": out.created}

    def find_task(self, **kwargs) -> Optional[int]:
        """`GET /api/agent/tasks/system/open` — the id of the row THIS dedup_key opened, or None."""
        project_id = self._project_id()
        if project_id is None:
            return None  # nothing can be open where nothing was filed
        return agent_task_reads.find_open_system_task(
            persona=kwargs["persona"], project_id=project_id, dedup_key=kwargs.get("dedup_key"),
            _=None, session=self.session,
        ).id

    def recent_settled_task(self, **kwargs) -> Optional[dict]:
        """The re-arm cooldown's read — the row this key's card settled as, if it settled recently.

        In-process only, no route; an unbound seam means "suppress nothing"
        (`node_liveness._rearm_suppressed`), so it is bound here, the only caller that can churn."""
        project_id = self._project_id()
        if project_id is None:
            return None
        return agent_task_reads.recently_settled_system_task(
            self.session,
            persona=kwargs["persona"],
            project_id=project_id,
            dedup_key=kwargs["dedup_key"],
            within_seconds=kwargs["within_seconds"],
            cleared_prefix=kwargs["cleared_prefix"],
        )

    def resolve_task(self, task_id: int, outcome_summary: str = "resolved") -> dict:
        """`POST /api/agent/tasks/system/{id}/resolve` — closes a cleared condition's Task."""
        out = agent_task_reads.resolve_system_task(
            task_id=task_id,
            body=agent_task_reads.ResolveSystemTaskIn(outcome_summary=outcome_summary),
            _=None,
            session=self.session,
        )
        return {"resolved": out.resolved}
