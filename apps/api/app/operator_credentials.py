"""The operator plane's designated credentials, opened for a reader that is nobody's tenant.

One home for the refusal every operator-tier credential knob shares — the site's reCAPTCHA key
(`app/site_enquiry.py`), the Telegram bot (`app/telegram_bot.py`), the tenant Gmail OAuth client
(`app/tenant_gmail/`): the row named by the knob must exist, be of the type the knob declared, and sit on the
operator plane — a tenant's row, whatever its type, is never served to a deployment-wide reader (the
`notify_targets._org_slack_bot_token` refusal).
"""
from __future__ import annotations

import logging
from typing import Any, Optional

from app.entity_shape import optional_uuid

log = logging.getLogger("operator-credentials")


def opened(credential_id: str, credential_type: str, *, consumer: str) -> Optional[dict[str, Any]]:
    """The opened config of the designated operator-plane credential, or None when it is unset, gone, of another
    type or off the operator plane. `consumer` names the reader on the `credential_read` row it lands."""

    from app import data_access_events
    from app.db import SessionLocal
    from app.models.credentials import Credential
    from app.tenancy import seam
    from app.tenancy.ids import OPERATOR_PLANE_ORG_IDS

    wanted = optional_uuid(credential_id)
    if wanted is None:
        return None
    # `detached()` first: the tenant Gmail OAuth client (`app/tenant_gmail/`) is asked for from org-bound requests
    # too — a member connecting their org's Gmail — and `operator_scope()` refuses to widen a bound org.
    with seam.detached(), seam.operator_scope(), SessionLocal() as session:
        row = session.get(Credential, wanted)
        if row is None or row.type != credential_type or row.org_id not in OPERATOR_PLANE_ORG_IDS:
            return None
        try:
            config = data_access_events.opened_config_for(session, row, consumer=consumer)
        except ValueError:
            log.warning("%s: credential id=%s cannot be opened — re-enter it", consumer.replace("_", " "), row.id)
            return None
        # The `credential_read` row lands before the config is used.
        session.commit()
        return config
