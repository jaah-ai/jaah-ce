"""The system cadences — one thin trigger per control-plane job.

Each `_invoke_*` calls the job module's existing synchronous body; cron owns only the schedule, the
enabled switch and the run record. This is the only place a control-plane cadence is declared.
Done-side work is not a cadence: it is enqueued per task by the `done` write itself
(`app/durable/task_done.py`).

The activity type is the wake key; `crons.name` is display-only (`app/task_wake.py`). Every
descriptor is `creatable`: `is_system` marks machinery, not a permission. Each trigger derives
`acted`/`noop` from evidence the job already produces (a summary, an exit code, a marker column),
because most rows seed `retention_mode = 'acted'`, where a constant answer is useless."""
from __future__ import annotations

import importlib
import logging
import re
from datetime import datetime
from collections.abc import Callable
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from app.cron_activities import (
    NO_PARAMS,
    ActivityContext,
    ActivityDescriptor,
    ActivityResult,
    DefaultSchedule,
    register,
)
from app import edition, edition_features
from app.crons import SCHEDULE_KIND_CRON, SCHEDULE_KIND_INTERVAL

log = logging.getLogger("cockpit-cron")

# The activity types, spelled once. Byte-stable: the key `app/task_wake.py` wakes on and what an
# operator matches old log lines against.
NODE_LIVENESS = "node-liveness"
TASK_PIPELINE = "task-pipeline"
LEASE_DRAIN = "lease-drain"
# Dynamic node groups: the control plane holds each at the capacity an operator writes.
NODE_GROUPS = "node-groups"
# The SSM instance-profile backfill for control-plane-launched nodes (`app/node_instance_profile.py`).
NODE_SSM = "node-ssm"
# The headscale edge's emergency allowlist (`app/headscale_allowlist.py`).
HEADSCALE_ALLOWLIST = "headscale-allowlist"
# System inbound mail off the SES inbound queue into `inbound_emails` and the email triggers (`app/ses_inbound.py`).
SES_INBOUND = "ses-inbound"
# SES's delivery events into the delivery log and the suppression list (`app/ses_events.py`).
SES_EVENTS = "ses-events"
# Each org's service-account Gmail's new mail into its email triggers (`app/tenant_gmail/receive.py`).
TENANT_GMAIL_SYNC = "tenant-gmail-sync"
# The support agent's knowledge base reconcile (`app/support_kb/index.py`).
SUPPORT_KB_INDEX = "support-kb-index"
# The jaah.ai crawl into the same knowledge base, `site` rows only (`app/support_kb/site_crawl.py`).
SUPPORT_KB_SITE = "support-kb-site"
# Lessons drafted from solved tickets, and the approved ones into the same knowledge base (`app/support_kb/lessons.py`).
SUPPORT_LESSONS = "support-lessons"
# The nightly security-event archive export (`app/security_archive.py`).
SECURITY_EVENT_EXPORT = "security-event-export"
# The CloudWatch Embedded Metric Format emitter the baseline alarms read (`app/metrics_emf.py`).
METRICS_EMIT = "metrics-emit"
# Stripe billing: the pulled event stream and the nightly re-read of every mirror (`app/billing/events.py`).
BILLING_EVENTS = "billing-events"
BILLING_RECONCILE = "billing-reconcile"
BILLING_CARD_EXPIRY = "billing-card-expiry"
# The platform Bedrock account's health probes and deny-policy repair (`app/bedrock/probes.py`).
BEDROCK_HEALTH = "bedrock-health"
# Jaah token billing (`app/token_billing/`): the nightly ledger audit.
TOKEN_BILLING_AUDIT = "token-billing-audit"
# Jaah sessions (`app/jaah_routing/sessions.py`): end those that never asked for credentials.
JAAH_SESSION_REAP = "jaah-session-reap"
# Jaah metering's AWS correction from the model-invocation log (`app/token_billing/metering.py`).
BEDROCK_USAGE_CORRECT = "bedrock-usage-correct"
# The daily Jaah-metered-cost-versus-AWS check (`app/token_billing/reconcile.py`).
BEDROCK_RECONCILE = "bedrock-reconcile"
# Add-node orders that cannot finish (`app/node_orders.py::sweep`).
NODE_ORDER_SWEEP = "node-order-sweep"
# Monthly hostings through past due, stopped and terminated (`app/hosting_lifecycle.py::run`).
HOSTING_LIFECYCLE = "rental-lifecycle"
# The nodes and Jaah AI credit an org's plan includes (`app/plan_bundles.py`); woken by `plan_terms.wake_bundles`.
PLAN_BUNDLES = "plan-bundles"
# An org whose plan ended onto that plan's then plan (`app/plan_rollover.py`).
PLAN_ROLLOVER = "plan-rollover"
# The 80% and 100% notices of each org's monthly budgets (`app/budgets.py::alert`).
BUDGET_ALERTS = "budget-alerts"
ALARM_RELAY = "alarm-relay"
# Portfolio Compliance (`app/governance/scans.py`): resolving auditor runs that died, and the daily scan.
COMPLIANCE_RECONCILE = "compliance-reconcile"
COMPLIANCE_DAILY = "compliance-daily"
# Event-driven Task triggers (`app/trigger_intake.py`): the one processor of `trigger_deliveries`. The value is
# `app/triggers.py::INTAKE_ACTIVITY`, spelled there for the ingest route and here for the registry.
TRIGGER_INTAKE = "trigger-intake"
# The daily trigger digest and idle auto-pause (`app/trigger_notices.py`); `app/triggers.py::DIGEST_ACTIVITY`.
TRIGGER_DIGEST = "trigger-digest"
# The compliance manifest backstop: start the seal of a done task the done workflow missed
# (`app/durable/compliance_seal.py::sweep`).
COMPLIANCE_SEAL_SWEEP = "compliance-seal-sweep"
# Live Cast's evaluation backstop: new rows and stale verdicts of every published project
# (`app/durable/live_cast.py::sweep`).
LIVE_CAST_SWEEP = "live-cast-sweep"
# The operator dashboard's hourly ops history (`app/ops_snapshots.py`).
OPS_SNAPSHOT = "ops-snapshot"
# The operator dashboard's weekly AI quality review (`app/operator_dashboard/review.py`).
OUTCOME_REVIEW = "outcome-review"
# Project memory's curation pass (`app/project_memory/curate.py`): settle mining, distillation, promotion.
MEMORY_CURATE = "memory-curate"
# Project memory's nightly pass (`app/project_memory/nightly.py`): decay, consolidation, trust, slice, graduation.
MEMORY_NIGHTLY = "memory-nightly"
# The operator dashboard's weekly AI insight digest (`app/operator_dashboard/digest.py`).
INSIGHT_DIGEST = "insight-digest"
# The SOC 2 evidence collectors (`app/compliance_evidence/collectors.py`): one daily cron per collector, literal
# here because the tenancy census reads each `_system(...)` call's first argument and refuses a computed one.
EVIDENCE_SECURITY_ARCHIVE = "evidence-security-archive"
EVIDENCE_RETENTION_SWEEP = "evidence-retention-sweep"
EVIDENCE_ACCESS_EVENTS = "evidence-access-events"
EVIDENCE_MFA_POSTURE = "evidence-mfa-posture"
EVIDENCE_GITHUB_SETTINGS = "evidence-github-settings"
EVIDENCE_DEPENDABOT = "evidence-dependabot"
EVIDENCE_CLOUDWATCH_ALARMS = "evidence-cloudwatch-alarms"
EVIDENCE_PAGING_SUBSCRIPTIONS = "evidence-paging-subscriptions"
EVIDENCE_SECURITYHUB = "evidence-securityhub"
EVIDENCE_EDGE_TLS = "evidence-edge-tls"
EVIDENCE_BACKUPS = "evidence-backups"
EVIDENCE_STORAGE_LIFECYCLE = "evidence-storage-lifecycle"
EVIDENCE_ECR_SCAN = "evidence-ecr-scan"
# NodeLink provisioning: finishes every requested NodeLink with nobody on its page (`app/nodelinks.py`).
NODELINK_PROVISION = "nodelink-provision"
# The control plane's own ECS tasks, kept past ECS's hour (`app/cp_ecs.py`).
CP_ECS_RECORD = "cp-ecs-record"
# Each org's new activity events, POSTed to its SIEM destination (`app/siem_export.py`).
SIEM_EXPORT = "siem-export"
# Jaah API keys' expiry reminders, expiry records and disabled-owner backstop (`app/api_keys.py::sweep`).
API_KEYS_HOURLY = "api-keys-hourly"
# Staged comment images no post or edit claimed within a day, deleted with their bytes
# (`app/comment_service.py::reap_unclaimed_images`).
COMMENT_IMAGE_REAP = "comment-image-reap"

# The order the seeding revision writes them and a test asserts (the picker orders by label).
# Org partitioning (spec §4.6): only `lease-drain` partitions (`routes/fleet.py::build_dispatch_plan`
# reads the asking node's org budget). `node-liveness` files one alarm per condition fleet-wide, into the
# Alarm project's own org; `task-pipeline` and `node-groups` are fleet-wide, their filings stamped;
# `retention_sweep` files nothing.
SYSTEM_ACTIVITY_TYPES: tuple[str, ...] = (
    NODE_LIVENESS, TASK_PIPELINE, LEASE_DRAIN, NODE_GROUPS, HEADSCALE_ALLOWLIST, SES_INBOUND,
    SUPPORT_KB_INDEX, SECURITY_EVENT_EXPORT, METRICS_EMIT, BILLING_EVENTS, BILLING_RECONCILE,
    BILLING_CARD_EXPIRY, BEDROCK_HEALTH,
    TOKEN_BILLING_AUDIT, JAAH_SESSION_REAP, BEDROCK_USAGE_CORRECT, BEDROCK_RECONCILE,
    NODE_ORDER_SWEEP, HOSTING_LIFECYCLE, SUPPORT_KB_SITE, PLAN_BUNDLES, ALARM_RELAY, COMPLIANCE_RECONCILE,
    COMPLIANCE_DAILY, TRIGGER_INTAKE, OPS_SNAPSHOT, OUTCOME_REVIEW, MEMORY_CURATE,
    INSIGHT_DIGEST, MEMORY_NIGHTLY, TRIGGER_DIGEST, NODE_SSM, COMPLIANCE_SEAL_SWEEP,
    LIVE_CAST_SWEEP, EVIDENCE_SECURITY_ARCHIVE, EVIDENCE_RETENTION_SWEEP, EVIDENCE_ACCESS_EVENTS,
    EVIDENCE_MFA_POSTURE, EVIDENCE_GITHUB_SETTINGS, EVIDENCE_DEPENDABOT, EVIDENCE_CLOUDWATCH_ALARMS,
    EVIDENCE_PAGING_SUBSCRIPTIONS, EVIDENCE_SECURITYHUB, EVIDENCE_EDGE_TLS, EVIDENCE_BACKUPS,
    EVIDENCE_STORAGE_LIFECYCLE, EVIDENCE_ECR_SCAN, NODELINK_PROVISION, CP_ECS_RECORD, PLAN_ROLLOVER, BUDGET_ALERTS,
    SIEM_EXPORT, SUPPORT_LESSONS, API_KEYS_HOURLY, SES_EVENTS, TENANT_GMAIL_SYNC, COMMENT_IMAGE_REAP,
)

# The cadences the community edition registers (`edition.IS_CE`): dispatch, the task pipeline, API keys and the
# comment-image reap.
# Every other one serves a cloud surface (billing, SES and the mail budget alerts send, Bedrock, CloudWatch, headscale,
# node groups and orders, NodeLink, project memory, the support knowledge base and the Live Cast judge on its provider,
# SIEM, compliance and its SOC 2 evidence, ECS, the operator dashboard, and event triggers, which nothing reaches on
# loopback — `routes/triggers.py::NO_EVENTS_LOCALLY`) and is not registered there; `app/ce_bootstrap.py` deletes its seeded row.
CE_SYSTEM_ACTIVITY_TYPES: frozenset[str] = frozenset({
    NODE_LIVENESS, TASK_PIPELINE, LEASE_DRAIN, API_KEYS_HOURLY, COMMENT_IMAGE_REAP,
})

# The floor between two firings for the two task-wakeable cadences: 5s, a cost statement about each
# pass (real GitHub round trips).
WAKE_MIN_GAP_SECONDS = 5.0


# ── Reading what a pass did, without touching the job body ────────────────────────────────────────────


def _db_now(session: Session) -> datetime:
    """The database's own clock, and then the transaction is ended.

    The rollback matters: holding the snapshot and pooled connection across a multi-second pass would
    starve the pool and read `tasks` as it was before the pass.

    Measure a column with the clock that stamps it: `updated_at` is stamped by the DB trigger's
    `now()`. The api (ECS) and database (RDS) clocks skew, so a Python-stamped column needs a Python
    clock, or real passes read as `noop`.
    """
    at = session.execute(text("SELECT now()")).scalar_one()
    session.rollback()
    return at


_Invoke = Callable[[datetime, ActivityContext], ActivityResult]


def _outcome(
    report: Any,
    *,
    unconfigured: str | None = None,
    acted: Callable[[Any], Any] = lambda report: report.acted,
    detail: Callable[[Any], str] = lambda report: report.summary(),
) -> ActivityResult:
    """A job's report carried onto the run record: with `unconfigured` given, a `None` report is a `noop` naming
    why (the row ships enabled everywhere); otherwise `acted` picks the outcome (default: the report's own
    `acted`) and `detail` the text (default: its `summary()`)."""
    if report is None and unconfigured is not None:
        return ActivityResult.noop(detail=unconfigured)
    written = detail(report)
    return ActivityResult.acted(detail=written) if acted(report) else ActivityResult.noop(detail=written)


def _trigger(
    target: str,
    *takes: str,
    imports: tuple[str, ...] = (),
    not_configured: str | None = None,
    not_configured_prefix: str = "",
    own_session: bool = False,
    **outcome: Any,
) -> _Invoke:
    """The trigger of a job whose report `_outcome(**outcome)` reads. `target` is `module:function`, resolved
    at CALL time: the catalogue stays DB-free and a test's patch of the job is seen. `takes` names its
    arguments: `scheduled_at`, or a field of the context (`session`, `run_id`).

    `imports` are modules imported first, for their registrations. `not_configured` names an exception of the
    job's module that becomes a `noop` carrying its text (after `not_configured_prefix`): the row ships enabled
    everywhere. `own_session` hands the job a `SessionLocal()` of its own instead of the wrapper's, for a job
    that commits as it goes."""
    module, function = target.split(":")

    def invoke(scheduled_at: datetime, context: ActivityContext) -> ActivityResult:
        for name in imports:
            importlib.import_module(name)
        job_module = importlib.import_module(module)
        job = getattr(job_module, function)

        def run(session: Any) -> Any:
            args = [
                scheduled_at if name == "scheduled_at" else session if name == "session" else getattr(context, name)
                for name in takes
            ]
            try:
                return job(*args)
            except (getattr(job_module, not_configured) if not_configured else ()) as exc:
                return ActivityResult.noop(detail=f"{not_configured_prefix}{exc}")

        if own_session:
            from app.db import SessionLocal

            with SessionLocal() as session:
                report = run(session)
        else:
            report = run(context.session if "session" in takes else None)
        return report if isinstance(report, ActivityResult) else _outcome(report, **outcome)

    # Read by `tests/test_cron_system_activities.py`, which resolves every target: a string escapes rename tooling.
    invoke.target, invoke.takes, invoke.imports = target, takes, imports  # type: ignore[attr-defined]
    invoke.not_configured = not_configured  # type: ignore[attr-defined]
    return invoke


def _tally(counts: dict[str, Any]) -> str:
    return ", ".join(f"{key}={value}" for key, value in counts.items())


# The columns a trigger may measure. An allowlist: `column` is interpolated bare, so only literals named
# here are admissible. `updated_at` is unindexed — a sequential scan of `tasks`; re-measure if it grows
# large.
_COUNTABLE_COLUMNS: frozenset[str] = frozenset({"updated_at"})


def _count_since(session: Session, column: str, since: datetime) -> int:
    """`tasks` rows whose `column` landed at or after `since`, then end the transaction."""
    if column not in _COUNTABLE_COLUMNS:
        raise ValueError(
            f"{column!r} is not one of this module's probe columns {sorted(_COUNTABLE_COLUMNS)}"
        )
    count = session.execute(
        text(f"SELECT count(*) FROM tasks WHERE {column} >= :since"), {"since": since}
    ).scalar_one()
    session.rollback()
    return int(count)


# ── node-liveness ─────────────────────────────────────────────────────────────────────────────────────


def _invoke_node_liveness(scheduled_at: datetime, context: ActivityContext) -> ActivityResult:
    """`app/node_liveness_job.py`'s sweep, with its exit code carried onto the run record.

    Two of the three exit codes become warnings in both the log and the run record, so "the roster
    could not be read" is never indistinguishable from a clean pass. `EXIT_COULD_NOT_VERIFY` reports
    `acted`, since `noop` would claim there was nothing to do.
    """
    from app import node_liveness
    from app.node_liveness_job import sweep_once

    rc = sweep_once()
    if rc == node_liveness.EXIT_OK:
        return ActivityResult.noop(detail="every liveness invariant holds")
    if rc == node_liveness.EXIT_INVARIANT_VIOLATED:
        log.warning(
            "node-liveness: invariant violated — an alarm Task was opened per condition (see /tasks)"
        )
        return ActivityResult.acted(
            detail="invariant violated — an alarm Task was opened per condition (see /tasks)"
        )
    if rc == node_liveness.EXIT_COULD_NOT_VERIFY:
        log.warning(
            "node-liveness: COULD NOT VERIFY — the roster could not be read; this is not all-clear"
        )
        return ActivityResult.acted(
            detail="COULD NOT VERIFY — the roster could not be read; this is not all-clear"
        )
    # Unreachable today; a future exit code surfaces as an unexplained run row rather than as healthy.
    log.warning("node-liveness: unrecognised exit code %r", rc)
    return ActivityResult.acted(detail=f"the sweep returned an unrecognised exit code {rc!r}")


# ── task-pipeline ─────────────────────────────────────────────────────────────────────────────────────


def _invoke_task_pipeline(scheduled_at: datetime, context: ActivityContext) -> ActivityResult:
    """`app/task_pipeline_job.py`'s sweep — the epic fan-out, the rollups and the three crash reclaimers.

    `acted` is a measurement: task rows whose `updated_at` moved during the pass. It over-reports
    (concurrent writers count) and under-reports a pass whose only effect was a Needs-attention alarm
    (`sweep_needs_attention_notify` writes only `alert_stamps`). An exact count needs the sweep to
    report what it did.
    """
    from app.agent_bundles import sweep_refresh
    from app.task_pipeline_job import sweep_once

    started = _db_now(context.session)
    sweep_once()
    # SDK agents on a branch ref re-snapshot here, off the dispatch path (DD4); its fault costs the sweep nothing.
    try:
        sweep_refresh()
    except Exception:  # noqa: BLE001 — retried on the next pass
        log.exception("task-pipeline: the agent refresh sweep raised; retried next pass")
    changed = _count_since(context.session, "updated_at", started)
    if not changed:
        return ActivityResult.noop(detail="no task row changed while this pass ran")
    return ActivityResult.acted(
        detail=f"{changed} task row(s) changed while this pass ran"
    )


# ── lease-drain ───────────────────────────────────────────────────────────────────────────────────────

# The keys of `lease_drain.drain_once`'s summary that mean the pass did something (`nodes` only counts
# what was looked at). `dispatch_errors` is work — an all-raising node would otherwise look idle — and
# `drained`/`abandoned` are irreversible acts that must show in the headline.
_DRAIN_WORK_KEYS: tuple[str, ...] = (
    "issued", "dispatch_errors", "released", "stacks_stopped", "purged", "slots_reaped",
    "unplaceable", "abandoned", "drained",
    # Separate from `released` (holds freed because their task settled): console holds freed because
    # empty (`lease_drain._release_leaked_user_holds`) — the only signal consoles are leaking.
    "user_holds_released",
)


# How many failing node names the headline may name before counting instead, keeping it one line; the
# log line carries every node.
_HEADLINE_FAILED_NAMES = 6

# Each work key as the headline words it ("issued 8"): what happened leads, in words, not `key=value`.
_DRAIN_WORK_WORDS: dict[str, str] = {
    "issued": "issued", "dispatch_errors": "dispatch errors", "released": "released",
    "stacks_stopped": "stacks stopped", "purged": "purged", "slots_reaped": "slots reaped",
    "unplaceable": "unplaceable", "abandoned": "abandoned", "drained": "drained",
    "user_holds_released": "console holds released",
}
assert set(_DRAIN_WORK_WORDS) == set(_DRAIN_WORK_KEYS), "every drain work key needs exactly one headline word"

# A journal line's dispatch decline (`lease_drain._Decision.render`): its reason and its repeat count.
_DECLINE_LINE = re.compile(r"^\+\d+ms dispatch .*? DECLINED: (?P<why>.*?)(?: \(x(?P<times>\d+)\))?$")

# How many distinct decline reasons the headline names, and how long each may run: the headline must stay
# one bounded line (it survives `_journal_detail`'s truncation); the journal below carries the rest.
_HEADLINE_DECLINE_REASONS = 2
_HEADLINE_REASON_CHARS = 80


def _declined_clause(decisions: list[str]) -> str:
    """"declined 3: no usable credential account" — the pass's dispatch declines by reason, most frequent
    first; empty when it declined nothing. Read off the rendered journal, the only form the summary carries."""
    counts: dict[str, int] = {}
    for line in decisions:
        match = _DECLINE_LINE.match(line)
        if match:
            why = match["why"]
            counts[why] = counts.get(why, 0) + int(match["times"] or 1)
    if not counts:
        return ""
    ranked = sorted(counts.items(), key=lambda item: -item[1])
    shown = ranked[:_HEADLINE_DECLINE_REASONS]

    def clip(why: str) -> str:
        # Up to its first em-dash: the headline must carry none (see `_invoke_lease_drain`'s closing note).
        why = why.split("—", 1)[0].strip()
        return why if len(why) <= _HEADLINE_REASON_CHARS else why[: _HEADLINE_REASON_CHARS - 3] + "..."

    total = sum(counts.values())
    if len(ranked) == 1:
        return f"declined {total}: {clip(shown[0][0])}"
    reasons = ", ".join(f"{clip(why)} x{times}" for why, times in shown)
    more = len(ranked) - len(shown)
    return f"declined {total}: {reasons}" + (f", {more} other reason(s)" if more else "")

# The overflow line: how many decisions the ledger is not showing.
_JOURNAL_OVERFLOW = "... and {count} more decision(s) this pass, not rendered here"


def _journal_detail(headline: str, lines: list[str], budget: int) -> str:
    """The headline, then as many of the pass's decision lines as the ledger can hold, then — only if
    something was left out — one line saying how many.

    Bounded here rather than by `cron_runtime`'s bare `[:MAX_DETAIL_CHARS]` slice, so whole lines are
    dropped and counted instead of cut mid-word. The headline is never dropped and comes first: the
    Runs table renders the first line (`apps/web/src/pages/cron/vocabulary.ts`).
    """
    kept = [headline]
    used = len(headline)
    for index, line in enumerate(lines):
        left = len(lines) - index
        # Room for this line plus, unless last, the overflow line. Conservative by a few characters.
        reserve = 0 if left == 1 else 1 + len(_JOURNAL_OVERFLOW.format(count=left - 1))
        if used + 1 + len(line) + reserve > budget:
            # Only if it fits: at index 0 nothing reserved room for it. Unreachable at
            # `MAX_DETAIL_CHARS`, but the headline embeds an unbounded `failed` list repr.
            overflow = _JOURNAL_OVERFLOW.format(count=left)
            if used + 1 + len(overflow) <= budget:
                kept.append(overflow)
            break
        kept.append(line)
        used += 1 + len(line)
    detail = "\n".join(kept)
    # Last resort, unreachable in practice — makes "never longer than the ledger's bound" a property of
    # this function.
    return detail if len(detail) <= budget else detail[:budget]


def _invoke_lease_drain(scheduled_at: datetime, context: ActivityContext) -> ActivityResult:
    """`app/lease_drain.py`'s dispatch pass — the fleet's dispatch loop, so a disabled row stops
    dispatching (which is why the cron engine itself is not a cron, AC9.5).

    Counts come from `drain_once`'s summary. A node whose pass failed reports `acted` even with no
    change, so repeating failures survive `acted` retention. The pass's decision journal
    (`lease_drain.DecisionLog`) is rendered under the headline; any journal entry is enough to report
    `acted`, so refusals are recorded too. A pass with nothing to decide writes nothing.
    """
    from app.cron_runtime import MAX_DETAIL_CHARS
    from app.lease_drain import drain_once

    summary: dict[str, Any] = drain_once() or {}
    did = {key: summary[key] for key in _DRAIN_WORK_KEYS if summary.get(key)}
    failed = list(summary.get("failed") or [])
    decisions = [str(line) for line in (summary.get("decisions") or [])]
    dropped = int(summary.get("decisions_dropped") or 0)
    if not did and not failed and not decisions:
        return ActivityResult.noop(
            detail=f"{summary.get('nodes', 0)} live node(s), nothing to dispatch or release"
        )
    # What happened leads: the counters in words, `issued` first, then the declines by reason.
    # The live-node count is left to the `noop` detail, where it explains an idle pass.
    headline: list[str] = []
    counts = [f"{_DRAIN_WORK_WORDS[key]} {did[key]}" for key in _DRAIN_WORK_KEYS if key in did]
    if failed:
        # Bounded: the headline's one unbounded input, and the headline must survive truncation.
        shown = ", ".join(str(node) for node in failed[:_HEADLINE_FAILED_NAMES])
        more = len(failed) - _HEADLINE_FAILED_NAMES
        counts.append(f"failed on {shown}" if more <= 0 else f"failed on {shown} and {more} more")
    if counts:
        headline.append(", ".join(counts))
    declined = _declined_clause(decisions)
    if declined:
        headline.append(declined)
    tally = f"{len(decisions)} decision(s)"
    if dropped:
        # "record(s)", not "decision(s)": counts refused calls (`DecisionLog.dropped`).
        tally = f"{tally}, {dropped} record(s) beyond this pass's journal cap"
    headline.append(tally)
    # Semicolons, no em-dash: the Runs table demotes a first line containing an em-dash to a footnote
    # (`apps/web/src/pages/cron/vocabulary.ts`).
    return ActivityResult.acted(
        detail=_journal_detail("; ".join(headline), decisions, MAX_DETAIL_CHARS)
    )


# ── node-groups ───────────────────────────────────────────────────────────────────────────────────────

# How much error text a run record carries before it is cut, so one AWS refusal cannot crowd the table
# cell. The full text is on `node_groups.last_error`.
_ERROR_DETAIL_BUDGET = 200


def _invoke_node_groups(scheduled_at: datetime, context: ActivityContext) -> ActivityResult:
    """`app/node_group_reconcile.py`'s pass — hold every enabled dynamic node group at its desired
    capacity, and declare the worktrees its project assignments call for.

    The count is exact (`ReconcileOutcome`). An errored pass reports `acted` even with no launch or
    termination, so the run survives `acted` retention. Takes the activity context's own session,
    because the pass commits per node.
    """
    if not edition_features.enabled("node_groups"):
        return ActivityResult.noop(detail="no enabled node group")
    from app.node_group_reconcile import reconcile_once

    outcome = reconcile_once(context.session)
    if not outcome.groups:
        return ActivityResult.noop(detail="no enabled node group")
    if not outcome.acted:
        return ActivityResult.noop(
            detail=f"{outcome.groups} group(s) already at desired capacity"
        )
    parts: list[str] = []
    if outcome.launched:
        parts.append(f"launched {outcome.launched}")
    if outcome.terminated:
        parts.append(f"terminated {outcome.terminated}")
    if outcome.slots_declared:
        parts.append(f"declared {outcome.slots_declared} worktree(s)")
    if outcome.slots_retired:
        parts.append(f"retired or demoted {outcome.slots_retired} worktree(s)")
    if outcome.errors:
        parts.append(f"{len(outcome.errors)} group(s) did not converge")
    detail = f"{outcome.groups} group(s): " + ", ".join(parts)
    if outcome.errors:
        # The first error only, truncated: points at which pass to inspect; each group row has its own.
        detail = f"{detail} — {outcome.errors[0][:_ERROR_DETAIL_BUDGET]}"
    return ActivityResult.acted(detail=detail)


# ── Triggers that need more than `_trigger` ──────────────────────────────────────────────────────────


def _invoke_bedrock_health(scheduled_at: datetime, context: ActivityContext) -> ActivityResult:
    """`app/bedrock/probes.py::check` — probe the platform Bedrock account, rebuild the deny policy, store the
    verdict and raise the operator alarm on a change to broken.

    Not configured (Settings > Billing > Bedrock names no credential) is a `noop` naming why: the row ships
    enabled everywhere, so selecting a credential is the only switch. Healthy is a `noop`; a broken account is
    `acted` naming the failing probes, never raised: the alarm, not the run ledger, is how an operator hears of it.
    Registered only with `jaah_ai_credit`."""
    if not edition_features.enabled("jaah_ai_credit"):
        return ActivityResult.noop(detail="platform Bedrock is not in this edition")
    from app import bedrock
    from app.db import SessionLocal

    # Its OWN session, not the wrapper's: `check` commits the verdict before it alarms, and a slow probe pass
    # must not hold the wrapper's transaction open. Run even when unconfigured: clearing the credential is a
    # transition the listeners must hear (one DB read, no AWS call).
    with SessionLocal() as session:
        configured = bedrock.connection(session) is not None
        verdict = bedrock.check(session)
    if not configured:
        return ActivityResult.noop(detail="platform Bedrock not configured (Settings > Billing > Bedrock)")
    if verdict.ok:
        # `noop`: a healthy pass every 5 minutes is not worth a retained run row.
        return ActivityResult.noop(detail="healthy" + (f"; {len(verdict.warnings)} warning(s)" if verdict.warnings else ""))
    return ActivityResult.acted(detail="unavailable: " + ", ".join(verdict.failures))


def _invoke_insight_digest(scheduled_at: datetime, context: ActivityContext) -> ActivityResult:
    """`app/operator_dashboard/digest.py::run` — write the digest of the last complete week. A failure is stored
    as the week's `failed` row first, then raised (an `error` run). Registered only with the operator dashboard."""
    if not edition_features.enabled("operator_dashboard"):
        return ActivityResult.noop(detail="the operator dashboard is not in this edition")
    from sqlalchemy import select

    from app.models.automations import Cron
    from app.operator_dashboard import digest

    org_id = context.session.scalar(select(Cron.org_id).where(Cron.id == context.cron_id))
    return ActivityResult.acted(detail=digest.run(context.session, org_id).summary())


# ── The `_trigger` one-liners ─────────────────────────────────────────────────────────────────────────
# Each is `acted` only on evidence the job's report carries; the parenthesised note is what is special.

_BILLING_UNCONNECTED = "billing not connected (Settings > Billing names no Stripe credential)"
_MEMORY_OFF = "project memory is switched off"


def _ssm_detail(outcome: Any) -> str:
    if not outcome.associated and not outcome.errors:
        return f"{outcome.checked} instance(s) checked; every one holds a profile"
    parts = [f"attached {outcome.associated} of {outcome.checked} checked"]
    if outcome.errors:
        parts.append(f"{len(outcome.errors)} failed: " + "; ".join(outcome.errors))
    return "; ".join(parts)[:_ERROR_DETAIL_BUDGET * 2]


# An instance profile onto every control-plane-launched instance holding none, so every Jaah host is reachable
# over SSM. `acted` when it attached one or something failed (the errors are the detail).
_invoke_node_ssm = _trigger(
    "app.node_instance_profile:reconcile_once", "session",
    acted=lambda o: o.associated or o.errors, detail=_ssm_detail,
)
# Live Cast rows for every published project's new tasks; an evaluation enqueued while any row is stale.
_invoke_live_cast_sweep = _trigger(
    "app.durable.live_cast:sweep", "session", acted=lambda r: r[1],
    detail=lambda r: f"enqueued {r[1]} evaluation(s) of {r[0]} published project(s)" if r[1]
    else f"{r[0]} published project(s), none owing an evaluation",
)
# Start the seal of every done task with no manifest over two minutes old; record `failed` one whose run ended
# without a row or is live long past its wait deadline (an offer to a seal already waiting attaches).
_invoke_compliance_seal_sweep = _trigger(
    "app.durable.compliance_seal:sweep", "session", acted=lambda r: r[1] or r[2],
    detail=lambda r: (
        f"started {r[1]} seal(s), recorded {r[2]} failed (ended without a result or overdue), of {r[0]} unsealed task(s)"
        if r[1] or r[2] else f"{r[0]} unsealed done task(s), each with a seal in flight"
    ),
)
# The headscale allow set follows the nodes' egress reports. An AWS refusal raises, so the run records it.
_invoke_headscale_allowlist = _trigger(
    "app.headscale_allowlist:sync_from_nodes", "session", not_configured="NotConfigured", acted=lambda r: r[1],
    detail=lambda r: f"allow set {'now' if r[1] else 'already'} holds {r[0]} node IP(s)",
)
# SES's inbound notifications into stored, routed messages (unconfigured on a local stack). An SQS refusal raises.
# `app.dmarc` registers the `dmarc-reports` handler; `ses_inbound.run` the hosted-address resolver, where tenant
# mail is in the edition.
_invoke_ses_inbound = _trigger(
    "app.ses_inbound:run", imports=("app.dmarc",),
    unconfigured="SES inbound not configured here: no inbound queue",
)
# SES's delivery events into the delivery log and suppressions (unconfigured on a local stack). An SQS refusal raises.
_invoke_ses_events = _trigger(
    "app.ses_events:run", unconfigured="SES events not configured here: no events queue",
)
# Tenant service-account mailboxes' new mail into their orgs' email triggers. One connection's failure is recorded on
# it, never raised.
_invoke_tenant_gmail_sync = _trigger("app.tenant_gmail.receive:run")
# New Stripe events into the mirrors they name; `hosting_provisioning` registers the hooks a settled change
# reaches. A failed poll is recorded and `acted`, never raised. Own short transactions (`lease-drain`'s shape):
# a slow tick keeps every event it finished.
_invoke_billing_events = _trigger("app.billing.events:poll_once", imports=("app.hosting_provisioning",))
# Monthly hostings through past due, stopped and terminated; `plan_bundles` registers the bundle rule.
_invoke_hosting_lifecycle = _trigger("app.hosting_lifecycle:run", imports=("app.plan_bundles",))
# Mine settled ON tasks, distil one, promote and corroborate. It commits task by task, so no model call runs
# inside the wrapper's transaction and a tick stopped at its budget keeps what it finished.
_invoke_memory_curate = _trigger(
    "app.project_memory.curate:run_tick", "session", own_session=True,
    unconfigured=_MEMORY_OFF, detail=lambda r: r.detail(),
)
# Provisional citations, trust, decay, consolidation, graduation and the slice; commits step by step.
_invoke_memory_nightly = _trigger(
    "app.project_memory.nightly:run", "session", own_session=True,
    unconfigured=_MEMORY_OFF, detail=lambda r: r.detail(),
)

# The corpus file into the embedded index. A provider refusal raises, so deletes included roll back. `acted`
# only when a row changed: most passes touch nothing, and the row keeps `retention_mode = 'acted'`.
_invoke_support_kb_index = _trigger(
    "app.support_kb.index:reconcile_from_settings", "session",
    unconfigured="no corpus file or no embeddings credential picked (Settings > Support)",
    acted=lambda r: r.added or r.removed,
)
# jaah.ai's English pages into the `site` rows. The credential is checked before any fetch; a crawl the module
# cannot vouch for (`CrawlFailed`) raises BEFORE anything is reconciled, so the existing rows keep serving.
_invoke_support_kb_site = _trigger(
    "app.support_kb.site_crawl:run_from_settings", "session",
    unconfigured="no embeddings credential picked (Settings > Support)",
    acted=lambda r: r.added or r.removed,
)
# Draft the owed lessons, then the approved ones into the `ticket` rows. It commits ticket by ticket, so no model
# call runs inside the wrapper's transaction and a tick stopped at its budget keeps the drafts it paid for.
_invoke_support_lessons = _trigger(
    "app.support_kb.lessons:run", "session", own_session=True, acted=lambda r: r.acted,
)
# Re-read every live subscription, card and open invoice.
_invoke_billing_reconcile = _trigger(
    "app.billing.events:reconcile_once", unconfigured=_BILLING_UNCONNECTED, acted=lambda _counts: True,
    detail=_tally,
)
# Warn about expiring cards and sweep settled notices (each warning owes its own email).
_invoke_billing_card_expiry = _trigger(
    "app.billing.notices:run_card_expiry", unconfigured=_BILLING_UNCONNECTED,
    acted=lambda counts: any(counts.values()), detail=_tally,
)
# Every wallet against its ledger; drift is an operator alarm.
_invoke_token_billing_audit = _trigger(
    "app.token_billing.audit:run", acted=bool,
    detail=lambda drifted: f"{len(drifted)} org(s) drifted" if drifted else "every wallet equals its ledger",
)
# End every open Jaah session that never asked for credentials within `REAP_AFTER` of starting.
_invoke_jaah_session_reap = _trigger(
    "app.jaah_routing.sessions:reap", acted=bool,
    detail=lambda ended: f"ended {ended} session(s)" if ended else "nothing to end",
)
# End the Add-node orders that cannot finish, and the node removals a silent host never will.
_invoke_node_order_sweep = _trigger("app.node_orders:sweep")
# Give every active org the nodes and Jaah AI credit its plan includes, and release what it no longer does.
_invoke_plan_bundles = _trigger("app.plan_bundles:run")
# Move every org whose plan ended to that plan's then plan, and email its Owners and Admins.
_invoke_plan_rollover = _trigger("app.plan_rollover:run")
# Remind each API key's owner before it expires, record each expiry, revoke a disabled owner's keys.
_invoke_api_keys_hourly = _trigger("app.api_keys:sweep")
# Delete the staged comment images no post or edit claimed within a day, then their bytes.
_invoke_comment_image_reap = _trigger(
    "app.comment_service:reap_unclaimed_images", "session", own_session=True, acted=bool,
    detail=lambda reaped: f"reaped {reaped} image(s)" if reaped else "no unclaimed image past its grace",
)
# Email the Owners and Admins of each org whose project or group budget crossed 80% or 100% this month.
_invoke_budget_alerts = _trigger("app.budgets:alert")
# POST each org's new activity events to its SIEM destination.
_invoke_siem_export = _trigger("app.siem_export:run")
# CloudWatch alarms off the relay queue to Slack (unconfigured outside the SOC 2 boundary).
_invoke_alarm_relay = _trigger(
    "app.alarm_relay:run", "session",
    unconfigured="alarm relay not configured here: ALARM_RELAY_QUEUE_URL unset",
)
# Every trigger's received and queued deliveries, through preflight, dedup and limits, into filed Tasks.
_invoke_trigger_intake = _trigger(
    "app.trigger_intake:run_pass", acted=lambda summary: summary.rows,
    detail=lambda summary: summary.detail() if summary.rows else "no trigger delivery to process",
)
# The idle auto-pause, then each trigger owner's daily digest for the 24 h before `scheduled_at`.
_invoke_trigger_digest = _trigger("app.trigger_notices:run_digest", "scheduled_at")
# This hour's node load, worktree occupancy, account quota and wallet floor; a row already taken is a `noop`.
_invoke_ops_snapshot = _trigger("app.ops_snapshots:take_snapshots", "session", acted=lambda r: r.total)
# Score this week's sample of delivered tasks (raises, an `error` run, when the support LLM is not configured).
_invoke_outcome_review = _trigger(
    "app.operator_dashboard.review:run", "session", "run_id", acted=lambda r: r.sampled,
    detail=lambda r: r.summary() if r.sampled else "no unreviewed task delivered in the last 7 days",
)
# Every open scan target whose auditor run ended without a submit becomes `error`, a run past its bound is
# stopped, and due waivers lapse. Its own session: it commits as it goes.
_invoke_compliance_reconcile = _trigger(
    "app.governance.scans:reconcile", "session", own_session=True, acted=bool,
    detail=lambda n: f"{n} scan target(s) resolved as error" if n else "no auditor run ended without a submit",
)
# The daily scan of every org that has it on.
_invoke_compliance_daily = _trigger(
    "app.governance.scans:run_daily", "session", acted=bool,
    detail=lambda n: f"{n} daily scan(s) started" if n else "no org has a daily scan due",
)
# AWS's invocation log since the cursor; each metered call corrected by it.
_invoke_bedrock_usage_correct = _trigger("app.token_billing.metering:correct")
# Yesterday's metered cost per tenant against AWS's.
_invoke_bedrock_reconcile = _trigger(
    "app.token_billing.reconcile:run",
    unconfigured="no platform Bedrock account (Settings > Billing > Bedrock)",
    acted=lambda r: r.checked or r.errors,
)
# Each complete day's security events into the Object Lock archive. A storage fault, or a bucket with no
# credentials, raises, so the next firing fills the day; `acted` only when a day was written.
_invoke_security_event_export = _trigger(
    "app.security_archive:run", "session",
    unconfigured="security archive not configured here: SECURITY_ARCHIVE_BUCKET or AWS_REGION missing",
    acted=lambda r: r.exported_days,
)
# Cron starvation and node disk, as EMF lines into the CP log group. Every pass acts, so the row is seeded
# `retention_mode = 'none'` (a minutely row would be noise); an `error` is still recorded.
_invoke_metrics_emit = _trigger(
    "app.metrics_emf:run", "session",
    unconfigured="metrics not configured here: METRICS_NAMESPACE unset", acted=lambda _r: True,
)


def _evidence(collector_id: str) -> _Invoke:
    """The thin trigger of one evidence collector: `collectors.run_collector`, resolved at call time like
    `_trigger`'s targets so the catalogue stays DB-free and a test's patch is seen."""

    def invoke(scheduled_at: datetime, context: ActivityContext) -> ActivityResult:
        if not edition_features.enabled("compliance"):
            return ActivityResult.noop(detail="compliance is not in this edition")
        from app.compliance_evidence import collectors

        return collectors.run_collector(collectors.collector(collector_id), scheduled_at, context)

    invoke.collector_id = collector_id  # type: ignore[attr-defined]
    return invoke


_invoke_evidence_security_archive = _evidence("security-archive")
_invoke_evidence_retention_sweep = _evidence("retention-sweep")
_invoke_evidence_access_events = _evidence("access-events")
_invoke_evidence_mfa_posture = _evidence("mfa-posture")
_invoke_evidence_github_settings = _evidence("github-settings")
_invoke_evidence_dependabot = _evidence("dependabot")
_invoke_evidence_cloudwatch_alarms = _evidence("cloudwatch-alarms")
_invoke_evidence_paging_subscriptions = _evidence("paging-subscriptions")
_invoke_evidence_securityhub = _evidence("securityhub")
_invoke_evidence_edge_tls = _evidence("edge-tls")
_invoke_evidence_backups = _evidence("backups")
_invoke_evidence_storage_lifecycle = _evidence("storage-lifecycle")
_invoke_evidence_ecr_scan = _evidence("ecr-scan")

def nodelink_pass(session: Session) -> dict[str, int]:
    """The `nodelink-provision` pass: every NodeLink still `provisioning` (`nodelinks.provision_requested`), then
    every store being deleted in its cloud (`org_shared_store.delete_requested`); one tally of both."""
    if not edition_features.enabled("nodelink"):
        # No NodeLink in this edition (the pass is registered only with it).
        return {"ready": 0, "failed": 0, "stores_deleted": 0, "stores_deleting": 0, "stores_busy": 0, "stores_skipped": 0}
    from app import nodelinks, org_shared_store

    return {**nodelinks.provision_requested(session), **org_shared_store.delete_requested(session)}


# Its own short transactions: `provision` commits per NodeLink and a delete per store, so a slow pass keeps every one
# it finished.
_invoke_nodelink_provision = _trigger(
    "app.cron_system_activities:nodelink_pass", "session", own_session=True,
    # Kept only when a NodeLink finished or a store went: a pass that found one still being made or deleted is a noop.
    acted=lambda counts: counts["ready"] + counts["failed"] + counts["stores_deleted"] > 0,
    detail=lambda counts: _tally(counts),
)

# Kept only when it recorded a stop or recovered a stopped task's workflows; off ECS (a local stack) a noop. An AWS
# refusal raises, so the run records it.
_invoke_cp_ecs_record = _trigger(
    "app.cp_ecs:record", "session", not_configured="NotConfigured",
    acted=lambda counts: counts["stopped"] > 0 or counts["recovered"] > 0 or "recover_failed" in counts, detail=lambda counts: _tally(counts),
)


# ── The bounds this module owns ───────────────────────────────────────────────────────────────────────
# The rest live with their job modules; `_register_all` reads them there.

# Every 30 s with a 25 s bound, the alarm relay's cadence: one short poll per pass (`aws/sqs_drain.WAIT_SECONDS`); the
# `ses-events` cron shares it.
SES_INBOUND_INTERVAL_SECONDS = 30
SES_INBOUND_TIMEOUT_SECONDS = 25
# Every 60 s (the architecture's cadence for tenant Gmail); the timeout covers the pass's 40 s budget
# (`tenant_gmail.receive._RUN_BUDGET_SECONDS`) plus one connection's last calls.
TENANT_GMAIL_SYNC_INTERVAL_SECONDS = 60
TENANT_GMAIL_SYNC_TIMEOUT_SECONDS = 55
#: Every 5 minutes (the epic's P-broken detection bound); the gap lets Run now work, the timeout covers seven
#: probes at 10 s each plus the full profile listing.
BEDROCK_HEALTH_INTERVAL_SECONDS = 300
BEDROCK_HEALTH_MIN_GAP_SECONDS = 30
BEDROCK_HEALTH_TIMEOUT_SECONDS = 120
TOKEN_BILLING_AUDIT_MIN_GAP_SECONDS = 60
TOKEN_BILLING_AUDIT_TIMEOUT_SECONDS = 120
JAAH_SESSION_REAP_INTERVAL_SECONDS = 300
JAAH_SESSION_REAP_MIN_GAP_SECONDS = 60
JAAH_SESSION_REAP_TIMEOUT_SECONDS = 60
#: Every 5 minutes: its deadlines are days (a machine that never joined) and half an hour (a payment).
NODE_ORDER_SWEEP_INTERVAL_SECONDS = 300
NODE_ORDER_SWEEP_TIMEOUT_SECONDS = 120
#: Every 5 minutes: its deadlines are days (grace, retention) and a cancelled hosting's paid-month end.
HOSTING_LIFECYCLE_INTERVAL_SECONDS = 300
#: An EC2 stop, start or terminate per hosting it moves.
HOSTING_LIFECYCLE_TIMEOUT_SECONDS = 240
#: Every 2 minutes, so a plan's end is acted on within that; woken after any commit that changes an org's terms.
PLAN_BUNDLES_INTERVAL_SECONDS = 120
PLAN_BUNDLES_MIN_GAP_SECONDS = 5
#: Up to `plan_bundles.PROVISION_PER_PASS` placements or launches.
PLAN_BUNDLES_TIMEOUT_SECONDS = 600
#: Every minute, so an org moves within one of its plan's end.
PLAN_ROLLOVER_INTERVAL_SECONDS = 60
PLAN_ROLLOVER_MIN_GAP_SECONDS = 5
PLAN_ROLLOVER_TIMEOUT_SECONDS = 50
#: Hourly, not daily: a key created with less than a day left still gets its reminder within the hour.
API_KEYS_HOURLY_INTERVAL_SECONDS = 3600
API_KEYS_HOURLY_MIN_GAP_SECONDS = 60
API_KEYS_HOURLY_TIMEOUT_SECONDS = 300
#: Hourly: an unclaimed image outlives its grace by at most an hour.
COMMENT_IMAGE_REAP_INTERVAL_SECONDS = 3600
COMMENT_IMAGE_REAP_MIN_GAP_SECONDS = 60
COMMENT_IMAGE_REAP_TIMEOUT_SECONDS = 300
#: Every minute, so a budget's notice follows the spend that crossed it within one.
BUDGET_ALERTS_INTERVAL_SECONDS = 60
BUDGET_ALERTS_MIN_GAP_SECONDS = 5
BUDGET_ALERTS_TIMEOUT_SECONDS = 50
#: Every minute: the PRD's bound from event to SIEM is five (`siem_export.SETTLE` is two of them).
SIEM_EXPORT_INTERVAL_SECONDS = 60
SIEM_EXPORT_MIN_GAP_SECONDS = 5
#: Above `siem_export.PASS_BUDGET_SECONDS` plus one POST's timeout.
SIEM_EXPORT_TIMEOUT_SECONDS = 50
#: Every 30 s: an alarm reaches Slack within a poll interval plus `aws/sqs_drain.WAIT_SECONDS`.
ALARM_RELAY_INTERVAL_SECONDS = 30
#: One poll (`aws/sqs_drain.WAIT_SECONDS`) plus the Slack posts for at most ten messages.
ALARM_RELAY_TIMEOUT_SECONDS = 25
#: Every 60 s: the backstop for an ingest or a closing filed Task that fires it early (§3.6).
TRIGGER_INTAKE_INTERVAL_SECONDS = 60
#: One pass files at most `trigger_intake.MAX_ROWS_PER_TRIGGER` rows per trigger; a preflight never runs inside it.
TRIGGER_INTAKE_TIMEOUT_SECONDS = 60
#: A floor low enough that a delivery files within seconds, high enough to coalesce a burst of ingests.
TRIGGER_INTAKE_MIN_GAP_SECONDS = 2
#: Daily at 08:00 in the CP's cron timezone (UTC unless the operator changed it), architecture §6.
TRIGGER_DIGEST_CRON_EXPRESSION = "0 0 8 * * *"
#: A read per owner and an email each; generous for a large portfolio.
TRIGGER_DIGEST_TIMEOUT_SECONDS = 300
#: A floor so Run now works; a second run the same day sends nothing new anyway (one digest per owner per day).
TRIGGER_DIGEST_MIN_GAP_SECONDS = 60
#: Hourly: one point per hour is the series' resolution (architecture §5.2).
OPS_SNAPSHOT_INTERVAL_SECONDS = 3600
#: A handful of grouped reads and four inserts; generous for a large fleet.
OPS_SNAPSHOT_TIMEOUT_SECONDS = 300
#: A floor so Run now works; a second pass in the same hour writes nothing anyway.
OPS_SNAPSHOT_MIN_GAP_SECONDS = 60
#: Up to 200 model calls at most twice each; generous for the default 15.
OUTCOME_REVIEW_TIMEOUT_SECONDS = 1800
#: A floor so Run now works, and so a double firing cannot pay twice (a reviewed task is never re-sampled anyway).
OUTCOME_REVIEW_MIN_GAP_SECONDS = 300
#: Every minute: two ticks are the documented ceiling from settle to searchable (AC-6, ≤ 120 s).
MEMORY_CURATE_INTERVAL_SECONDS = 60
#: Mining and promotion are DB work; the bound is the distillation's one model call plus a transcript read.
MEMORY_CURATE_TIMEOUT_SECONDS = 120
#: Wakeable, and Run now works.
MEMORY_CURATE_MIN_GAP_SECONDS = 5
#: Daily at 03:00 (seconds-first: second, minute, hour, …), an off-peak hour for the DB-only pass.
MEMORY_NIGHTLY_CRON_EXPRESSION = "0 0 3 * * *"
#: DB work only; the pass stops starting new units at `nightly.NIGHTLY_BUDGET_SECONDS` (600 s), inside this bound.
MEMORY_NIGHTLY_TIMEOUT_SECONDS = 900
#: A floor so Run now works; a second pass the same day finds nothing left to do.
MEMORY_NIGHTLY_MIN_GAP_SECONDS = 60
#: Two model calls of 180 s at most, after the week's section reads.
INSIGHT_DIGEST_TIMEOUT_SECONDS = 900
#: A floor so Run now works, and so a double firing cannot pay twice.
INSIGHT_DIGEST_MIN_GAP_SECONDS = 300
#: Every 15 s (the epic's correction cadence); the timeout covers a 50-page log backlog.
BEDROCK_USAGE_CORRECT_INTERVAL_SECONDS = 15
BEDROCK_USAGE_CORRECT_MIN_GAP_SECONDS = 5
BEDROCK_USAGE_CORRECT_TIMEOUT_SECONDS = 120
#: One Cost Explorer read per tenant that used the Jaah account yesterday.
BEDROCK_RECONCILE_MIN_GAP_SECONDS = 60
BEDROCK_RECONCILE_TIMEOUT_SECONDS = 300
#: Every 15 s: a requested NodeLink's progress line advances at the page's own poll.
NODELINK_PROVISION_INTERVAL_SECONDS = 15
#: The pass's start budget (`nodelinks.PROVISION_PASS_BUDGET_SECONDS`) plus one NodeLink's last pass.
NODELINK_PROVISION_TIMEOUT_SECONDS = 120
NODELINK_PROVISION_MIN_GAP_SECONDS = 5


# ── The registrations ─────────────────────────────────────────────────────────────────────────────────

# Each descriptor's bounds are imported from the job module, never retyped — a hand-copied product of
# changing values once left passes cancelled mid-call. A seeded row's NULL `timeout_seconds` means "use
# the registration's value".


def _system(
    type_: str,
    label: str,
    invoke: _Invoke,
    description: str,
    *,
    timeout: float,
    interval: float | None = None,
    cron: str | None = None,
    gap: float | None = None,
) -> None:
    """Register one system cadence: machinery (`is_system`), re-creatable from the picker, no params.

    Exactly one of `interval` (seconds) and `cron` (an expression) is the default schedule; `gap` is the
    wake floor (None: not wakeable). One call per cadence, `type_` first: the tenancy census
    (`test_tenancy_plane_sites.py`) reads each `_system(...)` call's first argument. The community edition skips a
    cadence outside `CE_SYSTEM_ACTIVITY_TYPES`."""
    if edition.IS_CE and type_ not in CE_SYSTEM_ACTIVITY_TYPES:
        return
    if (interval is None) == (cron is None):
        raise ValueError(f"{type_!r}: exactly one of interval and cron")
    schedule = (
        DefaultSchedule(kind=SCHEDULE_KIND_CRON, cron_expression=cron)
        if cron is not None
        else DefaultSchedule(kind=SCHEDULE_KIND_INTERVAL, interval_seconds=int(interval))
    )
    register(ActivityDescriptor(
        type=type_,
        label=label,
        description=description,
        is_system=True,
        creatable=True,
        params=NO_PARAMS,
        default_schedule=schedule,
        invoke=invoke,
        timeout_seconds=timeout,
        min_gap_seconds=gap,
    ))


def _register_all() -> None:
    """Register them all, with numbers from the job modules rather than retyped.

    Not a deferred import: it runs on this module's last line. Catalogue readers avoid the DB stack
    because `app/cron_activities.py::load_activities` imports this module only from
    `get()`/`creatable_descriptors()`.

    A `gap` is a wake floor and also what lets the console's Run now fire a cron at all. Only
    `task-pipeline` and `lease-drain` are woken by a task transition, and `plan-bundles` by
    `plan_terms.wake_bundles`; every other gap is there for Run now, and a cadence without one is not
    wakeable because nothing a task transition changes is in its worklist (notes say where else)."""
    from app import lease_drain, node_liveness_job, task_pipeline_job
    from app import security_archive, metrics_emf
    # Each feature's cadences, registered only in an edition that includes it (`packages/editions/features.toml`).
    if edition_features.enabled("node_groups"):
        from app import node_group_reconcile
    if edition_features.enabled("cloud_nodes"):
        from app import node_instance_profile
    if edition_features.enabled("knowledge_base"):
        from app.support_kb import index as support_kb_index, lessons as support_lessons, site_crawl
    if edition_features.enabled("billing"):
        from app.billing import events as billing_events, notices as billing_notices
    if edition_features.enabled("customers"):
        from app import customer_history  # noqa: F401 — registers the customer History feed's Stripe handlers
    if edition_features.enabled("live_cast"):
        from app.durable import live_cast
    if edition_features.enabled("compliance"):
        from app.durable import compliance_seal
        from app.governance import scans
        from app.compliance_evidence import collectors as evidence
    if edition_features.enabled("tailnet"):
        from app import headscale_allowlist
    if edition_features.enabled("control_plane_view"):
        from app import cp_ecs
    from app.operator_dashboard import vocabulary

    _system(NODE_LIVENESS, "Node liveness", _invoke_node_liveness,
            "Reads the node roster, places release generations and asserts the fleet's liveness "
            "invariants, opening or resolving an alarm Task per condition.",
            interval=node_liveness_job.INTERVAL_SECONDS, timeout=node_liveness_job.TIMEOUT_SECONDS)
    _system(TASK_PIPELINE, "Task pipeline", _invoke_task_pipeline,
            "Advances the Task pipeline: fans out an epic's children, rolls up epic state, notifies "
            "Needs-attention entries and reclaims children a crashed runner left active.",
            interval=task_pipeline_job.INTERVAL_SECONDS, timeout=task_pipeline_job.TIMEOUT_SECONDS,
            gap=WAKE_MIN_GAP_SECONDS)
    _system(LEASE_DRAIN, "Lease drain", _invoke_lease_drain,
            "The fleet's dispatch decision: releases finished holds, re-dispatches an un-acked lease into "
            "the same slot and issues a lease_slot intent for every task a live node can take.",
            interval=lease_drain.INTERVAL_SECONDS, timeout=lease_drain.TIMEOUT_SECONDS, gap=WAKE_MIN_GAP_SECONDS)
    # Its worklist is `node_groups`; waking on an operator saving a group would be a different trigger (follow-up).
    if edition_features.enabled("node_groups"):
        _system(NODE_GROUPS, "Node groups", _invoke_node_groups,
                "Holds every enabled dynamic node group at its desired capacity: launches the nodes a "
                "capacity line is short, terminates the free ones it is over by, and declares each "
                "group's assigned worktrees on the nodes it owns.",
                interval=node_group_reconcile.INTERVAL_SECONDS, timeout=node_group_reconcile.TIMEOUT_SECONDS)
    # Its worklist is the done tasks' rows.
    if edition_features.enabled("compliance"):
        _system(COMPLIANCE_SEAL_SWEEP, "Compliance manifest sweep", _invoke_compliance_seal_sweep,
                "Starts the compliance manifest seal of every finished task that has none, two minutes after it "
                "finished — the backstop for the seal the task-done workflow starts.",
                interval=compliance_seal.SWEEP_INTERVAL_SECONDS, timeout=compliance_seal.SWEEP_TIMEOUT_SECONDS)
    # Its worklist is the published projects' rows; a task event wakes one project through the nudge instead.
    if edition_features.enabled("live_cast"):
        _system(LIVE_CAST_SWEEP, "Live Cast evaluation", _invoke_live_cast_sweep,
                "Adds new tasks and text files of each published Live Cast project and starts the evaluation of "
                "every item whose rules or content changed since it was last judged.",
                interval=live_cast.SWEEP_INTERVAL_SECONDS, timeout=live_cast.SWEEP_TIMEOUT_SECONDS)
    # Its worklist is the node rows' instances.
    if edition_features.enabled("cloud_nodes"):
        _system(NODE_SSM, "Node SSM access", _invoke_node_ssm,
                "Attaches the Jaah SSM instance profile to every cloud node the control plane launched that has "
                "none, so each one is reachable over AWS Systems Manager.",
                interval=node_instance_profile.INTERVAL_SECONDS, timeout=node_instance_profile.TIMEOUT_SECONDS)
    # The console toggle refreshes the allow set inline instead.
    if edition_features.enabled("tailnet"):
        _system(HEADSCALE_ALLOWLIST, "Headscale allowlist", _invoke_headscale_allowlist,
                "Writes the egress IPs of enabled nodes that reported in the last 7 days into this "
                "environment's headscale edge allow set.",
                interval=headscale_allowlist.INTERVAL_SECONDS, timeout=headscale_allowlist.TIMEOUT_SECONDS)
    if edition_features.enabled("system_mail"):
        _system(SES_INBOUND, "SES inbound mail", _invoke_ses_inbound,
                "Stores each message SES received for this environment's mail domain and routes it to the first "
                "matching email trigger, else Unmatched emails.",
                interval=SES_INBOUND_INTERVAL_SECONDS, timeout=SES_INBOUND_TIMEOUT_SECONDS)
    if edition_features.enabled("tenant_mail"):
        _system(TENANT_GMAIL_SYNC, "Tenant Gmail sync", _invoke_tenant_gmail_sync,
                "Reads each org's service-account Gmail mailbox for new mail and routes it to that org's matching "
                "email trigger, else its Unmatched emails. Skips orgs whose plan has no Tenant email.",
                interval=TENANT_GMAIL_SYNC_INTERVAL_SECONDS, timeout=TENANT_GMAIL_SYNC_TIMEOUT_SECONDS)
    if edition_features.enabled("system_mail"):
        _system(SES_EVENTS, "SES delivery events", _invoke_ses_events,
                "Records what SES reported for each system email (delivered, delayed, bounced, complained) in the "
                "delivery log, and suppresses an address after a hard bounce or a complaint.",
                interval=SES_INBOUND_INTERVAL_SECONDS, timeout=SES_INBOUND_TIMEOUT_SECONDS)
    # The corpus changes with a deploy, never with a task; the gap is for the operator who has just picked the
    # embeddings credential and wants the index built then, not at the next ten-minute tick. 30s bounds a
    # double-click to one pass.
    if edition_features.enabled("knowledge_base"):
        _system(SUPPORT_KB_INDEX, "Support knowledge base", _invoke_support_kb_index,
                "Embeds the shipped documentation into the support agent's knowledge base, adding what "
                "changed and dropping what the corpus no longer holds.",
                interval=support_kb_index.INTERVAL_SECONDS, timeout=support_kb_index.TIMEOUT_SECONDS,
                gap=support_kb_index.MIN_GAP_SECONDS)
        # `support-kb-index`'s gap, for its reason: Run now is the refresh path after a site deploy.
        _system(SUPPORT_KB_SITE, "Support knowledge base: jaah.ai", _invoke_support_kb_site,
                "Crawls the English pages of jaah.ai into the support agent's knowledge base, adding what "
                "changed and dropping what the site no longer publishes.",
                interval=site_crawl.INTERVAL_SECONDS, timeout=site_crawl.CRON_TIMEOUT_SECONDS,
                gap=support_kb_index.MIN_GAP_SECONDS)
        # Its worklist is the tickets a solve marked `pending` and the approved lessons; the gap is for Run now, after
        # an operator approves one and wants Jarvis to have it before the next tick.
        _system(SUPPORT_LESSONS, "Support lessons", _invoke_support_lessons,
                "Drafts a lesson from each solved support ticket for an operator to approve, and adds the approved "
                "ones to the support agent's knowledge base.",
                interval=support_lessons.INTERVAL_SECONDS, timeout=support_lessons.TIMEOUT_SECONDS,
                gap=support_kb_index.MIN_GAP_SECONDS)
    # No `pr-review` lane: it acted as a GitHub App installation, which the classic OAuth App lacks, so it was
    # quarantined (its seeded row deleted by revision 202610151700) and then deleted.
    _system(SECURITY_EVENT_EXPORT, "Security event export", _invoke_security_event_export,
            "Exports each day's security events (sign-ins, access and role changes, credential use) to "
            "the write-once archive bucket, filling any day a missed night left out.",
            cron=security_archive.CRON_EXPRESSION, timeout=security_archive.TIMEOUT_SECONDS,
            gap=security_archive.MIN_GAP_SECONDS)
    _system(METRICS_EMIT, "CloudWatch metrics", _invoke_metrics_emit,
            "Publishes cron starvation and node disk space as CloudWatch metrics every minute, for the "
            "paging alarms.",
            interval=metrics_emf.INTERVAL_SECONDS, timeout=metrics_emf.TIMEOUT_SECONDS, gap=metrics_emf.MIN_GAP_SECONDS)
    if edition_features.enabled("billing"):
        _system(BILLING_EVENTS, "Billing events", _invoke_billing_events,
                "Reads new events from the connected Stripe account and refreshes the billing mirrors from "
                "the objects they name.",
                interval=billing_events.INTERVAL_SECONDS, timeout=billing_events.TIMEOUT_SECONDS,
                gap=billing_events.MIN_GAP_SECONDS)
        _system(BILLING_RECONCILE, "Billing reconcile", _invoke_billing_reconcile,
                "Re-reads every open subscription, saved payment method and open invoice from the connected "
                "Stripe account, catching anything an event missed.",
                cron=billing_events.RECONCILE_CRON_EXPRESSION, timeout=billing_events.RECONCILE_TIMEOUT_SECONDS,
                gap=billing_events.RECONCILE_MIN_GAP_SECONDS)
        _system(BILLING_CARD_EXPIRY, "Billing card expiry", _invoke_billing_card_expiry,
                "Warns each org's Owners and Admins about saved cards that expire within 30 days, within 7 "
                "days, or have expired — once per card per threshold.",
                cron=billing_notices.EXPIRY_CRON_EXPRESSION, timeout=billing_notices.EXPIRY_TIMEOUT_SECONDS,
                gap=billing_notices.EXPIRY_MIN_GAP_SECONDS)
    if edition_features.enabled("jaah_ai_credit"):
        _system(BEDROCK_HEALTH, "Bedrock health", _invoke_bedrock_health,
                "Checks the platform Bedrock account, repairs the tenant deny policy and alerts the operator "
                "when the account becomes unavailable.",
                interval=BEDROCK_HEALTH_INTERVAL_SECONDS, timeout=BEDROCK_HEALTH_TIMEOUT_SECONDS,
                gap=BEDROCK_HEALTH_MIN_GAP_SECONDS)
    # Jaah AI credit's cadences: its ledger, its sessions and its metering (`app/token_billing/`, `app/jaah_routing/`).
    if edition_features.enabled("jaah_ai_credit"):
        from app.token_billing import audit as token_billing_audit

        _system(TOKEN_BILLING_AUDIT, "Token billing audit", _invoke_token_billing_audit,
                "Checks every Jaah AI balance against its ledger and alerts the operator if one disagrees.",
                cron=token_billing_audit.CRON_EXPRESSION, timeout=TOKEN_BILLING_AUDIT_TIMEOUT_SECONDS,
                gap=TOKEN_BILLING_AUDIT_MIN_GAP_SECONDS)
        _system(JAAH_SESSION_REAP, "Jaah session reap", _invoke_jaah_session_reap,
                "Ends Jaah AI account sessions that never asked for credentials, so they can no longer get any.",
                interval=JAAH_SESSION_REAP_INTERVAL_SECONDS, timeout=JAAH_SESSION_REAP_TIMEOUT_SECONDS,
                gap=JAAH_SESSION_REAP_MIN_GAP_SECONDS)
    if edition_features.enabled("cloud_nodes"):
        _system(NODE_ORDER_SWEEP, "Node orders", _invoke_node_order_sweep,
                "Ends Add-node orders that cannot finish: a self-hosted machine that never joined, and a payment "
                "nobody completed. Also finishes removing a deleted node whose host has stopped reporting, and reads "
                "why a launched cloud machine has not reported in from its boot log.",
                interval=NODE_ORDER_SWEEP_INTERVAL_SECONDS, timeout=NODE_ORDER_SWEEP_TIMEOUT_SECONDS)
    if edition_features.enabled("hostings"):
        _system(HOSTING_LIFECYCLE, "Hosting", _invoke_hosting_lifecycle,
                "Moves hosted machines and nodes through unpaid, stopped and deleted, and warns their owners first.",
                interval=HOSTING_LIFECYCLE_INTERVAL_SECONDS, timeout=HOSTING_LIFECYCLE_TIMEOUT_SECONDS)
    if edition_features.enabled("billing"):
        # Woken by `plan_terms.wake_bundles` after an approval, a plan move or a plan edit commits.
        _system(PLAN_BUNDLES, "Plan products", _invoke_plan_bundles,
                "Gives each organization the nodes and Jaah AI credit its plan includes, and releases the ones it "
                "no longer does.",
                interval=PLAN_BUNDLES_INTERVAL_SECONDS, timeout=PLAN_BUNDLES_TIMEOUT_SECONDS,
                gap=PLAN_BUNDLES_MIN_GAP_SECONDS)
        _system(PLAN_ROLLOVER, "Plan rollover", _invoke_plan_rollover,
                "Moves each organization whose plan has ended to the plan it names next, and emails its Owners and "
                "Admins.",
                interval=PLAN_ROLLOVER_INTERVAL_SECONDS, timeout=PLAN_ROLLOVER_TIMEOUT_SECONDS,
                gap=PLAN_ROLLOVER_MIN_GAP_SECONDS)
    _system(BUDGET_ALERTS, "Budget alerts", _invoke_budget_alerts,
            "Emails each organization's Owners and Admins when a project or group budget reaches 80% or 100% "
            "of its month.",
            interval=BUDGET_ALERTS_INTERVAL_SECONDS, timeout=BUDGET_ALERTS_TIMEOUT_SECONDS,
            gap=BUDGET_ALERTS_MIN_GAP_SECONDS)
    _system(API_KEYS_HOURLY, "API key expiry", _invoke_api_keys_hourly,
            "Reminds each API key's owner 7 days and 1 day before it expires, records each key that expired, and "
            "revokes the keys of a disabled member.",
            interval=API_KEYS_HOURLY_INTERVAL_SECONDS, timeout=API_KEYS_HOURLY_TIMEOUT_SECONDS,
            gap=API_KEYS_HOURLY_MIN_GAP_SECONDS)
    _system(COMMENT_IMAGE_REAP, "Comment image reap", _invoke_comment_image_reap,
            "Deletes each image staged for a comment that no post or edit claimed within a day, with its stored "
            "bytes.",
            interval=COMMENT_IMAGE_REAP_INTERVAL_SECONDS, timeout=COMMENT_IMAGE_REAP_TIMEOUT_SECONDS,
            gap=COMMENT_IMAGE_REAP_MIN_GAP_SECONDS)
    if edition_features.enabled("siem_export"):
        _system(SIEM_EXPORT, "SIEM export", _invoke_siem_export,
                "Posts each organization's new activity events as JSON to its SIEM destination, retrying until the "
                "destination accepts them.",
                interval=SIEM_EXPORT_INTERVAL_SECONDS, timeout=SIEM_EXPORT_TIMEOUT_SECONDS,
                gap=SIEM_EXPORT_MIN_GAP_SECONDS)
    # The matrix read reconciles too.
    if edition_features.enabled("compliance"):
        _system(COMPLIANCE_RECONCILE, "Compliance reconcile", _invoke_compliance_reconcile,
                "Marks a compliance scan's repo as errored when its auditor run ended without a result, and "
                "lapses expired waivers.",
                interval=scans.RECONCILE_INTERVAL_SECONDS, timeout=scans.RECONCILE_TIMEOUT_SECONDS)
        _system(COMPLIANCE_DAILY, "Compliance daily scan", _invoke_compliance_daily,
                "Rescans each organization's last compliance selection every day, where Compliance and its daily "
                "scan are on.",
                cron=scans.DAILY_CRON_EXPRESSION, timeout=scans.DAILY_TIMEOUT_SECONDS,
                gap=scans.DAILY_MIN_GAP_SECONDS)
    # No task transition raises an alarm. The relay reads the deployment's own CloudWatch alarms (`aws`).
    if edition_features.enabled("aws"):
        _system(ALARM_RELAY, "Alarm relay", _invoke_alarm_relay,
                "Posts each CloudWatch alarm and its resolution to Slack: the alert channel and every superadmin.",
                interval=ALARM_RELAY_INTERVAL_SECONDS, timeout=ALARM_RELAY_TIMEOUT_SECONDS)
    if edition_features.enabled("jaah_ai_credit"):
        _system(BEDROCK_USAGE_CORRECT, "Bedrock usage correction", _invoke_bedrock_usage_correct,
                "Reads AWS's Bedrock invocation log and corrects each Jaah AI call's charge by what AWS recorded.",
                interval=BEDROCK_USAGE_CORRECT_INTERVAL_SECONDS, timeout=BEDROCK_USAGE_CORRECT_TIMEOUT_SECONDS,
                gap=BEDROCK_USAGE_CORRECT_MIN_GAP_SECONDS)
    _system(TRIGGER_INTAKE, "Trigger intake", _invoke_trigger_intake,
            "Processes trigger deliveries: preflight, duplicate and limit checks, then files each Task in the "
            "trigger's project.",
            interval=TRIGGER_INTAKE_INTERVAL_SECONDS, timeout=TRIGGER_INTAKE_TIMEOUT_SECONDS,
            gap=TRIGGER_INTAKE_MIN_GAP_SECONDS)
    _system(TRIGGER_DIGEST, "Trigger digest", _invoke_trigger_digest,
            "Stops triggers with no successful run for their idle days, then emails each trigger owner a digest of "
            "the last 24 hours and adds it to their notifications.",
            cron=TRIGGER_DIGEST_CRON_EXPRESSION, timeout=TRIGGER_DIGEST_TIMEOUT_SECONDS,
            gap=TRIGGER_DIGEST_MIN_GAP_SECONDS)
    _system(OPS_SNAPSHOT, "Ops snapshot", _invoke_ops_snapshot,
            "Records node load, worktree occupancy, account quota and wallet floor each hour, kept a year for the "
            "operator dashboard's trends.",
            interval=OPS_SNAPSHOT_INTERVAL_SECONDS, timeout=OPS_SNAPSHOT_TIMEOUT_SECONDS, gap=OPS_SNAPSHOT_MIN_GAP_SECONDS)
    if edition_features.enabled("operator_dashboard"):
        _system(OUTCOME_REVIEW, "AI quality review", _invoke_outcome_review,
                "Each week, an AI model scores a sample of the tasks delivered in the last 7 days from 1 to 5, with "
                "a reason, for the operator dashboard's Quality panel.",
                cron=vocabulary.OUTCOME_REVIEW_CRON_EXPRESSION, timeout=OUTCOME_REVIEW_TIMEOUT_SECONDS,
                gap=OUTCOME_REVIEW_MIN_GAP_SECONDS)
    if edition_features.enabled("project_memory"):
        _system(MEMORY_CURATE, "Project memory curation", _invoke_memory_curate,
                "Turns what settled tasks recorded into project memory: mines their findings, distils transcripts "
                "when a model is set, then checks, merges and accepts proposed lessons.",
                interval=MEMORY_CURATE_INTERVAL_SECONDS, timeout=MEMORY_CURATE_TIMEOUT_SECONDS,
                gap=MEMORY_CURATE_MIN_GAP_SECONDS)
        _system(MEMORY_NIGHTLY, "Project memory nightly", _invoke_memory_nightly,
                "Each night, retires project memory lessons nobody used, merges duplicates, recomputes trust, picks "
                "each project's top lessons, and files a draft task for each lesson that has earned a home in the "
                "repo.",
                cron=MEMORY_NIGHTLY_CRON_EXPRESSION, timeout=MEMORY_NIGHTLY_TIMEOUT_SECONDS,
                gap=MEMORY_NIGHTLY_MIN_GAP_SECONDS)
    if edition_features.enabled("operator_dashboard"):
        _system(INSIGHT_DIGEST, "AI insight digest", _invoke_insight_digest,
                "Each week, an AI model summarises what changed on the operator dashboard and the likely causes, "
                "each point citing its metric, for the dashboard's Insights panel.",
                cron=vocabulary.INSIGHT_DIGEST_CRON_EXPRESSION, timeout=INSIGHT_DIGEST_TIMEOUT_SECONDS,
                gap=INSIGHT_DIGEST_MIN_GAP_SECONDS)
    if edition_features.enabled("jaah_ai_credit"):
        from app.token_billing import reconcile as token_billing_reconcile

        _system(BEDROCK_RECONCILE, "Bedrock cost reconciliation", _invoke_bedrock_reconcile,
                "Compares each organization's Jaah AI cost for yesterday with AWS's and alerts the operator when "
                "they differ by more than the tolerance.",
                cron=token_billing_reconcile.CRON_EXPRESSION, timeout=BEDROCK_RECONCILE_TIMEOUT_SECONDS,
                gap=BEDROCK_RECONCILE_MIN_GAP_SECONDS)
    # The SOC 2 evidence collectors: label and schedule from `collectors.REGISTRY`, so the two cannot disagree.
    if edition_features.enabled("compliance"):
        _system(EVIDENCE_SECURITY_ARCHIVE, evidence.collector("security-archive").label,
                _invoke_evidence_security_archive,
                "Checks that each of the last 30 days' security events is in the write-once archive and files the "
                "result as SOC 2 evidence; a missing day raises an alarm.",
                cron=evidence.collector("security-archive").cron, timeout=evidence.TIMEOUT_SECONDS,
                gap=evidence.MIN_GAP_SECONDS)
        _system(EVIDENCE_RETENTION_SWEEP, evidence.collector("retention-sweep").label,
                _invoke_evidence_retention_sweep,
                "Checks that the retention sweep completed in the last 26 hours and files the result as SOC 2 "
                "evidence; a missed sweep raises an alarm.",
                cron=evidence.collector("retention-sweep").cron, timeout=evidence.TIMEOUT_SECONDS,
                gap=evidence.MIN_GAP_SECONDS)
        _system(EVIDENCE_ACCESS_EVENTS, evidence.collector("access-events").label,
                _invoke_evidence_access_events,
                "Files a week's counts of role changes, failed sign-ins and credential reads as SOC 2 evidence.",
                cron=evidence.collector("access-events").cron, timeout=evidence.TIMEOUT_SECONDS,
                gap=evidence.MIN_GAP_SECONDS)
        _system(EVIDENCE_MFA_POSTURE, evidence.collector("mfa-posture").label,
                _invoke_evidence_mfa_posture,
                "Files the list of organizations with two-factor sign-in skipped as SOC 2 evidence.",
                cron=evidence.collector("mfa-posture").cron, timeout=evidence.TIMEOUT_SECONDS,
                gap=evidence.MIN_GAP_SECONDS)
        _system(EVIDENCE_GITHUB_SETTINGS, evidence.collector("github-settings").label,
                _invoke_evidence_github_settings,
                "Compares the repository's GitHub rulesets and settings with the declared ones and files the export "
                "as SOC 2 evidence; a difference raises an alarm.",
                cron=evidence.collector("github-settings").cron, timeout=evidence.TIMEOUT_SECONDS,
                gap=evidence.MIN_GAP_SECONDS)
        _system(EVIDENCE_DEPENDABOT, evidence.collector("dependabot").label,
                _invoke_evidence_dependabot,
                "Files the open Dependabot alerts and their age as SOC 2 evidence; an alert past the vulnerability "
                "fix deadline raises an alarm.",
                cron=evidence.collector("dependabot").cron, timeout=evidence.TIMEOUT_SECONDS,
                gap=evidence.MIN_GAP_SECONDS)
        _system(EVIDENCE_CLOUDWATCH_ALARMS, evidence.collector("cloudwatch-alarms").label,
                _invoke_evidence_cloudwatch_alarms,
                "Files every CloudWatch alarm and its state as SOC 2 evidence; an alarm firing outside an accepted "
                "risk raises an alarm.",
                cron=evidence.collector("cloudwatch-alarms").cron, timeout=evidence.TIMEOUT_SECONDS,
                gap=evidence.MIN_GAP_SECONDS)
        _system(EVIDENCE_PAGING_SUBSCRIPTIONS, evidence.collector("paging-subscriptions").label,
                _invoke_evidence_paging_subscriptions,
                "Files the paging topics' subscriptions as SOC 2 evidence; a topic nobody confirmed raises an alarm.",
                cron=evidence.collector("paging-subscriptions").cron, timeout=evidence.TIMEOUT_SECONDS,
                gap=evidence.MIN_GAP_SECONDS)
        _system(EVIDENCE_SECURITYHUB, evidence.collector("securityhub").label,
                _invoke_evidence_securityhub,
                "Files this account's open high and critical Security Hub findings as SOC 2 evidence; any finding "
                "raises an alarm.",
                cron=evidence.collector("securityhub").cron, timeout=evidence.TIMEOUT_SECONDS,
                gap=evidence.MIN_GAP_SECONDS)
        _system(EVIDENCE_EDGE_TLS, evidence.collector("edge-tls").label,
                _invoke_evidence_edge_tls,
                "Files the load balancers' TLS policies, HTTP redirects and web ACLs as SOC 2 evidence; a gap raises "
                "an alarm.",
                cron=evidence.collector("edge-tls").cron, timeout=evidence.TIMEOUT_SECONDS,
                gap=evidence.MIN_GAP_SECONDS)
        _system(EVIDENCE_BACKUPS, evidence.collector("backups").label,
                _invoke_evidence_backups,
                "Files the last day's backup and copy jobs and the database's Multi-AZ setting as SOC 2 evidence; a "
                "gap raises an alarm.",
                cron=evidence.collector("backups").cron, timeout=evidence.TIMEOUT_SECONDS,
                gap=evidence.MIN_GAP_SECONDS)
        _system(EVIDENCE_STORAGE_LIFECYCLE, evidence.collector("storage-lifecycle").label,
                _invoke_evidence_storage_lifecycle,
                "Files the transcripts bucket's expiry rules as SOC 2 evidence; a missing 180-day rule raises an "
                "alarm.",
                cron=evidence.collector("storage-lifecycle").cron, timeout=evidence.TIMEOUT_SECONDS,
                gap=evidence.MIN_GAP_SECONDS)
        _system(EVIDENCE_ECR_SCAN, evidence.collector("ecr-scan").label,
                _invoke_evidence_ecr_scan,
                "Files the deployed images' vulnerability scans as SOC 2 evidence; a high or critical finding past "
                "its fix deadline raises an alarm.",
                cron=evidence.collector("ecr-scan").cron, timeout=evidence.TIMEOUT_SECONDS,
                gap=evidence.MIN_GAP_SECONDS)
    # Its worklist is the `provisioning` NodeLinks and the `deleting` stores; a provision or delete request leaves
    # one there.
    if edition_features.enabled("nodelink"):
        _system(NODELINK_PROVISION, "NodeLink provisioning", _invoke_nodelink_provision,
                "Finishes provisioning every NodeLink someone asked to provision: its storage, access point and "
                "policy, until it is ready or AWS refuses. Finishes deleting storage someone deleted in its cloud.",
                interval=NODELINK_PROVISION_INTERVAL_SECONDS, timeout=NODELINK_PROVISION_TIMEOUT_SECONDS,
                gap=NODELINK_PROVISION_MIN_GAP_SECONDS)
    # Inside ECS's hour of memory for a stopped task, so no stop between two passes is lost.
    if edition_features.enabled("control_plane_view"):
        _system(CP_ECS_RECORD, "Control plane ECS tasks", _invoke_cp_ecs_record,
                "Records the control plane's own ECS tasks, with each stopped task's stop reason and exit codes, "
                "before ECS forgets them.",
                interval=cp_ecs.INTERVAL_SECONDS, timeout=cp_ecs.TIMEOUT_SECONDS)


_register_all()
