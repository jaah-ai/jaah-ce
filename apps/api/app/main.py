"""Jaah API — FastAPI application factory."""
from __future__ import annotations

# FIRST, before the imports it measures: the boot's `api.exec` phase (the shell's exec, entrypoint.sh's
# worker guard, Granian's own import) ends here and `app.import` starts (`app/boot_phases.py`).
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

from app.edge_viewer import EdgeViewerMiddleware
from app.server_timing import ServerTimingMiddleware
from app.auth import (
    SecurityHeadersMiddleware,
    SessionAuthMiddleware,
    resolve_session_secret,
    warn_if_session_cookie_is_not_secure,
)
from app.authz.catalog import Tenancy
from app.authz.declarations import always
from app.authz.enforce import OtherOrgDenial
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
from app.dev_bypass import assert_agent_auth_posture, is_fixture_env
from app.events import start_listener, stop_listener
from app import cp_peers, ce_bootstrap, ce_caps, node_bus, worker_guard
from app.node_bus import registry as node_bus_registry
from app.ratelimit import SharedRateLimiter
from app import console_direct
from app.routes import node_bus as node_bus_routes
from app.durable import runtime as durable_runtime
from app.tenancy.exclusion_log import start_exclusion_flusher, stop_exclusion_flusher
from app.task_notify import start_task_notifier, stop_task_notifier
from app.cpu_profile import start_cpu_profiler, stop_cpu_profiler
from app import db_async, gc_watch, loop_debug, loop_watch, offload, pool_watch, route_inventory
from app.login_funnel import SessionRefreshMiddleware
from app.idempotency import IdempotencyMiddleware
from app.stale_tab_guard import StaleTabGuardMiddleware
from app import api_keys
from app import ip_allowlist  # registers the IP allowlist's funnel hooks; its refusal's handler is below
from app.conditional_get import ConditionalGetMiddleware
from app.user_activity import UserActivityMiddleware
from app.proxy_headers import ProxyHeadersMiddleware
from app import ingress_surface
from app.ingress_surface import IngressSurfaceMiddleware
from app.machine_access_log import MachineAccessLogMiddleware
from app.body_cap import BodyCapMiddleware


@asynccontextmanager
async def _lifespan(app: FastAPI):
    """Run this API process's actors: the LISTEN cockpit_events connection, the tenancy exclusion flusher, the CPU profiler (app/cpu_profile.py), the pool watch (app/pool_watch.py) and the loop watch (app/loop_watch.py), plus the cloud edition's own
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
    backing off. The sweep is the announcement, whatever the server itself does to open sockets on SIGTERM.

    The opening direct-console check only warns; it refuses nothing.

    First of all, the serving process's own checks: every import is done, so a C extension that switched the GIL
    back on refuses the boot here (`app/worker_guard.py::assert_gil_off`), so does a thread that would inherit
    its creator's tenancy scope (`assert_threads_start_clean`), and dev logs slow loop callbacks
    (`app/loop_debug.py`)."""
    worker_guard.assert_gil_off()
    worker_guard.assert_threads_start_clean()
    loop_debug.configure()
    console_direct.warn_if_tailnet_suffix_is_unusable()
    # First, and not an actor: pins this loop as the one threadpool callers push node-bus work onto
    # (`app/node_bus.py::run_on_serving_loop`, `app/node_send_intent.py`), and the one a forwarded
    # command for a peer-held socket runs on. Must precede anything that could serve a request.
    node_bus.capture_serving_loop()
    # The async pool (`app/db_async.py`) belongs to this loop: built here, before anything serves or samples it.
    await db_async.start()
    # Error tracking, OFF until the operator sets a DSN; read once here, so a change applies on restart.
    await _start_error_tracking()
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
    await loop_watch.start_loop_watch(app)
    # Last before serving: the startup heap is complete, so freeze it and set the thresholds (`app/gc_watch.py::tune`).
    # Not in the fixture env: a test suite enters this lifespan once per client, and each freeze would pin its heap.
    if not is_fixture_env():
        gc_watch.tune()
    try:
        yield
    finally:
        # First: this is the handoff's announcement (see the docstring).
        await node_bus_registry.close_all(node_bus_routes._WS_HANDOFF, "control plane handing off")
        await loop_watch.stop_loop_watch(app)
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
        await db_async.stop()
        offload.shutdown()


async def _start_error_tracking() -> None:
    """`app/error_tracking.py::start`, off the event loop: it reads an operator setting on a sync session."""
    from fastapi.concurrency import run_in_threadpool

    from app import error_tracking

    await run_in_threadpool(error_tracking.start)


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

    Granian configures only its own loggers, so without this every module logger falls to `lastResort`
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


#: Every edition's routers (`route_inventory.include_all` specs), in precedence order; the cloud's follow
#: (`routers_cloud.ROUTERS`).
ROUTERS: tuple[str, ...] = (
    # The `/api/agent/*` routers, in this order and before every other `/api/agent` router: static task paths
    # (`/tasks/by-id`) must match before any `/tasks/{task_id}`.
    "routes.agent.accounts_worktrees", "routes.agent.pool_tick", "routes.agent.slot_intents",
    "routes.agent.fleet_observe", "routes.agent.node_config", "routes.agent.releases", "routes.agent.task_reads",
    "routes.agent.runner_liveness", "routes.agent.task_terminal", "routes.agent.agent_filing",
    "routes.agent.task_secrets", "routes.agents", "routes.acp_registry", "routes.agent_marketplace",
    # The agent session endpoints: their own `/api/agent/sessions` router, same `_check_token` door.
    "routes.agent_sessions",
    # The apt channel: re-serves the signed apt repo to a node without AWS credentials; its own auth
    # (apt cannot send a custom header).
    "routes.agent_apt",
    # The dist channel: the macOS/Windows installers from the same bucket, header-token only.
    "routes.agent_dist",
    # The rpm channel: the Amazon Linux dnf repo from the same bucket, the apt channel's Basic auth.
    "routes.agent_rpm", "routes.agent_nodevault",
    # The project-settings channel: the only agent-plane governance write; its module docstring holds why
    # it is safe (the operator's writer under the launcher's authority).
    "routes.agent_project_settings",
    # The cron channel: a runner's Runner cron in its own project, run as the chain's executor through the
    # console's own writers (`SHAPE_CRON`).
    "routes.agent_crons",
    # The outcome scan's toolbox calls (`app/outcome_ledger/scan.py`); the ledger is kept in every edition.
    "routes.agent_outcome_scan",
    # The deploy probe run's toolbox calls (`app/production_qa/probes.py`); Production QA ships in every edition.
    "routes.agent_deploy_probe",
    # The release executor's toolbox calls and jaah-forge's release-PR question (`app/releases/runs.py`).
    "routes.agent_release",
    # The node gate driver's results (`app/pipeline_gates/results.py`), for the lane and the release executor.
    "routes.agent_pipeline_gates",
    # Its twin for event-driven triggers (`SHAPE_TRIGGER`); the secret never reaches the agent.
    "routes.agent_triggers",
    # The peer-task channel: a runner reads other tasks of its own project under its launcher's view.
    "routes.agent_peer_tasks",
    # The org-wide channels, as the launcher (`routes/agent/auth.py::launcher_principal`): the task board and the
    # org reads + notify. Before the SDK router, whose `/tasks/{task_id}/notify` would catch `/tasks/org/notify`.
    "routes.agent_task_board", "routes.agent_credentials", "routes.agent_automations", "routes.agent_org",
    # The runner-pin write, as the launcher (an operator); its module docstring holds the one loop-guard exception.
    "routes.agent_gha_runner_pin",
    "routes.agent_sdk",
    # The bootstrap channel: same `_check_token` door, but off the `/api/agent` plane because
    # `/api/node-bootstrap` is the one machine path the public ALB allows
    # (`app/node_bootstrap.py::BOOTSTRAP_PATH`).
    "routes.agent_bootstrap", "routes.auth", "routes.mfa", "routes.password_reset",
    # A second router: the permission catalog is served at `/api/permissions`, outside `/api/auth`.
    "routes.auth:catalog_router",
    # The built-in role catalog, read-only (app/routes/access.py).
    "routes.access",
    # The bell's feed (`/api/inbox*`).
    "routes.inbox", "routes.notification_channels", "routes.trackers", "routes.project_trackers", "routes.webhooks", "routes.budgets", "routes.ip_allowlist", "routes.org_retention", "routes.api_keys",
    "routes.public", "routes.public:well_known_router",
    # The community edition's first run and attachment volume; each answers 404 in the cloud.
    "routes.ce_setup", "routes.ce_storage", "routes.settings",
    # The persona catalogue: a read-only projection (app/routes/personas.py).
    "routes.personas", "routes.portfolio", "routes.production_qa", "routes.production_qa:deploys_router", "routes.releases", "routes.pipeline_guardrails", "routes.pipeline_templates", "routes.pipeline_board", "routes.outcome_links", "routes.review_findings",
    # The slot-row write surface on portfolio_routes' `/api/worktrees` paths; different methods.
    "routes.pool_slots", "routes.projects",
    # The nodes a task pin may name for a project: `/api/projects/{project_id}/runnable-nodes`, a distinct path.
    "routes.project_nodes",
    # The project Environment tab's routes: distinct `/{id}/env-vars...` sub-paths, no order hazard.
    "routes.project_env",
    "routes.commands",
    # The node routes (`app/routes/nodes/`), one router per module, in this fixed order. Rule: a literal-path
    # GET (`/api/nodes/intents`, `/api/nodes/page`) is included before any `/api/nodes/{node_id}` GET, or the
    # param route would claim it — `intents` first, and `reads` declares `/page` above `/{node_id}`.
    "routes.nodes.intents", "routes.nodes.reads", "routes.nodes.health", "routes.nodes.journal",
    "routes.nodes.metrics", "routes.nodes.register", "routes.nodes.lifecycle", "routes.nodes.update",
    "routes.nodes.delete", "routes.nodes.restart",
    # Orgs, the tenancy axis. Distinct paths, no order hazard.
    "routes.orgs", "routes.org_setup", "routes.ce_limits", "routes.task_comments",
    # The dispatch forecast (`GET /api/tasks/{task_id}/dispatch-forecast`).
    "routes.task_queue", "routes.accounts",
    # The credentials package, one router per module. Literal-path GETs (`/api/credentials/seal-key`,
    # `/types`, `/owners`, `/nodevault-state`) are declared in `reads` before `/{credential_id}`, and `reads`
    # is included first: a literal path registered after the `{credential_id}` route would 422 as a bad UUID.
    # Before the reads: their `/api/credentials/{credential_id}` would swallow `/api/credentials/tree`.
    "routes.credentials.tree", "routes.credentials.reads", "routes.credentials.writes", "routes.credentials.bulk",
    "routes.credentials.project_pool", "routes.credentials.mcp_links", "routes.credentials.connection_test",
    "routes.credentials.connect",
    # The integration catalog's gallery, logos and one-click Connect start (its callback is the connect router's).
    # Custom connectors: before the catalog router, so `/api/integrations/custom/...` is never read as a vendor slug.
    "routes.integrations_custom", "routes.integrations", "routes.nodevault",
    # Connect GitHub on `/api/git-connect`: its callback is a browser page reached by redirect
    # from github.com, not part of the credentials CRUD surface.
    "routes.git_connect",
    # Git Access per forge root (`/api/users/{id}/git-connections`), self-service.
    "routes.git_connections", "routes.health", "routes.attachments",
    # Its agent-token half (the runner's report upload); same module to avoid an import cycle with routes/agent/task_reads.py.
    "routes.attachments:agent_router", "routes.sessions", "routes.task_session_events", "routes.audit",
    # Audit > Client errors: what flagged console sessions report (`app/client_events.py`).
    "routes.client_events",
    # The org boundary's exclusion log, beside the other operator audit surfaces it reads like.
    "routes.tenancy_exclusions", "routes.stream", "routes.read_relay", "routes.console",
    # The console's chat view and live control of a slot's ACP run.
    "routes.console_runtime",
    # The peer surface: what one control-plane task presents to another, under `/api/agent/`
    # so the session middleware, the public ALB deny and nginx treat it as machine surface already.
    "routes.cp_peer", "routes.node_bus", "routes.tags", "routes.automations", "routes.crons", "routes.triggers",
    # The Task rating pair on `/api/tasks`. Declared before `tasks_routes`: they cannot shadow each other,
    # but narrower static paths first keeps it so.
    "routes.task_ratings",
    # The task page's read-only Routing panel (`GET /api/tasks/{id}/routing`).
    "routes.routing", "routes.routing:preview_router", "routes.task_control_messages",
    # The detail page's cold load, composed from the routes above and below (`app/routes/task_page.py`).
    "routes.task_page",
    # The `app/routes/tasks/` package, one router per domain module, all on `/api/tasks`. Order rule: every
    # router declaring a literal `GET /api/tasks/<word>` path (`listing`: `/models`, `/sources`, `/tag-counts`,
    # `/creators`, `/tree`, `/counts`, `/personas`) precedes the one declaring `GET /api/tasks/{task_id}`
    # (`read`) — the `int` param does not fall through, so a literal registered after it is a 422. The
    # pre-tasks includes above stay ahead of all of these.
    "routes.tasks.create", "routes.tasks.update", "routes.tasks.title", "routes.tasks.delete", "routes.tasks.listing",
    "routes.tasks.read", "routes.tasks.dispatch", "routes.tasks.answers", "routes.tasks.question_drafts", "routes.tasks.transitions",
    "routes.tasks.dependencies", "routes.tasks.activity", "routes.telemetry",
    # The durable-workflow read surface; `cron_tick` is deliberately absent (app/workflow_registry.py).
    "routes.workflows", "routes.features",
    # The Playbooks sandbox (isolated: nothing that runs a task reads it — app/routes/playbooks.py).
    "routes.playbooks",
    # Task templates: the gallery, the manage list and the editor (app/routes/task_templates.py).
    "routes.task_templates",
)


def create_app() -> FastAPI:
    # Fail fast if started with more than one Granian worker: a peer address names ONE process to the other
    # control-plane tasks (`app/cp_peers.py`), and the LISTEN fan-out is per process
    # (worker_guard.py). Scale is more tasks, never more workers.
    worker_guard.assert_single_api_worker()
    # On ECS the public ALBs dial this process directly: never start there with the ingress guard inert.
    ingress_surface.assert_configured_on_ecs()

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

    # Innermost: it fingerprints the handler's own 200 body for the node rosters (`app/conditional_get.py`).
    app.add_middleware(ConditionalGetMiddleware)
    app.add_middleware(UserActivityMiddleware)
    # Inside `SessionAuthMiddleware`: a request it refuses never reaches the sliding re-mint.
    app.add_middleware(SessionRefreshMiddleware)
    # Inside the stale-tab guard and outside the re-mint and the activity trail (pinned, `app/idempotency.py`): a
    # refused request never claims a key, and a replayed write re-mints nothing and records no second row.
    app.add_middleware(IdempotencyMiddleware)
    # Between the two (pinned, `app/stale_tab_guard.py`): after the session is resolved, and its `org_switched`
    # refusal never reaches the re-mint.
    app.add_middleware(StaleTabGuardMiddleware)
    app.add_middleware(SessionAuthMiddleware)
    app.add_middleware(SecurityHeadersMiddleware)
    # Outermost but the edge-viewer rewrite and the CE peer gate: its clock covers every layer this process spends on
    # a request (`app/server_timing.py`).
    app.add_middleware(ServerTimingMiddleware)
    # Outside every layer that reads the client address: through the CloudFront edge the client becomes the
    # viewer, and the edge's origin secret is stripped (`app/edge_viewer.py`).
    app.add_middleware(EdgeViewerMiddleware)
    if edition.IS_CE:
        # Outermost: a caller off this machine is refused before any other layer runs (`app/ce_local_peers.py`).
        from app.ce_local_peers import LocalPeersOnlyMiddleware

        app.add_middleware(LocalPeersOnlyMiddleware)
    # nginx's body ceiling, where no nginx fronts the API (`app/body_cap.py`); inside the access line, so a 413 is logged.
    app.add_middleware(BodyCapMiddleware)
    # Directly inside the guard: nginx's access line for the token-only surface, which the node-token alarm counts,
    # wherever no nginx fronts the API (`app/machine_access_log.py`).
    app.add_middleware(MachineAccessLogMiddleware)
    # Directly inside the proxy-header rewrite: it decides on the ORIGINAL TCP peer that layer keeps, before any other
    # layer or handler runs; inert until `JAAH_INGRESS_INTERNAL_CIDRS` is set (`app/ingress_surface.py`).
    app.add_middleware(IngressSurfaceMiddleware)
    # OUTERMOST, the CE peer gate included: the client's real address behind nginx (`app/proxy_headers.py`), so
    # every layer below and every per-client limit (`app/routes/public.py::check_rate_limit`) keys on the caller,
    # never on nginx's loopback hop. Outside the CE gate too, which therefore sees the same rewritten peer it saw
    # when the server itself did this rewrite.
    app.add_middleware(ProxyHeadersMiddleware)
    app.add_exception_handler(WrappingKeyUnavailable, _wrapping_key_unavailable)
    app.add_exception_handler(NodeVaultSealed, _nodevault_sealed)
    app.add_exception_handler(OtherOrgDenial, _other_org_denial)
    app.add_exception_handler(ip_allowlist.IpNotAllowed, _ip_not_allowed)
    app.add_exception_handler(api_keys.ApiKeyNotAllowed, _api_key_not_allowed)
    app.add_exception_handler(SystemProjectTaskRefused, _system_project_task_refused)
    app.add_exception_handler(ce_caps.CapRefusal, _ce_cap_refusal)

    # Shared across control-plane tasks (`rate_limit_hits`): N tasks are one budget, not N.
    app.state.public_limiter = SharedRateLimiter(
        limit=int(os.environ.get("PUBLIC_RATE_LIMIT", "600")), bucket="public"
    )

    route_inventory.include_all(app, ROUTERS)
    if not edition.IS_CE:
        from app import routers_cloud

        routers_cloud.include(app)

    # `async def` is required: a sync handler runs on AnyIO's shared thread pool (sized to the DB pool,
    # `app/pool_watch.py::apply_thread_limit`), where a saturated DB pool parks callers for `pool_timeout`,
    # so even a no-I/O probe cannot get a thread. The ALB kills the task after 3 missed 5s/2s probes.
    # Necessary but not sufficient: the global `authz_guard` must also decide on the event loop
    # (`app/authz/enforce.py::_decides_without_io`). Not served from nginx: this is the service's only liveness
    # signal, and it must reach Python to notice a wedged server.
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

    # Last, over every route: an endpoint's `DbDep` connection is back before its dependencies are torn down
    # (`app/db_async.py::release_after_async_endpoint`).
    db_async.release_after_async_endpoint(
        dependant for _route, dependant in route_inventory.dispatched_dependants(app)
    )
    return app


app = create_app()

# The durable-workflow runtime, bound to the app that is actually served. Not inside
# `create_app()`: DBOS launch runs startup recovery and belongs to the app whose lifespan really runs,
# and tests that build their own app skip DBOS construction entirely.
durable_runtime.init(app)
boot_phases.done("app.import")
