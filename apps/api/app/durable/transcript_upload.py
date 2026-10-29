"""The manifest PUT and the flip that makes it count, as one durable pair.

`routes/agent_sessions.py::_complete_upload` commits (so no row lock is held across S3), PUTs
`manifest.json`, then flips `transcript_status` to `uploaded` under a lease fence. A death between PUT and
flip leaves a complete capture in S3 while `_reclaim_expired_leases` later marks the row `failed`/`gave_up`:
the sweep reaches a false conclusion rather than recovering work, which is why this is durable. The PUT is a
checkpointed step, so recovery resumes at the flip without re-PUTting.

Called synchronously, not queued: the node's `POST /transcript` needs the answer before a 204. A direct
`SetWorkflowID` attach to a running run blocks in an untimed poll, so `_settling_elsewhere` refuses
duplicates first (`IN_FLIGHT`). Nothing secret is checkpointed (`app/durable/accounts.py`).
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional

from dbos import DBOS, SetWorkflowID, WorkflowStatusString

from app.durable import runtime

# Own logger, so engine faults here filter apart from the route's malformed-report logs.
log = logging.getLogger("transcript-upload")

# Outcomes the route acts on. `CAPTURED` and `RECLAIMED` both mean the manifest is in the bucket; they
# differ in whether the row was still ours to flip.
# `FAILED` (nothing reached the bucket) hands the row back charging nothing, so a CP outage cannot burn
# attempts. `FLIP_FAILED` (bytes landed, the DB write failed) charges an attempt via `_fail`, so a
# deterministic fault reaches `gave_up` instead of re-uploading forever.
# `IN_FLIGHT`: another report under this lease is settling the row (see `_settling_elsewhere`).
CAPTURED = "captured"
RECLAIMED = "reclaimed"
FAILED = "failed"
FLIP_FAILED = "flip_failed"
IN_FLIGHT = "in_flight"


class FlipFailed(Exception):
    """A fault in `mark_uploaded`: the manifest is in the bucket and the row could not be told.

    A type, because the step that raised is known only inside the workflow and acted on in
    `finish_upload`. The cause is in the message, not only `__cause__`: DBOS pickles the terminal error
    for later attaches, and pickling keeps `args` but drops the cause chain."""


def upload_workflow_id(session_id: str, attempt: datetime) -> str:
    """The deterministic ID every report settling one upload attempt must agree on (pure, testable
    without DBOS).

    Keyed on the lease stamp, not `transcript_attempts`: `SetWorkflowID` re-returns a recorded result
    without re-running, and CP-side failures charge no attempt, so a counter-keyed id would re-raise a
    transient S3 error forever and never reach `gave_up`. `transcript_leased_at` is re-stamped by every
    `upload_ticket`, and duplicates of one upload share it.

    Never NULL here: `ck_sessions_uploading_holds_a_lease` guarantees a stamp at `uploading`.
    """
    return f"transcript-upload:{session_id}:{attempt.isoformat()}"


# A new workflow is no reason to bump `runtime.ACCOUNTS_WORKFLOW_VERSION` (see
# `app/durable/task_title_queue.py`); the rolling-deploy residual is self-correcting, and meanwhile the
# reclaim protects the row's liveness.


@DBOS.step(retries_allowed=False)
def put_manifest(
    session_id: str, region: str, bucket: str, key: str, manifest_bytes: bytes
) -> None:
    """PUT `manifest.json`. Returns nothing on purpose: the checkpoint is the receipt.

    Everything this touches is secret (the AWS credential, the presigned URL); step outputs and workflow
    arguments are checkpointed unpruned, so the credential is resolved here, not passed in (it would be
    expired on recovery anyway). Bucket and region travel as arguments so a replay writes where the row
    will record. No session: this step reads no application row.

    Retries are off: the PUT budget is ~15s (`_MANIFEST_PUT_TIMEOUT`) on a shared anyio worker thread; a
    raise reaches `_record_error` (`failed`, uncharged) and the node's next tick retries.
    """
    # Deferred: imported at DBOS registration time, before the app is wired.
    from app import session_transcript_archive as archive
    from app.routes import agent_sessions

    # Through the module, not copied: `put_object` is the single home of the presign/header rule, and
    # tests patch `agent_sessions.put_object`.
    if archive.on_volume(bucket):
        archive.write_volume(key, manifest_bytes)
        log.info("transcript-upload: %s — manifest.json written to the local volume at %s", session_id, key)
        return
    found = archive.storage()
    if found is None:
        # Storage lost since the route's check: raise to `_record_error` (uncharged). Leaving the
        # row `uploading` would let the always-charging reclaim `gave_up` a captured transcript.
        raise RuntimeError(
            "transcript storage became unavailable before the manifest was written"
        )
    agent_sessions.put_object(
        bucket=bucket, region=region, key=key, body=manifest_bytes, credentials=found[2]
    )
    log.info("transcript-upload: %s — manifest.json written to s3://%s/%s", session_id, bucket, key)


@DBOS.step(retries_allowed=False)
def mark_uploaded(
    row_id: str,
    session_id: str,
    attempt: datetime,
    bucket: str,
    prefix: str,
    key: str,
    manifest_bytes: bytes,
    transcript_bytes: int,
) -> bool:
    """Flip the row to `uploaded`, fenced on it still holding the lease this upload was ticketed under.

    The fence is the route's own `_still_leased_id` (imported, not copied), since the pre-PUT commit lets
    `_reclaim_expired_leases` race this. `attempt` is part of the fence because a recovered step may wake
    after a reclaim and re-ticket put a different attempt at `uploading`; it travels as a checkpointed
    argument because a re-read would return the displacing lease. See `_still_leased_id`.

    On zero rowcount, a read-back tells a reclaim from a replay of an already-committed flip. It compares
    `transcript_sha256` (the digest of this workflow's manifest bytes), not the manifest key, which is
    identical across attempts.

    The org is bound via `bind_from` (unbound worker thread; `operator_scope()` would be too broad). No
    `rollback()`: nothing slow follows.
    """
    from uuid import UUID

    from sqlalchemy import select, update

    from app import session_transcripts as transcripts
    from app.agent_sessions import TRANSCRIPT_UPLOADED
    from app.entity_shape import SYSTEM_ACTOR_ID
    from app.models.agent_sessions import AgentSession
    from app.routes.agent_sessions import _still_leased_id, session_changed
    from app.tenancy import seam

    row_uuid = UUID(row_id)
    # Digest of the manifest bytes the CP wrote, computed once from the same bytes `put_manifest` got.
    digest = transcripts.manifest_digest(manifest_bytes)
    # Session row pruned between report and step: nothing to flip; raising would burn recovery attempts.
    # Reported as not flipped.
    gone = (
        f"transcript-upload: session {session_id} no longer exists — its manifest is in the bucket but "
        "there is no row left to mark `uploaded`"
    )
    with (
        runtime.step_session("durable:transcript_upload.mark_uploaded", gone=gone, logger=log) as session,
        seam.bind_from(session, AgentSession, row_uuid),
    ):
        settled = session.execute(
            update(AgentSession).where(*_still_leased_id(row_uuid, attempt)).values(
                transcript_bucket=bucket,
                transcript_prefix=prefix,
                transcript_manifest_key=key,
                transcript_bytes=transcript_bytes,
                transcript_sha256=digest,
                transcript_status=TRANSCRIPT_UPLOADED,
                # Cleared: `uploaded` holds no lease.
                transcript_leased_at=None,
                transcript_error=None,
                updated_by_id=SYSTEM_ACTOR_ID,
            )
        ).rowcount
        if settled:
            # Emit only when the write landed, on this session so the NOTIFY is transactional
            # with the flip.
            session_changed(session)
            session.commit()
            return True
        # Digest, not key (see the docstring).
        already = session.scalar(
            select(AgentSession.transcript_sha256).where(
                AgentSession.id == row_uuid,
                AgentSession.transcript_status == TRANSCRIPT_UPLOADED,
            )
        )
        session.commit()
        return already == digest
    return False


@DBOS.workflow()
def finish_transcript_upload(
    row_id: str,
    session_id: str,
    attempt: datetime,
    region: str,
    bucket: str,
    prefix: str,
    key: str,
    manifest_bytes: bytes,
    transcript_bytes: int,
) -> bool:
    """One durable completion for one upload attempt. True when the row reached `uploaded`.

    Imported by `app/durable/runtime.py` for its decorator side effect (registration is required for
    start and recovery).

    Two steps, no work between: the PUT is checkpointed so recovery skips it, and the flip is its own
    step so recovery retries it. Unlike the other workflows, it does not re-read from the DB: the bytes
    already in S3 are what the recorded digest must match, and `attempt` is the fence.

    The flip's raise is re-raised as `FlipFailed` so `finish_upload` can charge the right party. Both steps
    are `retries_allowed=False`, so exceptions arrive unwrapped; if a ladder is ever added, messages must
    go through `app/durable/task_done.py::_failure_reason`.
    """
    put_manifest(session_id, region, bucket, key, manifest_bytes)
    try:
        return mark_uploaded(
            row_id, session_id, attempt, bucket, prefix, key, manifest_bytes, transcript_bytes
        )
    except Exception as exc:
        # Cause interpolated (see `FlipFailed`); `from exc` kept for the live traceback. No URL can
        # enter: this step signs nothing.
        raise FlipFailed(f"{type(exc).__name__}: {exc}") from exc


# States in which an attach would block in `await_workflow_result`; read off DBOS's enum.
_UNSETTLED_WORKFLOW_STATES = frozenset({
    WorkflowStatusString.PENDING.value,
    WorkflowStatusString.ENQUEUED.value,
    WorkflowStatusString.DELAYED.value,
})


def _settling_elsewhere(workflow_id: str) -> bool:
    """Is a workflow under this id already running, so that attaching to it would block?

    Refuses only non-terminal states; a terminal run returns its recorded result and must still be
    attached. Fails open: a failed status read proceeds, since refusing would turn an engine outage from
    `FAILED` (uncharged) into `IN_FLIGHT` (parked for the reclaim); `TestClient` never launches DBOS. A
    winner starting after this read still blocks the loser, but only for its own ~15s budget.
    """
    try:
        status = DBOS.get_workflow_status(workflow_id)
    except Exception as exc:  # noqa: BLE001 — see the docstring: this read may not decide anything
        log.debug(
            "transcript-upload: could not read the status of %s (%s: %s); proceeding",
            workflow_id, type(exc).__name__, exc,
        )
        return False
    return status is not None and status.status in _UNSETTLED_WORKFLOW_STATES


def finish_upload(
    *,
    row_id: str,
    session_id: str,
    attempt: datetime,
    region: str,
    bucket: str,
    prefix: str,
    key: str,
    manifest_bytes: bytes,
    transcript_bytes: int,
) -> tuple[str, Optional[str]]:
    """Run the durable completion. Returns `(outcome, detail)`; `detail` is set only for `FAILED` and
    `FLIP_FAILED`.

    Does not raise into the report path: the route has already committed and the node learns the outcome
    by being re-offered the row, so an engine fault must cost a manifest, not a 500. Exceptions: the
    `upload_workflow_id` call is outside the guard (a bad argument is a caller bug), and `BaseException`
    is not caught.

    `detail` is rendered on Audit > Sessions: exception type and text, never a URL (a presigned
    URL is a bearer token). `FAILED` goes through `_record_error`, `FLIP_FAILED` through `_fail`.
    `RECLAIMED` is not a failure: the manifest landed; the row is owed again and the next attempt
    overwrites the same key.
    """
    workflow_id = upload_workflow_id(session_id, attempt)
    if _settling_elsewhere(workflow_id):
        return IN_FLIGHT, None
    try:
        with SetWorkflowID(workflow_id):
            # The module global, resolved at call time, so tests can substitute the undecorated body.
            settled = finish_transcript_upload(
                row_id,
                session_id,
                attempt,
                region,
                bucket,
                prefix,
                key,
                manifest_bytes,
                transcript_bytes,
            )
    # Order is the classification: only `FlipFailed` is raised after a successful PUT.
    except FlipFailed as exc:
        return FLIP_FAILED, f"the manifest was written but the row could not be marked `uploaded`: {exc}"
    except Exception as exc:  # noqa: BLE001 — see the docstring: this never fails a node's report
        # DBOS records a step's escaped raise in `durable_dbos_sys` (shown on the workflows page) before
        # route redaction, so messages must be URL-free at the raise: `put_object` raises a status-only
        # `RuntimeError` and httpx transport errors omit the URL. Keep new failure modes URL-free.
        return FAILED, f"writing manifest.json failed: {type(exc).__name__}: {exc}"
    return (CAPTURED if settled else RECLAIMED), None
