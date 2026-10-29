"""The run mode reported on an ACP session — reported, never enforced.

Every launch is `full-access` (the worktree, the push guard and the node fence are the boundary, not an
agent-side approval mode), so this module only names it. Nothing here gates a tool call.
"""
from __future__ import annotations

LAUNCH_MODE = "full-access"
