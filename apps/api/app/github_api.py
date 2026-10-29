"""GitHub HTTP API reader for the project docs browser and self-hosted runner credentials.

apps/api runs containerized with no `gh` CLI and no working tree, so this uses `httpx`; never
`subprocess`, `gh`/`git` or a working tree.

No control-plane decision may rest on Git content: task settlement comes from the runner's own report
and dispatch decides on the DB. Do not add merge-watch, merged-branch, CI-gate or merge/auto-merge
calls here; each made a GitHub outage read as a verdict.

Every function takes the caller's `token` (resolved from the operator's own Git Access credential via
`git_access.resolve_for_user`); there is no module credential or fallback. The token is never logged.
"""
from __future__ import annotations

import logging
from typing import Any, Optional

import httpx


log = logging.getLogger("github-api")

GITHUB_API_BASE = "https://api.github.com"
GITHUB_API_VERSION = "2022-11-28"
GITHUB_ACCEPT = "application/vnd.github+json"

# Page-load budget; a hung read fails fast rather than holding a worker.
_TIMEOUT_SECONDS = 15.0

# The save-time probe's own budget: a bare float sets connect/read/write/pool each, which could hold the
# single uvicorn worker's thread and DB connection well past 15s while an operator waits.
_PROBE_TIMEOUT = httpx.Timeout(connect=3.0, read=5.0, write=5.0, pool=1.0)


def headers(token: Optional[str], *, scheme: str = "Bearer") -> dict[str, str]:
    """GitHub's REST headers: `Authorization: {scheme} {token}` (omitted when `token` is None), the
    vendor `Accept`, and the pinned API version, so a default bump cannot change a field we read."""
    auth = {} if token is None else {"Authorization": f"{scheme} {token}"}
    return {**auth, "Accept": GITHUB_ACCEPT, "X-GitHub-Api-Version": GITHUB_API_VERSION}


def _client(token: str, *, timeout: Any = _TIMEOUT_SECONDS) -> httpx.Client:
    """An authenticated httpx client for api.github.com, sending `token` and nothing else.

    The token is required with no default, so every caller must answer "on whose behalf?".
    """
    return httpx.Client(base_url=GITHUB_API_BASE, headers=headers(token), timeout=timeout)


def _split(owner_repo: str) -> tuple[str, str]:
    owner, _, repo = owner_repo.partition("/")
    return owner, repo


# ── Self-hosted runner credentials ──────────────────────────────────────────────────────────────────
# Credential operations and GitHub's roster of our own runner hosts, not Git content. Tokens are never
# logged, stored or echoed beyond the desired-document route; failures raise `httpx` errors without a
# body. `RUNNER_ADMIN_SCOPE_HINT` is the single home of the needed permission and credential kind. A pool
# brings its own purpose-made token: a personal Connect GitHub connection carries everything its owner can
# reach and dies when they reconnect or leave (a classic `repo` token from a repository admin is accepted
# by the runner endpoints, but is still someone's whole reach).
RUNNER_ADMIN_SCOPE_HINT = (
    "Runner pools need repository Administration on the credential: 'Administration: write' mints "
    "the registration and remove tokens, 'Administration: read' shows which runners are busy (a "
    "classic PAT needs 'repo' and a repository admin behind it). Give a pool its own fine-grained PAT, "
    "scoped to this repository and stored as its own credential, rather than a person's Connect GitHub "
    "connection, which carries everything that person can reach and stops working when they reconnect "
    "or leave. Without Administration GitHub answers 403 and no runner can be enrolled."
)

# GitHub's maximum page size for the runners list.
_RUNNER_PAGE_SIZE = 100
# A loop ceiling (1000 runners on one repo), not a fleet estimate: past it the roster is knowingly
# partial and one warning is logged, rather than an unbounded walk while a page load waits.
_RUNNER_MAX_PAGES = 10


def registration_token(owner_repo: str, *, token: str, timeout: Any = _TIMEOUT_SECONDS) -> str:
    """A short-lived token a host uses to REGISTER a self-hosted runner with `owner_repo`.

    Minted per call, never stored: it expires in about an hour."""
    owner, repo = _split(owner_repo)
    with _client(token, timeout=timeout) as client:
        resp = client.post(f"/repos/{owner}/{repo}/actions/runners/registration-token")
    resp.raise_for_status()
    return str(resp.json()["token"])


def remove_token(owner_repo: str, *, token: str) -> str:
    """The counterpart: a short-lived token a host uses to DEREGISTER its runners from `owner_repo`.

    A registration token cannot remove and vice versa, so the desired-document serves exactly one."""
    owner, repo = _split(owner_repo)
    with _client(token) as client:
        resp = client.post(f"/repos/{owner}/{repo}/actions/runners/remove-token")
    resp.raise_for_status()
    return str(resp.json()["token"])


def list_runners(owner_repo: str, *, token: str) -> list[dict]:
    """Every self-hosted runner registered on `owner_repo`, as GitHub reports it — `id`, `name`,
    `status`, `busy`, and whatever else the API sends.

    `busy` is the only way to drain a runner: on SIGTERM a runner cancels its in-flight job within a
    hard 30s timeout, so "stop gracefully" means asking GitHub whether it is executing a job.

    Paginated to `_RUNNER_MAX_PAGES`: a slot past a single page's boundary would be in neither
    `busy_slots` nor `actionable_slots` on every pass, making the pool unremovable.

    Raises on any failure; the desired-document route then acts on nothing this pass, since acting on a
    stale `busy` flag kills somebody's build.
    """
    owner, repo = _split(owner_repo)
    runners: list[dict] = []
    with _client(token) as client:
        for page in range(1, _RUNNER_MAX_PAGES + 1):
            resp = client.get(
                f"/repos/{owner}/{repo}/actions/runners",
                params={"per_page": _RUNNER_PAGE_SIZE, "page": page},
            )
            resp.raise_for_status()
            batch = resp.json().get("runners")
            if not isinstance(batch, list) or not batch:
                return runners
            runners.extend(batch)
            # A short page is the last page, so a one-page repo costs one request.
            if len(batch) < _RUNNER_PAGE_SIZE:
                return runners
    log.warning(
        "github_api.list_runners: %s has more than %d self-hosted runners; the roster read here is "
        "truncated, so a runner past that point is in neither the busy set nor the actionable set",
        owner_repo, _RUNNER_MAX_PAGES * _RUNNER_PAGE_SIZE,
    )
    return runners


# How many in-progress workflow runs `running_jobs` walks for their jobs: one request per run, on a page
# load, so a busy repo is read knowingly partially rather than walked in full.
_JOB_RUNS_MAX = 10


def running_jobs(owner_repo: str, *, token: str, runners: frozenset[str]) -> list[dict]:
    """The jobs currently executing on `owner_repo` on any of `runners` (runner names), each as GitHub
    reports it — `runner_name`, `name`, `workflow_name`, `html_url` and the rest. The walk stops once every
    named runner has its job, so one busy runner usually costs two requests.

    A DISPLAY read for the runners tab (which job a busy runner is on), never an input to a decision.
    Needs 'Actions: read' on the credential; raises on any failure, and the caller shows no job.
    """
    owner, repo = _split(owner_repo)
    jobs: list[dict] = []
    # The probe budget, not the page-load one: up to `_JOB_RUNS_MAX + 1` sequential reads on a GET.
    with _client(token, timeout=_PROBE_TIMEOUT) as client:
        resp = client.get(
            f"/repos/{owner}/{repo}/actions/runs",
            params={"status": "in_progress", "per_page": _JOB_RUNS_MAX},
        )
        resp.raise_for_status()
        runs = resp.json().get("workflow_runs")
        for run in runs if isinstance(runs, list) else []:
            resp = client.get(
                f"/repos/{owner}/{repo}/actions/runs/{int(run['id'])}/jobs",
                params={"filter": "latest", "per_page": 100},
            )
            resp.raise_for_status()
            batch = resp.json().get("jobs")
            jobs.extend(
                job for job in (batch if isinstance(batch, list) else [])
                if job.get("status") == "in_progress" and job.get("runner_name") in runners
            )
            if {job["runner_name"] for job in jobs} >= runners:
                break
    return jobs


def jit_config(owner_repo: str, *, name: str, labels: list[str], token: str) -> str:
    """A just-in-time runner configuration registering ONE runner named `name` on `owner_repo`.

    Minted per call, a single-use secret spent by exactly one job; never stored or logged. GitHub answers
    409 when a runner with that name is already registered."""
    owner, repo = _split(owner_repo)
    with _client(token) as client:
        resp = client.post(
            f"/repos/{owner}/{repo}/actions/runners/generate-jitconfig",
            json={"name": name, "runner_group_id": 1, "labels": labels, "work_folder": "_work"},
        )
    resp.raise_for_status()
    return str(resp.json()["encoded_jit_config"])


def delete_runner(owner_repo: str, runner_id: int, *, token: str) -> None:
    """Deregister runner `runner_id` from `owner_repo` (GitHub's own id, from `list_runners`)."""
    owner, repo = _split(owner_repo)
    with _client(token) as client:
        resp = client.delete(f"/repos/{owner}/{repo}/actions/runners/{runner_id}")
    resp.raise_for_status()


# ── Docs browsing ───────────────────────────────────────────────────────────────────────────────────
# A repo opts in by committing a root `mkdocs.yml`. MkDocs is never run; the file is a marker plus
# `site_name` (heading) and `docs_dir` (which subtree to list).


class DocsNotEnabled(RuntimeError):
    """The repo has no root `mkdocs.yml`, so it has not opted into docs browsing. Distinct from an API
    failure: the caller renders "not enabled" rather than "GitHub is down"."""


class RepoNotVisible(RuntimeError):
    """The repo did not answer to THIS caller's token — it is private to them, or it does not exist.

    GitHub answers 404 for a private repo a token cannot see, so without this an operator lacking
    access would be told to add an `mkdocs.yml` to a repo they cannot read. Told apart by one extra
    request made only on the 404 path.
    """


def _get_text(owner_repo: str, path: str, ref: str, *, token: str) -> Optional[str]:
    """A file's decoded text, or None when it does not exist on `ref`. Uses the contents API's raw
    media type so there is no base64 hop."""
    owner, repo = _split(owner_repo)
    with _client(token) as client:
        resp = client.get(
            f"/repos/{owner}/{repo}/contents/{path}",
            params={"ref": ref},
            headers={"Accept": "application/vnd.github.raw"},
        )
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    return resp.text


def _repo_visible(owner_repo: str, *, token: str) -> bool:
    """True when `GET /repos/{owner}/{repo}` answers 200 to this token. False ONLY on a definite 404.

    Fails toward "visible" on anything else (rate limit, 5xx, transport error): it only picks which
    refusal to render, and a busy GitHub must not read as broken access.
    """
    owner, repo = _split(owner_repo)
    try:
        with _client(token) as client:
            resp = client.get(f"/repos/{owner}/{repo}")
    except httpx.HTTPError:
        return True
    return resp.status_code != 404


def docs_config(owner_repo: str, ref: str, *, token: str) -> tuple[str, str]:
    """`(site_name, docs_dir)` from the repo's root `mkdocs.yml`. Raises DocsNotEnabled when absent.

    Parsed with a tiny top-level-scalar reader, not PyYAML: real mkdocs.yml files carry
    `!!python/name:` tags that `yaml.safe_load` refuses, and the unsafe loader is not an option on
    repo-supplied input.
    """
    text = _get_text(owner_repo, "mkdocs.yml", ref, token=token)
    if text is None:
        # A 404 is two answers; probe the repo (`RepoNotVisible`). A probe error falls through as
        # "not enabled"; the caller's `httpx.HTTPError` arm covers GitHub being down.
        if not _repo_visible(owner_repo, token=token):
            raise RepoNotVisible(
                f"{owner_repo} did not answer — it is private to the GitHub account you connected, or "
                f"it does not exist"
            )
        raise DocsNotEnabled(f"{owner_repo} has no mkdocs.yml on {ref}")
    site_name, docs_dir = owner_repo.split("/")[-1], "docs"
    for line in text.splitlines():
        if line[:1].isspace() or line.lstrip().startswith("#") or ":" not in line:
            continue
        key, _, value = line.partition(":")
        value = value.split(" #")[0].strip().strip("'\"")
        if key == "site_name" and value:
            site_name = value
        elif key == "docs_dir" and value:
            docs_dir = value.strip("/")
    return site_name, docs_dir


def docs_tree(owner_repo: str, ref: str, docs_dir: str, *, token: str) -> list[str]:
    """Every `*.md` path under `docs_dir`, repo-relative and sorted, from one recursive git-trees call."""
    owner, repo = _split(owner_repo)
    with _client(token) as client:
        resp = client.get(f"/repos/{owner}/{repo}/git/trees/{ref}", params={"recursive": "1"})
    resp.raise_for_status()
    prefix = f"{docs_dir}/" if docs_dir and docs_dir != "." else ""
    return sorted(
        item["path"]
        for item in resp.json().get("tree", [])
        if item.get("type") == "blob"
        and item.get("path", "").endswith(".md")
        and item["path"].startswith(prefix)
    )


def docs_file(owner_repo: str, ref: str, path: str, *, token: str) -> Optional[str]:
    """One document's markdown, or None when it is not there. `path` is repo-relative and checked by the
    caller; never joined against anything here."""
    return _get_text(owner_repo, path, ref, token=token)
