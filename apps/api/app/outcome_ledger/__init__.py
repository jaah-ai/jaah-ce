"""The outcome ledger: one `task_outcomes` row per pipeline dispatch, CP-written, judged over a survival window.

`states.py` holds the state machine, `writer.py` is the only writer (every dispatch, attempt-end, settle, merge,
push and operator seam calls it), `dimensions.py` the derived dimensions, `tick.py` the 15-minute cron.
`metrics.py` is the read side (the seven metrics, Wilson intervals, strata, ranking, the cross-org floor), served by
`app/routes/outcomes.py`; `benchmark.py` is the Bradley-Terry ratings seam; `security.py` decides which CI checks are security scans. Models:
`app/models/outcomes.py`. Policy: the fleet task lifecycle policy, §"Outcome ledger".
"""
