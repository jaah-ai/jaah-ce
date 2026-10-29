"""THE MIME builder: every message the control plane sends — system mail and a tenant's Gmail — is built here.

`build` turns a sender, its recipients and a text body (plus an optional HTML alternative) into one RFC 5322 message:
From/To/Cc/Subject/Reply-To, `multipart/alternative` with the text part first when HTML is given, any extra headers,
and a `Message-ID` when the caller sets its own. It sends nothing: system mail (`app/system_mail.py`, over SES) and a
tenant's connection (`app/tenant_gmail/`) hand the result to their transport.

`OutboundEmail` is one system message (seam S4): always one recipient (the verified-address rule), its `mail_type`
(an `app/email_templates.py` type key) and `essential` (PRD R6) travel with it to the delivery log and the SES tags.
It carries no `Message-ID` or `Date`: SES sets both, and its MessageId is the stable id the delivery log records.

Header injection is refused structurally: every header value is flattened onto one line (`header_text`) and every
address must be exactly one bare addr-spec (`single_address`), never a list or a display name smuggled in.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from email import policy
from email.errors import HeaderParseError
from email.headerregistry import Address
from email.message import EmailMessage
from typing import Mapping, Optional, Sequence

#: Headers `build` sets itself; an `extra_headers` entry naming one is refused rather than doubled.
_OWN_HEADERS = frozenset({"from", "to", "cc", "bcc", "subject", "reply-to", "message-id", "content-type",
                          "content-transfer-encoding", "mime-version"})


@dataclass(frozen=True)
class OutboundEmail:
    """One system message to one recipient (seam S4). `html` is trusted markup (`build`'s rule); `headers` are extra
    headers (`build`'s `extra_headers`); `reply_to` is unset by default (replies land in SES inbound)."""

    to: str
    subject: str
    text: str
    html: Optional[str]
    mail_type: str
    essential: bool
    headers: Mapping[str, str] = field(default_factory=dict)
    reply_to: Optional[str] = None


#: CRLF, folding only past RFC 5322's hard limit of 998. At the default 78, a header with one long token — a
#: `List-Unsubscribe` URL (`app/email_unsubscribe.py`) — is rewritten as RFC 2047 encoded-words no client parses
#: as a URL. Body parts are unaffected: their transfer encoding already keeps lines short.
_WIRE_POLICY = policy.SMTP.clone(max_line_length=998)


def wire_bytes(message: EmailMessage) -> bytes:
    """`message` as the bytes a transport sends: CRLF line endings, every line within RFC 5322's limit."""
    return message.as_bytes(policy=_WIRE_POLICY)


class InvalidMessage(ValueError):
    """A message `build` will not produce; `str(exc)` says what to fix."""


def header_text(value: str, limit: int = 250) -> str:
    """A header value on one line: CR/LF would be a header injection, and `EmailMessage` refuses them."""
    flat = " ".join(value.split())
    return flat if len(flat) <= limit else flat[: limit - 1].rstrip() + "…"


def single_address(value: str) -> Optional[str]:
    """`value` as exactly one bare addr-spec (`a@b.example`), or `None`. A list, a display name, whitespace
    or an over-long value is refused, never trimmed into a different address."""
    candidate = value.strip()
    if not candidate or len(candidate) > 254 or any(ch in candidate for ch in ",;<> \t\r\n"):
        return None
    try:
        parsed = Address(addr_spec=candidate)
    except (ValueError, HeaderParseError, IndexError, TypeError):
        return None
    return parsed.addr_spec if parsed.domain and "." in parsed.domain else None


def _addresses(values: Sequence[str], field: str) -> list[str]:
    out = []
    for value in values:
        address = single_address(value)
        if address is None:
            raise InvalidMessage(f"{field} holds something that is not exactly one email address")
        out.append(address)
    return out


def build(
    sender: str,
    to: list[str],
    cc: list[str],
    *,
    subject: str,
    text: str,
    html: Optional[str] = None,
    sender_name: str = "",
    reply_to: Optional[str] = None,
    message_id: Optional[str] = None,
    extra_headers: Optional[Mapping[str, str]] = None,
) -> EmailMessage:
    """One message from `sender` (a bare address; `sender_name` its display name) to `to` and `cc`.

    `text` is always the text/plain part; `html`, when given, is added as a text/html alternative. `html` is trusted
    as markup: the caller must already have escaped any untrusted value in it. Raises `InvalidMessage` when the sender,
    a recipient or Reply-To is not exactly one address, when there is no `to` recipient, or when an extra header
    names one `build` sets itself or is not a plain header name."""
    from_address = single_address(sender)
    if from_address is None:
        raise InvalidMessage("the sender is not exactly one email address")
    recipients = _addresses(to, "To")
    copies = _addresses(cc, "Cc")
    if not recipients:
        raise InvalidMessage("a message needs at least one To recipient")
    message = EmailMessage()
    message["From"] = (
        str(Address(display_name=header_text(sender_name), addr_spec=from_address)) if sender_name.strip()
        else from_address
    )
    message["To"] = ", ".join(recipients)
    if copies:
        message["Cc"] = ", ".join(copies)
    message["Subject"] = header_text(subject)
    if reply_to is not None:
        reply = single_address(reply_to)
        if reply is None:
            raise InvalidMessage("Reply-To is not exactly one email address")
        message["Reply-To"] = reply
    if message_id is not None:
        message["Message-ID"] = header_text(message_id, limit=998)
    for name, value in (extra_headers or {}).items():
        if name.lower() in _OWN_HEADERS or not name or any(ch in name for ch in ": \t\r\n"):
            raise InvalidMessage(f"{name!r} cannot be set as an extra header")
        message[name] = header_text(value, limit=998)
    message.set_content(text)
    if html is not None:
        message.add_alternative(html, subtype="html")
    return message
