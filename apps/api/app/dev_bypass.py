"""The api's posture around the insecure-dev opt-in: the fixture/local env reads and the boot refusal.

The opt-in parse itself (`TRUTHY`, `env_flag_enabled`, `insecure_agent_enabled`) has ONE home, the node's
`apps/jaahd/jaah_env.py`, borrowed by path through `app/jaah_env.py` and re-exported here under the same names
for every agent-surface auth check (`app/auth.py::_valid_node_token`, `app/node_bus.py::token_ok`,
`app/routes/agent/auth.py::_check_token`, `app/node_credentials.py`).

Imports nothing else from `app` (`auth.py`/`node_bus.py` are imported everywhere; a cycle otherwise). Every
read happens at call time, never at import, so a rotation needs no restart.
"""
from __future__ import annotations

import ipaddress
import logging
import os

from app.jaah_env import TRUTHY, env_flag_enabled, insecure_agent_enabled  # noqa: F401 — re-exported, same names

# APP_ENV values meaning a throwaway environment: `local` (`./dc`), `test` (pytest), `ci`. Anything else,
# including `production` and unset, is real. An allowlist, so the unforeseen lands outside. Read by
# `app/seed.py` (fixture logins) and `app/credential_keys.py` (a published keypair).
FIXTURE_APP_ENVS = frozenset({"local", "ci", "test"})


def is_fixture_env() -> bool:
    """Does this process affirmatively declare itself throwaway (`FIXTURE_APP_ENVS`)? Absent denies."""
    return os.environ.get("APP_ENV", "").strip().lower() in FIXTURE_APP_ENVS


def is_local_env() -> bool:
    """Does this process affirmatively declare itself a local dev stack?

    An absent `APP_ENV` means not local (see `app/console_direct.py::origin_override`): a
    `get("APP_ENV", "local")` default would make the gate inert for any marker-less process.
    """
    return os.environ.get("APP_ENV", "").strip().lower() == "local"


def is_loopback_address(raw: str | None) -> bool:
    """Is this one bind address loopback? `127.0.0.0/8`, `::1` (bare or `[::1]`), or `localhost`.

    Anything else, including empty, is treated as routable (allowlist direction). `localhost` is
    trusted by name, not resolved: repointing it needs root, the same privilege as editing `.env`.
    """
    value = (raw or "").strip()
    if value.lower() == "localhost":
        return True
    if value.startswith("[") and value.endswith("]"):
        value = value[1:-1]
    try:
        return ipaddress.ip_address(value).is_loopback
    except ValueError:
        return False


# The variables describing where compose publishes a path into this API (the local compose stack):
#   API_BIND / FRONTEND_BIND — the api and vite publishes (vite proxies `/api`, so it counts too).
#   TAILNET_BIND — non-empty means the tailnet overlay is loaded (`./dc` loads it only on an explicit
#                  export, never derived); judged alone and conservatively.
_ALWAYS_A_PUBLISH = ("API_BIND", "FRONTEND_BIND")
_OVERLAY_PUBLISH = "TAILNET_BIND"


def non_loopback_publishes() -> list[tuple[str, str]]:
    """Every published path into this api that is not loopback, as `(variable, observed)` pairs — empty
    when reachable from loopback alone.

    An absent base publish is reported as `<unset>` and counts (fail-closed); `TAILNET_BIND` is judged
    only when non-empty. Cannot see publishes the process is not told about (`tailscale serve`, subnet
    routes into the docker bridge), so the flag stays a local-dev opt-in only.
    """
    offending: list[tuple[str, str]] = []
    for name in _ALWAYS_A_PUBLISH:
        raw = os.environ.get(name)
        if not is_loopback_address(raw):
            offending.append((name, (raw or "").strip() or "<unset>"))
    overlay = (os.environ.get(_OVERLAY_PUBLISH) or "").strip()
    if overlay and not is_loopback_address(overlay):
        offending.append((_OVERLAY_PUBLISH, overlay))
    return offending


def assert_agent_auth_posture() -> None:
    """Refuse to start when the insecure-agent bypass is on outside a self-declared local stack or on a
    stack published on a non-loopback interface; announce it when legitimately on.

    Called from `app/main.py::create_app`. Both conditions are required: `APP_ENV` says nothing about
    the publish interface, and a loaded tailnet overlay (an exported `TAILNET_BIND`) publishes off
    loopback, so `APP_ENV=local` alone could expose the tokenless agent surface to the whole tailnet.

    The legal case logs one WARNING at boot, after both refusals pass: the flag alone opens every
    `/api/agent/*` route and both node WebSockets to any local uid on a pool host.
    """
    if insecure_agent_enabled() and not is_local_env():
        # Names the variable and observed APP_ENV (never a secret) so the log line alone is actionable.
        raise RuntimeError(
            "ALLOW_INSECURE_AGENT is set to an affirmative value but APP_ENV is "
            f"{os.environ.get('APP_ENV') or '<unset>'!r}, not 'local' — refusing to start. That flag "
            "makes every /api/agent/* route and both node WebSockets accept ANY caller. Unset it — "
            "every node authenticates with its OWN per-node token and there is no fleet-wide "
            "NODE_TOKEN left to configure in its place (an absent APP_ENV counts as not-local by "
            "design)."
        )
    if insecure_agent_enabled() and (offending := non_loopback_publishes()):
        # Each variable and its observed address, so the fix is legible from the line.
        observed = ", ".join(f"{name}={value!r}" for name, value in offending)
        raise RuntimeError(
            f"ALLOW_INSECURE_AGENT is set to an affirmative value but the api is published off loopback "
            f"({observed}) — refusing to start. That flag makes every /api/agent/* route and both node "
            "WebSockets accept ANY caller, and a non-loopback publish — the api's own port, or the "
            "frontend whose dev server proxies /api to it — hands them to every host that can route "
            "here; on a tailnet host that is every peer. Unset the flag (the fix on any stack other "
            "nodes must reach), or publish on loopback only: API_BIND and FRONTEND_BIND at their "
            "127.0.0.1 defaults, and TAILNET_BIND unexported in the shell that runs ./dc, which loads "
            "the tailnet overlay only on that export and never reads a .env line for it (an absent "
            "API_BIND or FRONTEND_BIND counts as not-loopback by design)."
        )
    if insecure_agent_enabled():
        # Reached only after both refusals passed.
        logging.getLogger("dev-bypass").warning(
            "ALLOW_INSECURE_AGENT is ON, APP_ENV is 'local' and every publish is loopback: every "
            "/api/agent/* route and both node WebSockets on this stack accept ANY caller, with no token "
            "at all. That is legal here and nothing else is now stopping it — the fleet-wide token arm "
            "that used to outrank this flag is deleted. Unset it unless you meant it."
        )
