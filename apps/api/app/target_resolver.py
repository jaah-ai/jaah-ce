"""Target rules -> targets: the one place an automation's live rule is turned
into the projects or nodes it reaches.

A rule is resolved at each firing (D3), so `all` and `all_except` pick up a project created later without an edit.
Both the editor's preview (`routes/automations.py`) and the fire engine read the same two functions:

* `universe(session, org_id, kind)` — everything the org's automations may target: its projects
  (never a system project), or exactly the nodes placement allows the org (AC2.3) —
  `placement_grants.org_floor` minus the nodes co-located on another org's host (`foreign_hosted_among`). A
  disabled node stays in the universe (`enabled=False`): it is listed, and skipped at fire.
* `resolve(session, automation)` — the rule applied to the universe: who fires, who is skipped and why, and which
  stored ids no longer name anything in it (`dangling`; never dropped silently, AC3.7).

`resolve` runs with no caller: it uses the org's full universe, and the fire engine asks the owner's WRITE on each
project per target. The console's project roster additionally narrows to what the caller can see.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Iterable, Optional
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import edition_features
from app.automations import MODE_ALL, MODE_ALL_EXCEPT, MODE_SPECIFIC, TARGET_NODES, TARGET_PROJECTS
from app.entity_shape import optional_uuid

#: Why a target in the universe is not fired (the sentence the ledger records).
REASON_NODE_DISABLED = "node disabled"


@dataclass(frozen=True)
class TargetItem:
    kind: str
    id: int | UUID
    name: str
    enabled: bool = True


@dataclass
class Resolution:
    fire: list[TargetItem] = field(default_factory=list)
    skipped: list[tuple[TargetItem, str]] = field(default_factory=list)
    dangling: list[str] = field(default_factory=list)


def target_key(kind: str, value: Any) -> str:
    """The one spelling an id is compared and stored in: a project's int as text, a node's uuid lower-cased."""
    return str(value).lower() if kind == TARGET_NODES else str(int(value))


def normalize_ids(kind: str, ids: Iterable[Any]) -> list[Any]:
    """The ids as the wire and `automations.target_ids` hold them: project ints, node uuid strings; order kept,
    repeats dropped. A value that is neither raises `ValueError` naming it."""
    out: list[Any] = []
    seen: set[str] = set()
    for value in ids:
        if kind == TARGET_PROJECTS:
            if isinstance(value, bool) or not isinstance(value, (int, str)) or not str(value).strip().isdigit():
                raise ValueError(f"project id {value!r} is not a number")
            normalized: Any = int(value)
        else:
            parsed = optional_uuid(value)
            if parsed is None:
                raise ValueError(f"node id {value!r} is not a node id")
            normalized = str(parsed)
        key = target_key(kind, normalized)
        if key not in seen:
            seen.add(key)
            out.append(normalized)
    return out


def universe(session: Session, org_id: UUID, kind: str) -> list[TargetItem]:
    """Everything an automation of `org_id` may target, by name."""
    if kind == TARGET_PROJECTS:
        from app.models.projects import Project

        rows = session.execute(
            select(Project.id, Project.name)
            .where(Project.org_id == org_id, Project.system_kind.is_(None))
            .order_by(Project.name, Project.id)
        ).all()
        return [TargetItem(kind=TARGET_PROJECTS, id=row.id, name=row.name) for row in rows]
    if kind == TARGET_NODES:
        from app import placement_grants, tenant_defaults
        from app.models.nodes import Node

        floor = placement_grants.org_floor(session, "node", org_id)
        hosts: set[UUID] = set()
        if edition_features.enabled("multi_node_hosts"):  # The community edition has no Multi-Node Hosts.
            from app import vnodes

            # A Multi-Node Host runs no work (`vnodes.refuse_work_on_host`), so it is no target either.
            hosts = {node_id for node_id, fact in vnodes.hosting_facts(session, floor).items() if fact.is_host}
        allowed = set(floor) - set(tenant_defaults.foreign_hosted_among(session, floor)) - hosts
        if not allowed:
            return []
        rows = session.execute(
            select(Node.id, Node.name, Node.enabled).where(Node.id.in_(allowed)).order_by(Node.name, Node.id)
        ).all()
        return [TargetItem(kind=TARGET_NODES, id=row.id, name=row.name, enabled=bool(row.enabled)) for row in rows]
    raise ValueError(f"no target kind {kind!r}; expected {TARGET_PROJECTS!r} or {TARGET_NODES!r}")


def apply_rule(items: list[TargetItem], kind: str, mode: str, ids: Iterable[Any]) -> Resolution:
    """The rule over an already-read universe — pure, so the preview and the firing cannot disagree."""
    by_key = {target_key(kind, item.id): item for item in items}
    wanted = [target_key(kind, value) for value in ids]
    resolution = Resolution()
    if mode == MODE_ALL:
        chosen = list(items)
    elif mode == MODE_SPECIFIC:
        chosen = [by_key[key] for key in wanted if key in by_key]
    elif mode == MODE_ALL_EXCEPT:
        excluded = set(wanted)
        chosen = [item for key, item in by_key.items() if key not in excluded]
    else:
        raise ValueError(f"no target mode {mode!r}")
    resolution.dangling = [key for key in wanted if key not in by_key]
    for item in chosen:
        if item.enabled:
            resolution.fire.append(item)
        else:
            resolution.skipped.append((item, REASON_NODE_DISABLED))
    return resolution


def resolve(session: Session, automation: Any) -> Resolution:
    """The automation's rule applied to its org's universe now. An automation whose action takes no target
    resolves to nothing."""
    kind: Optional[str] = automation.target_kind
    if kind is None:
        return Resolution()
    return apply_rule(
        universe(session, automation.org_id, kind), kind, automation.target_mode, automation.target_ids or []
    )
