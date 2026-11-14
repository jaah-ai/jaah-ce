"""Re-point `org_bundled_products.comp_id` from `org_comps` at the product engine's comp lines

A comp copy names its comp by `comp_id`. Every comp is now read from `product_lines` (owner `comp`), whose ids are
the legacy comps' own (the line mirror writes both with one id), so the key moves there unchanged: the old key to
`org_comps` goes and one to `product_lines` comes, with the same name and ON DELETE CASCADE.

The new key is DEFERRABLE INITIALLY DEFERRED: the previous release, still serving while this one deploys, writes a
legacy comp and its copy first and catches the engine line up later in the same transaction, so the key is checked
at commit. It is added NOT VALID and validated when every copy's comp line exists (a copy of a comp the engine has
not caught up yet leaves it unvalidated, still enforced for every new row).

Revision ID: 202611141700
Revises: 202611141600
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op

# additive-check: exempt — drops only the foreign key over comp_id to org_comps and adds it to product_lines (same ids); no column or table goes, and the deferred key accepts the previous release's writes.

revision: str = "202611141700"
down_revision: Union[str, Sequence[str], None] = "202611141600"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Frozen copies (a migration never imports the live code).
_TABLE = "org_bundled_products"
_FK = "fk_org_bundled_products_comp_id"


def upgrade() -> None:
    op.execute(f"ALTER TABLE {_TABLE} DROP CONSTRAINT IF EXISTS {_FK}")
    op.execute(
        f"ALTER TABLE {_TABLE} ADD CONSTRAINT {_FK} FOREIGN KEY (comp_id) REFERENCES product_lines (id) "
        "ON DELETE CASCADE DEFERRABLE INITIALLY DEFERRED NOT VALID"
    )
    op.execute(f"""
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM {_TABLE} b
                WHERE b.comp_id IS NOT NULL AND NOT EXISTS (SELECT 1 FROM product_lines l WHERE l.id = b.comp_id)
            ) THEN
                ALTER TABLE {_TABLE} VALIDATE CONSTRAINT {_FK};
            ELSE
                RAISE NOTICE '{_FK} left NOT VALID: a comp copy names a comp with no product_lines row';
            END IF;
        END $$
    """)


def downgrade() -> None:
    op.execute(f"ALTER TABLE {_TABLE} DROP CONSTRAINT IF EXISTS {_FK}")
    op.execute(
        f"ALTER TABLE {_TABLE} ADD CONSTRAINT {_FK} FOREIGN KEY (comp_id) REFERENCES org_comps (id) "
        "ON DELETE CASCADE NOT VALID"
    )
    # Left NOT VALID: a comp line written since (the dev seed without billing) may have no legacy row.
