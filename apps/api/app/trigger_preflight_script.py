"""The `script` preflight (architecture §5 "Repo script", C2c; standalone clones, C5): run the owner's repo script
on a node.

The CP reads no Git. It picks an online, enabled node the project may start work on (`slot_allocation.placement_node_ids`)
and sends `trigger_preflight` (`apps/jaahd/worktree_ops.py`) — a CP-internal node command,
absent from `POST /api/commands`' `VALID_TYPES`. The node makes a throwaway shallow clone of the integration branch
from the forge, runs the script there as its session user with a curated env, removes the clone, and replies with
the exit code, the stdout (capped) and the first stderr line; that reply is what this module reads, and the shared
`trigger_intake.parse_preflight_output` judges the stdout like every other preflight kind's.

THE CLONE'S CREDENTIAL is the trigger OWNER'S git login (D14), and the only way a node gets it is the **grant** this
module mints into the dispatch: an HMAC-signed blob naming the trigger, the node, the project and an expiry. The
node's credential broker trades it at `GET /api/agent/trigger-preflights/git-token`
(`routes/agent/task_secrets.py::get_trigger_preflight_git_token`), which checks the MAC, the expiry and the
calling node (`verify_grant`) and then the trigger itself before minting. A replay of a grant by the same node
inside its expiry is an accepted residual (R-7).

What is "unavailable" (retried, not counted, until `trigger_intake.UNAVAILABLE_WINDOW`) versus a failure (counted
toward auto-pause) is decided here: no online node, a node too old to know the verb, or a node that cannot switch
to its session user is UNAVAILABLE; everything the script or its clone said is a failure.

The input is `{"event","state","seen_keys"}` from `PreflightInput` — never the trigger's secret, which never
enters a delivery row (AC-8.3).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import time
from dataclasses import asdict, dataclass
from typing import Optional
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import forges, node_bus, node_credentials, node_presence, signed_claims, slot_allocation
from app.models.automations import TaskTrigger
from app.models.nodes import Node
from app.models.projects import Project
from app.trigger_intake import (
    PreflightError,
    PreflightInput,
    PreflightResult,
    PreflightUnavailable,
    parse_preflight_output,
    register_preflight,
)
from app.triggers import PREFLIGHT_SCRIPT

COMMAND = "trigger_preflight"
ACTOR = "system:trigger-preflight"
# The CP waits this much past the script's own timeout: the node's clone shares that budget, and the reply still
# has to travel.
DISPATCH_SLACK_SECONDS = 10
# The node's websocket client takes frames up to 1 MiB (the `websockets` default); the input travels JSON-escaped
# inside the command frame, so a larger one would close the node's socket instead of reaching the script.
MAX_DISPATCH_BYTES = 768 * 1024
# What a daemon older than the grant refuses a dispatch without (`trigger_preflight needs <arg>`).
_PRE_GRANT_ARGS = ("clone_path", "project_id")


# ── The grant: what lets ONE node mint ONE trigger owner's git login, briefly ─────────────────────────────

GRANT_VERSION = 1
# Its own domain, so no other MAC derived from the session secret (`console_direct._signing_key`) verifies as one.
_GRANT_DOMAIN = b"trigger-preflight-grant/v1"
# How long a grant outlives the script's own timeout: the clone and the reply share that budget, and this is slack.
GRANT_SLACK_SECONDS = 60
# Without a session secret (a local stack) grants verify only on the replica that minted them.
_EPHEMERAL_KEY = secrets.token_bytes(32)


@dataclass(frozen=True)
class Grant:
    """A verified grant's claims."""

    trigger_id: UUID
    node_id: UUID
    project_id: int


def _grant_key() -> bytes:
    secret = os.environ.get("SESSION_SECRET", "")
    # The published dev constant counts as unset, as in `node_credentials._session_secret_or_ephemeral`.
    usable = secret and secret != node_credentials._PUBLISHED_DEV_SESSION_SECRET
    return hmac.new(secret.encode() if usable else _EPHEMERAL_KEY, _GRANT_DOMAIN, hashlib.sha256).digest()


def mint_grant(trigger: TaskTrigger, node: Node, project: Project, *, timeout_seconds: int,
               now: Optional[float] = None) -> str:
    """A `signed_claims` token of `{v, trigger_id, node_id, project_id, repo, exp}`. `repo` is always `primary` —
    the preflight clones the project's primary repository only."""
    exp = int((now if now is not None else time.time()) + timeout_seconds + GRANT_SLACK_SECONDS)
    claims = {"v": GRANT_VERSION, "trigger_id": str(trigger.id), "node_id": str(node.id),
              "project_id": project.id, "repo": "primary", "exp": exp}
    return signed_claims.mint(_grant_key(), claims)


def verify_grant(grant: str, *, node_id: Optional[UUID], now: Optional[float] = None) -> Optional[Grant]:
    """The claims of `grant` when its MAC is ours, it has not expired and it names `node_id`, else None. Never
    says which check failed: a caller learns nothing by probing."""
    claims = signed_claims.open_claims(_grant_key(), grant)
    if claims is None:
        return None
    try:
        if claims.get("v") != GRANT_VERSION or claims.get("repo") != "primary":
            return None
        exp, project_id = claims["exp"], claims["project_id"]
        if not isinstance(exp, int) or not isinstance(project_id, int) or isinstance(project_id, bool):
            return None
        claimed = Grant(trigger_id=UUID(claims["trigger_id"]), node_id=UUID(claims["node_id"]), project_id=project_id)
    except (ValueError, TypeError, KeyError):
        return None
    if exp <= (now if now is not None else time.time()) or node_id is None or claimed.node_id != node_id:
        return None
    return claimed


# ── The venue ───────────────────────────────────────────────────────────────────────────────────────────────

def _pick_node(session: Session, project_id: int) -> Node:
    """An enabled, online node the project may start work on (`slot_allocation.placement_node_ids`), preferring one
    that is not draining. Never a Multi-Node Host (it runs no sessions) nor a MicroVM node (whose sessions run
    only inside a VM; this script would run on its host). `PreflightUnavailable` when there is none."""
    assigned = slot_allocation.placement_node_ids(session, project_id)
    if not assigned:
        raise PreflightUnavailable("no node runs this project")
    online = set(node_presence.connected_node_ids(session))
    nodes = session.scalars(
        select(Node)
        .where(Node.enabled.is_(True), Node.microvm.is_(False), Node.id.in_(assigned))
        .order_by(Node.draining, Node.name)
    ).all()
    for node in nodes:
        if str(node.id) in online and not slot_allocation.is_host(session, node.id):
            return node
    raise PreflightUnavailable("no online node runs this project")


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
    node = _pick_node(session, project.id)
    ref = forges.repo_ref_or_none(project)
    args = {
        "project": project.name,
        # The node clones from this URL, as the trigger's owner (`grant`).
        "clone_url": ref.clone_url() if ref is not None else "",
        "integration_branch": project.integration_branch,
        "script_path": script,
        "timeout_seconds": trigger.preflight_timeout_seconds,
        "input": json.dumps(asdict(preflight_input)),
        "grant": mint_grant(trigger, node, project, timeout_seconds=trigger.preflight_timeout_seconds),
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
    if result.get("setup") and any(f"trigger_preflight needs {arg}" in detail for arg in _PRE_GRANT_ARGS):
        # A daemon older than the grant (it still wants the main clone): not the script's answer, and not counted.
        raise PreflightUnavailable(f"node {node.name} runs a daemon too old for this preflight; upgrade it")
    if result.get("unavailable"):
        raise PreflightUnavailable(f"node {node.name}: {detail}")
    if result.get("timed_out") or result.get("setup"):
        raise PreflightError(detail or "the preflight clone could not be prepared")
    exit_code = result.get("exit_code")
    if exit_code != 0:
        raise PreflightError(f"exit {exit_code}: {detail}" if detail else f"exit {exit_code}")
    return parse_preflight_output(str(result.get("stdout") or ""))


register_preflight(PREFLIGHT_SCRIPT, run_script_preflight)
