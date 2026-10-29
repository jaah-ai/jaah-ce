"""The knob declaration: `Knob`, its tiers and the `_Section` card markers that stamp it."""
from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any, Optional

from app import edition_features


@dataclass(frozen=True)
class Knob:
    """One catalogue entry. `minimum`/`maximum` are numeric bounds for `"int"`/`"float"` and length bounds
    for `"str"`.

    `section` is the card within `group` (a category in the Settings list; both follow declaration
    order, and `CATALOG` stamps them from its `_Section` markers); a group declares a section for
    every knob or none (tested). `choices` (str only) closes the value set: a select in the UI, refused
    outside it. `credential_type` (str only) says the value is a `credentials.id` of those types: a
    rendering and provenance declaration (the credential picker), not a liveness validator; the consumer
    checks existence at resolution. `choices` and `credential_type` are never both set (tested).

    `tier` is the required tenancy verdict (no default, so an unconsidered verdict cannot be constructed):
    `"operator"` governs the CP or fleet as a whole; `"org"` shapes one tenant's own work (spec §4.5).
    `cron_runs_retention_days`, `cron_runs_per_cron_cap` and `activity_events_retention_days` are pinned
    `"operator"` by name: a tenant zeroing retention could erase its own audit trail."""

    key: str
    #: `"operator"` or `"org"`; validated in `__post_init__`, so a typo fails at import.
    tier: str
    label: str
    help: str
    kind: str  # "int" | "float" | "bool" | "str"
    env: Optional[str]
    default: Any
    minimum: Optional[float] = None
    maximum: Optional[float] = None
    unit: Optional[str] = None
    group: str = "General"
    section: Optional[str] = None
    choices: Optional[tuple[str, ...]] = None
    #: `choices`' display words, paired positionally; None renders each token humanised.
    choice_labels: Optional[tuple[str, ...]] = None
    #: A warning the page shows while that choice is selected, paired positionally ("" = none).
    choice_notes: Optional[tuple[str, ...]] = None
    credential_type: Optional[tuple[str, ...]] = None
    #: The value is an operator host's `vnode_hosts.node_id` (str only): the page renders the
    #: host picker. A rendering declaration like `credential_type`, exclusive with it and with `choices`.
    vnode_host: bool = False
    #: The value is an EC2 instance type from the allow-list (`app/instance_types.py`, str only): served
    #: as a select over that list, and refused outside it on write. Blank means "the list's first type"
    #: (`app/cloud_node_launch.py::resolved_instance_type`), so the default follows the operator's list
    #: rather than naming a type the list may not allow. Exclusive with `choices`/`credential_type`/
    #: `vnode_host`: the choices are the allowlist's, read live, never authored here.
    instance_type: bool = False
    #: The value is a `billing_plans.key` (str only): served as a select over the active plans, and refused
    #: outside them on write. Exclusive with `choices`/`credential_type`/`vnode_host`/`instance_type`: the
    #: choices are the plan catalogue's, read live.
    plan: bool = False
    #: The value is a cloud connection's id (`cloud_node_defaults.id`, str only): the page renders a select
    #: fed by `GET /api/cloud-connections/options`. A rendering and provenance declaration like
    #: `credential_type`; the consumer (`app/hosting_provisioning.py::_hosting_connection`) checks existence
    #: and ownership at resolution. Exclusive with `choices`/`credential_type`/`vnode_host`/`instance_type`/
    #: `plan`: the choices are the connections table's, read live.
    cloud_connection: bool = False
    #: The value is a `projects.id` (str only), picked by the operator from every org's projects: served as a select
    #: over them, read live, and refused outside them on write. Blank is the knob's "unset" choice. Exclusive with
    #: `choices`/`credential_type`/`vnode_host`/`instance_type`/`plan`/`cloud_connection`.
    project: bool = False
    #: The value is a link signed-out visitors and emails carry (str only): refused unless an absolute `https`
    #: URL (`app/url_validation.py::https_url_problem`), so it is never blank either.
    https_url: bool = False
    #: The value is a comma-separated list of email addresses (str only); blank is a real setting.
    email_list: bool = False
    #: The value is exactly one bare email address, no display name (str only); blank is a real setting.
    single_email: bool = False
    #: The value is the local part of an address on a domain the deployment owns (str only; `notifications`, never
    #: `a@b` or a `+` tag): `app/system_mail.py::LOCAL_PART_PATTERN`. Blank is a real setting (the reader's default).
    email_local_part: bool = False
    #: The value is a comma-separated list of email domains (str only), each stored in its canonical form
    #: (`app/access.py::normalized_domain`); blank is a real setting.
    domain_list: bool = False
    #: With `https_url`: blank is a real setting too, meaning the knob's documented fallback (the reader
    #: resolves it, e.g. `app/legal_pages.py::signup_link`). Needs `minimum=0`.
    blank_ok: bool = False
    #: The tenant's own Org Admin may write this org-tier knob's org override from Settings
    #: (`routes/settings.py::put_settings`); an operator still may. Needs `tier="org"`.
    tenant_editable: bool = False
    #: A tenant's Settings page is served this org-tier knob (read-only unless `tenant_editable`). `False`
    #: keeps an org-tier knob the operator's alone to see and override per org
    #: (`routes/settings.py::_settings_out` drops it for a non-operator). Needs `tier="org"` when `False`.
    tenant_visible: bool = True
    #: The value is one shell command line (str only): refused with a newline, CR or NUL, which would make
    #: one command two, the second ungated (`routes/projects.py::_validated_shell_command`'s rule). Stripped
    #: on write; blank is a real setting (no command).
    command_line: bool = False
    #: The value is the `data_regions` JSON list (str only): refused on write unless `data_residency.parse` reads
    #: it, and stored in its canonical form. Rendered by its own panel, not as a row.
    data_regions: bool = False
    #: The edition-map feature this knob configures (`packages/editions/features.toml`), or None for one every
    #: edition has. An edition without it is neither served the knob nor may write it (`catalog.in_edition`).
    feature: Optional[str] = None

    def __post_init__(self) -> None:
        # A closed set, checked at import: a misspelled tier would read as "operator", the silently plausible
        # answer this boundary exists to stop.
        if self.tier not in TIERS:
            raise ValueError(
                f"{self.key}: tier={self.tier!r} is not one of {TIERS}. A knob either governs the "
                "control plane or the fleet as a whole ('operator') or shapes one tenant's own work "
                "('org') — app/operator_settings/knobs.py::Knob."
            )
        if self.feature is not None and self.feature not in edition_features.ALL:
            raise ValueError(f"{self.key}: feature={self.feature!r} is not a feature of the edition map")
        if self.choice_labels is not None and (
            self.choices is None or len(self.choice_labels) != len(self.choices)
        ):
            raise ValueError(f"{self.key}: choice_labels must pair one-to-one with choices")
        if self.choice_notes is not None and (
            self.choices is None or len(self.choice_notes) != len(self.choices)
        ):
            raise ValueError(f"{self.key}: choice_notes must pair one-to-one with choices")
        for flag, exclusive in _STR_FLAG_EXCLUSIONS:
            if getattr(self, flag) and (self.kind != "str" or any(getattr(self, other) for other in exclusive)):
                raise ValueError(f"{self.key}: {flag} is a str, exclusive with {'/'.join(exclusive)}")
        if self.blank_ok and not (self.https_url and self.minimum == 0):
            raise ValueError(f"{self.key}: blank_ok qualifies https_url and needs minimum=0")
        if self.project and self.tier != "operator":
            # `project_choices` reads every org's projects, so only the operator may be served or checked against it.
            raise ValueError(f"{self.key}: project needs tier='operator'")
        if self.tenant_editable and self.tier != "org":
            # A tenant writes only its own org override, which the resolver reads for org-tier knobs alone.
            raise ValueError(f"{self.key}: tenant_editable needs tier='org'")
        if not self.tenant_visible and (self.tier != "org" or self.tenant_editable):
            # An operator-tier knob is never served to a tenant anyway; one a tenant may save, it must see.
            raise ValueError(f"{self.key}: tenant_visible=False needs tier='org' and no tenant_editable")


#: The closed set of tenancy tiers; no "sometimes per-org" third tier.
TIERS: tuple[str, ...] = ("operator", "org")

#: Each str-only value declaration and the earlier declarations it may not share a knob with (one
#: direction per pair suffices): a knob renders as exactly one control, so two declarations would leave
#: the page to pick. Checked by `Knob.__post_init__`.
_STR_FLAG_EXCLUSIONS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("vnode_host", ("choices", "credential_type")),
    ("instance_type", ("choices", "credential_type", "vnode_host")),
    ("plan", ("choices", "credential_type", "vnode_host", "instance_type")),
    ("cloud_connection", ("choices", "credential_type", "vnode_host", "instance_type", "plan")),
    ("project", ("choices", "credential_type", "vnode_host", "instance_type", "plan", "cloud_connection")),
    ("https_url", ("choices",)),
    ("email_list", ("choices",)),
    ("single_email", ("choices",)),
    ("email_local_part", ("choices", "single_email")),
    ("domain_list", ("choices",)),
    ("command_line", ("choices",)),
    ("data_regions", ("choices",)),
)


@dataclass(frozen=True)
class _Section:
    """A card marker inside the `CATALOG` literal: the knobs after it, up to the next marker, belong to this
    (`group`, `section`), stamped by `_sectioned`. One statement per card rather than one per knob, so a
    card's name has one spelling and a knob cannot stray from its neighbours by a typo."""

    group: str
    section: Optional[str] = None


def _sectioned(*items: Knob | _Section) -> tuple[Knob, ...]:
    """`items` with every `Knob` stamped with the `_Section` before it; declaration order is kept, since the
    Settings page renders groups, sections and knobs in it."""
    out: list[Knob] = []
    card: Optional[_Section] = None
    for item in items:
        if isinstance(item, _Section):
            card = item
        elif card is None:
            raise ValueError(f"{item.key}: declared before the first _Section marker")
        else:
            out.append(replace(item, group=card.group, section=card.section))
    return tuple(out)
