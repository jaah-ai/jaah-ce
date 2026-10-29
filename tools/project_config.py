#!/usr/bin/env python3
"""project_config.py — THE single home for "what governance values does the project in THIS checkout
declare". Hooks, guards and gate runners read branch, prefix and gate commands from here.

The five values, all NOT NULL on the `projects` row, mandatory, no fallback: `integration_branch`,
`branch_prefix`, `gate_quick_cmd`, `gate_full_cmd`, `push_enabled`. Semantics: apps/api/app/models/'s
Project docstring — this module only RESOLVES them.

RESOLUTION ORDER:
  1. the live control-plane API (short timeout), refreshing an on-disk cache on success;
  2. that on-disk cache (a previous successful answer), even when stale;
  3. FAIL CLOSED — raise `UnresolvedProject`, naming the project and what was missing.
Nothing outside the row supplies a VALUE. A caller can still move WHERE THE CACHE LIVES (`JAAH_HOME`)
and WHICH CONSOLE IS ASKED: the variables in `_API_REDIRECT_VARS`/`_NODE_BASE_VARS`, and `API_PORT` in
the `.env` beside whichever copy of this resolver runs (a file `env -u` cannot scrub). Both were
measured turning a blocked push into an allowed one and planting a fake `gate_quick_cmd` in the cache.
Moving the lookup is a real affordance (the deterministic port grid), so it is fenced, not removed:
  • NOTHING UNCORROBORATED IS EVER PERSISTED: `api_base_redirect_reason` is non-empty for every source
    above, and `governance_map` then answers live and skips the cache write.
  • THE TWO ENFORCING HOOKS SCRUB EVERY VARIABLE: the `pre-push` and
    `pre-commit` hooks run this under `env -u …`, re-supplying `JAAH_CP_BASE_URL` and
    `JAAH_HOME` from root-owned /etc/jaah/jaahd.env.
  • THE `.env` MAY SPEAK FOR ITS OWN PROJECT AND FOR NOTHING ELSE: a `.env`-derived endpoint answering
    about another project falls back to the cache, else `NoProjectData` (the floor).
  • WHAT REMAINS: that `.env` still chooses, live, which console describes its own project (affordance and
    lever are one mechanism). Bounded: the push hook and Stop hook prefer the root-owned packaged copy,
    which ships no `.env`; what is exposed is the author's own pre-commit gate, nothing durable.
The one STRUCTURAL exception is apps/jaahd/guards/access-guard-pre-push.py, byte-copied into managed
clones that cannot reach our API: it reads the same values from CP-synced git-config markers (tier 2 in
another medium).

FOUR answers, not two — collapsing any pair is either an outage or a bypass:
  • `ProjectConfig`      — this repo IS a Jaah project and here are its values. Gate it.
  • `UnknownProject`     — this checkout is not a pool clone, or a project list was obtained and its
    project is not in it. NOT OURS TO GATE.
  • `UnresolvedProject`  — this repo IS in the list and the row does not carry the values. Callers BLOCK.
  • `NoProjectData`      — no governance obtainable at all (no list: API unreachable AND no cache). A
    SUBCLASS of `UnresolvedProject`, so a caller that does not distinguish it fails closed.

WHY `NoProjectData` IS A SEPARATE ANSWER. In a managed FOREIGN clone (and every repo the hooks are
vendored into) "no data" is the PERMANENT NORMAL STATE; blanket fail-closed would be a fleet-wide push
outage. On a jaah checkout it would be the "stop the API and delete the cache" bypass. THE CACHE IS THE
EVIDENCE OF KNOWN-NESS: a corroborated resolution leaves the list on disk and a stale cache is tier 2,
so a jaah host reaches `NoProjectData` only by losing the console AND the cache. (A `.env`-endpoint
checkout never writes a cache, and an API older than the columns reaches it with the console up; both
land on the callers' conservative posture.) Each caller picks its posture by blast radius:
  • the `pre-push` hook            -> its EXISTING universal floor (block `main`/`master`/`develop`
    destinations), never a no-op and never a total block. Never less safe than it is today.
  • `run_gate.py`                    -> BLOCK. A gate that silently vanishes is the failure mode.
  • `check_gh_pr_guard.py`           -> allow. It gates every Bash tool call in a session; see its
    own docstring for that documented exception, which predates this module.
The authoritative enforcement for a restricted clone is the git-native access-guard-pre-push.py, so the
floor is a redundancy rather than the boundary.

NOTE THE ASYMMETRY, deliberately: for `run_gate.py` an UNKNOWN project is a clean no-op (no gate
to run), while the `pre-push` hook keeps the `main`/`master`/`develop` floor (a direct push to
an integration branch is wrong in any repo). Do not "fix" one to match the other.

IDENTITY IS THE PROJECT ID, AND ONLY THE POOL LAYOUT SUPPLIES IT. `repo_url` is only where a project is
cloned from and the forge coordinate its PRs go to; several projects may share one repo, each with its
own branch pair and its own clone. A checkout under `<JAAH_HOME>/repos/<project id>/` (its git common
dir is that project's main clone) is governed by THAT project's row (`checkout_project_id`), whatever its
remote says. A checkout anywhere else is not a Jaah project: `UnknownProject`, nothing to gate.

A PROJECT MAY CONNECT SEVERAL REPOSITORIES. A non-primary one is cloned at
`<JAAH_HOME>/repos/<project id>/_repos/<repo id>/main`, and its checkouts sit at `_repos/<repo id>/<N>`; where a
checkout sits names its repository (`checkout_repo`). Its checkout is governed by the SAME project row, seen for that
repository (`ProjectConfig.for_repo`): its own slug and integration branch, the project's prefix and push
flag, and no gate or merge lane (those are the primary's alone). A checkout whose repository the project no
longer lists is `UnresolvedProject` (fail closed).

stdlib-only (matches the per-home dependency policy for `tools/` scripts). check_gh_pr_guard.py
delegates here so the two cannot drift.

Usage:
  tools/project_config.py [--remote-url URL] [--repo DIR]   # shell assignments on stdout; the URL is
                                        # the push destination (PUSH_REPO_SLUG), never the identity
Exit codes (the contract every bash caller switches on — one code per answer above):
  0 resolved (assignments printed)   1 FAIL CLOSED: a known project with unreadable values
  2 usage error                      3 unknown project — not ours to gate
  4 no governance data at all (no project list, or an API older than the five columns) — the caller
    picks its own posture
"""
from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

# Sibling imports (the tools/ convention).
sys.path.insert(0, str(Path(__file__).resolve().parent))

# A MISSING SIBLING MUST DEGRADE, NOT WEDGE THE FLEET: this module is vendored into every portfolio repo
# and the `pre-push` hook BLOCKs on any exit code it does not know, so an ImportError would stop
# every push. Exit 4 (`EXIT_NO_PROJECT_DATA`, "cannot tell whether governed") is the honest answer. A
# present-but-too-old sibling lands here too, which is why these names are imported eagerly (e.g.
# `api_base_source` arrived with `api_base_redirect_reason`).
try:
    from control_plane_client import (  # noqa: E402
        API_BASE_FROM_ENV_FILE,
        ControlPlaneApiError,
        DEFAULT_ENV_PATH as RESOLVER_ENV_PATH,
        api_base_source,
        fetch_projects,
    )
    import jaah_paths  # noqa: E402
except ImportError as _exc:  # pragma: no cover — exercised by the vendored-layout test, not in-tree
    if __name__ == "__main__":
        print(
            f"project_config: this copy is incomplete — {_exc}. Cannot resolve any project's governance "
            f"here; the caller should fall back to its own conservative default.",
            file=sys.stderr,
        )
        raise SystemExit(4)
    raise

# 0 where the platform lacks the flag (Windows has neither).
_O_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)

ROOT = Path(__file__).resolve().parent.parent

# Short-TTL on-disk cache of the governance map (the layout's `cache` kind; safe to delete).
# KEYED ON THE GUARDED REPO, not this file's location: the in-tree and packaged copies are preferred by
# different callers, and must prime and read the same file, or `NoProjectData` would need only the
# console plus ONE copy's cache lost.
CACHE_DIR = jaah_paths.cache_dir("project-config")
# Key for the whole-map caller (check_gh_pr_guard.py), kept apart from any single repo's file.
WHOLE_LIST_CACHE_KEY = "_all-projects"
CACHE_TTL_SECONDS = 300.0
# Bounded: callers include a pre-commit hook and a PreToolUse hook, so a hung console must not stall them.
API_TIMEOUT_SECONDS = 2.0

# Which `.env` supplies API_PORT/ADMIN_EMAIL/ADMIN_PASSWORD (for test isolation, as
# apps/jaahd/guards/project_resolve.py). It chooses the console, so it is a REDIRECT (see
# `_API_REDIRECT_VARS`): honoured live, never persisted, scrubbed by the enforcing hooks.
_ENV_PATH_VALUE = os.environ.get("JAAH_ENV_PATH")
ENV_PATH = Path(_ENV_PATH_VALUE) if _ENV_PATH_VALUE else None

# Every environment variable that can move WHERE the live project list comes from — each a bypass if
# trusted and a poisoning vector if persisted:
#   • JAAH_API_URL     — control_plane_client.api_base returns it verbatim, first arm.
#   • JAAH_ENV_PATH    — chooses the `.env` that supplies API_PORT (and the login credentials).
#   • *_proxy             — urllib's default ProxyHandler does NOT bypass loopback, so a proxy var
#     re-points the request without changing the URL.
# JAAH_HOME is not here (it moves the cache, not the answer); the hooks scrub it anyway, since a
# pre-planted cache is a tier-2 answer.
_API_REDIRECT_VARS = (
    "JAAH_API_URL", "JAAH_ENV_PATH", "COCKPIT_API_URL", "COCKPIT_ENV_PATH",
    "http_proxy", "HTTP_PROXY", "https_proxy", "HTTPS_PROXY", "all_proxy", "ALL_PROXY",
)

# api_base's node arms, read when the resolver's `.env` has no API_PORT (the packaged copy's normal
# state). On a node they are also the legitimate pointer to the real control plane (set by
# apps/jaahd/fleet_dispatch.py and the node unit), so the test is "does it agree with the node's
# ROOT-OWNED config", not "is it set".
_NODE_BASE_VARS = ("COCKPIT_AGENT_BASE", "JAAH_CP_BASE_URL")

# The node's root-owned unit config (0644 root:root; node.env beside it is 0640 and unreadable here):
# a source for "the control plane this node belongs to" the pusher cannot write.
NODE_CONFIG_PATH = Path("/etc/jaah/jaahd.env")

EXIT_OK = 0
EXIT_FAIL_CLOSED = 1
EXIT_USAGE = 2
EXIT_UNKNOWN_PROJECT = 3
EXIT_NO_PROJECT_DATA = 4

# The five mandatory columns, in CLI print order; names match the DB.
TEXT_FIELDS = ("integration_branch", "branch_prefix", "gate_quick_cmd", "gate_full_cmd")
BOOL_FIELDS = ("push_enabled",)
# All five. Never the API's seven: the stack pair must not be added.
GOVERNANCE_FIELDS = (*TEXT_FIELDS, *BOOL_FIELDS)

# A git hook exports `GIT_DIR`/`GIT_WORK_TREE`/`GIT_INDEX_FILE`/`GIT_PREFIX` (they override `cwd=`/`-C`, so
# a child git call answers about the WRONG repo), and a parent run as `git -c …` exports
# `GIT_CONFIG_PARAMETERS` (injected config: `core.hooksPath`, `core.fsmonitor` — code). The shallow, graft
# and replace-ref vars reshape the history a scan walks. So EVERY `GIT_*` goes, except what names an
# identity or a transport, which no caller wants to lose, and `GIT_CEILING_DIRECTORIES`: the daemon
# exports it as a folder slot's fence so an ancestor `.git` is NOT read,
# the one `GIT_*` that narrows discovery rather than widening it.
_GIT_ENV_KEEP_PREFIXES = (
    "GIT_AUTHOR_", "GIT_COMMITTER_", "GIT_SSH", "GIT_TERMINAL_PROMPT", "GIT_EDITOR", "GIT_CEILING_DIRECTORIES",
)


class UnknownProject(RuntimeError):
    """This checkout is not a pool clone, or its project is not in the list obtained. NOT ours to gate;
    callers never hard-block on it (see the module docstring's asymmetry note)."""


class UnresolvedProject(RuntimeError):
    """This checkout IS a Jaah project and its governance values could not be read off the row.
    Callers BLOCK (tier 3). Base class of `NoProjectData`, so not distinguishing fails closed."""


class NoProjectData(UnresolvedProject):
    """No project list could be obtained AT ALL (API unreachable AND no cache): whether this checkout is
    a Jaah project is unknowable. Normal in a vendored foreign clone, an incident on a jaah host, so
    the posture is the caller's — read the module docstring's `NoProjectData` block first."""


@dataclass(frozen=True)
class RepoConfig:
    """One repository the project connects (the row's `repos[]`): its id, `owner/name` slug and
    integration branch, and whether it is the primary."""

    id: int
    repo_slug: str
    integration_branch: str
    primary: bool


@dataclass(frozen=True)
class ProjectConfig:
    """One project's resolved governance. `branch_prefix` carries its trailing slash (`'sn/'`) or is
    `''` — a VALUE (no prefix), never "unset"."""

    name: str
    repo_slug: str
    integration_branch: str
    branch_prefix: str
    gate_quick_cmd: str
    gate_full_cmd: str
    push_enabled: bool
    #: `projects.id` — the project's identity (`project_key`); None for a row that carried none.
    project_id: int | None = None
    #: Whether the merge lane merges this project (the projection's `merge_lane`, apps/api/app/merge_lane_state.py).
    #: NOT a governance value: read LENIENTLY (`_lenient_merge_lane`) and outside `GOVERNANCE_FIELDS`, because
    #: a strict read would make every row from a control plane older than the field UNRESOLVED (fail-closed).
    merge_lane: bool = False
    #: Every repository the project connects; `()` from a control plane older than the list.
    repos: tuple[RepoConfig, ...] = ()
    #: False for the view of a NON-primary repository (`for_repo`): no gate and no merge lane apply there.
    primary: bool = True

    def for_repo(self, repo_id: int | None) -> "ProjectConfig | None":
        """This project's governance as seen from a checkout of repository `repo_id` (`checkout_repo`), or
        None when the project lists repositories and that is not one of them.

        None id (the primary's checkouts: every single-repository project's) and the primary's own id answer
        with the project's own values. A non-primary repository keeps the project-level prefix and push flag
        (N2) and takes its own slug and integration branch, with the gate commands blanked and the merge lane off:
        Jaah's gate and merge lane run on the primary only (D6). A row carrying no list (an older control plane
        or cache writer) cannot say what a non-primary checkout is: None, fail closed."""
        if repo_id is None:
            return self
        if not self.repos:
            return None
        repo = next((r for r in self.repos if r.id == repo_id), None)
        if repo is None:
            return None
        if repo.primary:
            return self
        return replace(self, repo_slug=repo.repo_slug, integration_branch=repo.integration_branch,
                       gate_quick_cmd="", gate_full_cmd="", merge_lane=False, primary=False)

    @property
    def restricted(self) -> bool:
        """True when this project restricts what we may push/PR at all. One definition for the hook,
        the installer and the checker."""
        return bool(self.branch_prefix) or not self.push_enabled

    def allows_ref(self, ref_or_branch: str) -> bool:
        """True when a destination ref (or bare branch name) may be pushed. `refs/heads/` is stripped;
        a non-branch ref (`refs/tags/...`) can never match a branch prefix."""
        if not self.push_enabled:
            return False
        branch = ref_or_branch[len("refs/heads/"):] if ref_or_branch.startswith("refs/heads/") else ref_or_branch
        return branch.startswith(self.branch_prefix)

    def allows_pr_from(self, head_branch: str) -> bool:
        """True when a PR may be opened/merged from `head_branch`.

        Both conjuncts of `allows_ref`: `push_enabled=false` with an empty prefix means "not ours to
        write", and `gh pr merge` needs no push, so the prefix alone would grant PRs there."""
        if not self.push_enabled:
            return False
        return head_branch.startswith(self.branch_prefix)

    # ── The rules as a session is TOLD them ────────────────────────────────────────
    # A dispatched runner gets them in its prompt (apps/api/app/dispatch_compose/task_prompt.py's
    # `_start_here_block`); every other session learnt them only from refusals. So they are stated up
    # front (context_router.py's `--session` hook) and restated by every refusal
    # (check_gh_pr_guard.py), both from here so they cannot drift; vocabulary matches
    # `_start_here_block`. The git-native guard
    # cannot import this and restates the recipe itself: the one
    # deployment-boundary exception.

    def branch_example(self, name: str = "<name>") -> str:
        """A branch name that is legal here. The example, not the rule; the rule is `allows_ref`."""
        return f"{self.branch_prefix}{name}"

    def delivery_recipe(self) -> str:
        """ONE LINE that lands work here, in this project's own values — what every refusal appends.
        The prefix is in the ref and the integration branch in `--base`, so no rule list is needed."""
        return delivery_recipe(self.branch_prefix, self.integration_branch, self.push_enabled)

    def rules_brief(self) -> str:
        """The whole contract in THREE dense lines, for the SessionStart injector: the four values
        `_start_here_block` gives a runner, plus the push flag."""
        push = "yes" if self.push_enabled else "NO (never)"
        return (
            f"  branch: {self.branch_example()} · base: {self.integration_branch} · "
            f"push: {push}\n"
            f"  {self.gate_directive()}\n"
            f"  ship: {self.delivery_recipe()}"
        )

    def gate_directive(self) -> str:
        """The gate line alone — also what run_gate.py prints when a gate FAILS. A blank command is
        a tier the project declares no gate for."""
        if not self.primary:
            return "gate: none (not the primary repository: Jaah's gate and merge lane run there only)"
        if not self.gate_quick_cmd and not self.gate_full_cmd:
            return "gate: none (this project declares no gate)"
        quick = self.gate_quick_cmd or "none"
        full = self.gate_full_cmd or "none"
        if self.merge_lane and self.gate_full_cmd:
            # The lane, not the session, runs the full tier on the PR.
            return f"gate: {quick} (loop) · merge lane runs {full}"
        return f"gate: {quick} (loop) · {full} (before PR)"


def delivery_recipe(branch_prefix: str, integration_branch: str, push_enabled: bool = True,
                    environ: "dict[str, str] | None" = None) -> str:
    """`ProjectConfig.delivery_recipe`'s line from bare values, so the byte-copied git-native guard
    (apps/jaahd/guards/access-guard-pre-push.py) prints the same line without a `ProjectConfig`.

    A fleet runner (`CONTROL_TASK_ID` set) opens its PR with `jaah-forge`, the one forge CLI it may use
    (check_gh_pr_guard.py denies `gh` there); every other session keeps `gh`."""
    if not push_enabled:
        return "no push route here (push_enabled=false) — hand over a patch instead"
    env = os.environ if environ is None else environ
    head = f"{branch_prefix}<name>"
    push = f"git push origin HEAD:refs/heads/{head}"
    if (env.get("CONTROL_TASK_ID") or "").strip():
        return (f"{push} && jaah-forge pr create --base {integration_branch} --head {head} "
                "--title <title> --body-file <file>")
    return f"{push} && gh pr create --base {integration_branch} --head {head}"


def normalize_repo_slug(s: str) -> str:
    """`git@github.com:owner/repo.git`, `https://github.com/owner/repo`, `owner/repo` -> `owner/repo`.

    THE single home for this normalization (check_gh_pr_guard.py aliases it): two normalizers
    that disagree once made a guarded repo read as unguarded."""
    s = s.strip()
    s = re.sub(r"^git@[^:]+:", "", s)
    s = re.sub(r"^https?://[^/]+/", "", s)
    s = re.sub(r"^github\.com/", "", s, flags=re.IGNORECASE)
    s = re.sub(r"\.git$", "", s)
    return s.strip("/").lower()


def cache_path(key: str = "") -> Path:
    """Where the governance cache for `key` (a `project_key`) lives — one file per GUARDED PROJECT, shared
    by every copy of this resolver. An empty key is the whole-list caller's.

    The key is folded to a safe basename (`[a-z0-9._-]`, no leading dots, length-capped)."""
    if not key.strip():
        return CACHE_DIR / f"{WHOLE_LIST_CACHE_KEY}.json"
    safe = re.sub(r"[^a-z0-9._-]", "_", key.strip().lower()).lstrip(".")[:120]
    return CACHE_DIR / f"{safe or '_unnamed'}.json"


def _node_config_cp_base() -> str:
    """`JAAH_CP_BASE_URL` as this NODE's root-owned unit config declares it, or '' off a fleet node.

    The point is the SOURCE: the env var is settable by whoever pushes, `NODE_CONFIG_PATH` is
    root-owned, so comparing them is how `api_base_redirect_reason` tells "this node's control plane"
    (persistable) from "pointed elsewhere" (not). Never raises."""
    try:
        text = NODE_CONFIG_PATH.read_text(encoding="utf-8")
    except OSError:
        return ""
    found = ""
    for line in text.splitlines():
        if line.startswith("JAAH_CP_BASE_URL="):
            found = line.split("=", 1)[1].strip().strip("\"'")
    return found.rstrip("/")


def _effective_env_path() -> Path:
    """The `.env` `fetch_projects` will read: the `JAAH_ENV_PATH` override, else the file beside
    WHICHEVER COPY of this resolver is running (`RESOLVER_ENV_PATH`)."""
    return ENV_PATH if ENV_PATH is not None else RESOLVER_ENV_PATH


def _base_comes_from_this_copys_own_env_file() -> bool:
    """True when the console endpoint came from `API_PORT` in the `.env` beside this COPY — the one
    redirect `env -u` cannot remove (for the in-tree copy it is the guarded repo's own `.env`), so
    `governance_map` also refuses to let it speak for any other project. False when `JAAH_ENV_PATH` named
    the file: that is an env redirect the hooks already scrub."""
    return ENV_PATH is None and api_base_source(RESOLVER_ENV_PATH)[0] == API_BASE_FROM_ENV_FILE


def api_base_redirect_reason() -> str:
    """'' when the console about to be asked is named by something OUTSIDE the guarded repo, else one
    line naming what moved it. Non-empty means a live answer must NOT be written to the cache.

    Invariant: every `api_base` source is corroborated by a file the repo cannot write
    (`NODE_CONFIG_PATH`), hardcoded (the port grid's default), or listed here as a redirect
    (`_API_REDIRECT_VARS`, `_NODE_BASE_VARS`, `API_PORT` in this copy's `.env`). `api_base_source` is
    asked rather than re-deriving the order. A redirect stays a live affordance but must leave no
    durable state: the cache is shared by every copy and read by later processes that set nothing.

    The price: a `.env`-endpoint checkout (every jaah worktree) never primes the cache, so with its stack
    down `run_gate.py` refuses (exit 78) and the push hook uses its floor, and
    check_gh_pr_guard.py makes one live call per Bash command (~25-39 ms loopback). Availability
    and latency, never enforcement; `./dc up -d` fixes the first."""
    named = [n for n in _API_REDIRECT_VARS if (os.environ.get(n) or "").strip()]
    # Redirects only when they disagree with the node's root-owned config (see `_NODE_BASE_VARS`).
    node_base = _node_config_cp_base()
    named += [
        n for n in _NODE_BASE_VARS
        if (os.environ.get(n) or "").strip().rstrip("/") not in ("", node_base)
    ]
    if named:
        return f"the environment moved it ({', '.join(named)})"
    env_path = _effective_env_path()
    source, base = api_base_source(env_path)
    # Same test as the node vars: does it agree with the root-owned pointer (normally it does not).
    if source == API_BASE_FROM_ENV_FILE and base.rstrip("/") != node_base:
        return f"API_PORT in {env_path} names it ({base})"
    return ""


def hermetic_git_env() -> dict[str, str]:
    """The process env with every repo-targeting `GIT_*` variable stripped — the ONE home for the scrub.
    Public: run_gate, the PreToolUse guards and the tools' own git calls use it."""
    return {k: v for k, v in os.environ.items()
            if not k.startswith("GIT_") or k.startswith(_GIT_ENV_KEEP_PREFIXES)}


def tracked_files(root: Path, *, under: str | None = None, fallback_walk: bool = False,
                  untracked: bool = False) -> list[str] | None:
    """The files git tracks under `root` — sorted POSIX names relative to `root` — the ONE home for the
    query for every tools script that walks the tree. `untracked` adds the untracked-not-ignored files
    (`--others --exclude-standard`): the working tree the next commit will contain, as a gate wants.

    `under` narrows to one subdirectory (a git pathspec, relative to `root`); `root` may then sit below
    the work tree's top level, as a docs tree does. Without `under`, `root` must BE the top level. Names
    are decoded with surrogateescape, so a non-UTF-8 name round-trips to its bytes via
    `.encode("utf-8", "surrogateescape")`. Returns None when git answers for no such tree (not a repo,
    git missing, timeout) — or, with `fallback_walk`, the filesystem walk of `root` instead.

    Both guards are load-bearing: git exports `GIT_DIR`/`GIT_INDEX_FILE` into hook children, so under
    pre-commit `git -C <tmpdir> ls-files` answered with THIS repo's index and a selftest read clean.
    So: scrub `GIT_*` (`hermetic_git_env`), and confirm `root` is the top level git names (or, with
    `under`, lies inside it) before trusting its answer."""
    env = hermetic_git_env()
    names: list[str] | None = None
    try:
        top = subprocess.run(["git", "-C", str(root), "rev-parse", "--show-toplevel"],
                             capture_output=True, text=True, check=True, env=env, timeout=30).stdout.strip()
        top_path, here = Path(top).resolve(), root.resolve()
        if top and (here == top_path if under is None else here.is_relative_to(top_path)):
            out = subprocess.run(["git", "-C", str(root), "ls-files", "-z",
                                  *(["--cached", "--others", "--exclude-standard"] if untracked else []),
                                  *(["--", under] if under else [])],
                                 capture_output=True, check=True, env=env, timeout=30).stdout
            names = sorted(n.decode("utf-8", "surrogateescape") for n in out.split(b"\0") if n)
    except (OSError, subprocess.SubprocessError):
        pass
    if names is None and fallback_walk:
        return sorted(n for n in (p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file())
                      if not (n == ".git" or n.startswith(".git/")))
    return names


def project_key(project_id: int) -> str:
    """The governance-map key for a project BY ID (also its cache file's name, `cache_path`)."""
    return f"#{int(project_id)}"


def _row_project_id(row: dict[str, Any]) -> int | None:
    value = row.get("id")
    # `bool` is an `int`; a JSON `true` must never read as project 1.
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None


def checkout_project_id(repo: str = ".") -> int | None:
    """The project whose pool clone `repo` belongs to, or None (`checkout_repo`'s first half)."""
    return checkout_repo(repo)[0]


def checkout_repo(repo: str = ".") -> tuple[int | None, int | None]:
    """`(project id, repository id)` of the pool checkout `repo` is in; `(None, None)` when it is no pool
    checkout, and a None repository id for the PRIMARY (every single-repository project's checkouts).

    THE LAYOUT ANSWERS: a slot `<JAAH_HOME>/repos/<id>/<N>` and the anchor `main` are always the
    primary's; `<JAAH_HOME>/repos/<id>/_repos/<rid>/<N|main>` is repository `<rid>`'s. Never a clone's
    `jaah.repoId` stamp: that file is the session's to rewrite, and a primary that could call itself another
    repository would shed its gate. The checkout's git COMMON dir must also be one of that project's clones —
    `<JAAH_HOME>/repos/<id>/main/.git` or `.../_repos/<x>/main/.git` — so a tree planted under the layout
    borrows nothing. Anchored on the home (which the enforcing hooks re-supply from a root-owned file);
    resolved with `realpath` so a symlink cannot move either answer."""
    try:
        result = subprocess.run(
            ["git", "-C", repo or ".", "rev-parse", "--path-format=absolute", "--show-toplevel",
             "--git-common-dir"],
            capture_output=True, text=True, timeout=5, env=hermetic_git_env(), check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None, None
    lines = result.stdout.strip().splitlines()
    if result.returncode != 0 or len(lines) != 2:
        # A bare common dir with no work tree prints one line: no checkout to govern.
        return None, None
    top, common = (_slashed(os.path.realpath(line.strip())) for line in lines)
    home = re.escape(_slashed(os.path.realpath(str(jaah_paths.jaah_root()))))
    # Where the CALLER is, not where git says the work tree is: a per-worktree `core.worktree` can point git's
    # answer at another repository's checkout. The two must agree, or the checkout governs nothing.
    here = _slashed(os.path.realpath(repo or "."))
    where = re.match(home + r"/repos/([0-9]+)/(?:_repos/([0-9]+)/)?(?:[0-9]+|main)(?=/|$)", here)
    clone = re.fullmatch(home + r"/repos/([0-9]+)/(?:_repos/[0-9]+/)?main/\.git", common)
    if not where or not clone or where.group(0) != top or where.group(1) != clone.group(1):
        return None, None
    return int(where.group(1)), int(where.group(2)) if where.group(2) else None


def _slashed(path: str, pathmod: Any = os.path) -> str:
    """`path` as the pattern above spells it: unchanged on posix; on Windows case-folded with `/`
    separators, since `realpath` answers there with `\\` and the pattern's `/` never matched — every
    Windows runner's checkout read as "not a Jaah project" (seen on a real Windows PC)."""
    if pathmod.sep != "\\":
        return path
    return pathmod.normcase(path).replace("\\", "/")


def config_for(
    gov: dict[str, ProjectConfig | None], repo: str = ".", project_id: int | None = None,
    repo_id: int | None = None,
) -> tuple[str | None, ProjectConfig | None]:
    """`(key, config)` governing the checkout at `repo` — its pool clone's project (`checkout_repo`, or
    `project_id`/`repo_id` when the caller already has them), seen for that checkout's repository
    (`ProjectConfig.for_repo`). `key` None when the checkout is not a pool clone or no row has that id
    (not ours); config None when the row is unreadable or does not list that repository."""
    if project_id is None:
        project_id, repo_id = checkout_repo(repo)
    if project_id is None:
        return None, None
    key = project_key(project_id)
    if key not in gov:
        return None, None
    config = gov[key]
    return key, (config.for_repo(repo_id) if config is not None else None)


def _repos_from_row(row: dict[str, Any]) -> tuple[RepoConfig, ...]:
    """The row's `repos[]` (`ProjectOut.repos`) as `RepoConfig`s; a row that does not parse is dropped
    alone, and an absent list (an older control plane) is `()`."""
    raw = row.get("repos")
    if not isinstance(raw, list):
        return ()
    out: list[RepoConfig] = []
    for entry in raw:
        if not isinstance(entry, dict):
            continue
        rid, branch = entry.get("id"), entry.get("integration_branch")
        slug = normalize_repo_slug(str(entry.get("repo_url") or ""))
        if (not isinstance(rid, int) or isinstance(rid, bool) or not slug or not isinstance(branch, str)
                or not branch.strip()):
            continue
        out.append(RepoConfig(id=rid, repo_slug=slug, integration_branch=branch.strip(),
                              primary=entry.get("is_primary") is True))
    return tuple(out)


def _bool_field(value: Any) -> bool | None:
    """A JSON boolean column, or None when unreadable as one. Strings are accepted; anything else is a
    refusal, never a truthiness guess (`bool("false")` is True and would un-guard a restricted repo)."""
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        low = value.strip().lower()
        if low in ("true", "1", "yes"):
            return True
        if low in ("false", "0", "no"):
            return False
    return None


def _lenient_merge_lane(row: dict[str, Any]) -> bool:
    """The row's `merge_lane`: True only when it reads as one. Absent or unreadable is False ("the lane is
    off"), never UNRESOLVED — the field is newer than the governance columns and gates nothing a session must
    be stopped for; its only consumer denies a self-merge it replaces (check_gh_pr_guard.py)."""
    return _bool_field(row.get("merge_lane")) is True


def _config_from_row(row: dict[str, Any]) -> ProjectConfig | None:
    """One `GET /api/projects` row -> its `ProjectConfig`, or None when the row does not carry all five
    mandatory values. None means UNRESOLVED, never "unrestricted": absence is not a state."""
    slug = normalize_repo_slug(str(row.get("repo_url") or ""))
    if not slug:
        return None
    values: dict[str, Any] = {}
    for field in TEXT_FIELDS:
        raw = row.get(field)
        if not isinstance(raw, str):
            return None                     # missing key or a NULL in a NOT NULL column
        values[field] = raw.strip()
    # A BLANK `integration_branch` IS UNRESOLVED: the `pre-push` hook blocks
    # `main master $INTEGRATION_BRANCH`, so an empty value would leave a governed repo blocking LESS than
    # the universal floor. Defense in depth (the API 422s a blank one). `branch_prefix` `''` is legal, and
    # so is a blank gate command: the project declares no gate for that tier (`run_gate.py` skips it).
    if not values["integration_branch"]:
        return None
    for field in BOOL_FIELDS:
        parsed = _bool_field(row.get(field))
        if parsed is None:
            return None
        values[field] = parsed
    # Canonicalize a non-empty prefix to end in `/` (the API normalizes on write; this covers other paths).
    prefix = values["branch_prefix"]
    if prefix and not prefix.endswith("/"):
        prefix += "/"
    return ProjectConfig(
        name=str(row.get("name") or ""),
        repo_slug=slug,
        project_id=_row_project_id(row),
        integration_branch=values["integration_branch"],
        branch_prefix=prefix,
        gate_quick_cmd=values["gate_quick_cmd"],
        gate_full_cmd=values["gate_full_cmd"],
        push_enabled=values["push_enabled"],
        merge_lane=_lenient_merge_lane(row),
        repos=_repos_from_row(row),
    )


def _map_from_projects(projects: list[dict[str, Any]]) -> dict[str, ProjectConfig | None]:
    """{`project_key(id)`: config-or-None} for every project row carrying an id. An id is unique, so rows
    sharing a repo are simply separate entries.

    check_gh_pr_guard.py and onboard_repo.py branch on `is None`: None ("ours, unreadable" →
    block) differs from ABSENT ("not ours" → no-op)."""
    out: dict[str, ProjectConfig | None] = {}
    for row in projects:
        pid = _row_project_id(row)
        if pid is None:
            continue
        out[project_key(pid)] = _config_from_row(row)
    return out


def _cache_age_seconds(path: Path) -> float | None:
    try:
        return time.time() - path.stat().st_mtime
    except OSError:
        return None


def _load_cache(path: Path) -> dict[str, ProjectConfig | None] | None:
    """The cached map, or None when there is no readable cache (a corrupt file counts as absent)."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict):
        return None
    rows = raw.get("projects")
    if not isinstance(rows, list):
        return None
    return _map_from_projects([r for r in rows if isinstance(r, dict)])


def _save_cache(projects: list[dict[str, Any]], path: Path) -> None:
    """Persist the RAW rows (the five columns, identity keys and the lenient `merge_lane`), not the parsed map, so an unparseable
    entry reads back as UNRESOLVED rather than an invented value.

    HARDEN FIRST, THEN WRITE: this file is an AUTHORIZATION INPUT (push permission, and gate commands a
    shell runs). `~/.jaah/cache` can be group-writable, so a planted symlink must not be followed:
    open the directory with `O_NOFOLLOW|O_DIRECTORY`, tighten it through that FD, and create the temp
    file relative to it with `O_EXCL|O_NOFOLLOW` (no check-then-write window). Hardening failures abort
    the write: a lost write costs a refetch, writing where we cannot prove control costs the guard.
    """
    keep = ("id", "name", "repo_url", *TEXT_FIELDS, *BOOL_FIELDS, "merge_lane", "repos")
    trimmed = [
        {k: row.get(k) for k in keep if k in row}
        for row in projects
        if isinstance(row, dict) and row.get("repo_url")
    ]
    parent = path.parent
    try:
        parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    except OSError as exc:
        print(f"project_config: cannot create the cache directory {parent} ({exc}) — not caching",
              file=sys.stderr)
        return
    try:
        dir_fd = os.open(str(parent), os.O_RDONLY | _O_DIRECTORY | _O_NOFOLLOW)
    except OSError as exc:
        print(
            f"project_config: refusing to write the governance cache — {parent} is not a directory we "
            f"can open safely ({exc}). A symlink here would let whoever planted it choose this repo's "
            f"push permission and gate command; delete or fix that path.",
            file=sys.stderr,
        )
        return
    # Unique per process so concurrent O_EXCL creates cannot collide; `os.replace` is the atomic swap.
    tmp_name = f".{path.name}.{os.getpid()}.tmp"
    try:
        os.fchmod(dir_fd, 0o700)
        fd = os.open(
            tmp_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _O_NOFOLLOW, 0o600, dir_fd=dir_fd
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump({"projects": trimmed}, handle)
            os.replace(tmp_name, path.name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
        except OSError:
            os.unlink(tmp_name, dir_fd=dir_fd)
            raise
    except OSError as exc:
        print(f"project_config: could not write the governance cache {path} ({exc}) — the next call "
              f"will refetch", file=sys.stderr)
    finally:
        os.close(dir_fd)


def governance_map(
    projects: list[dict[str, Any]] | None = None,
    *,
    prefer_cache: bool = False,
    project_id: int | None = None,
) -> dict[str, ProjectConfig | None] | None:
    """The whole {`project_key`: config-or-None} map, or **None when no project list could be obtained at
    all** (live API unreachable AND no cache on disk) — the tier-3 signal.

    `projects` injects a fixture list, bypassing cache and API (the selftests' seam).

    LIVE-FIRST; the cache is a fallback, not a fast path: every field is revocable, so a cache consulted
    first would keep granting a just-revoked permission for the TTL. A stale cache answers only when the
    live call fails (with a warning).

    `prefer_cache=True` is the fast path for check_gh_pr_guard.py's per-Bash-command hook only
    (a good-faith early refusal, never the enforcement boundary). Security-decisive callers leave it off.

    `project_id` picks the cache file (`cache_path`) and is what a `.env`-named endpoint may speak for;
    omitted only by the whole-list caller."""
    if projects is not None:
        return _map_from_projects(projects)
    path = cache_path(project_key(project_id) if project_id is not None else "")
    age = _cache_age_seconds(path)
    if prefer_cache and age is not None and age < CACHE_TTL_SECONDS:
        cached = _load_cache(path)
        if cached is not None:
            return cached

    redirect = api_base_redirect_reason()
    # An endpoint named by a file inside a checkout may speak for that checkout's project and nothing else
    # (the `.env` lever no hook can scrub). The live leg is skipped: the cache answers (it is only ever
    # written from a corroborated endpoint), else None → `NoProjectData` → the floor. Not a hard block: we
    # do not know the checkout is governed.
    if project_id is not None and _base_comes_from_this_copys_own_env_file():
        own = checkout_project_id(str(RESOLVER_ENV_PATH.parent))
        if project_id != own:
            print(
                f"project_config: the control-plane endpoint is named by {RESOLVER_ENV_PATH}, which belongs to "
                f"{f'project {own}' if own is not None else 'no Jaah project'} — refusing to let it answer "
                f"for project {project_id}. Falling back to the on-disk cache, and to no data at all when "
                f"there is none.",
                file=sys.stderr,
            )
            return _load_cache(path)

    kwargs: dict[str, Any] = {"timeout": API_TIMEOUT_SECONDS}
    if ENV_PATH is not None:
        kwargs["env_path"] = ENV_PATH
    try:
        live = fetch_projects(**kwargs)
    except ControlPlaneApiError as exc:
        stale = _load_cache(path)
        if stale is not None:
            print(
                f"project_config: control-plane API unreachable ({exc}) — using the cached project list "
                f"(age={age if age is not None else 'unknown'}s)", file=sys.stderr,
            )
            return stale
        print(
            f"project_config: control-plane API unreachable ({exc}) and no cached project list exists",
            file=sys.stderr,
        )
        return None

    # A redirected answer is answer-only: used for this call, never cached (see
    # `api_base_redirect_reason`).
    if redirect:
        # One line: the PreToolUse hook may reach this on every Bash call.
        print(
            f"project_config: this control plane is not one this host's own configuration names ({redirect}) — "
            f"answering from it, not caching it (see `api_base_redirect_reason`).",
            file=sys.stderr,
        )
    else:
        _save_cache(live, path)
    return _map_from_projects(live)


def resolve(repo: str = ".", projects: list[dict[str, Any]] | None = None) -> ProjectConfig:
    """This checkout's governance values, or one of the refusals the module docstring enumerates:
    `UnknownProject`, `UnresolvedProject`, `NoProjectData`."""
    pid, repo_id = checkout_repo(repo)
    if pid is None:
        # Not fail-closed: a checkout outside the pool is not a Jaah project.
        raise UnknownProject(
            f"{repo or '.'} is not a pool checkout (<JAAH_HOME>/repos/<project id>/) — not a Jaah "
            f"project, nothing to gate"
        )
    gov = governance_map(projects, project_id=pid)
    if gov is None:
        # Named because the remedy is "prime that file", the same whichever copy runs next.
        raise NoProjectData(
            f"cannot tell whether project {pid} is a Jaah project: no control plane answered for it — the "
            f"API was unreachable, or the only endpoint on offer was one this checkout's own `.env` named and "
            f"so may not speak for another project (whichever it was is on stderr above) — and no cached "
            f"project list exists ({cache_path(project_key(pid))}). On a jaah host, start the stack "
            f"(./dc up -d). Priming that file by hand no longer works from a checkout whose endpoint comes "
            f"from its own `.env`: such an answer is used live and never written down (see "
            f"`api_base_redirect_reason`)."
        )
    key = project_key(pid)
    if key not in gov:
        raise UnknownProject(f"project {pid} (from {repo or '.'}) is not in the project list — nothing to gate")
    config = gov[key]
    if config is not None and config.for_repo(repo_id) is None:
        raise UnresolvedProject(
            f"{repo or '.'} is a checkout of repository {repo_id}, which project {pid} ('{config.name}') no "
            f"longer connects — nothing may be pushed or merged from it. Its work is kept on disk; move what "
            f"you need into a repository the project lists."
        )
    config = config.for_repo(repo_id) if config is not None else None
    if config is None:
        raise UnresolvedProject(
            f"project {pid} IS a Jaah project but its row does not carry the five mandatory governance "
            f"values ({', '.join(GOVERNANCE_FIELDS)}) — refusing to proceed. Fix the "
            f"project's row in the console (/projects), then retry."
        )
    return config


# ── CLI ─────────────────────────────────────────────────────────────────────────
# Shell assignments rather than JSON: bash hooks `eval` shlex-quoted assignments with no jq step.
# Python callers import `resolve()`.

def _shell_assignments(config: ProjectConfig, push_repo_slug: str = "") -> str:
    # `PROJECT_RULES_BRIEF` carries the one wording of the rules so a bash hook can print it without
    # restating it (newlines survive inside the single-quoted string).
    brief = config.rules_brief()
    lines = [
        f"PROJECT_NAME={shlex.quote(config.name)}",
        f"PROJECT_REPO_SLUG={shlex.quote(config.repo_slug)}",
        # The repo the push writes to (`--remote-url`, normalized), '' when not given: the hook refuses a push
        # whose repo is not this checkout's own — a pool slot never lends its rules to another repo.
        f"PUSH_REPO_SLUG={shlex.quote(push_repo_slug)}",
        f"INTEGRATION_BRANCH={shlex.quote(config.integration_branch)}",
        f"BRANCH_PREFIX={shlex.quote(config.branch_prefix)}",
        f"GATE_QUICK_CMD={shlex.quote(config.gate_quick_cmd)}",
        f"GATE_FULL_CMD={shlex.quote(config.gate_full_cmd)}",
        f"PUSH_ENABLED={'1' if config.push_enabled else '0'}",
        f"PROJECT_RULES_BRIEF={shlex.quote(brief)}",
    ]
    return "\n".join(lines)


def main(argv: list[str]) -> int:
    repo = "."
    remote_url = ""
    i = 0
    while i < len(argv):
        arg = argv[i]
        if arg == "--remote-url" and i + 1 < len(argv):
            remote_url = argv[i + 1]  # the push destination, never the identity: see PUSH_REPO_SLUG
            i += 2
        elif arg == "--repo" and i + 1 < len(argv):
            repo = argv[i + 1]
            i += 2
        elif arg == "--show":
            i += 1
        else:
            print(
                "usage: project_config.py [--remote-url URL] [--repo DIR]",
                file=sys.stderr,
            )
            return EXIT_USAGE
    try:
        config = resolve(repo=repo)
    except UnknownProject as exc:
        print(f"project_config: {exc}", file=sys.stderr)
        return EXIT_UNKNOWN_PROJECT
    except NoProjectData as exc:
        # Before the `UnresolvedProject` arm it subclasses (first matching `except` wins).
        print(f"project_config: {exc}", file=sys.stderr)
        return EXIT_NO_PROJECT_DATA
    except UnresolvedProject as exc:
        print(f"❌ project_config: {exc}", file=sys.stderr)
        return EXIT_FAIL_CLOSED
    print(_shell_assignments(config, normalize_repo_slug(remote_url) if remote_url.strip() else ""))
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
