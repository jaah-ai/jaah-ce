"""The `script` preflight (architecture §5 "Repo script", C2c): run the owner's repo script on a node.

The CP reads no Git. It picks an online node that holds the project's main clone (a `worktrees` row
with `number = 0`) and sends `trigger_preflight` (`apps/jaahd/worktree_ops.py`) — a CP-internal node command,
absent from `POST /api/commands`' `VALID_TYPES`. The node refreshes a dedicated detached checkout at
`origin/<integration_branch>`, runs the script as its session user with a curated env, and replies with the exit
code, the stdout (capped) and the first stderr line; that reply is what this module reads, and the shared
`trigger_intake.parse_preflight_output` judges the stdout like every other preflight kind's.

What is "unavailable" (retried, not counted, until `trigger_intake.UNAVAILABLE_WINDOW`) versus a failure (counted
toward auto-pause) is decided here: no online node, a node too old to know the verb, or a node that is busy or
cannot switch to its session user is UNAVAILABLE; a node still creating the project's mirror (`mirror_bootstrapping`)
is PREPARING, retried for the clone budget (`trigger_intake.preparing_window`); everything the script or its
checkout said is a failure.

The input is `{"event","state","seen_keys"}` from `PreflightInput` — never the trigger's secret, which never
enters a delivery row (AC-8.3).
"""
from __future__ import annotations

import json
from dataclasses import asdict

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import forges, node_bus, node_presence
from app.models.automations import TaskTrigger
from app.models.nodes import Node
from app.models.projects import Project
from app.models.worktrees import Worktree
from app.node_clone import clone_for_slot_path, resolve_clone
from app.trigger_intake import (
    PreflightError,
    PreflightInput,
    PreflightPreparing,
    PreflightResult,
    PreflightUnavailable,
    parse_preflight_output,
    register_preflight,
)
from app.triggers import PREFLIGHT_SCRIPT

COMMAND = "trigger_preflight"
ACTOR = "system:trigger-preflight"
# The CP waits this much past the script's own timeout: the node's checkout refresh shares that budget, and the
# reply still has to travel.
DISPATCH_SLACK_SECONDS = 10
# The node's websocket client takes frames up to 1 MiB (the `websockets` default); the input travels JSON-escaped
# inside the command frame, so a larger one would close the node's socket instead of reaching the script.
MAX_DISPATCH_BYTES = 768 * 1024


def _pick_node(session: Session, project_id: int) -> tuple[Node, str]:
    """An enabled, online node holding the project's main clone, and that clone's path on it — preferring a node
    that is not draining. `PreflightUnavailable` when there is none."""
    online = set(node_presence.connected_node_ids(session))
    rows = session.execute(
        select(Node, Worktree.path)
        .join(Worktree, Worktree.node_id == Node.id)
        .where(Worktree.project_id == project_id, Worktree.number == 0, Node.enabled.is_(True))
        .order_by(Node.draining, Node.name)
    ).all()
    for node, path in rows:
        if str(node.id) in online:
            clone = clone_for_slot_path(node.jaah_home, path, 0) or resolve_clone(node.jaah_home, project_id)
            if clone:
                return node, clone
    raise PreflightUnavailable("no online node holds this project's repository")


def _dispatch(session: Session, node: Node, args: dict, timeout: float) -> dict:
    # Deferred: a service → routes import at module scope would cycle (`slot_teardown.py` does the same).
    from app.routes.commands import dispatch_and_audit_blocking  # noqa: PLC0415

    try:
        return dispatch_and_audit_blocking(session, str(node.id), COMMAND, args, ACTOR, timeout=timeout)
    except node_bus.NodeNotConnected:
        raise PreflightUnavailable(f"node {node.name} is not connected") from None
    except HTTPException as exc:
        if exc.status_code == 504:
            # The node took the command and went silent past the script's own deadline.
            raise PreflightError(f"timed out after {int(timeout - DISPATCH_SLACK_SECONDS)}s") from None
        raise PreflightUnavailable(f"node {node.name}: {exc.detail}") from None


def run_script_preflight(session: Session, trigger: TaskTrigger, preflight_input: PreflightInput) -> PreflightResult:
    """The `script` entry in `PREFLIGHT_RUNNERS`."""
    project = session.scalar(select(Project).where(Project.id == trigger.project_id))
    if project is None:
        raise PreflightError("the trigger's project no longer exists")
    script = (trigger.preflight_script_path or "").strip()
    if not script:
        raise PreflightError("no preflight script path is set")
    node, clone_path = _pick_node(session, project.id)
    ref = forges.repo_ref_or_none(project)
    args = {
        "project": project.name,
        # Keys the node's mirror (`<project_id>.git`); a node refuses a preflight without it.
        "project_id": project.id,
        "clone_path": clone_path,
        # The node fetches from this URL, never the clone's own `origin` (the node's mirror, `apps/jaahd/git_mirror.py`).
        "clone_url": ref.clone_url() if ref is not None else "",
        "integration_branch": project.integration_branch,
        "script_path": script,
        "timeout_seconds": trigger.preflight_timeout_seconds,
        "input": json.dumps(asdict(preflight_input)),
    }
    size = len(json.dumps(args).encode())
    if size > MAX_DISPATCH_BYTES:
        raise PreflightError(
            f"the preflight input is {size // 1024} KiB; a script preflight takes at most "
            f"{MAX_DISPATCH_BYTES // 1024} KiB (event, state and seen keys together)")
    reply = _dispatch(session, node, args, trigger.preflight_timeout_seconds + DISPATCH_SLACK_SECONDS)
    raw = reply.get("result")
    result: dict = raw if isinstance(raw, dict) else {}
    if "exit_code" not in result:
        # An older daemon answers `unknown command_type` with no exit code: not the script's answer.
        raise PreflightUnavailable(f"node {node.name}: {result.get('reason') or 'no result'}")
    detail = str(result.get("stderr") or "").strip()
    if result.get("unavailable"):
        if result.get("mirror_bootstrapping"):
            raise PreflightPreparing(f"node {node.name}: {detail}")
        raise PreflightUnavailable(f"node {node.name}: {detail}")
    if result.get("timed_out") or result.get("setup"):
        raise PreflightError(detail or "the preflight checkout could not be prepared")
    exit_code = result.get("exit_code")
    if exit_code != 0:
        raise PreflightError(f"exit {exit_code}: {detail}" if detail else f"exit {exit_code}")
    return parse_preflight_output(str(result.get("stdout") or ""))


register_preflight(PREFLIGHT_SCRIPT, run_script_preflight)
