"""The `production-qa` system activity (`cron_system_activities`, every 60 s) and its job registry: each owner
registers its periodic job by name (`register`), and `run_pass` runs every registered job in its own transaction,
catching and logging per job, so one job's fault never stops the rest.

A job's session reads every org (`trigger_intake.fleet_session`), so a job may only LIST there: every row it writes
is the subject's own — written under that org's binding (`trigger_intake.trigger_session`, `seam.bind_from`) or
stamped with the `org_id` read off the subject row itself, never one carried from another row."""
from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional

from sqlalchemy.orm import Session
from app.clock import utcnow

log = logging.getLogger("cockpit-production-qa")

Job = Callable[[Session, datetime], None]

_JOBS: dict[str, Job] = {}

INTERVAL_SECONDS = 60
# Every registered job together; each job bounds its own work well inside this.
TIMEOUT_SECONDS = 50
MIN_GAP_SECONDS = 5


def register(name: str, fn: Job) -> None:
    """Register one job under `name`; registering the same name again replaces it."""
    _JOBS[name] = fn


@dataclass
class PassReport:
    ran: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)

    @property
    def acted(self) -> bool:
        """Only a pass with a failed job is kept (`retention_mode = acted`): a clean minute-by-minute pass is a noop
        whose detail still names how many jobs ran."""
        return bool(self.failed)

    def summary(self) -> str:
        if not (self.ran or self.failed):
            return "no production QA job registered"
        text = f"{len(self.ran)} job(s) ran"
        return f"{text}; failed: {', '.join(self.failed)}" if self.failed else text


def run_pass(scheduled_at: Optional[datetime] = None) -> PassReport:
    """Run every registered job once, each in its own fleet-wide session (a job reads every org's deploys and
    incidents) and commit; a job that raises is rolled back, logged and named in the report."""
    from app.production_qa import load_owners
    from app.trigger_intake import fleet_session

    load_owners()
    at = scheduled_at or utcnow()
    report = PassReport()
    for name, job in list(_JOBS.items()):
        with fleet_session() as session:
            try:
                job(session, at)
                session.commit()
            except Exception:  # noqa: BLE001 — one job's fault must not stop the others
                session.rollback()
                log.exception("production-qa: job %s failed", name)
                report.failed.append(name)
                continue
        report.ran.append(name)
    return report
