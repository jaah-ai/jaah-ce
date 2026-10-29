"""The starter task templates (PRD §9) — the single home of the global template seed.

Revision 202610281600 inserts these rows into `task_templates`, published, each with its built-in image
(`image_key = builtin:<slug>`, the SVG at `apps/web/public/templates/<slug>.svg`). The insert is
`ON CONFLICT DO NOTHING`: a starter the operator has edited or deleted stays as the operator left it.

EDITING THIS MODULE IS A SCHEMA CHANGE. A fresh database seeds the edited values, but every database that already
ran 202610281600 keeps the old ones, so a change here must ship a revision that UPDATEs by id (minding operator
edits), as 202610291100 does. `tests/test_task_template_seed.py` checks the migrated rows equal this module on a fresh database only.
Renaming or deleting this module breaks 202610281600 on a fresh database — freeze its values into the revision
first.

STDLIB-ONLY AND LITERAL: a migration imports it (`tests/test_frozen_migration_imports.py`), so it names no app
module; purposes and personas are spelled as strings and checked against `app/models/task_templates.py::PURPOSES`
and `app/persona_registry.py::CREATABLE_PERSONAS` by `test_task_template_starters.py`. Depth and involvement are
derived from `persona` and `autonomous` (`app/routes/task_templates.py::depth_of` / `involvement_of`).
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Starter:
    #: Fixed UUIDv7 literal, so a migration and a test can name the row.
    id: str
    slug: str
    name: str
    summary: str
    purpose: str
    persona: str
    #: False = guided (asks before it acts); True = unattended.
    autonomous: bool
    #: The task's brief: a goal sentence, then a few bullets of what good looks like.
    body: str

    @property
    def title(self) -> str:
        """The started task's title: the template's name."""
        return self.name


def _id(n: int) -> str:
    return f"019a0000-0000-7000-8000-00000000c{n:03d}"


#: The starter every repo project's onboarding task is started from (`app/project_onboarding.py`).
PREPARE_FOR_JAAH_ID = _id(21)


STARTERS: tuple[Starter, ...] = (
    # ── Build & fix ──
    Starter(
        _id(1), "fix-a-bug", "Fix a bug", "Reproduce, fix, add a test.", "build_fix", "coder", True,
        "Fix the bug described below (issue link or error message).\n\n"
        "- Reproduce it first, then fix the cause, not the symptom.\n"
        "- Add a test that fails before the fix and passes after.\n"
        "- Keep the change small.",
    ),
    Starter(
        _id(2), "build-a-feature", "Build a feature", "From idea to shipped change, with a spec first.", "build_fix",
        "epic", False,
        "Build the feature described below.\n\n"
        "- Agree on what \"done\" looks like before writing code.\n"
        "- Ship it in small, reviewable steps.\n"
        "- Cover the main path and the obvious failure cases with tests.",
    ),
    Starter(
        _id(3), "make-ci-green", "Make CI green", "Find why the build fails and fix it.", "build_fix", "coder", True,
        "The build or tests are failing. Find out why and fix it.\n\n"
        "- Tell a real regression apart from a flaky test or a broken environment.\n"
        "- Fix real failures; skip a flaky test only with a note on why.",
    ),
    Starter(
        _id(4), "apply-review-comments", "Apply review comments", "Address open feedback on a pull request.",
        "build_fix", "coder", True,
        "Address the open review comments on the pull request linked below.\n\n"
        "- Make the requested changes, or reply where you disagree and say why.\n"
        "- Don't change anything the comments don't ask for.",
    ),
    Starter(
        _id(5), "build-a-quick-prototype", "Build a quick prototype", "A clickable throwaway to test an idea.",
        "build_fix", "prototyper", True,
        "Build a quick, clickable prototype of the idea below so we can react to it.\n\n"
        "- Favor speed over polish; it will be thrown away.\n"
        "- Show the main flow end to end.",
    ),
    Starter(
        PREPARE_FOR_JAAH_ID, "prepare-project-for-jaah", "Prepare project for Jaah",
        "Get a repo ready to work on here.", "build_fix", "onboard", False,
        "Get this project ready for Jaah: find out what it is and how it runs.\n\n"
        "- Make it run here; offer a simple container setup if it has none.\n"
        "- Fill in its tech stack and environment; list the secrets it needs and have missing ones added under "
        "Secrets.\n"
        "- Find its CI/CD; make the quick and full checks exist, match CI and pass.\n"
        "- Write or refresh its agent docs (CLAUDE.md, AGENTS.md).\n"
        "- Ask, in plain words, only what you cannot find out; end by suggesting next templates.",
    ),
    Starter(
        _id(22), "deploy-to-hosting", "Deploy to hosting (CI/CD)", "Host it and deploy on every push.",
        "build_fix", "epic", False,
        "Deploy this project to a hosting provider with a CI/CD pipeline.\n\n"
        "- Ask whether a free tier will do or a paid, more robust and secure setup is wanted; follow any CI/CD the "
        "project already has.\n"
        "- Offer AWS, Google Cloud or Vercel; guide opening an account, have its credentials added under Secrets, "
        "then check they work.\n"
        "- Write simple infrastructure as code (CloudFormation/SAM or Terraform).\n"
        "- Deploy pushes to the integration branch to dev, and to main to prod.\n"
        "- Prove both environments work end to end.",
    ),
    # ── Quality ──
    Starter(
        _id(6), "add-tests", "Add tests", "Cover the riskiest untested code.", "quality", "coder", True,
        "Add tests where they matter most in the area named below (or the whole project).\n\n"
        "- Start with code that changes often or breaks often.\n"
        "- Test behavior, not implementation details.\n"
        "- No tests that can never fail.",
    ),
    Starter(
        _id(8), "improve-test-suite", "Improve test suite", "Make tests faster and reliable.",
        "quality", "test_optimizer", True,
        "Make the test suite faster and reliable without weakening it.\n\n"
        "- Remove duplicate tests and tests that check nothing; speed up the slowest ones.\n"
        "- Fix tests that pass and fail without code changes at the cause (timing, shared state, order), not with "
        "retries.\n"
        "- Report time before and after, and any flaky test you couldn't fix.",
    ),
    Starter(
        _id(9), "clean-up-code", "Clean up code", "Cut dead and duplicate code.", "quality",
        "coder", True,
        "Shrink the code in the area named below (or the whole project) without changing behavior.\n\n"
        "- Find the top 5 chances to remove the most lines, then carry them out.\n"
        "- Unify similar code into one shared piece and reuse it.\n"
        "- Delete code, files and settings that were once useful but no longer are.\n"
        "- Merge front-end components or pages that duplicate each other, keeping the most modern, best-looking one.\n"
        "- Keep each change easy to review.",
    ),
    # ── Security ──
    Starter(
        _id(10), "security-review", "Security review", "Find and fix the most serious risks.", "security", "coder",
        False,
        "Review the project for security problems and fix the serious ones.\n\n"
        "- Check sign-in, permissions, secrets, input handling and dependencies.\n"
        "- Rank what you find by real risk; fix the top items.\n"
        "- Ask before changing anything users would notice.",
    ),
    Starter(
        _id(11), "patch-vulnerable-dependencies", "Patch vulnerable dependencies",
        "Upgrade packages with published security issues.", "security", "coder", True,
        "Upgrade dependencies that have published security issues.\n\n"
        "- Prefer the smallest version jump that fixes the issue.\n"
        "- Run the tests; note anything that needed code changes.",
    ),
    # ── Maintenance ──
    Starter(
        _id(12), "upgrade-a-dependency", "Upgrade a dependency", "Move a library or framework to a newer version.",
        "maintenance", "coder", True,
        "Upgrade the library or framework named below to the version named (or the latest stable).\n\n"
        "- Fix everything the upgrade breaks.\n"
        "- Note any behavior that changed for users.",
    ),
    Starter(
        _id(13), "migrate-a-framework", "Migrate a framework", "Plan and carry out a larger move.", "maintenance",
        "epic", False,
        "Move the project from the technology named below to the new one.\n\n"
        "- Plan the move in stages that each leave the app working.\n"
        "- Keep behavior the same unless stated otherwise.",
    ),
    Starter(
        _id(14), "remove-a-feature-flag", "Remove a feature flag", "Retire a flag that is fully rolled out.",
        "maintenance", "coder", True,
        "Remove the feature flag named below and keep the behavior users see today.\n\n"
        "- Delete the code for the retired behavior and its tests.",
    ),
    # ── Docs & release ──
    Starter(
        _id(15), "update-the-docs", "Update the docs", "Bring docs in line with the code.", "docs_release", "coder",
        True,
        "Bring the documentation up to date with how the product works today.\n\n"
        "- Fix what is wrong, remove what is obsolete, add what is missing.\n"
        "- Keep it short; link instead of repeating.",
    ),
    Starter(
        _id(16), "write-release-notes", "Write release notes", "What changed, in plain words.", "docs_release",
        "marketer", True,
        "Write release notes for the changes since the last release.\n\n"
        "- Lead with what users will notice.\n"
        "- Group by new, improved and fixed; skip internal changes.",
    ),
    Starter(
        _id(17), "translate-the-app", "Translate the app", "Put the product's text into another language.", "docs_release",
        "translate", True,
        "Translate the product's text into the language named below.\n\n"
        "- Match the tone of the original, not word for word.\n"
        "- Translate only what is new or changed.",
    ),
    # ── Research & planning ──
    Starter(
        _id(18), "clarify-a-request", "Clarify a request", "Turn a rough idea into clear requirements.",
        "research_planning", "business_analysis", False,
        "Turn the request below into clear requirements.\n\n"
        "- Settle what is in and out of scope.\n"
        "- Write checkable acceptance criteria.\n"
        "- Ask about anything that would change the result.",
    ),
    Starter(
        _id(19), "design-a-solution", "Design a solution", "Pick an approach before building.", "research_planning",
        "architecture", True,
        "Propose how to build the change described below.\n\n"
        "- Compare two or three options and recommend one.\n"
        "- Name the risks and what it would take to undo.",
    ),
    Starter(
        _id(20), "research-a-question", "Research a question", "Sourced answer to a technical or market question.",
        "research_planning", "researcher", True,
        "Research the question below and give a clear, sourced answer.\n\n"
        "- Lead with the answer, then the evidence.\n"
        "- Say where sources disagree.",
    ),
)
