"""The normaliser: ACP `session/update` (+ prompt results, errors, requests) -> Jaah event schema v1 (architecture §3.1).

One provider-neutral event shape is what lets the console, the CP and the transcript store stop parsing
each vendor's stream. Every event carries exactly EVENT_FIELDS; `seq` is NOT one of them — the daemon stamps
it at append (`runtime.events_append`), the only place an order is authoritative.

Chunks are coalesced: `agent_message_chunk`/`agent_thought_chunk` deltas accumulate per item and the item is
emitted ONCE, completed, with the full concatenated text — never truncated, never a delta stream. Item
boundary = a messageId change, a switch between message and thought, any non-chunk update, or turn end.

Oversized text is split, never truncated (schema v1): when an assistant_message/reasoning `data.text` or a
command_execution `data.output` is over MAX_TEXT_CHUNK_BYTES (measured JSON-escaped, the way the daemon's
socket carries it), the item is emitted as n events of the SAME kind, status and item_id, in order, each
carrying `data.chunk = {"index": i, "count": n}` (i from 0) and one slice of the text; the full text is the
concatenation of the slices in index order. An item that fits carries no `chunk` key. The cap leaves room
under the daemon's per-event limit and the broker's request-line limit for the envelope. An `error` event
carrying a chunked item's `item_id` (the host's placeholder for a batch the daemon would not record) ENDS that
item: a reader stops waiting for its missing slices.

`error.data.message` is engine-provided text and passes through `transcript_redact.redact_text` before it
leaves the host (verbatim otherwise); everything downstream (the CP, the runner-break detail) sees only that.

Tool calls are emitted twice: `in_progress` on first sight and `completed`/`failed` when the agent reports
it; names and paths are carried in full (a shortened name breaks the console's tool grouping).
`usage.data` always has every USAGE_KEYS key (missing -> None) so consumers never branch on presence.
Unknown `sessionUpdate` kinds are ignored: the protocol grows, the schema stays v1.
"""
from __future__ import annotations

import datetime as _dt
import functools
import json
import logging
import re
import time
from typing import Any, Callable

from agent_providers import AcpDialect

log = logging.getLogger("jaahd.acp.events")

SCHEMA_VERSION = 1
KINDS = ("session_start", "turn_start", "assistant_message", "reasoning", "command_execution", "file_change",
         "tool_call", "approval_request", "user_input_request", "subagent", "plan", "usage", "rate_limit", "error",
         "turn_end", "session_end", "answer_received")
STATUSES = ("in_progress", "completed", "failed", "declined")
ERROR_CLASSES = ("usage_limit", "auth", "api", "transport", "agent")
# `answer_received.data.decision`: what the host did with an operator's answer — fed to the agent's open
# request (`delivered`), queued as its next prompt (`queued`), or not passed on (`dropped`: blank, or the session
# is closing). The host's receipt, not the agent's: the console reads "resumed" off the events after it.
ANSWER_DECISIONS = ("queued", "delivered", "dropped")
# Who wrote a turn's prompt: the task's first prompt, the host's nudge, or an operator's steer/answer.
PROMPT_SOURCES = ("task", "nudge", "operator")
EVENT_FIELDS = ("v", "ts", "session_id", "native_session_ref", "task_id", "provider", "turn_id", "item_id", "kind",
                "status", "data")
USAGE_KEYS = ("model", "input_tokens", "cache_write_tokens", "cache_read_tokens", "output_tokens", "reasoning_tokens",
              "context_used", "context_size", "reported_cost_usd", "cost_kind", "source")
# `usage.data.cost_kind`: "cumulative" marks a `usage_update.cost`, the session's running total (the CP keeps the
# latest, never sums it); null on every other item, whose cost is one turn's.
COST_CUMULATIVE = "cumulative"

META_KEY = "jaah.ai"
_CHUNK_KINDS = {"agent_message_chunk": "assistant_message", "agent_thought_chunk": "reasoning"}
_TOOL_KINDS = {"execute": "command_execution", "edit": "file_change", "delete": "file_change", "move": "file_change"}
_TERMINAL_TOOL = ("completed", "failed")

_USAGE_RE = re.compile(r"usage limit|rate[ _-]?limit|quota|too many requests|limit reached|hit your limit|"
                       r"limit will reset|\b429\b", re.I)
_AUTH_RE = re.compile(r"\b40[13]\b|\bauth|api[ _-]?key|unauthori[sz]ed|forbidden|invalid (?:x-api-key|token)", re.I)
_TRANSPORT_RE = re.compile(r"connection ?closed|transport|broken pipe|connection reset|\beof\b", re.I)
_API_RE = re.compile(r"\b5\d\d\b|overloaded|internal server error|service unavailable|bad gateway", re.I)
# The break causes a dialect may name (`AcpDialect.error_kinds`/`error_text_patterns`; `hooks/break_cause.py::CAUSES`),
# each with the error class it reads as. Money and holds are `credit_exhausted` (not a window) but still a
# `usage_limit` class: the account cannot run. A refused model keeps the class its wording reads as.
_CAUSE_CLASSES = {"usage_limit": "usage_limit", "credit_exhausted": "usage_limit", "auth_expired": "auth",
                  "api_error": "api", "model_refused": "", "client_unsupported": ""}
BREAK_CAUSES = frozenset(_CAUSE_CLASSES)
# A cause the control plane fails the task on outright (`model_refused`) is read only off an error record, never
# off agent text: people quote those sentences (`hooks/break_cause.py::MODEL_REFUSED`). `client_unsupported` is
# terminal too but stays readable off a tool-less turn's OPENING text (`_text_failure`): the only engine that says
# it writes its failed turn as agent text, so record-only would never see it.
_RECORD_ONLY_CAUSES = frozenset({"model_refused"})
_ISO_RE = re.compile(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?(?:Z|[+-]\d{2}:?\d{2})?")
_EPOCH_RE = re.compile(r"(?<!\d)(1\d{9}|1\d{12})(?!\d)")
# A reset written for a human in the engine's LOCAL time, which is the host's (the engine is its child, same
# TZ): "try again at 11:00 AM" (today) or "try again at Oct 19th, 2026 11:00 AM" (both forms measured on
# live engines).
_LOCAL_RETRY_RE = re.compile(r"try again at (?:(?P<date>[A-Z][a-z]{2} \d{1,2})(?:st|nd|rd|th)?, (?P<year>\d{4}) )?"
                             r"(?P<time>\d{1,2}:\d{2} [AP]M)", re.I)

# An upstream adapter's own `_meta` shapes, mapped rather than forked (architecture C11), read under ANY
# namespace and never keyed by provider: a `session_info_update` whose `_meta.<ns>.threadStatus.type` is
# `systemError` marks the turn failed (its text follows as an agent message, then `end_turn`), and a prompt
# result's `_meta.quota.model_usage` names its model. Both shapes were measured on the providers' live adapters.
_THREAD_FAILED = "systemError"

MAX_TEXT_CHUNK_BYTES = 160 * 1024
# The most text kept from an ACCUMULATED stream (a terminal tool's output deltas, a failed turn's message): its
# tail, so a runaway command cannot grow the host without bound.
MAX_ACCUMULATED_CHARS = 8 * MAX_TEXT_CHUNK_BYTES

try:  # the jaahd dir is on sys.path in the host; a missing module keeps the text as it is
    from transcript_redact import redact_text as _redact_text
except Exception:  # noqa: BLE001 — import-guarded by contract
    _redact_text = None

try:  # the node's one vendor-error classifier (`AcpDialect.vendor_error_text`); missing, no record is refined
    from hooks.break_cause import UNKNOWN as _UNKNOWN_CAUSE, classify_vendor_error as _classify_vendor_error
    from hooks.break_cause import vendor_message as _vendor_message
except Exception:  # noqa: BLE001 — import-guarded by contract
    _classify_vendor_error = None  # type: ignore[assignment]  # the guarded fallback `vendor_cause` checks for
    _vendor_message = None  # type: ignore[assignment]  # an error then keeps the engine's whole text

# A CLI's own update nag, which an engine streams as agent text (measured: an adapter writing its startup notice
# "New version available: v1.4.0 (installed v1.0.2). Run: `npm i -g <its package>`"): never the
# runner's words, so Agent activity drops it. Whole lines only, anchored on the CLI's own version wording, so prose
# that merely mentions npm or an update is kept.
_UPDATE_NAG_RE = re.compile(
    r"^[^\w\n]*(?:New version available: v?\d[\w.+-]* \(installed v?\d[\w.+-]*\)\.?"
    r"|Update available!? v?\d[\w.+-]* (?:->|\u2192) v?\d[\w.+-]*\.?)"
    r"(?:[ \t]*\n?[ \t]*Run:? `?(?:npm i(?:nstall)?|pnpm add|bun add) -g [@\w./-]+`?(?: to update)?\.?)?"
    r"[ \t]*(?:\n|$)", re.M)


def redact(text: str) -> str:
    """Engine-provided text with credential shapes masked; unchanged when the redactor is unavailable."""
    if _redact_text is None or not text:
        return text
    try:
        return _redact_text(text)[0]
    except Exception:  # noqa: BLE001 — a redactor bug must not drop the error itself
        return text


def _escaped_len(text: str) -> int:
    return len(json.dumps(text)) - 2


def split_text(text: str, limit: int = MAX_TEXT_CHUNK_BYTES) -> list[str]:
    """`text` in order-preserving slices, each at most `limit` bytes JSON-escaped (ASCII-escaped: never
    smaller than the UTF-8 form). One slice when it already fits."""
    if _escaped_len(text) <= limit:
        return [text]
    parts: list[str] = []
    start, n = 0, len(text)
    while start < n:
        hi = min(n, start + limit)  # every char escapes to >= 1 byte: more than `limit` chars never fits
        if _escaped_len(text[start:hi]) > limit:
            lo = start + 1  # one char always fits (at most 12 escaped bytes)
            while lo < hi - 1:
                mid = (lo + hi) // 2
                if _escaped_len(text[start:mid]) <= limit:
                    lo = mid
                else:
                    hi = mid
            hi = lo
        parts.append(text[start:hi])
        start = hi
    return parts


def _meta(obj: object) -> dict:
    """`obj["_meta"]["jaah.ai"]` or {}."""
    if not isinstance(obj, dict):
        return {}
    meta = obj.get("_meta")
    inner = meta.get(META_KEY) if isinstance(meta, dict) else None
    return inner if isinstance(inner, dict) else {}


def iso_ms(ts: float | None = None) -> str:
    """Epoch `ts` (now when None) as the schema's `ts` spelling: ISO-8601 UTC, milliseconds, `Z`."""
    moment = _dt.datetime.now(_dt.timezone.utc) if ts is None else _dt.datetime.fromtimestamp(ts, tz=_dt.timezone.utc)
    return moment.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _norm_resets_at(value: object) -> str | None:
    """An ISO-8601 string or epoch (s or ms) -> ISO-8601 UTC; anything else -> None."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        try:
            return iso_ms(value / 1000 if value > 1e12 else value)
        except (ValueError, OverflowError, OSError):
            return None
    if not isinstance(value, str) or not value.strip():
        return None
    s = value.strip()
    if s.isdigit():
        return _norm_resets_at(int(s))
    try:
        parsed = _dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=_dt.timezone.utc)
        return iso_ms(parsed.timestamp())
    except (ValueError, OverflowError, OSError):
        return None


def _local_retry_at(text: str, now: float) -> str | None:
    """`_LOCAL_RETRY_RE`'s time as ISO-8601 UTC: the named day, else the local day of `now`."""
    m = _LOCAL_RETRY_RE.search(text)
    if not m:
        return None
    try:
        clock = _dt.datetime.strptime(m.group("time").upper(), "%I:%M %p").time()
        if m.group("date"):
            day = _dt.datetime.strptime(f"{m.group('date')} {m.group('year')}", "%b %d %Y").date()
            return iso_ms(_dt.datetime.combine(day, clock).timestamp())
        at = _dt.datetime.combine(_dt.datetime.fromtimestamp(now).date(), clock).timestamp()
        # A bare time already past was written yesterday (read just after midnight): it is tomorrow's.
        return iso_ms(at if at > now else at + 86400)
    except (ValueError, OverflowError, OSError):
        return None


def _resets_at_from(text: str, data: object, now: float | None = None) -> str | None:
    if isinstance(data, dict):
        found = _norm_resets_at(data.get("resets_at"))
        if found:
            return found
    m = _ISO_RE.search(text)
    if m and (found := _norm_resets_at(m.group(0))):
        return found
    m = _EPOCH_RE.search(text)
    if m:
        return _norm_resets_at(int(m.group(1)))
    return _local_retry_at(text, time.time() if now is None else now)


def classify_error(code: int | None, message: str, data: object = None, now: float | None = None,
                   dialect: AcpDialect = AcpDialect()) -> tuple[str, str | None]:
    """(class, resets_at ISO-8601 or None). Explicit `_meta["jaah.ai"]` wins, then the kind the dialect's
    `error_kind_meta` names; else the code and wording. `now` dates a reset given as a bare local time (default:
    the wall clock)."""
    explicit = _meta(data)
    if explicit.get("error_class") in ERROR_CLASSES:
        return explicit["error_class"], _norm_resets_at(explicit.get("resets_at"))
    text = _error_text(message, data)
    kind = _CAUSE_CLASSES.get(_kind_cause(data, dialect)) or None
    if kind:
        return kind, _resets_at_from(text, data) if kind == "usage_limit" else None
    if code == 429 or _USAGE_RE.search(text):
        return "usage_limit", _resets_at_from(text, data, now)
    if code in (401, 403) or _AUTH_RE.search(text):
        return "auth", None
    if _TRANSPORT_RE.search(text):
        return "transport", None
    if (isinstance(code, int) and 500 <= code <= 599) or _API_RE.search(text):
        return "api", None
    return "agent", None


def error_cause(data: object, message: str = "", dialect: AcpDialect = AcpDialect()) -> str:
    """The break cause an engine named for a prompt error, or "" when it named none: an explicit
    `_meta["jaah.ai"].cause`, one of the dialect's `error_text_patterns` in its text, or the kind its
    `error_kind_meta` names."""
    explicit = _meta(data).get("cause")
    if explicit in BREAK_CAUSES:
        return explicit
    text = _error_text(message, data)
    return vendor_cause(text, text_cause(text, dialect) or _kind_cause(data, dialect), dialect)


def vendor_cause(text: str, cause: str, dialect: AcpDialect) -> str:
    """`cause` refined by the vendor classifier when the dialect's error text is the vendor's own record
    (`AcpDialect.vendor_error_text`) and `cause` is none or the generic `api_error`: a 429 or a spent quota behind
    an engine's generic prefix is the account's break, not the API's."""
    if not dialect.vendor_error_text or cause not in ("", "api_error") or _classify_vendor_error is None:
        return cause
    found = _classify_vendor_error(text)[0]
    return found if found != _UNKNOWN_CAUSE and found in BREAK_CAUSES else cause


def text_cause(text: str, dialect: AcpDialect, *, at_start: bool = False) -> str:
    """The break cause of the first `dialect.error_text_patterns` entry found in `text` (matched at its start
    only, with `at_start`), or ""."""
    for pattern, cause in dialect.error_text_patterns:
        found = _compiled(pattern)
        if cause in BREAK_CAUSES and (found.match(text) if at_start else found.search(text)):
            return cause
    return ""


@functools.lru_cache(maxsize=64)
def _compiled(pattern: str) -> re.Pattern[str]:
    return re.compile(pattern, re.I)


def _kind_cause(data: object, dialect: AcpDialect) -> str:
    """The break cause the dialect maps the agent's own kind (`data[dialect.error_kind_meta]`) to, or ""."""
    if not dialect.error_kind_meta or not isinstance(data, dict):
        return ""
    cause = dict(dialect.error_kinds).get(data.get(dialect.error_kind_meta), "")
    return cause if cause in BREAK_CAUSES else ""


def error_detail(data: object) -> str:
    """The engine's own sentence behind a generic JSON-RPC message ("Internal error"): `data.message`, else
    `data.details` (the ACP SDKs' spelling, Python and TypeScript alike, for an exception an agent handler
    raised), else `data` itself when it is text; "" when it carries none."""
    if isinstance(data, dict):
        for key in ("message", "details", "detail"):
            value = data.get(key)
            if isinstance(value, str) and value.strip():
                return value
        return ""
    return data if isinstance(data, str) and data.strip() else ""


def _error_text(message: str, data: object) -> str:
    """An error's words for classifying it: the message plus `data`'s own text, one reading for class and cause."""
    if isinstance(data, str):
        return f"{message or ''} {data}"
    if isinstance(data, dict):
        return " ".join([message or ""] + [str(data[k]) for k in ("message", "detail", "details", "error") if k in data])
    return message or ""


def _usage(source: str, fields: dict) -> dict:
    data: dict[str, Any] = dict.fromkeys(USAGE_KEYS)
    for key in USAGE_KEYS:
        if fields.get(key) is not None:
            data[key] = fields[key]
    if data["reported_cost_usd"] is not None and all(
            data[k] is None for k in USAGE_KEYS if k not in ("reported_cost_usd", "cost_kind", "source", "model")):
        source = "agent_reported"
    data["source"] = fields.get("source") or source
    return data


def fold_inclusive_counts(gross_input: int, cached: int, output: int, thoughts: int) -> tuple[int, int, int]:
    """(fresh input, cache read, billed output) for an agent whose input count already includes its cached
    tokens and whose thoughts bill at the output rate: the single home of that rule, shared by the ACP
    normaliser (`Usage.input_includes_cached`) and the legacy transcript reader (an adapter's `read_usage`) of
    a provider whose input count includes its cache classes."""
    # Clamped at zero: the control plane refuses a whole report carrying any negative count.
    return max(gross_input - cached, 0), cached, output + thoughts


def _count(counts: dict, *keys: str) -> Any:
    """The first of `keys` that `counts` carries (camelCase before snake_case), else None."""
    return next((counts[key] for key in keys if counts.get(key) is not None), None)


def _cost_usd(cost: object) -> float | None:
    if isinstance(cost, dict) and str(cost.get("currency", "")).upper() == "USD" and isinstance(
            cost.get("amount"), (int, float)):
        return cost["amount"]
    return None


#: `compaction_observed`'s rule. An engine that reports a `usage_update`'s `used` as the tokens of its latest model
#: call, each of which sends the whole conversation, shrinks it this far only on a compaction (measured on live
#: engines). Below the floor a context is too small for a drop
#: to mean anything.
COMPACTION_DROP_RATIO = 0.5
COMPACTION_MIN_USED = 20_000


def usage_used(update: object) -> int | None:
    """A `usage_update`'s `used` (tokens in the context window), or None for any other update."""
    if not isinstance(update, dict) or update.get("sessionUpdate") != "usage_update":
        return None
    used = update.get("used")
    return used if isinstance(used, int) and not isinstance(used, bool) and used >= 0 else None


def compaction_observed(previous_used: int | None, used: int | None) -> bool:
    """True when a `usage_update` sequence shows the context compacted: `used` fell below `COMPACTION_DROP_RATIO` of
    the report before it, from at least `COMPACTION_MIN_USED`. A false signal costs one re-read the read-once guard
    would have refused (the guard's `--reset`), never a refused one, so the rule leans to firing."""
    if previous_used is None or used is None or previous_used < COMPACTION_MIN_USED:
        return False
    return used < previous_used * COMPACTION_DROP_RATIO


def _without_nags(text: str) -> str:
    """`text` with every CLI update nag line (`_UPDATE_NAG_RE`) removed."""
    return _UPDATE_NAG_RE.sub("", text) if text else text


def _text(content: object) -> str:
    if isinstance(content, dict) and content.get("type") == "text" and isinstance(content.get("text"), str):
        return content["text"]
    return ""


class Normaliser:
    def __init__(self, *, session_id: str, task_id: str, provider: str, native_session_ref: str = "",
                 clock: Callable[[], float] = time.time,
                 pricing_tier: Callable[[dict], str] | None = None, input_includes_cached: bool = False,
                 dialect: AcpDialect = AcpDialect()) -> None:
        self.session_id = session_id
        # The provider's `Capabilities.acp_dialect` (`agent_providers.dialect_for`): what the agent says beyond ACP.
        self._dialect = dialect
        # The provider's `Usage.input_includes_cached`: True folds a turn's counts by `fold_inclusive_counts`.
        self._input_includes_cached = input_includes_cached
        # The provider adapter's per-line tier (its `line_pricing_tier`, fed `{"timestamp"}`), appended
        # to a turn's model as the transcript fold does on the legacy path; None for a provider with none.
        self._pricing_tier = pricing_tier
        self.task_id = task_id
        self.provider = provider
        self.native_session_ref = native_session_ref
        self.turn_id = ""
        self._clock = clock
        self._turns = 0
        self._items = 0
        self._open: dict | None = None  # {kind, item_id, message_id, parts}
        self._tools: dict[str, dict] = {}
        # The turn's agent text and whether it ran a tool: a tool-less turn whose text opens with one of the
        # dialect's `error_text_patterns` is an error the agent wrote as a message (`_text_failure`).
        self._turn_text = ""
        self._turn_tools = False
        # True after a prompt result whose turn `_text_failure` read as failed: the host posts its break.
        self.text_failed = False
        # A turn the agent reported failed through its own `_meta` (`_vendor_meta`): the text it then wrote is
        # the error's message, emitted as an `error` at the turn's end. None while the turn is healthy.
        self._failure: str | None = None
        # The latest reset the agent reported (a `rate_limit` event): a usage-limit prompt error that names
        # none of its own takes it.
        self._last_reset: str | None = None

    def event(self, kind: str, *, status: str = "completed", data: dict | None = None, item_id: str = "") -> dict:
        if kind not in KINDS:
            raise ValueError(f"unknown event kind {kind!r}")
        if status not in STATUSES:
            raise ValueError(f"unknown event status {status!r}")
        return {"v": SCHEMA_VERSION, "ts": iso_ms(self._clock()), "session_id": self.session_id,
                "native_session_ref": self.native_session_ref, "task_id": self.task_id, "provider": self.provider,
                "turn_id": self.turn_id, "item_id": item_id, "kind": kind, "status": status, "data": data or {}}

    def session_start(self, data: dict) -> list[dict]:
        return [self.event("session_start", data=dict(data))]

    def turn_start(self, source: str = "task", prompt: str = "") -> list[dict]:
        """`data.source` says who wrote the turn's prompt (PROMPT_SOURCES). Only an operator's prompt rides along
        in `data.prompt`, whole, so the console's chat view shows what was sent; a prompt over
        MAX_TEXT_CHUNK_BYTES is left out with `prompt_omitted: true` (never truncated)."""
        if source not in PROMPT_SOURCES:
            raise ValueError(f"unknown prompt source {source!r}")
        out = self._flush()
        self._turns += 1
        self._items = 0
        self._failure = None
        self._turn_text, self._turn_tools = "", False
        self.turn_id = f"t{self._turns}"
        data: dict = {"source": source}
        if source == "operator" and prompt:
            if _escaped_len(prompt) <= MAX_TEXT_CHUNK_BYTES:
                data["prompt"] = prompt
            else:
                data["prompt_omitted"] = True
        out.append(self.event("turn_start", data=data))
        return out

    @property
    def turn_text(self) -> str:
        """The current turn's agent text so far (capped), what a turn-end hook reads as the last assistant message."""
        return _without_nags(self._turn_text).strip()

    def on_update(self, update: dict) -> list[dict]:
        if not isinstance(update, dict):
            return []
        su = update.get("sessionUpdate")
        out: list[dict] = []
        if su in _CHUNK_KINDS:
            self._chunk(_CHUNK_KINDS[su], update, out)
        else:
            out.extend(self._flush())
            if su in ("tool_call", "tool_call_update"):
                self._tool(update, out)
            elif su == "plan":
                entries = update.get("entries")
                out.append(self.event("plan", data={"entries": entries if isinstance(entries, list) else []}))
            elif su == "usage_update":
                cost = _cost_usd(update.get("cost"))
                fields = {"context_used": update.get("used"), "context_size": update.get("size"),
                          "reported_cost_usd": cost, "cost_kind": COST_CUMULATIVE if cost is not None else None}
                reported = _meta(update).get("usage")
                if isinstance(reported, dict):
                    # An SDK v2 agent's own model tokens, spelled as a prompt result's `_meta` usage: the SDK ends
                    # each turn itself, so the agent has no prompt result to carry them. No window size, no window.
                    # Only well-formed counts: one bad value would make the control plane refuse the whole report.
                    fields.update({k: v for k, v in reported.items() if k.endswith("_tokens") and k in USAGE_KEYS
                                   and type(v) is int and v >= 0}, source="agent_meta")
                    if isinstance(reported.get("model"), str):
                        fields["model"] = reported["model"]
                    if not update.get("size"):
                        fields["context_used"] = fields["context_size"] = None
                out.append(self.event("usage", data=_usage("usage_update", fields)))
            elif su == "session_info_update":
                self._vendor_meta(update)
            else:
                log.debug("acp: sessionUpdate %r ignored", su)
        meta = _meta(update)
        raw = update.get("_meta")
        key = self._dialect.rate_limit_meta
        if key and isinstance(raw, dict) and isinstance(raw.get(key), dict):
            meta = {**meta, key: raw[key]}
        out.extend(self._meta_events(meta))
        return out

    def on_prompt_result(self, result: dict) -> list[dict]:
        result = result if isinstance(result, dict) else {}
        out = self._flush()
        self.text_failed = False
        if self._failure is not None:
            message = _without_nags(self._failure).strip() or "the turn failed"
            self._failure = None
            cause = error_cause(None, message, self._dialect)
            # A failed turn whose error names a break cause posts that break, as a failed prompt does.
            self.text_failed = bool(cause)
            out.extend(self.error(*self._classified(message, vendor=True), cause=cause))
        elif result.get("stopReason") == "end_turn" and (cause := self._text_failure()):
            self.text_failed = True
            out.extend(self.error(*self._classified(self.turn_text, vendor=True), cause=cause))
        meta = _meta(result)
        usage = meta.get("usage")
        if isinstance(usage, dict):
            # A prompt result's cost is that turn's, never the running total.
            fields = {k: v for k, v in usage.items() if k != "cost_kind"}
            if fields.get("reported_cost_usd") is None and "cost" in fields:
                fields["reported_cost_usd"] = _cost_usd(fields["cost"])
            out.append(self.event("usage", data=_usage("agent_meta", fields)))
        else:
            out.extend(self._turn_usage(result))
        out.extend(self._meta_events({k: v for k, v in meta.items() if k == "rate_limit"}))
        out.append(self.event("turn_end", data={"stop_reason": result.get("stopReason")}))
        return out

    def _turn_usage(self, result: dict) -> list[dict]:
        """The turn's tokens from the protocol's own `PromptResponse.usage` — one event per model when the agent
        splits them (`_meta.quota.model_usage`), else one unattributed event. A row's counts are camelCase
        or snake_case `input_tokens`/`output_tokens`, as each agent spells them. A single snake_case row
        carries no cached or thought split, so the turn's `result.usage` (which does) is read for it instead,
        attributed to that row's model; several snake_case rows keep their own counts with no split."""
        key = self._dialect.usage_meta
        quota = result.get("_meta", {}).get(key) if key and isinstance(result.get("_meta"), dict) else None
        per_model = quota.get("model_usage") if isinstance(quota, dict) else None
        rows = [(m.get("model"), m.get("token_count")) for m in per_model or () if isinstance(m, dict)]
        rows = [(model, counts) for model, counts in rows if isinstance(counts, dict)]
        whole = result.get("usage") if isinstance(result.get("usage"), dict) else None
        if whole is not None and (not rows or (len(rows) == 1 and "input_tokens" in rows[0][1])):
            rows = [(rows[0][0] if rows else None, whole)]
        tier = self._pricing_tier({"timestamp": iso_ms(self._clock())}) if self._pricing_tier else ""
        return [self.event("usage", data=_usage("prompt_result", {
            "model": f"{model}{tier}" if isinstance(model, str) and model else None,
            **self._counts(counts),
        })) for model, counts in rows]

    def _counts(self, counts: dict) -> dict:
        fields = {
            "input_tokens": _count(counts, "inputTokens", "input_tokens"),
            "cache_read_tokens": _count(counts, "cachedReadTokens", "cachedInputTokens"),
            "cache_write_tokens": _count(counts, "cachedWriteTokens", "cacheWriteInputTokens"),
            "output_tokens": _count(counts, "outputTokens", "output_tokens"),
            "reasoning_tokens": _count(counts, "thoughtTokens", "reasoningOutputTokens"),
        }
        if self._input_includes_cached and isinstance(fields["input_tokens"], int):
            # `reasoning_tokens` stays as information: the fold already bills the thoughts as output.
            fields["input_tokens"], fields["cache_read_tokens"], fields["output_tokens"] = fold_inclusive_counts(
                fields["input_tokens"], fields["cache_read_tokens"] or 0, fields["output_tokens"] or 0,
                fields["reasoning_tokens"] or 0)
        return fields

    def on_prompt_error(self, exc: BaseException) -> list[dict]:
        from acp.jsonrpc import ConnectionClosed, JsonRpcError

        out = self._flush()
        cause = ""
        if isinstance(exc, ConnectionClosed):
            cls, resets_at, message = "transport", None, str(exc) or "connection closed"
        elif isinstance(exc, JsonRpcError):
            message = error_detail(exc.data) or exc.message
            cls, resets_at = classify_error(exc.code, exc.message, exc.data, now=self._clock(), dialect=self._dialect)
            cause = error_cause(exc.data, exc.message, self._dialect)
        else:
            message = str(exc) or type(exc).__name__
            cls, resets_at = classify_error(None, message, now=self._clock(), dialect=self._dialect)
            cause = error_cause(None, message, self._dialect)
        if cls == "usage_limit" and not resets_at:
            resets_at = self._last_reset
        out.extend(self.error(cls, self._vendor_said(message), resets_at, cause=cause))
        out.append(self.event("turn_end", data={"stop_reason": "error"}))
        return out

    def approval_request(self, data: dict) -> list[dict]:
        return [self.event("approval_request", data=dict(data), item_id=str(data.get("item_id") or ""))]

    def user_input_request(self, request: Any, *, status: str) -> list[dict]:
        wire = request.to_wire() if hasattr(request, "to_wire") else dict(request)
        return [self.event("user_input_request", status=status, data=wire, item_id=str(wire.get("id") or ""))]

    def answer_received(self, decision: str, **data: Any) -> list[dict]:
        """The host's receipt for an operator's answer (ANSWER_DECISIONS). Never carries the answer's text: the
        turn it starts carries that (`turn_start.data.prompt`)."""
        if decision not in ANSWER_DECISIONS:
            raise ValueError(f"unknown answer decision {decision!r}")
        return [self.event("answer_received", data={"decision": decision, **data})]

    def error(self, cls: str, message: str, resets_at: str | None = None, *, cause: str = "") -> list[dict]:
        if cls not in ERROR_CLASSES:
            raise ValueError(f"unknown error class {cls!r}")
        data: dict = {"class": cls, "message": redact(message)}
        if resets_at:
            data["resets_at"] = resets_at
        if cause:
            data["cause"] = cause
        return [self.event("error", status="failed", data=data)]

    def session_end(self, exit_code: int | None, cause: str) -> list[dict]:
        out = self._flush()
        out.append(self.event("session_end", data={"exit_code": exit_code, "cause": cause}))
        return out

    # ---- internals

    def _classified(self, message: str, *, vendor: bool = False) -> tuple[str, str, str | None]:
        cls, resets_at = classify_error(None, message, now=self._clock(), dialect=self._dialect)
        return cls, self._vendor_said(message) if vendor else message, resets_at

    def _vendor_said(self, message: str) -> str:
        """An engine's error as the vendor's own sentence (`break_cause.vendor_message`) when the dialect says its
        error text IS the vendor's record (`vendor_error_text`), else unchanged: what Agent activity shows and the
        break's detail carries. Never applied to a message the host writes itself."""
        if not self._dialect.vendor_error_text or _vendor_message is None:
            return message
        return _vendor_message(message)

    def _text_failure(self) -> str:
        """The break cause of a turn that ran no tool and whose agent text OPENS with one of the dialect's
        `error_text_patterns` (an engine writing its error as a message, then `end_turn`), or "". Anchored and
        tool-less so an agent that merely quotes the wording mid-reply never condemns its own account."""
        if self._turn_tools or not self.turn_text:
            return ""
        # Never refined by the vendor classifier: agent text is the model's to write, and an account verdict is
        # read only off an engine's own error record (`error_cause`).
        cause = text_cause(self.turn_text, self._dialect, at_start=True)
        return "" if cause in _RECORD_ONLY_CAUSES else cause

    def _vendor_meta(self, update: dict) -> None:
        """A turn the agent reports failed in its own `_meta` namespace (`_THREAD_FAILED`) opens `_failure`."""
        meta = update.get("_meta")
        for vendor in meta.values() if isinstance(meta, dict) else ():
            status = vendor.get("threadStatus") if isinstance(vendor, dict) else None
            if not isinstance(status, dict):
                continue
            if status.get("type") == _THREAD_FAILED:
                self._failure = "" if self._failure is None else self._failure
            elif status.get("type") == "active":
                self._failure = None  # recovered within the turn

    def _chunk(self, kind: str, update: dict, out: list[dict]) -> None:
        text = _text(update.get("content"))
        if kind == "assistant_message":
            if self._failure is not None:
                self._failure = (self._failure + text)[-MAX_ACCUMULATED_CHARS:]
            if len(self._turn_text) < MAX_TEXT_CHUNK_BYTES:
                self._turn_text += text
        mid = update.get("messageId") if isinstance(update.get("messageId"), str) else ""
        if self._open and (self._open["kind"] != kind or (mid and mid != self._open["message_id"])):
            out.extend(self._flush())
        if self._open is None:
            self._items += 1
            item_id = mid or f"{self.turn_id or 't0'}-i{self._items}"
            self._open = {"kind": kind, "item_id": item_id, "message_id": mid, "parts": []}
        self._open["parts"].append(text)

    def _flush(self) -> list[dict]:
        item, self._open = self._open, None
        if item is None:
            return []
        text = "".join(item["parts"])
        if item["kind"] == "assistant_message":
            # Over the whole message, so a nag the engine streamed in pieces goes too.
            said = _without_nags(text)
            if said != text and not said.strip():
                return []  # a message that was only the CLI's update nag
            text = said
        return self._chunked(item["kind"], "completed", {}, "text", text, item["item_id"])

    def _chunked(self, kind: str, status: str, data: dict, field: str, text: str, item_id: str) -> list[dict]:
        """One event with `data[field] = text`, or n same-kind events carrying ordered slices + `chunk`."""
        parts = split_text(text)
        if len(parts) == 1:
            return [self.event(kind, status=status, data={**data, field: text}, item_id=item_id)]
        return [self.event(kind, status=status, data={**data, field: part, "chunk": {"index": i, "count": len(parts)}},
                           item_id=item_id) for i, part in enumerate(parts)]

    def _tool(self, update: dict, out: list[dict]) -> None:
        tcid = update.get("toolCallId")
        if not isinstance(tcid, str) or not tcid:
            return
        self._turn_tools = True
        state = self._tools.setdefault(tcid, {"emitted": set()})
        for key in ("title", "kind", "status", "locations", "rawInput", "rawOutput", "content"):
            if update.get(key) is not None:
                state[key] = update[key]
        # A terminal-backed tool's output, streamed in `_meta.terminal_output_delta` (Zed's terminal convention).
        delta = update.get("_meta", {}).get("terminal_output_delta") if isinstance(update.get("_meta"), dict) else None
        if isinstance(delta, dict) and isinstance(delta.get("data"), str):
            state["terminal_output"] = (state.get("terminal_output", "") + delta["data"])[-MAX_ACCUMULATED_CHARS:]
        tool_name = _meta(update).get("tool_name")
        if tool_name:
            state["tool_name"] = tool_name
        ev_kind = _TOOL_KINDS.get(state.get("kind"), "tool_call")
        status = state.get("status")
        if "in_progress" not in state["emitted"]:
            state["emitted"].add("in_progress")
            out.append(self.event(ev_kind, status="in_progress", data=self._tool_data(state, ev_kind, False),
                                  item_id=tcid))
        if status in _TERMINAL_TOOL and "terminal" not in state["emitted"]:
            state["emitted"].add("terminal")
            data = self._tool_data(state, ev_kind, True)
            if isinstance(data.get("output"), str):
                out.extend(self._chunked(ev_kind, status, data, "output", data["output"], tcid))
            else:
                out.append(self.event(ev_kind, status=status, data=data, item_id=tcid))

    @staticmethod
    def _tool_data(state: dict, ev_kind: str, final: bool) -> dict:
        locations = state.get("locations") if isinstance(state.get("locations"), list) else []
        data: dict = {"name": state.get("tool_name") or state.get("title"),
                      "paths": [loc["path"] for loc in locations if isinstance(loc, dict) and loc.get("path")]}
        if ev_kind == "command_execution":
            raw = state.get("rawInput")
            cmd = raw.get("command") if isinstance(raw, dict) else None
            if raw is None:
                # An engine sending no `rawInput` for `execute` names the command line as the title; an empty
                # `rawInput: {}` (an engine whose input is still streaming) has a title that is not one.
                cmd = state.get("title")
            data["command"] = " ".join(map(str, cmd)) if isinstance(cmd, list) else cmd
            if final:
                output = _tool_output(state)
                if output is not None:
                    data["output"] = output
        return data

    def _meta_events(self, meta: dict) -> list[dict]:
        out = []
        sub = meta.get("subagent")
        if isinstance(sub, dict):
            status = sub.get("status") if sub.get("status") in STATUSES else "completed"
            out.append(self.event("subagent", status=status, data=dict(sub), item_id=str(sub.get("id") or "")))
        rl = meta.get("rate_limit")
        native_rl = meta.get(self._dialect.rate_limit_meta) if self._dialect.rate_limit_meta else None
        rejected = True
        if not isinstance(rl, dict) and isinstance(native_rl, dict):
            rl = {"window": native_rl.get("rateLimitType"), "resets_at": native_rl.get("resetsAt")}
            # The dialect's report comes on every status change; only a rejection's reset is the one a usage
            # limit waits for.
            rejected = native_rl.get("status") == "rejected"
        if isinstance(rl, dict):
            resets_at = _norm_resets_at(rl.get("resets_at"))
            if rejected and resets_at:
                self._last_reset = resets_at
            out.append(self.event("rate_limit", data={"window": rl.get("window"), "resets_at": resets_at,
                                                      "account_slug": rl.get("account_slug")}))
        return out


def _tool_output(state: dict) -> str | None:
    if isinstance(state.get("terminal_output"), str):
        return state["terminal_output"]
    raw = state.get("rawOutput")
    if isinstance(raw, str):
        return raw
    if isinstance(raw, dict):
        for key in ("output", "stdout", "formatted_output"):
            if isinstance(raw.get(key), str):
                return raw[key]
    texts = []
    for block in state.get("content") or ():
        if isinstance(block, dict) and block.get("type") == "content":
            texts.append(_text(block.get("content")))
    if texts:
        return "".join(texts)
    return json.dumps(raw) if raw is not None else None
