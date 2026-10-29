"""The ACP registry — browsing it and pinning one of its agents as an org's `agents` row (D-REG-1, R11).

The registry (`REGISTRY_URL`, agentclientprotocol.com) lists ACP agents with their distributions. Each agent is a
row of the provider grid, `acp:<registry id>` with `api_key` (`app/provider_auth_policy.py`): an org with any such row
on browses the registry (`routes/acp_registry.py`), seeing the agents whose row is on, and an org admin installs
one whose row is on: the install re-reads the registry here, server-side, and writes an ordinary `agents` row (no new table)
whose `manifest` is `{"kind": "acp_registry", "registry_url", "agent": <the entry's kept fields>, "pin":
{"version", "distribution"}, "key_delivery": "acp_authenticate", "profile": "acp-core"}` (`row_fields`).

The rules, each with one home below:

* The fetch: a short timeout, a capped read, cached in-process `CACHE_SECONDS`; any failure is
  `RegistryUnavailable` (the route's 502 `FETCH_FAILED`). `_download` is the seam a test replaces.
* The distribution (`pick_distribution`): Linux only, `binary` (any `linux-*` target carrying a sha256, only
  those kept) over `npx` over `uvx`, each only when usable: a package spec is a bare npm/PyPI name (never a
  URL, path, git or option), and it always names the exact version (appended when the registry's does not).
  None usable: `NO_CHECKSUM` when only unchecksummed Linux binaries were offered, else `NO_LINUX`. A
  distribution's `env` keeps no name the supervisor sets or that steers the loader or interpreter (`_safe_env`).
* The key route (`key_route`): an agent that can only take its key from the environment is refused
  (`ENV_ONLY`) — the key reaches a registry agent through ACP `authenticate`, never an env var.
* A row is pinned: no ref moves it and the refresh sweep skips it (`is_registry`, `pinned_sentence`).
* Dispatch (`fleet_verdicts.agent_launch`): the row runs under `jaah-acp-host` like a manifest 2 agent, on a node
  whose host installs registry agents, but carries no bundle: the launch names the pin, and the host installs it
  into the attempt's run dir as the session user (`apps/jaahd/acp_registry_install.py`) before it starts the engine.
* The key binding (`KEY_ACCOUNT`, `bound_account`, `key_account_refusal`): an org admin binds one of the org's
  enabled API-key accounts to the row (`manifest["key_account_id"]`, set by `PATCH /api/agents/{id}`). At
  dispatch the slot is leased that account and the launch names its slug (`lease_drain._dispatch_agent`); the ACP
  host fetches the key from the node broker and sends it in ACP `authenticate` before `session/new`
  (`apps/jaahd/acp/host.py`). A binding that stopped being usable — or whose account's `(provider, auth_kind)` pair,
  or the agent's own `acp:` row, the org's provider grid no longer runs — refuses the run, named
  (`agent_resolution._unrunnable`).
"""
from __future__ import annotations

import re
import shlex
import threading
import time
from datetime import datetime
from typing import Any, Optional
from uuid import UUID

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import account_auth, agents, http_bounded
from app.entity_shape import optional_uuid
from app.models.credentials import Account

REGISTRY_URL = "https://cdn.agentclientprotocol.com/registry/v1/latest/registry.json"
#: `agents.manifest.kind` of a registry row.
MANIFEST_KIND = agents.REGISTRY_KIND
#: How a registry agent receives its key: ACP `authenticate`, never the environment.
KEY_DELIVERY = "acp_authenticate"
#: `key_route`'s answer for an agent whose every key route is an environment variable (refused).
ENV_ROUTE = "env"
PROFILE = "acp-core"

CACHE_SECONDS = 300
_TIMEOUT_SECONDS = 5.0
_MAX_BYTES = 4 * 1024 * 1024

ENV_ONLY = "Needs key in environment, not supported"
NO_LINUX = "No Linux build in the registry"
NO_CHECKSUM = "No checksummed Linux build in the registry"
FETCH_FAILED = "Could not reach the ACP registry; try again"

#: Distribution kinds in preference order.
KINDS = ("binary", "npx", "uvx")
#: An environment variable that carries a key.
_KEY_VAR = re.compile(r"(?i)(API_?KEY|TOKEN|SECRET)$")
#: A registry id usable as an agent slug (and a path segment: `..` is refused on top).
_ID = re.compile(r"^[a-z0-9][a-z0-9._-]{0,99}$")


def is_registry_id(value: str) -> bool:
    """`value` is shaped like a registry id (`_ID`, no `..`): what the provider grid accepts after `acp:` without
    fetching the registry (`app/provider_auth_policy.py`)."""
    return bool(_ID.fullmatch(value)) and ".." not in value


#: A version written into a package spec (one leading `v` stripped first); digit first, so a dist-tag such as
#: `latest` is not a pin.
_VERSION = re.compile(r"^[0-9][0-9A-Za-z.+-]*$")
#: A bare npm package name, optionally scoped.
_NPM_NAME = re.compile(r"^(@[a-z0-9][a-z0-9._-]*/)?[a-z0-9][a-z0-9._-]*$")
_PYPI = r"[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?"
#: A bare PyPI name, optionally with extras (`name[a,b]`).
_PYPI_NAME = re.compile(rf"^{_PYPI}(?:\[{_PYPI}(?:,{_PYPI})*\])?$")
_SHA256 = re.compile(r"^[0-9a-fA-F]{64}$")
#: Environment names a registry distribution may not set: they steer the loader, the interpreter or the shell.
_ENV_DENY = frozenset({
    "PATH", "NODE_OPTIONS", "NODE_PATH", "PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP", "HOME", "SHELL",
    "BASH_ENV", "ENV",
})
_ENV_DENY_PREFIXES = ("LD_", "DYLD_")
_ENV_MAX_VALUE = 4096
#: `manifest.agent`: the entry fields kept, and the caps on the free text among them.
_AGENT_FIELDS = (
    "id", "name", "version", "description", "authors", "license", "license_url", "website", "repository", "icon",
)
_DESCRIPTION_MAX = 2000
_AUTHORS_MAX = 20
_AUTHOR_MAX = 200
#: Every other kept string (version, license, links), and the `authMethods` kept: how many, which string keys.
_FIELD_MAX = 2000
_METHODS_MAX = 10
_METHOD_FIELDS = ("id", "type", "name", "description", "varName")


class RegistryUnavailable(Exception):
    """The registry could not be read (network, status, body)."""


class Refused(Exception):
    """An entry that cannot be installed; `sentence` says why."""

    def __init__(self, sentence: str) -> None:
        super().__init__(sentence)
        self.sentence = sentence


# ── Fetch ───────────────────────────────────────────────────────────────────────────────────────────

_cache: Optional[tuple[float, list[dict[str, Any]]]] = None
_cache_lock = threading.Lock()


def _download() -> bytes:
    """The registry's raw body. The seam tests replace (no network in tests)."""
    with http_bounded.client(_TIMEOUT_SECONDS, {"Accept": "application/json"}) as client:
        with client.stream("GET", REGISTRY_URL) as response:
            if response.status_code != 200:
                raise RegistryUnavailable(f"HTTP {response.status_code}")
            raw, truncated = http_bounded.read_capped(response, _MAX_BYTES)
    if truncated:
        raise RegistryUnavailable("body too large")
    return raw


def fetch() -> list[dict[str, Any]]:
    """The registry's entries, from the in-process cache when it is under `CACHE_SECONDS` old."""
    global _cache
    with _cache_lock:
        if _cache is not None and time.monotonic() - _cache[0] < CACHE_SECONDS:
            return _cache[1]
    try:
        entries = parse(http_bounded.json_or_none(_download()))
    except (httpx.HTTPError, OSError) as exc:
        raise RegistryUnavailable(str(exc)) from exc
    with _cache_lock:
        _cache = (time.monotonic(), entries)
    return entries


def clear_cache() -> None:
    global _cache
    with _cache_lock:
        _cache = None


def parse(document: Any) -> list[dict[str, Any]]:
    """The usable entries of a registry document: a dict carrying string `id` (slug-shaped), `name` and
    `version` and a dict `distribution`. Anything else is dropped; a document that is not a registry raises."""
    if not isinstance(document, dict) or not isinstance(document.get("agents"), list):
        raise RegistryUnavailable("not a registry document")
    return [
        entry for entry in document["agents"]
        if isinstance(entry, dict)
        and isinstance(entry.get("id"), str) and is_registry_id(entry["id"])
        and all(isinstance(entry.get(field), str) and entry[field].strip() for field in ("name", "version"))
        and isinstance(entry.get("distribution"), dict)
    ]


def find(entries: list[dict[str, Any]], registry_id: str) -> Optional[dict[str, Any]]:
    return next((entry for entry in entries if entry["id"] == registry_id), None)


# ── Distribution and key route ──────────────────────────────────────────────────────────────────────


def _str_list(value: Any) -> list[str]:
    return [str(item) for item in value] if isinstance(value, list) else []


def _safe_env(value: Any) -> dict[str, str]:
    """A distribution's `env` without the names it may not set: the supervisor's (`routes/agents.py`'s
    reserved set) and the loader's, interpreter's and shell's (`_ENV_DENY`). Non-string and over-long
    values are dropped."""
    from app.routes.agents import _ENV_RESERVED, _ENV_RESERVED_PREFIX  # routes.agents imports this module

    if not isinstance(value, dict):
        return {}
    return {
        name: text for name, text in value.items()
        if isinstance(name, str) and isinstance(text, str) and len(text) <= _ENV_MAX_VALUE
        and name not in _ENV_RESERVED and not name.startswith(_ENV_RESERVED_PREFIX)
        and name.upper() not in _ENV_DENY and not name.upper().startswith(_ENV_DENY_PREFIXES)
    }


def _version(text: str) -> Optional[str]:
    """`text` as a spec version (one leading `v` dropped), or None when it is not one."""
    text = text.strip()
    if text[:1] in ("v", "V"):
        text = text[1:]
    return text if _VERSION.fullmatch(text) else None


def pinned_version(entry: dict[str, Any]) -> Optional[str]:
    """The version an install pins `entry` at, or None when the registry's is not usable."""
    return _version(entry["version"])


def _pinned_package(spec: str, version: str, kind: str) -> Optional[str]:
    """`spec` naming exactly one version — its own when it is an exact one, else `version` — or None when it is
    not a bare package name (a URL, path, git spec or option). npm's `@scope/name@v` and pip's `name==v` (or
    uv's `name@v`) are both read; npx writes `@`, uvx `==`."""
    spec = spec.strip()
    body, scope = (spec[1:], "@") if spec.startswith("@") else (spec, "")
    if "==" in body:
        name, own = body.split("==", 1)
    elif "@" in body:
        name, own = body.split("@", 1)
    else:
        name, own = body, ""
    name = scope + name
    if not (_NPM_NAME if kind == "npx" else _PYPI_NAME).fullmatch(name):
        return None
    return f"{name}{'@' if kind == 'npx' else '=='}{_version(own) or version}"


def pick_distribution(entry: dict[str, Any]) -> dict[str, Any]:
    """The pinned distribution `entry` installs as on a Linux node (the contract's `pin.distribution`
    shapes), or `Refused(NO_CHECKSUM)` / `Refused(NO_LINUX)`."""
    dist = entry.get("distribution") or {}
    version = pinned_version(entry)
    unhashed = False
    binary = dist.get("binary")
    if isinstance(binary, dict):
        targets = {}
        for target, spec in binary.items():
            if not (isinstance(target, str) and target.startswith("linux-") and isinstance(spec, dict)):
                continue
            if not (isinstance(spec.get("archive"), str) and isinstance(spec.get("cmd"), str)):
                continue
            if not (isinstance(spec.get("sha256"), str) and _SHA256.fullmatch(spec["sha256"])):
                unhashed = True
                continue
            kept: dict[str, Any] = {
                "archive": spec["archive"], "cmd": spec["cmd"], "args": _str_list(spec.get("args")),
                "sha256": spec["sha256"],
            }
            if isinstance(spec.get("env"), dict):
                kept["env"] = _safe_env(spec["env"])
            targets[target] = kept
        if targets and version:
            return {"kind": "binary", "targets": targets}
    offered = False
    for kind in ("npx", "uvx"):
        spec = dist.get(kind)
        if isinstance(spec, dict) and isinstance(spec.get("package"), str) and spec["package"].strip():
            offered = True
            package = _pinned_package(spec["package"], version, kind) if version else None
            if package:
                return {
                    "kind": kind, "package": package,
                    "args": _str_list(spec.get("args")), "env": _safe_env(spec.get("env")),
                }
    raise Refused(NO_CHECKSUM if unhashed and not offered and version else NO_LINUX)


def _distribution_env(distribution: dict[str, Any]) -> dict[str, str]:
    if distribution.get("kind") == "binary":
        merged: dict[str, str] = {}
        for target in (distribution.get("targets") or {}).values():
            merged.update(target.get("env") or {})
        return merged
    return dict(distribution.get("env") or {})


def _auth_methods(entry: dict[str, Any]) -> list[dict[str, Any]]:
    """The entry's `authMethods` that count: dicts with a non-empty string `id`."""
    methods = entry.get("authMethods")
    if not isinstance(methods, list):
        return []
    return [m for m in methods if isinstance(m, dict) and isinstance(m.get("id"), str) and m["id"].strip()]


def key_route(entry: dict[str, Any], distribution: dict[str, Any]) -> str:
    """How the agent takes its key: `KEY_DELIVERY` (ACP `authenticate`) or `ENV_ROUTE` (env-only, refused).

    `authMethods` decides when it holds a method (a dict with a non-empty string `id`): a method of type
    `env_var` is an environment route, any other (type absent, `agent`, `terminal`) is `authenticate`; env-only
    means every method is `env_var`. Without one, a key-shaped variable in the chosen distribution's `env`
    means env-only."""
    methods = _auth_methods(entry)
    if methods:
        env_only = all(m.get("type") == "env_var" for m in methods)
        return ENV_ROUTE if env_only else KEY_DELIVERY
    if any(_KEY_VAR.search(name) for name in _distribution_env(distribution)):
        return ENV_ROUTE
    return KEY_DELIVERY


def installable(entry: dict[str, Any]) -> tuple[dict[str, Any], str]:
    """`(distribution, key_route)` for an entry that can be installed, else `Refused` with the sentence."""
    distribution = pick_distribution(entry)
    route = key_route(entry, distribution)
    if route == ENV_ROUTE:
        raise Refused(ENV_ONLY)
    return distribution, route


def install_refusal(entry: dict[str, Any]) -> Optional[str]:
    """Why `entry` cannot be installed, or None when it can (`installable`'s verdict): the predicate the install route
    and the provider grid's registry rows share (`routes/provider_grid.py`)."""
    try:
        installable(entry)
    except Refused as exc:
        return exc.sentence
    return None


# ── The row ─────────────────────────────────────────────────────────────────────────────────────────


def entrypoint(distribution: dict[str, Any]) -> str:
    """The command line a person reads for `distribution`."""
    if distribution["kind"] == "binary":
        targets = distribution["targets"]
        target = targets.get("linux-x86_64") or next(iter(targets.values()))
        return shlex.join([target["cmd"], *target["args"]])
    runner = ["npx", "-y"] if distribution["kind"] == "npx" else ["uvx"]
    return shlex.join([*runner, distribution["package"], *distribution["args"]])


def row_fields(
    entry: dict[str, Any], distribution: dict[str, Any], *, installer_id: UUID, now: datetime
) -> dict[str, Any]:
    """The `agents` row columns for installing `entry` pinned at `distribution` (an `installable` result)."""
    agent = kept_agent(entry)
    version = pinned_version(entry)
    authors = agent.get("authors", [])
    repo_url = next(
        (agent[f] for f in ("repository", "website") if agent.get(f, "").startswith("https://")), REGISTRY_URL,
    )
    name = agent["name"]
    return {
        "slug": entry["id"], "name": name, "display_name": name,
        "description": agent.get("description", ""),
        "agent_type": "coding", "language": distribution["kind"], "entrypoint": entrypoint(distribution),
        "version": version, "maintainer": ", ".join(authors), "personas": ["coder"],
        "repo_url": repo_url, "subpath": "", "ref": version,
        "resolved_commit": None, "bundle_key": None, "bundle_sha256": None, "bundle_bytes": None,
        "resolved_at": now, "resolve_error": None, "env": {}, "repo_access_user_id": installer_id,
        "status": "active",
        "manifest": {
            "kind": MANIFEST_KIND, "registry_url": REGISTRY_URL, "agent": agent,
            "pin": {"version": version, "distribution": distribution},
            "key_delivery": KEY_DELIVERY, "profile": PROFILE,
        },
    }


def kept_agent(entry: dict[str, Any]) -> dict[str, Any]:
    """`manifest.agent`: the entry's `_AGENT_FIELDS` that are strings (`authors` a capped list of them,
    `authMethods` the counted methods), the name and description capped."""
    kept: dict[str, Any] = {
        field: entry[field].strip()[:_FIELD_MAX] for field in _AGENT_FIELDS if isinstance(entry.get(field), str)
    }
    kept["name"] = kept["name"][:agents.DISPLAY_NAME_MAX]
    if "description" in kept:
        kept["description"] = kept["description"][:_DESCRIPTION_MAX]
    authors = entry.get("authors")
    if isinstance(authors, list):
        kept["authors"] = [a[:_AUTHOR_MAX] for a in authors if isinstance(a, str)][:_AUTHORS_MAX]
    methods = [
        {key: method[key][:_FIELD_MAX] for key in _METHOD_FIELDS if isinstance(method.get(key), str)}
        for method in _auth_methods(entry)[:_METHODS_MAX]
    ]
    if methods:
        kept["authMethods"] = methods
    return kept


def is_registry(agent: Any) -> bool:
    """Whether an `agents` row was installed from the registry."""
    return agents.is_registry_manifest(getattr(agent, "manifest", None))


#: What a registry agent is told about settling, appended to its first prompt: under the `acp-core` profile the host
#: advertises no `jaah.ai` extension, so the control MCP server's tools are its only way to end the task.
SETTLE_NOTE = ("When the work is done, call the `task_complete` tool of the `control` MCP server; if it cannot be "
               "done, call `task_fail` with the reason.")


# ── The key binding ─────────────────────────────────────────────────────────────────────────────────

#: `manifest` key holding the bound API-key account's id (text), absent when none is bound.
KEY_ACCOUNT = "key_account_id"


def key_account_id(agent: Any) -> Optional[str]:
    """The id of the account bound to a registry row, or None."""
    manifest = getattr(agent, "manifest", None)
    value = manifest.get(KEY_ACCOUNT) if isinstance(manifest, dict) else None
    return value if isinstance(value, str) and value else None


def bound_account(session: Session, agent: Any) -> Optional[Account]:
    """The account bound to `agent`, read in the agent's own org (a stale or foreign id finds nothing)."""
    account_id = optional_uuid(key_account_id(agent))
    if account_id is None:
        return None
    return session.scalars(
        select(Account).where(Account.id == account_id, Account.org_id == agent.org_id).limit(1)
    ).first()


def key_account_refusal(account: Optional[Account], agent: Any) -> Optional[str]:
    """Why `account` cannot deliver `agent`'s key, or None when it can: an enabled API-key account holding a key."""
    if account is None:
        return f"{agent.display_name}'s key account no longer exists; bind another on Agents"
    if account.auth_kind != account_auth.AUTH_KIND_API_KEY or account.credential_id is None:
        return f"{account.slug} is not an API-key account; bind an API-key account to {agent.display_name}"
    if not account.enabled:
        return f"{account.slug}, {agent.display_name}'s key account, is disabled"
    return None


def key_only_sentence(agent: Any) -> str:
    """Why a key account cannot be bound to `agent`: only a registry install takes one."""
    return f"{agent.display_name} is not a registry agent; only registry agents take a key account"


def pinned_sentence(agent: Any) -> str:
    """Why a registry row's ref cannot move."""
    return f"{agent.display_name} is pinned at install; install it again to change version"


def env_sentence(agent: Any) -> str:
    """Why a registry row's `env` cannot be edited."""
    return f"{agent.display_name} takes its environment from the registry"
