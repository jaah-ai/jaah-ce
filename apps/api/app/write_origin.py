"""What caused the current write: the ONE context variable every Stripe write, billing notice and system email is
stamped with (`billing_stripe_writes.origin`, `billing_notices.origin`, `email_deliveries.origin`).

The label names the entry point, never a person or an id:
* `route:<METHOD> <path template>` — set by the global authz dependency (`app/authz/guard.py`) for every request;
* `cron:<activity type>` — set by the cron runner around each activity (`app/cron_runtime.py`);
* `event:<stripe event type>` — set by the billing event processor around each event's handlers;
* `durable:<step>` — set by `app/durable/runtime.py::step_session` for a DBOS step;
* `migration:<revision>` — for a data migration that would ever reach billing code (none may: a test forbids
  `app.billing` imports from a revision file).

The innermost one wins (an event processed inside the poll cron is `event:<type>`). Nothing set is `UNKNOWN`, and a
Stripe write recorded `unknown` is a reconcile finding (`app/billing/reconcile.py`): every entry point that can
reach Stripe must name itself.
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from typing import Final, Iterator, Optional

UNKNOWN: Final = "unknown"
#: Kept short enough for a log column and a screen; a longer label is cut.
_MAX: Final = 200
#: The label families, in the order the Stripe log's Source filter offers them.
FAMILIES: Final = ("route", "cron", "event", "durable", "migration", UNKNOWN)

_ORIGIN: ContextVar[Optional[str]] = ContextVar("write_origin", default=None)


def current() -> str:
    """The label of the current context, or `UNKNOWN`."""
    return _ORIGIN.get() or UNKNOWN


def family(label: str) -> str:
    """`route` for `route:GET /api/x`; `unknown` for anything outside `FAMILIES`."""
    head = label.split(":", 1)[0]
    return head if head in FAMILIES else UNKNOWN


def _fit(label: str) -> Optional[str]:
    label = (label or "").strip()
    if not label:
        return None
    return label if len(label) <= _MAX else label[: _MAX - 1] + "…"


def bind(label: str) -> None:
    """Attribute the rest of this context to `label`. From an `async def` on the request's own task only (a sync
    dependency runs on a copied context in a worker thread, so the set dies with it)."""
    _ORIGIN.set(_fit(label))


@contextmanager
def scope(label: str) -> Iterator[None]:
    """Attribute every write inside the block to `label`; whatever was there is restored on exit."""
    token = _ORIGIN.set(_fit(label))
    try:
        yield
    finally:
        _ORIGIN.reset(token)
