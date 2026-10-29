"""GitLab: REST v4 (gitlab.com and self-managed), the project addressed by its URL-encoded path."""
from __future__ import annotations

import re
import dataclasses
import urllib.parse

from ..model import Annotation, Check, ForgeError, Job, PolicyRefusal, PullRequest, Run, is_id
from .base import (MAX_PR_COMMITS, PAGE_MAX, TRUNCATED, Adapter, capped, commit, diff_stats, every, is_ci_file,
                   listed_item, provenance, take)

_STATE = {"opened": "open", "locked": "open", "merged": "merged", "closed": "closed"}
_ROLLUP = {"success": "success", "failed": "failure", "canceled": "failure", "skipped": "none"}
_DIRTY = {"conflict", "need_rebase", "broken_status"}
_PENDING = {"checking", "unchecked", "ci_still_running", "approvals_syncing", "preparing"}
_REVIEW = {"not_approved", "requested_changes"}
_BLOCKED = {"draft_status", "discussions_not_resolved", "blocked_status", "not_open", "policies_denied",
            "merge_request_blocked", "jira_association_missing", "external_status_checks", "locked_paths",
            "locked_lfs_files", "security_policy_violations", "title_regex", "commits_status", "merge_time"}

_CONCLUSION = {"success": "success", "failed": "failure", "canceled": "cancelled", "skipped": "skipped"}
_RUNNING = {"running", "canceling"}
_MR_STATE = {"open": "opened", "closed": "all", "all": "all"}  # `closed` = closed + merged: filtered here
# GitLab's draft flag IS a title prefix: dropping it is how an MR is marked ready.
_DRAFT_PREFIX = re.compile(r"^\s*(?:draft:|\[draft\]|\(draft\)|draft\s+-|wip:)", re.IGNORECASE)
# A job retried one by one: only a finished one can be (a running or queued job is already on its way).
# A merge-request pipeline builds `refs/merge-requests/<iid>/head` (or `/merge`, `/train`), not a branch.
_MR_REF = re.compile(r"refs/merge-requests/(\d+)/[a-z]+")


def outcome(status: "str | None") -> "tuple[str, str | None]":
    """A pipeline's or job's `status`. `manual`/`scheduled`/`created`… wait on something: `queued`."""
    if status in _CONCLUSION:
        return "completed", _CONCLUSION[status]
    return ("in_progress" if status in _RUNNING else "queued"), None


def _run(row: dict) -> Run:
    return Run(str(row["id"]), row.get("sha") or "", row.get("source") or "", *outcome(row.get("status")),
               row.get("created_at") or "", branch="" if row.get("tag") else row.get("ref") or "")


def _diff_path(row: dict) -> str:
    """The path one `/diffs` entry changed: the old one for a deleted file."""
    return (row.get("old_path") if row.get("deleted_file") else row.get("new_path")) or row.get("old_path") or ""


def _diff_row(row: dict) -> "tuple[str, int | None, int | None]":
    """`(path, additions, deletions)` for one `/diffs` entry, counted off its unified diff hunks."""
    path = _diff_path(row)
    text = row.get("diff") or ""
    if row.get("too_large") or (row.get("collapsed") and not text):
        return path, None, None
    lines = text.splitlines()
    return (path, sum(1 for line in lines if line.startswith("+")),
            sum(1 for line in lines if line.startswith("-")))


def _armed(mr: dict) -> bool:
    """Auto-merge armed: GitLab >= 17 names `auto_merge_enabled`, older releases `merge_when_pipeline_succeeds`."""
    return bool(mr.get("merge_when_pipeline_succeeds") or mr.get("auto_merge_enabled"))


def merge_state(status: str, checks: str) -> str:
    """`detailed_merge_status` (+ the pipeline rollup, for why `ci_must_pass` holds it)."""
    if status == "mergeable":
        return "clean"
    if status in _DIRTY:
        return "dirty"
    if status in _PENDING:
        return "pending"
    if status == "ci_must_pass":
        return "checks_failed" if checks == "failure" else "pending"
    if status in _REVIEW:
        return "review_required"
    if status in _BLOCKED:
        return "blocked"
    return "unknown"


class GitLab(Adapter):
    @property
    def _project(self) -> str:
        return f"projects/{urllib.parse.quote(self.login.repo_path, safe='')}"

    def _mr(self, iid) -> str:
        return f"{self._project}/merge_requests/{iid}"

    def create(self, *, title, body, head, base, draft):
        data = self.client.json("POST", f"{self._project}/merge_requests", json_body={
            "source_branch": head, "target_branch": base, "title": f"Draft: {title}" if draft else title,
            "description": body, "squash": False})
        return self.view(int(data["iid"]))

    def find(self, head):
        rows = self.client.get(f"{self._project}/merge_requests", params={
            "source_branch": head, "state": "all", "order_by": "created_at", "sort": "desc", "per_page": 1})
        if not rows:
            raise ForgeError(f"no GitLab MR from branch '{head}'")
        return int(rows[0]["iid"])

    def view(self, number):
        mr = self.client.get(self._mr(number))
        pipeline = mr.get("head_pipeline") or {}
        status = pipeline.get("status")
        checks = _ROLLUP.get(status, "pending") if status else "none"
        draft = mr["draft"] if "draft" in mr else mr.get("work_in_progress")
        return PullRequest(
            number=int(mr["iid"]), url=mr.get("web_url") or "", state=_STATE.get(mr.get("state"), "closed"),
            merge_state=merge_state(mr.get("detailed_merge_status") or "", checks), checks=checks,
            draft=bool(draft), auto_merge=_armed(mr),
            head=mr["source_branch"], base=mr["target_branch"],
            extra={"head_sha": mr.get("sha") or "", "pipeline_id": pipeline.get("id"),
                   "body": mr.get("description") or ""},
        )

    def checks(self, number):
        pipeline_id = self.view(number).extra["pipeline_id"]
        if pipeline_id is None:
            return []
        out: list[Check] = []
        for job in self.client.get(f"{self._project}/pipelines/{pipeline_id}/jobs", params={"per_page": 100}):
            status = job.get("status")
            if status == "success":
                state = "success"
            elif status == "failed":
                state = "skipped" if job.get("allow_failure") else "failure"
            elif status == "canceled":
                state = "failure"
            elif status in ("skipped", "manual"):
                state = "skipped"
            else:
                state = "pending"
            # A job allowed to fail never holds the merge: GitLab's own gating rule.
            out.append(Check(str(job["id"]), job.get("name") or "", state, job.get("web_url") or "",
                             not job.get("allow_failure")))
        return out

    def _require_merge_commits(self) -> None:
        """A merge commit only: a fast-forward or semi-linear project would land the head without one."""
        project = self.client.get(self._project)
        method = project.get("merge_method")
        if method != "merge":
            raise PolicyRefusal(f"this GitLab project merges by {method!r}, not with a merge commit — "
                                "refusing; a maintainer can set Settings → Merge requests → Merge commit")
        if project.get("squash_option") == "always":
            # `squash: false` is ignored when the project forces squashing.
            raise PolicyRefusal("this GitLab project always squashes — refusing; a maintainer can change "
                                "Settings → Merge requests → Squash commits when merging")

    def auto_merge(self, pr):
        self._require_merge_commits()
        # GitLab >= 17 reads `auto_merge`; older releases the deprecated `merge_when_pipeline_succeeds`.
        self.client.json("PUT", f"{self._mr(pr.number)}/merge",
                         json_body={"auto_merge": True, "merge_when_pipeline_succeeds": True, "squash": False})
        return self.view(pr.number)

    def disable_auto_merge(self, pr):
        # Cancels auto-merge on GitLab >= 17 too.
        self.client.json("POST", f"{self._mr(pr.number)}/cancel_merge_when_pipeline_succeeds")
        return self.view(pr.number)

    def merge(self, pr):
        self._require_merge_commits()
        body = {"squash": False}
        if pr.extra.get("head_sha"):
            body["sha"] = pr.extra["head_sha"]  # refuse to merge a head that moved since the checks passed
        self.client.json("PUT", f"{self._mr(pr.number)}/merge", json_body=body)
        return self.view(pr.number)

    def edit(self, number, *, title, body):
        # Never `state_event` here: closing is the separate `close` method.
        patch = {k: v for k, v in (("title", title), ("description", body)) if v is not None}
        self.client.json("PUT", self._mr(number), json_body=patch)
        return self.view(number)

    def comment(self, number, body):
        data = self.client.json("POST", f"{self._mr(number)}/notes", json_body={"body": body})
        return {"number": number, "url": f"{self.login.web_url}/-/merge_requests/{number}#note_{data.get('id')}"}

    def ready(self, number):
        title = self.client.get(self._mr(number)).get("title") or ""
        if _DRAFT_PREFIX.match(title):
            ready_title = title
            while _DRAFT_PREFIX.match(ready_title):  # GitLab reads `Draft: Draft: x` as a draft too
                ready_title = _DRAFT_PREFIX.sub("", ready_title, count=1).lstrip()
            self.client.json("PUT", self._mr(number), json_body={"title": ready_title})
        return self.view(number)

    def close(self, number):
        self.client.json("PUT", self._mr(number), json_body={"state_event": "close"})
        return self.view(number)

    def _pages(self, path: str, params: dict, per_page: int):
        page = 1
        while True:
            rows = self.client.get(path, params={**params, "per_page": per_page, "page": page}) or []
            yield rows
            if len(rows) < per_page:
                return
            page += 1

    def runs(self, workflow, branch, limit):
        # A project has ONE CI config, so its file (or no workflow) names every pipeline; anything else is a
        # `workflow:name`.
        params = {"order_by": "id", "sort": "desc"}
        if workflow is not None and not is_ci_file(workflow):
            params["name"] = workflow
        if branch:
            params["ref"] = branch
        return [_run(r) for r in take(self._pages(f"{self._project}/pipelines", params, min(limit, PAGE_MAX)), limit)]

    def run(self, run_id):
        if not is_id(run_id):
            raise ForgeError(f"'{run_id}' is not a GitLab pipeline id")
        head = _run(self.client.get(f"{self._project}/pipelines/{run_id}"))
        jobs, notes = [], []
        for row in every(self._pages(f"{self._project}/pipelines/{run_id}/jobs", {}, PAGE_MAX)):
            name = row.get("name") or ""
            jobs.append(Job(str(row["id"]), name, *outcome(row.get("status"))))
            # GitLab has no annotation lines; its one warning is a job that failed but was allowed to.
            if row.get("status") == "failed" and row.get("allow_failure"):
                reason = row.get("failure_reason") or "failed"
                notes.append(Annotation("warning", f"failed but allowed to fail ({reason})", name))
        return head, jobs, notes

    def run_head(self, run_id):
        if not is_id(run_id):
            raise ForgeError(f"'{run_id}' is not a GitLab pipeline id")
        row = self.client.get(f"{self._project}/pipelines/{run_id}")
        head = _run(row)
        found = _MR_REF.fullmatch(row.get("ref") or "")
        if found:
            # The MR's source branch — only when it lives in THIS project (a fork chose its own name).
            mr = self.client.get(self._mr(found.group(1)))
            own = mr.get("source_project_id") == mr.get("target_project_id")
            head = dataclasses.replace(head, branch=(mr.get("source_branch") or "") if own else "")
        return head

    def rerun(self, run, failed_only):
        if not is_id(run.id):
            raise ForgeError(f"'{run.id}' is not a GitLab pipeline id")
        if failed_only:
            # Retries the pipeline's failed and canceled jobs, in place.
            self.client.json("POST", f"{self._project}/pipelines/{run.id}/retry")
            return run.id
        # GitLab has no "re-run everything" in place (retrying finished jobs one by one would replay a played
        # manual job and ignore stage order): a fresh pipeline on the same branch, as a push would fire.
        data = self.client.json("POST", f"{self._project}/pipeline", params={"ref": run.branch})
        return str(data["id"])

    def prs(self, state, author, limit, head=None, base=None):
        params = {"state": _MR_STATE[state], "order_by": "created_at", "sort": "desc"}
        if author:
            params["author_username"] = author
        if head:
            params["source_branch"] = head
        if base:
            params["target_branch"] = base
        keep = (lambda mr: mr.get("state") not in ("opened", "locked")) if state == "closed" else None
        per_page = PAGE_MAX if keep else min(limit, PAGE_MAX)
        rows = take(self._pages(f"{self._project}/merge_requests", params, per_page), limit, keep)
        # A listed MR carries no head pipeline, so no checks or merge state: the row says only what the listing does.
        return [listed_item(number=int(mr["iid"]), title=mr.get("title") or "", head=mr.get("source_branch") or "",
                            base=mr.get("target_branch") or "", author=(mr.get("author") or {}).get("username") or "",
                            state=_STATE.get(mr.get("state"), "closed"), url=mr.get("web_url") or "",
                            auto_merge=_armed(mr))
                for mr in rows]

    def diff_stats(self, number):
        # GitLab names no line counts: they are counted off each file's diff text. A diff GitLab withholds
        # (`too_large`, or `collapsed` with no text) has none — that file counts as a file only.
        # Counted as read, so no diff text outlives its page.
        rows, more = capped(_diff_row(r) for page in self._pages(f"{self._mr(number)}/diffs", {}, PAGE_MAX)
                            for r in page)
        return diff_stats("gitlab", rows, TRUNCATED if more else None)

    def provenance(self, number):
        mr = self.client.get(self._mr(number))
        merged = mr.get("state") == "merged"
        # A merge commit, else the squash commit, else (a fast-forward merge) the head itself landed.
        merge_sha = mr.get("merge_commit_sha") or mr.get("squash_commit_sha") or mr.get("sha")
        rows, more_commits = capped((r for page in self._pages(f"{self._mr(number)}/commits", {}, PAGE_MAX)
                                     for r in page), MAX_PR_COMMITS)
        paths, more_files = capped(_diff_path(r) for page in self._pages(f"{self._mr(number)}/diffs", {}, PAGE_MAX)
                                   for r in page)
        # GitLab lists newest first; oldest first, as GitHub does. It names no account per commit.
        commits = [commit(r["id"], r.get("author_name") or "", None, r.get("message") or "") for r in reversed(rows)]
        # `changes_count` is a string, `"1000+"` past GitLab's own cap (MORE than 1000).
        count = str(mr.get("changes_count") or "")
        digits = count.rstrip("+")
        short = digits.isdigit() and (len(paths) <= int(digits) if count.endswith("+") else len(paths) < int(digits))
        return provenance("gitlab", number, merged=merged, merge_sha=merge_sha, commits=commits,
                          more_commits=more_commits, files=paths, more_files=more_files or short)

    def ci_log(self, check_id):
        if not is_id(check_id):
            raise ForgeError(f"'{check_id}' is not a GitLab job id")
        return self.client.text(f"{self._project}/jobs/{check_id}/trace")
