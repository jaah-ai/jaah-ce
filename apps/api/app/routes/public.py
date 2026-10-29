"""Generic public (no-session) endpoints + shared helpers."""
from __future__ import annotations

import json
import logging
import os

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse, Response
from sqlalchemy.orm import Session

from app import edition_features
from app.durable import notify_outbox
from app.auth import google_client_id
from app.authz.catalog import Tenancy
from app.authz.declarations import always
from app.db import get_session
from app.login_oauth import frontend_origin, github_configured, microsoft_configured
from app.ratelimit import SharedRateLimiter

router = APIRouter(prefix="/api/public", tags=["public"])
# The Telegram bot's webhook (cloud-only, included by `app/routers_cloud.py`).
telegram_router = APIRouter(prefix="/api/public", tags=["public"])
log = logging.getLogger("app.routes.public")


def check_rate_limit(request: Request) -> None:
    limiter = request.app.state.public_limiter
    client_ip = request.client.host if request.client else "unknown"
    if not limiter.allow(client_ip):
        raise HTTPException(status_code=429, detail="too many requests")


@router.get("/settings", dependencies=[Depends(check_rate_limit)])
@always(Tenancy.BOOTSTRAP)
def public_settings(session: Session = Depends(get_session)):
    """Unauthenticated: what the login and sign-up pages need before sign-in — the app title, the Google
    OAuth client id the "Sign in with Google" button initializes with, and the sign-up flow's links.

    The client id is served at runtime, not baked into the SPA bundle: one web image is deployed to
    every environment, and serving it from the API keeps the browser's audience equal to the one the
    token verifier checks (`app.auth.google_client_id`). `null` when unset — the button is then not
    rendered, matching POST /api/auth/google's 503.

    Serves org zero's title, never a tenant's: the caller is unidentified, and org zero is the
    operator plane, so nothing about any tenant leaks (`AC-GUARD-5`). Read-only — never inserts.

    The read runs under `operator_scope()`, because nothing is bound on a BOOTSTRAP route and an
    enforcing seam would otherwise return nothing; the statement is pinned to `org_id = ORG_ZERO_ID`.
    `identity_scope()` would not work: `AppSettings` is not in `IDENTITY_CLASSES`.
    """
    from app import legal_pages
    from app.operator_settings import reads as settings_reads
    from app.routes.settings import app_title_for
    from app.tenancy import seam
    from app.tenancy.ids import ORG_ZERO_ID

    with seam.operator_scope():
        app_title = app_title_for(session, ORG_ZERO_ID)
    return {
        "app_title": app_title,
        "google_client_id": google_client_id(),
        # Whether "Sign in with GitHub" renders: a platform GitHub OAuth App is designated (`login_oauth`).
        "github_sign_in": github_configured(),
        # Whether "Sign in with Microsoft" renders: the Entra client id and secret are both set.
        "microsoft_sign_in": microsoft_configured(),
        # Where "Sign in with SSO" finishes: the SP URLs are `FRONTEND_URL`'s, so a sign-in started on a
        # `CONSOLE_ORIGINS` console lands here (`routes/sso_signin.py::_restart_on_frontend`).
        "sso_origin": frontend_origin(),
        # The sign-up flow's links and the legal version an applicant accepts (operator settings,
        # "Organizations" > "Sign-up"): public pages by nature, so safe to serve signed out.
        "tutorials_url": settings_reads.get_str("signup_tutorials_url"),
        "home_url": settings_reads.get_str("signup_home_url"),
        "docs_url": settings_reads.get_str("signup_docs_url"),
        "terms_url": legal_pages.signup_link("terms"),
        "privacy_url": legal_pages.signup_link("privacy"),
        "legal_version": settings_reads.get_str("legal_version"),
    }


@router.get("/signup-status", dependencies=[Depends(check_rate_limit)])
@always(Tenancy.BOOTSTRAP)
def signup_status():
    """Unauthenticated: whether the self-service sign-up is open (`app/signup.py`), so the login and
    sign-up pages say "Sign-up is closed" before a visitor picks a provider. An edition without sign-up is closed."""
    if not edition_features.enabled("signup"):
        return {"open": False}
    from app import signup

    return {"open": signup.is_open()}


# ── The public legal pages (`app/legal_pages.py` owns the text's source, the values and when it is live) ─


@router.get("/legal/{slug}", dependencies=[Depends(check_rate_limit)])
@always(Tenancy.BOOTSTRAP)
def legal_page(slug: str):
    """Unauthenticated: one legal page (any `legal_pages.PAGES` slug) as a block tree the
    console's /legal route renders. 404 until an operator publishes the pages with every value set — the
    same answer as an unknown page, so a draft's existence is not served either. Operator settings are
    org-less, so nothing about any tenant is read."""
    from app import legal_pages

    page = legal_pages.render(slug, preview=False)
    if page is None:
        raise HTTPException(status_code=404, detail="Not found.")
    return page


# ── The compliance manifest's public keys (`app/compliance_signing.py` owns the keys; R5) ───────────────


def _published_keys_or_503():
    # An edition without compliance signs nothing, so it publishes no key: both key routes are its 404.
    if not edition_features.enabled("compliance"):
        from app.ce_caps import NOT_IN_CE

        raise HTTPException(status_code=404, detail=NOT_IN_CE)
    from app import compliance_signing
    from app.aws.kms import KmsError

    try:
        return compliance_signing.published_keys()
    except KmsError as exc:
        # The class and AWS's error name only — `KmsError` never carries the account or a key.
        compliance_signing.log.warning("compliance keys: KMS unavailable (%s)", exc)
        raise HTTPException(status_code=503, detail="Signing keys are unavailable. Try again later.") from None


@router.get("/compliance/keys", dependencies=[Depends(check_rate_limit)])
@always(Tenancy.BOOTSTRAP)
def compliance_keys():
    """Unauthenticated: every public key a compliance manifest from this environment may be signed under —
    the active key and each retired one — so a third party verifies a manifest offline (the reader doc
    "Verifying a Jaah compliance manifest"). Public halves only; an empty list when signing is unconfigured."""
    return {
        "keys": [
            {
                "kid": k.kid,
                "alg": k.alg,
                "environment": k.environment,
                "status": k.status,
                "public_key_pem": k.public_key_pem,
            }
            for k in _published_keys_or_503()
        ]
    }


@router.get("/compliance/keys/{kid}.pem", dependencies=[Depends(check_rate_limit)])
@always(Tenancy.BOOTSTRAP)
def compliance_key_pem(kid: str):
    """Unauthenticated: one published key as PEM text — what the documented `curl … > key.pem` step fetches.
    404 for a kid this environment does not publish."""
    key = next((k for k in _published_keys_or_503() if k.kid == kid), None)
    if key is None:
        raise HTTPException(status_code=404, detail="Not found.")
    return Response(content=key.public_key_pem, media_type="text/plain")


# ── One-click unsubscribe from optional system mail (`app/email_unsubscribe.py` owns the flow; RFC 8058) ──

UNSUBSCRIBE_PATH = "/email/unsubscribe/{token}"
# Per address. The page (GET) is a person's; the one-click POST is mostly a mail provider's, for many recipients from
# a few of its servers and not reliably retried, so it has a budget of its own ten times larger.
_unsubscribe_limiter = SharedRateLimiter(
    limit=int(os.environ.get("EMAIL_UNSUBSCRIBE_RATE_LIMIT", "120")), window=60.0, bucket="email-unsubscribe"
)
_unsubscribe_post_limiter = SharedRateLimiter(
    limit=int(os.environ.get("EMAIL_UNSUBSCRIBE_POST_RATE_LIMIT", "1200")), window=60.0,
    bucket="email-unsubscribe-post",
)
_UNSUBSCRIBE_MAX_BODY = 4096


async def _unsubscribe_answer(request: Request, token: str, *, act: bool) -> Response:
    import secrets as _secrets

    from fastapi.responses import HTMLResponse

    from app import email_unsubscribe

    nonce = _secrets.token_urlsafe(16)
    headers = email_unsubscribe.page_headers(nonce)
    visitor_ip = request.client.host if request.client else "unknown"
    limiter = _unsubscribe_post_limiter if act else _unsubscribe_limiter
    if not await run_in_threadpool(limiter.allow, visitor_ip):
        return Response("Too many requests. Try again in a minute.", status_code=429, media_type="text/plain")
    target = await run_in_threadpool(email_unsubscribe.verify, token)
    if target is None:
        return HTMLResponse(email_unsubscribe.page("invalid", nonce), status_code=404, headers=headers)
    if not act:
        return HTMLResponse(email_unsubscribe.page("confirm", nonce), headers=headers)
    # The body (RFC 8058's `List-Unsubscribe=One-Click`, or the confirm form's same field) is read and not
    # required: the signed token is the whole authority, and a client that posts another shape still means it.
    await _read_capped(request, _UNSUBSCRIBE_MAX_BODY)
    await run_in_threadpool(email_unsubscribe.unsubscribe, target)
    return HTMLResponse(email_unsubscribe.page("done", nonce), headers=headers)


@router.get(UNSUBSCRIBE_PATH)
@always(Tenancy.BOOTSTRAP)
async def email_unsubscribe_confirm(token: str, request: Request):
    """Unauthenticated: the page an unsubscribe link opens — one button that POSTs. A GET changes nothing, so a
    link scanner or a preview fetch never unsubscribes anyone. Reached through the hooks edge."""
    return await _unsubscribe_answer(request, token, act=False)


@router.post(UNSUBSCRIBE_PATH)
@always(Tenancy.BOOTSTRAP)
async def email_unsubscribe_one_click(token: str, request: Request):
    """Unauthenticated: RFC 8058's one-click POST (a mail client's Unsubscribe) or the confirm page's button. A
    token that verifies turns Email off for its one event and user, idempotently; 404 otherwise."""
    return await _unsubscribe_answer(request, token, act=True)


# ── Event-driven Task triggers (`app/trigger_intake.py` owns the flow) ─────────────────────────────

TRIGGER_INGRESS_PATH = "/triggers/{ingress_token}"


async def _read_capped(request: Request, limit: int) -> bytes | None:
    """The raw body, or None once it passes `limit` bytes — refused by the declared length first, then by
    what actually arrives, so a lying `Content-Length` cannot make us buffer more."""
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > limit:
        return None
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


@router.post(TRIGGER_INGRESS_PATH)
@always(Tenancy.BOOTSTRAP)
async def trigger_ingress(ingress_token: str, request: Request):
    """Unauthenticated at the session layer: one delivery to a webhook trigger. Reached from the internet only
    through the hooks edge's allow-list, so the Jaah API key a webhook presents (a GitHub trigger's, or a legacy
    webhook's, HMAC signature or bearer token) is the whole gate.

    Answered in milliseconds: the body is verified, stored as one `received` row and processed by the
    `trigger-intake` activity. The check order — 413, 404, 429, 401, paused 202, 202 — is
    `trigger_intake.ingest_http`'s. The secret is never accepted in the query string (the edge's access logs
    keep URLs)."""
    from app import trigger_intake
    from app.triggers import MAX_BODY_BYTES

    raw = await _read_capped(request, MAX_BODY_BYTES)
    visitor_ip = request.client.host if request.client else "unknown"
    answer = await run_in_threadpool(
        trigger_intake.ingest_http,
        ingress_token, raw, dict(request.headers), dict(request.query_params), visitor_ip,
    )
    return JSONResponse(answer.body or {}, status_code=answer.status)


# ── The Telegram bot's webhook (`app/telegram_bot.py` + `app/telegram_link.py`) ─────────────

_TELEGRAM_MAX_BODY = 64 * 1024
_TELEGRAM_LINKED = "Connected. Jaah notifications will arrive here; turn events on or off in Profile > Notifications."
_TELEGRAM_BAD_CODE = "That link expired or was already used. Open Profile > Notifications in Jaah and connect again."
_TELEGRAM_HELLO = "To get Jaah notifications here, open Profile > Notifications in Jaah and choose Connect."
_TELEGRAM_REPLACED = "This chat no longer gets Jaah notifications: another Telegram account was connected instead."
_TELEGRAM_LABEL_MAX = 64


def _telegram_sender_label(message: dict) -> str | None:
    """`@username`, else the sender's first and last name, cut to `_TELEGRAM_LABEL_MAX`; None when Telegram
    names neither."""
    sender = message.get("from") if isinstance(message.get("from"), dict) else {}
    username = sender.get("username")
    if isinstance(username, str) and username.strip():
        return f"@{username.strip()}"[:_TELEGRAM_LABEL_MAX]
    name = " ".join(
        part.strip() for part in (sender.get("first_name"), sender.get("last_name"))
        if isinstance(part, str) and part.strip()
    )
    return name[:_TELEGRAM_LABEL_MAX] or None


def _telegram_update(raw: bytes) -> dict | None:
    """The reply for one update, as a Bot API method in the webhook's own response, or None for no reply.

    Only a private chat's `/start` is acted on: the deep link's code redeems; anything else gets one line that
    names where to start. A group the bot is added to is ignored."""
    if not edition_features.enabled("telegram"):
        return None
    from app import telegram_link
    from app.db import SessionLocal

    try:
        update = json.loads(raw)
    except ValueError:
        return None
    message = update.get("message") if isinstance(update, dict) else None
    chat = message.get("chat") if isinstance(message, dict) else None
    text = message.get("text") if isinstance(message, dict) else None
    if not isinstance(chat, dict) or chat.get("type") != "private" or not isinstance(text, str):
        return None
    chat_id = chat.get("id")
    if not isinstance(chat_id, int) or isinstance(chat_id, bool):
        return None
    code = telegram_link.start_code(text)
    if code is None:
        reply = _TELEGRAM_HELLO
    else:
        with SessionLocal() as session:
            linked = telegram_link.redeem(session, code, chat_id, label=_telegram_sender_label(message))
        reply = _TELEGRAM_LINKED if linked is not None else _TELEGRAM_BAD_CODE
        if linked is not None and linked[1] is not None:
            _tell_replaced_chat(linked[1])
    return {"method": "sendMessage", "chat_id": chat_id, "text": reply}


def _tell_replaced_chat(chat_id: int) -> None:
    """Tell a chat a new link replaced that it gets nothing more: handed to the durable outbox
    (`durable/notify_outbox.py`), so the webhook answers Telegram at once. Never raises."""
    notify_outbox.telegram(chat_id, _TELEGRAM_REPLACED, "replaced-chat notice")


@telegram_router.post("/telegram/updates")
@always(Tenancy.BOOTSTRAP)
async def telegram_webhook(request: Request):
    """Unauthenticated at the session layer: one update from Telegram. The gate is the secret token Telegram
    echoes (`telegram_bot.SECRET_HEADER`), compared in constant time with the one derived from the designated
    bot's token; with no bot designated, or a wrong token, the path does not exist (404). A valid update is
    always answered 200, so Telegram never redelivers it; the reply rides the response body."""
    import hmac

    # Mounted only by `app/routers_cloud.py`; the community edition has no Telegram.
    if not edition_features.enabled("telegram"):
        raise HTTPException(status_code=404, detail="Not found.")
    from app import telegram_bot

    token, _ = await run_in_threadpool(telegram_bot.bot_token)
    presented = request.headers.get(telegram_bot.SECRET_HEADER, "")
    if token is None or not hmac.compare_digest(presented.encode(), telegram_bot.webhook_secret(token).encode()):
        raise HTTPException(status_code=404, detail="Not found.")
    raw = await _read_capped(request, _TELEGRAM_MAX_BODY)
    if raw is None:
        return Response(status_code=200)
    reply = await run_in_threadpool(_telegram_update, raw)
    return JSONResponse(reply) if reply is not None else Response(status_code=200)
