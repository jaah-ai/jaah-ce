"""The `[test] tester = "module:function"` hooks the vendor specs name: one thin binding each, onto a generic
tester in `credentials/testers.py`, for a vendor whose Test no declared HTTP call can express.

Imported only when a hook is first resolved (`testers.resolve_hook`), so importing `testers` here makes no cycle.
"""
from __future__ import annotations

from typing import Any

from app.credentials import testers

#: The one OAuth scope the FCM HTTP v1 API takes (firebase.google.com/docs/cloud-messaging/auth-server).
FIREBASE_MESSAGING_SCOPE = "https://www.googleapis.com/auth/firebase.messaging"


def firebase_cloud_messaging(config: dict[str, Any], deadline: float) -> testers.TestResult:
    """FCM has no read-only call, so the Test is the token grant every send starts with, for the FCM scope."""
    return testers.google_service_account_grant(
        "firebase_cloud_messaging", (FIREBASE_MESSAGING_SCOPE,), config, deadline,
    )
