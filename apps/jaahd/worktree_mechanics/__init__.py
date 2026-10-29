"""Worktree CLAIM/RETIRE/CREATE mechanics — HOW to do a thing to a worktree, never WHICH one. The CP
decides the slot and its ports; `reconcile/` calls `lifecycle.claim_mechanics`/`lifecycle.create_mechanics`
and `commands.py` calls `lifecycle.retire_mechanics`/`lifecycle.create_mechanics` in-process to make the host
match.

NO LIVENESS VETO, ON EITHER SIDE OF THE WIRE. The only exclusion is the ROW: `app/slot_state.py::lease`'s
`WHERE state = 'free' AND number > 0`. Safe because nothing frees a slot on silence — only completing,
deleting or resetting the task reaches `release`. Every liveness probe is an ALARM, not a gate: the cwd
scan (`processes`) feeds `reconcile.observe.observe_slot` and `fleet_launch.launch_refusal`, neither of which
refuses. The scan resolves unreadable `/proc/<pid>/cwd` (runners are `jaah`, the daemon `jaahd`)
through the session user and fails OPEN on a skip.

MODULES — import the one that owns a name (`from worktree_mechanics import ports`); this file exports none.
  core         slot identity (`PoolConfig`, `slot_path`, `wt_branch`) and the git plumbing (`_run_git`, `_git`)
  env_file     a slot's `.env` I/O: cached reader, private-mode writer, mode hardening, `KEY=value` edits
  ports        the port band, tier-2 auto ports, stack and node identity, `has_stack`
  managed_env  the CP-managed, marker-tracked `.env` assignments
  processes    `/proc` reads, redacted command lines, the cwd scan
  reap         orphan reaping
  reset        `reset_to_base`, released-slot sync, merged-branch pruning
  lifecycle    roster, clone, create, claim and retire of a worktree or folder slot
Siblings reach each other as `<module>.<name>`, so a test patch on the defining module reaches every caller.

IMPORTS: stdlib + `session_user` (uid split) + `git_mirror` (the single home of credentialed git: every
network op runs in the project's daemon-owned mirror, and a tree here is updated by a credential-free local
fetch from it) + `clone_identity` (its `ProjectBusy`) + the stdlib-only `host_platform` seam (which OS answers;
static pools only on Windows). MUST NOT import `main`, `state`, `commands`, `reconcile`, or any third-party
distribution — hence the duplicate `PoolConfig`."""
