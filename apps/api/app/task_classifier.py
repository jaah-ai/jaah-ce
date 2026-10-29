"""LLM task classification — the LLM half of Re-tag, and the one place a tag verdict is judged.

No fallback, unlike `session_summary.title_from_description`: a tag is data that drives the board's
grouping and looks exactly like an operator's pick, so every failure is an error the caller sees
(503/504/502), with no confidence field and no decline.

All-or-nothing before any write: `classify_task_tags` returns a complete validated selection or
raises; `app/task_tags.py::replace_task_tags` stays the single writer.

The word is `tag` everywhere (column, `TagRefOut.tag`, verdict key); "code" is prose only.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.llm_client import classify_timeout, first_text, summary_llm
from app.models.task_extras import Tag, TagGroup
from app.models.tasks import Task
from app.task_tags import TAG_GROUP_ACTIVE

log = logging.getLogger("task-classifier")

# One call's budget, sized for the thinking block, which spends it before the JSON; too small comes
# back `stop_reason: max_tokens` with no text. 4096 is DeepSeek's documented standard reply ceiling.
_MAX_TOKENS = 4096
# What a retry may spend. Retries must escalate: identical budgets overrun identically. 8192 is the
# documented beta ceiling; a model refusing it fails only the retry.
_MAX_TOKENS_CEILING = 8192


def budget_for_attempt(attempt: int) -> int:
    """The reply budget for `attempt` (1-based) — doubling, capped at `_MAX_TOKENS_CEILING`.

    `attempt <= 1` (the manual Re-tag button) gets the base budget, so a press does not bill the
    ceiling for the median task."""
    return min(_MAX_TOKENS * 2 ** max(0, attempt - 1), _MAX_TOKENS_CEILING)

_CLASSIFY_PROMPT = (
    "Classify ONE task into EVERY tag group listed below.\n\n"
    "RULES:\n"
    "- Answer for every group in the catalogue, exactly once each. Never invent a group.\n"
    "- Identify a tag by its `tag` value, copied verbatim — never its `name`, never an id.\n"
    '- A group with "multi_select": false takes EXACTLY ONE tag.\n'
    '- A group with "multi_select": true takes ONE OR MORE tags — every one that genuinely applies.\n'
    "- Every group has a catch-all tag, so a pick is always possible. There is no way to decline, no\n"
    "  confidence score, and no 'unclassified' answer.\n"
    "- Judge the task from its own text alone. Read `title`, `body` and, when present,\n"
    "  `outcome_summary` together — the outcome says what the work turned out to be.\n\n"
    "Output ONLY this JSON object, with no preamble and no markdown fence:\n"
    '{{"assignments": [{{"group_id": "<group_id from the catalogue>", "tags": ["<tag>"]}}]}}\n\n'
    "TAG GROUPS:\n{groups}\n\n"
    "TASK:\n{task}\n\n"
    "JSON:"
)


# Per-field prompt cap: `tasks.body` is unbounded `Text`, so without it one task can bill an arbitrary
# input per press or overflow the context window. Generous enough to touch no real body.
_MAX_FIELD_CHARS = 20_000


@dataclass(frozen=True)
class TaskDocument:
    """The task's own text, snapshotted off the ORM row.

    A plain value, not a `Task`: the route ends its read transaction before the LLM call, expiring ORM
    instances, and reading `task.body` then would silently reopen a transaction across the call."""

    title: str
    body: str
    outcome_summary: str | None

    @classmethod
    def from_task(cls, task: Task) -> "TaskDocument":
        return cls(
            title=task.title, body=task.body, outcome_summary=task.outcome_summary
        )


@dataclass(frozen=True)
class TagSpec:
    """One tag as the prompt describes it and as the verdict is checked against it."""

    id: UUID
    tag: str
    name: str
    description: str


@dataclass(frozen=True)
class GroupSpec:
    """One active, non-empty tag group: what the model is told, and what it must answer for.

    `id` (the group uuid) is what the verdict names; a positional index would re-point if the catalogue
    changed between prompt and reply."""

    id: UUID
    name: str
    description: str
    multi_select: bool
    tags: tuple[TagSpec, ...]


def active_groups(session: Session) -> list[GroupSpec]:
    """Every active tag group that has at least one tag, in the operator's order.

    A tag-less active group is skipped (deleting a group's last tag is still allowed); left in, every
    verdict would 502 on it. Order is `(position, name)` for groups and tags, matching the board;
    `name` tie-breaks because `position` defaults to 0 for raw-SQL writers."""
    rows = session.execute(
        select(
            TagGroup.id,
            TagGroup.name,
            TagGroup.description,
            TagGroup.multi_select,
            Tag.id,
            Tag.tag,
            Tag.name,
            Tag.description,
        )
        .join(Tag, Tag.tag_group_id == TagGroup.id)
        .where(TagGroup.state == TAG_GROUP_ACTIVE)
        .order_by(TagGroup.position, TagGroup.name, Tag.position, Tag.name)
    )
    ordered: list[UUID] = []
    groups: dict[UUID, dict] = {}
    for (
        group_id,
        group_name,
        group_description,
        multi_select,
        tag_id,
        tag_code,
        tag_name,
        tag_description,
    ) in rows:
        if group_id not in groups:
            ordered.append(group_id)
            groups[group_id] = {
                "name": group_name,
                "description": group_description,
                "multi_select": multi_select,
                "tags": [],
            }
        groups[group_id]["tags"].append(
            TagSpec(id=tag_id, tag=tag_code, name=tag_name, description=tag_description)
        )
    return [
        GroupSpec(
            id=group_id,
            name=groups[group_id]["name"],
            description=groups[group_id]["description"],
            multi_select=groups[group_id]["multi_select"],
            tags=tuple(groups[group_id]["tags"]),
        )
        for group_id in ordered
    ]


def catalogue_identity(groups: list[GroupSpec]) -> frozenset:
    """Which groups and which tags, and nothing else — what a verdict is answerable against.

    Not `==` on `GroupSpec`s: a mid-window rename or description edit does not invalidate a verdict,
    only a group or tag appearing or disappearing does. Sets, not sequences, so a mere reorder does not
    409 and position ties need no defined order. `multi_select` is absent: a flip is caught (422) by
    `replace_task_tags` under its lock."""
    return frozenset(
        (group.id, frozenset(tag.id for tag in group.tags)) for group in groups
    )


def build_prompt(document: TaskDocument, groups: list[GroupSpec]) -> str:
    """The exact string sent to the model: the catalogue, plus the task's own text.

    The task half carries `title`, `body` and `outcome_summary` (omitted when null), nothing else: PR
    url and no-PR evidence classify nothing, and board facets (state, persona, project, dates) would
    only be restated. JSON rather than prose layout, since descriptions are free text.

    Embedded free text can steer the model: a body carrying a well-formed verdict would be honoured.
    Nothing is gained, as the caller already holds `tasks:edit`."""
    catalogue = [
        {
            "group_id": str(group.id),
            "name": group.name,
            "description": group.description,
            "multi_select": group.multi_select,
            "tags": [
                {"tag": tag.tag, "name": tag.name, "description": tag.description}
                for tag in group.tags
            ],
        }
        for group in groups
    ]
    task_json: dict[str, str] = {
        "title": document.title[:_MAX_FIELD_CHARS],
        "body": document.body[:_MAX_FIELD_CHARS],
    }
    if document.outcome_summary is not None:
        task_json["outcome_summary"] = document.outcome_summary[:_MAX_FIELD_CHARS]
    return _CLASSIFY_PROMPT.format(
        groups=json.dumps(catalogue, indent=2, ensure_ascii=False),
        task=json.dumps(task_json, indent=2, ensure_ascii=False),
    )


def _decode_verdict(text: str) -> dict:
    """The first JSON object in `text`, or a 502 naming that nothing decodable came back.

    `raw_decode` from the first `{`, not brace-counting: free-text descriptions containing `}` would
    desync a counter. It also skips a leading markdown fence. The `except` needs both arms: a missing
    `{` or bad JSON is `ValueError`, deep nesting is `RecursionError`. Positioned on `{`, `raw_decode`
    returns a dict or raises, so no type guard."""
    try:
        verdict, _end = json.JSONDecoder().raw_decode(text, text.index("{"))
    except (ValueError, RecursionError) as exc:
        log.warning("retag: classifier reply carried no decodable JSON object: %s", exc)
        raise HTTPException(
            status_code=502,
            detail=(
                "the classifier returned a malformed reply — no JSON object could be read from it"
            ),
        ) from exc
    return verdict


def _reject(rule: str) -> HTTPException:
    """A 502 naming which rule the verdict broke, never a bare "invalid verdict", so the operator and
    the logs can see a prompt regression."""
    return HTTPException(status_code=502, detail=f"the classifier's verdict is invalid: {rule}")


def validate_verdict(verdict: dict, groups: list[GroupSpec]) -> list[UUID]:
    """The verdict → the complete list of `tags.id` to write, or a 502 naming the broken rule.

    All-or-nothing before any write: one bad tag rejects the whole verdict and existing tags stay.
    Rules: one entry per catalogue group, none twice, none outside, single-select exactly one tag,
    multi-select at least one, every tag a member of that group (sibling-group borrowing gets its own
    message)."""
    assignments = verdict.get("assignments")
    if not isinstance(assignments, list):
        raise _reject('"assignments" is missing or is not a list')

    seen: dict[UUID, list[str]] = {}
    by_id = {group.id: group for group in groups}
    for entry in assignments:
        if not isinstance(entry, dict):
            raise _reject(f"an assignment is a {type(entry).__name__}, not an object")
        raw_group_id = entry.get("group_id")
        try:
            group_id = UUID(str(raw_group_id))
        except (AttributeError, TypeError, ValueError):
            # Clipped: model-supplied text bound for an HTTP response.
            raise _reject(f'"group_id" {str(raw_group_id)[:64]!r} is not a uuid') from None
        if group_id not in by_id:
            raise _reject(
                f"group_id {group_id} is not one of the {len(groups)} active tag groups it was given"
            )
        if group_id in seen:
            raise _reject(f"tag group '{by_id[group_id].name}' was answered more than once")
        tags = entry.get("tags")
        if not isinstance(tags, list) or not all(isinstance(t, str) for t in tags):
            raise _reject(
                f"the tags for tag group '{by_id[group_id].name}' are not a list of strings"
            )
        # Duplicates collapse: naming a tag twice is the same answer, not two tags.
        seen[group_id] = list(dict.fromkeys(tags))

    missing = [group.name for group in groups if group.id not in seen]
    if missing:
        raise _reject(
            f"{len(missing)} of {len(groups)} active tag groups were not classified: "
            + ", ".join(f"'{name}'" for name in missing)
        )

    tag_ids: list[UUID] = []
    for group in groups:
        chosen = seen[group.id]
        if not chosen:
            raise _reject(f"tag group '{group.name}' was given no tag at all")
        if not group.multi_select and len(chosen) > 1:
            raise _reject(
                f"tag group '{group.name}' is single-select — {len(chosen)} tags were given for it"
            )
        # Case-folded and stripped; cannot collide because `routes/tags.py` normalizes tags on write and
        # they are unique per group. Refuses a tag not in the group, never its capitalization.
        members = {tag.tag.strip().lower(): tag.id for tag in group.tags}
        for code in chosen:
            member_id = members.get(code.strip().lower())
            if member_id is None:
                # Clipped: model-supplied text bound for an HTTP response.
                raise _reject(
                    f"tag '{code[:64]}' is not a tag of tag group '{group.name}' "
                    f"(its tags are {', '.join(sorted(members))})"
                )
            tag_ids.append(member_id)
    return tag_ids


def classify_task_tags(
    document: TaskDocument, groups: list[GroupSpec], *, max_tokens: int = _MAX_TOKENS
) -> list[UUID]:
    """Classify `document` into every group in `groups` and return the `tags.id` list to write.

    Takes no session and writes nothing, so the route can release its pool connection before the
    network call. Every failure raises first:

    | `groups` is empty (no active group, or every active group tag-less) | 409 |
    | no usable key for the support agent's provider — `summary_llm()` gave None | 503 |
    | the call failed: timeout, transport, credential, quota, oversized prompt | 504 |
    | the reply was truncated before the verdict, unparseable, or broke a rule | 502 |

    409, not an empty 200, which would look like "no tags apply". `max_tokens` is this call's budget
    (`budget_for_attempt` for retries)."""
    if not groups:
        raise HTTPException(
            status_code=409,
            detail=(
                "there is no active tag group with any tags to classify into — publish a tag group "
                "in Settings > Tasks > Tags first"
            ),
        )

    # Resolved once, so the call and every message about it name the same model and budget.
    timeout = classify_timeout()
    llm = summary_llm(timeout=timeout)
    if llm is None:
        raise HTTPException(
            status_code=503,
            detail="the task classifier is not configured (no model key for the support agent's provider)",
        )
    model, client = llm.model, llm.client

    prompt = build_prompt(document, groups)
    try:
        message = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            # A model that always thinks needs its effort; the budget stays this call's own.
            **{k: v for k, v in llm.request_options.items() if k != "max_tokens"},
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception as exc:
        # One arm for every call failure: the SDK is imported lazily, so its classes cannot be named.
        # The detail carries class name and upstream status, never `str(exc)` (it holds the response
        # body and request URL).
        status = getattr(exc, "status_code", None)
        # bool subclasses int.
        has_status = isinstance(status, int) and not isinstance(status, bool)
        cause = type(exc).__name__ + (f", upstream {status}" if has_status else "")
        # The log takes the clipped SDK text the detail refuses; logs are not a response.
        log.warning(
            "retag: classifier call failed (model=%s): %s: %.300s",
            model, type(exc).__name__, str(exc),
        )
        raise HTTPException(
            status_code=504,
            detail=(
                f"the task classifier call did not complete — model '{model}', "
                f"timeout {timeout:g}s ({cause})"
            ),
        ) from exc

    # Which provider and model the task text went to, never the text; `task_retag` logs task and actor.
    log.info("classifier: sent to %s (model=%s)", llm.provider, model)
    text = first_text(message)
    if not text:
        # Why there is no text is the diagnosis: `max_tokens` means the thinking spent the budget (fix:
        # a bigger budget), distinct from an empty answer. Log `stop_reason` unconditionally.
        stop_reason = getattr(message, "stop_reason", None)
        log.warning(
            "retag: classifier reply carried no text (model=%s, stop_reason=%s, budget=%d tokens)",
            model, stop_reason, max_tokens,
        )
        if stop_reason == "max_tokens":
            raise HTTPException(
                status_code=502,
                detail=(
                    f"the classifier ran out of reply budget before it answered — the model's "
                    f"thinking spent all {max_tokens} tokens (stop_reason=max_tokens)"
                ),
            )
        raise HTTPException(
            status_code=502,
            detail="the classifier returned a malformed reply — it carried no text at all",
        )
    return validate_verdict(_decode_verdict(text), groups)
