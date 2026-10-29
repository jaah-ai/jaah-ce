"""A dead run's pane-log tail: which sessions owe one, and how the node's raw bytes become what an operator reads.

jaahd tees every fleet runner's tmux pane to a `*.pane.log` on the node (`apps/jaahd/fleet_dispatch.py`). When a
session ends with no reason of its own (`node_log_history.NODE_EXPLAINED_ENDS`: `vanished`, `crashed`, …), the
node's sweep is offered it on `GET /api/agent/sessions/work` (`awaiting_pane_log`) and posts the file's last
`PANE_LOG_TAIL_BYTES` raw (`POST /api/agent/sessions/{sid}/pane-log`). This module turns those bytes into the
stored tail, once, at ingest:

1. `render` — a pane log is a terminal's byte stream, not text: escape sequences are dropped, a cursor move to
   another row or a carriage return becomes a line break, repeated redraws of one line collapse to one.
2. `secret_redact.redact` — the shared transcript pass (`apps/jaahd/transcript_redact.py`), best-effort like a
   transcript's: `pane_log_redactions` counts what was CAUGHT, never what was missed.
3. `tidy` — quiets what buries the cause: drops the adapter's known-harmless probe failure (`_noise`) and folds a
   retry countdown — frames that differ only by a spinner glyph or one number ticking down
   (`_next_frame`) become the last of them marked `[×N]`, and "8 7 6 5" within a line becomes "8…5". It runs
   again as a tail is served, so a tail stored before it existed reads the same.
4. The cap — the newest `MAX_STORED_CHARS` survive, cut at a line boundary.

Redaction runs before the cap, so a secret is never half-cut into something the patterns stop recognising; the
node's own cut can split the FIRST line, which is therefore dropped whenever the node sent a full window.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Optional

from app import secret_redact

# The node's read window (`apps/jaahd/session_sweep.py::PANE_LOG_TAIL_BYTES` reads this many bytes; the wire
# model bounds `tail` to it in characters, never fewer than the bytes decode to).
PANE_LOG_TAIL_BYTES = 65536
# What is stored and served: enough for the last few screens, small enough for a row and a page.
MAX_STORED_CHARS = 16_000
# How long after its end a session is still asked for: past it the node's file has likely moved on to later runs.
CAPTURE_WINDOW = timedelta(hours=24)
# A file written this long after the run's end was likely appended to by a later run of the same task.
LATER_RUN_SLACK = timedelta(minutes=5)
# The narrowest pane a row can have wrapped at (tmux's default width): only a row this long may continue onto the
# next, so a short line ending in a secret never swallows the line after it.
WRAP_MIN_COLUMNS = 80
# Per `/work` tick, so one node's backlog cannot make the work answer large.
WORK_BATCH = 20

STATE_CAPTURED = "captured"
STATE_MISSING = "missing"

# CSI (`ESC [ … final`), OSC (`ESC ] … BEL|ST`), DCS/SOS/PM/APC strings, then any other two-byte escape.
_OSC = re.compile(r"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?")
_STRING = re.compile(r"\x1b[P^_X][^\x1b]*(?:\x1b\\)?")
_CSI = re.compile(r"\x1b\[([0-?]*)[ -/]*([@-~])")
_OTHER_ESC = re.compile(r"\x1b[ -/]*[0-~]?")
# CSI finals that put the cursor on another row: up/down/next/previous line, absolute position, vertical position.
_ROW_MOVES = frozenset("ABEFHfd")
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")

# Lines that read like a cause and are not one. The ACP adapter probes `claude auth status --settings …`, which a
# CLI without that flag refuses ("error: unknown option '--settings'", on the same line or the next); the run goes
# on regardless. Any other auth-status failure is a real signal and stays.
_PROBE_FAILED = "claude auth status failed:"
_PROBE_REFUSAL = "unknown option '--settings'"
# What differs between two frames of one retry countdown: one number ticking down, and a spinner's glyph.
_NUMBER = re.compile(r"\d+")
_SPINNER = re.compile(r"[\u2800-\u28ff\u2722-\u273d\u23fa\u00b7]")
# Only a frame that says it is one may tick: a bare number, a spinner's, or a retry's (`⏺` marks every message,
# so it is no spinner here). "Exit code 1" then
# "Exit code 0" are two outcomes, never one countdown.
_FRAME = re.compile(r"^\s*\d+\s*$|[\u2800-\u28ff\u2722-\u273d]|\bretr(?:y|ying|ies)\b", re.I)
# Four or more small numbers in a row, space-separated: a countdown when each is one less than the last.
_NUMBER_RUN = re.compile(r"\b\d{1,3}(?:[ \t]+\d{1,3}){3,}\b")


def _csi(match: re.Match[str]) -> str:
    """A row-changing cursor move separates what was drawn before it from what after; anything else drops."""
    if match.group(2) in _ROW_MOVES:
        return "\n"
    # A column move within the row (`C`, `G`) stands between two drawn words.
    return " " if match.group(2) in "CG" else ""


def render(raw: str) -> str:
    """The readable lines of a terminal byte stream: escapes gone, one drawn line per line, no blank lines, and a
    line redrawn several times in a row kept once. Only trailing whitespace is dropped: a line's leading indent
    (a traceback's, a diff's, a YAML block's) is what makes it readable."""
    text = _OSC.sub("", raw)
    text = _STRING.sub("", text)
    text = _CSI.sub(_csi, text)
    text = _OTHER_ESC.sub("", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\t", "    ")
    text = _CONTROL.sub("", text)
    lines: list[str] = []
    for line in text.split("\n"):
        line = line.rstrip()
        if line.strip() and (not lines or lines[-1] != line):
            lines.append(line)
    return "\n".join(lines)


def _countdown(match: re.Match[str]) -> str:
    """A strictly descending-by-one run of numbers as its ends ("8…5"); any other run of numbers as it was."""
    numbers = [int(n) for n in match.group(0).split()]
    if all(a - b == 1 for a, b in zip(numbers, numbers[1:], strict=False)):
        return f"{numbers[0]}…{numbers[-1]}"
    return match.group(0)


def _noise(lines: list[str]) -> set[int]:
    """The indexes of the adapter's harmless probe failure (`_PROBE_FAILED`) and its refusal line."""
    dropped: set[int] = set()
    for i, line in enumerate(lines):
        if _PROBE_FAILED in line:
            if _PROBE_REFUSAL in line:
                dropped.add(i)
            elif i + 1 < len(lines) and _PROBE_REFUSAL in lines[i + 1]:
                dropped.update((i, i + 1))
    return dropped


def _next_frame(before: str, after: str) -> bool:
    """Whether `after` redraws `before`: the same words, a spinner glyph aside, with every number equal or (one of
    them, on a line `_FRAME` calls a countdown) one lower. A climbing counter or a changed code is distinct output.
    Numbers are masked with NUL, which `render` has already removed, so no drawn character can stand in for one."""
    if _SPINNER.sub("", _NUMBER.sub("\x00", before)) != _SPINNER.sub("", _NUMBER.sub("\x00", after)):
        return False
    olds, news = [int(n) for n in _NUMBER.findall(before)], [int(n) for n in _NUMBER.findall(after)]
    if len(olds) != len(news):
        return False
    ticks = [old - new for old, new in zip(olds, news, strict=True) if old != new]
    return not ticks or (ticks == [1] and _FRAME.search(after) is not None)


def tidy(text: str) -> str:
    """Rendered lines with the adapter's harmless probe noise dropped and each retry countdown folded to its last
    frame (`[×N]` when it stood for several), so the sentence that says why the run stopped is not buried. Safe to
    run twice: a folded line has no like neighbours left."""
    rows = [line for line in text.split("\n") if line.strip()]
    dropped = _noise(rows)
    lines: list[str] = []
    runs: list[int] = []
    for i, line in enumerate(rows):
        if i in dropped:
            continue
        line = _NUMBER_RUN.sub(_countdown, line)
        if lines and _next_frame(lines[-1], line):
            lines[-1] = line
            runs[-1] += 1
            continue
        lines.append(line)
        runs.append(1)
    return "\n".join(line if run == 1 else f"{line} [×{run}]" for line, run in zip(lines, runs, strict=True))


def _newest(text: str, limit: int) -> str:
    """The last `limit` characters of `text`, starting at a line boundary when one is inside them."""
    if len(text) <= limit:
        return text
    tail = text[-limit:]
    cut = tail.find("\n")
    return tail[cut + 1:] if 0 <= cut < len(tail) - 1 else tail


def _redact_lines(text: str) -> tuple[str, int]:
    """The shared pass over each line, joining a line to the one before whenever the pair redacts differently
    together than apart: a terminal wraps a long token onto the next row, which `render` makes a line break, so a
    key split in two (its first half often caught by a key-bound pattern, its tail by nothing) is redacted whole
    only when rejoined. A joined run keeps growing while the next row still continues the secret. Only a row as
    wide as a pane (`WRAP_MIN_COLUMNS`) can have wrapped, so a short line is never joined to the next."""
    raws: list[str] = []
    for line in text.split("\n"):
        if raws and len(raws[-1]) >= WRAP_MIN_COLUMNS:
            joined = raws[-1] + line
            together = secret_redact.redact(joined)[0]
            apart = secret_redact.redact(raws[-1])[0] + secret_redact.redact(line)[0]
            if together != apart:
                raws[-1] = joined
                continue
        raws.append(line)
    hits = 0
    lines: list[str] = []
    for raw in raws:
        cleaned, found = secret_redact.redact(raw)
        lines.append(cleaned)
        hits += found
    return "\n".join(lines), hits


def store_form(raw: str) -> tuple[Optional[str], int]:
    """`(the text to store, how many secrets the redaction pass replaced)` for the node's raw tail; `None` when
    nothing readable is left (an empty file, or escapes only), which reads as no pane log.

    A window as large as the node reads began mid-file, so its first line is partial and is dropped."""
    if len(raw.encode("utf-8", errors="replace")) >= PANE_LOG_TAIL_BYTES:
        newline = raw.find("\n")
        raw = raw[newline + 1:] if newline >= 0 else raw
    redacted, hits = _redact_lines(render(raw))
    # Tidied after redaction, so a fold never reshapes a secret before the patterns see it.
    return (_newest(tidy(redacted), MAX_STORED_CHARS) or None), hits


def written_after_end(written_at: Optional[datetime], ended_at: Optional[datetime]) -> bool:
    """Whether the file was written well after the run ended: every run of a task appends to one pane log, so
    the tail may then show a later run."""
    return written_at is not None and ended_at is not None and written_at > ended_at + LATER_RUN_SLACK


def state(captured_at: Optional[object], tail: Optional[str]) -> Optional[str]:
    """`captured` (a tail to read), `missing` (the node had none), or None (never asked)."""
    if captured_at is None:
        return None
    return STATE_CAPTURED if tail is not None else STATE_MISSING
