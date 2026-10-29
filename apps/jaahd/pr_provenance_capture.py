"""A `merged_pr` settle's PR provenance — merge sha, commits, changed paths — stamped as `pr_provenance`.

Read by `pr_stats_capture.capture_all` beside `pr_stats`, with the same login, PR number and ONE shared budget
(`pr_stats_capture.BUDGET_S`), through the forge-agnostic `Adapter.provenance`. GitHub and GitLab implement it;
a forge that does not answers `not_captured`/`unsupported_forge` rather than a guess. The caller may not send
`pr_provenance` itself (`agent_vocabulary`'s `task.complete` `daemon_body_keys`): commits a model typed would be
self-attested. The wire is `forge_cli.model.Provenance`; the CP half that stores it: `apps/api/app/pr_changes.py`.
"""
from __future__ import annotations


def read(adapter, number: int) -> dict:
    return adapter.provenance(number).to_json()


def not_captured(reason: str, forge: "str | None" = None, number: "int | None" = None) -> dict:
    """The full wire shape with nothing captured: the CP keys its row on `pr_number` when one is known."""
    return {"status": "not_captured", "reason": reason, "forge": forge, "pr_number": number, "merge_sha": None,
            "commits": [], "files": []}
