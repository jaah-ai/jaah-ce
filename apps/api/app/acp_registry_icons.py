"""Each ACP registry agent's icon as a `data:` URI, for the console's Agents › Registry cards.

The console's CSP `img-src` allows `data:` and not the registry's CDN, and it stays that way: the control plane
fetches each icon itself and hands the page the bytes inline (`routes/acp_registry.py::list_registry`).

* Only an `https` URL on the registry's own origin (`acp_registry.REGISTRY_URL`'s host, port 443) is dialled; any
  other icon URL is omitted, never fetched, so a registry entry cannot point the control plane elsewhere.
* The read is `http_bounded`'s (no redirects, identity-encoded, capped): `_TIMEOUT_SECONDS`, at most `_MAX_BYTES`
  (a body that fills the cap is dropped), status 200, and a `Content-Type` of `image/svg+xml` or `image/png` only.
* Any failure is None: the card shows its placeholder. Results, None included, are cached in-process per URL for
  `acp_registry.CACHE_SECONDS`, and a list's icons are fetched in parallel on a small pool.
* `_client` is the seam tests replace (an `httpx.MockTransport`; no network in tests).
"""
from __future__ import annotations

import base64
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Iterable, Optional

import httpx

from app import acp_registry, http_bounded

#: The one origin icons are fetched from: the registry's own (`cdn.agentclientprotocol.com`).
REGISTRY_ORIGIN = http_bounded.origin(acp_registry.REGISTRY_URL)
_TIMEOUT_SECONDS = 3.0
_MAX_BYTES = 64 * 1024
#: The whole read, however slowly the bytes drip in under the per-read timeout.
_DEADLINE_SECONDS = 5.0
_TYPES = frozenset({"image/svg+xml", "image/png"})
_WORKERS = 8

_cache: dict[str, tuple[float, Optional[str]]] = {}
_cache_lock = threading.Lock()


def _client() -> httpx.Client:
    """The bounded client icons are read with. The seam tests replace."""
    return http_bounded.client(_TIMEOUT_SECONDS, {"Accept": ", ".join(sorted(_TYPES))})


def allowed(url: str) -> bool:
    """Whether `url` is an https URL on the registry's own origin."""
    return REGISTRY_ORIGIN is not None and http_bounded.origin(url) == REGISTRY_ORIGIN


def _fetch(url: str) -> Optional[str]:
    deadline = time.monotonic() + _DEADLINE_SECONDS
    try:
        with _client() as client, client.stream("GET", url) as response:
            if response.status_code != 200:
                return None
            kind = response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
            if kind not in _TYPES:
                return None
            raw, truncated = http_bounded.read_capped(response, _MAX_BYTES, stop=lambda: time.monotonic() > deadline)
    except (httpx.HTTPError, OSError):
        return None
    if truncated or not raw:
        return None
    return f"data:{kind};base64,{base64.b64encode(raw).decode('ascii')}"


def data_uri(url: Optional[str]) -> Optional[str]:
    """`url`'s icon as a `data:` URI, or None (not on the registry's origin, unreadable, the wrong type, too big)."""
    if not url or not allowed(url):
        return None
    now = time.monotonic()
    with _cache_lock:
        hit = _cache.get(url)
        if hit is not None and now - hit[0] < acp_registry.CACHE_SECONDS:
            return hit[1]
    uri = _fetch(url)
    with _cache_lock:
        _cache[url] = (time.monotonic(), uri)
    return uri


def data_uris(urls: Iterable[Optional[str]]) -> dict[str, Optional[str]]:
    """Each distinct icon URL's `data_uri`, fetched in parallel; a URL off the registry's origin maps to None."""
    wanted = sorted({url for url in urls if url})
    if not wanted:
        return {}
    with ThreadPoolExecutor(max_workers=min(_WORKERS, len(wanted)), thread_name_prefix="acp-icon") as pool:
        return dict(zip(wanted, pool.map(data_uri, wanted)))


def clear_cache() -> None:
    with _cache_lock:
        _cache.clear()
