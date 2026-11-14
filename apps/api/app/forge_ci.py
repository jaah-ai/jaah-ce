"""The forge's CI as the VIEWER's own connection reads it: the latest run on a project's integration branch, and
the project's workflows for a pipeline gate's picker (`workflows`, at the end).

One read per forge, the newest run or pipeline on `project.integration_branch` of the primary repository (whose
URL, forge and branch `projects` carries as its projection, `models/projects.py::ProjectRepo`):

- GitHub: `GET /repos/{o}/{r}/actions/runs?branch=&per_page=1`
- GitLab: `GET /projects/{id}/pipelines?ref=&per_page=1`
- Bitbucket: `GET /repositories/{ws}/{slug}/pipelines/?target.branch=&sort=-created_on&pagelen=1`
- Azure DevOps: `GET {org}/{project}/_apis/build/builds?branchName=refs/heads/<b>&$top=1`

The credential is the viewer's own (`git_access.resolve_for_user`, then `rest_login_for`), never the project's or
a runner's: a person sees only the CI their own forge account can see. Nothing here raises to its caller — no
repository, no connection, a refusal, a limit or a slow forge each answer `unknown` with a sentence the viewer can
act on. Nothing is stored either (no table, no column): a process-local cache keeps a read five minutes and a
failed read one minute.

Session use stays on the caller's thread: `latest_runs` resolves every project's connection first, then only the
HTTP reads run on the pool.
"""

from __future__ import annotations

import logging
import posixpath
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, wait
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Literal, Optional
from uuid import UUID

import httpx
import yaml
from sqlalchemy.orm import Session

from app import forge_repos, forges, http_bounded
from app.clock import parse_utc
from app.models.identity import AdminUser
from app.models.projects import Project

log = logging.getLogger(__name__)

Status = Literal["success", "failure", "running", "cancelled", "unknown"]

_REQUEST_TIMEOUT_SECONDS = 5.0
_BATCH_WORKERS = 8
_BATCH_CAP_SECONDS = 8.0
_TTL_SECONDS = 300.0
_FAILURE_TTL_SECONDS = 60.0
# What a forge's answer may put on the page: a link is https and bounded, a name is cut.
_MAX_URL_CHARS = 2048
_MAX_TEXT_CHARS = 200


@dataclass(frozen=True)
class CiRun:
    """One forge's newest CI run on a branch; `status` `unknown` carries why in `reason`."""

    status: Status
    workflow: Optional[str]
    branch: str
    at: Optional[datetime]
    url: Optional[str]
    reason: Optional[str]


# THE STATUS TABLE, single-homed: `(forge, the forge's own state) -> status`. A completed GitHub run or Azure build,
# and a COMPLETED Bitbucket pipeline, is keyed by `<state>/<result>`. A state missing here is `unknown`, naming it.
_STATUS: dict[str, dict[str, Status]] = {
    "github": {
        "queued": "running", "in_progress": "running", "waiting": "running", "requested": "running",
        "pending": "running",
        "completed/success": "success", "completed/neutral": "success",
        "completed/failure": "failure", "completed/timed_out": "failure", "completed/startup_failure": "failure",
        "completed/action_required": "failure",
        "completed/cancelled": "cancelled", "completed/skipped": "cancelled",
    },
    "gitlab": {
        "created": "running", "waiting_for_resource": "running", "preparing": "running", "pending": "running",
        "running": "running", "scheduled": "running", "manual": "running",
        "success": "success",
        "failed": "failure",
        "canceled": "cancelled", "canceling": "cancelled", "skipped": "cancelled",
    },
    "bitbucket": {
        "PENDING": "running", "IN_PROGRESS": "running", "PAUSED": "running", "HALTED": "running",
        "COMPLETED/SUCCESSFUL": "success",
        "COMPLETED/FAILED": "failure", "COMPLETED/ERROR": "failure",
        "COMPLETED/STOPPED": "cancelled", "COMPLETED/EXPIRED": "cancelled",
    },
    "azure_devops": {
        "notStarted": "running", "inProgress": "running", "postponed": "running", "cancelling": "cancelled",
        "completed/succeeded": "success",
        "completed/partiallySucceeded": "failure", "completed/failed": "failure",
        "completed/canceled": "cancelled",
    },
}


def _unknown(branch: str, reason: str) -> CiRun:
    return CiRun("unknown", None, branch, None, None, reason)


# A read that answered no run; the message is the viewer-facing reason.
_ReadFailed = forge_repos.ForgeError


@dataclass(frozen=True)
class _Read:
    """What one project's HTTP read needs, resolved from the DB on the caller's thread."""

    key: tuple[int, str, UUID]
    ref: forges.RepoRef
    branch: str
    plan: Any  # forge_repos._Plan: the viewer's resolved login on `ref`'s root


# `(project_id, branch, credential_id) -> (monotonic expiry, run)`; process-local by design (never persisted).
_cache: dict[tuple[int, str, UUID], tuple[float, CiRun]] = {}
_cache_lock = threading.Lock()
# One pool for the process: a slow forge holds at most these workers, however often a page asks.
_pool = ThreadPoolExecutor(max_workers=_BATCH_WORKERS, thread_name_prefix="forge-ci")


def _cached(key: tuple[int, str, UUID]) -> Optional[CiRun]:
    with _cache_lock:
        hit = _cache.get(key)
        if hit is None:
            return None
        if hit[0] <= time.monotonic():
            del _cache[key]
            return None
        return hit[1]


def _remember(key: tuple[int, str, UUID], run: CiRun) -> None:
    ttl = _FAILURE_TTL_SECONDS if run.status == "unknown" else _TTL_SECONDS
    now = time.monotonic()
    with _cache_lock:
        # Sweep on write, so a key nobody asks for again (an old branch, a replaced credential) does not stay.
        for stale in [k for k, (expiry, _run) in _cache.items() if expiry <= now]:
            del _cache[stale]
        _cache[key] = (now + ttl, run)


def clear_cache() -> None:
    """Forget every cached read (tests)."""
    with _cache_lock:
        _cache.clear()


def latest_run(session: Session, project: Project, viewer: AdminUser) -> CiRun:
    """`project`'s newest CI run on its integration branch, as `viewer`'s own forge account sees it, under the
    batch's cap (`latest_runs`): a per-step HTTP timeout alone does not bound connect, TLS and headers together.
    Never raises."""
    return latest_runs(session, [project], viewer)[project.id]


def latest_runs(session: Session, projects: list[Project], viewer: AdminUser) -> dict[int, CiRun]:
    """`latest_run` for each of `projects`, keyed by project id: the reads run eight at a time and the whole batch
    is capped at eight seconds — a project still unread then is `unknown`; a read already running finishes into the
    cache for the next look, one still queued is dropped. Never raises."""
    out: dict[int, CiRun] = {}
    pending: dict[int, _Read] = {}
    for project in projects:
        planned = _plan(session, project, viewer)
        if isinstance(planned, CiRun):
            out[project.id] = planned
        else:
            hit = _cached(planned.key)
            if hit is not None:
                out[project.id] = hit
            else:
                pending[project.id] = planned
    if not pending:
        return out
    futures = {project_id: _pool.submit(_read, read) for project_id, read in pending.items()}
    wait(futures.values(), timeout=_BATCH_CAP_SECONDS)
    for project_id, future in futures.items():
        read = pending[project_id]
        if future.done():
            out[project_id] = future.result()  # `_read` never raises
        else:
            # Never wait on a slow forge: a running read finishes on its own and lands in the cache.
            future.cancel()
            label = forges.FORGE_LABELS[read.ref.forge]
            out[project_id] = _unknown(read.branch, f"Timed out waiting for {label}. Try again in a minute.")
    return out


def _plan(session: Session, project: Project, viewer: AdminUser) -> _Read | CiRun:
    """The read to make for `project` through `viewer`'s connection, or the `unknown` run that answers without one.
    DB reads only (and a token renewal when due)."""
    branch = project.integration_branch
    try:
        if not project.repo_url:
            return _unknown(branch, "This project has no repository connected, so there is no CI to show.")
        ref = forges.repo_ref_or_none(project)
        if ref is None:
            return _unknown(branch, "This project's repository URL cannot be read. Fix it in the project's settings.")
        label = forges.FORGE_LABELS[ref.forge]
        planned = forge_repos._plan_for(session, viewer, ref, None)
        if planned is None:
            return _unknown(branch, f"Connect {label} in Profile > Git to see CI.")
        if not isinstance(planned, forge_repos._Plan) or planned.credential_id is None:
            return _unknown(branch, f"Reconnect {label} in Profile > Git to see CI.")
        return _Read((project.id, branch, planned.credential_id), ref, branch, planned)
    except Exception:  # noqa: BLE001 - a CI badge never breaks the page that shows it
        log.exception("forge-ci: project id=%s could not be planned", project.id)
        return _unknown(branch, "CI could not be read for this project.")


def _read(read: _Read) -> CiRun:
    """One forge read, cached by its key; every failure an `unknown` run naming why. Never raises."""
    label = forges.FORGE_LABELS[read.ref.forge]
    try:
        run = _READERS[read.ref.forge](read)
    except _ReadFailed as exc:
        run = _unknown(read.branch, str(exc))
    except httpx.TimeoutException:
        run = _unknown(read.branch, f"{label} did not answer in time. Try again in a minute.")
    except httpx.HTTPError as exc:
        log.warning("forge-ci: %s read failed (%s)", read.ref.root, type(exc).__name__)
        run = _unknown(read.branch, f"{label} could not be reached. Try again in a minute.")
    except Exception:  # noqa: BLE001 - one forge's surprise is that project's unknown, not the page's error
        log.exception("forge-ci: %s read raised", read.ref.root)
        run = _unknown(read.branch, f"{label}'s CI answer could not be read.")
    _remember(read.key, run)
    return run


def _get(read: _Read, url: str, params: dict[str, Any]) -> Any:
    """One JSON answer from `read`'s forge, its refusals in words."""
    label = forges.FORGE_LABELS[read.ref.forge]

    def refused(status: int, _said: Optional[str]) -> str:
        # Azure DevOps answers a dead PAT with 203 and its sign-in page.
        if status in (401, 203):
            return f"{label} refused your token. Reconnect {label} in Profile > Git."
        if status == 403:
            # A live token without CI access (CI off, or a role that cannot see pipelines): reconnecting won't help.
            return f"Your {label} account cannot see CI on {read.ref.path}. Check your access on {label}."
        if status == 404:
            return f"{label} does not show {read.ref.path} or its CI to your account. Check your access on {label}."
        return f"{label} answered HTTP {status}. Try again in a minute."
    wording = forge_repos.Wording(
        timeout=f"{label} did not answer in time. Try again in a minute.",
        rate_limited=f"{label} is rate-limiting your account. Try again in a few minutes.",
        sso="An organization requires SAML sign-in for your GitHub token. Authorize it on GitHub.",
        refused=refused, unreadable=f"{label}'s CI answer could not be read.",
    )
    with forge_repos._plan_client(read.plan) as client:
        client.timeout = httpx.Timeout(_REQUEST_TIMEOUT_SECONDS)
        deadline = time.monotonic() + _REQUEST_TIMEOUT_SECONDS
        return forge_repos.request(client, "GET", url, deadline, wording, params=params)[0]


def _path(ref: forges.RepoRef) -> list[str]:
    """`ref.path`'s decoded segments (`forge_repos.path_segments`), or the refusal for one that would walk the API path."""
    segments = forge_repos.path_segments(ref.path)
    if segments is None:
        raise _ReadFailed("This project's repository URL cannot be read. Fix it in the project's settings.")
    return segments


def _api(read: _Read) -> str:
    return forge_repos.repo_api(read.plan, _path(read.ref))


def _first(body: Any, key: Optional[str], read: _Read) -> dict:
    """The newest run in `body` (`body[key]`, or `body` itself when `key` is None), or `_ReadFailed` for none."""
    items = body.get(key) if key is not None and isinstance(body, dict) else body
    if not isinstance(items, list):
        raise _ReadFailed(f"{forges.FORGE_LABELS[read.ref.forge]}'s CI answer could not be read.")
    if not items:
        raise _ReadFailed(f"No CI runs on {read.branch} yet.")
    if not isinstance(items[0], dict):
        raise _ReadFailed(f"{forges.FORGE_LABELS[read.ref.forge]}'s CI answer could not be read.")
    return items[0]


# The same nested-string read every forge answer needs, `forge_repos`' (as `_plan_for` and `_Plan` above).
_str = forge_repos._str


# ISO 8601 with at most microseconds: Azure writes seven fractional digits.
_FRACTION = re.compile(r"(\.\d{6})\d+")


def _time(raw: Optional[str]) -> Optional[datetime]:
    return None if raw is None else parse_utc(_FRACTION.sub(r"\1", raw))


def _run(read: _Read, state: Optional[str], workflow: Optional[str], at: Optional[str], url: Optional[str]) -> CiRun:
    """The run for the forge's own `state`, through the status table."""
    status = _STATUS[read.ref.forge].get(state or "")
    reason = None
    if status is None:
        status, reason = "unknown", f"{forges.FORGE_LABELS[read.ref.forge]} reported a state Jaah does not know: {_text(state)}."
    return CiRun(status, _text(workflow), read.branch, _time(at), _https(url), reason)


def _https(url: Optional[str]) -> Optional[str]:
    """A forge-supplied link, only when it is a bounded https URL: a self-managed forge picks its own answers."""
    return url if url and url.lower().startswith("https://") and len(url) <= _MAX_URL_CHARS else None


def _text(value: Optional[str]) -> Optional[str]:
    return value[:_MAX_TEXT_CHARS] if value else value


def _github(read: _Read) -> CiRun:
    body = _get(read, f"{_api(read)}/actions/runs", {"branch": read.branch, "per_page": 1})
    run = _first(body, "workflow_runs", read)
    state = _str(run, "status")
    if state == "completed" and _str(run, "conclusion") is not None:
        state = f"completed/{_str(run, 'conclusion')}"
    elif state == "completed":
        state = "in_progress"  # GitHub briefly reports a finished run before its conclusion lands
    return _run(read, state, _str(run, "name"), _str(run, "created_at"), _str(run, "html_url"))


def _gitlab(read: _Read) -> CiRun:
    body = _get(read, f"{_api(read)}/pipelines", {"ref": read.branch, "per_page": 1})
    run = _first(body, None, read)
    return _run(read, _str(run, "status"), _str(run, "name"), _str(run, "created_at"), _str(run, "web_url"))


def _bitbucket(read: _Read) -> CiRun:
    body = _get(read, f"{_api(read)}/pipelines/", {"target.branch": read.branch, "sort": "-created_on", "pagelen": 1})
    run = _first(body, "values", read)
    state = _str(run, "state", "name")
    if state == "COMPLETED":
        state = f"COMPLETED/{_str(run, 'state', 'result', 'name')}"
    number = run.get("build_number")
    # The API links no web page for a pipeline: its results page is built from the build number.
    url = f"{read.ref.web_url()}/pipelines/results/{number}" if isinstance(number, int) else None
    workflow = f"Pipeline #{number}" if isinstance(number, int) else None
    return _run(read, state, workflow, _str(run, "created_on"), url)


def _azure(read: _Read) -> CiRun:
    segments = _path(read.ref)
    if len(segments) != 3:
        raise _ReadFailed("This project's repository URL cannot be read. Fix it in the project's settings.")
    org, project, _repo = (forges.quote_segment(part) for part in segments)
    body = _get(read, f"https://dev.azure.com/{org}/{project}/_apis/build/builds", {
        "branchName": f"refs/heads/{read.branch}", "$top": 1, "queryOrder": "queueTimeDescending",
        "api-version": "7.1",
    })
    run = _first(body, "value", read)
    state = _str(run, "status")
    if state == "completed":
        state = f"completed/{_str(run, 'result')}"
    return _run(read, state, _str(run, "definition", "name"), _str(run, "queueTime"), _str(run, "_links", "web", "href"))


_READERS: dict[str, Callable[[_Read], CiRun]] = {
    "github": _github, "gitlab": _gitlab, "bitbucket": _bitbucket, "azure_devops": _azure,
}


# ── The project's workflows, for a CI gate's picker (`GET /api/projects/{id}/pipeline/ci-workflows`) ─────────────
#
# What a gate's `workflow` names is what `jaah-forge run dispatch` takes (`apps/jaahd/forge_cli/adapters`): GitHub a
# workflow file, Azure a pipeline id, Bitbucket a `custom:` pipeline, GitLab a job (`*` = the whole pipeline). Only
# GitHub and Azure list them through their API; GitLab and Bitbucket list them by reading the CI file at the default
# branch. Read lazily (the picker asks), never cached, never stored. An unreadable list is `[]`: the picker then takes
# the name as typed.

GITLAB_CI_FILE = ".gitlab-ci.yml"
BITBUCKET_CI_FILE = "bitbucket-pipelines.yml"
WHOLE_PIPELINE = "*"
# A picker lists at most this many; a GitLab file pulls in at most this many `include: local` files. A CI file is
# read up to `_MAX_CI_FILE_BYTES` (a larger one is unreadable: type the name), and one listing stops reading
# includes once `_LIST_BUDGET_SECONDS` have passed.
_MAX_WORKFLOWS = 200
_MAX_INCLUDES = 10
_MAX_CI_FILE_BYTES = 256 * 1024
_LIST_BUDGET_SECONDS = 15.0
# At most this many listings read at once in a process: a busy picker answers `[]` (type the name) rather than
# holding more worker threads.
_LIST_SLOTS = threading.BoundedSemaphore(4)
# GitLab's top-level keywords: every other mapping at the top of the file is a job (one starting `.` is hidden).
_GITLAB_KEYWORDS = frozenset({
    "default", "include", "stages", "variables", "workflow", "image", "services", "before_script", "after_script",
    "cache", "types", "spec",
})


@dataclass(frozen=True)
class Workflow:
    """One entry of the picker: `id` is what the gate stores and `run dispatch` takes; `path` the file it lives in."""

    id: str
    name: str
    path: str


class _CiYaml(yaml.SafeLoader):
    """A safe loader that reads a custom tag (GitLab's `!reference`) as nothing rather than refusing the file."""


_CiYaml.add_multi_constructor("!", lambda _loader, _suffix, _node: None)


def workflows(session: Session, project: Project, viewer: AdminUser) -> list[Workflow]:
    """`project`'s CI workflows on its own forge, as `viewer`'s own account reads them, in the forge's order; `[]` when
    there is no connection or the forge or its file cannot be read. Never raises. A route calls the two halves
    itself, the forge reads off the event loop (`plan_workflows`, then `read_workflows` on a worker thread)."""
    return read_workflows(plan_workflows(session, project, viewer))


def plan_workflows(session: Session, project: Project, viewer: AdminUser) -> Optional[_Read]:
    """The DB half of `workflows`: the viewer's read of `project`'s forge, or None when there is none to make."""
    planned = _plan(session, project, viewer)
    return None if isinstance(planned, CiRun) else planned


def read_workflows(planned: Optional[_Read]) -> list[Workflow]:
    """The forge half of `workflows`: HTTP only, no session, so it may run on a worker thread. Never raises."""
    if planned is None or not _LIST_SLOTS.acquire(blocking=False):
        return []
    try:
        found = _LISTERS[planned.ref.forge](planned)
    except (_ReadFailed, httpx.HTTPError, yaml.YAMLError) as exc:
        log.info("forge-ci: %s workflows unread (%s)", planned.ref.root, type(exc).__name__)
        return []
    except Exception:  # noqa: BLE001 - a picker that cannot list falls back to typing, never an error page
        log.exception("forge-ci: %s workflows read raised", planned.ref.root)
        return []
    finally:
        _LIST_SLOTS.release()
    seen: set[str] = set()
    out = []
    for item in found:
        if item.id and item.id not in seen:
            seen.add(item.id)
            out.append(Workflow(_text(item.id) or "", _text(item.name) or item.id, _text(item.path) or ""))
    return out[:_MAX_WORKFLOWS]


def _raw(read: _Read, url: str, params: dict[str, Any]) -> str:
    """A CI file's text as the forge serves it raw, at most `_MAX_CI_FILE_BYTES` (a larger one is unreadable)."""
    label = forges.FORGE_LABELS[read.ref.forge]
    deadline = time.monotonic() + _REQUEST_TIMEOUT_SECONDS
    with forge_repos._plan_client(read.plan) as client:
        client.timeout = httpx.Timeout(_REQUEST_TIMEOUT_SECONDS)
        with client.stream("GET", url, params=params, headers={"Accept": "*/*"}) as response:
            status = response.status_code
            raw, cut = http_bounded.read_capped(response, _MAX_CI_FILE_BYTES + 1,
                                                stop=lambda: time.monotonic() >= deadline)
    if status != 200:
        raise _ReadFailed(f"{label} answered HTTP {status} for the CI file.")
    if cut:
        raise _ReadFailed(f"{label}'s CI file is too large or too slow to read.")
    return raw.decode("utf-8", errors="replace")


def _yaml(text: str) -> dict:
    data = yaml.load(text, Loader=_CiYaml)  # noqa: S506 - `_CiYaml` is a SafeLoader
    return data if isinstance(data, dict) else {}


def _github_workflows(read: _Read) -> list[Workflow]:
    body = _get(read, f"{_api(read)}/actions/workflows", {"per_page": 100})
    rows = body.get("workflows") if isinstance(body, dict) else None
    out = []
    for row in rows if isinstance(rows, list) else []:
        path = _str(row, "path") or ""
        # A dynamic workflow (code scanning, Dependabot) has no file and cannot be started by hand.
        if not path.startswith(".github/workflows/") or _str(row, "state") not in (None, "active"):
            continue
        out.append(Workflow(posixpath.basename(path), _str(row, "name") or path, path))
    return out


def _gitlab_file(read: _Read, branch: str, path: str) -> dict:
    return _yaml(_raw(read, f"{_api(read)}/repository/files/{forges.quote_segment(path)}/raw", {"ref": branch}))


def _gitlab_jobs(data: dict, path: str) -> list[Workflow]:
    return [Workflow(name, name, path) for name, body in data.items()
            if isinstance(name, str) and isinstance(body, dict) and name not in _GITLAB_KEYWORDS
            and not name.startswith(".")]


def _gitlab_local_includes(data: dict) -> list[str]:
    """The repository's own files `include:` names (`local:`, or a bare path); a template, another project's file,
    a remote URL or a wildcard cannot be read here."""
    entries = data.get("include")
    out = []
    for entry in entries if isinstance(entries, list) else [entries]:
        path = entry.get("local") if isinstance(entry, dict) else entry
        if (isinstance(path, str) and path and "://" not in path and "*" not in path
                and not (isinstance(entry, dict) and set(entry) & {"project", "remote", "template", "component"})):
            out.append(path.lstrip("/"))
    return out[:_MAX_INCLUDES]


def _gitlab_workflows(read: _Read) -> list[Workflow]:
    project = _get(read, _api(read), {})
    branch = _str(project, "default_branch") or read.branch
    data = _gitlab_file(read, branch, GITLAB_CI_FILE)
    out = [Workflow(WHOLE_PIPELINE, "Whole pipeline", GITLAB_CI_FILE), *_gitlab_jobs(data, GITLAB_CI_FILE)]
    deadline = time.monotonic() + _LIST_BUDGET_SECONDS
    for path in _gitlab_local_includes(data):
        if time.monotonic() >= deadline:
            break  # the jobs read so far; the rest can be typed
        try:
            out += _gitlab_jobs(_gitlab_file(read, branch, path), path)
        except (_ReadFailed, yaml.YAMLError):
            continue  # one unreadable include leaves the rest of the list
    return out


def _bitbucket_workflows(read: _Read) -> list[Workflow]:
    # `HEAD` is the repository's main branch.
    data = _yaml(_raw(read, f"{_api(read)}/src/HEAD/{BITBUCKET_CI_FILE}", {}))
    custom = (data.get("pipelines") or {}).get("custom") if isinstance(data.get("pipelines"), dict) else None
    return [Workflow(name, name, BITBUCKET_CI_FILE) for name in (custom if isinstance(custom, dict) else {})
            if isinstance(name, str)]


def _azure_workflows(read: _Read) -> list[Workflow]:
    segments = _path(read.ref)
    if len(segments) != 3:
        raise _ReadFailed("This project's repository URL cannot be read. Fix it in the project's settings.")
    org, project, _repo = (forges.quote_segment(part) for part in segments)
    # A pipeline is project-wide: only those that build THIS repository are listed (`run dispatch` refuses the rest).
    body = _get(read, f"https://dev.azure.com/{org}/{project}/_apis/build/definitions",
                {"includeAllProperties": "true", "api-version": "7.1"})
    rows = body.get("value") if isinstance(body, dict) else None
    repo = segments[2].lower()
    return [Workflow(str(row["id"]), _str(row, "name") or str(row["id"]), _str(row, "process", "yamlFilename") or "")
            for row in (rows if isinstance(rows, list) else [])
            if isinstance(row, dict) and isinstance(row.get("id"), int)
            and (_str(row, "repository", "name") or "").lower() == repo]


_LISTERS: dict[str, Callable[[_Read], list[Workflow]]] = {
    "github": _github_workflows, "gitlab": _gitlab_workflows, "bitbucket": _bitbucket_workflows,
    "azure_devops": _azure_workflows,
}
