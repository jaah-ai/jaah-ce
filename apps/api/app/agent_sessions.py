"""The closed vocabularies the `sessions` table carries — the single home of all three.

A leaf module: imports nothing from `app` at module scope, because revision 0135 reads its CHECK
domains from here. `STATUSES` and `TRANSCRIPT_STATUSES` are DB CHECKs; `END_REASONS` is not, since its
larger half is owned by `app/runner_breaks.py` (see `end_reasons()`). `tests/test_sessions_schema.py`
asserts both halves.
"""
from __future__ import annotations

from typing import Optional

# ── status: is the SESSION running? ──────────────────────────────────────────────────────────────────
# `ended` means the fate was observed (`end_reason` names it); `unknown` means nobody saw one, so a
# reconstructed orphan never reads as a normal finish.
STATUS_RUNNING = "running"
STATUS_ENDED = "ended"
STATUS_UNKNOWN = "unknown"
STATUSES: tuple[str, ...] = (STATUS_RUNNING, STATUS_ENDED, STATUS_UNKNOWN)

# Rows are created when the session starts, so `running` is the only honest insert default.
DEFAULT_STATUS = STATUS_RUNNING

# ── runtime: which agent contract did the session run on? ───────────────────────────────────────────
# The dispatch fact that picks the ONE usage source an attempt is counted from: `legacy` reads the vendor
# transcript through its parser, `acp` the normalised event stream (`app/session_events.py`). Revision
# 202610190200's CHECK carries the same literal.
RUNTIME_LEGACY = "legacy"
RUNTIME_ACP = "acp"
RUNTIMES: tuple[str, ...] = (RUNTIME_LEGACY, RUNTIME_ACP)

# ── transcript_status: did the BYTES arrive? ─────────────────────────────────────────────────────────
# Independent of `status`: a session can end without a transcript, or upload while running.
# 0135's CHECK is rendered from the tuple below, so a reorder is a schema change.

# Nothing has been attempted yet. Every row starts here.
TRANSCRIPT_PENDING = "pending"
# A node holds the lease (`transcript_leased_at`) and is uploading; the stamp is what lets a dead
# node's upload be retried.
TRANSCRIPT_UPLOADING = "uploading"
# The bytes are in the bucket and `transcript_manifest_key` names the manifest that describes them.
# The only state that may render as a working download.
TRANSCRIPT_UPLOADED = "uploaded"
# An attempt failed and another is owed; `transcript_error` carries the reason a human reads.
TRANSCRIPT_FAILED = "failed"
# Retries exhausted. Terminal, so it must not read as about-to-retry.
TRANSCRIPT_GAVE_UP = "gave_up"
# Gone from the node's rolling on-disk window before upload — expected for old sessions, not a failure.
TRANSCRIPT_UNAVAILABLE = "unavailable"
# Never had a transcript (vs `unavailable`, which was lost); only the lost case is worth chasing.
TRANSCRIPT_NOT_APPLICABLE = "not_applicable"

TRANSCRIPT_STATUSES: tuple[str, ...] = (
    TRANSCRIPT_PENDING,
    TRANSCRIPT_UPLOADING,
    TRANSCRIPT_UPLOADED,
    TRANSCRIPT_FAILED,
    TRANSCRIPT_GAVE_UP,
    TRANSCRIPT_UNAVAILABLE,
    TRANSCRIPT_NOT_APPLICABLE,
)

DEFAULT_TRANSCRIPT_STATUS = TRANSCRIPT_PENDING

# States an upload is still owed work on; revision 0135's partial sweep index is built from this, so
# consumers' predicates (`app/routes/agent_sessions.py::_reclaim_expired_leases`, and
# `OWED_TRANSCRIPT_STATUSES` below) must stay inside it.
SWEEPABLE_TRANSCRIPT_STATUSES: tuple[str, ...] = (
    TRANSCRIPT_PENDING, TRANSCRIPT_UPLOADING, TRANSCRIPT_FAILED,
)

# Owed and not being worked — what `/api/agent/sessions/work` offers. A strict subset of the above so
# the index covers it; `uploading` is absent because serving a leased row would race two uploads.
OWED_TRANSCRIPT_STATUSES: tuple[str, ...] = (TRANSCRIPT_PENDING, TRANSCRIPT_FAILED)

# ── agent_kind: WHICH agent produced the session ─────────────────────────────────────────────────────
# Not a closed domain or CHECK: a new agent must cost a row, not a migration. The default is a constant
# so the ORM and the DDL spell it identically.
AGENT_KIND_CLAUDE_CODE = "claude_code"
DEFAULT_AGENT_KIND = AGENT_KIND_CLAUDE_CODE
# The agent kinds an audit document spells in words (`app/compliance_manifest.py::_WORDS`); another reads as its value.
AGENT_KIND_WORDS: dict[str, str] = {AGENT_KIND_CLAUDE_CODE: "Claude Code"}
# A registered SDK agent's run (`app/agents.py`): its row also carries `agent_ref` (which agent, version and
# commit), and the node's sweep reads its transcript from the SDK's events file, not a vendor adapter.
AGENT_KIND_SDK_AGENT = "sdk_agent"

# ── session_engine: WHERE a session's link opens (`app/routes/tasks/activity.py`) ────────────────────
# The default CLI's own kind for a run on the legacy runtime: the vendor's transcript viewer
# (`apps/web/src/lib/sessionLink.ts`). Every other engine (an SDK agent, any other provider, an ACP run) is
# `console`, whose transcript is the console's event stream.
SESSION_ENGINE_CONSOLE = "console"


def session_engine(kind: Optional[str], runtime: Optional[str], dispatched: Optional[str]) -> str:
    """The `session_engine` of a session of agent `kind` on `runtime` (dispatched as `dispatched`)."""
    if kind == AGENT_KIND_CLAUDE_CODE and RUNTIME_ACP not in (runtime, dispatched):
        return AGENT_KIND_CLAUDE_CODE
    return SESSION_ENGINE_CONSOLE

# ── end_reason: HOW the session ended ────────────────────────────────────────────────────────────────
# The lifecycle outcomes that are not breaks; `end_reasons()` joins them with the break causes.

# Ran to completion and settled its work.
END_REASON_NORMAL = "normal"
# Reaped as idle. Not a break: the slot stays held for its task and one Start press resumes it.
END_REASON_IDLE_TIMEOUT = "idle_timeout"
# Abandoned without settling. Producers: the CP gave up waiting with no break cause; an operator Reset
# (`routes/tasks/slots.py::abandon_task_slots`); or an operator slot release under a task hold
# (`routes/pool_slots.py::release_slot`). Never `normal`: `note_end` keeps the first reason.
END_REASON_ABANDONED = "abandoned"
# Written after the fact from on-disk evidence (the orphan case); `status` is `unknown` unless settled.
END_REASON_RECONSTRUCTED = "reconstructed"
# The CP re-armed this slot for the same task (`slot_state.redispatch`); the relaunch kills the old
# runner unreported. The task's work continues, so not `abandoned`. Recorded by the re-arm itself.
END_REASON_SUPERSEDED = "superseded"

LIFECYCLE_END_REASONS: tuple[str, ...] = (
    END_REASON_NORMAL,
    END_REASON_IDLE_TIMEOUT,
    END_REASON_ABANDONED,
    END_REASON_RECONSTRUCTED,
    END_REASON_SUPERSEDED,
)

# The lifecycle outcomes' words; a break cause reads `runner_terminations.CAUSE_LABELS` (`end_reason_label`).
LIFECYCLE_END_REASON_LABELS: dict[str, str] = {
    END_REASON_NORMAL: "finished",
    END_REASON_IDLE_TIMEOUT: "reaped as idle",
    END_REASON_ABANDONED: "abandoned",
    END_REASON_RECONSTRUCTED: "reconstructed from the node",
    END_REASON_SUPERSEDED: "relaunched",
}


def end_reason_label(reason: Optional[str]) -> Optional[str]:
    """An `end_reason` in words, capitalised ("Session vanished"); an unknown one verbatim, None for None.

    Deferred import, as `end_reasons`: `app.runner_terminations` reaches the model layer at module scope."""
    if not reason:
        return None
    from app.runner_terminations import CAUSE_LABELS

    words = LIFECYCLE_END_REASON_LABELS.get(reason) or CAUSE_LABELS.get(reason)
    return words[:1].upper() + words[1:] if words else reason


def inferred_ends() -> tuple[str, ...]:
    """The endings the CP inferred without the runner's word (it stopped; it was gone; nothing matched): a break
    cause stamped onto one of these names the ending instead (`shown_ending`; its SQL mirror is
    `routes/sessions.py::_shown_end_reason_clause`)."""
    from app.runner_terminations import CAUSE_VANISHED

    return (END_REASON_ABANDONED, CAUSE_VANISHED, "unknown")


def shown_ending(reason: Optional[str], detail: Optional[str]) -> tuple[Optional[str], Optional[str]]:
    """`(end_reason, end_detail)` of an ENDED row as a reader should see them. A break report stamps
    `"<cause>: <words>"` onto the row (`routes/agent/runner_liveness.py::record_runner_break`), often after the CP
    already ended it `abandoned`; that cause then names the ending ("Model not available to the account", not a
    bare "Abandoned"). The detail loses the prefix the reason now carries, so a row never reads
    "model_refused: model_refused: …"; a cause beside another recorded reason keeps its words, not its slug."""
    if not reason or not detail:
        return reason, detail
    head, sep, rest = detail.partition(": ")
    if not sep or not rest.strip():
        return reason, detail
    from app.runner_breaks import CAUSES

    if head == reason:
        return reason, rest
    if head in CAUSES and reason in inferred_ends():
        return head, rest
    if head in CAUSES:
        return reason, f"{end_reason_label(head)}: {rest}"
    return reason, detail


# No `status`/`end_reason` pairing constraint: `unknown` may carry a reason and `reconstructed` none,
# so a useful constraint would refuse legitimate rows. The writer keeps the pairing honest.


def end_reasons() -> tuple[str, ...]:
    """The full `end_reason` domain: the lifecycle outcomes, then every break cause.

    Deferred import: `app.runner_breaks` reaches `app.routes.tasks` at module scope, which would pull
    the route layer into migration 0135's import graph. Read causes through this; never re-spell them.
    """
    from app.runner_breaks import CAUSES

    return (*LIFECYCLE_END_REASONS, *CAUSES)
