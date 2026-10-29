"""Write a human's gate answer into a live runner's tmux pane — the MECHANICS half of `deliver_answer`.

The CP decides whether and to which session (`app/fleet_verdicts.py::build_deliver_answer_intent`); this
knows only HOW. A runner in `AskUserQuestion` blocks until a keystroke reaches its pane, so writing the
answer there keeps the warm session instead of a cold re-dispatch.

Delivery is `Esc`, then the answer as an ordinary message — never a picker selection (measured): a digit
falls through and the tool returns the HIGHLIGHTED DEFAULT as the human's answer; `Down`xN is index
arithmetic over rows this cannot see. `Esc` yields an honest "User declined to answer questions", is a
no-op if the picker already closed, and cannot fabricate an answer in any ordering. The one exception is a
CLI whose dialog has its own free-text row (`ComposerTui.write_in_row`, declared per adapter): there the
answer is typed INTO that row, its key read off the live screen, and returns as the tool's own result
(`_write_in_on`) — still the operator's text, never a choice among the options.

The payload is a bracketed paste from STDIN (`load-buffer -` + `paste-buffer -d -p -r`), never a
`send-keys -l` argv: one argv string is capped at `MAX_ARG_STRLEN` (~131 KB, `E2BIG`) and a literal newline
there SUBMITS. `-p` brackets (only if the app requested the mode — hence `_submit_on`'s composer gate); `-r`
stops tmux rewriting LF to CR; `-d` drops the buffer after the paste.
An `Enter` after a large paste can be absorbed into the paste chunk (a reader-side race no sleep fixes),
so `_submit_on` verifies and re-sends `Enter` only while the composer POSITIVELY holds our own payload.
The composer prompt is `❯` + NBSP; a submitted scrollback message is `❯` + a plain space.

Addressing: stable `#{window_id}` plus a cwd identity check, reusing `kill_gates`' helpers (with
`renumber-windows` on, `session:index` names a different window after churn). Nothing raises; False means
"the write did not happen" and the caller re-serves the intent. A QUEUED answer, or one still on the
composer after every re-send, counts as delivered — re-delivering would flood the session.

A MicroVM session has no tmux: `deliver_vm_answer`/`deliver_vm_message` run the same sequence on its console
through `jaah-microvmd` (`PtyTarget`), the VM proven by the registry instead of a cwd (`_proven_vm`).
"""
from __future__ import annotations

import base64
import itertools
import logging
import os
import re
import subprocess
import time
from typing import TYPE_CHECKING

import cloud_modules
import env_helpers
import kill_gates
import session_user

if TYPE_CHECKING:  # the descriptor is the ADAPTER's type; importing it at runtime would be a cycle
    from agents import ComposerTui
from fleet_tmux_server import HOLDER_WINDOW

log = logging.getLogger("jaahd.answer-delivery")

__all__ = ["PtyTarget", "compose_answer_message", "composer_prompt_present", "composer_text",
           "deliver_answer", "deliver_message", "deliver_task_prompt", "deliver_vm_answer",
           "deliver_vm_message", "pending_is_unsubmitted"]

# Verify-and-re-send rounds after the first `Enter`, and their pause. Bounded: the CP re-serving a
# `failed` intent is the real retry loop, and an unbounded one here would stall the 15s drain.
_SUBMIT_ATTEMPTS = 4
_SUBMIT_SETTLE_SECS = 1.5
# Polls waiting for the paste to APPEAR before `Enter` — an `Enter` before render is the absorbed one.
_LANDED_POLLS = 8
_LANDED_POLL_SECS = 0.5
# The glyph alone: the composer is always its LAST occurrence (composer: NBSP after; scrollback: space).
_PROMPT_MARKER = "❯"
# The composer's exact prompt (glyph + NBSP): proof a composer, not a modal, is on screen, and the gate on
# every re-sent `Enter`. Without it, a modal hiding the composer lets a scrollback copy of an earlier
# answer read as "pending", and the `Enter` would select the open picker's highlighted row.
_COMPOSER_PROMPT = "❯\u00a0"
# Where the composer region ends: the box rule tmux captures under it. Below that is the status footer.
_BOX_RULE = "─"
# Claude Code collapses a pasted payload to this placeholder; the `+N lines` half is conditional.
_PASTE_PLACEHOLDER_RE = re.compile(r"\[Pasted text #\d+(?: \+\d+ lines)?\]")
# 40 characters fits on one captured row at any realistic width, so wrapping cannot split it.
_FRAGMENT_CHARS = 40
# A CP message's leading source label (`[control plane] `, `apps/api/app/live_runner.py`): the same on every
# message, so the head fragment is taken AFTER it — else an earlier notice in the scrollback matches.
_SOURCE_LABEL_RE = re.compile(r"^\[[a-z ]+\] ")
# A recognition probe, not a transcript.
_COMPOSER_READ_CHARS = 2000
# Per-delivery buffer serial. `itertools.count`'s `next()` is atomic under the GIL, and deliveries run on
# worker threads — see `_paste`.
_BUFFER_SEQ = itertools.count()
# Every delivery buffer's prefix — what `_reap_orphan_answer_buffers` sweeps on.
_BUFFER_PREFIX = "jaahd-answer-"


def compose_answer_message(question: str, answer: str) -> str:
    """The message text delivered into the pane.

    Names the question: nothing guarantees the operator answered the round the session is blocked on,
    and a bare `Green` is not self-describing. The ANSWER goes over untouched (no truncation, no
    flattening). The agent-authored QUESTION is flattened to one line as a frame guard — a newline could
    end the quoted frame and stage a human-attributed instruction (the CP defangs it too, in
    `routes/tasks._deliver_answer_to_live_runner`)."""
    flat_q = " ".join((question or "").split())
    body = answer or ""
    if flat_q:
        return f"Answer to your question '{flat_q}': {body}"
    return f"Answer: {body}"


def composer_text(pane_text: str) -> str:
    """What is sitting UNSUBMITTED on the composer of a captured pane, or "" when nothing is.

    Reads the region after the LAST `❯` (earlier ones are submitted scrollback), skipping any whitespace
    after the glyph, up to the box rule. Rows are joined with a space: a recognition probe for
    `pending_is_unsubmitted`, not a faithful transcript."""
    idx = pane_text.rfind(_PROMPT_MARKER)
    if idx == -1:
        return ""
    tail = pane_text[idx + len(_PROMPT_MARKER):].lstrip(" \u00a0")
    rows: list[str] = []
    for row in tail.split("\n"):
        if row.lstrip().startswith(_BOX_RULE):
            break
        rows.append(row.strip())
    return " ".join(row for row in rows if row)[:_COMPOSER_READ_CHARS]


def composer_prompt_present(pane_text: str) -> bool:
    """Is a real composer on screen — as opposed to a modal covering it?

    True only for the exact glyph + NBSP spelling. A TUI variant using a plain space reads as "no
    composer", costing only the re-send — the safe direction."""
    return _COMPOSER_PROMPT in pane_text


def pending_is_unsubmitted(pending: str, message: str) -> bool:
    """Is PENDING our own MESSAGE still sitting untaken on the composer — the one state an `Enter` fixes?

    Positive identification is what makes the re-send safe: a `[Pasted text #N]` placeholder, or the head
    or tail of our message, is ours. Anything else — the `Press up to edit queued messages` hint (accepted,
    will submit itself) or a human's half-typed line — is not, and an `Enter` there would duplicate an
    answer or submit someone's unfinished thought. Messages under 8 chars never match."""
    if not pending or not message:
        return False
    if _PASTE_PLACEHOLDER_RE.search(pending):
        return True
    flat = " ".join(message.split())
    if len(flat) < 8:
        return False
    squeezed = " ".join(pending.split())
    return flat[:_FRAGMENT_CHARS] in squeezed or flat[-_FRAGMENT_CHARS:] in squeezed


def _capture(socket: str, target: str, run_fn) -> str:
    """The visible pane text of TARGET, or "" when it cannot be read."""
    try:
        probe = run_fn(
            session_user.tmux_cmd(socket, "capture-pane", "-p", "-t", target),
            capture_output=True, text=True, timeout=10, env=env_helpers._tmux_safe_env(),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("answer-delivery: capture-pane for %s on %r failed (%s)", target, socket, exc)
        return ""
    if getattr(probe, "returncode", 1) != 0:
        return ""
    return getattr(probe, "stdout", "") or ""


def pane_text_for_pid(pane_pid: int, run_fn=subprocess.run) -> str | None:
    """The visible text of the pane whose process is `pane_pid` AS TMUX NAMES IT (`#{pane_pid}`; a runner's
    `RunnerIdentity.tmux_pane_pid` — the MSYS pid on Windows, never the Windows one), or None when no
    reachable server has that pane or it cannot be read — a read-only probe, never a write. The pid is
    tmux's own answer at launch, so no operator-typed window name is resolved."""
    for socket in kill_gates._reachable_sockets():
        try:
            probe = run_fn(
                session_user.tmux_cmd(socket, "list-panes", "-a", "-F", "#{pane_pid} #{pane_id}"),
                capture_output=True, text=True, timeout=10, env=env_helpers._tmux_safe_env(),
            )
        except (OSError, subprocess.SubprocessError) as exc:
            log.warning("pane-read: list-panes on %r failed (%s)", socket, exc)
            continue
        if getattr(probe, "returncode", 1) != 0:
            continue
        for row in (getattr(probe, "stdout", "") or "").splitlines():
            pid, _, pane_id = row.partition(" ")
            if pid == str(pane_pid) and pane_id:
                return _capture(socket, pane_id, run_fn) or None
    return None


def _tmux(socket: str, run_fn, *args: str) -> bool:
    """One tmux command on SOCKET that carries no payload on stdin. False on any non-zero exit or OS error,
    logged, never raised."""
    try:
        probe = run_fn(
            session_user.tmux_cmd(socket, *args),
            capture_output=True, text=True, timeout=10, env=env_helpers._tmux_safe_env(),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("answer-delivery: tmux %s on %r failed (%s)", args, socket, exc)
        return False
    if getattr(probe, "returncode", 1) != 0:
        # tmux's own message ("can't find pane", …) IS the diagnosis.
        log.warning(
            "answer-delivery: tmux %s on %r exited non-zero (%s)",
            args, socket, (getattr(probe, "stderr", "") or "").strip(),
        )
        return False
    return True


# A pane whose foreground process is one of these is not a runner's CLI (`deliver_answer`'s vendor path).
_SHELLS = frozenset({"bash", "sh", "dash", "zsh", "fish", "ksh"})


def _pane_command(socket: str, target: str, run_fn) -> str:
    """TARGET's foreground command (`#{pane_current_command}`), or "" when it cannot be read."""
    try:
        probe = run_fn(
            session_user.tmux_cmd(socket, "display-message", "-p", "-t", target, "#{pane_current_command}"),
            capture_output=True, text=True, timeout=10, env=env_helpers._tmux_safe_env(),
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return (getattr(probe, "stdout", "") or "").strip() if getattr(probe, "returncode", 1) == 0 else ""


def _send(socket: str, target: str, run_fn, *args: str) -> bool:
    """One `send-keys` against TARGET. False on any non-zero exit or OS error, logged, never raised.

    Keys only (`Escape`/`Enter`); the answer goes through `_paste`."""
    return _tmux(socket, run_fn, "send-keys", "-t", target, *args)


def _reap_orphan_answer_buffers(socket: str, run_fn) -> None:
    """Delete any `jaahd-answer-*` buffer left behind by a PREVIOUS daemon process.

    A SIGKILL/OOM between load and paste leaves an operator's answer in a buffer GLOBAL to the tmux server,
    readable by every runner pane via `show-buffer`. Only other pids' buffers are reaped, so a concurrent
    delivery in this process is never a victim. Best effort; never stops the delivery it precedes."""
    mine = f"{_BUFFER_PREFIX}{os.getpid()}-"
    try:
        probe = run_fn(
            session_user.tmux_cmd(socket, "list-buffers", "-F", "#{buffer_name}"),
            capture_output=True, text=True, timeout=10, env=env_helpers._tmux_safe_env(),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("answer-delivery: list-buffers on %r failed (%s)", socket, exc)
        return
    if getattr(probe, "returncode", 1) != 0:
        return
    for name in (getattr(probe, "stdout", "") or "").splitlines():
        name = name.strip()
        if name.startswith(_BUFFER_PREFIX) and not name.startswith(mine):
            log.warning(
                "answer-delivery: deleting orphaned buffer %r — a previous run died between its load and "
                "its paste, leaving an operator's answer readable to every pane on this tmux server",
                name,
            )
            _tmux(socket, run_fn, "delete-buffer", "-b", name)


def _paste(socket: str, target: str, run_fn, message: str) -> bool:
    """Put MESSAGE on TARGET's composer as a BRACKETED PASTE. False on any failure, logged, never raised.

    `load-buffer -` takes the payload on STDIN (no `MAX_ARG_STRLEN`); `paste-buffer -p` brackets it so
    newlines are characters, not submits. The buffer name is unique per delivery (pid + counter): a shared
    name would let an overlapping delivery overwrite the payload between load and paste — one task's answer
    into another's session. The buffer is removed on EVERY exit path (`-d`, or an explicit `delete-buffer`,
    a harmless no-op when it was never created), because the server is shared by every pane on the node."""
    _reap_orphan_answer_buffers(socket, run_fn)
    buffer_name = f"{_BUFFER_PREFIX}{os.getpid()}-{next(_BUFFER_SEQ)}"
    try:
        loaded = run_fn(
            session_user.tmux_cmd(socket, "load-buffer", "-b", buffer_name, "-"),
            input=message, capture_output=True, text=True, encoding="utf-8", timeout=10,
            env=env_helpers._tmux_safe_env(),
        )
    except (OSError, subprocess.SubprocessError) as exc:
        # Clean up even here: a `TimeoutExpired` after tmux read stdin leaves the buffer created.
        log.warning(
            "answer-delivery: load-buffer for %s on %r failed (%s)", target, socket, exc)
        _tmux(socket, run_fn, "delete-buffer", "-b", buffer_name)
        return False
    if getattr(loaded, "returncode", 1) != 0:
        log.warning(
            "answer-delivery: load-buffer for %s on %r exited non-zero (%s)",
            target, socket, (getattr(loaded, "stderr", "") or "").strip(),
        )
        _tmux(socket, run_fn, "delete-buffer", "-b", buffer_name)
        return False
    if _tmux(socket, run_fn, "paste-buffer", "-d", "-p", "-r", "-b", buffer_name, "-t", target):
        return True
    # Best effort: the paste failed, so `-d` never ran and the payload is still sitting in the buffer.
    _tmux(socket, run_fn, "delete-buffer", "-b", buffer_name)
    return False


def _paste_markers(pane_text: str) -> set[str]:
    """Every `[Pasted text #N]` placeholder visible on a captured pane, as a set.

    Taken before and after a paste so `_landed_elsewhere` can tell OUR placeholder from an earlier
    delivery's; a multi-round interview makes a stale marker the ordinary case."""
    return set(_PASTE_PLACEHOLDER_RE.findall(pane_text))


def _landed_elsewhere(pane_text: str, message: str, known_markers: set[str]) -> bool:
    """Did the answer land at all, when the composer never showed it AND is empty after the `Enter`?

    An empty composer means either submitted or the write VANISHED (observed: an `Esc` into a generating
    session, then a payload that reached nothing). Claiming delivery on a vanished write resumes the Task
    under a runner that never heard the answer, so the pane is searched for the answer's head fragment;
    absent, False re-serves the intent."""
    flat = _SOURCE_LABEL_RE.sub("", " ".join(message.split()))
    if len(flat) < 8:
        return True
    if flat[:_FRAGMENT_CHARS] in " ".join(pane_text.split()):
        return True
    # A paste past Claude Code's collapse heuristic never shows its own text (the transcript shows
    # `[Pasted text #N]` too), so the fragment search always says "no" for large payloads. Only a marker
    # absent BEFORE the paste counts — a previous delivery's marker in the scrollback would turn a lost
    # paste into an acked `done`.
    if _paste_markers(pane_text) - known_markers:
        return True
    log.warning(
        "answer-delivery: the answer reached neither the composer nor the transcript — refusing to report "
        "it delivered (the next drain re-delivers; a false success would resume the Task under a runner "
        "that never heard the answer)",
    )
    return False


class _TmuxTarget:
    """A tmux pane as a delivery target: the four primitives `_submit_on`/`_paste_task_on` drive, each one
    the module's own tmux function, unchanged. `PtyTarget` is the MicroVM spelling of the same four."""

    def __init__(self, socket: str, target: str, run_fn) -> None:
        self.socket, self.target, self.run_fn = socket, target, run_fn
        self.label = target

    def capture(self) -> str:
        return _capture(self.socket, self.target, self.run_fn)

    def key(self, name: str) -> bool:
        return _send(self.socket, self.target, self.run_fn, name)

    def paste(self, message: str) -> bool:
        return _paste(self.socket, self.target, self.run_fn, message)

    def command(self) -> str:
        return _pane_command(self.socket, self.target, self.run_fn)


def _submit_on(pane, message: str, sleep_fn, *, require_composer: bool = False) -> bool:
    """Put MESSAGE on the composer and get it SUBMITTED. True once it is submitted, queued, or already
    in the transcript.

    Pasted, not typed, so no word in it can be read as a key name. Sequence: paste, WAIT for it to appear
    (an `Enter` in the same reader pass is absorbed), `Enter`, then VERIFY and re-send `Enter` while our
    own text is still there — spacing cannot prevent the reader-side race; one extra `Enter` always fixed
    it. A queued answer is delivered and never re-sent. False only when tmux refused a write or the answer
    landed nowhere; a composer still holding it after every re-send is reported delivered, loudly, rather
    than re-delivered every 15s."""
    # A multi-line payload goes only into a pane PROVABLY showing a composer: without the app requesting
    # bracketed paste, every line would submit. The upstream fences prove WHICH window, never WHAT runs in
    # it — a shell left in the right worktree passes them all, and would execute each line.
    # One capture serves that gate and the `[Pasted text #N]` baseline for `_landed_elsewhere`.
    before = pane.capture()
    known_markers = _paste_markers(before)
    if require_composer and not composer_prompt_present(before):
        # A caller that sends no `Escape` first: one line plus `Enter` into a picker selects its default.
        log.warning("answer-delivery: %s shows no composer prompt — a modal may be up; not pasting", pane.label)
        return False
    if "\n" in message and not composer_prompt_present(before):
        log.warning(
            "answer-delivery: %s shows no composer prompt — refusing to paste a MULTI-LINE answer, "
            "because `paste-buffer -p` brackets nothing unless the application asked for the mode and "
            "each newline would then submit at whatever is running in that pane", pane.label,
        )
        return False
    if not pane.paste(message):
        return False
    landed = ""
    for _ in range(_LANDED_POLLS):
        sleep_fn(_LANDED_POLL_SECS)
        landed = composer_text(pane.capture())
        if landed:
            break
    if not pane.key("Enter"):
        return False
    for attempt in range(1, _SUBMIT_ATTEMPTS + 1):
        sleep_fn(_SUBMIT_SETTLE_SECS)
        screen = pane.capture()
        pending = composer_text(screen)
        if not pending:
            return True if landed else _landed_elsewhere(screen, message, known_markers)
        if not pending_is_unsubmitted(pending, message):
            # The queued hint, or a human's own line: another `Enter` would duplicate or submit theirs.
            log.info(
                "answer-delivery: the answer is accepted and the composer now shows %r — delivered; not "
                "re-sending into it", pending[:80],
            )
            return True
        if not composer_prompt_present(screen):
            # Not provably the composer — a modal may be up, and `Enter` selects its highlighted row.
            log.warning(
                "answer-delivery: the pane shows no composer prompt, so %r cannot be proven to be pending "
                "— not re-sending Enter into what may be a modal", pending[:80],
            )
            return True
        if attempt < _SUBMIT_ATTEMPTS:
            log.info(
                "answer-delivery: the composer still holds the answer (%r) after %d Enter(s) — Claude Code "
                "absorbed it into a paste chunk; re-sending Enter", pending[:80], attempt,
            )
            if not pane.key("Enter"):
                return False
    log.warning(
        "answer-delivery: the composer STILL holds the answer after %d Enters — reporting delivered rather "
        "than re-delivering the same answer on every drain, but this session may be sitting on an "
        "unsubmitted answer and wants a human eye", _SUBMIT_ATTEMPTS,
    )
    return True


def _proven_window(tmux_target: str, expect_cwd: str, run_fn, kind: str) -> tuple[str, str] | None:
    """`(socket, window_id)` of TMUX_TARGET once it is PROVEN to be the slot's window, or None (logged
    under `kind`) — the identity gates every pane write shares.

    `expect_cwd` is required and an empty one is refused: a write with no identity check is the injection
    hazard. Addressed by stable `#{window_id}`, never the holder window, and only when a pane sits under
    `expect_cwd`."""
    if not expect_cwd:
        log.warning(
            "%s: refusing to write to %s with no expected worktree — an unverified window is not a "
            "delivery target", kind, tmux_target,
        )
        return None
    sockets = kill_gates._reachable_sockets()
    if not sockets:
        log.warning("%s: no reachable tmux socket — %s not delivered", kind, tmux_target)
        return None
    for socket in sockets:
        target = kill_gates._window_id_on(socket, tmux_target, run_fn)
        if not target:
            # Window ids are per-SERVER; try the next socket. NO raw `session:index` fallback (unlike the
            # kill's): both servers host a session under this node's name, so `<node>:3` names a
            # different real window on each — text typed into someone else's runner.
            continue
        if kill_gates._window_name_on(socket, target, run_fn) == HOLDER_WINDOW:
            # `_holder` keeps the node's tmux session alive; one keystroke ends its `sleep` and collapses
            # every console on the node. No slot record can legitimately name it.
            log.warning("%s: %s resolved to the holder window — refusing", kind, tmux_target)
            return None
        if not kill_gates._window_cwd_matches(socket, target, expect_cwd, run_fn):
            log.warning(
                "%s: %s (%s) has no pane under %s — refusing (a window that cannot be proven to be this "
                "slot's is not a delivery target)", kind, tmux_target, target, expect_cwd,
            )
            return None
        return socket, target
    log.warning("%s: %s resolved on no reachable socket — not delivered", kind, tmux_target)
    return None


def deliver_answer(
    tmux_target: str,
    message: str,
    *,
    expect_cwd: str,
    tui: "ComposerTui | None" = None,
    run_fn=subprocess.run,
    sleep_fn=time.sleep,
) -> bool:
    """Deliver MESSAGE to the runner in TMUX_TARGET. True only on a submitted message.

    `expect_cwd` is REQUIRED and an empty one is refused: a write with no identity check is the
    injection hazard. `tui` is the runner CLI's screen (`AgentAdapter.tui()`); None is Claude Code, whose
    screen is this module's own constants. On the other CLIs `Esc` also ends the turn (measured per
    adapter), so it is sent only when the composer is NOT up: a runner already sitting at an idle composer
    is not interrupted."""
    return _on_tmux(tmux_target, message, expect_cwd, run_fn, "answer-delivery",
                    lambda pane: _answer_on(pane, message, tui, sleep_fn, tmux_target))


def _on_tmux(tmux_target: str, message: str, expect_cwd: str, run_fn, kind: str, deliver) -> bool:
    """Prove the tmux window (`_proven_window`), then `deliver(pane)` on it: the one preamble of
    `deliver_answer` and `deliver_message`. An empty target or message is refused, never typed."""
    if not tmux_target or not message.strip():
        return False
    window = _proven_window(tmux_target, expect_cwd, run_fn, kind)
    if window is None:
        return False
    socket, target = window
    return deliver(_TmuxTarget(socket, target, run_fn))


def _answer_on(pane, message: str, tui: "ComposerTui | None", sleep_fn, label: str, *,
               require_composer: bool = False) -> bool:
    """`deliver_answer` once its target is proven — the same sequence on a tmux pane and a VM console."""
    if tui is not None:
        if pane.command() in _SHELLS:
            # The markers are plain text a shell can print; a shell would run the pasted answer.
            log.warning("answer-delivery: %s is running a shell, not the runner's CLI — refusing", label)
            return False
        screen = pane.capture()
        if tui.dialog_up(screen) or tui.write_in_open(screen):
            typed = _write_in_on(pane, message, tui, screen, sleep_fn, label)
            if typed is not None:
                return typed
            if tui.write_in_row is not None:
                # A key may have been sent: decide on the screen as it is now.
                screen = pane.capture()
        if tui.dialog_up(screen):
            # Only into the question dialog, proven by its own marker: a vendor `Esc` ends the turn, so
            # a working runner is never sent one — and a busy line beside the dialog does not stop it
            # (measured; see each adapter's `dialog_marker`).
            pane.key("Escape")
            sleep_fn(_SUBMIT_SETTLE_SECS)
        return _paste_task_on(pane, message, tui, sleep_fn, _ANSWER_READY_SECS, _LANDED_LAUNCH_SECS,
                              label, what="answer")
    # Dismiss the modal (normally the picker → an honest "User declined"); a no-op when none is open.
    pane.key("Escape")
    sleep_fn(_SUBMIT_SETTLE_SECS)
    if _submit_on(pane, message, sleep_fn, require_composer=require_composer):
        log.info("answer-delivery: delivered to %s (%s)", label, pane.label)
        return True
    return False


def _write_in_on(pane, message: str, tui: "ComposerTui", screen: str, sleep_fn, label: str) -> bool | None:
    """Type MESSAGE into the live dialog's free-text row (`ComposerTui.write_in_row`): the answer comes back as
    the question tool's own result, in the turn that asked. None = not done and nothing typed — no such row,
    or its key did not open the field — so the caller falls back to `Esc` + a message; True/False as
    `deliver_answer`.

    The row's key is read off the screen, never counted: `Down`xN is index arithmetic over rows this cannot
    vouch for. A field already open (a delivery cut short) is typed into as found — and not typed into again
    when it already holds this answer. Later questions of the same call are left alone: each is its own
    round, answered by its own delivery. Every `Enter` goes only into a field still open: once it closes, the
    next screen may be another question's picker, where an `Enter` picks its highlighted option."""
    if not tui.write_in_open(screen):
        key = tui.write_in_key(screen)
        if key is None:
            return None
        if not pane.key(key):
            return False
        for _ in range(_LANDED_POLLS):
            sleep_fn(_LANDED_POLL_SECS)
            screen = pane.capture()
            if tui.write_in_open(screen):
                break
        else:
            log.warning("answer-delivery: %s did not open its dialog's free-text field on %r — falling back "
                        "to dismissing the dialog", label, key)
            return None
    if not tui.paste_landed(tui.write_in_field(screen) or "", message):
        if not pane.paste(message):
            return False
        for _ in range(_LANDED_POLLS):
            sleep_fn(_LANDED_POLL_SECS)
            if tui.paste_landed(tui.write_in_field(pane.capture()) or "", message):
                break
        else:
            log.warning("answer-delivery: %s never showed the answer in its dialog's free-text field — refusing "
                        "to report a delivery this cannot verify", label)
            return False
    for _ in range(_SUBMIT_ATTEMPTS):
        if not pane.key("Enter"):
            return False
        sleep_fn(_SUBMIT_SETTLE_SECS)
        if not tui.write_in_open(pane.capture()):
            log.info("answer-delivery: typed into %s's question dialog (%s)", label, pane.label)
            return True
        # Still our field: the `Enter` went into a paste chunk still being drawn — send another.
    log.warning("answer-delivery: %s's free-text field STILL holds the answer after %d Enters — NOT delivered; "
                "the next try submits the answer already there", label, _SUBMIT_ATTEMPTS)
    return False


def deliver_message(
    tmux_target: str,
    message: str,
    *,
    expect_cwd: str,
    tui: "ComposerTui | None" = None,
    run_fn=subprocess.run,
    sleep_fn=time.sleep,
) -> bool:
    """Deliver a CP-composed MESSAGE to the WORKING runner in TMUX_TARGET (`deliver_message`). True only on
    a submitted (or queued) message.

    `deliver_answer`'s identity gates, minus its first key: NEVER `Escape`. The recipient is mid-turn, not
    parked in a picker, and `Esc` would abort its in-flight tool call; Claude Code queues a message typed
    while a tool runs, which `_submit_on` already counts as delivered. `expect_cwd` is REQUIRED, as there.
    `tui` is a vendor CLI's screen (None is Claude Code): there the paste waits for the adapter's ready
    composer and is refused when it never shows one."""
    return _on_tmux(tmux_target, message, expect_cwd, run_fn, "message-delivery",
                    lambda pane: _message_on(pane, message, tui, sleep_fn, tmux_target))


def _message_on(pane, message: str, tui: "ComposerTui | None", sleep_fn, label: str) -> bool:
    """`deliver_message` once its target is proven — the same sequence on a tmux pane and a VM console."""
    if tui is not None:
        if pane.command() in _SHELLS:
            log.warning("message-delivery: %s is running a shell, not the runner's CLI — refusing", label)
            return False
        return _paste_task_on(pane, message, tui, sleep_fn, _MESSAGE_READY_SECS, _LANDED_LAUNCH_SECS,
                              label, what="message")
    # No `Escape` to clear a modal first, so EVERY message — not only a multi-line one — needs a proven
    # composer (`require_composer`): one line plus `Enter` into a picker selects its highlighted default, and
    # into a shell runs as a command.
    if pane.command() in _SHELLS:
        log.warning("message-delivery: %s is running a shell, not the runner's CLI — refusing", label)
        return False
    if _submit_on(pane, message, sleep_fn, require_composer=True):
        log.info("message-delivery: delivered to %s (%s)", label, pane.label)
        return True
    return False


# ── A MicroVM session's console as the target (the MicroVM design's D19) ──────────────────────────────
#
# Same sequence as a tmux pane — `Esc`, settle, bracketed paste, wait for it to show, `Enter`, the same
# re-send rule — through `jaah-microvmd`: keys and the paste go in with its `write` op, and the screen the
# composer probes read is the console's ring (replayed, then live) through a read-only attach held for the
# delivery (`microvm_console.ConsoleReader`). No tmux, no cwd: the VM is proven by the registry binding it
# to the session the CP names, and by its supervisor reporting it running (`_proven_vm`).

# The arrows as a raw terminal sends them in normal cursor mode; `consent_watch` walks a dialog's rows with them.
_PTY_KEYS = {"Escape": b"\x1b", "Enter": b"\r", "Up": b"\x1b[A", "Down": b"\x1b[B"}
_PASTE_START, _PASTE_END = b"\x1b[200~", b"\x1b[201~"
# Raw bytes per `write` request: base64 makes it ~43 KB, inside microvmd's 64 KB request line.
_PTY_WRITE_CHUNK = 32 * 1024
# What a pasted message may not carry into a raw console: C0 controls but LF/TAB, DEL, and the C1 range.
_PTY_UNSAFE_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f\x80-\x9f]")


class PtyTarget:
    """A MicroVM console as a delivery target: `_TmuxTarget`'s four primitives over `jaah-microvmd`."""

    def __init__(self, vm_id: str, reader, request=None) -> None:
        microvm_client = cloud_modules.load("microvm_client")  # Linux-only machinery, off the macOS/Windows import graph

        self.vm_id = vm_id
        self.reader = reader
        self.request = request or microvm_client.request
        self.label = f"vm:{vm_id}"

    def capture(self) -> str:
        return self.reader.screen()

    def _write(self, data: bytes) -> bool:
        microvm_client = cloud_modules.load("microvm_client")

        sent = 0
        try:
            for start in range(0, len(data), _PTY_WRITE_CHUNK):
                self.request("write", vm_id=self.vm_id, timeout=10.0,
                             b64=base64.b64encode(data[start:start + _PTY_WRITE_CHUNK]).decode())
                sent += 1
        except (microvm_client.SupervisorError, OSError) as exc:
            log.warning("answer-delivery: write to VM %s failed (%s)", self.vm_id, exc)
            if sent and data.startswith(_PASTE_START):
                # Half a paste leaves the CLI in paste mode, reading every later key — the retry's `Enter`
                # included — as pasted text. Close the bracket, best effort.
                try:
                    self.request("write", vm_id=self.vm_id, timeout=10.0,
                                 b64=base64.b64encode(_PASTE_END).decode())
                except (microvm_client.SupervisorError, OSError):
                    pass
            return False
        return True

    def key(self, name: str) -> bool:
        if len(name) == 1 and name in "123456789":
            # A dialog row's own key (`ComposerTui.write_in_key`), sent as itself.
            return self._write(name.encode("ascii"))
        return self._write(_PTY_KEYS[name])

    def paste(self, message: str) -> bool:
        """MESSAGE as ONE bracketed paste. Every control character but newline and tab is dropped: an ESC
        (or the 8-bit CSI) would let `ESC[201~` end the bracket early, and the rest would arrive as typed
        keys — a newline among them submitting at whatever reads the console."""
        body = _PTY_UNSAFE_RE.sub("", message).encode("utf-8")
        return self._write(_PASTE_START + body + _PASTE_END)

    def command(self) -> str:
        """Unknown: a serial console has no foreground-process probe. The VM callers compensate by never
        pasting a Claude answer without a proven composer (`deliver_vm_answer`)."""
        return ""


def _proven_vm(vm_id: str, *, task_id: object, session_id: object, kind: str, request=None) -> bool:
    """Whether VM_ID is PROVEN to be this session's VM and running — the VM spelling of `_proven_window`.

    The registry (`vms.json`) must bind it to exactly this task and session (a console VM: its console
    session), and this node's supervisor must report it `running`. Either unknown is a refusal: a write
    with no identity proof is the injection hazard."""
    microvm_client = cloud_modules.load("microvm_client")  # Linux-only machinery, off the macOS/Windows import graph
    microvm_registry = cloud_modules.load("microvm_registry")
    if microvm_client is None or microvm_registry is None:
        log.warning("%s: this node runs no MicroVM — VM %s not delivered", kind, vm_id)
        return False
    binding = microvm_registry.load().get(vm_id)
    if binding is None or not microvm_registry.bound_to(binding, task_id=task_id, session_id=session_id):
        log.warning("%s: VM %s is not bound to task %s session %s — refusing", kind, vm_id, task_id,
                    str(session_id or "")[:8])
        return False
    try:
        listed = (request or microvm_client.request)("list", timeout=5.0)
    except (microvm_client.SupervisorError, OSError) as exc:
        log.warning("%s: this node's VM supervisor did not answer (%s) — VM %s not delivered", kind, exc, vm_id)
        return False
    state = next((row.get("state") for row in listed.get("vms") or []
                  if isinstance(row, dict) and row.get("vm_id") == vm_id), None)
    if state != "running":
        log.warning("%s: VM %s is %s, not running — refusing", kind, vm_id, state or "unknown here")
        return False
    return True


def _on_vm(vm_id: str, kind: str, request, open_stream, deliver) -> bool:
    """Open the delivery's console reader, run DELIVER on a `PtyTarget`, close the reader. Never raises."""
    microvm_client = cloud_modules.load("microvm_client")
    microvm_console = cloud_modules.load("microvm_console")

    try:
        reader = microvm_console.ConsoleReader(vm_id, **({"open_stream": open_stream} if open_stream else {}))
        with reader:
            return deliver(PtyTarget(vm_id, reader, request))
    except (microvm_client.SupervisorError, OSError) as exc:
        log.warning("%s: could not read VM %s's console (%s) — not delivered", kind, vm_id, exc)
        return False


def deliver_vm_answer(
    vm_id: str,
    message: str,
    *,
    task_id: object,
    session_id: object,
    tui: "ComposerTui | None" = None,
    request=None,
    open_stream=None,
    sleep_fn=time.sleep,
) -> bool:
    """`deliver_answer` into a MicroVM session. True only on a submitted message.

    Claude's path pastes only into a PROVEN composer (`require_composer`), stricter than a tmux pane's:
    the guest drops to a shell when the CLI exits, and `PtyTarget.command` cannot see that."""
    if not vm_id or not message.strip():
        return False
    if not _proven_vm(vm_id, task_id=task_id, session_id=session_id, kind="answer-delivery", request=request):
        return False
    return _on_vm(vm_id, "answer-delivery", request, open_stream, lambda pane: _answer_on(
        pane, message, tui, sleep_fn, pane.label, require_composer=True))


def deliver_vm_message(
    vm_id: str,
    message: str,
    *,
    task_id: object,
    session_id: object,
    tui: "ComposerTui | None" = None,
    request=None,
    open_stream=None,
    sleep_fn=time.sleep,
) -> bool:
    """`deliver_message` into a MicroVM session: never `Escape`, a proven composer always."""
    if not vm_id or not message.strip():
        return False
    if not _proven_vm(vm_id, task_id=task_id, session_id=session_id, kind="message-delivery",
                      request=request):
        return False
    return _on_vm(vm_id, "message-delivery", request, open_stream,
                  lambda pane: _message_on(pane, message, tui, sleep_fn, pane.label))


# ── Delivering a runner's TASK prompt into a freshly launched pane ──────────────────────────────────────
#
# Same paste mechanism as `deliver_answer`, different screen: the constants above are Claude Code's TUI,
# and a CLI that needs its task pasted is not that one. The caller hands in the launching vendor's
# `ComposerTui` from its adapter; nothing here names a vendor or reads `_COMPOSER_PROMPT`.

# Wait for a vendor composer to come back after its question dialog is dismissed; bounded like the rest,
# since a `failed` delivery is re-served by the CP.
_ANSWER_READY_SECS = 20.0
# A `deliver_message` recipient is mid-turn, so its vendor composer may not be ready for a long while: one
# short look, then `failed` and the CP's re-serve is the retry — never a 20s stall of the node's drain.
_MESSAGE_READY_SECS = 2.0
# Wait for a just-launched TUI's composer: a minute tolerates a cold start on a loaded node; bounded so a
# CLI that never comes up cannot hold a worker thread.
_LAUNCH_READY_SECS = 60.0
_LAUNCH_POLL_SECS = 1.0
# Wait for the TUI to RENDER the pasted payload. Its own constant, not the answer path's 4s: a task prompt
# can be ~100 000 characters, and a short window expires while it is still painting. A wall clock, since
# each poll also pays a `capture-pane` with a 10s timeout.
_LANDED_LAUNCH_SECS = 15.0


def deliver_task_prompt(
    tmux_target: str,
    message: str,
    *,
    expect_cwd: str,
    tui: "ComposerTui | None",
    run_fn=subprocess.run,
    sleep_fn=time.sleep,
    ready_timeout: float = _LAUNCH_READY_SECS,
    landed_timeout: float = _LANDED_LAUNCH_SECS,
) -> bool:
    """Paste MESSAGE into the freshly launched runner in TMUX_TARGET and submit it. True only on a
    submitted prompt.

    For a vendor whose `task_delivery()` is `TASK_TMUX_PASTE` (its CLI takes no prompt at launch). `tui` is
    REQUIRED: a wrong guess about "ready" is a prompt typed into a modal. Same identity gates as
    `deliver_answer` — the window NAMED must be proven to be the window that ANSWERED. Never sends
    `Escape` first: a fresh session has no picker, and a blind `Escape` into an unknown modal answers a
    question nobody read (the adapter's `prepare()` handles launch modals)."""
    if not tmux_target or not message.strip():
        return False
    if tui is None:
        log.warning("task-delivery: %s asked for a pane paste with no TUI descriptor — refusing (there "
                    "is no way to tell a ready composer from a modal without one)", tmux_target)
        return False
    window = _proven_window(tmux_target, expect_cwd, run_fn, "task-delivery")
    if window is None:
        return False
    socket, target = window
    return _paste_task(socket, target, message, tui, run_fn, sleep_fn, ready_timeout,
                       landed_timeout, tmux_target)


def _paste_task(socket: str, target: str, message: str, tui: "ComposerTui",
                run_fn, sleep_fn, ready_timeout: float, landed_timeout: float, label: str,
                what: str = "task prompt") -> bool:
    """`_paste_task_on` a tmux pane."""
    return _paste_task_on(_TmuxTarget(socket, target, run_fn), message, tui, sleep_fn, ready_timeout,
                          landed_timeout, label, what)


def _paste_task_on(pane, message: str, tui: "ComposerTui", sleep_fn, ready_timeout: float,
                   landed_timeout: float, label: str, what: str = "task prompt") -> bool:
    """The paste itself, once the window has been resolved and proven. `what` names the payload in the
    log (a task prompt, or an answer/message — `deliver_answer`'s and `deliver_message`'s vendor path)."""
    kind = {"answer": "answer-delivery", "message": "message-delivery"}.get(what, "task-delivery")
    # A wall-clock deadline: each poll also pays a 10s-timeout `capture-pane`, so a poll count would not
    # bound a wedged tmux.
    deadline = time.monotonic() + ready_timeout
    ready = False
    ready_pane = ""
    while time.monotonic() < deadline:
        ready_pane = pane.capture()
        if tui.is_ready(ready_pane):
            ready = True
            break
        sleep_fn(_LAUNCH_POLL_SECS)
    if not ready:
        # Not a paste anyway: pasting into a CLI that failed to start, a modal, or a busy one loses the task.
        log.warning("%s: %s never showed a ready composer within %.0fs — the %s was NOT delivered",
                    kind, label, ready_timeout, what)
        return False
    if not pane.paste(message):
        return False
    # Wait for it to render before `Enter`, as `_submit_on` does.
    landed = False
    landed_pane = ""
    landed_by = time.monotonic() + landed_timeout
    while time.monotonic() < landed_by:
        sleep_fn(_LANDED_POLL_SECS)
        landed_pane = pane.capture()
        # And the screen CHANGED: an earlier answer's text or placeholder in the scrollback would pass alone.
        if landed_pane != ready_pane and tui.paste_landed(landed_pane, message):
            landed = True
            break
    if not landed:
        # Refuse rather than submit-and-hope: an empty composer afterwards cannot tell "submitted" from
        # "never there", and a false success leaves a heartbeating runner with no prompt until the idle reap.
        log.warning("%s: %s never showed the pasted %s on its composer within %.0fs — refusing to "
                    "report a delivery this cannot verify", kind, label, what, landed_timeout)
        return False
    if not pane.key("Enter"):
        return False
    for attempt in range(1, _SUBMIT_ATTEMPTS + 1):
        sleep_fn(_SUBMIT_SETTLE_SECS)
        screen = pane.capture()
        # Asked of the adapter (a turn in flight, or its empty-composer placeholder back). Not the payload's
        # disappearance: a short payload stays visible in the transcript after submit.
        if tui.submitted(screen):
            log.info("%s: delivered to %s (%s)", kind, label, pane.label)
            return True
        if screen != landed_pane:
            # Something replaced the composer — measured: the model answering with a NEW question dialog.
            # Our `Enter` was consumed; another would select that dialog's highlighted option.
            log.info("%s: %s changed after the submit and shows neither composer nor turn — delivered; "
                     "not re-sending Enter into what may be a dialog", kind, label)
            return True
        # Unchanged since the paste landed: the `Enter` was absorbed into the paste chunk, so re-send it.
        if attempt < _SUBMIT_ATTEMPTS and not pane.key("Enter"):
            return False
    log.warning("%s: %s still shows the pasted payload after %d submits — the %s was NOT submitted",
                kind, label, _SUBMIT_ATTEMPTS, what)
    return False
