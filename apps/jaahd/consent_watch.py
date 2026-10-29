"""Accept a startup consent dialog in a freshly launched runner's pane — the runtime backstop for consents
the launch path pre-seeds (folder trust, managed settings, bypass mode, API key, project MCP servers).

A missed seed parks an unattended runner on a dialog forever: no session registers, no Task is claimed,
and the CP relaunches it into the same dialog. So from launch until the CLI's first composer this reads
the pane, and on a dialog its adapter names (`ConsentDialog`) it ACCEPTS it, logging a warning because a
seed missed.

Accepting is moving the cursor to the named accept row, one key at a time with a re-read between, and
`Enter` only once that row is the highlighted one — never a bare `Enter`: several dialogs highlight their
REJECT row by default ("No, exit"), and an `Enter` there ends the runner. The one exception is a dialog
measured to open focused on its accept row (`ConsentDialog.enter_accepts`): drawn without its rows, it gets
`Enter` alone. The highlighted row is the one led by `❯` and a plain space; the composer's `❯` is followed
by NBSP (`answer_delivery`).

Addressing and capture are `answer_delivery`'s: a tmux pane by its stable window id, proven by cwd
(`watch_launch`), or a MicroVM session's console through `jaah-microvmd`, proven by the VM registry
(`watch_vm`). Nothing raises: a worker thread that raises takes the log with it.
"""
from __future__ import annotations

import logging
import re
import subprocess
import time
from typing import TYPE_CHECKING

import answer_delivery

if TYPE_CHECKING:  # the descriptor is the ADAPTER's type; importing it at runtime would be a cycle
    from agents import ConsentDialog

log = logging.getLogger("jaahd.consent-watch")

__all__ = ["next_key", "open_dialog", "watch_launch", "watch_pane", "watch_vm"]

# Launch to first composer: consents appear at startup, before the first turn. Bounded like
# `answer_delivery._LAUNCH_READY_SECS`, with headroom for a cold start that also shows dialogs.
_WATCH_SECS = 120.0
# A MicroVM launch lands while the guest may still be finishing cloud-init and waiting up to 30 s for its
# Docker engine (`microvm_seed.JAAH_SESSION`) before the CLI even starts.
_VM_WATCH_SECS = 300.0
_POLL_SECS = 1.0
# Re-read after each key: the cursor moves, or the dialog closes, on the next paint.
_KEY_SETTLE_SECS = 0.7
# Every key sent across the watch. Six dialogs at a few rows each fit well inside; a screen that keeps
# eating keys without closing is not one this understands, so it stops rather than typing on.
_MAX_KEYS = 40
# After `Enter`, how long to wait for the dialog to close. Any change to the screen in that time means
# the `Enter` was taken — the next dialog may already be up, highlighting its own "No, exit", so a second
# `Enter` is sent ONLY when the screen stayed byte-identical throughout (`answer_delivery._submit_on`'s rule).
_CLOSE_SECS = 3.0
# `Enter`s on one unchanged dialog before it counts as refusing the answer.
_MAX_ENTERS_PER_DIALOG = 2
# Unreadable captures in a row before the pane counts as gone.
_MAX_BLANK_CAPTURES = 5
# The highlighted row's lead: the glyph and a PLAIN space (the composer's is NBSP).
_CURSOR = "❯ "
# An option row's number, as Claude Code 2.1.300 draws it ("❯ 1. Yes, I trust these settings").
_ROW_NUMBER = re.compile(r"\A\d+\.\s+")


def open_dialog(screen: str, dialogs: "tuple[ConsentDialog, ...]") -> "ConsentDialog | None":
    """The consent dialog live on SCREEN, or None. The live one is the signature appearing LAST whose
    accept row is rendered below it — an earlier dialog's text left above it is not live. An
    `enter_accepts` dialog needs no row, only that nothing follows it: no composer, and no other dialog's
    signature (a rowless dialog below it may open on "No, exit")."""
    best, at = None, -1
    for dialog in dialogs:
        pos = screen.rfind(dialog.signature)
        if pos > at and (_row_index(_rows_after(screen, pos), dialog.accept) is not None
                         or (dialog.enter_accepts and _nothing_after(screen, pos, dialog, dialogs))):
            best, at = dialog, pos
    return best


def _nothing_after(screen: str, pos: int, dialog: "ConsentDialog", dialogs: "tuple[ConsentDialog, ...]") -> bool:
    """Is DIALOG's signature at POS the last thing SCREEN shows of any dialog or the composer?"""
    return (not answer_delivery.composer_prompt_present(screen[pos:])
            and all(screen.rfind(other.signature) < pos for other in dialogs if other is not dialog))


def next_key(screen: str, dialog: "ConsentDialog") -> str | None:
    """The one key that moves DIALOG toward accepted: `Enter` when the accept row is highlighted, else
    `Up`/`Down` toward it. None unless exactly one highlighted row and the accept row can be read — never
    a guess. An `enter_accepts` dialog drawn with no rows at all gets `Enter`: it opens on its accept row."""
    pos = screen.rfind(dialog.signature)
    if pos == -1:
        return None
    rows = _rows_after(screen, pos)
    target = _row_index(rows, dialog.accept)
    cursors = [i for i, row in enumerate(rows) if row.lstrip().startswith(_CURSOR)]
    if dialog.enter_accepts and target is None and not cursors:
        return "Enter"
    # Exactly one: a second cursor-shaped row (repo text, a crafted server name) makes the screen ambiguous.
    if target is None or len(cursors) != 1:
        return None
    cursor = cursors[0]
    if target == cursor:
        return "Enter"
    return "Down" if target > cursor else "Up"


def _rows_after(screen: str, pos: int) -> list[str]:
    """The screen's lines from the one holding position POS down."""
    start = screen.rfind("\n", 0, pos) + 1
    return screen[start:].split("\n")


def _row_index(rows: list[str], label: str) -> int | None:
    """Index of the row whose whole text, cursor glyph and option number stripped, is LABEL. Whole-row, so
    a short label ("Yes") never matches inside prose."""
    for i, row in enumerate(rows):
        text = row.strip()
        if text.startswith("❯"):
            text = text[1:].strip()
        if _ROW_NUMBER.sub("", text) == label:
            return i
    return None


def watch_pane(pane, dialogs: "tuple[ConsentDialog, ...]", *, label: str, task_id: object = "",
               sleep_fn=time.sleep, clock=time.monotonic, timeout: float = _WATCH_SECS,
               max_blanks: int | None = _MAX_BLANK_CAPTURES) -> list[str]:
    """Accept every consent dialog PANE shows until its first composer, the deadline, or the pane goes.
    Returns the names of the dialogs accepted. PANE is `answer_delivery`'s target (`capture`, `key`).
    MAX_BLANKS None: a blank screen never means the pane is gone (a console that has not painted yet).

    No key is sent on a screen still settling: the cursor is read off two identical captures, so an arrow
    key still in flight cannot move it off the accept row between the read and the `Enter`."""
    accepted: list[str] = []
    enters: dict[str, int] = {}
    keys = blanks = 0
    deadline = clock() + timeout
    previous = None
    while clock() < deadline:
        screen = pane.capture()
        if not screen:
            blanks += 1
            if max_blanks is not None and blanks >= max_blanks:
                return accepted
            sleep_fn(_POLL_SECS)
            continue
        blanks = 0
        dialog = open_dialog(screen, dialogs)
        if dialog is None:
            if answer_delivery.composer_prompt_present(screen):
                return accepted
            previous = None
            sleep_fn(_POLL_SECS)
            continue
        key = next_key(screen, dialog)
        if key is None or screen != previous:  # unreadable, or not yet settled: look again, never guess
            previous = screen
            sleep_fn(_KEY_SETTLE_SECS)
            continue
        previous = None
        if keys >= _MAX_KEYS or enters.get(dialog.name, 0) >= _MAX_ENTERS_PER_DIALOG:
            log.warning("consent-watch: task %s runner in %s is still on the %r consent after %d keys — "
                        "leaving it for a human", task_id, label, dialog.name, keys)
            return accepted
        if not pane.key(key):
            return accepted
        keys += 1
        if key != "Enter":
            sleep_fn(_KEY_SETTLE_SECS)
            continue
        enters[dialog.name] = enters.get(dialog.name, 0) + 1
        if _closed(pane, screen, sleep_fn, clock):
            # The pane text is never logged: the API-key dialog prints part of the key.
            log.warning("consent-watch: task %s runner in %s stopped on the %r consent — accepted it; the "
                        "pre-seeded consent missed", task_id, label, dialog.name)
            accepted.append(dialog.name)
    log.info("consent-watch: task %s runner in %s showed no composer within %.0fs; watch ended",
             task_id, label, timeout)
    return accepted


def _closed(pane, screen: str, sleep_fn, clock) -> bool:
    """Did the `Enter` just sent on SCREEN land? True as soon as the pane shows anything else; False only
    when it stayed byte-identical for `_CLOSE_SECS` — the one case where re-sending is safe."""
    by = clock() + _CLOSE_SECS
    while clock() < by:
        sleep_fn(_KEY_SETTLE_SECS)
        if pane.capture() != screen:
            return True
    return False


def watch_launch(tmux_target: str, dialogs: "tuple[ConsentDialog, ...]", *, expect_cwd: str,
                 task_id: object = "", run_fn=subprocess.run, sleep_fn=time.sleep,
                 timeout: float = _WATCH_SECS) -> list[str]:
    """`watch_pane` the freshly launched runner in TMUX_TARGET, once it is proven to be the slot's window.
    For a launch thread: never raises."""
    if not dialogs:
        return []
    try:
        window = answer_delivery._proven_window(tmux_target, expect_cwd, run_fn, "consent-watch")
        if window is None:
            return []
        pane = answer_delivery._TmuxTarget(*window, run_fn)
        return watch_pane(pane, dialogs, label=tmux_target, task_id=task_id, sleep_fn=sleep_fn,
                          timeout=timeout)
    except Exception as exc:  # noqa: BLE001 — a worker thread that raises takes the log with it
        log.warning("consent-watch: task %s watch of %s raised (%s: %s)", task_id, tmux_target,
                    type(exc).__name__, exc)
        return []


def watch_vm(vm_id: str, dialogs: "tuple[ConsentDialog, ...]", *, task_id: object, session_id: object,
             request=None, open_stream=None, sleep_fn=time.sleep, timeout: float = _VM_WATCH_SECS) -> list[str]:
    """`watch_pane` a MicroVM session's console (`answer_delivery.PtyTarget`), once the registry proves VM_ID
    is this task's and session's and its supervisor reports it running. For a launch thread: never raises."""
    if not dialogs:
        return []
    accepted: list[str] = []
    try:
        if not answer_delivery._proven_vm(vm_id, task_id=task_id, session_id=session_id, kind="consent-watch",
                                          request=request):
            return []
        answer_delivery._on_vm(vm_id, "consent-watch", request, open_stream, lambda pane: accepted.extend(
            watch_pane(pane, dialogs, label=pane.label, task_id=task_id, sleep_fn=sleep_fn, timeout=timeout,
                       max_blanks=None)) or True)
    except Exception as exc:  # noqa: BLE001 — a worker thread that raises takes the log with it
        log.warning("consent-watch: task %s watch of VM %s raised (%s: %s)", task_id, vm_id,
                    type(exc).__name__, exc)
    return accepted
