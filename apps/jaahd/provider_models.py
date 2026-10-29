"""Report this node's vendor-CLI model catalogues to the control plane — the input a vendor-neutral tier
(`max`/`high`/`standard`/`low`) resolves against for a provider with no vendor-moving aliases.

Claude accepts moving aliases (`opus`), so its tier is a static `tier_aliases` entry.
A provider whose `tier_aliases == ()` has none: its current models are only knowable from its own CLI, so
the node asks that CLI and `POST /api/agent/nodes/model-catalogs` carries `{"catalogs": {provider: [...]}}`.
The CP stores it on `nodes.model_catalogs`; the tier → model rule is `app/model_catalog.py::pick_from_catalog`
and nowhere here. Each entry is reduced to `{slug, visibility, priority}` — nothing else leaves the node.

Per-provider sensing is a table keyed by provider value (`_SENSORS`);
a catalogue provider without a sensor, an account or an answer is OMITTED — absence asserts nothing.

The CLI runs AS THE SESSION USER (`_run_as_account_uid`), never as the daemon: the account home is
runner-writable and the CLI obeys config found there. Its environment is spelled out, never `os.environ`
(the daemon's carries `NODE_TOKEN`), and its output is never logged.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import stat
import shutil
import subprocess
from pathlib import Path
from typing import Callable, Optional

import agent_providers
import control_plane_api as control_plane_api_mod
import fleet_dispatch
import provider_health
import session_user

log = logging.getLogger("jaahd.provider_models")

INTERVAL_SECONDS = 1800.0
_SENSE_TIMEOUT_SECONDS = 90.0
_MAX_MODELS = 200
_SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-]{0,127}$")
_CODEX_MODELS_ARGV = ("debug", "models")


def _reduce(models: object) -> Optional[list[dict]]:
    """`[{slug, visibility, priority}]` from a CLI's raw model list; None when nothing valid survives."""
    if not isinstance(models, list):
        return None
    out: list[dict] = []
    for model in models:
        if not isinstance(model, dict):
            continue
        slug, priority = model.get("slug"), model.get("priority")
        if not isinstance(slug, str) or not _SLUG_RE.match(slug):
            continue
        if not isinstance(priority, int) or isinstance(priority, bool):
            continue
        out.append({"slug": slug, "visibility": str(model.get("visibility") or ""), "priority": priority})
        if len(out) >= _MAX_MODELS:
            break
    return out or None


def _run_as_account_uid(argv: list[str], auth_dir: Path) -> Optional[subprocess.CompletedProcess]:
    """Run a vendor CLI against an account home AS THE SESSION USER — the uid that owns and writes that
    home — never as the daemon. None (fail closed, logged) when this node cannot switch.

    The home is runner-writable: a runner can plant config the CLI obeys (a command-backed provider auth,
    a `.env`), so running it as `jaahd` would hand a runner the daemon uid and `NODE_TOKEN`. Run as the
    session user, a planted config can only lie about a catalogue — bounded by `_reduce`, and read only
    for this node's own dispatches. Same uid already (a dev host, an operator shell): run in-process, which
    grants nothing the runner does not have."""
    env = {provider_health.CODEX_HOME_ENV: str(auth_dir)}
    if session_user.is_session_user():
        binary = shutil.which(argv[0], path=session_user.delegated_path())
        if not binary:
            log.warning("model-catalogs: %s is not on the session user's PATH", argv[0])
            return None
        return subprocess.run(
            [binary, *argv[1:]], capture_output=True, text=True,
            env={**provider_health._probe_env(auth_dir, provider_health.CODEX_HOME_ENV),
                 "PATH": session_user.delegated_path()},
            timeout=_SENSE_TIMEOUT_SECONDS, check=False,
        )
    if not session_user.can_run_as_session_user():
        log.warning("model-catalogs: this node cannot run a command as the session user — %s is not "
                    "sensed (never run as the daemon: the account home is runner-writable)", argv[0])
        return None
    return session_user.run_as_session_user(argv, env=env, timeout=_SENSE_TIMEOUT_SECONDS)


def _sense_codex(auth_dir: Path) -> Optional[list[dict]]:
    """`codex debug models` under this account's home, reduced; None when it could not be asked or read.

    A missing home is answered None without running the CLI (which might create it), as
    `provider_health._codex_healthy` does."""
    try:
        if not stat.S_ISDIR(os.stat(auth_dir).st_mode):
            return None
    except OSError:
        return None
    try:
        proc = _run_as_account_uid([agent_providers.CODEX.binary, *_CODEX_MODELS_ARGV], auth_dir)
    except Exception as exc:  # noqa: BLE001 — TimeoutExpired, OSError: an unanswered sense, never a catalogue
        log.warning("model-catalogs: %s models failed for %s (%s)",
                    agent_providers.CODEX.binary, auth_dir, type(exc).__name__)
        return None
    if proc is None:
        return None
    if proc.returncode != 0:
        log.warning("model-catalogs: %s models exited %s for %s",
                    agent_providers.CODEX.binary, proc.returncode, auth_dir)
        return None
    try:
        payload = json.loads(proc.stdout or "")
    except ValueError:
        log.warning("model-catalogs: %s models returned unparseable JSON for %s",
                    agent_providers.CODEX.binary, auth_dir)
        return None
    return _reduce(payload.get("models") if isinstance(payload, dict) else None)


_SENSORS: dict[str, Callable[[Path], Optional[list[dict]]]] = {
    agent_providers.CODEX.value: _sense_codex,
}


def catalog_providers() -> list[str]:
    """Providers whose tier resolves against a sensed catalogue: those with no static `tier_aliases`."""
    return [p.value for p in agent_providers.PROVIDERS if p.tier_aliases == ()]


def sense_model_catalogs() -> dict[str, list[dict]]:
    """`{provider: [models]}` for each catalogue provider one of whose local accounts answered."""
    wanted = catalog_providers()
    dirs: dict[str, list[str]] = {provider: [] for provider in wanted}
    # The daemon's shared, TTL-floored roster read (`reconcile.accounts_probe.account_roster`), never a second GET of
    # its own: one idle pass makes at most one call per CP endpoint. Lazy: `reconcile` is heavy.
    from reconcile import accounts_probe  # noqa: PLC0415

    rows, _ok = accounts_probe.account_roster()
    for rec in rows:
        if not isinstance(rec, dict) or rec.get("provider") not in dirs:
            continue
        if rec.get("enabled", True) is False:
            continue
        local_dir = fleet_dispatch.local_account_dir(rec.get("config_dir"), provider=rec.get("provider"))
        if local_dir:
            dirs[rec["provider"]].append(local_dir)
    catalogs: dict[str, list[dict]] = {}
    for provider, local_dirs in dirs.items():
        sensor = _SENSORS.get(provider)
        if sensor is None:
            log.warning("model-catalogs: no sensor for provider %r — its catalogue stays unreported", provider)
            continue
        for local_dir in local_dirs:
            models = sensor(Path(local_dir))
            if models is not None:
                catalogs[provider] = models
                break
    return catalogs


class ModelCatalogReporter:
    """One `PeriodicStep`: sense every catalogue provider and POST whatever answered."""

    def __init__(
        self,
        *,
        sense: Callable[[], dict] = sense_model_catalogs,
        post: Callable[[dict], bool] | None = None,
    ) -> None:
        self._sense = sense
        self._post = post or control_plane_api_mod.agent_api_node_model_catalogs

    def pass_once(self) -> bool:
        """One pass, synchronously. Returns whether a report was sent; never raises."""
        try:
            catalogs = self._sense()
            if not catalogs:
                return False
            return bool(self._post(catalogs))
        except Exception as exc:  # noqa: BLE001 — a PeriodicStep must never take the daemon down
            log.warning("model-catalogs: pass failed (%s)", type(exc).__name__)
            return False

    async def step(self) -> None:
        await asyncio.to_thread(self.pass_once)
