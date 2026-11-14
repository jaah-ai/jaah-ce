"""A blind review child's tree: the PR's merge-base, detached, with exactly the PR's diff applied uncommitted.

The control plane sends `blind_apply` on a review child's `lease_slot` intent
(`app/fleet_verdicts.build_lease_intent`): `{"head_branch", "head_sha", "base_branch", "pr_number"}`. The
lease executor (`reconcile/enact_launch.py`) runs `prepare` after the slot's claim checkout and before the
launch. The session gets no branch name, PR body or commit message — only the tree: HEAD detached at the
merge-base, the diff in the working tree (new files intent-to-add, so `git diff` shows them), FETCH_HEAD and
the head's remote-tracking ref deleted. Fetched objects stay reachable by sha; that is accepted. So are the
refs a slot shares with its source clone (a sibling slot's own branch of the same name): blindness rests on
what the session is told and the CP's tool refusals, not on git hiding history.

The lease has already cloned or refreshed the slot (`slot_clone`) detached at `origin/<base>`. Network goes to
the forge only, with the leasing task's credential, and only from the daemon: each fetch is `slot_clone.fetch`
against the project's credential-free `clone_url` (the daemon's mirror fetched acting for the task, then the
tree filled from it locally) — the base branch into `refs/remotes/origin/<base>`, then the head by branch, or on
GitHub `refs/pull/<n>/head` when the branch is gone.
Every git call in the tree rides `worktree_mechanics.core._run_git` — as the session user where the uid split
applies, repo-local code pinned off — and every diff is `--no-textconv --no-ext-diff`, so nothing the PR or the
tree's config plants runs.

A fetch the forge refuses or cannot answer raises `slot_clone.CloneFailed` unchanged — the executor maps it to
the lease's clone-failure ack, as for the slot's own clone. A head branch the forge does not have (git's "couldn't
find remote ref" classifies as `network`) is a refusal, not a clone failure. Any other step that cannot produce
that tree raises `BlindCheckoutRefused` with a named reason; the executor turns it into a `refused_precondition`
ack (the normal retry path). The node advertises `BLIND_CHECKOUT` in its
`launch_kinds` (`advertised`), so the CP places a review child only on a node that runs this module.
"""
from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass

import git_env
import session_user
import slot_clone
from clone_identity import Acting
from worktree_mechanics import core

#: The `launch_kinds` entry the CP's placement predicate reads.
BLIND_CHECKOUT = "blind_checkout"
#: The note's name under the tree's own git dir (`git rev-parse --git-path`), written only when the PR moved
#: since it was reported. Per worktree, outside the working tree, so it never shows in the reviewed diff.
NOTE_NAME = "jaah-blind-review-note.md"
NOTE_TEXT = (
    "The PR moved since it was reported: the reported head commit is no longer on its branch, so this "
    "tree carries the branch's current tip instead.\n"
)
#: The PR's patch, written by the session user beside the note while it is applied, then removed.
_PATCH_NAME = "jaah-blind-review.patch"

_SHA = re.compile(r"[0-9a-f]{40}|[0-9a-f]{64}")
_GITHUB = re.compile(r"^(?:[a-z+]+://)?(?:[^@/]+@)?github\.com[:/]", re.IGNORECASE)
_TIMEOUT = 300
#: Every diff taken here: no renames, colour, external driver or textconv — so no PR-chosen driver runs.
_DIFF = ("diff", "--binary", "--no-renames", "--no-color", "--no-ext-diff", "--no-textconv")


class BlindCheckoutRefused(Exception):
    """The tree cannot be made blind-reviewable; the message is the named reason the ack carries."""


@dataclass(frozen=True)
class BlindApply:
    head_branch: str
    head_sha: str
    base_branch: str
    pr_number: int | None

    @classmethod
    def from_payload(cls, raw: object) -> "BlindApply":
        """The intent's `blind_apply`, validated; anything malformed is refused, never guessed."""
        if not isinstance(raw, dict):
            raise BlindCheckoutRefused(f"blind_apply is not an object (got {type(raw).__name__})")
        head_branch = _branch(raw, "head_branch")
        base_branch = _branch(raw, "base_branch")
        head_sha, pr_number = raw.get("head_sha"), raw.get("pr_number")
        if not isinstance(head_sha, str) or not _SHA.fullmatch(head_sha):
            raise BlindCheckoutRefused(f"blind_apply.head_sha is not a full commit sha ({head_sha!r})")
        if pr_number is not None and (isinstance(pr_number, bool) or not isinstance(pr_number, int)
                                      or pr_number <= 0):
            raise BlindCheckoutRefused(f"blind_apply.pr_number is not a positive integer ({pr_number!r})")
        return cls(head_branch, head_sha, base_branch, pr_number)


def _branch(raw: dict, key: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not _valid_branch(value):
        raise BlindCheckoutRefused(f"blind_apply.{key} is not a valid branch name ({value!r})")
    return value


def _valid_branch(name: str) -> bool:
    """A name git's own `check-ref-format` takes under `refs/heads/`, and never one git could read as an option
    or a reflog expression. Asked outside any repository, so no repository config is read."""
    if not name or name.startswith("-") or "@{" in name or any(ord(c) < 33 or ord(c) == 127 for c in name):
        return False
    done = subprocess.run(["git", "check-ref-format", f"refs/heads/{name}"], cwd="/", capture_output=True,
                          timeout=30, check=False, env={**os.environ, **git_env.NO_USER_CONFIG})
    return done.returncode == 0


def advertised() -> bool:
    """Whether this node reports `BLIND_CHECKOUT`: every static node; never a MicroVM one (out of scope)."""
    import cloud_modules  # noqa: PLC0415 — deferred: read on the observation path only

    cloud_node = cloud_modules.load("cloud_node")
    return not (cloud_node is not None and cloud_node.microvm_marked())


def _git(wt: str, *args: str, env_extra: dict[str, str] | None = None) -> tuple[int, str, str]:
    return core._run_git("-C", wt, *args, timeout=_TIMEOUT, env_extra=env_extra)


def _must(wt: str, *args: str, what: str) -> str:
    rc, out, err = _git(wt, *args)
    if rc != 0:
        raise BlindCheckoutRefused(f"{what}: git {args[0]} exited {rc}: {(err or out).strip()[:400]}")
    return out.strip()


def _in_git_dir(wt: str, name: str) -> str:
    path = _must(wt, "rev-parse", "--git-path", name, what="locating the tree's git dir")
    return path if os.path.isabs(path) else os.path.join(wt, path)


def note_path(wt: str) -> str:
    return _in_git_dir(wt, NOTE_NAME)


def _as_session_user(argv: list[str], what: str) -> None:
    """A file op in the tree's git dir, as the uid that owns it — so a planted link reaches nothing the session
    user could not already write."""
    try:
        done = session_user.run_as_session_user_or_here(argv, timeout=60)
    except (RuntimeError, OSError, subprocess.TimeoutExpired) as exc:
        raise BlindCheckoutRefused(f"{what}: {exc}") from None
    if done.returncode != 0:
        raise BlindCheckoutRefused(f"{what}: {(done.stderr or done.stdout or '').strip()[:400]}")


def _fetch(wt: str, url: str, acting: Acting, *refspecs: str, forge_refs: tuple[str, ...] = ()) -> None:
    slot_clone.fetch(wt, url, acting, *refspecs, forge_refs=forge_refs)


def _fetch_head(wt: str, spec: BlindApply, url: str, acting: Acting) -> str:
    """The head's current tip into FETCH_HEAD, from the forge by branch; on GitHub (judged from `url`, never the
    tree's config) `refs/pull/<n>/head` when the branch is gone (a merged PR whose branch was deleted). A missing
    ref fails as `CloneFailed(network)`, which here is a refusal; any other cause is re-raised."""
    try:
        _fetch(wt, url, acting, f"refs/heads/{spec.head_branch}")
    except slot_clone.CloneFailed as exc:
        if exc.cause != slot_clone.CAUSE_NETWORK:
            raise
        if spec.pr_number is None or not _GITHUB.match(url or ""):
            raise BlindCheckoutRefused(f"could not fetch the PR's head branch {spec.head_branch!r}: {exc}") from None
        pull_ref = f"refs/pull/{spec.pr_number}/head"
        try:
            _fetch(wt, url, acting, pull_ref, forge_refs=(pull_ref,))
        except slot_clone.CloneFailed as exc2:
            if exc2.cause != slot_clone.CAUSE_NETWORK:
                raise
            raise BlindCheckoutRefused(
                f"could not fetch the PR's head: neither branch {spec.head_branch!r} nor {pull_ref}") from None
    return _must(wt, "rev-parse", "--verify", "FETCH_HEAD^{commit}", what="reading the fetched head")


def _merge_base(wt: str, base_ref: str, head: str) -> str:
    """The commit the PR's diff is taken from. When the head is already on the base (a PR merged with a merge
    commit), the base side of the commit that merged it, so the diff is the PR's, not empty."""
    mb = _must(wt, "merge-base", base_ref, head, what="finding the PR's merge-base")
    if mb != head:
        return mb
    merged_by = _must(wt, "rev-list", "--first-parent", "--ancestry-path", f"{head}..{base_ref}",
                      what="finding the commit that merged the PR").splitlines()
    if not merged_by:
        return mb  # the head IS the base tip: the diff is empty and refused below
    return _must(wt, "merge-base", f"{merged_by[-1]}^1", head, what="finding the merged PR's merge-base")


def _forget_head_refs(wt: str, head_branch: str) -> None:
    """No name points the session at the PR's head: FETCH_HEAD and the head's remote-tracking ref are removed,
    and their absence checked."""
    _git(wt, "update-ref", "-d", "FETCH_HEAD")
    fetch_head = _in_git_dir(wt, "FETCH_HEAD")
    if os.path.lexists(fetch_head):  # several lines: `update-ref -d` leaves the file
        _as_session_user(["rm", "-f", "--", fetch_head], "removing FETCH_HEAD")
    tracking = f"refs/remotes/origin/{head_branch}"
    _git(wt, "update-ref", "-d", tracking)
    if _git(wt, "rev-parse", "-q", "--verify", tracking)[0] == 0 or os.path.lexists(fetch_head):
        raise BlindCheckoutRefused("could not remove FETCH_HEAD or the PR head's remote-tracking ref")


def _same_content(wt: str, commit: str) -> bool:
    """Whether the working tree (tracked and intent-to-add paths) holds exactly `commit`'s content and modes."""
    return _git(wt, *_DIFF, "--quiet", commit)[0] == 0


def prepare(wt: str, spec: BlindApply, url: str, acting: Acting) -> str:
    """Make `wt` the blind review tree for `spec`, fetching from the project's credential-free `url` as `acting`
    (the lease's subject); returns the commit whose diff was applied. Raises `slot_clone.CloneFailed` unchanged
    when the forge refuses or cannot answer a fetch, and `BlindCheckoutRefused` with a named reason for anything
    else that cannot produce the tree."""
    note = note_path(wt)
    patch = _in_git_dir(wt, _PATCH_NAME)
    _as_session_user(["rm", "-f", "--", note, patch], "clearing a previous review's files")
    base_ref = f"refs/remotes/origin/{spec.base_branch}"
    _fetch(wt, url, acting, f"+refs/heads/{spec.base_branch}:{base_ref}")
    tip = _fetch_head(wt, spec, url, acting)
    head = spec.head_sha
    reported_on_branch = head == tip or (
        _git(wt, "cat-file", "-e", f"{head}^{{commit}}")[0] == 0
        and _git(wt, "merge-base", "--is-ancestor", head, tip)[0] == 0)
    if not reported_on_branch:
        head = tip
        _as_session_user(["sh", "-c", 'printf "%s" "$1" > "$2"', "sh", NOTE_TEXT, note], "writing the note")
    mb = _merge_base(wt, base_ref, head)
    if _git(wt, *_DIFF, "--quiet", mb, head)[0] == 0:
        raise BlindCheckoutRefused(f"the PR's diff against its merge-base {mb[:12]} is empty — nothing to review")

    _must(wt, "reset", "-q", "--hard", what="resetting the tree")
    _must(wt, "clean", "-qfd", what="cleaning the tree")
    _must(wt, "checkout", "-q", "--detach", mb, what="checking out the merge-base")
    _must(wt, *_DIFF, f"--output={patch}", mb, head, what="writing the PR's patch")
    try:
        _must(wt, "apply", "--index", "--binary", patch, what="applying the PR's diff to its merge-base")
    finally:
        _as_session_user(["rm", "-f", "--", patch], "removing the PR's patch")
    added = _must(wt, "diff", "--cached", "--no-renames", "--name-only", "--diff-filter=A", "-z",
                  what="listing added files")
    _must(wt, "reset", "-q", what="unstaging the applied diff")
    names = [n for n in added.split("\0") if n]
    if names:
        # Intent-to-add: the files stay uncommitted and unstaged, yet `git diff` shows them.
        _must(wt, "add", "-N", "--", *names, what="marking the PR's new files")
    _forget_head_refs(wt, spec.head_branch)

    # Content equality with the head is exact: it implies the patch-ids agree, and also catches a mode-only or
    # whitespace-only drift that `git patch-id` would hash away.
    if _same_content(wt, mb):
        raise BlindCheckoutRefused("the working tree carries no diff after applying the PR's — refusing an "
                                   "empty review")
    if not _same_content(wt, head):
        raise BlindCheckoutRefused(
            f"the working tree does not hold the PR's change ({mb[:12]}..{head[:12]}) after applying it — "
            f"refusing to review a different change")
    return head
