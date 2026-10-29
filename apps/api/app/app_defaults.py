"""Out-of-the-box branding default: the title an org that has never chosen one reads.

Single home for the literal used by `AppSettings`' ORM `default`/`server_default` and
`routes/settings.py::app_title_for`. The column's DDL default is a SQL literal in
`migrations/versions/202610251700_baseline.py` and is not derived from this; changing the value here
needs a migration to reach the database (no drift test catches it). `app/config.py`'s `APP_TITLE` is a separate
process setting, deliberately not collapsed into this per-org default."""
from __future__ import annotations

DEFAULT_APP_TITLE = "Jaah"


def default_app_title() -> str:
    return DEFAULT_APP_TITLE
