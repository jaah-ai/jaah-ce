#!/usr/bin/env python3
"""Project → default-model lookup for this node, over the project roster (`ctl.py card-model`).

The DB column is the single source; the dispatch composer reads it too, and neither side caches it.

The roster is read over the local vocabulary socket (`projects.list`) because this runs as user `jaah`,
who has no NODE_TOKEN; see `_roster` for which failure may fall back to a credential.
"Could not find out" is not "no such project" — see `RosterUnreadable`.

Leaf module: stdlib + `agent_socket` + `control_plane_api` only. `ctl.match_project_row` is not imported
(ctl is daemon-space; the arrow points one way), so its two-line exact match is repeated.
"""
from __future__ import annotations

import agent_socket
import control_plane_api


class RosterUnreadable(RuntimeError):
    """The project roster could not be read — distinct from "no such project" (`""`).

    Raised, not returned: `""` is the permissive branch in every consumer, so an unreadable roster must
    be an exception a caller has to catch visibly to proceed."""


def _roster() -> list:
    """This node's view of `GET /api/projects`, or raise `RosterUnreadable`. Never `[]` for a failure —
    `[]` means the control plane answered and there are no projects.

    Three-way split, the same as the control-plane client's socket path:
      * holding NODE_TOKEN → direct HTTP; the daemon never calls its own socket.
      * `AgentSocketUnavailable` → fall back to the credential path (no policy decision was made).
      * `AgentSocketRefused` → `RosterUnreadable`, never a fallback: retrying with a credential would
        perform the very call the vocabulary declined."""
    if control_plane_api.NODE_TOKEN:
        return _roster_via_credential()
    try:
        rows = agent_socket.call("projects.list")
    except agent_socket.AgentSocketUnavailable as exc:
        return _roster_via_credential(socket_note=str(exc))
    except agent_socket.AgentSocketRefused as exc:
        raise RosterUnreadable(
            f"the node daemon refused `projects.list` ({exc.reason}) — NOT retried with a credential, by "
            f"design; the project roster is unknown here"
        ) from exc
    if not isinstance(rows, list):
        raise RosterUnreadable(
            f"the projects.list verb answered {type(rows).__name__}, not a list — the project roster "
            f"could not be read"
        )
    return rows


def _roster_via_credential(socket_note: str = "") -> list:
    """The roster over the daemon's own token-carrying HTTP client, or `RosterUnreadable`.

    Renamed rather than left to escape: a bare traceback reaches `ctl.py`'s consumers as an empty stdout
    line, the permissive answer. `HTTPError` ⊂ `URLError` ⊂ `OSError`; `ValueError` is a malformed body."""
    try:
        return control_plane_api._agent_api_get_list("/api/projects")
    except (OSError, ValueError) as exc:
        note = f" (after the node daemon was unreachable: {socket_note})" if socket_note else ""
        raise RosterUnreadable(
            f"the project roster could not be read from the control plane{note}: {exc}"
        ) from exc


def _card_default_model(project: str) -> str:
    """The project's `default_model` from the roster.

    `""` = the roster answered with no such project or no default (caller inherits the account's model);
    `RosterUnreadable` = it could not be read. Exact, case-sensitive name match, first row wins."""
    for proj in _roster():
        if isinstance(proj, dict) and proj.get("name") == project:
            return proj.get("default_model") or ""
    return ""
