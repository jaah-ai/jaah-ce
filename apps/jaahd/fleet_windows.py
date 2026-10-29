"""Fleet-session window enumeration — the read behind the CP's `GET /api/console/nodes/{node}/windows`.

A native read, not a mutation command. `stdout` carries `{"windows": [{"id", "index", "name"}]}`.
`fleet_tmux_server.HOLDER_WINDOW` is excluded (read from that constant): one keystroke in it ends its
`sleep` and collapses the session. `#{window_id}` is the identity; the index is display only
(`renumber-windows` is on). Scope is `-t =<this node's session>`, never `list-windows -a`."""
from __future__ import annotations

import json

import fleet_launch   # tmux_session(): the single home for the node's default-session fallback chain
import session_user   # which tmux server a client call must name (`-S`), post uid switch
from commands import native_command
from env_helpers import _tmux_safe_env, NoNodeIdentity
from fleet_tmux_server import HOLDER_WINDOW

# TAB-separated: window names contain `|` and spaces; the parse splits on the first two tabs only.
_LIST_FORMAT = "#{window_id}\t#{window_index}\t#{window_name}"


def parse_windows(stdout: str) -> list[dict]:
    """`list-windows -F _LIST_FORMAT` output → the wire rows, `_holder` removed, sorted by window index.

    An unparseable line is dropped, not raised on: one bad row degrades to a missing tab."""
    rows: list[dict] = []
    for line in stdout.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t", 2)
        if len(parts) != 3:
            continue
        window_id, raw_index, name = parts
        if name == HOLDER_WINDOW:
            continue
        try:
            index = int(raw_index)
        except ValueError:
            continue
        rows.append({"id": window_id, "index": index, "name": name})
    return sorted(rows, key=lambda row: row["index"])


@native_command("fleet_windows")
def fleet_windows(command_type: str, args: dict, run) -> dict:
    """List this node's tmux session's windows; one `list-windows`, no tmux state mutated.

    A failing tmux call is returned verbatim, never flattened into an empty list ("no windows")."""
    socket = session_user.fleet_socket()
    try:
        session = fleet_launch.tmux_session()
    except NoNodeIdentity as exc:
        # Same shape as a failing tmux call, so it is not read as "no windows".
        return {"exit_code": 1, "stdout": "", "stderr": str(exc)}
    listing = run(
        *session_user.tmux_cmd(socket, "list-windows", "-t", f"={session}", "-F", _LIST_FORMAT),
        env=_tmux_safe_env(),
    )
    if listing.get("exit_code") != 0:
        return listing
    windows = parse_windows(listing.get("stdout") or "")
    return {
        "exit_code": 0,
        "stdout": json.dumps({"windows": windows}),
        "stderr": listing.get("stderr", ""),
    }
