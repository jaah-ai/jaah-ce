"""Offboarding a tenant: every store that holds an org's data, counted, emptied, and checked empty.

`app/tenancy/org_lifecycle.py` deletes an org's DATABASE rows; that alone leaves the tenant behind in
other places, which is what this module adds. One census (`census`, the dry run) and one executor
(`execute`) over the stores in `STORES`, in the order a later store needs an earlier one's rows:

  installs        `credentials` rows of a Slack or forge type, revoked at the vendor where it has an API:
                  Slack `auth.revoke`; a Connect-minted GitHub grant (refreshed first, then the App's grant
                  deleted) or GitLab token. Their rows go with the purge.
  shared_store    each account's token leaf on a mounted token share (`{jaah-home}/auth/.shared`), removed by
                  every connected node that reports one (`account_shared_delete`, `apps/jaahd/accounts.py`)
                  — deprovision never reaches it.
  account_dirs    each account's credential dir on every node that holds it (`account_delete`).
  worktrees       every slot row on the org's nodes or of its projects, through the console's own slot Delete
                  (`routes/pool_slots.py::delete_one_slot`: ledger, row, then the node erases the tree).
  s3_objects      transcript and artifact objects (keys from each session's manifest — the CP holds no
                  `s3:ListBucket`) and attachment objects (reaped by the org delete itself).
  db_rows         the census `org_lifecycle.org_owned_tables` purges, plus the `orgs` row, through
                  `routes/orgs.py::purge_org` — its wind-down and its `org_deleted` audit row.

The one caller is Compliance Delete (`routes/orgs.py::compliance_delete_org`): org delete is soft, and
this is the only purge, authorized by the operator's TOTP.

Nothing is touched until every precondition holds (`Census.blockers`, `node_blockers`). Every store before
the purge is re-read for residue BEFORE `purge_org` runs, and any residue stops the execution there
(`OffboardStopped`): the rows that name the residue still exist, so running it again finishes the job —
except a worktree tree the node did not erase, whose row the slot Delete has already removed: that one is
recorded by path and leaves the record incomplete, and the purge goes on.
Only attachments and the rows themselves are checked after the purge. Every stop and every finish files
the record (inside the `org_compliance_deleted` audit row) in the operator's org; `complete` is true only when every
residue is zero and nothing was unverifiable. The operator's offboarding tool files it in the evidence store.

ERASURE IN S3 IS A DELETE MARKER: the bucket is versioned, so the deleted bytes become a noncurrent version
that the bucket's lifecycle expires after `NONCURRENT_EXPIRY_DAYS` (set in the
transcript bucket's Terraform). The record names that date.

Caveats the record lists rather than fixes, each beyond our reach: an org's own shared-accounts EFS
(`org_shared_stores`) lives in the TENANT'S AWS account; a vendor grant with no revoke API (Bitbucket, Azure
DevOps) or a pasted personal token is destroyed only on our side; the tenant's own authorization of the
GitHub OAuth App on github.com is theirs to remove.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from uuid import UUID

import httpx
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, aliased, object_session as session_of

from app import account_auth, attachment_storage, data_residency, session_transcripts
from app.data_access_events import opened_config_for
from app.models.agent_sessions import AgentSession
from app.models.credentials import Account, AccountObservation, Credential
from app.models.identity import AdminUser
from app.models.nodes import Node
from app.models.orgs import Org
from app.models.projects import Project
from app.models.worktrees import Worktree
from app.node_clone import resolve_auth_dir
from app.tenancy.ids import OPERATOR_PLANE_ORG_IDS
from app.tenancy.org_lifecycle import (
    org_owned_attachment_keys,
    org_owned_row_counts,
)

log = logging.getLogger("cockpit-tenant-offboard")

#: The stores, in the record's order. `tests/test_tenant_offboard.py` holds the record to this set.
STORES = (
    "db_rows", "s3_objects", "worktrees", "shared_store", "account_dirs", "installs",
)

#: `credentials.type` values that are a Slack or forge install. The `*_app` rows are the operator's own
#: Connect apps (`credentials/registry.py`), counted if a tenant ever held one.
INSTALL_TYPES = frozenset({
    "slack", "github", "gitlab", "bitbucket", "azure_devops",
    "github_app", "github_install_app", "gitlab_app", "bitbucket_app", "azure_devops_app",
})

#: Days a deleted object's noncurrent version survives — the bucket lifecycle's `noncurrent_days`
#: in the transcript bucket's Terraform, asserted equal by a deployment contract test.
NONCURRENT_EXPIRY_DAYS = 1

#: The `consumer` a revoke's credential decrypt is recorded under (`app/data_access_events.py`).
OFFBOARD_CONSUMER = "tenant_offboarding"

#: The node verb that removes one shared account's token leaf (`apps/jaahd/accounts.py`).
SHARED_DELETE_VERB = "account_shared_delete"

_VENDOR_TIMEOUT = 15.0
_SLACK_REVOKE_URL = "https://slack.com/api/auth.revoke"
_GITLAB_REVOKE_URL = "https://gitlab.com/oauth/revoke"
_GITHUB_API = "https://api.github.com"


class OffboardStopped(Exception):
    """The execution stopped before the purge; `sentence` reaches the operator, `stores` is what ran."""

    def __init__(self, sentence: str, stores: dict[str, Any]) -> None:
        super().__init__(sentence)
        self.sentence = sentence
        self.stores = stores


@dataclass(frozen=True)
class SlotRef:
    project: str
    project_id: int
    number: int
    node_name: Optional[str]
    node_id: Optional[UUID]
    path: str
    state: str

    def label(self) -> str:
        return f"{self.project} #{self.number} on {self.node_name or 'no node'} ({self.path})"


@dataclass(frozen=True)
class AccountRef:
    slug: str
    #: `(node uuid, node name, credential dir)` for every node that holds this account's dir.
    dirs: tuple[tuple[UUID, str, str], ...]
    #: The nodes reporting a mounted token share in the account's trust domain (`_store_nodes`).
    store_nodes: tuple[UUID, ...]


@dataclass
class Census:
    """What the org holds, store by store — the dry run's answer and the executor's worklist."""

    org_id: UUID
    db_rows: dict[str, int]
    #: `(bucket, key)` — each row's own bucket (`TaskAttachment.s3_bucket`; None = the configured one).
    attachment_keys: list[tuple[Optional[str], str]]
    sessions: list[tuple[str, datetime, Optional[str], Optional[str], int]]
    slots: list[SlotRef]
    accounts: list[AccountRef]
    installs: list[tuple[UUID, str]]
    blockers: list[str] = field(default_factory=list)

    def counts(self) -> dict[str, Any]:
        return {
            "db_rows": dict(sorted(self.db_rows.items())),
            "s3_objects": {
                "attachments": len(self.attachment_keys),
                "transcript_sessions": len(self.sessions),
            },
            "worktrees": [slot.label() for slot in self.slots],
            "shared_store": [a.slug for a in self.accounts if a.store_nodes],
            "account_dirs": [f"{a.slug} on {name}" for a in self.accounts for _id, name, _dir in a.dirs],
            "installs": _by_type(self.installs),
        }


def _by_type(installs: list[tuple[UUID, str]]) -> dict[str, int]:
    out: dict[str, int] = {}
    for _id, kind in installs:
        out[kind] = out.get(kind, 0) + 1
    return dict(sorted(out.items()))


# ── The census ─────────────────────────────────────────────────────────────────────────────────────


def _slots(session: Session, org_id: UUID) -> list[SlotRef]:
    """Every slot row that is the org's by any of the three columns that can make it so: its own, its
    node's, its project's. A tenant project's slot on an operator node is the case the org delete misses."""
    node = aliased(Node)
    rows = session.execute(
        select(
            Worktree.number, Worktree.state, Worktree.node_id, Worktree.path, Worktree.project_id, Project.name,
            node.name,
        )
        .join(Project, Project.id == Worktree.project_id)
        .outerjoin(node, node.id == Worktree.node_id)
        .where(or_(Worktree.org_id == org_id, node.org_id == org_id, Project.org_id == org_id))
        # Numbered slots before slot 0: a numbered slot erased after its clone is a tree with a dangling `.git`.
        .order_by(Project.name, node.name, Worktree.number.desc())
    ).all()
    return [
        SlotRef(
            project=r[5], project_id=r.project_id, number=r.number, node_name=r[6], node_id=r.node_id,
            path=r.path, state=r.state,
        )
        for r in rows
    ]


def _store_nodes(
    session: Session, org_id: UUID, pinned: Optional[UUID], legacy_shared: bool,
) -> tuple[UUID, ...]:
    """The nodes that report a mounted token share (`nodes.shared_accounts_root` non-empty) where the
    account's token may sit — every one of them is asked, since an org may hold several stores.

    The org decides the store, not the node: a tenant's co-located nodes mount the tenant's own EFS at the
    same root the fleet share uses, so a node of another org would truthfully answer "absent" about a leaf
    that exists. The pinned node's org (it logged the account in) and the account's own org; only an
    unpinned row an old login recorded on a share (`legacy_shared`, `accounts.shared`) in an org mounting
    none falls back to the operator plane's fleet share."""
    reports_share = func.coalesce(Node.shared_accounts_root, "") != ""
    domain: set = set()
    if pinned is not None:
        pinned_org = session.scalar(select(Node.org_id).where(Node.id == pinned))
        if pinned_org is not None:
            domain.add(pinned_org)
    if session.scalar(select(Node.id).where(Node.org_id == org_id, reports_share).limit(1)) is not None:
        domain.add(org_id)
    elif not domain and legacy_shared:
        domain = set(OPERATOR_PLANE_ORG_IDS)
    if not domain:
        return ()
    rows = session.scalars(
        select(Node.id).where(reports_share, Node.org_id.in_(domain))
        # The pinned node first: it is the one known to mount the store.
        .order_by(Node.id != pinned if pinned is not None else Node.name, Node.name)
    ).all()
    return tuple(rows)


def account_ref(session: Session, org_id: UUID, slug: str, pinned: Optional[UUID], shared: bool) -> AccountRef:
    """Where one account's credentials sit: its dir on every node that holds it, and the nodes mounting its
    token share. Also the console's account Delete (`routes/accounts.py::remove_account`)."""
    holders = set(session.scalars(
        select(AccountObservation.node_id).where(AccountObservation.slug == slug)
    ))
    if pinned is not None:
        holders.add(pinned)
    dirs = []
    for node in session.execute(
        select(Node.id, Node.name, Node.jaah_home).where(Node.id.in_(holders)).order_by(Node.name)
    ).all():
        dirs.append((node.id, node.name, resolve_auth_dir(node.jaah_home, slug)))
    return AccountRef(slug=slug, dirs=tuple(dirs), store_nodes=_store_nodes(session, org_id, pinned, shared))


def _accounts(session: Session, org_id: UUID) -> list[AccountRef]:
    out: list[AccountRef] = []
    for row in session.execute(
        select(Account.slug, Account.node_id, Account.shared, Account.auth_kind)
        .where(Account.org_id == org_id).order_by(Account.slug)
    ).all():
        if account_auth.is_api_key(row.auth_kind):
            continue  # holds nothing on any host; its `credentials` row goes with the purge
        out.append(account_ref(session, org_id, row.slug, row.node_id, bool(row.shared)))
    return out


def accounts_on_node(session: Session, org_id: UUID, node_id: UUID) -> list[AccountRef]:
    """The org's accounts holding a credential dir on `node_id` — the census's own reading, one node's share
    (the operator org transfer of a node erases exactly these: `transfer_node.py`)."""
    return [account for account in _accounts(session, org_id) if any(d[0] == node_id for d in account.dirs)]


def census(session: Session, org: Org, actor: Optional[AdminUser]) -> Census:
    """Count what the org holds and name every reason an execution would be refused now. Reads only."""
    org_id = org.id
    db_rows = org_owned_row_counts(session, org_id)
    db_rows["orgs"] = 1
    found = Census(
        org_id=org_id,
        db_rows=db_rows,
        attachment_keys=org_owned_attachment_keys(session, org_id),
        # EVERY session, not only uploaded ones: an upload that failed midway left objects whose keys
        # derive from the row alone, and once the row is purged nothing could ever name them.
        sessions=[
            (r.agent_session_id, r.started_at, r.transcript_manifest_key, r.transcript_bucket,
             r.transcript_attempts)
            for r in session.execute(
                select(
                    AgentSession.agent_session_id, AgentSession.started_at,
                    AgentSession.transcript_manifest_key, AgentSession.transcript_bucket,
                    AgentSession.transcript_attempts,
                ).where(AgentSession.org_id == org_id)
            ).all()
        ],
        slots=_slots(session, org_id),
        accounts=_accounts(session, org_id),
        installs=[
            (r.id, r.type)
            for r in session.execute(
                select(Credential.id, Credential.type).where(
                    Credential.org_id == org_id, Credential.type.in_(INSTALL_TYPES)
                )
            ).all()
        ],
    )
    if (found.attachment_keys or found.sessions) and attachment_storage.target() is None:
        found.blockers.append(
            "this org has stored objects but object storage is not configured here, so they could not be "
            "deleted — configure SESSION_TRANSCRIPT_BUCKET and AWS credentials first."
        )
    unresolved = [f"{a.slug} on {name}" for a in found.accounts for _id, name, path in a.dirs if not path]
    if unresolved:
        found.blockers.append(
            f"the credential dir of {', '.join(unresolved[:3])} cannot be resolved (the node's Jaah home is "
            "not set) — set it, so the dir can be removed."
        )
    return found


def node_blockers(found: Census, connected: set[str]) -> list[str]:
    """Every node-side step whose node is offline — checked before anything is touched, so an unreachable
    node refuses the execution instead of leaving a tree or a token behind."""
    out: list[str] = []

    def offline(label: str, names: set[str]) -> None:
        if names:
            out.append(f"the node(s) holding this org's {label} are not connected ({', '.join(sorted(names))})"
                       " — bring them online, or delete the nodes first.")

    offline("worktrees", {
        s.node_name or "?" for s in found.slots if s.node_id is not None and str(s.node_id) not in connected
    })
    offline("account credential dirs", {
        name for a in found.accounts for node, name, _dir in a.dirs if str(node) not in connected
    })
    stranded = [a.slug for a in found.accounts if a.store_nodes and not any(str(n) in connected for n in a.store_nodes)]
    if stranded:
        out.append(
            f"no connected node mounts the token share of {', '.join(stranded[:3])}, so the token on it "
            "could not be removed — bring one online."
        )
    return out


# ── Vendor revokes ─────────────────────────────────────────────────────────────────────────────────

#: A revoke's outcome: done, retryable (stops the purge), or beyond reach (a caveat the record names).
REVOKED, RETRY, NO_API = "revoked", "retry", "no_api"


def _fresh_config(row: Credential) -> dict[str, Any]:
    """The credential's config after one refresh, so the token revoked is a live one (an expired access
    token is answered 404 and would leave the refresh token valid). A dead refresh token means the grant
    is already gone, reported by the empty token."""
    from app.durable import git_token  # noqa: PLC0415 — deferred: DBOS steps

    outcome = git_token.rotate_and_persist(str(row.id))
    if not outcome.get("ok"):
        if outcome.get("dead") or not git_token.is_retryable(outcome):
            return {"token": ""}
        raise RuntimeError("the refresh token could not be spent")
    return opened_config_for(session_of(row), row, consumer=OFFBOARD_CONSUMER)


def _vendor_revoke(credential_type: str, config: dict[str, Any], row: Optional[Credential] = None) -> tuple[str, str]:
    """Revoke one install at its vendor: `(REVOKED|RETRY|NO_API, why)`. Never raises, never logs a token."""
    try:
        if credential_type == "slack":
            token = str(config.get("token") or "")
            if not token:
                return REVOKED, "no bot token stored"
            with httpx.Client(timeout=_VENDOR_TIMEOUT, follow_redirects=False) as client:
                response = client.post(_SLACK_REVOKE_URL, headers={"Authorization": f"Bearer {token}"})
            body = response.json() if response.status_code == 200 else {}
            if body.get("ok") or body.get("error") in ("invalid_auth", "token_revoked", "account_inactive"):
                return REVOKED, ""
            return RETRY, f"Slack answered {body.get('error') or response.status_code}"
        if credential_type in ("github", "gitlab"):
            if not config.get("refresh_token"):
                return NO_API, "a pasted personal token: the tenant revokes it; our copy is destroyed"
            base = str(config.get("base_url") or "").rstrip("/")
            if credential_type == "gitlab" and base and base != "https://gitlab.com":
                return NO_API, "a self-managed GitLab: our copy is destroyed"
            from app import git_access  # noqa: PLC0415 — deferred, as in git_access's own callers

            fresh = _fresh_config(row) if row is not None else config
            token = str(fresh.get("token") or "")
            if not token:
                return REVOKED, "the grant was already dead"
            if credential_type == "github":
                # The app that minted it: a GitHub App's token is revoked only with that App's pair.
                app = git_access.app_config(kind=git_access.token_app_kind(token))
                # The GRANT, not the token: it takes the refresh token with it.
                with httpx.Client(timeout=_VENDOR_TIMEOUT, follow_redirects=False) as client:
                    response = client.request(
                        "DELETE", f"{_GITHUB_API}/applications/{app.client_id}/grant",
                        json={"access_token": token}, auth=(app.client_id, app.client_secret),
                        headers={"Accept": "application/vnd.github+json"},
                    )
                ok = response.status_code in (204, 404)
                return (REVOKED, "") if ok else (RETRY, f"GitHub answered HTTP {response.status_code}")
            app = git_access.forge_app_config("gitlab")
            with httpx.Client(timeout=_VENDOR_TIMEOUT, follow_redirects=False) as client:
                response = client.post(_GITLAB_REVOKE_URL, data={
                    "client_id": app.client_id, "client_secret": app.client_secret, "token": token,
                })
            # RFC 7009: 200 for a revoked or already-unknown token; GitLab revokes the pair.
            ok = response.status_code == 200
            return (REVOKED, "") if ok else (RETRY, f"GitLab answered HTTP {response.status_code}")
    except Exception as exc:  # noqa: BLE001 — a vendor hop must not stop the offboarding
        return RETRY, f"{type(exc).__name__} reaching the vendor"
    return NO_API, "the vendor offers no revoke API: our copy is destroyed"


def revoke_installs(session: Session, installs: list[tuple[UUID, str]]) -> dict[str, Any]:
    """Revoke each install at its vendor. Its row goes with the purge."""
    revoked = 0
    retry: list[dict[str, str]] = []
    no_api: list[dict[str, str]] = []
    for credential_id, credential_type in installs:
        row = session.scalar(select(Credential).where(Credential.id == credential_id))
        if row is None:
            continue
        try:
            config = opened_config_for(session, row, consumer=OFFBOARD_CONSUMER)
        except Exception as exc:  # noqa: BLE001 — an unopenable row holds no usable token; still deleted
            log.warning("offboard: credential %s config unopenable (%s)", credential_id, type(exc).__name__)
            no_api.append({"credential": str(credential_id), "type": credential_type,
                           "reason": "its stored config could not be opened"})
            continue
        outcome, why = _vendor_revoke(credential_type, config, row)
        entry = {"credential": str(credential_id), "type": credential_type, "reason": why}
        if outcome == REVOKED:
            revoked += 1
        elif outcome == RETRY:
            retry.append(entry)
        else:
            no_api.append(entry)
    session.commit()
    return {"deleted": len(installs), "vendor_revoked": revoked, "vendor_no_api": no_api, "residual": retry}


# ── Objects ────────────────────────────────────────────────────────────────────────────────────────


def _with_bucket(where: attachment_storage.Target, bucket: Optional[str]) -> attachment_storage.Target:
    """`where` aimed at `bucket` — a row's own, in its own data region (`data_residency.region_of_bucket`):
    signed for another region, S3 refuses the request."""
    if not bucket or bucket == where.bucket:
        return where
    return attachment_storage.Target(
        region=data_residency.region_of_bucket(bucket), bucket=bucket, credentials=where.credentials,
        endpoint=where.endpoint,
    )


def _present(keys: list[tuple[str, str]]) -> list[tuple[str, str]]:
    """The keys whose object is there now. An unanswerable probe counts as present (fail closed)."""
    where = attachment_storage.target()
    if where is None:
        return list(keys)
    out = []
    for bucket, key in keys:
        try:
            if attachment_storage.exists(_with_bucket(where, bucket), key):
                out.append((bucket, key))
        except attachment_storage.StorageUnavailable:
            out.append((bucket, key))
    return out


def transcript_keys(sessions) -> tuple[list[tuple[str, str]], list[str]]:
    """`(bucket, key)` of every transcript/artifact object the org's sessions hold, and the sessions whose
    full member set cannot be known (an upload began, no manifest names its members).

    Every candidate is probed, so the list is what EXISTS — the residue check then reads only those.
    Read before the purge: the keys derive from the session rows, and the CP cannot list the bucket."""
    where = attachment_storage.target()
    candidates: list[tuple[str, str]] = []
    unverifiable: list[str] = []
    for session_id, started_at, manifest_key, bucket, attempts in sessions:
        target_bucket = bucket or (where.bucket if where else "")
        names = {session_transcripts.MAIN_TRANSCRIPT_MEMBER, *session_transcripts.ARTIFACT_MEMBERS}
        manifest_read = False
        if manifest_key and where is not None:
            candidates.append((target_bucket, manifest_key))
            try:
                document = json.loads(attachment_storage.fetch(_with_bucket(where, target_bucket), manifest_key))
                names |= {str(m["name"]) for m in document.get("members", []) if isinstance(m, dict)}
                manifest_read = True
            except Exception as exc:  # noqa: BLE001 — the well-known names are still tried
                log.warning("offboard: manifest for session %s unreadable (%s)", session_id, type(exc).__name__)
        if not manifest_read and attempts:
            unverifiable.append(session_id)
        for name in sorted(names):
            try:
                candidates.append((target_bucket, session_transcripts.member_key(
                    session_id=session_id, started_at=started_at, name=name,
                )))
            except session_transcripts.KeyRefused:
                continue
    return _present(candidates), unverifiable


def delete_objects(keys: list[tuple[str, str]]) -> None:
    where = attachment_storage.target()
    if where is None:
        return
    for bucket, key in keys:
        try:
            attachment_storage.delete(_with_bucket(where, bucket), key)
        except attachment_storage.StorageUnavailable as exc:
            log.warning("offboard: delete of an org object failed (%s)", exc)


def objects_remaining(keys: list[tuple[str, str]]) -> list[str]:
    """The keys still answering — the S3 half of the residual check."""
    return [key for _bucket, key in _present(keys)]


# ── Node-side steps ────────────────────────────────────────────────────────────────────────────────


async def _dispatch(session, node_id: UUID, verb: str, args: dict, actor_email: str) -> tuple[bool, dict, str]:
    """One node command: `(done, parsed stdout, why not)`. Never raises."""
    from app.routes.commands import dispatch_and_audit  # noqa: PLC0415 — service→routes, deferred

    try:
        reply, _audit = await dispatch_and_audit(session, str(node_id), verb, args, actor_email)
    except Exception as exc:  # noqa: BLE001
        # A bus refusal's own detail ("node … is not connected") is the reason a reader can act on.
        detail = getattr(exc, "detail", None)
        return False, {}, str(detail) if isinstance(detail, str) and detail else f"{type(exc).__name__} dispatching to the node"
    result = reply.get("result") if isinstance(reply.get("result"), dict) else {}
    if str(reply.get("status")) == "done":
        try:
            parsed = json.loads(result.get("stdout") or "{}")
        except ValueError:
            parsed = {}
        return True, parsed if isinstance(parsed, dict) else {}, ""
    # `reason`: a refusal before any handler ran (an older daemon's `unknown command_type`).
    return False, {}, str(result.get("stderr") or result.get("reason") or "the node refused").strip()


async def delete_shared_leaves(
    session, accounts: list[AccountRef], connected: set[str], actor_email: str,
    owed: Optional[list[tuple[UUID, str, dict[str, str]]]] = None,
) -> dict:
    """Ask every connected node reporting a token share in the account's domain to remove its leaf: an org
    may hold several stores, so one node's `done` does not speak for another's. Any refusal is residue.
    `owed` collects `(node, verb, args)` for every store node not reached or refusing, to send again later."""
    deleted, residual = 0, []
    for account in (a for a in accounts if a.store_nodes):
        reached = [n for n in account.store_nodes if str(n) in connected]
        if owed is not None:
            owed += [(n, SHARED_DELETE_VERB, {"slug": account.slug}) for n in account.store_nodes if n not in reached]
        if not reached:
            residual.append(f"{account.slug}: no connected node mounts its token share")
            continue
        refused = []
        for node_id in reached:
            done, _out, reason = await _dispatch(
                session, node_id, SHARED_DELETE_VERB, {"slug": account.slug}, actor_email,
            )
            if not done:
                refused.append(reason)
                if owed is not None:
                    owed.append((node_id, SHARED_DELETE_VERB, {"slug": account.slug}))
        if refused:
            residual.append(f"{account.slug}: {refused[0]}")
        else:
            deleted += 1
    session.commit()
    return {"deleted": deleted, "residual": residual}


async def delete_account_dirs(
    session, accounts: list[AccountRef], actor_email: str,
    owed: Optional[list[tuple[UUID, str, dict[str, str]]]] = None,
) -> dict:
    """Each account's credential dir on every node that holds it — the deprovision verb, per node. `owed` collects
    `(node, verb, args)` for every node that did not remove it, to send again later."""
    deleted, residual = 0, []
    for account in accounts:
        for node_id, name, config_dir in account.dirs:
            if not config_dir:
                # `resolve_auth_dir` failed closed: an empty path must never reach a host.
                residual.append(f"{account.slug} on {name}: the node's Jaah home is not set")
                continue
            done, _out, reason = await _dispatch(
                session, node_id, "account_delete", {"config_dir": config_dir}, actor_email,
            )
            if done:
                deleted += 1
            else:
                residual.append(f"{account.slug} on {name}: {reason}")
                if owed is not None:
                    owed.append((node_id, "account_delete", {"config_dir": config_dir}))
    session.commit()
    return {"deleted": deleted, "residual": residual}


async def delete_slots(session: Session, slots: list[SlotRef], actor: AdminUser) -> dict[str, Any]:
    """Each slot through the console's own Delete: ledger closed, row gone, tree erased on its node."""
    from app.routes.pool_slots import delete_one_slot  # noqa: PLC0415

    deleted, erased, not_erased = 0, 0, []
    for slot in slots:
        if slot.node_id is None:
            continue  # a nodeless row has no tree; the purge (or its project's cascade) takes the row
        outcome = await delete_one_slot(
            session, str(slot.node_id), slot.project_id, slot.number,
            actor_email=actor.email, actor_org=actor.org_id,
        )
        deleted += 1
        if outcome.node_erased:
            erased += 1
        else:
            not_erased.append(f"{slot.label()}: {outcome.node_detail or 'not erased'}")
    return {"deleted": deleted, "erased": erased, "residual": not_erased}


# ── The executor ───────────────────────────────────────────────────────────────────────────────────


def _residue(stores: dict[str, Any]) -> list[str]:
    """The stores whose `residual` is non-empty, by name."""
    out = []
    for name, store in stores.items():
        if name == "s3_objects":
            if any(part.get("residual") for part in store.values()):
                out.append(name)
        elif store.get("residual"):
            out.append(name)
    return out


async def execute(
    session: Session, org: Org, found: Census, actor: AdminUser, connected: set[str], stores: dict[str, Any],
) -> None:
    """Empty every store, in `STORES`' dependency order, recording each into the caller's `stores` — so
    whatever stops it, the caller files what ran.

    Raises `OffboardStopped` (carrying what ran) when a store before the purge still holds residue: the
    rows that name it are intact, so a re-run continues. Callers run `census` and the blockers first."""
    from starlette.concurrency import run_in_threadpool  # noqa: PLC0415
    from app.routes.orgs import purge_org  # noqa: PLC0415 — the purge lives beside the org routes

    org_id = org.id
    transcripts, unverifiable = await run_in_threadpool(transcript_keys, found.sessions)
    stores["installs"] = await run_in_threadpool(revoke_installs, session, found.installs)
    stores["shared_store"] = await delete_shared_leaves(session, found.accounts, connected, actor.email)
    stores["account_dirs"] = await delete_account_dirs(session, found.accounts, actor.email)
    stores["worktrees"] = await delete_slots(session, found.slots, actor)
    await run_in_threadpool(delete_objects, transcripts)
    transcripts_left = await run_in_threadpool(objects_remaining, transcripts)
    stores["s3_objects"] = {
        "transcripts": {
            "deleted": len(transcripts) - len(transcripts_left), "residual": transcripts_left,
            "sessions": len(found.sessions), "unverifiable_sessions": unverifiable,
        },
    }
    # Worktrees do not stop the purge: `delete_one_slot` removes the row before the node erases the tree, so
    # a re-run could not retry an unerased one. It stays on the record, by path, and `complete` stays false.
    stopped = [name for name in _residue(stores) if name != "worktrees"]
    if stopped:
        raise OffboardStopped(
            f"residue remains in {', '.join(stopped)} — the org's rows were NOT purged, so running the "
            "offboarding again retries exactly what is left (see the record).",
            stores,
        )

    attachments = [(bucket or "", key) for bucket, key in found.attachment_keys]
    # The wind-down, the purge, its `org_deleted` row and the attachment-object reap: the purge itself.
    await purge_org(org_id=str(org_id), user=actor, session=session)

    session.expire_all()
    residual_rows = org_owned_row_counts(session, org_id)
    if session.scalar(select(Org.id).where(Org.id == org_id)) is not None:
        residual_rows["orgs"] = 1
    session.rollback()
    stores["db_rows"] = {"deleted": found.db_rows, "residual": residual_rows}
    attachments_left = await run_in_threadpool(objects_remaining, attachments)
    stores["s3_objects"]["attachments"] = {
        "deleted": len(attachments) - len(attachments_left), "residual": attachments_left,
    }


def record(org: dict, actor: AdminUser, started_at: str, stores: dict[str, Any], *, stopped: Optional[str] = None) -> dict:
    """The completion record: per-store counts and residue, operator, timestamps, and the caveats."""
    caveats = []
    for entry in stores.get("installs", {}).get("vendor_no_api", []):
        caveats.append(f"{entry['type']} credential {entry['credential']}: {entry['reason']}")
    unverifiable = stores.get("s3_objects", {}).get("transcripts", {}).get("unverifiable_sessions", [])
    if unverifiable:
        caveats.append(
            f"{len(unverifiable)} session(s) began an upload but no manifest names their members, so objects "
            "under names the control plane never issued cannot be proven gone"
        )
    caveats.append("each member's authorization of the GitHub OAuth App on github.com is theirs to remove")
    caveats.append("an org's own shared-accounts EFS lives in the tenant's AWS account and is theirs to delete")
    completed = datetime.now(timezone.utc)
    complete = stopped is None and not _residue(stores) and not unverifiable and set(stores) == set(STORES)
    return {
        "kind": "tenant_offboarding",
        "org": org,
        "operator": actor.email,
        "started_at": started_at,
        "completed_at": completed.isoformat(timespec="seconds"),
        "complete": complete,
        "stopped": stopped,
        "s3_erasure": (
            "current objects deleted; their noncurrent versions expire by "
            f"{(completed + timedelta(days=NONCURRENT_EXPIRY_DAYS + 1)).date().isoformat()} (bucket lifecycle)"
        ),
        "stores": stores,
        "caveats": caveats,
    }
