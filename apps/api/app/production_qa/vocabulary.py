"""The production-QA vocabulary — stdlib-only, so `app/models/production_qa.py` renders its CHECKs from these
tuples and every other module names them instead of re-listing them (the `app/triggers.py` pattern).

The rules these words take part in are the package docstring's (`app/production_qa/__init__.py`).
"""
from __future__ import annotations

from datetime import datetime, timezone, tzinfo
from typing import Optional, cast

# `deployments.status`: one workflow run attempt's outcome (the conclusion always wins; architecture A-4).
DEPLOYMENT_STATUSES = ("queued", "in_progress", "success", "failure", "cancelled", "skipped")
# `deployments.range_status`: how the commit range back to the previous production anchor was found.
RANGE_STATUSES = ("first", "resolved", "partial", "pending", "rollback")
# `deployments.regression_status`: whether the post-deploy regression window was measured.
REGRESSION_STATUSES = ("pending", "measured", "not_measured")
# `deployment_prs.source`: which control-plane record mapped a merge SHA to its PR.
PR_SOURCES = ("merge_lane", "pr_changes")
# `deployment_metrics.metric`.
METRICS = ("error_rate", "p95_latency_ms")

# `incidents.type`.
INCIDENT_TYPES = ("monitor", "deploy_failed", "health_breach", "regression", "lane_slowdown")
# `incidents.severity`, lowest first: a threshold admits its own word and every word after it.
SEVERITIES = ("debug", "info", "warning", "error", "critical")


def severity_at_least(severity: str, threshold: str) -> bool:
    """Whether `severity` meets `threshold` on the `SEVERITIES` scale; an unknown word on either side is a ValueError."""
    return SEVERITIES.index(severity) >= SEVERITIES.index(threshold)


# `incidents.state`.
INCIDENT_STATES = ("open", "resolved", "reopened")
# `incidents.count_mode`: `absolute` keeps the larger of stored and sent; `increment` adds one per occurrence.
COUNT_MODES = ("absolute", "increment")
# `incident_events.kind`.
INCIDENT_EVENT_KINDS = ("opened", "occurred", "resolved", "reopened", "ignored_stale")
# `incident_suspects.basis` / `.confidence` / `.state`.
SUSPECT_BASES = ("release", "deployment", "window_paths", "window", "operator")
SUSPECT_CONFIDENCES = ("high", "medium", "low")
SUSPECT_STATES = ("suggested", "confirmed", "dismissed")
# `incident_tasks.role`. A `revert` claim never blocks dedup (the partial unique index excludes it).
INCIDENT_TASK_ROLES = ("triage", "fix", "revert")

# `production_qa_edges.kind`: the facts the outcome ledger reads.
EDGE_KINDS = ("deployed", "incident_linked", "resolved_by_fix")

# `merge_lane_stage_timings.outcome`.
STAGE_OUTCOMES = ("passed", "failed", "flaked")

# `projects.revert_policy`.
REVERT_POLICIES = ("off", "draft", "auto_file")
REVERT_POLICY_DEFAULT = "off"

# `trigger_deliveries.reason` codes this epic writes (the delivery-level vocabulary, not `automation_fire.REASONS`),
# each with the words the run log shows (`delivery_reason_label`).
REASON_RECORDED = "recorded"
REASON_RECORD_ERROR = "record_error"
REASON_PRODUCTION_QA_OFF = "production_qa_off"
REASON_PROJECT_NOT_OPTED_IN = "project_not_opted_in"
REASON_BELOW_SEVERITY = "below_severity"
REASON_TYPE_NOT_SELECTED = "type_not_selected"
REASON_ENVIRONMENT_NOT_SELECTED = "environment_not_selected"
REASON_INCIDENT_TASK_OPEN = "incident_task_open"
REASON_INCIDENT_RESOLVED = "incident_resolved"
REASON_HELD_QUIET_HOURS = "held_quiet_hours"
REASON_REOPEN_COOLDOWN = "reopen_cooldown"
DELIVERY_REASON_LABELS = {
    REASON_RECORDED: "Recorded",
    REASON_RECORD_ERROR: "Recording failed",
    REASON_PRODUCTION_QA_OFF: "Production QA off",
    REASON_PROJECT_NOT_OPTED_IN: "Project not opted in",
    REASON_BELOW_SEVERITY: "Below the severity threshold",
    REASON_TYPE_NOT_SELECTED: "Incident type not selected",
    REASON_ENVIRONMENT_NOT_SELECTED: "Environment not selected",
    REASON_INCIDENT_TASK_OPEN: "A task for this incident is already open",
    REASON_INCIDENT_RESOLVED: "Incident already resolved",
    REASON_HELD_QUIET_HOURS: "Held: quiet hours",
    REASON_REOPEN_COOLDOWN: "Reopened inside the cooldown",
}


def delivery_reason_label(reason: Optional[str], not_before: Optional[datetime] = None,
                          tz: Optional[str] = None) -> Optional[str]:
    """The run log's words for a delivery `reason`: this epic's codes in words (a hold names when it ends, in the
    quiet hours' own zone `tz` when known, else UTC), any other reason — already a sentence — unchanged."""
    if reason == REASON_HELD_QUIET_HOURS and not_before is not None:
        zone_name, zone = "UTC", cast(tzinfo, timezone.utc)
        if tz:
            from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

            try:
                zone_name, zone = tz, ZoneInfo(tz)
            except (ZoneInfoNotFoundError, ValueError):
                pass
        return f"{DELIVERY_REASON_LABELS[reason]} until {not_before.astimezone(zone):%H:%M} {zone_name}"
    return DELIVERY_REASON_LABELS.get(reason, reason) if reason else reason
