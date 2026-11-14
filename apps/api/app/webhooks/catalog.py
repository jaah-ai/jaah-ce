"""The webhook event types — read once from the event catalog's `[webhooks]` table, their single home
(`apps/docs/reference-data/events.toml`; the api image COPYs it to `/app/shared`, `shared_module.candidates`).

Each type names the `activity_events.kind` values it is made from (`kinds`); `ping` has none — only a test sends it.
A missing file is a deployment defect, logged once; it then reads as offering no type, so a create is refused (422
`unknown_event_type`) rather than the api failing to start.
"""
from __future__ import annotations

import functools
import logging
import tomllib
from dataclasses import dataclass
from typing import Final, Mapping

from app import shared_module

log = logging.getLogger("app.webhooks")

PING: Final = "ping"


@dataclass(frozen=True)
class EventType:
    type: str
    label: str
    description: str
    kinds: tuple[str, ...]


@functools.cache
def event_types() -> tuple[EventType, ...]:
    """Every type, in catalog order."""
    path = next((p for p in shared_module.candidates("events.toml", "apps/docs/reference-data") if p.is_file()), None)
    if path is None:
        log.error("events.toml is missing — the api image must COPY it; no webhook event type is offered")
        return ()
    with path.open("rb") as fh:
        raw = tomllib.load(fh)
    return tuple(
        EventType(type=e["type"], label=e["label"], description=e["description"], kinds=tuple(e.get("kinds", ())))
        for e in raw.get("webhooks", {}).get("event", [])
    )


@functools.cache
def type_by_kind() -> Mapping[str, str]:
    """`{activity kind: webhook type}`."""
    return {kind: e.type for e in event_types() for kind in e.kinds}


def known(event_type: str) -> bool:
    return any(e.type == event_type for e in event_types())


def subscribable() -> tuple[str, ...]:
    """The types an endpoint may choose: every type made from an event (`ping` is sent on demand to any endpoint)."""
    return tuple(e.type for e in event_types() if e.kinds)
