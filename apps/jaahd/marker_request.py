"""The shared mechanics of asking a ROOT systemd path unit for work by creating an empty marker file —
the one home of what `upgrade_request.py`, `gha_request.py` and `nodelink_request.py` share (the
posture gate, the marker's path and its sandbox refusal, the monotonic floor's arithmetic, the write
and every operator sentence about it: `MarkerChannel`), so each of those modules is a thin declaration
plus its own throttle ladder. `uninstall_request.py`, `root_grow_request.py` and `disk_reclaim_trigger.py`
use only where their marker lives (`marker_path`) and the node-posture gates (`requests_enabled`,
`root_request_enabled`).

The marker is EMPTY — a security property: anything this unprivileged process wrote into it would
become attacker-chosen input to a root unit (apt versions, runner argv, mount options). Every parameter
reaches root only from the CP, over the root unit's OWN credential. Never put diagnostics in it.

Stdlib at import; the posture gate and the paths read the daemon's `environment` lazily.
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger("jaahd.marker_request")


@dataclass(frozen=True)
class MarkerRequestResult:
    """What one request call did, in the terms an OPERATOR needs — the shape the console verbs render.

    `requested` — a marker was written. The gates are answered once, by the request call (a caller
    probing first would consume the throttle it probed).
    `reason` — a self-contained operator sentence; empty only on an unqualified success.
    `retry_after` — seconds until a throttled ask could succeed.
    `stale_marker_age` — the prior marker sat unconsumed past the channel's `stale_after`:
    `requested` is True yet nothing watches the path, so a bool-only caller would wrongly show success.
    `queued` — refused by the floor but kept for a later re-ask (`nodelink_request.step`)."""

    requested: bool
    reason: str
    retry_after: float | None = None
    stale_marker_age: float | None = None
    queued: bool = False


def write_marker(
    target: Path, *, now: float, stale_after: float, preserve_age: bool = True,
) -> tuple[float | None, OSError | None]:
    """Create `target` EMPTY; answer `(stale_age, error)`. Never raises `OSError`.

    `stale_age` — the age of a prior marker older than `stale_after` (the root unit's `TimeoutStartSec`),
    the only "nothing is watching" detector. Tested by AGE, not existence: a marker written mid-converge
    legitimately waits. Measured before the write, so it is reported even when the write fails.
    `error` — the `OSError` that stopped the write, or None.

    `preserve_age` restores the old mtime so the age survives our own rewrite (else a second click clears
    the alarm); truncation still happens every ask, and a failed utime costs only the detector's memory."""
    try:
        existing_mtime: float | None = target.stat().st_mtime
    except OSError:
        existing_mtime = None   # absent (the normal case) or unstattable — either way, nothing to report
    age = None if existing_mtime is None else now - existing_mtime
    stale_age = age if age is not None and age > stale_after else None
    try:
        # `write_bytes(b"")`, not `touch()`: touch does not truncate, and the marker must be empty.
        target.write_bytes(b"")
    except OSError as exc:
        return stale_age, exc
    if preserve_age and existing_mtime is not None:
        try:
            os.utime(target, (existing_mtime, existing_mtime))
        except OSError as exc:
            log.debug("marker-request: could not preserve %s's age: %s", target, exc)
    return stale_age, None


def marker_path(env_name: str, parts: tuple[str, ...], *, sandbox_refusal: str | None = None) -> Path:
    """`$<env_name>`, else `<run root>/<parts>` (the directory is jaahd.service's ExecStartPre — never
    created here). With `sandbox_refusal`, sandbox posture and no override RAISES
    `environment.SandboxSurfaceDisabled(sandbox_refusal)` — the markers that start a root unit."""
    import environment  # noqa: PLC0415 — lazy: the write helpers above stay stdlib-only
    from jaah_env import env_get  # noqa: PLC0415

    explicit = env_get(env_name)
    if explicit:
        return Path(explicit)
    if sandbox_refusal is not None and environment.is_sandbox():
        raise environment.SandboxSurfaceDisabled(sandbox_refusal)
    return Path(environment.run_path(*parts))


def requests_enabled(env_name: str) -> bool:
    """May this process ask a root unit for work through the marker `$<env_name>` overrides?

    False in sandbox posture with no explicit marker: the marker starts a ROOT unit on a machine the
    sandbox does not own, and a periodic pass would write it unasked. Disabled, not redirected (a
    redirected marker reports a request nothing watches). An explicit override re-enables it in any
    posture, against a path of the operator's own. Always False on a community-edition node, which has no root unit
    at all (no NodeLink, GHA pool, package upgrade, reclaim, root grow or uninstall to converge)."""
    import edition  # noqa: PLC0415
    import environment  # noqa: PLC0415
    from jaah_env import env_get  # noqa: PLC0415

    if edition.IS_CE:
        return False
    return bool(env_get(env_name)) or not environment.is_sandbox()


def root_request_enabled(env_name: str) -> bool:
    """`requests_enabled`, and never on a virtual node (the package and the filesystem are the HOST's —
    one tenant must never act for every vnode) or off Linux. An unparseable vnode flag fails closed."""
    import environment  # noqa: PLC0415
    import host_platform  # noqa: PLC0415

    if not host_platform.is_linux():
        return False
    try:
        if environment.is_vnode():
            return False
    except RuntimeError:
        return False
    return requests_enabled(env_name)


def inside_floor(last: float | None, floor: float, now: float) -> tuple[float, float] | None:
    """`(since, retry_after)` when `now` (monotonic) is within `floor` seconds of `last`, else None — the
    arithmetic of every channel's floor and repeat gate, so "asked Xs ago, try again in Ys" is one sum."""
    if last is None or now - last >= floor:
        return None
    since = now - last
    return since, floor - since


@dataclass(frozen=True)
class MarkerChannel:
    """One marker-request channel's declaration — what a `*_request` module says about ITS marker, from
    which the gate, the path, the write and every operator sentence follow.

    `env` — the override, named by every refusal as the one way out. `parts` — the marker under
    `environment.run_root()`. `watcher` — the root unit consuming it; `check` — how an operator confirms
    it is enabled (empty: the systemd spelling). `stale_after` — the watcher's `TimeoutStartSec` (single
    home; move together): older than that, unconsumed, means nothing is watching. The nouns fill the
    sentences: `noun` the marker, `what` the thing asked for, `asks` what root would do, `owns` what only
    a provisioned node converges, `resets` what a change of does not reset the floor; the two
    `*_consequence` strings end the failure sentences with what the node does meanwhile."""

    log_prefix: str
    env: str
    parts: tuple[str, ...]
    watcher: str
    stale_after: float
    noun: str
    what: str
    asks: str
    owns: str
    resets: str
    stale_consequence: str
    write_failure_consequence: str
    check: str = ""

    @property
    def check_cmd(self) -> str:
        return self.check or f"systemctl is-enabled {self.watcher}"

    def enabled(self) -> bool:
        """`requests_enabled` for this channel's override."""
        return requests_enabled(self.env)

    def path(self) -> Path:
        """`marker_path` for this channel: raises `environment.SandboxSurfaceDisabled` in sandbox posture
        with no override — the second line of defence behind `enabled`."""
        return marker_path(self.env, self.parts, sandbox_refusal=(
            f"sandbox posture disables the {self.noun} outright: creating it would ask the root "
            f"{self.watcher} unit to {self.asks} on a machine this process does not own. Set {self.env} "
            f"to exercise the write against a path of your own."))

    def sandbox_refusal(self) -> MarkerRequestResult:
        """The refusal when `enabled()` is False. No `retry_after`: not a throttle, waiting cannot help."""
        import edition  # noqa: PLC0415

        if edition.IS_CE:
            log.info("%s: the community edition has no %s — no request", self.log_prefix, self.noun)
            return MarkerRequestResult(
                requested=False,
                reason=f"the community edition runs no root {self.watcher} unit, so it never asks to {self.asks}",
            )
        log.info("%s: sandbox posture disables the %s — no request", self.log_prefix, self.noun)
        return MarkerRequestResult(
            requested=False,
            reason=(
                f"this process is running in sandbox posture, which disables the {self.noun} outright "
                "rather than redirecting it, so nothing was requested and no amount of retrying will "
                f"change that: creating the marker asks a ROOT path unit to {self.asks} on a machine "
                "this process does not own, and a marker written anywhere else would be reported as a "
                f"request that nothing is watching. Only a provisioned fleet node converges {self.owns}; "
                f"set {self.env} to a path of your own to exercise the write here."
            ),
        )

    def floor_refusal(self, last: float | None, floor: float, now: float) -> MarkerRequestResult | None:
        """The refusal when `now` is inside `floor` of `last` (`inside_floor`), else None — the gate a
        change of `resets` does not reset, so an oscillating desired state cannot hot-loop root."""
        hit = inside_floor(last, floor, now)
        if hit is None:
            return None
        since, retry_after = hit
        log.debug("%s: this process asked %.1fs ago (floor %.0fs) — not re-asking",
                  self.log_prefix, since, floor)
        return MarkerRequestResult(
            requested=False,
            reason=(
                f"this node's daemon already asked for {self.what} {since:.1f}s ago, inside the "
                f"{floor:.0f}s floor that a change of {self.resets} does not reset. Try again in "
                f"{retry_after:.1f}s."
            ),
            retry_after=retry_after,
        )

    def write(self, target: Path, *, now: float, preserve_age: bool = True,
              detail: str = "") -> MarkerRequestResult:
        """`write_marker`, reported: a failed write is a refusal naming the likeliest cause (postinst's
        `ExecStartPre` never made the directory; this daemon deliberately creates and chowns nothing); a
        stale prior marker is a WARNING and rides a success (`stale_marker_age`) — the caller decides
        whether a request nothing watches is a refusal. `detail` joins the success log line."""
        import environment  # noqa: PLC0415

        stale_marker_age, error = write_marker(target, now=now, stale_after=self.stale_after,
                                               preserve_age=preserve_age)
        if stale_marker_age is not None:
            # Likely cause: postinst's `enable --now <watcher>` is `|| true`.
            log.warning(
                "%s: %s has existed unconsumed for %.0fs (longer than any converge can run) — no root "
                "unit is taking it, so %s is probably not enabled on this node (check: %s).",
                self.log_prefix, target, stale_marker_age, self.watcher, self.check_cmd,
            )
        if error is not None:
            parent = environment.run_path(self.parts[0])
            log.warning("%s: could not create the %s %s: %s — %s (is jaahd.service's ExecStartPre "
                        "creating %s?)", self.log_prefix, self.noun, target, error,
                        self.write_failure_consequence, parent)
            return MarkerRequestResult(
                requested=False,
                reason=(
                    f"could not create the {self.noun} {target}: {error}. The likeliest cause is that "
                    f"jaahd.service's ExecStartPre never created the containing directory ({parent}); "
                    f"this daemon deliberately does not create or chown it. {self.write_failure_consequence}"
                ),
            )
        log.info("%s: wrote %s for %s%s", self.log_prefix, target, self.watcher, detail)
        if stale_marker_age is not None:
            return MarkerRequestResult(
                requested=True,
                reason=(
                    f"{self.what} was requested, but the previous marker at {target} had sat unconsumed "
                    f"for {stale_marker_age:.0f}s — longer than any converge can run, so no root unit is "
                    f"taking it. {self.watcher} is probably not enabled on this node (check: "
                    f"{self.check_cmd}); {self.stale_consequence}"
                ),
                stale_marker_age=stale_marker_age,
            )
        return MarkerRequestResult(requested=True, reason="")
