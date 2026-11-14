"""The defect detectors (Architecture §8): pure functions from an `outcome_scan.report` page to proposed links.

No database, no clock, no forge: `scan.py` hands in the origins it watches and the raw facts the toolbox read, and
gets back `Proposal`s, which `writer.propose_link` stores idempotently under UNIQUE(origin, kind, evidence_ref).

| Detector     | Rule                                                                                  | Confidence     |
|--------------|---------------------------------------------------------------------------------------|----------------|
| `revert`     | a commit `This reverts commit <merge sha or a PR commit>` or `Revert "<origin title>"`; a | 0.95           |
|              | merged PR titled `Revert "…"` that references the origin PR                           |                |
| `fixes_ref`  | a later merged PR (or Jaah task) inside the window referencing the origin PR          | 0.80 with a    |
|              | (`#N`, `!N` on GitLab/Azure DevOps, or its URL)                                       | closing keyword, 0.60 bare |
| `same_files` | a later merged PR inside the window touching ≥ 1 of the origin's files, with a        | 0.30 + 0.10 per extra file, |
|              | defect-shaped title                                                                   | capped at 0.50 |

A PR of the origin's own task chain (`Origin.own_prs`) is never evidence against it. One evidence item yields at most
one link per origin, the strongest kind: a revert PR is not also a `fixes_ref` or `same_files` link.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Iterable, Optional

from app.clock import as_utc, parse_utc

REVERT = "revert"
FIXES_REF = "fixes_ref"
SAME_FILES = "same_files"
CAUSED_BY = "caused_by"

CONFIDENCE_REVERT = Decimal("0.95")
CONFIDENCE_FIXES_CLOSING = Decimal("0.80")
CONFIDENCE_FIXES_BARE = Decimal("0.60")
CONFIDENCE_SAME_FILES = Decimal("0.30")
CONFIDENCE_SAME_FILES_STEP = Decimal("0.10")
CONFIDENCE_SAME_FILES_CAP = Decimal("0.50")

#: The defect state each kind moves its origin to.
DEFECT_STATE = {REVERT: "reverted", FIXES_REF: "fixed_forward", SAME_FILES: "fixed_forward", CAUSED_BY: "fixed_forward"}

#: The PR-reference sigil per forge: GitLab merge requests and Azure DevOps pull requests are `!N` (`#N` names an
#: issue or work item there); GitHub and Bitbucket PRs are `#N`.
_SIGIL = {"github": "#", "bitbucket": "#", "gitlab": "!", "azure_devops": "!"}
_CLOSING = r"(?:fix(?:es|ed)?|close[sd]?|resolve[sd]?)"
_DEFECT_TITLE = re.compile(r"\b(fix(es|ed)?|bug|hotfix|regression|broken|crash|revert|patch)\b", re.IGNORECASE)
_REVERTS_COMMIT = re.compile(r"This reverts commit ([0-9a-fA-F]{7,64})")
_REVERT_TITLE = re.compile(r'^\s*Revert\s+"(.+)"\s*$', re.IGNORECASE | re.DOTALL)
_TITLE_CHARS = 200


@dataclass(frozen=True)
class Origin:
    """A merged dispatch the scan watches. `own_prs` are the PR numbers of its own task (never evidence)."""

    outcome_id: Any
    pr_number: Optional[int]
    pr_url: Optional[str]
    merge_sha: Optional[str]
    merged_at: Optional[datetime]
    window_ends_at: Optional[datetime]
    title: str = ""
    commit_shas: tuple[str, ...] = ()
    files: tuple[str, ...] = ()
    own_prs: frozenset[int] = field(default_factory=frozenset)


@dataclass(frozen=True)
class Proposal:
    origin_outcome_id: Any
    kind: str
    evidence_kind: str
    evidence_ref: str
    evidence_title: str
    confidence: Decimal

    @property
    def defect_state(self) -> str:
        return DEFECT_STATE[self.kind]


def _sha_match(sha: str, known: Iterable[Optional[str]]) -> bool:
    """Whether `sha` and one of `known` name the same commit (either may be abbreviated, both ≥ 7 hex)."""
    sha = sha.lower()
    return any(k and len(k) >= 7 and (k.lower().startswith(sha) or sha.startswith(k.lower())) for k in known)


def _parse_at(value: Any) -> Optional[datetime]:
    """A wire stamp or an already-parsed datetime as an aware UTC moment."""
    return as_utc(value) if isinstance(value, datetime) else parse_utc(value)


def _in_window(origin: Origin, at: Optional[datetime]) -> bool:
    """A later item: merged after the origin and no later than its survival window's end. Unknown time: kept only
    when the origin's merge time is unknown too (nothing to order it by)."""
    if at is None:
        return origin.merged_at is None
    if origin.merged_at is not None and at <= origin.merged_at:
        return False
    return origin.window_ends_at is None or at <= origin.window_ends_at


def references(text: str, origin: Origin, forge: str) -> Optional[Decimal]:
    """The `fixes_ref` confidence of `text` against the origin PR: closing keyword + reference → 0.80, a bare
    reference → 0.60, none → None. A reference is the forge's `<sigil>N` (not part of a longer number or word) or
    the PR's URL."""
    if not text or origin.pr_number is None:
        return None
    sigil = re.escape(_SIGIL.get(forge, "#"))
    refs = [rf"(?<![\w&/]){sigil}{origin.pr_number}(?!\d)"]
    if origin.pr_url:
        refs.append(re.escape(origin.pr_url.rstrip("/")) + r"(?![\d])")
    ref = "(?:" + "|".join(refs) + ")"
    if re.search(rf"\b{_CLOSING}\b[\s:]*{ref}", text, re.IGNORECASE):
        return CONFIDENCE_FIXES_CLOSING
    if re.search(ref, text, re.IGNORECASE):
        return CONFIDENCE_FIXES_BARE
    return None


def _title(value: Any) -> str:
    return str(value or "").strip()[:_TITLE_CHARS]


def revert_commits(origin: Origin, commits: list[dict]) -> list[Proposal]:
    """Commits on the integration branch that revert the origin: by sha (merge or one of its PR commits) or by the
    `Revert "<origin title>"` subject git writes."""
    out = []
    known = (origin.merge_sha, *origin.commit_shas)
    for commit in commits:
        sha = str(commit.get("sha") or "").lower()
        if not sha or _sha_match(sha, known):
            continue  # The origin's own commit is never its own revert.
        at = _parse_at(commit.get("committed_at"))
        if at is not None and not _in_window(origin, at):
            continue  # Committed after the window (a re-read in the grace day): the origin's verdict is in.
        body = str(commit.get("body") or "")
        subject = str(commit.get("subject") or "")
        hit = any(_sha_match(m, known) for m in _REVERTS_COMMIT.findall(body))
        title = _REVERT_TITLE.match(subject)
        if not hit and title and origin.title and title.group(1).strip() == origin.title.strip():
            hit = True
        if hit:
            out.append(Proposal(origin.outcome_id, REVERT, "commit", sha, _title(subject), CONFIDENCE_REVERT))
    return out


def _pr_ref(pr: dict) -> str:
    return str(pr.get("url") or f"#{pr.get('number')}")


def pr_links(origin: Origin, prs: list[dict], forge: str) -> list[Proposal]:
    """Later merged PRs against the origin: a revert PR, else a `fixes_ref`, else a defect-shaped `same_files`."""
    out = []
    origin_files = set(origin.files)
    for pr in prs:
        number = pr.get("number")
        if not isinstance(number, int) or number == origin.pr_number or number in origin.own_prs:
            continue
        if not _in_window(origin, _parse_at(pr.get("merged_at"))):
            continue
        title = str(pr.get("title") or "")
        text = f"{title}\n{pr.get('body') or ''}"
        ref = references(text, origin, forge)
        revert_title = _REVERT_TITLE.match(title)
        if revert_title and (ref is not None or (origin.title and revert_title.group(1).strip() == origin.title.strip())):
            out.append(Proposal(origin.outcome_id, REVERT, "pr", _pr_ref(pr), _title(title), CONFIDENCE_REVERT))
            continue
        if ref is not None:
            out.append(Proposal(origin.outcome_id, FIXES_REF, "pr", _pr_ref(pr), _title(title), ref))
            continue
        shared = origin_files & {str(f) for f in (pr.get("files") or [])}
        if shared and _DEFECT_TITLE.search(title):
            confidence = min(CONFIDENCE_SAME_FILES + CONFIDENCE_SAME_FILES_STEP * (len(shared) - 1),
                             CONFIDENCE_SAME_FILES_CAP)
            out.append(Proposal(origin.outcome_id, SAME_FILES, "pr", _pr_ref(pr), _title(title), confidence))
    return out


def task_links(origin: Origin, tasks: list[dict], forge: str) -> list[Proposal]:
    """Later Jaah tasks of the project whose title or body references the origin PR (`fixes_ref`). Each task is
    `{id, title, body, created_at}`; the origin's own task is excluded by the caller."""
    out = []
    for task in tasks:
        if not _in_window(origin, _parse_at(task.get("created_at"))):
            continue
        ref = references(f"{task.get('title') or ''}\n{task.get('body') or ''}", origin, forge)
        if ref is not None:
            out.append(Proposal(origin.outcome_id, FIXES_REF, "task", str(task["id"]), _title(task.get("title")), ref))
    return out


def detect(origins: list[Origin], *, commits: list[dict], prs: list[dict], tasks: list[dict],
           forge: str) -> list[Proposal]:
    """Every proposal for every origin from one report page (plus the project's later tasks)."""
    out: list[Proposal] = []
    for origin in origins:
        out.extend(revert_commits(origin, commits))
        out.extend(pr_links(origin, prs, forge))
        out.extend(task_links(origin, tasks, forge))
    return out


def combined_ci(states: Iterable[str]) -> str:
    """One head's combined CI verdict over the forge's checks (Architecture §8, A4): any `failure` → `fail`; every
    check `success`/`skipped` → `pass`; otherwise (none yet, or any pending) `pending`."""
    seen = [s for s in states if s]
    if any(s == "failure" for s in seen):
        return "fail"
    if seen and all(s in ("success", "skipped") for s in seen):
        return "pass"
    return "pending"
