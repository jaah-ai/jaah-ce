"""The org label a response carries — one wire shape and one resolver for every tenant-owned surface.

Routes import `OrgRef`/`org_refs` rather than re-spelling them. `routes/tenancy_exclusions.py::
TenancyExclusionOrgOut` is deliberately separate (a filter-option list without `slug`). `slug` is what
the indicator draws, `name` its tooltip and accessible name; `id` is for keying, never rendered.
`OrgRef` stays optional: `org_id` has no foreign key to `orgs(id)`, and `org_refs` answers `None`
rather than guessing the default org — a confident wrong tenant is worse than an honest blank. It
resolves only the ids asked about, since the read seam does not constrain `orgs`.
"""

from __future__ import annotations

from typing import Iterable, Mapping, Optional
from uuid import UUID

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models.orgs import Org


class OrgRef(BaseModel):
    """The org a row belongs to, as a response carries it. Mirrored in `apps/web/src/types.ts::OrgRef`."""

    id: UUID
    slug: str
    name: str


def org_refs(session: Session, org_ids: Iterable[Optional[UUID]]) -> dict[UUID, OrgRef]:
    """Resolve the given org ids to labels. Unknown and NULL ids are simply absent from the result.

    Call once per list, then `org_ref_of` per row; per-row calls are an N+1.

    An id with no `orgs` row is dropped rather than raised on: `org_id` has no foreign key to
    `orgs(id)` (see `app/models/base.py::OrgScoped`), so a dangling value is representable. It renders as the
    same blank a NULL does.
    """
    wanted = {org_id for org_id in org_ids if org_id is not None}
    if not wanted:
        return {}
    rows = session.execute(
        select(Org.id, Org.slug, Org.name).where(Org.id.in_(wanted))
    ).all()
    return {row.id: OrgRef(id=row.id, slug=row.slug, name=row.name) for row in rows}


def org_ref_of(refs: Mapping[UUID, OrgRef], org_id: Optional[UUID]) -> Optional[OrgRef]:
    """One row's label out of the map `org_refs` built. `None` in, `None` out — see the NULL note above."""
    return None if org_id is None else refs.get(org_id)
