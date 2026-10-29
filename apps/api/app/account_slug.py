"""The server-chosen slug of a new runner account.

A slug names the credential directory (`{jaah_home}/auth/{slug}`) and `uq_accounts_slug` makes it unique
across the whole fleet, while a caller sees only its own org's rows. A slug a person picks from what they
can see collides with rows they cannot (a bare-404 "not found"), so a new account's slug is
drawn here instead and the person names the account with `accounts.name`."""

from __future__ import annotations

import secrets
import string

from sqlalchemy import select

from app.db import SessionLocal
from app.models.credentials import Account
from app.tenancy import seam

#: Six lowercase alphanumerics: 36^6 ≈ 2.2e9 values, inside the wizard's `_SLUG_RE`.
ALPHABET = string.ascii_lowercase + string.digits
LENGTH = 6
_ATTEMPTS = 20


def new_account_slug() -> str:
    """A slug no `accounts` row holds, fleet-wide.

    Probed on its own session outside the org seam, as `routes/commands.py::_refuse_slug_held_by_another_org`
    does: uniqueness is a fleet fact, and only whether a value is free leaves this function. Not reserved —
    the insert's unique constraint still decides a race, which at this space is never expected."""
    with seam.detached(), seam.operator_scope(), SessionLocal() as fleet:
        for _ in range(_ATTEMPTS):
            candidate = "".join(secrets.choice(ALPHABET) for _ in range(LENGTH))
            if fleet.scalar(select(Account.id).where(Account.slug == candidate)) is None:
                return candidate
    raise RuntimeError(f"no free account slug after {_ATTEMPTS} draws")
