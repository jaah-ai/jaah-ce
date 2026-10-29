"""Writing into a task's LIVE runner pane: the one lookup of "which session is that", the one terminal-safe
scrub every typed text goes through, and the `deliver_message` mint the merge lane calls.

Two CP writers type into a runner's pane: `routes/tasks/answers.py::_deliver_answer_to_live_runner` (a human's gate
answer, `deliver_answer`) and `deliver_message_to_live_runner` here (a CP-composed message to a WORKING
runner, `deliver_message`). Every `deliver_message` text carries `CONTROL_PLANE_LABEL`, so the runner and the
operator watching its console can tell it from the operator's own typing. Both address the pane through `live_runner_hold`, so the identity rule — the
task's `state='task'` hold and the session the node said it LAUNCHED — exists once. No live session means
nothing is minted: a pane write never falls back to a guess, and the merge lane never dispatches a runner.
"""
from __future__ import annotations

import logging
import unicodedata
from typing import Any, Optional

from app import fleet_verdicts, slot_state
from app.events import emit_console_event

log = logging.getLogger("cockpit-live-runner")

# These are the only controls surviving `scrub_control_chars`: the wire is a tmux bracketed paste
# (`apps/jaahd/answer_delivery.py`), whose only escape is `ESC` (scrubbed), so newline and tab are safe and
# keep paragraphs.
WIRE_SAFE_CONTROL_CHARS = frozenset("\n\t")

# The source label every CP-composed pane message starts with. The runner charter tells a runner
# what a labelled message may ask of it; the web console badges it (`apps/web/src/components/consoleLabels.ts`
# mirrors this literal, pinned by a parity test).
CONTROL_PLANE_LABEL = "[control plane]"


def scrub_control_chars(value: str) -> str:
    """Text with every C0/C1 control, DEL, bidi override and zero-width formatter removed EXCEPT the two in
    `WIRE_SAFE_CONTROL_CHARS` — what any text is put through before it can reach a runner's terminal.

    A keystroke-injection fix: an embedded `ESC` would end the bracketed paste and hand the rest to the
    TUI as keys. Both halves are untrusted. Classified by Unicode category, not `ch < " "` (C1 controls
    and U+202E would pass). No normalization here (NFKC expands).

    Whitespace is not collapsed, so answers keep their lines; `\\r\\n` and lone `\\r` fold to `\\n` first so
    a CRLF answer keeps its breaks."""
    text = (value or "").replace("\r\n", "\n").replace("\r", "\n")
    return "".join(
        ch if ch in WIRE_SAFE_CONTROL_CHARS
        else (" " if unicodedata.category(ch) in ("Cc", "Cf") else ch)
        for ch in text
    )


def live_runner_hold(session, task_id: object) -> Optional[dict[str, Any]]:
    """The task's hold when a pane write can be ADDRESSED to it, else None: `slot_state.live_hold_for_task`
    (its `state='task'` hold with a launched session) narrowed to one that names both its session and its
    node. An unidentified session is the injection hazard the node's fence exists for, so it is never a
    target. The single lookup both pane writers use."""
    hold = slot_state.live_hold_for_task(session, task_id)
    if hold is None or not hold["session_id"] or not hold["node_id"]:
        return None
    return hold


def deliver_message_to_live_runner(session, task_id: int, text: str, *, sender: str) -> bool:
    """Mint a `deliver_message` intent typing TEXT, prefixed with `CONTROL_PLANE_LABEL`, into task TASK_ID's
    live runner pane. True when minted. SENDER is the short name of the CP part speaking ("Merge lane"); it
    rides the payload for the task page's message list (`routes/task_control_messages.py`), never the pane.

    False — and nothing minted — when the task has no live, identified runner session or the text is blank;
    the caller keeps the message wherever it already recorded it (the merge lane: the PR comment and the
    queue row) and dispatches nothing. Not bound to any task state: the recipient is a working runner.
    `issue_intents` commits and pushes, so the message does not wait for the node's next pool tick.
    Frame: `fleet_verdicts.build_deliver_message_intent`."""
    clean = scrub_control_chars(text)
    if not clean.strip():
        return False
    hold = live_runner_hold(session, task_id)
    if hold is None:
        return False
    intent = fleet_verdicts.build_deliver_message_intent(
        hold["project_id"], hold["project"], hold["slot"],
        task_id=task_id, session_id=hold["session_id"], text=f"{CONTROL_PLANE_LABEL} {clean}", sender=sender,
    )
    # Rides `issue_intents`' commit: the task page's message list re-reads (`routes/task_control_messages.py`).
    # The live `task` event a pane message's mint (and its delivery, `routes/agent/slot_intents.py`) emits — not a transition,
    # so no activity row.
    emit_console_event(session, entity="task", id=task_id, action="control_message", record_transition=False)
    fleet_verdicts.issue_intents(session, hold["node_id"], [intent])
    log.info(
        "task %s: minted deliver_message for %s#%s session %s",
        task_id, hold["project"], hold["slot"], hold["session_id"][:8],
    )
    return True
