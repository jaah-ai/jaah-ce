"""Template passes over prompt text: project-value placeholders, conditional blocks and name respelling."""
from __future__ import annotations

import re
from collections.abc import Callable

from app import agent_providers
from app.dispatch_compose.fencing import (
    _FENCE_BEGIN,
    _FENCE_END,
    defang_conditional_markers,
    defang_fence_markers,
)
from app.dispatch_compose.role_tables import _GATE_ABSENT_REWRITES

#: This module's own placeholder tokens, as a narrow alternation (unrecognized braces in operator prose stay
#: verbatim). Case-sensitive. `_PLACEHOLDER_TOKEN_RE` captures the name for one-pass substitution;
#: `_PLACEHOLDER_OPENER_RE` matches only the brace so a leftover token can be neutered in place.
_PLACEHOLDER_NAMES = r"integration_branch|branch_prefix|gate_quick_cmd|gate_full_cmd"
_PLACEHOLDER_TOKEN_RE = re.compile(r"\{(" + _PLACEHOLDER_NAMES + r")\}")
_PLACEHOLDER_OPENER_RE = re.compile(r"\{(?=(?:" + _PLACEHOLDER_NAMES + r")\})")
#: What `apply_project_values` actually substitutes with: the same token plus an optional backtick on each
#: side, so a blank gate command's phrase can replace the code span whole rather than leave an empty pair.
_PLACEHOLDER_SUB_RE = re.compile(r"(`?)\{(" + _PLACEHOLDER_NAMES + r")\}(`?)")

#: What a blank gate command (the project declares no gate for that tier) renders as wherever
#: doctrine names `{gate_quick_cmd}` / `{gate_full_cmd}`. Constants, carrying no marker or token fragment.
#: A noun phrase, so it reads as the subject or object doctrine uses the command as ("`X` green").
NO_QUICK_GATE = "the project's quick gate (none declared — skip it)"
NO_FULL_GATE = "the project's full gate (none declared — skip it)"


def _rewrite_absent_gates(value: str) -> str:
    """`value` with each declared-gate fragment of `_GATE_ABSENT_REWRITES` swapped for its blank-gate twin.

    A non-string role value (a playbook role's flag) passes through unchanged."""
    if not isinstance(value, str):
        return value
    for declared, absent in _GATE_ABSENT_REWRITES:
        value = value.replace(declared, absent)
    return value


# ── Project-value placeholders ──────────────────────────────────────────────────────────────────────────

# The charter and `_ROLES` prose are templates over the four project columns a runner needs (branch,
# rebase, PR, gate), substituted by explicit named replace, never `str.format`/`Template`: the prose is
# full of legitimate braces (JSON examples, `${VAR}`, `{o}/{r}`) that would raise or be eaten.

# A closed set, so a test can assert no token survives a compose. Derived from `_PLACEHOLDER_NAMES`
# (its only reader is that guard), which is valid only while that regex is a bare alternation of
# literals; asserted at import.
assert re.fullmatch(r"[a-z_]+(?:\|[a-z_]+)*", _PLACEHOLDER_NAMES), (
    f"`_PLACEHOLDER_NAMES` is no longer a bare alternation of literal names ({_PLACEHOLDER_NAMES!r}) — "
    f"`_PROMPT_PLACEHOLDERS` derives itself by splitting it on `|` and would silently yield garbage"
)
_PROMPT_PLACEHOLDERS = tuple("{%s}" % name for name in _PLACEHOLDER_NAMES.split("|"))


def apply_project_values(
    text: str,
    *,
    integration_branch: str,
    branch_prefix: str,
    gate_quick_cmd: str,
    gate_full_cmd: str,
) -> str:
    """Substitute the four project-column placeholders in doctrine text, in one exact named pass.

    `branch_prefix` may be empty (renders `{branch_prefix}fleet/task-7` as `fleet/task-7`); a blank gate
    command means the project has no gate for that tier, and renders as `NO_QUICK_GATE`/`NO_FULL_GATE`,
    replacing a backtick pair around its token too so no empty code span is left. That is the value's own
    rendering, not a byte-divergence cause: the benign-value baseline has both gates set.
    `integration_branch` is NOT NULL and callers refuse a blank, so no validation here. Every value is defanged of fence
    and conditional markers on the way in: all four are operator-written columns substituted into
    doctrine the provider pass scans, and this dict is the one seam they all pass through.

    One regex pass rather than sequential `str.replace` is a security property: sequential passes let a
    substituted value carry a later token and complete a marker another value opened. Narrow alternation
    so unrecognized braces stay verbatim.

    THE THIRD OF THE SIX WAYS a default-provider prompt's bytes can differ (see `defang_conditional_markers`): a
    column whose value is itself a placeholder token now reaches the runner literally instead of being
    filled transitively. Strictly safer; pinned in `tests/test_dispatch_compose_providers.py`."""
    # Both defangs, paired as in `build_task_prompt`'s `_trusted()`, so a column value cannot forge
    # the trusted-region fence markers (`_validated_shell_command` rejects only `\n`/`\r`/`\0`). Capped per value
    # at the rendered-section cap: `agent_instructions` can repeat a gate token ~533 times, so one
    # `re.sub` could otherwise render GBs in the process hosting `lease-drain`. THE SIXTH DELIBERATE WAY
    # (over-cap values render shorter here than in the task prompt); fence-pass rewrites are THE FIFTH
    # DELIBERATE WAY. Defang first, cap second, as in `_defanged_operator_text`.
    def _both(value: str) -> str:
        return defang_conditional_markers(
            defang_fence_markers(value))[:_PROJECT_INSTRUCTIONS_RENDERED_MAX_CHARS]

    values = {
        "integration_branch": _both(integration_branch),
        "branch_prefix": _both(branch_prefix),
        "gate_quick_cmd": _both(gate_quick_cmd),
        "gate_full_cmd": _both(gate_full_cmd),
    }
    no_gate = {"gate_quick_cmd": NO_QUICK_GATE, "gate_full_cmd": NO_FULL_GATE}

    def _render(m: re.Match[str]) -> str:
        opener, name, closer = m.groups()
        if name in no_gate and not values[name].strip():
            # The phrase replaces a whole `{token}` code span; a lone tick is prose's, so it stays.
            return no_gate[name] if opener and closer else opener + no_gate[name] + closer
        return opener + values[name] + closer

    return _PLACEHOLDER_SUB_RE.sub(_render, text)


# ── Provider-aware rendering ──
# One closed post-pass over both assembled prompts. Markers are always removed (a residue raises) and are
# glued to text on both sides, never alone on a line, or removal would leave a blank line.

#: The closed set of conditional flags; a typo'd flag raises rather than render the wrong branch.
#: `ask` and `native` come from the account (`can_interview`, `has_review_lane`), `epic` from the row (an epic
#: child), `repo` from the slot (`slot_allocation.task_has_repo`; False for a folder slot).
_COND_FLAGS = ("ask", "epic", "native", "repo")

#: The flag spelling, stated once: `_CONDITIONAL_RE` and the walk in `resolve_conditionals` both use it,
#: so the resolver and its check cannot disagree.
_FLAG_CLASS = r"[a-z]+"

_CONDITIONAL_RE = re.compile(
    r"<!--if:(?P<flag>" + _FLAG_CLASS + r")-->(?P<on>.*?)"
    r"(?:<!--else:(?P=flag)-->(?P<off>.*?))?"
    r"<!--end:(?P=flag)-->",
    re.S,
)

#: The residue scan and the walk's candidate scan. Case-insensitive, and needs neither `:` nor the full
#: keyword, where `_CONDITIONAL_RE` is exact: typos and near-misses (`<!--IF:`, `<!--endif:`,
#: `<!-- if:`) become candidates that `_MARKER_STRICT_RE` refuses, instead of being silently shipped or
#: swallowed. That breadth requires caller text to be pre-mangled (`defang_conditional_markers`) and
#: doctrine files to be guarded by a test; widen this without
#: those and an ordinary `<!-- end of section -->` stops every dispatch.
_MARKER_RESIDUE_RE = re.compile(r"<!--\s*(?:if|else|end)", re.I)

#: A complete, well-formed marker. Every loose candidate must match this at the same offset, so a
#: malformed or mis-flagged marker is refused on the input rather than swallowed by a lazy group.
_MARKER_STRICT_RE = re.compile(r"<!--(if|else|end):([A-Za-z_]+)-->", re.I)
#: Case-insensitive so a mis-cased marker is reported by casing (the walk re-checks case); the resolver
#: `_CONDITIONAL_RE` stays exact.

#: The flag spelling `_CONDITIONAL_RE` resolves; narrower than `_MARKER_STRICT_RE`'s group, which is wide
#: so the walk can report a bad flag.
_FLAG_SPELLING_RE = re.compile(_FLAG_CLASS)


def resolve_conditionals(text: str, **flags: bool) -> str:
    """Resolve every `<!--if:F-->on<!--else:F-->off<!--end:F-->` block in `text` against `flags`.

    `else` is optional: without it a False flag renders nothing. Every flag in `_COND_FLAGS` must be
    supplied and no other (a default is a branch nobody chose). A leftover marker raises."""
    unknown = sorted(set(flags) - set(_COND_FLAGS))
    missing = sorted(set(_COND_FLAGS) - set(flags))
    if unknown or missing:
        raise ValueError(
            f"conditional flags must be exactly {list(_COND_FLAGS)} — "
            f"unknown={unknown} missing={missing}"
        )

    # Structure is checked on the input, before any branch is discarded (else detection depends on the
    # flag value), walked in order rather than counted (counts balance across blocks): `if`, at most one
    # `else`, `end`, no nesting. Also ReDoS protection: `_CONDITIONAL_RE` is quadratic on unbalanced input.
    # Every candidate must be exactly lowercase `<!--if|else|end:<flag>-->`, or `_CONDITIONAL_RE` would
    # swallow prose (e.g. `A<!--if:ask-->B<!--ELSE:ask-->C<!--end:ask-->D` rendering "AD").
    depth = 0
    seen_else = False
    open_flag = ""
    for candidate in _MARKER_RESIDUE_RE.finditer(text):
        marker = _MARKER_STRICT_RE.match(text, candidate.start())
        if marker is None:
            raise ValueError(
                f"malformed conditional marker {text[candidate.start():candidate.start() + 24]!r} — a "
                "marker is exactly `<!--if:<flag>-->` in lower case, and one this pass cannot resolve is "
                "silently swallowed by whichever branch encloses it"
            )
        kind, flag = marker.group(1), marker.group(2)
        if kind != kind.lower():
            raise ValueError(
                f"mis-cased conditional marker {marker.group(0)!r} — the markers resolve only in lower "
                "case, so this one would ship into a runner prompt as literal punctuation"
            )
        # Check the flag's spelling here so the diagnosis names the real cause (`_MARKER_STRICT_RE`
        # matches wide on purpose); otherwise only the residue backstop catches it, with a wrong message.
        if not _FLAG_SPELLING_RE.fullmatch(flag):
            raise ValueError(
                f"conditional marker {marker.group(0)!r} names flag {flag!r} — a flag is lower-case "
                "ASCII letters only, so this block would not resolve and its arm would be swallowed"
            )
        if kind == "if":
            if depth:
                raise ValueError(
                    "nested conditional markers: an `if` opened inside an open block — blocks may not "
                    "nest, and an unresolved marker must never reach a runner"
                )
            depth, seen_else, open_flag = 1, False, flag
        elif kind == "else":
            if not depth:
                raise ValueError(
                    "conditional marker `else` outside any block — every `else` needs its own `if`, and "
                    "an unresolved marker must never reach a runner"
                )
            if seen_else:
                raise ValueError(
                    "two `else` markers in one conditional block — a block takes at most one, and the "
                    "second would be swallowed into the discarded arm and render silently"
                )
            # The flag must match its `if`: `_CONDITIONAL_RE` pairs by backreference and would swallow it.
            if flag != open_flag:
                raise ValueError(
                    f"conditional `else:{flag}` inside a block opened as `if:{open_flag}` — a block's "
                    "three markers must name the identical flag, or this one is swallowed by the branch "
                    "around it"
                )
            seen_else = True
        else:  # "end"
            if not depth:
                raise ValueError(
                    "conditional marker `end` outside any block — every `end` needs its own `if`, and an "
                    "unresolved marker must never reach a runner"
                )
            if flag != open_flag:
                raise ValueError(
                    f"conditional `end:{flag}` closing a block opened as `if:{open_flag}` — a block's "
                    "three markers must name the identical flag, or the block is never closed at all"
                )
            depth, seen_else, open_flag = 0, False, ""
    if depth:
        raise ValueError(
            "unterminated conditional block: an `if` was never closed by its `end` — an unresolved "
            "marker must never reach a runner"
        )

    def pick(match: re.Match) -> str:
        flag = match.group("flag")
        if flag not in flags:
            raise ValueError(
                f"prompt text names an unknown conditional flag {flag!r}; the closed set is "
                f"{list(_COND_FLAGS)}"
            )
        return match.group("on") if flags[flag] else (match.group("off") or "")

    out = _CONDITIONAL_RE.sub(pick, text)
    # The backstop: catches a well-formed but mis-cased block, which the case-insensitive walk accepts
    # and the exact `_CONDITIONAL_RE` does not resolve.
    survivor = _MARKER_RESIDUE_RE.search(out)
    if survivor is not None:
        raise ValueError(
            f"conditional marker {survivor.group(0)!r} survived the pass — either it is mis-cased (the "
            "markers resolve only in lowercase) or a block is missing its `if`/`else`/`end` partner, and "
            "an unresolved marker must never reach a runner"
        )
    return out


#: The spelling the charter's prose is written in, read from the catalogue (the default provider's CLI is the
#: baseline the charter was written for).
_CHARTER_PROVIDER = agent_providers.by_value(agent_providers.DEFAULT_PROVIDER)
_CHARTER_TOOL_PREFIX = _CHARTER_PROVIDER.mcp_tool_prefix
_CHARTER_ASK_TOOL = _CHARTER_PROVIDER.ask_tool
_CHARTER_ASK_DISMISSED_RE = re.compile(
    r"\s+".join(map(re.escape, _CHARTER_PROVIDER.ask_dismissed_result.split())))


def _outside_fence(text: str, respell: Callable[[str], str]) -> str:
    """`text` with `respell` applied outside the untrusted fence (a task body is a record, left as typed):
    before `_FENCE_BEGIN`, and after the last `_FENCE_END` when one follows it."""
    begin = text.find(_FENCE_BEGIN)
    if begin < 0:
        return respell(text)
    end = text.rfind(_FENCE_END)
    if end < begin:
        return respell(text[:begin]) + text[begin:]
    return respell(text[:begin]) + text[begin:end] + respell(text[end:])


def respell_vocabulary(text: str) -> str:
    """`text` with every renamed agent verb (`agent_providers.VOCABULARY_SPELLINGS`) back in its pre-rename
    spelling — dotted (`jaah-call merge_queue.batch`) and as a tool name, bare or after any provider's prefix
    (`mcp__plugin_jaah_control__task_complete`) — outside the untrusted fence. For a node that does not report
    `vocabulary: industry` (`_node_uses_legacy_vocabulary`): it serves only the pre-rename names."""
    pairs = sorted(agent_providers.VOCABULARY_SPELLINGS, key=lambda pair: -len(pair[0]))

    def respell(part: str) -> str:
        for current, old in pairs:
            part = re.sub(r"(?<![\w.-])" + re.escape(current) + r"(?![\w])", old, part)
            # A tool name stands alone or right after a provider prefix (`…__` or `mcp_control_`), never
            # inside a longer identifier — so a pre-rename name is never respelled twice.
            part = re.sub(r"(?:(?<=__)|(?<=mcp_control_)|(?<![\w.-]))" + re.escape(current.replace(".", "_"))
                          + r"(?![\w])", old.replace(".", "_"), part)
        return part

    return _outside_fence(text, respell)


# ── Per-project operator instructions (`projects.agent_instructions`) ──

# Cap on the operator's pasted text: 8000 (not `_safe_trusted_text`'s 4000), because this per-project
# column holds every deviation a runner needs and must not be cut mid-sentence.
_PROJECT_INSTRUCTIONS_MAX_CHARS = 8000

# Cap on the rendered text: substitution multiplies (placeholders × gate columns), so the paste cap alone
# does not bound the output. 4x the paste cap cannot truncate legitimate text (the tokens expand to tens
# of characters) and bounds amplification. It is a byte-divergence cause (see
# `defang_conditional_markers`); it truncates operator text, so it logs at WARNING.
_PROJECT_INSTRUCTIONS_RENDERED_MAX_CHARS = 4 * _PROJECT_INSTRUCTIONS_MAX_CHARS
