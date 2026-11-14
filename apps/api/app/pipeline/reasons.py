"""Why a step waits: the ONE judging contract (architecture §4.4). A step's evaluators each yield a `Reason`; the
gate's state is the first blocking one, and the Tasks pipeline shows the whole list (B4), so enforcement and display
never disagree. Task steps: `app/pipeline/gate.py::reasons`; release steps: `app/releases/judge.py::reasons`.

The wire shape of each kind (`out`) is the `WaitReason` union in `apps/web/src/lib/pipelineBoard.ts`.
"""
from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any, Optional

#: Display order; the first blocking reason in it is the step's chip and its gate state.
KINDS = ("freeze_now", "freeze", "paused", "verify", "press", "approvals", "order", "soak", "hours", "sign_off",
         "instructions", "timeout", "freeze_soon")
#: Kinds that never hold a step: shown beside the blocking ones.
ADVISORY = frozenset({"instructions", "timeout", "freeze_soon"})


@dataclass(frozen=True)
class Reason:
    kind: str
    blocking: bool
    #: The wire fields of this kind (`apps/web/src/lib/pipelineBoard.ts`'s `WaitReason`).
    data: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.kind not in KINDS:
            raise ValueError(f"unknown wait reason {self.kind!r}")


def of(kind: str, **data: Any) -> Reason:
    return Reason(kind, kind not in ADVISORY, data)


def ordered(reasons: Iterable[Optional[Reason]]) -> list[Reason]:
    """The reasons present, in display order (`KINDS`)."""
    return sorted((r for r in reasons if r is not None), key=lambda r: KINDS.index(r.kind))


def first_blocking(reasons: Iterable[Optional[Reason]]) -> Optional[Reason]:
    return next((r for r in ordered(reasons) if r.blocking), None)


def out(reason: Reason) -> dict[str, Any]:
    return {"kind": reason.kind, **reason.data}
