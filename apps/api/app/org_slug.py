"""An org's generated slug: a 5-7 character abbreviation of its name plus two random digits (`timesvr38`).

The abbreviation comes from the same LLM client as task titles (`app/llm_client.py::summary_llm`), with a
short inline timeout because sign-up waits on it; any failure, an absent key, or a reply outside the shape
falls back to `_fallback_abbreviation`, so a slug is always produced.

Used by the self-service sign-up (`app/signup.py`) and by the operator's Create dialog suggestion
(`GET /api/orgs/slug-suggestion`). The result is lowercase letters and digits only, so it satisfies both
the slug grammar (`app/routes/commands.py::_SLUG_RE`) and the tailnet tag grammar
(`app/cloud_node_launch.py::tailnet_tags_for`). Uniqueness is checked here; a concurrent create taking the
same slug is still `uq_orgs_slug`'s, answered by `app/org_create.py::create_org_row`.
"""
from __future__ import annotations

import logging
import re
import secrets
import string
import unicodedata

from sqlalchemy import select
from sqlalchemy.orm import Session

# Bound locally so tests patch `app.org_slug._summary_llm`, as `session_summary` does.
from app.llm_client import NO_THINKING_OPTIONS, summary_llm as _summary_llm, text_blocks
from app.models.orgs import Org

log = logging.getLogger(__name__)

#: `uq_orgs_slug`, the one constraint a create race or a slug rename can hit.
SLUG_UNIQUE = "uq_orgs_slug"

ABBREV_MIN = 5
ABBREV_MAX = 7
#: Sign-up blocks on the call, so it gets far less than the title budget.
LLM_TIMEOUT_SECS = 5.0

_PROMPT = (
    "Abbreviate the organization name below into a short handle of {lo} to {hi} characters, "
    "using only lowercase letters a-z and digits 0-9.\n"
    "Keep it recognisable: keep the leading letters and the distinctive consonants, drop vowels and "
    "spaces first (e.g. \"Northwind\" -> \"nrthwnd\", \"Acme Labs\" -> \"acmelbs\").\n"
    "Output ONLY the handle — no quotes, no punctuation, no explanation.\n\n"
    "NAME:\n{name}\n\nHANDLE:"
)
_NON_ALNUM = re.compile(r"[^a-z0-9]+")
_VOWELS = frozenset("aeiou")


def _ascii(name: str) -> str:
    """The name folded to lowercase ASCII letters and digits, everything else dropped."""
    folded = unicodedata.normalize("NFKD", name or "").encode("ascii", "ignore").decode().lower()
    return _NON_ALNUM.sub("", folded)


def _random_letters(n: int) -> str:
    return "".join(secrets.choice(string.ascii_lowercase) for _ in range(n))


def _fallback_abbreviation(name: str) -> str:
    """A deterministic abbreviation: the name's ASCII alphanumerics, vowels after the first character dropped
    from the end while it is longer than `ABBREV_MAX`, then cut to it. A name too short (or with no ASCII at all, all CJK say) is
    padded with random letters, so such orgs do not all queue behind one shared base."""
    chars = list(_ascii(name))
    # Vowels go from the end first, so the recognisable start survives: `northwind` -> `nrthwnd`.
    for i in range(len(chars) - 1, 0, -1):
        if len(chars) <= ABBREV_MAX:
            break
        if chars[i] in _VOWELS:
            del chars[i]
    text = "".join(chars)[:ABBREV_MAX]
    if len(text) < ABBREV_MIN:
        text += _random_letters(ABBREV_MIN - len(text))
    return text


def _shaped(reply: str) -> str:
    """The model's reply as an abbreviation, or "" when it is not one: first non-empty line, folded to
    lowercase alphanumerics, and between `ABBREV_MIN` and `ABBREV_MAX` long (a reply over the cap is cut,
    never trusted to the prompt alone)."""
    lines = [ln.strip() for ln in (reply or "").splitlines() if ln.strip()]
    if not lines:
        return ""
    text = _ascii(lines[0])[:ABBREV_MAX]
    return text if len(text) >= ABBREV_MIN else ""


def abbreviation(name: str) -> str:
    """The 5-7 character abbreviation of `name` — the LLM's when it answers in shape, else the fallback.
    Never raises."""
    fallback = _fallback_abbreviation(name)
    if not (name or "").strip():
        return fallback
    llm = _summary_llm(timeout=LLM_TIMEOUT_SECS)
    if llm is None:
        return fallback
    try:
        message = llm.client.messages.create(
            model=llm.model,
            # The summary model thinks; `NO_THINKING_OPTIONS` turns it off and keeps the cap for the reply
            # (measured).
            **(llm.request_options or NO_THINKING_OPTIONS),
            messages=[{"role": "user", "content": _PROMPT.format(lo=ABBREV_MIN, hi=ABBREV_MAX, name=name)}],
        )
        for text in text_blocks(message):
            shaped = _shaped(text)
            if shaped:
                return shaped
    except Exception as exc:  # noqa: BLE001 — any provider failure is the fallback, never a failed sign-up
        log.debug("org slug LLM call failed: %s", exc)
    return fallback


def _taken(session: Session, slug: str) -> bool:
    return session.scalar(select(Org.id).where(Org.slug == slug)) is not None


def unique_slug(session: Session, base: str) -> str:
    """`base` plus two random digits, the first free one in a random order over all 100. Should all 100 be
    taken, three digits, then the base re-rolled — the slug must be globally unique, and two digits only
    hold 100 orgs per abbreviation."""
    suffixes = [f"{n:02d}" for n in range(100)]
    secrets.SystemRandom().shuffle(suffixes)
    for suffix in suffixes:
        if not _taken(session, base + suffix):
            return base + suffix
    for _ in range(20):
        candidate = f"{base}{secrets.randbelow(1000):03d}"
        if not _taken(session, candidate):
            return candidate
    return unique_slug(session, base[: ABBREV_MIN - 1] + _random_letters(ABBREV_MAX - ABBREV_MIN + 1))


def generate_slug(session: Session, name: str) -> str:
    """A free generated slug for an org called `name`."""
    return unique_slug(session, abbreviation(name))
