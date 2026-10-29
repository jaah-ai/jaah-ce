"""The normalised event stream of an `acp` session (§3.1 of the ACP architecture): its wire model,
storage and usage reader. Nodes post batches (`routes/agent_sessions.py::ingest_events`); the task view reads
them (`routes/task_session_events.py`).

Stored as session members, not DB rows: JSON-lines chunks of `CHUNK_EVENTS` consecutive seqs at keys derived
from the chunk index (`session_transcripts.events_chunk_key`). The CP's role may get and put but not list, so
`sessions.events_last_seq` says how far the chunks reach (on the community edition's volume, the same keys as files:
`session_transcript_archive.on_volume`); ingest keeps seqs gapless (`check_contiguous`), so every
chunk up to it exists. Ingest read-merge-writes each touched chunk under `ingest_lock`, deduplicated on `seq`.

One usage source per attempt: an `acp` session's usage is its `usage` items, folded into `Task.metrics`; a vendor
parser's figures for it are refused at the transcript and live-usage routes."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Iterable, Iterator, Literal, Optional, get_args

import httpx
from pydantic import BaseModel, ConfigDict, Field, StrictInt, model_validator
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import edition_features, http_bounded
from app import session_transcripts as transcripts
from app.clock import as_utc
from app.entity_shape import MAX_NAME as MAX_MODEL_NAME, StrictModel
from app.model_prices import REPORTED_CUMULATIVE_COST, canonical_model, empty_usage
from app.task_metrics_ingest import MAX_TOKEN_COUNT

SCHEMA_VERSION = 1

Kind = Literal[
    "session_start", "turn_start", "assistant_message", "reasoning", "command_execution", "file_change",
    "tool_call", "approval_request", "user_input_request", "subagent", "plan", "usage", "rate_limit",
    "error", "turn_end", "session_end",
    # The ACP host's delivery decision for an operator's answer (`queued`/`delivered`/`dropped`): the
    # task page tells "the node never received it" from "the agent never resumed" by it (`web lib/answerPickup.ts`).
    "answer_received",
]
KINDS: tuple[str, ...] = get_args(Kind)
ItemStatus = Literal["in_progress", "completed", "failed", "declined"]

#: `usage.data.source`: counted tokens (priced by `model_prices`) or a cost the agent reported itself.
SOURCE_TOKENS = "tokens"
SOURCE_AGENT_REPORTED = "agent_reported"
#: `usage.data.cost_kind` of a running total (an ACP `usage_update.cost`): the latest replaces, never sums.
COST_CUMULATIVE = "cumulative"
#: Marks a `Task.metrics` session entry the stream wrote (`task_metrics_ingest.fold_session_metrics(source=)`).
STREAM_SOURCE = "event_stream"

#: Events per stored chunk. A poll reads one or two chunks; a download or a usage recompute reads them all.
CHUNK_EVENTS = 256
#: Events per posted batch (the node batches at most 64; this is the ceiling, not the norm).
MAX_BATCH_EVENTS = 500
#: One event's `data`, serialised. Text is stored whole (never truncated), so the bound sits far above a real item.
MAX_EVENT_BYTES = 256 * 1024
#: One batch's events, serialised: what one request may make the CP hold and write.
MAX_BATCH_BYTES = 8 * 1024 * 1024
#: A chunk is `CHUNK_EVENTS` bounded events, each `data` plus an envelope its own field bounds keep under
#: 4 KiB; this refuses a stored object no ingest could have written.
MAX_CHUNK_BYTES = CHUNK_EVENTS * (MAX_EVENT_BYTES + 4 * 1024)
#: JSON's exact-integer range, so a browser reads every `seq` exactly.
MAX_SEQ = 2**53
#: Events one read returns, and their serialised bytes; the reader asks again with `after=<last_seq>` while
#: `more` is true.
READ_LIMIT = 500
READ_BYTES_LIMIT = 4 * 1024 * 1024
#: Events one session's stream may hold, so its storage and every whole-stream read stay bounded.
MAX_STREAM_EVENTS = 1_000_000
#: A cost the agent reports for one usage item, bounded so a crafted line cannot post an absurd figure.
MAX_REPORTED_COST_USD = Decimal(10**6)

_TIMEOUT = httpx.Timeout(connect=3.0, write=10.0, read=10.0, pool=1.0)
_PRESIGN_EXPIRES_SECONDS = 300


class EventStorageUnavailable(RuntimeError):
    """The stream could not be stored or read here (not configured, or S3 refused). The message is safe to show:
    it never carries a URL."""


class SeqGap(ValueError):
    """A batch that would leave a gap below its seqs; the node resends from `expected`."""

    def __init__(self, expected: int) -> None:
        super().__init__(f"events must continue the stream without a gap: the next seq is {expected}")
        self.expected = expected


def check_contiguous(seqs: Iterable[int], *, last_seq: Optional[int]) -> int:
    """The stream's new high-water mark after a batch of `seqs`, refusing one that would leave a gap (`SeqGap`).

    Gapless is what bounds every read: a sparse seq would make a reader walk chunks that never existed, and a
    poll whose cursor passed a gap would never show what later filled it. Seqs at or below `last_seq` are
    re-sends (stored once by `append`)."""
    high = last_seq or 0
    for seq in sorted(set(seqs)):
        if seq > high + 1:
            raise SeqGap(high + 1)
        high = max(high, seq)
    return high


# ── The wire model ──────────────────────────────────────────────────────────────────────────────────

class UsageData(BaseModel):
    """`usage.data`, read exactly as §3.1 pins it. Unknown extra keys are ignored, absent counts are zero."""

    model_config = ConfigDict(extra="ignore")

    model: str = Field(default="unknown", min_length=1, max_length=MAX_MODEL_NAME)
    input_tokens: StrictInt = Field(default=0, ge=0, le=MAX_TOKEN_COUNT)
    cache_write_tokens: StrictInt = Field(default=0, ge=0, le=MAX_TOKEN_COUNT)
    cache_read_tokens: StrictInt = Field(default=0, ge=0, le=MAX_TOKEN_COUNT)
    output_tokens: StrictInt = Field(default=0, ge=0, le=MAX_TOKEN_COUNT)
    # Informational: every engine that reports it separately already counts it inside `output_tokens` (as the
    # node's vendor adapters read it), so it is never added again.
    reasoning_tokens: StrictInt = Field(default=0, ge=0, le=MAX_TOKEN_COUNT)
    context_used: Optional[StrictInt] = Field(default=None, ge=0, le=MAX_TOKEN_COUNT)
    context_size: Optional[StrictInt] = Field(default=None, ge=0, le=MAX_TOKEN_COUNT)
    reported_cost_usd: Optional[Decimal] = Field(default=None, ge=0, le=MAX_REPORTED_COST_USD)
    source: Literal["tokens", "agent_reported"] = SOURCE_TOKENS
    cost_kind: Optional[Literal["cumulative"]] = None

    @model_validator(mode="before")
    @classmethod
    def _as_the_node_writes_it(cls, raw: Any) -> Any:
        """The node's normaliser (`apps/jaahd/acp/events.py::_usage`) writes every key, null where the engine
        reported nothing, and names its provenance in `source` (`usage_update`, `agent_meta`, `prompt_result`):
        a null is read as absent, and any provenance but `agent_reported` as counted tokens."""
        if not isinstance(raw, dict):
            return raw
        out = {key: value for key, value in raw.items() if value is not None}
        if "source" in out and out["source"] != SOURCE_AGENT_REPORTED:
            out["source"] = SOURCE_TOKENS
        return out


class AgentEvent(StrictModel):
    """One §3.1 event. The envelope is closed (an unknown key is a 422); `data` is kind-specific and free-form
    except for `usage`, whose shape this module reads."""

    v: Literal[1]
    seq: int = Field(ge=1, le=MAX_SEQ)
    ts: datetime
    session_id: str = Field(min_length=1, max_length=128)
    native_session_ref: Optional[str] = Field(default=None, max_length=256)
    task_id: Optional[int] = Field(default=None, ge=1)
    provider: str = Field(min_length=1, max_length=64)
    turn_id: Optional[str] = Field(default=None, max_length=256)
    item_id: Optional[str] = Field(default=None, max_length=256)
    kind: Kind
    status: Optional[ItemStatus] = None
    data: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _bounded_and_readable(self) -> "AgentEvent":
        if len(json.dumps(self.data, default=str)) > MAX_EVENT_BYTES:
            raise ValueError(f"event seq {self.seq}: data is larger than {MAX_EVENT_BYTES} bytes")
        if self.kind == "usage":
            UsageData.model_validate(self.data)
        return self

    def stored(self) -> dict[str, Any]:
        """The stored line: every envelope field present (null when absent), `ts` in UTC — so two engines'
        streams read back in one identical shape."""
        line = self.model_dump(mode="json")
        stamp = as_utc(self.ts)
        line["ts"] = stamp.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        return line


# ── Storage ─────────────────────────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class Where:
    """Where one session's chunks live: the row's identity plus the bucket and credentials to reach them."""

    region: str
    bucket: str
    credentials: Any
    session_id: str
    started_at: datetime
    #: The chunks are files on the community edition's volume, not S3 objects (`session_transcript_archive.on_volume`).
    on_volume: bool = False


def where_for(row: Any, *, pin: bool = False) -> Optional[Where]:
    """The storage for session `row`, or None when this control plane has no transcript storage: the row's own
    bucket (in its own region), else its org's data region's (`archive.org_bucket`). `pin` = a write:
    the bucket is fixed on the row first (`archive.pin_bucket`), so the chunks, the transcript and every later
    read share one bucket whatever the org picks next. Imported at call time so a test's patch of
    `archive.storage`'s sources is seen."""
    from sqlalchemy.orm import object_session

    from app.attachment_storage import LOCAL_BUCKET
    from app.session_transcript_archive import org_bucket, pin_bucket, stored_at

    owner = object_session(row)
    bucket = row.transcript_bucket
    if not bucket and owner is not None:
        bucket = pin_bucket(owner, row) if pin else org_bucket(owner, row.org_id)
    found = stored_at(bucket)
    if found is None:
        return None
    region, stored_bucket, credentials = found
    return Where(
        region=region,
        bucket=stored_bucket,
        credentials=credentials,
        session_id=row.agent_session_id,
        started_at=row.started_at,
        on_volume=stored_bucket == LOCAL_BUCKET,
    )


def ingest_lock(session: Session, agent_session_id: str) -> None:
    """Serialise ingest per session for the rest of the transaction: a chunk is read-merged-written, so two
    concurrent batches would otherwise drop each other's events. An advisory lock, not the row lock, so the
    session row's other writers (`/end`, `/usage`) are never held behind an S3 round trip."""
    session.execute(
        select(func.pg_advisory_xact_lock(func.hashtext("session_events"), func.hashtext(agent_session_id)))
    )


def chunk_of(seq: int) -> int:
    return (seq - 1) // CHUNK_EVENTS


def _send(where: Where, method: str, chunk: int, body: Optional[bytes] = None) -> tuple[int, bytes]:
    key = transcripts.events_chunk_key(
        session_id=where.session_id, started_at=where.started_at, chunk=chunk
    )
    if where.on_volume:
        return _send_volume(method, key, body)
    if not edition_features.enabled("aws"):
        raise RuntimeError("no S3 in this edition: its sessions are always on the volume")
    from app.aws import presign

    url = presign.presigned_url(
        method=method,
        url=presign.s3_object_url(bucket=where.bucket, key=key, region=where.region),
        region=where.region,
        service="s3",
        credentials=where.credentials,
        amz_date=datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
        expires_seconds=_PRESIGN_EXPIRES_SECONDS,
    )
    try:
        with httpx.Client(timeout=_TIMEOUT, follow_redirects=False) as client:
            request = client.build_request(method, url, content=body)
            request.headers.pop("content-type", None)
            presign.reject_forbidden_headers(request.headers.keys())
            response = client.send(request, stream=True)
            try:
                content, cut = http_bounded.read_capped(response, MAX_CHUNK_BYTES + 1)
            finally:
                response.close()
    except httpx.HTTPError as exc:
        # The type only, never the URL: it is a bearer token.
        raise EventStorageUnavailable(f"{type(exc).__name__} during an S3 {method} of the event stream") from None
    if cut:
        raise EventStorageUnavailable("an event chunk is larger than any ingest could have written")
    return response.status_code, content


def _send_volume(method: str, key: str, body: Optional[bytes]) -> tuple[int, bytes]:
    """`_send` against the community edition's volume, answering as S3 would: 404 for a chunk not written."""
    from app import session_transcript_archive as archive

    try:
        if method == "PUT":
            archive.write_volume(key, body or b"")
            return 200, b""
        path = archive.volume_path(key)
        if not path.is_file():
            return 404, b""
        if path.stat().st_size > MAX_CHUNK_BYTES:
            raise EventStorageUnavailable("an event chunk is larger than any ingest could have written")
        return 200, path.read_bytes()
    except (OSError, ValueError) as exc:
        raise EventStorageUnavailable(f"{type(exc).__name__} during a {method} of the event stream's volume") from None


def _read_chunk(where: Where, chunk: int, *, stored: bool) -> list[dict[str, Any]]:
    """One chunk's events, in seq order. Without `s3:ListBucket` S3 answers a missing key with 403, so 403 and 404
    both mean absent: empty for a chunk past the high-water mark (`stored=False`), an error for one the row says
    holds events — read as empty, an ingest would overwrite it and lose them."""
    status, content = _send(where, "GET", chunk)
    if status in (403, 404) and not stored:
        return []
    if status != 200:
        raise EventStorageUnavailable(f"S3 answered {status} for an event chunk")
    events = []
    for line in content.decode("utf-8", errors="replace").splitlines():
        if line.strip():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(event, dict) and isinstance(event.get("seq"), int):
                events.append(event)
    return sorted(events, key=lambda event: event["seq"])


def _write_chunk(where: Where, chunk: int, events: list[dict[str, Any]]) -> None:
    body = "".join(json.dumps(event, separators=(",", ":"), ensure_ascii=False) + "\n" for event in events)
    status, _ = _send(where, "PUT", chunk, body.encode("utf-8"))
    if not 200 <= status < 300:
        raise EventStorageUnavailable(f"S3 answered {status} for an event chunk PUT")


def append(where: Where, events: Iterable[AgentEvent], *, last_seq: Optional[int]) -> list[dict[str, Any]]:
    """Store `events` (the caller holds `ingest_lock`), returning the ones not stored before, in seq order.

    `last_seq` is the row's `events_last_seq`: a chunk at or below it must exist. One past it is still read (a batch
    whose commit failed after its PUT may have written it) and may be absent."""
    by_chunk: dict[int, dict[int, dict[str, Any]]] = {}
    for event in events:
        by_chunk.setdefault(chunk_of(event.seq), {}).setdefault(event.seq, event.stored())
    added: list[dict[str, Any]] = []
    for chunk, incoming in sorted(by_chunk.items()):
        existing = _read_chunk(where, chunk, stored=bool(last_seq) and chunk <= chunk_of(last_seq or 1))
        known = {event["seq"] for event in existing}
        fresh = [incoming[seq] for seq in sorted(incoming) if seq not in known]
        if fresh:
            _write_chunk(where, chunk, sorted(existing + fresh, key=lambda event: event["seq"]))
            added.extend(fresh)
    return added


def read(where: Where, *, last_seq: Optional[int], after: int, limit: int = READ_LIMIT) -> tuple[list[dict[str, Any]], bool]:
    """Events with `seq > after`, at most `limit` of them and about `READ_BYTES_LIMIT` bytes (always at least
    one), and whether more follow."""
    if not last_seq or last_seq <= after:
        return [], False
    out: list[dict[str, Any]] = []
    budget = READ_BYTES_LIMIT
    for chunk in range(chunk_of(after + 1), chunk_of(last_seq) + 1):
        for event in _read_chunk(where, chunk, stored=True):
            if event["seq"] <= after:
                continue
            budget -= len(json.dumps(event, separators=(",", ":"), ensure_ascii=False))
            if out and (len(out) >= limit or budget < 0):
                return out, True
            out.append(event)
    return out, False


def iter_all(where: Where, *, last_seq: Optional[int]) -> Iterator[dict[str, Any]]:
    """Every stored event in seq order (the download, the usage recompute)."""
    if not last_seq:
        return
    for chunk in range(0, chunk_of(last_seq) + 1):
        yield from _read_chunk(where, chunk, stored=True)


# ── Usage ───────────────────────────────────────────────────────────────────────────────────────────

def totals_from_entry(entry: Any) -> UsageTotals:
    """The running totals a session's `Task.metrics` entry holds (what `routes/agent_sessions.py::
    _fold_stream_usage` wrote), so a batch adds only its own new items."""
    if not isinstance(entry, dict):
        return UsageTotals()
    usage = {
        model: {cls: int(count) for cls, count in classes.items() if isinstance(count, int)}
        for model, classes in (entry.get("usage_by_model") or {}).items()
        if isinstance(classes, dict)
    }
    # An entry written before the split holds only the effective `reported_cost_usd`: read it as the turn sum.
    split = REPORTED_CUMULATIVE_COST in entry or "reported_turn_cost_usd" in entry
    turn_key = "reported_turn_cost_usd" if split else "reported_cost_usd"
    return UsageTotals(
        usage_by_model={model: {**empty_usage(), **classes} for model, classes in usage.items()},
        tokens_in=int(entry.get("tokens_in") or 0),
        tokens_out=int(entry.get("tokens_out") or 0),
        cumulative_cost_usd=_decimal_or_none(entry.get(REPORTED_CUMULATIVE_COST)),
        turn_cost_usd=_decimal_or_none(entry.get(turn_key)),
    )


def _decimal_or_none(value: Any) -> Optional[Decimal]:
    try:
        return Decimal(str(value)) if value is not None else None
    except ArithmeticError:
        return None


@dataclass
class UsageTotals:
    """One session's usage from its stream. `usage_by_model` holds counted tokens only (what `model_prices`
    prices); an `agent_reported` item adds its tokens to the totals and its cost to `turn_cost_usd`, but
    never to `usage_by_model`, so the same spend is not priced twice. A running total (`cost_kind:
    cumulative`) sets `cumulative_cost_usd` instead, and once one arrived it is the session's whole cost."""

    usage_by_model: dict[str, dict[str, int]] = field(default_factory=dict)
    tokens_in: int = 0
    tokens_out: int = 0
    cumulative_cost_usd: Optional[Decimal] = None
    turn_cost_usd: Optional[Decimal] = None
    model: Optional[str] = None
    items: int = 0

    @property
    def reported_cost_usd(self) -> Optional[Decimal]:
        """The cost the agent reported: its latest running total, else the sum of its per-turn costs."""
        return self.cumulative_cost_usd if self.cumulative_cost_usd is not None else self.turn_cost_usd


def usage_of(events: Iterable[dict[str, Any]], start: Optional[UsageTotals] = None) -> UsageTotals:
    """Sum the `usage` items onto `start` (each item is one turn's usage, so they add), except a running total
    (`cost_kind: cumulative`), whose cost replaces the last one, even a higher one: the agent's total is
    authoritative (a restart may lower it). A malformed item is skipped: ingest validated it, so only a
    hand-edited chunk could carry one."""
    totals = start or UsageTotals()
    for event in events:
        if event.get("kind") != "usage":
            continue
        try:
            usage = UsageData.model_validate(event.get("data") or {})
        except ValueError:
            continue
        if usage.cost_kind == COST_CUMULATIVE:
            # A running total carries no turn's tokens: no item, no model, no bucket.
            if usage.reported_cost_usd is not None:
                totals.cumulative_cost_usd = usage.reported_cost_usd
            continue
        # A context-only report (`usage_update`: no model, no tokens, no cost) is no usage item: counting it would
        # name the session's model `unknown` and add an unpriced bucket, batch after batch.
        if "model" not in usage.model_fields_set and usage.reported_cost_usd is None and not any(
                (usage.input_tokens, usage.output_tokens, usage.cache_read_tokens, usage.cache_write_tokens)):
            continue
        totals.items += 1
        model = canonical_model(usage.model)
        totals.model = model
        totals.tokens_in += usage.input_tokens + usage.cache_read_tokens + usage.cache_write_tokens
        totals.tokens_out += usage.output_tokens
        if usage.source == SOURCE_AGENT_REPORTED:
            if usage.reported_cost_usd is not None:
                totals.turn_cost_usd = (totals.turn_cost_usd or Decimal(0)) + usage.reported_cost_usd
            continue
        counts = totals.usage_by_model.setdefault(model, empty_usage())
        counts["input"] += usage.input_tokens
        counts["output"] += usage.output_tokens
        counts["cache_read"] += usage.cache_read_tokens
        # An unsplit cache write is the cheaper 5-minute class: a floor, never an invention (as Efficiency's
        # transcript reader attributes one).
        counts["cache_write_5m"] += usage.cache_write_tokens
    return totals
