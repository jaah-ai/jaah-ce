"""Push one decided slot intent down a node's live socket so it is enacted now rather than at the next poll.

The node's `pool-tick` drain stays the backstop, so nothing here raises or blocks and no outcome may
depend on a frame landing. The frame carries the intent in the `pool-tick` shape
(`app/routes/agent/pool_tick.py::PoolTickIntent`), so pushed and polled intents share one parser and ack path.

Send only after `session.commit()`: an overtaking frame is enacted, acked against a missing row, then
re-served and enacted twice. The loop is captured because `answer_question`/`ignore_question` run in the
threadpool, where the send must be `run_coroutine_threadsafe` onto it, never `asyncio.run` (a second loop
would touch the first's `WebSocket`). A node another control-plane peer holds is reached the way
`app/node_bus.py` reaches it: the frame is forwarded to the holder; only a node nobody holds drops it.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

from app import node_bus

log = logging.getLogger("app.node_send_intent")

# CP -> node, no reply. Twin of `apps/jaahd/node_bus.py::FRAME_SEND_INTENT`; change both or neither.
# Lives here, not in `app/node_bus.py`, because that module is payload-blind transport. A node that does
# not know the frame drops it, so adding it needed no version bump.
FRAME_SEND_INTENT = "send_intent"

# CP -> node, no reply: "your host was deleted with uninstall — remove Jaah from this machine". Twin of
# `apps/jaahd/node_bus.py::FRAME_UNINSTALL`; change both or neither. The node only drops a marker; its root
# unit acts only once the CP refuses the node's token (the row is gone), so a stray frame uninstalls nothing.
FRAME_UNINSTALL = "uninstall"

# Strong refs to in-flight send tasks: the loop holds only a weak one, so an unreferenced task can be
# GC'd mid-await. Discarded on completion.
_in_flight: set[asyncio.Task] = set()


def send_intent(node_id: Optional[str], intent: dict[str, Any]) -> None:
    """Push one decided intent to `node_id` for immediate execution. Fire-and-forget; never raises.

    `node_id` is the node's uuid as text (the `app/node_bus.py` registry key; a name repeats across orgs);
    resolution happens once, in the rail's mint (`fleet_verdicts.issue_intents`, `microvm_rail.issue`). `None`/empty is a no-op so callers may call unconditionally.
    Call after the commit that wrote the intent (see the module docstring)."""
    if not node_id or not intent:
        return

    _schedule(_send(node_id, intent), label=f"intent {intent.get('id')}")


def send_frame(node_id: Optional[str], frame: dict[str, Any]) -> None:
    """Push one payload-carrying NOTIFY frame (`{"t": <type>, ...}`) to `node_id`. Fire-and-forget; never
    raises. The non-intent twin of `send_intent` (caller: `app/vnodes.py::FRAME_CONVERGE`). Every such
    frame is a latency nudge backed by the node's own poll. `node_id` is the node's uuid as text."""
    if not node_id or not frame or not frame.get("t"):
        return
    _schedule(_notify(node_id, frame), label=f"frame {frame.get('t')}")


def _schedule(coro: Any, *, label: str) -> None:
    """Run `coro` on the captured loop from any context; the one place the second-loop hazard is handled."""
    try:
        running = asyncio.get_running_loop()
    except RuntimeError:
        # A FastAPI threadpool worker, where `answer_question`/`ignore_question` run.
        running = None

    # The captured loop wins: it owns the node's WebSocket. Falling back to the running loop keeps this
    # usable without a lifespan (tests, in-process drivers).
    loop = node_bus.serving_loop() or running
    if loop is None or loop.is_closed():
        log.debug("node-send-intent: no event loop to push %s on — the node's own drain covers it", label)
        coro.close()
        return

    try:
        if running is loop:
            task = loop.create_task(coro)
            _in_flight.add(task)
            task.add_done_callback(_in_flight.discard)
        else:
            # Future dropped on purpose: waiting would put the request thread behind a socket write,
            # and the coroutine already logs every failure.
            asyncio.run_coroutine_threadsafe(coro, loop)
    except Exception as exc:  # noqa: BLE001 — a scheduling failure must not surface as a failed mint
        coro.close()
        log.debug("node-send-intent: could not schedule %s (%s) — the node's drain covers it", label, exc)


async def _notify(node_id: str, frame: dict[str, Any]) -> None:
    """Write one arbitrary NOTIFY frame down the node's live socket (local or a peer's), or drop it."""
    conn = node_bus.registry.get(node_id)
    if conn is None:
        log.debug("node-send-intent: %s is not connected — dropping %s frame", node_id, frame.get("t"))
        return
    try:
        await conn.notify(frame)
    except node_bus.NodeNotConnected:
        # A peer-routed miss surfaces here rather than as `None` above: no live peer holds the socket.
        log.debug("node-send-intent: %s is not connected — dropping %s frame", node_id, frame.get("t"))
    except Exception as exc:  # noqa: BLE001 — see the module docstring; every failure here is tolerable
        log.debug("node-send-intent: pushing %s to %s failed (%s)", frame.get("t"), node_id, exc)


async def _send(node_id: str, intent: dict[str, Any]) -> None:
    """Write one `send_intent` frame down the node's live socket, or drop it.

    `notify`, not `request`: no correlation id, so it cannot occupy a `MAX_PENDING_PER_NODE` slot. Wire
    frame: `{"t": "send_intent", "v": 1, "intent": {...}}`."""
    conn = node_bus.registry.get(node_id)
    if conn is None:
        # Ordinary miss (offline, on another CP task, or unresolved name); the node's drain delivers it.
        log.debug("node-send-intent: %s is not connected — its own drain will pick intent %s up",
                  node_id, intent.get("id"))
        return
    try:
        await conn.notify({"t": FRAME_SEND_INTENT, "intent": intent})
        log.debug("node-send-intent: pushed intent %s (%s on %s#%s) to %s",
                  intent.get("id"), intent.get("verb"), intent.get("project"), intent.get("number"),
                  node_id)
    except node_bus.NodeNotConnected:
        # A peer-routed miss (see `_notify`); the node's drain delivers it.
        log.debug("node-send-intent: %s is not connected — its own drain will pick intent %s up",
                  node_id, intent.get("id"))
    except Exception as exc:  # noqa: BLE001 — see the module docstring; every failure here is tolerable
        log.debug("node-send-intent: pushing intent %s to %s failed (%s) — the node's drain covers it",
                  intent.get("id"), node_id, exc)


def _reset_for_tests() -> None:
    """Drop the captured loop and in-flight sends; tests build a fresh loop per case."""
    _in_flight.clear()
    node_bus.release_serving_loop()


#: What `send_uninstall` answers when no socket could carry the frame.
UNINSTALL_BY_HAND = "the machine is offline, so remove Jaah by hand: sudo apt-get purge jaah"


async def send_uninstall(node_key: str) -> Optional[str]:
    """Ask a just-deleted machine to uninstall Jaah, awaited so the caller can say whether it went. Returns
    None once the frame is on the socket, else the sentence the operator reads. Never raises: the deletion is
    committed. `node_key` is the bus key (`node_identity.bus_key`: the node's uuid)."""
    conn = node_bus.registry.get(node_key)
    if conn is None:
        return UNINSTALL_BY_HAND
    try:
        await conn.notify({"t": FRAME_UNINSTALL})
    except node_bus.NodeNotConnected:
        return UNINSTALL_BY_HAND
    except Exception as exc:  # noqa: BLE001 — see the docstring
        log.warning("node-send-intent: pushing uninstall to %s failed (%s)", node_key, exc)
        return UNINSTALL_BY_HAND
    return None
