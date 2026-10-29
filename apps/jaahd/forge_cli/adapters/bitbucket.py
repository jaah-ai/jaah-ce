"""Bitbucket Cloud: REST 2.0. No native auto-merge (O4: watch-then-merge); CI is Bitbucket Pipelines.
`close` declines the PR; `rerun` queues a copy of the whole pipeline (the API cannot re-run failed steps)."""
from __future__ import annotations

import re
import urllib.parse

from ..model import Check, ForgeError, Job, PullRequest, Run, is_id, rollup
from .base import PAGE_MAX, TRUNCATED, Adapter, capped, diff_stats, every, is_ci_file, pr_item, take

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
    @property
    def _repo(self) -> str:
        return f"repositories/{self.login.repo_path}"

    def create(self, *, title, body, head, base, draft):
        data = self.client.json("POST", f"{self._repo}/pullrequests", json_body={
            "title": title, "description": body, "source": {"branch": {"name": head}},
            "destination": {"branch": {"name": base}}, "draft": draft, "close_source_branch": False})
        return self.view(int(data["id"]))

    def find(self, head):
        data = self.client.get(f"{self._repo}/pullrequests", params={
            "q": f"source.branch.name={_bbql(head)}", "state": ["OPEN", "MERGED", "DECLINED", "SUPERSEDED"],
            "sort": "-created_on", "pagelen": 1})
        values = data.get("values") or []
        if not values:
            raise ForgeError(f"no Bitbucket PR from branch '{head}'")
        return int(values[0]["id"])

    def view(self, number):
        pr = self.client.get(f"{self._repo}/pullrequests/{number}")
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
        data = self.client.get(f"{self._repo}/pullrequests/{number}/diffstat", params={"pagelen": 100})
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

    def auto_merge(self, pr):
        return pr  # no native auto-merge: the CLI prints the watch-then-merge advice

    def disable_auto_merge(self, pr):
        return pr  # nothing to disarm

    def merge(self, pr):
        self.client.json("POST", f"{self._repo}/pullrequests/{pr.number}/merge",
                         json_body={"merge_strategy": "merge_commit", "close_source_branch": False})
        return self.view(pr.number)

    def edit(self, number, *, title, body):
        patch = {k: v for k, v in (("title", title), ("description", body)) if v is not None}
        self.client.json("PUT", f"{self._repo}/pullrequests/{number}", json_body=patch)
        return self.view(number)

    def ready(self, number):
        # The current title rides along: an update without one is not guaranteed to be accepted.
        title = self.client.get(f"{self._repo}/pullrequests/{number}").get("title") or ""
        self.client.json("PUT", f"{self._repo}/pullrequests/{number}", json_body={"title": title, "draft": False})
        return self.view(number)

    def close(self, number):
        self.client.json("POST", f"{self._repo}/pullrequests/{number}/decline")
        return self.view(number)

    def comment(self, number, body):
        data = self.client.json("POST", f"{self._repo}/pullrequests/{number}/comments",
                                json_body={"content": {"raw": body}})
        return {"number": number, "url": ((data.get("links") or {}).get("html") or {}).get("href") or ""}

    def _pages(self, path: str, params: dict):
        """Pages by the answer's own `next` link (a same-host URL that carries the query on)."""
        data = self.client.get(path, params=params)
        while True:
            yield data.get("values") or []
            if not data.get("next"):
                return
            data = self.client.get(data["next"])

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
        return [_run(r) for r in take(self._pages(f"{self._repo}/pipelines", params), limit)]

    def run(self, run_id):
        if not is_id(run_id):
            raise ForgeError(f"'{run_id}' is not a Bitbucket pipeline build number")
        row, pipe = self._pipeline(run_id)
        jobs = [Job(step.get("uuid") or "", step.get("name") or "", *outcome(step.get("state") or {}))
                for step in every(self._pages(f"{pipe}/steps", {"pagelen": PAGE_MAX}))]
        # Bitbucket Pipelines has no channel for a step's warning or notice lines: there are none to read.
        return _run(row), jobs, []

    def rerun(self, run, failed_only):
        if failed_only:
            raise ForgeError("Bitbucket Pipelines cannot re-run only the failed steps through its API; drop "
                             "--failed to re-run the whole pipeline")
        if not is_id(run.id):
            raise ForgeError(f"'{run.id}' is not a Bitbucket pipeline build number")
        row, _pipe = self._pipeline(run.id)
        original = row.get("target") or {}
        target = {k: original[k] for k in _TARGET_KEYS if original.get(k) is not None}
        sha = (original.get("commit") or {}).get("hash")
        if sha:
            target["commit"] = {"type": "commit", "hash": sha}
        data = self.client.json("POST", f"{self._repo}/pipelines/", json_body={"target": target})
        return str(data["build_number"])  # a new pipeline: Bitbucket queues a copy, never re-runs in place

    def prs(self, state, author, limit, head=None, base=None):
        params = {"state": _PR_STATES[state], "sort": "-created_on", "pagelen": min(limit, _PR_PAGE_MAX)}
        clauses = [f"{field}={_bbql(value)}" for field, value in (
            ("author.nickname", author), ("source.branch.name", head), ("destination.branch.name", base)) if value]
        if clauses:
            params["q"] = " AND ".join(clauses)
        rows = take(self._pages(f"{self._repo}/pullrequests", params), limit)
        # A listed PR's mergeability and statuses are not in the listing: the view reads them.
        return [pr_item(self.view(int(pr["id"])), pr.get("title") or "",
                        (pr.get("author") or {}).get("nickname") or "") for pr in rows]

    def diff_stats(self, number):
        # The PR diffstat redirects (same host) to the repository diffstat of the PR's revspec.
        rows, more = capped(r for page in self._pages(f"{self._repo}/pullrequests/{number}/diffstat",
                                                      {"pagelen": PAGE_MAX}) for r in page)
        return diff_stats("bitbucket", ((((r.get("new") or r.get("old") or {}).get("path") or ""),
                                         int(r.get("lines_added") or 0), int(r.get("lines_removed") or 0))
                                        for r in rows), TRUNCATED if more else None)

    def ci_log(self, check_id):
        if not is_id(check_id):
            raise ForgeError(f"'{check_id}' is not a Bitbucket Pipelines check: it has no log here")
        _row, pipe = self._pipeline(check_id)
        steps = self.client.get(f"{pipe}/steps", params={"pagelen": 100})
        parts = []
        for step in steps.get("values") or []:
            # Bitbucket may serve the log from a presigned storage URL: the one credential-free hop.
            log = self.client.text(f"{pipe}/steps/{urllib.parse.quote(step['uuid'])}/log", anonymous_hop=True)
            parts.append(f"=== {step.get('name') or ''} ===\n{log}")
        return "\n".join(parts)
