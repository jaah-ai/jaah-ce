"""A repository slot's tree: a standalone `git clone` straight from the forge, one function per concern.

Every repository-backed slot is its own clone (its own `.git` directory) of the project's credential-free
`clone_url`: no node mirror, no shared clone, no `git worktree`. A slot is cloned at its first lease
(`clone`) and, when the tree survives its hold (a static slot), reset to the integration branch at the next
lease (`refresh`). A resume touches neither. Policy and paths: the worktree-pool policy, §Layout.

EMPTY FORGE. A repository with no branch at all (a new one) is cloned empty, the integration branch and the hold's
branch UNBORN (`_unborn`); nothing is created on the forge. The first push from it is the genesis push the access
guard lets through (`guards/access-guard-pre-push.py::is_genesis`).

CREDENTIAL. No git the session user runs ever talks to the forge. Each network op (clone, fetch, push) runs in
the daemon-owned mirror of the URL as the daemon uid, acting for the leasing task (`git_mirror.fetch_for`,
`shallow_for`, `push_for`), so the node's credential helper is served the ACTING task's login through the CP's
task arm (`cred_broker`) — never a slot declarer's, and never to a process on the session user's tmux channel.
The slot is then filled by a LOCAL, credential-free fetch from that mirror (`git_mirror.local_fetch_args`), as
the session user. Nothing credential-bearing is written: `origin` is the URL as the CP serves it. A static
slot's `.git/config` planted with a key that would steer a credentialed connection still fails its refresh as
`auth` (`refuse_steering`).

FAILURE. A network failure raises `CloneFailed(host, cause)`, cause one of `CAUSES`, worded as one sentence
(`str()`), never git's stderr. A clone lands in `<dest>.cloning` and is renamed into place, so a failed clone
leaves no tree. A local step that fails raises `core.ResetRefused`."""
from __future__ import annotations

import logging
import os
import re
import stat
import sys
import urllib.parse

import clone_identity
import env_helpers
import git_credential_helper
import git_env
import git_mirror
import session_user
from clone_identity import Acting
from worktree_mechanics import core

log = logging.getLogger("jaahd.slot_clone")

CAUSE_AUTH = "auth"
CAUSE_NOT_FOUND = "not_found"
CAUSE_NETWORK = "network"
CAUSE_TIMEOUT = "timeout"
# The node's own `credential.helper` is not on disk: its install is broken, not the repository or the login, so a
# retry after the install is re-run can succeed — it spends no attempt (not in `REFUSED_CAUSES`).
CAUSE_NODE_BROKEN = "node_broken"
CAUSES = frozenset({CAUSE_AUTH, CAUSE_NOT_FOUND, CAUSE_NETWORK, CAUSE_TIMEOUT, CAUSE_NODE_BROKEN})
# The causes a retry will not change: the attempt fails (`clone_refused`), it is not re-served.
REFUSED_CAUSES = frozenset({CAUSE_AUTH, CAUSE_NOT_FOUND})

_CAUSE_WORDS = {
    CAUSE_AUTH: "authentication was refused",
    CAUSE_NOT_FOUND: "the repository was not found",
    CAUSE_NETWORK: "the forge could not be reached",
    CAUSE_TIMEOUT: "the forge did not answer in time",
    CAUSE_NODE_BROKEN: "the node's git credential helper is missing; re-run the node install",
}

# Git's text, across forges and the node's credential helper. Order matters: a missing helper is matched first (its
# shell's "not found" would read as `not_found`); 429 is a rate limit (network), matched before the auth codes;
# "not found" before auth because GitHub answers a login that cannot see a private repository with "Repository not
# found".
# The shell's words when git runs a `credential.helper` that is not on disk — dash's "<helper> get: 1: <helper>: not
# found", bash's "<helper> get: line 1: <helper>: No such file or directory" (the CP's `checkout_gate._HELPER_MISSING`).
_HELPER_MISSING = re.compile(r"\S{1,512} get: (?:line )?\d{1,6}: \S{1,512}: (?:not found|No such file or directory)")
_RATE_LIMITED = re.compile(r"\b429\b|too many requests|rate limit", re.IGNORECASE)
_NOT_FOUND = re.compile(r"not found|\b404\b|does not appear to be a git repository|does not exist", re.IGNORECASE)
_AUTH = re.compile(
    r"authentication failed|\b40[13]\b|could not read username|could not read password|terminal prompts disabled|"
    r"permission denied \(publickey|access denied|not authorized|jaah-git-credential|credential helper|refused:",
    re.IGNORECASE)
_TIMEOUT = re.compile(r"timed out|timeout", re.IGNORECASE)
# The node's credential helper's own refusal line (`git_credential_helper.main`): no login reached the forge.
_HELPER_REFUSED = "jaah-git-credential:"
# `clone --branch <b>` for a branch the forge does not have.
_MISSING_BRANCH = re.compile(r"remote branch \S+ not found", re.IGNORECASE)

# Refspecs a slot keeps: every branch as `origin/*`, and the tags.
_HEADS = "+refs/heads/*:refs/remotes/origin/*"
_TAGS = "+refs/tags/*:refs/tags/*"
STAGING_SUFFIX = ".cloning"


class CloneFailed(RuntimeError):
    """A network git op of a slot failed: `host` is the forge's, `cause` one of `CAUSES`."""

    def __init__(self, host: str, cause: str, *, helper_refused: bool = False) -> None:
        self.host = host or "the forge"
        self.cause = cause if cause in CAUSES else CAUSE_NETWORK
        # The node's own credential helper said no (its `jaah-git-credential:` line), not the forge: no login was
        # sent, so retrying with a fresh one changes nothing.
        self.helper_refused = helper_refused
        super().__init__(f"clone failed: {self.host}: {_CAUSE_WORDS[self.cause]}")

    @property
    def refused(self) -> bool:
        """Whether a retry would be refused the same way (`REFUSED_CAUSES`)."""
        return self.cause in REFUSED_CAUSES


def classify(rc: int, text: str) -> str:
    """The cause of a failed network git op from its exit code and stderr (`CAUSES`)."""
    if _HELPER_MISSING.search(text or ""):
        return CAUSE_NODE_BROKEN
    if rc == 124 or (_TIMEOUT.search(text or "") and not _AUTH.search(text or "")):
        return CAUSE_TIMEOUT
    if _RATE_LIMITED.search(text or ""):
        return CAUSE_NETWORK
    if _NOT_FOUND.search(text or ""):
        return CAUSE_NOT_FOUND
    if _AUTH.search(text or ""):
        return CAUSE_AUTH
    return CAUSE_NETWORK


def host_of(url: str) -> str:
    """The forge host a URL names (no userinfo, no port), or "" when it names none."""
    try:
        return (urllib.parse.urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""


def is_standalone(tree: str) -> bool:
    """A standalone clone: its `.git` is a real directory (not a symlink, not a linked worktree's pointer file)."""
    git = os.path.join(tree, ".git")
    return os.path.isdir(git) and not os.path.islink(git)


def is_linked_worktree(tree: str) -> bool:
    """A tree from the retired worktree layout: `.git` is a FILE pointing at another repository."""
    return os.path.isfile(os.path.join(tree, ".git"))


def _failed(acting: Acting, url: str, what: str, rc: int, err: str) -> CloneFailed:
    cause = classify(rc, err)
    log.warning("slot-clone: %s for %s failed (rc %s, %s): %s", what, acting.label, rc, cause,
                git_env.redact_userinfo(err or "").strip()[-300:])
    return CloneFailed(host_of(url), cause, helper_refused=_HELPER_REFUSED in (err or ""))


def _forge(acting: Acting, url: str, what: str, op):
    """`op()` — one `git_mirror` forge op for `acting`, run by the daemon in its own mirror — with its failure as
    `CloneFailed`: another op holding the mirror past the budget is `network` (retryable)."""
    try:
        return op()
    except clone_identity.ProjectBusy as exc:
        raise _failed(acting, url, what, 1, f"busy: {exc}") from None
    except git_mirror.MirrorError as exc:
        raise _failed(acting, url, what, 124 if "timed out" in str(exc) else 128, str(exc)) from None


def _mirror(acting: Acting, url: str, *forge_refs: str, timeout: float | None = None, head: bool = False):
    """`url`'s mirror, fetched now with `acting`'s access (`git_mirror.fetch_for`); `forge_refs` are refs beyond
    the branches and tags to bring into it under their own names. Raises `CloneFailed`."""
    budget = timeout or core._clone_timeout()

    def fetch():
        return git_mirror.fetch_for(acting, url, *(f"+{ref}:{ref}" for ref in forge_refs), timeout=budget, head=head)

    try:
        return _forge(acting, url, "fetch", fetch)
    except CloneFailed as exc:
        if exc.cause != CAUSE_AUTH or exc.helper_refused:
            raise
    # A refused login is retried ONCE. git's `erase` on that refusal made the broker drop its cached login and read the
    # control plane's current one (`git_credential_helper`): GitHub revokes a token the moment the control plane
    # refreshes it, so this node may have served a token another node's refresh killed — the node's staleness, not
    # the person's login. A second refusal is the login's.
    log.info("slot-clone: retrying %s's fetch once with a fresh login after the forge refused the cached one",
             acting.label)
    return _forge(acting, url, "fetch", fetch)


def _fill(tree: str, url: str, acting: Acting, mirror, *refspecs: str, depth: int | None = None,
          prune: bool = False, timeout: float | None = None) -> None:
    """Fetch `refspecs` into `tree` from the daemon's `mirror`: local, credential-free, as the session user
    (`git_mirror.local_fetch_args`). A ref the mirror lacks fails as git says (`couldn't find remote ref` reads
    `network`). Raises `CloneFailed`, or `ResetRefused` for a mirror path git cannot be handed."""
    try:
        args = git_mirror.local_fetch_args(mirror, *refspecs, depth=depth, prune=prune)
    except ValueError as exc:
        raise core.ResetRefused(f"clone-unresolvable: {exc}") from None
    rc, _out, err = core._run_git("-C", tree, *args, timeout=timeout or core._clone_timeout(),
                                  env_extra=dict(git_mirror.LOCAL_FETCH_ENV))
    if rc != 0:
        raise _failed(acting, url, "local fetch from the mirror", rc, err)


def fetch(tree: str, url: str, acting: Acting, *refspecs: str, forge_refs: tuple[str, ...] = ()) -> None:
    """`git fetch <url> <refspecs>` into `tree` for `acting`, the forge reached only by the daemon: its mirror is
    fetched first (`forge_refs`: refs beyond the branch heads, e.g. `refs/pull/<n>/head`), then `refspecs` are
    fetched from it locally. A refspec's source names the forge's ref. Raises `CloneFailed`."""
    _fill(tree, url, acting, _mirror(acting, url, *forge_refs), *refspecs)


def _local(tree: str, *args: str) -> str:
    """One local git op in `tree`; its stdout. Raises `ResetRefused` naming the op."""
    rc, out, err = core._git(tree, *args)
    if rc != 0:
        raise core.ResetRefused(f"git {args[0]} failed in {tree}: {(err or '').strip()[-300:] or f'rc {rc}'}")
    return out


def _has_ref(tree: str, ref: str) -> bool:
    return core._git(tree, "rev-parse", "-q", "--verify", f"{ref}^{{commit}}")[0] == 0


def prepare(tree: str, url: str, base: str, branch: str, acting: Acting, *, push_missing_base: bool = False) -> None:
    """Make `tree` a standalone clone of `url` on the hold's start point (`land`) — the one tree policy of a lease:
    absent → `clone`; a linked worktree of the retired layout → erased, then cloned (never refreshed); a held-over
    standalone clone → `refresh`. A refresh the forge REFUSED (auth, not found, a planted steering key) erases the
    tree before raising, so the attempt's retry clones afresh rather than resuming into it; a refresh that failed
    locally (a corrupt index, an unborn HEAD) is erased and cloned again. Either way the harness's scratch is hidden
    from git (`hide_harness_scratch`). A directory that is no repository is
    refused, never cloned into nor removed. Raises `CloneFailed` or `ResetRefused`."""
    if is_linked_worktree(tree):
        log.warning("slot-clone: %s is a linked worktree of the retired layout — erasing it for a clone", tree)
        erase(tree)
    if os.path.lexists(tree) and not is_standalone(tree):
        raise core.ResetRefused(
            f"clone-unusable: {tree} exists but is not a git repository — refusing to clone into it or replace it")
    if os.path.lexists(tree):
        try:
            refresh(tree, url, base, branch, acting, push_missing_base=push_missing_base)
            hide_harness_scratch(tree)
            return
        except CloneFailed as exc:
            if exc.refused:
                erase(tree)
            raise
        except core.ResetRefused as exc:
            log.warning("slot-clone: refreshing %s failed locally (%s) — erasing it for a fresh clone", tree, exc)
            erase(tree)
    clone(tree, url, base, acting, push_missing_base=push_missing_base)
    land(tree, base, branch)
    hide_harness_scratch(tree)


# Directories the runner harness itself writes inside a slot tree (OMC's state and its `.omx/` twin). Never the
# customer's work, so git must not see them: the settle guard, reconcile's `wt_dirty` and `slot_repos.unsaved_work`
# all read `git status`, which honours the clone's own `info/exclude` — the repository's `.gitignore` is never
# touched. Excluded, they survive a refresh's `clean -ffd` as they already do in a repo that ignores them.
HARNESS_SCRATCH = (".omc/", ".omx/")


def hide_harness_scratch(tree: str) -> None:
    """Add each `HARNESS_SCRATCH` pattern missing from `tree`'s `.git/info/exclude` (kept lines survive).
    A failure only warns: the lease still stands, and the settle guard then names the folder."""
    path = os.path.join(tree, ".git", "info", "exclude")
    try:
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
    except FileNotFoundError:
        text = ""
    except OSError as exc:
        log.warning("slot-clone: could not read %s: %s", path, exc)
        return
    present = {line.strip() for line in text.splitlines()}
    missing = [pattern for pattern in HARNESS_SCRATCH if pattern not in present]
    if not missing:
        return
    if text and not text.endswith("\n"):
        text += "\n"
    err = (session_user.ensure_dir_as_session_user(os.path.dirname(path))
           or session_user.replace_file_as_session_user(path, text + "".join(f"{p}\n" for p in missing), mode=0o644))
    if err:
        log.warning("slot-clone: could not hide the harness's scratch in %s: %s", tree, err)


def _credential_free(url: str) -> None:
    """Refuse a URL carrying userinfo: it would land in `.git/config` (R10)."""
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        raise core.ResetRefused("clone-unresolvable: the repository URL is not a URL") from None
    if parts.username or parts.password:
        raise core.ResetRefused("clone-unresolvable: the repository URL carries a credential — refusing to write it")


def clone(dest: str, url: str, base: str, acting: Acting, *, depth: int | None = None,
          push_missing_base: bool = False, timeout: float | None = None) -> None:
    """Clone `url` into `dest` (absent), detached at `origin/<base>`. Lands in `<dest>.cloning` first and is
    renamed into place, so a failure leaves nothing at `dest`. Full history unless `depth` — a shallow clone takes
    `<base>` alone (`--depth` implies one branch, which would otherwise be the forge's DEFAULT branch), and a forge
    without it is `ResetRefused` (`core.missing_base_refusal`), never a fallback to another branch; no `--filter`;
    `timeout` bounds the network clone (default: the node's clone budget).
    Raises `CloneFailed` (network) or `ResetRefused` (local)."""
    if not url:
        raise core.ResetRefused(f"clone-unresolvable: no repository URL for {dest} — set the project's Repo URL")
    _credential_free(url)
    staging = dest.rstrip("/") + STAGING_SUFFIX
    err = session_user.ensure_dir_as_session_user(os.path.dirname(dest.rstrip("/")) or "/")
    if err:
        raise core.ResetRefused(f"clone-unresolvable: cannot create {os.path.dirname(dest)}: {err}")
    erase(staging)
    try:
        mirror = None
        if depth:
            _clone_shallow(staging, url, base, acting, depth, timeout)
        else:
            mirror = _mirror(acting, url, timeout=timeout, head=True)
            _init(staging, url, mirror, timeout)
            _fill(staging, url, acting, mirror, _HEADS, _TAGS, timeout=timeout)
            default = git_mirror.head_branch(mirror)
            if default and _has_ref(staging, f"refs/remotes/origin/{default}"):
                _local(staging, "symbolic-ref", "refs/remotes/origin/HEAD", f"refs/remotes/origin/{default}")
        # A shallow clone took `<base>` itself, so its forge has a branch; an empty forge leaves HEAD unborn.
        if mirror is None or not _ensure_base(staging, url, base, acting, push_missing_base, mirror):
            _local(staging, "checkout", "-q", "-f", "--detach", _start(staging, base, ""))
        else:
            _unborn(staging, base)
    except BaseException:
        erase(staging)
        raise
    move(staging, dest)
    log.info("slot-clone: cloned %s into %s for %s", host_of(url), dest, acting.label)


def _init(tree: str, url: str, mirror, timeout: float | None = None) -> None:
    """An empty repository at `tree` as `git clone` makes it: the mirror's object format, then `_fresh_config`."""
    rc, _out, err = core._run_git("init", "-q", f"--object-format={git_mirror.object_format(mirror)}", "--", tree,
                                  timeout=min(120.0, timeout or 120.0))
    if rc != 0:
        raise core.ResetRefused(f"git init failed in {tree}: {(err or '').strip()[-300:] or f'rc {rc}'}")
    err = session_user.replace_file_as_session_user(os.path.join(tree, ".git", "config"), _fresh_config(tree, url),
                                                    mode=0o644)
    if err:
        raise core.ResetRefused(f"cannot write {tree}/.git/config: {err}")


def _clone_shallow(staging: str, url: str, base: str, acting: Acting, depth: int, timeout: float | None) -> None:
    """`clone --depth <depth> --branch <base>` into `staging`: the daemon clones a throwaway shallow mirror
    (`git_mirror.shallow_for`) and the session user fetches from it. Raises `CloneFailed` or `ResetRefused`."""
    budget = timeout or core._clone_timeout()
    try:
        with git_mirror.shallow_for(acting, url, base, depth=depth, timeout=budget) as mirror:
            _init(staging, url, mirror, budget)
            _fill(staging, url, acting, mirror, _HEADS, depth=depth, timeout=budget)
    except git_mirror.MirrorError as exc:
        if _MISSING_BRANCH.search(str(exc)):
            raise core.ResetRefused(core.missing_base_refusal(base)) from None
        raise _failed(acting, url, "shallow clone", 124 if "timed out" in str(exc) else 128, str(exc)) from None


def refresh(tree: str, url: str, base: str, branch: str, acting: Acting, *, push_missing_base: bool = False) -> None:
    """Bring a held-over standalone clone to `origin/<base>` for a new hold: refuse a planted steering key
    (`refuse_steering`), `scrub` what its last runner could plant in `.git`, fetch from `url` (never the tree's own
    `origin`), `reset --hard`, `clean -ffd` (no `-x`: ignored caches such as `node_modules` and `.venv` survive),
    land `branch` (`land`), and delete every other local branch but a local-only base. Raises `CloneFailed` or
    `ResetRefused`."""
    _credential_free(url)
    refuse_steering(tree, url)
    scrub(tree, url)
    mirror = _mirror(acting, url)
    _fill(tree, url, acting, mirror, _HEADS, _TAGS, prune=True)
    if _ensure_base(tree, url, base, acting, push_missing_base, mirror):
        # An empty forge: no start point exists; the hold starts unborn and keeps no branch of an earlier one.
        core.clear_skip_worktree_bits(tree)
        _unborn(tree, branch or base)
        _drop_other_branches(tree, ())
        return
    core.clear_skip_worktree_bits(tree)
    _local(tree, "reset", "-q", "--hard")
    _local(tree, "clean", "-qffd")
    land(tree, base, branch)
    # The base survives only where it is the local-only one `_ensure_base` made: a local copy of a branch the forge
    # has would go stale under the runner.
    _drop_other_branches(tree, (branch, "" if _has_ref(tree, f"refs/remotes/origin/{base}") else base))


def refuse_steering(tree: str, url: str) -> None:
    """Raise `CloneFailed(auth)` when the clone's own config (its last runner's to write) sets a key that can
    redirect or intercept the credentialed connection — the node's credential helper's own rule
    (`git_credential_helper.steering_refusal`), asked before the fetch so the lease fails whether or not the
    forge would have asked for a login."""
    # The global scope cut as the fetch cuts it (`git_env.NO_USER_CONFIG`): only what the fetch would read counts.
    rc, out, err = core._run_git("-C", tree, "config", "--list", "--show-scope", "--show-origin", "--includes",
                                 "-z", env_extra=dict(git_env.NO_USER_CONFIG))
    refusal = (git_credential_helper.steering_refusal(out.encode(), tree) if rc == 0
               else f"refused: could not read the clone's git config: {(err or '').strip()[-200:]}")
    if refusal:
        log.warning("slot-clone: %s %s", tree, refusal)
        raise CloneFailed(host_of(url), CAUSE_AUTH)


# Repo-local files of a held-over clone that can run a program or point git elsewhere, none of which a slot clone
# made by `clone` has: alternates (and `core.alternateRefsCommand` with them), attributes naming filter drivers, a
# worktree-config or common-dir redirection, and hooks (the push guard reinstalls its own after the lease's scrub).
_PLANTABLE = ("objects/info/alternates", "objects/info/http-alternates", "info/attributes", "commondir",
              "config.worktree")


def _fresh_config(tree: str, url: str) -> str:
    """The `.git/config` of a clone `clone` would make: the repository format (sha256 kept, any other extension
    dropped), the platform's core defaults, and `origin` — nothing a runner wrote survives."""
    rc, fmt, _e = core._run_git("config", "--file", os.path.join(tree, ".git", "config"), "--get",
                                "extensions.objectformat", env_extra=dict(git_env.NO_USER_CONFIG))
    sha256 = rc == 0 and fmt.strip().lower() == "sha256"
    core_lines = [f"\trepositoryformatversion = {1 if sha256 else 0}", "\tbare = false", "\tlogallrefupdates = true"]
    if sys.platform == "win32":
        core_lines += ["\tfilemode = false", "\tsymlinks = false", "\tignorecase = true"]
    else:
        core_lines += ["\tfilemode = true"]
        if sys.platform == "darwin":
            core_lines += ["\tignorecase = true", "\tprecomposeunicode = true"]
    if any(ch in url for ch in '"\\\n\r'):
        raise core.ResetRefused("clone-unresolvable: the repository URL cannot be written into a git config")
    return "\n".join(["[core]", *core_lines,
                      *(["[extensions]", "\tobjectformat = sha256"] if sha256 else []),
                      '[remote "origin"]', f'\turl = {url}', f"\tfetch = {_HEADS}", ""])


def scrub(tree: str, url: str) -> None:
    """Return a held-over clone's `.git` to what `clone` makes before any credentialed op runs in it: `.git/config`
    rewritten from scratch (`_fresh_config` — a key that runs a program, such as `core.alternateRefsCommand`,
    `core.sshCommand`, `core.worktree` or a filter driver, never survives), and every `_PLANTABLE` file and hook
    removed. A concurrent same-uid writer can still race the op that follows; that is the node's same-uid
    boundary, not this function's. Raises `ResetRefused`."""
    git = os.path.join(tree, ".git")
    for rel in _PLANTABLE:
        erase(os.path.join(git, rel))
    hooks = os.path.join(git, "hooks")
    if os.path.islink(hooks) or not os.path.isdir(hooks):
        erase(hooks)
    else:
        for name in os.listdir(hooks):
            erase(os.path.join(hooks, name))
    err = session_user.replace_file_as_session_user(os.path.join(git, "config"), _fresh_config(tree, url),
                                                    mode=0o644)
    if err:
        raise core.ResetRefused(f"cannot rewrite {git}/config: {err}")


def land(tree: str, base: str, branch: str) -> None:
    """Check out the hold's start point: `branch` from `origin/<branch>` when the forge has it, else from
    `origin/<base>`; with no `branch` (a blind review child) HEAD is detached at that base. A tree `clone` made of
    an empty forge (no branch, local or remote) lands `branch` — else `base` — unborn (`_unborn`); that reading of
    the tree holds only right after `clone` (its callers), before any runner touched it — a held-over tree is
    landed by `refresh`, which judges emptiness from the mirror."""
    if has_no_branch(tree):
        _unborn(tree, branch or base)
        return
    start = _start(tree, base, branch)
    if branch:
        _local(tree, "checkout", "-q", "-f", "-B", branch, start)
    else:
        _local(tree, "checkout", "-q", "-f", "--detach", start)


def has_no_branch(tree: str) -> bool:
    """Whether `tree` holds no branch, local or `origin`'s: a clone of an empty forge, read only right after
    `clone`/`refresh` landed it — both fill `refs/remotes/origin/` from the daemon's mirror (pruned), and an empty
    refresh drops every local branch, so a ref a last runner planted elsewhere cannot answer. Raises
    `ResetRefused` when git cannot answer."""
    return not _local(tree, "for-each-ref", "--count=1", "--format=%(refname)", "refs/heads",
                      "refs/remotes/origin/").strip()


def _start(tree: str, base: str, branch: str) -> str:
    """The commit a hold starts from: `origin/<branch>`, else `origin/<base>`, else the local `<base>`."""
    for ref in ((f"refs/remotes/origin/{branch}",) if branch else ()) + (f"refs/remotes/origin/{base}",
                                                                         f"refs/heads/{base}"):
        if _has_ref(tree, ref):
            return ref
    raise core.ResetRefused(core.missing_base_refusal(base))


def _unborn(tree: str, name: str) -> None:
    """HEAD on the unborn branch `name` — what `git checkout --orphan` makes — with an empty index and no tracked
    file left: the start point of a hold on an empty forge."""
    _local(tree, "symbolic-ref", "HEAD", f"refs/heads/{name}")
    _local(tree, "read-tree", "--empty")
    _local(tree, "clean", "-qffd")


def _forge_is_empty(mirror) -> bool:
    """Whether the forge has no branch at all, judged from the daemon's freshly fetched `mirror` (never the tree,
    whose refs are its runner's to write). Raises `ResetRefused` when the mirror cannot be read."""
    try:
        return not git_mirror.has_branch(mirror)
    except git_mirror.MirrorError as exc:
        raise core.ResetRefused(f"clone-unresolvable: {exc}") from None


def _ensure_base(tree: str, url: str, base: str, acting: Acting, push: bool, mirror) -> bool:
    """Make the integration branch resolvable when the forge lacks it: created ON THE FORGE off its default
    branch when the project may push (`push`), else as a local branch only. True — and nothing created anywhere —
    when the forge has no branch at all (`_forge_is_empty`): the caller lands the tree unborn. Raises
    `ResetRefused` naming the branch when the forge has branches but no default branch."""
    if _forge_is_empty(mirror):
        return True
    if _has_ref(tree, f"refs/remotes/origin/{base}") or _has_ref(tree, f"refs/heads/{base}"):
        return False
    head = core._git(tree, "symbolic-ref", "-q", "refs/remotes/origin/HEAD")[1].strip()
    if not head.startswith("refs/remotes/origin/") or not _has_ref(tree, head):
        raise core.ResetRefused(core.missing_base_refusal(base))
    if push:
        # Re-fetched with `head=True` (the forge's default branch named now), not the caller's `mirror`.
        # What is pushed is read from the daemon's mirror, never the tree: the tree's `origin/HEAD` and refs are
        # its runner's to write, and a planted one would create the base off a commit of the runner's choosing.
        mirror = _mirror(acting, url, head=True)
        default = git_mirror.head_branch(mirror)
        sha = git_mirror.branch_sha(mirror, default) if default else None
        if not sha:
            raise core.ResetRefused(core.missing_base_refusal(base))
        _forge(acting, url, "push", lambda: git_mirror.push_for(
            acting, url, sha, f"refs/heads/{base}", timeout=core._clone_timeout()))
        _fill(tree, url, acting, mirror, f"+refs/heads/{default}:refs/remotes/origin/{default}")
        _local(tree, "update-ref", f"refs/remotes/origin/{base}", sha)
        log.info("slot-clone: integration branch %s created on %s off %s", base, host_of(url), default)
    else:
        _local(tree, "update-ref", f"refs/heads/{base}", head, "")
        log.info("slot-clone: integration branch %s created in %s off %s — local only: this project may not push",
                 base, tree, head)
    return False


def _drop_other_branches(tree: str, keep: tuple[str, ...]) -> None:
    """Delete every local branch but `keep`: a held-over clone carries nothing of an earlier hold's branches."""
    out = _local(tree, "for-each-ref", "--format=%(refname)", "refs/heads")
    for ref in out.split():
        if ref not in {f"refs/heads/{name}" for name in keep if name}:
            core._git(tree, "update-ref", "-d", ref)


def move(src: str, dst: str) -> None:
    """Rename `src` onto an absent `dst` — `os.replace` in-process, `mv` as the session user where the uid
    split applies (the parent directory is theirs). Raises `ResetRefused`."""
    if os.path.lexists(dst):
        raise core.ResetRefused(f"cannot move {src} to {dst}: {dst} already exists")
    if session_user.switch_needed():
        proc = session_user.run_as_session_user(["mv", "--", src, dst], timeout=120)
        if proc.returncode != 0:
            raise core.ResetRefused(f"cannot move {src} to {dst}: {(proc.stderr or '').strip()}")
        return
    try:
        os.replace(src, dst)
    except OSError as exc:
        raise core.ResetRefused(f"cannot move {src} to {dst}: {exc}") from exc


def _clear_readonly(func, path, _exc) -> None:
    """`shutil.rmtree`'s error hook: git writes its objects read-only, which Windows refuses to delete."""
    os.chmod(path, stat.S_IWRITE)
    func(path)


def erase(path: str) -> None:
    """Remove `path` and everything under it; an absent path is fine. Raises `ResetRefused`."""
    if not os.path.lexists(path):
        return
    if session_user.switch_needed():
        err = session_user.remove_tree_as_session_user(path)
        if err:
            raise core.ResetRefused(f"cannot remove {path}: {err}")
        return
    try:
        env_helpers.remove_path(path, onexc=_clear_readonly)
    except FileNotFoundError:
        return
    except OSError as exc:
        raise core.ResetRefused(f"cannot remove {path}: {exc}") from exc
