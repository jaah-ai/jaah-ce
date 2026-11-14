"""The reason sentence on every routing decision — THE ONE HOME of its words (architecture §4.4, R4a).

One or two full sentences a person reads: runners by display name, tiers by label, languages as written; never a
slug, an enum value or a config key (a test holds it). Shape: "<runner> (High) chosen: 0.82 expected survival per
dollar vs 0.61 for <other runner> (High) on similar Python tasks."
"""
from __future__ import annotations

from typing import Any, Optional

from sqlalchemy.orm import Session

from app import model_catalog
from app.agent_router import decisions

#: The sentence per fallback; `{runner}` is the precedence chain's runner in words.
FALLBACKS = {
    "persona_not_routed": "The router does not choose runners for {persona} tasks, so this one runs on {runner}.",
    "playbook": "This task is a playbook stage, so it runs on {runner} as its playbook says.",
    "human_pinned": "A person chose this task's runner or model, so it runs on {runner}.",
    "system_pinned": "An automation set this task's runner or model, so it runs on {runner}.",
    "vendor_bound": "This task is kept on the vendor it first ran on, so it runs on {runner}.",
    "mode_off": "Routing is in Shadow mode for this project, so the task runs on {runner}. The router only "
                "records what it would choose.",
    "allowed_set_one": "The router has only one runner to choose from for this task, so it runs on {runner}.",
    "data_below_minimum": "No runner has enough finished tasks like this one to compare yet, so it runs on {runner}.",
    "candidate_unavailable": "The runner the router chose could not be placed in time, so this task runs on "
                             "{runner}.",
    # Recorded as `candidate_unavailable` too: nothing the router may choose can be placed now.
    "none_placeable": "None of the runners the router may choose can be placed right now, so this task runs on "
                      "{runner}.",
}
RECOMMENDED = "The router recommends {pick}; the task runs on {runner} unless someone accepts it."
#: Appended when the exploration budget turned an exploratory pick into the best-known one.
EXPLORATION_CAPPED = ("Trying other runners is at this project's budget share, so the router chose the best runner "
                      "it knows.")
WOULD_CHOOSE = " The router would choose {pick}."
REVIEW_OTHER = "{pick} reviews this task because the project prefers a different vendor from the one that wrote it."
REVIEW_SAME = "No other vendor is available, so this review runs on {pick}, the same runner that wrote the task."
REVIEW_UNKNOWN = "{pick} reviews this task; which runner wrote it is not recorded, so any vendor may review it."

_LANGUAGE_WORDS = {
    "typescript": "TypeScript", "javascript": "JavaScript", "csharp": "C#", "fsharp": "F#", "cpp": "C++",
    "objective-c": "Objective-C", "php": "PHP", "sql": "SQL", "html": "HTML", "css": "CSS", "yaml": "YAML",
    "json": "JSON", "toml": "TOML", "xml": "XML", "r": "R", "powershell": "PowerShell",
}
_TIER_LABELS = {choice.value: choice.label for choice in model_catalog.MODELS}


def runner(session: Session, cfg: dict[str, Any]) -> str:
    """A Config in words: the runner's display name, and its tier's label when it names one."""
    name = decisions.describe(session, cfg)
    tier = cfg.get("tier")
    return f"{name} ({_TIER_LABELS[tier]})" if tier in _TIER_LABELS else name


def language_words(language: Optional[str]) -> str:
    if not language:
        return ""
    return _LANGUAGE_WORDS.get(language, language.capitalize()) + " "


def fallback(session: Session, why: str, default: dict[str, Any], *, persona: str = "",
             pick: Optional[dict[str, Any]] = None) -> str:
    """The sentence for a fallback; Shadow mode also says what the router would have chosen when it differs."""
    from app.persona_registry import PERSONA_LABELS

    words = PERSONA_LABELS.get(persona, "these").lower()
    text = FALLBACKS[why].format(runner=runner(session, default), persona=words)
    if why == "mode_off" and pick is not None and decisions.config_key(pick) != decisions.config_key(default):
        text = text.replace(" The router only records what it would choose.", WOULD_CHOOSE.format(
            pick=runner(session, pick)))
    return text


def recommended(session: Session, pick: dict[str, Any], default: dict[str, Any]) -> str:
    return RECOMMENDED.format(pick=runner(session, pick), runner=runner(session, default))


def review(session: Session, pick: dict[str, Any], *, other: bool, author_known: bool = True) -> str:
    if not author_known:
        return REVIEW_UNKNOWN.format(pick=runner(session, pick))
    return (REVIEW_OTHER if other else REVIEW_SAME).format(pick=runner(session, pick))


def chosen(session: Session, pick: dict[str, Any], pick_score: Optional[float], *, language: Optional[str],
           prior: bool, rival: Optional[dict[str, Any]] = None, rival_score: Optional[float] = None,
           exploratory: bool = False) -> str:
    """The sentence for a pick the router acts on: its expected survival per dollar against the best rival's."""
    where = f"on similar {language_words(language)}tasks"
    if prior:
        where += " across all organizations, as this one has too few"
    name = runner(session, pick)
    if exploratory and pick_score is None:
        return f"{name} chosen to learn how it does {where}: it has too few finished tasks to score yet."
    if rival is not None and rival_score is not None and pick_score is not None:
        lead = "chosen to keep learning" if exploratory else "chosen"
        return (f"{name} {lead}: {pick_score:.2f} expected survival per dollar vs {rival_score:.2f} for "
                f"{runner(session, rival)} {where}.")
    if pick_score is None:
        return f"{name} chosen {where}."
    return f"{name} chosen: {pick_score:.2f} expected survival per dollar {where}."
