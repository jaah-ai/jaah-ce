"""What jaah-forge refuses — decided BEFORE any network call wherever it can be.

The rules are the `gh` PR guard's, carried to every forge (AC4.4):
  * the base must be the project's `integration_branch` and the head under its `branch_prefix`
    (`tools/project_config.py::ProjectConfig`, read unchanged — the pool checkout's own project);
  * `push_enabled=false` refuses every PR write that could land work (create, merge, auto-merge);
  * no closing keyword next to an issue reference in a PR's title or body, in THAT forge's dialect,
    unless opted in exactly as the commit-msg guard allows (`Closes-Intentionally:` / `ALLOW_CLOSING_KEYWORD`);
  * `pr merge --sha S` refuses when the forge's head is no longer S (and sends S as the merge's pin);
  * `pr merge` makes a merge commit only, and only once the REQUIRED checks are `success` (or there are
    none) and `merge_state` is `clean` — a red optional check does not hold a PR the forge would merge;
  * `run rerun` re-runs only a run on a branch under the project's `branch_prefix` — under an empty prefix only
    the runner's own `fleet/task-<CONTROL_TASK_ID>` — never with `push_enabled=false`: the integration
    branch's own runs are not a runner's to retrigger;
  * `pr close` and `pr ready` act only on an open PR; `pr close` posts its reason as a comment first (`cli.py`).
  * `run dispatch` starts a workflow on the integration branch or your own branch only, never with
    `push_enabled=false` (`check_dispatch`).
`api` has only `get`, and `run` never cancels (`cli.py`).

Every refusal exits 2 with the project's delivery recipe on stderr (`ProjectConfig.delivery_recipe`).
"""
from __future__ import annotations

import re
import subprocess
import sys
import urllib.parse
from pathlib import Path

from .model import PolicyRefusal, PullRequest

# tools/ beside apps/ — in a checkout and in the installed bundle (/usr/lib/jaahd) alike.
_TOOLS = Path(__file__).resolve().parents[3] / "tools"

# GitHub's nine; Bitbucket adds the -ing forms; GitLab's default closing pattern adds `implement…` too.
_KEYWORDS = {
    "github": r"(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)",
    "bitbucket": r"(?:clos(?:e[sd]?|ing)|fix(?:e[sd]|ing)?|resolv(?:e[sd]?|ing))",
    "gitlab": r"(?:clos(?:e[sd]?|ing)|fix(?:e[sd]|ing)?|resolv(?:e[sd]?|ing)|implement(?:s|ed|ing)?)",
    "azure_devops": r"(?:close[sd]?|fix(?:e[sd])?|resolve[sd]?)",
}
# Both GitLab and Bitbucket read `closes issue #12` (GitLab also `issues`).
_ISSUE_WORD = {"gitlab": r"(?:issues?[ \t]+)?", "bitbucket": r"(?:issue[ \t]+)?"}
# `#N` must not be the tail of a longer token (`AB#12` is Azure's, `x#1` is not a reference on its own).
_HASH = r"(?<![\w#])#\d+"
_ISSUE_URL = r"https?://[^\s)>\]]+/issues/\d+"
_REFERENCES = {
    # GitHub: exactly what the commit-msg guard matches (`tools/check_commit_msg_closing_keyword.py`).
    "github": (r"https?://github\.com/[\w.-]+/[\w.-]+/(?:issues|pull)/\d+", r"[\w.-]+/[\w.-]+#\d+",
               r"GH-\d+", _HASH),
    # GitLab: the same set, plus `project#N` / `group/project#N` at any depth and a full issue URL.
    "gitlab": (_ISSUE_URL, r"[\w.-]+(?:/[\w.-]+)*#\d+", _HASH),
    # Bitbucket: the same set, plus a full issue URL.
    "bitbucket": (_ISSUE_URL, r"[\w.-]+/[\w.-]+#\d+", _HASH),
    # Azure Boards: `fixes AB#N`, and `#N` which Azure Repos links to a work item too.
    "azure_devops": (r"AB#\d+", _HASH),
}
CLOSING_RES = {
    forge: re.compile(r"\b" + _KEYWORDS[forge] + r"\b[ \t]*:?[ \t]+" + _ISSUE_WORD.get(forge, "")
                      + r"(?:" + "|".join(refs) + r")", re.IGNORECASE)
    for forge, refs in _REFERENCES.items()
}
# GitLab runs a line that starts with a quick action (`/close`, `/merge`, `/target_branch master`) in an MR's
# title, description or a note — a close verb by another name, and a merge past every check here.
QUICK_ACTION_RE = re.compile(r"^[ \t]*/[a-z_]+(?=\s|$)", re.MULTILINE | re.IGNORECASE)


def _project_config():
    if str(_TOOLS) not in sys.path:
        sys.path.insert(0, str(_TOOLS))
    import project_config  # noqa: E402 — tools/ is not a package

    return project_config


def governance(cwd: str, login):
    """The governing `ProjectConfig` for the pool checkout at `cwd` — its own project, by id. Not a pool
    checkout (a `cd /tmp` included), a project whose values cannot be read, or a checkout whose repo is not
    the one the LOGIN writes to (another project's slot) refuses every PR write (fail closed)."""
    pc = _project_config()
    try:
        config = pc.resolve(repo=cwd)
    except pc.UnknownProject as exc:
        raise PolicyRefusal(f"not a pool checkout of a Jaah project this runner may write to: {exc}") from None
    except pc.UnresolvedProject as exc:  # NoProjectData included
        raise PolicyRefusal(f"cannot read this project's governance, so no PR write is allowed: {exc}") from None
    # The login is minted for the task's own project's repo: a checkout of any other repo's project may not
    # lend its rules to it. org/repo here is the forge coordinate, never the identity.
    if pc.normalize_repo_slug(login.web_url) != config.repo_slug:
        raise PolicyRefusal(
            f"this checkout is project '{config.name}' ({config.repo_slug}), but the login writes to "
            f"{login.web_url} — work from your own task's pool checkout")
    return config


# A forge origin's repository path: after `scheme://[user@]host[:port]/`, or after scp-style `user@host:`.
_ORIGIN_PATH = re.compile(r"^(?:[a-z][a-z0-9+.-]*://[^/]*/|[^/@\s]+@[^:/\s]+:)(.+)$", re.IGNORECASE)


def _repo_name(path: str) -> str:
    return re.sub(r"\.git$", "", urllib.parse.unquote(path.strip("/")).rsplit("/", 1)[-1], flags=re.IGNORECASE).lower()


def check_checkout(cwd: str, login) -> None:
    """Every verb, reads included: a git checkout at `cwd` whose `origin` names ANOTHER repository than the one the
    login reaches is refused. The broker answers a checkout it cannot place in the runner's slot with the
    primary's login, so without this a `pr view 25` run in a clone of a repository the project does not connect
    would silently answer about the primary's #25. Only the repository NAME is compared — owners, groups, Azure
    `_git/`, legacy hosts and escapes differ between an origin and the login's `repo_path` — so a same-named
    fork passes and is left to the broker's answer, as is a checkout with no origin or a non-forge one (a path)."""
    try:
        out = subprocess.run(["git", "-C", cwd, "remote", "get-url", "origin"], capture_output=True, text=True,
                             timeout=10, stdin=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return
    match = _ORIGIN_PATH.match(out.stdout.strip() if out.returncode == 0 else "")
    if not match or match.group(0).lower().startswith("file:"):
        return
    name, own = _repo_name(match.group(1)), _repo_name(login.repo_path or "")
    if name and own and name != own:
        raise PolicyRefusal(
            f"this checkout is repository '{name}', not '{own}', the one your login reaches: jaah-forge acts only "
            "on your project's repositories — run it from one of their checkouts")


def current_branch(cwd: str) -> str:
    """The checked-out branch (local git, no network), or "" when detached."""
    try:
        out = subprocess.run(["git", "symbolic-ref", "--quiet", "--short", "HEAD"], cwd=cwd,
                             capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return ""
    return out.stdout.strip() if out.returncode == 0 else ""


def recipe(config) -> str:
    return config.delivery_recipe() if config is not None else ""


def refuse(config, reason: str) -> PolicyRefusal:
    line = recipe(config)
    return PolicyRefusal(reason + (f"\n  deliver: {line}" if line else ""))


def closing_hits(forge: str, text: str, environ) -> list[str]:
    """The closing-keyword hits in `text` for `forge`'s dialect; none when opted in."""
    hits = [m.group(0) for m in CLOSING_RES[forge].finditer(text or "")]
    if hits and _opted_in(text, environ):
        return []
    return hits


def _opted_in(text: str, environ) -> bool:
    if str(_TOOLS) not in sys.path:
        sys.path.insert(0, str(_TOOLS))
    from check_commit_msg_closing_keyword import has_opt_in  # the one escape hatch, shared with `gh`

    return has_opt_in(text, dict(environ))


def check_quick_actions(config, forge: str, *texts: str) -> None:
    if forge != "gitlab":
        return
    hits = [m.group(0).strip() for text in texts for m in QUICK_ACTION_RE.finditer(text or "")]
    if hits:
        raise refuse(config, (
            f"GitLab would run {', '.join(hits)} as a quick action (it can close, merge or retarget the MR); "
            "indent or reword that line"))


def check_text(config, forge: str, title: str, body: str, environ) -> None:
    check_quick_actions(config, forge, title, body)
    hits = closing_hits(forge, f"{title}\n{body}", environ)
    if hits:
        raise refuse(config, (
            f"the PR title/body carries a closing keyword next to an issue reference ({', '.join(hits)}); "
            "merging it would CLOSE that issue. Reword it (`the gap on #12 is closed`), or opt in with a "
            "`Closes-Intentionally: <ref>` line in the body"))


def check_push(config) -> None:
    if config is not None and not config.push_enabled:
        raise refuse(config, "this project has push_enabled=false: no PR may be opened or merged here")


def check_branches(config, head: str, base: str) -> None:
    if config is None:
        return
    check_push(config)
    if base != config.integration_branch:
        raise refuse(config, f"base '{base}' is not this project's integration branch '{config.integration_branch}'")
    if not config.allows_pr_from(head):
        raise refuse(config, f"head '{head}' is not under this project's branch prefix '{config.branch_prefix}'")


_BRANCH = re.compile(r"[A-Za-z0-9._/+\-]+")


def is_branch_name(value: str) -> bool:
    """A git branch name a list filter may carry (Bitbucket splices it into a BBQL literal)."""
    return bool(_BRANCH.fullmatch(value)) and ".." not in value and not value.startswith(("-", "/"))


def check_open(pr: PullRequest) -> None:
    if pr.state != "open":
        raise PolicyRefusal(f"PR #{pr.number} is {pr.state}, not open")


def check_rerun(config, run, task_id: str = "") -> None:
    if config is None:
        return
    check_push(config)
    # An empty prefix admits every branch, the integration branch included: only the runner's own
    # `fleet/task-<id>` (its `CONTROL_TASK_ID`, the spelling `slot_repos._fleet_branch` cuts) is then "your own".
    # A guardrail against a mistake, not a boundary against a hostile runner: the id is the caller's env.
    task_id = task_id.strip()
    own = config.branch_prefix or (task_id.isdigit() and run.branch == f"fleet/task-{task_id}")
    if (not run.branch or not own or run.branch == config.integration_branch
            or not config.allows_pr_from(run.branch)):
        mine = (f"this project's prefix '{config.branch_prefix}'" if config.branch_prefix
                else f"your own 'fleet/task-{task_id or '<id>'}'")
        raise refuse(config, (
            f"run {run.id} built '{run.branch or '(no branch)'}', not {mine}: "
            "only your own branch's runs are yours to re-run"))


def check_dispatch(config, ref: str, task_id: str = "") -> None:
    """`run dispatch`: a run on the integration branch (its hand-run workflows) or on your own branch, as for
    `check_rerun` — never on a branch no task of this project delivers to (`master`, a release branch)."""
    if config is None:
        return
    check_push(config)
    task_id = task_id.strip()
    own = (config.allows_pr_from(ref) if config.branch_prefix
           else task_id.isdigit() and ref == f"fleet/task-{task_id}")
    if ref != config.integration_branch and not own:
        raise refuse(config, (
            f"--ref '{ref}' is neither this project's integration branch '{config.integration_branch}' nor your "
            "own branch: only those are yours to start a workflow on"))


def check_target(config, pr: PullRequest) -> None:
    """A merge / auto-merge is judged against the PR the forge reports, not the command line."""
    if pr.state != "open":
        raise refuse(config, f"PR #{pr.number} is {pr.state}, not open")
    check_branches(config, pr.head, pr.base)


def check_head(config, pr: PullRequest, sha: str) -> None:
    """`pr merge --sha S`: the forge's head must still be S — the tree a merge lane tested, not a later push."""
    if (pr.extra.get("head_sha") or "").lower() != sha:
        raise refuse(config, (
            f"PR #{pr.number}'s head moved: the forge reports {pr.extra.get('head_sha') or '(none)'}, "
            f"not the pinned {sha}"))


# A PR merges on a green REQUIRED rollup (`PullRequest.checks`), or on NO checks at all: a repo with no CI is
# in the branching policy's local-hook-gate mode ("where no CI exists, merge immediately"), its gate being the
# pushing process's own `./dc check full`. `pending` and `failure` still refuse.
MERGEABLE_ROLLUPS = ("success", "none")


def check_mergeable(config, pr: PullRequest) -> None:
    if pr.checks not in MERGEABLE_ROLLUPS or pr.merge_state != "clean":
        raise refuse(config, (
            f"PR #{pr.number} is not mergeable yet (checks={pr.checks}, merge_state={pr.merge_state}); "
            f"watch it with `jaah-forge pr checks {pr.number} --watch`, then merge"))
