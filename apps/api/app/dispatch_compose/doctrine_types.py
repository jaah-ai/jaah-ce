"""The doctrine bundle's shape: what each edition's doctrine module supplies (`dispatch_compose/doctrine.py`), the one
key table per level both editions fill, and `assemble`, which fills them from an edition's namespace."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


@dataclass(frozen=True)
class DoctrineBundle:
    """Every piece of role prose the composer splices, by the name its readers use.

    Both editions' bundles carry the same keys at every level (`tests/test_doctrine_bundle.py`); a value may be
    empty where an edition withholds that section."""

    #: charter key -> role row (`role_render.render_role_section_with` reads its keys), in role-table order.
    roles: Mapping[str, Mapping[str, str]]
    #: Prose constants `role_render`, `system_prompt` and the playbook sandbox's contracts splice, by name.
    shared: Mapping[str, str]
    #: (authored fragment, twin) tables the composer applies per slot shape, delivery and merge lane, by name.
    rewrites: Mapping[str, tuple[tuple[str, str], ...]]
    #: The autonomy directives `directives._autonomous_directive` picks from, by name.
    directives: Mapping[str, str]


#: (charter key, the name of its role row) in role-table order: `roles.charter_keys()` returns it.
ROLE_KEYS: tuple[tuple[str, str], ...] = (
    ("implementer", "IMPLEMENTER"),
    ("ba", "BA"),
    ("architect", "ARCHITECT"),
    ("planner", "PLANNER"),
    ("finish", "FINISH"),
    ("maturity", "MATURITY"),
    ("portfolio_drift", "PORTFOLIO_DRIFT"),
    ("adhoc", "ADHOC"),
    ("merge_lane", "MERGE_LANE"),
    ("release", "RELEASE"),
    ("compliance_fix", "COMPLIANCE_FIX"),
    ("compliance_auditor", "COMPLIANCE_AUDITOR"),
    ("outcome_scan", "OUTCOME_SCAN"),
    ("reviewer", "REVIEWER"),
    ("security_reviewer", "SECURITY_REVIEWER"),
    ("tester", "TESTER"),
    ("deploy_probe", "DEPLOY_PROBE"),
    ("duel_review", "DUEL_REVIEW"),
    ("test_optimizer", "TEST_OPTIMIZER"),
    ("translate", "TRANSLATE"),
    ("marketer", "MARKETER"),
    ("onboard", "ONBOARD"),
    ("business_wizard", "BUSINESS_WIZARD"),
    ("repo_move", "REPO_MOVE"),
    ("advisor", "ADVISOR"),
    ("researcher", "RESEARCHER"),
    ("prototyper", "PROTOTYPER"),
    ("app_wizard", "APP_WIZARD"),
)
#: The `shared`, `rewrites` and `directives` names, each keyed by itself.
SHARED_KEYS: tuple[str, ...] = (
    "_ADVISOR_STANDING_ORDERS",
    "_ANALYSIS_PROHIBITIONS",
    "_BEHAVIORAL_EVIDENCE_R20",
    "_BOUNDED_GATE_WAIT",
    "_ENGINEERING_RULES",
    "_FOLDER_DELIVERY",
    "_MERGE_WAIT_POINTER",
    "_MERGED_PR_TERMINAL_REPORTING",
    "_QUICK_GATE_HOOKS",
    "_RECON_BATCH_DELEGATE",
    "_RECON_PREFER_READ",
    "_RENDERED_REPORT_MANDATE",
    "_WAIT_FOR_PR_MERGE",
)
REWRITE_KEYS: tuple[str, ...] = (
    "_FOLDER_CHARTER_REWRITES",
    "_FOLDER_REWRITES",
    "_MERGE_LANE_REWRITES",
    "_OUTCOME_MERGE_WAIT_REWRITES",
    "_OUTCOME_SETTLE_REWRITES",
)
DIRECTIVE_KEYS: tuple[str, ...] = (
    "_CONSOLE_DIRECTIVE",
    "_FULL_AUTO_DIRECTIVE",
    "_INTERVIEW_DIRECTIVE",
    "_NO_CARVE_OUT_DIRECTIVE",
)


def assemble(namespace: Mapping[str, Any]) -> DoctrineBundle:
    """An edition's bundle: every key table's name looked up in `namespace` (the edition module's globals, plus the
    modules its values live in). Plain dicts, not read-only views: a test swaps one entry with `monkeypatch.setitem`."""
    wanted = [name for _key, name in ROLE_KEYS] + [*SHARED_KEYS, *REWRITE_KEYS, *DIRECTIVE_KEYS]
    missing = [name for name in wanted if name not in namespace]
    if missing:
        raise KeyError(f"doctrine edition lacks {', '.join(missing)}: every edition carries every key table name")
    return DoctrineBundle(
        roles={key: namespace[name] for key, name in ROLE_KEYS},
        shared={name: namespace[name] for name in SHARED_KEYS},
        rewrites={name: namespace[name] for name in REWRITE_KEYS},
        directives={name: namespace[name] for name in DIRECTIVE_KEYS},
    )
