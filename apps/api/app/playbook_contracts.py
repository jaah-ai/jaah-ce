"""The composition data a playbook stage's prompt is rendered from — deliverable contracts and expert layouts.

A stage is an **Expert** × a **Deliverable** (`app/playbook_vocabulary.py`). Rendering one means splitting
today's role prose (`app/dispatch_compose/roles.py::_ROLES`) along that seam (decision A2): the Expert brings its
prose and its own quality gate; the Deliverable brings the mode rules, its deliverable rules, the settle
terms, the manifest and the filing tool. This module holds both halves as data; `app/playbook_prompt.py`
walks them.

- `DELIVERABLE_CONTRACTS[d]` — a deliverable's mode, delivery channel, settle terms, the role-dict keys it
  owns, and its ORDERED slots (name → text). Every text is a live constant of `dispatch_compose` or a slice
  of a native expert's own prose, never re-typed.
- `LAYOUTS[expert]` — that expert's `extra_rules`, sliced at each shared constant's actual position and at
  its `##`/`###` headings, as an ordered list of segments:
  `("expert", text)` always renders; `("slot", name, text)` renders the stage deliverable's text for `name`
  (the sliced `text` is what the NATIVE deliverable supplies); `("native", name, text)` is the expert's own
  version of contract section `name`, rendered only with its native deliverable — any other deliverable
  renders its own text for `name` there, if it has one. The join of every segment's text IS the live
  `extra_rules` string (`tests/test_playbook_parity.py` holds that for all ten experts).

BEHIND ONE SEAM: this module reads `dispatch_compose`/`persona_registry`; the run path reaches it only
through `app/playbook_engine.py` (`tests/test_playbook_isolation.py`). Pure: built once at import, from strings already loaded.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Callable, Mapping

from app import playbook_vocabulary as v
from app.dispatch_compose.doctrine import BUNDLE
from app.dispatch_compose.roles import _ROLES
from app.dispatch_compose.settle_terms import SettleTerms
from app.persona_registry import (
    _ACTIVE_ONLY,
    _FINISH_SETTLE_STATES,
    _REPORT_ADVISORY,
    _REPORT_HARD,
    DB_DIRECT_SETTLE_STATES,
    DELIVERY_DB,
    DELIVERY_PR,
)
from app.task_manifest import REQUIRED_HARD

# ── Slot names ───────────────────────────────────────────────────────────────────────────────────────

MODE_RULES = "mode_rules"
PR_EVIDENCE = "pr_evidence"
PR_TERMINAL = "pr_terminal"
CONVERSATION_RULES = "conversation_rules"
MOCKUP_RULES = "mockup_rules"
PLAN_RULES = "plan_rules"
REVIEW_RULES = "review_rules"
#: The onboarding engineer's own build-pair terminal section — no deliverable supplies one.
OWN_TERMINAL = "own_terminal"

#: The role-dict keys an EXPERT brings (its prose and its own quality gate), in `_ROLES` spelling.
EXPERT_ROLE_KEYS: tuple[str, ...] = (
    "title", "inputs", "focus", "output_contract", "input_slots", "skills", "tools",
    "gate", "gate_name", "settle_gate_arg", "settle_summary_hint",
)

Segment = tuple  # ("expert", text) | ("slot", name, text) | ("native", name, text)

# The shared constants, from this edition's doctrine bundle (`dispatch_compose/doctrine.py`).
_ADVISOR_STANDING_ORDERS = BUNDLE.shared["_ADVISOR_STANDING_ORDERS"]
_ANALYSIS_PROHIBITIONS = BUNDLE.shared["_ANALYSIS_PROHIBITIONS"]
_BEHAVIORAL_EVIDENCE_R20 = BUNDLE.shared["_BEHAVIORAL_EVIDENCE_R20"]
_ENGINEERING_RULES = BUNDLE.shared["_ENGINEERING_RULES"]
_MERGED_PR_TERMINAL_REPORTING = BUNDLE.shared["_MERGED_PR_TERMINAL_REPORTING"]

# ── Expert layouts ───────────────────────────────────────────────────────────────────────────────────

#: Shared constants sliced out wherever they occur, and the slot each one fills.
_SLOT_CONSTANTS: tuple[tuple[str, str], ...] = (
    (_ENGINEERING_RULES, MODE_RULES),
    (_ANALYSIS_PROHIBITIONS, MODE_RULES),
    (_BEHAVIORAL_EVIDENCE_R20, PR_EVIDENCE),
    (_MERGED_PR_TERMINAL_REPORTING, PR_TERMINAL),
    (_ADVISOR_STANDING_ORDERS, CONVERSATION_RULES),
)

#: Per expert: the heading prefix of each remainder section that is not the expert's own prose, and what
#: it is. Every prefix must match exactly one section (asserted), so a renamed heading fails at import.
_SECTION_KINDS: Mapping[str, Mapping[str, tuple[str, str]]] = {
    "planner": {
        prefix: ("slot", PLAN_RULES) for prefix in (
            "### Ordering",
            "### Grouping level",
            "### Every child body",
            "### Coverage",
            "### Required checks",
        )
    },
    "ui_prototyper": {
        prefix: ("slot", MOCKUP_RULES) for prefix in (
            "### One self-contained HTML file",
            "### Where the write you ARE allowed goes",
            "### Then settle `outcome`",
        )
    },
    "reviewer": {
        prefix: ("slot", REVIEW_RULES) for prefix in (
            "### The LAST SHOT",
            "### The epic's final report",
        )
    },
    "researcher": {"## Your terminal outcome — the build pair": ("native", PR_TERMINAL)},
    "onboarding": {"## Your terminal outcome\n": ("native", OWN_TERMINAL)},
}

#: Empty native segments: a contract section the expert's native deliverable supplies but the expert, on
#: that deliverable, deliberately omits (the researcher carries no R20 block).
_EMPTY_NATIVE: Mapping[str, tuple[str, ...]] = {"researcher": (PR_EVIDENCE,)}

_HEADING = re.compile(r"(?m)^(?=#{2,3} )")


def _slice(expert: str, text: str) -> list[Segment]:
    spans = []
    for constant, slot in _SLOT_CONSTANTS:
        if not constant:
            continue  # a section this edition's doctrine withholds: nothing to slice
        count = text.count(constant)
        assert count <= 1, f"{expert}: a shared constant occurs {count} times in its extra_rules"
        if count:
            start = text.index(constant)
            spans.append((start, start + len(constant), slot))
    spans.sort()
    kinds = _SECTION_KINDS.get(expert, {})
    matched: dict[str, int] = dict.fromkeys(kinds, 0)
    raw: list[Segment] = []

    def remainder(chunk: str) -> None:
        for section in (piece for piece in _HEADING.split(chunk) if piece):
            hits = [prefix for prefix in kinds if section.startswith(prefix)]
            if hits:
                matched[hits[0]] += 1
                kind, name = kinds[hits[0]]
                raw.append((kind, name, section))
            else:
                raw.append(("expert", section))

    cursor = 0
    for start, end, slot in spans:
        assert start >= cursor, f"{expert}: two shared constants overlap in its extra_rules"
        remainder(text[cursor:start])
        raw.append(("slot", slot, text[start:end]))
        cursor = end
    remainder(text[cursor:])
    unmatched = [prefix for prefix, hits in matched.items() if hits != 1]
    assert not unmatched, f"{expert}: section prefixes matched no single section: {unmatched}"
    for name in _EMPTY_NATIVE.get(expert, ()):
        raw.append(("native", name, ""))
    # Adjacent segments of the same kind and name are one segment.
    merged: list[Segment] = []
    for segment in raw:
        if merged and merged[-1][:-1] == segment[:-1]:
            merged[-1] = (*segment[:-1], merged[-1][-1] + segment[-1])
        else:
            merged.append(segment)
    return merged


def expert_role(expert: str) -> Mapping[str, object]:
    """The expert's own `_ROLES` row, reached through its prose key (never a dispatch path)."""
    return _ROLES[v.EXPERTS[expert].charter_key]


LAYOUTS: Mapping[str, tuple[Segment, ...]] = MappingProxyType({
    expert: tuple(_slice(expert, str(expert_role(expert).get("extra_rules") or "")))
    for expert in v.EXPERTS
})


def _native_text(expert: str, name: str) -> str:
    """The joined text of `expert`'s segments that fill `name` — how a native slice becomes a slot text."""
    return "".join(seg[-1] for seg in LAYOUTS[expert] if seg[0] != "expert" and seg[1] == name)


def asks_at_gate(expert: str) -> bool:
    """Whether this expert's gate may ask the operator — every expert but the Planner (`_NEVER_ASKS`)."""
    return expert != "planner"


# ── Deliverable contracts ────────────────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class DeliverableContract:
    key: str
    mode: str
    #: `build_task_prompt`'s `delivery`: `pr` for the build deliverables, `db` for the others.
    delivery: str
    #: The settle terms for a stage whose expert is the argument.
    terms: Callable[[str], SettleTerms]
    #: Ordered `(slot name, text)`: an unplaced slot renders in this order right after `mode_rules`.
    slots: tuple[tuple[str, str], ...]
    #: The role-dict keys this deliverable owns (the filing tool, the no-terminal flag).
    role_keys: Mapping[str, object] = field(default_factory=dict)

    def slot(self, name: str) -> str | None:
        return dict(self.slots).get(name)


def _terms(terminals: tuple[str, ...], manifest, settle_states) -> Callable[[str], SettleTerms]:
    return lambda _expert: SettleTerms(tuple(manifest), terminals, tuple(settle_states))


def _pull_request_terms(expert: str) -> SettleTerms:
    # The researcher's native manifest: its report is the product, so HARD even beside a docs PR.
    manifest = _REPORT_HARD if expert == "researcher" else _REPORT_ADVISORY
    return SettleTerms(tuple(manifest), ("merged_pr", "no_pr"), tuple(_ACTIVE_ONLY))


def _document_terms(expert: str) -> SettleTerms:
    hand_off = tuple((kind, REQUIRED_HARD) for kind in v.hands_off(expert, v.DOCUMENT))
    return SettleTerms(tuple(_REPORT_HARD) + hand_off, ("analysis",), tuple(DB_DIRECT_SETTLE_STATES))


_PLANNER_ROLE = _ROLES[v.EXPERTS["planner"].charter_key]
_ADVISOR_ROLE = _ROLES[v.EXPERTS["advisor"].charter_key]
_MODE = {d.key: d.mode for d in v.DELIVERABLE_RECORDS}

DELIVERABLE_CONTRACTS: Mapping[str, DeliverableContract] = MappingProxyType({
    v.PULL_REQUEST: DeliverableContract(
        v.PULL_REQUEST, _MODE[v.PULL_REQUEST], DELIVERY_PR, _pull_request_terms,
        ((MODE_RULES, _ENGINEERING_RULES), (PR_EVIDENCE, _BEHAVIORAL_EVIDENCE_R20),
         (PR_TERMINAL, _MERGED_PR_TERMINAL_REPORTING)),
    ),
    v.DOCUMENT: DeliverableContract(
        v.DOCUMENT, _MODE[v.DOCUMENT], DELIVERY_DB, _document_terms,
        ((MODE_RULES, _ANALYSIS_PROHIBITIONS),),
    ),
    v.MOCKUP: DeliverableContract(
        v.MOCKUP, _MODE[v.MOCKUP], DELIVERY_DB, _terms(("outcome",), _REPORT_HARD, _ACTIVE_ONLY),
        ((MODE_RULES, _ANALYSIS_PROHIBITIONS), (MOCKUP_RULES, _native_text("ui_prototyper", MOCKUP_RULES))),
    ),
    v.PLAN: DeliverableContract(
        v.PLAN, _MODE[v.PLAN], DELIVERY_DB, _terms(("coders", "analysis"), _REPORT_HARD, DB_DIRECT_SETTLE_STATES),
        ((MODE_RULES, _ANALYSIS_PROHIBITIONS), (PLAN_RULES, _native_text("planner", PLAN_RULES))),
        role_keys=MappingProxyType({
            key: _PLANNER_ROLE[key] for key in ("tool", "tool_heading", "structured_endpoint")
        }),
    ),
    v.REVIEW: DeliverableContract(
        v.REVIEW, _MODE[v.REVIEW], DELIVERY_DB, _terms(("finish",), _REPORT_HARD, _FINISH_SETTLE_STATES),
        ((MODE_RULES, _ANALYSIS_PROHIBITIONS), (REVIEW_RULES, _native_text("reviewer", REVIEW_RULES))),
    ),
    v.CONVERSATION: DeliverableContract(
        v.CONVERSATION, _MODE[v.CONVERSATION], DELIVERY_PR, _terms((), (), ()),
        ((MODE_RULES, _ENGINEERING_RULES), (PR_EVIDENCE, _BEHAVIORAL_EVIDENCE_R20),
         (CONVERSATION_RULES, _ADVISOR_STANDING_ORDERS)),
        role_keys=MappingProxyType({"no_terminal_call": _ADVISOR_ROLE["no_terminal_call"]}),
    ),
})
assert set(DELIVERABLE_CONTRACTS) == set(v.DELIVERABLES)


def extra_rules(expert: str, deliverable: str) -> str:
    """`expert`'s `extra_rules` recomposed for `deliverable`: the layout walked, each slot filled from the
    deliverable, and every deliverable slot the layout never placed rendered right after `mode_rules`."""
    contract = DELIVERABLE_CONTRACTS[deliverable]
    native = v.EXPERTS[expert].native_deliverable == deliverable
    layout = LAYOUTS[expert]
    placed = {seg[1] for seg in layout if seg[0] != "expert"}
    unplaced = [text for name, text in contract.slots if name not in placed]
    out: list[str] = []
    for seg in layout:
        if seg[0] == "expert":
            out.append(seg[1])
            continue
        kind, name, text = seg
        out.append(text if kind == "native" and native else contract.slot(name) or "")
        if name == MODE_RULES:
            out.extend(unplaced)
            unplaced = []
    return _joined(unplaced + out)


def _joined(pieces: list[str]) -> str:
    """The pieces as one text, each followed by a blank line before the next. A slot that ends a native
    layout (the planner's, the reviewer's, the shared terminal section) carries no trailing blank line, so
    placed anywhere else it would glue the next heading onto its last line. Natively a no-op."""
    out = ""
    for piece in (p for p in pieces if p):
        if out and not out.endswith("\n\n"):
            out += "\n" if out.endswith("\n") else "\n\n"
        out += piece
    return out


def role(expert: str, deliverable: str) -> dict[str, object]:
    """The role dict `render_role_section_with` takes: the expert's keys ∪ the deliverable's ∪ the
    recomposed `extra_rules`."""
    own = expert_role(expert)
    out: dict[str, object] = {key: own[key] for key in EXPERT_ROLE_KEYS if key in own}
    out.update(DELIVERABLE_CONTRACTS[deliverable].role_keys)
    rules = extra_rules(expert, deliverable)
    if rules:
        out["extra_rules"] = rules
    return out
