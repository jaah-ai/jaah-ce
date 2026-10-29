"""The daemon-published config snapshot on the `nodes` row.

A pinned cross-agent contract: ``{"node_id": <id>, "config": {<allowlisted key>: <str>}, "reported_at":
<iso8601>}``; the daemon redacts, apps/api persists. Sole caller: `routes/agent/node_config.py::set_node_config_snapshot`.
Degradation contract: a missing/malformed `reported_at` yields None and the snapshot is still stored
(malformed logs, absent does not); only a delete or a non-dict `config` suppresses the write; a delete
never clears the stored snapshot; the change gate compares `config` only, so `config_reported_at` is no
freshness signal (use `nodes.observed_at`).
"""
from __future__ import annotations

import logging
import re
import time
from datetime import datetime
from typing import Any, Optional
from uuid import UUID

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.models.nodes import Node
from app.models.orgs import OrgConsentHash
from app.node_identity import node_matches, warn_unregistered_node

log = logging.getLogger("node-config")

# ── Each org's managed-settings consent, carried from that org's nodes ───────────────────────────────
# A runner launched before its account approves the managed settings blocks at the prompt; a fresh node
# has no consenting peer (`apps/jaahd/plugin_repair.py`), so the CP carries the uncomputable hash across
# nodes. PER ORG: the hash derives from the Claude account's Anthropic org managed settings, so a tenant
# whose accounts sit in another Anthropic org reports another value, and serving it to a different org's
# nodes wedges their runners. A one-way digest naming no org, self-reported with no trust tier (a wrong
# one costs a prompt, inside the reporting org only); close that before the self-hosted join.
# Kept in `org_consent_hashes`, not the operator-typed `operator_settings`.

#: The key the daemon publishes its consent state under; the reader's copy of
#: `apps/jaahd/main.py::CONSENT_SNAPSHOT_KEY` (a test pins the two equal).
CONSENT_SNAPSHOT_KEY = "remote_settings_consent"

#: What the daemon publishes when it resolved no hash: a value, never an absent key, so "will wedge"
#: differs from "too old to say" (`apps/jaahd/main.py::CONSENT_NONE`).
CONSENT_NONE = "none"

#: The winning consent record's `updatedAt` (epoch ms, decimal string) beside the hash — "newest" is judged
#: by it; the reader's copy of `apps/jaahd/main.py::CONSENT_UPDATED_AT_SNAPSHOT_KEY`. Absent on an older
#: daemon and whenever the hash is `"none"`.
CONSENT_UPDATED_AT_SNAPSHOT_KEY = "remote_settings_consent_updated_at"

# The daemon's own `plugin_repair._CONSENT_HASH_RE`. Validated on the way in: a malformed value seeded
# into every new node would be a silent fleet-wide wedge.
_CONSENT_HASH_RE = re.compile(r"\A[0-9a-f]{64}\Z")


def parse_iso8601(value: Any) -> Optional[datetime]:
    """Parse an ISO-8601 timestamp (trailing `Z` allowed); missing or malformed yields None, never raises,
    so a bad `reported_at` degrades to "unknown when" rather than dropping the snapshot."""
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        log.warning("node-config: dropping malformed config reported_at %r", value)
        return None


def org_consent_hash(session, org_id: Optional[UUID]) -> str:
    """The managed-settings hash an org's nodes are served (pool tick, bootstrap env), or `""`.

    `""` is the ordinary answer for an org that has learned nothing yet: its node then falls back to its
    peers or the prompt. Never another org's value, and never raises — a malformed stored value answers
    `""` too, since this is on the provisioning path.
    """
    if org_id is None:
        return ""
    digest = session.scalar(select(OrgConsentHash.hash).where(OrgConsentHash.org_id == org_id))
    if digest is None:
        return ""
    if not _CONSENT_HASH_RE.fullmatch(digest):
        log.warning(
            "node-config: org %s does not hold a usable managed-settings hash (%r) — its next node gets no "
            "seed and its first runner may meet the approval prompt", org_id, digest,
        )
        return ""
    return digest


#: How far past the CP's own clock a node-clock stamp may run before it is read as no stamp: beyond it, one
#: fast clock or bad daemon would pin its org's hash against every later rotation.
_STAMP_SKEW_MS = 24 * 3600 * 1000


def _plausible(stamp: Optional[int]) -> Optional[int]:
    """`stamp`, or None when it lies further in the future than `_STAMP_SKEW_MS`."""
    if stamp is None or stamp > int(time.time() * 1000) + _STAMP_SKEW_MS:
        return None
    return stamp


def _reported_stamp(node_id: str, config: dict) -> Optional[int]:
    """The consent record's `updatedAt` the node published, or None (an older daemon, or unusable)."""
    raw = config.get(CONSENT_UPDATED_AT_SNAPSHOT_KEY)
    if raw is None:
        return None
    if isinstance(raw, str) and raw.isascii() and raw.isdigit() and len(raw) <= 18:
        stamp = _plausible(int(raw))
        if stamp is not None:
            return stamp
    log.warning(
        "node-config: node %s published an unusable or future %s (%s) — its hash is taken as unstamped",
        node_id, CONSENT_UPDATED_AT_SNAPSHOT_KEY, type(raw).__name__,
    )
    return None


def _is_newer(reported: Optional[int], stored: Optional[int]) -> bool:
    """NEWEST WINS, by the consent record's own `updatedAt`, not by who reported last: after a settings
    change, nodes whose peers still hold the old hash keep republishing it with that old stamp. A stamp
    beats no stamp; no stamp never beats a stamp (an older daemon cannot say how old its hash is); between
    two unstamped values the later report wins, as before stamps existed. A stored stamp now implausibly far
    ahead counts as none, so it cannot hold the org against every later rotation."""
    stored = _plausible(stored)
    if reported is None:
        return stored is None
    return stored is None or reported > stored


def record_org_consent_hash(session, node_id: str, org_id: Optional[UUID], config: Any) -> None:
    """Learn the reporting node's org's managed-settings hash from a node that has one. Never raises.

    A `"none"` report teaches and clears nothing, so one fresh node cannot erase the seed. The row is
    written only when the hash changes or its stamp advances, so `updated_at` is the last real learn.
    """
    if not isinstance(config, dict) or org_id is None:
        return
    reported = config.get(CONSENT_SNAPSHOT_KEY)
    if not isinstance(reported, str) or not _CONSENT_HASH_RE.fullmatch(reported):
        # `CONSENT_NONE` and an absent key are silent; anything else is logged, described by type and
        # length and never reproduced: the field is node-controlled and unbounded, so a `%r` would be a
        # log-filling primitive.
        if reported not in (None, CONSENT_NONE):
            log.warning(
                "node-config: node %s published an unusable %s (%s, %s chars) — ignored",
                node_id, CONSENT_SNAPSHOT_KEY, type(reported).__name__,
                len(reported) if isinstance(reported, str) else "n/a",
            )
        return
    stamp = _reported_stamp(node_id, config)
    # Atomic first learn: on a fresh org its nodes race to create the row, and the loser would 500 its
    # whole config-snapshot write on a duplicate key. Core insert, so `org_id` is stamped by hand.
    created = session.execute(
        pg_insert(OrgConsentHash)
        .values(org_id=org_id, hash=reported, consented_at=stamp, node=node_id)
        .on_conflict_do_nothing(index_elements=[OrgConsentHash.org_id])
        .returning(OrgConsentHash.id)
    ).first()
    if created is not None:
        log.info("node-config: org %s managed-settings consent hash learned from node %s", org_id, node_id)
        return

    def _due(row: OrgConsentHash) -> bool:
        if row.hash == reported:
            return stamp is not None and (_plausible(row.consented_at) is None or stamp > row.consented_at)
        return _is_newer(stamp, row.consented_at)

    # Unlocked first: the steady-state republish writes nothing and must not serialise its org's snapshots.
    current = session.scalars(select(OrgConsentHash).where(OrgConsentHash.org_id == org_id)).one_or_none()
    if current is None or not _due(current):
        if current is not None and current.hash != reported:
            log.info(
                "node-config: node %s reports an older managed-settings consent hash %s… for org %s than the "
                "stored %s… — kept the stored one", node_id, reported[:8], org_id, current.hash[:8],
            )
        return
    # Locked, so two concurrent reports compare against each other rather than both against the old row.
    row = session.scalars(
        select(OrgConsentHash).where(OrgConsentHash.org_id == org_id)
        .with_for_update().execution_options(populate_existing=True)
    ).one_or_none()
    if row is None or not _due(row):
        return
    if row.hash == reported:
        # Same hash, newer evidence: keep the stamp current so an older hash cannot outrank it later.
        row.consented_at = stamp
        return
    # A change, not a first learn: a settings rotation, or a node disagreeing with its org. The row keeps
    # no history, so this line is the only record.
    log.warning(
        "node-config: org %s managed-settings consent hash CHANGED %s… -> %s… (reported by node %s) — "
        "its nodes are served the new value from now on", org_id, row.hash[:8], reported[:8], node_id,
    )
    row.hash, row.consented_at, row.node = reported, stamp, node_id


#: Where a MicroVM node's report lives in `nodes.config_snapshot` (`GET /api/nodes/{id}` serves it).
MICROVM_SNAPSHOT_KEY = "microvm"


def apply_config_snapshot(
    session, node_id: str, config: Any, reported_at_raw: Any, *, is_delete: bool = False, microvm: Any = None
) -> bool:
    """Persist one node's config snapshot. Returns True on a real snapshot change (the caller emits the
    console event on that, and only that). An unregistered node's snapshot is refused, not created.

    `microvm` is a MicroVM node's own report, sent beside the config (architecture §4.3): stored opaque under
    `MICROVM_SNAPSHOT_KEY`, never validated or refused here. None = the node sent none.
    """
    if is_delete:
        return False
    if not isinstance(config, dict):
        return False
    if microvm is not None:
        config = {**config, MICROVM_SNAPSHOT_KEY: microvm}
    reported_at = parse_iso8601(reported_at_raw)

    # `node_id` is the name the daemon posted; select the uuid too so the UPDATE keys on the primary key.
    prev = session.execute(
        select(Node.id, Node.config_snapshot, Node.org_id).where(node_matches(node_id))
    ).first()
    # No such node: refused.
    if prev is None:
        warn_unregistered_node(node_id, "the node config snapshot")
        return False
    node_uuid, stored_config, node_org = prev
    # Before the change gate: the daemon publishes only on change, so a CP that lost the row must still
    # learn the hash from an identical republish.
    record_org_consent_hash(session, node_id, node_org, config)
    if stored_config == config:
        return False

    session.execute(update(Node).where(Node.id == node_uuid).values(
        config_snapshot=config, config_reported_at=reported_at))
    return True
