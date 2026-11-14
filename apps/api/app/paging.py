"""The one token-following walk every paginated listing takes — AWS, Azure, GCP, Gmail, the forges — pure, no
transport. One home because the hand-rolled copies drifted in exactly the cases it pins: a repeated token, and a token
still present after the page cap (some looped, some raised, some stopped quietly)."""
from __future__ import annotations

from collections.abc import Callable, Iterator


def follow_pages[Page](
    fetch: Callable[[str | None], tuple[Page, object]],
    *,
    max_pages: int,
    overflow: Callable[[], Exception | None],
) -> Iterator[Page]:
    """Every page of a token-paginated listing. `fetch(token)` returns one page and the token for the next (from
    `None`); anything but a non-empty `str` there is the last page, so a site that refuses a malformed token checks
    it inside `fetch` and keeps its own error. A token seen before, or one still present after `max_pages` pages,
    ends the walk by `overflow()`: an exception to raise, or `None` to stop quietly with the pages read."""
    token: str | None = None
    seen: set[str] = set()
    for _ in range(max_pages):
        page, found = fetch(token)
        yield page
        token = found if isinstance(found, str) else None
        if not token:
            return
        if token in seen:
            break
        seen.add(token)
    error = overflow()
    if error is not None:
        raise error
