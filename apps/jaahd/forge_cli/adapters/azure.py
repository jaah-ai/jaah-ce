"""Azure DevOps Services: REST 7.1 under `https://dev.azure.com/{org}/{project}/_apis`.

Checks are the PR's statuses plus its BUILD policy evaluations; every other enabled policy evaluation is a
merge gate (a reviewer policy, a work-item link…), not a check; a build policy that is not blocking is an
optional check. Auto-merge is the native auto-complete; `close` abandons the PR.
"""
from __future__ import annotations

import urllib.parse
from collections.abc import Iterator

from ..model import Annotation, Check, ForgeError, Job, PullRequest, Run, required_rollup, strip_heads
from .base import (MAX_MERGED_FILES, MAX_PAGES, MAX_PR_COMMITS, PAGE_MAX, TRUNCATED, Adapter, Started, capped, commit,
                   diff_stats, iso, listed_item, merged_item, newest_merged, parse_time, provenance, take)

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


def _path(change: dict) -> str:
    """The repository path one iteration change names, without Azure's leading `/`."""
    return ((change.get("item") or {}).get("path") or change.get("originalPath") or "").lstrip("/")


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
    DESCRIPTION = "description"
    PR_NAME = "Azure DevOps PR"
    NUMBER = "pullRequestId"
    PRS = "pullrequests"
    PARAMS = _V
    CLOSE = {"status": "abandoned"}

    @property
    def _repo(self) -> str:
        name = self.login.repo_path.rsplit("/", 1)[-1]
        return f"git/repositories/{urllib.parse.quote(name, safe='')}"

    def _web(self, number: int) -> str:
        return f"{self.login.web_url}/pullrequest/{number}"

    def _create_payload(self, title, body, head, base, draft):
        return {"sourceRefName": f"refs/heads/{head}", "targetRefName": f"refs/heads/{base}",
                "title": title, "description": body, "isDraft": draft}

    def _find_rows(self, head):
        return self.client.get(self._pr_path(), params={
            "searchCriteria.sourceRefName": f"refs/heads/{head}", "searchCriteria.status": "all", "$top": 1, **_V}
        ).get("value") or []

    def _pr(self, number: int) -> dict:
        return self.client.get(self._pr_path(number), params=_V)

    # A PR's sub-resources (statuses, threads, iterations, commits) are spelled `pullRequests/...` below, as the
    # recorded fixtures match them: not under `_pr_path`, whose collection is `pullrequests`.

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

    def checks_for_sha(self, sha):
        # The latest status per context (`latestOnly`); `pending`/`notSet` read as pending.
        data = self.client.get(f"{self._repo}/commits/{sha}/statuses", params={"latestOnly": "true", **_V})
        return [Check(f"status-{s.get('id')}", (s.get("context") or {}).get("name") or "",
                      _STATUS.get(s.get("state") or "", "pending"), s.get("targetUrl") or "")
                for s in data.get("value") or []]

    def auto_merge(self, pr):
        org = self.client.api_base.rsplit("/", 2)[0]  # drop `/{project}/_apis`
        me = self.client.get(f"{org}/_apis/connectionData", params=_V_PREVIEW)
        uid = (me.get("authenticatedUser") or {}).get("id")
        if not uid:
            raise ForgeError("Azure DevOps connectionData names no authenticated user")
        return self._update(pr.number, {"autoCompleteSetBy": {"id": uid}, "completionOptions": _COMPLETION})

    def disable_auto_merge(self, pr):
        # Azure's documented cancel: auto-complete set by the empty identity.
        return self._update(pr.number, {"autoCompleteSetBy": {"id": _NOBODY}})

    def merge(self, pr):
        # The head the checks passed on: Azure refuses the completion if the source moved since.
        return self._update(pr.number, {"status": "completed", "completionOptions": _COMPLETION,
                                        "lastMergeSourceCommit": {"commitId": pr.extra.get("head_sha") or ""}})

    def ready(self, number):
        return self._update(number, {"isDraft": False})

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
        self._id(run_id, "an Azure Pipelines build id")
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
        self._id(run.id, "an Azure Pipelines build id")
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

    def dispatch(self, workflow, ref, inputs):
        # `workflow` is a pipeline id, project-wide: only a pipeline that builds THIS repo is started.
        self._id(workflow, "an Azure Pipelines pipeline id")
        repo = self.login.repo_path.rsplit("/", 1)[-1].lower()
        definition = self.client.get(f"build/definitions/{workflow}", params=_V)
        if ((definition.get("repository") or {}).get("name") or "").lower() != repo:
            raise ForgeError(f"Azure Pipelines pipeline {workflow} does not build {self.login.repo_path}")
        body: dict = {"resources": {"repositories": {"self": {"refName": f"refs/heads/{ref}"}}}}
        if inputs:
            body["templateParameters"] = inputs
        data = self.client.json("POST", f"pipelines/{workflow}/runs", params=_V, json_body=body)
        run_id = str(data["id"])
        sha = ((((data.get("resources") or {}).get("repositories") or {}).get("self") or {}).get("version")) or ""
        if not sha:
            sha = self.client.get(f"build/builds/{run_id}", params=_V).get("sourceVersion") or ""
        return Started(run_id, ((data.get("_links") or {}).get("web") or {}).get("href") or "", sha)

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

        pages = self._pages(self._pr_path(), {**criteria, **_V}, size, "value", skip=True)
        # A listed PR carries no statuses or policy evaluations, so no checks or merge state: the row says only what
        # the listing does.
        return [listed_item(number=int(pr["pullRequestId"]), title=pr.get("title") or "",
                            head=strip_heads(pr.get("sourceRefName") or ""),
                            base=strip_heads(pr.get("targetRefName") or ""),
                            author=(pr.get("createdBy") or {}).get("uniqueName") or "",
                            state=_STATE.get(pr.get("status") or "", "closed"),
                            url=self._web(int(pr["pullRequestId"])), auto_merge=_armed(pr))
                for pr in take(pages, limit, wanted)]

    def _changes(self, number: int) -> "Iterator[dict]":
        """The PR's last iteration's file changes (folders left out), lazily: against the base (`$compareTo`
        0), so the PR's whole change, not the last push's."""
        iterations = self.client.get(f"{self._repo}/pullRequests/{number}/iterations", params=_V)
        rows = iterations.get("value") if isinstance(iterations, dict) else iterations
        if not rows:
            raise ForgeError(f"Azure DevOps PR #{number} has no iterations")
        last = max(int(it["id"]) for it in rows)
        skip = 0
        while True:
            data = self.client.get(f"{self._repo}/pullRequests/{number}/iterations/{last}/changes", params={
                "$top": _CHANGES_PAGE, "$skip": skip, "$compareTo": 0, **_V})
            for change in data.get("changeEntries") or []:
                item = change.get("item") or {}
                if not item.get("isFolder") and item.get("gitObjectType") != "tree":
                    yield change
            following = int(data.get("nextSkip") or 0)
            if not data.get("nextTop") or following <= skip:
                return
            skip = following

    def diff_stats(self, number):
        # Azure DevOps serves no per-file line counts (Pull Request Iteration Changes 7.1 names a path and a
        # change type only): files are counted, lines are null, and the result is `partial`.
        rows, more = capped(self._changes(number))
        return diff_stats("azure_devops", (((c.get("item") or {}).get("path") or c.get("originalPath") or "", None, None)
                                           for c in rows),
                          TRUNCATED if more else NO_LINE_COUNTS)

    def provenance(self, number):
        pr = self._pr(number)
        # One page one past the cap says whether the list was cut. Azure lists newest first; oldest first, as
        # GitHub does. A commit names an author's name and email, never an account.
        rows = self.client.get(f"{self._repo}/pullRequests/{number}/commits",
                               params={"$top": MAX_PR_COMMITS + 1, **_V}).get("value") or []
        commits = [commit(r["commitId"], (r.get("author") or {}).get("name") or "", None, r.get("comment") or "")
                   for r in reversed(rows[:MAX_PR_COMMITS])]
        paths, more_files = capped(_path(c) for c in self._changes(number))
        return provenance("azure_devops", number, merged=pr.get("status") == "completed",
                          merge_sha=(pr.get("lastMergeCommit") or {}).get("commitId"), commits=commits,
                          more_commits=len(rows) > MAX_PR_COMMITS, files=paths, more_files=more_files)

    def merged_prs(self, since, limit):
        # Completed PRs closed since `since` (`queryTimeRangeType=closed`: the completion is the merge, and its
        # `closedDate` the merge time); a completion whose merge did not succeed merged nothing.
        criteria = {"searchCriteria.status": "completed", "searchCriteria.minTime": iso(since),
                    "searchCriteria.queryTimeRangeType": "closed", **_V}
        rows = take(self._pages(self._pr_path(), criteria, PAGE_MAX, "value", skip=True),
                    MAX_PAGES * PAGE_MAX, lambda pr: pr.get("mergeStatus") in ("succeeded", "completed"))
        return [merged_item(number=int(pr["pullRequestId"]), url=self._web(int(pr["pullRequestId"])),
                            title=pr.get("title") or "", body=pr.get("description"), merged_at=when,
                            files=[_path(c) for c in capped(self._changes(int(pr["pullRequestId"])),
                                                            MAX_MERGED_FILES)[0]])
                for when, pr in newest_merged(rows, lambda pr: parse_time(pr.get("closedDate")), since, limit)]

    def ci_log(self, check_id):
        self._id(check_id, "an Azure Pipelines build: it has no log here")
        logs = self.client.get(f"build/builds/{check_id}/logs", params=_V)
        parts = []
        for log in logs.get("value") or []:
            text = self.client.text(f"build/builds/{check_id}/logs/{log['id']}", params=_V)
            parts.append(f"=== log {log['id']} ===\n{text}")
        return "\n".join(parts)

    VARIABLE_RIGHTS = ("a token with Variable Groups (Read, create & manage) and Project and Team (Read), and the "
                       "Administrator or Creator role on the project's Library")

    # No repository-level variables exist: each level is a Library variable group of the project, `jaah-<repo>`
    # (`jaah-<repo>-<E>` with --env), created on first `set`. A group-wide write is org-scoped: the org's own URL.
    def _group_name(self, env: "str | None") -> str:
        repo = self.login.repo_path.rsplit("/", 1)[-1]
        return f"jaah-{repo}-{env}" if env else f"jaah-{repo}"

    def _description(self, env: "str | None") -> str:
        # The group's owner mark: two levels can share a name (repo `api` --env `prod`, repo `api-prod`), and a
        # group someone made by hand may carry ours; only a group whose description is this one is written.
        repo = self.login.repo_path.rsplit("/", 1)[-1]
        return f"CI variables jaah-forge sets for {repo}" + (f", environment {env}" if env else "")

    def _own_group(self, env: "str | None") -> "dict | None":
        name = self._group_name(env)
        group = self._group(name)
        if group is not None and (group.get("description") != self._description(env)
                                  or (group.get("type") or "Vsts") != "Vsts"):
            raise ForgeError(f"Azure DevOps variable group {group.get('name')} is not the one jaah-forge keeps for "
                             f"{self.login.repo_path}" + (f" environment {env}" if env else "")
                             + ": rename or remove it, or set its variables by hand")
        return group

    def _org(self) -> str:
        return self.client.api_base.rsplit("/", 2)[0]  # drop `/{project}/_apis`

    def _group(self, name: str) -> "dict | None":
        # `groupName` takes wildcards: the exact name is matched here.
        rows = self.client.get("distributedtask/variablegroups", params={"groupName": name, **_V}).get("value") or []
        return next((g for g in rows if (g.get("name") or "").casefold() == name.casefold()), None)

    @staticmethod
    def _extras(group: str) -> dict:
        return {"group": group, "pipeline_yaml": f"variables:\n- group: {group}"}

    def _put_group(self, group: dict, variables: dict) -> None:
        # The WHOLE group: a secret read back as `{value: null, isSecret: true}` and sent back so keeps its value.
        self.client.json("PUT", f"{self._org()}/_apis/distributedtask/variablegroups/{group['id']}", params=_V,
                         json_body={"name": group.get("name"), "type": group.get("type") or "Vsts",
                                    "description": group.get("description") or "", "variables": variables,
                                    "variableGroupProjectReferences": group.get("variableGroupProjectReferences")
                                    or []})

    def variables(self, kind, env):
        group = self._own_group(env)
        secret = kind == "secret"
        return [{"name": name, **({} if secret else {"value": (v or {}).get("value") or ""})}
                for name, v in ((group or {}).get("variables") or {}).items()
                if bool((v or {}).get("isSecret")) == secret]

    def set_variable(self, kind, name, value, env):
        group_name = self._group_name(env)
        entry = {"value": value, "isSecret": kind == "secret"}
        group = self._own_group(env)
        if group is None:
            project = urllib.parse.unquote(self.login.repo_path.split("/")[1])
            project_id = self.client.get(
                f"{self._org()}/_apis/projects/{urllib.parse.quote(project, safe='')}", params=_V).get("id")
            if not project_id:
                raise ForgeError(f"Azure DevOps names no id for project {project}")
            description = self._description(env)
            self.client.json("POST", f"{self._org()}/_apis/distributedtask/variablegroups", params=_V, json_body={
                "name": group_name, "type": "Vsts", "description": description, "variables": {name: entry},
                "variableGroupProjectReferences": [{"name": group_name, "description": description,
                                                    "projectReference": {"id": project_id, "name": project}}]})
            return {"action": "created", **self._extras(group_name)}
        variables = dict(group.get("variables") or {})
        action = "updated" if name in variables else "created"
        variables[name] = entry
        self._put_group(group, variables)
        return {"action": action, **self._extras(group_name)}

    def delete_variable(self, kind, name, env):
        group_name = self._group_name(env)
        group = self._own_group(env)
        variables = dict((group or {}).get("variables") or {})
        if name not in variables or bool((variables[name] or {}).get("isSecret")) != (kind == "secret"):
            raise ForgeError(f"no {kind} {name} in Azure DevOps variable group {group_name}")
        del variables[name]
        if variables:
            self._put_group(group, variables)
        else:  # its last variable: the group goes, rather than standing empty
            ids = ",".join(str((r.get("projectReference") or {}).get("id"))
                           for r in group.get("variableGroupProjectReferences") or []
                           if (r.get("projectReference") or {}).get("id"))
            self.client.json("DELETE", f"{self._org()}/_apis/distributedtask/variablegroups/{group['id']}",
                             params={"projectIds": ids, **_V})
        return self._extras(group_name)
