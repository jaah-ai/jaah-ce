"""The community edition's one user: the password rule, the lookup, and `python -m app.ce_admin reset-password`.

The edition has exactly one person and no mail, so a forgotten password is reset on the machine itself: the
command prompts (never an argument, never echoed), sets the new password and ends every session. The first-run
route (`app/routes/ce_setup.py`) reads the same rule and lookup.
"""
from __future__ import annotations

import getpass
import sys
from typing import Optional

from sqlalchemy import select
from sqlalchemy.orm import Session

from app import edition
from app.auth import PASSWORD_PROBLEM_MESSAGES, hash_password, password_problem

#: The community edition's floor; the cloud's general rule (`app/auth.py::PASSWORD_MIN`) still applies on top.
CE_PASSWORD_MIN = 12
CE_PASSWORD_TOO_SHORT = f"Use at least {CE_PASSWORD_MIN} characters."

_USAGE = "usage: python -m app.ce_admin reset-password"


def password_refusal(password: str, email: Optional[str]) -> Optional[str]:
    """Why `password` may not be the CE user's, as a sentence a person reads, or None."""
    if len(password) < CE_PASSWORD_MIN:
        return CE_PASSWORD_TOO_SHORT
    problem = password_problem(password, email)
    return PASSWORD_PROBLEM_MESSAGES[problem] if problem is not None else None


def people_query():
    """Every person row but the system actor's."""
    from app.models.identity import AdminUser

    return select(AdminUser).where(AdminUser.is_system.is_(False)).order_by(AdminUser.created_at)


def the_user(session: Session):
    """The community edition's one user, or None before the first run. Read under the operator scope."""
    from app.tenancy import seam

    with seam.operator_scope():
        return session.scalars(people_query().limit(1)).first()


def reset_password(session: Session, password: str) -> None:
    """Set the one user's password, end every session it holds and forget its trusted devices, audited as the
    emailed reset is (`app/password_reset.py::complete_reset`). Raises `ValueError` with a readable reason. Does
    not commit. A direct-console grant the node already holds is not revoked here: it lapses at its own expiry."""
    from app import identities, mfa
    from app.models.identity import set_identity_fields
    from app.user_activity import record_write

    user = the_user(session)
    if user is None:
        raise ValueError("No user yet — open the console to create one.")
    refusal = password_refusal(password, user.email)
    if refusal is not None:
        raise ValueError(refusal)
    set_identity_fields(user, password_hash=hash_password(password))
    identities.end_every_session(session, user)
    mfa.revoke_trusted_devices(session, user)
    record_write(
        session, None, "password_reset_completed", f"Password reset completed for {user.email}",
        data={"user_id": user.id, "email": user.email, "reset_id": None},
        org_id=user.org_id,
    )


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if not edition.IS_CE:
        print("this command belongs to the community edition", file=sys.stderr)
        return 2
    if args != ["reset-password"]:
        print(_USAGE, file=sys.stderr)
        return 2
    from app.db import SessionLocal

    password = getpass.getpass("New password: ")
    if getpass.getpass("Repeat it: ") != password:
        print("The two passwords differ — nothing changed.", file=sys.stderr)
        return 1
    with SessionLocal() as session:
        try:
            reset_password(session, password)
        except ValueError as exc:
            print(f"{exc} Nothing changed.", file=sys.stderr)
            return 1
        session.commit()
    print("Password changed. Every open session is signed out.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
