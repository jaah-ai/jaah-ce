"""The org boundary — the tenancy axis and the classification that makes it total.

DELIBERATELY EMPTY OF RE-EXPORTS. `manifest.py` imports `app.models`, and an alembic revision that
wants only the well-known org ids must not drag the whole ORM in to get them — so `ids.py` is a
stdlib-only leaf and importing it costs nothing. Re-exporting the manifest here would undo that:
`from app.tenancy.ids import ORG_ZERO_ID` runs this module first.

Import the module you mean:
  * `app.tenancy.ids`      — the well-known org ids and the seed rows. No dependencies.
  * `app.tenancy.manifest` — the census: one verdict per mapped class.
"""
