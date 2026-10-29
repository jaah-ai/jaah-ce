"""The operator org (org zero): its fixed id and the row it is written from.

Single home: the baseline revision INSERTs `OPERATOR_ORG` and `app.seed`'s convergence re-creates it. It is
the one org the control plane treats specially; every other org is a tenant created from the console.

Stdlib only: an alembic revision imports this leaf and must not import the ORM.

Org zero is a row, never a NULL, so `org_id` can be NOT NULL everywhere. The id is fixed across
environments so a migration can name it without querying, and an operator recognizes it.
"""

from __future__ import annotations

from typing import NamedTuple
from uuid import UUID


class SeedOrg(NamedTuple):
    """The org row a fresh database is created with. `enabled` is not carried: a seed starts enabled."""

    id: UUID
    slug: str
    name: str
    description: str


# UUIDv7 by construction, in the same hand-assigned block as `SYSTEM_ACTOR_ID`.
ORG_ZERO_ID = UUID("01900000-0000-7000-8000-000000000010")

# Where a refused sign-in that resolved to NO user files its `login_failed` row
# (`app/login_funnel.py::record_login_failure`): an unknown email or a forged callback belongs to no tenant,
# and the operator plane owns the platform's sign-in security. A row the caller cannot attribute, never a
# tenant row filed by default — a refusal naming a user files under that user's org.
UNATTRIBUTED_SIGN_IN_ORG_ID = ORG_ZERO_ID


#: The operator plane's row: the one org the control plane protects (never disabled, deleted or re-slugged)
#: and the one boot re-creates (`app/seed.py::_seed_orgs`).
OPERATOR_ORG = SeedOrg(
    id=ORG_ZERO_ID,
    slug="operator",
    name="Operator",
    description=(
        "Org zero — the operator plane. Fleet machinery, operator staff and the system rows that "
        "run the control plane itself belong here. It is a row rather than a NULL so that every "
        "org_id can be NOT NULL."
    ),
)


#: The orgs whose superadmins are the deployment's operator rather than a tenant's admin: org zero alone.
#: A superadmin in any other org acts as an operator only from an org-zero seat (`app/seed.py`). Needed because `is_superadmin` carries no org. Not a
#: substitute for `Tenancy`: it says whose configuration the caller may write, never which rows it may read.
OPERATOR_PLANE_ORG_IDS: frozenset[UUID] = frozenset({ORG_ZERO_ID})


def is_tenant_org(org_id: UUID | None) -> bool:
    """Is `org_id` a tenant (a console-created org), never the deployment's operator?

    The org half of `is_operator_plane`, asked alone: operator staff without superadmin must still
    touch the operator's hosts under ordinary keys, so this keys on the org, not the principal.
    `None` is a tenant (fail-closed). A new reader calls this rather than re-spelling
    `OPERATOR_PLANE_ORG_IDS`."""
    return org_id is None or org_id not in OPERATOR_PLANE_ORG_IDS


def is_operator_plane(org_id: UUID | None, is_superadmin: bool) -> bool:
    """Is a superadmin in `org_id` the deployment's operator rather than a tenant's administrator?

    The one home for the verdict, both halves: the flag and the org. `is_superadmin` alone would hand
    a tenant's superadmin the fleet. It authorizes the fleet plane, never rows; a
    superadmin outside it keeps allow-all inside its own org. A new reader calls this rather than
    re-spelling the test."""
    return bool(is_superadmin) and org_id is not None and org_id in OPERATOR_PLANE_ORG_IDS
