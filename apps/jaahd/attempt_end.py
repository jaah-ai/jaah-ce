"""attempt_end — the ONE settle/fail ladder for an attempt's end.

Imported by the runner-status hook (`hooks/runner-status.py`, path-imported via the jaahd dir it appends
to `sys.path`) and by the ACP host (`acp/host.py`). The ladder: `task.get` -> not active: no-op; active
WITH evidence: re-post the terminal (`settle_task` commits then may refuse, so nobody else will); active
with NO evidence: a broken session — `record_break()` then `task.fail`, the cause riding in its reason.

Stdlib-only and raises nothing at import (a hook imports it; an import failure kills the session). Every
call goes through the daemon's socket (`agent_socket`); a refusal is never worked around.
"""
from __future__ import annotations

from typing import Callable

import agent_socket


def end_attempt(*, record_break: Callable[[], tuple[str, str, str]], log: Callable[[str], None],
                call=None) -> str:
    """Run the ladder once. Returns "noop" | "reposted" | "failed". `call` defaults to
    `agent_socket.call`, looked up at call time so a monkeypatch of that attribute still bites.
    `task.get` reads OUR task only; the daemon fills the id."""
    if call is None:
        call = agent_socket.call
    try:
        data = call("task.get")
    except agent_socket.AgentSocketRefused as exc:
        # The daemon answered no: `not_a_runner`, or `unavailable` (incl. the CP's 404 for a task no longer
        # open). No task state to act on, and a refusal is never a cue to find another way in.
        log(f"settle noop=get-refused reason={exc.reason} (nothing to settle)")
        return "noop"
    except agent_socket.AgentSocketUnavailable:
        log("settle noop=get-failed (best-effort)")
        return "noop"
    if not isinstance(data, dict) or not data:
        # No open task (the verb collapses 404, empty body and bad JSON into "no result").
        log("settle noop=empty-body (no open task)")
        return "noop"
    state = data.get("state") or ""
    pr = data.get("outcome_pr_url") or ""
    no_pr = data.get("outcome_no_pr_reason") or ""
    if state != "active":
        log(f"settle noop=not-active state={state or 'unknown'}")
        return "noop"
    if pr or no_pr:
        # Evidence recorded yet still `active`: `settle_task` COMMITS the payload and THEN may 422 on an
        # incomplete manifest, so nobody else will settle it and the reclaimer would requeue onto a merged
        # branch. Re-post the terminal from the row's own evidence — idempotent both ways.
        summary = (data.get("outcome_summary") or "").strip()
        evidence = (data.get("outcome_no_pr_evidence") or "").strip()
        if not pr and not evidence:
            # A `no_pr` terminal needs reason AND evidence (`task_settle.TERMINAL_REFERENCES`); this ladder
            # never invents evidence behind the claim that nothing merged. Post nothing; the row stays
            # `active` for the reclaimer guard.
            log("settle noop=no-pr-evidence-absent (a no_pr terminal needs reason AND evidence; the "
                "row carries only its reason, and this hook does not invent the other half)")
            return "noop"
        args = {
            # Non-empty (`SettleIn.summary` min_length=1); the fallback says who is speaking.
            "summary": summary or (
                "terminal evidence was recorded but the settle never landed — re-posted by the "
                "runner's exit hook"),
            "terminal_type": "merged_pr" if pr else "no_pr",
        }
        if pr:
            args["pr_url"] = pr
        else:
            # Both halves present (the branch above returned otherwise).
            args["no_pr_reason"] = no_pr
            args["no_pr_evidence"] = evidence
        try:
            # Past the default socket wait: a `merged_pr` settle first spends up to 20s reading the PR's
            # diff stats and provenance off the forge (`pr_stats_capture.BUDGET_S`).
            result = call("task.complete", args, timeout=30.0)
            settled = result.get("settled") if isinstance(result, dict) else None
            log(f"settle re-posted terminal={args['terminal_type']} settled={settled} "
                f"(evidence recorded, row still active)")
        except agent_socket.AgentSocketRefused as exc:
            # The row stays `active` with its evidence: the reclaimer guard skips it, and an incomplete
            # manifest has already alerted (`_raise_manifest_incomplete_alert`).
            log(f"settle re-post refused (daemon: {exc.reason}, non-fatal) — row left active with its "
                f"recorded evidence")
        except agent_socket.AgentSocketUnavailable as exc:
            log(f"settle re-post failed (daemon unreachable: {exc}, non-fatal)")
        return "reposted"
    # Active, no evidence -> THE SESSION BROKE. Classify, record, then fail cleanly so the row never
    # lingers active.
    cause, detail, session_id = record_break()
    # `session_id` fences this /fail to THIS attempt (the daemon pins the task; this pins the attempt). Empty
    # means "no fence", the CP's compatibility default.
    try:
        call(
            "task.fail",
            {
                "reason": f"broken session: {cause}" + (f" — {detail}" if detail else ""),
                "session_id": session_id,
            },
        )
        log(f"settle posted /fail (active, no evidence — broken session: {cause})")
    except agent_socket.AgentSocketRefused as exc:
        log(f"settle /fail best-effort failed (daemon refused: {exc.reason}, non-fatal)")
    except agent_socket.AgentSocketUnavailable:
        log("settle /fail best-effort failed (non-fatal)")
    return "failed"
