"""The plain text every notification leg shares: the one-line task headline (`build_task_line`) and the
field rules under it — fold, inert, clip (`_fit`, `_clip`), "renders as something" (`_has_glyph`), the
`<url|label>` grammar guard (`link_url`) — plus `NO_UNFURL`, the request fact every Slack-shaped post carries.

Out of `app/slack_blocks.py` so a leg that is not Slack (email, Telegram, the operator webhook, the answer
gate in `routes/tasks/answers.py`) reads them in an edition that ships no Slack; `slack_blocks` imports them
back, so each still has one home. Stdlib-only and imports nothing from `app.*`, like `slack_blocks`
(hermetic `--noconftest` suites import both).
"""
from __future__ import annotations

import unicodedata
from types import MappingProxyType
from typing import Any, Callable, Mapping

# The single home of "never ask Slack to unfurl a link", read by `app/slack_egress.py` and `app/alert_relay.py`
# (the operator webhook is often Slack-shaped; other receivers ignore the fields). Every link we post points at
# our own console, so the preview is identical noise. `unfurl_media` is a separate switch for media URLs.
# Applied to `chat.postMessage` only: `chat.update` documents neither argument. Read-only so a stray write
# cannot flip every egress leg at once.
NO_UNFURL: Mapping[str, bool] = MappingProxyType({"unfurl_links": False, "unfurl_media": False})

# Ours: the done card is one scannable line; matches `_HEADER_MAX`.
_DONE_TITLE_MAX = 150

_ELLIPSIS = "…"


# Not in an invisible Unicode category yet render as nothing; a category test cannot catch them.
_BLANK_GLYPHS = frozenset(
    "\u2800\u3164\u115f\u1160\u17b4\u17b5\uffa0\u180e\u200b\u2060\ufeff"
)


def _has_glyph(text: str) -> bool:
    """Does `text` render as anything a human can see and aim at?

    `.strip()` is not enough: ZWSP, `U+FEFF`, braille blank etc. survive it and would make an invisible
    button whose `value` a misclick records. `Z*`/`C*`/`M*` categories plus `_BLANK_GLYPHS`.
    """
    return any(
        not unicodedata.category(ch).startswith(("Z", "C", "M")) and ch not in _BLANK_GLYPHS
        for ch in text
    )


def _clip(text: str, limit: int, marker: str = _ELLIPSIS) -> str:
    """`text` at most `limit` characters, with `marker` visible when anything was dropped.

    The marker is charged against the limit. A `limit` shorter than the marker is a hard cut, never a raise
    on the alert path.
    """
    if len(text) <= limit:
        return text
    room = limit - len(marker)
    return text[:limit] if room <= 0 else text[:room] + marker


def link_url(url: Any) -> str | None:
    """`url` back if it is safe to drop into Slack's `<url|label>` grammar, `None` otherwise.

    Public: the single home of this grammar, also used by `alert_relay._brief_text`. A grammar guard, not
    a URL validator: `>` and `|` are unescapable in the token, so an unguarded url forges a link and label
    (`<` opens a new element). Whitespace and `C*` characters (bidi overrides included) are refused. Scheme
    allow-list is `https://`/`http://`; deliberately no host check, since the console's `frontend_url`
    differs per environment and is `http` locally.
    """
    if not isinstance(url, str):
        return None
    if any(ch.isspace() or unicodedata.category(ch).startswith("C") for ch in url):
        return None
    if any(ch in url for ch in "<>|"):
        return None
    return url if url.startswith(("https://", "http://")) else None


# Ours: a project name is one short token; `projects.name` has no bound of its own.
_PROJECT_MAX = 40

# An SDK agent's display name: the manifest caps it at 60 (`agents.DISPLAY_NAME_MAX`).
AGENT_NAME_MAX = 60

# Ours: an org's display name is short; `orgs.name` has no bound of its own. Room for a readable name, never
# a fragment — the org is how a person in several orgs tells whose message this is.
ORG_MAX = 60

# Ours: a name is one token; the pair it derives from (`identities.person_label`) has no bound of its own.
_ASSIGNED_BY_MAX = 60

# Halving passes `_fit` may take; clears ordinary neutralizer growth in one or two. Safety does not
# rest on it: exhausting the passes returns `""`, never an un-neutralized value.
_FIT_MAX_PASSES = 8


def build_task_line(
    *,
    project: str | None,
    task_id: int,
    task_title: str,
    status: str,
    url: str | None,
    inert: Callable[[str], str],
    inert_name: Callable[[str], str] | None = None,
    actor: str | None = None,
    org: str | None = None,
    agent: str | None = None,
) -> str:
    """The one home of what a task alert says:

        {org} · {project}|{task id} {task title}: {status}  →  Acme · jaah-sell|3647 Market research: Shipped

    Every surface renders this (flat webhook text, Slack flat text, brief line, both cards); card furniture
    wraps it. `url` is linked on the identifier only if `link_url` accepts it; plain callers pass `None`.

    Two injected neutralizers, each applied to its own field only, neither ever the identity: `inert` for
    the title (Slack: `alert_relay.inert_mrkdwn`), `inert_name` for the project (Slack: the escape
    alone, since the URL sanitizer mangles names like `jaah.io`; `verbatim` on the cards closes the
    residual); `org` (`app/org_names.py`) takes `inert_name` too, and leads so a person in several orgs reads
    whose message it is first. `agent` (an SDK agent's display name, tenant-authored) takes `inert` and trails
    the status as `· via {agent}`, naming which engine did the work. `|` is stripped from the name, since it
    is this line's delimiter. Each field is folded,
    inerted, clipped and re-inerted (`_fit`). Degradation is per-field: a field that renders as nothing
    (`_has_glyph`) drops its separator with it.
    """
    link = link_url(url)
    lead = f"<{link}|{task_id}>" if link else str(task_id)

    # `|` comes out of the raw name before any neutralizer: stripping after one could re-form what it
    # took apart. Only the name precedes the identifier, so only it needs this.
    name = _fit((project or "").replace("|", ""), _PROJECT_MAX, inert_name or inert)
    head = f"{name}|{lead}" if _has_glyph(name) else lead

    org_text = _fit((org or "").replace("|", ""), ORG_MAX, inert_name or inert)
    if _has_glyph(org_text):
        head = f"{org_text} · {head}"

    title = _fit(task_title, _DONE_TITLE_MAX, inert)
    subject = f"{head} {title}" if _has_glyph(title) else head
    # `actor` names who did it (`Robert mentioned you`). Operator-edited, so the title's full `inert`
    # and `_ASSIGNED_BY_MAX`. The status word's capital then goes.
    who = _fit(actor or "", _ASSIGNED_BY_MAX, inert)
    if _has_glyph(who) and status:
        status = f"{who} {status[:1].lower()}{status[1:]}"
    # Last arm: an empty status drops its `: ` too.
    line = f"{subject}: {status}" if _has_glyph(status) else subject
    engine = _fit(agent or "", AGENT_NAME_MAX, inert)
    return f"{line} · via {engine}" if _has_glyph(engine) else line


def visible(text: str) -> str:
    """`text` on one line with every `C*` character dropped. Fold, drop, fold again: stripping `Cc` first
    would weld words across a newline; what remains after the fold is `Cf` (bidi overrides, zero-width),
    which `str.split()` cannot see and which would reorder the line."""
    kept = "".join(ch for ch in " ".join(text.split()) if not unicodedata.category(ch).startswith("C"))
    return " ".join(kept.split())


def _fit(value: Any, limit: int, inert: Callable[[str], str]) -> str:
    """One untrusted field, made safe and bounded: fold to one line, inert, clip, re-inert after a cut.

    `inert` is always the last operation. A cut can manufacture a hostile token (`evil.toml` → `evil.to`,
    a real ccTLD), and `inert` is not idempotent (`&` → `&amp;` grows each pass), so iterating on inerted
    output never settles. Instead the raw value is cut, halving the budget until its inerted form fits;
    the marker is ours and appended after. Exhausting `_FIT_MAX_PASSES` returns `""`. The one exemption:
    a `limit` shorter than the marker hard-cuts via `_clip` (unreachable from our callers).
    """
    # `visible`: a `Cf` would reorder a line this field now leads.
    folded = visible(str(value or ""))
    # An absent field never reaches `inert`: the injected neutralizer need not map `""` to `""`.
    if not folded:
        return ""
    inerted = inert(folded)
    if len(inerted) <= limit:
        # Nothing was cut, so no cut is unsafe.
        return inerted
    room = limit - len(_ELLIPSIS)
    if room <= 0:
        # A limit shorter than the marker: `_clip`'s hard cut.
        return _clip(inerted, limit)
    # Cut the raw value so `inert` runs after it; halving terminates.
    budget = room
    for _ in range(_FIT_MAX_PASSES):
        cut = inert(folded[:budget])
        if len(cut) + len(_ELLIPSIS) <= limit:
            return cut + _ELLIPSIS
        budget //= 2
        if budget == 0:
            break
    # A pathological `inert`: drop the field rather than return an un-inerted value.
    return ""
