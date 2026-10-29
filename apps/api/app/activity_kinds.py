"""The activity-log row vocabulary, in code so a change breaks loudly (`tests/test_activity_kinds.py`).

`app/user_activity.py` reads `SENTENCE_TEMPLATES` and `SUBJECT_PARAMS` at runtime; the call-site
tuples are read by the tests and stop a cleanup silently deleting a survivor.

The middleware writes one `activity_events` row per API mutation, `kind = route.endpoint.__name__`
(`authz.declare` returns the handler itself, not a wrapper). Kept sites therefore produce two rows per
mutation (different kinds, different facts), no-op PUTs still get a middleware row, and middleware
rows are best-effort where `record_write` propagates (D5+D13). Handler names are not unique
(`get_credential`, `get_port_grid`), so the test asserts uniqueness among non-GET routes.
"""
from __future__ import annotations

import unicodedata
from typing import Any, Callable, Optional

# ── The audited call-site keep list ───────────────────────────────────────────────────────────────

# The hand-written audit call sites the capture cutover must not retire. Criterion (D13, per site):
# keep a site when its row (`summary` and `data`) records a value not derivable from the request body,
# response body, path params, query string or the captured row's own columns, on any reachable path.
# Recurring keeps: a 204 with no body, a before/after pair, a pre-delete snapshot or side effect.
# Anchor strings: repo-relative module, enclosing function, ordinal within that function.
KEPT_RECORD_WRITE_SITES: tuple[str, ...] = (
    # ── The structurally unretireable survivor ──
    #
    # `object_grant_auto` is HISTORICAL: its writer (the creator grant on create) went with the switch to
    # role bindings (`app/access.py`), so no site writes it now; old `activity_events` rows still carry it.
    #
    # `revoke_orphaned_project_roles`: a cascade side effect writing one row per cleared role, with an
    # email the request never names.
    "apps/api/app/access_admin.py::revoke_orphaned_project_roles#1",
    # ── Additional survivors. Each names the field nothing else carries. ──
    #
    # `credential_tested`: fires only when a stored secret was filled and sent, which no request or
    # response field says; written before the outbound call so a hung request leaves a trace.
    "apps/api/app/routes/credentials/connection_test.py::_audit_test#1",
    # `_audit`: one site serving create/update/delete credential; the delete is a 204 whose name is
    # nowhere in the call, and `/api/credentials` bodies are suppressed from capture anyway.
    "apps/api/app/routes/credentials/credential_wire.py::_audit#1",
    # `org_plan_rollover_emailed`: a cron's write (no request at all) recording how many Owners and Admins the
    # rollover email reached (PRD A3.2) — a count only the send knows.
    "apps/api/app/plan_rollover.py::_email#1",
    # `billing.budget_alert_emailed`: the `budget-alerts` cron's write (no request) recording how many Owners and
    # Admins a budget's 80% or 100% notice reached — a count only the send knows.
    "apps/api/app/budgets.py::_notify#1",
    # `cron_update`: `data["changed"]` is diffed against the prior row (`_validate` strips fields), not
    # the request's field set.
    "apps/api/app/routes/crons.py::write_cron_update#1",
    # `cron_delete`: 204; `Deleted cron {name}`.
    "apps/api/app/routes/crons.py::write_cron_delete#1",
    # `trigger_update`: `data["changed"]` is diffed against the prior row, as `cron_update`'s is.
    "apps/api/app/routes/triggers.py::write_trigger_update#1",
    # `trigger_delete`: 204; `Deleted trigger {name}`.
    "apps/api/app/routes/triggers.py::write_trigger_delete#1",
    # `cron_delete`/`trigger_delete` from an automation's When-kind change: the retired child's name is in no
    # request field (the PATCH names only the new When).
    "apps/api/app/automation_service.py::_retire#1",
    # `automation_update` on a file removed from an automation's Task action: 204; the automation's name is
    # nowhere in the call (ids on the path).
    "apps/api/app/routes/automations.py::delete_automation_attachment#1",
    # `trigger_create` by a runner: the capture middleware skips `/api/agent/`, so no other row records it.
    "apps/api/app/routes/agent_triggers.py::create_agent_trigger#1",
    # `trigger_dedup_clear`: 204; the trigger's `name` is nowhere in the call (ids on the path).
    "apps/api/app/routes/triggers.py::clear_trigger_dedup#1",
    # `group_update`: `name_from`/`name_to`; memberships resolve groups by name, so the old name matters.
    "apps/api/app/routes/groups.py::update_group#1",
    # `group_delete`: 204. Field: `name`.
    "apps/api/app/routes/groups.py::delete_group_row#1",
    # `group_member_add`/`group_member_remove`: the member's `email`, absent from the path (ids only);
    # the remove is a 204.
    "apps/api/app/routes/groups.py::add_member#1",
    "apps/api/app/routes/groups.py::remove_member#1",
    # `role_binding_add`/`role_binding_remove` (`app/access.py`, the one write home): the role, scope and
    # principal NAMES, which the ids on the wire cannot supply; the remove is a 204 whose binding is gone.
    "apps/api/app/access.py::_audit_add#1",
    "apps/api/app/access.py::remove_binding#1",
    # `project_updated`: `role_changes` and `jira_credential_change` carry pre-update `before` values,
    # and the credential pointer is redacted from captured bodies.
    "apps/api/app/routes/projects.py::apply_project_update#1",
    # `project_deleted`: 204. Field: `name`.
    "apps/api/app/routes/projects.py::delete_project#1",
    # `project_env_var_created`/`_updated`/`_deleted`: `redact` blanks the credential fields from both
    # captured halves, so `data["credential_name"]`/`["credential_field"]` exist only here; the delete is
    # also a 204 snapshotted before `session.delete`. These rows never carry a value (literal or
    # resolved; R10), since a `record_write` payload is not redacted. Payload shape:
    # `routes/project_env.py::_activity_data`.
    "apps/api/app/routes/project_env.py::create_env_var#1",
    "apps/api/app/routes/project_env.py::update_env_var#1",
    "apps/api/app/routes/project_env.py::delete_env_var#1",
    # `feature_deleted`: 204; the feature's `name` exists nowhere in the call.
    "apps/api/app/routes/features.py::delete_feature#1",
    # `project_erased`: 204; records `name`, the slots the cascade erased and the tasks deleted with the project
    # (`erase_project`, and `delete_project` for a project with slots).
    "apps/api/app/routes/projects.py::_erase_slots_then_delete#1",
    # `memory_unit_purged`: erasure evidence — the unit's id, `mem-N` ref and the prompts scrubbed, filed
    # in the project's org (an operator's purge too). Evidence must not ride the best-effort capture: this row commits
    # with the purge or rolls it back.
    "apps/api/app/routes/project_memory.py::purge_memory_unit#1",
    # `project_created`: the operator seeding a tenant org — filed IN that org (`in_org`); the capture
    # row is the operator's own org's, so the tenant's trail would otherwise never show who made the project.
    "apps/api/app/routes/projects.py::_create_project_in#1",
    # Access-model C6. `org_default_role` (#2): the `from` role, read off the row before the PATCH.
    # `org_domain_add`/`org_domain_remove`: the org's `name` (ids on the wire), the remove a 204.
    # `user_domain_join`: a user the system created on a Google sign-in — the domain and the new id are the
    # server's, and the request carries only a credential.
    # `org_enabled`/`org_disabled` (#1): a milestone on the org's Plan tab, written only when the value changed.
    # `org_readmitted` (#2): that a rejected org was re-admitted — the request only says `enabled: true`.
    # `org_rejected`: the org's `name`/`slug` (an id on the path), the reject's stripped reason and signed-out
    # member count (AC3.5: every decision audited with its actor and time). `org_approved`/`org_auto_approved`
    # are `app/org_approval.py`'s, below.
    "apps/api/app/routes/orgs.py::update_org#1",
    "apps/api/app/routes/orgs.py::update_org#2",
    "apps/api/app/routes/orgs.py::update_org#3",
    # `org_slug_renamed`: both slugs and how many tailnet devices the retag will move — the request carries
    # only the new one, and the old is what a by-hand `headscale nodes tag` needs (`app/durable/org_retag.py`).
    "apps/api/app/routes/orgs.py::_rename_slug#1",
    # `org_tailnet_retagged`: the org's slug and how many devices the converge will retag — the request has no body
    # (`app/durable/org_retag.py::enqueue_converge`).
    "apps/api/app/routes/orgs.py::retag_org_tailnet#1",
    "apps/api/app/routes/orgs.py::reject_org#1",
    "apps/api/app/routes/orgs.py::add_org_domain#1",
    "apps/api/app/routes/orgs.py::remove_org_domain#1",
    # `org_settings_changed`: the org's `name` (an id on the path) and the keys and values sent, `null` for a
    # cleared override (`routes/orgs.py::put_org_settings`); no setting holds a secret (none is a credential).
    "apps/api/app/routes/orgs.py::put_org_settings#1",
    "apps/api/app/routes/auth.py::_join_by_domain#1",
    # The self-service sign-up (`app/signup.py`): the same domain join from any of its three providers,
    # and `org_signup`, the org and owner the system created — neither id is on the wire.
    "apps/api/app/signup.py::join_by_domain#1",
    # `domain_join_skipped`: a Google sign-in whose email matched a registered domain while joins are paused
    # (`signup_domain_join` off) — the matched org and its domain are the server's, and the request is refused.
    "apps/api/app/signup.py::record_domain_join_skipped#1",
    # `org_approved`/`org_auto_approved`: one approval sequence for the operator's route and the sign-up's
    # automatic approval — the org's `name`/`slug` (an id on the path, or none at all at sign-up) and, for the
    # automatic one, the listed domain the owner's email matched, which no request names.
    "apps/api/app/org_approval.py::_commit_approval#1",
    "apps/api/app/signup.py::record_submission#1",
    # `org_created`: the customer History's first line (`app/customer_history.py`) — the new org's id, which the
    # operator's create returns but the self-create's caller never names, and its actor.
    "apps/api/app/routes/orgs.py::create_org#1",
    "apps/api/app/signup.py::_create_signup#1",
    # A staging org's `org_created`: its parent and its creator, which the new org's own history
    # carries and no request names.
    "apps/api/app/staging_org.py::create#1",
    # `staging_admins_synced`: what a parent's admin change added to and removed from its staging org,
    # written in the staging org by the system after the parent's write committed — no request names either org.
    "apps/api/app/staging_org.py::sync_admins#1",
    # `signup_email_verified`: an email applicant proved their address (org zero, the system actor) — the
    # `email_signups` row and its address are the server's; the request carries only a code or a token.
    # An operator's verify (`#1`) and the applicant's own (`#2`).
    "apps/api/app/email_signup.py::_verified#1",
    "apps/api/app/email_signup.py::_verified#2",
    # The operator's sign-up actions: `signup_email_deleted`, a draft's create, edit and submit — the
    # `email_signups` row, its address and the draft org are the server's; the request names them by id only.
    "apps/api/app/email_signup.py::delete_by_operator#1",
    "apps/api/app/signup_draft.py::create_by_operator#1",
    "apps/api/app/signup_draft.py::edit_by_operator#1",
    "apps/api/app/signup_draft.py::submit_by_operator#1",
    # `signup_code_resent`: an operator mailed a sign-up's code again — the address is the
    # `email_signups` row's, which the request names by id only.
    "apps/api/app/routes/signup_backlog.py::resend_signup_email#1",
    # `org_welcome_sent`: one row per welcome email attempt (`app/org_welcome.py`), the org page's full send
    # history — the outcome is the mail transport's, which no request or response field records.
    "apps/api/app/org_welcome.py::send#1",
    # `nodelink_share_consent`: the caller's confirmation that their model provider allows one
    # subscription on several devices, with the NodeLink it was given for — a legal record that must not be the
    # capture's best-effort row, and committed with the NodeLink's attachments.
    "apps/api/app/routes/nodelinks.py::share_subscriptions#1",
    # `trust_request_decided`: an operator approved or declined a trust document request (`app/trust_requests.py`)
    # — the requester, the documents and the NDA version accepted are the row's, which the request never names.
    "apps/api/app/trust_requests.py::decide#1",
    # `prospect_data_erased`: how many of one prospect's rows an erase deleted, committed with the delete — the capture
    # keeps no body on that path (the email is the erased data), so no other row says what went.
    "apps/api/app/routes/prospect_data.py::erase_prospect_data#1",
    # `membership_added`: the user's `email`, absent from path, body and `MembershipOut`.
    # `git_connected`: `/api/git-connect` and `/api/auth/` are excluded from both capture halves, so
    # this is the only record of a Connect GitHub or a GitHub sign-in keeping its grant (`connect_github`, their
    # one home); `login` and `credential_id` are not derivable.
    "apps/api/app/git_connections.py::connect_github#1",
    # The same for the other forges' Connect (`routes/forge_connect.py`), under the same excluded prefix.
    "apps/api/app/routes/forge_connect.py::forge_connect_callback#1",
    # `slack_connected`: Add to Slack's callback (`routes/slack_connect.py`) is a GET under the excluded
    # `/api/slack-connect` prefix, so this row is the only record of an org installing its workspace; the team is
    # Slack's answer to the exchange, not derivable from the request.
    "apps/api/app/routes/slack_connect.py::slack_connect_callback#1",
    # `gmail_connected`: Connect Gmail's callback (`routes/org_email.py`) is a GET under the excluded `/api/email-connect`
    # prefix, so this row is the only record of an org connecting a mailbox by OAuth; the address is Google's answer.
    "apps/api/app/routes/org_email.py::gmail_connect_callback#1",
    # `tenant_email_sent` / `tenant_email_failed`: a tenant's Gmail send (`app/tenant_gmail/`), reached by an agent or an
    # automation with no request of its own; the recipients, outcome and Gmail's id are nowhere else.
    "apps/api/app/tenant_gmail/__init__.py::_settle#1",
    # `sign_in`: a GitHub/Microsoft console sign-in is a GET redirect whose query is excluded
    # from capture (`/api/auth/`), so this row is the only record; the provider outcome is not derivable.
    "apps/api/app/login_oauth.py::record_sign_in#1",
    # The same for a SAML/OIDC sign-in (`app/sso_signin.py`), under the same excluded prefix; and the
    # member a JIT sign-in created, on a door with no session (no capture row at all).
    "apps/api/app/sso_signin.py::record_sign_in#1",
    "apps/api/app/sso_signin.py::resolve_member#1",
    "apps/api/app/routes/users.py::add_membership#1",
    # `membership_removed`: 204; `data["group"]` is the group name read off the deleted row, which the
    # path params (ids) cannot supply.
    "apps/api/app/routes/users.py::remove_membership#1",
    # `user_deleted`: 204. Field: `email`.
    "apps/api/app/routes/users.py::delete_member#1",
    # `org_deleted` (soft: nothing purged): a 204 — the org's `slug` and how many members were signed out are in no
    # request or response field.
    "apps/api/app/routes/orgs.py::_mark_deleted#1",
    # `org_restored`: the org's `slug`, absent from the path (an id); the row the history feed shows.
    "apps/api/app/routes/orgs.py::restore_org#1",
    # `org_wound_down`: what a delete ended (tasks failed, hostings ended, nodes deleted), committed on its own so
    # a refused purge still leaves it.
    "apps/api/app/routes/orgs.py::_record_wind_down#1",
    # `org_deleted` (the purge): a pre-delete snapshot (`rows_deleted` per table, the org's `slug`); the purge
    # destroys the tenant's own history, so this is the only surviving description. It escapes the purge
    # by being filed in a surviving org after the purge (`routes/orgs.py::_purge`).
    "apps/api/app/routes/orgs.py::_purge#1",
    # `org_compliance_delete_refused` (#1): a refused 2FA step-up on the purge (wrong or throttled) — the outcome
    # is the server's, and the route's capture row of a 403 says nothing of why.
    "apps/api/app/routes/orgs.py::compliance_delete_org#1",
    # `org_compliance_deleted` (#2): who authorized the purge (2FA), when, and the offboarding's completion record
    # (counts and residue per store) — none of it in a request or response field the capture keeps, and the
    # tenant's own rows are gone. Stamped with the actor's org, like `org_deleted`.
    "apps/api/app/routes/orgs.py::compliance_delete_org#2",
    # `org_delete_refused`: a refused 2FA step-up on the Owner's own delete (wrong or throttled) — the outcome is the
    # server's, and the route's capture row of a 403 says nothing of why.
    "apps/api/app/routes/orgs.py::delete_own_org#1",
    # `superadmin_granted`/`superadmin_revoked`: written only when the flag changed, which needs the
    # before value; the two opposite kinds would collapse into one handler-name kind.
    "apps/api/app/routes/users.py::set_superadmin#1",
    # `user_updated`: the second row of that request. Field: `email_from`.
    "apps/api/app/routes/users.py::set_superadmin#2",
    # `sign_in_override_changed` (G-01): an operator opening or shutting this account's password door
    # or second factor — the before values and whether its sessions ended are in no request or response field.
    "apps/api/app/routes/users.py::set_superadmin#3",
    # `user_disabled`/`user_enabled` (G-02): the access change's SOC 2 evidence, which must commit with the
    # change rather than ride the best-effort capture row; an idempotent repeat writes neither.
    "apps/api/app/routes/users.py::disable_member#1",
    # `org_transfer` (AC13): an operator's move of an entity to another org, one row in EACH org it
    # touched sharing `transfer_id` — the manifest's counts, and the org the second row is filed in, are in no
    # request field; the route's own capture is excluded (`WRITE_EXCLUDED_PREFIXES`).
    "apps/api/app/tenancy/transfer.py::_audit#1",
    # `org_transfer_refused`: a move that did not happen — its reason and the target org are the
    # engine's, and the move's own transaction (with the capture row) rolled back.
    "apps/api/app/tenancy/transfer.py::record_refused#1",
    # `node_transfer_fence_lifted`: the operator let a node an org transfer fenced take work before it
    # confirmed the old org's cleanup — that a fence stood, and the node's org the row is filed in, are in no
    # request field (a no-op lift writes none).
    "apps/api/app/routes/nodes/update.py::lift_node_transfer_fence#1",
    "apps/api/app/routes/users.py::enable_member#1",
    # SCIM provisioning (`app/scim.py`): its doors carry no session, so the capture middleware writes no
    # row; these are the only record of a member made, a profile changed, a group made or renamed by the IdP.
    "apps/api/app/scim.py::create_user#1",
    "apps/api/app/scim.py::create_user#2",
    "apps/api/app/scim.py::apply_user#1",
    "apps/api/app/scim.py::_rename#1",
    "apps/api/app/scim.py::create_group#1",
    # `login_failed`: a refused sign-in is a 403 the handler raises, so the capture row (if any) rolls back
    # with it; this committed row is the only record, and carries the source IP and refusal reason.
    "apps/api/app/login_funnel.py::record_login_failure#1",
    # `login_locked`: the failure just recorded engaged the lockout; derivable from no request field (it is
    # a count over earlier refusals), and rides the same committed write as the `login_failed` row.
    "apps/api/app/login_funnel.py::_record_lock#1",
    # `password_reset_requested`/`password_reset_completed` (R-X4): the forgot and reset doors answer no
    # principal, so the capture writes no row; these carry the account, its org and the source address.
    "apps/api/app/password_reset.py::request_reset#1",
    "apps/api/app/password_reset.py::complete_reset#1",
    # The community edition's on-machine reset (`python -m app.ce_admin reset-password`): no request at all.
    "apps/api/app/ce_admin.py::reset_password#1",
    # `mfa_reset` (G-01): the access change's evidence, committed with it; `mfa_enrolled` and
    # `mfa_recovery_used` ride sign-in routes under `/api/auth/`, which capture excludes, so these rows are
    # the only record. None carries a secret or a code.
    "apps/api/app/routes/users.py::reset_user_mfa#1",
    # `sso_link_reset`: the cleared SAML/OIDC link's protocol and issuer, which no request carries.
    "apps/api/app/sso_signin.py::reset_link#1",
    "apps/api/app/routes/mfa.py::mfa_enrol_confirm#1",
    "apps/api/app/routes/mfa.py::mfa_verify#1",
    "apps/api/app/routes/mfa.py::regenerate_recovery_codes#1",
    # `terms_accepted` (PRD D7): the Terms version an operator-submitted owner accepted at first sign-in. A 204 under
    # `/api/auth/`, which capture excludes: this row is the only audit of the acceptance.
    "apps/api/app/routes/legal_acceptance.py::accept_terms#1",
    # The factor traces of an org switch. `/api/auth/switch` is fenced out of capture (no audit for a switch,
    # PRD §5), so each is the only record of how the factor was met: `#1` `mfa_skipped`, an org
    # that demands the factor entered with no code on the person's trusted browser, naming its label; `#2`
    # `mfa_recovery_used`, a recovery code spent to enter it, naming no code.
    "apps/api/app/login_funnel.py::switch_membership#1",
    "apps/api/app/login_funnel.py::switch_membership#2",
    # `mfa_device_trusted` / `mfa_device_revoked`: a browser that will skip the code, named by the
    # User-Agent label the server derived (the trust rides a sign-in route capture excludes; a revoke's
    # 204 carries no body, and revoke-all names no device at all).
    "apps/api/app/routes/mfa.py::_trust_device#1",
    "apps/api/app/routes/mfa.py::_forget#1",
    # `mfa_skipped`: a session minted with no code (a trusted device or an org skip) — the stand-in and the
    # device label, in no request field; the only trace that this sign-in never saw a second factor.
    "apps/api/app/login_funnel.py::_mint_without_code#1",
    # `sso_config_changed` (G-01): an org's SSO-only setting and MFA attestation, with the before state the
    # PATCH body does not carry — the SOC 2 evidence of who changed which doors an org's members may use.
    "apps/api/app/routes/orgs.py::_apply_sso#1",
    # `sso_connection_changed`: an org's SAML/OIDC identity provider saved or removed, or a domain removed,
    # with the before state no request carries — whether Require SSO was on, that an IdP change switched it off, the
    # removed domain's name (a 204 path carries only its id).
    "apps/api/app/org_sso.py::_record#1",
    # Jaah API keys: bodies under `/api/api-keys` are never captured (the secret rides them), so each write
    # records itself — the created key's display id and settings, an edit's changed fields, a rotation's overlap, a
    # revoke's owner. `record_refusal`: a refused key, which no request names (a 401 has no actor). `revoke_for_owner`
    # and `_forget` (behind `forget_owner`): a member's disable or delete ending each of their keys, the name and
    # display id in no request.
    "apps/api/app/routes/api_keys.py::create_api_key#1",
    "apps/api/app/routes/api_keys.py::edit_api_key#1",
    "apps/api/app/routes/api_keys.py::rotate_api_key#1",
    "apps/api/app/routes/api_keys.py::revoke_api_key#1",
    "apps/api/app/api_keys.py::record_refusal#1",
    "apps/api/app/api_keys.py::revoke_for_owner#1",
    "apps/api/app/api_keys.py::_forget#1",
    # `api_key_expired`: the hourly sweep's record of a key that ran out, which no request makes.
    "apps/api/app/api_keys.py::_record_expiry#1",
    # `mfa_skip_changed`: an operator exempting an org from the second factor, with how many
    # members turning it back on signed out — a count the PATCH body does not carry.
    "apps/api/app/routes/orgs.py::_apply_mfa_skip#1",
    # `hipaa_changed`: an operator turning an org's HIPAA mode on or off or moving its BAA date,
    # with the BEFORE state the PATCH body does not carry. `hipaa_warning_acknowledged`: an org admin accepting a
    # non-BAA surface; the vendor it names is `app/hipaa.py`'s, in no request field.
    "apps/api/app/routes/orgs.py::_apply_hipaa#1",
    "apps/api/app/routes/orgs.py::acknowledge_hipaa_warning#1",
    # `compliance_manifests_erased`: how many signed manifest versions of which tasks a task or org
    # delete erased through their seven-year retention — read from the index rows the delete then removes.
    "apps/api/app/durable/compliance_seal.py::erase_for_tasks#1",
    "apps/api/app/durable/compliance_seal.py::erase_for_org#1",
    # `billing.plan_edit`: a plan's before and after — its caps, flags and the Stripe Price id and
    # amount Stripe created, none of which the request body carries — so who changed what a plan grants and
    # charges stays answerable.
    "apps/api/app/routes/billing_plans.py::create_plan#1",
    "apps/api/app/routes/billing_plans.py::update_plan#1",
    "apps/api/app/routes/billing_plans.py::retire_plan#1",
    # A 204 deleting the plan: its pre-delete snapshot and the Stripe Prices it archived.
    "apps/api/app/routes/billing_plans.py::delete_plan#1",
    # `billing.rental_price_edit`: a hosting price's before and after and the Stripe Price it was
    # given — no request field carries the Price, and a PATCH body names only what changed.
    "apps/api/app/routes/billing_hosting_prices.py::create_hosting_price#1",
    "apps/api/app/routes/billing_hosting_prices.py::update_hosting_price#1",
    # `instance_type.disallow`: a 204 whose request names only the type; what still used it at that
    # moment (products, node groups, live hostings) is a pre-delete snapshot nothing else records.
    "apps/api/app/routes/instance_types.py::disallow_instance_type#1",
    # Billing (the org's billing routes). Each names what the request cannot: a client-supplied
    # `pm_…` id is opaque, so the card it named (brand and last four, read off our mirror) is only here.
    # `billing.customer.create`: the Stripe customer id made on the org's first billing action.
    "apps/api/app/billing/customers.py::ensure_customer#1",
    # `billing.details.update`: the Stripe customer the details landed on; the body names no customer.
    "apps/api/app/routes/org_billing.py::put_details#1",
    # `billing.setup_intent.create`: the SetupIntent id — the response's only field is its client secret,
    # which the capture redacts and must never keep.
    "apps/api/app/billing/payment_methods.py::start_setup#1",
    # `billing.payment_method.default` / `.update`: the card (brand •••• last4) the opaque id named.
    "apps/api/app/routes/org_billing.py::set_default#1",
    "apps/api/app/routes/org_billing.py::update_payment_method#1",
    # `billing.payment_method.delete`: the card deleted and the one promoted to default, which neither the
    # path nor the request carries.
    "apps/api/app/routes/org_billing.py::delete_payment_method#1",
    # `billing.sync`: what the re-read from Stripe touched (counts), in no request or response field.
    "apps/api/app/routes/org_billing.py::sync#1",
    # `billing.subscribe` / `.change` / `.cancel` / `.undo` / `.checkout.abandon` / `.invoice.pay` /
    # `.subscribe.declined` (C4, `app/billing/subscriptions.py`, one helper): the Stripe subscription or
    # invoice id, the outcome (paid / 3-D Secure / declined, with Stripe's reason) and the amount charged —
    # none of which the request (a plan key at most) or the response carries.
    "apps/api/app/billing/subscriptions.py::_audit#1",
    # `billing.rental.cancel` / `.undo_cancel` / `.resume` / `.delete` (the hostings module's
    # one helper): which hosting moved its bill, and for a Resume or a Delete the money it started or credited —
    # the action's request carries a hosting id only, and nothing else records who changed the tenant's bill.
    "apps/api/app/billing/hostings.py::_audit#1",
    # Jaah token billing (`app/token_billing/settings.py`), one helper for every write:
    # `jaah_ai.mode` / `.floor_micros` / `.markup_bps` / `.low_threshold_micros` /
    # `.auto_recharge` carry the BEFORE value no request holds (`{from, to}`), and `jaah_ai.credit` /
    # `.debit` the ledger entry id the response does not name — so who moved a tenant's money stays answerable.
    "apps/api/app/token_billing/settings.py::audit#1",
    # The operator's desk (C7, `app/billing/operator.py`, AC-7.3): `billing.refund` / `.refund.partial` — the
    # credit note (or the Refunds made before it failed) Stripe created; `billing.charge` — the invoice item
    # and the invoice raised for it; `billing.override` (custom price) — the per-org Price Stripe created;
    # `billing.invoice.<action>` — the invoice's number, amount and the status Stripe left it in.
    # None of those ids is in the request, and the response is the desk, not the object.
    # NodeVault: `nodevault.key_delivered` is written by a key-delivery WORKFLOW on the
    # node's acknowledgement, with no request at all (AC-9.1); `nodevault.identity_reset` carries the identity
    # fingerprint the reset ERASED, which neither the request (a node id) nor the response names.
    "apps/api/app/durable/nodevault.py::_audit_delivered#1",
    "apps/api/app/routes/nodevault.py::reset_node_identity#1",
    # NodeVault setup and migration (AC-9.1): `_audit` — setup started, the key generated or uploaded
    # and its backup confirmed (the key HASH the CP recomputed; the request body is suppressed on this surface),
    # and an abandon; `_audit_operation` — a credential migrated (KMS → key hash) and setup/migration completed
    # with their counts, written by a WORKFLOW with no request at all.
    "apps/api/app/routes/nodevault.py::_audit#1",
    "apps/api/app/durable/nodevault.py::_audit_operation#1",
    "apps/api/app/billing/operator.py::_audit_refund#1",
    "apps/api/app/billing/operator.py::charge#1",
    "apps/api/app/billing/operator.py::_org_price#1",
    "apps/api/app/billing/operator.py::invoice_action#1",
    # The operator's products without billing (`app/routes/customers.py`): `billing.comp.add|end|resize|
    # remove` and `billing.included.remove` carry the BEFORE and AFTER product (§3.4) and the comp a request
    # creates, which no request holds.
    "apps/api/app/routes/customers.py::_audit#1",
    # The customer's Billing tab (`app/routes/billing_orgs.py`): `billing.details.update`,
    # `.payment_method.default|delete`, `.payment_link.send` and `.balance_credit` carry the BEFORE and AFTER
    # (§3.4) and the Stripe objects the write touched or made — the request names a dialog id at most.
    "apps/api/app/routes/billing_orgs.py::_record#1",
    # The org's plan (`app/org_plan_admin.py`, `app/plan_terms.py`): `org_plan_changed` and
    # `org_plan_override_changed` carry the BEFORE value (`{from, to}`) no request holds; `org_plan_override_cleared`
    # is each override a plan move cleared, which the request never names; `org_plan_synced` is Stripe's move
    # (written by `assign`), with no request at all; `org_plan_push_failed` is Stripe's refusal of the durable push, after the request
    # returned.
    "apps/api/app/plan_terms.py::assign#1",
    "apps/api/app/plan_terms.py::_record_cleared#1",
    "apps/api/app/org_plan_admin.py::_record_override#1",
    "apps/api/app/org_plan_admin.py::_push_refused#1",
    # `org_plan_sync_held`: Stripe entitled a new plan while the org is on one an operator assigned, so it
    # stays put — both plans named, written by the inbound sync with no request at all.
    "apps/api/app/org_plan_admin.py::_follow_plan#1",
    # `org_plan_chosen` for a PRICED choice at approval (`app/signup_plans.py`): the org stays on the
    # Default plan, so no `assign` row records it; the plan applied for is the application's, not the request's.
    "apps/api/app/signup_plans.py::apply_choice#1",
    # `nodelink_absorbed`: an operator's "Use existing" save moved another org's NodeLinks onto
    # Jaah-managed storage and deleted that org's store rows — one row in EACH absorbed org, naming the stores it
    # lost and the NodeLinks that moved, which the request (one file system id) never names.
    "apps/api/app/routes/nodelink_locations.py::_audit_absorbed#1",
    # `nodelink_location_credential`: an operator's re-save moved a location's store onto another AWS
    # credential — the credential it LEFT, which the request (the new one) never names.
    "apps/api/app/routes/nodelink_locations.py::put_location#1",
    # `node_order:launch_failed`: a Your Cloud launch failed after its commit and the compensation
    # deleted the node row — the cloud's refusal and the removed node's id, which no request or response carries.
    "apps/api/app/node_orders.py::_record_launch_failure#1",
    # `node:removed_unjoined`: the control plane deleted a node row with no request — a launch's
    # compensation or a never-joined expiry — naming the node and why, which nothing else keeps once the row is gone.
    "apps/api/app/node_orders.py::record_machine_removed#1",
)


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
    "credential_nodevault_toggled",
    "create_credential", "update_credential", "delete_credential", "git_connected",
    # An org's Slack workspace (`routes/slack_connect.py`): installing it routes the org's alerts there.
    "slack_connected", "disconnect_slack_workspace",
    # An org's own Gmail (`app/tenant_gmail/`): a connection is a standing credential to send as the mailbox.
    "gmail_connected", "connect_gmail_service_account", "delete_gmail_connection",
    # Its hosted trigger addresses (`app/tenant_gmail/addresses.py`): each one opens a way for mail into its triggers.
    "create_tenant_address", "revoke_tenant_address",
    # Node tokens: issued on create, re-issued on rotation, revoked with the node.
    "create_node", "reissue_node_token", "delete_node",
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
