"""A slot's `stack_state` and `urls`, derived at read time from its own sensed columns.

Neither has a stored column; read-time derivation follows the `holder`/`idle_hours` precedent. Client-facing seams only. Plain values in, no ORM or I/O.

`observation_is_fresh` is public because the same staleness rule also guards a write
(`routes/pool_slots.py::release_slot`).
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timezone
from typing import Optional

from app import console_direct, fleet_decide, pool_state
from app.clock import as_utc
from app.entity_shape import optional_uuid

log = logging.getLogger("slot-stack")

# The node's own tailnet DNS name in its config snapshot — published by
# `apps/jaahd/main.py::TAILNET_DNS_NAME_SNAPSHOT_KEY` from `tailscale status`.
TAILNET_DNS_NAME_SNAPSHOT_KEY = "tailnet_dns_name"

# A bare DNS name: dot-separated LDH labels, nothing a URL could smuggle (scheme, port, path, userinfo).
_DNS_NAME = re.compile(r"(?=.{1,253}$)[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?)+", re.I)

# Out-of-vocabulary sentinel: `resolve_stack` returns `prev_stack` verbatim for unknown, so this
# separates unknown from "down". Non-empty on purpose — a falsy sentinel comes back as "down".
_UNKNOWN = "\x00unknown"


def stack_state_for(
    *, stack_present: object, stack_up: object, probes: object, observed_at: object
) -> Optional[str]:
    """This slot's `stack_state` — `"none"` / `"up"` / `"down"`, or **None for UNKNOWN**.

    Fails closed to None: with no prior verdict to carry forward, a blind probe is unknown, never
    `"down"` (which would report a running stack as stopped).

      * stale or absent `observed_at`           -> `None`
      * `stack_present is False`                -> `"none"` (no compose stack — nothing to be up)
      * `stack_present is None`                 -> `None`   (a missing probe is not a missing stack)
      * `docker_ok` not affirmatively observed  -> `None`   (`fleet_decide.probe_observed`)
      * otherwise                               -> `"up"` if `stack_up is True` else `"down"`

    Freshness (`observation_is_fresh`) matters because sensed columns are never cleared: a dead node's
    last `stack_up=True` would otherwise read `"up"` forever."""
    if not observation_is_fresh(observed_at):
        return None
    state = fleet_decide.resolve_stack(
        {"stack_present": stack_present, "stack_up": stack_up, "probes": probes}, _UNKNOWN
    )
    return None if state == _UNKNOWN else state


def urls_for(
    ports: object, node_id: object, tailnet_dns_name: object = None
) -> Optional[dict[str, str]]:
    """This slot's `urls` — `fleet_decide.urls_from_ports`' role mapping over the SENSED `ports`,
    rehosted onto the node that owns the slot, with an empty map normalised to None.

    `tailnet_dns_name` is the node's own report (`TAILNET_DNS_NAME_SNAPSHOT_KEY` in its config snapshot)
    and wins when it is a DNS name naming this node (`_reported_host`); otherwise the host is derived from
    `node_id` (`_url_host`).

    `node_id` is the node's name, not the `worktrees.node_id` uuid: a uuid yields a URL that resolves
    nowhere. Resolve it first (`node_identity.name_expr` / `names_by_id`).

    `None` rather than `{}` separates "no ports observed". Not freshness-gated: `urls` is addressing
    (the deterministic port grid), and the console only links behind `stack_state === 'up'`. Rehosted from
    `localhost` because the console is remote; host rule: `console_direct.tailnet_suffix()`."""
    roles = fleet_decide.urls_from_ports(ports)
    if not roles:
        return None
    host = _reported_host(tailnet_dns_name, node_id) or _url_host(node_id)
    # Port taken back off the built URL so the grid mapping stays single-homed there.
    return {role: f"http://{host}:{url.rsplit(':', 1)[-1]}" for role, url in roles.items()}


def _reported_host(tailnet_dns_name: object, node_id: object) -> Optional[str]:
    """The node's self-reported tailnet name as a link host, or None to fall back to `_url_host`.

    It is node-supplied, so it must be a bare DNS name AND name this node: its first label is the node's
    own (first) label, which the CP holds. A node cannot point its slot links at another host."""
    if tailnet_dns_name is None:
        return None
    reported = str(tailnet_dns_name).strip().rstrip(".").lower()
    own_label = str(node_id or "").strip().lower().split(".", 1)[0]
    if _DNS_NAME.fullmatch(reported) and own_label and reported.split(".", 1)[0] == own_label:
        return reported
    # The value is node-controlled, so only its shape is logged.
    log.warning(
        "slot-stack: node %r reports a tailnet name that is not a DNS name under its own (%s, %d chars) — "
        "hosting its slot URLs on the derived name instead", own_label, type(tailnet_dns_name).__name__,
        len(str(tailnet_dns_name)),
    )
    return None


def _url_host(node_id: object) -> str:
    """The host a REMOTE console can reach this slot's node on — rule sourced from `urls_for`.

    A name with a `.` is an FQDN, used verbatim; a bare one gains the tailnet suffix; no suffix or a
    blank id gives `localhost`. A uuid is hosted verbatim but logged, since the failure is otherwise a
    silent dead link.
    """
    name = str(node_id or "").strip()
    if not name:
        return "localhost"
    if _looks_like_uuid(name):
        log.warning(
            "slot-stack: building a slot URL host from %r, which is a node UUID rather than a node NAME "
            "— the resulting link will not resolve. The caller must resolve `worktrees.node_id` to "
            "`nodes.name` (migration 0106) before calling urls_for().", name,
        )
    if "." in name:
        return name
    suffix = console_direct.tailnet_suffix()
    return f"{name}.{suffix}" if suffix else "localhost"


def _looks_like_uuid(name: str) -> bool:
    """Whether `name` is a uuid spelling — i.e. whether a caller handed us the FK instead of the name."""
    return optional_uuid(name) is not None


def observation_is_fresh(observed_at: object) -> bool:
    """Whether this row's observation is recent enough to be reported as a state at all — the freshness
    rule itself, exported so other consumers call rather than re-derive it.

    Bound: `pool_state.max_observation_age_secs()` (`FLEET_MAX_OBSERVATION_AGE_SECS`; deploy sets 60s).
    Separate from `stack_state_for`, which also returns None for a fresh-but-silent probe; a write guard
    needs only this. Fails closed: a non-`datetime` is not fresh. A naive stamp is read as UTC, matching
    `console_direct.resolve_origin`."""
    if not isinstance(observed_at, datetime):
        return False
    stamp = as_utc(observed_at)
    age = (datetime.now(timezone.utc) - stamp).total_seconds()
    return age <= pool_state.max_observation_age_secs()
