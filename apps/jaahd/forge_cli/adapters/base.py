"""The operations every forge adapter implements. The CLI owns policy; an adapter only talks to its forge.

The surface below is the whole of what the CLI can ask for. The CI writes are `rerun` and `dispatch` (no
cancel); `close` is the one way a PR leaves the open set unmerged, and the CLI posts its
reason as a comment first.
"""
from __future__ import annotations

from ..login import Login
from collections.abc import Callable, Iterable

from ..loc_filter import is_excluded
from ..model import Annotation, Check, DiffStats, Job, Provenance, PullRequest, Run
from ..transport import Client

# The largest page any of the four forges serves; `--limit` is capped to it, so a list is one page unless
# a filter the forge cannot apply itself (`--state closed` on some) discards rows.
PAGE_MAX = 100
# A client-side filter (`--author`, `--state closed` where the forge cannot) reads at most this many pages:
# a filter that matches nothing must not walk a repo's whole history on the shared rate limit.
MAX_PAGES = 10


class Adapter:
    def __init__(self, login: Login, client: Client) -> None:
        self.login = login
        self.client = client

    def create(self, *, title: str, body: str, head: str, base: str, draft: bool) -> PullRequest:
        raise NotImplementedError

    def find(self, head: str) -> int:
        """The number of the newest PR (any state) from `head`, or `ForgeError` when there is none."""
        raise NotImplementedError

    def view(self, number: int) -> PullRequest:
        raise NotImplementedError

    def checks(self, number: int) -> "list[Check]":
        """Every check on the PR's head, each marked `required` per the forge's own gating rule."""
        raise NotImplementedError

    def auto_merge(self, pr: PullRequest) -> PullRequest:
        """Enable the forge's native auto-merge (a merge commit, never a squash), returning the fresh view.
        A forge without one returns `pr` unchanged with `auto_merge` false (O4: watch-then-merge). A forge
        that refuses because the PR can merge NOW raises `AlreadyMergeable`."""
        raise NotImplementedError

    def disable_auto_merge(self, pr: PullRequest) -> PullRequest:
        """Disarm the forge's native auto-merge, returning the fresh view; a forge without one returns `pr`
        unchanged."""
        raise NotImplementedError

    def merge(self, pr: PullRequest) -> PullRequest:
        """Merge NOW with a merge commit. The CLI has already checked checks/merge_state."""
        raise NotImplementedError

    def status(self, sha: str, *, state: str, context: str, description: str) -> dict:
        """Set commit status `context` on `sha` (`pending`/`success`/`failure`), returning `{sha, context, state,
        url}`. GitHub only so far: the CLI refuses the verb on any other forge before calling this."""
        raise NotImplementedError

    def edit(self, number: int, *, title: "str | None", body: "str | None") -> PullRequest:
        raise NotImplementedError

    def comment(self, number: int, body: str) -> dict:
        """`{"number", "url"}` of the new comment."""
        raise NotImplementedError

    def ready(self, number: int) -> PullRequest:
        """Mark a draft PR ready for review, returning the fresh view."""
        raise NotImplementedError

    def close(self, number: int) -> PullRequest:
        """Close the PR unmerged (GitLab close, Bitbucket decline, Azure abandon), returning the fresh view."""
        raise NotImplementedError

    def ci_log(self, check_id: str) -> str:
        raise NotImplementedError

    def runs(self, workflow: "str | None", branch: "str | None", limit: int) -> "list[Run]":
        """The newest `limit` runs of `workflow` — of every workflow when None — (on `branch`), newest first."""
        raise NotImplementedError

    def run(self, run_id: str) -> "tuple[Run, list[Job], list[Annotation]]":
        raise NotImplementedError

    def run_head(self, run_id: str) -> Run:
        """The run alone, without its jobs — what `run rerun`'s policy reads. Override where `run` is costly."""
        return self.run(run_id)[0]

    def rerun(self, run: Run, failed_only: bool) -> str:
        """Re-run `run` (only its failed jobs when `failed_only`), returning the id of the run that now
        carries the re-run — the same id where the forge re-runs in place, a new one where it queues a copy."""
        raise NotImplementedError

    def dispatch(self, workflow: str, ref: str, inputs: "dict[str, str]") -> "str | None":
        """Start a run of `workflow` (its file name) on branch `ref` with `inputs`, returning the new run's id
        where the forge names it, else None. GitHub only so far: the CLI refuses the verb on any other forge
        before calling this."""
        raise NotImplementedError

    def prs(self, state: str, author: "str | None", limit: int, head: "str | None" = None,
            base: "str | None" = None) -> "list[dict]":
        """The newest `limit` PRs in `state` (`open`, `closed` = every settled one, `all`), each as `pr_item`
        (`listed_item` where the listing names no checks); only those from branch `head` / into branch `base` when
        given."""
        raise NotImplementedError


    def diff_stats(self, number: int) -> DiffStats:
        """The PR's lines added/removed and files changed, read off the forge's per-file list (at most
        `MAX_DIFF_FILES` files; past that, `partial`). Built by `diff_stats` below."""
        raise NotImplementedError

    def provenance(self, number: int) -> Provenance:
        """The PR's merge sha, commits (at most `MAX_PR_COMMITS`) and changed paths (at most `MAX_DIFF_FILES`).
        Built by `provenance` below. A forge without it leaves this unimplemented: the capture answers
        `unsupported_forge` rather than a guess."""
        raise NotImplementedError


# The most files `diff_stats` reads (GitHub's own per-PR file list stops at 3,000): past it, `partial`.
MAX_DIFF_FILES = 3000
TRUNCATED = f"more than {MAX_DIFF_FILES} files changed; counted the first {MAX_DIFF_FILES}"


def diff_stats(forge: str, files: "Iterable[tuple[str, int | None, int | None]]", partial: "str | None" = None
               ) -> DiffStats:
    """Total `(path, additions, deletions)` rows into a `DiffStats`; `partial` names why it is incomplete.
    A row whose counts are None (a diff the forge would not return, a forge with no line counts) counts
    as a file only, and makes the whole `partial` — the line totals become None when NO row had counts."""
    adds = dels = ex_adds = ex_dels = files_changed = counted = 0
    for path, added, deleted in files:
        files_changed += 1
        if added is None or deleted is None:
            continue
        counted += 1
        if is_excluded(path):
            ex_adds, ex_dels = ex_adds + added, ex_dels + deleted
        else:
            adds, dels = adds + added, dels + deleted
    if counted < files_changed and partial is None:
        partial = f"{files_changed - counted} of {files_changed} files carry no line counts from the forge"
    lines = counted or not files_changed

    def known(value: int) -> "int | None":
        return value if lines else None

    return DiffStats("partial" if partial else "captured", partial, known(adds), known(dels), files_changed,
                     known(ex_adds), known(ex_dels), forge)


# The most commits `provenance` lists (GitHub's own per-PR commit list stops at 250): past it, `partial`.
MAX_PR_COMMITS = 250
# A commit message is kept whole up to this many UTF-8 bytes, and cut there past it.
MAX_COMMIT_MESSAGE_BYTES = 4096


# A name or a path is a line: the CP refuses a longer one (`apps/api/app/pr_changes.py`), so it is cut here.
MAX_LINE_BYTES = 4096


def utf8_cut(text: str, cap: int = MAX_LINE_BYTES) -> str:
    """`text` as valid UTF-8 (a lone surrogate replaced) of at most `cap` bytes, cut on a character boundary."""
    return text.encode("utf-8", "replace")[:cap].decode("utf-8", "ignore")


def commit(sha: str, author_name: str, author_login: "str | None", message: str) -> dict:
    """One `Provenance.commits` entry, its message cut to `MAX_COMMIT_MESSAGE_BYTES`."""
    return {"sha": sha, "author_name": utf8_cut(author_name),
            "author_login": utf8_cut(author_login) if author_login else None,
            "message": utf8_cut(message, MAX_COMMIT_MESSAGE_BYTES)}


def provenance(forge: str, number: int, *, merged: bool, merge_sha: "str | None", commits: "list[dict]",
               more_commits: bool, files: "list[str]", more_files: bool) -> Provenance:
    """A `Provenance`, `partial` whenever any part is short: an unmerged PR, a list the forge cut, or an empty
    list — a merged PR with no commits or no changed files is a forge that did not answer, never a fact."""
    gaps = []
    if not merged or not merge_sha:
        gaps.append("the forge reports no merge commit for this PR")
    if more_commits:
        gaps.append(f"more commits than were listed; listed {len(commits)}")
    elif not commits:
        gaps.append("the forge listed no commits")
    if more_files:
        gaps.append(f"more files changed than were listed; listed {len(files)}")
    elif not files:
        gaps.append("the forge listed no changed files")
    files = [utf8_cut(path) for path in files]
    reason = "; ".join(gaps) or None
    return Provenance("partial" if reason else "captured", reason, forge, number, merge_sha if merged else None,
                      tuple(commits), tuple(files))


def capped(rows: "Iterable", limit: int = MAX_DIFF_FILES) -> "tuple[list, bool]":
    """The first `limit` rows of a lazy list, and whether any were left unread. `rows` is consumed one past
    the cap at most, so no page beyond the one that crosses it is fetched."""
    out: list = []
    for row in rows:
        if len(out) >= limit:
            return out, True
        out.append(row)
    return out, False


def pr_item(pr: PullRequest, title: str, author: str) -> dict:
    """One `pr list` row: the PR's normalised view narrowed, plus what only a listing names."""
    return {"number": pr.number, "title": title, "head": pr.head, "base": pr.base, "author": author,
            "state": pr.state, "merge_state": pr.merge_state, "checks": pr.checks, "url": pr.url,
            "auto_merge": pr.auto_merge}


def listed_item(*, number: int, title: str, head: str, base: str, author: str, state: str, url: str,
                auto_merge: bool) -> dict:
    """A `pr list` row built from the listing alone, for a forge whose listing names no mergeability or checks:
    `merge_state` and `checks` are null (`pr view N` names them) — no call per PR."""
    return {"number": number, "title": title, "head": head, "base": base, "author": author, "state": state,
            "merge_state": None, "checks": None, "url": url, "auto_merge": auto_merge}


def take(pages: "Iterable[list]", limit: int, keep: "Callable[[object], bool] | None" = None) -> list:
    """Rows from successive pages until `limit` are kept. `pages` is lazy: no page past the last one needed
    is ever fetched — the shared rate limit is spent on pages actually read — and none past `MAX_PAGES`."""
    out: list = []
    for count, rows in enumerate(pages, 1):
        for row in rows:
            if keep is None or keep(row):
                out.append(row)
                if len(out) >= limit:
                    return out
        if count >= MAX_PAGES:
            break
    return out


def every(pages: "Iterable[list]") -> list:
    """Every row of every page (a run's jobs: the whole set is the answer)."""
    return [row for rows in pages for row in rows]


def is_ci_file(workflow: str) -> bool:
    """A CI config FILE, where the forge reads one per repo (GitLab, Bitbucket): it names every run."""
    return workflow.lower().endswith((".yml", ".yaml"))
