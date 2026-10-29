"""Node log history: what a node's shipped journal says HAPPENED (`POST /api/agent/node-logs`), kept as short
lists on the node row. `app/node_log_findings.py` keeps what is WRONG now; this module keeps the record.

The single home for three folds, all run from `node_log_findings.record` (the one call the ingest route makes):

* `nodes.update_history` — the updater's outcomes, newest first, at most `MAX_UPDATES`:
  `[{"at", "version", "outcome": "converged"|"failed", "detail"}]`. The node's update script logs
  `jaahd-update: converged to <package> <version>` on success; a failure is systemd's own
  `<updater unit>: Failed with result '…'` line (the script's `die` writes stderr, which journald files at the
  unit's default level, so a priority is rarely there to read), or a `jaahd-update:` line at priority ≤ 3.
  The failed entry's detail is the updater's last own line before systemd's (the `die` sentence), when one
  arrived in the same batch. A Windows node ships its updater's `update.log` under the same unit
  (`apps/jaahd/node_logs.py::LOG_FILES`): its `jaah-update.ps1` logs `jaah-update: updated
  to <version>` on success, and a failure is the service host's `jaah-update exited <n>` (n ≠ 0) or `ran past`
  note (`JaahService.cs`), its detail again the script's last own line.
* `nodes.gha_recent_jobs` — each host GHA runner unit's last `MAX_JOBS_PER_UNIT` jobs, newest first:
  `{"<unit>": [{"job", "started_at", "ended_at", "result", "shutdown_signal"}]}`, from the runner's own
  stdout (`Running job: <name>`, `Job <name> completed with result: <Result>`) and systemd's lines about the
  unit (stopped or killed while a job ran: `shutdown_signal`; the runner gone with no completion:
  `result: "interrupted"`). Served by no route here; `recent_jobs_for` is the accessor. At most
  `models.MAX_GHA_REPORTED_SLOTS` units are kept, those whose newest job started last: the unit name is the
  node's own text, and a host's runner pool never reports more slots than that (the pool's `observed` is not
  read here, as the fold sees the node row alone).

* `nodes.recent_errors` — the node's newest `MAX_RECENT_ERRORS` journal lines at priority ≤ `ERROR_PRIORITY`
  (warning or worse), newest first: `[{"at", "unit", "message"}]`. The kernel's lines and the GHA runner units'
  are not kept: neither is a Jaah launch or host step, and the kernel names any process on the machine. What it
  is for: why a runner's session ENDED when nothing reported it (`vanished`, `crashed`, …) — the launch or host
  error lines just before the end (`end_lines`), served on the task's Activity row and Audit › Sessions, so the
  diagnosis needs no SSH.

All three are reassigned as new objects (JSONB change tracking is by identity). A re-sent batch (the node
re-sends at most one) is recognised by its identical timestamps and folds to no change.

`end_lines` is the one serving rule for `recent_errors` (tenancy, window, redaction, wording): a shared node
serves several orgs and its journal can name another tenant's work, so a line is served only when the node's
org is the session's own.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable, Optional, Protocol

from app import agent_sessions, runner_terminations
from app.clock import as_utc, parse_utc
from app.fleet_verdicts import redact_node_detail
from app.models.node_groups import MAX_GHA_REPORTED_SLOTS

MAX_UPDATES = 20
MAX_JOBS_PER_UNIT = 5
MAX_DETAIL_CHARS = 300
MAX_RECENT_ERRORS = 50
# journald's `warning`: syslog priorities run 0 (emerg) … 7 (debug).
ERROR_PRIORITY = 4
# What `end_lines` serves for one ended session: the newest lines from `END_LOOKBACK` before its end (not before
# it started) to `END_LOOKAHEAD` after — the node ships its journal on a cadence, and the CP may conclude a
# `vanished` end a sweep after the runner died, so the cause sits before the stamp, its echo just after.
END_LINES_LIMIT = 5
END_LOOKBACK = timedelta(minutes=10)
END_LOOKAHEAD = timedelta(minutes=2)
_UNIT_CHARS = 128
KERNEL_UNIT = "kernel"

# The ends a node's journal may explain: a session (`end_reason`, `agent_sessions.end_reasons()`) or a termination
# note (`cause`, `runner_terminations.CAUSES`) whose runner stopped without a reason of its own. Spanning both
# vocabularies deliberately: a break cause is both. Not the runner's own verdicts (a usage limit, expired
# credentials: the transcript already says why) nor an operator's or the CP's decisions.
NODE_EXPLAINED_ENDS: frozenset[str] = frozenset({
    "timeout", "api_error", "crashed", runner_terminations.CAUSE_VANISHED, "unknown",
    runner_terminations.CAUSE_LAUNCH_UNANNOUNCED, runner_terminations.CAUSE_NODE_FAULT,
    agent_sessions.END_REASON_RECONSTRUCTED, agent_sessions.END_REASON_ABANDONED,
})

CONVERGED = "converged"
FAILED = "failed"
INTERRUPTED = "interrupted"

# `apps/jaahd/node_paths.py::_UNITS["update"]`'s current name (not importable from here). The pre-rename
# updater is not read: a node still on it logs its next converge under this name once it lands.
UPDATER_UNITS = frozenset({"jaahd-update.service"})
GHA_UNIT_PREFIX = "actions.runner."

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)

# One failure is often two lines (a `die` at priority ≤ 3, then systemd's); within this they are one entry.
_SAME_FAILURE = timedelta(minutes=5)

# The text is the node's own: every scan is over a bounded prefix with capped captures (as `node_log_findings`).
_SCAN_CHARS = 2048
_UPDATER_PREFIX = r"jaahd-update: "
_CONVERGED_RE = re.compile(rf"^{_UPDATER_PREFIX}converged to (\S{{1,64}}) ([A-Za-z0-9.+~-]{{1,64}})\s*$")
_UPDATER_LINE_RE = re.compile(rf"^{_UPDATER_PREFIX}")
_NAMED_VERSION_RE = re.compile(r"\bjaah=([A-Za-z0-9.+~-]{1,64})")
_FAILED_MARK = "Failed with result"
# The Windows updater's lines, after the service host's optional local-time stamp (`JaahService.cs`'s `Note`).
_WIN_STAMP = r"(?:\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\S{0,16}\s+)?"
_WIN_STAMP_RE = re.compile(rf"^{_WIN_STAMP}")
_WIN_UPDATER_LINE_RE = re.compile(rf"^{_WIN_STAMP}jaah-update: ")
_WIN_CONVERGED_RE = re.compile(rf"^{_WIN_STAMP}jaah-update: updated to ([A-Za-z0-9.+~-]{{1,64}})\s*$")
_WIN_FAILED_RE = re.compile(r"jaah-service\[jaah-update\]: jaah-update (?:exited (-?\d{1,10})|ran past \d{1,6} minutes)")
# The release a Windows failure names: the archive it fetched, the version it switched to, or the one it wanted.
_WIN_VERSION_RE = re.compile(
    r"\bjaah-([0-9][A-Za-z0-9.+~-]{0,63}?)-windows-x64\.zip\b"
    r"|\bcome up on ([0-9][A-Za-z0-9.+~-]{0,63})\b|\bdesired='([0-9][A-Za-z0-9.+~-]{0,63})'")

# The runner's stdout: `2026-10-17 12:00:00Z: Running job: build`.
_RUNNER_TS_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2})Z: ")
_JOB_START_RE = re.compile(r"Running job: (.{1,256})$")
_JOB_DONE_RE = re.compile(r"Job (.{1,256}) completed with result: (\w{1,32})")
# systemd about the unit: a stop or kill is coming (signal only), or the runner is gone (closes an open job).
_SIGNAL_MARKS = ("A process of this unit has been killed by the OOM killer",)
_GONE_MARKS = ("Main process exited", "Deactivated", "Failed with result")


class _Entry(Protocol):
    ts: datetime
    message: str


def fold(row: Any, entries: Iterable[_Entry], now: Optional[datetime] = None) -> bool:
    """Fold `entries` (oldest first, as shipped) into `row.update_history`, `row.gha_recent_jobs` and
    `row.recent_errors`; whether any changed. A line stamped ahead of `now` is read as now."""
    ceiling = as_utc(now) if now is not None else datetime.now(timezone.utc)
    updates = list(getattr(row, "update_history", None) or [])
    jobs = {unit: list(items) for unit, items in (getattr(row, "gha_recent_jobs", None) or {}).items()}
    errors = list(getattr(row, "recent_errors", None) or [])
    seen_errors = {(e.get("at"), e.get("message")) for e in errors if isinstance(e, dict)}
    updates_changed = jobs_changed = errors_changed = False
    last_updater_line: Optional[tuple[datetime, str]] = None
    for entry in entries:
        unit = str(getattr(entry, "unit", "") or "")
        at = min(as_utc(entry.ts), ceiling)
        text = (entry.message or "")[:_SCAN_CHARS]
        priority = getattr(entry, "priority", None)
        if unit in UPDATER_UNITS:
            item = _update_item(text, at, priority, last_updater_line)
            if _UPDATER_LINE_RE.match(text) or _WIN_UPDATER_LINE_RE.match(text):
                last_updater_line = (at, text)
            if item is not None and _add_update(updates, item):
                updates_changed = True
        elif unit.startswith(GHA_UNIT_PREFIX):
            unit_jobs = jobs.setdefault(unit, [])
            if _fold_job_line(unit_jobs, text, at):
                jobs_changed = True
        if isinstance(priority, int) and priority <= ERROR_PRIORITY and unit != KERNEL_UNIT \
                and not unit.startswith(GHA_UNIT_PREFIX):
            key = (at.isoformat(), text.rstrip()[:MAX_DETAIL_CHARS])
            if key[1] and key not in seen_errors:
                seen_errors.add(key)
                # Prepended, so a batch's later line sorts first among lines sharing one stamp.
                errors.insert(0, {"at": key[0], "unit": unit[:_UNIT_CHARS], "message": key[1]})
                errors_changed = True
    if updates_changed:
        row.update_history = updates[:MAX_UPDATES]
    if jobs_changed:
        kept = sorted(((unit, items[:MAX_JOBS_PER_UNIT]) for unit, items in jobs.items() if items),
                      key=lambda pair: parse_utc(pair[1][0].get("started_at")) or _EPOCH, reverse=True)
        row.gha_recent_jobs = dict(kept[:MAX_GHA_REPORTED_SLOTS])
    if errors_changed:
        errors.sort(key=lambda e: parse_utc(e.get("at")) or _EPOCH, reverse=True)
        row.recent_errors = errors[:MAX_RECENT_ERRORS]
    return updates_changed or jobs_changed or errors_changed


# ── Updates ──────────────────────────────────────────────────────────────────────


def _update_item(
    text: str, at: datetime, priority: Optional[int], last_line: Optional[tuple[datetime, str]]
) -> Optional[dict]:
    converged = _CONVERGED_RE.match(text)
    if converged:
        return {"at": at.isoformat(), "version": converged.group(2), "outcome": CONVERGED,
                "detail": text[:MAX_DETAIL_CHARS]}
    if _FAILED_MARK in text:
        detail = text
        if last_line is not None and at - last_line[0] <= _SAME_FAILURE:
            detail = last_line[1]
        return _failed(at, detail, systemd=True)
    if _UPDATER_LINE_RE.match(text) and isinstance(priority, int) and priority <= 3:
        return _failed(at, text, systemd=False)
    return _windows_update_item(text, at, last_line)


def _windows_update_item(
    text: str, at: datetime, last_line: Optional[tuple[datetime, str]]
) -> Optional[dict]:
    converged = _WIN_CONVERGED_RE.match(text)
    if converged:
        return {"at": at.isoformat(), "version": converged.group(1), "outcome": CONVERGED,
                "detail": _WIN_STAMP_RE.sub("", text)[:MAX_DETAIL_CHARS]}
    failed = _WIN_FAILED_RE.search(text)
    if failed is None or failed.group(1) == "0":
        return None
    # `abs`: a host older than the stamped script lines leaves them the shipping time, after the exit note's.
    detail = last_line[1] if last_line is not None and abs(at - last_line[0]) <= _SAME_FAILURE else text
    return _failed(at, _WIN_STAMP_RE.sub("", detail), systemd=False)


def _failed(at: datetime, detail: str, *, systemd: bool) -> dict:
    named = _NAMED_VERSION_RE.search(detail)
    version = named.group(1) if named else None
    if version is None:
        windows = _WIN_VERSION_RE.search(detail)
        version = next((group for group in windows.groups() if group), None) if windows else None
    return {"at": at.isoformat(), "version": version, "outcome": FAILED,
            "detail": detail[:MAX_DETAIL_CHARS], "_systemd": systemd}


def _add_update(updates: list, item: dict) -> bool:
    """Insert `item` in newest-first order; False for a re-sent line or the second half of one failure."""
    systemd = item.pop("_systemd", False)
    at = parse_utc(item["at"])
    for prior in updates:
        if prior.get("at") == item["at"] and prior.get("outcome") == item["outcome"]:
            return False
    if item["outcome"] == FAILED and systemd and updates:
        newest = updates[0]
        newest_at = parse_utc(newest.get("at"))
        if newest.get("outcome") == FAILED and newest_at is not None and at is not None \
                and timedelta(0) <= at - newest_at <= _SAME_FAILURE:
            return False  # the `die` line already recorded this failure, with the better detail
    index = 0
    while index < len(updates) and at is not None and (parse_utc(updates[index].get("at")) or at) > at:
        index += 1
    updates.insert(index, item)
    return True


# ── GHA runner jobs ──────────────────────────────────────────────────────────────


def _runner_time(text: str, at: datetime) -> str:
    """The runner's own stdout timestamp, else the journal entry's."""
    stamp = _RUNNER_TS_RE.match(text)
    if stamp:
        try:
            return datetime.fromisoformat(stamp.group(1).replace(" ", "T")).replace(tzinfo=timezone.utc).isoformat()
        except ValueError:
            pass
    return at.isoformat()


def _fold_job_line(unit_jobs: list, text: str, at: datetime) -> bool:
    """Apply one line from a runner unit to its jobs (newest first); whether anything changed."""
    line = text.rstrip()
    start = _JOB_START_RE.search(line)
    if start:
        name, started_at = start.group(1), _runner_time(line, at)
        if any(j.get("job") == name and j.get("started_at") == started_at for j in unit_jobs):
            return False  # re-sent
        for index, job in enumerate(unit_jobs):
            if job.get("ended_at") is None:
                # The runner runs one job at a time: an open one whose end never arrived ended by now, outcome unknown.
                unit_jobs[index] = dict(job, ended_at=started_at)
        unit_jobs.insert(0, {"job": name, "started_at": started_at, "ended_at": None, "result": None,
                             "shutdown_signal": False})
        return True
    done = _JOB_DONE_RE.search(line)
    if done:
        name, result, ended_at = done.group(1), done.group(2), _runner_time(line, at)
        for index, job in enumerate(unit_jobs):
            if job.get("job") == name and job.get("ended_at") is None:
                # The flag stays: the job finished, but under a stop or kill.
                unit_jobs[index] = dict(job, ended_at=ended_at, result=result)
                return True
        return False  # its start was never seen (or this is a re-send): no start time to serve
    gone = any(mark in line for mark in _GONE_MARKS)
    if gone or line.startswith("Stopping ") or any(mark in line for mark in _SIGNAL_MARKS):
        changed = False
        for index, job in enumerate(unit_jobs):
            if job.get("ended_at") is not None:
                continue
            updated = dict(job, shutdown_signal=True)
            if gone:
                updated.update(ended_at=at.isoformat(), result=INTERRUPTED)
            if updated != job:
                unit_jobs[index] = updated
                changed = True
        return changed
    return False


def recent_jobs_for(node: Any, unit_names: Iterable[str]) -> dict[str, list[dict]]:
    """`{unit: [job, …]}` (newest first, at most `MAX_JOBS_PER_UNIT`) for each of `unit_names`; `[]` for a unit
    with no job seen."""
    recorded = getattr(node, "gha_recent_jobs", None) or {}
    return {unit: list(recorded.get(unit) or [])[:MAX_JOBS_PER_UNIT] for unit in unit_names}


# ── Recent errors, served at a session's end ─────────────────────────────────────


def errors_near(
    recent_errors: Any, start: Optional[datetime], end: datetime, *, limit: int = END_LINES_LIMIT
) -> list[dict]:
    """The newest ≤ `limit` items of `recent_errors` (newest first, as stored) stamped within
    [`end` - `END_LOOKBACK` (not before `start`), `end` + `END_LOOKAHEAD`]; malformed items skipped."""
    end = as_utc(end)
    low = end - END_LOOKBACK
    if start is not None and as_utc(start) > low:
        low = as_utc(start)
    high = end + END_LOOKAHEAD
    out: list[dict] = []
    for item in recent_errors if isinstance(recent_errors, list) else ():
        at = parse_utc(item.get("at")) if isinstance(item, dict) else None
        if at is not None and low <= at <= high:
            out.append(item)
            if len(out) >= limit:
                break
    return out


def end_lines(
    *, node_org: Any, recent_errors: Any, owner_org: Any, start: Optional[datetime], end: Optional[datetime],
) -> list[str]:
    """What a session's node logged as it ended, for a reader of the session's (or its task's) org: each
    `"HH:MM:SSZ <unit>: <message>"` (UTC; compact, as the lines are evidence beside a timestamped row), newest
    first, the message run through `fleet_verdicts.redact_node_detail`. `[]`
    unless the node is the owner's own org's — a shared node's journal can name another tenant's work — and
    for an end not yet known."""
    if end is None or node_org is None or node_org != owner_org:
        return []
    lines: list[str] = []
    for item in errors_near(recent_errors, start, end):
        message = redact_node_detail(str(item.get("message") or ""), limit=MAX_DETAIL_CHARS)
        if not message:
            continue
        unit = redact_node_detail(str(item.get("unit") or ""), limit=_UNIT_CHARS) or "?"
        lines.append(f"{parse_utc(item['at']).astimezone(timezone.utc):%H:%M:%S}Z {unit}: {message}")
    return lines
