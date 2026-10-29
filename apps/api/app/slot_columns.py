"""Who writes which column of `worktrees` — the column-ownership contract, as sets a test can intersect.

The "one author per fact" rule as frozensets. Uncoordinated writers on one row are safe because
their SET lists are pairwise disjoint: under READ COMMITTED the second blocks on the row lock, then
re-evaluates against the committed tuple, so no update is lost and no CAS is needed.

Two rules, enforced by `tests/test_slot_two_writers.py`: R-W1, `worktrees` is written only by Core
statements with explicit column lists from the census modules, never a mapped-instance mutation (which
writes from a pre-commit snapshot); R-W2, each statement's compiled SET keys equal its frozenset here —
except `OCCUPANCY` (per-statement subset, union equals family; see its comment).
"""
from __future__ import annotations

# The identity triple: the natural key the observation's UPDATE matches on, never a column it sets.
IDENTITY = frozenset({"node_id", "project_id", "number"})

# ── SENSED — written only by `POST /api/agent/fleet/observe` (app/fleet_observe.py) ───────────────────
# `observe_slot`'s record keys plus the identity triple. `ports` carries `ports_observed`.
SENSED = frozenset(
    {
        "node_id",
        "project_id",
        "number",
        "path",
        "branch",
        "dirty",
        "merged",
        "ahead",
        "behind",
        "head_sha",
        "session",
        "session_id",
        "session_status",
        "session_started_at",
        # Runner identity, read from `/proc`; the observation is its only writer.
        "session_pid",
        "session_proc_started_at",
        # The FK the observation resolves the reported credential dir onto, not the path itself.
        "account_id",
        "console",
        "probes",
        "probed_config_dirs",
        "observed_at",
        "age_last_used_secs",
        "age_last_commit_secs",
        "age_index_mtime_secs",
        # Derived from the node's `age_transcript_write_secs`; the UPDATE keeps the stored value when it is absent.
        "transcript_written_at",
        "tmux_window_id",
        "tmux_target",
        "window_name",
        "compose_project_name",
        "stack_present",
        "stack_up",
        "ports",
        "default_title",
        "host_env",
        "model",
        # The slot manifest (`ObserveSlotIn.repos`); NULL for a single-repo slot.
        "repos",
    }
)

# What the observation's UPDATE may SET — everything it senses except the key it matched on.
SENSED_UPDATABLE = SENSED - IDENTITY

# ── DECIDED — written by nobody ───────────────────────────────────────────────────────────────────────
# NULL on every live row; `idle_hours_expr` reads it as the unknown-age sentinel (`app/slot_state.py`).
# Kept declared so the census keeps every statement off it. `name` is not a member — it lives in
# `MUTATED` alone.
DECIDED = frozenset({"last_active"})

# ── OCCUPANCY — written only by the slot lifecycle (app/slot_state.py) ────────────────────────────────
# Who owns the tree (task, human, nobody) — a CP decision; ends on a decision, never a clock. R-W2 is a
# subset rule here: one statement per edge, union equals the family. `pid`/`pid_start_time`/
# `launch_session_id` (launch ack) must stay apart from SENSED's `session_*` (last `/proc` scan) — the
# comparison is how liveness is told. `state_since` is not `updated_at`, which the trigger advances on
# every write (`models/worktrees.py::Worktree.state_since`).
OCCUPANCY = frozenset(
    {
        "state",
        "state_since",
        # The owner columns: `ck_worktrees_state_ownership` makes them and `state` one fact, so moving an
        # owner without the state is rejected by the database.
        "task_id",
        "owner_user_id",
        "generation",
        "launch_claimed_generation",
        "dispatch_id",
        "dispatched_at",
        "acked_at",
        "pid",
        "pid_start_time",
        "launch_session_id",
        "reason",
        "last_seen_live",
        # The account this lease decided to spend (vs SENSED `account_id`, what the node observed).
        # Written by `slot_state.bind_account` with the take; read by
        # `routes/agent/task_secrets.py::get_account_api_key` before any observation of this dispatch exists.
        "leased_account_id",
    }
)

# ── MUTATED — written only by `POST /api/agent/slot-holder` (app/slot_mutate.py) and the operator's own
# `PUT /api/worktrees/{project}/{number}/title` ──────────────────────────────────────────────────────
# Declared by an operator, never sensed. `SENSED ∩ MUTATED == ∅` keeps the ~5s observation from racing
# the operator's stamp; `DECIDED ∩ MUTATED` and `OCCUPANCY ∩ MUTATED` are asserted separately.

# `task_id` is not here: `slot_state.py` is the sole writer of every OCCUPANCY column, which
# makes R-W2's census over that one module complete.

# `name`, `description` and `updated_by_id` belong here, not in `DB_OWNED`: the operator statement names
# all three, and R-W2 requires a named column to be in that statement's frozenset.

# No statement outside this family names a MUTATED column: the observation is an UPDATE on the natural
# key (`fleet_observe.slot_update_statement`), and a slot's row exists before the node reports on it.
MUTATED = frozenset({"last_used", "name", "description", "updated_by_id"})

# ── JOINED — `worktrees` columns no writer above owns ────────────────────────────────────────────────
# Empty on purpose: the reflection test asserts `declared - reflected == frozenset()`. A future
# joined-or-derived column belongs here.
JOINED: frozenset[str] = frozenset()

# ── DB_OWNED — written by Postgres, named by no statement of ours ──────────────────────────
# `id` (`DEFAULT uuidv7()`), `created_at` (`DEFAULT now()`), `updated_at` (trigger). A declared statement
# must never name one.
DB_OWNED = frozenset({"id", "created_at", "updated_at"})

# ── DECLARATION — written by the declare-a-slot INSERT (`slot_allocation.converge`'s `_add_row`) and by the ──
# clone-owner reassignment UPDATE (`routes/pool_slots._clone_owner_statement`), which names only this
# column. Not `DB_OWNED`: the converge stamps it (`slot_allocation._declarer`). Not `MUTATED`: the
# reassignment names it alone, which would break that family's SET equality. Every INSERT names a
# human; rows still attributed to the system actor are repaired by the reassignment UPDATE (see
# `fleet_observe.slot_update_statement`).
DECLARATION = frozenset({"created_by_id"})

# ── SESSION_RUN — written only by the session-run stamp in `app/fleet_observe.py` ─────────────────────
# Its own family: a second statement writes the CP's stamp on the edge between two successive `session`
# values, computable only against the stored row. It targets present slots only.
SESSION_RUN = frozenset({"session_since"})


# ── ALLOCATION — written only by `app/slot_allocation.py` ──────────────────────────────────
# A slot's kind and the order to destroy it: the assignment model's decisions, never sensed, never an
# occupancy edge. Two statements (`_retire_statement`, `_kind_statement`), each naming one member — a subset
# rule like `OCCUPANCY`'s, whose union equals the family. The declare INSERT sets `kind` too; INSERTs are not
# statement families.
ALLOCATION = frozenset({"kind", "retiring_at"})

# ── DESTROY_ACK — the node's refusal of a `destroy_slot`, written only by that verb's ack handler ─────────
# Its own family: the node's words about one destroy, not an allocation decision. One statement,
# `app/slot_destroy.py::_destroy_error_statement`.
DESTROY_ACK = frozenset({"destroy_error"})


# ── ORG — the tenancy column, written at INSERT and by no UPDATE of ours ─────────────────────────────
# Written on row creation by `app/slot_allocation.py::converge` and the write seam's
# `before_flush` stamp; `lease`/`claim_for_user` only compare it. A slot never changes tenant. Not
# `DB_OWNED` (no default). Reclassify it if an UPDATE ever assigns it. Named `ORG`, not `TENANCY`, to
# avoid colliding with `app.tenancy.manifest.TENANCY`.
ORG = frozenset({"org_id"})

# ── SLOT_KIND — written at INSERT only, by `app/slot_allocation.py::_add_row` ──────────────
# Git worktree or empty folder, copied from the project at declare and named by no UPDATE: a held slot's kind
# is what its node built, so it never follows a repository later connected or removed.
SLOT_KIND = frozenset({"has_repo"})


def unclassified(reflected_columns: frozenset[str]) -> frozenset[str]:
    """Columns of `worktrees` no set above claims. Non-empty means a new column arrived without an
    owner."""
    return (
        frozenset(reflected_columns)
        - SENSED
        - DECIDED
        - OCCUPANCY
        - MUTATED
        - JOINED
        - SESSION_RUN
        - DB_OWNED
        - DECLARATION
        - ORG
        - ALLOCATION
        - DESTROY_ACK
        - SLOT_KIND
    )
