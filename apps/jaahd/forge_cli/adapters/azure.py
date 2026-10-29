"""Azure DevOps Services: REST 7.1 under `https://dev.azure.com/{org}/{project}/_apis`.

Checks are the PR's statuses plus its BUILD policy evaluations; every other enabled policy evaluation is a
merge gate (a reviewer policy, a work-item link…), not a check; a build policy that is not blocking is an
optional check. Auto-merge is the native auto-complete; `close` abandons the PR.
"""
from __future__ import annotations

import urllib.parse

from ..model import Annotation, Check, ForgeError, Job, PullRequest, Run, is_id, required_rollup, strip_heads
from .base import PAGE_MAX, TRUNCATED, Adapter, capped, diff_stats, listed_item, take

_V = {"api-version": "7.1"}
# The identity a cancelled auto-complete is set to (and may read back as): nobody, so not armed.
_NOBODY = "00000000-0000-0000-0000-000000000000"
_V_PREVIEW = {"api-version": "7.1-preview.1"}
_BUILD_POLICY = "0609b952-1397-4640-95ec-e00a01b2c241"
_STATE = {"active": "open", "completed": "merged", "abandoned": "closed"}
_STATUS = {"succeeded": "success", "failed": "failure", "error": "failure", "notApplicable": "skipped"}
_EVALUATION = {"approved": "success", "rejected": "failure", "broken": "failure", "notApplicable": "skipped",
               "queued": "pending", "running": "pending"}
# Always a merge commit; the source branch is the runner's to keep.
_COMPLETION = {"mergeStrategy": "noFastForward", "deleteSourceBranch": False}

# A build's `result`, and a timeline record's. `partiallySucceeded` is a task that failed and carried on.
_RESULT = {"succeeded": "success", "succeededWithIssues": "success", "partiallySucceeded": "failure",
           "failed": "failure", "canceled": "cancelled", "abandoned": "cancelled", "skipped": "skipped",
           "none": "skipped"}
_ISSUE = {"error": "error", "warning": "warning"}
# Iteration Changes' documented `$top` maximum is 2000: the 3,000-file cap is `capped`'s, across pages.
_CHANGES_PAGE = 1000
NO_LINE_COUNTS = "Azure DevOps reports no per-file line counts; files counted only"
_PR_STATUS = {"open": "active", "closed": "all", "all": "all"}  # `closed` = completed + abandoned: filtered here


def outcome(status: "str | None", result: "str | None") -> "tuple[str, str | None]":
    """A build's `status`/`result`, or a timeline record's `state`/`result`."""
    if status == "completed":
        return "completed", _RESULT.get(result or "", "failure")
    return ("in_progress" if status in ("inProgress", "cancelling") else "queued"), None


def _branch(row: dict) -> str:
    """The branch a build built: a PR build's `refs/pull/N/merge` names the PR's source branch in `triggerInfo`."""
    ref = row.get("sourceBranch") or ""
    if ref.startswith("refs/pull/"):
        info = row.get("triggerInfo") or {}
        if str(info.get("pr.isFork", "")).lower() == "true":
            return ""  # a fork chose that name
        return strip_heads(info.get("pr.sourceBranch") or "")
    return strip_heads(ref) if ref.startswith("refs/heads/") else ""  # a tag names no branch


def _run(row: dict) -> Run:
    return Run(str(row["id"]), row.get("sourceVersion") or "", row.get("reason") or "",
               *outcome(row.get("status"), row.get("result")), row.get("queueTime") or "", branch=_branch(row))


def _armed(pr: dict) -> bool:
    """Auto-complete set by someone (a cancelled one may read back as `_NOBODY`)."""
    return (pr.get("autoCompleteSetBy") or {}).get("id") not in (None, "", _NOBODY)


def merge_state(status: str, merge_status: str, draft: bool, checks: str,
                gates: "list[tuple[str, bool, str]]") -> str:
    """`status` + `mergeStatus` + the check rollup + the non-build policy gates `(name, blocking, status)`."""
    if status != "active":
        return "unknown"
    if merge_status == "conflicts":
        return "dirty"
    if checks == "failure":
        return "checks_failed"
    if merge_status in ("queued", "notSet") or checks == "pending":
        return "pending"
    for name, blocking, state in gates:
        if blocking and state not in ("approved", "notApplicable"):
            return "review_required" if "reviewer" in name.lower() else "blocked"
    if merge_status == "rejectedByPolicy" or draft:
        return "blocked"
    if merge_status == "succeeded":
        return "clean"
    return "unknown"


class Azure(Adapter):
    @property
    def _repo(self) -> str:
        name = self.login.repo_path.rsplit("/", 1)[-1]
        return f"git/repositories/{urllib.parse.quote(name, safe='')}"

    def _web(self, number: int) -> str:
        return f"{self.login.web_url}/pullrequest/{number}"

    def create(self, *, title, body, head, base, draft):
        data = self.client.json("POST", f"{self._repo}/pullrequests", params=_V, json_body={
            "sourceRefName": f"refs/heads/{head}", "targetRefName": f"refs/heads/{base}",
            "title": title, "description": body, "isDraft": draft})
        return self.view(int(data["pullRequestId"]))

    def find(self, head):
        data = self.client.get(f"{self._repo}/pullrequests", params={
            "searchCriteria.sourceRefName": f"refs/heads/{head}", "searchCriteria.status": "all", "$top": 1, **_V})
        rows = data.get("value") or []
        if not rows:
            raise ForgeError(f"no Azure DevOps PR from branch '{head}'")
        return int(rows[0]["pullRequestId"])

    def _pr(self, number: int) -> dict:
        return self.client.get(f"{self._repo}/pullrequests/{number}", params=_V)

    def _checks_and_gates(self, number: int, pr: dict):
        latest: dict[str, dict] = {}
        data = self.client.get(f"{self._repo}/pullRequests/{number}/statuses", params=_V)
        for status in data.get("value") or []:
            ctx = status.get("context") or {}
            key = f"{ctx.get('genre') or ''}/{ctx.get('name') or ''}"
            if key not in latest or int(status.get("id") or 0) > int(latest[key].get("id") or 0):
                latest[key] = status
        checks = [Check(f"status-{s.get('id')}", (s.get("context") or {}).get("name") or "",
                        _STATUS.get(s.get("state") or "", "pending"), s.get("targetUrl") or "")
                  for s in latest.values()]
        gates: list[tuple[str, bool, str]] = []
        if pr.get("status") != "active":
            return checks, gates  # a settled PR's policies no longer say anything
        project_id = ((pr.get("repository") or {}).get("project") or {}).get("id") or ""
        data = self.client.get("policy/evaluations", params={
            "artifactId": f"vstfs:///CodeReview/CodeReviewId/{project_id}/{number}", **_V_PREVIEW})
        for ev in data.get("value") or []:
            config = ev.get("configuration") or {}
            if not config.get("isEnabled"):
                continue
            kind = config.get("type") or {}
            if kind.get("id") == _BUILD_POLICY:
                ctx = ev.get("context") or {}
                check_id = str(ctx["buildId"]) if ctx.get("buildId") else f"evaluation-{ev.get('evaluationId')}"
                checks.append(Check(check_id, ctx.get("buildDefinitionName") or kind.get("displayName") or "",
                                    _EVALUATION.get(ev.get("status") or "", "pending"),
                                    required=bool(config.get("isBlocking"))))
            else:
                gates.append((kind.get("displayName") or "", bool(config.get("isBlocking")), ev.get("status") or ""))
        return checks, gates

    def view(self, number):
        pr = self._pr(number)
        checks, gates = self._checks_and_gates(number, pr)
        roll = required_rollup(checks)  # an optional build policy does not gate
        status = pr.get("status") or ""
        draft = bool(pr.get("isDraft"))
        return PullRequest(
            number=int(pr["pullRequestId"]), url=self._web(number), state=_STATE.get(status, "closed"),
            merge_state=merge_state(status, pr.get("mergeStatus") or "", draft, roll, gates),
            checks=roll, draft=draft, auto_merge=_armed(pr),
            head=strip_heads(pr.get("sourceRefName") or ""), base=strip_heads(pr.get("targetRefName") or ""),
            extra={"head_sha": (pr.get("lastMergeSourceCommit") or {}).get("commitId") or "",
                   "project_id": ((pr.get("repository") or {}).get("project") or {}).get("id") or "",
                   "body": pr.get("description") or ""},
        )

    def checks(self, number):
        return self._checks_and_gates(number, self._pr(number))[0]

    def auto_merge(self, pr):
        org = self.client.api_base.rsplit("/", 2)[0]  # drop `/{project}/_apis`
        me = self.client.get(f"{org}/_apis/connectionData", params=_V_PREVIEW)
        uid = (me.get("authenticatedUser") or {}).get("id")
        if not uid:
            raise ForgeError("Azure DevOps connectionData names no authenticated user")
        self.client.json("PATCH", f"{self._repo}/pullrequests/{pr.number}", params=_V, json_body={
            "autoCompleteSetBy": {"id": uid}, "completionOptions": _COMPLETION})
        return self.view(pr.number)

    def disable_auto_merge(self, pr):
        # Azure's documented cancel: auto-complete set by the empty identity.
        self.client.json("PATCH", f"{self._repo}/pullrequests/{pr.number}", params=_V, json_body={
            "autoCompleteSetBy": {"id": _NOBODY}})
        return self.view(pr.number)

    def merge(self, pr):
        # The head the checks passed on: Azure refuses the completion if the source moved since.
        self.client.json("PATCH", f"{self._repo}/pullrequests/{pr.number}", params=_V, json_body={
            "status": "completed", "lastMergeSourceCommit": {"commitId": pr.extra.get("head_sha") or ""},
            "completionOptions": _COMPLETION})
        return self.view(pr.number)

    def edit(self, number, *, title, body):
        patch = {k: v for k, v in (("title", title), ("description", body)) if v is not None}
        self.client.json("PATCH", f"{self._repo}/pullrequests/{number}", params=_V, json_body=patch)
        return self.view(number)

    def ready(self, number):
        self.client.json("PATCH", f"{self._repo}/pullrequests/{number}", params=_V, json_body={"isDraft": False})
        return self.view(number)

    def close(self, number):
        self.client.json("PATCH", f"{self._repo}/pullrequests/{number}", params=_V,
                         json_body={"status": "abandoned"})
        return self.view(number)

    def comment(self, number, body):
        data = self.client.json("POST", f"{self._repo}/pullRequests/{number}/threads", params=_V, json_body={
            "comments": [{"parentCommentId": 0, "content": body, "commentType": 1}], "status": 1})
        return {"number": number, "url": f"{self._web(number)}?discussionId={data.get('id')}"}

    def runs(self, workflow, branch, limit):
        # A YAML pipeline is a build DEFINITION that reads the file: the file names the definition(s); no
        # file, every definition. Either listing spans the PROJECT: keep only the definitions that build this repo.
        repo = self.login.repo_path.rsplit("/", 1)[-1].lower()
        query = {"includeAllProperties": "true", **_V}
        if workflow is not None:
            query = {"yamlFilename": workflow, **query}
        found = [d for d in self.client.get("build/definitions", params=query).get("value") or []
                 if ((d.get("repository") or {}).get("name") or "").lower() == repo]
        if not found:
            if workflow is None:
                raise ForgeError(f"no Azure Pipelines definition builds {repo}")
            raise ForgeError(f"no Azure Pipelines definition of {repo} reads '{workflow}'")
        params = {"definitions": ",".join(str(d["id"]) for d in found), "queryOrder": "queueTimeDescending",
                  "$top": limit, **_V}
        if branch:
            params["branchName"] = f"refs/heads/{branch}"
        return [_run(r) for r in (self.client.get("build/builds", params=params).get("value") or [])[:limit]]

    def run_head(self, run_id):
        """The build alone — and only a build of THIS repo: a build id is project-wide, not per repo."""
        if not is_id(run_id):
            raise ForgeError(f"'{run_id}' is not an Azure Pipelines build id")
        row = self.client.get(f"build/builds/{run_id}", params=_V)
        repo = self.login.repo_path.rsplit("/", 1)[-1].lower()
        if ((row.get("repository") or {}).get("name") or "").lower() != repo:
            raise ForgeError(f"build {run_id} does not build {self.login.repo_path}")
        return _run(row)

    def run(self, run_id):
        head = self.run_head(run_id)
        records = self.client.get(f"build/builds/{run_id}/timeline", params=_V).get("records") or []
        by_id = {r.get("id"): r for r in records}

        def job_of(record: dict) -> str:
            seen = set()
            while record and record.get("type") != "Job" and record.get("id") not in seen:
                seen.add(record.get("id"))
                record = by_id.get(record.get("parentId"))
            return (record or {}).get("name") or ""

        jobs = [Job(r["id"], r.get("name") or "", *outcome(r.get("state"), r.get("result")))
                for r in records if r.get("type") == "Job"]
        # `##vso[task.logissue]` lines (and a task's own warnings/errors) are the records' `issues`.
        notes = [Annotation(_ISSUE.get(i.get("type") or "", "notice"), i.get("message") or "", job_of(r))
                 for r in records for i in r.get("issues") or []]
        return head, jobs, notes

    def rerun(self, run, failed_only):
        if not is_id(run.id):
            raise ForgeError(f"'{run.id}' is not an Azure Pipelines build id")
        if failed_only:
            # Retries the failed jobs of the same build, in place.
            self.client.json("PATCH", f"build/builds/{run.id}", params={"retry": "true", **_V}, json_body={})
            return run.id
        # A full re-run is a new build of the same definition, branch and commit.
        build = self.client.get(f"build/builds/{run.id}", params=_V)
        data = self.client.json("POST", "build/builds", params=_V, json_body={
            "definition": {"id": (build.get("definition") or {}).get("id")},
            "sourceBranch": build.get("sourceBranch"), "sourceVersion": build.get("sourceVersion")})
        return str(data["id"])

    def prs(self, state, author, limit, head=None, base=None):
        def wanted(pr: dict) -> bool:
            if state == "closed" and pr.get("status") == "active":
                return False
            who = pr.get("createdBy") or {}
            return not author or author.lower() in ((who.get("uniqueName") or "").lower(),
                                                    (who.get("displayName") or "").lower())

        filtered = state == "closed" or bool(author)
        size = PAGE_MAX if filtered else min(limit, PAGE_MAX)

        criteria = {"searchCriteria.status": _PR_STATUS[state]}
        if head:
            criteria["searchCriteria.sourceRefName"] = f"refs/heads/{head}"
        if base:
            criteria["searchCriteria.targetRefName"] = f"refs/heads/{base}"

        def pages():
            skip = 0
            while True:
                rows = self.client.get(f"{self._repo}/pullrequests", params={
                    **criteria, "$top": size, "$skip": skip, **_V}).get("value") or []
                yield rows
                if len(rows) < size:
                    return
                skip += size

        # A listed PR carries no statuses or policy evaluations, so no checks or merge state: the row says only what
        # the listing does.
        return [listed_item(number=int(pr["pullRequestId"]), title=pr.get("title") or "",
                            head=strip_heads(pr.get("sourceRefName") or ""),
                            base=strip_heads(pr.get("targetRefName") or ""),
                            author=(pr.get("createdBy") or {}).get("uniqueName") or "",
                            state=_STATE.get(pr.get("status") or "", "closed"),
                            url=self._web(int(pr["pullRequestId"])), auto_merge=_armed(pr))
                for pr in take(pages(), limit, wanted)]

    def diff_stats(self, number):
        # Azure DevOps serves no per-file line counts (Pull Request Iteration Changes 7.1 names a path and a
        # change type only): files are counted, lines are null, and the result is `partial`.
        iterations = self.client.get(f"{self._repo}/pullRequests/{number}/iterations", params=_V)
        rows = iterations.get("value") if isinstance(iterations, dict) else iterations
        if not rows:
            raise ForgeError(f"Azure DevOps PR #{number} has no iterations")
        last = max(int(it["id"]) for it in rows)

        def changes():
            skip = 0
            while True:
                data = self.client.get(f"{self._repo}/pullRequests/{number}/iterations/{last}/changes", params={
                    "$top": _CHANGES_PAGE, "$skip": skip, "$compareTo": 0, **_V})
                yield from data.get("changeEntries") or []
                following = int(data.get("nextSkip") or 0)
                if not data.get("nextTop") or following <= skip:
                    return
                skip = following

        files = (c for c in changes()
                 if not (c.get("item") or {}).get("isFolder") and (c.get("item") or {}).get("gitObjectType") != "tree")
        rows, more = capped(files)
        return diff_stats("azure_devops", (((c.get("item") or {}).get("path") or c.get("originalPath") or "", None, None)
                                           for c in rows),
                          TRUNCATED if more else NO_LINE_COUNTS)

    def ci_log(self, check_id):
        if not is_id(check_id):
            raise ForgeError(f"'{check_id}' is not an Azure Pipelines build: it has no log here")
        logs = self.client.get(f"build/builds/{check_id}/logs", params=_V)
        parts = []
        for log in logs.get("value") or []:
            text = self.client.text(f"build/builds/{check_id}/logs/{log['id']}", params=_V)
            parts.append(f"=== log {log['id']} ===\n{text}")
        return "\n".join(parts)
