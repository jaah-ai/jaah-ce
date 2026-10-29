"""Canonical user-preferences shape. Stored server-side in admin_users.preferences (JSONB)."""
from __future__ import annotations

import logging
import re
from typing import Annotated, Any, Literal

from fastapi import HTTPException
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from app.alert_relay import ALERT_KINDS, NODE_KINDS, PAGE_KINDS
from app.entity_shape import StrictModel
from app.models.identity import AdminUser
from app.tenancy.ids import is_operator_plane

log = logging.getLogger("cockpit-preferences")

# Slack member/bot ids (`U…`/`W…`), spec R2.6. Bounded because an over-long stored id fails at Slack as
# `ok:false`, which does not fall through to the webhook (R3.8). Real ids are 9-13 chars.
_SLACK_USER_ID_RE = re.compile(r"^[UW][A-Z0-9]{2,32}$")

# Warn once per process when a stored `notifications` blob degrades to the default (as
# `alert_relay._warn_once`).
_warned_bad_notifications = False

NOTIFICATION_METHOD_TYPES: tuple[str, ...] = ("slack", "email", "telegram")
"""The channels a user may add a delivery method for, in the order the panel offers them.

A channel is a member here plus a destination arm in `notify_targets` (`_slack_destination`,
`email_destination`, `telegram_destination`). `email` carries no address: it is delivered to the user's
login email (`admin_users.email`), so its `user_id` is always empty (`app/email_notify.py`).
`telegram` carries none either: its chat is the PERSON's link (`telegram_links`, proven by the bot's
deep link, `app/telegram_link.py`), so the method only says this membership wants it.
"""

#: The method types whose `user_id` is always empty: the destination is held elsewhere, never typed.
_ADDRESSLESS_METHOD_TYPES: frozenset[str] = frozenset({"email", "telegram"})

# The subscribable events, in panel order, derived from `alert_relay.ALERT_KINDS`.
# Six extra names with no kind of their own: `question` (a `blocked` alert whose question card resolves;
# derived in `notify_targets`), `mention` (a comment mention, produced only by `app/comment_notify.py`),
# `reaction` (an emoji on your comment, only by `app/comment_reactions.py`) — neither postable over the
# agent API — `support` (a support ticket's activity, only by `app/durable/support_notify_queue.py`) and
# `billing` (a failed payment, refund or expiring card, only by `app/billing/notices.py`'s durable notice email) and
# `budget` (a project or group budget at 80% or 100%, only by `app/budgets.py`; seeded from `billing` by revision
# 202610240737) and
# `triggers` (a trigger owner's stopped-trigger email and daily digest, only by `app/trigger_notices.py`) and
# `api_key_expiry` (a Jaah API key's owner, reminded before it expires, through `app/user_notify.py`) and
# `automation_failures` (an org's Owners and Admins, told an automation is failing or stalling, `app/automation_alerts.py`).
# The pages (`alert_relay.PAGE_KINDS`) are left out: an alarm reaches every superadmin and nobody may mute it.
NOTIFICATION_EVENTS: tuple[str, ...] = (
    "done", "question", "mention", "reaction", "support", "billing", "budget", "triggers", "api_key_expiry",
    "automation_failures",
) + tuple(
    kind for kind in ALERT_KINDS if kind != "done" and kind not in PAGE_KINDS
)

#: The events only an org's Owners and Admins (and operators) receive, so only they may see or set them: billing
#: notices and budget warnings (`billing/notices.py::owners_and_admins`), the node-level alerts (`alert_relay.NODE_KINDS`) and API key
#: expiry (only an Owner or Admin may hold a key), and automation failures (`app/automation_alerts.py`).
#: Every other event is gated per message at delivery instead (`notify_targets.may_read_task`).
ORG_ADMIN_EVENTS: frozenset[str] = frozenset(
    {"billing", "budget", "api_key_expiry", "automation_failures", *NODE_KINDS}
)


def subscribable_events(session: Any, user: AdminUser) -> list[str]:
    """The `NOTIFICATION_EVENTS` `user` may see and set, in panel order — served as `notification_events` beside
    the preferences document (`routes/auth.py`, `routes/users.py`) and held on write (`hold_unsubscribable_events`)."""
    from app import access

    admin = is_operator_plane(user.org_id, user.is_superadmin) or (
        access.org_role_of(session, user.id) in (access.ORG_OWNER, access.ORG_ADMIN)
    )
    return [event for event in NOTIFICATION_EVENTS if admin or event not in ORG_ADMIN_EVENTS]


class NotificationEventPreference(BaseModel):
    """One row of the events table: whether this event is delivered at all, and how much of it.

    `detail="brief"` reduces the card to headline plus link, no question prose or buttons
    (`alert_relay.relay_alert`); chosen per event.

    `extra="ignore"` and all fields defaulted: reached only through `NotificationsPreferences._tolerate`, so
    nothing here may raise. Strictness lives on the write path.
    """

    model_config = ConfigDict(extra="ignore")

    enabled: bool = True
    detail: Literal["full", "brief"] = "full"
    #: False: this event is not emailed — a one-click unsubscribe (`app/email_unsubscribe.py`) or the row's Email
    #: box. Slack and Telegram are unaffected.
    email: bool = True


class NotificationMethod(BaseModel):
    """One place a user's alerts are delivered — a channel plus the id they are reachable at there.

    `user_id` is not required here: the write path refuses an empty id and delivery treats a missing one as
    `REASON_RECIPIENT_MISSING`. Requiring it would degrade the whole block on read.
    """

    model_config = ConfigDict(extra="ignore")

    type: Literal["slack", "email", "telegram"] = "slack"
    #: The channel-native id the recipient is reachable at — for Slack, a member id (e.g. `U0123ABCD`).
    #: Always empty for `email` (the login email) and `telegram` (the person's linked chat).
    user_id: str = ""
    #: Slack only: the workspace (`team_id`) the member id was proven in by Connect (`app/slack_link.py`). A tenant
    #: org's alerts go only to a member proven in that org's workspace (`app/slack_workspaces.py`); empty for an id
    #: proven before workspaces existed or typed by the operator, which the home workspace alone accepts. Written by
    #: Connect only: a save carries the stored one forward (`preferences_to_store`) and may not change it.
    team_id: str = ""
    #: Slack only: the member's Slack display name, read by Connect (`slack_link.redeem`) so the channel line names
    #: a person rather than an id. Empty for a link made before it was read, or when Slack would not say; the
    #: line then shows the member id. Connect-written and carried forward exactly as `team_id` is.
    display_name: str = ""


class NotificationsPreferences(StrictModel):
    """Where a user's alerts are delivered and which of them they want.

    `methods` (where) and `events` (subscription) are independent. Empty `methods` is the default: deliver
    nothing. The Slack credential is org-wide (`operator_settings` knob
    `notifications_slack_credential_id`); a user supplies only their member id.

    Read-tolerant: `_tolerate` coerces stored shapes and drops what it cannot use, because both write paths
    PUT back what they were served and a raise would permanently erase a user's methods. Strictness lives in
    `validate_notifications_preferences`. Strict on extra keys anyway: `_tolerate` has already dropped
    the ones a stored document may legitimately carry.
    """

    #: Every place this user's alerts go. At most one per `type` (enforced on write, deduplicated on read).
    methods: list[NotificationMethod] = Field(default_factory=list)
    #: The subscription, one entry per `NOTIFICATION_EVENTS` member, always complete after validation.
    events: dict[str, NotificationEventPreference] = Field(default_factory=dict)

    @model_validator(mode="before")
    @classmethod
    def _tolerate(cls, data: Any) -> Any:
        """Coerce any stored document into this shape, dropping what makes no sense. Never raises.

        1. Drop undeclared keys (newer or retired), else `extra="forbid"` raises and the block degrades.
        2. Clean `methods`: drop non-dicts, unknown and duplicate types (first wins).
        3. Clean `events` per entry, so one bad row costs that row only.
        """
        if not isinstance(data, dict):
            return data
        data = {key: value for key, value in data.items() if key in cls.model_fields}

        raw_methods = data.get("methods")
        if isinstance(raw_methods, list):
            clean_methods: list[dict[str, Any]] = []
            seen_types: set[str] = set()
            for entry in raw_methods:
                if not isinstance(entry, dict):
                    continue
                method_type = entry.get("type")
                if method_type not in NOTIFICATION_METHOD_TYPES or method_type in seen_types:
                    continue
                seen_types.add(method_type)
                member = entry.get("user_id")
                clean = {"type": method_type, "user_id": member if isinstance(member, str) else ""}
                team = entry.get("team_id")
                if method_type == "slack" and isinstance(team, str):
                    # The workspace Connect proved the member id in (`NotificationMethod.team_id`).
                    clean["team_id"] = team
                name = entry.get("display_name")
                if method_type == "slack" and isinstance(name, str):
                    clean["display_name"] = name
                clean_methods.append(clean)
            data["methods"] = clean_methods
        else:
            data["methods"] = []

        raw_events = data.get("events")
        clean_events: dict[str, dict[str, Any]] = {}
        if isinstance(raw_events, dict):
            for event, value in raw_events.items():
                if event not in NOTIFICATION_EVENTS or not isinstance(value, dict):
                    continue
                enabled = value.get("enabled")
                detail = value.get("detail")
                email = value.get("email")
                clean_events[event] = {
                    "enabled": enabled if isinstance(enabled, bool) else True,
                    "detail": detail if detail in ("full", "brief") else "full",
                }
                # Only when given, so a save from a bundle that predates it keeps the stored one (`_carry_event_email`).
                if isinstance(email, bool):
                    clean_events[event]["email"] = email
        data["events"] = clean_events
        return data

    @model_validator(mode="after")
    def _complete_events(self) -> "NotificationsPreferences":
        """Fill every event a stored document did not mention with the default — enabled, full detail.

        Defaulting to silence would mute existing recipients with no visible symptom.
        """
        for event in NOTIFICATION_EVENTS:
            if event not in self.events:
                self.events[event] = NotificationEventPreference()
        return self

    def method_for(self, method_type: str) -> NotificationMethod | None:
        """This user's method of one type, or `None` — the read side of "at most one per type"."""
        return next((m for m in self.methods if m.type == method_type), None)

    def wants(self, event: str) -> bool:
        """Whether this user has subscribed to `event`.

        An event outside the vocabulary (from a newer producer) is delivered, not muted: an unwanted alert is
        recoverable, a silently dropped one is not.
        """
        pref = self.events.get(event)
        return True if pref is None else pref.enabled

    def emails(self, event: str) -> bool:
        """Whether `event` may be emailed (not unsubscribed); True outside the vocabulary, on `wants`' reasoning."""
        pref = self.events.get(event)
        return True if pref is None else pref.email

    def detail_for(self, event: str) -> str:
        """`"full"` or `"brief"` for `event`; `"full"` outside the vocabulary, on `wants`' reasoning."""
        pref = self.events.get(event)
        return "full" if pref is None else pref.detail


# The rail lists' caps, enforced by truncation on write and read alike.
TASK_RAIL_LIST_CAPS: dict[str, int] = {"task_rail_pins": 50, "task_rail_expanded": 500, "task_muted": 64}

# A `task_muted` entry: a project's or feature's rail scope key.
_MUTE_KEY_RE = re.compile(
    r"project:[1-9][0-9]*|feature:[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)

# The fields a write that omits them keeps from the stored document (`preferences_to_store`).
CARRIED_FORWARD_FIELDS: tuple[str, ...] = (
    # A knob save (`auth.tsx::updatePreferences`) omits it, so a stale tab cannot undo a Slack link the bot
    # wrote meanwhile (`app/slack_link.py`).
    "notifications",
    "task_rail_pins",
    "task_rail_expanded",
    "task_muted",
    "task_done_expanded",
    # Set from the rail's row menu as well as the settings form; a bundle that predates it must not clear it.
    "focused_project_id",
)

# Keys no release declares any more that a stored document or an OUTGOING web bundle may still carry: the web
# PUTs back the whole document it was served, so a tab loaded before the retirement would 422 every save.
# Dropped in `UserPreferences._drop_retired_keys`, the one inbound seam.
RETIRED_PREFERENCE_KEYS: frozenset[str] = frozenset({"new_task_project", "task_pane_mode"})


class UserPreferences(StrictModel):
    wide_mode: bool = False
    # `None` = never picked: the console follows the operator's `default_theme` knob (`app/operator_settings/`),
    # served beside this document as `AdminOut.default_theme`. Stored only once the user picks.
    theme: Literal["light", "dark"] | None = None
    # Sidebar nav groups the user has folded shut. Server-side so it follows the user across browsers.
    # Plain `list[str]`: group ids live in the frontend's NAV_GROUPS, and a Literal would 422 a new group
    # until the API redeployed. Unknown ids are harmless.
    nav_collapsed_groups: list[str] = Field(default_factory=list)
    # The new-user guide's two flags: whether the "Get started" card on `/tasks` is dismissed, and which
    # steps are done. Side-effect flags written by the card, not settings (so not in `UserSettingsDraft`;
    # see `lib/userSettings.ts`). Server-side (Q4) so a dismissal follows the user to every device.
    onboarding_dismissed: bool = False
    # Steps whose completion is not otherwise observable (today only `choose_theme`). Derived steps (GitHub
    # connected, first task created) are never stored, since a stored claim could go stale. Plain
    # `list[str]` for `nav_collapsed_groups`' reason (ids live in `lib/onboarding.ts`).
    onboarding_steps_done: list[str] = Field(default_factory=list)
    # Whether this user hid the company-setup card. Per-user by design: its progress is org-wide and derived
    # (`routes/org_setup.py`), so only the dismissal is stored, and one admin hiding it hides it for nobody else.
    company_setup_dismissed: bool = False
    # Once "skip the full-page setup wizard"; that page is gone and nothing writes this any more. Kept
    # as a stored field so an older client's saved preferences still validate (`extra="forbid"`).
    welcome_wizard_dismissed: bool = False
    notifications: NotificationsPreferences = Field(default_factory=NotificationsPreferences)
    # The task rail (`GET /api/tasks/tree`). Pins and expanded nodes are scope keys shared with the web
    # (`'all' | 'project:<id>' | 'unfiled:<id>' | 'feature:<uuid>'`), pins in display
    # order. Plain `list[str]` for `nav_collapsed_groups`' reason; a stale key is harmless. Capped by
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
    # The user's FOCUSED PROJECT: where a bare `/tasks` opens and where a task created on All projects is
    # filed. `None` = All projects. Not checked against the user's reach: a project they cannot see is
    # ignored by the web (it degrades to All projects), which a stored id outliving a revoked grant needs anyway.
    # Strict: a `True` or `"5"` would otherwise be coerced into an id.
    focused_project_id: Annotated[int, Field(gt=0, strict=True)] | None = None

    @model_validator(mode="before")
    @classmethod
    def _drop_retired_keys(cls, data: Any) -> Any:
        """Drop `RETIRED_PREFERENCE_KEYS` before `extra="forbid"` sees them."""
        if isinstance(data, dict) and not RETIRED_PREFERENCE_KEYS.isdisjoint(data):
            return {key: value for key, value in data.items() if key not in RETIRED_PREFERENCE_KEYS}
        return data

    @field_validator("task_rail_pins", "task_rail_expanded", "task_muted", mode="after")
    @classmethod
    def _cap_rail_lists(cls, value: list[str], info) -> list[str]:
        """Keep the most recent entries (the tail: a new pin or expansion is appended) up to the cap."""
        cap = TASK_RAIL_LIST_CAPS[info.field_name]
        return value[-cap:] if len(value) > cap else value

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
        """Read tolerant, write strict. Runs on every user read (`routes/users.py`, `routes/auth.py`) and in
        `notify_targets.resolve_slack_target`, so it must never raise.

        A value written by a newer release (two-release overlap) would otherwise 500 user reads and break the
        whole alert path inside `record_alert`. So an invalid field degrades to its default and warns once; the
        write paths stay strict.
        """
        global _warned_bad_notifications
        # The JSONB value may be a scalar or list, which would raise below outside any except.
        data = raw if isinstance(raw, dict) else {}
        known = {k: data[k] for k in cls.model_fields if k in data}
        # Degrade per offending field, not only `notifications`: any field can raise under a newer release. Each
        # pass removes a key from `known` or returns, so the loop terminates.
        while True:
            try:
                return cls(**known)
            except ValidationError as err:
                bad = {str(e["loc"][0]) for e in err.errors() if e["loc"]}
                bad &= set(known)
                if not bad:
                    # Nothing left to drop; all-defaults is still better than raising on a read path.
                    return cls()
                # Field locations only, never values: member ids are not logged (R3.13), and `str(err)` echoes input.
                message = "stored preferences did not validate — falling back to defaults for: %s"
                if _warned_bad_notifications:
                    log.debug(message, ", ".join(sorted(bad)))
                else:
                    _warned_bad_notifications = True
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
    if omitted:
        kept = UserPreferences.from_stored(stored)
        prefs = prefs.model_copy(update={name: getattr(kept, name) for name in omitted})
    return preferences_document(_carry_event_email(_carry_slack_connect(prefs, stored), stored))


def hold_unsubscribable_events(prefs: UserPreferences, stored: dict | None, allowed: list[str]) -> UserPreferences:
    """`prefs` with every event outside `allowed` (`subscribable_events`) reset to its stored choice, so a write
    cannot set an event its user may not see. Called by both write routes before the "notifications changed"
    test; a choice stored while the user held the role survives a demotion for when they hold it again."""
    if "notifications" not in prefs.model_fields_set:
        # Omitted: `preferences_to_store` carries the stored block forward whole; a copy here would mark it set.
        return prefs
    held = UserPreferences.from_stored(stored).notifications.events
    events = {
        event: pref if event in allowed else held.get(event, NotificationEventPreference())
        for event, pref in prefs.notifications.events.items()
    }
    return prefs.model_copy(update={"notifications": prefs.notifications.model_copy(update={"events": events})})


#: What Connect writes on the Slack method beside the member id, and a save carries forward.
_SLACK_CONNECT_FIELDS = ("team_id", "display_name")


def _carry_slack_connect(prefs: UserPreferences, stored: dict | None) -> UserPreferences:
    """The stored Slack method's `_SLACK_CONNECT_FIELDS`, carried onto a save that keeps the same member id: `team_id`
    when the save omits it (a client that sends `{type, user_id}` back), `display_name` always — a label only Connect
    writes (`slack_link.redeem`, after this), so a stale tab's copy never overwrites a newer one. A different member id
    keeps whatever the save says (the validator has already refused a non-operator changing it)."""
    slack = prefs.notifications.method_for("slack")
    if slack is None:
        return prefs
    kept = UserPreferences.from_stored(stored).notifications.method_for("slack")
    if kept is None or kept.user_id != slack.user_id:
        return prefs
    update = {"display_name": kept.display_name}
    if not slack.team_id:
        update["team_id"] = kept.team_id
    if all(getattr(slack, name) == value for name, value in update.items()):
        return prefs
    methods = [
        method.model_copy(update=update) if method.type == "slack" else method
        for method in prefs.notifications.methods
    ]
    return prefs.model_copy(update={"notifications": prefs.notifications.model_copy(update={"methods": methods})})


def _carry_event_email(prefs: UserPreferences, stored: dict | None) -> UserPreferences:
    """Each event's stored `email` choice, carried onto a save that omits it: a tab loaded before the per-event Email
    box would otherwise undo a one-click unsubscribe (`app/email_unsubscribe.py`) it never showed."""
    kept = UserPreferences.from_stored(stored).notifications.events
    events = {
        event: pref.model_copy(update={"email": kept[event].email})
        if "email" not in pref.model_fields_set and event in kept else pref
        for event, pref in prefs.notifications.events.items()
    }
    return prefs.model_copy(update={"notifications": prefs.notifications.model_copy(update={"events": events})})


def preferences_document(prefs: UserPreferences) -> dict:
    """`prefs` as the JSON document every path stores AND serves (`routes/auth.py`, `routes/users.py`).

    An unset `theme` is left OUT rather than written as `null`: a release that predates the nullable theme
    reads an absent key as its default but 422s a `null` the web would send back to it while two releases
    overlap."""
    # `mode="json"`: JSONB takes only what `json.dumps` can encode.
    document = prefs.model_dump(mode="json")
    if document["theme"] is None:
        del document["theme"]
    # An empty `team_id` or `display_name` is left out, so a method that has none reads as it did before the field
    # existed.
    for method in (document.get("notifications") or {}).get("methods") or []:
        if isinstance(method, dict):
            for name in _SLACK_CONNECT_FIELDS:
                if not method.get(name):
                    method.pop(name, None)
    return document


#: The display preferences that are the PERSON's (`identities.preferences`), the same in every org they act
#: as. Everything else — `notifications` (channels, Slack link, events: per org by operator decision), the
#: task rail, the onboarding and setup flags — is the membership's (`admin_users.preferences`). The single
#: home of that split; `stored_preferences` reads it and `store_preferences` writes it.
IDENTITY_PREFERENCE_KEYS: tuple[str, ...] = ("theme", "wide_mode", "nav_collapsed_groups")


def stored_preferences(user: AdminUser) -> dict:
    """`user`'s preferences document as one dict: this membership's keys, with the person's display keys
    (`IDENTITY_PREFERENCE_KEYS`) read from the identity wherever it holds them — except on a membership still
    waiting on its invitation, which reads only its own (`store_preferences`)."""
    merged = dict(user.preferences) if isinstance(user.preferences, dict) else {}
    if user.invitation_id is not None:
        return merged
    identity = user._loaded_identity()
    held = identity.preferences if identity is not None and isinstance(identity.preferences, dict) else {}
    merged.update({key: held[key] for key in IDENTITY_PREFERENCE_KEYS if key in held})
    return merged


def store_preferences(user: AdminUser, prefs: UserPreferences) -> None:
    """Write a preferences document through `preferences_to_store`: the display keys onto the identity, the
    whole document onto the membership (its copy of the display keys is the legacy one, kept in step until
    the revision-2 contraction)."""
    document = preferences_to_store(prefs, stored_preferences(user))
    identity = user._loaded_identity()
    # A membership still waiting on its invitation (`AdminUser.invitation_id`) is not the person's yet, and
    # the display keys are the person's: a save there leaves them as they were. (Changed on the membership,
    # revision 0360's overlap trigger would copy them onto the person.)
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
        identity.preferences = held
    user.preferences = document


def validate_notifications_preferences(
    prefs: UserPreferences,
    *,
    actor: AdminUser,
    target: AdminUser,
) -> None:
    """The one shared validator for a `preferences.notifications` write (`PATCH /api/users/{id}` and
    `PUT /api/auth/me`); these rules may live nowhere else.

    Takes no session: the Slack credential is designated deployment-wide, so there is nothing to look up.
    `actor` is who is saving, `target` whose row; they differ on the admin surface, which is the whole
    authorization rule. Raises `HTTPException(422, ...)` naming the field; returns None on success.
    """
    methods = prefs.notifications.methods
    if not methods:
        # Nothing to deliver to, nothing to authorize; choosing your own subscriptions asserts nothing.
        return

    # A typed member id is self-asserted: a colleague's id would make our bot DM them your cards.
    # So only the operator types one, on any row; everyone else proves theirs through the one-time code
    # (`app/slack_link.py`, which writes past this check) and may here only keep or remove it. This function,
    # not the route, is the rule's home. Use `is_operator_plane` (`tenancy/ids.py`), never bare
    # `actor.is_superadmin`, which tenant superadmins also hold. No same-org check: the operator
    # surface edits users across orgs.
    operator = is_operator_plane(actor.org_id, actor.is_superadmin)
    if actor.id != target.id and not operator:
        raise HTTPException(
            status_code=422,
            # Says nothing about whether the id is already claimed, to avoid an enumeration oracle.
            detail=(
                "notifications.methods can only be set on your own account — a channel id is "
                "self-asserted, so it is not a value one user may claim on another's behalf"
            ),
        )

    for method in methods:
        if method.type in _ADDRESSLESS_METHOD_TYPES:
            # No address to claim: email goes to the target's own login email, Telegram to the chat the
            # target linked through the bot.
            if method.user_id:
                where = "the login email" if method.type == "email" else "the chat linked in Telegram"
                raise HTTPException(
                    status_code=422,
                    detail=f"notifications.methods[{method.type}].user_id must be empty — "
                           f"{method.type} goes to {where}",
                )
            continue
        if not method.user_id:
            raise HTTPException(
                status_code=422,
                detail=f"notifications.methods[{method.type}].user_id is required",
            )
        if method.type == "slack" and not _SLACK_USER_ID_RE.match(method.user_id):
            raise HTTPException(
                status_code=422,
                detail=(
                    "notifications.methods[slack].user_id must be a Slack member id "
                    "(e.g. 'U0123ABCD')"
                ),
            )

    # After the shape checks, so a malformed id gets the same answer on both routes. An id stored before this
    # rule (typed, not proven) is kept: unchanged passes, and so does re-saving it.
    slack = prefs.notifications.method_for("slack")
    stored = UserPreferences.from_stored(target.preferences).notifications.method_for("slack")
    # The workspace is never typed, not even by the operator: blank carries the stored one forward when the member id
    # is unchanged (`preferences_to_store`); anything else must be that stored one.
    if slack is not None and slack.team_id and (
        stored is None or stored.user_id != slack.user_id or slack.team_id != stored.team_id
    ):
        raise HTTPException(
            status_code=422,
            detail="notifications.methods[slack].team_id is set by Connect, not typed",
        )
    # On an unchanged member id the stored name wins whatever the save says (`preferences_to_store`); a name sent with
    # a new id would be typed.
    if slack is not None and slack.display_name and (stored is None or stored.user_id != slack.user_id):
        raise HTTPException(
            status_code=422,
            detail="notifications.methods[slack].display_name is set by Connect, not typed",
        )
    if slack is not None and not operator:
        if stored is None or stored.user_id != slack.user_id:
            raise HTTPException(
                status_code=422,
                # Names no stored id: the message must not reveal what the row holds.
                detail="notifications.methods[slack].user_id is set by Connect, not typed — DM the code "
                       "to the Jaah app to prove the Slack account is yours",
            )

    # No credential check: the Slack credential is designated deployment-wide, and a missing designation is
    # an operator problem surfaced by the panel and `notify_targets` (`REASON_CREDENTIAL_MISSING`).
