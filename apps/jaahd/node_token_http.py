"""node_token_http.py — the ONE opener for any request carrying this node's `X-Agent-Token`.

`urllib`'s default redirect handler copies every header onto the redirected request. An attachment's
content route answers a 307 to a presigned S3 URL, so a plain `urlopen` posted the node's bearer token to
Amazon. `OFF_HOST_SAFE_OPEN` follows the redirect but strips the token whenever SCHEME or HOST changes.

Who uses it: `control_plane_api` (the daemon's every agent call, bound there as `_OFF_HOST_SAFE_OPEN`) and
`tools/control_plane_client.py`'s credential leg (loaded by path through `tools/jaahd_module.py`, since the
full `control_plane_api` is too heavy for a hook). STDLIB ONLY, no sibling imports: that is what lets a
`tools/` script load it without the daemon's import graph.
"""
from __future__ import annotations

import urllib.parse
import urllib.request


def origin(url: str) -> tuple[str, str]:
    """`(scheme, host:port)`, folded, for comparing two URLs as the same trust boundary.

    Shared by the redirect handler and `control_plane_api._agent_urlopen`'s body capture so they agree on
    "somewhere else"."""
    parts = urllib.parse.urlsplit(url)
    return parts.scheme.lower(), parts.netloc.lower()


class DropNodeTokenOffHost(urllib.request.HTTPRedirectHandler):
    """Follow a redirect, but never carry this node's own CP credential across a host boundary (module
    docstring). Stripped whenever SCHEME or HOST changes (a same-host https→http downgrade would put it on
    the wire in cleartext).

    Matched case-insensitively, not via `Request.remove_header`: that capitalizes its argument through
    3.13 and not on 3.14, so no single spelling works on both."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected is None:
            return None
        if origin(newurl) != origin(req.full_url):
            for store in (redirected.headers, redirected.unredirected_hdrs):
                for name in [k for k in store if k.lower() == "x-agent-token"]:
                    store.pop(name, None)
        return redirected


# Every default handler, only the redirect one replaced.
OFF_HOST_SAFE_OPEN = urllib.request.build_opener(DropNodeTokenOffHost()).open
