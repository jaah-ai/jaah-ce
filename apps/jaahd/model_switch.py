"""Switch a live pane runner's model in place — the vendor-neutral MECHANICS half of `switch_model`.

The CP decides whether and for which session (`app/fleet_verdicts.py`); `reconcile.enact_signal._enact_switch_model`
fences the session and asks the runner's adapter for its driver (`AgentAdapter.model_switch`: None = this CLI has
no in-place switch). An ACP session never reaches here: its host takes a `set_model` control (`acp/host.py`). This
module proves the window with the gates every pane write shares (`answer_delivery._proven_window`), refuses a pane
running a shell, and hands the driver a `_TmuxTarget`; HOW each CLI switches lives on its adapter.

The driver answers `(status, reason)`, status one of `agents.base.MODEL_SWITCH_STATUSES` (re-exported here). Never
raises. `SavedDefault` is the per-account record of an account default a driver must put back (`AgentAdapter
.model_switch`), kept in the daemon's own state dir — a runner cannot plant the value restored — and dropped when the
last intent holding it retires; a hold a superseded intent left behind ages out (`_SAVED_MAX_AGE_SECS`).
"""
from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import re
import subprocess
import time
from typing import Callable

import answer_delivery
import env_helpers
import host_platform
from agents.base import MODEL_SWITCH_BUSY, MODEL_SWITCH_RETRY, MODEL_SWITCH_UNSUPPORTED, MODEL_SWITCHED

log = logging.getLogger("jaahd.model-switch")

__all__ = ["BUSY", "MODEL_RE", "RETRY", "SWITCHED", "SavedDefault", "UNSUPPORTED", "switch_model"]

SWITCHED = MODEL_SWITCHED
BUSY = MODEL_SWITCH_BUSY
RETRY = MODEL_SWITCH_RETRY
UNSUPPORTED = MODEL_SWITCH_UNSUPPORTED

# What a model id may be: it is TYPED into a terminal, so no whitespace, quote, control or shell character.
MODEL_RE = re.compile(r"^[A-Za-z0-9._:/\[\]@-]{1,128}$")

# Where `SavedDefault` keeps its records (under `env_helpers.daemon_state_dir`), and how long a holder outlives its
# last write: a superseded intent is never retired here, so its hold (and a record nobody refreshes) ages out.
_SAVED_DIR = "model-switch"
_SAVED_MAX_AGE_SECS = 24 * 3600


class SavedDefault:
    """The record of the account default a switch overwrites, keyed by the ACCOUNT (its resolved config dir), not
    the intent: every run of one account on this node shares the file the CLI writes, so a second switch — a
    superseding intent, another run of the same account — must restore the same TRUE original, never the value an
    in-flight switch left. `first(present, value)` keeps the FIRST pair offered while any switch holds the record
    (`present` False: the account had no such default) and adds this intent as a holder; `forget()` (the intent
    retired, its driver's `finally` having restored) drops the hold, and the record with its last holder. Each
    read-modify-write runs under an exclusive lock; every write is atomic."""

    def __init__(self, intent_id: object, *, config_dir: str, root=None) -> None:
        key = hashlib.sha256(os.path.realpath(config_dir or "").encode()).hexdigest()[:32]
        self.path = (root or env_helpers.daemon_state_dir(_SAVED_DIR)) / f"{key}.json"
        self.holder = str(intent_id)

    def first(self, present: bool, value: object) -> tuple[bool, object]:
        try:
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            with self._locked():
                held, holders = self._read()
                if held is not None and holders:
                    present, value = held
                holders[self.holder] = time.time()
                _sweep(self.path.parent)
                env_helpers.atomic_write_json(self.path, {
                    "present": present, "value": value if present else None, "holders": holders}, mode=0o600)
        except OSError as exc:
            log.warning("model-switch: could not record the account default in %s (%s)", self.path.name, exc)
        return present, value

    def forget(self) -> None:
        try:
            with self._locked():
                held, holders = self._read()
                holders.pop(self.holder, None)
                if held is None or not holders:
                    self.path.unlink(missing_ok=True)
                else:
                    env_helpers.atomic_write_json(self.path, {
                        "present": held[0], "value": held[1], "holders": holders}, mode=0o600)
        except OSError:
            pass

    def _read(self) -> "tuple[tuple[bool, object] | None, dict[str, float]]":
        """`((present, value) | None, live holders)`: a missing or unreadable record is None; a holder past
        `_SAVED_MAX_AGE_SECS` is dropped (a superseded intent's)."""
        try:
            raw = json.loads(self.path.read_text())
            held = (bool(raw["present"]), raw.get("value"))
            holders = raw.get("holders")
        except (OSError, ValueError, KeyError, TypeError):
            return None, {}
        cut = time.time() - _SAVED_MAX_AGE_SECS
        live = {str(k): float(t) for k, t in holders.items()
                if isinstance(t, (int, float)) and t >= cut} if isinstance(holders, dict) else {}
        return held, live

    @contextlib.contextmanager
    def _locked(self):
        with open(self.path.with_suffix(".lock"), "a", opener=lambda p, f: os.open(p, f, 0o600)) as handle:
            host_platform.lock(handle)
            yield


def _sweep(folder) -> None:
    """Drop records nobody has written for `_SAVED_MAX_AGE_SECS` (every holder superseded). Best effort."""
    cut = time.time() - _SAVED_MAX_AGE_SECS
    for entry in folder.glob("*.json"):
        try:
            if entry.stat().st_mtime < cut:
                entry.unlink()
        except OSError:
            continue


def switch_model(
    tmux_target: str,
    model: str,
    *,
    expect_cwd: str,
    driver: "Callable[..., tuple[str, str]] | None",
    config_dir: str = "",
    saved: "SavedDefault | None" = None,
    run_fn=subprocess.run,
    sleep_fn=time.sleep,
) -> tuple[str, str]:
    """Switch the runner in TMUX_TARGET to MODEL with its adapter's DRIVER. `(status, reason)` as the module says.

    An unsafe model or a missing driver is `UNSUPPORTED` before any pane is touched. An unprovable window, or a pane
    running a shell, is `RETRY`, as a pane write's is re-served. `config_dir` is the run's account config dir
    (`RunnerIdentity.account_dir`), for a driver that must put back what its CLI saves there; `saved` the intent's
    record of what that was (`SavedDefault`)."""
    if not MODEL_RE.match(model or ""):
        return UNSUPPORTED, "the model id is not a safe token"
    if driver is None:
        return UNSUPPORTED, "this CLI has no in-place model switch"
    window = answer_delivery._proven_window(tmux_target, expect_cwd, run_fn, "model-switch")
    if window is None:
        return RETRY, "the runner's window could not be proven"
    pane = answer_delivery._TmuxTarget(*window, run_fn)
    pane.label = tmux_target
    if pane.command() in answer_delivery._SHELLS:
        return RETRY, "the pane runs a shell, not the runner's CLI"
    status, why = driver(pane, model, config_dir=config_dir, sleep_fn=sleep_fn, saved=saved)
    if status == SWITCHED:
        log.info("model-switch: %s switched to %s", tmux_target, model)
    return status, why
