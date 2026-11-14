"""Security findings in the outcome ledger (Architecture §11): which CI checks are security scans, and what a failing
one on an origin's merge sha records.

Every finding lives in ONE table, `review_findings` (`app/review_findings.py`, its only writer): the settle call's
in-session lanes and the outcome scan's CI scans alike. The ledger keeps only the per-dispatch counts
(`task_outcomes.security_findings`, `security_by_detector`, written by `writer.add_findings`), which metric #7
(`metrics.py`, security per 100) reads.

- **A security check** is a CI check whose name contains one of `DEFAULT_PATTERNS` or one of the project's own
  `projects.security_check_patterns` (additive, case-insensitive substrings — never a regex a tenant could make slow).
- **A CI finding** is one failing security check on an origin's merge sha: lane `ci_scan`, category `security`, the
  check name as its title. Its severity is the forge's own where the check carries one (`SEVERITY_WORDS`), else
  `medium` — stored as `major`, the review vocabulary's word for it. One finding per (task, sha, check), at most
  `MAX_CI_FINDINGS_PER_SHA` per sha: a re-scan records nothing new.
"""
from __future__ import annotations

import re
from typing import Any, Iterable, Optional

from app.models.projects import Project
from app.review_findings import FindingIn

#: The default security-scan check names (substrings), before the project's own.
DEFAULT_PATTERNS: tuple[str, ...] = (
    "codeql", "semgrep", "snyk", "trivy", "gitleaks", "bandit", "dependency-review", "sonar", "checkmarx", "osv",
)
#: A project's own list: at most this many patterns, each at most this long.
MAX_PATTERNS = 30
MAX_PATTERN_CHARS = 100

#: The most CI findings one origin's merge sha holds: a misreporting scan cannot inflate a row past it.
MAX_CI_FINDINGS_PER_SHA = 20
#: The lane a CI scan's finding is credited to (`review_findings.detecting_lane`).
CI_LANE = "ci_scan"
#: The severity of a failing security check that names none.
DEFAULT_CI_SEVERITY = "medium"
#: A scanner's severity word → the review vocabulary (`FindingIn.severity`).
SEVERITY_WORDS: dict[str, str] = {
    "critical": "blocker", "blocker": "blocker", "error": "blocker",
    "high": "major", "major": "major", "medium": "major", "moderate": "major", "warning": "major",
    "low": "minor", "minor": "minor", "note": "info", "info": "info", "notice": "info",
}


def clean_patterns(raw: Iterable[Any]) -> list[str]:
    """A project's own patterns as stored: trimmed, lower-cased, de-duplicated, empty ones dropped. Raises
    `ValueError` with a sentence a person reads when the list is too long or a pattern too long."""
    out: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            raise ValueError("Each security check pattern must be text.")
        pattern = item.strip().lower()
        if len(pattern) > MAX_PATTERN_CHARS:
            raise ValueError(f"A security check pattern is at most {MAX_PATTERN_CHARS} characters.")
        if pattern and pattern not in out:
            if len(out) >= MAX_PATTERNS:
                raise ValueError(f"Add at most {MAX_PATTERNS} security check patterns.")
            out.append(pattern)
    return out


def patterns(project: Project) -> tuple[str, ...]:
    """The defaults, then the project's own (additive)."""
    own = [p for p in (project.security_check_patterns or []) if p and p not in DEFAULT_PATTERNS]
    return DEFAULT_PATTERNS + tuple(own)


def _folded(text: str) -> str:
    """Lower case with `-`, `_` and spaces as one separator: "Dependency Review" matches `dependency-review`."""
    return re.sub(r"[\s_\-]+", "-", text.strip().lower())


def is_security_check(name: str, active: Iterable[str]) -> bool:
    folded = _folded(name)
    return any(_folded(pattern) in folded for pattern in active)


def severity(check: dict[str, Any]) -> str:
    """The review-vocabulary severity of a failing security check: its own where the forge gave one, else medium's."""
    word = str(check.get("severity") or "").strip().lower()
    return SEVERITY_WORDS.get(word) or SEVERITY_WORDS[DEFAULT_CI_SEVERITY]


def ci_finding(check: dict[str, Any], active: Iterable[str]) -> Optional[FindingIn]:
    """The finding a reported check makes, or None when it is not a failing security check."""
    name = str(check.get("name") or "").strip()
    if not name or str(check.get("state") or "") != "failure" or not is_security_check(name, active):
        return None
    url = str(check.get("url") or "")
    return FindingIn(severity=severity(check), category="security", title=name[:500],
                     detail=f"Failing CI security check. {url}".strip()[:20_000])
