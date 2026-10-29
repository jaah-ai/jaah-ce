"""Who sent an inbound message, as every inbound path judges it: system mail (`ses_inbound`) and the email triggers
(`trigger_intake`), which ship in every edition. Stdlib only.

`from_domain` is the From domain as we parse it, `aligned` whether an authenticated domain vouches for it, and
`DAILY_ROUTED_LIMIT` the per-sender cap on routed messages (`ses_inbound.over_rate_limit` enforces it).
"""
from __future__ import annotations

import re
from email.utils import parseaddr

# §9.7: routed messages per (authenticated sender domain, target org) in any 24 h.
DAILY_ROUTED_LIMIT = 50

#: A plain host name: no comment, encoded word, NUL or look-alike space a reader might drop or keep.
_PLAIN_DOMAIN = re.compile(r"[a-z0-9-]+(?:\.[a-z0-9-]+)+")


def address_domain(address: str) -> str:
    """The lower-cased part after an address's last `@`; "" when it has none."""
    return address.rpartition("@")[2].strip().strip(">").lower() if "@" in address else ""


def aligned(from_domain: str, authenticated: str) -> bool:
    """Whether an authenticated domain aligns with the From domain we parsed: the same domain, or one a subdomain of
    the other. Stricter than DMARC's relaxed alignment, which needs the public-suffix list: siblings under one
    organizational domain (`news.example.com` signed by `mail.example.com`) do not align here, so such mail fails
    closed."""
    return bool(from_domain and authenticated) and (
        from_domain == authenticated or from_domain.endswith("." + authenticated) or authenticated.endswith("." + from_domain))


def from_domain(address: str) -> str:
    """The lower-cased domain of a stored `from_address` (or a From header) as we parse it; "" when it names none or
    one that is not a plain host name."""
    domain = address_domain(parseaddr(address or "")[1])
    return domain if _PLAIN_DOMAIN.fullmatch(domain) else ""
