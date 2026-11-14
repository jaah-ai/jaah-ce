"""The tracker intake's words — stdlib only, so `app/models/trackers.py` renders its CHECKs from these sets and every
other module names them instead of re-listing them."""
from __future__ import annotations

from typing import Any

from app.triggers import TRACKER_SOURCE_KINDS

TRACKER_CONNECTIONS_TABLE = "tracker_connections"
TRACKER_ISSUE_LINKS_TABLE = "tracker_issue_links"
TRACKER_WRITEBACKS_TABLE = "tracker_writebacks"
TRACKER_MAPPINGS_TABLE = "tracker_mappings"

# A tracker is its trigger's source kind: one name, one home (`app/triggers.py`).
TRACKERS = TRACKER_SOURCE_KINDS
TRACKER_LABELS = {"linear": "Linear", "jira": "Jira", "backlog": "Backlog"}
assert tuple(TRACKER_LABELS) == TRACKERS, "every tracker needs exactly one label"

# How the connection authenticates to the tracker: the tracker's OAuth (a credential), an API token or basic login
# (a credential), or a Jira Forge app's token (sealed on the row, minted by the app after it claims the connection).
AUTH_OAUTH = "oauth"
AUTH_BASIC = "basic"
AUTH_FORGE = "forge"
AUTH_KINDS = (AUTH_OAUTH, AUTH_BASIC, AUTH_FORGE)

# `IssueEvent.kind`: what happened on the issue.
KIND_ASSIGNED = "assigned"
KIND_UNASSIGNED = "unassigned"
KIND_MENTIONED = "mentioned"
KIND_LABELED = "labeled"
KIND_UNLABELED = "unlabeled"
KIND_COMMENT = "comment"
START_KINDS = (KIND_ASSIGNED, KIND_MENTIONED, KIND_LABELED)
STOP_KINDS = (KIND_UNASSIGNED, KIND_UNLABELED)
EVENT_KINDS = START_KINDS + STOP_KINDS + (KIND_COMMENT,)

# `tracker_writebacks.kind`: each posted back to the issue once per (task, kind, ref).
WRITEBACK_ACK = "ack"
WRITEBACK_PROGRESS = "progress"
WRITEBACK_QUESTION = "question"
WRITEBACK_PR_OPENED = "pr_opened"
WRITEBACK_OUTCOME = "outcome"
WRITEBACK_DONE = "done"
WRITEBACK_KINDS = (
    WRITEBACK_ACK, WRITEBACK_PROGRESS, WRITEBACK_QUESTION, WRITEBACK_PR_OPENED, WRITEBACK_OUTCOME, WRITEBACK_DONE,
)
# `tracker_issue_links.origin`: how the task came to be linked — started by the tracker's intake, or linked by hand.
ORIGIN_INTAKE = "intake"
ORIGIN_MANUAL = "manual"
LINK_ORIGINS = (ORIGIN_INTAKE, ORIGIN_MANUAL)
# `tracker_issue_links.writeback_scope`: what the task posts back to its issue — everything, or the outcome only.
SCOPE_FULL = "full"
SCOPE_OUTCOME = "outcome"
WRITEBACK_SCOPES = (SCOPE_FULL, SCOPE_OUTCOME)
# The tracker's done state a merged PR moves the issue to when the connection names none (`settings.done_state`).
DEFAULT_DONE_STATE = "Done"
# The one reply a comment from someone who may not act on the issue's task gets (`trackers.replies`).
NOT_ALLOWED_REPLY = "Not allowed to act on this task."

# `tracker_connections.settings`: every key and its default. `mappings` is on the wire only, read from the primary
# `tracker_mappings` rows (`trackers.project_links.mappings_wire`); `allow_unmapped_users` lets a tracker user with
# no matching org member start a task.
DEFAULT_START_LABEL = "jaah"
DEFAULT_SETTINGS: dict[str, Any] = {
    "mappings": [], "start_label": DEFAULT_START_LABEL, "done_state": None, "allow_unmapped_users": False,
}

# A claim code (Jira Forge) is good for this long after connect.
CLAIM_CODE_TTL_HOURS = 24
