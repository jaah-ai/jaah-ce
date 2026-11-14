"""THE alert catalog: which notification events exist, the groups the Alerts panel shows them in, and the level a
channel resolves for one event. The single home of every name the panel, the delivery engine and the unsubscribe
link share — the web reads it in the channels document, never a copy.

An event is what a person chooses about (`done`, `mention`); a producer's alert KIND maps onto one through
`event_for` (`error` reads as `failed`, both node-full kinds as `node-full`). A kind `event_for` answers `None` for
is no longer a notification (`REMOVED_KINDS`): it is sent on no channel. A kind the catalog has never heard of (a
newer producer) is delivered — an unwanted alert is recoverable, a silently dropped one is not. The operator pages
(`alert_relay.PAGE_KINDS`) are outside the catalog: nobody may mute an alarm.

A channel's settings document (`notification_channels.settings`) stores only explicit choices:
`{"groups": {group: level}, "events": {event: level}}`. A level resolves `events[e]`, then `groups[e's group]`,
then `default_level` for the channel's type and the viewer's role. A group switch writes `groups[g]` and drops every
event choice inside it (`apply_choice`). Admin-only events and groups resolve `off` for a member and are refused on
write.

Stdlib-only: imported by the leaf delivery modules and the routes alike.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Optional

LEVELS: tuple[str, ...] = ("off", "brief", "full")
OFF, BRIEF, FULL = LEVELS

CHANNEL_TYPES: tuple[str, ...] = ("email", "slack", "telegram")


@dataclass(frozen=True)
class Event:
    """One alert a person may switch: its key, its panel label, and whether only Owners and Admins may see it."""

    key: str
    label: str
    admin_only: bool = False


@dataclass(frozen=True)
class Group:
    """One row of the Alerts panel: a switch for every event inside it. `admin_only` hides the whole group."""

    key: str
    label: str
    events: tuple[Event, ...]
    admin_only: bool = False


#: The groups, in panel order (PRD R6). Labels are sentence case, as the console's.
GROUPS: tuple[Group, ...] = (
    Group("assigned", "Assigned to you", (
        Event("assigned", "Assigned to you"),
        Event("question", "Question waiting"),
        Event("compliance", "Compliance nudge"),
    )),
    Group("messages", "Messages", (
        Event("mention", "Mentioned"),
        Event("reaction", "Reactions"),
        # An agent's message to named members (`org_notify`), which a human's task asked it to send.
        Event("message", "Direct messages"),
    )),
    Group("support", "Support", (Event("support", "Support"),)),
    Group("api", "API & automation", (
        Event("triggers", "Trigger stopped"),
        Event("api_key_expiry", "API key expiry", admin_only=True),
        Event("automation_failures", "Automation failures", admin_only=True),
    )),
    Group("progress", "Task progress", (
        Event("done", "Finished"),
        Event("blocked", "Blocked"),
        Event("failed", "Failed"),
        Event("merge_lane", "Merge fix filed"),
    )),
    # A project's releases (`app/releases/notify.py`).
    Group("deploys", "Releases", (
        Event("release_approval", "Approval needed"),
        Event("release_live", "Live"),
        Event("release_failed", "Release failed"),
        Event("release_rolled_back", "Rolled back"),
        Event("release_approval_reminder", "Approval reminder"),
        Event("release_approval_timeout", "Approval timed out"),
        Event("release_rejected", "Rejected"),
    )),
    Group("billing", "Billing", (
        Event("billing", "Billing", admin_only=True),
        Event("budget", "Budgets", admin_only=True),
    ), admin_only=True),
    Group("ai_login", "AI account login", (
        Event("login-expiring", "Login expiring", admin_only=True),
        Event("login-expired", "Login code needed", admin_only=True),
    ), admin_only=True),
    Group("nodes", "Node failures", (
        Event("node-full", "Node full", admin_only=True),
        Event("node-offline", "Node offline", admin_only=True),
        Event("runner-crashing", "Runner keeps crashing", admin_only=True),
    ), admin_only=True),
)

GROUPS_BY_KEY: dict[str, Group] = {group.key: group for group in GROUPS}
EVENTS: dict[str, Event] = {event.key: event for group in GROUPS for event in group.events}
_GROUP_OF: dict[str, str] = {event.key: group.key for group in GROUPS for event in group.events}

#: A producer kind that is another event's name.
EVENT_ALIASES: dict[str, str] = {
    "compliance-nudge": "compliance",
    "compliance-waiver-expired": "compliance",
    "error": "failed",
    "node-disk-paused": "node-full",
    "node-tmp-inodes": "node-full",
    "org_notify": "message",
    # A task step's approval notices (`app/pipeline/notices.py`) share the release ones' switches.
    "step_approval_reminder": "release_approval_reminder",
    "step_approval_timeout": "release_approval_timeout",
    "step_rejected": "release_rejected",
}

#: Kinds that are no longer notifications (PRD R9, every alert must be actionable): sent on no channel.
#: `agent-message` is an agent's informational post about its task; `self-heal` the fleet fixing itself.
REMOVED_KINDS: frozenset[str] = frozenset({"agent-message", "self-heal"})

#: Default levels per channel type, for the groups whose default is not `FULL`.
DEFAULTS: dict[str, dict[str, str]] = {
    "email": {"progress": OFF},
    "slack": {"progress": BRIEF},
    "telegram": {"progress": BRIEF},
}


def event_for(kind: str) -> Optional[str]:
    """The catalog event a producer `kind` is, or `None` when it is no longer a notification. A kind the catalog
    does not know comes back unchanged (delivered: see the module docstring)."""
    if kind in REMOVED_KINDS:
        return None
    return EVENT_ALIASES.get(kind, kind)


def default_level(channel_type: str, account_email: bool, group: str, viewer_is_admin: bool) -> str:
    """The level a channel resolves for `group` when nobody chose one. `account_email` is part of the signature so
    a typed address may default apart from the login one; today they agree."""
    del account_email
    found = GROUPS_BY_KEY.get(group)
    if found is not None and found.admin_only and not viewer_is_admin:
        return OFF
    return DEFAULTS.get(channel_type, {}).get(group, FULL)


def _clean(settings: Optional[Mapping[str, Any]]) -> tuple[dict[str, str], dict[str, str]]:
    """The stored document's two maps, keeping only known levels (a malformed value is no choice)."""
    raw: Mapping[str, Any] = settings if isinstance(settings, Mapping) else {}
    found_groups, found_events = raw.get("groups"), raw.get("events")
    groups: Mapping[str, Any] = found_groups if isinstance(found_groups, Mapping) else {}
    events: Mapping[str, Any] = found_events if isinstance(found_events, Mapping) else {}
    return (
        {key: value for key, value in groups.items() if isinstance(key, str) and value in LEVELS},
        {key: value for key, value in events.items() if isinstance(key, str) and value in LEVELS},
    )


def level(
    channel_type: str, settings: Optional[Mapping[str, Any]], event: str, *, viewer_is_admin: bool,
    account_email: bool = False,
) -> str:
    """The level (`off`/`brief`/`full`) one channel resolves for `event`, given its stored settings.

    `event` is a catalog event (`event_for` already applied). An admin-only event is `off` for a member whatever the
    document says. An event outside the catalog takes its explicit choice (an unsubscribe can name one), else full."""
    groups, events = _clean(settings)
    if event not in EVENTS:
        return events.get(event, FULL)
    if EVENTS[event].admin_only and not viewer_is_admin:
        return OFF
    group = _GROUP_OF[event]
    return events.get(event) or groups.get(group) or default_level(channel_type, account_email, group, viewer_is_admin)


def visible(viewer_is_admin: bool) -> list[Group]:
    """The groups `viewer` sees, each holding only the events that apply to them; a group left empty is not drawn."""
    shown: list[Group] = []
    for group in GROUPS:
        if group.admin_only and not viewer_is_admin:
            continue
        events = tuple(event for event in group.events if viewer_is_admin or not event.admin_only)
        if events:
            shown.append(Group(group.key, group.label, events, group.admin_only))
    return shown


def catalog_document(viewer_is_admin: bool) -> dict[str, Any]:
    """The wire shape of the catalog for one viewer (`Catalog` in the web's types)."""
    return {
        "groups": [
            {"key": group.key, "label": group.label,
             "events": [{"key": event.key, "label": event.label} for event in group.events]}
            for group in visible(viewer_is_admin)
        ]
    }


def effective(
    channel_type: str, settings: Optional[Mapping[str, Any]], *, viewer_is_admin: bool, account_email: bool = False,
) -> dict[str, str]:
    """Every visible event's resolved level, keyed by event (`Channel.effective` on the wire)."""
    return {
        event.key: level(channel_type, settings, event.key, viewer_is_admin=viewer_is_admin, account_email=account_email)
        for group in visible(viewer_is_admin) for event in group.events
    }


class ChoiceRefused(ValueError):
    """A settings write the catalog refuses: an unknown group, event or level, or one the viewer may not set."""


def apply_choice(
    settings: Optional[Mapping[str, Any]], *, level: str, viewer_is_admin: bool,
    group: Optional[str] = None, event: Optional[str] = None,
) -> dict[str, Any]:
    """A new settings document with one choice applied: a group's level (dropping every event choice inside that
    group, so the switch sets them all) or one event's. Raises `ChoiceRefused`."""
    if level not in LEVELS:
        raise ChoiceRefused(f"level must be one of {', '.join(LEVELS)}")
    if (group is None) == (event is None):
        raise ChoiceRefused("name exactly one of group or event")
    groups, events = _clean(settings)
    if group is not None:
        found = GROUPS_BY_KEY.get(group)
        if found is None:
            raise ChoiceRefused(f"unknown group {group!r}")
        if found.admin_only and not viewer_is_admin:
            raise ChoiceRefused(f"{group!r} is for Owners and Admins only")
        groups[group] = level
        for member in found.events:
            events.pop(member.key, None)
    else:
        assert event is not None
        if event not in EVENTS:
            raise ChoiceRefused(f"unknown event {event!r}")
        if EVENTS[event].admin_only and not viewer_is_admin:
            raise ChoiceRefused(f"{event!r} is for Owners and Admins only")
        events[event] = level
    return {"groups": groups, "events": events}


def validate_document(settings: Any, *, viewer_is_admin: bool) -> dict[str, Any]:
    """A whole settings document as sent (`PATCH {settings}`), checked choice by choice. Raises `ChoiceRefused`."""
    if not isinstance(settings, Mapping):
        raise ChoiceRefused("settings must be an object")
    unknown = set(settings) - {"groups", "events"}
    if unknown:
        raise ChoiceRefused(f"unknown settings keys: {', '.join(sorted(unknown))}")
    document: dict[str, Any] = {"groups": {}, "events": {}}
    groups = settings.get("groups") or {}
    events = settings.get("events") or {}
    if not isinstance(groups, Mapping) or not isinstance(events, Mapping):
        raise ChoiceRefused("groups and events must be objects")
    for key, value in groups.items():
        document = apply_choice(document, level=value, viewer_is_admin=viewer_is_admin, group=key)
    for key, value in events.items():
        document = apply_choice(document, level=value, viewer_is_admin=viewer_is_admin, event=key)
    return document
