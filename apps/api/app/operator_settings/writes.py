"""The writers: validating a submitted value and storing operator-tier and org-tier overrides."""
from __future__ import annotations

import math
import re
from typing import TYPE_CHECKING, Any, Optional
from uuid import UUID

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

from app import edition_features
from app.entity_shape import optional_uuid
from app.operator_settings import defaults
from app.operator_settings.catalog import _BY_KEY, STATE_KEY, in_edition
from app.operator_settings.errors import OperatorSettingsError
from app.operator_settings.knobs import Knob
from app.url_validation import https_url_problem

# One address of an `email_list` knob: a deliberately loose shape check (one `@`, a dotted host, no spaces).
_EMAIL_RE = re.compile(r"[^@\s,]+@[^@\s,]+\.[^@\s,]+")


def _validate_value(knob: Knob, value: Any) -> Any:
    """`value` coerced/checked against `knob`'s declared kind and bounds, or `OperatorSettingsError`
    naming the knob by its label (every console surface shows the sentence as is) and the accepted range.
    Never clamps (unlike the read side): an explicit submission
    out of range is rejected so the UI can say why."""
    if knob.kind == "int":
        if isinstance(value, bool) or not isinstance(value, int):
            raise OperatorSettingsError(f"{knob.label} must be a whole number.")
        if knob.minimum is not None and value < knob.minimum:
            raise OperatorSettingsError(f"{knob.label} must be {int(knob.minimum):,} or more.")
        if knob.maximum is not None and value > knob.maximum:
            raise OperatorSettingsError(f"{knob.label} must be {int(knob.maximum):,} or less.")
        return value
    if knob.kind == "float":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise OperatorSettingsError(f"{knob.label} must be a number.")
        numeric = float(value)
        if not math.isfinite(numeric):
            raise OperatorSettingsError(f"{knob.label} must be a finite number.")
        if knob.minimum is not None and numeric < knob.minimum:
            raise OperatorSettingsError(f"{knob.label} must be {knob.minimum:g} or more.")
        if knob.maximum is not None and numeric > knob.maximum:
            raise OperatorSettingsError(f"{knob.label} must be {knob.maximum:g} or less.")
        return numeric
    if knob.kind == "bool":
        if not isinstance(value, bool):
            raise OperatorSettingsError(f"{knob.label} must be on or off.")
        return value
    # "str"
    if not isinstance(value, str):
        raise OperatorSettingsError(f"{knob.label} must be text.")
    if knob.command_line:
        if "\n" in value or "\r" in value or "\0" in value:
            raise OperatorSettingsError(
                f"{knob.label} must be one command on one line — a line break would run a second, ungated "
                "command."
            )
        value = value.strip()
    if knob.https_url and not (knob.blank_ok and value == ""):
        # Before the length bounds, so a blank or `http://` value is refused in words, not by its length.
        problem = https_url_problem(value)
        if problem is not None:
            raise OperatorSettingsError(f"{knob.label} {problem}.")
    if knob.email_list:
        for address in (part.strip() for part in value.split(",")):
            if address and not _EMAIL_RE.fullmatch(address):
                raise OperatorSettingsError(
                    f"{knob.label}: “{address[:80]}” is not an email address. Separate addresses with commas."
                )
    if knob.single_email and value.strip():
        # The one home for "exactly one address".
        from app.mail_message import single_address

        if single_address(value) is None:
            raise OperatorSettingsError(f"{knob.label}: “{value[:80]}” is not one bare email address.")
    if knob.email_local_part and value.strip():
        from app.system_mail import LOCAL_PART_PATTERN

        # The reader lower-cases (`system_mail.from_local`), so `Notifications` is the same address.
        if not LOCAL_PART_PATTERN.fullmatch(value.strip().lower()):
            raise OperatorSettingsError(
                f"{knob.label}: “{value[:80]}” is not a local part — letters, digits, “.”, “_” or “-”, "
                "e.g. notifications."
            )
    if knob.domain_list:
        # Stored canonical, so a reader compares exact strings. Local import: `app.access` is a heavy module.
        from fastapi import HTTPException

        from app.access import normalized_domain

        domains: list[str] = []
        for part in (part.strip() for part in value.split(",")):
            if not part:
                continue
            try:
                domain = normalized_domain(part)
            except HTTPException:
                raise OperatorSettingsError(
                    f"{knob.label}: “{part[:80]}” is not a domain. Separate domains with commas."
                ) from None
            if domain not in domains:
                domains.append(domain)
        value = ",".join(domains)
    if knob.data_regions:
        from app import data_residency  # Local: it reads this module's knobs.

        try:
            value = data_residency.canonical(value)
        except ValueError as exc:
            raise OperatorSettingsError(f"{knob.label}: {exc}") from None
    if knob.minimum is not None and len(value) < knob.minimum:
        raise OperatorSettingsError(f"{knob.label} must be at least {int(knob.minimum)} characters long.")
    if knob.maximum is not None and len(value) > knob.maximum:
        raise OperatorSettingsError(f"{knob.label} must be at most {int(knob.maximum)} characters long.")
    if knob.choices is not None and value not in knob.choices:
        # A closed set cannot be corrected toward safety, so the message lists every choice, by the words the
        # console shows when the knob has them (else its operational identifiers, safe to echo in the 422).
        raise OperatorSettingsError(f"{knob.label} must be one of: {', '.join(knob.choice_labels or knob.choices)}.")
    if knob.instance_type and value:
        # Local import (see `effective_settings`). Blank stays valid: it means "the first allowed type".
        if not edition_features.enabled("cloud_nodes"):
            raise OperatorSettingsError(f"{knob.label} must be blank: Jaah Community Edition has no instance types.")
        from app import cloud_node_launch, instance_types

        allowed = cloud_node_launch.allowed_instance_types()
        if value not in allowed:
            raise OperatorSettingsError(
                f"{knob.label} must be blank or one of the allowed instance types: "
                f"{', '.join(allowed) or f'(none — allow one in {instance_types.NOT_ALLOWED_HINT})'}."
            )
    return value


def apply_overrides(session: Session, values: dict[str, Any], *, org: Optional[UUID] = None) -> None:
    """Validate every `{key: value_or_null}` pair in `values`, then write. All-or-nothing: one bad key
    raises before anything is touched. `null` clears that key's override. The dict is replaced whole,
    never mutated in place, or SQLAlchemy misses the change and `updated_at` does not fire.

    `org` is the writer's own and authorizes nothing (`routes/settings.py` decides tier access); it is
    used only so a credential-pointer knob names a credential that org can see (`_validated_credential`).
    Optional in the signature but mandatory for a pointer: omitting it is a refusal, never a
    skipped check (a required keyword would be ceremony for every other knob; a skip would fail open).

    Does not invalidate the cache: the PUT route does after a successful commit, so a rolled-back write
    never appears to take effect."""
    if not isinstance(values, dict):
        raise OperatorSettingsError("values must be a JSON object")

    planned: list[tuple[Knob, Optional[Any]]] = []
    for key, value in values.items():
        knob = _BY_KEY.get(key)
        if knob is None or not in_edition(knob):
            raise OperatorSettingsError(f"unknown setting '{key}'")
        if value is None and not knob.data_regions:
            planned.append((knob, None))
            continue
        # A cleared region list is the empty one, and must not drop a region in use either (`refuse_dropped`).
        checked = "" if value is None else _validate_value(knob, value)
        if knob.credential_type is not None:
            if org is None:
                # Fail-closed (see the docstring). Phrased for a developer: the one production caller,
                # `routes/settings.py`, passes the actor's org.
                raise OperatorSettingsError(
                    f"{knob.key} names a credential, so it can only be written by a caller that "
                    "supplies its own org — the pointer is checked against the credentials that org "
                    "owns before it is stored"
                )
            checked = _validated_credential(session, org, knob, checked)
        if knob.plan and checked and checked not in defaults._active_plans(session):
            raise OperatorSettingsError(f"{knob.label} must be one of the active plans in Products › Plans.")
        if knob.project and checked and checked not in defaults.project_choices():
            raise OperatorSettingsError(f"{knob.label} must name an existing project.")
        if knob.data_regions:
            from app import data_residency  # Local: it reads this package's knobs.

            try:
                data_residency.refuse_dropped(session, checked)
            except ValueError as exc:
                raise OperatorSettingsError(f"{knob.label}: {exc}") from None
            if value is None:
                planned.append((knob, None))
                continue
        if knob.cloud_connection:
            if org is None:
                # The same fail-closed door as a credential pointer: a connection is a pointer into an
                # org-scoped table, checked against the writer's own org before it is stored.
                raise OperatorSettingsError(
                    f"{knob.key} names a cloud connection, so it can only be written by a caller that "
                    "supplies its own org — the pointer is checked against the connections that org owns "
                    "before it is stored"
                )
            checked = _validated_connection(session, org, knob, checked)
        planned.append((knob, checked))

    from app.models.control_plane import SystemState

    kv_row = session.get(SystemState, STATE_KEY)
    kv_values: dict[str, Any] = dict(kv_row.value) if kv_row is not None and isinstance(kv_row.value, dict) else {}

    for knob, value in planned:
        if value is None:
            kv_values.pop(knob.key, None)
        else:
            kv_values[knob.key] = value

    if kv_row is None:
        kv_row = SystemState(key=STATE_KEY, value={})
        session.add(kv_row)
    kv_row.value = kv_values  # whole-object replace — see the docstring above

    session.commit()


def _validated_connection(session: Session, org: UUID, knob: Knob, raw: str) -> str:
    """`raw` if it names a cloud connection of `org`, else refuse — `_validated_credential`'s rule for a
    `cloud_connection` pointer. Blank is the knob's own "unconfigured" value. Not a liveness or
    ownership check on the connection's credential: that is the consumer's
    (`app/hosting_provisioning.py::_hosting_connection`)."""
    from sqlalchemy import select as _select

    from app.models.credentials import CloudConnection

    if not str(raw).strip():
        return ""
    unusable = OperatorSettingsError(f"{knob.label} must name one of this organization's cloud connections.")
    connection_id = optional_uuid(raw)
    if connection_id is None:
        raise unusable
    row = session.scalar(
        _select(CloudConnection.id).where(CloudConnection.id == connection_id, CloudConnection.org_id == org)
    )
    if row is None:
        raise unusable
    return raw


def _validated_credential(session: Session, org: UUID, knob: Knob, raw: str) -> str:
    """`raw` if it names a credential of `org` and of one of `knob`'s declared types, else refuse.

    The write door on every credential pointer, at both tiers (an unchecked operator-tier
    `git_app_credential_id` once cost the fleet its git access). Not the only guard: `routes/settings.py` decides
    whose configuration this is; this decides which row a pointer may name. At the org tier it stops org A
    pointing at org B's row (neither resolver adds its own `org_id` predicate).
    Not a liveness check: that stays the consumer's question. One message for both refusals, so it is not a
    cross-org existence oracle.

    The writer's own org at both tiers, deliberately: `GET /api/credentials` is org-scoped, so the picker
    only offers own-org rows, and `PUT /api/settings` is org-scoped too, so a wider `IN` would be a no-op
    under the read seam anyway. Residual: the deployment's own credentials must live in the org its
    operators occupy (`tenancy/ids.py`)."""
    from sqlalchemy import select as _select

    from app.models.credentials import Credential

    # Blank is the knob's own "unconfigured" value (`default=""`): it names no row, so there is nothing to
    # authorize. The Settings picker sends exactly `""` when cleared, which must not be refused.
    if not str(raw).strip():
        return ""
    unusable = OperatorSettingsError(
        f"{knob.label} must name one of this organization's credentials of type "
        f"{', '.join(knob.credential_type or ())}."
    )
    credential_id = optional_uuid(raw)
    if credential_id is None:
        raise unusable
    row = session.scalar(
        _select(Credential).where(Credential.id == credential_id, Credential.org_id == org)
    )
    if row is None or row.type not in (knob.credential_type or ()):
        raise unusable
    # The control plane opens every knob-named credential itself (D1, AC-5.3).
    from app import credential_consumers

    try:
        credential_consumers.refuse_if_nodevault(row, credential_consumers.KNOB_LABELS.get(knob.key, knob.label))
    except credential_consumers.NodeVaultUnavailable as exc:
        raise OperatorSettingsError(str(exc)) from None
    return raw


def apply_org_overrides(session: Session, org: UUID, values: dict[str, Any]) -> None:
    """The org tier's writer: `apply_overrides`' twin, for one org.

    Same contract: validated before anything is touched (all-or-nothing), `null` clears, out-of-range is
    refused (clamping is the read side's rule). It also refuses a knob whose `tier` is not `"org"`, since
    the resolver would never read it ("saves cleanly and changes nothing"). The caller invalidates the
    cache after commit; `invalidate_cache()` clears both tiers."""
    from sqlalchemy import select

    from app.models.orgs import OrgSetting

    if not isinstance(values, dict):
        raise OperatorSettingsError("values must be a JSON object")

    planned: list[tuple[Knob, Optional[Any]]] = []
    for key, value in values.items():
        knob = _BY_KEY.get(key)
        if knob is None or not in_edition(knob):
            raise OperatorSettingsError(f"unknown setting '{key}'")
        if knob.tier != "org":
            raise OperatorSettingsError(
                f"'{key}' is an operator-global setting and cannot be overridden per org"
            )
        if value is None:
            planned.append((knob, None))
            continue
        checked = _validate_value(knob, value)
        if knob.credential_type is not None:
            checked = _validated_credential(session, org, knob, checked)
        if knob.plan and checked and checked not in defaults._active_plans(session):
            raise OperatorSettingsError(f"{knob.label} must be one of the active plans in Products › Plans.")
        if knob.cloud_connection:
            checked = _validated_connection(session, org, knob, checked)
        planned.append((knob, checked))

    # No fleet-wide escape here, unlike the reads: a writer is reached from a request with a bound scope,
    # and widening would raise `TenancyScopeConflict`. `write_seam.stamp_org` only refuses a mismatch for an
    # org-bound caller; on the operator plane nothing is checked. So a route adding a per-org settings page
    # owes its own authorization of `org`; this function authorizes which credential a pointer names, never
    # whose row is written.
    existing = {
        row.key: row
        for row in session.scalars(select(OrgSetting).where(OrgSetting.org_id == org)).all()
    }
    for knob, value in planned:
        row = existing.get(knob.key)
        if value is None:
            if row is not None:
                session.delete(row)
            continue
        if row is None:
            # `org_id` set explicitly: the row's org is the argument, not the caller's bound scope.
            session.add(OrgSetting(org_id=org, key=knob.key, value=value))
        else:
            row.value = value
    session.commit()
