"""Print a signed-in session cookie for a fixture login — `./dc fixture-session <email>` (e2e, local dev).

The e2e suite's superadmin cannot sign in with a password at all (PRD D7, `app/login_funnel.py`), and a
TOTP step serves one sign-in, so the suite takes its sessions from here instead of the form. Refused unless
the process is a throwaway environment (`app/dev_bypass.py::is_fixture_env`): a shell that can run this can
already read `SESSION_SECRET`, so it grants nothing a real environment's operator lacks, but it never runs
there. Prints `<cookie name>=<value>` on stdout and nothing else.
"""
from __future__ import annotations

import sys

from app import auth, edition_features, identities
from app.db import SessionLocal
from app.dev_bypass import is_fixture_env
from app.tenancy import seam


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print("usage: python -m app.fixture_session <email>", file=sys.stderr)
        return 2
    if not is_fixture_env():
        print("fixture_session: refused — APP_ENV is not local, ci or test", file=sys.stderr)
        return 2
    with seam.identity_scope(), SessionLocal() as session:
        user = identities.first_membership(session, args[0])
        if user is None or user.is_system or user.disabled_at is not None:
            print(f"fixture_session: no loginable user {args[0]!r}", file=sys.stderr)
            return 1
        first = "oauth" if auth.password_refusal(user) is not None else "pwd"
        # The Terms claim as the funnel would set it (`login_funnel._mint`), so a gated owner's fixture is gated.
        terms = None
        # An edition without sign-up has no operator-submitted application to hold its owner at the Terms.
        if edition_features.enabled("signup"):
            from app import signup

            terms = auth.TERMS_PENDING if signup.terms_pending(user.id, user.org_id) else None
        token = auth.create_session_token(
            user.id, user.session_epoch or 0, iepoch=auth.identity_epoch(user), amr=(first, "otp"), terms=terms,
        )
    print(f"{auth.COOKIE_NAME}={token}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
