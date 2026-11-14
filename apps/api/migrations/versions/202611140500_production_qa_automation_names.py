"""Rename the generated Production QA incident automations: `Production QA/<project>: <kind>`

"Create default automations" (`app/routes/production_qa.py::DEFAULT_INCIDENT_AUTOMATIONS`) named its automations
`<project>: <kind>`, and the release-failure one `<project>: Deploy failed` before Deploy was renamed Release. With no
`/` they sat at the Automations tree's root, below whatever folder came before them, and read as that folder's. They
are now named `Production QA/<project>: <kind>`, which files them in a Production QA folder, and the release-failure
one says "Release failed", as its When column does. This renames the ones created before.

Only a generated row is renamed: not a system row, its description still the generated one, its event row a trigger
of the matching kind on a project, and its name exactly `<that project's name>: <old kind name>`. A name the user
changed no longer matches, so it is left alone. Its event trigger is renamed with it: every save copies the trigger's
name onto the automation (`app/automation_service.py::_apply`), so a trigger left at the old name would bring it back
at the next pause or description edit. The trigger is renamed only while its name still equals the automation's, and
before the automation, while the automation's old name still matches. A new name over 200 characters (`MAX_NAME`) is
not written: that row stays as it is.

Not renamed, and left at the Automations tree's root (harmless): a row whose project was renamed after it was
generated (its name no longer equals `<project name>: <kind>`), and a row an older release creates while both
releases overlap after this ran.

Data only and idempotent (a renamed row's name starts with `Production QA/`, so it no longer equals the old form).
The downgrade changes nothing: which rows this renamed is not recorded.

Revision ID: 202611140500
Revises: 202611140400
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op

revision: str = "202611140500"
down_revision: Union[str, Sequence[str], None] = "202611140400"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Frozen copies (a migration never imports the live code): the generated description, the folder, the name limit
# (`app/entity_shape.py::MAX_NAME`), and each (old name, trigger kind, new name) the generator has written.
_DESCRIPTION = "Files a task for each new production incident of this kind."
_FOLDER = "Production QA/"
_MAX_NAME = 200
_RENAMES = (
    ("Deploy failed", "deploy_failed", "Release failed"),
    ("Release failed", "deploy_failed", "Release failed"),
    ("Monitor incidents", "incident", "Monitor incidents"),
    ("Health breach", "health_breach", "Health breach"),
    ("Regressions and slowdowns", "incident", "Regressions and slowdowns"),
)


def upgrade() -> None:
    for old, kind, new in _RENAMES:
        renamed = f"'{_FOLDER}' || p.name || ': {new}'"
        # One org throughout: a trigger, its automation and its project never cross a tenant.
        match = (f"t.automation_id = a.id AND a.org_id = p.org_id AND t.org_id = p.org_id "
                 f"AND t.source_kind = '{kind}' AND NOT a.is_system "
                 f"AND a.description = '{_DESCRIPTION}' AND a.name = p.name || ': {old}' "
                 f"AND length({renamed}) <= {_MAX_NAME}")
        # The trigger first: its match reads the automation's name, still the old one until the second statement.
        op.execute(
            f"UPDATE task_triggers t SET name = {renamed} FROM automations a, projects p "
            f"WHERE p.id = t.project_id AND t.name = a.name AND {match}"
        )
        op.execute(
            f"UPDATE automations a SET name = {renamed} "
            f"FROM task_triggers t JOIN projects p ON p.id = t.project_id WHERE {match}"
        )


def downgrade() -> None:
    pass
