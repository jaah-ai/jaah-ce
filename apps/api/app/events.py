"""Postgres LISTEN/NOTIFY fan-out — the single event bus for console state changes.

Write paths call ``emit_console_event(...)`` to ``NOTIFY cockpit_events``; one background task per API
process holds the only ``LISTEN`` connection and fans each notification out via ``EventBroadcaster``.
Consumers subscribe to that broadcaster, never a second ``LISTEN`` holder.

Assumes a single API worker: a notification only reaches subscribers in this same process.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any, Optional
from uuid import UUID

import psycopg
from sqlalchemy import text
from sqlalchemy.orm import Session

from app import db_credentials, edition_features
from app.jaah_env import env_get

log = logging.getLogger("cockpit-events")

# One channel for the whole system; consumers filter by entity/stack.
CHANNEL = "cockpit_events"

# This API instance's stack identity, stamped onto every event. Currently unread; kept as the extension
# point for per-stack scoping. The browser relay (`routes/stream.py`) strips it — it is a host-layout
# fact a tenant must not read.
_STACK = env_get("JAAH_STACK") or os.environ.get("COMPOSE_PROJECT_NAME") or None


def emit_node_upserted(session: Session, node_id: UUID) -> None:
    """A node row changed: tell the console, so an open Nodes page refreshes itself without a reload
    (`lease_drain` finishing a drain, `node_group_reconcile` declaring a node)."""
    emit_console_event(session, entity="node", id=node_id, action="upserted")


def emit_console_event(
    session: Session,
    entity: str,
    id: Any,
    action: str,
    stack: Optional[str] = None,
    record_transition: bool = True,
    transition_extra: Optional[dict[str, Any]] = None,
    transition_org_id: Optional[UUID] = None,
    org_id: Optional[UUID] = None,
) -> None:
    """Queue a ``NOTIFY cockpit_events`` on the caller's transaction, delivered atomically on COMMIT.

    ``org_id`` is the org a collection sentinel (``id=0``) happened in. A sentinel names no row, so the
    relay (`routes/stream.py::_may_see`) serves it only to that org's subscribers and the operator
    plane; a sentinel with no org reaches the operator plane only (fail closed — a cross-org readout is
    a covert channel between tenants). Stripped from the wire. Row-naming events need no tag, except the
    superadmin-only entities `_may_see` answers by org rather than by probe (cron, node group, port-grid var).

    ``pg_notify`` is transactional, so a rolled-back write emits nothing. The payload is a bound
    parameter, never interpolated.

    For ``entity == "task"`` this also persists one ``ActivityEvent`` row on the same transaction (see
    ``user_activity.record_task_transition``). ``record_transition=False`` skips that row for a live
    signal that is not a lifecycle transition — a same-state row would be misread as a transition.
    ``transition_extra`` is merged into the row's ``data``; ignored when no row is written.
    """
    payload = json.dumps(
        {
            "entity": entity,
            # `json.dumps` cannot serialize a UUID and this runs inside the caller's write transaction,
            # so an unconverted uuid id would 500 the whole write.
            "id": str(id) if isinstance(id, UUID) else id,
            "action": action,
            "stack": stack if stack is not None else _STACK,
            # Absent rather than `null` when untagged, so "names an org" is a key test.
            **({"org_id": str(org_id)} if org_id is not None else {}),
        }
    )
    session.execute(
        text("SELECT pg_notify(:channel, :payload)"),
        {"channel": CHANNEL, "payload": payload},
    )
    if entity == "task":
        # Hurry the passes that read a task transition (`app/task_wake.py`), once this write commits.
        from app import cron_runtime, task_wake

        for activity_type in task_wake.wake_activities_for(action):
            cron_runtime.wake_after_commit(session, activity_type)
    if entity == "task" and action == "done":
        # `tasks.done_at`, the compliance seal's clock (`app/durable/compliance_seal.py`), in the transaction of
        # the `done` write itself. HERE because every `done` writer emits this event in its own transaction
        # (pinned by the task-state write-site test), whatever `record_transition` says. COALESCE: a
        # re-emitted `done` on a task already done (`revise_question`) keeps the instant it first got there.
        from sqlalchemy import func, update

        from app.models.tasks import Task

        session.execute(
            update(Task).where(Task.id == id).values(done_at=func.coalesce(Task.done_at, func.now()))
        )
        # The done workflow, on the same transaction for the same reason: it exists iff this `done` commits.
        from app.durable import task_done

        task_done.enqueue_in_transaction(session, id)
    if entity == "task" and record_transition:
        # Same transaction as the notify. Imported locally: user_activity pulls in starlette.
        from app.user_activity import record_task_transition

        # `transition_org_id` is for the `deleted` transition, recorded after the row is gone.
        record_task_transition(session, id, action, extra=transition_extra, org_id=transition_org_id)
        # A task that left the working states ends its Jaah-account sessions (`app/jaah_routing/sessions.py`).
        if edition_features.enabled("jaah_ai_credit"):
            from app.jaah_routing import sessions as jaah_sessions

            jaah_sessions.end_for_task_state(session, id)


#: A browser session changed org (`login_funnel.switch_membership`). Names the session as `(identity_id, iat)`
#: — every tab of one browser shares the cookie, so its `iat` — never an object, so it is never relayed to a
#: browser: `routes/stream.py` closes that session's open streams on it and drops it.
SESSION_SWITCHED = "session_switched"


def emit_session_switched(session: Session, identity_id: UUID, iat: int) -> None:
    """Queue the `session_switched` notification on the caller's transaction (delivered on COMMIT, like
    `emit_console_event`), so every API process's broadcaster hears it."""
    payload = json.dumps({"entity": SESSION_SWITCHED, "identity_id": str(identity_id), "iat": int(iat)})
    session.execute(text("SELECT pg_notify(:channel, :payload)"), {"channel": CHANNEL, "payload": payload})


class EventBroadcaster:
    """In-process pub/sub: fan one incoming notification out to every subscriber's queue.

    ``publish()`` never blocks: a full queue drops the event for that subscriber only. Neither the cron
    wakes nor the done workflow ride this bus: `emit_console_event` owes both to the write's own transaction.
    """

    def __init__(self, maxsize: int = 100) -> None:
        self._maxsize = maxsize
        self._subscribers: set[asyncio.Queue[dict[str, Any]]] = set()

    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=self._maxsize)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[dict[str, Any]]) -> None:
        self._subscribers.discard(queue)

    def publish(self, event: dict[str, Any]) -> None:
        for queue in self._subscribers:
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                log.warning(
                    "cockpit-events subscriber queue full — dropping %s/%s event for one subscriber",
                    event.get("entity"), event.get("action"),
                )

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)


# The one broadcaster for this API process. Endpoints import this singleton to subscribe.
broadcaster = EventBroadcaster()


async def _listen_forever(broadcaster: EventBroadcaster, stop: asyncio.Event) -> None:
    """Hold one LISTEN cockpit_events connection and republish every notification to the broadcaster.

    Reconnects with capped backoff on a dropped connection and never crash-loops; exits cleanly when
    ``stop`` is set.
    """
    backoff = 1.0
    while not stop.is_set():
        try:
            # Through `db_credentials`, not `psycopg` directly: this raw dialer bypasses the SQLAlchemy
            # chokepoint, so it must inject the rotating RDS credential itself.
            conn = await db_credentials.psycopg_connect_async(autocommit=True)
        except Exception as exc:
            log.error("cockpit-events LISTEN connect failed (%s) — retrying in %.0fs", exc, backoff)
            await _sleep_or_stop(stop, backoff)
            backoff = min(backoff * 2, 30.0)
            continue

        try:
            await conn.execute(f"LISTEN {CHANNEL}")
            log.info("cockpit-events LISTEN established on channel %s", CHANNEL)
            backoff = 1.0
            gen = conn.notifies()
            async for notify in gen:
                try:
                    event = json.loads(notify.payload)
                except json.JSONDecodeError:
                    log.warning("cockpit-events: ignoring non-JSON NOTIFY payload %r", notify.payload)
                    continue
                broadcaster.publish(event)
                if stop.is_set():
                    break
        except Exception as exc:
            log.error("cockpit-events LISTEN dropped (%s) — reconnecting in %.0fs", exc, backoff)
            await _sleep_or_stop(stop, backoff)
            backoff = min(backoff * 2, 30.0)
        finally:
            try:
                await conn.close()
            except Exception:
                pass


async def _sleep_or_stop(stop: asyncio.Event, seconds: float) -> None:
    """Sleep up to ``seconds`` but wake immediately if shutdown is requested."""
    try:
        await asyncio.wait_for(stop.wait(), timeout=seconds)
    except asyncio.TimeoutError:
        pass


async def start_listener(app) -> None:
    """FastAPI startup hook: spawn the single LISTEN task and stash its handle on app.state."""
    stop = asyncio.Event()
    app.state.console_events_stop = stop
    app.state.console_events_broadcaster = broadcaster
    app.state.console_events_task = asyncio.create_task(_listen_forever(broadcaster, stop))


async def stop_background(app, *, stop_attr: str, task_attr: str) -> None:
    """Shared lifespan shutdown: set the stop Event, cancel the Task, and swallow whatever it raises."""
    stop = getattr(app.state, stop_attr, None)
    task = getattr(app.state, task_attr, None)
    if stop is not None:
        stop.set()
    if task is not None:
        task.cancel()
        try:
            await task
        except (asyncio.CancelledError, Exception):  # noqa: BLE001
            pass


async def stop_listener(app) -> None:
    """FastAPI shutdown hook: signal the LISTEN task to stop and await its clean exit."""
    await stop_background(app, stop_attr="console_events_stop", task_attr="console_events_task")
