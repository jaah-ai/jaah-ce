"""Accounts: the local edition's account cap as an AFTER INSERT trigger

The control plane's second layer under `POST /api/accounts`'s check (`app/local_caps.py`): any insert path — seed,
portfolio, a direct SQL insert — past 3 rows in total (any state or provider) is refused. AFTER INSERT, so an
`ON CONFLICT DO NOTHING` upsert of a slug already present (the sign-in wizard's phase 2) inserts nothing and is
never refused. The error names `accounts_local_cap` as its constraint, which `routes/accounts.py` maps to the cap's
409. The 3 is a literal in the
function body: no setting, env var or row moves it (PRD §7). Created only on the community edition
(`migrations/_edition.py::ce`); the cloud gets nothing.

EXPAND ONLY: a trigger the outgoing release never reads.

Revision ID: 202610252247
Revises: 202610252210
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op

from migrations._edition import ce

revision: str = "202610252247"
down_revision: Union[str, None] = "202610252210"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

#: Every insert counts under one transaction-scoped lock, so two concurrent inserts at 2 rows cannot both pass.
FUNCTION_SQL = """
CREATE OR REPLACE FUNCTION public.accounts_local_cap() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  PERFORM pg_advisory_xact_lock(hashtext('accounts_local_cap'));
  IF (SELECT count(*) FROM public.accounts) > 3 THEN
    RAISE EXCEPTION 'Jaah Community Edition allows 3 accounts'
      USING ERRCODE = 'check_violation', CONSTRAINT = 'accounts_local_cap';
  END IF;
  RETURN NULL;
END
$$
"""
TRIGGER_SQL = (
    "CREATE OR REPLACE TRIGGER trg_accounts_local_cap AFTER INSERT ON public.accounts "
    "FOR EACH ROW EXECUTE FUNCTION public.accounts_local_cap()"
)


def upgrade() -> None:
    if not ce():
        return
    op.execute(FUNCTION_SQL)
    op.execute(TRIGGER_SQL)


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_accounts_local_cap ON public.accounts")
    op.execute("DROP FUNCTION IF EXISTS public.accounts_local_cap()")
