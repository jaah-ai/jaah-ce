"""The outcome state machine — one declarative table, `TRANSITIONS`; `writer.py` is its only caller.

CP-only: no runner tool moves a row (`review_tier` and findings on the settle call are attribution, not state).

    pending ──merge──► merged ──tick: window over, no LIVE link──► survived
       │                 └──LIVE link──► reverted | fixed_forward | incident_linked (and back when dismissed)
       ├──fail──► failed            ├──end without settle──► abandoned
       └──settle without a merge (no_pr, analysis)──► settled ──epic children final──► inherited result

A LIVE link is `confirmed`, or `proposed` with confidence above `LIVE_CONFIDENCE`: one rule for moving and blocking.
Strictly above, so a heuristic `same_files` match (capped at 0.50) never moves or blocks an outcome on its own: it
needs an operator's confirm.
"""
from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

PENDING = "pending"
MERGED = "merged"
DEPLOYED = "deployed"
SURVIVED = "survived"
REVERTED = "reverted"
FIXED_FORWARD = "fixed_forward"
INCIDENT_LINKED = "incident_linked"
FAILED = "failed"
ABANDONED = "abandoned"
SETTLED = "settled"

#: The defect states, worst first: an analysis row inheriting several children takes the worst.
DEFECT_STATES: tuple[str, ...] = (REVERTED, FIXED_FORWARD, INCIDENT_LINKED)
#: Judged: in a success-rate denominator. `pending`/`merged`/`deployed`/`settled` are not.
FINAL_STATES: frozenset[str] = frozenset({SURVIVED, *DEFECT_STATES, FAILED, ABANDONED})
#: A merged row whose code is still in the integration branch and may yet be judged by a link.
LANDED_STATES: frozenset[str] = frozenset({MERGED, DEPLOYED, SURVIVED, *DEFECT_STATES})

#: The confidence a `proposed` link must exceed to be LIVE.
LIVE_CONFIDENCE = Decimal("0.5")

# The edges (what moved the row), for the table and the refusal message.
EDGE_MERGE = "merge"
EDGE_DEPLOY = "deploy"
EDGE_SURVIVE = "survive"
EDGE_LINK = "link"
EDGE_FAIL = "fail"
EDGE_ABANDON = "abandon"
EDGE_SETTLE = "settle"
EDGE_INHERIT = "inherit"
EDGE_RECOVER = "recover"


@dataclass(frozen=True)
class Transition:
    edge: str
    sources: frozenset[str]
    targets: frozenset[str]


_LINKABLE = frozenset({MERGED, DEPLOYED, SURVIVED, *DEFECT_STATES})

TRANSITIONS: tuple[Transition, ...] = (
    Transition(EDGE_MERGE, frozenset({PENDING}), frozenset({MERGED})),
    # Production QA's `deployed` fact (`writer.record_deployed`).
    Transition(EDGE_DEPLOY, frozenset({MERGED}), frozenset({DEPLOYED})),
    Transition(EDGE_SURVIVE, frozenset({MERGED, DEPLOYED}), frozenset({SURVIVED})),
    # A link recomputes the state from the LIVE links left: a defect, or back to merged/survived.
    Transition(EDGE_LINK, _LINKABLE, frozenset({MERGED, DEPLOYED, SURVIVED, *DEFECT_STATES})),
    # A lane failure on a settled-at-queued coder fails its still-pending row; the re-dispatch opens a new one.
    Transition(EDGE_FAIL, frozenset({PENDING}), frozenset({FAILED})),
    Transition(EDGE_ABANDON, frozenset({PENDING}), frozenset({ABANDONED})),
    Transition(EDGE_SETTLE, frozenset({PENDING}), frozenset({SETTLED})),
    # A task settled from `system_failed` after its attempt already ended: the delivered work reopens that row.
    Transition(EDGE_RECOVER, frozenset({ABANDONED, FAILED}), frozenset({PENDING})),
    # Analysis inheritance (D5): survived, the worst child defect, or failed when no build child merged — and again
    # when a child's result later changes (a revert found after the analysis row inherited `survived`).
    Transition(
        EDGE_INHERIT, frozenset({SETTLED, SURVIVED, FAILED, *DEFECT_STATES}), frozenset({SURVIVED, *DEFECT_STATES, FAILED}),
    ),
)

_BY_EDGE: dict[str, Transition] = {t.edge: t for t in TRANSITIONS}


class IllegalTransition(ValueError):
    """A writer tried a move `TRANSITIONS` does not allow."""


def check(edge: str, source: str, target: str) -> None:
    """Refuse a move the table does not hold; a no-op move (`source == target`) is always allowed."""
    if source == target:
        return
    rule = _BY_EDGE.get(edge)
    if rule is None or source not in rule.sources or target not in rule.targets:
        raise IllegalTransition(f"outcome edge {edge!r} cannot move {source!r} to {target!r}")


def allowed(edge: str, source: str) -> bool:
    """Whether `edge` may fire from `source` at all."""
    rule = _BY_EDGE.get(edge)
    return rule is not None and source in rule.sources


def is_live(state: str, confidence: Decimal | float | None) -> bool:
    """The single LIVE-link rule."""
    if state == "confirmed":
        return True
    return state == "proposed" and confidence is not None and Decimal(str(confidence)) > LIVE_CONFIDENCE


def worst(states: list[str]) -> str | None:
    """The worst defect among `states`, by `DEFECT_STATES` order, or None."""
    for state in DEFECT_STATES:
        if state in states:
            return state
    return None
