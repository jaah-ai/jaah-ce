"""The forge-neutral shapes every adapter returns, and the exit-code exceptions the CLI maps."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

EXIT_OK = 0
EXIT_POLICY = 2
EXIT_FORGE = 3
EXIT_CREDENTIAL = 4
EXIT_MISUSE = 5

# A forge error's message is capped: it reaches a terminal, and a forge's HTML error page is not ours to pass on.
FORGE_ERROR_CAP = 200

PR_STATES = ("open", "merged", "closed")
MERGE_STATES = ("clean", "pending", "dirty", "blocked", "review_required", "checks_failed", "behind",
                "unknown")
# One check's state, and the rollup over a PR's checks (`none` = the PR has no checks at all).
CHECK_STATES = ("pending", "success", "failure", "skipped")
ROLLUPS = ("pending", "success", "failure", "none")
# A CI run's (or one of its jobs') status, and — once `completed`, never before — its conclusion.
RUN_STATUSES = ("queued", "in_progress", "completed")
RUN_CONCLUSIONS = ("success", "failure", "cancelled", "skipped")
ANNOTATION_LEVELS = ("notice", "warning", "error")


class CliExit(Exception):
    """A non-zero exit, carrying its code and the one message stderr prints."""

    code = EXIT_FORGE

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class PolicyRefusal(CliExit):
    code = EXIT_POLICY


class ForgeError(CliExit):
    code = EXIT_FORGE

    def __init__(self, message: str) -> None:
        # One line, capped.
        flat = " ".join(str(message).split())
        if len(flat) > FORGE_ERROR_CAP:
            flat = flat[: FORGE_ERROR_CAP - 1] + "…"
        super().__init__(flat)


class AlreadyMergeable(ForgeError):
    """The forge refused to ARM auto-merge because the PR can merge right now (GitHub's `clean`/`unstable`
    status). The CLI answers it by merging directly, under the same checks `pr merge` applies."""


class CredentialRefused(CliExit):
    code = EXIT_CREDENTIAL


class Misuse(CliExit):
    code = EXIT_MISUSE


@dataclass(frozen=True)
class Check:
    id: str
    name: str
    state: str  # CHECK_STATES
    url: str = ""
    # Whether the forge makes this check gate the merge (GitHub branch protection's required checks, GitLab's
    # jobs that may not fail). Where a forge names no required set, every check gates: `True`.
    required: bool = True

    def to_json(self) -> dict:
        return {"id": self.id, "name": self.name, "state": self.state, "url": self.url, "required": self.required}


#: A check whose name ends with this never gates a merge, red or pending: a report for a human, not a gate (the GitHub
#: adapter applies it). Needed where the base requires no check, so every check counts (`continue-on-error` still
#: reports `failure`).
ADVISORY_SUFFIX = "(advisory)"


def is_advisory(name: str) -> bool:
    return name.endswith(ADVISORY_SUFFIX)


def rollup(checks: "list[Check]") -> str:
    """failure beats pending beats success; nothing but skipped (or nothing at all) is `none`."""
    states = {c.state for c in checks}
    if "failure" in states:
        return "failure"
    if "pending" in states:
        return "pending"
    if "success" in states:
        return "success"
    return "none"


def required_rollup(checks: "list[Check]") -> str:
    """The rollup over the checks that GATE the merge — what `pr checks` waits on and `pr merge` requires. A red
    optional check does not hold a PR the forge itself would merge."""
    return rollup([c for c in checks if c.required])


@dataclass(frozen=True)
class PullRequest:
    number: int
    url: str  # the forge's real web URL for the PR
    state: str  # PR_STATES
    merge_state: str  # MERGE_STATES
    checks: str  # ROLLUPS
    draft: bool
    auto_merge: bool
    head: str
    base: str
    # Forge-private handles an adapter needs for a follow-up call (GitHub's node id, a head sha…). Never printed.
    extra: dict = field(default_factory=dict, compare=False, repr=False)

    def __post_init__(self) -> None:
        assert self.state in PR_STATES, self.state
        assert self.merge_state in MERGE_STATES, self.merge_state
        assert self.checks in ROLLUPS, self.checks

    def to_json(self) -> dict:
        return {
            "number": self.number, "url": self.url, "state": self.state, "merge_state": self.merge_state,
            "checks": self.checks, "draft": self.draft, "auto_merge": self.auto_merge,
            "head": self.head, "base": self.base,
        }


@dataclass(frozen=True)
class Run:
    """One CI run: a GitHub Actions workflow run, a GitLab pipeline, a Bitbucket pipeline, an Azure build."""

    id: str
    head_sha: str
    event: str  # the forge's own trigger name (`push`, `schedule`, `individualCI`…), passed through
    status: str  # RUN_STATUSES
    conclusion: "str | None"  # RUN_CONCLUSIONS once completed, else None
    created_at: str
    # The branch OF THIS REPO the run built — "" for a tag, a fork's branch, or another repo's build, so
    # `run rerun`'s policy never reads a name someone outside the repo chose.
    branch: str = ""

    def __post_init__(self) -> None:
        _assert_outcome(self.status, self.conclusion)

    def to_json(self) -> dict:
        return {"id": self.id, "head_sha": self.head_sha, "event": self.event, "status": self.status,
                "conclusion": self.conclusion, "created_at": self.created_at, "branch": self.branch}


@dataclass(frozen=True)
class Job:
    id: str
    name: str
    status: str  # RUN_STATUSES
    conclusion: "str | None"

    def __post_init__(self) -> None:
        _assert_outcome(self.status, self.conclusion)

    def to_json(self) -> dict:
        return {"id": self.id, "name": self.name, "status": self.status, "conclusion": self.conclusion}


@dataclass(frozen=True)
class Annotation:
    """A line a job emitted for the run's summary: GitHub's `::warning::`, Azure's `logissue`…"""

    level: str  # ANNOTATION_LEVELS
    message: str
    job: str  # the emitting job's name
    title: str = ""

    def __post_init__(self) -> None:
        assert self.level in ANNOTATION_LEVELS, self.level

    def to_json(self) -> dict:
        return {"level": self.level, "job": self.job, "title": self.title, "message": self.message}


DIFF_STATUSES = ("captured", "partial")


@dataclass(frozen=True)
class DiffStats:
    """A PR's diff stats — the `pr_stats` wire (`apps/api/app/pr_stats.py`).
    `additions`/`deletions` leave out `loc_filter.is_excluded` files, whose lines are `excluded_*`;
    `files_changed` counts every file. A line count the forge did not give is None (`partial`)."""

    status: str  # DIFF_STATUSES
    reason: "str | None"  # required unless `captured`
    additions: "int | None"
    deletions: "int | None"
    files_changed: int
    excluded_additions: "int | None"
    excluded_deletions: "int | None"
    forge: str

    def __post_init__(self) -> None:
        assert self.status in DIFF_STATUSES, self.status
        assert (self.reason is None) == (self.status == "captured"), (self.status, self.reason)

    def to_json(self) -> dict:
        return {"status": self.status, "reason": self.reason, "additions": self.additions,
                "deletions": self.deletions, "files_changed": self.files_changed,
                "excluded_additions": self.excluded_additions, "excluded_deletions": self.excluded_deletions,
                "forge": self.forge}


PROVENANCE_STATUSES = ("captured", "partial")


@dataclass(frozen=True)
class Provenance:
    """A merged PR's provenance — the `pr_provenance` wire (`apps/api/app/pr_changes.py`, compliance-manifest
    architecture §2.7). `commits` are `{sha, author_name, author_login, message}` dicts (`author_login` None where
    the forge names no account; `message` at most 4 KiB); `merge_sha` is None for a PR the forge does not report
    merged. Anything the forge did not list in full makes it `partial`, with the reason saying what is missing."""

    status: str  # PROVENANCE_STATUSES
    reason: "str | None"  # required unless `captured`
    forge: str
    pr_number: int
    merge_sha: "str | None"
    commits: "tuple[dict, ...]"
    files: "tuple[str, ...]"

    def __post_init__(self) -> None:
        assert self.status in PROVENANCE_STATUSES, self.status
        assert (self.reason is None) == (self.status == "captured"), (self.status, self.reason)

    def to_json(self) -> dict:
        return {"status": self.status, "reason": self.reason, "forge": self.forge, "pr_number": self.pr_number,
                "merge_sha": self.merge_sha, "commits": [dict(c) for c in self.commits], "files": list(self.files)}


def _assert_outcome(status: str, conclusion: "str | None") -> None:
    assert status in RUN_STATUSES, status
    assert (conclusion in RUN_CONCLUSIONS) if status == "completed" else conclusion is None, (status, conclusion)


def is_id(value: str) -> bool:
    """ASCII digits only — `str.isdigit` admits `²` and other scripts' digits."""
    return bool(re.fullmatch(r"[0-9]+", value))


def strip_heads(ref: str) -> str:
    return ref[len("refs/heads/"):] if ref.startswith("refs/heads/") else ref
