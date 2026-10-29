"""The public legal pages: terms, privacy notice, security page, subprocessors, DPA, BAA, trust center and NDA,
served at /legal/<page>.

THE TEXT IS SINGLE-HOMED in the compliance docs' public pages, beside the policies whose numbers it quotes, and
ships in the api image (its Dockerfile copies it to /app/doctrine/legal; compose mounts it there).
Nothing here restates it. Each page is read, its internal drafting blocks dropped, parsed into a small block
tree the console renders as React elements (never raw HTML), and filled with the deployment's own values.

THE VALUES ARE OPERATOR SETTINGS (Settings > Legal, `app/operator_settings/`), never committed: the legal
entity, its address, the contacts, the governing law, plus `legal_version`, served as `last_updated` and
shown by the client above each page. A page
writes one as `{{token}}`; `TOKENS` maps each to its knob.

NO HARVESTABLE CONTACT DETAIL is served — not in the JSON, the HTML or a static DOM scrape (a headless
browser that runs the console and reads what it renders still can; that is obfuscation's limit): the entity, its address and the contacts (`_MASKED`) leave as a
`masked` run — XOR-scrambled hex, never their text and never a `mailto:` href — and only the console's
renderer turns one back into visible text (with decoys a scraper reads but a reader never sees) and an email
link (built when clicked). A page served as JSON, HTML or a raw DOM dump therefore carries none of them.

LIVE means the `legal_pages_published` switch is on, every value is set (`missing`) and no page still
carries text left for counsel (`unfinished`: a `[bracketed]` passage outside a link). Until then the public
route answers 404 and only an operator's preview shows the pages, marked as drafts, naming what is left.

THE MARKDOWN SUBSET is what those files use and nothing more: `#`/`##`/`###` headings, paragraphs,
`-` and `1.` lists, `>` quotes, pipe tables, and inline `**bold**`, `*italic*`, `` `code` `` and
`[text](target)` links. A link target is another page (`terms.md` → `/legal/terms`) or `https:`; anything
else — a literal `mailto:` included, since a contact is written as its `{{token}}` — is refused at parse, so a link back into the repo can never reach a published page.
`<!-- internal -->` … `<!-- /internal -->` wraps what is never published.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Optional

#: (slug, file) in navigation order. The slug is the public path segment and the link target's stem.
PAGES: tuple[tuple[str, str], ...] = (
    ("terms", "terms.md"),
    ("privacy", "privacy.md"),
    ("security", "security.md"),
    ("subprocessors", "subprocessors.md"),
    ("dpa", "dpa.md"),
    ("baa", "baa.md"),
    ("trust", "trust.md"),
    ("nda", "nda.md"),
)
#: The navigation's short name for each page; its title is the page's own `# ` heading.
NAV_LABELS: dict[str, str] = {
    "terms": "Terms",
    "privacy": "Privacy",
    "security": "Security",
    "subprocessors": "Subprocessors",
    "dpa": "DPA",
    "baa": "BAA",
    "trust": "Trust",
    "nda": "NDA",
}
SLUGS = frozenset(slug for slug, _ in PAGES)

#: token → (operator-settings key, its label for the "fill these in" list). Contacts render as email links.
TOKENS: dict[str, tuple[str, str]] = {
    "entity_name": ("legal_entity_name", "Legal entity"),
    "entity_address": ("legal_entity_address", "Registered address"),
    "privacy_contact": ("legal_privacy_contact", "Privacy contact"),
    "security_contact": ("legal_security_contact", "Security contact"),
    "governing_law": ("legal_governing_law", "Governing law"),
    "last_updated": ("legal_version", "Legal version"),
}
_CONTACTS = frozenset({"privacy_contact", "security_contact"})
#: The values a harvester must not read off a page: who we are, where, and how to write to us.
_MASKED = frozenset({"entity_name", "entity_address"}) | _CONTACTS
PUBLISHED_KEY = "legal_pages_published"

_INTERNAL = re.compile(r"<!-- internal -->.*?<!-- /internal -->\n?", re.S)
_TOKEN = re.compile(r"\{\{([a-z_]+)\}\}")
_INLINE = re.compile(r"`([^`]+)`|\*\*(.+?)\*\*|\*(.+?)\*|\[([^\]]+)\]\(([^)\s]+)\)")
_TABLE_RULE = re.compile(r"^\|(\s*:?-+:?\s*\|)+$")
_ORDERED = re.compile(r"^\d+\.\s+")


class LegalSourceError(ValueError):
    """A page uses markdown outside the subset, an unknown token, or a link that may not be published."""


def source_dir() -> Path:
    """`LEGAL_PAGES_DIR`, else the image's copy. No guessed repo fallback: a missing page fails loudly."""
    explicit = os.environ.get("LEGAL_PAGES_DIR")
    if explicit:
        return Path(explicit)
    return Path(os.environ.get("DOCTRINE_ROOT") or "/app/doctrine") / "legal"


# ── parsing ─────────────────────────────────────────────────────────────────────────────────────────


def _href(target: str) -> str:
    if target.startswith("https://"):
        return target
    stem = target[:-3] if target.endswith(".md") else None
    if stem in SLUGS:
        return f"/legal/{stem}"
    raise LegalSourceError(f"link target {target!r} is not a legal page or https: (write a contact as its {{token}})")


def _fill(text: str, values: dict[str, str], style: dict[str, Any]) -> list[dict[str, Any]]:
    """Split plain text on `{{token}}`, replacing each with its value (or `[Label]` while unset)."""
    out: list[dict[str, Any]] = []
    pos = 0
    for match in _TOKEN.finditer(text):
        token = match.group(1)
        if token not in TOKENS:
            raise LegalSourceError(f"unknown token {{{{{token}}}}}")
        if match.start() > pos:
            out.append({"text": text[pos:match.start()], **style})
        value = values.get(token, "")
        node: dict[str, Any] = {"text": value or f"[{TOKENS[token][1]}]", **style}
        if value and token in _MASKED:
            node["text"] = ""
            node["masked"] = mask(value)
            if token in _CONTACTS and "href" not in style:
                node["mail"] = True
        out.append(node)
        pos = match.end()
    if pos < len(text):
        out.append({"text": text[pos:], **style})
    return out


def mask(value: str) -> str:
    """`value` as a scraper cannot pattern-match it: a fresh one-byte key, then each UTF-8 byte XOR that key,
    all as hex. The console legal page's `unmask` is the inverse."""
    key = 1 + secrets.randbelow(255)
    return f"{key:02x}" + bytes(b ^ key for b in value.encode("utf-8")).hex()


def unmask(masked: str) -> str:
    key = int(masked[:2], 16)
    return bytes(b ^ key for b in bytes.fromhex(masked[2:])).decode("utf-8")


def inlines(text: str, values: dict[str, str], style: Optional[dict[str, Any]] = None) -> list[dict[str, Any]]:
    """Inline markdown → `[{text, bold?, italic?, code?, href?}]`. Text is never markup: the client renders it."""
    style = style or {}
    out: list[dict[str, Any]] = []
    pos = 0
    for match in _INLINE.finditer(text):
        if match.start() > pos:
            out.extend(_fill(text[pos:match.start()], values, style))
        code, bold, italic, label, target = match.groups()
        if code is not None:
            out.extend(_fill(code, values, {**style, "code": True}))
        elif bold is not None:
            out.extend(inlines(bold, values, {**style, "bold": True}))
        elif italic is not None:
            out.extend(inlines(italic, values, {**style, "italic": True}))
        else:
            out.extend(inlines(label, values, {**style, "href": _href(target)}))
        pos = match.end()
    if pos < len(text):
        out.extend(_fill(text[pos:], values, style))
    return out


def _list_item(line: str, ordered: bool) -> Optional[str]:
    """The text after a list marker of the given kind, or None when `line` does not start such an item."""
    if ordered:
        match = _ORDERED.match(line)
        return line[match.end():] if match else None
    return line[2:] if line.startswith("- ") else None


def _cells(row: str) -> list[str]:
    return [cell.strip() for cell in row.strip().strip("|").split("|")]


@dataclass(frozen=True)
class Page:
    slug: str
    title: str
    blocks: list[dict[str, Any]]


def parse(slug: str, source: str, values: dict[str, str]) -> Page:
    """One page's markdown → its title and block tree. Raises `LegalSourceError` outside the subset."""
    body = _INTERNAL.sub("", source)
    if "<!--" in body:
        raise LegalSourceError(f"{slug}: an HTML comment outside an internal block would be published")
    lines = body.splitlines()
    title = ""
    blocks: list[dict[str, Any]] = []
    i = 0

    def starts_block(line: str) -> bool:
        return line.startswith(("#", ">", "|", "- ")) or bool(_ORDERED.match(line))

    while i < len(lines):
        line = lines[i]
        if not line.strip():
            i += 1
        elif line.startswith("# "):
            if title:
                raise LegalSourceError(f"{slug}: more than one title")
            title = line[2:].strip()
            i += 1
        elif line.startswith("#") and not line.startswith(("## ", "### ")):
            raise LegalSourceError(f"{slug}: {line[:40]!r} is not a `#`, `##` or `###` heading")
        elif line.startswith(("## ", "### ")):
            level = 2 if line.startswith("## ") else 3
            blocks.append({"type": "heading", "level": level, "text": inlines(line[level + 1:].strip(), values)})
            i += 1
        elif line.startswith(">"):
            parts = []
            while i < len(lines) and lines[i].startswith(">"):
                parts.append(lines[i][1:].strip())
                i += 1
            blocks.append({"type": "quote", "text": inlines(" ".join(parts), values)})
        elif line.startswith("|"):
            rows = []
            while i < len(lines) and lines[i].startswith("|"):
                rows.append(lines[i])
                i += 1
            if len(rows) < 2 or not _TABLE_RULE.match(rows[1].replace(" ", "")):
                raise LegalSourceError(f"{slug}: a table needs a header row and a rule row")
            header = _cells(rows[0])
            body = [_cells(row) for row in rows[2:]]
            if any(len(row) != len(header) for row in body):
                raise LegalSourceError(f"{slug}: a table row's cell count differs from its header's")
            blocks.append({
                "type": "table",
                "header": [inlines(cell, values) for cell in header],
                "rows": [[inlines(cell, values) for cell in row] for row in body],
            })
        elif line.startswith("- ") or _ORDERED.match(line):
            ordered = not line.startswith("- ")
            items: list[str] = []
            while i < len(lines):
                current = lines[i]
                item = _list_item(current, ordered)
                if item is not None:
                    items.append(item)
                elif items and current.startswith("  ") and current.strip():
                    items[-1] += " " + current.strip()
                else:
                    break
                i += 1
            blocks.append({"type": "list", "ordered": ordered, "items": [inlines(it.strip(), values) for it in items]})
        else:
            parts = []
            while i < len(lines) and lines[i].strip() and not starts_block(lines[i]):
                parts.append(lines[i].strip())
                i += 1
            blocks.append({"type": "paragraph", "text": inlines(" ".join(parts), values)})
    if not title:
        raise LegalSourceError(f"{slug}: no `# ` title")
    return Page(slug=slug, title=title, blocks=blocks)


# ── settings, readiness and serving ─────────────────────────────────────────────────────────────────


def current_values() -> dict[str, str]:
    from app.operator_settings import reads as settings_reads

    return {token: settings_reads.get_str(key).strip() for token, (key, _label) in TOKENS.items()}


def missing(values: dict[str, str]) -> list[str]:
    """The labels of every value still unset, in `TOKENS` order."""
    return [label for token, (_key, label) in TOKENS.items() if not values.get(token)]


_BRACKETED = re.compile(r"\[([^\]]+)\]")


def unfinished(values: dict[str, str]) -> list[str]:
    """Every `[bracketed]` passage still on a page — counsel's drafting placeholders — as `Page: text`.
    An unset value renders as `[Label]` too; those are `missing`'s, so they are skipped here."""
    labels = {f"[{label}]" for _key, label in TOKENS.values()}
    found: list[str] = []
    for slug, _ in PAGES:
        page = load(slug, values)
        for block in page.blocks:
            for text in _block_texts(block):
                for match in _BRACKETED.finditer(text):
                    if match.group(0) not in labels:
                        found.append(f"{NAV_LABELS[slug]}: {match.group(1)}")
    return found


def _block_texts(block: dict[str, Any]) -> list[str]:
    """A block's plain text, one string per paragraph, heading, list item or table cell."""
    if block["type"] == "list":
        parts = block["items"]
    elif block["type"] == "table":
        parts = block["header"] + [cell for row in block["rows"] for cell in row]
    else:
        parts = [block["text"]]
    return ["".join(unmask(node["masked"]) if "masked" in node else node["text"] for node in part) for part in parts]


def is_published() -> bool:
    from app.operator_settings import reads as settings_reads

    return settings_reads.get_bool(PUBLISHED_KEY)


def load(slug: str, values: dict[str, str]) -> Page:
    filename = dict(PAGES)[slug]
    return parse(slug, (source_dir() / filename).read_text(encoding="utf-8"), values)


def navigation() -> list[dict[str, str]]:
    """Every page's slug and short name, in order. `title` is kept as the name the client renders."""
    return [{"slug": slug, "title": NAV_LABELS[slug]} for slug, _ in PAGES]


def render(slug: str, *, preview: bool) -> Optional[dict[str, Any]]:
    """The page's response body, or None when it is not to be served: an unknown slug, or (unless
    `preview`) pages that are not yet published or still miss a value."""
    if slug not in SLUGS:
        return None
    values = current_values()
    unset = missing(values)
    left = unfinished(values)
    live = is_published() and not unset and not left
    if not live and not preview:
        return None
    page = load(slug, values)
    return {
        "slug": page.slug,
        "title": page.title,
        "last_updated": values["last_updated"],
        "blocks": page.blocks,
        "pages": navigation(),
        "draft": not live,
        "missing": unset,
        "unfinished": left,
    }


# ── when each page's text last changed (Settings > Legal's "Last updated" warning) ─────────────────────

#: Beside the pages: `{slug: {"changed": "YYYY-MM-DD", "digest": <published_digest>}}`. Whoever edits a page's
#: published text records the date here; `tests/test_legal_pages.py` fails until the digest matches again.
CONTENT_DATES_FILE = "page-dates.json"


def published_digest(slug: str) -> str:
    """A short hash of the page's PUBLISHED source: its `<!-- internal -->` blocks dropped, so a drafting note
    is never a change the public sees."""
    filename = dict(PAGES)[slug]
    text = _INTERNAL.sub("", (source_dir() / filename).read_text(encoding="utf-8"))
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def content_dates() -> dict[str, dict[str, str]]:
    """`page-dates.json`, or nothing when the source directory carries none (then no page reads as changed)."""
    try:
        return json.loads((source_dir() / CONTENT_DATES_FILE).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}


def changed_since(last_updated: str) -> list[dict[str, str]]:
    """Every page whose text changed after `last_updated` (`legal_version`), in navigation order. Empty when
    that value is not a date: then there is nothing to compare against."""
    try:
        since = date.fromisoformat(last_updated.strip()[:10])
    except ValueError:
        return []
    dates = content_dates()
    return [
        {"slug": slug, "title": NAV_LABELS[slug], "changed": dates[slug]["changed"]}
        for slug, _ in PAGES
        if slug in dates and date.fromisoformat(dates[slug]["changed"]) > since
    ]


def is_live() -> bool:
    """Whether the public sees the pages: published, every value set, nothing left for counsel."""
    values = current_values()
    return is_published() and not missing(values) and not unfinished(values)


#: Where sign-up links while the pages are not live and no URL is set (owner decision 2026-10-06): the
#: marketing site's pages, as before /legal existed, so sign-up never links to a 404.
UNPUBLISHED_SIGNUP_LINKS = {"terms": "https://jaah.ai/terms", "privacy": "https://jaah.ai/privacy"}


def signup_link(slug: str) -> str:
    """The URL the sign-up flow links for `terms` or `privacy`: the operator's own setting; else, once the
    pages are live, this console's /legal page (`FRONTEND_URL`'s origin, so it follows the console to its
    next host); else `UNPUBLISHED_SIGNUP_LINKS`."""
    from app.operator_settings import reads as settings_reads
    from app.login_oauth import frontend_origin

    explicit = settings_reads.get_str(f"signup_{slug}_url").strip()
    if explicit:
        return explicit
    if is_live():
        return f"{frontend_origin()}/legal/{slug}"
    return UNPUBLISHED_SIGNUP_LINKS[slug]
