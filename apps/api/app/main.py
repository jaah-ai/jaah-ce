"""Jaah API — FastAPI application factory."""
from __future__ import annotations

# FIRST, before the imports it measures: the boot's `api.exec` phase (the shell's exec, entrypoint.sh's
# worker guard, uvicorn's own import) ends here and `app.import` starts (`app/boot_phases.py`).
from app import boot_phases

boot_phases.done("api.exec")

import importlib
import logging
import os
import sys
from contextlib import asynccontextmanager

from app import edition, edition_features

# Before every shared import: `login_funnel.POLICY_HOOKS` and `ADMISSION_CHECKS` run in registration order, and the
# SSO-only orgs' hook has always preceded the IP allowlist's (registered by `app/ip_allowlist.py`, which the shared
# imports below load) — so a member of such an org signing in from outside its allowlist is told SSO is required.
# The rest of the cloud registrations load with `app/routers_cloud.py`.
if edition_features.enabled("sso"):
    from app import sso_policy  # noqa: F401  # pyright: ignore[reportUnusedImport]

from fastapi import Depends, FastAPI, Request
from fastapi.responses import JSONResponse

from app.auth import (
    SecurityHeadersMiddleware,
    SessionAuthMiddleware,
    resolve_session_secret,
    warn_if_session_cookie_is_not_secure,
)
from app.authz.catalog import Tenancy
from app.authz.declarations import always
from app.authz.enforce import HintedDenial, OtherOrgDenial
from app.authz.guard import authz_guard
from app.node_targets import SystemProjectTaskRefused
from app.config import get_settings
from app.credential_crypto import resolve_credential_key_ring
from app.credential_keys import (
    NodeVaultSealed,
    WrappingKeyUnavailable,
    resolve_previous_key_providers,
    resolve_secret_key_provider,
)
from app.dev_bypass import assert_agent_auth_posture
from app.events import start_listener, stop_listener
from app import cp_peers, ce_bootstrap, ce_caps, node_bus, worker_guard
from app.node_bus import registry as node_bus_registry
from app.ratelimit import SharedRateLimiter
from app.routes import accounts as accounts_routes
from app.routes.agent import accounts_worktrees as agent_accounts_worktrees_routes
from app.routes.agent import pool_tick as agent_pool_tick_routes
from app.routes.agent import slot_intents as agent_slot_intents_routes
from app.routes.agent import fleet_observe as agent_fleet_observe_routes
from app.routes.agent import node_config as agent_node_config_routes
from app.routes.agent import releases as agent_releases_routes
from app.routes.agent import task_reads as agent_task_reads_routes
from app.routes.agent import runner_liveness as agent_runner_liveness_routes
from app.routes.agent import task_terminal as agent_task_terminal_routes
from app.routes.agent import agent_filing as agent_filing_routes
from app.routes.agent import task_secrets as agent_task_secrets_routes
from app.routes import agent_apt as agent_apt_routes
from app.routes import agent_dist as agent_dist_routes
from app.routes import agent_rpm as agent_rpm_routes
from app.routes import agent_bootstrap as agent_bootstrap_routes
from app.routes import agent_nodevault as agent_nodevault_routes
from app.routes import agent_project_settings as agent_project_settings_routes
from app.routes import agent_crons as agent_crons_routes
from app.routes import agent_triggers as agent_triggers_routes
from app.routes import agent_peer_tasks as agent_peer_tasks_routes
from app.routes import agent_sdk as agent_sdk_routes
from app.routes import agent_sessions as agent_session_routes
from app.routes import attachments as attachments_routes
from app.routes import audit as audit_routes
from app.routes import auth as auth_routes
from app.routes import commands as commands_routes
from app.routes import automations as automations_routes
from app.routes import crons as crons_routes
from app.routes import triggers as triggers_routes
from app import console_direct
from app.routes import cp_peer as cp_peer_routes
from app.routes import ce_limits as ce_limits_routes
from app.routes import org_setup as org_setup_routes
from app.routes import assignments as assignments_routes
from app.routes import console as console_routes
from app.routes import console_runtime as console_runtime_routes
from app.routes import forge_connect as forge_connect_routes
from app.routes import git_connect as git_connect_routes
from app.routes import mfa as mfa_routes
from app.routes import password_reset as password_reset_routes
from app.routes import git_connections as git_connections_routes
from app.routes import budgets as budgets_routes
from app.routes import ip_allowlist as ip_allowlist_routes
from app.routes import org_retention as org_retention_routes
from app.routes import api_keys as api_keys_routes
from app.routes import groups as groups_routes
from app.routes import health as health_routes
from app.routes import jira as jira_routes
from app.routes.credentials import (
    bulk as credential_bulk_routes,
    connect as credential_connect_routes,
    connection_test as credential_test_routes,
    project_pool as credential_pool_routes,
    reads as credential_reads_routes,
    tree as credential_tree_routes,
    writes as credential_writes_routes,
)
from app.routes import nodevault as nodevault_routes
from app.routes import node_bus as node_bus_routes
from app.routes.nodes import (
    delete as nodes_delete,
    health as nodes_health,
    intents as nodes_intents,
    journal as nodes_journal,
    lifecycle as nodes_lifecycle,
    metrics as nodes_metrics,
    reads as nodes_reads,
    register as nodes_register,
    update as nodes_update,
)
from app.routes import task_comments as task_comments_routes
from app.routes import task_queue as task_queue_routes
from app.routes import orgs as orgs_routes
from app.routes import access as access_routes
from app.routes import access_requests as access_request_routes
from app.routes import inbox as inbox_routes
from app.routes import personas as personas_routes
from app.routes import pool_slots as pool_slots_routes
from app.routes import portfolio as portfolio_routes
from app.routes import project_env as project_env_routes
from app.routes import projects as projects_routes
from app.routes import public as public_routes
from app.routes import ce_setup as ce_setup_routes
from app.routes import ce_storage as ce_storage_routes
from app.routes import port_grid as port_grid_routes
from app.routes import sessions as sessions_routes
from app.routes import task_session_events as task_session_events_routes
from app.routes import settings as settings_routes
from app.routes import stream as stream_routes
from app.routes import tags as tags_routes
from app.routes import agents as agents_routes
from app.routes import acp_registry as acp_registry_routes
from app.routes import task_control_messages as task_control_messages_routes
from app.routes import task_page as task_page_routes
from app.routes import task_ratings as task_ratings_routes
from app.routes.tasks import (
    activity as tasks_activity_routes,
    answers as tasks_answers_routes,
    create as tasks_create_routes,
    delete as tasks_delete_routes,
    dependencies as tasks_dependencies_routes,
    dispatch as tasks_dispatch_routes,
    listing as tasks_listing_routes,
    read as tasks_read_routes,
    title as tasks_title_routes,
    transitions as tasks_transitions_routes,
    update as tasks_update_routes,
)
from app.routes import telemetry as telemetry_routes
from app.routes import tenancy_exclusions as tenancy_exclusions_routes
from app.routes import workflows as workflows_routes
from app.routes import features as features_routes
from app.routes import playbooks as playbooks_routes
from app.routes import task_templates as task_templates_routes
from app.durable import runtime as durable_runtime
from app.tenancy.exclusion_log import start_exclusion_flusher, stop_exclusion_flusher
from app.task_notify import start_task_notifier, stop_task_notifier
from app.cpu_profile import start_cpu_profiler, stop_cpu_profiler
from app import db, pool_watch, route_inventory
from app.login_funnel import SessionRefreshMiddleware
from app.stale_tab_guard import StaleTabGuardMiddleware
from app import api_keys
from app import ip_allowlist  # registers the IP allowlist's funnel hooks; its refusal's handler is below
from app.user_activity import UserActivityMiddleware


@asynccontextmanager
async def _lifespan(app: FastAPI):
    """Run this API process's actors: the LISTEN cockpit_events connection, the tenancy exclusion flusher, the CPU profiler (app/cpu_profile.py) and the pool watch (app/pool_watch.py), plus the cloud edition's own
    workers (`app/routers_cloud.py::WORKERS`).
    The startup cron projection is not one of them: the durable lifespan wrap asks the `cron-projection` queue
    for a pass once DBOS has launched (`app/durable/runtime.py::_durable_lifespan`).

    There is no leader: every actor runs on every replica and is safe to run twice (each says why in its
    own docstring). This process is also a PEER: `cp_peers.start` registers its address before
    any node can dial, because a node's socket row (`app/node_presence.py`) is keyed to that peer row, and
    the registry's shared store is attached only once the row exists. The exclusion flusher (`app/tenancy/exclusion_log.py`) is last in and first out so its
    final flush lands while the pool is up; it is per-process and its flush upserts additively.

    Node sockets close first on the way out: `registry.close_all` sends 4504 ("redial now",
    `app/routes/node_bus.py::_WS_HANDOFF`) so the fleet dials the overlapping successor at once instead of
    backing off. Under uvicorn, SIGTERM already closes WebSockets with 1012 before lifespan shutdown
    (`apps/api/entrypoint.sh`), so nodes must treat both as "redial now"; the sweep covers non-uvicorn hosts.

    The opening direct-console check only warns; it refuses nothing."""
    console_direct.warn_if_tailnet_suffix_is_unusable()
    # First, and not an actor: pins this loop as the one threadpool callers push node-bus work onto
    # (`app/node_bus.py::run_on_serving_loop`, `app/node_send_intent.py`), and the one a forwarded
    # command for a peer-held socket runs on. Must precede anything that could serve a request.
    node_bus.capture_serving_loop()
    # Then the peer row, then the store that reads it — in that order, before the first dial (docstring).
    await cp_peers.start(app)
    node_bus_registry.attach_store(node_bus.SharedSocketStore())
    await start_listener(app)
    await start_task_notifier(app)
    # The cloud edition's own workers; the community edition's tree has no `app/routers_cloud.py`.
    cloud = None if edition.IS_CE else importlib.import_module("app.routers_cloud")
    if cloud is not None:
        await cloud.start(app)
    await start_exclusion_flusher(app)
    await start_cpu_profiler(app)
    await pool_watch.start_pool_watch(app)
    try:
        yield
    finally:
        # First: this is the handoff's announcement (see the docstring).
        await node_bus_registry.close_all(node_bus_routes._WS_HANDOFF, "control plane handing off")
        await pool_watch.stop_pool_watch(app)
        await stop_cpu_profiler(app)
        await stop_exclusion_flusher(app)
        await stop_task_notifier(app)
        if cloud is not None:
            await cloud.stop(app)
        await stop_listener(app)
        # The peer row goes last among the DB actors: its delete cascades this process's socket rows, which
        # `close_all` above has already emptied, so no peer forwards here any more.
        node_bus_registry.detach_store()
        await cp_peers.stop(app)
        # Last, mirroring the capture: past this line `send_intent` reads "no loop" as a drop.
        node_bus.release_serving_loop()


# The app's own log level: INFO by default, `LOG_LEVEL` to override.
_DEFAULT_LOG_LEVEL = "INFO"


async def _wrapping_key_unavailable(request: Request, exc: WrappingKeyUnavailable) -> JSONResponse:
    """A 503 for every route that opens a credential secret (`credential_crypto.open_secret`) while KMS is
    unreachable or refusing: an outage at the key, not a caller error, and not a 500. The detail is
    fixed — the exception text names the AWS error class, which is for the log, not the caller."""
    logging.getLogger(__name__).warning("secret-wrapping key unavailable on %s: %s", request.url.path, exc)
    return JSONResponse(
        status_code=503, content={"detail": "the secret-wrapping key is unavailable; try again shortly"}
    )


async def _nodevault_sealed(request: Request, exc: NodeVaultSealed) -> JSONResponse:
    """A 409 for a route that reached a NodeVault secret it tried to open, or pointed a control-plane
    consumer at one (`app/credential_consumers.py`): the control plane holds no NodeVault key (AC-8.1), so
    this is the credential's state, not a server fault. Fixed detail, prefixed by the consumer's registry
    label when the refusal names one; never a value or a field."""
    detail = "NodeVault credential — not available to Jaah"
    consumer = getattr(exc, "consumer", None)
    return JSONResponse(status_code=409, content={"detail": f"{consumer}: {detail}" if consumer else detail})


async def _hinted_denial(request: Request, exc: HintedDenial) -> JSONResponse:
    """A guard refusal that carries the access-request hint: `{detail, request: {scope, role}}`
    (`app/access_requests.py::request_hint`). Every other denial keeps FastAPI's own `{detail}` body."""
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail, "request": exc.request})


async def _other_org_denial(request: Request, exc: OtherOrgDenial) -> JSONResponse:
    """An operator's mutation of another org's row (`app/authz/enforce.py::OtherOrgDenial`):
    `{detail, reason: "other_org", org}`, so the console can say whose row it is."""
    return JSONResponse(
        status_code=exc.status_code, content={"detail": exc.detail, "reason": exc.reason, "org": exc.org}
    )


async def _api_key_not_allowed(request: Request, exc: Exception) -> JSONResponse:
    """A key on a `@session_only` route or arm (`app/api_keys.py`), in the `{"detail", "code"}` shape."""
    return api_keys.not_allowed_response()


async def _ip_not_allowed(request: Request, exc: ip_allowlist.IpNotAllowed) -> JSONResponse:
    """A route's IP allowlist refusal, in the middleware's own 403 shape (`app/ip_allowlist.py::refused_response`)."""
    return ip_allowlist.refused_response(exc.ip)


async def _ce_cap_refusal(request: Request, exc: ce_caps.CapRefusal) -> JSONResponse:
    """A community edition cap's refusal (`app/ce_caps.py`): `{detail, kind, limit, upgrade_url}`."""
    return JSONResponse(status_code=exc.status_code, content=exc.payload())


async def _system_project_task_refused(request: Request, exc: SystemProjectTaskRefused) -> JSONResponse:
    """The flush floor's refusal (`app/node_targets.py`) answers 422 on whichever route built the row, as the create
    routes' own check does."""
    return JSONResponse(status_code=422, content={"detail": str(exc)})


# Loggers whose every record is one JSON object, written bare: a CloudWatch JSON metric filter
# (`app/login_funnel.py::security_log`), Embedded Metric Format extraction (`app/metrics_emf.py`) and the
# CPU profile's Logs Insights fields (`app/cpu_profile.py`) all match only a whole-JSON event.
_BARE_JSON_LOGGERS = frozenset({"app.security", "app.metrics", "app.profile"})


class _AppFormatter(logging.Formatter):
    """`LEVEL logger: message`, save `_BARE_JSON_LOGGERS`' lines, written bare."""

    def format(self, record: logging.LogRecord) -> str:
        return record.getMessage() if record.name in _BARE_JSON_LOGGERS else super().format(record)


def _configure_app_logging() -> None:
    """Give the ROOT logger a handler and a level, so this app's own `log.info` actually reaches the logs.

    Uvicorn configures only its own loggers, so without this every module logger falls to `lastResort`
    and drops everything below WARNING. An explicit handler rather than `basicConfig`, which is a silent
    no-op when a handler exists; the level is set unconditionally. `httpx`'s WARNING pin (set before this)
    is per-logger and survives the root going to INFO. Idempotent: never stacks a second handler."""
    root = logging.getLogger()
    if not root.handlers:
        handler = logging.StreamHandler(stream=sys.stdout)
        handler.setFormatter(_AppFormatter("%(levelname)s %(name)s: %(message)s"))
        root.addHandler(handler)
    root.setLevel(os.environ.get("LOG_LEVEL", _DEFAULT_LOG_LEVEL).upper())


_ALL_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]


@always(Tenancy.BOOTSTRAP)
async def _not_in_ce() -> JSONResponse:
    """Every request under a route prefix the community edition removed: one 404 that names Jaah Cloud."""
    return ce_caps.not_in_ce()


# Signed in or not: `app/auth.py::SessionAuthMiddleware` lets a request this route answers through without a session.
_not_in_ce.answers_not_in_ce = True  # type: ignore[attr-defined]


def _register_not_in_ce(app: FastAPI) -> None:
    """The community edition's answer to a cloud-only route: `_not_in_ce` on each removed prefix and below it.

    The prefixes are those of every feature this edition lacks (`app/edition_features.py`, rendered from
    `packages/editions/features.toml`; none in this monorepo, which is the cloud edition). A `*` segment
    matches any one path segment (`/api/orgs/*/sso`). Registered after every other route, so a shared route under
    a removed prefix (`/api/users/{user_id}/git-connections` under `/api/users`) still serves."""
    from app import edition_features

    for prefix in edition_features.REMOVED_ROUTE_PREFIXES:
        path = "/".join(f"{{_seg{n}}}" if part == "*" else part for n, part in enumerate(prefix.split("/")))
        for route in (path, path + "/{_rest:path}"):
            app.add_api_route(route, _not_in_ce, methods=_ALL_METHODS, include_in_schema=False)


def create_app() -> FastAPI:
    # Fail fast if started with more than one uvicorn worker: a peer address names ONE process to the other
    # control-plane tasks (`app/cp_peers.py`), and the LISTEN fan-out is per process
    # (worker_guard.py). Scale is more tasks, never more workers.
    worker_guard.assert_single_api_worker()

    # httpx logs every request URL at INFO, and `str(httpx.URL)` renders `user:pass@` userinfo in plaintext;
    # presigned S3 URLs also carry their signature and session token in the query. Pinned to WARNING so a
    # root-logger change cannot leak either. `httpx2` is its successor (the anthropic SDK's transport, and
    # starlette's TestClient) and logs the same request line.
    for name in ("httpx", "httpx2"):
        logging.getLogger(name).setLevel(logging.WARNING)

    _configure_app_logging()

    settings = get_settings()
    # The community edition's generated secrets, before anything reads them, and its refusal of a setting it removed
    # (`app/ce_bootstrap.py::refuse_removed_settings`); a no-op here.
    ce_bootstrap.ensure_secrets()
    resolve_session_secret()
    # Refuse to boot without the credential-store key (`app/credential_crypto.py`): a plaintext fallback
    # would make "the store is encrypted" unobservable. Raised at boot, not at the first read.
    # The whole ring: a malformed previous key is refused here too, not at the first old row.
    resolve_credential_key_ring()
    # A refusal, not a log line: with ALLOW_INSECURE_AGENT truthy every /api/agent/* route and node
    # WebSocket accepts any caller, so a non-local or non-loopback-published process must not serve
    # (`dev_bypass.non_loopback_publishes`).
    assert_agent_auth_posture()
    # And the secret-wrapping key (`app/credential_keys.py`): a KMS ARN, or a local keypair only on a
    # throwaway `APP_ENV` (local/test/ci). Config is validated here; KMS itself is first dialled on first
    # use. After the posture guard: the suites' fixture keypair is refused off a throwaway stack too, and
    # `test_dev_bypass.py` pins the posture refusal on a non-local `APP_ENV` (with a real credential key,
    # since the fixture one is refused above).
    resolve_secret_key_provider()
    resolve_previous_key_providers()
    # The compliance-manifest signing setup (`app/compliance_signing.py`): resolved and logged now, but NEVER a
    # refusal — missing config degrades the feature to the reason a seal records.
    if edition_features.enabled("compliance"):
        from app import compliance_signing

        compliance_signing.resolve_signer()
    # A warning, not a refusal; see the function's docstring.
    warn_if_session_cookie_is_not_secure()
    # The enforcement point: a global dependency, not a middleware. A middleware runs before routing and
    # would need its own path matcher, where every normalisation difference is a bypass; `authz_guard`
    # decides on the resolved route, before every route-level dependency, for `@app.get` routes too.
    # `openapi_url=None` so no unauthenticated route inventory is served, and so the flattened inventory
    # (`app/route_inventory.py::flat_routes`) holds only APIRoute/APIWebSocketRoute (`test_authz_registry.py`
    # asserts completeness with an empty skip-list).
    app = FastAPI(
        title=settings.app_title,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=_lifespan,
        dependencies=[Depends(authz_guard)],
    )

    app.add_middleware(UserActivityMiddleware)
    # Inside `SessionAuthMiddleware`: a request it refuses never reaches the sliding re-mint.
    app.add_middleware(SessionRefreshMiddleware)
    # Between the two (pinned, `app/stale_tab_guard.py`): after the session is resolved, and its `org_switched`
    # refusal never reaches the re-mint.
    app.add_middleware(StaleTabGuardMiddleware)
    app.add_middleware(SessionAuthMiddleware)
    app.add_middleware(SecurityHeadersMiddleware)
    if edition.IS_CE:
        # Outermost: a caller off this machine is refused before any other layer runs (`app/ce_local_peers.py`).
        from app.ce_local_peers import LocalPeersOnlyMiddleware

        app.add_middleware(LocalPeersOnlyMiddleware)
    app.add_exception_handler(WrappingKeyUnavailable, _wrapping_key_unavailable)
    app.add_exception_handler(NodeVaultSealed, _nodevault_sealed)
    app.add_exception_handler(HintedDenial, _hinted_denial)
    app.add_exception_handler(OtherOrgDenial, _other_org_denial)
    app.add_exception_handler(ip_allowlist.IpNotAllowed, _ip_not_allowed)
    app.add_exception_handler(api_keys.ApiKeyNotAllowed, _api_key_not_allowed)
    app.add_exception_handler(SystemProjectTaskRefused, _system_project_task_refused)
    app.add_exception_handler(ce_caps.CapRefusal, _ce_cap_refusal)

    # Shared across control-plane tasks (`rate_limit_hits`): N tasks are one budget, not N.
    app.state.public_limiter = SharedRateLimiter(
        limit=int(os.environ.get("PUBLIC_RATE_LIMIT", "600")), bucket="public"
    )

    # The `/api/agent/*` routers, in this order and before every other `/api/agent` router: static task paths
    # (`/tasks/by-id`, `/tasks/needs-attention`, `/tasks/system/open`) must match before any `/tasks/{task_id}`.
    app.include_router(agent_accounts_worktrees_routes.router)
    app.include_router(agent_pool_tick_routes.router)
    app.include_router(agent_slot_intents_routes.router)
    app.include_router(agent_fleet_observe_routes.router)
    app.include_router(agent_node_config_routes.router)
    app.include_router(agent_releases_routes.router)
    app.include_router(agent_task_reads_routes.router)
    app.include_router(agent_runner_liveness_routes.router)
    app.include_router(agent_task_terminal_routes.router)
    app.include_router(agent_filing_routes.router)
    app.include_router(agent_task_secrets_routes.router)
    app.include_router(agents_routes.router)
    app.include_router(acp_registry_routes.router)
    # The agent session endpoints: their own `/api/agent/sessions` router, same `_check_token` door.
    app.include_router(agent_session_routes.router)
    # The apt channel: re-serves the signed apt repo to a node without AWS credentials; its own auth
    # (apt cannot send a custom header).
    app.include_router(agent_apt_routes.router)
    # The dist channel: the macOS/Windows installers from the same bucket, header-token only.
    app.include_router(agent_dist_routes.router)
    # The rpm channel: the Amazon Linux dnf repo from the same bucket, the apt channel's Basic auth.
    app.include_router(agent_rpm_routes.router)
    app.include_router(agent_nodevault_routes.router)
    # The project-settings channel: the only agent-plane governance write; its module docstring holds why
    # it is safe (the operator's writer under the launcher's authority).
    app.include_router(agent_project_settings_routes.router)
    # The cron channel: a runner's Runner cron in its own project, run as the chain's executor through the
    # console's own writers (`SHAPE_CRON`).
    app.include_router(agent_crons_routes.router)
    # Its twin for event-driven triggers (`SHAPE_TRIGGER`); the secret never reaches the agent.
    app.include_router(agent_triggers_routes.router)
    # The peer-task channel: a runner reads other tasks of its own project under its launcher's view.
    app.include_router(agent_peer_tasks_routes.router)
    app.include_router(agent_sdk_routes.router)
    # The bootstrap channel: same `_check_token` door, but off the `/api/agent` plane because
    # `/api/node-bootstrap` is the one machine path the public ALB allows
    # (`app/node_bootstrap.py::BOOTSTRAP_PATH`).
    app.include_router(agent_bootstrap_routes.router)
    app.include_router(auth_routes.router)
    app.include_router(mfa_routes.router)
    app.include_router(password_reset_routes.router)
    # A second router: the permission catalog is served at `/api/permissions`, outside `/api/auth`.
    app.include_router(auth_routes.catalog_router)
    # The built-in role catalog, read-only (app/routes/access.py).
    app.include_router(access_routes.router)
    # Access requests (`/api/access/requests*`) and the bell's feed (`/api/inbox*`), access-model C7.
    app.include_router(access_request_routes.router)
    app.include_router(inbox_routes.router)
    app.include_router(groups_routes.router)
    app.include_router(budgets_routes.router)
    app.include_router(ip_allowlist_routes.router)
    app.include_router(org_retention_routes.router)
    app.include_router(api_keys_routes.router)
    app.include_router(public_routes.router)
    # The community edition's first run and attachment volume; each answers 404 in the cloud.
    app.include_router(ce_setup_routes.router)
    app.include_router(ce_storage_routes.router)
    app.include_router(settings_routes.router)
    app.include_router(port_grid_routes.router)
    # The persona catalogue: a read-only projection (app/routes/personas.py).
    app.include_router(personas_routes.router)
    app.include_router(portfolio_routes.router)
    # The slot-row write surface on portfolio_routes' `/api/worktrees` paths; different methods.
    app.include_router(pool_slots_routes.router)
    app.include_router(projects_routes.router)
    # Assignments: `/api/nodes/{node_id}/projects` + `/api/projects/{project_id}/nodes`, distinct paths.
    app.include_router(assignments_routes.router)
    # The project Environment tab's routes: distinct `/{id}/env-vars...` sub-paths, no order hazard.
    app.include_router(project_env_routes.router)
    # The task form's project-scoped Jira reads: distinct `/{id}/jira/...` sub-paths, no order hazard.
    app.include_router(jira_routes.router)
    app.include_router(commands_routes.router)
    # The node routes (`app/routes/nodes/`), one router per module, in this fixed order. Rule: a literal-path
    # GET (`/api/nodes/intents`, `/api/nodes/page`) is included before any `/api/nodes/{node_id}` GET, or the
    # param route would claim it — `intents` first, and `reads` declares `/page` above `/{node_id}`.
    app.include_router(nodes_intents.router)
    app.include_router(nodes_reads.router)
    app.include_router(nodes_health.router)
    app.include_router(nodes_journal.router)
    app.include_router(nodes_metrics.router)
    app.include_router(nodes_register.router)
    app.include_router(nodes_lifecycle.router)
    app.include_router(nodes_update.router)
    app.include_router(nodes_delete.router)
    # Orgs, the tenancy axis. Distinct paths, no order hazard.
    app.include_router(orgs_routes.router)
    app.include_router(org_setup_routes.router)
    app.include_router(ce_limits_routes.router)
    app.include_router(task_comments_routes.router)
    # The dispatch forecast (`GET /api/tasks/{task_id}/dispatch-forecast`).
    app.include_router(task_queue_routes.router)
    app.include_router(accounts_routes.router)
    # The credentials package, one router per module. Literal-path GETs (`/api/credentials/seal-key`,
    # `/types`, `/owners`, `/nodevault-state`) are declared in `reads` before `/{credential_id}`, and `reads`
    # is included first: a literal path registered after the `{credential_id}` route would 422 as a bad UUID.
    # Before the reads: their `/api/credentials/{credential_id}` would swallow `/api/credentials/tree`.
    app.include_router(credential_tree_routes.router)
    app.include_router(credential_reads_routes.router)
    app.include_router(credential_writes_routes.router)
    app.include_router(credential_bulk_routes.router)
    app.include_router(credential_pool_routes.router)
    app.include_router(credential_test_routes.router)
    app.include_router(credential_connect_routes.router)
    app.include_router(nodevault_routes.router)
    # Connect GitHub on `/api/git-connect`: its callback is a browser page reached by redirect
    # from github.com, not part of the credentials CRUD surface.
    app.include_router(git_connect_routes.router)
    app.include_router(forge_connect_routes.router)
    # Git Access per forge root (`/api/users/{id}/git-connections`), self-service.
    app.include_router(git_connections_routes.router)
    app.include_router(health_routes.router)
    app.include_router(attachments_routes.router)
    # Its agent-token half (the runner's report upload); same module to avoid an import cycle with routes/agent/task_reads.py.
    app.include_router(attachments_routes.agent_router)
    app.include_router(sessions_routes.router)
    app.include_router(task_session_events_routes.router)
    app.include_router(audit_routes.router)
    # The org boundary's exclusion log, beside the other operator audit surfaces it reads like.
    app.include_router(tenancy_exclusions_routes.router)
    app.include_router(stream_routes.router)
    app.include_router(console_routes.router)
    # The console's chat view and live control of a slot's ACP run.
    app.include_router(console_runtime_routes.router)
    # The peer surface: what one control-plane task presents to another, under `/api/agent/`
    # so the session middleware, the public ALB deny and nginx treat it as machine surface already.
    app.include_router(cp_peer_routes.router)
    app.include_router(node_bus_routes.router)
    app.include_router(tags_routes.router)
    app.include_router(automations_routes.router)
    app.include_router(crons_routes.router)
    app.include_router(triggers_routes.router)
    # The Task rating pair on `/api/tasks`. Declared before `tasks_routes`: they cannot shadow each other,
    # but narrower static paths first keeps it so.
    app.include_router(task_ratings_routes.router)
    app.include_router(task_control_messages_routes.router)
    # The detail page's cold load, composed from the routes above and below (`app/routes/task_page.py`).
    app.include_router(task_page_routes.router)
    # The `app/routes/tasks/` package, one router per domain module, all on `/api/tasks`. Order rule: every
    # router declaring a literal `GET /api/tasks/<word>` path (`listing`: `/models`, `/sources`, `/tag-counts`,
    # `/creators`, `/tree`, `/counts`, `/personas`) precedes the one declaring `GET /api/tasks/{task_id}`
    # (`read`) — the `int` param does not fall through, so a literal registered after it is a 422. The
    # pre-tasks includes above stay ahead of all of these.
    app.include_router(tasks_create_routes.router)
    app.include_router(tasks_update_routes.router)
    app.include_router(tasks_title_routes.router)
    app.include_router(tasks_delete_routes.router)
    app.include_router(tasks_listing_routes.router)
    app.include_router(tasks_read_routes.router)
    app.include_router(tasks_dispatch_routes.router)
    app.include_router(tasks_answers_routes.router)
    app.include_router(tasks_transitions_routes.router)
    app.include_router(tasks_dependencies_routes.router)
    app.include_router(tasks_activity_routes.router)
    app.include_router(telemetry_routes.router)
    # The durable-workflow read surface; `cron_tick` is deliberately absent (app/workflow_registry.py).
    app.include_router(workflows_routes.router)
    app.include_router(features_routes.router)
    # The Playbooks sandbox (isolated: nothing that runs a task reads it — app/routes/playbooks.py).
    app.include_router(playbooks_routes.router)
    # Task templates: the gallery, the manage list and the editor (app/routes/task_templates.py).
    app.include_router(task_templates_routes.router)
    if not edition.IS_CE:
        from app import routers_cloud

        routers_cloud.include(app)

    # `async def` is required: a sync handler runs on AnyIO's shared thread pool (sized to the DB pool,
    # `app/pool_watch.py::apply_thread_limit`), where a saturated DB pool parks callers for `pool_timeout`,
    # so even a no-I/O probe cannot get a thread. The ALB kills the task after 3 missed 5s/2s probes.
    # Necessary but not sufficient: the global `authz_guard` must also decide on the event loop
    # (`app/authz/enforce.py::_decides_without_io`). Not served from nginx: this is the service's only liveness
    # signal, and it must reach Python to notice a wedged uvicorn.
    # The one thing it does report: a pool `pool_watch.unhealthy()` — wedged (every connection out and none
    # returned, for longer than any burst) or starved (full while checkouts keep timing out) — is a wedged
    # task, answered 503 so ECS replaces it — a memory read, never a checkout, so the probe itself can never
    # be what saturates the pool. The body says no more than the status: how long is in the `pool watch:`
    # log line, not on the one unauthenticated route.
    @app.get("/api/health")
    @always(Tenancy.BOOTSTRAP)
    async def health() -> JSONResponse:
        if pool_watch.unhealthy():
            return JSONResponse({"status": "wedged"}, status_code=503)
        return JSONResponse({"status": "ok"})

    if edition.IS_CE:
        _register_not_in_ce(app)

    # Last, over every route: a sync endpoint's route connection is back in the pool before its response is
    # validated on another thread (`app/db.py::release_after_endpoint`).
    db.release_after_endpoint(dependant for _route, dependant in route_inventory.dispatched_dependants(app))
    return app


app = create_app()

# The durable-workflow runtime, bound to the app that is actually served. Not inside
# `create_app()`: DBOS launch runs startup recovery and belongs to the app whose lifespan really runs,
# and tests that build their own app skip DBOS construction entirely.
durable_runtime.init(app)
boot_phases.done("app.import")
