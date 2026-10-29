"""An agent is a process that governs itself through the daemon socket.

Any engine — a vendor coding CLI, an open-source terminal agent, a company's own bot, something that writes
no code at all — joins Jaah's governance pipeline the same way: its repo carries a `jaah-agent.toml`, the
control plane snapshots that repo at a pinned commit, and a node runs the manifest's `command` (SDK v2,
`is_acp_manifest`) as the ACP engine of `jaah-acp-host` in a leased slot. The process then reports, asks, notifies, reads
credentials and settles its task over the node daemon's socket, exactly the verbs a built-in runner uses,
so logging, notifications, approvals and metrics are Jaah's whatever engine does the work. It holds no token: the daemon identifies
it by process ancestry.

This module is the control plane's door onto the manifest contract and the home of the vocabulary every
other part reads:

* `validate_manifest` — the `jaah-agent.toml` rules, each failure a human sentence naming its field. The
  rules are single-homed in `tools/manifest_check.py`; this module loads it by path (`app/shared_module.py`)
  and re-exports it.
* `SDK_PERSONAS` — the personas an agent may claim; script and system personas never resolve to one.
* `V1_RETIRED` — why an SDK v1 (manifest 1) agent is refused at registration and cannot run.
* `AGENT_STATUSES` and the table names `app/models/` maps.

Built-in providers are not agents here (DD11): the picker derives them from `agent_providers.PROVIDERS` at
read time (`routes/agents.py::agent_choices`), never from rows. The registration flow (forge read, snapshot,
store) is `app/agent_bundles.py`; the console API is `app/routes/agents.py`.
"""
from __future__ import annotations

from typing import Optional

from app import shared_module

AGENTS_TABLE = "agents"
AGENT_ENABLEMENTS_TABLE = "agent_enablements"

#: `agents.status` (`ck_agents_status`). `removed` is the catalog's withdrawal (D13): the row stays, so an org
#: that enabled it sees it marked rather than vanished.
STATUS_ACTIVE, STATUS_DISABLED, STATUS_REMOVED = "active", "disabled", "removed"
AGENT_STATUSES: tuple[str, ...] = (STATUS_ACTIVE, STATUS_DISABLED, STATUS_REMOVED)

_RULES = shared_module.load("_shared_manifest_check", shared_module.candidates("manifest_check.py", "tools"))

SDK_PERSONAS: tuple[str, ...] = _RULES.SDK_PERSONAS
MANIFEST_FILENAME: str = _RULES.MANIFEST_FILENAME
V1_RETIRED: str = _RULES.V1_RETIRED
DISPLAY_NAME_MAX: int = _RULES.DISPLAY_NAME_MAX
Manifest = _RULES.Manifest
ManifestError = _RULES.ManifestError
validate_manifest = _RULES.validate_manifest
is_commit_sha = _RULES.is_commit_sha
short_sha = _RULES.short_sha
is_acp_manifest = _RULES.is_acp_manifest
secret_env_refusals = _RULES.secret_env_refusals
REGISTRY_KIND: str = _RULES.REGISTRY_KIND
is_registry_manifest = _RULES.is_registry_manifest


def unsupported(manifest: object) -> Optional[str]:
    """`V1_RETIRED` for a stored SDK v1 (manifest 1) row, which can no longer run; None for an SDK v2 agent or an
    ACP registry install. The row is kept (S4): it is shown unsupported and refused at dispatch."""
    if is_acp_manifest(manifest) or is_registry_manifest(manifest):
        return None
    return V1_RETIRED
