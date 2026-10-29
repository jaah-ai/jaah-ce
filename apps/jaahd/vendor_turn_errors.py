"""vendor_turn_errors.py — a non-Claude runner's `StopFailure`, sensed by the node.

When a Claude runner's turn ends on a vendor API error (usage wall, overload, dead credential, spent
credit), Claude Code fires `StopFailure` and `hooks/runner-status.py stopfailure` files
`session.error` with `request_replace` within a second: the break is classified, a walled account
leaves rotation, and the task goes back to `ready` for a relaunch. The other runner CLIs fire NO hook on
such a turn — no `StopFailure`, and not their turn-end event either — and idle at their
composer. Left alone that runner holds its task `active` until the control plane's reclaim window.

So on every idle tick this step asks each live non-Claude runner's adapter how the runner's own
transcript ENDS (`AgentAdapter.turn_error`) and, on a turn that ended on a vendor API error with the
runner's pane back at an idle composer (`ComposerTui.is_ready`), files the
SAME report the Claude hook files: the same verb, spoken as that runner (`cred_broker.call_as_runner`),
the cause from the same classifier (`break_cause.classify_vendor_error`), `observed_by` naming this sensor. Each turn
is reported once; a report the daemon could not deliver is retried next tick, one the CP refused is not.
Where its transcript is: the account dir the dispatcher recorded, an API-key account's included (the dispatcher
gives one its own home, `fleet_dispatch.api_key_account_dir`). A hermetic run — a MicroVM task VM's
terminal session, which is in this node's VM registry rather than its runner registry — is read under the VM's
own copy of the account (`session_sweep.vm_transcript_path`) and speaks as the identity the VM's own control
calls carry (`cred_broker.vm_runner_identity`); it has no host pane, so it is filed on the waiver below, and only
while its task is still `active` (a retained VM outlives its run). Only a runner with no account home is declined: its CLI writes in the session user's own home, which any run
on this node shares, so no file there is attributable to it.

Two fallbacks, because the pane is a weak witness (a vendor popup or an unreadable pane hid it on Task
4134, a runner out of credits held its task for 8h):
  * AN ERRORED TURN WITHOUT THE PANE: once this daemon has seen the same closing error record for
    `_ERROR_WITHOUT_PANE_SECONDS` with nothing of the session written since, the CLI is not retrying
    anything, so the break is filed without the pane witness — its own clock, never a file's mtime, and
    never an account-family cause: runner-writable text alone may end an attempt, not condemn an account.
  * THE IDLE BACKSTOP: `hooks/autoclose-check.py` keys on a Claude transcript and declines every other
    CLI at its first rung (`skip=root-not-my-session`), so a non-Claude runner that ends a turn without
    settling was held until a human noticed (16h, on dev). Once no session file has changed for
    `_IDLE_BACKSTOP_SECONDS`, the pane shows an idle composer UNCHANGED across two ticks (a running tool's
    spinner and timer change it), and its task still reads `active` and is not an advisor chat (never
    idle-killed), this files the same break an idle-confirmed Claude settle would (cause `unknown`,
    `request_replace`).
Every gate that stops a report is logged once per runner when it starts to apply, so the next miss says why.

A Claude runner gets one narrower check, a safety net under its own hooks (`_sense_claude_wall`): since Claude
Code's usage-limit grace allowance a wall can end a turn on prose with no `StopFailure`, its quota banner
written after the Stop hook — so a turn that ended on an account wall and sat unwritten past
`_CLAUDE_WALL_GRACE_SECONDS` is filed here, with the same pane witness an account cause needs above.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
import urllib.error
from dataclasses import replace
from pathlib import Path

import acp_runtime
import agent_providers
import agent_vocabulary
import agents
import answer_delivery
import cloud_modules
import cred_broker
import runner_registry
import session_sweep

log = logging.getLogger("jaahd.vendor_turn_errors")

# The task VMs' bindings and guest paths; None on a community-edition node, which runs no VM.
microvm_registry = cloud_modules.load("microvm_registry")
microvm_seed = cloud_modules.load("microvm_seed")

# The `observed_by` this sensor's incidents carry: the Claude hook's is `stopfailure-hook`, and the
# evidence differs (a structured transcript record read by the node, not a hook payload).
OBSERVED_BY = "vendor-turn-error-sensor"

# The verb, and why it and nothing else: `request_replace` ends the attempt exactly as the Claude hook's
# does (apps/api/app/routes/agent/runner_liveness.py `RunnerBreakIn`).
_VERB = "session.error"

# The cause of a turn the VENDOR recorded as an API error whose words match no pattern: still an API error
# by the vendor's own record, not a guess — so not `unknown`, which would say nothing was read.
_UNMATCHED_CAUSE = "api_error"

# How long a runner whose transcript could not be resolved waits before the next attempt: resolution walks
# the vendor's session tree, and a CLI that has not written yet will not have in the next five seconds.
_UNRESOLVED_RETRY_SECONDS = 30.0

# How much of the vendor's (runner-writable) error text a journal line carries.
_LOG_DETAIL_CHARS = 300

# How long a session must sit unwritten after its closing error record before the break is filed without the
# pane's idle-composer witness: the witness guards a CLI retrying a prompt it has not written down yet, which
# takes seconds, never minutes.
_ERROR_WITHOUT_PANE_SECONDS = 300.0

# How long every file of a session must sit unwritten before the idle backstop looks at it: autoclose's own
# linger default (`fleet_launch._VISIBLE_LINGER_DEFAULT`), but its own control — that knob is a display
# setting, and a small value on a dev node must not shorten how soon a runner is replaced.
_IDLE_BACKSTOP_SECONDS = 1800.0

# The idle backstop's cause and `observed_by`: nothing was read that names a vendor error, and the evidence is
# the node's (idle files + idle composer + an `active` task), not a hook payload.
_IDLE_CAUSE = "unknown"
IDLE_OBSERVED_BY = "vendor-idle-backstop"

# The interactive operator chat: settled only by the operator, never idle-killed (fleet-task-lifecycle.md).
_ADVISOR = "advisor"

# How long an idle runner whose task is not `active` waits before its task is read again: a runner parked on
# a question can sit idle for hours, and every read is a CP request and a journal line.
_STATE_RECHECK_SECONDS = 300.0

# Causes that record a verdict on the ACCOUNT at the CP (`_ACCOUNT_BREAK_VERDICTS`). Never filed without the
# pane witness: the error record is runner-writable, and a co-tenant on the account could forge one.
_ACCOUNT_CAUSES = frozenset({"usage_limit", "credit_exhausted", "auth_expired"})

# Control-plane answers that are not a verdict on the report: a rotating node token, a timeout, a throttle.
_RETRYABLE_STATUSES = frozenset({401, 408, 429})

# The Claude safety net's `observed_by`: the node read a wall the session's own hooks did not report.
CLAUDE_WALL_OBSERVED_BY = "claude-wall-sensor"

# How long a Claude runner walled at its turn's end must sit unwritten before the node files for it: its hooks
# report within a second and the kill follows, so a wall still idle after this was missed.
_CLAUDE_WALL_GRACE_SECONDS = 90.0

# Where Claude Code draws its quota chrome: the bottom of the pane; anything higher is scrollback the model
# may have written.
_PANE_WALL_TAIL_LINES = 12


class VendorTurnErrorSensor:
    """The step's state, which lives only as long as the daemon: a restart re-reads every runner, and a
    re-report of a break the CP already acted on is refused as stale, never acted on twice."""

    def __init__(self) -> None:
        # runner session id -> its resolved transcript. Resolution walks the vendor's tree, so it is paid
        # once per runner rather than every tick.
        self._transcripts: dict[str, Path] = {}
        # runner session id -> when its unresolved transcript may next be looked for (monotonic).
        self._unresolved_until: dict[str, float] = {}
        # `(runner session id, turn marker)` already delivered or refused.
        self._reported: set[tuple[str, str]] = set()
        # runner session id -> the gate that last stopped a report, so each is logged once, when it starts.
        self._declined: dict[str, str] = {}
        # `(runner session id, turn marker)` -> when this daemon first saw that errored turn (monotonic).
        self._error_seen_at: dict[tuple[str, str], float] = {}
        # runner session id -> when an idle runner's non-`active` task may next be read (monotonic).
        self._state_recheck_at: dict[str, float] = {}
        # runner session id -> its pane as the idle backstop last read it: a positive idle signal is the same
        # screen on two ticks.
        self._idle_pane: dict[str, str] = {}

    async def step(self) -> None:
        await asyncio.to_thread(self.pass_once)

    def _runners(self) -> list[tuple[runner_registry.RunnerIdentity, str]]:
        """`(identity, VM id or "")` for every live runner: the runner registry's panes, then each task VM's
        terminal session (an ACP launch's host reports its own engine's errors; a row recorded before its
        `provider` names no CLI to read)."""
        found = [(ident, "") for ident in runner_registry.live_runners()]
        for vm_id, binding in (microvm_registry.load() if microvm_registry is not None else {}).items():
            if binding.kind != "task" or binding.host_capability or not binding.provider:
                continue
            ident = cred_broker.vm_runner_identity(binding)
            if ident is not None:
                found.append((replace(ident, provider=binding.provider), vm_id))
        return found

    def pass_once(self) -> int:
        """One sweep over the live runners; returns how many breaks it filed. Never raises."""
        filed = 0
        live: set[str] = set()
        for ident, vm_id in self._runners():
            if ident.provider in runner_registry.NO_MODEL_PROVIDERS:
                # A script persona (the merge lane) or an SDK agent's pane has no vendor transcript and
                # no composer: it is never idle in this sense, and alive exactly while its pane is. A
                # 40-minute gate is not a stalled turn.
                continue
            # Claude Code (DeepSeek's runners included) reports its own breaks from its hooks; only a usage
            # wall those hooks missed is sensed here. An unrecorded provider is Claude (the field predates
            # every other provider); an ACP host reports its own engine's errors.
            claude = not ident.provider or agent_providers.account_layer(ident.provider) == "omc"
            if claude and (ident.runtime == acp_runtime.RUNTIME_ACP or vm_id):
                # A VM's Claude Code runs its own hooks in the guest.
                continue
            live.add(ident.session_id)
            try:
                if not ident.account_dir and not vm_id:
                    filed += self._decline(ident, "no account home: its CLI writes in the session user's own "
                                                  "home, which other runs share")
                else:
                    filed += self._sense_claude_wall(ident) if claude else self._sense(ident, vm_id)
            except Exception as exc:  # noqa: BLE001 — one runner may never cost the others their tick
                log.warning("vendor turn errors: task %s (%s) could not be read: %s: %s",
                            ident.task_id, ident.provider, type(exc).__name__, exc)
        # A runner that is gone takes its state with it.
        self._transcripts = {s: p for s, p in self._transcripts.items() if s in live}
        self._unresolved_until = {s: t for s, t in self._unresolved_until.items() if s in live}
        self._reported = {r for r in self._reported if r[0] in live}
        self._declined = {k: v for k, v in self._declined.items() if k in live}
        self._error_seen_at = {k: v for k, v in self._error_seen_at.items() if k[0] in live}
        self._state_recheck_at = {k: v for k, v in self._state_recheck_at.items() if k in live}
        self._idle_pane = {k: v for k, v in self._idle_pane.items() if k in live}
        return filed

    def _decline(self, ident: runner_registry.RunnerIdentity, gate: str, detail: str = "") -> int:
        """Log `gate` once per runner when it starts to apply — the sensor runs every idle tick, so `gate`
        is a fixed phrase and what varies rides in `detail` — and report nothing."""
        if self._declined.get(ident.session_id) != gate:
            self._declined[ident.session_id] = gate
            log.info("vendor turn errors: task %s (%s, session %s) not reported: %s%s",
                     ident.task_id, ident.provider, ident.session_id, gate, f" ({detail})" if detail else "")
        return 0

    def _sense_claude_wall(self, ident: runner_registry.RunnerIdentity) -> int:
        """The safety net under a Claude runner's own hooks: a session whose turn ended on an account wall
        (`break_cause.classify_turn_end` — the vendor's error record, its quota banner, or the turn's
        usage-limit grace note) and that has written nothing for `_CLAUDE_WALL_GRACE_SECONDS` was missed by
        `wallcheck`/the Stop hook — it idles on Claude Code's auto-continue until reaped as vanished (Task
        9648). Filed as the hook would, with the same pane witness `_sense` demands for an account cause."""
        if not ident.session_id:
            return 0
        transcript = self._transcript(ident, lambda: session_sweep.resolve_capture(
            ident.session_id, ident.worktree, ident.account_dir, []))
        if transcript is None:
            return 0
        # One `stat` first: a working runner (nearly every tick) never pays for a transcript parse.
        idle_for, newest = _idle_seconds(ident, transcript, ident.account_dir)
        if idle_for < _CLAUDE_WALL_GRACE_SECONDS:
            # Still writing (the grace allowance at work), or the hooks' own replace is in flight.
            return 0
        marker = f"claude-wall@{int(newest)}"
        if (ident.session_id, marker) in self._reported:
            return 0
        cause, detail = session_sweep.break_cause.classify_turn_end(str(transcript))
        if cause not in _ACCOUNT_CAUSES:
            self._declined.pop(ident.session_id, None)
            return 0
        pane = _pane(ident)
        witnessed = pane is not None and _pane_shows_wall(pane)
        # The waiver runs on this daemon's own clock as well as the file's, so a restart never files at once.
        seen_for = time.monotonic() - self._error_seen_at.setdefault((ident.session_id, marker), time.monotonic())
        if not witnessed and min(seen_for, idle_for) < _ERROR_WITHOUT_PANE_SECONDS:
            return self._decline(
                ident, f"turn ended on an account wall ({cause}); waiting for the pane to show it or "
                       f"{_ERROR_WITHOUT_PANE_SECONDS:.0f}s without a write",
                "pane unreadable" if pane is None else "pane shows no wall")
        if not witnessed:
            cause, detail = _UNMATCHED_CAUSE, f"{detail} [no pane witness; nothing written for {idle_for:.0f}s]"
        return self._file(ident, transcript, marker, cause, detail, CLAUDE_WALL_OBSERVED_BY)

    def _transcript(self, ident: runner_registry.RunnerIdentity, resolve) -> Path | None:
        """`ident`'s transcript, resolved once per runner by `resolve()` (a path, a `(path, …)` tuple or None)
        and re-resolved when it vanishes; None — logged once — while there is none to read."""
        transcript = self._transcripts.get(ident.session_id)
        if transcript is not None and not transcript.exists():
            # Replaced under us (a dead pin re-resolved): look again rather than read a vanished path.
            del self._transcripts[ident.session_id]
            transcript = None
        if transcript is not None:
            return transcript
        if time.monotonic() < self._unresolved_until.get(ident.session_id, 0.0):
            return None
        found = resolve()
        transcript = found[0] if isinstance(found, tuple) else found
        if transcript is None:
            self._unresolved_until[ident.session_id] = time.monotonic() + _UNRESOLVED_RETRY_SECONDS
            self._decline(ident, "its transcript is not resolved")
            return None
        self._unresolved_until.pop(ident.session_id, None)
        self._transcripts[ident.session_id] = transcript
        return transcript

    def _sense(self, ident: runner_registry.RunnerIdentity, vm_id: str = "") -> int:
        if not ident.session_id:
            return 0
        adapter = agents.get_adapter(ident.provider)
        if vm_id:
            # Re-resolved every tick, never cached: the guest may swap a directory under a resolved path, and the
            # resolution is what checks every link from its share down.
            self._transcripts.pop(ident.session_id, None)
        transcript = self._transcript(ident, (lambda: _vm_transcript(ident)) if vm_id else (
            lambda: session_sweep.vendor_transcript_path(ident.session_id, ident.worktree, ident.account_dir)))
        if transcript is None:
            return 0
        error = adapter.turn_error(transcript=str(transcript))
        if error is not None and (ident.session_id, error.marker) in self._reported:
            # Already filed (or refused): a runner still here after that is the idle backstop's.
            error = None
        # A VM's other session files are in its own share, which no dispatch note names: its main file alone.
        idle_for, newest = _idle_seconds(ident, transcript, "" if vm_id else ident.account_dir)
        tui = adapter.tui()
        if error is None:
            return self._idle_backstop(ident, transcript, idle_for, newest, tui)
        # The second, independent witness: the CLI is back at an idle composer, not still retrying a turn
        # whose prompt it has not yet written down. Waived once THIS daemon has watched the same errored turn
        # sit unwritten for long enough (always, for a VM: it has no host pane).
        pane = _pane(ident)
        witnessed = pane is not None and tui.is_ready(pane)
        seen_for = time.monotonic() - self._error_seen_at.setdefault(
            (ident.session_id, error.marker), time.monotonic())
        if not witnessed and min(seen_for, idle_for) < _ERROR_WITHOUT_PANE_SECONDS:
            return self._decline(
                ident, "turn ended on a vendor error; waiting for the idle-composer witness or "
                       f"{_ERROR_WITHOUT_PANE_SECONDS:.0f}s without a write",
                "pane unreadable" if pane is None else "pane not at an idle composer")
        cause, detail = session_sweep.break_cause.classify_vendor_error(error.text)
        if cause == session_sweep.break_cause.UNKNOWN:
            cause, detail = _UNMATCHED_CAUSE, error.text
        if not witnessed:
            if cause in _ACCOUNT_CAUSES:
                cause, detail = _UNMATCHED_CAUSE, error.text
            detail = f"{detail} [no idle-composer witness; nothing written for {idle_for:.0f}s]"
        if vm_id and not self._task_active(ident):
            return self._decline(ident, "turn ended on a vendor error, but its VM's task is no longer active")
        return self._file(ident, transcript, error.marker, cause, detail, OBSERVED_BY)

    def _task_active(self, ident: runner_registry.RunnerIdentity) -> bool:
        """Whether `ident`'s task still reads `active`; a failed read is not. A retained VM keeps its share after
        its run ended, so a VM's errored turn is filed only for a run still holding its task."""
        try:
            row = cred_broker.call_as_runner("task.get", {}, ident)
        except Exception:  # noqa: BLE001 — a failed read is uncertainty, never a green light
            return False
        return isinstance(row, dict) and row.get("state") == "active"

    def _idle_backstop(self, ident: runner_registry.RunnerIdentity, transcript: Path, idle_for: float,
                       newest: float, tui) -> int:
        """The autoclose this CLI does not get: a runner idle at its composer for a whole linger with its
        task still `active` never settled, and nothing else will end its attempt."""
        if idle_for < _IDLE_BACKSTOP_SECONDS:
            # Working again: a later idle spell logs afresh and needs two fresh pane reads.
            self._declined.pop(ident.session_id, None)
            self._idle_pane.pop(ident.session_id, None)
            return 0
        idle = f"idle {idle_for:.0f}s"
        if time.monotonic() < self._state_recheck_at.get(ident.session_id, 0.0):
            return 0
        pane = _pane(ident)
        if pane is None:
            return self._decline(ident, "idle a linger, but the pane is unreadable", idle)
        if tui.dialog_up(pane):
            return self._decline(ident, "idle a linger at its question dialog", idle)
        if not tui.is_ready(pane):
            return self._decline(ident, "idle a linger, but the pane is not at an idle composer", idle)
        # The busy marker is one line of text; a tool still running changes the screen (its spinner and
        # timer), so an idle verdict also needs the same screen on two consecutive reads.
        previous, self._idle_pane[ident.session_id] = self._idle_pane.get(ident.session_id), pane
        if previous != pane:
            return self._decline(ident, "idle a linger at its composer; confirming the screen is still", idle)
        marker = f"idle@{int(newest)}"
        if (ident.session_id, marker) in self._reported:
            return 0
        try:
            row = cred_broker.call_as_runner("task.get", {}, ident)
        except Exception as exc:  # noqa: BLE001 — a failed read is uncertainty, never a green light
            return self._decline(ident, "idle a linger, but its task could not be read",
                                 f"{idle}; {type(exc).__name__}: {exc}")
        state = row.get("state") if isinstance(row, dict) else None
        if state != "active" or row.get("persona") == _ADVISOR:
            self._state_recheck_at[ident.session_id] = time.monotonic() + _STATE_RECHECK_SECONDS
            # `awaiting_response` is a runner parked on a question by design; anything else is not ours.
            return self._decline(ident, f"idle a linger, but its task is {state or 'not open'}"
                                        + (" (an advisor chat, never idle-killed)" if state == "active" else ""),
                                 idle)
        detail = (f"{idle} at its composer with its task still active and no terminal call "
                  f"(the node's idle backstop; autoclose cannot judge a {ident.provider} runner)")
        return self._file(ident, transcript, marker, _IDLE_CAUSE, detail, IDLE_OBSERVED_BY)

    def _file(self, ident: runner_registry.RunnerIdentity, transcript: Path, marker: str, cause: str,
              detail: str, observed_by: str) -> int:
        payload = {
            "cause": cause,
            "detail": detail[:1000],
            "session_id": ident.session_id,
            "transcript": str(transcript),
            "observed_by": observed_by,
            "request_replace": True,
        }
        try:
            answer = cred_broker.call_as_runner(_VERB, payload, ident)
        except Exception as exc:  # noqa: BLE001 — classified below; never raised into the tick
            if _refused(exc):
                # The CP heard it and said no (a stale session, a task already moved), or took it: asking
                # again cannot change that, and every tick would re-file it.
                self._reported.add((ident.session_id, marker))
                log.warning("vendor turn errors: task %s's %s runner-break was not accepted as sent: "
                            "%s: %s", ident.task_id, ident.provider, type(exc).__name__, exc)
            else:
                log.warning("vendor turn errors: task %s's %s runner-break did not reach the control "
                            "plane (%s: %s) — retrying next tick", ident.task_id, ident.provider,
                            type(exc).__name__, exc)
            return 0
        self._reported.add((ident.session_id, marker))
        log.warning("vendor turn errors: task %s's %s runner reported by %s (cause=%s: %r) — filed a runner "
                    "break with request_replace; answer=%s",
                    ident.task_id, ident.provider, observed_by, cause, detail[:_LOG_DETAIL_CHARS], answer)
        return 1


def _idle_seconds(ident: runner_registry.RunnerIdentity, transcript: Path, home: str) -> tuple[float, float]:
    """`(seconds since ANY file of this session was last written, that newest mtime)`: the main transcript,
    and — only once it is itself older than the shortest window, so a busy runner costs one `stat` a tick —
    every other file the vendor keeps for the session under `home` (a sub-agent thread can keep working after its
    parent's turn ended; "" = the main file alone). Anything that could not be read answers `0.0` idle: never a
    green light."""
    now = time.time()
    try:
        newest = os.stat(transcript).st_mtime
    except OSError:
        return 0.0, now  # vanished under us: re-resolved next tick, never read as idle
    if home and now - newest >= min(_ERROR_WITHOUT_PANE_SECONDS, _IDLE_BACKSTOP_SECONDS):
        try:
            members, notes, _root = session_sweep.vendor_session_members(ident.session_id, transcript, home)
        except Exception as exc:  # noqa: BLE001 — a failed listing must not read as idle
            log.warning("vendor turn errors: task %s's session files could not be listed (%s: %s)",
                        ident.task_id, type(exc).__name__, exc)
            return 0.0, newest
        if notes:
            # The listing catches its own failures and says so here: an incomplete one is not "idle".
            return 0.0, newest
        for _name, path in members:
            try:
                newest = max(newest, os.stat(path).st_mtime)
            except OSError:
                continue
    return max(0.0, now - newest), newest


def _vm_transcript(ident: runner_registry.RunnerIdentity) -> Path | None:
    """A task VM's transcript: under the VM's own config share beside its worktree (`cred_broker._vm_identity`
    roots the identity at the VM dir), as the guest CLI named it from its guest worktree, after its launch —
    `launch.sh` is written last and starts the session (`microvm_session.launch`)."""
    vdir = Path(ident.worktree).parent
    try:
        since = os.stat(vdir / "launch" / "launch.sh").st_mtime
    except OSError:
        return None  # not launched yet: looked for again after the back-off
    return session_sweep.vm_transcript_path(ident.provider, ident.session_id, str(vdir / "config"),
                                            microvm_seed.FIXED_MOUNTS["worktree"], since)


def _pane(ident: runner_registry.RunnerIdentity) -> str | None:
    """`ident`'s pane as text, or None when unreadable — always for a VM's runner, which has no host pane
    (`pane_pid` 0)."""
    return answer_delivery.pane_text_for_pid(ident.tmux_pane_pid) if ident.pane_pid else None


def _pane_shows_wall(pane: str) -> bool:
    """Does the bottom of a Claude runner's pane show an account wall (its quota banner)? The pane is the
    second, independent witness an account verdict needs (`_ACCOUNT_CAUSES`)."""
    tail = "\n".join([line for line in pane.splitlines() if line.strip()][-_PANE_WALL_TAIL_LINES:])
    return session_sweep.break_cause.classify(tail)[0] in _ACCOUNT_CAUSES


def _refused(exc: Exception) -> bool:
    """True when the report must not be sent again: the control plane refused it (a 4xx that is a verdict,
    or the vocabulary's own refusal) or answered it (a 2xx whose body would not parse). False when it may
    simply not have arrived, or the answer was not about the report (`_RETRYABLE_STATUSES`)."""
    if isinstance(exc, agent_vocabulary.VerbRefused):
        return True
    if isinstance(exc, urllib.error.HTTPError):
        return 400 <= exc.code < 500 and exc.code not in _RETRYABLE_STATUSES
    # An HTTP failure is an `OSError` (`URLError`); what is left raised AFTER a 2xx arrived.
    return isinstance(exc, ValueError)
