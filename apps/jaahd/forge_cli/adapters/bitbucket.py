"""Bitbucket Cloud: REST 2.0. No native auto-merge (O4: watch-then-merge); CI is Bitbucket Pipelines.
`close` declines the PR; `rerun` queues a copy of the whole pipeline (the API cannot re-run failed steps)."""
from __future__ import annotations

import re
import urllib.parse
from datetime import timezone

from ..model import Check, ForgeError, Job, PullRequest, Run, rollup
from .base import (MAX_MERGED_FILES, MAX_PAGES, MAX_PR_COMMITS, PAGE_MAX, TRUNCATED, Adapter, Started, capped, commit,
                   diff_stats, every, is_ci_file, merged_item, newest_merged, parse_time, pr_item, provenance, take)

_STATES = {"OPEN": "open", "MERGED": "merged", "DECLINED": "closed", "SUPERSEDED": "closed"}
_CHECK = {"SUCCESSFUL": "success", "FAILED": "failure", "STOPPED": "failure", "INPROGRESS": "pending"}
# A Pipelines status links its build: `.../pipelines/results/N`, or the addon form `...#!/results/N`.
_BUILD = re.compile(r"(?:/pipelines/results/|#!/results/)(\d+)")

_RESULT = {"SUCCESSFUL": "success", "STOPPED": "cancelled", "SKIPPED": "skipped", "NOT_RUN": "skipped"}
_PR_PAGE_MAX = 50  # the pull-request listing refuses a larger pagelen
# A re-run copies only the keys that pick what to build; the rest of the target is the forge's own report.
_TARGET_KEYS = ("type", "ref_type", "ref_name", "selector", "source", "destination", "destination_commit",
                "pullrequest")
_PR_STATES = {"open": ["OPEN"], "closed": ["MERGED", "DECLINED", "SUPERSEDED"],
              "all": ["OPEN", "MERGED", "DECLINED", "SUPERSEDED"]}


def outcome(state: dict) -> "tuple[str, str | None]":
    """A pipeline's or step's `state` object; FAILED, ERROR and EXPIRED results are all `failure`."""
    name = state.get("name") or ""
    if name == "COMPLETED":
        return "completed", _RESULT.get((state.get("result") or {}).get("name") or "", "failure")
    return ("in_progress" if name in ("IN_PROGRESS", "RUNNING") else "queued"), None


def _bbql(value: str) -> str:
    """A BBQL string literal."""
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _path(row: dict) -> str:
    """The path one diffstat entry changed: the old one for a removed file."""
    return (row.get("new") or row.get("old") or {}).get("path") or ""


def _run(row: dict) -> Run:
    # The build number is the id: the one Bitbucket's UI shows, and the one `ci log` takes.
    target = row.get("target") or {}
    return Run(str(row["build_number"]), (target.get("commit") or {}).get("hash") or "",
               ((row.get("trigger") or {}).get("name") or "").lower(), *outcome(row.get("state") or {}),
               row.get("created_on") or "",
               # A pull-request target names no ref: the branch it built is the PR's source.
               branch=(target.get("ref_name") if target.get("ref_type", "branch") == "branch" else "")
               or target.get("source") or "")


def merge_state(state: str, draft: bool, conflict: bool, checks: str) -> str:
    """Bitbucket's REST API reports no mergeability: derived from the diffstat's conflicts, the checks and
    draft. Required approvals and merge checks are not readable here, so `clean` never means reviewed; the
    merge endpoint itself still enforces whatever merge checks the workspace's plan applies."""
    if state != "open":
        return "unknown"
    if conflict:
        return "dirty"
    if checks == "failure":
        return "checks_failed"
    if checks == "pending":
        return "pending"
    if draft:
        return "blocked"
    return "clean"


class Bitbucket(Adapter):
    REPO = "repositories/{}"
    DESCRIPTION = "description"
    PR_NAME = "Bitbucket PR"
    NUMBER = "id"
    PRS = "pullrequests"
    UPDATE = "PUT"

    def _create_payload(self, title, body, head, base, draft):
        return {"title": title, "description": body, "source": {"branch": {"name": head}},
                "destination": {"branch": {"name": base}}, "draft": draft, "close_source_branch": False}

    def _find_rows(self, head):
        return self.client.get(self._pr_path(), params={
            "q": f"source.branch.name={_bbql(head)}", "state": ["OPEN", "MERGED", "DECLINED", "SUPERSEDED"],
            "sort": "-created_on", "pagelen": 1}).get("values") or []

    def view(self, number):
        pr = self.client.get(self._pr_path(number))
        state = _STATES.get(pr.get("state") or "", "closed")
        sha = ((pr.get("source") or {}).get("commit") or {}).get("hash") or ""
        checks = rollup(self._statuses(sha)) if sha else "none"
        draft = bool(pr.get("draft"))
        conflict = state == "open" and self._conflicted(number)
        return PullRequest(
            number=int(pr["id"]), url=((pr.get("links") or {}).get("html") or {}).get("href") or "",
            state=state, merge_state=merge_state(state, draft, conflict, checks), checks=checks, draft=draft,
            auto_merge=False, head=pr["source"]["branch"]["name"], base=pr["destination"]["branch"]["name"],
            extra={"head_sha": sha, "body": pr.get("description") or ""},
        )

    def _conflicted(self, number) -> bool:
        data = self.client.get(f"{self._pr_path(number)}/diffstat", params={"pagelen": 100})
        return any("conflict" in (v.get("status") or "") for v in data.get("values") or [])

    def _statuses(self, sha) -> "list[Check]":
        data = self.client.get(f"{self._repo}/commit/{sha}/statuses", params={"pagelen": 100})
        out: list[Check] = []
        for status in data.get("values") or []:
            url = status.get("url") or ""
            build = _BUILD.search(url)
            key = status.get("key") or ""
            out.append(Check(build.group(1) if build else f"status-{key}", status.get("name") or key,
                             _CHECK.get(status.get("state") or "", "pending"), url))
        return out

    def checks(self, number):
        return self._statuses(self.view(number).extra["head_sha"])

    def checks_for_sha(self, sha):
        return self._statuses(sha)

    def head_commit(self, pr):
        # A PR's `source.commit.hash` is abbreviated (12 hex digits): the commit endpoint names it in full.
        sha = pr.extra.get("head_sha") or ""
        if not sha:
            return ""
        return self.client.get(f"{self._repo}/commit/{sha}").get("hash") or ""

    def merge(self, pr):
        # The merge endpoint takes no head commit: a pinned merge is `pr merge --sha`'s head check just before.
        self.client.json("POST", f"{self._pr_path(pr.number)}/merge",
                         json_body={"merge_strategy": "merge_commit", "close_source_branch": False})
        return self.view(pr.number)

    def ready(self, number):
        # The current title rides along: an update without one is not guaranteed to be accepted.
        title = self.client.get(self._pr_path(number)).get("title") or ""
        return self._update(number, {"title": title, "draft": False})

    def close(self, number):
        self.client.json("POST", f"{self._pr_path(number)}/decline")
        return self.view(number)

    def comment(self, number, body):
        data = self.client.json("POST", f"{self._pr_path(number)}/comments",
                                json_body={"content": {"raw": body}})
        return {"number": number, "url": ((data.get("links") or {}).get("html") or {}).get("href") or ""}

    def _pipeline(self, build_number: str) -> "tuple[dict, str]":
        """The pipeline with this build number among the newest 100, and its API path."""
        pipelines = self.client.get(f"{self._repo}/pipelines", params={"sort": "-created_on", "pagelen": 100})
        match = [p for p in pipelines.get("values") or [] if p.get("build_number") == int(build_number)]
        if not match:
            raise ForgeError(f"no Bitbucket pipeline #{build_number} in {self.login.repo_path}")
        return match[0], f"{self._repo}/pipelines/{urllib.parse.quote(match[0]['uuid'])}"

    def runs(self, workflow, branch, limit):
        # A repo has ONE `bitbucket-pipelines.yml`, so its file (or no workflow) names every pipeline; anything
        # else is the selector pattern of a custom/branch pipeline in it.
        params = {"sort": "-created_on", "pagelen": min(limit, PAGE_MAX)}
        if workflow is not None and not is_ci_file(workflow):
            params["target.selector.pattern"] = workflow
        if branch:
            params["target.branch"] = branch
        return [_run(r) for r in take(self._next_pages(f"{self._repo}/pipelines", params), limit)]

    def run(self, run_id):
        self._id(run_id, "a Bitbucket pipeline build number")
        row, pipe = self._pipeline(run_id)
        jobs = [Job(step.get("uuid") or "", step.get("name") or "", *outcome(step.get("state") or {}))
                for step in every(self._next_pages(f"{pipe}/steps", {"pagelen": PAGE_MAX}))]
        # Bitbucket Pipelines has no channel for a step's warning or notice lines: there are none to read.
        return _run(row), jobs, []

    def rerun(self, run, failed_only):
        if failed_only:
            raise ForgeError("Bitbucket Pipelines cannot re-run only the failed steps through its API; drop "
                             "--failed to re-run the whole pipeline")
        self._id(run.id, "a Bitbucket pipeline build number")
        row, _pipe = self._pipeline(run.id)
        original = row.get("target") or {}
        target = {k: original[k] for k in _TARGET_KEYS if original.get(k) is not None}
        sha = (original.get("commit") or {}).get("hash")
        if sha:
            target["commit"] = {"type": "commit", "hash": sha}
        data = self.client.json("POST", f"{self._repo}/pipelines/", json_body={"target": target})
        return str(data["build_number"])  # a new pipeline: Bitbucket queues a copy, never re-runs in place

    def dispatch(self, workflow, ref, inputs):
        # `workflow` names a `custom:` pipeline of `bitbucket-pipelines.yml`, run on the branch's head.
        target = {"type": "pipeline_ref_target", "ref_type": "branch", "ref_name": ref,
                  "selector": {"type": "custom", "pattern": workflow}}
        body = {"target": target, "variables": [{"key": k, "value": v} for k, v in inputs.items()]}
        data = self.client.json("POST", f"{self._repo}/pipelines/", json_body=body)
        number = str(data["build_number"])
        sha = ((data.get("target") or {}).get("commit") or {}).get("hash") or ""
        if not sha and data.get("uuid"):
            # A just-created pipeline may not name its commit yet: its own row does once resolved.
            row = self.client.get(f"{self._repo}/pipelines/{urllib.parse.quote(data['uuid'])}")
            sha = ((row.get("target") or {}).get("commit") or {}).get("hash") or ""
        return Started(number, f"{self.login.web_url}/pipelines/results/{number}", sha)

    def prs(self, state, author, limit, head=None, base=None):
        params = {"state": _PR_STATES[state], "sort": "-created_on", "pagelen": min(limit, _PR_PAGE_MAX)}
        clauses = [f"{field}={_bbql(value)}" for field, value in (
            ("author.nickname", author), ("source.branch.name", head), ("destination.branch.name", base)) if value]
        if clauses:
            params["q"] = " AND ".join(clauses)
        rows = take(self._next_pages(self._pr_path(), params), limit)
        # A listed PR's mergeability and statuses are not in the listing: the view reads them.
        return [pr_item(self.view(int(pr["id"])), pr.get("title") or "",
                        (pr.get("author") or {}).get("nickname") or "") for pr in rows]

    def diff_stats(self, number):
        # The PR diffstat redirects (same host) to the repository diffstat of the PR's revspec.
        rows, more = capped(r for page in self._next_pages(f"{self._pr_path(number)}/diffstat",
                                                      {"pagelen": PAGE_MAX}) for r in page)
        return diff_stats("bitbucket", ((_path(r), int(r.get("lines_added") or 0), int(r.get("lines_removed") or 0))
                                        for r in rows), TRUNCATED if more else None)

    def provenance(self, number):
        pr = self.client.get(self._pr_path(number))
        rows, more_commits = capped((r for page in self._next_pages(f"{self._pr_path(number)}/commits",
                                                                    {"pagelen": _PR_PAGE_MAX}) for r in page),
                                    MAX_PR_COMMITS)
        paths, more_files = capped(_path(r) for page in self._next_pages(
            f"{self._pr_path(number)}/diffstat", {"pagelen": PAGE_MAX}) for r in page)
        # Bitbucket lists newest first; oldest first, as GitHub does. `author.raw` is `Name <email>`; only a commit
        # Bitbucket matched to an account names its `user`.
        commits = [commit(r["hash"], (((r.get("author") or {}).get("raw") or "").split("<", 1)[0].strip()
                                      or ((r.get("author") or {}).get("user") or {}).get("display_name") or ""),
                          ((r.get("author") or {}).get("user") or {}).get("nickname"), r.get("message") or "")
                   for r in reversed(rows)]
        # `merge_commit.hash` is the merge commit Bitbucket made — an abbreviated id, passed on as the forge names it.
        return provenance("bitbucket", number, merged=pr.get("state") == "MERGED",
                          merge_sha=(pr.get("merge_commit") or {}).get("hash"), commits=commits,
                          more_commits=more_commits, files=paths, more_files=more_files)

    def merged_prs(self, since, limit):
        # Bitbucket names no merge time: a merged PR's `updated_on` stands in for it — the merge is its last
        # update unless someone commented on it after, which makes it read as merged later than it was.
        stamp = since.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00")
        params = {"state": "MERGED", "q": f"updated_on>={stamp}", "sort": "-updated_on", "pagelen": _PR_PAGE_MAX}
        rows = take(self._next_pages(self._pr_path(), params), MAX_PAGES * _PR_PAGE_MAX)
        return [merged_item(number=int(pr["id"]), url=((pr.get("links") or {}).get("html") or {}).get("href") or "",
                            title=pr.get("title") or "", body=pr.get("description"), merged_at=when,
                            files=[_path(r) for r in next(self._next_pages(
                                f"{self._pr_path(pr['id'])}/diffstat", {"pagelen": MAX_MERGED_FILES}))])
                for when, pr in newest_merged(rows, lambda pr: parse_time(pr.get("updated_on")), since, limit)]

    def ci_log(self, check_id):
        self._id(check_id, "a Bitbucket Pipelines check: it has no log here")
        _row, pipe = self._pipeline(check_id)
        steps = self.client.get(f"{pipe}/steps", params={"pagelen": 100})
        parts = []
        for step in steps.get("values") or []:
            # Bitbucket may serve the log from a presigned storage URL: the one credential-free hop.
            log = self.client.text(f"{pipe}/steps/{urllib.parse.quote(step['uuid'])}/log", anonymous_hop=True)
            parts.append(f"=== {step.get('name') or ''} ===\n{log}")
        return "\n".join(parts)

    VARIABLE_RIGHTS = ("admin on the repository, through a token with `read:pipeline:bitbucket` and "
                       "`admin:pipeline:bitbucket` (OAuth: `pipeline` and `pipeline:variable`)")

    def _variables_path(self, env):
        """Repository variables, or one deployment environment's — `env` matched by its exact name."""
        if env is None:
            return f"{self._repo}/pipelines_config/variables"
        match = [e for e in every(self._next_pages(f"{self._repo}/environments", {"pagelen": PAGE_MAX}))
                 if e.get("name") == env]
        if not match:
            raise ForgeError(f"no deployment environment named '{env}' in {self.login.repo_path}")
        uuid = urllib.parse.quote(match[0]["uuid"], safe="")
        return f"{self._repo}/deployments_config/environments/{uuid}/variables"

    def _variable_rows(self, path):
        return every(self._next_pages(path, {"pagelen": PAGE_MAX}))

    def variables(self, kind, env):
        # One list holds both: `secured` splits a secret (its value reads back empty) from a variable.
        secret = kind == "secret"
        return [{"name": r["key"], **({} if secret else {"value": r.get("value") or ""})}
                for r in self._variable_rows(self._variables_path(env)) if bool(r.get("secured")) == secret]

    def set_variable(self, kind, name, value, env):
        path = self._variables_path(env)
        body = {"key": name, "value": value, "secured": kind == "secret"}
        # A key is unique across both kinds: an existing row of either is overwritten, never duplicated.
        row = next((r for r in self._variable_rows(path) if r.get("key") == name), None)
        if row is not None:
            self.client.json("PUT", f"{path}/{urllib.parse.quote(row['uuid'], safe='')}", json_body=body)
            return {"action": "updated"}
        self.client.json("POST", path, json_body=body)
        return {"action": "created"}

    def delete_variable(self, kind, name, env):
        path = self._variables_path(env)
        # Only a row of this kind: `secret delete` never removes a plain variable of that name, nor the reverse.
        row = next((r for r in self._variable_rows(path)
                    if r.get("key") == name and bool(r.get("secured")) == (kind == "secret")), None)
        if row is None:
            raise ForgeError(f"no {kind} named '{name}' in {self.login.repo_path}"
                             + (f" environment '{env}'" if env is not None else ""))
        self.client.json("DELETE", f"{path}/{urllib.parse.quote(row['uuid'], safe='')}")
        return {}
