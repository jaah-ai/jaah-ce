#!/usr/bin/env python3
"""break_cause.py — WHY a runner session broke, read off its own transcript. Single home for the
broken-session cause vocabulary's NODE half and for locating a session's transcript on disk.

A broken session ended because the SESSION broke (usage window, credit, API error, timeout, crash), not
the work. `CAUSES` mirrors
`apps/api/app/runner_causes.py::CAUSES` (a hook cannot import apps/api); `vanished`, `launch_failed` and `stalled` are
absent because no node can observe them.

NEVER WRONG-BY-DEFAULT: an unreadable transcript, an unmatched tail or any OSError yields `unknown`,
never a guess — a mis-attributed break inflates one bucket while emptying another. `classify_transcript`'s
`detail` tells "read, nothing matched" from "nothing was read". Stdlib only and nothing may raise at
import: a hook that fails to import kills the Claude Code session running it.
"""
from __future__ import annotations

import glob
import json
import os
import re
import time

# ── The cause vocabulary (node half) ────────────────────────────────────────────────────────────────
# Ordered most-specific first — `classify` returns the FIRST match. Loose about punctuation, strict
# about the phrase. EVERY PATTERN MUST BE SELF-IDENTIFYING: the scanned text includes the runner's own
# output, so a bare `429`, `billing` or `timeout` would classify a runner merely working on that subject.
_PATTERNS = (
    # ── THE ACCOUNT-CANNOT-RUN FAMILY, money first ──────────────────────────────────────────────────
    # `credit_exhausted` BEFORE `usage_limit`: usage_limit's open second-person form also matches
    # "you've hit your individual spend limit", which must count as money (needs a top-up), not a
    # self-clearing window. The two never co-occur in one tail. `/usage-credits` is Claude Code's own
    # remedy command, so the bare string is self-identifying.
    ("credit_exhausted", re.compile(
        r"credit balance is too low|insufficient (?:credits?|funds|quota)|"
        r"(?:out of|no remaining) credits?|/usage-credits|"
        r"(?:hit|reached|exceeded) (?:your|the|its) [^.\n]{0,30}?spend limit", re.I)),
    # The 5h/7d rolling window: self-clears, so the remedy (switch account or wait) differs from money.
    # The ONE family that favours recall over precision: a false positive benches a healthy account for
    # the verdict's TTL, but a false negative writes no verdict, so the walled credential goes straight
    # back to the replacement runner and the death repeats (`classify_turn_end` reads only the vendor's
    # last record for this reason). Alternatives: qualifier + "limit reached"; the second-person "you've
    # hit your … limit" (qualifier unconstrained — the vendor keeps inventing new ones); rate_limit_error
    # / labelled 429; "limit reached · resets|continuing automatically" (Claude Code's chrome); the
    # older "Claude (AI) usage limit reached|<epoch>".
    ("usage_limit", re.compile(
        r"(?:usage|weekly|monthly|daily|\d+[- ]hour|opus|sonnet|haiku) limit reached|"
        r"you(?:['\u2019]ve| have) (?:hit|reached|exceeded) (?:your|the) [^.\n]{0,40}?limit|"
        r"rate_limit_error|(?:Error|HTTP|status):? 429|"
        r"limit reached \u00b7 (?:resets|continuing automatically)|"
        r"claude (?:ai )?usage limit reached", re.I)),
    # OAuth credentials went invalid mid-session; remedy is a re-login. Claude Code's own chrome ends in
    # its remedy command after a middle dot ("Login expired · Please run /login", "OAuth token revoked · …"),
    # which is what makes those self-identifying.
    ("auth_expired", re.compile(
        r"oauth token (?:has )?expired|invalid[_ ]api[_ ]key|invalid x-api-key|authentication_error|"
        r"(?:Error|HTTP|status):? 401|"
        r"(?:login expired|oauth token revoked) · please run /login", re.I)),
    # Before api_error: a different remedy (retry). The verb `timed out`, not the noun `timeout`, which
    # is what a runner configures.
    ("timeout", re.compile(r"timed out|\b(?:ETIMEDOUT|ECONNRESET)\b", re.I)),
    # Everything else the API said no to: overload, 5xx, an internal error.
    ("api_error", re.compile(r"overloaded_error|api_error|internal server error|API Error:", re.I)),
)

# ── THE PROVIDER REFUSES THE MODEL TO THE ACCOUNT (Bedrock model access) ─────────────────────────────
# No retry, relaunch or other credential on the same provider account clears it, so the control plane fails
# the task at once (`apps/api/app/runner_causes.py::TERMINAL_CAUSES`). AWS's own sentences, which people
# QUOTE (a task body, a runner fixing this classifier), so this is read ONLY off a vendor-written API-error
# record (`classify_api_error`), never over prose: a prose match would fail a task outright. The ACP host
# reads the same phrases off its engine's error, never its agent text (the Claude Code rows' `AcpDialect`,
# `agent_providers.py::_CLAUDE_CODE_ACP`; a test pins them).
MODEL_REFUSED_CAUSE = "model_refused"
MODEL_REFUSED = re.compile(
    r"is not available for this account\. You can explore other available models|"
    r"anthropic\.claude[\w.:-]* is not available for this account|"
    r"You don't have access to the model with the specified model ID|"
    r"Model use case details have not been submitted", re.I)

# ── THE VENDOR RETIRED THE CLIENT ───────────────────────────────────────────────────────────────────
# Google's own sentence, often after a 401 or `Onboarding failed:`: no re-login clears it, so it is terminal
# like `MODEL_REFUSED` and read off vendor-written error text only, never prose. The ACP host reads it off the
# engine's error (`agent_providers.py::ANTIGRAVITY`'s `AcpDialect`).
CLIENT_UNSUPPORTED_CAUSE = "client_unsupported"
CLIENT_UNSUPPORTED = re.compile(r"This client is no longer supported for Gemini Code Assist", re.I)

# ── A VENDOR'S OWN ERROR RECORD: extra alternatives, never scanned over a transcript's prose ────────────
# `classify_vendor_error` alone reads these: its input is one structured error record a non-Claude CLI
# wrote (`AgentAdapter.turn_error`), not the runner's own words, so a vendor's plain phrasing is evidence
# there — and would NOT be in `_PATTERNS`, where a runner writing integration code for that vendor
# ("API key not valid", "status 500") would condemn its own account. Tried in `_PATTERNS`' cause order.
_VENDOR_ERROR_PATTERNS = {
    # OpenAI's billing refusal on an API key: its 429 is money, not a window.
    # A prepaid key out of credit answers a 429 `RESOURCE_EXHAUSTED` with the depleted-credits sentence: money
    # too (a top-up), and tried before `usage_limit`, so the sentence wins over the status it rides.
    "credit_exhausted": re.compile(
        r"insufficient_quota|exceeded your current quota|prepayment credits are depleted", re.I),
    # Google's own status for a spent quota, and its CLI's rendering of it.
    "usage_limit": re.compile(r"RESOURCE_EXHAUSTED|exhausted your (?:\w+ )?(?:quota|capacity)", re.I),
    # Google's and OpenAI's own words for a rejected key.
    "auth_expired": re.compile(r"API key not valid|API_KEY_INVALID|incorrect API key provided", re.I),
    # The vendor's HTTP status for a server-side failure.
    "api_error": re.compile(r"(?:HTTP|status):? 5\d\d\b", re.I),
}

CAUSES = (
    "usage_limit", "credit_exhausted", "auth_expired", "model_refused", "client_unsupported", "timeout", "api_error",
    "crashed", "unknown",
)

UNKNOWN = "unknown"

# The causes no relaunch clears, read only off a vendor's error record (`classify_api_error`), in match order.
_TERMINAL_PATTERNS = ((CLIENT_UNSUPPORTED_CAUSE, CLIENT_UNSUPPORTED), (MODEL_REFUSED_CAUSE, MODEL_REFUSED))

# A break announces itself in the last few records; reading more of a megabyte transcript risks
# matching a long-recovered error as this session's cause.
_TAIL_BYTES = 65536

# ── CLAUDE CODE'S USAGE-LIMIT GRACE NOTE ─────────────────────────────────────────────────────────────
# At the usage wall Claude Code (2.1.310+) no longer ends the turn on an API error: it writes a meta `user`
# record carrying `usageLimitNote` ("wrap_up": "a short grace allowance remains…") and lets the model finish,
# so the turn ends on ordinary prose and its `Usage limit reached · continuing automatically` banner is
# written AFTER the Stop hook ran. No StopFailure fires; the session idles on its auto-continue until reaped
# (once seen: 28 minutes, then `vanished`). The note is a structural key the CLI writes, never prose, so it
# is evidence wherever a hook reads it — but only for the turn it belongs to (`turn_usage_limit_note`).
USAGE_LIMIT_NOTE_KEY = "usageLimitNote"

# A turn can run long after its note (the grace allowance pays for a few more tool calls, each result a
# record), so the note is looked for further back than a break; a byte check skips the parse when absent.
_NOTE_TAIL_BYTES = 1 << 20

# Enough to recognise the error, short enough for a Task body line.
_DETAIL_CHARS = 300

# ── THE FLUSH RACE ────────────────────────────────────────────────────────────────────────────────
# A break is normally observed (StopFailure hook, ~200ms) BEFORE Claude Code flushes the record, so a
# single read answers `unknown`. The retry lives in the classifier because both callers (`settle`,
# `stopfailure`) funnel through it, and `unknown` writes no account verdict. Sub-second and paid only
# when blind: a first-read match returns without sleeping.
_TAIL_READ_ATTEMPTS = 3
_TAIL_READ_DELAY_SECONDS = 0.25


def munge(cwd: str) -> str:
    """Claude Code's project-dir munge of a cwd: every non-alphanumeric byte -> '-'."""
    return "".join(c if c.isalnum() and c.isascii() else "-" for c in cwd)


def safe_mtime(path: str):
    """`path`'s mtime, or None if it cannot be stat'd (an uncaught OSError in a hook kills the session)."""
    try:
        return os.stat(path).st_mtime
    except OSError:
        return None


def worktree_root() -> str:
    """WHICH CWD IDENTIFIES THIS SESSION'S FILES ON DISK — the single home for that question.

    Claude Code munges the project dir ONCE from the cwd at session start; the agent then `cd`s around,
    so a `$PWD`-derived key names a project dir no account has — autoclose read that as "blind" and
    killed a busy runner, and the break classifier recorded `unknown` unclassified. The leased worktree
    root is immutable and is exactly the munge source. Ladder: 1. `$JAAH_WORKTREE` (the leased slot,
    exported by `launch.sh` and pinned by `fleet_launch.build_settings`); 2. `$PWD`, then `os.getcwd()`
    (sessions the launcher exports nothing for). Unset OR EMPTY skips a rung.

    Assumed here, verified at the gate: `autoclose-check.py` requires the root to hold
    `$CONTROL_RUNNER_SESSION_ID`'s transcript before judging it. `$CLAUDE_PROJECT_DIR` is REJECTED as a
    rung: it is the vendor's value, unverifiable at a kill gate, and wrong exactly where it matters — a
    wrong-but-populated root (any pool slot on this host, e.g. a subdirectory start) makes autoclose judge
    a foreign session's evidence and KILL, where a wrong-and-empty root only declines. Console/user
    sessions stay uncovered on purpose rather than closed with an unverifiable guess."""
    return (
        os.environ.get("JAAH_WORKTREE")
        or os.environ.get("PWD")
        or os.getcwd()
    )


def project_dir(cwd: str = "", config_dir: str = "") -> str:
    """The Claude Code project dir holding this cwd's session files. Defaults read env
    (`worktree_root()` + $CLAUDE_CONFIG_DIR), not hook stdin, so a caller in a multi-command hook chain
    never competes for that stdin. An explicit `cwd` always wins."""
    cwd = cwd or worktree_root()
    config_dir = config_dir or os.environ.get("CLAUDE_CONFIG_DIR") or os.path.expanduser("~/.claude")
    return os.path.join(config_dir, "projects", project_name(cwd))


def project_name(cwd: str) -> str:
    """The project-dir NAME Claude Code files `cwd`'s sessions under: the munge of its PHYSICAL path, since
    Claude Code munges `getcwd()` — on macOS `/var/jaah/repos/1` is filed as `-private-var-jaah-repos-1`."""
    return munge(os.path.realpath(cwd))


def session_transcript_path(proj_dir: str):
    """The live session's transcript in `proj_dir`, or None when there is no readable candidate.

    Newest-by-mtime answers WHICH file is live, though mtime is the wrong clock for WHETHER it is still
    active (`autoclose-check._last_record_ts`). An unstat'able file sorts last instead of raising."""
    if not os.path.isdir(proj_dir):
        return None
    candidates = sorted(
        glob.glob(os.path.join(proj_dir, "*.jsonl")),
        key=lambda p: safe_mtime(p) or 0.0,
        reverse=True,
    )
    return candidates[0] if candidates else None


def classify(text: str, *, extra=None):
    """(cause, detail) for a blob of transcript text. `(UNKNOWN, "")` when nothing matches — never a
    guess. `detail` is the matched line, trimmed, so the incident carries the words that produced the
    verdict and not just the verdict. `extra` (cause -> pattern) widens a cause for a caller whose text
    is not prose (`classify_vendor_error`)."""
    for cause, pattern in _PATTERNS:
        match = pattern.search(text)
        if match is None and extra and cause in extra:
            match = extra[cause].search(text)
        if match is None:
            continue
        # The matched LINE, not the match: what a human reads to confirm the classification.
        start = text.rfind("\n", 0, match.start()) + 1
        end = text.find("\n", match.end())
        line = text[start:end if end != -1 else len(text)].strip()
        return cause, line[:_DETAIL_CHARS]
    return UNKNOWN, ""


def classify_api_error(text: str, *, extra=None):
    """(cause, detail) for ONE vendor-written API-error record's text: the terminal refusals first
    (`CLIENT_UNSUPPORTED`, `MODEL_REFUSED`, readable only here), then `classify`."""
    for cause, pattern in _TERMINAL_PATTERNS:
        match = pattern.search(text)
        if match is not None:
            start = text.rfind("\n", 0, match.start()) + 1
            end = text.find("\n", match.end())
            return cause, text[start:end if end != -1 else len(text)].strip()[:_DETAIL_CHARS]
    return classify(text, extra=extra)


def classify_vendor_error(text: str):
    """(cause, detail) for ONE vendor-written API-error record (`AgentAdapter.turn_error`'s text) — the
    shared patterns plus `_VENDOR_ERROR_PATTERNS`, which only a structured error record may be read with.
    `detail` is the vendor's own sentence (`vendor_message`) when the record wraps one."""
    cause, detail = classify_api_error(text, extra=_VENDOR_ERROR_PATTERNS)
    message = vendor_message(text)
    return cause, (message[:_DETAIL_CHARS] if cause != UNKNOWN and message != text else detail)


# The prefixes a CLI writes in front of a vendor's JSON error body (an engine's own wording, no vendor's name).
_CLI_ERROR_PREFIXES = ("Agent execution error: ", "[API Error: ")
# A real stack frame, never prose that happens to start with "at": JavaScript's `at fn (file:1:2)` / `at file:1:2`,
# Python's `File "x.py", line 3`.
_JS_FRAME = re.compile(r"^\s+at (?:[^\s()]+ \()?[^\s()]+:\d+:\d+\)?\s*$")
_PY_FRAME = re.compile(r'^\s+File "[^"]+", line \d+')


def vendor_message(text: str) -> str:
    """The vendor's own sentence inside ONE vendor error record, else `text` unchanged — the one reading of it,
    for a break's detail and the error an operator reads (`acp/events.py`, vendor-record paths only): the
    `error.message` of a JSON error body that starts the record or follows a CLI's prefix (`_CLI_ERROR_PREFIXES`),
    the last line of a Python traceback, or the line above a JavaScript stack."""
    if not text:
        return text
    body = text.lstrip()
    stripped = True
    while stripped:
        stripped = False
        for prefix in _CLI_ERROR_PREFIXES:
            if body.startswith(prefix):
                body, stripped = body[len(prefix):].lstrip(), True
    if body.startswith("{"):
        try:
            record, _end = json.JSONDecoder().raw_decode(body)
        except ValueError:
            record = None
        inner = record.get("error") if isinstance(record, dict) else None
        message = inner.get("message") if isinstance(inner, dict) else None
        if isinstance(message, str) and message.strip():
            return message.strip()
    lines = [line for line in text.splitlines() if line.strip()]
    if any(line.startswith("Traceback (most recent call last)") for line in lines) and any(
            _PY_FRAME.match(line) for line in lines):
        said = [line for line in lines if not line[:1].isspace() and not line.startswith("Traceback")]
        return said[-1].strip() if said else text
    frames = [i for i, line in enumerate(lines) if _JS_FRAME.match(line)]
    if frames and frames[0] > 0:
        return lines[frames[0] - 1].strip()
    return text


def classify_transcript(
    path: str,
    *,
    attempts: int = _TAIL_READ_ATTEMPTS,
    delay: float = _TAIL_READ_DELAY_SECONDS,
    sleep=time.sleep,
):
    """(cause, detail) read off a transcript's tail. Only record TEXT is scanned — the JSON envelope
    (field names, tool names, quoted paths) would otherwise match a pattern.

    `unknown` means (a) a transcript was read and nothing matched — the honest floor, empty `detail` —
    or (b) no transcript was resolved/readable, a measurement failure, which says so in `detail`.
    Measured 2026-08-19 on all 22 dev-CP incident rows: 14 `unknown`, 8 with no `transcript` at all;
    only 2 of those (1901, 1925) are attributable to the cwd drift `worktree_root()` removes, the other 6
    are unattributed — so (b) stays a category, marked in the detail rather than as a new cause.

    Retried while `unknown` (the flush race, see `_TAIL_READ_ATTEMPTS`). `sleep`/`attempts`/`delay` are
    injectable only for tests."""
    if not path:
        # (b): prose unlike any transcript line. Not retried: nothing will appear at an unresolved path.
        return UNKNOWN, f"no transcript resolved under {worktree_root()}"[:_DETAIL_CHARS]
    # The last attempt's answer is returned: the retry changes WHEN the verdict is taken, never what it may be.
    result = (UNKNOWN, "")
    for attempt in range(max(1, attempts)):
        # Sleep before a re-read, never the first: an already-flushed break pays nothing.
        if attempt:
            sleep(delay)
        try:
            with open(path, "rb") as handle:
                if os.fstat(handle.fileno()).st_size > _TAIL_BYTES:
                    handle.seek(-_TAIL_BYTES, os.SEEK_END)
                tail = handle.read()
        except OSError:
            # (b) one step further: resolved but unreadable (rotated/being created) — retried, same race.
            result = (UNKNOWN, f"transcript unreadable: {path}"[:_DETAIL_CHARS])
            continue
        # The newest API-error record first: a terminal refusal is read off it alone (`_TERMINAL_PATTERNS`).
        api_error = _last_api_error_text(tail)
        if api_error and any(pattern.search(api_error) for _, pattern in _TERMINAL_PATTERNS):
            return classify_api_error(api_error)
        result = classify(_record_text(tail))
        if result[0] != UNKNOWN:
            return result
    return result


def classify_turn_end(path: str):
    """(cause, detail) for the record a turn ENDED on — the ordinary-`Stop` complement to
    `classify_transcript`, which is what a `StopFailure`/settle caller reads.

    A usage wall can end a turn with a plain `Stop`, so no error hook fires and the session idles until
    reclaimed as `vanished`, then relaunched on the same walled account. ONLY THE VENDOR'S OWN LAST
    RECORD IS EVIDENCE: with no error signal at a `Stop`, scanning prose would classify a runner merely
    talking about limits and kill it at every turn boundary. So only the last text-carrying record
    counts, and only if it is an API-error record (`isApiErrorMessage: true`) or a `system` record;
    else the turn's own usage-limit grace note (`turn_usage_limit_note`), the vendor's other wall shape;
    anything else answers `(UNKNOWN, "")`. No retry: a `Stop` fires after the last record is on disk.
    Never raises — a hook exception kills the live session."""
    verdict = _classify_last_record(path)
    return verdict if verdict[0] != UNKNOWN else turn_usage_limit_note(path)


def turn_usage_limit_note(path: str):
    """`("usage_limit", detail)` when the session's CURRENT turn carries Claude Code's usage-limit note
    (`USAGE_LIMIT_NOTE_KEY`), else `(UNKNOWN, "")`.

    Current turn: no `system`/`turn_duration` record (written as a turn closes) and no record of a newer
    `promptId` since the note — a note an earlier turn survived (the window reset, the session continued)
    is no wall now. Measured on 2.1.310: the note and the turn's later records share its `promptId`; a CLI that
    stopped stamping it on the note would make this miss mid-turn (never fire falsely), leaving the turn-end
    banner to the Stop read and the node's safety net. Read by the PreToolUse check the moment the note lands
    and by the turn-end reader.
    Never raises — a hook exception kills the live session."""
    if not path:
        return UNKNOWN, ""
    try:
        with open(path, "rb") as handle:
            if os.fstat(handle.fileno()).st_size > _NOTE_TAIL_BYTES:
                handle.seek(-_NOTE_TAIL_BYTES, os.SEEK_END)
            tail = handle.read()
    except OSError:
        return UNKNOWN, ""
    if USAGE_LIMIT_NOTE_KEY.encode() not in tail:
        return UNKNOWN, ""   # the hot path: one byte scan per tool call
    note = None
    for raw in tail.splitlines():
        try:
            record = json.loads(raw)
        except (ValueError, RecursionError):
            continue
        if not isinstance(record, dict):
            continue
        if record.get("type") == "user" and isinstance(record.get(USAGE_LIMIT_NOTE_KEY), str):
            note = record
        elif note is not None and (
            (record.get("type") == "system" and record.get("subtype") == "turn_duration")
            or (record.get("promptId") and record.get("promptId") != note.get("promptId"))
        ):
            note = None
    if note is None:
        return UNKNOWN, ""
    text = "\n".join(_text_of(note)).strip()
    detail = text.splitlines()[0] if text else ""
    return "usage_limit", f"{USAGE_LIMIT_NOTE_KEY}={note[USAGE_LIMIT_NOTE_KEY]}: {detail}"[:_DETAIL_CHARS]


def _classify_last_record(path: str):
    """`classify_turn_end`'s read of the turn's last text-carrying record (its docstring)."""
    if not path:
        return UNKNOWN, ""
    try:
        with open(path, "rb") as handle:
            if os.fstat(handle.fileno()).st_size > _TAIL_BYTES:
                handle.seek(-_TAIL_BYTES, os.SEEK_END)
            tail = handle.read()
    except OSError:
        return UNKNOWN, ""
    last = None
    for raw in tail.splitlines():
        try:
            record = json.loads(raw)
        except (ValueError, RecursionError):
            # ValueError covers a mid-record seek / non-record line; RecursionError a pathological one.
            # Runs on every turn boundary, and a hook exception kills the session.
            continue
        if isinstance(record, dict) and _text_of(record) and not _is_task_notification(record):
            last = record
    if last is None:
        return UNKNOWN, ""
    if last.get("isApiErrorMessage") is not True and last.get("type") != "system":
        return UNKNOWN, ""
    text = "\n".join(_text_of(last))
    # A `system` record may echo hook or gate output: only the vendor's API-error record may carry a terminal cause.
    return classify_api_error(text) if last.get("isApiErrorMessage") is True else classify(text)


def _last_api_error_text(tail: bytes) -> str:
    """The text of the newest `isApiErrorMessage` record in a transcript tail, or "" — the vendor's own
    words for the error that ended the turn."""
    last = ""
    for raw in tail.splitlines():
        try:
            record = json.loads(raw)
        except (ValueError, RecursionError):
            continue
        if isinstance(record, dict) and record.get("isApiErrorMessage") is True and not _is_task_notification(record):
            last = "\n".join(_text_of(record))
    return last


def _record_text(tail: bytes) -> str:
    """The human-readable text of every complete record in a transcript tail, newest LAST (the order
    `classify` reads). Partial or unparseable lines are skipped rather than raising."""
    lines = []
    for raw in tail.splitlines():
        try:
            record = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            continue  # the seek landed mid-record, or this line is not a record
        if isinstance(record, dict) and not _is_task_notification(record):
            lines.extend(_text_of(record))
    return "\n".join(lines)


def _is_task_notification(record: dict) -> bool:
    """Is this record a background subagent's `<task-notification>` delivered into the session? Its words are the
    SUBAGENT's (its own usage wall, its own API error) while the session lives on, so no break is read off it.
    Starts-with, not contains: a session's own record quoting the block is still its own."""
    texts = _text_of(record)
    return bool(texts) and texts[0].lstrip().startswith("<task-notification>")


def _text_of(record: dict):
    """Every text fragment carried by one transcript record: `message.content` (string or text blocks)
    — where an `isApiErrorMessage` record's cause lives — and a record's top-level `content`, which is
    where Claude Code writes its quota banner (`{"type": "system", "content": "Usage limit reached ·
    …"}`). Both are prose for a human; field names, tool names and paths are never scanned."""
    content = record.get("content")
    if isinstance(content, str) and content:
        return [content]
    message = record.get("message")
    if isinstance(message, str):
        return [message]
    if not isinstance(message, dict):
        return []
    content = message.get("content")
    if isinstance(content, str):
        return [content]
    if not isinstance(content, list):
        return []
    return [
        block["text"] for block in content
        if isinstance(block, dict) and isinstance(block.get("text"), str)
    ]
