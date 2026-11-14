"""The operations every forge adapter implements. The CLI owns policy; an adapter only talks to its forge.

The surface below is the whole of what the CLI can ask for. The CI writes are `rerun` and `dispatch` (no
cancel); `close` is the one way a PR leaves the open set unmerged, and the CLI posts its
reason as a comment first.
"""
from __future__ import annotations

import itertools
from dataclasses import dataclass
from datetime import datetime, timezone

from ..login import Login
from collections.abc import Callable, Iterable, Iterator, Mapping
from types import MappingProxyType

from ..loc_filter import is_excluded
from ..model import Annotation, Check, DiffStats, ForgeError, Job, Provenance, PullRequest, Run, is_id
from ..transport import Client

# The largest page any of the four forges serves; `--limit` is capped to it, so a list is one page unless
# a filter the forge cannot apply itself (`--state closed` on some) discards rows.
PAGE_MAX = 100
# A client-side filter (`--author`, `--state closed` where the forge cannot) reads at most this many pages:
# a filter that matches nothing must not walk a repo's whole history on the shared rate limit.
MAX_PAGES = 10


@dataclass(frozen=True)
class Started:
    """A run `dispatch` started: its id, web page and the commit it builds — each "" where the forge names none."""

    id: str = ""
    url: str = ""
    head_sha: str = ""

    def to_json(self) -> dict:
        return {k: v for k, v in (("id", self.id), ("url", self.url), ("head_sha", self.head_sha)) if v}


class Adapter:
    # The repository's API path, `{}` the login's `repo_path`; a forge that encodes or trims it overrides `_repo`.
    REPO = "repos/{}"
    # The PR collection under `_repo` (`_pr_path` names one PR's own path), the method an update of one takes,
    # and the query every call carries (Azure's `api-version`; none elsewhere).
    PRS = "pulls"
    UPDATE = "PATCH"
    PARAMS: Mapping[str, str] = MappingProxyType({})
    # The key a PR's description travels under in `_update`'s body.
    DESCRIPTION = "body"
    # What a PR is called in a message ("GitHub PR", "GitLab MR"), and the key a listed row's number travels under.
    PR_NAME = "GitHub PR"
    NUMBER = "number"
    # What `_update` sends to close a PR unmerged; a forge that closes by its own endpoint overrides `close`.
    CLOSE = {"state": "closed"}

    def __init__(self, login: Login, client: Client) -> None:
        self.login = login
        self.client = client

    @property
    def _repo(self) -> str:
        return self.REPO.format(self.login.repo_path)

    def _pr_path(self, number: "int | str | None" = None) -> str:
        """One PR's own API path, or the collection's without `number`."""
        path = f"{self._repo}/{self.PRS}"
        return f"{path}/{number}" if number is not None else path

    def _pages(self, path: str, params: dict, per_page: int, key: "str | None" = None, *, skip: bool = False
               ) -> "Iterator[list]":
        """Pages of `path` (its `key` array, or the answer itself) until a short page ends them: numbered
        (`per_page`/`page`), or by offset (`$top`/`$skip`) where `skip`."""
        for page in itertools.count():
            paging = {"$top": per_page, "$skip": page * per_page} if skip else {"per_page": per_page, "page": page + 1}
            data = self.client.get(path, params={**params, **paging})
            rows = (data.get(key) if key else data) or []
            yield rows
            if len(rows) < per_page:
                return

    def _rows(self, path: str, params: dict, per_page: int, key: "str | None" = None) -> "Iterator":
        """Every row of `_pages`, as lazily: `capped` reads one row past its cap at most."""
        return (row for page in self._pages(path, params, per_page, key) for row in page)

    def _next_pages(self, path: str, params: dict) -> "Iterator[list]":
        """Pages by the answer's own `next` link (a same-host URL that carries the query on)."""
        data = self.client.get(path, params=params)
        while True:
            yield data.get("values") or []
            if not data.get("next"):
                return
            data = self.client.get(data["next"])

    def _id(self, value: str, what: str) -> str:
        """`value` when it is a numeric id, else `ForgeError` naming `what` it is not."""
        if not is_id(value):
            raise ForgeError(f"'{value}' is not {what}")
        return value

    def _update(self, number: int, body: dict) -> PullRequest:
        """Send `body` (`UPDATE`) to the PR's own path, returning the fresh view (`edit`, and `ready`/`close`
        where the forge does them by update)."""
        self.client.json(self.UPDATE, self._pr_path(number), params=self.PARAMS or None, json_body=body)
        return self.view(number)

    def _create_payload(self, title: str, body: str, head: str, base: str, draft: bool) -> dict:
        """The forge's own spelling of a new PR: what `create` POSTs."""
        raise NotImplementedError

    def create(self, *, title: str, body: str, head: str, base: str, draft: bool) -> PullRequest:
        """Open the PR with one POST to the collection, returning the fresh view of the `NUMBER` it answers."""
        data = self.client.json("POST", self._pr_path(), params=self.PARAMS or None,
                                json_body=self._create_payload(title, body, head, base, draft))
        return self.view(int(data[self.NUMBER]))

    def _find_rows(self, head: str) -> list:
        """The newest PR (any state) from `head` as the forge lists it: one row, or none."""
        raise NotImplementedError

    def find(self, head: str) -> int:
        """The number of the newest PR (any state) from `head`, or `ForgeError` when there is none."""
        rows = self._find_rows(head)
        if not rows:
            raise ForgeError(f"no {self.PR_NAME} from branch '{head}'")
        return int(rows[0][self.NUMBER])

    def view(self, number: int) -> PullRequest:
        raise NotImplementedError

    def checks(self, number: int) -> "list[Check]":
        """Every check on the PR's head, each marked `required` per the forge's own gating rule."""
        raise NotImplementedError

    def checks_for_sha(self, sha: str) -> "list[Check]":
        """Every check on commit `sha`, each `required` (no PR names a gating rule for a bare commit): the
        outcome scan's `checks --sha` verb."""
        raise NotImplementedError

    def auto_merge(self, pr: PullRequest) -> PullRequest:
        """Enable the forge's native auto-merge (a merge commit, never a squash), returning the fresh view.
        A forge without one (Bitbucket) leaves this default: `pr` unchanged with `auto_merge` false, and the CLI
        prints the watch-then-merge advice (O4). A forge that refuses because the PR can merge NOW raises
        `AlreadyMergeable`."""
        return pr

    def disable_auto_merge(self, pr: PullRequest) -> PullRequest:
        """Disarm the forge's native auto-merge, returning the fresh view; a forge without one leaves this
        default: `pr` unchanged, nothing to disarm."""
        return pr

    def head_commit(self, pr: PullRequest) -> str:
        """The PR's head as a full commit id, for `pr merge --sha`'s comparison. The default trusts the view's own
        head: a forge whose view abbreviates it (Bitbucket) resolves it here."""
        return pr.extra.get("head_sha") or ""

    def merge(self, pr: PullRequest) -> PullRequest:
        """Merge NOW with a merge commit. The CLI has already checked checks/merge_state."""
        raise NotImplementedError

    def status(self, sha: str, *, state: str, context: str, description: str) -> dict:
        """Set commit status `context` on `sha` (`pending`/`success`/`failure`), returning `{sha, context, state,
        url}`. GitHub only so far: the CLI refuses the verb on any other forge before calling this."""
        raise NotImplementedError

    def edit(self, number: int, *, title: "str | None", body: "str | None") -> PullRequest:
        return self._update(number, {k: v for k, v in (("title", title), (self.DESCRIPTION, body)) if v is not None})

    def comment(self, number: int, body: str) -> dict:
        """`{"number", "url"}` of the new comment."""
        raise NotImplementedError

    def ready(self, number: int) -> PullRequest:
        """Mark a draft PR ready for review, returning the fresh view."""
        raise NotImplementedError

    def close(self, number: int) -> PullRequest:
        """Close the PR unmerged (GitLab close, Bitbucket decline, Azure abandon), returning the fresh view."""
        return self._update(number, self.CLOSE)

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

    def dispatch(self, workflow: str, ref: str, inputs: "dict[str, str]") -> "Started":
        """Start a run of `workflow` on branch `ref` with `inputs`. What `workflow` names is the forge's own: a
        GitHub workflow file, a GitLab job name (`*` = the whole pipeline: GitLab starts the ref's whole pipeline,
        `JAAH_GATE` naming the job), a Bitbucket custom pipeline, an Azure Pipelines pipeline id. The answer names
        the started run where the forge does (a GitHub that predates run details names none)."""
        raise NotImplementedError

    def prs(self, state: str, author: "str | None", limit: int, head: "str | None" = None,
            base: "str | None" = None) -> "list[dict]":
        """The newest `limit` PRs in `state` (`open`, `closed` = every settled one, `all`), each as `pr_item`
        (`listed_item` where the listing names no checks); only those from branch `head` / into branch `base` when
        given."""
        raise NotImplementedError

    def merged_prs(self, since: datetime, limit: int) -> "list[dict]":
        """The PRs merged at or after `since`, newest merge first, at most `limit`, each as `merged_item` (its
        first `MAX_MERGED_FILES` paths). Read off a listing by recent update (a merge is an update), at most
        `MAX_PAGES` pages of it: a busier window than that comes back short."""
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

    # CI variables and secrets (`var` / `secret`). `kind` is `variable` or `secret`; `env` names a deployment
    # environment, None the repository level. The CLI has validated the name and read the value.
    # What the login must hold to read and write them, named when the forge answers 403.
    VARIABLE_RIGHTS = ""
    # `secret --dependabot` (GitHub only: the CLI refuses it on any other forge, so only GitHub's overrides take
    # the `dependabot` keyword) targets the store Dependabot reads instead of the CI's. Its rights, named on a 403.
    DEPENDABOT_RIGHTS = ""

    def variables(self, kind: str, env: "str | None") -> "list[dict]":
        """Every variable (`{name, value}`) or secret (`{name}`, never a value) at that level, by name."""
        raise NotImplementedError

    def set_variable(self, kind: str, name: str, value: str, env: "str | None") -> dict:
        """Create or overwrite one, returning `{"action": "created"|"updated"}` plus anything the forge must say
        (Azure: the variable group a pipeline has to reference)."""
        raise NotImplementedError

    def _upsert_variable(self, method: str, collection: str, name: str, body: dict, create_body: dict,
                         params: "dict | None" = None) -> dict:
        """`set_variable` on a forge that updates a variable at its own path and creates one in the collection
        (GitHub, GitLab): `body` by `method` to `collection/name` (with `params`); when the forge answers 404,
        no such name yet, `create_body` is POSTed to `collection` instead. The action taken is the answer."""
        try:
            self.client.json(method, f"{collection}/{name}", params=params, json_body=body)
            return {"action": "updated"}
        except ForgeError as exc:
            if exc.status != 404:
                raise
        self.client.json("POST", collection, json_body=create_body)
        return {"action": "created"}

    def delete_variable(self, kind: str, name: str, env: "str | None") -> dict:
        """Delete one; a name that does not exist is a `ForgeError`. Returns what `set_variable`'s extras name."""
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


def take(pages: "Iterable[list]", limit: int, keep: "Callable[[object], bool] | None" = None,
         stop: "Callable[[object], bool] | None" = None) -> list:
    """Rows from successive pages until `limit` are kept, or until the first row `stop` names (a listing sorted
    on the field it reads). `pages` is lazy: no page past the last one needed is ever fetched — the shared rate
    limit is spent on pages actually read — and none past `MAX_PAGES`."""
    out: list = []
    for count, rows in enumerate(pages, 1):
        for row in rows:
            if stop is not None and stop(row):
                return out
            if keep is None or keep(row):
                out.append(row)
                if len(out) >= limit:
                    return out
        if count >= MAX_PAGES:
            break
    return out


# `pr list --state merged`: the most rows it prints, and the most paths each row lists (one page of the forge's
# per-file list — a scan of many PRs must not walk any one PR's whole diff).
MERGED_MAX = 200
MAX_MERGED_FILES = 100


def parse_time(value: "str | None") -> "datetime | None":
    """A forge's ISO-8601 timestamp as an aware datetime (`Z` read as UTC, a naive one taken as UTC), or None
    for none or one that does not parse."""
    if not value:
        return None
    try:
        when = datetime.fromisoformat(value)
    except ValueError:
        return None
    return when if when.tzinfo else when.replace(tzinfo=timezone.utc)


def iso(when: datetime) -> str:
    """`when` in UTC, ISO-8601 with a `Z`: the one timestamp spelling the merged list prints."""
    return when.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def merged_item(*, number: int, url: str, title: str, body: "str | None", merged_at: datetime,
                files: "Iterable[str]") -> dict:
    """One `pr list --state merged` row (the outcome scan's input); a missing description is ""."""
    return {"number": number, "url": url, "title": title, "body": body or "", "merged_at": iso(merged_at),
            "files": [utf8_cut(path) for path in files]}


def newest_merged(rows: list, merged_at: "Callable[[dict], datetime | None]", since: datetime, limit: int
                  ) -> "list[tuple[datetime, dict]]":
    """`(merged_at, row)` of the `limit` rows merged latest at or after `since`, newest first — a listing ordered
    by update is not ordered by merge."""
    kept = [(when, row) for row in rows if (when := merged_at(row)) is not None and when >= since]
    return sorted(kept, key=lambda pair: pair[0], reverse=True)[:limit]


def every(pages: "Iterable[list]") -> list:
    """Every row of every page (a run's jobs: the whole set is the answer)."""
    return [row for rows in pages for row in rows]


def is_ci_file(workflow: str) -> bool:
    """A CI config FILE, where the forge reads one per repo (GitLab, Bitbucket): it names every run."""
    return workflow.lower().endswith((".yml", ".yaml"))
