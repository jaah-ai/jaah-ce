"""The activity-log row vocabulary, in code so a change breaks loudly (`tests/test_activity_kinds.py`).

`app/user_activity.py` reads `SENTENCE_TEMPLATES` and `SUBJECT_PARAMS` at runtime; the audited call-site
keep list (`KEPT_RECORD_WRITE_SITES`) lives in that test, which stops a cleanup silently deleting a survivor.

The middleware writes one `activity_events` row per API mutation, `kind = route.endpoint.__name__`
(`authz.declare` returns the handler itself, not a wrapper). Kept sites therefore produce two rows per
mutation (different kinds, different facts), no-op PUTs still get a middleware row, and middleware
rows are best-effort where `record_write` propagates (D5+D13). Handler names are not unique
(`get_credential`, `get_port_grid`), so the test asserts uniqueness among non-GET routes.
"""
from __future__ import annotations

import unicodedata
from typing import Any, Callable, Optional

# ── Path params that name the row a request acted on ──────────────────────────────────────────────

# Path param -> subject noun, first match wins in declaration order. Order is load-bearing:
# `POST /api/accounts/{account_id}/nodes/{node_id}/deprovision` carries two, and `node_id` must come
# first so the node is the subject. Pinned by `test_activity_kinds.py`.
SUBJECT_PARAMS: dict[str, str] = {
    "task_id": "task",
    "project_id": "project",
    "user_id": "user",
    "group_id": "group",
    "cron_id": "cron",
    "credential_id": "credential",
    "tag_id": "tag",
    "node_id": "node",
    "account_id": "account",
    # `/api/node-orders/{order_id}/…` (retry, re-issue, confirm, dismiss): the order, labelled by the node's name.
    "order_id": "node_order",
}


# ── Sentence templates ────────────────────────────────────────────────────────────────────────────

Template = Callable[[Any, Any, dict[str, Any]], Optional[str]]

# Caller-controlled values are flattened and capped before reaching a sentence: a newline could forge
# a second log entry, and an unbounded value an unbounded summary.
MAX_FIELD_CHARS = 120


def _usable(body: Any) -> dict[str, Any]:
    """`body` as a plain dict, or `{}` when there is nothing usable in it.

    A captured body may be `None`, a non-dict, or `serialise_body`'s truncation marker; all must make
    the template return `None` so the caller falls back to `"{METHOD} {path}"`."""
    if not isinstance(body, dict) or body.get("_truncated"):
        return {}
    return body


def _text(*values: Any) -> Optional[str]:
    """The first usable value as one flat, capped line — or `None`.

    Several sources so a template can prefer the response's normalised value over the request's."""
    for value in values:
        if not isinstance(value, str) or not value.strip():
            continue
        # Before the flatten: lone surrogates are replaced (unencodable, would roll back the audited
        # mutation); `Cc`/`Cf` chars become spaces (NUL, ANSI escapes, RTL override); the flatten then
        # collapses runs so a newline cannot forge an entry.
        storable = value.encode("utf-8", errors="replace").decode("utf-8")
        flat = "".join(
            " " if unicodedata.category(char) in ("Cc", "Cf") else char for char in storable
        )
        if not flat.strip():
            continue
        return " ".join(flat.split())[:MAX_FIELD_CHARS]
    return None


def _count(value: Any) -> Optional[int]:
    """The number of distinct entries in a caller-supplied list (routes count `sorted(set(...))`).
    `None` when not a countable list; a template degrades rather than raises."""
    if not isinstance(value, list):
        return None
    try:
        return len(set(value))
    except TypeError:
        return None


def _create_group(request_body: Any, response_body: Any, params: dict[str, Any]) -> Optional[str]:
    name = _text(_usable(response_body).get("name"), _usable(request_body).get("name"))
    return None if name is None else f"Created group {name}"


def _create_user(request_body: Any, response_body: Any, params: dict[str, Any]) -> Optional[str]:
    email = _text(_usable(response_body).get("email"), _usable(request_body).get("email"))
    return None if email is None else f"Created user {email}"


def _create_project(request_body: Any, response_body: Any, params: dict[str, Any]) -> Optional[str]:
    """Every create reaching this label has an authenticated admin (`NODE_TOKEN_API_PATHS` grants the node
    token `GET /api/projects` only), so it always lands a row."""
    name = _text(_usable(response_body).get("name"), _usable(request_body).get("name"))
    return None if name is None else f"Created project {name}"


def _allow_instance_types(request_body: Any, response_body: Any, params: dict[str, Any]) -> Optional[str]:
    """Born on the capture path: the response names what was new, the request what was asked."""
    response, request = _usable(response_body), _usable(request_body)
    added, asked = response.get("added"), request.get("instance_types")
    named = added if isinstance(added, list) and added else asked
    if not isinstance(named, list):
        return None
    types = _text(", ".join(t for t in named if isinstance(t, str)))
    if types is None:
        return None
    return f"Allowed instance types {types}" if named is added else f"Allowed instance types {types} (already allowed)"


#: How each `NodeOrderIn.path` reads in a sentence (`routes/node_orders.py`).
_NODE_ORDER_PATHS: dict[str, str] = {
    "self_hosted": "self-hosted", "cloud": "your cloud", "rent_dedicated": "dedicated hosting",
    "rent_shared": "shared hosting", "co_locate": "co-located",
}


def _create_node_order(request_body: Any, response_body: Any, params: dict[str, Any]) -> Optional[str]:
    """Born on the capture path: an order is how the console adds a node, so its row names the node
    and how it is hosted. The response names no node (only the order's stage), so the request is read."""
    request = _usable(request_body)
    name = _text(request.get("name"))
    if name is None:
        return None
    path = request.get("path")
    how = _NODE_ORDER_PATHS.get(path) if isinstance(path, str) else None
    return f"Started adding node {name}" + (f" ({how})" if how else "")


def _restart_cloud_node(request_body: Any, response_body: Any, params: dict[str, Any]) -> Optional[str]:
    """Born on the capture path: a reboot of a node's machine (the node is the row's subject). `(forced)` says
    `force` was sent, not that work was at risk; the 202's `cloud` block proves the reboot was asked."""
    if "instance_id" not in _usable(response_body):
        return None
    forced = _usable(request_body).get("force") is True
    return "Restarted the node's machine" + (" (forced)" if forced else "")


def _restart_node(request_body: Any, response_body: Any, params: dict[str, Any]) -> Optional[str]:
    """Born on the capture path: one node's daemon restarted, named off the 202."""
    restarted = _usable(response_body).get("restarted")
    if not isinstance(restarted, list) or not restarted:
        return None
    return f"Restarted node {_text(restarted[0])}"


def _restart_host(request_body: Any, response_body: Any, params: dict[str, Any]) -> Optional[str]:
    """Born on the capture path: a host's machine rebooted, or its nodes' daemons restarted, read off the 202."""
    response = _usable(response_body)
    if response.get("rebooted") is True:
        return "Rebooted the host's machine"
    restarted = response.get("restarted")
    if not isinstance(restarted, list):
        return None
    return f"Restarted {len(restarted)} node(s) on the host"


def _change_cloud_node_type(request_body: Any, response_body: Any, params: dict[str, Any]) -> Optional[str]:
    """Born on the capture path: the type change the 202 shows running, read off the response."""
    change = _usable(response_body).get("change")
    if not isinstance(change, dict):
        return None
    old, new = _text(change.get("from_type")), _text(change.get("to_type"))
    if new is None:
        return None
    return f"Started changing the node's machine type to {new}" + (f" from {old}" if old else "")


#: What `PUT /api/auth/me` changed, by body field, in the order the sentence names them.
_SELF_UPDATE_PARTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("profile", ("first_name", "last_name", "email")),
    ("password", ("password",)),
    ("preferences", ("preferences",)),
    ("Git credential", ("git_credential_id",)),
)


def _update_me(request_body: Any, response_body: Any, params: dict[str, Any]) -> Optional[str]:
    """Born on the capture path: names the parts a person changed on their own account, read off the
    request's keys (the redacted body keeps a password's key, never its value)."""
    request = _usable(request_body)
    parts = [part for part, keys in _SELF_UPDATE_PARTS if any(key in request for key in keys)]
    if not parts:
        return None
    named = parts[0] if len(parts) == 1 else f"{', '.join(parts[:-1])} and {parts[-1]}"
    return f"Updated their {named}"


# One entry per handler scheduled for retirement, keyed by the new kind (the handler's `__name__`).
# Each sentence is moved from its call site; `test_activity_kinds.py` checks the literal segments.
# Departures: `_text` flattens and caps values, and a request-value fallback gets the raw value where
# the site used a normalised one (the response is preferred). Every template returns `None` rather than
# a half-rendered sentence when inputs are missing.
SENTENCE_TEMPLATES: dict[str, Template] = {
    "create_group": _create_group,
    "create_user": _create_user,
    "create_project": _create_project,
    "allow_instance_types": _allow_instance_types,
    "update_me": _update_me,
    "create_node_order": _create_node_order,
    "restart_cloud_node": _restart_cloud_node,
    "change_cloud_node_type": _change_cloud_node_type,
    "restart_node": _restart_node,
    "restart_host": _restart_host,
}


# ── Security events (SOC 2 G-16, PRD 5.4) ─────────────────────────────────────────────────────────────

# The kinds the retention sweep keeps hot for a year whatever the activity-log knob says
# (`app/cron_retention.py::SECURITY_EVENT_MIN_RETENTION_DAYS`) and the nightly export archives to the
# Object Lock bucket (`app/security_archive.py`). Families: sign-in and its refusals, MFA and SSO; user,
# membership, role and org-access changes; credential CRUD, test and read; node-token issue and revoke; data hand-overs;
# offboarding. Recorded rows and middleware handler names (`create_credential`, `reissue_node_token`, …)
# both, since a handler-named capture row is the only record of some of these. No break-glass kind exists
# yet; one joins here when it does. `tests/test_security_archive.py` asserts every recorded kind of these
# families is listed, so a new `mfa_*`/`credential_*`/… kind cannot silently fall out of the archive.
SECURITY_EVENT_KINDS: frozenset[str] = frozenset({
    # Sign-in, MFA, SSO.
    "sign_in", "login_failed", "login_locked",
    "mfa_enrolled", "mfa_reset", "mfa_recovery_used", "mfa_recovery_regenerated", "terms_accepted",
    "mfa_device_trusted", "mfa_device_revoked", "mfa_skip_changed", "mfa_skipped",
    "sso_config_changed", "sso_connection_changed", "sso_link_reset", "password_reset_requested", "password_reset_completed",
    # Users, superadmin, memberships, roles.
    "create_user", "user_created", "user_invited", "user_updated", "user_deleted", "user_disabled", "user_enabled",
    "user_domain_join", "superadmin_granted", "superadmin_revoked", "sign_in_override_changed",
    "membership_added", "membership_removed", "role_binding_add", "role_binding_remove",
    "accept_invitation", "decline_invitation",
    # `project_role_revoked` is HISTORICAL: written when a lost project access cleared a retired "Humans" role; no
    # site writes it now, and old rows still carry it.
    "project_role_revoked",
    # Org access: who may join, and the decisions that admit or remove a whole tenant.
    "org_signup", "org_approved", "org_auto_approved", "org_rejected", "org_readmitted", "org_default_role",
    "org_domain_add", "org_domain_remove", "domain_join_skipped", "org_wound_down", "org_deleted", "org_restored",
    # `signup_draft_purged` is HISTORICAL: the retired `signup-drafts` cron wrote it when it deleted an abandoned
    # sign-up; no site writes it now, and old rows still carry it.
    "signup_draft_purged",
    "org_compliance_deleted", "org_compliance_delete_refused", "org_offboarded", "org_slug_renamed",
    "org_tailnet_retagged",
    # Credentials: CRUD (recorded and handler-named), test, read, forge links.
    "credential_created", "credential_updated", "credential_deleted", "credential_tested",
    "credential_read", "credential_attached", "credential_detached", "credential_connected",
    "credential_nodevault_toggled", "credential_mcp_linked", "credential_mcp_unlinked", "credential_owner_changed",
    "change_credential_owner", "account_owner_changed",
    "create_credential", "update_credential", "delete_credential", "git_connected",
    # An org's Slack workspace (`routes/slack_connect.py`): installing it routes the org's alerts there.
    "slack_connected", "disconnect_slack_workspace",
    # An org's own Gmail (`app/tenant_gmail/`): a connection is a standing credential to send as the mailbox.
    "gmail_connected", "connect_gmail_service_account", "delete_gmail_connection",
    # Its hosted trigger addresses (`app/tenant_gmail/addresses.py`): each one opens a way for mail into its triggers.
    "create_tenant_address", "revoke_tenant_address",
    # Node tokens: issued on create, re-issued on rotation, revoked with the node.
    "create_node", "reissue_node_token", "delete_node",
    # The console's Add node flow (`routes/node_orders.py`): an order registers its machine and mints its token, a retry
    # registers it anew, and a re-issued join command mints a fresh one.
    "create_node_order", "retry_node_order", "reissue_node_order_command",
    # Data hand-overs.
    "transcript_downloaded", "artifact_downloaded",
    # A deleted task's (or org's) signed compliance manifests erased through their retention.
    "compliance_manifests_erased",
    # HIPAA mode (`app/hipaa.py`): the setting, an org admin's acceptance of a non-BAA surface, and a read of
    # data a HIPAA org may hold (`phi_read`, recorded by the PHI audit).
    "hipaa_changed", "hipaa_warning_acknowledged", "phi_read",
    # An org shortening its own retention (`routes/org_retention.py`, the handler-named capture row): kept a year
    # hot so a shortened activity window cannot erase the record of shortening it.
    "put_retention",
    # Where an org's audit trail is sent (`routes/org_siem.py`, the handler-named capture rows): re-pointing or
    # removing it is how a trail would be diverted or silenced.
    "put_siem_export", "delete_siem_export",
    # Who may provision an org's members (`routes/scim.py`, the handler-named capture rows): a token is a standing
    # admin credential.
    "create_scim_token", "revoke_scim_token", "remove_scim_token",
    # Jaah API keys (`app/api_keys.py`): a member's standing bearer credential — its life and every refusal, recorded
    # and handler-named.
    "api_key_created", "api_key_edited", "api_key_rotated", "api_key_revoked", "api_key_expired", "api_key_refused",
    "create_api_key", "edit_api_key", "rotate_api_key", "revoke_api_key",
    # How an org's members sign in (`routes/org_sso.py`, the handler-named capture rows): its identity provider, and
    # the domains whose people it signs in.
    "put_sso", "delete_sso", "add_sso_domain", "verify_sso_domain", "delete_sso_domain",
})


# ── NodeVault labels ──────────────────────────────────────────────────────────────────────────────

# The recorded `nodevault.*` kinds (`durable/nodevault.py`, `routes/nodevault.py`). The Audit page finds every
# NodeVault row with a plain `q=nodevault` search, which also matches the credential toggle's kind and every
# `/api/nodevault` request path.
NODEVAULT_KIND_PREFIX = "nodevault."


def nodevault_label(kind: str) -> Optional[str]:
    """The Audit label of a NodeVault kind ("NodeVault key delivered"), or None for any other kind. Spelled
    here because a plain humanize reads `nodevault.key_delivered` as "Nodevault.key delivered"."""
    if kind == "credential_nodevault_toggled":
        return "NodeVault toggled"
    if kind.startswith(NODEVAULT_KIND_PREFIX):
        return "NodeVault " + kind.removeprefix(NODEVAULT_KIND_PREFIX).replace("_", " ")
    return None
