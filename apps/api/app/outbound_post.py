"""A bounded, signed-nothing HTTPS POST to a customer's own URL, shared by the SIEM export (`app/siem_export.py`,
cloud only) and outgoing webhooks (`app/webhooks/engine.py`, every edition): the URL's form and public-address
check, where a new cursor over `activity_events` starts, the POST to addresses `http_bounded.vetted_addresses`
vetted, and a refusing answer as one readable line.
"""
from __future__ import annotations

import asyncio
import re
from typing import Final, Optional, Sequence
from uuid import UUID

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app import http_bounded
from app.models.audit import ActivityEvent

#: One POST's wall-clock bound: DNS, connect, TLS, headers and body together.
POST_TIMEOUT_SECONDS: Final = 10.0
#: How much of an answer is read: enough to show why it refused.
ANSWER_BYTES: Final = 1024
#: How much of a refusing answer is shown.
ERROR_BODY_CHARS: Final = 300
MAX_URL_CHARS: Final = 2048

#: One sentence for unresolvable and private alike: telling them apart would map the control plane's own DNS.
NOT_PUBLIC: Final = "The address is not a public address the control plane can reach. Use a public https:// address."


def url_refusal(url: str) -> Optional[str]:
    """Why `url` cannot be a destination, as a sentence naming what to do; None when it can. Resolves DNS."""
    why = form_refusal(url)
    if why is None and http_bounded.public_destination_refusal(url.strip()):
        return NOT_PUBLIC
    return why


def form_refusal(url: str) -> Optional[str]:
    """`url_refusal` short of the address check."""
    url = url.strip()
    if not url:
        return "Enter the destination's https:// address."
    if len(url) > MAX_URL_CHARS:
        return f"The address is over {MAX_URL_CHARS} characters. Use a shorter one."
    if not url.lower().startswith("https://"):
        return "The address must start with https://. The export sends only over HTTPS."
    return None


def starting_cursor(session: Session, org_id: UUID) -> int:
    """Where a new destination starts: after the org's highest event id, so it receives what happens next rather
    than the org's whole history at once."""
    return int(session.scalar(select(func.max(ActivityEvent.id)).where(ActivityEvent.org_id == org_id)) or 0)


async def post_async(url: str, body: bytes, headers: dict[str, str], addresses: Sequence[str]) -> tuple[int, bytes]:
    """POST to the vetted `addresses` in turn, the next one only when a connection could not be made; the URL's host
    is kept as `Host` and as the TLS SNI and certificate name. The name is never resolved again, so a rebinding
    domain cannot reach another address."""
    target = httpx.URL(url)
    merged = {**headers, "Accept-Encoding": "identity", "Host": target.netloc.decode("ascii")}
    sni = {"sni_hostname": target.raw_host.decode("ascii")}
    async with httpx.AsyncClient(timeout=POST_TIMEOUT_SECONDS, follow_redirects=False,
                                 verify=http_bounded._verify_context()) as client:
        for index, address in enumerate(addresses):
            try:
                async with client.stream("POST", target.copy_with(host=address), content=body, headers=merged,
                                         extensions=sni) as response:
                    received = bytearray()
                    async for chunk in response.aiter_raw():
                        received += chunk
                        if len(received) >= ANSWER_BYTES:
                            break
                    return response.status_code, bytes(received[:ANSWER_BYTES])
            except httpx.ConnectError:
                if index == len(addresses) - 1:
                    raise
    raise httpx.ConnectError("no address to dial")


def post_raw(url: str, body: bytes, headers: dict[str, str], addresses: Sequence[str]) -> tuple[int, bytes]:
    """POST `body` to the vetted `addresses`, return the status and the start of the answer.

    ONE wall-clock bound over the whole exchange — DNS, connect, TLS, headers, body — because httpx's timeout is per
    operation and a destination dripping a byte every few seconds would otherwise hold the pass (and a test
    request's worker thread) for hours. Async, so the bound's cancellation closes the socket; the policy
    (redirects off, identity encoding, the shared TLS context) is `http_bounded`'s."""
    return asyncio.run(asyncio.wait_for(post_async(url, body, headers, addresses), timeout=POST_TIMEOUT_SECONDS))


_TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)


def answer_snippet(answer: bytes) -> str:
    """A refusing answer as one readable line: an HTML page by its `<title>` (or nothing), whitespace collapsed,
    at most `ERROR_BODY_CHARS`."""
    text = answer.decode("utf-8", "replace").strip()
    if text.startswith("<"):
        title = _TITLE.search(text)
        text = title.group(1) if title else ""
    return " ".join(text.split())[:ERROR_BODY_CHARS]
