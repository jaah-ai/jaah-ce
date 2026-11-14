"""The stage health dot (architecture §4.5, PRD D10): the `stage-health` system activity probes each stage's address,
and the two reads that draw the dot (`GET …/pipeline`, `GET …/pipeline/view`) say someone is looking.

Every `INTERVAL_SECONDS` one pass checks at most `PER_PASS` stages with an address: one whose last check is older than
`FRESH` while someone looks (`wanted_until` in the future), or older than `STALE` anyway. A check is one HTTPS GET of
the address through the control plane's SSRF guard (`http_bounded.vetted_addresses`, then a client pinned to the
vetted addresses), `PROBE_TIMEOUT_SECONDS`, redirects not followed: a status of 200–399 is `healthy`, anything else
(or no answer) `down`, with a short detail for the hover. The body is never read. An address the control plane may not
dial (not https, a private or reserved network) is never called `down`: it stays `unchecked`, its reason the detail.

The dot is an address probe for every deploy target, Argo CD's included (a settled deviation from D10: the control
plane often cannot reach an in-cluster Argo CD, and one mechanism keeps the dot's meaning the same everywhere).

A read marks its stages wanted (`want`): `wanted_until = now + WANTED_FOR`, written at most once per `TOUCH_EVERY` per
stage, so opening the board costs that one throttled write and never a probe of its own.
"""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from time import monotonic
from typing import Any, Iterable, Optional
from uuid import UUID

import httpx
from sqlalchemy import case, literal, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app import http_bounded
from app.clock import utcnow
from app.db import excluded_of
from app.entity_shape import optional_uuid
from app.models.orgs import in_live_org
from app.models.projects import Project
from app.models.releases import ReleaseStage, StageHealth

log = logging.getLogger(__name__)

ACTIVITY = "stage-health"
INTERVAL_SECONDS = 60
TIMEOUT_SECONDS = 50
MIN_GAP_SECONDS = 5
#: The most stages one pass checks (the oldest check first); the rest wait for the next pass.
PER_PASS = 200
PROBE_TIMEOUT_SECONDS = 10
#: Probes in flight at once, and how long a pass waits for them all: a probe still running then (a server that drips
#: its headers) is left unrecorded and taken again by a later pass, so the pass ends inside `TIMEOUT_SECONDS`.
WORKERS = 25
PROBE_BUDGET_SECONDS = 35
#: The most resolved addresses one probe tries (each may take a whole connect timeout).
PROBE_ADDRESSES_MAX = 2
#: A check this fresh is not redone while someone looks; one this old is redone even when nobody does.
FRESH = timedelta(seconds=60)
STALE = timedelta(minutes=10)
#: A pass's own run time: a check stamped by the previous pass is still due a minute later.
SLACK = timedelta(seconds=10)
#: How long a read keeps its stages wanted, and the least time between two such writes for one stage.
WANTED_FOR = timedelta(minutes=2)
TOUCH_EVERY = timedelta(seconds=30)
DETAIL_MAX = 200

HEALTHY, DOWN, UNCHECKED = "healthy", "down", "unchecked"


def out(stage: ReleaseStage, row: Optional[StageHealth]) -> Optional[dict[str, Any]]:
    """A stage's `health` in a read: None without an address (no dot), `unchecked` until the first check."""
    if not (stage.url or "").strip():
        return None
    if row is None:
        return {"state": UNCHECKED, "checked_at": None, "detail": None}
    return {"state": row.state, "checked_at": row.checked_at.isoformat() if row.checked_at else None,
            "detail": row.detail}


def rows_of(session: Session, stage_ids: Iterable[UUID]) -> dict[UUID, StageHealth]:
    ids = list(stage_ids)
    if not ids:
        return {}
    return {h.stage_id: h for h in session.scalars(select(StageHealth).where(StageHealth.stage_id.in_(ids)))}


def want(session: Session, stage_ids: Iterable[object], now: Optional[datetime] = None) -> None:
    """Mark the stored stages among `stage_ids` that have an address as looked at: `wanted_until = now +
    WANTED_FOR`, skipped for a stage written within `TOUCH_EVERY`. Synthesised stages (no row) are passed over.
    Flushes nothing else; the caller commits."""
    ids = sorted({stage_id for s in stage_ids if (stage_id := optional_uuid(s))})
    if not ids:
        return
    try:
        with session.begin_nested():
            _touch(session, ids, now or utcnow())
    except Exception:  # noqa: BLE001 — a best-effort mark must never fail the read that draws the page
        log.warning("stage health: could not mark %s stage(s) wanted", len(ids), exc_info=True)


def looked_at(session: Session, stage_ids: Iterable[object]) -> None:
    """A GET drew these stages: mark them wanted (`want`) and commit that mark alone. The write is derived from
    server state, the request choosing only which of the caller's own stages it reads — why a cookie GET may make
    it (`tests/test_git_access.py::test_no_cookie_authenticated_get_mutates_state`); the throttle is
    `tests/test_stage_health.py::test_reads_show_the_dot_and_mark_it_wanted_once_per_30_s_without_probing`."""
    want(session, stage_ids)
    session.commit()


def _touch(session: Session, ids: list[UUID], now: datetime) -> None:
    until = now + WANTED_FOR
    source = select(ReleaseStage.id, ReleaseStage.org_id, literal(UNCHECKED), literal(until)).where(
        ReleaseStage.id.in_(ids), ReleaseStage.url.is_not(None), ReleaseStage.url != "")
    stmt = insert(StageHealth).from_select(["stage_id", "org_id", "state", "wanted_until"], source)
    stmt = stmt.on_conflict_do_update(
        index_elements=[StageHealth.stage_id],
        set_={"wanted_until": excluded_of(StageHealth.__table__).wanted_until},
        where=or_(StageHealth.wanted_until.is_(None), StageHealth.wanted_until < until - TOUCH_EVERY),
    )
    session.execute(stmt)


# ── The pass ─────────────────────────────────────────────────────────────────────────────────────────

@dataclass
class Report:
    checked: int = 0
    healthy: int = 0
    down: int = 0
    failed: list[int] = field(default_factory=list)

    def detail(self) -> str:
        if not self.checked:
            return "no stage address due a check"
        text = f"checked {self.checked} stage address(es): {self.healthy} healthy, {self.down} down"
        if self.failed:
            text += f"; failed: {', '.join(str(p) for p in self.failed)}"
        return text


@dataclass(frozen=True)
class _Due:
    stage_id: UUID
    project_id: int
    url: str


def due(session: Session, now: datetime) -> list[_Due]:
    """The stages a pass checks, at most `PER_PASS`: the ones someone looks at first, then the never-checked, then the
    oldest check. A live org's only. `SLACK` keeps a check the previous pass stamped due a minute later."""
    checked, wanted = StageHealth.checked_at, StageHealth.wanted_until > now
    stmt = (
        select(ReleaseStage.id, ReleaseStage.project_id, ReleaseStage.url)
        .outerjoin(StageHealth, StageHealth.stage_id == ReleaseStage.id)
        .where(ReleaseStage.url.is_not(None), ReleaseStage.url != "", in_live_org(ReleaseStage.org_id))
        .where(or_(
            checked.is_(None),
            checked <= now - STALE + SLACK,
            (checked <= now - FRESH + SLACK) & wanted,
        ))
        .order_by(case((wanted, 0), else_=1), checked.asc().nulls_first(), ReleaseStage.id)
        .limit(PER_PASS)
    )
    return [_Due(sid, int(pid), str(url)) for sid, pid, url in session.execute(stmt)]


def probe(url: str) -> tuple[str, str]:
    """One check of `url`: (`healthy`|`down`|`unchecked`, a short detail for the hover). Never raises. An address the
    control plane may not dial (not https, a private or reserved network) is never called `down`: it stays
    `unchecked`, its reason the detail."""
    target = url.strip()
    if "://" not in target:
        target = f"https://{target}"
    refusal, addresses = http_bounded.vetted_addresses(target)
    if refusal is not None or not addresses:
        return UNCHECKED, _cap(f"Not checked: {refusal or 'the address did not resolve'}.")
    try:
        host = httpx.URL(target).raw_host.decode("ascii")
        with http_bounded.pinned_client(host, addresses[:PROBE_ADDRESSES_MAX], PROBE_TIMEOUT_SECONDS) as client:
            with client.stream("GET", target) as response:
                status = response.status_code
    except httpx.TimeoutException:
        return DOWN, f"No answer in {PROBE_TIMEOUT_SECONDS} s."
    except httpx.HTTPError as exc:
        return DOWN, _cap(f"Could not connect: {type(exc).__name__}.")
    except Exception:  # noqa: BLE001 — one address's fault is that stage's `down`, never the pass's
        log.exception("stage health: probe of a stage address failed")
        return DOWN, "Could not connect."
    return (HEALTHY if 200 <= status <= 399 else DOWN), f"HTTP {status}"


def run(scheduled_at: Optional[datetime] = None) -> Report:
    """One pass: list what is due fleet-wide, probe it off the database, then write each project's results in that
    project's own org and commit."""
    from app.tenancy import seam
    from app.trigger_intake import fleet_session

    now = scheduled_at or utcnow()
    with fleet_session() as session:
        work = due(session, now)
    report = Report()
    if not work:
        return report
    results = _probe_all(work)
    by_project: dict[int, list[tuple[_Due, str, str]]] = {}
    for item, (state, detail) in results:
        by_project.setdefault(item.project_id, []).append((item, state, detail))
    for project_id, rows in by_project.items():
        try:
            _write(project_id, rows, now)
        except seam.TenancyRowMissing:
            continue  # Deleted since the listing.
        except Exception:  # noqa: BLE001 — one project's fault must not stop the others
            log.exception("stage health: project %s failed", project_id)
            report.failed.append(project_id)
            continue
        report.checked += len(rows)
        report.healthy += sum(1 for _, state, _ in rows if state == HEALTHY)
        report.down += sum(1 for _, state, _ in rows if state == DOWN)
    return report


def _probe_all(work: list[_Due]) -> list[tuple[_Due, tuple[str, str]]]:
    """Each due stage's probe, `WORKERS` at a time, for at most `PROBE_BUDGET_SECONDS` in all: one not started or not
    finished by then is left unrecorded (a later pass takes it again) and the pass never waits on it."""
    deadline = monotonic() + PROBE_BUDGET_SECONDS

    def one(item: _Due) -> Optional[tuple[_Due, tuple[str, str]]]:
        if monotonic() > deadline:
            return None
        return item, probe(item.url)

    pool = ThreadPoolExecutor(max_workers=WORKERS, thread_name_prefix="stage-health")
    try:
        futures = [pool.submit(one, item) for item in work]
        done, _ = wait(futures, timeout=PROBE_BUDGET_SECONDS)
        return [r for f in futures if f in done and (r := f.result()) is not None]
    finally:
        pool.shutdown(wait=False, cancel_futures=True)


def _write(project_id: int, rows: list[tuple[_Due, str, str]], now: datetime) -> None:
    from app.db import BridgedSession
    from app.tenancy import seam

    with BridgedSession() as session, seam.detached(), seam.bind_from(session, Project, project_id):
        # In stage order (`want` locks the same way); a stage whose address changed since the listing is skipped: the
        # check was of the old one.
        for item, state, detail in sorted(rows, key=lambda r: r[0].stage_id):
            stmt = insert(StageHealth).from_select(
                ["stage_id", "org_id", "state", "checked_at", "detail"],
                select(ReleaseStage.id, ReleaseStage.org_id, literal(state), literal(now), literal(detail))
                .where(ReleaseStage.id == item.stage_id, ReleaseStage.project_id == project_id,
                       ReleaseStage.url == item.url),
            )
            excluded = excluded_of(StageHealth.__table__)
            stmt = stmt.on_conflict_do_update(
                index_elements=[StageHealth.stage_id],
                set_={"state": excluded.state, "checked_at": excluded.checked_at, "detail": excluded.detail},
            )
            session.execute(stmt)
        session.commit()


def _cap(text: str) -> str:
    return text if len(text) <= DETAIL_MAX else text[:DETAIL_MAX - 1] + "…"
