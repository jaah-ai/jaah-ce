"""The runner-pool convergence — one node's GitHub Actions pool, as durable work.

The node re-converges every 60s, so the dispatch poke alone would not earn durability; the token burn
does: `_preflight_runner_admin` mints (and discards) a real registration token, and a crash before the
verdict would make the retry mint again. So the probe is a checkpointed step (secret-free verdict only;
`probe_with` never binds the token) and the dispatch rides along.

Probe ids extend `gha_pool_workflow_id` with the credential id, its `updated_at` revision and a time
bucket, so a retry replays the recorded verdict only for the same credential state; dispatch ids add
the enqueue instant. An unreachable engine falls back to an in-request mint; only the start is guarded
— a started run that ends badly is `incomplete`, never re-minted.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Optional

from dbos import DBOS, SetWorkflowID

from app import edition_features
from app.durable import runtime
from app.durable.runtime import enqueue_or_warn

# Own logger: failures here (engine, spent credential) are unrelated to `gha-runners`' (rejected save,
# unreadable roster).
log = logging.getLogger("gha-pool")

# How many pokes may be in flight (dispatch leg only; the probe is started directly). Small, so one
# unreachable node cannot park another's convergence. No limiter: a poke spends no vendor quota.
_CONCURRENCY = 4

GHA_POOL_QUEUE = runtime.declare_queue("gha-pool", concurrency=_CONCURRENCY, polling_interval_sec=runtime.BACKGROUND_POLL_SEC)

# The probe's replay window, in seconds: must cover a crash-and-retry (seconds) but not an operator's
# trip to GitHub to add a permission. See the module docstring.
_PROBE_WINDOW_SECONDS = 300

# The verdict arms `probe_with` returns and `routes/gha_runners.py` turns into its six refusals — named
# constants so a typo in either module cannot make an arm silently unreachable.
ARM_NEEDS_RECONNECT = "needs_reconnect"
ARM_CONFIG = "config"
ARM_UNAUTHORIZED = "unauthorized"
ARM_RATE_LIMITED = "rate_limited"
ARM_FORBIDDEN = "forbidden"
ARM_INCOMPLETE = "incomplete"


# ── The deterministic identities ──────────────────────────────────────────────────────────────────


def gha_pool_workflow_id(project_id: Any, node_id: Any) -> str:
    """The pinned base identity for this node's pool on this project. A pure seam, testable without
    DBOS. Each leg extends it (see the module docstring); every id stays greppable by pool."""
    return f"gha-pool:{project_id}:{node_id}"


def probe_window(now: Optional[float] = None) -> int:
    """The coarse bucket a probe's replay window is keyed on. Its own function so a test can pin it.

    Clock-keyed, unlike the title queue's id: a preflight verdict has a shelf life, since permissions
    change."""
    return int((time.time() if now is None else now) // _PROBE_WINDOW_SECONDS)


def credential_revision(credential: Any) -> str:
    """Which version of a credential row a verdict was gathered about — the probe id's middle
    discriminator, so an in-place token replacement is not replayed the old refusal.

    `updated_at` is maintained by the `set_updated_at` trigger, so it moves for raw-SQL
    writers too. A timestamp, not a value. Absent (unreachable against the ORM), answers `"0"`."""
    stamp = getattr(credential, "updated_at", None)
    return "0" if stamp is None else stamp.isoformat()


def probe_workflow_id(
    project_id: Any, node_id: Any, credential_id: Any, credential_rev: str, bucket: int
) -> str:
    """The id a probe and its retry must agree on — see the module docstring for the three
    discriminators.

    Carries only row ids and a revision clock, never credential contents. The uuid head and integer
    tail keep the join unambiguous despite colons in the revision."""
    return (
        f"{gha_pool_workflow_id(project_id, node_id)}"
        f":probe:{credential_id}:{credential_rev}:{bucket}"
    )


def apply_workflow_id(project_id: Any, node_id: Any, stamp: str) -> str:
    """The id one post-commit poke is enqueued under. `stamp` is the enqueue instant.

    Not a dedup key (one enqueuer per commit); the instant lets a re-save re-poke a stuck node."""
    return f"{gha_pool_workflow_id(project_id, node_id)}:apply:{stamp}"


# ── The probe ─────────────────────────────────────────────────────────────────────────────────────


def probe_with(session: Any, project: Any, credential: Any) -> dict[str, Any]:
    """Mint one runner registration token for `project`'s repository with `credential`, discard it,
    and report a secret-free verdict.

    The only place the minted value exists; never bound. The verdict is built from the exception or
    the credential id, never the response body. `_classify` is the single classifier.

    Takes resolved ORM objects and opens no session, so the durable step and the in-request fallback
    share one mint site (a fresh session in an operator-scoped request would raise — see
    `seam.bind_from`).

    `git_access.token_for` may commit the given session to persist a rotated App grant (see
    `upsert_gha_runner_pool`). Every refusal logs one line: exception type only, credential by id.
    """
    from app import forges, git_access, github_api

    owner_repo = forges.github_repo_path(project.repo_url)
    try:
        # Not bound: the answer is a live enrolment secret with no use here.
        github_api.registration_token(
            owner_repo,
            token=git_access.token_for(session, credential),
            # A probe's budget (`github_api._PROBE_TIMEOUT`).
            timeout=github_api._PROBE_TIMEOUT,
        )
    except Exception as exc:  # noqa: BLE001 — every failure is a refusal; see the docstring
        verdict = _classify(exc)
        log.warning(
            "gha-pool: preflight refused %s for credential %s (%s%s)",
            owner_repo, credential.id, type(exc).__name__,
            f" {verdict['status']}" if verdict.get("status") is not None else "",
        )
        return verdict
    return {"ok": True}


def _is_rate_limited(response: Any) -> bool:
    """Is this GitHub refusal an exhausted rate limit rather than a permission answer?

    403 means both a missing scope and a primary/secondary rate limit; only headers tell them apart
    (`x-ratelimit-remaining: 0`, `retry-after`). Read defensively: anything unreadable answers False,
    routing to the permission arm."""
    headers = getattr(response, "headers", None)
    if headers is None:
        return False
    try:
        if headers.get("retry-after") is not None:
            return True
        return str(headers.get("x-ratelimit-remaining") or "").strip() == "0"
    except Exception:  # noqa: BLE001 — an unreadable header set is not evidence of a rate limit
        return False


def _classify(exc: BaseException) -> dict[str, Any]:
    """One failed mint, read as the action it implies. Pure: no session, no network.

    `_preflight_runner_admin`'s docstring owns why each status maps as it does; this is only the
    exception-to-arm mapping, shared by step and fallback. `message` is set on exactly two arms (our
    own `git_access.GitAccessError` text, never a token); others leave it empty to keep GitHub bodies
    out of the checkpoint.
    """
    import httpx

    from app import git_access

    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    if isinstance(exc, git_access.GitAccessNeedsReconnect):
        return {"ok": False, "arm": ARM_NEEDS_RECONNECT, "status": status, "message": str(exc)}
    if isinstance(exc, git_access.GitAccessError):
        return {"ok": False, "arm": ARM_CONFIG, "status": status, "message": str(exc)}
    if isinstance(exc, httpx.HTTPStatusError) and status in (401, 403, 404, 429):
        if status == 401:
            return {"ok": False, "arm": ARM_UNAUTHORIZED, "status": status, "message": ""}
        if status == 429 or _is_rate_limited(response):
            return {"ok": False, "arm": ARM_RATE_LIMITED, "status": status, "message": ""}
        return {"ok": False, "arm": ARM_FORBIDDEN, "status": status, "message": ""}
    # The one arm where the check did not complete (transport error, 5xx). The class name travels.
    return {
        "ok": False, "arm": ARM_INCOMPLETE, "status": status, "message": "",
        "exc_type": type(exc).__name__,
    }


# Adding a workflow does not bump `runtime.ACCOUNTS_WORKFLOW_VERSION` — a bump would strand in-flight
# work; see `task_title_queue` for the self-correcting rolling-deploy residual.


@DBOS.step(retries_allowed=False)
def probe_runner_admin(project_id: int, credential_id: str) -> dict[str, Any]:
    """One mint, checkpointed, so the spend is recorded independently of the response.

    `retries_allowed=False`: every attempt is an irreversible spend, and a retry would push a
    rate-limited credential further; the operator's Save is the retry.

    Only row ids travel; the repo URL and token are read at run time. The org is derived from the
    project row (`bind_from`), since a DBOS worker thread has nothing bound and would read no rows.
    """
    from uuid import UUID

    from sqlalchemy import select

    from app.models.credentials import Credential
    from app.models.projects import Project
    from app.tenancy import seam

    with (
        runtime.step_session("durable:gha_pool.probe", gone="") as session,
        seam.bind_from(session, Project, project_id),
    ):
        # `select()`, not `session.get`: the seam's org predicate cannot reach a primary-key load.
        project = session.scalar(select(Project).where(Project.id == project_id))
        credential = session.scalar(
            select(Credential).where(Credential.id == UUID(str(credential_id)))
        )
        if project is None or credential is None:
            # Deleted between save and run: reported as the configuration arm, not raised — the
            # remedy is picking another row, and a raise would spend recovery on nothing.
            return {
                "ok": False, "arm": ARM_CONFIG, "status": None,
                "message": (
                    "The project or credential this pool names was removed while the control "
                    "plane was checking it."
                ),
            }
        # Release the pooled connection before the GitHub round trip (`seam.bind_from`). Taken
        # after `token_for`, which may need to write a rotated grant.
        session.rollback()
        return probe_with(session, project, credential)
    # The project is gone: nothing to derive or probe. Same disposition as above.
    return {
        "ok": False, "arm": ARM_CONFIG, "status": None,
        "message": "The project this pool names was removed while the control plane was "
                   "checking its credential.",
    }


# ── The dispatch ──────────────────────────────────────────────────────────────────────────────────


@DBOS.step(retries_allowed=False)
def dispatch_apply(node_id: str, actor_email: str) -> bool:
    """Poke the node once. Best effort.

    `retries_allowed=False`: `dispatch_and_audit_sync` records an incomplete dispatch rather than
    raising, so there is nothing to retry. The step buys recovery across a deploy only.

    Calls the route's own dispatch seam (imported at call time) so the audit row matches the inline
    path and a test substituting it covers both. Inherits that function's `asyncio.run` threading
    (see `node_bus.run_on_serving_loop`); fixing it belongs in `routes/commands.py`.

    The node name is read here, not passed: a name frozen across a rename would poke nobody while the
    audit claimed success.

    Every other failure is logged at WARNING and returns False — the row is the instruction and the
    node's own 60s pass applies it. `BaseException` stays uncaught.
    """
    if not edition_features.enabled("gha_runners"):
        return False  # The community edition has no GHA runners.
    from app.db import SessionLocal
    from app.models.nodes import Node
    from app.routes.gha_runners import _APPLY_COMMAND, dispatch_and_audit_sync
    from app.tenancy import exclusion_log, seam

    with exclusion_log.caller("durable:gha_pool.dispatch"):
        try:
            # Bound off the node, not the project: the bus key and audit row are the node's
            # (`app/tenancy/manifest.py`).
            with SessionLocal() as session, seam.bind_from(session, Node, node_id):
                row = session.get(Node, node_id)
                if row is None:  # pragma: no cover - `bind_from` has already refused this case
                    return False
                # `session.get` is fine here: this is the row `bind_from` just resolved. Read before the
                # rollback, which would expire it and force a round trip.
                # Release the pooled connection before the network hop (`app/db.py::_pool_kwargs`);
                # `dispatch_and_audit_sync` opens its own transaction. Addressed by uuid: names repeat across orgs.
                session.rollback()
                dispatch_and_audit_sync(session, str(node_id), _APPLY_COMMAND, {}, actor_email)
                return True
        except seam.TenancyRowMissing:
            # The node was deleted between save and poke: nothing to converge, not an error. The name is
            # gone with the row, so the line names the command.
            log.info(
                "gha-pool: node %s no longer exists — its %s poke is dropped", node_id, _APPLY_COMMAND,
            )
            return False
        except Exception as exc:  # noqa: BLE001 — see the docstring: best effort reports, never fails
            # Class name in the message, traceback in the log (as `_trigger` does). Nothing is returned
            # or checkpointed beyond the bool.
            log.warning(
                "gha-pool: the %s poke for node %s did not complete (%s) — the pool row is written and "
                "the node's own pass will converge it",
                _APPLY_COMMAND, node_id, type(exc).__name__, exc_info=True,
            )
            return False


# ── The workflow ──────────────────────────────────────────────────────────────────────────────────


@DBOS.workflow()
def converge_gha_pool(
    project_id: int,
    node_id: str,
    probe_credential_id: Optional[str] = None,
    dispatch_actor: Optional[str] = None,
) -> dict[str, Any]:
    """One node's pool, converged: prove the credential, then poke the host.

    Imported for its decorator side effect by `app/durable/runtime.py` — required for enqueue and
    recovery.

    Both legs are optional and each caller uses one: the probe runs before the route's first mutation,
    the poke after its commit, so they are two invocations under two ids. Only ids and one actor's
    email travel; everything else is re-read at run time.
    """
    verdict: dict[str, Any] = {"ok": True, "arm": "not-probed"}
    if probe_credential_id is not None:
        verdict = probe_runner_admin(project_id, probe_credential_id)
        if not verdict.get("ok"):
            # A refused probe ends the workflow successfully with the refusal as a value: a credential
            # without Administration is an answer, not a failure.
            return verdict
    if dispatch_actor is not None:
        dispatch_apply(node_id, dispatch_actor)
    return verdict


# ── The seams the route calls ─────────────────────────────────────────────────────────────────────


def probe_pool_credential(
    session: Any, project: Any, credential: Any, node_id: Any
) -> dict[str, Any]:
    """Prove `credential` can mint for `project`'s repository, durably where it can be.

    Started directly, not enqueued, because the operator is waiting. A second call in the same window
    with the same credential attaches to the recorded verdict — no second mint (`probe_workflow_id`).

    Falls back to an in-request mint when the engine is unreachable. Only the start is guarded: a
    started run that ends badly is `incomplete`, so no retry buys another token or double-spends.
    Takes ORM objects for the fallback, which cannot open a second session (`seam.bind_from`).
    """
    workflow_id = probe_workflow_id(
        project.id, node_id, credential.id, credential_revision(credential), probe_window()
    )
    try:
        with SetWorkflowID(workflow_id):
            handle = DBOS.start_workflow(
                converge_gha_pool,
                project.id,
                str(node_id),
                str(credential.id),
                None,
            )
    except Exception as exc:  # noqa: BLE001 — see the docstring: the gate must not become the outage
        log.warning(
            "gha-pool: could not start the preflight durably for credential %s (%s: %s) — minting in "
            "this request instead, which is what this route did before the workflow existed",
            credential.id, type(exc).__name__, exc,
        )
        verdict = probe_with(session, project, credential)
        verdict["durable"] = False
        return verdict
    try:
        verdict = handle.get_result()
    except Exception as exc:  # noqa: BLE001 — a run that ended badly is a verdict, never a second mint
        # Only the class name enters the verdict (a `StatementError` carries bound parameters); the log
        # takes the full traceback. No `DBOSMaxStepRetriesExceeded` unwrapping needed: both steps are
        # `retries_allowed=False` (see `durable/task_done.py::_failure_reason` if that changes).
        log.warning(
            "gha-pool: the durable preflight for credential %s did not complete (%s) — refusing the "
            "save as incomplete rather than minting again, which every retry would repeat",
            credential.id, type(exc).__name__, exc_info=True,
        )
        return {
            "ok": False, "arm": ARM_INCOMPLETE, "status": None, "message": "",
            "exc_type": type(exc).__name__, "durable": True,
        }
    verdict = dict(verdict or {})
    verdict["durable"] = True
    return verdict


def enqueue_apply(project_id: Any, node_id: Any, node_name: str, actor_email: str) -> bool:
    """Enqueue one post-commit poke. True when this call is the one that placed it.

    Never raises into the route: the desired state is already committed and the node's 60s pass
    applies it anyway. False tells `_trigger` to dispatch inline (e.g. under `TestClient`).
    `node_name` is for the log line only; the work is enqueued with ids.
    """
    from datetime import datetime, timezone

    workflow_id = apply_workflow_id(
        project_id, node_id, datetime.now(timezone.utc).isoformat()
    )
    return enqueue_or_warn(
        GHA_POOL_QUEUE, workflow_id, converge_gha_pool, project_id, str(node_id), None, actor_email,
        log=log, failure=f"gha-pool: could not enqueue the apply poke for node {node_name} ({node_id})",
        consequence="dispatching inline",
    )
