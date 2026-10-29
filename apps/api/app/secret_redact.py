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


def detect(text: str) -> tuple[str, dict[str, int]]:
    """`(redacted text, {pattern name: matches})` — the shared pass's own answer, for a caller that must say WHICH
    secret shape it saw (`governance.guard.kinds.CredentialDetector`)."""
    return _load().redact_text(text)
