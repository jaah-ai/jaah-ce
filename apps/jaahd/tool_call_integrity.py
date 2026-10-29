"""Refuse a tool call whose parameters were swallowed by the model's own tool-call markup.

When the model closes a parameter with its own name instead of the literal close tag, the harness reads
the tag as text and folds the rest of the call, later parameters included, into one string; those then
arrive absent, and a settle call carrying them stalls.

Detected by the tail, not the tag: `</summary>` etc. appear in legitimate HTML, so a value is refused only
when the remainder after the tag parses entirely as declared parameter blocks. Strict or nothing — a
partial match is a guess about a runner's terminal report.

Refused rather than repaired: the refusal travels back up the socket to a live, mid-turn session that can
resend (`ERR_MALFORMED_CALL`), so the call is correct or absent, never quietly wrong. Pure: no I/O.
"""
from __future__ import annotations

import re

__all__ = ["MARKUP_REFUSAL_HINT", "find_swallowed_parameters", "find_over_long_arg"]


# One parameter of the swallowed tail, in both spellings the harness produces: `<name>…</name>` and
# `<parameter name="name">…</parameter>`. Non-greedy DOTALL: a value can be multi-line.
_TAIL_BLOCK = re.compile(
    r"\s*(?:<(?P<bare>[A-Za-z_][A-Za-z0-9_]*)>(?P<bare_value>.*?)</(?P=bare)>"
    r"|<(?:[A-Za-z][\w.-]*:)?parameter\s+name\s*=\s*[\"'](?P<named>[A-Za-z_][A-Za-z0-9_]*)[\"']\s*>"
    r"(?P<named_value>.*?)</(?:[A-Za-z][\w.-]*:)?parameter\s*>)",
    re.DOTALL,
)

# The wrapper close, optional (a truncated call may lack it) and accepted only as the last thing in the tail.
_TRAILING_CLOSE = re.compile(r"\s*</(?:[A-Za-z][\w.-]*:)?invoke\s*>\s*\Z")

# Says nothing landed: a runner suspecting a partial write would retry a different call, not the same one.
MARKUP_REFUSAL_HINT = (
    "The harness did not parse your parameters as separate arguments: everything after that tag was "
    "folded into one value and the parameters after it arrived ABSENT. Close every parameter with the "
    "literal </parameter> — never with the parameter's own name — and send the whole call again. "
    "NOTHING was recorded."
)


def _strings(value: object, path: str) -> "list[tuple[str, str]]":
    """Every string inside an argument, each with a readable path to it.

    Recursive: `task.create_batch` carries its bodies (and trusted-preamble fields) inside an
    array, which a top-level scan would pass unread.
    """
    if isinstance(value, str):
        return [(path, value)]
    if isinstance(value, dict):
        return [found for key in sorted(value) for found in _strings(value[key], f"{path}.{key}")]
    if isinstance(value, list):
        return [found for i, item in enumerate(value) for found in _strings(item, f"{path}[{i}]")]
    return []


def _swallowed_tail(value: str, marker: str, declared: "frozenset[str]") -> "tuple[str, ...] | None":
    """The parameter names in the tail after `marker`, or None (ordinary prose containing the tag)."""
    _, separator, tail = value.partition(marker)
    if not separator:
        return None
    recovered: list[str] = []
    position = 0
    while (match := _TAIL_BLOCK.match(tail, position)) is not None:
        name = match.group("bare") or match.group("named")
        # An undeclared name or a repeat is not this shape; bail whole rather than keep a partial read.
        if name not in declared or name in recovered:
            return None
        recovered.append(name)
        position = match.end()
    if not recovered:
        return None
    remainder = tail[position:]
    if remainder.strip() and not _TRAILING_CLOSE.match(remainder):
        return None
    return tuple(recovered)


def find_swallowed_parameters(
    args: dict, arg_names: "frozenset[str]"
) -> "tuple[str, str, tuple[str, ...]] | None":
    """`(argument_path, the_marker_tag, the_swallowed_parameter_names)` for the first swallowed call.

    The marker is a close tag named after an argument this verb declares (keeps `</div>` out); the tail
    confirms it.
    """
    for name in sorted(args):
        for path, value in _strings(args[name], name):
            if not value:
                continue
            for declared_name in sorted(arg_names):
                marker = f"</{declared_name}>"
                if marker not in value:
                    continue
                swallowed = _swallowed_tail(value, marker, arg_names)
                if swallowed is not None:
                    return path, marker, swallowed
    return None


def find_over_long_arg(args: dict, limits: "dict[str, int]") -> "tuple[str, int, int] | None":
    """The first `(argument_path, length, limit)` past its declared cap, or None.

    The caps are the control plane's, mirrored (jaahd cannot import `apps/api`). Enforced here too
    because the CP's 422 lands after the wire on a runner's last call; here the runner still gets the
    number while it can meet it.
    """
    for name in sorted(args):
        for path, value in _strings(args[name], name):
            # Keyed on the leaf name, so a nested field's cap binds on that field.
            limit = limits.get(path.rsplit(".", 1)[-1])
            if limit is not None and len(value) > limit:
                return path, len(value), limit
    return None
