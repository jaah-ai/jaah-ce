"""Canonical user-preferences shape. Stored server-side in admin_users.preferences (JSONB)."""
from __future__ import annotations

import logging
import re
from typing import Any, Literal

from pydantic import Field, ValidationError, field_validator, model_validator

from app.entity_shape import StrictModel
from app.models.identity import AdminUser

log = logging.getLogger("cockpit-preferences")

# Slack member/bot ids (`U…`/`W…`), spec R2.6. Bounded because an over-long stored id fails at Slack as
# `ok:false`, which does not fall through to the webhook (R3.8). Real ids are 9-13 chars.
_SLACK_USER_ID_RE = re.compile(r"^[UW][A-Z0-9]{2,32}$")

# Warn once per process when a stored preferences document degrades a field to its default (as
# `alert_relay._warn_once`).
_warned_bad_preferences = False


# The rail lists' caps, enforced by truncation on write and read alike.
TASK_RAIL_LIST_CAPS: dict[str, int] = {
    "task_rail_pins": 50, "task_rail_expanded": 500, "task_muted": 64, "task_hidden_columns": 32,
    "task_pinned": 64,
}

# A `task_muted` entry: a project's or feature's rail scope key.
_MUTE_KEY_RE = re.compile(
    r"project:[1-9][0-9]*|feature:[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)

# A `task_hidden_columns` entry: a board slot key (`apps/web/src/lib/taskColumns.ts` owns the vocabulary).
_COLUMN_KEY_RE = re.compile(r"[a-z]{1,16}")

# The fields a write that omits them keeps from the stored document (`preferences_to_store`).
CARRIED_FORWARD_FIELDS: tuple[str, ...] = (
    "task_rail_pins",
    "task_rail_expanded",
    "task_muted",
    "task_pinned",
    "task_done_expanded",
    # Written from the board's Filter & view menu, never from the settings form.
    "task_hidden_columns",
)

# Keys no release declares any more that a stored document or an OUTGOING web bundle may still carry: the web
# PUTs back the whole document it was served, so a tab loaded before the retirement would 422 every save.
# Dropped in `UserPreferences._drop_retired_keys`, the one inbound seam.
RETIRED_PREFERENCE_KEYS: frozenset[str] = frozenset({
    "new_task_project", "task_pane_mode", "nav_collapsed_groups", "welcome_wizard_dismissed",
    "wide_mode", "focused_project_id",
    # Alerts live in `notification_channels` (`app/notification_channels.py`).
    "notifications",
})


class UserPreferences(StrictModel):
    # `None` = never picked: the console follows the operator's `default_theme` knob (`app/operator_settings/`),
    # served beside this document as `AdminOut.default_theme`. Stored only once the user picks.
    theme: Literal["light", "dark"] | None = None
    # The new-user guide's two flags: whether the "Get started" card on `/tasks` is dismissed, and which
    # steps are done. Side-effect flags written by the card, not settings (so not in `UserSettingsDraft`;
    # see `lib/userSettings.ts`). Server-side (Q4) so a dismissal follows the user to every device.
    onboarding_dismissed: bool = False
    # Steps whose completion is not otherwise observable (today only `choose_theme`). Derived steps (GitHub
    # connected, first task created) are never stored, since a stored claim could go stale. Plain
    # `list[str]`, not a Literal: the ids live in `lib/onboarding.ts`, and a Literal would 422 a new one until
    # the API redeployed.
    onboarding_steps_done: list[str] = Field(default_factory=list)
    # Whether this user hid the company-setup card. Per-user by design: its progress is org-wide and derived
    # (`routes/org_setup.py`), so only the dismissal is stored, and one admin hiding it hides it for nobody else.
    company_setup_dismissed: bool = False
    # The task rail (`GET /api/tasks/tree`). Pins and expanded nodes are scope keys shared with the web
    # (`'all' | 'project:<id>' | 'feature:<uuid>'`), pins in display
    # order. Plain `list[str]` for `onboarding_steps_done`'s reason; a stale key is harmless. Capped by
    # truncation (`_cap_rail_lists`), never a 422.
    task_rail_pins: list[str] = Field(default_factory=list)
    task_rail_expanded: list[str] = Field(default_factory=list)
    # The projects and features this user MUTED (`'project:<id>' | 'feature:<uuid>'`, the rail's scope keys):
    # their tasks show only when the muted node itself (or something under it) is selected, and the rail greys
    # them and counts them in no parent. This user's alone. The web owns the rule (`lib/taskMute.ts`) and sends
    # it as the board's `exclude_*` params. Capped at the `exclude_*` params' own bound.
    task_muted: list[str] = Field(default_factory=list)
    # Whether the board's Done section is open. Collapsed by default (AC-6.5).
    task_done_expanded: bool = False
    # The board columns this user HID from Filter & view > Columns (slot keys; the web owns the vocabulary,
    # `lib/taskColumns.ts`). Malformed entries are dropped (`_drop_malformed_columns`); an unknown key is ignored by the web.
    task_hidden_columns: list[str] = Field(default_factory=list)
    # The tasks this user PINNED (task ids): each sits at the top of whatever board section draws it, whatever
    # the order. This user's alone. The server orders the board page by it (`routes/tasks/listing.py::board_page`)
    # so a pinned row is in the first window; the web lifts it within its section (`lib/taskPin.ts`). A pin
    # outlives Done and archive; an id the caller cannot see orders nothing.
    task_pinned: list[int] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def _drop_retired_keys(cls, data: Any) -> Any:
        """Drop `RETIRED_PREFERENCE_KEYS` before `extra="forbid"` sees them."""
        if isinstance(data, dict) and not RETIRED_PREFERENCE_KEYS.isdisjoint(data):
            return {key: value for key, value in data.items() if key not in RETIRED_PREFERENCE_KEYS}
        return data

    @field_validator("task_rail_pins", "task_rail_expanded", "task_muted", "task_hidden_columns", "task_pinned", mode="after")
    @classmethod
    def _cap_rail_lists(cls, value: list, info) -> list:
        """Keep the most recent entries (the tail: a new pin or expansion is appended) up to the cap."""
        cap = TASK_RAIL_LIST_CAPS[info.field_name]
        return value[-cap:] if len(value) > cap else value

    @field_validator("task_hidden_columns", mode="before")
    @classmethod
    def _drop_malformed_columns(cls, value: Any) -> Any:
        """Keep only short lowercase slot keys; anything else is dropped, not refused, as the caps."""
        if not isinstance(value, list):
            return value
        return [v for v in value if isinstance(v, str) and _COLUMN_KEY_RE.fullmatch(v)]

    @field_validator("task_pinned", mode="before")
    @classmethod
    def _drop_malformed_pins(cls, value: Any) -> Any:
        """Keep only positive integer task ids, once each (the LAST occurrence: the newest pin), in pin order;
        anything else is dropped, not refused. Linear: the cap applies only after this pass."""
        if not isinstance(value, list):
            return value
        ids = [v for v in value if isinstance(v, int) and not isinstance(v, bool) and v > 0]
        return list(reversed(dict.fromkeys(reversed(ids))))

    @field_validator("task_muted", mode="before")
    @classmethod
    def _drop_malformed_mutes(cls, value: Any) -> Any:
        """Keep only `project:<id>` / `feature:<uuid>` entries: a malformed one, sent on as an `exclude_*` param,
        would 422 every board read. Dropped, not refused, as the caps."""
        if not isinstance(value, list):
            return value
        return [v for v in value if isinstance(v, str) and _MUTE_KEY_RE.fullmatch(v)]

    @classmethod
    def from_stored(cls, raw: dict | None) -> "UserPreferences":
        """Read tolerant, write strict. Runs on every user read (`routes/users.py`, `routes/auth.py`), so it must
        never raise.

        A value written by a newer release (two-release overlap) would otherwise 500 user reads. So an invalid field degrades to its default and warns once; the
        write paths stay strict.
        """
        global _warned_bad_preferences
        # The JSONB value may be a scalar or list, which would raise below outside any except.
        data = raw if isinstance(raw, dict) else {}
        known = {k: data[k] for k in cls.model_fields if k in data}
        # Degrade per offending field: any field can raise under a newer release. Each pass removes a key from
        # `known` or returns, so the loop terminates.
        while True:
            try:
                return cls(**known)
            except ValidationError as err:
                bad = {str(e["loc"][0]) for e in err.errors() if e["loc"]}
                bad &= set(known)
                if not bad:
                    # Nothing left to drop; all-defaults is still better than raising on a read path.
                    return cls()
                # Field locations only, never values: `str(err)` echoes input.
                message = "stored preferences did not validate — falling back to defaults for: %s"
                if _warned_bad_preferences:
                    log.debug(message, ", ".join(sorted(bad)))
                else:
                    _warned_bad_preferences = True
                    log.warning(message, ", ".join(sorted(bad)))
                for field in bad:
                    known.pop(field)


def preferences_to_store(prefs: UserPreferences, stored: dict | None) -> dict:
    """The one door a write path stores a preferences document through (`PUT /api/auth/me`,
    `PATCH /api/users/{id}`), so a new write path cannot bypass it.

    Carries each `CARRIED_FORWARD_FIELDS` member forward when the incoming document omits it
    (`model_fields_set`), because writes replace the document wholesale: a web bundle that predates the task
    rail would wipe its pins and layout. Other fields are visible and one-click recoverable.
    """
    # Through `from_stored`, so an unrecognised stored value degrades rather than being carried forward.
    omitted = [name for name in CARRIED_FORWARD_FIELDS if name not in prefs.model_fields_set]
    kept = UserPreferences.from_stored(stored)
    prefs = prefs.model_copy(update={name: getattr(kept, name) for name in omitted})
    return preferences_document(prefs)


def preferences_document(prefs: UserPreferences) -> dict:
    """`prefs` as the JSON document every path stores AND serves (`routes/auth.py`, `routes/users.py`).

    An unset `theme` is left OUT rather than written as `null`: a release that predates the nullable theme
    reads an absent key as its default but 422s a `null` the web would send back to it while two releases
    overlap."""
    # `mode="json"`: JSONB takes only what `json.dumps` can encode.
    document = prefs.model_dump(mode="json")
    if document["theme"] is None:
        del document["theme"]
    return document


#: The display preferences that are the PERSON's (`identities.preferences`), the same in every org they act
#: as. Everything else — the task rail, the onboarding and setup flags — is the membership's (`admin_users.preferences`). The single
#: home of that split; `stored_preferences` reads it and `store_preferences` writes it.
IDENTITY_PREFERENCE_KEYS: tuple[str, ...] = ("theme",)


def stored_preferences(user: AdminUser) -> dict:
    """`user`'s preferences document as one dict: this membership's keys, with the person's display keys
    (`IDENTITY_PREFERENCE_KEYS`) read from the identity — except on a membership still waiting on its
    invitation, which reads only its own (`store_preferences`)."""
    merged = dict(user.preferences) if isinstance(user.preferences, dict) else {}
    if user.invitation_id is not None:
        return merged
    for key in IDENTITY_PREFERENCE_KEYS:
        merged.pop(key, None)
    identity = user._loaded_identity()
    held = identity.preferences if identity is not None and isinstance(identity.preferences, dict) else {}
    merged.update({key: held[key] for key in IDENTITY_PREFERENCE_KEYS if key in held})
    return merged


def store_preferences(user: AdminUser, prefs: UserPreferences) -> None:
    """Write a preferences document through `preferences_to_store`: the display keys onto the identity, the
    rest onto the membership."""
    document = preferences_to_store(prefs, stored_preferences(user))
    identity = user._loaded_identity()
    # A membership still waiting on its invitation (`AdminUser.invitation_id`) is not the person's yet, and
    # the display keys are the person's: a save there leaves them as they were. (Changed on the membership,
    # the outgoing release's revision 0360 trigger would copy them onto the person.)
    if user.invitation_id is not None:
        held = user.preferences if isinstance(user.preferences, dict) else {}
        for key in IDENTITY_PREFERENCE_KEYS:
            if key in held:
                document[key] = held[key]
            else:
                document.pop(key, None)
        user.preferences = document
        return
    if identity is not None:
        held = dict(identity.preferences) if isinstance(identity.preferences, dict) else {}
        # A key the document leaves out (an unset `theme`, `preferences_document`) is removed, not kept.
        for key in IDENTITY_PREFERENCE_KEYS:
            if key in document:
                held[key] = document[key]
            else:
                held.pop(key, None)
        # A retired key the outgoing release wrote onto the person is dropped here, its one write seam.
        for key in RETIRED_PREFERENCE_KEYS:
            held.pop(key, None)
        identity.preferences = held
        document = {key: value for key, value in document.items() if key not in IDENTITY_PREFERENCE_KEYS}
    user.preferences = document
