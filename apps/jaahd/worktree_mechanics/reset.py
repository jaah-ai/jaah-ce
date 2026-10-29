"""Resetting a slot to its base — unpushed-work backup, the reset, released-slot sync, merged-branch pruning."""
from __future__ import annotations

import logging
import os

import env_helpers
import host_platform
import session_user

from worktree_mechanics import core

log = logging.getLogger("jaahd.worktree_mechanics.reset")


# ── reset_to_base — the destructive chokepoint, with its kill-safety gates ─────────────────────────────
def _checkout_reset_branch(wt: str, branch: str, resume_ref: str, base: str) -> bool:
    """Land `branch` on the resume tip, falling back to origin/base then local base. False only if all
    three fail."""
    for ref in (resume_ref, f"origin/{base}", base):
        rc, _o, _e = core._git(wt, "checkout", "-q", "-B", branch, ref)
        if rc == 0:
            return True
    return False


def _existing_unpushed_backup(wt: str, branch: str, sha: str) -> str:
    """The ONE `refs/pool-unpushed/<branch>/…` ref that ALREADY backs `sha` up (`""` if none does yet),
    collapsing any same-sha duplicates into it on the way out.

    One ref per sha, never per retry: the pending intent re-fires gate 3 every ~5s (measured 145 refs for
    one sha). The OLDEST survives (fixed-width `<stamp>-<sha12>` sorts lexicographically); only exact-sha
    twins are deleted — a different sha is another tip's only copy.

    A prerequisite of the CP's park budget: gate 3's refusal names this ref, so a per-attempt value would
    vary the CP's `refusal_key` and reset its identical-refusal counter forever."""
    rc, out, _e = core._git(wt, "for-each-ref", "--format=%(refname) %(objectname)",
                       f"refs/pool-unpushed/{core._safe_ref_component(branch)}")
    if rc != 0:
        # Loud: `""` also means "none yet", and a failed enumeration disarms the CP's park budget.
        log.warning(
            "could not enumerate refs/pool-unpushed for %s in %s (rc %s) — proceeding as if none exists, "
            "which writes a fresh backup ref and may re-accumulate duplicates", branch, wt, rc,
        )
        return ""
    twins = []
    for line in out.splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1] == sha:
            twins.append(parts[0])
    twins.sort()
    if not twins:
        return ""
    keep = twins[0]
    for dup in twins[1:]:
        # Expected old value, so a ref that changed under us is not clobbered. A failed delete is not fatal.
        rc_del, _o, err = core._git(wt, "update-ref", "-d", dup, sha)
        if rc_del != 0:
            log.warning(
                "could not delete duplicate unpushed-backup ref %s (rc %s) — keeping it; %s still backs "
                "up %s: %s", dup, rc_del, keep, sha[:12], (err or "").strip()[:200],
            )
    return keep


def _is_ancestor(wt: str, ancestor: str, descendant: str) -> bool:
    """`merge-base --is-ancestor` as a BOOLEAN, raising `ResetRefused` when the probe itself failed.

    rc 1 is "no"; any higher rc (128, 124/125/127) is UNANSWERED. Both callers pick a `checkout -B` tip off
    this, so an unanswered probe must REFUSE: a retry is possible, un-moving a branch ref is not."""
    rc, _o, err = core._git(wt, "merge-base", "--is-ancestor", ancestor, descendant)
    if rc in (0, 1):
        return rc == 0
    raise core.ResetRefused(
        f"could not tell whether {ancestor} is contained in {descendant} in {wt} (`merge-base "
        f"--is-ancestor` rc {rc}) — refusing to choose a reset tip on an unanswered safety "
        f"question:\n{(err or '').strip()}"
    )


def _backup_unpushed_commits(wt: str, branch: str) -> str:
    """Gate 3's WRITE half, with none of its landing: tag `refs/heads/<branch>`'s commits absent from
    every `origin/*` ref under `refs/pool-unpushed/...` (`_existing_unpushed_backup` reuses one for an
    unchanged sha), then stop — no `resume_ref` decision, no checkout.

    Split out for `detach_base`, which must not `checkout -B` (that force-moves `branch`; detach promises to
    leave it). The tag is also what survives the runner's own later `git checkout -B fleet/task-<id>
    <base>`, which force-moves an existing ref without asking.

    FAILS CLOSED like gate 3: an unreadable tip, a failed `rev-list`, or an unwritable tag with unpushed
    commits raise `ResetRefused`. `""` only for no local ref, or every commit already on `origin`."""
    rc_tip, local_sha, err_tip = core._git(wt, "rev-parse", "-q", "--verify", f"refs/heads/{branch}")
    local_sha = local_sha.strip()
    if rc_tip == 1:
        return ""  # `--verify`'s real answer "no such local branch" — nothing to back up
    if rc_tip != 0 or not local_sha:
        raise core.ResetRefused(
            f"could not read refs/heads/{branch} in {wt} (rev-parse rc {rc_tip}) — refusing to detach "
            f"this tree while that branch's unpushed-commit state is UNKNOWN; an unreadable local tip is "
            f"not an empty one:\n{(err_tip or '').strip()}"
        )
    rc_list, out, err_list = core._git(wt, "rev-list", local_sha, "--not", "--remotes=origin")
    if rc_list != 0:
        raise core.ResetRefused(
            f"could not list the commits on {branch} ({local_sha[:12]}) absent from every origin ref in "
            f"{wt} (rev-list rc {rc_list}) — an empty answer from a FAILED probe is what makes this gate "
            f"silently skip the backup:\n{(err_list or '').strip()}"
        )
    unpushed = [line for line in out.splitlines() if line.strip()]
    if not unpushed:
        return ""
    tag = _existing_unpushed_backup(wt, branch, local_sha)
    if not tag:
        tag = (
            f"refs/pool-unpushed/{core._safe_ref_component(branch)}/"
            f"{core._safe_ref_component(env_helpers.now_iso())}-{local_sha[:12]}"
        )
        rc_tag, _o, err = core._git(wt, "update-ref", tag, local_sha)
        if rc_tag != 0:
            raise core.ResetRefused(
                f"{branch} in {wt} carries {len(unpushed)} commit(s) absent from every origin ref (tip "
                f"{local_sha[:12]}) and the durable backup ref {tag} could NOT be recorded (rc {rc_tag}) "
                f"— refusing to detach over them:\n{(err or '').strip()}"
            )
    log.warning(
        "%s: backed up %s commit(s) on %s absent from every origin ref (tip %s) to %s before detaching "
        "— a prior attempt's unpushed work, kept recoverable even if the branch ref itself later moves: "
        "git -C '%s' branch <name> %s",
        wt, len(unpushed), branch, local_sha[:12], tag, wt, tag,
    )
    return tag


def reset_to_base(
    config: "core.PoolConfig",
    wt: str,
    branch: str | None,
    *,
    fresh: bool = False,
    detach_base: bool = False,
    own_branch: str | None = None,
    abandon: bool = False,
    delete_branch: bool = False,
) -> None:
    """Reset a worktree's TRACKED tree to a clean origin/<base> on `branch`, preserving untracked files
    (the warm stack). Raises `ResetRefused` on an unrecoverable git failure (a failed lease). NO LIVENESS
    PROBE RUNS HERE: the CP decided this slot is free, and the node executes. `detach_base` (retire, and a
    fresh fleet dispatch) detaches HEAD onto origin/<base>, leaving any branch ref intact.

    `branch` is falsy for a fresh dispatch (the CP names none), making the branch fetch and collision check
    no-ops. `own_branch` (`{branch_prefix}fleet/task-<id>`) is then the IDENTITY signal for the two
    destructive gates only — `identity_branch = branch or (own_branch if detach_base else None)`. Detaching
    is no licence to skip them.

    DIRTY-TREE GATE: dirty on the IDENTITY branch refuses (the CP is overwriting the held task's unresumed
    work — a contradiction); on any other branch it is a settled task's debris
    (`app/slot_state.py::settled_holds` frees it dirty) and resets through. No stash path for uncommitted
    edits. `abandon` (operator Reset, `routes/tasks/transitions.py::reset_task`) is the ONE exemption — nothing to
    resume to; committed work is still tagged.

    GATE 3 backs up and resumes: commits absent from every `origin/*` are tagged under
    `refs/pool-unpushed/...` (one per sha) then checked out; `fresh` and `auto/*` discard. Refuses on any
    unknown answer. `detach_base` runs only the write half (`_backup_unpushed_commits`).

    `delete_branch` deletes `refs/heads/<identity_branch>` after a SUCCESSFUL detach only (a checked-out
    ref cannot be deleted); a failed delete WARNS. Order — backup, detach, delete — keeps it recoverable."""
    base = config.base

    # Fetch base + the branch's own remote counterpart (best-effort; warn on base fetch failure).
    #
    # From the project's mirror (`_fetch_from_mirror`), the most frequent authenticated op here. The swallow stays — an offline node reuses its local base — but the warning
    # carries git's own stderr: a slot nobody declared gets its clone token REFUSED, and
    # "offline?" would hide that.
    rc, _o, err = core._fetch_from_mirror(config, wt, base)
    if rc != 0:
        log.warning(
            "fetch of origin/%s failed in %s (rc=%s) — reusing the STALE local %s. If this says "
            "authentication/could not read Username, it is a CREDENTIAL refusal, not the network: "
            "the CP serves this clone the Git Access credential of whoever declared %s's slots on "
            "this node, and refuses when nobody did. git said: %s",
            base, wt, rc, base, config.name, (err or "").strip() or "(nothing on stderr)",
        )
        # A base that is nowhere (renamed, deleted, never created) is created here, or named in the refusal —
        # every landing below would otherwise fail as a misleading "warm-stack collision".
        core.ensure_base(config, wt)
    if branch:
        core._fetch_from_mirror(config, wt, branch, refresh=False)  # best-effort; a never-pushed branch just fails

    # The dirty-tree gate (docstring); runs for `detach_base` too.
    identity_branch = branch or (own_branch if detach_base else None)
    dirty = core._git(wt, "diff", "--quiet")[0] != 0 or core._git(wt, "diff", "--cached", "--quiet")[0] != 0
    # `abandon` is exempt — ONLY `abandon`; never widen it to another caller.
    if dirty and identity_branch and core.wt_branch(wt) == identity_branch and abandon:
        # Loud: the one place a working tree is discarded on purpose.
        log.warning(
            "reset_to_base: DISCARDING the uncommitted changes in %s on %s — the operator ABANDONED this "
            "attempt (Reset), so the dirty-tree refusal is deliberately skipped. Committed work on that "
            "branch is still backed up under refs/pool-unpushed/... before the detach; uncommitted edits "
            "are gone.",
            wt, identity_branch,
        )
    elif dirty and identity_branch and core.wt_branch(wt) == identity_branch:
        raise core.ResetRefused(
            f"{wt} is dirty on {identity_branch!r} — the EXACT branch this reset is FOR — with no resume "
            f"requested: that is a contradiction, not a case to preserve-and-continue. A slot whose tree "
            f"already sits, dirty, on the task's own branch must resume in place, never reset out from "
            f"under itself. Investigate why this dispatch was not sent as a resume; do not re-add a "
            f"stash/backup path here — the backup refs from tasks 1806 and 1808 sat unread until a human "
            f"extracted them by hand, proof that mechanism only delays discovery of the same bug."
        )
    core.clear_skip_worktree_bits(wt)
    core._git(wt, "reset", "-q", "--hard")  # tracked tree → HEAD (keeps untracked)

    # Dispatch race: `branch` already checked out in ANOTHER worktree — `checkout -B` would silently steal
    # it. Skipped for a branchless reset. Defensive under one-slot-per-task.
    if branch:
        other = _worktree_holding_branch(wt, branch)
        if other:
            raise core.ResetRefused(
                f"[{core.BRANCH_HELD_ELSEWHERE}] cannot lease {wt} onto {branch}: that branch is ALREADY "
                f"checked out in another pool worktree ({other}) — a dispatch race leased two slots for "
                f"the same fleet branch (#667). Release/reclaim {other} first; NOT auto-detaching it "
                f"(would disturb that slot's in-flight or crash-held work)."
            )

    # Detach mode: gate 3's WRITE half first, never its landing (docstring). `delete_branch` only after the
    # detach, so the ref is not checked out and its commits are already backed up.
    if detach_base:
        if identity_branch:
            _backup_unpushed_commits(wt, identity_branch)
        for ref in (f"origin/{base}", base):
            if core._git(wt, "checkout", "-q", "--detach", ref)[0] == 0:
                if delete_branch and identity_branch:
                    # Never a raise: the reset is the contract, the delete housekeeping.
                    rc_del, _o_del, err_del = core._git(wt, "branch", "-D", identity_branch)
                    if rc_del != 0:
                        log.warning(
                            "reset_to_base: %s detached onto %s but the abandoned branch %s could NOT be "
                            "deleted (rc %d) — it may be checked out elsewhere or may not exist here; the "
                            "reset stands:\n%s",
                            wt, ref, identity_branch, rc_del, (err_del or "").strip(),
                        )
                return
        err = core._git(wt, "checkout", "--detach", f"origin/{base}")[2]
        raise core.ResetRefused(f"could not detach {wt} onto origin/{base} for retire — resolve by hand:\n{err}")

    # Resume from origin/branch's own tip when it carries commits origin/base does not have yet (unmerged
    # WIP), instead of always wiping to a fresh origin/base.
    resume_ref = f"origin/{base}"
    # Gate 3's backup tag, named by the landing verification's refusal.
    unpushed_backup = ""
    if core._git(wt, "rev-parse", "-q", "--verify", f"refs/remotes/origin/{branch}")[0] == 0 and (
        not _is_ancestor(wt, f"origin/{branch}", f"origin/{base}")
    ):
        resume_ref = f"origin/{branch}"
        log.warning(
            "%s: resuming %s from its own pushed tip (origin/%s), not a fresh %s — unmerged WIP found",
            config.name, branch, branch, base,
        )

    # GATE 3: a never-pushed or locally advanced branch carries commits `_checkout_reset_branch` would
    # force-discard. Tag them FIRST, then RESUME onto the local tip — refusing would wedge the task on
    # every slot (the ref is pool-shared).
    rc_tip, local_sha, err_tip = core._git(wt, "rev-parse", "-q", "--verify", f"refs/heads/{branch}")
    local_sha = local_sha.strip()
    # Detection is fail-closed: rc 1 = no such branch; 124/125/127/128 also give empty stdout, and reading
    # that as "nothing to lose" force-moves the branch with no backup.
    if rc_tip > 1:
        raise core.ResetRefused(
            f"could not read refs/heads/{branch} in {wt} (rev-parse rc {rc_tip}) — refusing to reset a "
            f"branch whose unpushed-commit state is UNKNOWN; an unreadable local tip is not an empty "
            f"one:\n{(err_tip or '').strip()}"
        )
    if rc_tip == 0 and local_sha:
        rc_list, out, err_list = core._git(wt, "rev-list", local_sha, "--not", "--remotes=origin")
        if rc_list != 0:
            raise core.ResetRefused(
                f"could not list the commits on {branch} ({local_sha[:12]}) absent from every origin ref "
                f"in {wt} (rev-list rc {rc_list}) — an empty answer from a FAILED probe is what makes this "
                f"gate silently skip the backup:\n{(err_list or '').strip()}"
            )
        unpushed = [line for line in out.splitlines() if line.strip()]
        if unpushed:
            # Reuse an existing backup of this sha (`_existing_unpushed_backup`).
            tag = _existing_unpushed_backup(wt, branch, local_sha)
            if not tag:
                tag = (
                    f"refs/pool-unpushed/{core._safe_ref_component(branch)}/"
                    f"{core._safe_ref_component(env_helpers.now_iso())}-{local_sha[:12]}"
                )
                # Write path only: an unrecordable backup refuses.
                rc_tag, _o, err = core._git(wt, "update-ref", tag, local_sha)
                if rc_tag != 0:
                    raise core.ResetRefused(
                        f"{branch} in {wt} carries {len(unpushed)} commit(s) absent from every origin ref "
                        f"(tip {local_sha[:12]}) and the durable backup ref {tag} could NOT be recorded "
                        f"(rc {rc_tag}) — refusing to reset over them:\n{(err or '').strip()}"
                    )
            unpushed_backup = tag
            # `claim_mechanics`' per-slot placeholder; both spellings (the prefix is per-project config).
            placeholder = (branch.startswith("auto/")
                           or branch.startswith(f"{config.branch_prefix}auto/"))
            if fresh or placeholder:
                # DISCARD, not resume: `fresh` has no tree state to resume into; `auto/*` is per-slot
                # scratch, and resuming would hand a new lease the previous occupant's commits. The tag is
                # the recovery. EXPOSURE: for `fresh`, `create_worktree`'s `worktree add -B` has already
                # force-moved the shared branch ref with no backup — the fix belongs there.
                why = ("a FRESH claim (the slot was just materialised), i.e. an explicit wipe" if fresh
                       else "an `auto/*` per-slot placeholder branch no task owns")
                log.warning(
                    "%s: DISCARDING %s commit(s) on %s absent from every origin ref (tip %s) — this is %s, "
                    "not a resume. Recover with: git -C '%s' branch <name> %s",
                    config.name, len(unpushed), branch, local_sha[:12], why, wt, tag,
                )
            elif resume_ref.startswith("origin/") and resume_ref != f"origin/{base}" and (
                not _is_ancestor(wt, resume_ref, local_sha)
            ):
                # DIVERGED: keep the PUSHED tip (authoritative; local-only commits stay at the tag). Not a
                # refusal — the ref is pool-shared, so refusing wedges the task in every slot.
                log.error(
                    "%s: %s has DIVERGED from %s — %s local commit(s) are on neither, so resuming onto the "
                    "local tip %s would drop pushed work off the branch. Staying on %s and leaving the "
                    "local-only commits addressable at %s. Recover with: git -C '%s' branch <name> %s",
                    config.name, branch, resume_ref, len(unpushed), local_sha[:12], resume_ref, tag,
                    wt, tag,
                )
                # Pin the SHA: a sibling's fetch or a force-push can move `origin/*` before the checkout,
                # dropping commits `rev-list` excluded from the tag because they were on origin.
                rc_pin, pushed_sha, err_pin = core._git(
                    wt, "rev-parse", "-q", "--verify", f"{resume_ref}^{{commit}}")
                if rc_pin != 0 or not pushed_sha.strip():
                    raise core.ResetRefused(
                        f"{branch} in {wt} has DIVERGED from {resume_ref}, whose commit could no longer be "
                        f"resolved (rc {rc_pin}) — refusing rather than landing on a ref that moved under "
                        f"the decision; the local-only commits are at {tag}:\n{(err_pin or '').strip()}"
                    )
                resume_ref = pushed_sha.strip()
            else:
                # Lands the branch where it already is; the SHA, so nothing moving the ref can change it.
                resume_ref = local_sha
                log.warning(
                    "%s: RESUMING %s onto its own LOCAL tip %s — %s commit(s) there are absent from every "
                    "origin ref (never pushed, or pushed and then advanced locally), so there is no origin "
                    "ref to resume from and a reset to %s would have discarded them. Backed up to %s.",
                    config.name, branch, local_sha[:12], len(unpushed), base, tag,
                )

    if not _checkout_reset_branch(wt, branch, resume_ref, base):
        # RECOVERABLE COLLISION: an UNTRACKED file shadowing a path TRACKED at the checkout target makes
        # `checkout -B` refuse. The committed version is authoritative, so remove ONLY the untracked files
        # tracked at the target — never the non-colliding warm-stack files — and retry ONCE.
        co_ref = resume_ref
        if core._git(wt, "rev-parse", "-q", "--verify", f"{co_ref}^{{commit}}")[0] != 0:
            co_ref = f"origin/{base}"
        if core._git(wt, "rev-parse", "-q", "--verify", f"{co_ref}^{{commit}}")[0] != 0:
            co_ref = base
        cleaned = False
        rc, out, _e = core._git(wt, "ls-files", "-z", "--others", "--exclude-standard")
        if rc == 0:
            for f in out.split("\0"):
                if not f:
                    continue
                if core._git(wt, "cat-file", "-e", f"{co_ref}:{f}")[0] == 0:
                    try:
                        if host_platform.is_windows():
                            # As the runner account there: the daemon deletes nothing in the runner-writable
                            # home itself, and the name came out of the runner's own tree.
                            err = session_user.unlink_as_session_user(os.path.join(wt, f))
                            if err:
                                raise OSError(err)
                        else:
                            os.remove(os.path.join(wt, f))
                        cleaned = True
                        log.warning(
                            "%s: removed untracked '%s' shadowing a path tracked at %s (warm-stack collision) "
                            "so the lease checkout can proceed — the committed version is restored by the "
                            "checkout", config.name, f, co_ref,
                        )
                    except OSError:
                        pass
        if not cleaned or not _checkout_reset_branch(wt, branch, resume_ref, base):
            err = core._git(wt, "checkout", "-B", branch, resume_ref)[2]
            raise core.ResetRefused(
                f"could not put {wt} on {branch} off {resume_ref} — likely an untracked warm-stack file now "
                f"collides with a path tracked in {base}; resolve by hand:\n{err}"
            )

    # Verify the landing whenever the target was not the plain base: `_checkout_reset_branch` falls back
    # to base and STILL returns True, moving the branch off a deliberately chosen tip (e.g. a warm-stack
    # file tracked at the intended ref but not at base).
    if resume_ref != f"origin/{base}":
        # Resolved commits, never ref names.
        rc_want, wanted, _e = core._git(wt, "rev-parse", "-q", "--verify", f"{resume_ref}^{{commit}}")
        rc_got, landed, _e = core._git(wt, "rev-parse", "-q", "--verify", "HEAD^{commit}")
        wanted, landed = wanted.strip(), landed.strip()
        if rc_want != 0 or rc_got != 0 or landed != wanted:
            # Put the branch ref back FIRST (compare-and-swap): left on base, the next pass sees nothing
            # unpushed and resets silently — a runner then "resumes" on an empty base.
            restored = False
            if wanted and landed:
                rc_restore, _o, err_restore = core._git(
                    wt, "update-ref", f"refs/heads/{branch}", wanted, landed)
                restored = rc_restore == 0
                if not restored:
                    log.error(
                        "%s: could NOT restore %s to %s after a failed landing (rc %s) — the branch stays "
                        "on %s, so the NEXT pass will see it as fully pushed and reset it silently; %s",
                        config.name, branch, wanted[:12], rc_restore, landed[:12],
                        (err_restore or "").strip()[:200],
                    )
            raise core.ResetRefused(
                f"{wt} was to put {branch} on {resume_ref} ({wanted[:12] or 'unresolvable'}) — the tip "
                f"this reset DECIDED to keep — but it landed on {landed[:12] or 'nothing'}: the checkout "
                f"fell back to a base ref, which takes those commits off the branch. The branch ref was "
                f"{'RESTORED' if restored else 'NOT restorable'}, so this refuses again next pass instead of "
                f"looking fully pushed"
                + (f". Unpushed work is also at {unpushed_backup} (git -C '{wt}' branch <name> "
                   f"{unpushed_backup} to recover)." if unpushed_backup else ".")
            )


def _worktree_holding_branch(wt: str, branch: str) -> str:
    """The path of ANOTHER worktree that has `branch` checked out (excluding `wt`), or ""."""
    rc, out, _e = core._git(wt, "worktree", "list", "--porcelain")
    if rc != 0:
        return ""
    current = ""
    target = f"branch refs/heads/{branch}"
    for line in out.splitlines():
        if line.startswith("worktree "):
            current = line[len("worktree "):]
        elif line == target and current and current != wt:
            return current
    return ""


# ── A RELEASED SLOT GOES BACK TO BASE, HERE ─────────────────────────────────────────────────────────────
# The opposite end of `reset_to_base`'s caution: the CP has ALREADY ended the hold (`release_slot`), so an
# uncommitted edit in a free tree is litter. Without this, free slots drifted off base (measured: 98 of 100,
# the worst by 618 commits) and merged `fleet/task-<id>` refs piled up (493 in one clone). Operator
# decision (2026-09-19): discard unconditionally, no backup refs. No refusal path; NEVER RAISES.
def sync_released_to_base(config: "core.PoolConfig", wt: str) -> str:
    """Bring a just-RELEASED worktree back to a clean, detached `origin/<base>` and prune the merged
    branch refs behind it. Returns one line for the intent's outcome reason ("" when there is nothing to
    say). Never raises.

    In order:
      1. FETCH (from the project's mirror, `_fetch_from_mirror`); a failure is logged and the sync continues on the local ref.
      2. DISCARD: `reset --hard`, then `clean -fd` — not `-fdx`, the ignored files are the warm stack. A
         nested git repository is skipped by `clean` and survives ("clean" = this repo's own files).
      3. DETACH onto `origin/<base>`, else local `<base>` — detached, since a worktree holding the base
         branch blocks every other worktree from checking it out.
      4. PRUNE merged branches repo-wide (`_prune_merged_branches`): the refs are shared by the clone.
    Fenced by the caller: `_enact_release_slot` runs only after `slot_state.release` committed. Never call
    this where the slot may still be held — step 2 discards a live runner's work with no backup."""
    base = config.base
    notes: list[str] = []
    try:
        # `--end-of-options` (`local_fetch_args`): `_reject_illegal_git_ref` validates `integration_branch` at
        # every writer, but no CHECK backs it, and a `--upload-pack=<cmd>` base would be executed by git.
        rc, _o, err = core._fetch_from_mirror(config, wt, base)
    except core.ResetRefused as exc:
        # A busy project — sync against the local base; never fail a committed release.
        rc, err = 1, str(exc)
    if rc != 0:
        # As `reset_to_base`'s fetch warning.
        log.warning(
            "release sync: fetch of origin/%s failed in %s (rc=%s) — syncing against the STALE local %s. "
            "An authentication/could not read Username failure here is a CREDENTIAL refusal rather than "
            "the network (the CP serves this clone the Git Access credential of whoever declared %s's "
            "slots on this node). git said: %s",
            base, wt, rc, base, config.name, (err or "").strip() or "(nothing on stderr)",
        )
        notes.append(f"fetch of origin/{base} failed — synced against the stale local {base}")
    prior = core.wt_branch(wt)
    core.clear_skip_worktree_bits(wt)
    core._git(wt, "reset", "-q", "--hard")
    core._git(wt, "clean", "-qfd")
    for ref in (f"origin/{base}", base):
        # `--end-of-options` on `rev-parse`, NOT `checkout`: git 2.43 (the fleet's) refuses
        # `checkout --detach --end-of-options <ref>`. A resolved sha never begins with a dash.
        rc, sha, _e = core._git(wt, "rev-parse", "-q", "--verify", "--end-of-options", f"{ref}^{{commit}}")
        if rc == 0 and core._git(wt, "checkout", "-q", "--detach", sha.strip())[0] == 0:
            break
    else:
        # Not a raise (a `failed` release would be re-served forever); loud and named in the outcome.
        _err = core._git(wt, "checkout", "--detach", f"origin/{base}")[2]
        log.error(
            "release sync: %s could not detach onto origin/%s or %s — the tree stays on %s; resolve by "
            "hand:\n%s", wt, base, base, prior, (_err or "").strip(),
        )
        # `notes` rides along: a failed fetch explains a failed detach.
        return "; ".join([*notes, f"could not detach onto origin/{base} — the tree stays on {prior}"])
    pruned, kept = _prune_merged_branches(wt, base)
    if prior != "DETACHED":
        notes.append(f"detached from {prior} onto origin/{base}")
    if pruned:
        notes.append(f"pruned {pruned} merged branch ref(s)")
    if kept:
        # Unpushed commits are never destroyed ("discard" meant the working tree), and are said out loud.
        notes.append(f"kept {len(kept)} branch(es) with unpushed commits: {', '.join(sorted(kept)[:5])}")
    return "; ".join(notes)


# Each probe is a delegated round trip, serial, inside the reconcile pass, and any runner can add refs, so
# an unbounded sweep is a lever on the pool tick. 200 clears a realistic pile (502 seen) in 2-3 releases at
# ~1s worst case; the remainder is the next release's.
_PRUNE_BUDGET = 200


def _prune_merged_branches(wt: str, base: str) -> tuple[int, set[str]]:
    """Delete every local branch whose commits are ALL already in some `origin/*` ref. Returns
    `(deleted count, the names kept because they carry unpushed commits)`.

    Every ref is named in FULL (`refs/heads/<name>`): `rev-list <short>` resolves tags FIRST, so a tag
    shadowing a branch made the probe judge the tag while the delete hit the branch (measured: a
    commit existing nowhere else was destroyed), and `%(refname:short)` becomes `heads/develop` when a tag
    `develop` exists, slipping the base exclusion.

    Three exclusions: the base branch; any branch checked out in ANY worktree (git's own list — so a live
    runner's branch is safe without knowing about holds); any branch with a commit no `origin/*` ref
    contains (stronger than `branch -d`'s upstream test; a FAILED probe keeps the branch). That test
    means "in THIS CLONE's `refs/remotes/origin/*`", not "on github now": tracking refs are never
    `--prune`d here, so a squash-merged branch with a stale remote ref is collected — adding `--prune`
    upstream would turn those into KEPT ones.

    The delete is compare-and-swap (`update-ref -d <ref> <tip>`), not `branch -D`: a sibling slot may
    commit onto a judged branch meanwhile, and CAS turns that race into a kept branch. RESIDUAL:
    `update-ref` does not refuse a checked-out ref and `checked_out` is read once — a branch checked out
    mid-loop could be deleted under its worktree (unborn branch, no commit lost; nearly unreachable)."""
    rc, out, _e = core._git(wt, "for-each-ref", "--format=%(refname) %(objectname)", "refs/heads")
    if rc != 0:
        log.warning("release sync: could not list local branches in %s (rc=%s) — nothing pruned", wt, rc)
        return 0, set()
    checked_out = _checked_out_branches(wt)
    candidates: list[tuple[str, str, str]] = []
    for line in out.splitlines():
        full, _sep, tip = line.strip().partition(" ")
        if not full.startswith("refs/heads/") or not tip:
            continue
        branch = full[len("refs/heads/"):]
        if not branch or branch == base or branch in checked_out:
            continue
        candidates.append((full, branch, tip))
    overflow = candidates[_PRUNE_BUDGET:]
    deleted, kept = 0, set()
    for full, branch, tip in candidates[:_PRUNE_BUDGET]:
        # Full refname (docstring). NO `--end-of-options`: `rev-list` would swallow `--not`/`--remotes`
        # as revisions and every probe would fail closed; a `refs/heads/...` path cannot begin with a dash.
        rc_c, count, _e2 = core._git(wt, "rev-list", "--count", full, "--not", "--remotes=origin")
        if rc_c != 0:
            log.warning(
                "release sync: could not tell whether %s in %s is fully pushed (rc=%s) — KEPT",
                branch, wt, rc_c,
            )
            kept.add(branch)
            continue
        if count.strip() != "0":
            kept.add(branch)
            continue
        # Compare-and-swap on the judged tip (docstring).
        rc_d, _o, err_d = core._git(wt, "update-ref", "-d", full, tip)
        if rc_d != 0:
            log.warning(
                "release sync: could not delete the fully-pushed branch %s in %s (rc=%s) — it may have "
                "moved since it was judged, which is exactly what this refusal is for: %s",
                branch, wt, rc_d, (err_d or "").strip(),
            )
            kept.add(branch)
            continue
        deleted += 1
    if overflow:
        # NAMED, NOT SILENT: a clone this far behind is one an operator should know about, and the next
        # release picks the remainder up.
        log.info(
            "release sync: %s — %d branch ref(s) beyond this release's budget of %d, left for the next "
            "release", wt, len(overflow), _PRUNE_BUDGET,
        )
    if deleted or kept:
        log.info(
            "release sync: %s — pruned %d merged branch ref(s), kept %d carrying unpushed commits",
            wt, deleted, len(kept),
        )
    return deleted, kept


def _checked_out_branches(wt: str) -> set[str]:
    """Every branch checked out in ANY worktree of this clone, the main clone included.

    `_worktree_holding_branch` answers the same question for ONE branch and returns the holder's path;
    this answers it for ALL of them, because the prune above tests every ref in the clone and calling
    that one per branch would re-run `git worktree list` once per branch. An unreadable list answers the
    EMPTY set only through the caller's own probe — a branch that is checked out is also a branch git
    refuses to `-D`, so the worst case here is a logged delete failure rather than a lost ref."""
    rc, out, _e = core._git(wt, "worktree", "list", "--porcelain")
    if rc != 0:
        return set()
    return {
        line[len("branch refs/heads/"):]
        for line in out.splitlines()
        if line.startswith("branch refs/heads/")
    }
