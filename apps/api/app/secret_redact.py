"""Secret redaction for text a person typed into support — the node's own pass, loaded, never forked.

The one redaction pass lives in `apps/jaahd/transcript_redact.py` (its patterns, its placeholder, its
coverage claim); a second copy here would drift from it the first time a pattern is added, so this module
LOADS that file by path and adds nothing. It is loaded by path rather than imported because the daemon's
modules must never become importable by the API (the local compose stack's `/app/jaahd`
mount), so `/app/jaahd` is never put on `sys.path`.

Where the file is found: `app/shared_module.py` (the image's `/app/shared`, else the checkout).

Neither present is a DEPLOYMENT DEFECT, and it raises: text is never passed through unredacted.
"""
from __future__ import annotations

from types import ModuleType
from typing import Any

from app import shared_module

#: The candidates, in order. A tuple so a test can point it at nothing.
CANDIDATES = shared_module.candidates("transcript_redact.py", "apps/jaahd")


class RedactionUnavailable(RuntimeError):
    """The shared redaction pass could not be found — refusing to store unredacted text."""


def _load() -> ModuleType:
    return shared_module.load(
        "_support_transcript_redact", CANDIDATES, RedactionUnavailable, "; refusing to store text unredacted"
    )


def redact(text: str) -> tuple[str, int]:
    """`(text with every secret-shaped value replaced by the shared placeholder, how many were)`."""
    redacted, counts = _load().redact_text(text)
    return redacted, sum(counts.values())


def redacted(text: str) -> str:
    """`redact`'s text alone, for a caller that does not count."""
    return redact(text)[0]


def one_line(text: str, limit: int) -> str:
    """Text fit to show on one line: redacted first (a cut never halves a secret), whitespace collapsed, head-capped
    at `limit` characters with an ellipsis where the cut falls."""
    line = " ".join(redacted(str(text)).split())
    return line if len(line) <= limit else line[:limit - 1] + "…"


def detect(text: str) -> tuple[str, dict[str, int]]:
    """`(redacted text, {pattern name: matches})` — the shared pass's own answer, for a caller that must say WHICH
    secret shape it saw (`governance.guard.kinds.CredentialDetector`)."""
    return _load().redact_text(text)


def redact_deep(value: Any) -> Any:
    """`value` with every string in it (down through dicts and lists) passed through `redact`; keys and non-strings
    kept."""
    if isinstance(value, str):
        return redact(value)[0]
    if isinstance(value, dict):
        return {k: redact_deep(v) for k, v in value.items()}
    if isinstance(value, list):
        return [redact_deep(v) for v in value]
    return value
