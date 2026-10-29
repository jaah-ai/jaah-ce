"""The community edition's boot: its two secrets, and its one org, one node and that node's pool.

Runs only when `edition.IS_CE`; the cloud boot never reaches it (PRD §7). Two entry points:

* `ensure_secrets()` — before any process reads one of its secrets: the readers call it
  (`app/auth.py::resolve_session_secret`, `app/credential_crypto.py::resolve_credential_encryption_key`,
  `app/credential_keys.py::resolve_secret_key_provider`), and the API's boot before them
  (`app/main.py::create_app`). Three secrets: the session secret, the tier-1 credential key, and the RSA keypair
  that wraps every credential secret's data key. The edition signs no compliance manifest (compliance is cloud-only).
  An operator-supplied value wins; absent (or the published dev value), each is
  generated once into `SECRETS_FILE` on the `jaah-secrets` volume (0600) and read from there on every later
  boot, so sessions and sealed credentials survive a restart. Never printed, never logged, never written
  elsewhere. The edition never needs `APP_ENV`: no install wraps or signs under a key published in this repo.
* `run(session)` — after the seed (`app/seed.py::main`), on every boot, idempotent: the single org (org zero,
  which the seed converges; its first boot turns every built-in provider on and arms the dispatch crons), its
  cloud-only system crons deleted, the single node (`os` ubuntu, credential minted at its INSERT), every project of
  the org assigned to that node, a pool ceiling of `_CE_PORT_BLOCK - 1` slots per project
  (`slot_allocation.pool_bound`; worktrees are not the cap, live agents are), per-account runner caps that never
  bind below `CE_MAX_AGENTS`, the
  node's identity file `NODE_ENV_FILE` (0600) on the `jaah-node-identity` volume, and every enqueued or pending run of
  a cloud-only workflow cancelled (an install upgraded from a release that still shipped its module).

The pool is a ceiling, not pre-declared rows: a slot belongs to a (node, project) pair and a static slot needs a
repository (`slot_allocation.static_target`), so on a fresh install with no project the pair's slots are made
per task, up to that ceiling.
"""
from __future__ import annotations

import base64
import fcntl
import hmac
import logging
import os
import secrets
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app import edition, edition_features
from app.tenancy.ids import ORG_ZERO_ID

if TYPE_CHECKING:
    from app.models.nodes import Node

log = logging.getLogger("app.ce_bootstrap")

#: Where the community edition keeps its own files (each subdirectory a compose volume). `JAAH_CE_DATA_DIR` moves the
#: whole root — a location, never a limit — for a process run outside the container (the per-PR boot check).
CE_DATA_DIR = Path(os.environ.get("JAAH_CE_DATA_DIR") or "/var/lib/jaah-ce")
#: The `jaah-secrets` volume's file: `KEY=value` lines, 0600, generated on first boot.
SECRETS_FILE = CE_DATA_DIR / "secrets" / "secrets.env"
#: The `jaah-node-identity` volume's file the container node reads (and `python -m app.ce_node env` prints).
NODE_ENV_FILE = CE_DATA_DIR / "node-identity" / "node.env"
#: The control plane as the container node dials it (compose service `api`); loopback http is accepted.
CONTAINER_CP_BASE_URL = "http://api:8000"
#: The one node's name.
NODE_NAME = "local"
#: Where the container node keeps its pool, accounts and identity: `JAAH_HOME` in the node image's Dockerfile.
#: The node row starts there; the native installer re-points it at its own (`app/ce_node.py`'s `home`).
CONTAINER_JAAH_HOME = "/home/jaah/.jaah"
#: The edition's one org: org zero, where the seed already put the operator seat its one user takes.
CE_ORG_ID = ORG_ZERO_ID

_SESSION_SECRET = "SESSION_SECRET"
_CREDENTIAL_KEY = "CREDENTIAL_ENCRYPTION_KEY"
_WRAPPING_KEY = "CREDENTIAL_LOCAL_PRIVATE_KEY"  # `app/credential_keys.py::LOCAL_KEY_ENV`
#: Settings an earlier release honoured that this edition removed (it has no `aws`: no S3, no KMS). Set, the install
#: refuses to start (`refuse_removed_settings`) rather than silently ignore where the operator thinks its data is.
#: Spelled by `app/config.py::get_settings` (the session and attachment bucket) and `app/credential_keys.py`.
REMOVED_SETTINGS = ("SESSION_TRANSCRIPT_BUCKET", "CREDENTIAL_KMS_KEY_ID", "CREDENTIAL_KMS_PREVIOUS_KEY_IDS")
# Secrets an earlier release generated that nothing reads now, dropped from the file on boot: the compliance manifest
# signing key, from before compliance left the community edition (a private key nothing rotates must not linger).
_RETIRED = ("COMPLIANCE_SIGNING_LOCAL_PRIVATE_KEY",)


def _published(name: str, value: str) -> bool:
    """Is `value` one this repository publishes — for dev stacks or the test fixtures? It counts as absent
    (`app/auth.py`, `app/credential_crypto.py` and `app/credential_keys.py` refuse it too). A key is
    compared by its material, so no other spelling of a published key passes for the install's own."""
    if name == _WRAPPING_KEY:
        from app import credential_keys

        return credential_keys.is_published_key(value)
    if name == _CREDENTIAL_KEY:
        from app import credential_crypto

        return credential_crypto.is_published_key(value)
    return hmac.compare_digest(value.encode(), b"dev-session-secret-change-me")


def _private_key_b64(key) -> str:
    """A private key on one line: unencrypted PKCS#8 DER, base64 (what the key reader accepts)."""
    from cryptography.hazmat.primitives import serialization

    der = key.private_bytes(serialization.Encoding.DER, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
    return base64.b64encode(der).decode("ascii")


def _new_wrapping_key() -> str:
    from cryptography.hazmat.primitives.asymmetric import rsa

    return _private_key_b64(rsa.generate_private_key(public_exponent=65537, key_size=4096))


_GENERATORS = {
    _SESSION_SECRET: lambda: secrets.token_hex(32),
    _CREDENTIAL_KEY: lambda: base64.b64encode(secrets.token_bytes(32)).decode("ascii"),
    _WRAPPING_KEY: _new_wrapping_key,
}


# ── Secrets ─────────────────────────────────────────────────────────────────────────────────────────


# Name → the value already found not to be a published key. Every secret reader calls `ensure_secrets`, many
# per request; the published-key check parses RSA keys, so it runs once per value, not once per call.
_SETTLED: dict[str, str] = {}


def _configured(name: str) -> bool:
    value = os.environ.get(name, "").strip()
    if not value:
        return False
    if _SETTLED.get(name) == value:
        return True
    if _published(name, value):
        return False
    _SETTLED[name] = value
    return True


def _read_env_file(path: Path) -> dict[str, str]:
    if not path.exists():
        return {}
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        key, sep, value = line.partition("=")
        if sep and key.strip() and not key.lstrip().startswith("#"):
            values[key.strip()] = value.strip()
    return values


def _write_private(path: Path, text: str) -> None:
    """`text` at `path`, created 0600 (never world-readable, not even for a moment) and
    swapped in by rename, so a crash mid-write leaves the old file whole."""
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    scratch = path.with_name(f".{path.name}.tmp")
    if scratch.exists():
        scratch.unlink()
    fd = os.open(scratch, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)
    os.chmod(scratch, 0o600)
    os.replace(scratch, path)


def refuse_removed_settings() -> None:
    """Refuse to start while a `REMOVED_SETTINGS` entry is set: the attachment and session bucket, or the KMS
    wrapping key. Names the variable, never its value. The upgrade steps (copy the bucket to the volume, re-enter
    credentials) are in the edition's README, § Upgrade. A no-op where the edition has `aws` (the cloud)."""
    if edition_features.enabled("aws"):
        return
    for name in REMOVED_SETTINGS:
        if os.environ.get(name, "").strip():
            raise RuntimeError(
                f"{name} is set, but the community edition no longer supports it: it stores attachments and "
                "sessions on its own volume and wraps credentials under its own generated keypair. Unset "
                f"{name} and restart (README, Upgrade: copy the bucket into the volume first; credentials sealed "
                "under KMS must be re-entered)."
            )


def ensure_secrets() -> None:
    """Put a real session secret, credential key and wrapping keypair in this process's
    environment (module docstring). Refuses first any setting the edition removed (`refuse_removed_settings`), so
    every process that reads a secret refuses with it."""
    if not edition.IS_CE:
        return
    refuse_removed_settings()
    missing = [name for name in _GENERATORS if not _configured(name)]
    if not missing and not set(_RETIRED) & _read_env_file(SECRETS_FILE).keys():
        return
    SECRETS_FILE.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    # One generator at a time: two processes booting together (the seed, the API) must agree on one value.
    lock_fd = os.open(SECRETS_FILE.with_name(f".{SECRETS_FILE.name}.lock"), os.O_WRONLY | os.O_CREAT, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        if SECRETS_FILE.exists():
            # Re-tightened every boot: a file restored from a backup may have come back readable.
            os.chmod(SECRETS_FILE, 0o600)
        stored = _read_env_file(SECRETS_FILE)
        # A published key in the file itself (a hand edit, an old backup) is replaced like a missing one.
        absent = [name for name in missing if not stored.get(name) or _published(name, stored[name])]
        retired = [name for name in _RETIRED if stored.pop(name, None) is not None]
        if absent or retired:
            stored.update({name: _GENERATORS[name]() for name in absent})
            _write_private(SECRETS_FILE, "".join(f"{key}={value}\n" for key, value in stored.items()))
            # Names only: the values never reach a log.
            log.info("ce: generated %s, dropped %s in the secrets volume", ", ".join(absent) or "none",
                     ", ".join(retired) or "none")
    finally:
        os.close(lock_fd)
    for name in missing:
        # Just generated, or checked against the published keys above.
        os.environ[name] = stored[name]
        _SETTLED[name] = stored[name]


# ── The org, the node and its pool ──────────────────────────────────────────────────────────────────


def node_env_lines(node_id: str, token: str, cp_base_url: str) -> str:
    """The node identity file's three lines (`apps/jaahd` reads them as environment)."""
    return f"NODE_ID={node_id}\nNODE_TOKEN={token}\nJAAH_CP_BASE_URL={cp_base_url}\n"


def read_node_env() -> dict[str, str]:
    return _read_env_file(NODE_ENV_FILE)


def _held_token(node_id: str, token_hash: Optional[str]) -> Optional[str]:
    """The token `NODE_ENV_FILE` holds for this node, when it is still the node's live credential."""
    from app import node_credentials

    held = read_node_env()
    token = held.get("NODE_TOKEN", "")
    if held.get("NODE_ID") != node_id or not token or not token_hash:
        return None
    return token if hmac.compare_digest(node_credentials.digest(token), token_hash) else None


def node_id(session: Session) -> Optional[str]:
    """The edition's one node's uuid, or None before the first boot made it — what an unpinned dispatch runs on
    (`routes/commands.py::default_dispatch_node`): the API's `NODE_ID` names no row here, the node's own does."""
    from app.models.nodes import Node

    found = session.scalar(select(Node.id).where(Node.org_id == CE_ORG_ID).order_by(Node.id).limit(1))
    return str(found) if found is not None else None


def _ensure_node(session: Session, org_id) -> tuple["Node", str]:
    """The one node and a plaintext token for it: the file's when it still resolves, else a fresh mint (a lost
    file, or a first boot). Any other node row in the org is a fault, refused: the edition has exactly one."""
    from app import node_platform
    from app.models.nodes import Node
    from app.node_credentials import issue
    from app.routes.nodes.register import _registered_node

    nodes = list(session.scalars(select(Node).where(Node.org_id == org_id).order_by(Node.id)).all())
    if len(nodes) > 1:
        raise RuntimeError(
            f"the community edition runs exactly one node, and this database holds {len(nodes)} — refusing to boot"
        )
    if not nodes:
        from app.ce_node import NATIVE_CP_BASE_URL

        # `cp_base_url` renders only the onboarding command, which the edition never runs (its node reads
        # `NODE_ENV_FILE`); that renderer admits plain http to a loopback host alone, so it gets the host's.
        node, credential = _registered_node(
            session, None, NODE_NAME, cp_base_url=NATIVE_CP_BASE_URL, org_id=org_id, os=node_platform.OS_UBUNTU,
            description="This machine's node, created by the community edition's first boot.",
            jaah_home=CONTAINER_JAAH_HOME,
        )
        return node, credential.token
    node = nodes[0]
    token = _held_token(str(node.id), node.node_token_hash)
    return node, token if token is not None else issue(session, node)


#: The org's port block: the clone plus ten slots, one port each. The pool ceiling it implies is independent of
#: `CE_MAX_AGENTS` (worktrees are not capped; live agents are), and both the pin and the check below read it, so a
#: boot whose pool already matches rewrites nothing.
_CE_PORT_BLOCK = 11


def _ensure_pool(session: Session, org_id, node: "Node") -> None:
    """Every project assigned to the node, and the pair's slot ceiling exactly `_CE_PORT_BLOCK - 1`."""
    from app import slot_allocation
    from app.operator_settings import cache, writes
    from app.routes.assignments import auto_assign_as

    # Attributed to each project's creator: a pool clone authenticates as its assignment's creator.
    auto_assign_as(session, None, node_id=node.id)
    if slot_allocation.pool_bound(session, org_id) != _CE_PORT_BLOCK - 1:
        # `index_ceiling` is `(block_size - 1) // stride`: one port each, the clone plus the ten slots. Both knobs,
        # so a stride edited in Settings cannot push the block past its own maximum and stop the boot.
        writes.apply_org_overrides(
            session, org_id, {"port_stride": 1, "port_block_size": _CE_PORT_BLOCK}
        )
        cache.invalidate_cache()


def _ensure_runner_caps(session: Session, org_id) -> None:
    """No per-account cap binds below the edition's session cap: accounts carry none of their own, and the
    fleet's default is at least `CE_MAX_AGENTS`. The edition's own session cap is not this module's to enforce."""
    from app.models.credentials import Account
    from app.operator_settings import cache, reads, writes

    session.execute(
        update(Account).where(Account.org_id == org_id, Account.max_runners.is_not(None)).values(max_runners=None)
    )
    if reads.get_int("max_runners_per_account") < edition.CE_MAX_AGENTS:
        writes.apply_overrides(session, {"max_runners_per_account": edition.CE_MAX_AGENTS})
        cache.invalidate_cache()


#: The system crons the baseline ships disarmed (revision 202610251700's arming procedure): without them nothing
#: places a ready task, advances an epic or notices a dead node.
_DISPATCH_CRONS = ("node-liveness", "task-pipeline", "lease-drain")


def _arm_dispatch_crons(session: Session) -> None:
    """Arm the dispatch crons, once, at the first boot: the edition's node is served from its first minute, so
    the cloud's one-at-a-time post-deploy arming has no one to wait for. Later boots leave them to the Cron page."""
    from app.durable.cron_projection import request_projection_in_transaction
    from app.models.automations import Cron

    rows = session.scalars(
        select(Cron).where(Cron.activity_type.in_(_DISPATCH_CRONS), Cron.is_system.is_(True), Cron.enabled.is_(False))
    ).all()
    for row in rows:
        row.enabled = True
    if rows:
        # DBOS schedules are projected by the serving api; ask it in this transaction, as the dev seed does.
        request_projection_in_transaction(session)


def _drop_cloud_crons(session: Session, org_id) -> None:
    """Delete the system crons the migrations seeded for cloud surfaces, on every boot: the edition registers none of
    them (`cron_system_activities.CE_SYSTEM_ACTIVITY_TYPES`), and a later revision may seed another. Each one's
    runs cascade, and the one-child trigger (`trg_crons_drop_childless_automation`) deletes its parent automation."""
    from sqlalchemy import delete

    from app import cron_system_activities
    from app.durable.cron_projection import request_projection_in_transaction
    from app.models.automations import Cron

    cloud_only = set(cron_system_activities.SYSTEM_ACTIVITY_TYPES) - cron_system_activities.CE_SYSTEM_ACTIVITY_TYPES
    dropped = session.execute(
        delete(Cron)
        .where(Cron.org_id == org_id, Cron.is_system.is_(True), Cron.activity_type.in_(cloud_only))
        .returning(Cron.id)
    ).all()
    if dropped:
        # The projection drops their DBOS schedules as orphans.
        request_projection_in_transaction(session)


def _cancel_cloud_workflows() -> None:
    """Cancel the enqueued and pending runs of every workflow whose feature the edition lacks
    (`workflow_registry.FEATURE_OF`, e.g. a compliance seal an earlier release enqueued): its module does not ship,
    so the engine could never run one, and would retry its recovery on every launch."""
    from app import workflow_registry
    from app.durable import runtime

    names = tuple(name for name, feature in workflow_registry.FEATURE_OF.items()
                  if not edition_features.enabled(feature))
    cancelled = runtime.cancel_unfinished(names)
    if cancelled:
        log.info("ce: cancelled %d unfinished run(s) of workflows not in this edition", cancelled)


def _open_provider_grid(session: Session, org) -> None:
    """Every built-in provider and sign-in kind on, once, at the first boot: the migrations leave org zero with the
    seed grid alone (`provider_auth_policy.SEED`), and the edition's one user may pick any provider for their three
    accounts. Later boots leave it to Settings > Providers."""
    from app import provider_auth_policy

    org.providers_override = provider_auth_policy.encoded(provider_auth_policy.builtin_cells())


def run(session: Session) -> dict[str, str]:
    """Converge the community edition's org, node, pool and caps, and write the node's identity file. Flushes,
    never commits (the seed's transaction does). Returns the node id (no secret)."""
    if not edition.IS_CE:
        return {}
    from app.models.nodes import Node
    from app.models.orgs import Org
    from app.tenancy import seam

    with seam.operator_scope():
        org = session.get(Org, CE_ORG_ID)
        if org is None:
            raise RuntimeError("the community edition's org is missing — the seed must run first")
        if session.scalar(select(Node.id).where(Node.org_id == CE_ORG_ID).limit(1)) is None:
            _open_provider_grid(session, org)
            _arm_dispatch_crons(session)
        _drop_cloud_crons(session, CE_ORG_ID)
        _cancel_cloud_workflows()
        node, token = _ensure_node(session, CE_ORG_ID)
        _ensure_pool(session, CE_ORG_ID, node)
        _ensure_runner_caps(session, CE_ORG_ID)
        session.flush()
        node_id = str(node.id)
    wanted = node_env_lines(node_id, token, CONTAINER_CP_BASE_URL)
    current = NODE_ENV_FILE.read_text(encoding="utf-8") if NODE_ENV_FILE.exists() else None
    if current != wanted or (NODE_ENV_FILE.stat().st_mode & 0o777) != 0o600:
        _write_private(NODE_ENV_FILE, wanted)
    log.info("ce: node %s ready", node_id)
    return {"node_id": node_id}

