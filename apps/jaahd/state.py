"""jaahd's observation payload schema version. No I/O.

Node and slot state live in Postgres, written by `POST /api/agent/fleet/observe`
(`reconcile.observe.post_observation`).
"""
from __future__ import annotations

# Observation payload schema version; `main.py`'s BUS_SCHEMA_VERSION reads it from here.
SCHEMA_VERSION = 1
