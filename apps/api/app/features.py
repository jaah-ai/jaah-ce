"""The feature layer — hierarchy level 3: a named body of work inside one project, closed by a person.

Project › Feature › Task. A feature lives in exactly one project for its whole life (no move). A task's
feature is optional (`tasks.feature_id` NULL is "No feature"), and a task in a feature is always in that
feature's project: the composite foreign key `tasks (feature_id, project_id) -> features (id, project_id)`
is the whole mechanism, and the task-resolve trigger (revision 0300) keeps the pair legal when a writer
moves a task's project without naming a feature. Access is the task keys at the feature's project
(`authz.FEATURE_SPEC`); there is no permission model of its own.

Every name the revision, `app/models/` and the catalog tests share is spelled once, here.
"""
from __future__ import annotations

# An entity (the standard uuid entity shape, no slug: nothing routes to a feature by name, and names are not unique).
FEATURES_TABLE = "features"

# The two lifecycle values. `shipped` is a person's call ("Mark shipped"), never derived from task states.
FEATURE_ACTIVE = "active"
FEATURE_SHIPPED = "shipped"

# The name bound, held by the DDL check and the route alike.
FEATURE_NAME_MAX = 200
FEATURE_DESCRIPTION_MAX = 2000

# ── Every constraint and index this layer owns, named once ────────────────────────────────────────────
#
FEATURE_LEAD_FK = f"fk_{FEATURES_TABLE}_lead_id"
FEATURE_SHIPPED_BY_FK = f"fk_{FEATURES_TABLE}_shipped_by_id"
# The target of `TASK_FEATURE_PROJECT_FK` (revision 0297): Postgres needs a unique index over exactly the
# referenced pair.
FEATURE_ID_PROJECT_UNIQUE = f"uq_{FEATURES_TABLE}_id_project"
# "The active features of one project" — the rail and the pickers (revision 0300).
FEATURE_PROJECT_STATUS_INDEX = f"ix_{FEATURES_TABLE}_project_status"
FEATURE_NAME_CHECK = f"ck_{FEATURES_TABLE}_name_length"
FEATURE_STATUS_CHECK = f"ck_{FEATURES_TABLE}_status"
# `shipped` iff it carries who and when; and the two stamps are set or cleared together.
FEATURE_SHIPPED_STAMP_CHECK = f"ck_{FEATURES_TABLE}_shipped_stamped"
FEATURE_SHIPPED_PAIR_CHECK = f"ck_{FEATURES_TABLE}_shipped_pair"
# `icon` is NULL or one of the 24 tokens (`models.FEATURE_ICONS`, revision 0307).
FEATURE_ICON_CHECK = f"ck_{FEATURES_TABLE}_icon"

# The DDL each check renders, shared by the model and the revision (the icon check's is
# `models.FEATURE_ICON_CHECK_SQL`: its revision writes the tokens as literals).
FEATURE_NAME_CHECK_SQL = f"char_length(btrim(name)) BETWEEN 1 AND {FEATURE_NAME_MAX}"
FEATURE_STATUS_CHECK_SQL = f"status IN ('{FEATURE_ACTIVE}', '{FEATURE_SHIPPED}')"
FEATURE_SHIPPED_STAMP_CHECK_SQL = (
    f"(status = '{FEATURE_SHIPPED}') = (shipped_at IS NOT NULL AND shipped_by_id IS NOT NULL)"
)
FEATURE_SHIPPED_PAIR_CHECK_SQL = "(shipped_at IS NULL) = (shipped_by_id IS NULL)"

# ── The task side ─────────────────────────────────────────────────────────────────────────────────────
# A task in a feature is in that feature's project (revision 0297), `ON DELETE SET NULL (feature_id)`.
TASK_FEATURE_PROJECT_FK = "fk_tasks_feature_project"
# Partial: most tasks carry no feature, and "the tasks of one feature" is the only question asked of it.
TASK_FEATURE_INDEX = "ix_tasks_feature_id"
