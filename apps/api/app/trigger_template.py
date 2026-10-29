"""The trigger template renderer — our own, no Jinja and no logic.

A template is text with `{{ns.path}}` placeholders. `ns` is one of `NAMESPACES`; the path is dot-separated
segments of `[A-Za-z0-9_-]+`, each optionally followed by `[n]` list indexes (`{{body.items[0].name}}`). A bare
numeric segment indexes a list too. `headers` is matched case-insensitively.

Rendering never fails: a missing path renders as `""` and is reported in `missing_paths` (A7); a non-string
value renders as compact JSON; each value is capped at `MAX_VALUE_CHARS` and the whole output at
`MAX_OUTPUT_CHARS`, both with a "…(truncated)" suffix. Every refusal happens at SAVE (`validate_template`),
so a stored template is always renderable.

The rendered text becomes the filed Task's body, so it reaches the runner inside the untrusted fence
by construction (`dispatch_compose`); nothing here needs to escape it.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

NAMESPACES = ("body", "headers", "query", "email", "slack", "user", "item")

# Which namespaces each source kind's event carries. `item` is always offered: it is a preflight's, and a
# template naming it on a trigger without one simply renders it empty (and names the path).
NAMESPACES_BY_KIND: dict[str, frozenset[str]] = {
    "webhook": frozenset({"body", "headers", "query", "item"}),
    "github": frozenset({"body", "headers", "query", "item"}),
    "gmail": frozenset({"email", "item"}),
    "slack": frozenset({"slack", "item"}),
    # `{{user.*}}`: the keys `trigger_user_registered.USER_EVENT_KEYS` lists.
    "user_registered": frozenset({"user", "item"}),
}

# Headers a template may never name: the credentials a sender proves itself with. They are also stripped
# from the stored event (`trigger_intake.redact_headers`), so this refusal is the readable half.
FORBIDDEN_HEADERS = ("authorization", "cookie")
FORBIDDEN_HEADER_PREFIXES = ("x-hub-signature", "x-jaah-signature")

MAX_VALUE_CHARS = 8 * 1024
MAX_OUTPUT_CHARS = 64 * 1024
TRUNCATED = "…(truncated)"

# A title is one line; longer than this is a body, not a title.
MAX_TITLE_CHARS = 200

_PLACEHOLDER = re.compile(r"\{\{(.*?)\}\}", re.DOTALL)
_SEGMENT = re.compile(r"([A-Za-z0-9_-]+)((?:\[\d+\])*)")
_INDEX = re.compile(r"\[(\d+)\]")


class TemplateError(ValueError):
    """A template that cannot be saved; the message is the readable reason the console shows."""


@dataclass(frozen=True)
class Placeholder:
    raw: str
    namespace: str
    steps: tuple[str | int, ...]

    @property
    def path(self) -> str:
        return self.raw


@dataclass
class Rendered:
    text: str
    missing_paths: list[str] = field(default_factory=list)


def _parse(inner: str) -> Placeholder:
    """One placeholder's inner text → its namespace and lookup steps, or `TemplateError`."""
    raw = inner.strip()
    if not raw:
        raise TemplateError("an empty placeholder {{}} names nothing — write {{body.field}}")
    segments = raw.split(".")
    namespace = segments[0]
    if namespace not in NAMESPACES:
        raise TemplateError(
            f"{{{{{raw}}}}} starts with {namespace!r}, which is not a template namespace — use one of "
            f"{', '.join(NAMESPACES)}"
        )
    steps: list[str | int] = []
    for segment in segments[1:]:
        match = _SEGMENT.fullmatch(segment)
        if match is None:
            raise TemplateError(
                f"{{{{{raw}}}}} has a malformed path segment {segment!r} — use letters, digits, '_' or "
                f"'-', optionally followed by [n] indexes"
            )
        name, indexes = match.groups()
        steps.append(int(name) if name.isdigit() else name)
        steps.extend(int(i) for i in _INDEX.findall(indexes))
    return Placeholder(raw=raw, namespace=namespace, steps=tuple(steps))


def placeholders(template: str) -> list[Placeholder]:
    """Every placeholder in `template`, parsed; `TemplateError` on the first malformed one."""
    return [_parse(m.group(1)) for m in _PLACEHOLDER.finditer(template or "")]


def _forbidden_header(placeholder: Placeholder) -> bool:
    if placeholder.namespace != "headers" or not placeholder.steps:
        return False
    name = str(placeholder.steps[0]).lower()
    return name in FORBIDDEN_HEADERS or name.startswith(FORBIDDEN_HEADER_PREFIXES)


def validate_template(template: str, *, source_kind: str, field_name: str = "prompt_template") -> None:
    """Refuse, with a readable reason, a template that names a credential header, a namespace this source
    cannot provide, or a malformed placeholder. `field_name` prefixes the reason so the console can place it."""
    allowed = NAMESPACES_BY_KIND.get(source_kind)
    if allowed is None:
        raise TemplateError(f"{field_name}: unknown source kind {source_kind!r}")
    try:
        parsed = placeholders(template)
    except TemplateError as exc:
        raise TemplateError(f"{field_name}: {exc}") from None
    for placeholder in parsed:
        if _forbidden_header(placeholder):
            raise TemplateError(
                f"{field_name}: {{{{{placeholder.raw}}}}} would copy the sender's credential into the Task — "
                f"the authorization, cookie and signature headers can never be templated"
            )
        if placeholder.namespace not in allowed:
            raise TemplateError(
                f"{field_name}: {{{{{placeholder.raw}}}}} — a {source_kind} trigger's event has no "
                f"{placeholder.namespace!r}; use one of {', '.join(sorted(allowed))}"
            )


_MISSING = object()


def _lookup(event: dict[str, Any], placeholder: Placeholder) -> Any:
    node: Any = event.get(placeholder.namespace, _MISSING)
    for index, step in enumerate(placeholder.steps):
        if node is _MISSING or node is None:
            return _MISSING
        if isinstance(step, int):
            if isinstance(node, list) and -1 < step < len(node):
                node = node[step]
            elif isinstance(node, dict) and str(step) in node:
                node = node[str(step)]
            else:
                return _MISSING
        elif isinstance(node, dict):
            if placeholder.namespace == "headers" and index == 0:
                lowered = {str(k).lower(): v for k, v in node.items()}
                node = lowered.get(step.lower(), _MISSING)
            else:
                node = node.get(step, _MISSING)
        else:
            return _MISSING
    return node


def _cap(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - len(TRUNCATED)] + TRUNCATED


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False, sort_keys=False)


def render(template: str, event: dict[str, Any]) -> Rendered:
    """Render `template` over `event` (`{"body", "headers", "query", ...}`). Never raises on a value; a
    placeholder that does not parse (only reachable for a template saved before a grammar change) is left as
    literal text. A credential header renders empty even if it somehow reached the event."""
    missing: list[str] = []

    def substitute(match: re.Match[str]) -> str:
        try:
            placeholder = _parse(match.group(1))
        except TemplateError:
            return match.group(0)
        if _forbidden_header(placeholder):
            return ""
        value = _lookup(event, placeholder)
        if value is _MISSING:
            if placeholder.raw not in missing:
                missing.append(placeholder.raw)
            return ""
        return _cap(_as_text(value), MAX_VALUE_CHARS)

    text = _PLACEHOLDER.sub(substitute, template or "")
    return Rendered(text=_cap(text, MAX_OUTPUT_CHARS), missing_paths=missing)


def render_title(template: str, event: dict[str, Any]) -> Rendered:
    """`render`, collapsed to one line and capped at `MAX_TITLE_CHARS`."""
    rendered = render(template, event)
    line = " ".join(rendered.text.split())
    return Rendered(text=_cap(line, MAX_TITLE_CHARS), missing_paths=rendered.missing_paths)
