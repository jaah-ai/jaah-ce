"""The node's half of "is this frame really from the control plane" — the CP/node double guard.

Every CP→node frame family (`commands.py`, `console.py`, `console_direct.py`) carries one credential both
sides derive and neither is told (the CP stores only `sha256(token)`, see `apps/api/app/node_credentials.py`):

    cp_frame_proof = HMAC-SHA256(key = sha256(NODE_TOKEN), msg = "jaah-cp-frame-v1")

A captured frame hands the reader nothing that authenticates as this node. An unconfigured node (no
`NODE_TOKEN`) refuses every frame unless `env_helpers.insecure_agent_enabled` (an allowlist) opts in;
two empty strings never match. The label is versioned in the string so a derivation change is a
different value — a mismatch reads as "refused", never "accidentally still equal".
"""
from __future__ import annotations

import hashlib
import hmac
import re
from typing import Any, Callable

import env_helpers

# Must equal `apps/api/app/node_credentials.py::_CP_FRAME_LABEL` (a wire constant across the CP/node
# boundary); `test_node_frame_auth.py` asserts the two agree.
_CP_FRAME_LABEL = b"jaah-cp-frame-v1"

# The config-snapshot key this daemon publishes, and the value that means "verifies the current label": a CP of
# the previous release signs with `_CP_FRAME_LABEL` only for a node that reports it. Dropped once no CP of that
# release can run (a rolling deploy or a rollback).
WIRE_LABELS_SNAPSHOT_KEY = "wire_labels"
CURRENT_WIRE_LABELS = "jaah"

PROOF_FIELD = "cp_frame_proof"

# `hmac.hexdigest()` shape, checked first so any other value (a lone surrogate would raise on `.encode()`)
# is a plain refusal rather than an exception.
_PROOF_RE = re.compile(r"[0-9a-f]{64}")


def cp_frame_proof(node_token: str) -> str:
    """This node's expected `cp_frame_proof`, derived from its own plaintext token.

    `""` for an empty token — `frame_auth_ok` treats that as "unconfigured" and answers with the
    insecure opt-in, never with an accidental match against another empty string.
    """
    if not node_token:
        return ""
    token_hash = hashlib.sha256(node_token.encode("utf-8")).hexdigest()
    return hmac.new(token_hash.encode("utf-8"), _CP_FRAME_LABEL, hashlib.sha256).hexdigest()


def frame_auth_ok(frame: Any, *, expected_token: Callable[[], str]) -> bool:
    """Is this CP→node frame authenticated by its `cp_frame_proof`?

    `expected_token` is resolved at call time, so a rotation takes effect without a daemon restart.
    """
    if not isinstance(frame, dict):
        return False
    expected = expected_token()
    if not expected:
        return env_helpers.insecure_agent_enabled()
    presented = frame.get(PROOF_FIELD)
    if not isinstance(presented, str) or not _PROOF_RE.fullmatch(presented):
        return False
    return hmac.compare_digest(presented.encode("ascii"), cp_frame_proof(expected).encode("ascii"))
