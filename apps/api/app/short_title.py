"""Short-title generation — the one home for the cap on every auto-generated Task title.

Consumers: ``session_summary.title_from_description`` (LLM reply and no-key fallback alike, so the cap
cannot live in the prompt alone), ``routes/tasks/epics.py:append_tasks`` (normalized at the writer), and
the plan persona prompt (backstopped here). A runner's filed title is authored, not generated: it gets
``authored_title`` (sanitize only), never the cap.

"5-6 words" means ≤6 content words with the function words between them preserved, plus a 9-token /
80-char backstop — not a literal 6-token ceiling, which shreds readable phrases. A ceiling, not a quota.

``prose=True`` (a description body) runs segment split, lead-in drop and clause cut; ``prose=False``
(an authored title) runs only sanitize, compaction and clip, since the body steps misfire on it.

A body that is only a link, or a link plus a verb ("Fix https://…/tasks/42?tab=activity"), is titled from the
link's target ("Fix task 42 Activity"): ``link_only_title``. Sanitize strips every URL, so without it such a
body has nothing left to name.

An operator's leading tag ("K10 …", "[ABC-123] …") is kept in the title — left where it stands, else prepended:
``keep_leading_tag``, applied to the heuristic here and to the LLM reply in ``session_summary.title_from_description``.
"""
from __future__ import annotations

import re
from urllib.parse import parse_qs, unquote, urlsplit

# Content words carry the "5-6 words"; the token and char caps are backstops. 80 chars sits well above
# the longest real title, so it only bounds a pathological token.
MAX_CONTENT_WORDS = 6
MAX_TITLE_TOKENS = 9
MAX_TITLE_CHARS = 80

_UNTITLED = "Untitled task"

_MARKDOWN_IMAGE_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)")   # ![alt](target) incl. attachment:/URL targets
_MARKDOWN_LINK_RE = re.compile(r"\[([^\]]*)\]\([^)]*\)")   # [text](target) -> keep the link text
_ATTACHMENT_TOKEN_RE = re.compile(r"attachment:\S*")
_URL_RE = re.compile(r"https?://\S+")
# A CommonMark backslash escape (`\` before ASCII punctuation): the web editor serializes a typed
# `[ops] snake_case` as `\[ops\] snake\_case`, so the escape is undone before anything else reads the text.
_MD_ESCAPE_RE = re.compile(r"\\([!-/:-@\[-`{-~])")
# Markdown syntax, stripped only where it acts as syntax: every backtick, a heading's `#` run and a quote's
# `>` run at a line start, and an emphasis `*`/`_` run unless it sits inside a word (`snake_case` stays).
_MD_CODE_RE = re.compile(r"`+")
_MD_HEADING_RE = re.compile(r"^[ \t]*#{1,6}(?=\s|$)", re.MULTILINE)
_MD_QUOTE_RE = re.compile(r"^[ \t]*(?:>[ \t]?)+", re.MULTILINE)
_MD_EMPHASIS_RE = re.compile(r"(?<![^\W_])[*_]+|[*_]+(?![^\W_])")

# Sentence/clause terminators a body is split on (step 2).
# A terminator ends a segment only before whitespace or the end, so a filename (`CLAUDE.md`), a version
# (`v1.2`) or a URL scheme stays whole.
_SEGMENT_SPLIT_RE = re.compile(r"[.:;!?]+(?=\s|$)|\n+")

# Bullet/list/quote leaders on a segment ("- Save space", "1) Fix the header").
_LEADER_RE = re.compile(r"^(?:[\s\-–—*•+>]+|\d+[.)]\s*)+")

# A content-free lead-in a screenshot-first body opens with ("See screenshot.", "as per the image below").
_LEAD_IN_RE = re.compile(
    r"^(?:see|check|look\s+at|as\s+(?:per|shown|discussed))\b.{0,40}?"
    r"(?:screenshots?|images?|attached|attachments?|pictures?|photos?|below|mock\s?ups?)\b",
    re.IGNORECASE,
)

# Clause boundaries a first segment is cut at (step 4), longest-first so " — " wins over a bare "-".
_CLAUSE_BOUNDARIES = (",", " — ", " – ", " / ", " (")

# An operator's leading tag: a key/ticket code opening the body ("K10", "ABC-123", "[K10]", "K10:"). Upper-case
# letters, an optional hyphen, digits, optionally bracketed; it must end at whitespace, ":" or the end, so
# "K10x", "Add K10" or "Fix" is none. Every auto title keeps it (`keep_leading_tag`).
_LEADING_TAG_RE = re.compile(r"(\[[A-Z]{1,10}-?\d{1,6}\]|[A-Z]{1,10}-?\d{1,6})(?=[\s:]|$)")

# A link-only body: at most this many words survive once its URLs are stripped ("Fix", "Look at this").
MAX_LINK_ONLY_WORDS = 3
# Query keys whose value names the view a link opens (`?tab=activity`).
_LINK_VIEW_KEYS = ("tab", "view", "section")
# A CommonMark autolink (`<https://…>`): its brackets go with the URL.
_AUTOLINK_RE = re.compile(r"<(https?://[^\s>]+)>")
# Characters a URL picks up from the prose or markup around it.
_URL_EDGE = ".,;:!?)]}>\"'"

_LETTER_RE = re.compile(r"[^\W\d_]", re.UNICODE)
_EDGE_PUNCT = " \t,.;:!?-–—/\\|"

# Function words: preserved between kept content words but never consuming a content slot, and never
# ending a truncated title. `one` is a numeral-determiner here; a digit stays content.
_STOPWORDS = frozenset(
    """
    a an the
    and or but nor so yet if then than as that which while when where because though although
    of in on at to for with from by into onto over under above below about after before between during
    per via up down out off across through within without upon near against along around
    is are was were be been being am do does did done have has had
    will would can could should shall may might must
    it its this these those there their our your my his her him them they we you i he she
    one
    not no
    """.split()
)


def sanitize_title(text: str) -> str:
    """Undo Markdown escapes, strip markdown/image/attachment/URL syntax and re-collapse whitespace. A bare
    `[tag]` keeps its brackets (only `[text](url)` becomes `text`), and an in-word `_` stays.

    Sanitize before clipping, never the reverse (clipping first can strand a ``![name](attachment:...``
    stub). Applied to LLM output too, since a model can echo raw tokens."""
    text = _MD_ESCAPE_RE.sub(r"\1", text or "")
    text = _MARKDOWN_IMAGE_RE.sub(" ", text)
    text = _ATTACHMENT_TOKEN_RE.sub(" ", text)
    # The link before the bare-URL strip, which would otherwise eat an `http` target's `)` with it.
    text = _MARKDOWN_LINK_RE.sub(r"\1", text)
    text = _URL_RE.sub(" ", text)
    text = _MD_CODE_RE.sub("", text)
    text = _MD_HEADING_RE.sub(" ", text)
    text = _MD_QUOTE_RE.sub(" ", text)
    text = _MD_EMPHASIS_RE.sub("", text)
    return " ".join(text.split())


def authored_title(text: str) -> str:
    """A title a filer wrote, kept whole: sanitized (`sanitize_title`) but never compacted or clipped — the
    caller bounds its length at intake. `"Untitled task"` when nothing survives. A runner's `task_create`
    title is the filer's own words; cutting it to the auto-title cap dropped half its meaning."""
    return sanitize_title(text) or _UNTITLED


def leading_tag(text: str) -> str | None:
    """The tag `text` opens with (`_LEADING_TAG_RE`), as written less a trailing ":", or None."""
    match = _LEADING_TAG_RE.match(sanitize_title(text))
    return match.group(1) if match else None


def keep_leading_tag(title: str, source: str) -> str:
    """`title`, carrying `source`'s leading tag: unchanged when the tag already appears anywhere in it as a
    whole word (bracketed or bare) — never moved, since "S3"/"EC2"/"V2" is often a plain word in an ordinary
    title — else prepended, the rest clipped to the char cap. A model told to write a title drops "K10" as
    noise; the operator filed it to find the task by."""
    tag = leading_tag(source)
    if tag is None:
        return title
    if re.search(rf"(?<![\w-]){re.escape(tag.strip('[]'))}(?![\w-])", title):
        return title
    rest = title.strip(_EDGE_PUNCT)
    return _clip_chars(f"{tag} {rest}" if rest else tag)


def link_target(url: str) -> str:
    """Name what `url` opens in words: the last path segments (a hash route's when it has one), a
    `<plural>/<id>` pair read as its singular noun and the id (`/tasks/42` -> "task 42"), plus a view the
    query names (`?tab=activity` -> "Activity"). The host alone when the path is empty; "" when the URL does
    not parse (a malformed IPv6 host)."""
    try:
        parts = urlsplit(url.strip(_URL_EDGE + "<"))
        hostname = parts.hostname
    except ValueError:
        return ""
    path = parts.fragment if parts.fragment.startswith("/") else parts.path
    words: list[str] = []
    for segment in path.split("/"):
        word = re.sub(r"[-_+\s]+|\.\w{1,5}$", " ", unquote(segment)).strip()
        if not word:
            continue
        if word.isdigit() and words and not words[-1][-1].isdigit():
            noun = words[-1]
            singular = noun[:-1] if len(noun) > 3 and noun.endswith("s") and not noun.endswith("ss") else noun
            words[-1] = f"{singular} {word}"
        else:
            words.append(word)
    named = words[-2:]
    if not named and hostname:
        named = [hostname.removeprefix("www.")]
    query = parse_qs(parts.query)
    views = [query[key][0] for key in _LINK_VIEW_KEYS if query.get(key)]
    named += [re.sub(r"[-_]+", " ", view).capitalize() for view in views[:1]]
    return " ".join(named)


def link_only_title(text: str) -> str | None:
    """The title of a body that is only a link, or a link plus a few words — its words, then the first link's
    target (`link_target`) — or None for any other body. A link written as `[text](url)` names itself and is
    not one."""
    text = _MARKDOWN_IMAGE_RE.sub(" ", _AUTOLINK_RE.sub(r"\1", _MD_ESCAPE_RE.sub(r"\1", text or "")))
    text = _MARKDOWN_LINK_RE.sub(r"\1", text)
    urls = _URL_RE.findall(text)
    remainder = sanitize_title(text)
    if not urls or len(remainder.split()) > MAX_LINK_ONLY_WORDS:
        return None
    target = link_target(urls[0])
    if not target:
        return None
    title = _compact(f"{remainder.strip(_EDGE_PUNCT)} {target}".strip())
    return title[:1].upper() + title[1:] if title else None


def _bears_alnum(token: str) -> bool:
    """Does this token carry any letter or digit, i.e. is it a word rather than a bare separator?"""
    return any(ch.isalnum() for ch in token.strip(_EDGE_PUNCT + "\"'()[]{}"))


def _is_content_word(token: str) -> bool:
    """A token that spends one of the `MAX_CONTENT_WORDS` slots: alphanumeric-bearing and not a function
    word. A punctuation-only token ("/", "-") is never a content word, so it can never end a title."""
    bare = token.strip(_EDGE_PUNCT + "\"'()[]{}")
    if not _bears_alnum(token):
        return False
    return bare.lower() not in _STOPWORDS


def _compact(text: str) -> str:
    """Keep the first `MAX_CONTENT_WORDS` content words (function words between them preserved), then
    apply the token and char backstops. Returns `text` untouched when nothing had to be dropped."""
    tokens = text.split()
    kept: list[str] = []
    content = 0
    for token in tokens:
        if _is_content_word(token):
            if content >= MAX_CONTENT_WORDS:
                break
            content += 1
        kept.append(token)
    if len(kept) == len(tokens) and len(tokens) <= MAX_TITLE_TOKENS and len(text) <= MAX_TITLE_CHARS:
        # Nothing was dropped, so the last token is the author's and must not be trimmed — provided it
        # bears a letter/digit (a function word may legitimately end a phrase: `Fix everything`) and the
        # text holds at least one content word (a pure function-word label must fall through so
        # `short_title`'s stitch does not open a title with it).
        if not tokens or (content and _bears_alnum(tokens[-1])):
            return text
    del kept[MAX_TITLE_TOKENS:]
    while kept and not _is_content_word(kept[-1]):   # never end on a stopword or a dangling separator
        kept.pop()
    return _clip_chars(" ".join(kept).strip(_EDGE_PUNCT))


def _fits_cap(text: str) -> bool:
    """True when `text` is already within all three caps, so no rule needs to shorten it."""
    tokens = text.split()
    return (
        len(tokens) <= MAX_TITLE_TOKENS
        and len(text) <= MAX_TITLE_CHARS
        and sum(1 for token in tokens if _is_content_word(token)) <= MAX_CONTENT_WORDS
    )


def _clip_chars(text: str) -> str:
    """Final char backstop, breaking on a word boundary and marking the elision."""
    if len(text) <= MAX_TITLE_CHARS:
        return text
    head = text[: MAX_TITLE_CHARS - 1]
    if " " in head:
        head = head.rsplit(" ", 1)[0]
    return head.rstrip(_EDGE_PUNCT) + "…"


def _segments(text: str) -> list[str]:
    """Split a body into candidate title segments (step 2): terminator split, leaders stripped, letterless
    segments dropped; falls back to the whole text when the filter would empty the list."""
    segs = []
    for raw in _SEGMENT_SPLIT_RE.split(text):
        seg = _LEADER_RE.sub("", raw).strip()
        if seg and _LETTER_RE.search(seg):
            segs.append(seg)
    return segs or ([text] if text else [])


def _cut_at_clause(segment: str) -> str:
    """Cut a segment at its first clause boundary (step 4) — a fallback for a segment too long to keep.

    Skipped when the segment already fits the cap, holds two or more commas (an enumeration), or the
    cut would leave fewer than two words."""
    if _fits_cap(segment) or segment.count(",") >= 2:
        return segment
    cut = min(
        (segment.index(sep) for sep in _CLAUSE_BOUNDARIES if sep in segment),
        default=-1,
    )
    if cut < 0:
        return segment
    head = segment[:cut].strip()
    return head if len(head.split()) >= 2 else segment


def short_title(text: str, *, prose: bool = True) -> str:
    """Derive a ≤6-content-word title from `text`; `"Untitled task"` when nothing survives.

    `prose=True` titles a link-only body from its link (`link_only_title`); otherwise it runs sanitize → segment → drop a screenshot lead-in → clause cut when over the cap →
    compact → stitch a bare label onto the next segment → clip. `prose=False` runs only sanitize →
    compact → clip."""
    linked = link_only_title(text) if prose else None
    if linked:
        return keep_leading_tag(linked, text)
    if not prose:
        return _compact(sanitize_title(text)) or _UNTITLED
    # Per line, so the line breaks survive for `_segments`: sanitizing the whole body joined a paragraph's
    # last word to the next one's first ("Add a note to the README Add").
    # A markdown hard break (`\` + newline) is a line break too.
    lines = (text or "").replace("\\\n", "\n").splitlines()
    text = "\n".join(line for line in map(sanitize_title, lines) if line)
    if not text:
        return _UNTITLED

    segs = _segments(text)
    # Drop leading lead-in segments, but never all of them.
    surviving = segs
    while surviving and _LEAD_IN_RE.match(surviving[0]):
        surviving = surviving[1:]
    segs = surviving or segs
    if not segs:
        return _UNTITLED

    title = _compact(_cut_at_clause(segs[0]))
    # A bare leading label ("Projects") is stitched onto the next segment and re-compacted, so the cap
    # counts it too. A label that compacted away contributes no prefix and no dangling ": ".
    if len(title.split()) < 2 and len(segs) > 1:
        next_segment = _cut_at_clause(segs[1])
        # A label that already ends a clause ("Hi,") joins with a space, never a second separator.
        joint = " " if title.endswith(",") else ": "
        stitched = _compact(f"{title}{joint}{next_segment}" if title else next_segment)
        if len(stitched.split()) > len(title.split()):
            title = stitched
    return keep_leading_tag(title, text) if title else _UNTITLED
