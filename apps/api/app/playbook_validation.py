"""Playbook validation and derivation — pure functions over one playbook in the `PlaybookIn` wire shape.

`validate(pb)` returns every `Issue` the playbook has: an `error` blocks Save, a `warning` does not (D7).
`derive(pb)` returns, per stage, the facts the editor shows beside it — model tier, mode, when it ends,
what it receives and hands off, what it may file.

THE ONE HOME of every rule: `app/routes/playbooks.py` runs the same `validate()` on create, update,
`/validate` and every read, so a saved playbook shows the warnings it was saved with. The route's Pydantic
models check JSON SHAPE only; every membership question (is this a known expert, deliverable, seniority,
key) is an `Issue` here, so the console gets a sentence naming the stage rather than a schema error.
Name uniqueness needs the database and is the route's (`name_taken_issue`).

A chosen-at-start stage's deliverable follows the expert picked (D3), so every rule that depends on the
deliverable is checked once per allowed expert and a hit names the expert.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass
from typing import Any, Iterable, Optional

from app import playbook_vocabulary as v

ERROR, WARNING = "error", "warning"

KEY_GRAMMAR = "^[a-z][a-z0-9_]{0,39}$"
_KEY_RE = re.compile(KEY_GRAMMAR)
MAX_NAME = 120
MAX_DESCRIPTION = 2000
MAX_LABEL = 80
MAX_EXTRA_INSTRUCTIONS = 8000
MAX_STAGES = 30
# Postgres text and JSONB hold no NUL, and a control character renders as nothing: refuse them all
# except tab, newline and return.
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


@dataclass(frozen=True)
class Issue:
    stage: Optional[str]
    stage_index: Optional[int]
    field: Optional[str]
    severity: str
    message: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def has_errors(issues: Iterable[Issue]) -> bool:
    return any(issue.severity == ERROR for issue in issues)


def name_taken_issue(name: str) -> Issue:
    return Issue(
        None, None, "name", ERROR,
        f'Another playbook in your organization is already called "{name.strip()}". Choose a different name.',
    )


# ── Resolution helpers ───────────────────────────────────────────────────────────────────────────────


def _stages(pb: dict[str, Any]) -> list[dict[str, Any]]:
    stages = pb.get("stages")
    return stages if isinstance(stages, list) else []


def _is_chosen(stage: dict[str, Any]) -> bool:
    return (stage.get("expert") or {}).get("kind") == v.EXPERT_CHOSEN


def _experts(stage: dict[str, Any]) -> list[str]:
    expert = stage.get("expert") or {}
    if expert.get("kind") == v.EXPERT_CHOSEN:
        return list(expert.get("allowed") or [])
    return [expert["expert"]] if expert.get("expert") else []


def _default_expert(stage: dict[str, Any]) -> Optional[str]:
    expert = stage.get("expert") or {}
    return expert.get("default") if expert.get("kind") == v.EXPERT_CHOSEN else expert.get("expert")


def _options(stage: dict[str, Any]) -> list[tuple[Optional[str], Optional[str]]]:
    """`(expert, deliverable)` for every way this stage can run. A pinned stage has one option whose
    expert is None (a message about it need not name the expert); a chosen-at-start stage has one per
    allowed expert, its deliverable the expert's native one (D3)."""
    if _is_chosen(stage):
        return [
            (key, v.EXPERTS[key].native_deliverable) for key in _experts(stage) if key in v.EXPERTS
        ]
    return [(None, stage.get("deliverable"))]


def _default_option(stage: dict[str, Any]) -> tuple[Optional[str], Optional[str]]:
    if _is_chosen(stage):
        key = _default_expert(stage)
        record = v.EXPERTS.get(key or "")
        return key, record.native_deliverable if record else None
    return _default_expert(stage), stage.get("deliverable")


def _filing_set(deliverable: Optional[str]) -> tuple[str, ...]:
    record = v.DELIVERABLES.get(deliverable or "")
    return record.may_file if record else ()


def _may_file(stage: dict[str, Any]) -> tuple[str, ...]:
    """What this stage may file: its own `may_file`, or its deliverable's whole set when that is null."""
    may_file = stage.get("may_file")
    if may_file is None:
        return _filing_set(stage.get("deliverable"))
    return tuple(may_file)


def _tier(seniority: Optional[str], default: Optional[str]) -> Optional[str]:
    resolved = default if seniority == v.INHERIT else seniority
    return v.SENIORITY_TIERS.get(resolved or "")


def resolved_seniority(stage: dict[str, Any], pb: dict[str, Any]) -> Optional[str]:
    seniority = stage.get("seniority")
    return pb.get("default_seniority") if seniority == v.INHERIT else seniority


def _direct_dependencies(stages: list[dict[str, Any]], index: int) -> list[int]:
    """The indexes this stage waits on directly — `all_earlier` expands to every earlier stage; a key that
    names no stage is skipped (its error is reported separately)."""
    depends = stages[index].get("depends_on") or {}
    kind = depends.get("kind")
    if kind == v.DEPENDS_ALL_EARLIER:
        return list(range(index))
    if kind == v.DEPENDS_STAGES:
        positions = _positions(stages)
        return [positions[key] for key in depends.get("keys") or [] if key in positions]
    return []


def _positions(stages: list[dict[str, Any]]) -> dict[str, int]:
    """Each key's FIRST position (a duplicate key is its own error)."""
    positions: dict[str, int] = {}
    for index, stage in enumerate(stages):
        positions.setdefault(stage.get("key") or "", index)
    return positions


def _named(stage: dict[str, Any], index: int) -> str:
    """How a message names a stage: its label and key, or its position when it has neither."""
    key = (stage.get("key") or "")[:40]
    label = (stage.get("label") or "").strip()[:MAX_LABEL]
    if label and key:
        return f'Stage "{label}" ({key})'
    if label or key:
        return f'Stage "{label or key}"'
    return f"Stage {index + 1}"


def _with(expert: Optional[str]) -> str:
    return f" when {v.EXPERTS[expert].label} is chosen" if expert in v.EXPERTS else ""


def _storable(value: str) -> bool:
    if _CONTROL_RE.search(value):
        return False
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


# ── validate ─────────────────────────────────────────────────────────────────────────────────────────


def validate(pb: dict[str, Any]) -> list[Issue]:
    """Every issue in `pb`, playbook-level first, then stage by stage in order."""
    issues: list[Issue] = list(_playbook_issues(pb))
    stages = _stages(pb)
    if len(stages) > MAX_STAGES:
        return issues  # already refused; walking an oversized list is cost for nothing
    for index, stage in enumerate(stages):
        issues.extend(_stage_issues(pb, stages, index))
    issues.extend(_cycle_issues(stages))
    return issues


def _playbook_issues(pb: dict[str, Any]) -> Iterable[Issue]:
    def issue(field: str, message: str, severity: str = ERROR) -> Issue:
        return Issue(None, None, field, severity, message)

    name = (pb.get("name") or "").strip()
    if not name:
        yield issue("name", "Give the playbook a name.")
    elif len(name) > MAX_NAME:
        yield issue("name", f"The name is {len(name)} characters long; shorten it to {MAX_NAME} or fewer.")
    elif not _storable(name):
        yield issue("name", "The name contains a control character. Remove it.")
    description = pb.get("description") or ""
    if len(description) > MAX_DESCRIPTION:
        yield issue(
            "description",
            f"The description is {len(description)} characters long; shorten it to {MAX_DESCRIPTION} or fewer.",
        )
    elif not _storable(description):
        yield issue("description", "The description contains a control character. Remove it.")
    if pb.get("entry") not in v.ENTRY_LABELS:
        yield issue("entry", "Choose where the playbook starts from: New Task or New Chat.")
    if pb.get("default_seniority") not in v.SENIORITY_TIERS:
        yield issue("default_seniority", "Choose the playbook's default seniority: Junior, Mid or Senior.")
    stages = _stages(pb)
    if not stages:
        yield issue("stages", "Add at least one stage.")
    elif len(stages) > MAX_STAGES:
        yield issue("stages", f"A playbook holds at most {MAX_STAGES} stages; this one has {len(stages)}.")


def _stage_issues(pb: dict[str, Any], stages: list[dict[str, Any]], index: int) -> Iterable[Issue]:
    stage = stages[index]
    key = stage.get("key") or ""
    who = _named(stage, index)
    last = index == len(stages) - 1

    def issue(field: str, message: str, severity: str = ERROR) -> Issue:
        return Issue(key or None, index, field, severity, message)

    # Key and label.
    if not key:
        yield issue("key", f"{who} needs a key: lowercase letters, digits and underscores, starting with a letter.")
    elif not _KEY_RE.fullmatch(key):
        yield issue(
            "key",
            f'Stage {index + 1}: the key "{key[:40]}" is not usable. Use lowercase letters, digits and '
            "underscores, starting with a letter, 40 characters at most.",
        )
    elif _positions(stages)[key] != index:
        yield issue(
            "key",
            f'{who} uses the key "{key}", which stage {_positions(stages)[key] + 1} already uses. '
            "Give each stage its own key.",
        )
    label = (stage.get("label") or "").strip()
    if not label:
        yield issue("label", f"{who} needs a label.")
    elif len(label) > MAX_LABEL:
        yield issue("label", f"{who}: the label is {len(label)} characters long; shorten it to {MAX_LABEL} or fewer.")
    elif not _storable(label):
        yield issue("label", f"{who}: the label contains a control character. Remove it.")
    extra = stage.get("extra_instructions") or ""
    if len(extra) > MAX_EXTRA_INSTRUCTIONS:
        yield issue(
            "extra_instructions",
            f"{who}: the extra instructions are {len(extra)} characters long; shorten them to "
            f"{MAX_EXTRA_INSTRUCTIONS} or fewer.",
        )
    elif not _storable(extra):
        yield issue("extra_instructions", f"{who}: the extra instructions contain a control character. Remove it.")

    # Expert and deliverable.
    yield from _expert_issues(stage, who, issue)
    chosen = _is_chosen(stage)
    deliverable = stage.get("deliverable")
    if chosen and deliverable is not None:
        yield issue(
            "deliverable",
            f"{who} lets you choose the expert when the playbook starts, so its deliverable follows the "
            "expert. Clear the deliverable.",
        )
    elif not chosen and deliverable not in v.DELIVERABLES:
        yield issue("deliverable", f"{who} needs a deliverable. Choose one from the list.")

    # Enumerated forms.
    if stage.get("seniority") not in v.STAGE_SENIORITIES:
        yield issue("seniority", f"{who} needs a seniority: Playbook default, Junior, Mid or Senior.")
    runs = stage.get("runs")
    if runs not in v.RUNS_LABELS:
        yield issue("runs", f"{who} needs to say whether it runs once or once for each planned item.")
    if stage.get("interviews") not in v.INTERVIEWS_LABELS:
        yield issue("interviews", f"{who} needs to say whether it asks when Full Auto is off or never asks.")

    # may_file narrows, never widens.
    may_file = stage.get("may_file")
    if may_file is not None:
        if chosen:
            yield issue(
                "may_file",
                f"{who} lets you choose the expert when the playbook starts, so what it may file follows "
                "the expert's deliverable. Clear the filing limits.",
            )
        elif len(set(may_file)) != len(may_file):
            yield issue("may_file", f"{who} lists the same filing item twice. Remove the repeat.")
        elif deliverable in v.DELIVERABLES:
            allowed = _filing_set(deliverable)
            extra_items = [item for item in may_file if item not in allowed]
            if extra_items:
                named = ", ".join(v.FILING_LABELS.get(item, f'"{item}"') for item in extra_items)
                can = (
                    "files nothing" if not allowed
                    else "may file only: " + ", ".join(v.FILING_LABELS[item] for item in allowed)
                )
                yield issue(
                    "may_file",
                    f"{who} may not file {named}: a {v.DELIVERABLES[deliverable].label} stage {can}. "
                    "Remove the extra items.",
                )

    # Created and runs.
    yield from _created_issues(stages, index, who, issue)

    # The last stage must be able to settle (D6).
    if last:
        for expert, option in _options(stage):
            if option == v.PLAN:
                yield issue(
                    "deliverable",
                    f"{who} is the last stage and delivers a Plan{_with(expert)}, but a plan's items are "
                    "built by a later stage. Add the stage that runs once for each planned item after it.",
                )
        if runs == v.RUNS_PER_ITEM:
            yield issue(
                "runs",
                f"{who} is the last stage and runs once for each planned item, so nothing after it collects "
                "the items. Add a stage after it, or make this one run once.",
            )

    # Warnings.
    if len(stages) > 1:
        for expert, option in _options(stage):
            if option == v.CONVERSATION:
                yield issue(
                    "deliverable",
                    f"{who} is a Conversation{_with(expert)} inside a playbook with several stages. A "
                    "conversation ends only when the operator completes it, so the stages after it wait "
                    "for that.",
                    WARNING,
                )
    if resolved_seniority(stage, pb) == v.JUNIOR:
        if any(option == v.PLAN for _, option in _options(stage)):
            yield issue(
                "seniority",
                f"{who} plans the work at Junior seniority. Planning decides what every later stage builds; "
                "consider Mid or Senior.",
                WARNING,
            )
        if v.EXPERT_ARCHITECT in _experts(stage):
            yield issue(
                "seniority",
                f"{who} runs the Architect at Junior seniority. The design shapes every later stage; "
                "consider Mid or Senior.",
                WARNING,
            )

    # depends_on references.
    depends = stage.get("depends_on") or {}
    if depends.get("kind") == v.DEPENDS_STAGES:
        keys = depends.get("keys") or []
        positions = _positions(stages)
        if not keys:
            yield issue(
                "depends_on",
                f"{who} waits for chosen stages but names none. Choose at least one, or pick "
                '"Waits for nothing".',
            )
        elif len(set(keys)) != len(keys):
            yield issue("depends_on", f"{who} waits for the same stage twice. Remove the repeat.")
        for name in keys:
            if name == key:
                yield issue("depends_on", f"{who} waits for itself. Remove it from its own list.")
            elif name not in positions:
                yield issue(
                    "depends_on",
                    f'{who} waits for "{str(name)[:60]}", but no stage has that key. Choose an existing stage.',
                )


def _expert_issues(stage: dict[str, Any], who: str, issue) -> Iterable[Issue]:
    expert = stage.get("expert") or {}
    if expert.get("kind") == v.EXPERT_PINNED:
        if expert.get("expert") not in v.EXPERTS:
            yield issue("expert", f"{who} names an expert that does not exist. Choose one from the list.")
        return
    allowed = list(expert.get("allowed") or [])
    if not allowed:
        yield issue("expert", f"{who} lets you choose the expert but allows none. Allow at least one expert.")
        return
    unknown = [key for key in allowed if key not in v.EXPERTS]
    if unknown:
        yield issue(
            "expert",
            f"{who} allows an expert that does not exist ({', '.join(str(k)[:40] for k in unknown)}). "
            "Remove it.",
        )
    if len(set(allowed)) != len(allowed):
        yield issue("expert", f"{who} allows the same expert twice. Remove the repeat.")
    default = expert.get("default")
    if default not in allowed:
        yield issue(
            "expert",
            f"{who}: the default expert must be one of the allowed experts. Allow it, or pick another default.",
        )


def _created_issues(stages: list[dict[str, Any]], index: int, who: str, issue) -> Iterable[Issue]:
    stage = stages[index]
    created = stage.get("created") or {}
    runs = stage.get("runs")
    if created.get("kind") != v.CREATED_BY:
        if runs == v.RUNS_PER_ITEM:
            yield issue(
                "created",
                f"{who} runs once for each planned item, so an earlier Plan stage must create it. Set "
                '"Created by" to that Plan stage.',
            )
        return
    creator_key = created.get("stage")
    positions = _positions(stages)
    if creator_key not in positions:
        yield issue(
            "created",
            f'{who} is created by "{str(creator_key)[:60]}", but no stage has that key. Choose an earlier '
            "Plan stage.",
        )
        return
    creator_index = positions[creator_key]
    creator = stages[creator_index]
    creator_name = _named(creator, creator_index)
    if creator_index >= index:
        yield issue(
            "created",
            f"{who} is created by {creator_name}, which does not come before it. Only an earlier Plan "
            "stage can create a stage.",
        )
        return
    not_plan = [expert for expert, option in _options(creator) if option != v.PLAN]
    if not_plan:
        detail = (
            f" when {', '.join(v.EXPERTS[e].label for e in not_plan if e in v.EXPERTS)} is chosen"
            if _is_chosen(creator) else ""
        )
        yield issue(
            "created",
            f"{who} is created by {creator_name}, which does not deliver a Plan{detail}. Only a Plan stage "
            "can create other stages.",
        )
        return
    needed = v.FILE_PER_ITEM_STAGE if runs == v.RUNS_PER_ITEM else v.FILE_FINISH
    # A chosen-at-start creator files its option's whole set (its `may_file` must be null), so only a
    # pinned creator can have narrowed the edge away.
    if not _is_chosen(creator) and needed not in _may_file(creator):
        yield issue(
            "created",
            f"{who} is created by {creator_name}, but that stage may not file "
            f"{v.FILING_LABELS[needed].lower()}. Allow it on {creator_name}.",
        )


def _cycle_issues(stages: list[dict[str, Any]]) -> list[Issue]:
    """One error per stage caught in a waiting loop over `created: by` and `depends_on` edges."""
    edges: dict[int, list[int]] = {index: [] for index in range(len(stages))}
    positions = _positions(stages)
    for index, stage in enumerate(stages):
        edges[index].extend(_direct_dependencies(stages, index))
        created = stage.get("created") or {}
        if created.get("kind") == v.CREATED_BY and created.get("stage") in positions:
            edges[index].append(positions[created["stage"]])
    reported: set[int] = set()
    issues: list[Issue] = []
    state: dict[int, int] = {}  # 1 = on the current path, 2 = done
    # Iterative, so no stage count can exhaust the interpreter's recursion limit.
    for start in range(len(stages)):
        if state.get(start) is not None:
            continue
        state[start] = 1
        path = [start]
        stack = [iter(edges[start])]
        while stack:
            target = next(stack[-1], None)
            if target is None:
                stack.pop()
                state[path.pop()] = 2
                continue
            if target == path[-1]:
                continue  # waiting on itself is its own, clearer, error
            if state.get(target) == 1:
                loop = path[path.index(target):]
                chain = " → ".join((stages[i].get("key") or str(i + 1))[:40] for i in [*loop, loop[0]])
                for member in loop:
                    if member not in reported:
                        reported.add(member)
                        stage = stages[member]
                        issues.append(Issue(
                            stage.get("key") or None, member, "depends_on", ERROR,
                            f"{_named(stage, member)} is part of a waiting loop ({chain}): each stage waits "
                            "for the next, so none can start. Remove one of these dependencies.",
                        ))
            elif state.get(target) is None:
                state[target] = 1
                path.append(target)
                stack.append(iter(edges[target]))
    return sorted(issues, key=lambda issue: issue.stage_index)


# ── derive ───────────────────────────────────────────────────────────────────────────────────────────


def _hands_off(stage: dict[str, Any]) -> tuple[str, ...]:
    expert, deliverable = _default_option(stage)
    return v.hands_off(expert or "", deliverable or "")


def _ordered(kinds: Iterable[str]) -> list[str]:
    wanted = set(kinds)
    return [kind for kind in v.HAND_OFF_KINDS if kind in wanted]


def derive(pb: dict[str, Any]) -> list[dict[str, Any]]:
    """Per stage, in order: the `Derived` facts of the API contract. Best effort on an invalid playbook —
    a value that cannot be resolved is None or empty, never a raise."""
    stages = _stages(pb)
    out: list[dict[str, Any]] = []
    if len(stages) > MAX_STAGES:
        return out  # refused by `validate`; nothing to show beside a list that cannot be saved
    for index, stage in enumerate(stages):
        _, deliverable = _default_option(stage)
        record = v.DELIVERABLES.get(deliverable or "")
        derived: dict[str, Any] = {
            "tier": _tier(stage.get("seniority"), pb.get("default_seniority")),
            "mode": record.mode if record else None,
            "ends_when": record.ends_when if record else None,
            "deliverable_by_expert": {},
            "by_expert": {},
            "receives": _ordered(
                kind for dep in _direct_dependencies(stages, index) for kind in _hands_off(stages[dep])
            ),
            "hands_off": list(_hands_off(stage)),
            "may_file_options": [] if _is_chosen(stage) else list(_filing_set(stage.get("deliverable"))),
        }
        if _is_chosen(stage):
            for expert, option in _options(stage):
                option_record = v.DELIVERABLES[option]
                derived["deliverable_by_expert"][expert] = option
                derived["by_expert"][expert] = {
                    "mode": option_record.mode,
                    "ends_when": option_record.ends_when,
                    "hands_off": list(v.hands_off(expert, option)),
                }
        out.append(derived)
    return out
