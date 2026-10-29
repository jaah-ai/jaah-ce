"""The git token refresh, made durable: one rotation per grant, checkpointed as ciphertext.

The rotation itself is keyed by forge (`ROTATIONS`), one per `forge_oauth.FORGE_OAUTH` entry. A GitHub
App refresh token is single-use, and GitLab's and Entra's rotate too, so a death between `rotate` and
`persist` would brick the credential. `rotate` is a checkpointed `@DBOS.step`, its output sealed under `DURABLE_GIT_TOKEN_AAD` (no
secret in the clear anywhere on this path); recovery replays it and resumes at the lost write.

Synchronous: `git_access.token_for` blocks on the answer and releases its row lock first (holding it
would self-deadlock `persist`). The deterministic id (`workflow_id`) is the fence between racing
callers; DBOS runs the body once and the loser adopts the outcome. Transient refusals, and a grant's run
that ended in an error, get one retry under a coarse windowed id (`retry_workflow_id`, `is_retryable`,
`_ended_in_error`); `persist` is fenced on the grant
it replaces (`_REPLACES`) so a replay never overwrites a human's reconnect.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Callable, Optional
from uuid import UUID

from dbos import DBOS, SetWorkflowID

from app.durable import runtime
from app.entity_shape import iso_or_none

# Its own logger, separate from `app.git_access`: the two fail for unrelated reasons.
log = logging.getLogger("git-token-refresh")

# Bound on GitHub's refusal text, matching `git_access.token_for`: it is written to `durable_dbos_sys`
# and relayed to journald and a runner's terminal. A blast-radius bound, applied here so the unbounded
# form never reaches the checkpoint.
_DETAIL_LIMIT = 200

#: `outcome["code"]` when the credential row is gone by the time the rotation runs (the caller's lock is
#: released first and a replay can be much later). Not a GitHub code, so `token_for` can distinguish it.
CREDENTIAL_GONE = "credential_gone"

#: `outcome["code"]` when the fence held: the row no longer carries the grant this rotation was taken
#: against, so nothing was written. Not a failure to the caller: `token_for` serves the row's working
#: token. Kept distinct from the other two codes because causes and remedies differ.
GRANT_SUPERSEDED = "grant_superseded"

#: The field inside the sealed payload naming the grant a rotation was taken against, checked by
#: `persist`. Inside the envelope so the GCM tag covers it and it cannot be edited apart from the grant.
_REPLACES = "replaces"

# Retry window: wide enough that concurrent callers share one id (a straddled boundary costs the
# loser one spurious reconnect prompt), narrow enough that a brief GitHub incident heals in minutes.
# Five minutes is ~0.3% straddle for a one-second race and two attempts within
# `git_access._REFRESH_MARGIN`; after expiry `_needs_refresh` keeps retrying.
_RETRY_WINDOW_SECONDS = 300


def _github_rotation(refresh_token: str) -> Any:
    """One GitHub refresh: the deployment's GitHub App (`git_access.app_config`, no org) spends
    `refresh_token` and returns the new `github_oauth.TokenGrant`, raising `GitHubOAuthError`. Only a GitHub
    App's token carries a refresh token; a classic OAuth App's never expires."""
    from app import github_oauth
    from app.git_access import app_config

    app = app_config(kind="github_app")
    return github_oauth.refresh(
        client_id=app.client_id, client_secret=app.client_secret, refresh_token=refresh_token,
    )


def _forge_rotation(forge: str) -> Callable[[str], Any]:
    """One non-GitHub refresh: the deployment's `forge` app (`git_access.forge_app_config`, no org) spends
    `refresh_token` through `forge_oauth.refresh`, raising `ForgeOAuthError`."""

    def rotation(refresh_token: str) -> Any:
        from app import forge_oauth
        from app.git_access import forge_app_config

        return forge_oauth.refresh(
            forge_oauth.FORGE_OAUTH[forge], forge_app_config(forge), refresh_token,
        )

    return rotation


#: The rotation for each forge whose grants renew, keyed by credential type, the single dispatch point
#: `rotate` goes through. Each takes the opened refresh token and returns a `TokenGrant`, raising an
#: `OAuth2Error` carrying the forge's machine-readable `code`. Held equal to
#: `git_access.REFRESHABLE_TYPES` by a test.
ROTATIONS: dict[str, Callable[[str], Any]] = {
    "github": _github_rotation,
    "gitlab": _forge_rotation("gitlab"),
    "bitbucket": _forge_rotation("bitbucket"),
    "azure_devops": _forge_rotation("azure_devops"),
}


def token_refresh_workflow_id(credential_id: Any, grant_expires_at_iso: str) -> str:
    """The deterministic ID every caller racing one expiring grant must agree on.

    A pure seam, testable without a live DBOS instance. Keyed on the grant: the credential alone would
    freeze it on its first rotation's outcome; anything finer would admit two racers and spend the token
    twice. Nothing ambient enters it; the retry's clock lives in `retry_workflow_id`.

    `grant_expires_at_iso` is the expiry of the grant being spent, read under the caller's row lock: a
    timestamp, not a token, since ids are stored, listed and logged.
    """
    return f"git-token-refresh:{credential_id}:{grant_expires_at_iso}"


def retry_workflow_id(
    credential_id: Any, grant_expires_at_iso: str, *, now: Optional[datetime] = None
) -> str:
    """The id a second attempt at one grant runs under: the base id plus a coarse time window.

    The base id is terminal (DBOS returns its recorded outcome), so a retry needs a new id that concurrent
    callers still agree on. `floor(now / _RETRY_WINDOW_SECONDS)` is epoch-aligned, so every caller in the
    bucket gets the same id. `now` is for tests only; production never passes it.
    """
    window = int((now or datetime.now(timezone.utc)).timestamp()) // _RETRY_WINDOW_SECONDS
    return f"{token_refresh_workflow_id(credential_id, grant_expires_at_iso)}:retry-{window}"


def is_retryable(outcome: dict[str, Any]) -> bool:
    """Whether `outcome` is a refusal another attempt could still turn into a token.

    A terminal list, so unknown forge codes default to retry: a hopeless retry costs a round trip, a
    false permanence costs a human reconnect. Terminal: a refusal `rotate` marked `dead` (the code is one
    of the refreshed forge's OWN `refresh_dead_codes`; GitHub's `bad_refresh_token` also by code, for a
    checkpoint recorded before the flag),
    `credential_gone` (no row), `grant_superseded` (another grant is on the row; serve it). A success is
    never retryable, checked explicitly since it has no `code`, like the ambiguous refusals.
    """
    # Deferred (the wire pulls no ORM). Imported so the terminal code cannot drift from GitHub's.
    from app.github_oauth import REFRESH_TOKEN_DEAD

    if outcome.get("ok") or outcome.get("dead"):
        return False
    return outcome.get("code") not in (REFRESH_TOKEN_DEAD, CREDENTIAL_GONE, GRANT_SUPERSEDED)


@DBOS.step()
def rotate(credential_id: str) -> dict[str, Any]:
    """Spend the credential's refresh token exactly once and return the new grant, sealed.

    Returns `{"ok": True, "sealed": {...}}` or `{"ok": False, "code": ..., "detail": ...}`, `code` being
    GitHub's machine-readable `error` when sent.

    No step retries: re-sending after a timeout would burn a rotated token. (`run_refresh`'s single retry
    is a new workflow id one window later, not a blind ladder.) A refusal is a return value; a defect
    (DB fault, unresolvable App) raises, ending the run ERROR, which DBOS never re-attempts.

    Only the row id travels: the refresh token and the deployment's App (`git_access.app_config`, no org)
    are read here, since secrets must not be step arguments and a replay must act on current state.
    """
    # Deferred: imported at DBOS registration time, before the app is wired; see
    # `task_notify_queue.deliver_task_notification`.
    from app import oauth2_wire
    from app.credential_crypto import DURABLE_GIT_TOKEN_AAD, has_sealed_secret, open_secret, seal
    from app.db import SessionLocal
    from app.models.credentials import Credential
    from app.tenancy import exclusion_log, seam

    # Attributed, since a step runs on a DBOS worker thread with no request; see
    # `app/durable/runtime.py::step_session`.
    with exclusion_log.caller("durable:git_token.rotate"):
        try:
            # Bound from the row being refreshed, never `operator_scope()`. It does not cover the App:
            # `app_config` reads on its own session and plane. `detached()` first because `token_for` runs
            # this workflow INLINE on the caller's thread, so the step inherits the request's scope: an
            # operator-plane caller (`OPERATOR`) reading its own connection, which lives in its home org,
            # would otherwise be refused the derivation. Detached, the inline run binds exactly
            # as a recovered run on a DBOS worker does. ENTITLEMENT (`seam.detached`): `credential_id` is
            # only ever a row `git_access.token_for` was handed by a scoped `resolve_*` read.
            with seam.detached(), SessionLocal() as session, seam.bind_from(session, Credential, UUID(credential_id)):
                credential = session.get(Credential, UUID(credential_id))
                if credential is None:
                    # Unreachable while `bind_from` resolves the same row first; handled anyway.
                    raise seam.TenancyRowMissing(f"credential {credential_id} is gone")
                credential_type = credential.type
                rotation = ROTATIONS.get(credential_type)
                if rotation is None:
                    # Unreachable: `git_access._needs_refresh` offers only `REFRESHABLE_TYPES`. A defect.
                    raise RuntimeError(f"credential {credential_id} has no rotation for its type")
                # Read out as plain strings before the session closes (a lazy attribute). The refresh
                # token is a sealed field, opened here (`credential_crypto.open_secret`, the private
                # operation) because this is where it is spent; the opened string is what crosses to
                # the round trip below. `token_expires_at` is plain.
                refresh_token = (
                    open_secret(credential, "refresh_token")
                    if has_sealed_secret(credential.config or {}, "refresh_token")
                    else ""
                )
                # The fence's left-hand side, read here because `persist` asks whether the row moved since
                # now. Re-read rather than passed in, since a step argument would be trusted.
                replaces = str(credential.config.get("token_expires_at") or "")
        except seam.TenancyRowMissing:
            # Deleted between the caller's lock and this run. Not an exception: nothing to refresh and no
            # retry would help. The caller gives its usual deleted-credential refusal.
            log.info(
                "git-token-refresh: credential %s no longer exists — nothing to refresh", credential_id
            )
            return {"ok": False, "code": CREDENTIAL_GONE, "detail": ""}

    # The session is closed before the round trip so the connection returns to the pool rather than
    # idling in a transaction (`app/tenancy/seam.py::bind_from`).
    try:
        # The App (for GitHub) is the deployment's, resolved inside the rotation without an org.
        grant = rotation(refresh_token)
    except oauth2_wire.OAuth2Error as exc:
        detail = str(exc)
        if len(detail) > _DETAIL_LIMIT:
            detail = detail[:_DETAIL_LIMIT] + "…"
        # `exc.code`, never `str(exc)`: the caller's reconnect branch keys on the forge's dead-grant code.
        log.warning(
            "git-token-refresh: credential %s was refused by its forge (code=%s)", credential_id, exc.code,
        )
        # `dead`: judged against THIS forge's codes, since one forge's `invalid_grant` is not another's.
        from app.forge_oauth import FORGE_OAUTH

        dead = exc.code in FORGE_OAUTH[credential_type].refresh_dead_codes
        return {"ok": False, "code": exc.code, "detail": detail, "dead": dead}

    log.info(
        "git-token-refresh: credential %s rotated; the grant is checkpointed sealed", credential_id
    )
    # The only place the grant exists in the clear is this frame. `expires_at` is an ISO string, the
    # spelling `credentials.config` stores. `_REPLACES` travels inside the same envelope (`{v, n, ct}`
    # only), binding it to the grant.
    return {
        "ok": True,
        "sealed": seal(
            {
                "access_token": grant.access_token,
                "refresh_token": grant.refresh_token,
                "expires_at": iso_or_none(grant.expires_at),
                _REPLACES: replaces,
            },
            aad=DURABLE_GIT_TOKEN_AAD,
        ),
    }


@DBOS.step(**runtime.DB_RETRY)
def persist(credential_id: str, sealed: dict[str, Any]) -> bool:
    """Open the sealed grant and write it onto the credential row, unless the row has moved on.

    True when this call wrote the grant; False when the fence held and nothing was written, meaning the
    row's current token is authoritative (`git_access.token_for` serves it).

    The fence exists because a human may reconnect before a recovery replay; an unconditional write
    would re-brick the credential. Under a fresh `FOR UPDATE` it compares the row's `token_expires_at`
    with the sealed `_REPLACES` (exact ISO equality; a cleared column reads `""` and disagrees). No
    `populate_existing`: nothing is loaded yet. Writes via `store_grant` and commits; a discarded grant's
    refresh token is already a dead end.

    `retries_allowed`: an exception would end the run ERROR (terminal for this id) with the token
    unwritten. Retrying is safe both ways: after a lost commit the write lands; after a landed one the
    fence returns False. An unopenable envelope costs three attempts; a deleted row returns False.
    """
    from app.credential_crypto import DURABLE_GIT_TOKEN_AAD, unseal
    from app.db import SessionLocal
    from app.git_access import store_grant
    from app.github_oauth import TokenGrant
    from app.models.credentials import Credential
    from app.tenancy import exclusion_log, seam

    grant_fields = unseal(
        sealed, aad=DURABLE_GIT_TOKEN_AAD, label="the git-token refresh checkpoint",
    )
    replaces = str(grant_fields.get(_REPLACES) or "")
    expires_at_raw = grant_fields.get("expires_at")
    grant = TokenGrant(
        access_token=str(grant_fields.get("access_token") or ""),
        refresh_token=grant_fields.get("refresh_token") or None,
        # `fromisoformat` on `rotate`'s string, so the aware UTC instant survives the checkpoint rather than
        # being recomputed from an expired lifetime.
        expires_at=datetime.fromisoformat(expires_at_raw) if expires_at_raw else None,
    )

    with exclusion_log.caller("durable:git_token.persist"):
        try:
            # The same derivation `rotate` makes, taken again and detached for the same reason: a replay of
            # this step alone must bind for itself (`runtime.py`).
            with seam.detached(), SessionLocal() as session, seam.bind_from(session, Credential, UUID(credential_id)):
                credential = session.get(
                    Credential, UUID(credential_id), with_for_update=True
                )
                if credential is None:
                    raise seam.TenancyRowMissing(f"credential {credential_id} is gone")
                current = str(credential.config.get("token_expires_at") or "")
                if current != replaces:
                    # The fence holding: not an error, but logged as the one event that tells an operator
                    # their reconnect was not undone. Only the row's timestamp is logged (neither is secret).
                    log.warning(
                        "git-token-refresh: credential %s moved on while its rotation was in flight "
                        "(the row now expires at %s) — the rotated grant is discarded UNWRITTEN so the "
                        "grant already on the row survives", credential_id, current or "<none>",
                    )
                    return False
                store_grant(credential, grant)
                session.commit()
        except seam.TenancyRowMissing:
            # Deleted between the rotation and this write: a no-op, and the minted token is abandoned with its
            # row. False as for the fence; `token_for`'s re-read distinguishes the two (gone raises, moved
            # serves).
            log.info(
                "git-token-refresh: credential %s was deleted before its rotated grant could be "
                "stored — the grant is discarded with it", credential_id,
            )
            return False
    log.info("git-token-refresh: credential %s now holds the rotated grant", credential_id)
    return True


def rotate_and_persist(credential_id: str) -> dict[str, Any]:
    """The two steps in order, as a plain function. What the workflow body is.

    Extracted because a `@DBOS.workflow()` needs a launched engine (never under `TestClient`), while a
    step outside a workflow runs as a plain function, so tests exercise the real steps without restating
    the order.

    `rotate` first (irreversible, checkpointed), `persist` second (recoverable). A refusal short-circuits.
    Three outcomes: ok, refusal, and the fence's `grant_superseded`.
    """
    outcome = rotate(credential_id)
    if not outcome.get("ok"):
        # The refusal travels out unchanged; `code` and `detail` are what the caller translates.
        return outcome
    if not persist(credential_id, outcome["sealed"]):
        return {"ok": False, "code": GRANT_SUPERSEDED, "detail": ""}
    # Not `outcome`: it carries the sealed envelope, and this is the workflow's recorded output.
    return {"ok": True}


@DBOS.workflow()
def refresh_git_token(credential_id: str, grant_expires_at_iso: str) -> dict[str, Any]:
    """One durable rotation of one GitHub grant.

    Imported for its decorator side effect by `app/durable/runtime.py`; an unregistered workflow cannot
    start or recover.

    `grant_expires_at_iso` is unread by the body but is half the run's identity: DBOS records inputs, so
    operators see which grant a run spent, and the id cannot be split back apart (a uuid and an ISO
    timestamp both contain the separator).

    Returns an outcome marker, never a token; the caller re-reads the row.
    """
    return rotate_and_persist(credential_id)


#: DBOS statuses of a run that is over without an outcome; its id re-raises on every attach.
_FINISHED_IN_ERROR = frozenset({"ERROR", "CANCELLED", "MAX_RECOVERY_ATTEMPTS_EXCEEDED"})


def _ended_in_error(workflow_id: str) -> bool:
    """Whether the run under `workflow_id` is finished in an error. False when the status cannot be read."""
    try:
        status = DBOS.get_workflow_status(workflow_id)
    except Exception:
        return False
    return status is not None and str(getattr(status.status, "value", status.status)) in _FINISHED_IN_ERROR


def run_refresh(credential_id: str, grant_expires_at_iso: str) -> dict[str, Any]:
    """Run one rotation under its deterministic id and return the outcome. The seam `token_for` calls.

    Raises into its caller, unlike the package's post-commit enqueue seams: the caller is blocked on a
    token, and swallowing would mean serving an expiring token or an un-checkpointed inline refresh.

    `SetWorkflowID` plus a direct call is an idempotent attach: same id, same run and outcome. Only an
    `is_retryable` refusal, or a grant's run that FINISHED in an error, leads to one further attempt, in this same call, under the window's id (at
    most two GitHub round trips); later callers in the window attach to both. Returns the retry's outcome,
    never a merge.
    """
    base_id = token_refresh_workflow_id(credential_id, grant_expires_at_iso)
    try:
        with SetWorkflowID(base_id):
            outcome = refresh_git_token(credential_id, grant_expires_at_iso)
    except Exception:
        # A run that ended ERROR (a defect, such as `rotate`'s scope conflict) is cached under the grant's id
        # for good, so every later attach re-raises it and the credential is frozen until a human reconnects
        # it, even once the defect is fixed. The window's id gives it a fresh run instead. If the ERROR came
        # after a rotation, the fresh run earns `bad_refresh_token`, the refusal that names the reconnect.
        # Only a run the engine reports FINISHED: a fault while attached to one still running (or an
        # unreachable engine) raises, so two runs never spend one single-use token.
        if not _ended_in_error(base_id):
            raise
        log.warning(
            "git-token-refresh: credential %s's run under the grant's id ended in an error — trying again "
            "under the window's id", credential_id,
        )
        outcome = None
    if outcome is not None and not is_retryable(outcome):
        return outcome

    retry_id = retry_workflow_id(credential_id, grant_expires_at_iso)
    if outcome is not None:
        log.info(
            "git-token-refresh: credential %s was refused without spending its grant (code=%s) — trying "
            "again under %s", credential_id, outcome.get("code"), retry_id,
        )
    with SetWorkflowID(retry_id):
        return refresh_git_token(credential_id, grant_expires_at_iso)
