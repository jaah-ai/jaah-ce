"""Defanging caller-supplied text: the untrusted-data fence and conditional-marker openers."""
from __future__ import annotations

import re

# The single untrusted-data fence. ALL DB-sourced content (task body, prior-stage output,
# prior Q&A) lives between these two markers and nothing else does.
_FENCE_BEGIN = "--- BEGIN UNTRUSTED TASK DATA ---"
_FENCE_END = "--- END UNTRUSTED TASK DATA ---"

# Leading whitespace stripped before the fence-marker test; explicit escapes so the code points are
# auditable. Each renders as (or precedes) a fresh visual line to an LLM.
_FENCE_LEAD_WS = (
    " \t"                    # ASCII space, tab
    "\x0b\x0c"               # vertical tab, form feed
    "\r"                     # carriage return (lone CR)
    "\x1c\x1d\x1e"           # file / group / record separators
    "\x85"                   # NEL (next line)
    "\xa0"                   # NBSP
    "\u2028\u2029"           # line separator / paragraph separator
    "\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007\u2008\u2009\u200a"  # en/em/thin spaces
    "\u202f"                 # narrow no-break space
    "\u205f"                 # medium mathematical space
    "\u3000"                 # ideographic space
    "\ufeff"                 # zero-width no-break space / BOM
)


def defang_fence_markers(text: str) -> str:
    """Neutralize any untrusted line that could forge a fence/section marker.

    Applied to every content string interpolated inside the fence. `splitlines()` splits on the full
    Unicode line-boundary set, leading Unicode whitespace is stripped before the test, a leading
    `[-~]{3,}` run becomes `###` (not `~~~`, itself a fence marker), line endings are normalized, and
    marker phrases are neutralized anywhere, mid-line included."""
    out = []
    for line in text.splitlines():
        probe = line.lstrip(_FENCE_LEAD_WS)
        if probe.startswith("---") or probe.startswith("~~~"):
            line = re.sub(r"^([" + re.escape(_FENCE_LEAD_WS) + r"]*)[-~]{3,}", r"\1###", line, count=1)
        out.append(line)
    joined = "\n".join(out)
    for phrase in ("END UNTRUSTED TASK DATA", "BEGIN UNTRUSTED TASK DATA"):
        joined = joined.replace(phrase, phrase.replace(" ", "_"))
    return joined


#: Any HTML comment opener, with no marker-keyword lookahead; `defang_conditional_markers` mangles it
#: wherever caller-supplied text enters the trusted region. Positional, not lexical: a keyword-matching
#: pattern let a value carry a keyword fragment (e.g. `branch_prefix` = `<!--i`) that adjacent prose
#: completed into a marker, raising on every compose. Doctrine files cannot be defanged and are guarded
#: by a repository test instead. Cost: a literal HTML comment in a title renders
#: mangled, which is harmless.
_COMMENT_OPENER_RE = re.compile(r"<!--")


def defang_conditional_markers(text: str) -> str:
    """Neutralize every HTML comment opener in caller-supplied text bound for the trusted region.

    The provider pass raises on any unresolvable marker, and DB free text (`title`, `issue_state`,
    `agent_instructions`) renders into that region, so an operator's typed
    `<!--if:...` would otherwise stall dispatch. Not applied inside the fence: the untrusted body is never
    fed to the pass and must stay byte-for-byte. Mangled rather than deleted, like `defang_fence_markers`.
    Where a placeholder substitution follows, defang after substituting (`_defanged_operator_text`,
    `apply_project_values`), or the substitution can assemble a marker this pass never saw; "contains"
    means after substitution.

    THE FIRST OF EXACTLY SIX deliberate ways a default-provider prompt's bytes can differ from the benign-value
    baseline; this is the enumerating site, so add a new cause here first (the test derives the count):
    this function, `_defanged_operator_text`'s leftover-token neuter, `apply_project_values`' single regex pass,
    `_PROJECT_INSTRUCTIONS_RENDERED_MAX_CHARS` truncation, `defang_fence_markers` over the project columns
    and trusted task values, and `_both()`'s per-value cap. Every other value is untouched."""
    return _COMMENT_OPENER_RE.sub("<!##", text)


def _safe_title(title: str) -> str:
    """Sanitize the DB `title` for the `Title:` line above the fence: untrusted, so collapse to one line,
    defang fence and conditional markers, and length-cap."""
    single = " ".join((title or "").splitlines()).strip()
    return defang_conditional_markers(defang_fence_markers(single))[:200]


def _safe_trusted_text(text: str | None, max_len: int = 4000) -> str:
    """Sanitize DB-sourced text for the trusted region above the fence (e.g. `issue_state`).

    May span lines (unlike `_safe_title`); gets fence- and conditional-marker defanging plus a length cap."""
    return defang_conditional_markers(defang_fence_markers((text or "").strip()))[:max_len]
