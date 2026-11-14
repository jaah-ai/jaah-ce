"""Pipeline gates and deploy targets: each stage's ordered gate lists —
`before` (the step into the stage) and `after` (verify on arrival) — and its deploy target.

- `schema.py` — what a gate's and a target's config may hold, the save's refusals, the one-line summary.
- `defaults.py` — every stage's default gates and target (D14), the one home the migration mirrors.
- `full_gate.py` — the one control for the full gate: the merge step's list, its command, the lane certificate.
- `results.py` — gate runs, the node driver's events, the results read.

The node gate driver runs the lists; the CP stores them and hands them out inside the work it already
serves (the merge lane's batch, the release executor's `release.next`), to a caller announcing the `gates`
capability (`caps`).
"""
