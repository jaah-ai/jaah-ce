"""Text collapsed onto one line and bounded to N characters, saying so when cut.

One line because an embedded newline forges structure wherever the text is quoted at column 0 (a second
ledger row, a numbered Slack option, a digest paragraph). The marker is charged against `limit`, so the
result never exceeds it and a column sized to it holds; a limit shorter than the marker is a hard cut, never
a raise, since this runs on alert and notice paths. None reads as "".
"""

from __future__ import annotations


def one_line(text: object, limit: int, *, marker: str = "…") -> str:
    flat = " ".join(("" if text is None else str(text)).split())
    if len(flat) <= limit:
        return flat
    room = limit - len(marker)
    return flat[:limit] if room <= 0 else flat[:room].rstrip() + marker
