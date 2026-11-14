"""GitLab: REST v4 (gitlab.com and self-managed), the project addressed by its URL-encoded path."""
from __future__ import annotations

import re
import dataclasses
import urllib.parse

from ..model import Annotation, Check, ForgeError, Job, PolicyRefusal, PullRequest, Run
from .base import (MAX_MERGED_FILES, MAX_PAGES, MAX_PR_COMMITS, PAGE_MAX, TRUNCATED, Adapter, Started, capped, commit,
                   diff_stats, every, is_ci_file, iso, listed_item, merged_item, newest_merged, parse_time, provenance,
                   take)

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
# A commit status (`repository/commits/:sha/statuses`): `manual` waits on a person, so it is skipped as a job is.
_COMMIT_STATUS = {"success": "success", "failed": "failure", "canceled": "failure", "skipped": "skipped",
                  "manual": "skipped", "pending": "pending", "running": "pending", "created": "pending"}
_MR_STATE = {"open": "opened", "closed": "all", "all": "all"}  # `closed` = closed + merged: filtered here
# GitLab's draft flag IS a title prefix: dropping it is how an MR is marked ready.
_DRAFT_PREFIX = re.compile(r"^\s*(?:draft:|\[draft\]|\(draft\)|draft\s+-|wip:)", re.IGNORECASE)
# A job retried one by one: only a finished one can be (a running or queued job is already on its way).
# A merge-request pipeline builds `refs/merge-requests/<iid>/head` (or `/merge`, `/train`), not a branch.
_MR_REF = re.compile(r"refs/merge-requests/(\d+)/[a-z]+")
# The pipeline variable `run dispatch` sets to the job a gate is judged on (`*` = the whole pipeline).
GATE_VARIABLE = "JAAH_GATE"


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
    DESCRIPTION = "description"
    PR_NAME = "GitLab MR"
    NUMBER = "iid"
    PRS = "merge_requests"
    UPDATE = "PUT"
    CLOSE = {"state_event": "close"}

    @property
    def _repo(self) -> str:
        return f"projects/{urllib.parse.quote(self.login.repo_path, safe='')}"

    def _create_payload(self, title, body, head, base, draft):
        return {"source_branch": head, "target_branch": base, "title": f"Draft: {title}" if draft else title,
                "description": body, "squash": False}

    def _find_rows(self, head):
        return self.client.get(self._pr_path(), params={
            "source_branch": head, "state": "all", "order_by": "created_at", "sort": "desc", "per_page": 1})

    def view(self, number):
        mr = self.client.get(self._pr_path(number))
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
        for job in self.client.get(f"{self._repo}/pipelines/{pipeline_id}/jobs", params={"per_page": 100}):
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

    def checks_for_sha(self, sha):
        # The latest status per name (no `all`): a retried job's earlier attempt no longer counts.
        rows = every(self._pages(f"{self._repo}/repository/commits/{sha}/statuses", {}, PAGE_MAX))
        return [Check(str(r["id"]), r.get("name") or "", _COMMIT_STATUS.get(r.get("status") or "", "pending"),
                      r.get("target_url") or "") for r in rows]

    def _require_merge_commits(self) -> None:
        """A merge commit only: a fast-forward or semi-linear project would land the head without one."""
        project = self.client.get(self._repo)
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
        self.client.json("PUT", f"{self._pr_path(pr.number)}/merge",
                         json_body={"auto_merge": True, "merge_when_pipeline_succeeds": True, "squash": False})
        return self.view(pr.number)

    def disable_auto_merge(self, pr):
        # Cancels auto-merge on GitLab >= 17 too.
        self.client.json("POST", f"{self._pr_path(pr.number)}/cancel_merge_when_pipeline_succeeds")
        return self.view(pr.number)

    def merge(self, pr):
        self._require_merge_commits()
        body = {"squash": False}
        if pr.extra.get("head_sha"):
            body["sha"] = pr.extra["head_sha"]  # refuse to merge a head that moved since the checks passed
        self.client.json("PUT", f"{self._pr_path(pr.number)}/merge", json_body=body)
        return self.view(pr.number)

    def comment(self, number, body):
        data = self.client.json("POST", f"{self._pr_path(number)}/notes", json_body={"body": body})
        return {"number": number, "url": f"{self.login.web_url}/-/merge_requests/{number}#note_{data.get('id')}"}

    def ready(self, number):
        title = self.client.get(self._pr_path(number)).get("title") or ""
        if _DRAFT_PREFIX.match(title):
            ready_title = title
            while _DRAFT_PREFIX.match(ready_title):  # GitLab reads `Draft: Draft: x` as a draft too
                ready_title = _DRAFT_PREFIX.sub("", ready_title, count=1).lstrip()
            return self._update(number, {"title": ready_title})
        return self.view(number)

    def runs(self, workflow, branch, limit):
        # A project has ONE CI config, so its file (or no workflow) names every pipeline; anything else is a
        # `workflow:name`.
        params = {"order_by": "id", "sort": "desc"}
        if workflow is not None and not is_ci_file(workflow):
            params["name"] = workflow
        if branch:
            params["ref"] = branch
        return [_run(r) for r in take(self._pages(f"{self._repo}/pipelines", params, min(limit, PAGE_MAX)), limit)]

    def run(self, run_id):
        self._id(run_id, "a GitLab pipeline id")
        head = _run(self.client.get(f"{self._repo}/pipelines/{run_id}"))
        jobs, notes = [], []
        for row in every(self._pages(f"{self._repo}/pipelines/{run_id}/jobs", {}, PAGE_MAX)):
            name = row.get("name") or ""
            jobs.append(Job(str(row["id"]), name, *outcome(row.get("status"))))
            # GitLab has no annotation lines; its one warning is a job that failed but was allowed to.
            if row.get("status") == "failed" and row.get("allow_failure"):
                reason = row.get("failure_reason") or "failed"
                notes.append(Annotation("warning", f"failed but allowed to fail ({reason})", name))
        return head, jobs, notes

    def run_head(self, run_id):
        self._id(run_id, "a GitLab pipeline id")
        row = self.client.get(f"{self._repo}/pipelines/{run_id}")
        head = _run(row)
        found = _MR_REF.fullmatch(row.get("ref") or "")
        if found:
            # The MR's source branch — only when it lives in THIS project (a fork chose its own name).
            mr = self.client.get(self._pr_path(found.group(1)))
            own = mr.get("source_project_id") == mr.get("target_project_id")
            head = dataclasses.replace(head, branch=(mr.get("source_branch") or "") if own else "")
        return head

    def rerun(self, run, failed_only):
        self._id(run.id, "a GitLab pipeline id")
        if failed_only:
            # Retries the pipeline's failed and canceled jobs, in place.
            self.client.json("POST", f"{self._repo}/pipelines/{run.id}/retry")
            return run.id
        # GitLab has no "re-run everything" in place (retrying finished jobs one by one would replay a played
        # manual job and ignore stage order): a fresh pipeline on the same branch, as a push would fire.
        data = self.client.json("POST", f"{self._repo}/pipeline", params={"ref": run.branch})
        return str(data["id"])

    def dispatch(self, workflow, ref, inputs):
        # A project has ONE pipeline per ref: it starts whole, `JAAH_GATE` naming the job the gate is judged on
        # (`*`: the whole pipeline), so a job's `rules:` may key on it. Inputs travel as pipeline variables.
        given = {**inputs, GATE_VARIABLE: workflow}
        variables = [{"key": k, "value": v, "variable_type": "env_var"} for k, v in given.items()]
        data = self.client.json("POST", f"{self._repo}/pipeline", json_body={"ref": ref, "variables": variables})
        return Started(str(data["id"]), data.get("web_url") or "", data.get("sha") or "")

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
        rows = take(self._pages(self._pr_path(), params, per_page), limit, keep)
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
        rows, more = capped(_diff_row(r) for r in self._rows(f"{self._pr_path(number)}/diffs", {}, PAGE_MAX))
        return diff_stats("gitlab", rows, TRUNCATED if more else None)

    def provenance(self, number):
        mr = self.client.get(self._pr_path(number))
        merged = mr.get("state") == "merged"
        # A merge commit, else the squash commit, else (a fast-forward merge) the head itself landed.
        merge_sha = mr.get("merge_commit_sha") or mr.get("squash_commit_sha") or mr.get("sha")
        rows, more_commits = capped(self._rows(f"{self._pr_path(number)}/commits", {}, PAGE_MAX), MAX_PR_COMMITS)
        paths, more_files = capped(_diff_path(r) for r in self._rows(f"{self._pr_path(number)}/diffs", {}, PAGE_MAX))
        # GitLab lists newest first; oldest first, as GitHub does. It names no account per commit.
        commits = [commit(r["id"], r.get("author_name") or "", None, r.get("message") or "") for r in reversed(rows)]
        # `changes_count` is a string, `"1000+"` past GitLab's own cap (MORE than 1000).
        count = str(mr.get("changes_count") or "")
        digits = count.rstrip("+")
        short = digits.isdigit() and (len(paths) <= int(digits) if count.endswith("+") else len(paths) < int(digits))
        return provenance("gitlab", number, merged=merged, merge_sha=merge_sha, commits=commits,
                          more_commits=more_commits, files=paths, more_files=more_files or short)

    def merged_prs(self, since, limit):
        # `updated_after` bounds the listing server-side (a merge is an update); `merged_at` decides.
        params = {"state": "merged", "updated_after": iso(since), "order_by": "updated_at", "sort": "desc"}
        rows = take(self._pages(self._pr_path(), params, PAGE_MAX), MAX_PAGES * PAGE_MAX)
        return [merged_item(number=int(mr["iid"]), url=mr.get("web_url") or "", title=mr.get("title") or "",
                            body=mr.get("description"), merged_at=when,
                            files=[_diff_path(d) for d in self.client.get(
                                f"{self._pr_path(mr['iid'])}/diffs", params={"per_page": MAX_MERGED_FILES})])
                for when, mr in newest_merged(rows, lambda mr: parse_time(mr.get("merged_at")), since, limit)]

    def ci_log(self, check_id):
        self._id(check_id, "a GitLab job id")
        return self.client.text(f"{self._repo}/jobs/{check_id}/trace")

    # A project's CI/CD variables: the repository level is environment scope `*`, `--env E` is scope `E`; a
    # secret is a masked variable, so `kind` splits one listing by its `masked` flag.
    VARIABLE_RIGHTS = "the Maintainer role on the project, through a token with the `api` scope"

    def variables(self, kind, env):
        scope, masked = env or "*", kind == "secret"
        rows = every(self._pages(f"{self._repo}/variables", {}, PAGE_MAX))
        return [{"name": r["key"], **({} if masked else {"value": r.get("value") or ""})}
                for r in rows if r.get("environment_scope") == scope and bool(r.get("masked")) == masked]

    def set_variable(self, kind, name, value, env):
        scope, masked = env or "*", kind == "secret"
        # `raw`: a value is stored as given, never expanded as `$VAR` references. `masked_and_hidden` (17.4+,
        # create-only; older releases ignore it) keeps a secret out of the UI too.
        return self._upsert_variable(
            "PUT", f"{self._repo}/variables", name, {"value": value, "masked": masked, "raw": True},
            {"key": name, "value": value, "environment_scope": scope, "masked": masked, "masked_and_hidden": masked,
             "protected": False, "raw": True}, params={"filter[environment_scope]": scope})

    def delete_variable(self, kind, name, env):
        scope = {"filter[environment_scope]": env or "*"}
        # Only one of this kind: `secret delete` never removes a plain variable of that name, nor the reverse.
        row = self.client.get(f"{self._repo}/variables/{name}", params=scope)
        if bool(row.get("masked")) != (kind == "secret"):
            raise ForgeError(f"'{name}' in {self.login.repo_path} is not a {kind}")
        self.client.json("DELETE", f"{self._repo}/variables/{name}", params=scope)
        return {}
