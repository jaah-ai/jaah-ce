"""The account login and retirement verbs as durable workflows.

The Add-Account / Re-login wizard asks a node to start the provider's login, surfaces the authorize
URL, then waits for the operator. The provider's declared `login_completion` (`_declared_completion`)
decides how: in paste mode the pasted code arrives via `recv`; in `await_credentials` mode the CLI
completes on disk and `_await_redirect` polls `*_await_login`. The node half (login process, FIFO, pid
check, URL scrape) stays in `apps/jaahd/accounts.py`, invisible to DBOS, so `login_process_gone`
remains.

Sealed handoff: the wizard sends a per-login public key (`peer_key`), the node answers with
the authorize URL sealed to it (`sealed_url`) and its own per-login public key (`seal_key`), and the
operator's code comes back sealed to the node (`sealed_code`). This control plane stores and relays
ciphertext only; it can read neither the URL nor the code. A node that answers in the clear to a sealed
request is an old daemon: the `dispatch` step drops its `oauth_url` before the reply is recorded
(`_keep_plain_url_off_the_record`) and the login fails (`login_seal_unsupported`), never continues
unsealed.

Steps reuse `commands.py::_dispatch` and `commands.py::_ensure_account_row`. The adapters at
the bottom keep `POST /api/commands` and `GET /api/accounts/provisioning` in their existing shapes.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from collections.abc import Iterable
from typing import Any

from dbos import DBOS, SetWorkflowID
from dbos._error import DBOSMaxStepRetriesExceeded
from fastapi import HTTPException
from sqlalchemy import text

from app import agent_providers
from app.db import SessionLocal
from app.entity_shape import optional_uuid
from app.login_seal_wire import is_wire_key, is_wire_sealed
from app.tenancy import exclusion_log, seam

log = logging.getLogger(__name__)

CODE_TOPIC = "code"
AUTH_EVENT = "auth"

# How long the operator has to authorize and paste the code back; kept at the node's 900s process cap
# so the workflow never outlives the login.
CODE_TIMEOUT_SECONDS = 900.0

# Gap between two `*_await_login` polls in `await_credentials` mode; the node's verb itself blocks for
# its poll window. A durable `DBOS.sleep`, so a restart resumes rather than re-dispatches.
REDIRECT_POLL_INTERVAL_SECONDS = 2.0

# How long phase 1 waits for the workflow to publish the URL. It must cover the node's whole phase-1
# budget, run serially: `apps/jaahd/accounts.py::_LOGIN_PID_WAIT` (10s), `_OAUTH_SCRAPE_TIMEOUT` (30s)
# and `_USER_CODE_SCRAPE_TIMEOUT` (5s, consumed in full when no code arrives) = 45s, plus dispatch
# headroom. Too short and a healthy login reports `oauth_url_scrape_failed` while `in_flight` refuses
# the retry until `CODE_TIMEOUT_SECONDS`. The packages cannot cross-import, so `tests/test_commands_login_workflow.py`
# restates the values and asserts this comment names each.
AUTH_URL_WAIT_SECONDS = 60.0

# How long a phase-2 request waits for the exchange; the wizard's POST is synchronous, the workflow is
# not, so the adapter polls to this bound.
EXCHANGE_WAIT_SECONDS = 120.0

# DBOS statuses that mean "still going"; `DELAYED` is not terminal.
LIVE_STATUSES = ("PENDING", "ENQUEUED", "DELAYED")

LOGIN_WORKFLOW_NAME = "authenticate_account"

# How many login workflows the wizard's poll renders, newest first; `workflow_status` keeps a row per
# login ever run, so the bound must be stated.
RECORD_LIMIT = 100

_serving_loop: asyncio.AbstractEventLoop | None = None


def set_serving_loop(loop: asyncio.AbstractEventLoop) -> None:
    """Record the loop the node bus lives on. Called once, from `runtime._CaptureServingLoop`."""
    global _serving_loop
    _serving_loop = loop


def login_workflow_id(node_id: str, slug: str, started_at_ms: int) -> str:
    """This login attempt's durable identity — unique per attempt, deliberately.

    A per-`(node_id, slug)` id would make a re-login attach to the finished run and serve its stale
    authorize URL. Mutual exclusion is `_live_login`'s query plus the 409 in `enqueue_command`, not the
    id. The timestamp is passed in so this stays pure. Never parsed back: slugs and node ids may contain
    dashes, so readers use the workflow's recorded inputs.
    """
    return f"{login_workflow_prefix(node_id)}{slug}-{started_at_ms}"


def login_workflow_prefix(node_id: str) -> str:
    """Every `login_workflow_id` on this node starts with this, so a read can be scoped to some nodes' logins."""
    return f"acct-login-{node_id}-"


def _login_args(workflow: Any) -> tuple[str, str] | None:
    """`(slug, node_id)` out of a login workflow's recorded inputs, or None if unreadable."""
    args = (getattr(workflow, "input", None) or {}).get("args") or ()
    if len(args) < 2:
        return None
    return str(args[0]), str(args[1])


def _live_login(node_id: str, slug: str) -> Any:
    """The one login workflow still running for this pair, or None.

    Mutual exclusion and phase-2 routing rest on this. A query because attempts have distinct ids;
    bounded by `LIVE_STATUSES`, so it scans open logins only.
    """
    for workflow in DBOS.list_workflows(
        name=LOGIN_WORKFLOW_NAME, status=list(LIVE_STATUSES), limit=RECORD_LIMIT, sort_desc=True
    ):
        parsed = _login_args(workflow)
        if parsed == (slug, node_id):
            return workflow
    return None


# --------------------------------------------------------------------------- steps

# The two fields of a node reply kept out of the step record: a step's output is serialized into
# `operation_outputs` forever. The device-auth `user_code` (RFC 8628, ~15 min) is held in this process
# only, keyed by workflow id (cf. `routes/commands.py::_without_user_code`); losing it on restart is
# fine: `set_event` is recorded, so a replay keeps the published code. Drained by its reader
# (`_reported_user_code` pops). A plaintext `oauth_url` answered to a sealed request is dropped outright
# (`_keep_plain_url_off_the_record`): nothing may hold it.
_unrecorded_user_codes: dict[str, str] = {}
# The marker `_keep_plain_url_off_the_record` leaves in the node's JSON stdout where `oauth_url` was.
_PLAIN_URL_REFUSED = "plain_url_refused"


def _current_workflow_id() -> str | None:
    """The workflow this thread is running inside, or None when there is not one.

    A function so tests can substitute it: monkeypatching the `DBOS.workflow_id` classproperty replaces
    the descriptor and breaks later readers."""
    return DBOS.workflow_id


def _keep_user_code_off_the_record(reply: dict[str, Any]) -> dict[str, Any]:
    """`reply` with the device-auth `user_code` lifted out of it into `_unrecorded_user_codes`.

    Borrows `routes/commands.py::_without_user_code` (lazy import, cycle) so both sinks share one
    definition of removal. The stashed value goes through `_user_code_from_reply`, so a malformed code
    stashes nothing (fail-closed). Absent stays absent (the reply is returned untouched, Claude's path is
    byte-identical) and `""` stays `""`."""
    from app.routes.commands import _user_code_from_reply, _without_user_code

    result = (reply or {}).get("result")
    if not isinstance(result, dict):
        return reply
    scrubbed = _without_user_code(result)
    if scrubbed is result:
        return reply
    workflow_id = _current_workflow_id()
    if not workflow_id:
        # Nothing to hand the code to, so nothing is taken away: stripping it here would strand the
        # login. Unreachable today (steps always run in a workflow); failing towards a finishable
        # login is deliberate.
        log.warning("durable login: a node reply carried a device code outside any workflow, so it was "
                    "left in place rather than dropped; nothing was recorded")
        return reply
    code = _user_code_from_reply(result)
    if code is not None:
        _unrecorded_user_codes[workflow_id] = code
    return {**reply, "result": scrubbed}


def _keep_plain_url_off_the_record(reply: dict[str, Any]) -> dict[str, Any]:
    """`reply` with a plaintext `oauth_url` taken out of its JSON stdout and `plain_url_refused: true`
    left in its place — the twin of `_keep_user_code_off_the_record` for a sealed request.

    Only an old daemon answers a `peer_key` in the clear; the workflow refuses it
    (`login_seal_unsupported`), but the step's return is recorded into `operation_outputs` first, so the
    URL is dropped HERE, not stashed: nothing may hold the one secret this CP must not. The marker is
    what `authenticate_account` keys the verdict off (`_plain_url_refused`), since the URL itself is
    gone. Absent stays absent (the reply is returned untouched)."""
    from app.routes.commands import _stdout_payload

    result = (reply or {}).get("result")
    if not isinstance(result, dict):
        return reply
    payload = _stdout_payload(result)
    if payload is None or "oauth_url" not in payload:
        return reply
    payload.pop("oauth_url")
    payload[_PLAIN_URL_REFUSED] = True
    return {**reply, "result": {**result, "stdout": json.dumps(payload)}}


class NodeDispatchRefused(HTTPException):
    """The transport `HTTPException` out of `commands._dispatch`, made to survive DBOS's pickle round trip.

    DBOS pickles the retry wrapper into `workflow_status.error`; a keyword-built `HTTPException` has
    empty `args` and fails to unpickle, so the raw base64 reached the wizard. Spelling the constructor
    arguments in `__reduce__` fixes it while staying an `HTTPException` for existing `except` arms.
    """

    def __reduce__(self):
        return (self.__class__, (self.status_code, self.detail, self.headers))


# A step so `retries_allowed` handles a briefly unreachable node, and so a replay re-reads the recorded
# answer rather than re-dispatching (no double-started login).
@DBOS.step(retries_allowed=True, max_attempts=3, interval_seconds=2.0)
def dispatch(node_id: str, command_type: str, args: dict[str, Any]) -> dict[str, Any]:
    """Issue one node command over the existing node bus and return its parsed reply.

    Hops onto the serving loop (`runtime._CaptureServingLoop` says why).
    """
    from app.node_identity import bus_key
    from app.routes.commands import COMMAND_TIMEOUT, _dispatch

    # Attributed for the whole step (`app/durable/runtime.py::step_session` says why).
    with exclusion_log.caller("durable:accounts.dispatch"):
        # The machine key is resolved on this thread before the hop, in its own session.
        # An identity read, hence `identity_scope()` not `bind_from`: `bus_key` asks which socket a wire
        # value addresses and falls back to the raw value for an unregistered node, which `bind_from`
        # would refuse. The fail-closed decision is `commands.py::_dispatch`'s. One call wide on purpose.
        with SessionLocal() as session:
            with seam.identity_scope():
                bus_target = bus_key(session, node_id)

        if _serving_loop is None:
            # Fail loudly: an `asyncio.run` fallback would hang instead.
            raise RuntimeError(
                "durable accounts: the serving loop was never captured, so the node bus is unreachable "
                "from a workflow step; app.durable.runtime.init must run before any workflow starts"
            )
        future = asyncio.run_coroutine_threadsafe(
            _dispatch(node_id, command_type, args, bus_target=bus_target), _serving_loop
        )
        # Bounded past the dispatch's own deadline, so a wedged bridge errors rather than holds a thread.
        try:
            reply = future.result(timeout=COMMAND_TIMEOUT + 10)
        except HTTPException as exc:
            # Re-typed so DBOS can read it back after pickling (`NodeDispatchRefused`).
            raise NodeDispatchRefused(exc.status_code, exc.detail, exc.headers) from exc
        # The device-auth code — and, to a sealed request, a plaintext URL — come out before the
        # return: what a step returns is recorded forever. Only the start verbs carry `peer_key`.
        if "peer_key" in args:
            reply = _keep_plain_url_off_the_record(reply)
        return _keep_user_code_off_the_record(reply)


@DBOS.step()
def ensure_row(
    slug: str, result: dict[str, Any], node_id: str, enabled: bool,
    creator_user_id: str | None = None, provider: str = agent_providers.DEFAULT_PROVIDER,
    born_shared: bool = False, personal: bool = False,
) -> None:
    """Write the roster row through the existing helper, in its own session.

    A step so a replay does not re-run the upsert; `_ensure_account_row` is idempotent and stamps the
    creator's ownership (`authz.stamp_owner_on_create`) only on a genuine insert. The creator arrives as an id, never a `Principal`:
    step arguments are replayed, and a stale permission snapshot must not be; `principal_for_user`
    rebuilds it. `provider` (which agent CLI) is defaulted so a step recorded by an older release replays
    as Claude; `personal` (the account is the creator's own, G2) is last and defaulted to the org's
    default for the same reason."""
    from app.authz.principal import principal_for_user
    from app.models.identity import AdminUser
    from app.models.nodes import Node
    from app.node_identity import node_id_or_none
    from app.routes.commands import _ensure_account_row

    # Attributed for `dispatch`'s reason.
    with exclusion_log.caller("durable:accounts.ensure_row"), SessionLocal() as session:
        # The account belongs to the node's org; this DBOS thread has nothing bound. The wire `node_id`
        # is resolved under `identity_scope()` (as in `dispatch`), then `bind_from` binds its org.
        with seam.identity_scope():
            resolved_node = node_id_or_none(session, node_id)
        if resolved_node is None:
            # Raise rather than proceed unbound. `_ensure_account_row` degrades an unresolvable node to
            # a NULL pin, but an account row with no attributable org is the persisted unbound write the
            # tenancy boundary forbids. The workflow ends `ERROR` and `_terminal_reason` carries this
            # message to the wizard. Reachable for a never-registered node (`dispatch` tolerates one).
            # Cost accepted: the node already minted a token, now with no roster row to deprovision
            # from; recording org-less credentials is a schema question. Do not build it here.
            log.warning(
                "durable login for slug %r cannot write its roster row: node %r resolves to no `nodes` "
                "row, so the account has no org to belong to. Refusing rather than writing an "
                "unattributable account; the credential this login minted is now live on the host with "
                "no control-plane record — clear it there, then re-register the node (or re-run the "
                "wizard against a registered one) and run the login again.", slug, node_id,
            )
            raise RuntimeError(
                f"durable accounts: node {node_id!r} is not registered, so the roster row for slug "
                f"{slug!r} has no org to be written under"
            )
        with seam.bind_from(session, Node, resolved_node):
            principal = None
            if creator_user_id:
                # Not a bare `session.get`: the id crossed JSON as text, and a malformed one should cost
                # the ownership only, never the roster row.
                creator_id = optional_uuid(creator_user_id)
                user = session.get(AdminUser, creator_id) if creator_id is not None else None
                if user is None:
                    log.warning(
                        "durable login for slug %r could not resolve its creator %r, so the new account "
                        "is the org's, not personal: only an Org Owner/Admin reaches it until one "
                        "shares it", slug, creator_user_id,
                    )
                else:
                    principal = principal_for_user(session, user)
            _ensure_account_row(
                session, slug, result, enabled=enabled, node_id=node_id, principal=principal,
                provider=provider,
                # The CP's own record of asking for a shared mint, from persisted inputs; defaulted for
                # replay.
                born_shared=born_shared,
                personal=personal,
            )


@DBOS.step()
def lift_auth_rejection(slug: str, node_id: str) -> None:
    """Clear the account's `auth_rejected` verdict after a successful login (`account_fold.clear_auth_rejection`).

    A step so a replay does not re-run it. Bound to the login node's org, as `ensure_row` is. Never fails the
    login: a write that does not land leaves the verdict to its TTL, which is logged."""
    from app.account_fold import clear_auth_rejection
    from app.models.nodes import Node
    from app.node_identity import node_id_or_none

    try:
        with exclusion_log.caller("durable:accounts.lift_auth_rejection"), SessionLocal() as session:
            with seam.identity_scope():
                resolved_node = node_id_or_none(session, node_id)
            if resolved_node is None:
                return
            with seam.bind_from(session, Node, resolved_node) as org_id:
                if org_id is None:
                    return
                clear_auth_rejection(session, slug=slug, org_id=org_id)
                session.commit()
    except Exception:  # noqa: BLE001 — the login itself succeeded; its verdict expires on its own
        log.warning("login of %r on node %r succeeded but its rejection could not be cleared; it lifts when "
                    "the verdict expires or the next probe reads the account healthy", slug, node_id,
                    exc_info=True)


@DBOS.step()
def drop_abandoned_row(slug: str, node_id: str) -> bool:
    """Remove the roster row an unfinished Add-Account sign-in left, with its credential dir; True when gone.

    Phase 1 writes a DISABLED row so the node's config dir is never stranded (`routes/commands.py`); a sign-in
    that then times out or fails left it in the list for good, "Not signed in on any node yet".
    Only a row that never held a credential is dropped: `signed_in_at` NULL (phase 1 writes it so, the first
    completed sign-in stamps it, every other path defaults it), still disabled and pinned to this login's
    node. So an account that ever worked — an administrator's disabled one, or one being enrolled onto
    another node — is never touched, while an abandoned restart's row goes like a first attempt's. Skipped
    while another login for this pair is live — the wizard's restart reuses the slug and the row — checked
    again under the row lock, just before anything is removed. Credentials first, row second, as
    `routes/accounts.py::deprovision_account` orders them: a node refusal keeps the row."""
    from sqlalchemy import select

    from app.events import emit_console_event
    from app.models.credentials import Account
    from app.models.nodes import Node
    from app.node_clone import resolve_auth_dir
    from app.node_identity import node_id_or_none

    def another_login_is_live() -> bool:
        live = _live_login(node_id, slug)
        return live is not None and live.workflow_id != _current_workflow_id()

    if another_login_is_live():
        return False
    with exclusion_log.caller("durable:accounts.drop_abandoned_row"), SessionLocal() as session:
        with seam.identity_scope():
            resolved_node = node_id_or_none(session, node_id)
        if resolved_node is None:
            return False
        with seam.bind_from(session, Node, resolved_node):
            row = session.scalar(select(Account).where(Account.slug == slug).with_for_update())
            if (row is None or row.signed_in_at is not None or row.enabled or row.node_id != resolved_node
                    or another_login_is_live()):
                return False
            config_dir = resolve_auth_dir(
                session.scalar(select(Node.jaah_home).where(Node.id == resolved_node)), slug)
            if not config_dir:
                log.warning("abandoned sign-in %r: node %r has no Jaah home, so its row is kept", slug, node_id)
                return False
            try:
                reply = dispatch(node_id, "account_delete", {"config_dir": config_dir})
            except Exception as exc:  # noqa: BLE001 — an unreachable node keeps the row; the sign-in verdict stands
                log.warning("abandoned sign-in %r: node %r unreachable (%s), so its row is kept", slug, node_id, exc)
                return False
            if str(reply.get("status")) != "done":
                log.warning(
                    "abandoned sign-in %r: node %r did not remove its credential dir (%s), so its row is kept",
                    slug, node_id, _failure_reason(reply) or "no reason")
                return False
            account_id = row.id
            session.delete(row)
            emit_console_event(session, entity="account", id=account_id, action="deleted")
            session.commit()
    log.info("abandoned sign-in %r on node %r: dropped its never-signed-in account row", slug, node_id)
    return True


def _declared_completion(provider: str) -> str:
    """`provider`'s declared login-completion mode (`app/agent_providers.py`, `login_completion`).

    Declared for every provider, Claude included: nothing infers a mode from the URL any more (the
    loopback/redirect path is retired: a login is sealed end to end), and inferring was wrong for device auth
    anyway (non-loopback yet completes on disk, which would hang to `CODE_TIMEOUT_SECONDS`). An unknown value
    (a newer build's persisted input) resolves to paste mode and logs. The rule is duplicated in
    `apps/web/src/pages/credentials/AccountEditPage.tsx::loginNeedsCode`; change both."""
    row = agent_providers.by_value(provider)
    if row is None:
        log.warning(
            "durable login carries provider %r, which this build's catalogue does not know; waiting for "
            "a pasted code, the only completion mode that always has a way to finish", provider,
        )
        return agent_providers.PASTE_CODE
    return row.login_completion


# --------------------------------------------------------------------------- workflows

def _await_redirect(
    node_id: str, start_verb: str, slug: str,
) -> tuple[dict[str, Any] | None, dict[str, Any]]:
    """Wait out an `await_credentials` login by re-asking the node, and answer
    `(failure_verdict, last_reply)`.

    Plain workflow-body code, so its `dispatch` calls are recorded steps and a replay re-reads them.
    `recv` would be wrong here: nothing sends on the topic in this mode. Bounded by
    `CODE_TIMEOUT_SECONDS`, wall-clock (a restart resets it; the node's 900s wrapper is authoritative).
    """
    deadline = time.monotonic() + CODE_TIMEOUT_SECONDS
    while True:
        reply = dispatch(node_id, f"{start_verb}_await_login", {"slug": slug})
        if str(reply.get("status")) != "done":
            return {"ok": False, "error": _failure_reason(reply) or "login_failed"}, reply
        status = str(_node_payload(reply).get("status") or "")
        if status == "done":
            return None, reply
        if status != "awaiting_redirect":
            # Exit 0 with neither state is unreadable; fail rather than loop to the deadline.
            return {"ok": False, "error": _failure_reason(reply) or status or "login_failed"}, reply
        if time.monotonic() >= deadline:
            return {"ok": False, "error": "login_timeout"}, reply
        DBOS.sleep(REDIRECT_POLL_INTERVAL_SECONDS)


@DBOS.workflow()
def authenticate_account(
    slug: str, node_id: str, reauth: bool, with_code: bool = True,
    creator_user_id: str | None = None, new_account: bool = True,
    provider: str = agent_providers.DEFAULT_PROVIDER, born_shared: bool = False,
    peer_key: str | None = None, personal: bool = False,
) -> dict[str, Any]:
    """Start a login, wait for it to complete, and finalize (`_authenticate`). Survives a control-plane
    restart. An Add-Account sign-in that ends without finishing then drops the roster row its first phase
    wrote (`drop_abandoned_row`), so a timed-out or failed sign-in leaves no never-signed-in account behind.
    Appended after every existing step, so an in-flight login recorded by an older release still replays."""
    verdict: dict[str, Any] = {"ok": False}
    try:
        verdict = _authenticate(
            slug, node_id, reauth, with_code, creator_user_id, new_account, provider, born_shared, peer_key,
            personal,
        )
    finally:
        # Also on a raise, which ends a login as surely as a failed verdict. Not gated on `new_account`: the
        # wizard's restart is an enrolment of the row its first start wrote, and may be abandoned too; the
        # step drops only a row that never signed in.
        if not reauth and not verdict.get("ok"):
            try:
                drop_abandoned_row(slug, node_id)
            except Exception:  # noqa: BLE001 — a cleanup failure must not replace the sign-in's own outcome
                log.warning("abandoned sign-in %r on node %r: could not drop its row", slug, node_id, exc_info=True)
    return verdict


def _authenticate(
    slug: str, node_id: str, reauth: bool, with_code: bool = True,
    creator_user_id: str | None = None, new_account: bool = True,
    provider: str = agent_providers.DEFAULT_PROVIDER, born_shared: bool = False,
    peer_key: str | None = None, personal: bool = False,
) -> dict[str, Any]:
    """The login itself, run inside `authenticate_account`'s workflow.

    Per-attempt `login_workflow_id`; concurrent-login refusal is `in_flight`'s query, not the id. Every
    parameter after the first three is last and defaulted to the older release's behaviour (a durable
    workflow's recorded inputs are a replay contract):
      * `with_code` (True): RETIRED and unread — the provider's declared `login_completion` decides
        (`_declared_completion`). Kept in position so an older release's recorded inputs still replay.
      * `creator_user_id`: who the account may belong to (`authz.stamp_owner_on_create`); absent
        means the org's.
      * `new_account` (True): create vs enrol, passed in (`routes/commands.py::_account_slug_is_new`)
        because a replay after `ensure_row` would read it wrong.
      * `provider`: which agent CLI; decides the node flag and the row's `provider`.
      * `born_shared` (False): whether an old `jaahd` is told to mint on its share (the route's decision,
        `routes/commands.py::enqueue_command`), passed in so a replay cannot drift.
      * `peer_key`: the wizard's per-login public key, required; the node seals the authorize
        URL to it and the code comes back sealed to the node's key. The `None` default only keeps the
        parameter in position for the recorded-input contract: a login without one is refused
        (`login_seal_required`), never run in the clear.
      * `personal` (False): the new account is the creator's own (`authz.stamp_owner_on_create`'s
        `personal` choice; always for a non-administrator, G2) rather than the creator's default.
    Returns a verdict dict rather than raising, so adapters can render the reason.
    """
    start_verb = "account_reauth" if reauth else "account_add"
    # The declared mode is resolved before the dispatch: the node reads no mode flag any more, and the
    # CP's own wait (`recv` vs `_await_redirect`) follows the catalogue, never the request.
    paste_code = _declared_completion(provider) == agent_providers.PASTE_CODE
    if peer_key is None:
        # The route requires a key (`routes/commands.py`); nothing may run this login in the clear.
        return {"ok": False, "error": "login_seal_required"}
    # The node seals the authorize URL to this key and mints its own for the code (`seal_key`).
    start_args: dict[str, Any] = {"slug": slug, "peer_key": peer_key}
    # A creation sends `shared: true` to a node reporting a mounted share; enrolment and re-auth send nothing
    # (the node reads the roster). Never `shared: false`: the node's override is one-way. The share flag
    # is injected here because the workflow dispatches the start verb (`routes/commands.py::
    # enqueue_command` is the argument's home); `new_account` keeps a retried creation from becoming an
    # enrolment.
    if not reauth and new_account and born_shared:
        start_args["shared"] = True
    # `reauth`/`new_account` still decide everything else; they no longer select a store.
    if not agent_providers.implied_on_wire(provider):
        # Omitted for Claude: absence means `DEFAULT_PROVIDER` on both sides, keeping the payload
        # byte-identical for older daemons.
        start_args["provider"] = provider
    reply = dispatch(node_id, start_verb, start_args)
    # Take the device code off the stash immediately, before any branch can return past it and leak
    # the entry.
    user_code = _reported_user_code(reply)
    config_dir = _login_config_dir(reply)
    sealed_url, seal_key = _sealed_login(reply)
    if str(reply.get("status")) != "done" or not sealed_url:
        if _plain_url_refused(reply) and str(reply.get("status")) == "done":
            # A plain `oauth_url` to a sealed request is a daemon that predates the seal. The `dispatch`
            # step already dropped the URL (it never reached `operation_outputs`); it is never published
            # either: continuing in the clear would put the one secret this CP must not hold into
            # `workflow_events`, and a wizard that sent a key cannot render a plaintext link anyway.
            log.warning(
                "durable login for slug %r asked node %r to seal the authorize URL but it answered in "
                "the clear (an old daemon); failing the login rather than continuing unsealed",
                slug, node_id,
            )
            return {"ok": False, "error": "login_seal_unsupported"}
        return {"ok": False, "error": _failure_reason(reply) or "login_start_failed"}

    # The projection the wizard polls, in one write (`set_event` has no cross-key atomicity): only
    # ciphertext and the node's public key, never `oauth_url` — the CP cannot read what it stores. The
    # node-reported store rides along (`start_login` answers from `**published`, and
    # `_ensure_account_row` writes `accounts.shared` from it), as does the device-auth user code, the
    # other half of the instruction. Each is published only when the node named it: absent (old daemon,
    # or Claude for the code) and `""` (local mint, or an adapter defect) differ. Never logged.
    published: dict[str, Any] = {
        "sealed_url": sealed_url, "seal_key": seal_key, "config_dir": config_dir,
    }
    reported_store = _reported_store(reply)
    if reported_store is not None:
        published["securestorage_dir"] = reported_store
    if user_code is not None:
        published["user_code"] = user_code
    elif not paste_code:
        # A login whose declared mode expects a code publishes `""` when none is recoverable: a restart
        # between the step commit and this `set_event` replays from the scrubbed record with an empty
        # stash. Omitting the key would render "nothing to paste", which is false here; `""` renders
        # the warning. Gated on `await_credentials`, the only signal the CP has. Claude cannot reach
        # this branch.
        published["user_code"] = ""
    DBOS.set_event(AUTH_EVENT, published)

    # The code is retired in `finally` once the workflow stops being live: `workflow_events` has no
    # GC, so it must not outlive the login. This instant is the edge of `LIVE_STATUSES`, the only set
    # `provisioning_records` reads the event for. Limit: `workflow_events_history` still holds the
    # publish; that needs a retention pass on the DBOS schema.
    try:
        if paste_code:
            code = DBOS.recv(topic=CODE_TOPIC, timeout_seconds=CODE_TIMEOUT_SECONDS)
            if code is None:
                # `recv` reports a timeout as None; nothing sends None (`submit_code` validates).
                return {"ok": False, "error": "login_timeout"}

            # The browser's ciphertext, relayed as `sealed_code` for the node to unseal with its
            # per-login key; this CP never sees the code.
            reply = dispatch(node_id, f"{start_verb}_submit_code",
                             {"slug": slug, "sealed_code": str(code)})
            if str(reply.get("status")) != "done":
                return {"ok": False, "error": _failure_reason(reply) or "login_failed"}
        else:
            verdict, reply = _await_redirect(node_id, start_verb, slug)
            if verdict is not None:
                return verdict

        if not reauth:
            # Only Add-Account creates a roster row; re-auth must never re-enable a disabled one.
            # Ungated by `new_account`: an enrolment's insert loses to `ON CONFLICT DO NOTHING`. No
            # store is passed: phase 2 names none, and the insert only wins when phase 1 wrote no row,
            # where fail-closed `shared = false` is honest.
            ensure_row(slug, reply.get("result") or {}, node_id, True, creator_user_id, provider,
                       born_shared, personal)
        lift_auth_rejection(slug, node_id)
        return {"ok": True, "config_dir": _login_config_dir(reply)}
    finally:
        _retire_published_user_code(published)


def _retire_published_user_code(published: dict[str, Any]) -> None:
    """Re-publish the auth projection without its device-auth `user_code`, keeping every other key.

    Called only from `authenticate_account`'s `finally`. A re-publish, not a delete or a null: the other
    keys feed the roster row, and null would be a fourth spelling. A no-op (no write) for Claude.

    The stored projection is read back rather than trusting `published`, which a replay rebuilds from
    the scrubbed step output; "could not look" (`AuthProjectionEngineUnlaunched`) is waited out, not
    read as "nothing to clear". Also drains the stash for replies from later steps. Never replaces the
    login's own outcome: a failed re-publish is logged and swallowed; the drain happens first.
    """
    workflow_id = _current_workflow_id()
    _unrecorded_user_codes.pop(workflow_id or "", None)
    try:
        stored = (published if "user_code" in published
                  else _stored_auth_projection_once_readable(workflow_id))
        if "user_code" not in stored:
            return
        DBOS.set_event(AUTH_EVENT, {k: v for k, v in stored.items() if k != "user_code"})
    except Exception:
        log.warning(
            "durable login: could not clear the device code from the auth projection; it stays in "
            "workflow_events until the DBOS system schema is pruned", exc_info=True,
        )


# The DBOS system table `set_event` writes into; named because it is a third-party schema
# (`cron_retention.DBOS_STATUS_TABLE`'s reason).
_DBOS_EVENTS_TABLE = "workflow_events"


class AuthProjectionEngineUnlaunched(RuntimeError):
    """The stored auth projection could not be read because this process has no launched DBOS engine yet.

    A type rather than `{}`: "could not look" and "nothing stored" send the terminal clear in opposite
    directions. Distinct from `DbosGarbageCollectUnavailable` (a moved private symbol, always
    escalates); this is an ordinary startup window the retry may wait out."""


# How long the terminal clear waits for DBOS to finish launching: recovery runs inside the launch, so a
# recovered login can finish before `launched_engine()` answers. Moments, not minutes.
_ENGINE_LAUNCH_WAIT_SECONDS = 15.0
# Polled: `launched_engine` reads a module global with no event. `time.sleep`, never `DBOS.sleep`,
# which would record a step inside the workflow body.
_ENGINE_LAUNCH_POLL_SECONDS = 0.5


def _stored_auth_projection_once_readable(workflow_id: str | None) -> dict[str, Any]:
    """`_stored_auth_projection`, waiting out a DBOS engine that is still coming up.

    Only `AuthProjectionEngineUnlaunched` is retried; every other empty answer is returned at once.
    Past `_ENGINE_LAUNCH_WAIT_SECONDS` it re-raises into the caller's logging `try`."""
    deadline = time.monotonic() + _ENGINE_LAUNCH_WAIT_SECONDS
    while True:
        try:
            return _stored_auth_projection(workflow_id)
        except AuthProjectionEngineUnlaunched:
            if time.monotonic() >= deadline:
                raise
            time.sleep(_ENGINE_LAUNCH_POLL_SECONDS)


def _stored_auth_projection(workflow_id: str | None) -> dict[str, Any]:
    """The auth projection as it currently stands in `workflow_events`, or `{}` when there is none.

    Separate so tests can substitute it. A plain select, never `DBOS.get_event`, which inside a workflow
    records its result (the live code) into `operation_outputs`. A look, never a wait. The schema, the
    serializer and `resolve_deserialize_value` come via `cron_retention` (canaried by
    `tests/test_cron_retention.py`), so a moved `dbos` symbol raises instead of answering `{}`.

    Only "no workflow id / no row / not a mapping" answer `{}`. An unlaunched engine raises
    `AuthProjectionEngineUnlaunched`; anything else broken raises too, into the caller's logging `try`."""
    if not workflow_id:
        return {}
    # Deferred: these pull the cron runtime in; not a cycle break.
    from app.cron_reconcile import launched_engine
    from app.cron_retention import (
        dbos_system_schema,
        dbos_system_serializer,
        resolve_deserialize_value,
    )

    engine = launched_engine()
    if engine is None:
        raise AuthProjectionEngineUnlaunched(
            "the DBOS engine is not launched in this process, so the stored auth projection cannot be "
            "read; treating that as an empty projection would skip a device-code clear rather than "
            "report it"
        )
    schema = dbos_system_schema(engine)
    with SessionLocal() as session:
        row = session.execute(
            text(
                f'SELECT "value", "serialization" FROM "{schema}"."{_DBOS_EVENTS_TABLE}" '
                'WHERE "workflow_uuid" = :workflow_id AND "key" = :key'
            ),
            {"workflow_id": workflow_id, "key": AUTH_EVENT},
        ).first()
    if row is None:
        # The only `{}` that means something was read: no event under this key.
        return {}
    stored = resolve_deserialize_value()(row[0], row[1], dbos_system_serializer(engine))
    return stored if isinstance(stored, dict) else {}


@DBOS.workflow()
def retire_account(slug: str, config_dir: str, node_ids: list[str]) -> dict[str, Any]:
    """Remove an account's credentials from every named node.

    Credentials first; the roster row is the caller's to delete afterwards
    (`routes/accounts.py::deprovision_account`), so a failure keeps the row. Binds nothing: `dispatch`
    scopes its own identity read, and nothing else here reads tenant data.
    """
    results = [dispatch(node_id, "account_delete", {"config_dir": config_dir}) for node_id in node_ids]
    failed = [r for r in results if str(r.get("status")) != "done"]
    return {"ok": not failed, "slug": slug, "results": results}


# --------------------------------------------------------------------------- reply shapes

def _node_payload(reply: dict[str, Any]) -> dict[str, Any]:
    """The node's own JSON stdout, parsed as `commands.py`'s reply readers parse it."""
    raw = ((reply or {}).get("result") or {}).get("stdout")
    if not isinstance(raw, str) or not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except ValueError:
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _login_config_dir(reply: dict[str, Any]) -> str | None:
    """`config_dir` out of a phase reply, via the existing single-home reader. A login reply carries no
    `oauth_url` to read (the link travels as `sealed_url`, read by `_sealed_login`)."""
    from app.routes.commands import _config_dir_from_reply

    return _config_dir_from_reply((reply or {}).get("result"))


def _sealed_login(reply: dict[str, Any]) -> tuple[str | None, str | None]:
    """`(sealed_url, seal_key)` out of a phase-1 reply's own JSON stdout, or `(None, None)` unless BOTH
    are in the wire shape (`app/login_seal_wire.py`: a sealed value is whole RSA chunks, a key decodes;
    both bounded, since an unbounded blob must not reach `workflow_events`).

    All-or-nothing: a sealed URL without the node's key (or the reverse) cannot complete a login, and
    publishing half would render a link the browser cannot open. Untrusted subprocess stdout; nothing
    is logged (the ciphertext is the operator's authorize URL)."""
    payload = _node_payload(reply)
    sealed_url, seal_key = payload.get("sealed_url"), payload.get("seal_key")
    if not is_wire_sealed(sealed_url) or not is_wire_key(seal_key):
        return None, None
    return sealed_url, seal_key


def _plain_url_refused(reply: dict[str, Any]) -> bool:
    """Whether the `dispatch` step dropped a plaintext `oauth_url` from this sealed request's reply
    (`_keep_plain_url_off_the_record`'s marker): the node is an old daemon."""
    return _node_payload(reply).get(_PLAIN_URL_REFUSED) is True


def _reported_store(reply: dict[str, Any]) -> str | None:
    """The credential store the node says a phase-1 login was pointed at — `""` for an ordinary local
    mint, None when it named none this CP can use.

    Borrows `routes/commands.py`'s reader so the two engines cannot drift; `_ensure_account_row`
    compares this exact string. `""` and None stay apart all the way to the row."""
    from app.routes.commands import _securestorage_dir_from_reply

    return _securestorage_dir_from_reply((reply or {}).get("result"))


def _reported_user_code(reply: dict[str, Any]) -> str | None:
    """The device-auth user code the node printed for this phase-1 login, or None when the reply named
    no such key.

    Borrows `routes/commands.py`'s reader (which says why it is the user code, never logged). `""` and
    None stay apart to the wizard. Reads the stash first (`dispatch` strips the reply), then the reply,
    for a login recorded by an older release; the stash is drained either way."""
    from app.routes.commands import _user_code_from_reply

    stashed = _unrecorded_user_codes.pop(_current_workflow_id() or "", None)
    code = _user_code_from_reply((reply or {}).get("result"))
    return code if code is not None else stashed


def _failure_reason(reply: dict[str, Any]) -> str | None:
    """The node's reason for refusing, in the order `enqueue_command` looks for it."""
    result = (reply or {}).get("result") or {}
    if result.get("reason"):
        return str(result["reason"])
    if _node_payload(reply).get("status") == "in_progress_elsewhere":
        # The node's per-slug flock refused: a host property, carried verbatim.
        return "in_progress_elsewhere"
    stderr = result.get("stderr")
    return str(stderr) if stderr else None


def _error_text(error: Any, workflow_id: str | None = None) -> str | None:
    """A person-readable sentence for a recorded workflow error, or None when there is none to give.

    A `str` here is DBOS's undeserializable raw column (base64), not a reason: logged, treated as none.
    The retry wrapper is unwrapped to its last attempt's error.
    """
    if error is None:
        return None
    if isinstance(error, str):
        log.warning(
            "durable login: workflow %s recorded an error DBOS could not deserialize; its reason is "
            "withheld from the reply rather than rendered raw", workflow_id or "<unknown>",
        )
        return None
    if isinstance(error, DBOSMaxStepRetriesExceeded) and error.errors:
        return _error_text(error.errors[-1], workflow_id)
    if isinstance(error, HTTPException):
        return str(error.detail)
    if isinstance(error, RuntimeError):
        # This module's own loud failures are written for a person and rendered as-is.
        return str(error)
    # Anything else is withheld (SQL fragments, hostnames, DBOS internals); logged with its type, and
    # the caller's fallback is rendered.
    log.warning(
        "durable login: workflow %s failed with %s; its reason is withheld from the reply rather than "
        "rendered raw", workflow_id or "<unknown>", type(error).__name__,
    )
    return None


def _terminal_reason(status: Any) -> str | None:
    """Why a finished workflow finished badly, or None if it finished well."""
    if status is None:
        return None
    if status.status == "SUCCESS" and isinstance(status.output, dict):
        return None if status.output.get("ok") else str(status.output.get("error") or "login_failed")
    if status.status in ("ERROR", "MAX_RECOVERY_ATTEMPTS_EXCEEDED"):
        # None for an undeserializable error: each caller has its own `or <fallback>` the wizard renders.
        if status.error is None:
            return status.status
        return _error_text(status.error, getattr(status, "workflow_id", None))
    return "cancelled" if status.status == "CANCELLED" else None


def _reply(status: str, payload: dict[str, Any], reason: str | None = None) -> tuple[str, dict]:
    """A node-shaped reply, which is what `enqueue_command` audits and returns to the wizard."""
    result: dict[str, Any] = {
        "exit_code": 0 if status == "done" else 1,
        "stdout": json.dumps(payload),
        "stderr": "",
    }
    if reason:
        result["reason"] = reason
    return status, result


# --------------------------------------------------------------------------- endpoint adapters

def in_flight(node_id: str, slug: str) -> bool:
    """Whether a login for this pair is already running — the 409 guard's question.

    DBOS offers no refusal: a duplicate `SetWorkflowID` start attaches to the running workflow. Ids are
    per-attempt (`login_workflow_id`), so this query is the only exclusion. No freshness window is
    needed: `CODE_TIMEOUT_SECONDS` ends every workflow.
    """
    return _live_login(node_id, slug) is not None


#: What a login was started with, read off its recorded inputs: the three values an operator can
#: change between attempts. Defaults are `authenticate_account`'s, for an older release's shorter list.
def login_request(node_id: str, slug: str) -> dict[str, Any] | None:
    """The parameters the live login for this pair was started with, or None if none is running.

    The 409 guard's second question: it tells a double-click (resume) from a deliberately different
    request. `peer_key` is one of them: a reopened wizard mints a new key, so its request never equals
    the live one and the dead attempt is cancelled rather than resumed. Read off `workflow.input`, never
    re-decided.
    """
    workflow = _live_login(node_id, slug)
    if workflow is None:
        return None
    args = (getattr(workflow, "input", None) or {}).get("args") or ()
    return {
        "reauth": bool(args[2]) if len(args) > 2 else False,
        "peer_key": (str(args[8]) if len(args) > 8 and args[8] is not None else None),
        "provider": str(args[6]) if len(args) > 6 else agent_providers.DEFAULT_PROVIDER,
    }


#: `login_creator`'s answer when no login for the pair is running — distinct from `None`, a login an
#: older release started with no recorded creator.
NO_LIVE_LOGIN = object()


def login_creator(node_id: str, slug: str) -> Any:
    """Who started the live login for this pair — the recorded `creator_user_id` (a string, or None when
    none was recorded) — or `NO_LIVE_LOGIN`. Read off `workflow.input`, as `login_request`; the personal
    path's bound on touching someone else's login (`routes/commands.py::_require_own_login`)."""
    workflow = _live_login(node_id, slug)
    if workflow is None:
        return NO_LIVE_LOGIN
    args = (getattr(workflow, "input", None) or {}).get("args") or ()
    return str(args[4]) if len(args) > 4 and args[4] is not None else None


def cancel_login(node_id: str, slug: str) -> bool:
    """End the live login for this pair so a differently-parameterised one can take its place.

    Cancelled is not in `LIVE_STATUSES`, so the next `in_flight`/`login_request` sees nothing. Nothing
    is undone on the node: its `_login_start` clears a prior attempt's process and FIFO. The cancelled
    workflow may make one more read-only node poll. Returns whether a login was actually cancelled.
    """
    workflow = _live_login(node_id, slug)
    if workflow is None:
        return False
    DBOS.cancel_workflow(workflow.workflow_id)
    log.info(
        "cancelled login workflow %s for slug %r on node %r so a differently-parameterised attempt can "
        "start", workflow.workflow_id, slug, node_id,
    )
    return True


def start_login(
    command_type: str, node_id: str, slug: str, with_code: bool = True,
    creator_user_id: str | None = None, new_account: bool = True,
    provider: str = agent_providers.DEFAULT_PROVIDER, born_shared: bool = False,
    peer_key: str | None = None, personal: bool = False,
) -> tuple[str, dict]:
    """Phase 1: start the workflow and answer with the (sealed) authorization URL.

    The reply splats the whole published projection because the roster row is written from it
    (`_ensure_account_row` reads `securestorage_dir`). `creator_user_id` (a string: a persisted input),
    `new_account`, `provider` and `peer_key` are relayed from the route, never re-decided, and defaulted
    like the workflow's parameters; `with_code` is kept in position for the recorded-input contract and
    ignored (the declared mode decides)."""
    workflow_id = login_workflow_id(node_id, slug, int(time.time() * 1000))
    with SetWorkflowID(workflow_id):
        DBOS.start_workflow(
            authenticate_account, slug, node_id, command_type == "account_reauth", with_code,
            creator_user_id, new_account, provider, born_shared, peer_key, personal,
        )
    published = _await_auth_event(workflow_id)
    if not isinstance(published, dict) or not published.get("sealed_url"):
        reason = _terminal_reason(DBOS.get_workflow_status(workflow_id))
        return _reply("failed", {"status": "error"}, reason or "oauth_url_scrape_failed")
    return _reply("done", {"status": "awaiting_code", **published})


#: How often `_await_auth_event` re-checks the workflow while no link is published.
AUTH_EVENT_POLL_SECONDS = 1.0


def _await_auth_event(workflow_id: str) -> Any:
    """The login's published projection, or None once the workflow has ended without one.

    Polled rather than one `AUTH_URL_WAIT_SECONDS` wait: a node refusing the start (wrong OS user, no CLI, a
    Windows sign-in rule) ends the workflow without publishing, and the page must get the node's words at
    once instead of a timeout a minute later."""
    deadline = time.monotonic() + AUTH_URL_WAIT_SECONDS
    while True:
        left = deadline - time.monotonic()
        published = DBOS.get_event(workflow_id, AUTH_EVENT,
                                   timeout_seconds=max(0.0, min(AUTH_EVENT_POLL_SECONDS, left)))
        if isinstance(published, dict) or left <= 0:
            return published
        status = DBOS.get_workflow_status(workflow_id)
        if status is not None and status.status not in LIVE_STATUSES:
            # Once more, at once: the link may have been published between the wait and the status read.
            return DBOS.get_event(workflow_id, AUTH_EVENT, timeout_seconds=0)


def submit_code(node_id: str, slug: str, sealed_code: str) -> tuple[str, dict]:
    """Phase 2: hand the operator's sealed code to the waiting workflow and report the outcome.

    `sealed_code` is ciphertext the browser sealed to the node's per-login key; this CP relays it and
    cannot check its content (the route bounds its wire shape). The node unseals it, applies its own
    code check, and still does the pid check, FIFO open and credential poll; see the module docstring.
    """
    live = _live_login(node_id, slug)
    if live is None:
        # No workflow is waiting (the CP twin of `login_process_gone`). No fallback to a finished one.
        return _reply("failed", {"status": "error"}, "login_process_gone")
    workflow_id = live.workflow_id
    status: Any = live
    if not sealed_code:
        return _reply("failed", {"status": "error"}, "empty code")
    DBOS.send(workflow_id, sealed_code, topic=CODE_TOPIC)

    deadline = time.monotonic() + EXCHANGE_WAIT_SECONDS
    while time.monotonic() < deadline:
        status = DBOS.get_workflow_status(workflow_id)
        if status is not None and status.status not in LIVE_STATUSES:
            break
        time.sleep(0.5)

    output = status.output if status is not None else None
    if isinstance(output, dict) and output.get("ok"):
        return _reply("done", {"status": "done", "config_dir": output.get("config_dir")})
    return _reply("failed", {"status": "error"}, _terminal_reason(status) or "login_timeout")


def _latest_login(node_id: str, slug: str) -> Any:
    """The newest login workflow for this pair whatever its status, or None.

    Unlike `_live_login`, `await_login` may poll just after the workflow finished and must report the
    outcome.
    """
    for workflow in DBOS.list_workflows(name=LOGIN_WORKFLOW_NAME, limit=RECORD_LIMIT, sort_desc=True):
        if _login_args(workflow) == (slug, node_id):
            return workflow
    return None


def completed_logins(node_ids: Iterable[str]) -> dict[str, list[tuple[str, float]]]:
    """`{slug: [(node id, completed at as epoch seconds), ...]}`, newest first, for the logins on `node_ids` that
    ended in a login (`_latest_login`'s records, `SUCCESS` with `ok`) — the newest `RECORD_LIMIT` of them. ONE
    engine read for every account, scoped by `login_workflow_id`'s prefix to the caller's nodes, so another org's
    logins never crowd these out: the Brains page's Syncing window (`app/account_sharing.py`). `{}` when the
    engine cannot answer — the window then starts from the observations alone."""
    prefixes = sorted({login_workflow_prefix(node_id) for node_id in node_ids})
    if not prefixes:
        return {}
    try:
        workflows = DBOS.list_workflows(
            name=LOGIN_WORKFLOW_NAME, status="SUCCESS", workflow_id_prefix=prefixes, limit=RECORD_LIMIT,
            sort_desc=True,
        )
    except Exception as exc:  # noqa: BLE001 — a page read; the engine down must not take the Brains page
        log.warning("completed logins unreadable, Syncing starts from observations: %s", exc)
        return {}
    out: dict[str, list[tuple[str, float]]] = {}
    for workflow in workflows:
        pair = _login_args(workflow)
        done_ms = getattr(workflow, "completed_at", None) or getattr(workflow, "updated_at", None)
        if pair is None or not done_ms or not (isinstance(workflow.output, dict) and workflow.output.get("ok")):
            continue
        out.setdefault(pair[0], []).append((pair[1], done_ms / 1000.0))
    return out


def await_login(node_id: str, slug: str) -> tuple[str, dict]:
    """Phase 2 in `await_credentials` mode: report where the waiting workflow has got to.

    Does not dispatch: the workflow already polls the node, and a second dispatch would hit the
    per-slug flock. Reads workflow state: running is `awaiting_redirect`, else `done` or the reason.
    """
    workflow = _latest_login(node_id, slug)
    if workflow is None:
        # Nothing to wait on (the CP twin of `login_process_gone`).
        return _reply("failed", {"status": "error"}, "login_process_gone")
    if workflow.status in LIVE_STATUSES:
        # Non-terminal, exit 0. `driver: cp` tells the wizard to stop re-firing (the provisioning poll
        # reports the outcome). A marker: absence means "keep firing", which old wizards do harmlessly.
        return _reply("done", {"status": "awaiting_redirect", "driver": "cp"})
    output = workflow.output if isinstance(workflow.output, dict) else {}
    if workflow.status == "SUCCESS" and output.get("ok"):
        return _reply("done", {"status": "done", "config_dir": output.get("config_dir")})
    return _reply("failed", {"status": "error"}, _terminal_reason(workflow) or "login_failed")


def retire(slug: str, config_dir: str, node_ids: list[str]) -> dict[str, Any]:
    """Run the retirement workflow to completion and answer in the node-reply shape.

    Synchronous: `deprovision_account` deletes the roster row only once credentials are gone.
    """
    # No pinned workflow id: a retry must not attach to a failed attempt, and `account_delete` is
    # idempotent on the node.
    handle = DBOS.start_workflow(retire_account, slug, config_dir, node_ids)
    try:
        outcome = handle.get_result()
    except DBOSMaxStepRetriesExceeded as exc:
        # Transport failure unwrapped from DBOS's retry wrapper, so the route reports which failure and
        # audits it; anything else stays wrapped as a defect.
        last = exc.errors[-1] if exc.errors else None
        if isinstance(last, HTTPException):
            raise last from exc
        raise
    if outcome.get("ok"):
        return {"status": "done", "result": {"exit_code": 0, "stdout": "", "stderr": ""}}
    failures = [r for r in outcome.get("results", []) if str(r.get("status")) != "done"]
    reason = (_failure_reason(failures[0]) if failures else None) or "retire_failed"
    return {"status": "failed", "result": {"exit_code": 1, "stdout": "", "stderr": reason}}


def provisioning_records() -> list[dict[str, Any]]:
    """The wizard's poll rows, derived from workflow state instead of a table.

    `node_id` and `slug` come from recorded inputs, never the workflow id. Events are read for live
    workflows only (one `get_event` each); terminal rows take `config_dir` from the output.
    """
    out: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for workflow in DBOS.list_workflows(name=LOGIN_WORKFLOW_NAME, limit=RECORD_LIMIT, sort_desc=True):
        parsed = _login_args(workflow)
        if parsed is None:
            # Unattributable inputs: a wrong-slug record is worse than none.
            continue
        slug, node_id = parsed
        # One row per `(node_id, slug)`, newest wins (`sort_desc`).
        if (node_id, slug) in seen:
            continue
        seen.add((node_id, slug))
        output = workflow.output if isinstance(workflow.output, dict) else {}

        # The user code is set only by the live branch: a finished login has no code to type. Absent
        # stays absent for Claude; `""` survives unflattened.
        user_code = None

        # The sealed link is set only by the live branch too: the record carries ciphertext or nothing.
        sealed_url = seal_key = None

        if workflow.status in LIVE_STATUSES:
            published = DBOS.get_event(workflow.workflow_id, AUTH_EVENT, timeout_seconds=0)
            published = published if isinstance(published, dict) else {}
            sealed_url, seal_key = published.get("sealed_url"), published.get("seal_key")
            config_dir = published.get("config_dir")
            user_code = published.get("user_code")
            step = "awaiting_code" if sealed_url else "awaiting_oauth"
            state, error = "in_progress", None
        elif workflow.status == "SUCCESS" and output.get("ok"):
            config_dir = output.get("config_dir")
            step, state, error = "done", "done", None
        else:
            config_dir = output.get("config_dir")
            error = _terminal_reason(workflow) or workflow.status
            step = "error"
            # The wizard renders a timeout distinctly (`error_timeout`).
            state = "error_timeout" if error == "login_timeout" else "error"

        out.append(
            {
                "slug": slug,
                "config_dir": config_dir,
                "step": step,
                "sealed_url": sealed_url,
                "seal_key": seal_key,
                "user_code": user_code,
                "status": state,
                "error": error,
                "started_at": _iso(workflow.created_at),
                "node_id": node_id,
            }
        )
    return out


def _iso(epoch_ms: int | None) -> str | None:
    """DBOS records times as epoch milliseconds; `ProvisioningOut.started_at` is an ISO string."""
    if not epoch_ms:
        return None
    return datetime.fromtimestamp(epoch_ms / 1000, tz=timezone.utc).isoformat()
