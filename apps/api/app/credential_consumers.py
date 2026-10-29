"""Every control-plane CONSUMER of a credential — the single home for NodeVault eligibility (D1, AC-5.3).

The control plane holds no NodeVault key (AC-8.1), so a credential it opens itself can never be a NodeVault
credential, and a NodeVault credential can never back something it opens. This module is the one list of
those consumers and the two refusals that follow from it:

* `consumers_of(session, credential_id)` — who on the control plane uses a credential. Non-empty means it may
  not be marked NodeVault (`routes/credentials/`, and `eligibility` for the form).
* `refuse_if_nodevault(credential, label)` — called by every consumer's WRITE door, so pointing a consumer at
  a NodeVault credential is a 409 naming the consumer, never a 500 later. At consumption the open itself
  refuses (`credential_crypto.open_sealed` raises the same `NodeVaultUnavailable`).

A consumer references a credential one of three ways, each declared here as data:

1. an FK column (`FK_CONSUMERS`). A census test fails when a new FK to `credentials.id` is in neither
   `FK_CONSUMERS` nor `NODE_SIDE_FKS` (`tests/test_credential_consumers.py`);
2. an operator-settings knob whose value is a credential id (`Knob.credential_type`, `KNOB_LABELS`; every
   such knob must have a label, tested). Notify targets, Slack notifications, Stripe, Bedrock, the LLM and
   embeddings keys, headscale/vnodes, email and site enquiry all reach their credential this way;
3. JSON pointers outside the knob table (`Node.infra`'s launch keys) and the one
   pointer-free consumer, Slack socket mode, which connects as an operator-plane `slack` row holding both
   tokens.

Not consumers (none stores a pointer): tenancy offboard revokes the installs it is handed; MFA seals its own
factor. Project env vars are deliberately NOT a refusing consumer: a NodeVault field resolves as unavailable
(`project_env.REASON_NODEVAULT`, AC-5.4). Nothing here opens a secret.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Optional, Sequence
from uuid import UUID

from sqlalchemy import Text, Uuid, any_, exists, literal, select, type_coerce
from sqlalchemy.dialects.postgresql import ARRAY, JSONB
from sqlalchemy.orm import InstrumentedAttribute, Session

from app.credential_keys import NodeVaultUnavailable
from app.models.automations import TaskTrigger
from app.models.control_plane import SystemState
from app.models.credentials import (
    Account,
    CloudConnection,
    Credential,
    NodeVaultOperationItem,
    ProjectCredential,
    UserGitConnection,
)
from app.models.identity import AdminUser
from app.models.node_groups import GhaRunnerPool, NodeGroup
from app.models.nodelinks import OrgSharedStore
from app.models.nodes import Node
from app.models.orgs import OrgSsoConnection, OrgSetting
from app.models.projects import Project, ProjectEnvVar

__all__ = [
    "NodeVaultUnavailable",
    "consumers_of",
    "consumers_of_row",
    "delete_blockers",
    "references_of",
    "refuse_if_nodevault",
    "type_ineligible_reason",
]

# Labels: what an operator reads in a refusal ("<label>: NodeVault credential — not available to Jaah") and
# in the form's eligibility list. Constants, so a write door and the registry cannot spell one differently.
GIT_ACCESS = "Git access"
PROJECT_JIRA = "Project Jira integration"
AGENT_ACCOUNT = "Agent account"
CLOUD_NODE_DEFAULTS = "Cloud connections"
CLOUD_NODE_TAILNET = "Cloud node tailnet"
CLOUD_NODE_LAUNCH = "Cloud node launch"
NODE_GROUP = "Node group"
GHA_RUNNERS = "GitHub Actions runners"
SHARED_STORAGE = "Shared storage"
TRIGGER_PREFLIGHT = "Trigger model preflight"
SLACK_APP = "Slack app connection"
SSO = "SSO"

#: The reason an OAuth2 type is never eligible (D7): the control plane must open the row to refresh its tokens.
#: Exact text pinned by AC tests.
OAUTH2_REASON = "Jaah's servers refresh this credential's Connect tokens, so it can't use NodeVault"


@dataclass(frozen=True)
class FkConsumer:
    column: InstrumentedAttribute
    label: str


#: Every FK to `credentials.id` that the CONTROL PLANE opens. (table, column) is what the census matches.
FK_CONSUMERS: tuple[FkConsumer, ...] = (
    FkConsumer(AdminUser.git_credential_id, GIT_ACCESS),
    FkConsumer(UserGitConnection.credential_id, GIT_ACCESS),
    FkConsumer(Project.jira_credential_id, PROJECT_JIRA),
    FkConsumer(Account.credential_id, AGENT_ACCOUNT),
    FkConsumer(CloudConnection.credential_id, CLOUD_NODE_DEFAULTS),
    FkConsumer(NodeGroup.credential_id, NODE_GROUP),
    FkConsumer(GhaRunnerPool.credential_id, GHA_RUNNERS),
    FkConsumer(OrgSharedStore.credential_id, SHARED_STORAGE),
    FkConsumer(TaskTrigger.preflight_credential_id, TRIGGER_PREFLIGHT),
    FkConsumer(OrgSsoConnection.oidc_credential_id, SSO),
)

#: FKs to `credentials.id` that are NOT a control-plane consumer, each with why. The census requires every
#: FK to be in one of the two tuples, so adding an FK forces this decision.
NODE_SIDE_FKS: Mapping[tuple[str, str], str] = {
    ("project_credentials", "credential_id"): "a grant: the runner fetch hands the credential to a node",
    ("project_env_vars", "credential_id"): "resolves as unavailable for NodeVault (AC-5.4), never refused",
    ("nodevault_operation_items", "credential_id"): "NodeVault's own migration bookkeeping",
}

#: `NODE_SIDE_FKS` as columns, each in operator words: a row one points at cannot change org, or the pointer would
#: cross tenants (`references_of` refuses it; no database key does). A census test keeps the two in step.
NODE_SIDE_REFERENCES: tuple[FkConsumer, ...] = (
    FkConsumer(ProjectCredential.credential_id, "Project access"),
    FkConsumer(ProjectEnvVar.credential_id, "Project env var"),
    FkConsumer(NodeVaultOperationItem.credential_id, "NodeVault migration"),
)

#: Every operator-settings knob that names a credential (`Knob.credential_type`), in operator words. The
#: census test fails on a credential knob missing here.
KNOB_LABELS: Mapping[str, str] = {
    "support_kb_credential_id": "Support assistant embeddings",
    "billing_stripe_credential_id": "Stripe billing",
    "platform_bedrock_credential_id": "Bedrock",
    "cloud_node_tailscale_credential_id": CLOUD_NODE_TAILNET,
    "git_app_credential_id": "GitHub OAuth App",
    "git_github_app_credential_id": "GitHub App",
    "compliance_github_credential_id": "Compliance evidence",
    "gitlab_app_credential_id": "GitLab app",
    "bitbucket_app_credential_id": "Bitbucket app",
    "azure_devops_app_credential_id": "Microsoft Entra app",
    "notifications_slack_credential_id": "Slack notifications",
    "notifications_telegram_credential_id": "Telegram notifications",
    "tenant_gmail_oauth_credential_id": "Gmail OAuth client",
    "email_unsubscribe_credential_id": "Email unsubscribe links",
    "site_enquiry_recaptcha_credential_id": "Site enquiry reCAPTCHA",
    "site_enquiry_hubspot_credential_id": "Site enquiry HubSpot",
    "support_llm_credential_id": "Support assistant model",
    "memory_llm_credential_id": "Project memory model",
    "trigger_preflight_llm_credential_id": "Trigger preflight platform model",
}

#: `Node.infra` keys holding a credential id the control plane signs or provisions with
#: (`cloud_node_launch.SHARED_STORE_CREDENTIAL_KEY` and the launch's `credential_id`).
NODE_INFRA_KEYS: tuple[str, ...] = ("credential_id", "shared_storage_credential_id")

_OPERATOR_SETTINGS_KEY = "operator_settings"  # `operator_settings.catalog.STATE_KEY`


def _ordered_unique(labels: list[str]) -> list[str]:
    return list(dict.fromkeys(labels))


def _fk_labels(session: Session, credential_id: UUID) -> list[str]:
    return [
        consumer.label
        for consumer in FK_CONSUMERS
        if session.scalar(select(exists().where(consumer.column == credential_id)))
    ]


def _knob_labels(session: Session, credential_id: UUID) -> list[str]:
    wanted = str(credential_id)
    labels: list[str] = []
    row = session.get(SystemState, _OPERATOR_SETTINGS_KEY)
    stored: Mapping[str, Any] = row.value if row is not None and isinstance(row.value, dict) else {}
    labels += [label for key, label in KNOB_LABELS.items() if str(stored.get(key) or "").strip() == wanted]
    org_keys = session.scalars(
        select(OrgSetting.key).where(OrgSetting.key.in_(tuple(KNOB_LABELS)), OrgSetting.value == type_coerce(wanted, JSONB))
    ).all()
    labels += [KNOB_LABELS[key] for key in org_keys]
    return labels


def _json_pointer_labels(session: Session, credential_id: UUID) -> list[str]:
    wanted = str(credential_id)
    labels: list[str] = []
    for key in NODE_INFRA_KEYS:
        if session.scalar(select(exists().where(Node.infra[key].astext == wanted))):
            labels.append(CLOUD_NODE_LAUNCH)
            break
    return labels


def _is_slack_app(
    session: Session, credential: Credential, config: Optional[Mapping[str, Any]], type_slug: Optional[str]
) -> bool:
    """`slack_socket._load_credential`'s candidate rule: an operator-plane `slack` row holding both tokens,
    created by a superadmin or the system actor."""
    from app.tenancy.ids import OPERATOR_PLANE_ORG_IDS

    config = credential.config if config is None else config
    if not (
        (type_slug or credential.type) == "slack"
        and credential.org_id in OPERATOR_PLANE_ORG_IDS
        and bool((config or {}).get("token"))
        and bool((config or {}).get("app_token"))
        and credential.created_by_id is not None
    ):
        return False
    return bool(session.scalar(select(exists().where(
        AdminUser.id == credential.created_by_id,
        (AdminUser.is_superadmin.is_(True)) | (AdminUser.is_system.is_(True)),
    ))))


def consumers_of_row(
    session: Session, credential: Credential, config: Optional[Mapping[str, Any]] = None, *,
    type_slug: Optional[str] = None,
) -> list[str]:
    """The labels of every control-plane consumer of `credential`, deduplicated, in registry order.

    `config` and `type_slug` override the row's own for the content rule (a create or PATCH checks the type
    and config it is about to write). A row not yet flushed has no pointers, so only that rule can match it (give it `created_by_id`)."""
    labels: list[str] = []
    if credential.id is not None:
        labels += _fk_labels(session, credential.id)
        labels += _knob_labels(session, credential.id)
        labels += _json_pointer_labels(session, credential.id)
    if _is_slack_app(session, credential, config, type_slug):
        labels.append(SLACK_APP)
    return _ordered_unique(labels)


def consumers_of(session: Session, credential_id: UUID) -> list[str]:
    """`consumers_of_row` by id; `[]` for a credential `session` cannot see."""
    # A seam-scoped select, not `session.get` (which skips the seam on an identity-map hit).
    credential = session.scalar(select(Credential).where(Credential.id == credential_id))
    return consumers_of_row(session, credential) if credential is not None else []


def references_of(session: Session, credentials: Sequence[Credential]) -> dict[UUID, list[str]]:
    """What points at each of `credentials`, by label: `consumers_of_row`'s consumers plus every node-side FK
    (`NODE_SIDE_REFERENCES`) — the set a change of org would strand in the old org. Rows nothing points at are
    absent. A fixed number of queries whatever the set's size (one per FK, two for knobs, one per node infra key),
    plus one per `slack` row for the Slack app rule."""
    found: dict[UUID, list[str]] = {}
    if not credentials:
        return found
    ids = [row.id for row in credentials]
    wanted = {str(credential_id): credential_id for credential_id in ids}
    array = any_(literal(ids, type_=ARRAY(Uuid())))
    for consumer in (*FK_CONSUMERS, *NODE_SIDE_REFERENCES):
        for credential_id in session.scalars(select(consumer.column).where(consumer.column == array).distinct()):
            found.setdefault(credential_id, []).append(consumer.label)
    state = session.get(SystemState, _OPERATOR_SETTINGS_KEY)
    stored: Mapping[str, Any] = state.value if state is not None and isinstance(state.value, dict) else {}
    knob_hits = [(key, str(stored.get(key) or "").strip()) for key in KNOB_LABELS]
    knob_hits += [
        (key, value if isinstance(value, str) else "")
        for key, value in session.execute(select(OrgSetting.key, OrgSetting.value).where(OrgSetting.key.in_(tuple(KNOB_LABELS))))
    ]
    for key, value in knob_hits:
        if value in wanted:
            found.setdefault(wanted[value], []).append(KNOB_LABELS[key])
    text_array = any_(literal(list(wanted), type_=ARRAY(Text())))
    for key in NODE_INFRA_KEYS:
        for value in session.scalars(select(Node.infra[key].astext).where(Node.infra[key].astext == text_array)):
            found.setdefault(wanted[value], []).append(CLOUD_NODE_LAUNCH)
    for row in credentials:
        if row.type == "slack" and _is_slack_app(session, row, None, None):
            found.setdefault(row.id, []).append(SLACK_APP)
    return {credential_id: _ordered_unique(labels) for credential_id, labels in found.items()}


def delete_blockers(session: Session, credential_ids: Sequence[UUID]) -> dict[UUID, list[str]]:
    """Which of `credential_ids` a consumer's `ON DELETE RESTRICT` key holds, by label: deleting one would fail at
    the database. One query per such key (`FK_CONSUMERS`, read off the schema), whatever the set's size."""
    found: dict[UUID, list[str]] = {}
    if not credential_ids:
        return found
    array = any_(literal(list(credential_ids), type_=ARRAY(Uuid())))
    for consumer in FK_CONSUMERS:
        keys = [fk for column in consumer.column.property.columns for fk in column.foreign_keys]
        if not any((fk.ondelete or "NO ACTION").upper() in ("RESTRICT", "NO ACTION") for fk in keys):
            continue
        for credential_id in session.scalars(select(consumer.column).where(consumer.column == array).distinct()):
            found.setdefault(credential_id, []).append(consumer.label)
    return {credential_id: _ordered_unique(labels) for credential_id, labels in found.items()}


def consumers_reason(labels: list[str]) -> str:
    """The one sentence an in-use credential is refused NodeVault with, naming its consumers."""
    return f"Used by Jaah's servers ({', '.join(labels)}), so it can't be NodeVault"


def type_ineligible_reason(type_slug: str) -> Optional[str]:
    """Why a credential TYPE can never be NodeVault, or None when it can (`CredentialTypeSpec.nodevault_eligible`)."""
    from app.credentials.registry import CREDENTIAL_TYPES

    spec = CREDENTIAL_TYPES.get(type_slug)
    if spec is None:
        return "Unknown credential type"
    return None if spec.nodevault_eligible else spec.nodevault_reason or OAUTH2_REASON


#: The secret a row's OAuth grant leaves: the control plane refreshes it (`oauth2_tokens.py`, forge Connect).
_OAUTH_REFRESH_FIELD = "refresh_token"


def oauth2_reason(type_slug: str, config: Optional[Mapping[str, Any]]) -> Optional[str]:
    """`type_ineligible_reason`, plus a row of an otherwise eligible type holding an OAuth refresh token (a forge
    credential made by Connect): the control plane refreshes it, so it is OAuth2 too (D7)."""
    reason = type_ineligible_reason(type_slug)
    if reason is None and (config or {}).get(_OAUTH_REFRESH_FIELD):
        return OAUTH2_REASON
    return reason


def eligibility(session: Session, credential: Credential) -> tuple[Optional[str], list[str]]:
    """(reason it may not be NodeVault or None, consumer labels). Eligible = the type (and row) is eligible AND
    no control-plane consumer opens it."""
    consumers = consumers_of_row(session, credential)
    reason = oauth2_reason(credential.type, credential.config)
    if reason is None and consumers:
        reason = consumers_reason(consumers)
    return reason, consumers


def refuse_if_nodevault(credential: Credential, consumer_label: str) -> None:
    """Raise `NodeVaultUnavailable` naming `consumer_label` when `credential` is NodeVault — the call every
    consumer's write door makes before it stores a pointer (`main.py` answers it 409)."""
    if credential.nodevault:
        raise NodeVaultUnavailable(
            f"{consumer_label}: NodeVault credential — not available to Jaah", consumer=consumer_label
        )
