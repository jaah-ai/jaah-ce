"""Refresh the starter templates (`app/task_template_starters.py`): two new starters, test-suite merge, counters

* Inserts "Prepare project for Jaah" and "Deploy to hosting (CI/CD)", published with their built-in images.
* "Speed up the test suite" becomes "Improve test suite" (same id, so its starts carry over) and "Fix flaky tests" is
  deleted; "Clean up code" gets its longer brief. Each only where the row still holds every 202610281600 seed value
  frozen below: an operator's edit stays as the operator left it. Any row left on a retired image shows the merged one. The delete also skips a starter an automation still
  starts, and the rename one whose new slug another global template already holds.
* Adds 20 starts, dated today, to Prepare project for Jaah, Deploy to hosting, Clean up code, Improve test suite and
  Security review, so Most popular leads with them until real starts (90-day window) take over.

On a fresh database 202610281600 already seeded the current module, so the updates match nothing and the inserts
conflict. Additive: data only; an older release reads the same columns.

Revision ID: 202610291100
Revises: 202610290030
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
from sqlalchemy import text

revision: str = "202610291100"
down_revision: Union[str, None] = "202610290030"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_FLAKY = "019a0000-0000-7000-8000-00000000c007"
_TEST_SUITE = "019a0000-0000-7000-8000-00000000c008"
_CLEAN_UP = "019a0000-0000-7000-8000-00000000c009"
_PREPARE = "019a0000-0000-7000-8000-00000000c021"
_DEPLOY = "019a0000-0000-7000-8000-00000000c022"
_SECURITY_REVIEW = "019a0000-0000-7000-8000-00000000c010"
_NEW = (_PREPARE, _DEPLOY)
_POPULAR = (_PREPARE, _DEPLOY, _CLEAN_UP, _TEST_SUITE, _SECURITY_REVIEW)
_SEEDED_STARTS = 20

# The 202610281600 seed values, frozen: a row still holding every one of them was never edited.
_SEED = {
    _FLAKY: (
        "fix-flaky-tests", "Fix flaky tests", "Find and fix tests that fail at random.", "coder",
        "Find tests that pass and fail without code changes, and make them reliable.\n\n"
        "- Fix the cause (timing, shared state, order) rather than adding retries.\n"
        "- List what you fixed and what you couldn't.",
    ),
    _TEST_SUITE: (
        "speed-up-the-test-suite", "Speed up the test suite", "Cut test time without losing coverage.", "test_optimizer",
        "Make the test suite faster without weakening it.\n\n"
        "- Remove duplicate tests and tests that check nothing; speed up the slowest ones.\n"
        "- Report time before and after.",
    ),
    _CLEAN_UP: (
        "clean-up-code", "Clean up code", "Remove dead code and simplify tangled parts.", "coder",
        "Tidy the area named below.\n\n"
        "- Delete unused code, files and settings.\n"
        "- Simplify the most tangled parts without changing behavior.\n"
        "- Keep each change easy to review.",
    ),
}
_UNEDITED = (
    "id = CAST(:id AS uuid) AND name = :old_name AND title = :old_name AND summary = :old_summary "
    "AND body = :old_body AND persona = :old_persona AND autonomous AND model IS NULL AND image_key = :old_image "
    "AND published AND NOT featured"
)
# Their SVGs are gone; a row that kept either key (an edited starter, an org's customized copy) shows the merged one.
_RETIRED_IMAGES = ("builtin:fix-flaky-tests", "builtin:speed-up-the-test-suite")
_MERGED_IMAGE = "builtin:improve-test-suite"


def _seed_args(template_id: str) -> dict[str, str]:
    slug, name, summary, persona, body = _SEED[template_id]
    return {
        "id": template_id, "old_name": name, "old_summary": summary, "old_body": body, "old_persona": persona,
        "old_image": f"builtin:{slug}",
    }


def upgrade() -> None:
    from app.task_template_starters import STARTERS

    conn = op.get_bind()
    by_id = {starter.id: starter for starter in STARTERS}
    for template_id in _NEW:
        starter = by_id[template_id]
        conn.execute(
            text(
                "INSERT INTO task_templates "
                "(id, name, slug, summary, purpose, title, body, persona, autonomous, image_key, published) "
                "VALUES (:id, :name, :slug, :summary, :purpose, :title, :body, :persona, :autonomous, :image_key, true) "
                "ON CONFLICT DO NOTHING"
            ),
            {
                "id": starter.id, "name": starter.name, "slug": starter.slug, "summary": starter.summary,
                "purpose": starter.purpose, "title": starter.title, "body": starter.body, "persona": starter.persona,
                "autonomous": starter.autonomous, "image_key": f"builtin:{starter.slug}",
            },
        )
    suite = by_id[_TEST_SUITE]
    conn.execute(
        text(
            "UPDATE task_templates SET name = :name, title = :name, slug = :slug, summary = :summary, body = :body, "
            f"image_key = :image_key WHERE {_UNEDITED} "
            "AND NOT EXISTS (SELECT 1 FROM task_templates other WHERE other.slug = :slug)"
        ),
        {
            **_seed_args(_TEST_SUITE), "name": suite.name, "slug": suite.slug, "summary": suite.summary,
            "body": suite.body, "image_key": f"builtin:{suite.slug}",
        },
    )
    clean_up = by_id[_CLEAN_UP]
    conn.execute(
        text(f"UPDATE task_templates SET summary = :summary, body = :body WHERE {_UNEDITED}"),
        {**_seed_args(_CLEAN_UP), "summary": clean_up.summary, "body": clean_up.body},
    )
    conn.execute(
        text(
            f"DELETE FROM task_templates WHERE {_UNEDITED} AND NOT EXISTS (SELECT 1 FROM automations "
            "WHERE action_params->>'template_id' = CAST(:id AS text))"
        ),
        _seed_args(_FLAKY),
    )
    for table in ("task_templates", "org_task_templates"):
        conn.execute(
            text(f"UPDATE {table} SET image_key = :merged WHERE image_key = ANY(CAST(:retired AS text[]))"),
            {"merged": _MERGED_IMAGE, "retired": list(_RETIRED_IMAGES)},
        )
    conn.execute(
        text(
            "INSERT INTO task_template_start_days (template_id, day, starts) "
            "SELECT id, CURRENT_DATE, :starts FROM task_templates WHERE id = ANY(CAST(:ids AS uuid[])) "
            "ON CONFLICT (template_id, day) DO UPDATE SET starts = task_template_start_days.starts + EXCLUDED.starts"
        ),
        {"starts": _SEEDED_STARTS, "ids": list(_POPULAR)},
    )


def downgrade() -> None:
    """Removes the two new starters (their starts cascade). The rename, the brief, the delete and the other seeded
    starts stay: the frozen seed above is the only record of the old values, and a restore is an operator's edit."""
    op.get_bind().execute(
        text("DELETE FROM task_templates WHERE id = ANY(CAST(:ids AS uuid[]))"), {"ids": list(_NEW)}
    )
