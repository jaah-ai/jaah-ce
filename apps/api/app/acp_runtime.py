"""The per-provider runtime switch (R14, architecture C7 / D-SW-1): which runtime a dispatch runs on — the ACP host
(`jaah-acp-host`) or the legacy CLI path — resolved here at dispatch and sent to the node as the `runtime` fact.

Storage: the key `acp_runtime` in the operator-settings `system_state` row (`app/operator_settings/catalog.py::STATE_KEY`),
holding `{"<provider>:<auth_kind>": true|false}` for the pairs the operator changed. It is not a `CATALOG` knob (a
knob is one scalar); `GET/PUT /api/settings/acp-runtime` (`routes/settings.py`) read and write it. Operator scope:
the switch is the operator's cutover control, one value for the whole fleet.

Defaults live here, never in a deploy variable: every switch is ON (`acp`), so an unset pair runs ACP, except a pair
in `DEFAULT_OFF` (the catalogue's `acp_switch_off`: an ACP launch not yet seen working live), which runs legacy until
the operator turns it on. A pair in `LEGACY_ONLY` has no switch and always resolves `legacy`. The node may still downgrade an `acp` fact to legacy when
its adapter has no ACP launch (`apps/jaahd/acp_runtime.py`); the downgrade the catalogue names, a signed-in run on a
MicroVM node of an engine whose ACP sign-in is seeded on the host (`MICROVM_SIGNED_IN_LEGACY_GAP`), is resolved here
(`microvm_runtime`) so the session row says what ran. A downgrade the node makes on its own (an older node with no launch for the pair) is not seen
here: the session's `runtime` is written from the node's own event stream, so it says `legacy`, while its display-only
`dispatched_runtime` (`fleet_verdicts.dispatch_runtime_for_task`) still says `acp`. That is why a `DEFAULT_OFF` switch
is turned on only once every node runs the release that built the launch.

The runtime is fixed per attempt. OAuth-first spillover picks another account for a NEW attempt, and that attempt is
resolved afresh on its own pair, so a default-provider task moving from an OAuth account (legacy) to an API-key account
runs the new attempt on ACP; no session is carried across runtimes.
"""
from __future__ import annotations

from typing import Any, Optional

from sqlalchemy.orm import Session

from app import account_auth, agent_providers, node_platform, provider_auth_policy
from app.operator_settings import (
    catalog as settings_catalog,
    errors as settings_errors,
    reads as settings_reads,
)

RUNTIME_ACP = "acp"
RUNTIME_LEGACY = "legacy"

#: The key inside the operator-settings row.
SETTING_KEY = "acp_runtime"

Pair = tuple[str, str]

#: Pairs that never run ACP: the catalogue's per-auth-kind `runtime` saying `legacy_only` — the default provider on a
#: subscription login (architecture A5), and an engine whose pinned CLI serves no ACP (its `no-acp` gap).
LEGACY_ONLY: frozenset[Pair] = frozenset(
    (provider.value, kind) for provider in agent_providers.PROVIDERS
    for kind, runtime in provider.capabilities.runtime if runtime == "legacy_only")
#: Pairs that only run ACP: the catalogue's `runtime` saying `acp_only` (an engine with no terminal runtime, its
#: `acp-runtime-only` gap). No switch, and always `acp`, so turning one off can never strand its runs.
ACP_ONLY: frozenset[Pair] = frozenset(
    (provider.value, kind) for provider in agent_providers.PROVIDERS
    for kind, runtime in provider.capabilities.runtime if runtime == "acp_only")
#: Switchable pairs whose switch ships OFF: the catalogue's `acp_switch_off` (ACP available, not yet the default).
DEFAULT_OFF: frozenset[Pair] = frozenset(
    (provider.value, kind) for provider in agent_providers.PROVIDERS for kind in provider.capabilities.acp_switch_off)
#: The catalogue gap that says why an engine has only ACP; its consequence is that pair's reason.
ACP_ONLY_GAP = "acp-runtime-only"
#: The catalogue gap that says why an engine has no ACP at all; its consequence is that pair's reason.
NO_ACP_GAP = "no-acp"
#: The catalogue gap that says an engine's signed-in run on a MicroVM node runs legacy (its ACP sign-in is seeded on
#: the host, which the guest's copy of the account cannot take).
MICROVM_SIGNED_IN_LEGACY_GAP = "microvm-signed-in-legacy-runtime"


def legacy_only_reason(provider: str) -> str:
    """Why a legacy-only pair has no switch: the provider's `no-acp` gap, else its `Capabilities.legacy_only_reason`
    (the A5 sentence), "" for a value this build does not know."""
    record = agent_providers.by_value(provider)
    if record is None:
        return ""
    gap = next((d for d in record.degradations if d.key == NO_ACP_GAP), None)
    return gap.consequence if gap else record.capabilities.legacy_only_reason


#: Launch lanes that are no account pair but carry a switch of their own, listed beside the pairs (Settings ›
#: Providers). A Jaah-account (Bedrock) run is metered live from the transcript its ACP adapter names
#: (`native_session_ref`, `apps/jaahd/bedrock_usage_tail.py`), so it runs ACP unless the operator turns it off.
LANE_JAAH_ACCOUNT = "jaah_account"
EXTRA_LANES: tuple[Pair, ...] = ((agent_providers.JAAH_ACCOUNT_PROVIDER.value, LANE_JAAH_ACCOUNT),)

def acp_only_reason(provider: str) -> str:
    """Why `provider`'s ACP-only pairs have no switch: its `acp-runtime-only` gap, else ""."""
    record = agent_providers.by_value(provider)
    gap = next((d for d in (record.degradations if record else ()) if d.key == ACP_ONLY_GAP), None)
    return gap.consequence if gap else ""


def pair_key(provider: str, auth_kind: str) -> str:
    return f"{provider}:{auth_kind}"


def switchable_pairs() -> tuple[Pair, ...]:
    """Every pair this build serves that has a switch, in catalogue order, then each `EXTRA_LANES` lane."""
    return tuple(pair for pair in provider_auth_policy.supported_pairs()
                 if pair not in LEGACY_ONLY and pair not in ACP_ONLY) + EXTRA_LANES


def _stored(raw: Any) -> dict[str, bool]:
    """The operator's stored values, malformed entries dropped (a non-dict reads as none)."""
    if not isinstance(raw, dict):
        return {}
    return {key: value for key, value in raw.items() if isinstance(key, str) and isinstance(value, bool)}


def _effective(stored: dict[str, bool]) -> dict[str, bool]:
    """`{"<provider>:<auth_kind>": on}` for every switchable pair: the stored value, else ON (OFF for `DEFAULT_OFF`)."""
    return {pair_key(*pair): stored.get(pair_key(*pair), pair not in DEFAULT_OFF) for pair in switchable_pairs()}


def switches(session: Optional[Session] = None) -> dict[str, bool]:
    """The operator's switches, every switchable pair spelled."""
    return _effective(_stored(settings_reads.overrides(session).get(SETTING_KEY)))


def resolve(provider: str, auth_kind: str) -> str:
    """The runtime one dispatch on this pair runs: `legacy` for a `LEGACY_ONLY` pair or a switch OFF (turned off, or
    a `DEFAULT_OFF` pair never turned on), `acp` for an `ACP_ONLY` pair whatever was stored, else `acp`. An unknown pair resolves `acp` too: its node has no ACP
    launch for it and runs legacy."""
    if (provider, auth_kind) in LEGACY_ONLY:
        return RUNTIME_LEGACY
    if (provider, auth_kind) in ACP_ONLY:
        return RUNTIME_ACP
    on = _effective(_stored(settings_reads.overrides().get(SETTING_KEY))).get(pair_key(provider, auth_kind), True)
    return RUNTIME_ACP if on else RUNTIME_LEGACY


def microvm_runtime(provider: str, auth_kind: str) -> str:
    """`resolve` for a MicroVM dispatch: `legacy` for a signed-in run whose provider carries the
    `MICROVM_SIGNED_IN_LEGACY_GAP` gap (the guest runs it legacy whatever the fact says), else `resolve`."""
    record = agent_providers.by_value(provider)
    gaps = {d.key for d in (record.degradations if record else ())}
    if auth_kind == account_auth.AUTH_KIND_OAUTH and MICROVM_SIGNED_IN_LEGACY_GAP in gaps:
        return RUNTIME_LEGACY
    return resolve(provider, auth_kind)


def dispatch_runtime(provider: str, *, api_key_account: str, node_os: str, bedrock: bool = False) -> str:
    """`resolve` for one dispatch, from what the dispatch carries: an API-key slug means `api_key`, none `oauth`.

    A Jaah-account (Bedrock) dispatch resolves its own lane (`LANE_JAAH_ACCOUNT`, one of `EXTRA_LANES`): it is no
    account pair, and its switch is neither auth kind's. A node whose OS has no ACP runtime (`node_os`,
    `node_platform.CAP_ACP_RUNTIME`) runs `legacy` whatever the switch says: it has no engine adapter to spawn.
    Required, so a caller cannot silently resolve every node as Ubuntu. Every OS has the runtime today;
    a node that has not attested a provider's adapter yet is not given that run (`routes/fleet.py::_acp_engine_refusal`),
    and a node off Linux downgrades one to legacy itself (`apps/jaahd/acp_runtime.py`)."""
    if not node_platform.supports(node_os, node_platform.CAP_ACP_RUNTIME):
        return RUNTIME_LEGACY
    if bedrock:
        return resolve(provider, LANE_JAAH_ACCOUNT)
    auth_kind = account_auth.AUTH_KIND_API_KEY if api_key_account else account_auth.AUTH_KIND_OAUTH
    return resolve(provider, auth_kind)


def set_switches(session: Session, values: dict[str, bool]) -> dict[str, bool]:
    """Store `values` over the operator's current ones and return the resulting `switches`. Raises
    `OperatorSettingsError` (nothing written) on a key that is no switchable pair or a non-boolean value. Commits;
    the caller invalidates the settings cache."""
    from app.models.control_plane import SystemState

    known = {pair_key(*pair) for pair in switchable_pairs()}
    for key, value in values.items():
        if key not in known:
            raise settings_errors.OperatorSettingsError(f"'{key}' is not a provider and auth type with a runtime "
                                                          "switch")
        if not isinstance(value, bool):
            raise settings_errors.OperatorSettingsError(f"'{key}' must be true or false")
    # Locked: a concurrent Settings save rewrites the same row whole, and must not undo this write (or this it).
    row = session.get(SystemState, settings_catalog.STATE_KEY, with_for_update=True)
    current: dict[str, Any] = dict(row.value) if row is not None and isinstance(row.value, dict) else {}
    current[SETTING_KEY] = {**_stored(current.get(SETTING_KEY)), **values}
    if row is None:
        row = SystemState(key=settings_catalog.STATE_KEY, value={})
        session.add(row)
    row.value = current  # whole-object replace, as `operator_settings.writes.apply_overrides` does
    session.commit()
    return _effective(current[SETTING_KEY])
