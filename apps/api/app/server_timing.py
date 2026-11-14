"""How long this process held a request before it answered: `Server-Timing: app;dur=<ms>` on a console session's answers.

A console read that took seconds on someone's screen is either the network or this process, and from the browser
the two look the same. The browser exposes this header to the page on the request's own resource timing
(`PerformanceResourceTiming.serverTiming`, same origin), so the console's slow-read and stall reports
(`apps/web/src/lib/resourceTiming.ts`) can say "server 300 ms, the rest was the wire" — or the reverse.

`dur` runs from the moment the request reaches the app (the outermost middleware) to its response's first byte:
the thread and pool queueing, the middlewares and the handler. The nginx sidecar's and the load balancer's time
is not in it; for a body that streams (SSE, a download) it is the time to the headers. A route that sets its own
`Server-Timing` keeps it. An uncaught exception's 500 is answered outside every middleware and carries none.

ONLY A SIGNED-IN CONSOLE SESSION gets it: one `SessionAuthMiddleware` admitted (`request.state.console_session`),
never a public door (sign-in, sign-up, password reset), an API key, a node token or an anonymous caller. On a public
door the server's own time, to a tenth of a millisecond and with the network taken out, would tell in one request
what plain latency hides in jitter — whether an address has an account (sign-up's known address answers without
the write a new one costs). The console session is the header's only reader, so nothing is lost.

Pure ASGI, as `auth.SecurityHeadersMiddleware` is and for the same reason: no `call_next` task or body relay.
"""
from __future__ import annotations

import time

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

HEADER = "Server-Timing"
METRIC = "app"


def _console_session(scope: Scope) -> bool:
    """Whether the auth middleware admitted this request as a signed-in console session (it has run by the time
    the response starts)."""
    return (scope.get("state") or {}).get("console_session") is not None


class ServerTimingMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        started = time.perf_counter()

        async def send_with_timing(message: Message) -> None:
            if message["type"] == "http.response.start" and _console_session(scope):
                message.setdefault("headers", [])  # optional in ASGI; MutableHeaders needs the list
                elapsed = (time.perf_counter() - started) * 1000
                MutableHeaders(scope=message).setdefault(HEADER, f"{METRIC};dur={elapsed:.1f}")
            await send(message)

        await self.app(scope, receive, send_with_timing)
