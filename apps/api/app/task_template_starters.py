"""The starter task templates (PRD §9) — the single home of the global template seed.

The baseline (`migrations/versions/202611130100_baseline.py::_seed`) inserts these rows into `task_templates`,
published, each with its built-in image (`image_key = builtin:<slug>`, the SVG at
`apps/web/public/templates/<slug>.svg`). The insert is `ON CONFLICT DO NOTHING`: a starter the operator has edited
or deleted stays as the operator left it.

EDITING THIS MODULE IS A SCHEMA CHANGE. A fresh database seeds the edited values, but every database already seeded
keeps the old ones, so a change here must ship a revision that UPDATEs by id (minding operator edits), as the
starter refresh and brief revisions folded into the baseline did. `tests/test_task_template_seed.py` checks the
migrated rows equal this module on a fresh database only. Renaming or deleting this module breaks the baseline on a
fresh database — freeze its values into the revision first.

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
    #: The task's brief: a goal sentence, then a few bullets of what good looks like. `{{input: Label}}` marks what
    #: Start asks for (`app/task_templates.py::inputs_of`); none on Prepare project for Jaah, which onboarding starts.
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
        "Fix this bug: {{input: The bug — issue link, error message or steps to reproduce}}\n"
        "\n"
        "- Reproduce it first, on the smallest input that shows it. If you cannot, say what you tried and ask "
        "before guessing.\n"
        "- Find the root cause, not the line that throws: trace the bad value back to where it was created.\n"
        "- Write a test that fails before the fix and passes after, at the lowest level that shows the bug.\n"
        "- Fix the cause with the smallest change; leave unrelated code alone.\n"
        "- Look for the same mistake elsewhere and fix those copies too.\n"
        "- Report the cause in one or two sentences, the fix, and how to check it.",
    ),
    Starter(
        _id(2), "build-a-feature", "Build a feature", "From idea to shipped change, with a spec first.", "build_fix",
        "epic", False,
        "Build this feature: {{input: The feature — what it should do, and for whom}}\n"
        "\n"
        "- Restate the goal and write the acceptance criteria before any code; ask about anything that would "
        "change them.\n"
        "- Follow the project's existing patterns, components and naming; add no new library without a reason.\n"
        "- Split the work into small steps that each leave the app working and can be reviewed on their own.\n"
        "- Cover the main path and the obvious failure cases (empty, invalid, no access) with tests.\n"
        "- If it has a screen, check it at phone and desktop widths.\n"
        "- Update the docs or help text the feature changes.\n"
        "- Finish with what shipped, how to try it, and what was left out.",
    ),
    Starter(
        _id(3), "make-ci-green", "Make CI green", "Find why the build fails and fix it.", "build_fix", "coder", True,
        "The build or tests are failing. Find out why and make them pass.\n"
        "\n"
        "- Read the failing job's log first; reproduce the failure locally with the same command CI runs.\n"
        "- Tell the cause apart: a real regression, a flaky test, a broken environment (versions, caches, "
        "secrets), or a change in a dependency.\n"
        "- Fix a real regression in the code, not by loosening the test.\n"
        "- Fix a flaky test at its cause (timing, shared state, order); skip one only with a note on why and a "
        "follow-up.\n"
        "- Pin or update the tool or dependency that broke the environment, and say which.\n"
        "- Never disable a check to make it pass.\n"
        "- Report each failure, its cause, and what you changed.",
    ),
    Starter(
        _id(4), "apply-review-comments", "Apply review comments", "Address open feedback on a pull request.",
        "build_fix", "coder", True,
        "Address the open review comments on this pull request: {{input: Pull request link}}\n"
        "\n"
        "- Read every unresolved comment and the code around it before changing anything.\n"
        "- Make each requested change in the smallest way that satisfies it.\n"
        "- Where you disagree or the request is unclear, reply on the thread with your reasoning instead of "
        "guessing.\n"
        "- Don't change anything the comments don't ask for; no drive-by refactors.\n"
        "- Run the tests and checks the pull request runs, and keep them green.\n"
        "- Push to the same branch, and reply on each thread with what you did.",
    ),
    Starter(
        _id(5), "build-a-quick-prototype", "Build a quick prototype", "A clickable throwaway to test an idea.",
        "build_fix", "prototyper", True,
        "Build a quick, clickable prototype of this idea so we can react to it: {{input: The idea — what it does "
        "and who uses it}}\n"
        "\n"
        "- Favor speed over polish; it will be thrown away. Use fake data and skip sign-in, error handling and "
        "tests.\n"
        "- Show the main flow end to end, so a person can click through it in under a minute.\n"
        "- Keep it separate from production code (its own folder or branch) so it never ships by accident.\n"
        "- Make it easy to open: one command or one link.\n"
        "- End with how to open it, what it shows, the open questions it raises, and what a real build would "
        "need.",
    ),
    Starter(
        PREPARE_FOR_JAAH_ID, "prepare-project-for-jaah", "Prepare project for Jaah",
        "Get a repo ready to work on here.", "build_fix", "onboard", False,
        "Get this project ready for Jaah: find out what it is and how it runs.\n"
        "\n"
        "- Read the code, README, build files and CI config to learn what it is and how it is built.\n"
        "- Make the whole app run here, on placeholder settings where secrets are missing; offer a simple "
        "container setup if it has none.\n"
        "- Fill in its tech stack and environment; have missing secrets added under Secrets.\n"
        "- Find its CI/CD; make the quick and full checks exist, match CI and pass.\n"
        "- Bring its agent docs (CLAUDE.md, AGENTS.md) to Jaah's context rules, rules checker passing; check "
        "other docs against the code, fixing or deleting stale ones.\n"
        "- Show it working: screenshots of its key screens, behind the login too.\n"
        "- Ask, in plain words, only what you cannot find out; end with what works, what is still missing, "
        "and the next templates worth running.",
    ),
    Starter(
        _id(22), "deploy-to-hosting", "Deploy to hosting (CI/CD)", "Host it and deploy on every push.",
        "build_fix", "epic", False,
        "Deploy this project to a hosting provider with a CI/CD pipeline.\n"
        "\n"
        "- Ask whether a free tier will do or a paid, more robust and secure setup is wanted; follow any CI/CD "
        "the project already has.\n"
        "- Offer AWS, Google Cloud or Vercel; guide opening an account, have its credentials added under "
        "Secrets, then check they work.\n"
        "- Write simple infrastructure as code (CloudFormation/SAM or Terraform), kept in the repo.\n"
        "- Deploy pushes to the integration branch to dev, and to main to prod; keep secrets out of the repo and "
        "the logs.\n"
        "- Give each environment its own URL, and say what it costs per month.\n"
        "- Prove both environments work end to end, and write down how to deploy, roll back and read the logs.",
    ),
    # ── Quality ──
    Starter(
        _id(6), "add-tests", "Add tests", "Cover the riskiest untested code.", "quality", "coder", True,
        "Add tests where they matter most in this area: {{input?: Area, module or feature to cover (default: the "
        "whole project)}}\n"
        "\n"
        "- Find the riskiest untested code first: code that changes often, breaks often, or handles money, "
        "access or data.\n"
        "- Test behavior a user or caller relies on, not implementation details.\n"
        "- Cover the edge cases: empty, missing, too large, invalid, no permission.\n"
        "- Follow the project's test style and helpers; keep each test fast and independent of the others.\n"
        "- No tests that can never fail: make each one fail once against a broken version before you keep it.\n"
        "- Report what you covered, what risk is left, and the test run time.",
    ),
    Starter(
        _id(8), "improve-test-suite", "Improve test suite", "Make tests faster and reliable.",
        "quality", "test_optimizer", True,
        "Make the test suite faster and reliable without weakening it.\n"
        "\n"
        "- Measure first: total time and the slowest tests, and run it a few times to find the flaky ones.\n"
        "- Remove duplicate tests and tests that check nothing; speed up the slowest ones.\n"
        "- Fix tests that pass and fail without code changes at the cause (timing, shared state, order), not "
        "with retries.\n"
        "- Share expensive setup where it is safe; replace sleeps with waiting on the real condition.\n"
        "- Keep coverage of what matters: never delete the only test of a behavior.\n"
        "- Report time before and after, and any flaky test you couldn't fix.",
    ),
    Starter(
        _id(9), "clean-up-code", "Clean up code", "Cut dead and duplicate code.", "quality",
        "coder", True,
        "Shrink the code in this area without changing behavior: {{input?: Area to clean up (default: the whole "
        "project)}}\n"
        "\n"
        "- Find the top 5 chances to remove the most lines, then carry them out.\n"
        "- Unify similar code into one shared piece and reuse it.\n"
        "- Delete code, files and settings that were once useful but no longer are.\n"
        "- Merge front-end components or pages that duplicate each other, keeping the most modern, best-looking "
        "one.\n"
        "- Prove behavior is unchanged: the tests pass before and after, and add one where a change is not "
        "covered.\n"
        "- Keep each change easy to review.\n"
        "- Report lines removed and what was merged or deleted.",
    ),
    # ── Security ──
    Starter(
        _id(10), "security-review", "Security review", "Find and fix the most serious risks.", "security", "coder",
        False,
        "Review this project for security problems and fix the serious ones. Focus: {{input?: Area or concern to "
        "focus on (default: the whole project)}}\n"
        "\n"
        "- Check sign-in, permissions (can one user reach another's data?), secrets, input handling and "
        "dependencies.\n"
        "- Look for secrets in code and history, injection (SQL, shell, HTML), missing access checks and unsafe "
        "defaults.\n"
        "- Rank what you find by real risk: how easy it is to exploit and what it exposes.\n"
        "- Fix the top items, each with a test that proves the hole is closed.\n"
        "- Ask before changing anything users would notice.\n"
        "- Report every finding with its risk, where it is, and whether it is fixed or what fixing it would take.",
    ),
    Starter(
        _id(11), "patch-vulnerable-dependencies", "Patch vulnerable dependencies",
        "Upgrade packages with published security issues.", "security", "coder", True,
        "Upgrade dependencies that have published security issues.\n"
        "\n"
        "- Run the ecosystem's audit (npm audit, pip-audit, cargo audit, or similar) to list them.\n"
        "- Prefer the smallest version jump that fixes the issue; avoid major upgrades unless they are the only "
        "fix.\n"
        "- Update the lock files, then run the full tests and build.\n"
        "- Fix code that the upgrade breaks, and note each change.\n"
        "- For an issue with no fixed version, say whether the project is actually exposed and what would reduce "
        "the risk.\n"
        "- Report each package, old and new version, the issue fixed, and what is left.",
    ),
    # ── Maintenance ──
    Starter(
        _id(12), "upgrade-a-dependency", "Upgrade a dependency", "Move a library or framework to a newer version.",
        "maintenance", "coder", True,
        "Upgrade {{input: Library or framework}} to {{input?: Target version (default: the latest stable)}}.\n"
        "\n"
        "- Read the changelog and migration guide between the current and the target version first.\n"
        "- Upgrade in steps through each major version if the jump is large.\n"
        "- Fix everything the upgrade breaks, and replace APIs it deprecates.\n"
        "- Run the full tests and build, and check the main flows by hand where tests are thin.\n"
        "- Note any behavior that changed for users.\n"
        "- Report the old and new version, what you changed, and anything left for later.",
    ),
    Starter(
        _id(13), "migrate-a-framework", "Migrate a framework", "Plan and carry out a larger move.", "maintenance",
        "epic", False,
        "Move the project from {{input: Current technology}} to {{input: New technology}}.\n"
        "\n"
        "- Survey everything that depends on the current one, and size the move before you start.\n"
        "- Plan the move in stages that each leave the app working and shippable; share the plan before the "
        "first stage.\n"
        "- Keep behavior the same unless stated otherwise; tests should pass unchanged at every stage.\n"
        "- Run old and new side by side where you can, and remove the old one only at the end.\n"
        "- Update the build, CI and docs as each stage lands.\n"
        "- Report what moved, what is left, and the risks of the remaining stages.",
    ),
    Starter(
        _id(14), "remove-a-feature-flag", "Remove a feature flag", "Retire a flag that is fully rolled out.",
        "maintenance", "coder", True,
        "Remove this feature flag and keep the behavior users see today: {{input: Feature flag name}}\n"
        "\n"
        "- Find its current value in every environment; if they differ, ask which behavior to keep.\n"
        "- Find every place the flag is read: code, config, tests and the flag service.\n"
        "- Delete the code for the retired behavior and its tests.\n"
        "- Simplify the code the flag guarded, so no dead branch is left.\n"
        "- Remove the flag's definition and config, and say where to delete it in the flag service if that lives "
        "outside the repo.\n"
        "- Run the tests, and report what you removed.",
    ),
    # ── Docs & release ──
    Starter(
        _id(15), "update-the-docs", "Update the docs", "Bring docs in line with the code.", "docs_release", "coder",
        True,
        "Bring the documentation up to date with how the product works today. Focus: {{input?: Which docs or "
        "area (default: all)}}\n"
        "\n"
        "- Check each doc against the code: commands, settings, screens and examples must work as written.\n"
        "- Fix what is wrong, remove what is obsolete, add what is missing.\n"
        "- Run every command and example you keep, and fix the ones that fail.\n"
        "- Keep it short; link instead of repeating, and give every fact one home.\n"
        "- Write for a newcomer: what it is, how to start, and where to look next.\n"
        "- Report which docs changed and anything you could not verify.",
    ),
    Starter(
        _id(16), "write-release-notes", "Write release notes", "What changed, in plain words.", "docs_release",
        "marketer", True,
        "Write release notes for the changes since {{input?: Since — tag, version or date (default: the last "
        "release)}}.\n"
        "\n"
        "- Read the merged changes in that range, not just commit titles.\n"
        "- Lead with what users will notice.\n"
        "- Group by new, improved and fixed; skip internal changes.\n"
        "- Write each item in plain words, as the benefit to the user, one or two sentences.\n"
        "- Call out anything users must do: breaking changes, migrations, new settings.\n"
        "- Link each item to its issue or pull request where there is one.",
    ),
    Starter(
        _id(17), "translate-the-app", "Translate the app", "Put the product's text into another language.", "docs_release",
        "translate", True,
        "Translate the product's text into {{input: Target language(s)}}.\n"
        "\n"
        "- Find where the text lives (string files, templates, docs) and follow the project's existing i18n "
        "setup.\n"
        "- Match the tone of the original, not word for word; adapt idioms and jokes.\n"
        "- Keep placeholders, markup and formatting exactly as they are.\n"
        "- Keep terms consistent; add a glossary for product terms if there is none.\n"
        "- Translate only what is new or changed.\n"
        "- Check that longer text still fits the screens it appears on.\n"
        "- Report what was translated and any string you were unsure of.",
    ),
    # ── Research & planning ──
    Starter(
        _id(18), "clarify-a-request", "Clarify a request", "Turn a rough idea into clear requirements.",
        "research_planning", "business_analysis", False,
        "Turn this request into clear requirements: {{input: The request, in your own words}}\n"
        "\n"
        "- Restate the goal and who it is for in one or two sentences.\n"
        "- Settle what is in and out of scope.\n"
        "- Write checkable acceptance criteria: each one true or false when the work is done.\n"
        "- List the open questions; ask about anything that would change the result.\n"
        "- Note the risks, dependencies and anything you assumed.\n"
        "- End with a short spec someone could build from without asking again.",
    ),
    Starter(
        _id(19), "design-a-solution", "Design a solution", "Pick an approach before building.", "research_planning",
        "architecture", True,
        "Propose how to build this change: {{input: The change to design}}\n"
        "\n"
        "- Read the code it touches first, and follow its existing patterns.\n"
        "- Compare two or three options and recommend one, with the reason.\n"
        "- For each option, name its cost, its risks and what it would take to undo.\n"
        "- Show the parts that change and how data flows between them.\n"
        "- Break the recommended option into steps someone can build in order.\n"
        "- List what you assumed and the questions that would change the pick.",
    ),
    Starter(
        _id(20), "research-a-question", "Research a question", "Sourced answer to a technical or market question.",
        "research_planning", "researcher", True,
        "Research this question and give a clear, sourced answer: {{input: The question}}\n"
        "\n"
        "- Lead with the answer, then the evidence.\n"
        "- Use primary sources (official docs, data, code) over summaries, and give a link for each claim.\n"
        "- Say where sources disagree, and which you trust more and why.\n"
        "- Say how confident you are and what would change the answer.\n"
        "- Keep it short: one page, with the details in an appendix.",
    ),
)
