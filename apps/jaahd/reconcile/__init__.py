"""The node's sensor and intent executor for the worktree pool.

Senses ground truth (git, `claude agents`, docker, tmux, the console bridge) and publishes one record per
slot via `POST /api/agent/fleet/observe`, sensed fields only; executes the CP's slot intents, each
re-verified against ground truth. Every verdict (holder, task, idle, stack) is the CP's (`fleet_verdicts.py`).

R1: one representation of "not observed" — per-probe `ok` flags, from which the encoder derives the nulls
(`observe._ok`), so unknown never overwrites or frees. R3: an intent carries its observation revision and
preconditions re-verified against a fresh probe. Per-tick order FETCH → EXECUTE → PROBE → PUBLISH
(publishing first would invalidate every pending intent). Every kill is a CP-requested verb behind
`kill_gates.guarded_window_kill`. This is the only slot-intent executor and ack path in the package.

Module map (import direction runs down the list; a module reaches a lower one by `from reconcile import <module>`
and calls `<module>.<name>`, so a test patches a name on the module that defines it):
  vocab          — wire constants, probe budgets, the subprocess/clock/container primitives
  tick_wire      — the pool-tick wire model (`RosterSlot`, `SlotIntent`, `PoolTick`, ...), parsers, tick fetch
  accounts_probe — the account roster read and the cross-tick `claude agents` cache (`AgentsProbeCache`)
  context        — `ReconcileContext` and the per-slot git/stack/tmux/session sensing
  observe        — `observe_slot` and the observation POST payload
  enact_reset    — `reset_to_base`, `stop_stack`, `release_slot` and the shared `_clear_tree` teardown
  enact_launch   — `lease_slot`: build or resume the tree and launch the runner (`_launch_into_slot`)
  enact_signal   — `kill_runner`, `deliver_answer`/`deliver_message`, `runtime_control`, `erase_tree`,
                   `destroy_slot`
  execute        — `execute_intents`: the verb→executor table, slot locks, R3 guards, the one ack
  orchestrate    — `reconcile_pass`, `launch_pass`, `drain_pushed_intents`, slot materialisation

This file holds no names: import the module that owns one."""
