"""Resolved views over the catalogue: the create-form prefills, the Settings page rows and the live picker choices."""
from __future__ import annotations

from typing import TYPE_CHECKING, Any, Optional
from uuid import UUID

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

from app import edition_features
from app.operator_settings import reads
from app.operator_settings.catalog import CATALOG, in_edition


def task_create_defaults(org: Optional[UUID]) -> dict[str, Any]:
    """The three `Tasks > Defaults` knobs, resolved into what a create surface seeds an untouched form
    with. Rides on `GET /api/auth/me` (`routes/auth.py::AdminOut`), not `GET /api/settings`.

    The catalogue needs `system:view` but creating a task needs `tasks:create`, so a creator without
    operator capability fell back to hard-coded seeds (fail-open for the autonomy switch). The identity
    payload reaches every principal. Still only a prefill (see `apps/web/src/components/tasks/taskDefaults.ts`);
    the keys are spelled only in `CATALOG`.

    `project_memory` is NOT a prefill: the editor names it in its "Default (On)" option, and a row left on
    Default stays null and is resolved at dispatch (`project_memory/switch.py`).

    All four are `tier="org"`, so the principal's org is passed: on the identity payload an unscoped read
    raising would take login down for everyone."""
    return {
        "autonomous": reads.get_bool("task_default_autonomous", org=org),
        "complex": reads.get_bool("task_default_complex", org=org),
        "persona": reads.get_str("task_default_persona", org=org),
        "project_memory": reads.get_bool("project_memory_default", org=org),
    }


def project_create_defaults(org: Optional[UUID]) -> dict[str, str]:
    """The four `Projects > Defaults` knobs, resolved into the gate and stack commands a new project is
    created with (`routes/projects.py`'s create path). Also on `GET /api/auth/me` (`routes/auth.py::AdminOut`), for
    `task_create_defaults`' reason: a project creator need not hold `system:view`.

    A seed, never re-applied: a project's own gate and stack fields are its own from creation on. Blank
    means no command. All are `tier="org"`, so the creator's org is passed; `org=None` is the operator-tier
    read, which `_refuse_unscoped_read` refuses once any org overrides a knob."""
    return {
        "gate_quick_cmd": reads.get_str("project_default_gate_quick_cmd", org=org),
        "gate_full_cmd": reads.get_str("project_default_gate_full_cmd", org=org),
        "stack_up_cmd": reads.get_str("project_default_stack_up_cmd", org=org),
        "stack_down_cmd": reads.get_str("project_default_stack_down_cmd", org=org),
    }


# ── GET-shaped view ──────────────────────────────────────────────────────────────────────────────────

def effective_settings(session: Session, *, org: Optional[UUID] = None) -> list[dict[str, Any]]:
    """The catalogue (but a knob whose feature this edition lacks, `catalog.in_edition`), each entry annotated with its live `value`, its `fallback`, and whether it is
    currently `overridden`. Feeds `GET /api/settings`'s `settings` array.

    With no org, the operator's view (`_operator_candidates`). With an org, for an org-tier knob: `value`
    is what governs this org (the same `_stored_candidates` chain the accessors read, so the page and the
    New-task prefill agree), `fallback` what it would inherit with its override cleared, `overridden`
    whether the org holds its own row. Operator-tier knobs keep the operator view
    (`routes/settings.py::_settings_out` withholds them from tenants).

    `org_overrides(org)` runs on its own fleet-wide session, like every org-tier read here."""
    ov = reads.overrides(session)
    out: list[dict[str, Any]] = []
    for knob in CATALOG:
        if not in_edition(knob):
            continue
        if org is not None and knob.tier == "org":
            # One snapshot for both fields, so a concurrent write cannot pair `overridden: true` with the
            # operator's value, and a rejected org row is not reported as the org's own.
            cands = reads._stored_candidates(knob, org)
            value = reads._resolve_typed(knob, cands)
            fallback = reads._typed_value(knob)
            overridden = bool(cands) and cands[0][0] == "org" and reads._resolve_typed(knob, cands[:1]) == value
        else:
            value = reads._typed_value(knob)
            fallback = reads._fallback(knob)
            overridden = knob.key in ov
        choices = list(knob.choices) if knob.choices else None
        choice_labels = list(knob.choice_labels) if knob.choice_labels else None
        if knob.instance_type:
            # Local import: `cloud_node_launch` imports this module. The choices are the live allowlist,
            # plus a stored type the list no longer names, so the page shows the truth rather than a
            # select that silently reads as another type. Labels are the tokens themselves: "t4g.micro"
            # must not be humanised. `value` and `fallback` are what a reader resolves (the top of the list
            # when blank).
            stored = str(value or "").strip()
            # The community edition has no instance types: the only choice is what is stored.
            choices = [stored] if stored else []
            if edition_features.enabled("cloud_nodes"):
                from app import cloud_node_launch

                choices = cloud_node_launch.allowed_instance_types(session)
                if stored and stored not in choices:
                    choices.append(stored)
                value = cloud_node_launch.resolved_instance_type(stored)
                # Reset previews a real choice: the blank default, resolved the same way.
                fallback = cloud_node_launch.resolved_instance_type(str(fallback or ""))
            choice_labels = list(choices)
        if knob.plan:
            # Blank (the `(default)` row) and the active plans, read live.
            plans = {"": "(default)", **_active_plans(session)}
            # A stored key no active plan names reads as blank, as its reader takes it (`plan_terms.lapse_pick`).
            if str(value or "") not in plans:
                value = ""
            choices, choice_labels = list(plans), list(plans.values())
        choice_notes = list(knob.choice_notes) if knob.choice_notes else None
        if knob.project and org is None:
            # Operator view only (a tenant is never served an operator knob). Every org's projects, read live, behind an explicit "Not set"; a stored id that names no row stays
            # a choice worded as such. Both states carry the note, so the page says alarms are being dropped.
            projects = project_choices()
            stored = str(value or "")
            unfiled = "No alarm is filed until this names a project."
            choices, choice_labels, choice_notes = [""], ["Not set"], [unfiled]
            if stored and stored not in projects:
                choices.append(stored)
                choice_labels.append("Unknown project")
                choice_notes.append(unfiled)
            choices += list(projects)
            choice_labels += list(projects.values())
            choice_notes += [""] * len(projects)
        out.append({
            "key": knob.key,
            "label": knob.label,
            "help": knob.help,
            "kind": knob.kind,
            "env": knob.env,
            "default": knob.default,
            "minimum": knob.minimum,
            "maximum": knob.maximum,
            "unit": knob.unit,
            # The tenancy verdict (see `routes/settings.py::SettingKnobOut.tier`), emitted on every knob.
            "tier": knob.tier,
            "group": knob.group,
            "section": knob.section,
            # A list: JSON has no tuple, and the page compares against the authored shape.
            "choices": choices,
            # A list for the same reason; the page filters the credential roster against it.
            "credential_type": list(knob.credential_type) if knob.credential_type else None,
            "choice_labels": choice_labels,
            "choice_notes": choice_notes,
            "vnode_host": knob.vnode_host,
            "instance_type": knob.instance_type,
            "plan": knob.plan,
            "cloud_connection": knob.cloud_connection,
            "data_regions": knob.data_regions,
            "tenant_editable": knob.tenant_editable,
            "value": value,
            "fallback": fallback,
            "overridden": overridden,
        })
    return out


def _active_plans(session: Session) -> dict[str, str]:
    """`{key: name}` of the active plans in catalogue order — a `plan` knob's choices."""
    from sqlalchemy import select as _select

    from app.models.billing import BillingPlan

    rows = session.execute(
        _select(BillingPlan.key, BillingPlan.name).where(BillingPlan.active.is_(True))
        .order_by(BillingPlan.sort_order, BillingPlan.key)
    ).all()
    return {key: name for key, name in rows}


def project_choices() -> dict[str, str]:
    """`{str(id): "Org / project"}` of every project in every org, by org then name — a `project` knob's choices.
    Not org zero alone: the operator's own projects may live in its company org (dev and prod keep `jaah` there),
    and the org in the label is what tells two same-named projects apart.

    On the fleet-wide plane (`_org_plane`), not the caller's session: an operator's GET binds `OPERATOR` but its
    PUT binds its own org (`authz._principal_scope`), so the save refused every project the picker offered
    outside org zero. Operator-only: a `project` knob is operator-tier, served and saved by the
    operator alone (`routes/settings.py`)."""
    from sqlalchemy import select as _select

    from app.db import SessionLocal
    from app.models.orgs import Org
    from app.models.projects import Project

    detached, operator = reads._org_plane()
    with detached, operator, SessionLocal() as session:
        rows = session.execute(
            _select(Project.id, Org.name, Project.name).join(Org, Org.id == Project.org_id)
            .order_by(Org.name, Project.name)
        ).all()
    return {str(project_id): f"{org_name} / {name}" for project_id, org_name, name in rows}
