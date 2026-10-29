"""One-click unsubscribe from non-essential system mail (RFC 8058; PRD R6).

An optional email (a notification event: a mention, a merge-lane notice, a budget warning... — never an essential
type, `email_templates.TemplateType.essential`) names its recipient and event as an `UnsubscribeTarget`. Its link is
`{HOOKS_PUBLIC_BASE_URL}/api/public/email/unsubscribe/<token>`: the token is the target signed with the operator's
keyring (`email_unsubscribe_credential_id` -> an `email_unsubscribe_secret` credential: `current_secret`, and
`previous_secret` so links already sent survive a rotation), salted, with no expiry. The message carries it three ways
from one URL: `List-Unsubscribe` + `List-Unsubscribe-Post: List-Unsubscribe=One-Click` (`headers`), and the layout's
footer link (`email_templates._layout`).

The public route (`routes/public.py`) shows a confirm page on GET — a link scanner never unsubscribes anyone — and on
POST turns Email off for that event alone (`preferences.NotificationEventPreference.email`), with no sign-in; the
switch shows on Profile > Alerts. A blank hooks URL or keyring sends optional mail with neither headers nor link,
never a dead one; on a deployed environment that is a fault (`problem`).
"""
from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass
from typing import Optional
from uuid import UUID

from itsdangerous import BadSignature, URLSafeSerializer

from app.entity_shape import optional_uuid

log = logging.getLogger("app.email_unsubscribe")

SETTING_KEY = "email_unsubscribe_credential_id"
CREDENTIAL_TYPE = "email_unsubscribe_secret"
_SALT = "email-unsubscribe"
_VERSION = 1
PATH = "/api/public/email/unsubscribe"
#: RFC 8058's one body a one-click POST carries.
ONE_CLICK_BODY = "List-Unsubscribe=One-Click"
_TOKEN_MAX = 512
#: Shorter keys are refused (no link at all): a sent token is offline-brute-forceable against a short one.
KEY_MIN = 32


@dataclass(frozen=True)
class UnsubscribeTarget:
    """Whose preference one click turns off (`user_id`) and for which notification event (`event`)."""

    user_id: UUID
    event: str


# The opened keyring per designation, for `_KEYRING_TTL_S`: opening the credential decrypts it and lands a
# `credential_read` row, which neither each send nor each unauthenticated click should cost (`telegram_bot`'s rule).
_KEYRING_TTL_S = 60.0
_keyring_cache: dict[str, tuple[float, tuple[str, ...]]] = {}
_keyring_lock = threading.Lock()


def forget_keyring() -> None:
    """Drop the cached keyring, so the next read opens the credential again."""
    with _keyring_lock:
        _keyring_cache.clear()


def _keyring() -> tuple[str, ...]:
    """`(current, previous)` (previous only when set), or `()` when no usable key is designated."""
    from app import operator_credentials
    from app.operator_settings import reads as settings_reads

    designated = settings_reads.get_text(SETTING_KEY).strip()
    if not designated:
        return ()
    now = time.monotonic()
    with _keyring_lock:
        cached = _keyring_cache.get(designated)
    if cached is not None and now - cached[0] < _KEYRING_TTL_S:
        return cached[1]
    try:
        config = operator_credentials.opened(designated, CREDENTIAL_TYPE, consumer="email_unsubscribe") or {}
    except Exception:  # noqa: BLE001 — no key means no link, never a failed send
        log.exception("email unsubscribe: the signing key could not be opened")
        config = {}
    current = str(config.get("current_secret") or "").strip()
    previous = str(config.get("previous_secret") or "").strip()
    if current and len(current) < KEY_MIN:
        log.warning("email unsubscribe: the signing key is shorter than %d characters — no unsubscribe links", KEY_MIN)
        current = ""
    keys = (current, previous) if current and len(previous) >= KEY_MIN else (current,) if current else ()
    with _keyring_lock:
        _keyring_cache.clear()
        _keyring_cache[designated] = (now, keys)
    return keys


def _serializer(keys: tuple[str, ...]) -> URLSafeSerializer:
    # itsdangerous signs with the LAST key and verifies against every one: current last.
    return URLSafeSerializer(list(reversed(keys)), salt=_SALT)


def token_for(target: UnsubscribeTarget) -> Optional[str]:
    """`target` signed with the current key, or None without one."""
    keys = _keyring()
    if not keys:
        return None
    return _serializer(keys).dumps({"u": str(target.user_id), "e": target.event, "v": _VERSION})


def verify(token: str) -> Optional[UnsubscribeTarget]:
    """The target a token names when it was signed with the current or the previous key, else None."""
    keys = _keyring()
    if not keys or not token or len(token) > _TOKEN_MAX:
        return None
    try:
        payload = _serializer(keys).loads(token)
    except BadSignature:
        return None
    if not isinstance(payload, dict) or payload.get("v") != _VERSION or not isinstance(payload.get("e"), str):
        return None
    user_id = optional_uuid(payload.get("u"))
    return UnsubscribeTarget(user_id=user_id, event=payload["e"]) if user_id is not None else None


def _base_url() -> str:
    from app.config import get_settings

    return get_settings().hooks_public_base_url.rstrip("/")


def url_for(target: Optional[UnsubscribeTarget]) -> Optional[str]:
    """The one-click link for `target`, or None: no target (essential mail), an event nobody may turn off (a page),
    no hooks edge or no signing key. None sends the message without headers or footer link."""
    from app.preferences import NOTIFICATION_EVENTS

    if target is None or target.event not in NOTIFICATION_EVENTS:
        return None
    base = _base_url()
    if not base:
        return None
    token = token_for(target)
    return f"{base}{PATH}/{token}" if token else None


def headers(url: Optional[str]) -> dict[str, str]:
    """RFC 8058's two headers for `url`; none without one. Both are listed in SES's DKIM signature."""
    if not url:
        return {}
    return {"List-Unsubscribe": f"<{url}>", "List-Unsubscribe-Post": ONE_CLICK_BODY}


def problem() -> Optional[str]:
    """Why optional mail goes out without an unsubscribe link on a DEPLOYED environment (`SES_MAIL_DOMAIN` set), or
    None. A local stack sends it without one by design."""
    from app import system_mail

    if not os.environ.get(system_mail.DOMAIN_VARIABLE, "").strip():
        return None
    if not _base_url():
        return "HOOKS_PUBLIC_BASE_URL is blank, so notifications go out without an unsubscribe link"
    if not _keyring():
        return (
            f"No unsubscribe signing key of {KEY_MIN}+ characters is set, so notifications go out without an "
            "unsubscribe link"
        )
    return None


def unsubscribe(target: UnsubscribeTarget) -> None:
    """Turn Email off for `target.event` on `target.user_id`'s membership, and commit. Idempotent; a membership that
    is gone is nothing to do. Under `seam.identity_scope()`: the public route binds no org."""
    from app.db import SessionLocal
    from app.models.identity import AdminUser
    from app.preferences import UserPreferences, preferences_to_store
    from app.tenancy import seam

    with seam.identity_scope(), SessionLocal() as session:
        user = session.get(AdminUser, target.user_id, with_for_update=True)
        if user is None:
            return
        prefs = UserPreferences.from_stored(user.preferences)
        events = prefs.notifications.events
        current = events.get(target.event)
        if current is None or not current.email:
            return
        events = {**events, target.event: current.model_copy(update={"email": False})}
        prefs = prefs.model_copy(update={"notifications": prefs.notifications.model_copy(update={"events": events})})
        user.preferences = preferences_to_store(prefs, user.preferences)
        session.commit()
    log.info("email unsubscribe: user %s turned Email off for %s", target.user_id, target.event)


# ── The pages the public route answers with ─────────────────────────────────────────────────────────────────
# The mail's own look (`email_templates`' palette and font), one card, no script: the CSP below allows only the
# nonce'd style and a form posting back to this same path.

_PAGE_STYLE = (
    "body{{margin:0;padding:48px 16px;background:{paper};font-family:{font};color:{ink};font-size:16px;"
    "line-height:26px}}main{{max-width:480px;margin:0 auto}}"
    ".brand{{display:flex;align-items:center;gap:10px;font-size:18px;font-weight:700;margin:0 4px 20px}}"
    ".mark{{width:28px;height:28px;border-radius:7px;background:{accent};color:#fff;text-align:center;"
    "line-height:28px;font-size:16px}}"
    ".card{{background:#fff;border:1px solid {rule};border-radius:12px;padding:32px}}"
    "h1{{margin:0 0 12px;font-size:22px;line-height:30px}}p{{margin:0 0 16px}}.muted{{color:{muted};font-size:14px;"
    "line-height:22px;margin:0}}button{{font:inherit;font-weight:600;min-height:44px;padding:0 24px;border:0;"
    "border-radius:8px;background:{accent};color:#fff;cursor:pointer;margin:4px 0 20px}}a{{color:{accent}}}"
)


def _profile_alerts_url() -> str:
    from app.config import get_settings

    return f"{get_settings().frontend_url.rstrip('/')}/profile?tab=notifications"


def page(kind: str, nonce: str) -> str:
    """`confirm` (the GET: one button, which POSTs), `done` (unsubscribed) or `invalid` (a token that does not
    verify), as a whole HTML document."""
    import html

    from app import email_templates as t

    esc = html.escape
    alerts = f'<a href="{esc(_profile_alerts_url(), quote=True)}">Profile &gt; Alerts</a>'
    if kind == "confirm":
        title = "Unsubscribe"
        body = (
            "<h1>Unsubscribe from emails like this?</h1>"
            "<p>You'll stop getting this kind of email from Jaah. Other emails, and other channels, are unchanged.</p>"
            f'<form method="post"><input type="hidden" name="List-Unsubscribe" value="One-Click">'
            "<button type=\"submit\">Unsubscribe</button></form>"
            f'<p class="muted">Turn it back on any time in {alerts}.</p>'
        )
    elif kind == "done":
        title = "Unsubscribed"
        body = (
            "<h1>You're unsubscribed</h1>"
            "<p>Jaah won't email you about this any more.</p>"
            f'<p class="muted">Changed your mind? Turn Email back on in {alerts}.</p>'
        )
    else:
        title = "Link not valid"
        body = (
            "<h1>This link doesn't work</h1>"
            "<p>It may be incomplete. Copy the whole link from the email, or change what Jaah emails you "
            f"in {alerts}.</p>"
        )
    style = _PAGE_STYLE.format(
        paper=t._PAPER, font=t._FONT, ink=t._INK, accent=t._ACCENT, rule=t._RULE, muted=t._MUTED,
    )
    return (
        '<!doctype html><html lang="en"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1"><meta name="robots" content="noindex">'
        f'<title>{esc(title)} · {esc(t.PRODUCT_NAME)}</title><style nonce="{nonce}">{style}</style></head>'
        f'<body><main><div class="brand"><span class="mark">{esc(t.PRODUCT_NAME[:1])}</span>{esc(t.PRODUCT_NAME)}'
        f'</div><div class="card">{body}</div></main></body></html>'
    )


def page_headers(nonce: str) -> dict[str, str]:
    return {
        "Content-Security-Policy": (
            f"default-src 'none'; style-src 'nonce-{nonce}'; form-action 'self'; base-uri 'none'; "
            "frame-ancestors 'none'"
        ),
        "Cache-Control": "no-store",
        "Referrer-Policy": "no-referrer",
        "X-Robots-Tag": "noindex",
    }
