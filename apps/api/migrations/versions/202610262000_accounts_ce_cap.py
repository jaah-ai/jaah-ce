"""Accounts: the community edition's account cap renamed `accounts_local_cap` -> `accounts_ce_cap`

Revision 202610252247's trigger, function and constraint name, under the edition's new name. The same AFTER INSERT
trigger and the same literal 3 (`app/ce_caps.py`); only the names and the error text change. `routes/accounts.py`
maps the error to the cap's 409 by `ce_caps.TRIGGER_CONSTRAINT`, which this revision's constraint name matches.
Created only on the community edition (`migrations/_edition.py::ce`); the cloud gets nothing.

Revision ID: 202610262000
Revises: 202610261500
"""
from __future__ import annotations

# additive-check: exempt — CE-only objects (the cloud runs nothing here), and a CE install is one api container that
# upgrades in place, so no outgoing release reads the old trigger while this one runs.

from typing import Sequence, Union

from alembic import op

from migrations._edition import ce

revision: str = "202610262000"
down_revision: Union[str, None] = "202610261500"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _function_sql(name: str, edition_label: str) -> str:
    """The cap's function under `name`: every insert counts under one transaction-scoped lock, so two concurrent
    inserts at 2 rows cannot both pass."""
    return f"""
CREATE OR REPLACE FUNCTION public.{name}() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  PERFORM pg_advisory_xact_lock(hashtext('{name}'));
  IF (SELECT count(*) FROM public.accounts) > 3 THEN
    RAISE EXCEPTION '{edition_label} allows 3 accounts'
      USING ERRCODE = 'check_violation', CONSTRAINT = '{name}';
  END IF;
  RETURN NULL;
END
$$
"""


def _install(name: str, edition_label: str) -> None:
    op.execute(_function_sql(name, edition_label))
    op.execute(f"CREATE OR REPLACE TRIGGER trg_{name} AFTER INSERT ON public.accounts "
               f"FOR EACH ROW EXECUTE FUNCTION public.{name}()")


def _drop(name: str) -> None:
    op.execute(f"DROP TRIGGER IF EXISTS trg_{name} ON public.accounts")
    op.execute(f"DROP FUNCTION IF EXISTS public.{name}()")


def upgrade() -> None:
    if not ce():
        return
    _drop("accounts_local_cap")
    _install("accounts_ce_cap", "Jaah Community Edition")


def downgrade() -> None:
    if not ce():
        return
    _drop("accounts_ce_cap")
    _install("accounts_local_cap", "Jaah Community Edition")
