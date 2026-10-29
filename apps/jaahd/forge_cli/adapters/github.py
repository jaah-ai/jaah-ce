"""GitHub: REST v3, with the PR view and auto-merge over GraphQL (`api_base` + `/graphql`)."""
from __future__ import annotations

import urllib.parse

import dataclasses
import re

from ..model import (AlreadyMergeable, Annotation, Check, ForgeError, Job, PullRequest, Run, is_advisory, is_id,
                     rollup)
from .base import (MAX_DIFF_FILES, MAX_PR_COMMITS, PAGE_MAX, TRUNCATED, Adapter, capped, commit, diff_stats, every,
                   pr_item, provenance, take)

_PR_FIELDS = """id number url state isDraft mergeStateStatus reviewDecision headRefName baseRefName headRefOid
autoMergeRequest{enabledAt} commits(last:1){nodes{commit{statusCheckRollup{state%s}}}}"""
# The view alone names which checks branch protection REQUIRES (`isRequired` needs the PR number, which a
# list page cannot pass per node): a list row's rollup counts every check.
_CONTEXTS = (" contexts(first:100){nodes{__typename ...on CheckRun{name status conclusion "
             "isRequired(pullRequestNumber:$number)} ...on StatusContext{context state "
             "isRequired(pullRequestNumber:$number)}}}")
_VIEW = ("query($owner:String!,$name:String!,$number:Int!){repository(owner:$owner,name:$name){"
         "pullRequest(number:$number){body " + _PR_FIELDS % _CONTEXTS + "}}}")
# One call answers a whole `pr list` page, the view's fields included — no call per PR. Not `search`: it
# leaves `mergeStateStatus` uncomputed (UNKNOWN) where the repository's own connection computes it.
_LIST = ("query($owner:String!,$name:String!,$states:[PullRequestState!],$n:Int!,$after:String,"
         "$head:String,$base:String){repository(owner:$owner,name:$name){pullRequests(states:$states,first:$n,"
         "after:$after,headRefName:$head,baseRefName:$base,orderBy:{field:CREATED_AT,direction:DESC}){"
         "pageInfo{hasNextPage endCursor} nodes{title author{login} " + _PR_FIELDS % "" + "}}}}")
_LIST_STATES = {"open": ["OPEN"], "closed": ["CLOSED", "MERGED"], "all": None}
_AUTO_MERGE = """mutation($id:ID!){enablePullRequestAutoMerge(input:{pullRequestId:$id,mergeMethod:MERGE}){
pullRequest{number}}}"""
_DISABLE_AUTO_MERGE = "mutation($id:ID!){disablePullRequestAutoMerge(input:{pullRequestId:$id}){pullRequest{number}}}"
_READY = "mutation($id:ID!){markPullRequestReadyForReview(input:{pullRequestId:$id}){pullRequest{number}}}"
# GitHub will not ARM auto-merge on a PR that can merge now: "Pull request is in clean status".
_MERGEABLE_NOW = re.compile(r"\bis in (?:clean|unstable|has_hooks) status\b", re.IGNORECASE)
# A repository with "Allow auto-merge" off (GitHub's default), or a base with no branch protection, refuses
# to arm it at all; that is the same answer as a forge with no native auto-merge, so the caller watches and
# merges instead.
_AUTO_MERGE_OFF = re.compile(r"\bauto[- ]?merge is not allowed for this repository\b"
                             r"|\bprotected branch rules not configured for this branch\b", re.IGNORECASE)

_ROLLUP = {"SUCCESS": "success", "FAILURE": "failure", "ERROR": "failure", "PENDING": "pending",
           "EXPECTED": "pending"}
_FAILED = {"failure", "cancelled", "timed_out", "action_required", "startup_failure", "stale"}
_RUN_CONCLUSION = {"success": "success", "cancelled": "cancelled", "skipped": "skipped", "neutral": "skipped"}
_LEVEL = {"notice": "notice", "warning": "warning", "failure": "error"}


def outcome(status: "str | None", conclusion: "str | None") -> "tuple[str, str | None]":
    """A workflow run's or job's `status`/`conclusion`; every unsuccessful conclusion but a cancel is `failure`."""
    if status == "completed":
        return "completed", _RUN_CONCLUSION.get(conclusion or "", "failure")
    return ("in_progress" if status == "in_progress" else "queued"), None


def merge_state(status: str, review: "str | None", checks: str) -> str:
    """`mergeStateStatus` (+ `reviewDecision` and the check rollup, for why a PR is BLOCKED)."""
    if status in ("CLEAN", "HAS_HOOKS"):
        return "clean"
    if status == "DIRTY":
        return "dirty"
    if status == "UNSTABLE":
        # Mergeable with a non-passing check: GitHub would merge it, unless the failing check is one we count.
        return "clean" if checks in ("success", "none") else "checks_failed"
    if status in ("BLOCKED", "BEHIND", "DRAFT"):
        if checks == "failure":
            return "checks_failed"
        if checks == "pending":
            return "pending"
        if status == "BEHIND":  # strict required checks: the head must first be brought up to date with the base
            return "behind"
        if review in ("REVIEW_REQUIRED", "CHANGES_REQUESTED"):
            return "review_required"
        return "blocked"
    return "unknown"


def _context(node: dict) -> "tuple[str, str]":
    """A rollup context's (name, CHECK_STATES state)."""
    if node.get("__typename") == "StatusContext":
        return node.get("context") or "", _ROLLUP.get(node.get("state") or "", "failure")
    if node.get("status") != "COMPLETED":
        return node.get("name") or "", "pending"
    conclusion = (node.get("conclusion") or "").lower()
    state = "success" if conclusion == "success" else "skipped" if conclusion in ("neutral", "skipped") else "failure"
    return node.get("name") or "", state


def _pull_request(pr: dict, ignore: "frozenset[str]" = frozenset()) -> PullRequest:
    """`ignore`: check names left out of `checks` and `merge_state` (`pr view --ignore-check`); an advisory check
    (`is_advisory`) is always left out."""
    nodes = (pr.get("commits") or {}).get("nodes") or []
    roll = ((nodes[0].get("commit") or {}).get("statusCheckRollup") or {}) if nodes else {}
    checks = _ROLLUP.get(roll.get("state") or "", "none")
    required: "frozenset[str]" = frozenset()
    listed = [n for n in ((roll.get("contexts") or {}).get("nodes") or []) if n]
    ignored = [n for n in listed if _context(n)[0] in ignore or is_advisory(_context(n)[0])]
    contexts = [n for n in listed if n not in ignored]
    if any(n.get("isRequired") for n in listed):
        # Branch protection names its required checks: only they gate (an optional red check does not).
        named = [_context(n) for n in contexts if n.get("isRequired")]
        required = frozenset(name for name, _ in named)
        checks = rollup([Check("", name, state) for name, state in named])
    elif ignored:
        # GitHub's own rollup counts the ignored check: count the rest, every one gating as with no protection.
        checks = rollup([Check("", name, state) for name, state in map(_context, contexts)])
    status = pr.get("mergeStateStatus") or ""
    if (status == "BLOCKED" and checks in ("success", "none")
            and pr.get("reviewDecision") not in ("REVIEW_REQUIRED", "CHANGES_REQUESTED")
            and any(n.get("isRequired") and _context(n)[1] != "success" for n in ignored if _context(n)[0] in ignore)):
        status = "CLEAN"  # nothing this view can see holds it but the ignored required check
    return PullRequest(
        number=int(pr["number"]), url=pr["url"], state=pr["state"].lower(),
        merge_state=merge_state(status, pr.get("reviewDecision"), checks),
        checks=checks, draft=bool(pr.get("isDraft")), auto_merge=pr.get("autoMergeRequest") is not None,
        head=pr["headRefName"], base=pr["baseRefName"],
        extra={"node_id": pr["id"], "head_sha": pr.get("headRefOid") or "", "required": required,
               "body": pr.get("body") or ""},
    )


def _own(row: dict) -> bool:
    """A run of this repo's own branch: a fork PR's `head_branch` is a name the fork chose."""
    repo = (row.get("head_repository") or {}).get("full_name")
    base = (row.get("repository") or {}).get("full_name")
    return repo is None or base is None or repo.lower() == base.lower()


def _run(row: dict) -> Run:
    return Run(str(row["id"]), row.get("head_sha") or "", row.get("event") or "",
               *outcome(row.get("status"), row.get("conclusion")), row.get("created_at") or "",
               branch=(row.get("head_branch") or "") if _own(row) else "")


class GitHub(Adapter):
    @property
    def _repo(self) -> str:
        return f"repos/{self.login.repo_path}"

    def create(self, *, title, body, head, base, draft):
        data = self.client.json("POST", f"{self._repo}/pulls",
                                json_body={"title": title, "body": body, "head": head, "base": base, "draft": draft})
        return self.view(int(data["number"]))

    def find(self, head):
        owner = self.login.repo_path.split("/", 1)[0]
        rows = self.client.get(f"{self._repo}/pulls",
                               params={"head": f"{owner}:{head}", "state": "all", "per_page": 1})
        if not rows:
            raise ForgeError(f"no GitHub PR from branch '{head}'")
        return int(rows[0]["number"])

    def view(self, number, ignore=frozenset()):
        owner, name = self.login.repo_path.split("/", 1)
        data = self._graphql(_VIEW, {"owner": owner, "name": name, "number": number}, "GraphQL")
        pr = (data.get("repository") or {}).get("pullRequest")
        if not pr:
            raise ForgeError(f"no GitHub PR #{number} in {self.login.repo_path}")
        return _pull_request(pr, frozenset(ignore))

    def _graphql(self, query: str, variables: dict, what: str) -> dict:
        data = self.client.json("POST", "graphql", json_body={"query": query, "variables": variables})
        if data.get("errors"):
            raise ForgeError(f"GitHub {what}: {data['errors'][0].get('message', data['errors'][0])}")
        return data.get("data") or {}

    def prs(self, state, author, limit, head=None, base=None):
        owner, name = self.login.repo_path.split("/", 1)
        # The connection has no author filter: an `--author` list reads full pages and keeps that author's.
        size = PAGE_MAX if author else min(limit, PAGE_MAX)

        # GraphQL names an app by its bare login: `dependabot[bot]` is `dependabot` here.
        wanted = (author or "").lower().removesuffix("[bot]")

        def mine(node: dict) -> bool:
            return not author or ((node.get("author") or {}).get("login") or "").lower() == wanted

        def pages():
            after = None
            while True:
                found = ((self._graphql(_LIST, {"owner": owner, "name": name, "states": _LIST_STATES[state],
                                                "n": size, "after": after, "head": head, "base": base},
                                               "PR list").get("repository") or {})
                         .get("pullRequests") or {})
                yield [n for n in found.get("nodes") or [] if n]
                info = found.get("pageInfo") or {}
                if not info.get("hasNextPage") or not info.get("endCursor"):
                    return
                after = info.get("endCursor")

        return [pr_item(_pull_request(n), n.get("title") or "", (n.get("author") or {}).get("login") or "")
                for n in take(pages(), limit, mine)]

    def _pages(self, path: str, key: "str | None", params: dict, per_page: int):
        """REST pages of `path` (its `key` array, or the answer itself), until a short page ends them."""
        page = 1
        while True:
            data = self.client.get(path, params={**params, "per_page": per_page, "page": page})
            rows = (data.get(key) if key else data) or []
            yield rows
            if len(rows) < per_page:
                return
            page += 1

    def runs(self, workflow, branch, limit):
        if workflow is None:
            path = f"{self._repo}/actions/runs"  # every workflow's runs
        else:
            # The API names a workflow by its file's basename (or its numeric id), never by its path.
            path = f"{self._repo}/actions/workflows/{urllib.parse.quote(workflow.rsplit('/', 1)[-1], safe='')}/runs"
        size = min(limit, PAGE_MAX)
        rows = take(self._pages(path, "workflow_runs", {"branch": branch} if branch else {}, size), limit)
        if branch:
            # The `branch` filter is served from an index that intermittently answers stale (observed: one
            # call in eight returned an 11-day-old "newest" run). The unfiltered newest page is current, so
            # its runs on `branch` are merged in: the newest run is right whichever answer was fresh.
            fresh = next(self._pages(path, "workflow_runs", {}, size))
            seen = {r["id"] for r in rows}
            rows += [r for r in fresh if r.get("head_branch") == branch and r["id"] not in seen]
            rows = sorted(rows, key=lambda r: (r.get("created_at") or "", r["id"]), reverse=True)[:limit]
        return [_run(r) for r in rows]

    def _run_row(self, run_id):
        if not is_id(run_id):
            raise ForgeError(f"'{run_id}' is not a GitHub Actions run id")
        return _run(self.client.get(f"{self._repo}/actions/runs/{run_id}"))

    def run_head(self, run_id):
        head = self._run_row(run_id)
        if head.branch and not self._is_branch(head.branch):
            # A tag push's run names the TAG as its `head_branch`.
            head = dataclasses.replace(head, branch="")
        return head

    def _is_branch(self, name: str) -> bool:
        try:
            self.client.get(f"{self._repo}/git/ref/heads/{urllib.parse.quote(name)}")
        except ForgeError as exc:
            if "HTTP 404" in exc.message:
                return False
            raise
        return True

    def run(self, run_id):
        head = self._run_row(run_id)
        jobs, notes = [], []
        for row in every(self._pages(f"{self._repo}/actions/runs/{run_id}/jobs", "jobs", {"filter": "latest"},
                                     PAGE_MAX)):
            jobs.append(Job(str(row["id"]), row.get("name") or "", *outcome(row.get("status"), row.get("conclusion"))))
            # A job IS a check run: its `::warning::`/`::notice::` lines are that check run's annotations.
            for ann in every(self._pages(f"{self._repo}/check-runs/{row['id']}/annotations", None, {}, PAGE_MAX)):
                notes.append(Annotation(_LEVEL.get(ann.get("annotation_level") or "", "notice"),
                                        ann.get("message") or "", row.get("name") or "", ann.get("title") or ""))
        return head, jobs, notes

    def rerun(self, run, failed_only):
        if not is_id(run.id):
            raise ForgeError(f"'{run.id}' is not a GitHub Actions run id")
        self.client.json("POST", f"{self._repo}/actions/runs/{run.id}/{'rerun-failed-jobs' if failed_only else 'rerun'}")
        return run.id  # re-run in place: the same run, a new attempt

    def dispatch(self, workflow, ref, inputs):
        # `return_run_details` asks for the new run's id (200); a GitHub that predates it answers 204, no body.
        data = self.client.json("POST", f"{self._repo}/actions/workflows/{urllib.parse.quote(workflow, safe='')}"
                                "/dispatches", json_body={"ref": ref, "inputs": inputs, "return_run_details": True})
        run_id = data.get("workflow_run_id") if isinstance(data, dict) else None
        return str(run_id) if run_id is not None else None

    def checks(self, number):
        pr = self.view(number)
        sha, required = pr.extra["head_sha"], pr.extra["required"]
        out: list[Check] = []
        runs = self.client.get(f"{self._repo}/commits/{sha}/check-runs", params={"per_page": 100})
        for run in runs.get("check_runs") or []:
            if run.get("status") != "completed":
                state = "pending"
            elif run.get("conclusion") == "success":
                state = "success"
            elif run.get("conclusion") in _FAILED:
                state = "failure"
            else:
                state = "skipped"  # neutral, skipped
            name = run.get("name") or ""
            # No required set (no branch protection): every check gates, but an advisory one.
            out.append(Check(str(run["id"]), name, state, run.get("html_url") or "",
                             (not required or name in required) and not is_advisory(name)))
        combined = self.client.get(f"{self._repo}/commits/{sha}/status")
        for status in combined.get("statuses") or []:
            state = {"success": "success", "pending": "pending"}.get(status.get("state"), "failure")
            name = status.get("context") or ""
            out.append(Check(f"status-{status['id']}", name, state, status.get("target_url") or "",
                             (not required or name in required) and not is_advisory(name)))
        return out

    def auto_merge(self, pr):
        data = self.client.json("POST", "graphql",
                                json_body={"query": _AUTO_MERGE, "variables": {"id": pr.extra["node_id"]}})
        if data.get("errors"):
            message = str(data["errors"][0].get("message", data["errors"][0]))
            if _MERGEABLE_NOW.search(message):
                raise AlreadyMergeable(f"GitHub auto-merge: {message}")
            if _AUTO_MERGE_OFF.search(message):
                return self.view(pr.number)
            raise ForgeError(f"GitHub auto-merge: {message}")
        return self.view(pr.number)

    def disable_auto_merge(self, pr):
        data = self.client.json("POST", "graphql",
                                json_body={"query": _DISABLE_AUTO_MERGE, "variables": {"id": pr.extra["node_id"]}})
        if data.get("errors"):
            raise ForgeError(f"GitHub auto-merge disable: {data['errors'][0].get('message', data['errors'][0])}")
        return self.view(pr.number)

    def merge(self, pr):
        body = {"merge_method": "merge"}
        if pr.extra.get("head_sha"):
            body["sha"] = pr.extra["head_sha"]  # refuse to merge a head that moved since the checks passed
        self.client.json("PUT", f"{self._repo}/pulls/{pr.number}/merge", json_body=body)
        return self.view(pr.number)

    def status(self, sha, *, state, context, description):
        data = self.client.json("POST", f"{self._repo}/statuses/{sha}",
                                json_body={"state": state, "context": context, "description": description})
        return {"sha": sha, "context": context, "state": data.get("state") or state, "url": data.get("url") or ""}

    def edit(self, number, *, title, body):
        patch = {k: v for k, v in (("title", title), ("body", body)) if v is not None}
        self.client.json("PATCH", f"{self._repo}/pulls/{number}", json_body=patch)
        return self.view(number)

    def comment(self, number, body):
        data = self.client.json("POST", f"{self._repo}/issues/{number}/comments", json_body={"body": body})
        return {"number": number, "url": data.get("html_url") or ""}

    def ready(self, number):
        self._graphql(_READY, {"id": self.view(number).extra["node_id"]}, "ready for review")
        return self.view(number)

    def close(self, number):
        self.client.json("PATCH", f"{self._repo}/pulls/{number}", json_body={"state": "closed"})
        return self.view(number)

    def diff_stats(self, number):
        rows, more = capped(r for page in self._pages(f"{self._repo}/pulls/{number}/files", None, {}, PAGE_MAX)
                            for r in page)
        if not more and len(rows) == MAX_DIFF_FILES:
            # GitHub's file list itself stops at 3,000: the PR's own count says whether it did here.
            more = int(self.client.get(f"{self._repo}/pulls/{number}").get("changed_files") or 0) > MAX_DIFF_FILES
        return diff_stats("github", ((r["filename"], int(r["additions"]), int(r["deletions"])) for r in rows),
                          TRUNCATED if more else None)

    def provenance(self, number):
        pr = self.client.get(f"{self._repo}/pulls/{number}")
        # GitHub's own commit list stops at 250, and its file list at 3,000: the PR's counts say whether either did.
        rows, more_commits = capped((r for page in self._pages(f"{self._repo}/pulls/{number}/commits", None, {},
                                                                PAGE_MAX) for r in page), MAX_PR_COMMITS)
        paths, more_files = capped(r["filename"] for page in self._pages(f"{self._repo}/pulls/{number}/files", None,
                                                                         {}, PAGE_MAX) for r in page)
        commits = [commit(r["sha"], ((r.get("commit") or {}).get("author") or {}).get("name") or "",
                          (r.get("author") or {}).get("login"), (r.get("commit") or {}).get("message") or "")
                   for r in rows]
        return provenance(
            "github", number, merged=bool(pr.get("merged")), merge_sha=pr.get("merge_commit_sha"),
            commits=commits, more_commits=more_commits or int(pr.get("commits") or 0) > len(commits),
            files=paths, more_files=more_files or int(pr.get("changed_files") or 0) > len(paths))

    def ci_log(self, check_id):
        if not is_id(check_id):
            raise ForgeError(f"'{check_id}' is a commit status, not a GitHub Actions job: it has no log here")
        # GitHub serves the log from a presigned URL on another host: the one credential-free hop.
        return self.client.text(f"{self._repo}/actions/jobs/{urllib.parse.quote(check_id)}/logs",
                                anonymous_hop=True)
