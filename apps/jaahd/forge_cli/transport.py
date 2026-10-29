"""HTTPS to ONE forge host, with the credential never leaving it.

`Transport` is the only thing that touches the network (tests replace it with recorded responses); it
never follows a redirect itself. `Client` builds every URL under the login's `api_base`, attaches the
credential, and follows a redirect only to the SAME host: a redirect to any other host is refused, because
urllib would carry the Authorization header there. The one narrow exception is `anonymous_hop`, which an
adapter asks for when a forge serves a download (GitHub's job logs) from a presigned URL on another host —
that hop is made WITHOUT the credential, and only over https.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from typing import Callable

from .model import FORGE_ERROR_CAP, CredentialRefused, ForgeError

TIMEOUT_S = 30
# A log can be large; nothing a runner reads needs more.
_MAX_BODY_BYTES = 32 * 1024 * 1024
_MAX_REDIRECTS = 5
_USER_AGENT = "jaah-forge"


@dataclass(frozen=True)
class Response:
    status: int
    headers: dict  # lower-cased names
    body: bytes


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuses every redirect: urllib would carry the bearer header to whatever host a redirect names."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: D401 — urllib's signature
        return None  # a 3xx surfaces as an HTTPError, which `send` turns into a Response


class Transport:
    """One HTTP exchange. Never follows redirects."""

    def send(self, method: str, url: str, headers: dict, body: "bytes | None") -> Response:
        req = urllib.request.Request(url, data=body, method=method, headers=headers)
        opener = urllib.request.build_opener(NoRedirect)
        host = urllib.parse.urlsplit(url).netloc
        try:
            with opener.open(req, timeout=TIMEOUT_S) as resp:
                return Response(resp.status, _lower(resp.headers), resp.read(_MAX_BODY_BYTES))
        except urllib.error.HTTPError as exc:
            try:
                data = exc.read(_MAX_BODY_BYTES)
            except OSError:
                data = b""
            return Response(exc.code, _lower(exc.headers or {}), data)
        except (urllib.error.URLError, OSError) as exc:
            reason = getattr(exc, "reason", exc)
            raise ForgeError(f"cannot reach {host}: {reason}") from None


def _lower(headers) -> dict:
    return {str(k).lower(): str(v) for k, v in dict(headers).items()}


def host_of(url: str) -> str:
    """`host[:port]`, lower-cased, of an https URL ("" for anything else)."""
    try:
        parts = urllib.parse.urlsplit(url)
        port = parts.port
    except ValueError:
        return ""
    if parts.scheme.lower() != "https" or not parts.hostname or "@" in parts.netloc:
        return ""
    host = parts.hostname.lower()
    return f"{host}:{port}" if port not in (None, 443) else host


class Client:
    """Requests under one `api_base`, authenticated with one header, confined to that base's host."""

    def __init__(self, api_base: str, auth_header: str, transport: Transport, label: str, repo: str = "",
                 reauth: "Callable[[], str] | None" = None, on_rejected: "Callable[[], None] | None" = None,
                 write_problem: str = "") -> None:
        self.api_base = api_base.rstrip("/")
        # `owner/name` of the repository the login is for, named in a credential refusal (`_checked`).
        self.repo = repo
        self.host = host_of(self.api_base)
        if not self.host:
            raise ForgeError(f"api_base {api_base!r} is not an https URL")
        self._auth = auth_header
        self._transport = transport
        self.label = label
        # A fresh auth header past the daemon's cache, asked ONCE per invocation on the first 401: the
        # forge revokes a user token when the control plane refreshes it (GitHub does), while the daemon
        # may still hold the old one for minutes. A 401 means the request was not acted on, so resending it
        # is safe for a write too. None: a 401 is a refusal at once (tests, and any caller without a broker).
        self._reauth = reauth
        # Told ONCE per invocation when a 401 stands after the fresh login (`git_credential_helper.report_forge_auth`):
        # the control plane ends a merge-lane run the forge keeps refusing. Never raises into the request.
        self._on_rejected = on_rejected
        # The control plane's sentence naming what this login lacks on this repository (`Login.write_problem`):
        # a refusal then says exactly what a person must grant, rather than "grant it access".
        self._write_problem = write_problem

    def url(self, path: str, params: "dict | None" = None) -> str:
        if "://" in path:
            if host_of(path) != self.host:
                raise ForgeError(f"refusing {path.split('?')[0]!r}: not on {self.host}")
            url = path
        else:
            url = f"{self.api_base}/{path.lstrip('/')}"
        if params:
            url += ("&" if "?" in url else "?") + urllib.parse.urlencode(params, doseq=True)
        return url

    def raw(self, method: str, path: str, *, params: "dict | None" = None, json_body=None,
            content_type: str = "application/json", accept: str = "application/json",
            anonymous_hop: bool = False) -> Response:
        url = self.url(path, params)
        body = None if json_body is None else json.dumps(json_body).encode()
        headers = {"Authorization": self._auth, "Accept": accept, "User-Agent": _USER_AGENT}
        if body is not None:
            headers["Content-Type"] = content_type
        for _ in range(_MAX_REDIRECTS + 1):
            resp = self._transport.send(method, url, headers, body)
            if resp.status == 401 and "Authorization" in headers and self._renew_auth():
                headers = {**headers, "Authorization": self._auth}
                resp = self._transport.send(method, url, headers, body)
            if resp.status == 401 and "Authorization" in headers:
                self._report_rejected()
            if resp.status not in (301, 302, 303, 307, 308):
                return self._checked(method, path, resp)
            location = urllib.parse.urljoin(url, resp.headers.get("location", ""))
            target = host_of(location)
            if not target:
                raise ForgeError(f"{self.label} redirected {method} {_short(path)} to a non-https location")
            if target != host_of(url):
                if not (anonymous_hop and method == "GET"):
                    raise ForgeError(
                        f"{self.label} redirected {method} {_short(path)} to another host ({target}); "
                        "refusing to carry the credential there")
                # A presigned download: the credential stays behind.
                headers = {"Accept": accept, "User-Agent": _USER_AGENT}
            url = location
            if resp.status == 303:
                method, body = "GET", None
        raise ForgeError(f"{self.label} redirected {method} {_short(path)} too many times")

    def _renew_auth(self) -> bool:
        """Swap in a fresh auth header (`_reauth`), once. True only when it differs from the refused one."""
        reauth, self._reauth = self._reauth, None
        if reauth is None:
            return False
        try:
            fresh = reauth()
        except (CredentialRefused, ForgeError):
            return False  # no fresh login to be had: the 401 stands as the refusal it is
        if not fresh or fresh == self._auth:
            return False
        self._auth = fresh
        return True

    def _report_rejected(self) -> None:
        """Report the standing 401 (`on_rejected`), once; a failed report never fails the request."""
        report, self._on_rejected = self._on_rejected, None
        if report is None:
            return
        try:
            report()
        except Exception:  # noqa: BLE001 — fire-and-forget: the refusal below is what the runner must see
            pass

    def _checked(self, method: str, path: str, resp: Response) -> Response:
        if resp.status < 400:
            return resp
        message = _message(resp.body)
        if _refuses_the_login(method, resp, message):
            # The forge is reachable and refuses THIS login on THIS repository: the task starter's connection
            # cannot act on it, which only a person can change — exit 4 naming the repository, as a refused
            # broker login is, so a runner raises a blocker for it rather than skipping the repository.
            where = self.repo or self.host
            fix = (f"no {self.label} credential of the task starter's can write to it: {self._write_problem}"
                   if self._write_problem else "a person must grant it access or reconnect it")
            raise CredentialRefused(
                f"{self.label} refused this login for {where} ({method} {_short(path)}: HTTP {resp.status}: "
                f"{' '.join(message.split())[:FORGE_ERROR_CAP]}) — the task starter's {self.label} connection "
                f"cannot act on {where}; {fix}")
        raise ForgeError(f"{self.label} {method} {_short(path)}: HTTP {resp.status}: {message}")

    def json(self, method: str, path: str, **kwargs):
        resp = self.raw(method, path, **kwargs)
        if not resp.body.strip():
            return {}
        try:
            return json.loads(resp.body)
        except ValueError:
            raise ForgeError(f"{self.label} {method} {_short(path)}: the answer is not JSON") from None

    def get(self, path: str, **kwargs):
        return self.json("GET", path, **kwargs)

    def text(self, path: str, **kwargs) -> str:
        return self.raw("GET", path, accept="*/*", **kwargs).body.decode("utf-8", errors="replace")


def _refuses_the_login(method: str, resp: Response, message: str) -> bool:
    """A 401, or a 403 to a WRITE (create, merge, auto-merge — GitHub's GraphQL included) that is not a rate
    limit: the login cannot act on the repository. A 403 to a read is often just a path the role may not see,
    and GitHub answers an exhausted or secondary rate limit with 403 too, which passes by itself."""
    if resp.status == 401:
        return True
    if resp.status != 403 or method == "GET":
        return False
    return resp.headers.get("x-ratelimit-remaining") != "0" and "rate limit" not in message.lower()


def _short(path: str) -> str:
    return path.split("?", 1)[0]


def _details(errors) -> str:
    """GitHub's 422 detail (`{"message": "Validation Failed", "errors": [{"message": "A pull request already
    exists for …"}]}`): the headline alone tells a runner nothing it can act on."""
    if not isinstance(errors, list):
        return ""
    parts = []
    for err in errors:
        if isinstance(err, dict):
            text = err.get("message") or " ".join(str(err.get(k)) for k in ("field", "code") if err.get(k))
        else:
            text = str(err)
        if text:
            parts.append(text)
    return f": {'; '.join(parts)}" if parts else ""


def _message(body: bytes) -> str:
    """The forge's own error text, from whichever field its JSON uses."""
    try:
        data = json.loads(body)
    except ValueError:
        return body.decode("utf-8", errors="replace").strip() or "(no body)"
    if isinstance(data, dict):
        for key in ("message", "error_description", "error"):
            value = data.get(key)
            if isinstance(value, dict):
                value = value.get("message")
            if isinstance(value, str) and value:
                return value + _details(data.get("errors")) if key == "message" else value
            if isinstance(value, list) and value:
                return "; ".join(str(v) for v in value)
    return json.dumps(data)[:200]
