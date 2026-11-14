"""Pipeline save history (Project settings › Pipeline › History): every save of the stages, steps and freezes, and
every Restore, is one `pipeline_revisions` row holding who, when, and the whole setup before and after, written in
the save's own transaction under the project row's lock (`add`), so a save and its row land together or not at all.

A snapshot is `pipeline.read`'s editable part (`snapshot`): the stages (id, name, branch, address), the step into
each later stage (who, approvals, by hand or by itself) and the freezes. A save records one row only when the
snapshot changed (`add`), so a Save that changes nothing leaves no row. A row's diff (`diff`) is plain sentences
computed from its pair on read, never stored. Restore writes a row's `after` back through the same save
(`restore_body`), a stage or freeze deleted since coming back as a new one; it is itself recorded, kind `restore`.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Optional
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.access_catalog import ROLES_BY_SLUG
from app.actors import actor_names
from app.models.identity import AdminUser
from app.models.projects import Project
from app.models.releases import PipelineRevision
from app.releases import freezes as freezes_mod, pipeline, stages as stages_mod


def snapshot(session: Session, project: Project, fields: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """The project's editable pipeline setup as the session sees it (`pipeline.read`'s stages, steps and freezes).
    `fields`: the project columns a save is about to write (`pipeline.save`'s `before_commit`), read in place of the
    project row's, so a snapshot taken inside the save shows the integration branch and prefix it writes."""
    shown = pipeline.read(session, project)
    fields = fields or {}

    def branch(stage: dict[str, Any], index: int) -> str:
        if stage["level"] == stages_mod.WORK and "branch_prefix" in fields:
            return f"{fields['branch_prefix'] or ''}*"
        if index == first_branch and "integration_branch" in fields:
            return str(fields["integration_branch"])
        return str(stage["branch"])

    first_branch = next((i for i, s in enumerate(shown["stages"]) if s["level"] == stages_mod.BRANCH), None)
    return {
        "stages": [{"id": s["id"], "name": s["name"], "branch": branch(s, i), "url": s["url"]}
                   for i, s in enumerate(shown["stages"])],
        "steps": [{"who": s["who"], "approvals": s["approvals"], "auto": s["auto"],
                   **{k: s[k] for k in pipeline.RULE_FIELDS}} for s in shown["steps"]],
        "freezes": [
            {k: f[k] for k in ("id", "name", "starts_at", "ends_at", "weekly", "timezone", *pipeline.FREEZE_RULE_FIELDS)}
            for f in shown["freezes"]
        ],
    }


def add(session: Session, project: Project, actor: Optional[AdminUser], before: dict[str, Any],
        after: dict[str, Any], *, restored_from: Optional[int] = None) -> Optional[PipelineRevision]:
    """Add the row for the save that turned `before` into `after` to the save's own transaction (the caller commits
    both together); None (no row) when nothing changed."""
    if after == before:
        return None
    row = PipelineRevision(
        org_id=project.org_id, project_id=project.id, kind="restore" if restored_from is not None else "save",
        restored_from_id=restored_from, created_by_id=actor.id if actor is not None else None,
        before=before, after=after,
    )
    session.add(row)
    session.flush()
    return row


# ── The diff ─────────────────────────────────────────────────────────────────────────────────────────

def _who_text(who: Optional[dict[str, Any]], names: dict[UUID, str]) -> str:
    if who is None:
        return "the default roles"
    parts = [ROLES_BY_SLUG[r].name if r in ROLES_BY_SLUG else r for r in who.get("roles") or []]
    for user in who.get("users") or []:
        try:
            parts.append(names.get(UUID(str(user)), "a former member"))
        except ValueError:
            continue
    return ", ".join(parts) if parts else "nobody"


def _branch_text(branch: Optional[str]) -> str:
    return branch.strip() if branch and branch.strip() else "none"


def _when(value: str, tz_name: str, *, weekly: bool) -> str:
    try:
        local = datetime.fromisoformat(value).astimezone(freezes_mod.zone(tz_name))
    except ValueError:
        return value
    return local.strftime("%a %H:%M") if weekly else local.strftime("%b %-d %Y, %H:%M")


def freeze_text(freeze: dict[str, Any]) -> str:
    """`Weekend (Fri 18:00 – Mon 08:00 weekly, Europe/London)`."""
    weekly = bool(freeze.get("weekly"))
    tz_name = str(freeze.get("timezone") or "UTC")
    span = (f"{_when(freeze['starts_at'], tz_name, weekly=weekly)} – {_when(freeze['ends_at'], tz_name, weekly=weekly)}"
            f"{' weekly' if weekly else ''}")
    return f"{freeze['name']} ({span}, {tz_name})"


def _steps_by_stage(snap: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """`{stage id: the step into it}`."""
    stages = snap.get("stages") or []
    return {stages[i + 1]["id"]: step for i, step in enumerate(snap.get("steps") or []) if i + 1 < len(stages)}


def diff(before: dict[str, Any], after: dict[str, Any], names: dict[UUID, str]) -> list[str]:
    """What a save changed, as plain sentences in a stable order: stages, then steps, then freezes."""
    out: list[str] = []
    old = {s["id"]: s for s in before.get("stages") or []}
    new = {s["id"]: s for s in after.get("stages") or []}
    for stage in after.get("stages") or []:
        if stage["id"] not in old:
            out.append(f"Added stage {stage['name']}.")
    for stage in before.get("stages") or []:
        if stage["id"] not in new:
            out.append(f"Removed stage {stage['name']}.")
    for stage in after.get("stages") or []:
        was = old.get(stage["id"])
        if was is None:
            continue
        if was["name"] != stage["name"]:
            out.append(f"Renamed stage {was['name']} to {stage['name']}.")
        if _branch_text(was.get("branch")) != _branch_text(stage.get("branch")):
            out.append(f"{stage['name']}: branch {_branch_text(was.get('branch'))} → {_branch_text(stage.get('branch'))}.")
        if (was.get("url") or None) != (stage.get("url") or None):
            out.append(f"{stage['name']}: address {was.get('url') or 'none'} → {stage.get('url') or 'none'}.")
    kept_old = [s["id"] for s in before.get("stages") or [] if s["id"] in new]
    kept_new = [s["id"] for s in after.get("stages") or [] if s["id"] in old]
    if kept_old != kept_new:
        out.append("New order: " + " → ".join(s["name"] for s in after.get("stages") or []) + ".")
    old_steps, new_steps = _steps_by_stage(before), _steps_by_stage(after)
    for stage in after.get("stages") or []:
        was, now = old_steps.get(stage["id"]), new_steps.get(stage["id"])
        if was is None or now is None:
            continue
        into = f"Step into {stage['name']}"
        if not same_who(was["who"], now["who"]):
            out.append(f"{into}: who may do it {_who_text(was['who'], names)} → {_who_text(now['who'], names)}.")
        if was["approvals"] != now["approvals"]:
            out.append(f"{into}: approvals {was['approvals']} → {now['approvals']}.")
        if was["auto"] != now["auto"]:
            how = {False: "by hand", True: "by itself"}
            out.append(f"{into}: {how[bool(was['auto'])]} → {how[bool(now['auto'])]}.")
        # A revision written before the rules existed has none: it reads as their defaults, so no change.
        changed = [words for key, words in pipeline.RULE_FIELDS.items()
                   if key in was and key in now and was[key] != now[key]]
        if changed:
            out.append(f"{into}: changed {', '.join(changed)}.")
    old_f = {f["id"]: f for f in before.get("freezes") or []}
    new_f = {f["id"]: f for f in after.get("freezes") or []}
    for freeze in after.get("freezes") or []:
        was = old_f.get(freeze["id"])
        if was is None:
            out.append(f"Added freeze {freeze_text(freeze)}.")
        elif was != freeze:
            out.append(f"Changed freeze {freeze_text(was)} → {freeze_text(freeze)}.")
    for freeze in before.get("freezes") or []:
        if freeze["id"] not in new_f:
            out.append(f"Removed freeze {freeze_text(freeze)}.")
    return out


def same_who(a: Optional[dict[str, Any]], b: Optional[dict[str, Any]]) -> bool:
    """Whether two steps' `who` name the same roles and users, in any order (None: the default)."""
    if a is None or b is None:
        return a is b
    return all(set(map(str, a.get(k) or [])) == set(map(str, b.get(k) or [])) for k in ("roles", "users"))


def _users_in(*snaps: dict[str, Any]) -> set[UUID]:
    ids: set[UUID] = set()
    for snap in snaps:
        for step in snap.get("steps") or []:
            for user in (step.get("who") or {}).get("users") or []:
                try:
                    ids.add(UUID(str(user)))
                except ValueError:
                    continue
    return ids


def revisions_out(session: Session, rows: list[PipelineRevision]) -> list[dict[str, Any]]:
    """`GET …/pipeline/history` items: who, when, the kind and the diff's sentences."""
    names = actor_names(session, {r.created_by_id for r in rows} | _users_in(*(s for r in rows for s in (r.before, r.after))))
    return [
        {
            "id": r.id, "kind": r.kind, "restored_from_id": r.restored_from_id,
            "created_at": r.created_at.isoformat(),
            "created_by": None if r.created_by_id is None else {
                "id": str(r.created_by_id), "name": names.get(r.created_by_id, "Unknown")},
            "changes": diff(r.before, r.after, names),
        }
        for r in rows
    ]


# ── Restore ──────────────────────────────────────────────────────────────────────────────────────────

def restore_body(session: Session, project: Project, revision: PipelineRevision) -> dict[str, Any]:
    """`revision.after` as a pipeline save's body (`routes/releases.py::PipelineSaveIn`): a stage or freeze still
    stored keeps its id, one deleted since is sent as new; a named user no longer in the org is dropped (the save
    refuses an unknown user, and a departed one could never act), and a step left naming nobody takes its default."""
    current = snapshot(session, project)
    stage_ids = {s["id"] for s in current["stages"]}
    freeze_ids = {f["id"] for f in current["freezes"]}
    target = revision.after
    users = _users_in(target)
    members = set(session.scalars(
        select(AdminUser.id).where(AdminUser.id.in_(users), AdminUser.org_id == project.org_id)
    )) if users else set()

    def who(value: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
        if value is None:
            return None
        kept = {"roles": list(value.get("roles") or []),
                "users": [u for u in value.get("users") or [] if _is_member(u, members)]}
        # Everyone it named has left: the step's default roles, never a step nobody may take.
        return kept if kept["roles"] or kept["users"] else None

    return {
        "stages": [
            {**({"id": s["id"]} if s["id"] in stage_ids else {}), "name": s["name"], "branch": s.get("branch") or "",
             "url": s.get("url")}
            for s in target.get("stages") or []
        ],
        "steps": [{"who": who(s.get("who")), "approvals": s["approvals"], "auto": s["auto"],
                   **{k: s[k] for k in pipeline.RULE_FIELDS if k in s},
                   **({"approvers": who(s["approvers"])} if s.get("approvers") is not None else {})}
                  for s in target.get("steps") or []],
        "freezes": [_freeze_back(f, f["id"] in freeze_ids, stage_ids) for f in target.get("freezes") or []],
    }


def _freeze_back(freeze: dict[str, Any], stored: bool, stage_ids: set[str]) -> dict[str, Any]:
    """One freeze of a restored save: its rules as saved, its stages less any removed since (none left: every
    stage after integration, today's default, rather than a freeze over nothing)."""
    out = {**({"id": freeze["id"]} if stored else {}),
           **{k: freeze[k] for k in ("name", "starts_at", "ends_at", "weekly", "timezone")},
           **{k: freeze[k] for k in pipeline.FREEZE_RULE_FIELDS if k in freeze}}
    if out.get("scope") == "stages":
        out["stage_ids"] = [s for s in out.get("stage_ids") or [] if s in stage_ids]
        if not out["stage_ids"]:
            out["scope"] = "after_integration"
    return out


def _is_member(user: Any, members: set[UUID]) -> bool:
    try:
        return UUID(str(user)) in members
    except ValueError:
        return False
