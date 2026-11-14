"""The tenancy census: every mapped class, one verdict, with the reason it was given.

The classification must be total (an unclassified table fails open): a census test
asserts `TENANCY`'s keys equal the live mapped classes as sets, never counts.

One-hop invariant (tested): a `Derived` class reaches `orgs` through a `Stored` parent, never another
derived class, so each derivation is one fixed-shape composite `(parent_id, org_id)` key; if a table
can only derive through a derived parent, promote that parent. Verdicts record where `org_id` comes
from; the read path treats `Stored` and `Derived` alike (`app/tenancy/seam.py::criteria_for`,
`app/models/base.py::OrgScoped`). `on_delete` is required, never inferred. `same_org` declares references
that must agree; `app/tenancy/keys.py` generates both key sets. `orgs` is `Axis`; operator plane = org 0.
"""

from __future__ import annotations

from dataclasses import dataclass

from app import edition_features
from app.live_cast import models as live_cast
from app.models import (
    access,
    api_keys,
    agent_sessions,
    audit,
    automations,
    billing,
    control_plane,
    credentials,
    governance,
    hosted_apps,
    hosting,
    jaah_repos,
    identity,
    integrations,
    merge_lane,
    metering,
    node_groups,
    nodelinks,
    nodes,
    notifications,
    operator_dashboard,
    orgs,
    placement_groups,
    production_qa,
    products,
    outcomes,
    projects,
    releases,
    review,
    routing,
    support,
    tenant_email,
    task_extras,
    task_templates,
    tasks,
    trackers,
    webhooks,
    worktrees,
)
from app.models.base import Base


@dataclass(frozen=True)
class Verdict:
    """Base of the closed verdict union. Every entry carries a `reason` a reviewer can disagree with."""

    reason: str


@dataclass(frozen=True)
class Axis(Verdict):
    """The tenancy axis itself. Carries no `org_id` — it is what `org_id` points at."""


@dataclass(frozen=True)
class Stored(Verdict):
    """Tenant-owned, with a real `org_id` column of its own.

    `same_org` declares references into tenant-owned tables that must agree with this row's org (see
    `SameOrg`), exactly as on `Derived`: a Stored row has no derivation, but its second references still
    must not span orgs (e.g. `tasks.project_id`)."""

    same_org: tuple[SameOrg, ...] = ()


@dataclass(frozen=True)
class SameOrg:
    """One tenant-owned reference that is not its entry's derivation and still must not span orgs.

    A derivation says nothing about the rows a table points at; `app/tenancy/keys.py::same_org_keys`
    turns each entry into a `(column, org_id) REFERENCES parent(id, org_id)` key (references without one: the
    unkeyed-reference register in `test_tenancy_composite_keys.py`). Declared, not swept off `Base.metadata`,
    because actor references (`created_by_id`, `granted_by_id`) must not get one: an operator in org zero authoring a tenant's row is correct.
    The composite-key test holds declared and generated sets equal.

    `on_delete` is required for `Derived.on_delete`'s reason: a NO ACTION composite beside a CASCADE key
    would break the parent delete.

    `null_is_legitimate`: keys are MATCH SIMPLE, so a NULL satisfies them outright. `True` means NULL is an
    intended shape (e.g. `role_bindings`' exactly-one-of-four scope columns); `False` means the column should be
    NOT NULL. The census checks it against the delivered nullability.
    """

    column: str
    parent: type[Base]
    on_delete: str
    null_is_legitimate: bool
    reason: str


@dataclass(frozen=True)
class Derived(Verdict):
    """Tenant-owned, reaching `orgs` through a NOT NULL foreign key to a `Stored` parent.

    Records the derivation: parent, local column(s), and ON DELETE. Derived tables also carry their own
    NOT NULL `org_id` (the composite `(parent_id, org_id)` key requires it); `app/models/base.py::OrgScoped`
    single-homes what the keys guarantee, and `app/tenancy/keys.py` derives them from these entries.

    `parent` is a tuple only for the XOR shapes — `role_bindings` (exactly one of user XOR group), the
    Task-or-Ticket rows (`task_comments`, `task_comment_mentions`) and `task_attachments` (Task, Ticket or
    support conversation) — and for `task_ratings` (derives through both, which must agree); `.parents`
    does not distinguish these, so a consumer that must will need a discriminator. Read `.parents`, not
    `.parent`. `fk_columns` pair with parents positionally (pinned by the census test).

    `same_org` declares other references into tenant-owned tables that must agree (see `SameOrg`); most
    entries leave it empty, with unkeyed references in `test_tenancy_composite_keys.py`'s register.
    """

    parent: type[Base] | tuple[type[Base], ...]
    fk_columns: tuple[str, ...]
    on_delete: str
    same_org: tuple[SameOrg, ...] = ()

    @property
    def parents(self) -> tuple[type[Base], ...]:
        # `tuple(...)`, not an isinstance arm: a `parent=[A, B]` typo would otherwise fail later as an obscure
        # unhashable-type TypeError.
        if isinstance(self.parent, (tuple, list)):
            return tuple(self.parent)
        return (self.parent,)

    @property
    def pairs(self) -> tuple[tuple[str, type[Base]], ...]:
        """This entry's (local column, parent class) pairs: the one implementation every consumer calls.

        One parent: every column points at it; several: positional pairing. Single-homed here so
        `app/tenancy/keys.py` and the write seam's guard cannot read the rule differently.
        `test_tenancy_manifest.py` spells it out independently on purpose.
        """
        parents = self.parents
        columns = self.fk_columns
        if len(parents) == 1:
            return tuple((column, parents[0]) for column in columns)
        if len(parents) != len(columns):
            raise ValueError(
                f"{len(columns)} fk_columns against {len(parents)} parents — with several parents the "
                "manifest pairs them POSITIONALLY, so the two lists must be the same length. Zipping to "
                "the shorter one would silently drop a derivation path from every consumer that walks "
                f"these pairs (app/tenancy/manifest.py::Derived): {columns} vs "
                f"{tuple(p.__name__ for p in parents)}"
            )
        return tuple(zip(columns, parents, strict=False))


@dataclass(frozen=True)
class Shared(Verdict):
    """Genuinely shared across every org, by design rather than by omission."""


def _project(reason: str, *, on_delete: str, null_is_legitimate: bool) -> SameOrg:
    """A `project_id` reference held to its row's org: the census's most common second reference."""
    return SameOrg("project_id", projects.Project, on_delete, null_is_legitimate, reason)


def _credential(reason: str, *, on_delete: str, null_is_legitimate: bool) -> SameOrg:
    """A `credential_id` reference held to its row's org."""
    return SameOrg("credential_id", credentials.Credential, on_delete, null_is_legitimate, reason)


def _stage(reason: str, *, on_delete: str, null_is_legitimate: bool) -> SameOrg:
    """A `stage_id` reference to a pipeline stage held to its row's org."""
    return SameOrg("stage_id", releases.ReleaseStage, on_delete, null_is_legitimate, reason)


# ---------------------------------------------------------------------------------------------------
# The census. This file is the authoritative and only classification.
# ---------------------------------------------------------------------------------------------------

TENANCY: dict[type[Base], Verdict] = {
    # -- The axis ------------------------------------------------------------------------------------
    orgs.Org: Axis(
        "The tenancy axis. It carries no org_id because it is what org_id references; it is written "
        "only on the operator plane and read during binding. Org zero is a real row, not a NULL."
    ),
    # -- Tenant-owned, org STORED --------------------------------------------------------------------
    identity.AdminUser: Stored(
        "The root of the subject side; there is nothing above it to derive from. Operator staff are "
        "members of org zero, not a NULL.",
        same_org=(
            SameOrg(
                column="git_credential_id",
                parent=credentials.Credential,
                on_delete="SET NULL",
                null_is_legitimate=True,
                reason="A user's git connection is a personal credential of the user's own org.",
            ),
        ),
    ),
    access.Group: Stored("A permission bundle is authored inside one org."),
    orgs.AppSettings: Stored(
        "One row per org, carrying that org's branding — it stopped being the id=1 singleton when "
        "UNIQUE(org_id) landed. Stored rather than Derived because there is nothing above it to derive "
        "from: the org IS its subject."
    ),
    orgs.OrgSetting: Stored(
        "One org's override of one operator knob — the tier above the operator's own. Stored and not "
        "Derived for `app_settings`' reason: org_id is the row's subject, not a fact reachable through "
        "a parent. IT WAS BORN WITH THE COLUMN rather than given one by revision 0178, which is the "
        "one thing about this entry a reader needs: the table did not exist when that revision was "
        "written, so it is outside 0178's target set by construction and `test_tenancy_org_column.py` "
        "records it as such. The knobs that may appear here, and the resolution order this tier adds a "
        "step to, are `app/operator_settings/`'s."
    ),
    orgs.OrgConsentHash: Stored(
        "One org's managed-settings consent hash (`app/node_config.py`); the org IS the row's subject. "
        "Born with the column, like `org_settings`."
    ),
    audit.ActivityEvent: Stored("user_id is nullable for agent-plane writes, so no derivation is total."),
    agent_sessions.AgentSession: Stored(
        "node_id, project_id and task_id are ALL nullable, so nothing derives. Transcript pointers — "
        "the highest-sensitivity row in the schema.",
        same_org=(
            _project(
                "A session runs a project of its own org; its node places only that org's work.",
                on_delete="SET NULL", null_is_legitimate=True,
            ),
        ),
    ),
    projects.Project: Stored(
        "The anchor: org sits directly above project.",
        same_org=(
            SameOrg("pipeline_template_id", releases.PipelineTemplate, "RESTRICT", True,
                    "The org pipeline template the project follows is its own org's; NULL when not linked."),
            SameOrg("pipeline_template_version_id", releases.PipelineTemplateVersion, "RESTRICT", True,
                    "The template version the project is on is its own org's; NULL when not linked."),
        ),
    ),
    orgs.OrgDomain: Stored(
        "A domain's whole meaning is the org it joins users to, so org_id is the row's subject and there "
        "is nothing above it to derive from — app_settings' reason."
    ),
    orgs.OrgIpRange: Stored(
        "One range of an org's IP allowlist. The org is its subject and there is no tenant-owned parent to derive "
        "through — org_domains' reason."
    ),
    orgs.OrgSiemDestination: Stored(
        "An org's SIEM export destination, one per org. The org is its subject and there is no tenant-owned parent "
        "to derive through — org_domains' reason."
    ),
    webhooks.OrgWebhookEndpoint: Stored(
        "One of an org's outgoing webhook endpoints, made by its admin. The org is its subject and there is no "
        "tenant-owned parent to derive through — org_siem_destinations' reason."
    ),
    webhooks.WebhookDelivery: Derived(
        "One event owed to one webhook endpoint: that endpoint's tenant.",
        parent=webhooks.OrgWebhookEndpoint,
        fk_columns=("endpoint_id",),
        on_delete="CASCADE",
    ),
    orgs.ScimToken: Stored(
        "One of an org's SCIM bearer tokens. The org is its subject and there is no tenant-owned parent to derive "
        "through — org_domains' reason."
    ),
    tenant_email.GmailConnection: Stored(
        "A Gmail mailbox an org connected for its own mail. The org is its subject and there is no tenant-owned parent "
        "to derive through — org_domains' reason.",
        same_org=(
            _credential(
                "The credential row the mailbox is catalogued as is the org's own; NULL until linked.",
                on_delete="SET NULL", null_is_legitimate=True,
            ),
        ),
    ),
    trackers.TrackerConnection: Stored(
        "An org's connection to its own tracker workspace, made by its admin. The org is its subject and there is no "
        "tenant-owned parent to derive through — gmail_connections' reason.",
        same_org=(
            _credential(
                "The tracker token the connection acts with is the org's own credential; NULL for a Forge app.",
                on_delete="SET NULL", null_is_legitimate=True,
            ),
            SameOrg(
                column="trigger_id", parent=automations.TaskTrigger, on_delete="SET NULL", null_is_legitimate=True,
                reason="The connection's events become its own org's trigger's deliveries; NULL once that is deleted.",
            ),
        ),
    ),
    trackers.TrackerMapping: Derived(
        "A link of a tracker connection's container to a Jaah project: that connection's tenant. project_id names "
        "the linked project, held to the same org below.",
        parent=trackers.TrackerConnection,
        fk_columns=("connection_id",),
        on_delete="CASCADE",
        same_org=(
            _project("A connection's container is linked only to its own org's project.", on_delete="CASCADE",
                     null_is_legitimate=False),
        ),
    ),
    trackers.TrackerIssueLink: Derived(
        "The tracker issue a task was started from: one row per task, so it is that task's tenant. connection_id "
        "names the tracker_connections row that started it, held to the same org below.",
        parent=tasks.Task,
        fk_columns=("task_id",),
        on_delete="CASCADE",
        same_org=(
            SameOrg(
                column="connection_id", parent=trackers.TrackerConnection, on_delete="CASCADE",
                null_is_legitimate=False,
                reason="A task is started only by its own org's tracker connection.",
            ),
        ),
    ),
    trackers.TrackerWriteback: Derived(
        "One thing posted back to a task's issue: that task's tenant.",
        parent=tasks.Task,
        fk_columns=("task_id",),
        on_delete="CASCADE",
    ),
    hosted_apps.HostedApp: Stored(
        "An Easy Mode app outlives its project: project_id goes NULL when the project is deleted and the row stays as "
        "the deleted app's record, so nothing derives.",
        same_org=(
            _project(
                "An app is its own org's project's; NULL once that project is deleted.",
                on_delete="SET NULL", null_is_legitimate=True,
            ),
        ),
    ),
    hosted_apps.HostedAppUsage: Derived(
        "A month's metered use belongs to the app it measures.",
        parent=hosted_apps.HostedApp,
        fk_columns=("app_id",),
        on_delete="CASCADE",
    ),
    hosted_apps.HostedAppDomain: Derived(
        "A custom domain belongs to the app it serves.",
        parent=hosted_apps.HostedApp,
        fk_columns=("hosted_app_id",),
        on_delete="CASCADE",
    ),
    jaah_repos.JaahRepo: Stored(
        "A Jaah-made repository outlives its project: project_id goes NULL when the project is deleted and the row "
        "stays as the repo's record, so nothing derives.",
        same_org=(
            _project(
                "A Jaah repo is its own org's project's; NULL once that project is deleted.",
                on_delete="SET NULL", null_is_legitimate=True,
            ),
            SameOrg(
                column="migration_task_id",
                parent=tasks.Task,
                on_delete="SET NULL",
                null_is_legitimate=True,
                reason="The task moving the project off this repo is the same org's; NULL until one runs.",
            ),
        ),
    ),
    integrations.CustomConnector: Stored(
        "A custom connector an org defines for itself; the org is its subject and there is no tenant-owned parent "
        "to derive through — org_domains' reason."
    ),
    integrations.IntegrationVote: Shared(
        "A person's vote for a Beta catalog vendor: one per identity, so a person in several orgs votes once, as "
        "user_mfa_factors is per identity. The catalog it votes on is platform-wide, and only the per-vendor "
        "count leaves the table (the operator's Settings list) besides the person's own voted mark.",
    ),
    tenant_email.TenantMailAddress: Stored(
        "A hosted address an org receives mail at. The org is its subject: it exists before any trigger names it, so "
        "there is no NOT NULL tenant-owned parent to derive through."
    ),
    tenant_email.TenantEmailSend: Stored(
        "One message an org sent through its Gmail. Its connection may be gone (a disconnect keeps the record), so "
        "the org is its subject, not a parent's."
    ),
    task_templates.OrgTaskTemplate: Stored(
        "An org's own task template, written by its editors; nothing above it to derive from. The global library is "
        "`task_templates`, a table of its own because this `org_id` is NOT NULL."
    ),
    task_templates.TaskTemplateFavorite: Stored(
        "One member's star, an act inside their org: the org is its subject. The star may name a global template, "
        "which belongs to no org, so the template cannot be its derivation."
    ),
    task_templates.OrgTaskTemplateStartDay: Stored(
        "One day's start count of an org template, written beside the task it counts, in that task's org."
    ),
    api_keys.ApiKey: Stored(
        "A member's personal API key. Its org is its owner's, which `app/api_keys.py` takes at mint and re-checks on "
        "every request; there is no tenant-owned parent to derive through without making its secrets two hops away."
    ),
    orgs.OrgSsoDomain: Stored(
        "One email domain an org claims for SAML/OIDC sign-in. The org is its subject and there is no tenant-owned "
        "parent to derive through — org_domains' reason."
    ),
    orgs.OrgSsoConnection: Stored(
        "An org's one SAML or OIDC identity provider. The org is its subject and there is no tenant-owned parent "
        "to derive through — org_domains' reason.",
        same_org=(
            SameOrg(
                column="oidc_credential_id",
                parent=credentials.Credential,
                on_delete="RESTRICT",
                null_is_legitimate=True,
                reason="An org's OIDC client secret is its own credential.",
            ),
        ),
    ),
    products.OrgProduct: Stored(
        "One product an org holds and where its money stands (the product engine's holding). The org is its "
        "subject and its catalogue product is shared, so there is no tenant-owned parent to derive through — "
        "node_rentals' reason."
    ),
    products.ProductTrial: Stored(
        "An org used its one trial of a product. The org is its subject and the product is shared, so there is "
        "nothing tenant-owned to derive through — org_domains' reason."
    ),
    products.OrgProductSettings: Stored(
        "One org's settings of one product (automatic top-up). The org is its subject and the product is shared, "
        "so there is nothing tenant-owned to derive through — org_domains' reason."
    ),
    billing.PlanTermsSnapshot: Stored(
        "One org's plan terms as they stood before the plan engine's inheritance and rollover shipped. The org is "
        "its subject (org_id is the key) and there is no tenant-owned parent to derive through — org_domains' "
        "reason. Read only by `billing.plans.ever_used`; the table awaits its drop."
    ),
    billing.CustomerEvent: Stored(
        "One thing that happened to an org, filed under that org. The org is its subject and the row outlives "
        "every object it names (refs are pointers, not keys), so there is nothing to derive through."
    ),
    billing.OrgBundledProduct: Stored(
        "One node an org's plan bundles. STORED: it is claimed before its node or hosting exists and outlives "
        "both (node_id is a pointer with no foreign key; hosting_id is SET NULL), so there is nothing to derive "
        "through — node_rentals' reason. hosting_id is a second reference classified in the unkeyed-reference register."
    ),
    tasks.Task: Stored(
        "Derivable from project_id, but stored DELIBERATELY: the placement floor must be a "
        "single-table predicate rather than a join, because it rides the lease's one atomic UPDATE. "
        "project_id is a SECOND REFERENCE into a tenant-owned table, not a derivation; its same-org key "
        "holds this row's org equal to its project's.",
        same_org=(
            _project(
                "A task is its project's org's work; alarms file into the Alarm project's own org.",
                on_delete="NO ACTION", null_is_legitimate=False,
            ),
        ),
    ),
    task_extras.TagGroup: Stored("A taxonomy is a tenant's own vocabulary."),
    agent_sessions.Agent: Stored(
        "An org's registered SDK agent belongs to the org that registered it; a catalog connector is a row of "
        "org zero flagged is_catalog (the seam hides it from tenants, who read the catalog on a fleet session "
        "of its own, `agent_resolution.catalog_rows`). Nothing above it to derive from."
    ),
    agent_sessions.AgentEnablement: Stored(
        "One org's opt-in to one catalog connector: the org IS the row's subject. agent_id names a row of org "
        "zero by design, so it cannot derive from it — a second reference classified in the unkeyed-reference register."
    ),
    tasks.Playbook: Stored("A tenant's own playbook — a duplicated built-in or one written from scratch."),
    # Living Governance (revision 0378): an org's own packs, scans, ledger, twin and debt. Each
    # Stored entry's org is its subject; the project/pack a row names is a second reference (unkeyed-reference register).
    governance.GovPack: Stored("An org's own compliance pack, a fork or one written from scratch; the org IS its subject."),
    governance.GovRuleOracle: Stored(
        "One org's latest oracle run for one rule version. Its pack may be a managed (Shared) one, so there is no "
        "tenant-owned parent to derive through — the org that ran it is the subject."
    ),
    governance.GovEntity: Stored(
        "A node of one org's portfolio graph. `project_id` is nullable (a team or model entity names none), so "
        "nothing derives; the project is a second reference.",
        same_org=(
            _project("An entity names a project of its own portfolio.", on_delete="CASCADE", null_is_legitimate=True),
        ),
    ),
    governance.GovOrgSettings: Stored("One org's Portfolio Compliance settings; the org IS the row's subject."),
    governance.GovScan: Stored("One org's scan of a selection of its projects; its pack may be Shared, so nothing derives."),
    governance.GovLedger: Stored(
        "One immutable attestation. Every reference is provenance without a key (an ON DELETE would have to "
        "rewrite an immutable row), so nothing derives; the org that attested is the subject."
    ),
    governance.GovGuardDecision: Stored(
        "One Architectural Guard decision (kept forever). Its project and task are provenance without a key, as "
        "on gov_ledger, so nothing derives; the org whose guard decided is the subject."
    ),
    governance.GovTwinCell: Stored(
        "Materialised state, keyed per project for the matrix read. Stored so the twin's reads and its one writer "
        "filter on a single table; project_id is a second reference.",
        same_org=(
            _project(
                "A twin cell is materialised for a project of its own org.",
                on_delete="CASCADE", null_is_legitimate=False,
            ),
        ),
    ),
    governance.GovTwinRepo: Stored(
        "A project's twin summary under one pack; gov_twin_cells' reason.",
        same_org=(
            _project("A twin summary is of a project of its own org.", on_delete="CASCADE", null_is_legitimate=False),
        ),
    ),
    governance.GovException: Stored(
        "A waiver. `project_id` is NULL when a predicate scope is set, so no derivation is total.",
        same_org=(
            _project("A waiver names a project of its own org.", on_delete="CASCADE", null_is_legitimate=True),
        ),
    ),
    governance.GovCampaign: Stored("A remediation campaign across one org's projects; the org IS its subject."),
    governance.GovRemediation: Stored(
        "One Fix on one project; Stored for gov_twin_cells' reason (the overlay read).",
        same_org=(
            _project("A fix is filed on a project of its own org.", on_delete="CASCADE", null_is_legitimate=False),
        ),
    ),
    governance.GovPackProposal: Stored(
        "A managed pack's new version offered to one org; its target pack may be Shared, so nothing derives."
    ),
    # ── The merge lane (revision 0409; `app/merge_lane.py`, the one writer of all three) ──────────────────
    merge_lane.MergeLaneRow: Stored(
        "A runner PR queued for the lane. Stored so the lane's reads filter one table; project_id and both "
        "task references are second references, each read off the caller task's own project by the writer.",
        same_org=(
            _project(
                "A lane row is the reporting task's own project's.",
                on_delete="CASCADE", null_is_legitimate=False,
            ),
        ),
    ),
    outcomes.TaskOutcome: Stored(
        "task_id, project_id and agent_session_id are SET NULL (a deleted task's rows are kept, excluded), so no "
        "derivation is total; the writer copies org_id off the dispatched task.",
        same_org=(
            SameOrg(
                column="task_id", parent=tasks.Task, on_delete="SET NULL", null_is_legitimate=True,
                reason="Provenance outlives a deleted task: SET NULL keeps the outcome's history.",
            ),
            _project(
                "An outcome is its task's project's; SET NULL keeps it past the project's delete.",
                on_delete="SET NULL", null_is_legitimate=True,
            ),
            SameOrg(
                column="agent_session_id", parent=agent_sessions.AgentSession, on_delete="SET NULL",
                null_is_legitimate=True,
                reason="The dispatch's own session, of its own task; NULL until the launch announces itself.",
            ),
        ),
    ),
    outcomes.TaskOutcomeHead: Derived(
        "A pushed head belongs to the dispatch it was pushed for.",
        parent=outcomes.TaskOutcome,
        fk_columns=("outcome_id",),
        on_delete="CASCADE",
        same_org=(
            SameOrg(
                column="task_id", parent=tasks.Task, on_delete="SET NULL", null_is_legitimate=True,
                reason="The outcome's own task, SET NULL with it.",
            ),
        ),
    ),
    outcomes.OutcomeLink: Derived(
        "Defect evidence belongs to the merged dispatch it judges; its actor columns are provenance.",
        parent=outcomes.TaskOutcome,
        fk_columns=("origin_outcome_id",),
        on_delete="CASCADE",
    ),
    outcomes.BenchmarkDuel: Derived(
        "A benchmark duel belongs to the sampled task it re-runs (`app/outcome_ledger/benchmark.py`); its shadow and "
        "review tasks and its project are that task's own, each held same-org by its own key.",
        parent=tasks.Task,
        fk_columns=("origin_task_id",),
        on_delete="CASCADE",
        same_org=(
            _project(
                "The origin task's project, copied at sampling; SET NULL past the project's delete.",
                on_delete="SET NULL", null_is_legitimate=True,
            ),
            SameOrg(
                column="shadow_task_id", parent=tasks.Task, on_delete="SET NULL", null_is_legitimate=True,
                reason="The hidden shadow the engine filed in the origin's project; NULL once it is deleted.",
            ),
            SameOrg(
                column="review_task_id", parent=tasks.Task, on_delete="SET NULL", null_is_legitimate=True,
                reason="The hidden duel_review task the engine filed in the origin's project; NULL until filed.",
            ),
        ),
    ),
    routing.RoutingDecision: Derived(
        "A routing decision belongs to the dispatch of the task it was made for (`app/agent_router/decisions.py`); "
        "its project_id is that task's project, held same-org by its own key.",
        parent=tasks.Task,
        fk_columns=("task_id",),
        on_delete="CASCADE",
        same_org=(
            _project(
                "The task's own project, copied at write; the task moves it with itself.",
                on_delete="CASCADE", null_is_legitimate=False,
            ),
        ),
    ),
    review.ReviewFinding: Derived(
        "A review finding belongs to the task whose change it is about (`app/review_findings.py`); the task that "
        "detected it is held same-org by its own key.",
        parent=tasks.Task,
        fk_columns=("task_id",),
        on_delete="CASCADE",
        same_org=(
            SameOrg(
                column="detecting_task_id", parent=tasks.Task, on_delete="SET NULL", null_is_legitimate=True,
                reason="A reviewing task of the authoring task's own org; NULL once it is deleted.",
            ),
        ),
    ),
    merge_lane.MergeLaneCertificate: Stored(
        "A green tree for one project's gate; merge_lane_rows' reason.",
        same_org=(
            _project("A certificate is its project's org's.", on_delete="CASCADE", null_is_legitimate=False),
        ),
    ),
    merge_lane.MergeLaneFlake: Stored(
        "A flaky test the lane saw on one project; merge_lane_rows' reason.",
        same_org=(
            _project("A flake is its project's org's.", on_delete="CASCADE", null_is_legitimate=False),
        ),
    ),
    # ── Production QA (revision 202611012000; rules: `app/production_qa/`) ─────────────────────────────────
    production_qa.BranchPush: Stored(
        "A push event on one project's deploy-watched repo; the deploy range ledger.",
        same_org=(
            _project(
                "A push is recorded against the trigger's own project.",
                on_delete="CASCADE", null_is_legitimate=False,
            ),
        ),
    ),
    production_qa.Deployment: Stored(
        "One deploy run attempt of one project; the parent its children derive through.",
        same_org=(
            _project(
                "A deploy is recorded against the trigger's own project.",
                on_delete="CASCADE", null_is_legitimate=False,
            ),
        ),
    ),
    production_qa.Incident: Stored(
        "One production problem of one project; the parent its children derive through.",
        same_org=(
            _project(
                "An incident is recorded against the trigger's own project.",
                on_delete="CASCADE", null_is_legitimate=False,
            ),
        ),
    ),
    production_qa.ProductionQaEdge: Stored(
        "A ledger fact about one task. Stored so the applier reads one table in id order.",
        same_org=(
            SameOrg(
                column="task_id",
                parent=tasks.Task,
                on_delete="CASCADE",
                null_is_legitimate=False,
                reason="An edge is a fact about a task of the deploy's or incident's own project.",
            ),
            SameOrg(
                column="deployment_id",
                parent=production_qa.Deployment,
                on_delete="CASCADE",
                null_is_legitimate=True,
                reason="A deployed edge names a deploy of the task's own org; NULL on incident edges.",
            ),
            SameOrg(
                column="incident_id",
                parent=production_qa.Incident,
                on_delete="CASCADE",
                null_is_legitimate=True,
                reason="An incident edge names an incident of the task's own org; NULL on deployed edges.",
            ),
        ),
    ),
    production_qa.MergeLaneStageTiming: Stored(
        "How long one merge-lane gate stage took on one project.",
        same_org=(
            _project("A timing is its lane's project's.", on_delete="CASCADE", null_is_legitimate=False),
        ),
    ),
    # ── Releases (revision 202611102100; rules: `app/releases/`) ─────────────────────────────────────────────
    releases.ReleaseStage: Stored(
        "One environment of one project; the parent its observed shas derive through.",
        same_org=(
            _project("A stage is its project's.", on_delete="CASCADE", null_is_legitimate=False),
        ),
    ),
    releases.ReleaseFreeze: Stored(
        "A deploy freeze of one project.",
        same_org=(
            _project("A freeze is its project's.", on_delete="CASCADE", null_is_legitimate=False),
        ),
    ),
    releases.Release: Stored(
        "One request to move a stage's head into the next; the parent its approvals and changes derive through. "
        "Its stage, task and deployment references are second references, written off the project's own rows.",
        same_org=(
            _project("A release is its project's.", on_delete="CASCADE", null_is_legitimate=False),
        ),
    ),
    releases.ReleaseJob: Stored(
        "One executor job (survey, connect) of one project.",
        same_org=(
            _project("A job is its project's.", on_delete="CASCADE", null_is_legitimate=False),
        ),
    ),
    releases.PipelineRevision: Stored(
        "One save of a project's pipeline and freezes (`app/releases/history.py`).",
        same_org=(
            _project("A revision is its project's.", on_delete="CASCADE", null_is_legitimate=False),
        ),
    ),
    releases.PipelineGuardrail: Stored(
        "One org's setting for one built-in pipeline rule; nothing above it but the org, its subject."
    ),
    releases.PipelineTemplate: Stored(
        "One org pipeline template; nothing above it but the org. published_version_id -> pipeline_template_versions "
        "is one of its own versions.",
        same_org=(
            SameOrg("published_version_id", releases.PipelineTemplateVersion, "SET NULL", True,
                    "A template's published version is one of its own; NULL until the first publish."),
        ),
    ),
    # ── Pipeline gates (revision 202611131300; rules: `app/pipeline_gates/`) ─────────────────────────────────
    releases.PipelineGate: Stored(
        "One gate of a project's pipeline stage; the stage is its project's.",
        same_org=(
            _project("A gate is its project's.", on_delete="CASCADE", null_is_legitimate=False),
            _stage("A gate sits on one of its project's stages.", on_delete="CASCADE", null_is_legitimate=False),
        ),
    ),
    releases.GateRun: Stored(
        "One run of a stage's gate list on one subject; the parent its results derive through.",
        same_org=(
            _project("A run is its project's.", on_delete="CASCADE", null_is_legitimate=False),
            _stage("A run gated one of its project's stages; NULL once that stage is removed.", on_delete="SET NULL",
                   null_is_legitimate=True),
        ),
    ),
    releases.StageArrival: Stored(
        "A change reaching one of a project's stages. Its release reference is a second reference, written off "
        "the project's own release row.",
        same_org=(
            _project("An arrival is its project's.", on_delete="CASCADE", null_is_legitimate=False),
            _stage("An arrival is on one of its project's stages.", on_delete="CASCADE", null_is_legitimate=False),
        ),
    ),
    releases.ReleaseNotice: Stored(
        "One release notice in one member's bell, written by `app/releases/notify.py` into the project's org. "
        "user_id -> admin_users is that member's row in the same org (the writer resolves it by membership).",
        same_org=(
            _project("A notice is about its project.", on_delete="CASCADE", null_is_legitimate=False),
        ),
    ),
    # Billing mirrors (`app/billing/mirrors.py`): Stripe's state for ONE org's customer, written only by the
    # operator-plane billing crons from objects re-fetched from Stripe. The org is the row's subject — it is
    # resolved from the Stripe customer — and nothing above it carries one, so each is Stored.
    billing.BillingCustomer: Stored("An org's Stripe Customer on one account; the org IS the row's subject."),
    billing.BillingSubscription: Stored(
        "An org's Stripe Subscription mirror. Its customer is a Stripe id, not a foreign key, so there is "
        "nothing to derive through — billing_customers' reason."
    ),
    billing.BillingPaymentMethod: Stored(
        "An org's saved payment method (display fields only). Keyed by its Stripe id; billing_customers' reason."
    ),
    billing.BillingInvoice: Stored("An org's Stripe Invoice mirror; billing_customers' reason."),
    billing.BillingSetupAttempt: Stored(
        "One SetupIntent an org opened to save a card and its failure count; keyed by the Stripe id, "
        "billing_customers' reason."
    ),
    billing.BillingCheckout: Stored(
        "An org's self-serve purchase of a paid plan; its subscription is a Stripe id, billing_customers' reason."
    ),
    billing.BillingPreview: Stored(
        "One plan-change preview an org was shown; keyed by nothing above it, billing_customers' reason."
    ),
    billing.BillingNotice: Stored(
        "A billing notice to one org's Owner/Admins; its subject is a Stripe id, billing_customers' reason."
    ),
    billing.BillingNoticeRead: Stored(
        "One reader's read stamp on a billing notice. Like every billing row it carries no foreign key, so "
        "there is nothing to derive through — billing_customers' reason."
    ),
    # Platform Bedrock (`app/bedrock/`): written only by the operator-plane machinery for ONE tenant; the org
    # is each row's subject and nothing above it carries one.
    metering.BedrockInferenceProfile: Stored("A tenant's own Bedrock inference profile; the org IS the row's subject."),
    metering.BedrockRevocation: Stored("A tenant's Bedrock revocation; the org IS the row's subject."),
    # Jaah token billing (`app/token_billing/`): one tenant's balance, its ledger, its metered calls and the
    # effects owed for it; the org is each row's subject.
    metering.TokenLedgerEntry: Stored("A change to a tenant's Jaah AI balance; the org IS the row's subject."),
    metering.TokenUsageCall: Stored("One metered model call of a tenant; the org IS the row's subject."),
    operator_dashboard.OpsSnapshot: Stored(
        "An hourly snapshot of one subject (a node, a slot group, an account, a wallet), written into that "
        "subject's own org by the one fleet-wide writer (`app/ops_snapshots.py`). Stored, not Derived: the "
        "subject columns carry no FK, so a retired node's history outlives it."
    ),
    operator_dashboard.InsightDigest: Stored(
        "The operator dashboard's weekly AI digest: fleet-wide operator content that names tenants, written into "
        "the Operator org by the one writer (`app/operator_dashboard/digest.py`) and read only on the operator "
        "plane."
    ),
    metering.TokenWallet: Stored("A tenant's Jaah AI balance; the org IS the row's subject."),
    metering.TokenTopup: Stored("One Stripe charge funding a tenant's Jaah AI balance; the org IS the row's subject."),
    metering.BedrockReconciliation: Stored("One tenant's day of Jaah AI cost checked against AWS; the org IS the row's subject."),
    agent_sessions.JaahSession: Stored("One tenant's session on the Jaah account; the org IS the row's subject."),
    nodes.Node: Stored("The root of the resource side; its org changes only through the operator transfer."),
    credentials.NodeVaultKey: Stored(
        "An org's NodeVault public key. STORED: a key outlives every node it reaches and is reached by its "
        "hash, so there is nothing to derive through. Holds no private key (AC-8.1)."
    ),
    credentials.NodeVaultGrant: Derived(
        "Where one NodeVault key should be on one node: the node's tenant. key_hash names a nodevault_keys "
        "row by (org_id, key_hash) with no foreign key (abandon deletes both in one transaction), so the node "
        "is the only derivation.",
        parent=nodes.Node,
        fk_columns=("node_id",),
        on_delete="CASCADE",
    ),
    credentials.NodeVaultOperation: Stored(
        "One NodeVault operation of an org (setup, rotation, migration, retire); its items derive through it."
    ),
    credentials.NodeVaultOperationItem: Derived(
        "One credential's result inside an operation: the operation's tenant. credential_id is a second "
        "NOT NULL reference into credentials, which stores org_id; the one writer (app/durable/nodevault.py) "
        "takes it off the operation's own org's rows, and its same-org key holds it there.",
        parent=credentials.NodeVaultOperation,
        fk_columns=("operation_id",),
        on_delete="CASCADE",
        same_org=(
            _credential(
                "An operation migrates its own org's credentials.",
                on_delete="CASCADE", null_is_legitimate=False,
            ),
        ),
    ),
    audit.CommandAudit: Stored("node_id is nullable. Carries argv verbatim, so it must not read across orgs."),
    audit.ClientEvent: Stored("user_id is nullable (SET NULL), so no derivation is total; the org is the reporter's."),
    credentials.Account: Stored(
        "node_id is nullable — NULL means fleet-wide today — so no derivation is total.",
        same_org=(
            _credential(
                "An API-key account runs on its own org's credential.",
                on_delete="RESTRICT", null_is_legitimate=True,
            ),
        ),
    ),
    agent_sessions.RunTelemetry: Stored("node_id is nullable and task_id is a string rather than a foreign key."),
    notifications.Alert: Stored(
        "The dedup key must gain an org term, or two orgs collapse into one tumbling window."
    ),
    notifications.SlackThread: Stored(
        "The correlation record; the channel becomes per-org. Derivable from its NOT NULL task_id, but "
        "stored DELIBERATELY, on the same reasoning as tasks above: it is written from the inbound "
        "Slack path, where the cheapest possible predicate matters and a join is one more thing to "
        "forget. Being a second copy of the same fact, it needs a (task_id, org_id) composite key to "
        "keep the two from disagreeing — the note a Derived entry would carry, written here because "
        "Stored has no field for it."
    ),
    credentials.Credential: Stored(
        "A third-party connection is a tenant's own.",
        same_org=(
            SameOrg(
                column="home_project_id",
                parent=projects.Project,
                on_delete="SET NULL",
                null_is_legitimate=True,
                reason="A credential's home project is in the credential's own org.",
            ),
        ),
    ),
    support.SiteEnquiry: Stored(
        "A marketing-site lead, always written into org zero: the site's visitors are the operator's "
        "prospects, and no route serves the table to a tenant."
    ),
    support.TrustDocumentRequest: Stored(
        "A Trust page visitor's request for trust documents, always written into org zero like SiteEnquiry: "
        "the requester is a prospect, not any tenant's member, and no route serves the table to a tenant."
    ),
    support.SiteChatConversation: Stored(
        "An anonymous website visitor's chat with website Jarvis, always written into org zero like "
        "SiteEnquiry: the visitor belongs to no tenant, and no route serves the table to a tenant."
    ),
    orgs.EmailSignup: Stored(
        "An email address proving it is reachable before it applies for an org, always written into org "
        "zero: nobody owns the address yet, so it is the operator's pre-tenant data (SiteEnquiry's "
        "verdict), and no route serves the table to a tenant."
    ),
    notifications.SlackInstallation: Stored(
        "An org's Slack workspace install, one per org. The org is its subject and there is no tenant-owned parent "
        "to derive through — org_domains' reason.",
        same_org=(
            _credential(
                "The credential row the workspace is catalogued as is the org's own; NULL until linked.",
                on_delete="SET NULL", null_is_legitimate=True,
            ),
        ),
    ),
    notifications.InboundEmail: Stored(
        "A received message, one row per target its recipients resolved to (`app/ses_inbound.py`), written "
        "into that target's org: org zero for the deployment's own mail domain, a tenant's org for an address a "
        "registered resolver claims, or a tenant's service-account Gmail. Each row is routed under its own org's "
        "bind; the Unmatched list serves a caller its own org's only."
    ),
    notifications.EmailSuppression: Stored(
        "One address system mail is not sent to, always written into org zero beside the delivery log it guards "
        "(EmailDelivery's reasoning); no route serves the table to a tenant."
    ),
    notifications.DmarcReport: Stored(
        "One DMARC aggregate report about the deployment's mail domain, always written into org zero beside the "
        "delivery log (EmailDelivery's reasoning); no route serves the table to a tenant."
    ),
    notifications.EmailDelivery: Stored(
        "One system email in the operator's delivery log, always written into org zero: system mail is the "
        "deployment's own whoever receives it, and no route serves the table to a tenant."
    ),
    automations.Cron: Stored(
        "System rows belong to org zero and tenant-authored rows to their own org. No nullable "
        "'global' arm."
    ),
    automations.Automation: Stored(
        "An automation lives in its owner's org, as its one When-detail child (a cron or a trigger) does; "
        "a system automation is machinery on org zero, like its system cron (app/crons.py::org_for)."
    ),
    automations.AutomationRunTarget: Stored(
        "A per-target ledger row of one firing, written by the fire engine into its automation's org. "
        "Stored, not Derived: it names its firing through either cron_run_id or delivery_id (exactly one), "
        "so neither run record can be the derivation, and the automation is a second reference."
    ),
    automations.TaskTrigger: Stored(
        "A trigger lives in its project's org, which the console route stamps from the project it names; "
        "its filings never leave that project.",
        same_org=(
            _project("A trigger files into a project of its own org.", on_delete="CASCADE", null_is_legitimate=False),
            SameOrg(
                column="preflight_credential_id",
                parent=credentials.Credential,
                on_delete="SET NULL",
                null_is_legitimate=True,
                reason="A trigger's preflight check signs with its own org's credential.",
            ),
        ),
    ),
    node_groups.NodeGroup: Stored(
        "A held capacity is a tenant's own commitment, and it is the parent the two node-group children "
        "derive through — storing here is what keeps that derivation one hop rather than two. Landed "
        "after the specification's census was taken.",
        same_org=(
            _credential(
                "A group launches with its own org's cloud credential.",
                on_delete="RESTRICT", null_is_legitimate=False,
            ),
        ),
    ),
    placement_groups.PlacementGroup: Stored(
        "A node group is an org's own named set of its nodes, and the parent its two join tables derive "
        "through — storing here keeps each of them one hop."
    ),
    support.Ticket: Stored(
        "A support ticket belongs to its REQUESTER'S org, and is the parent its links and reads derive "
        "through. Derivable from requester_id, but stored so the list's scope filter is a single-table "
        "predicate and the two children stay one hop. requester_id and the actor pair are actor "
        "references; conversation_id is a second reference into support_conversations — both classified "
        "in the unkeyed-reference register."
    ),
    support.SupportConversation: Stored(
        "One person's chat with the support agent, in that person's org. Stored rather than derived "
        "through user_id so support_messages derives one hop from it (the one-hop invariant); user_id "
        "and ticket_id are second references classified in the unkeyed-reference register."
    ),
    # -- Tenant-owned, org DERIVED -------------------------------------------------------------------
    notifications.DmarcReportRow: Derived(
        "One source's line of a DMARC aggregate report; it belongs to its report's org (org zero) and goes with it.",
        parent=notifications.DmarcReport,
        fk_columns=("dmarc_report_id",),
        on_delete="CASCADE",
    ),
    api_keys.ApiKeySecret: Derived(
        "One hashed secret of an API key; it belongs to its key's org and goes with the key.",
        parent=api_keys.ApiKey,
        fk_columns=("api_key_id",),
        on_delete="CASCADE",
    ),
    access.UserGroupMembership: Derived(
        "A membership belongs to the member's org. Its NOT NULL group_id reaches groups, which stores "
        "org_id — a second reference needing a same-org key of its own, not a second derivation, and "
        "the one place a membership could otherwise grant one org's user a seat in another org's "
        "permission bundle.",
        parent=identity.AdminUser,
        fk_columns=("user_id",),
        on_delete="CASCADE",
    ),
    access.RoleBinding: Derived(
        "Derives through its PRINCIPAL, an XOR: one of user_id/group_id "
        "is set on every row (ck_role_bindings_one_principal). The SCOPE side is held by `same_org` below, "
        "one key per object column; the fourth scope, scope_org_id, references the axis itself and is held "
        "to the row's own org by ck_role_bindings_scope_org_is_own_org rather than by a key. role_id "
        "points at the platform catalog (Shared) and needs none.",
        parent=(identity.AdminUser, access.Group),
        fk_columns=("user_id", "group_id"),
        on_delete="CASCADE",
        # All `null_is_legitimate=True`: `ck_role_bindings_one_scope` leaves two of three NULL on every row.
        same_org=(
            _project(
                "A project-scope binding must name a project in the principal's own org.",
                on_delete="CASCADE", null_is_legitimate=True,
            ),
            SameOrg(
                column="account_id",
                parent=credentials.Account,
                on_delete="CASCADE",
                null_is_legitimate=True,
                reason="An account-scope binding must name an account in the principal's own org.",
            ),
        ),
    ),
    access.Budget: Derived(
        "A monthly budget belongs to the ONE project or group it caps, an XOR (ck_budgets_one_scope); deleting "
        "either deletes it (`app/budgets.py`).",
        parent=(projects.Project, access.Group),
        fk_columns=("project_id", "group_id"),
        on_delete="CASCADE",
    ),
    credentials.ProjectCredential: Derived(
        "An attachment belongs to the project it attaches to; project_id is NOT NULL. The credential is a "
        "second NOT NULL reference (credential_id) and must be the same org's — one tenant's secret attached to another "
        "tenant's project is the breach this key refuses.",
        parent=projects.Project,
        fk_columns=("project_id",),
        on_delete="CASCADE",
        same_org=(
            _credential(
                "An attached credential must be the project's own org's.",
                on_delete="CASCADE", null_is_legitimate=False,
            ),
        ),
    ),
    credentials.ProjectMcpLink: Derived(
        "A project's explicit opt-in to an MCP connection; it belongs to the project it opts in, and project_id is "
        "NOT NULL. The MCP connection is a second NOT NULL reference (credential_id) and must be the same org's — "
        "ProjectCredential's reason.",
        parent=projects.Project,
        fk_columns=("project_id",),
        on_delete="CASCADE",
        same_org=(
            _credential(
                "An opted-in MCP connection must be the project's own org's.",
                on_delete="CASCADE", null_is_legitimate=False,
            ),
        ),
    ),
    worktrees.Worktree: Derived(
        "A slot belongs to the node that hosts it; node_id is NOT NULL. Its NOT NULL project_id is a second "
        "reference that carries NO same-org key on purpose: after a project move, a live runner's slot stays "
        "on its old-org node naming the moved project until it finishes (`app/carryover.py`).",
        parent=nodes.Node,
        fk_columns=("node_id",),
        on_delete="CASCADE",
    ),
    worktrees.SlotIntent: Derived(
        "An intent is addressed to one node. Its NOT NULL project_id is a second reference that carries "
        "NO same-org key on purpose: the org transfer's erase intent stays on the node and names the "
        "project that moved away (`transfer_project.py`, `Erase(\"node_command\")`).",
        parent=nodes.Node,
        fk_columns=("node_id",),
        on_delete="CASCADE",
    ),
    projects.Feature: Derived(
        "A feature is a folder of work inside one project. lead_id is caller-chosen rather than an actor "
        "stamp, so it is keyed below; deleted_by_id is an actor stamp.",
        parent=projects.Project,
        fk_columns=("project_id",),
        on_delete="CASCADE",
        same_org=(
            SameOrg(
                column="lead_id",
                parent=identity.AdminUser,
                on_delete="RESTRICT",
                null_is_legitimate=False,
                reason=(
                    "A feature's lead is named by the caller, not stamped from the session, so the actor "
                    "exemption does not cover it: it must be a person in the feature's own org."
                ),
            ),
        ),
    ),
    projects.ProjectEnvVar: Derived(
        "An environment variable is declared for one project, at the project tier or a user's.",
        parent=projects.Project,
        fk_columns=("project_id",),
        on_delete="CASCADE",
        same_org=(
            _credential(
                "The resolver writes the DECRYPTED value to the env file, so the credential must be the project's own org's; SET NULL keeps the tombstone.",
                on_delete="SET NULL", null_is_legitimate=True,
            ),
        ),
    ),
    projects.Runner: Derived(
        "A runner is a process on one node.",
        parent=nodes.Node,
        fk_columns=("node_id",),
        on_delete="CASCADE",
    ),
    releases.GateResult: Derived(
        "One gate's result belongs to the run that recorded it.",
        parent=releases.GateRun,
        fk_columns=("run_id",),
        on_delete="CASCADE",
    ),
    releases.StageHealth: Derived(
        "A stage address's last probe belongs to that stage.",
        parent=releases.ReleaseStage,
        fk_columns=("stage_id",),
        on_delete="CASCADE",
    ),
    releases.ReleaseApproval: Derived(
        "One approval belongs to the release it approves. user_id -> admin_users is the approver, an actor "
        "reference: an operator in org zero may approve, as they may author a tenant's row.",
        parent=releases.Release,
        fk_columns=("release_id",),
        on_delete="CASCADE",
    ),
    releases.ReleaseChange: Derived(
        "One change reference belongs to the release that carries it.",
        parent=releases.Release,
        fk_columns=("release_id",),
        on_delete="CASCADE",
    ),
    releases.ReleaseContained: Derived(
        "A sha observed on a stage's branch belongs to that stage. project_id -> projects is the stage's own "
        "project, copied by the one writer off the stage; it is part of the primary key, not a second owner.",
        parent=releases.ReleaseStage,
        fk_columns=("stage_id",),
        on_delete="CASCADE",
    ),
    releases.PipelineTemplateVersion: Derived(
        "A published version belongs to its template. created_by_id -> admin_users is the publisher, an actor "
        "reference.",
        parent=releases.PipelineTemplate,
        fk_columns=("template_id",),
        on_delete="CASCADE",
    ),
    releases.ReleaseFreezeStage: Derived(
        "A stage a freeze covers belongs to that freeze. stage_id -> release_stages is a same-project stage the "
        "freeze names.",
        parent=releases.ReleaseFreeze,
        fk_columns=("freeze_id",),
        on_delete="CASCADE",
    ),
    releases.PipelinePass: Derived(
        "An item moved into a no-branch stage belongs to that stage. project_id -> projects is the stage's own "
        "project, copied by the one writer off the stage; task_id -> tasks is the item, a same-project task; "
        "created_by_id -> admin_users is the actor.",
        parent=releases.ReleaseStage,
        fk_columns=("stage_id",),
        on_delete="CASCADE",
    ),
    releases.PipelineApproval: Derived(
        "A press or approval of a step belongs to the stage the step leads into. project_id -> projects is the "
        "stage's own project, copied by the one writer off the stage; task_id -> tasks is the item; user_id -> "
        "admin_users is the actor.",
        parent=releases.ReleaseStage,
        fk_columns=("stage_id",),
        on_delete="CASCADE",
    ),
    production_qa.DeploymentEnvironment: Derived(
        "An environment a deploy reported belongs to that deploy.",
        parent=production_qa.Deployment,
        fk_columns=("deployment_id",),
        on_delete="CASCADE",
    ),
    production_qa.DeploymentCommit: Derived(
        "A commit in a deploy's range belongs to that deploy.",
        parent=production_qa.Deployment,
        fk_columns=("deployment_id",),
        on_delete="CASCADE",
    ),
    production_qa.DeploymentPr: Derived(
        "A PR in a deploy's range belongs to that deploy.",
        parent=production_qa.Deployment,
        fk_columns=("deployment_id",),
        on_delete="CASCADE",
    ),
    production_qa.DeploymentMetric: Derived(
        "A regression measurement belongs to its deploy.",
        parent=production_qa.Deployment,
        fk_columns=("deployment_id",),
        on_delete="CASCADE",
    ),
    production_qa.IncidentEvent: Derived(
        "A transition belongs to its incident.",
        parent=production_qa.Incident,
        fk_columns=("incident_id",),
        on_delete="CASCADE",
    ),
    production_qa.IncidentSuspect: Derived(
        "A suspect belongs to its incident.",
        parent=production_qa.Incident,
        fk_columns=("incident_id",),
        on_delete="CASCADE",
    ),
    production_qa.IncidentTask: Derived(
        "A task claim belongs to its incident.",
        parent=production_qa.Incident,
        fk_columns=("incident_id",),
        on_delete="CASCADE",
    ),
    tasks.TaskQuestion: Derived(
        "A question is asked against one task.",
        parent=tasks.Task,
        fk_columns=("task_id",),
        on_delete="CASCADE",
    ),
    support.TicketTaskLink: Derived(
        "A link belongs to its ticket. task_id points ACROSS orgs by design — the operator's task, a "
        "tenant's ticket — so it is never a same-org key and only the task's state is read back "
        "(app/support_access.py::linked_task_states); it is in the unkeyed-reference register, as is created_by_id.",
        parent=support.Ticket,
        fk_columns=("ticket_id",),
        on_delete="CASCADE",
    ),
    support.TicketRead: Derived(
        "A read stamp belongs to its ticket. user_id is the reader, who may be an operator in another "
        "org, so it is a reference classified in the unkeyed-reference register rather than a derivation.",
        parent=support.Ticket,
        fk_columns=("ticket_id",),
        on_delete="CASCADE",
    ),
    support.SiteChatMessage: Derived(
        "A website chat turn belongs to its conversation, which stores the org.",
        parent=support.SiteChatConversation,
        fk_columns=("conversation_id",),
        on_delete="CASCADE",
    ),
    support.SupportMessage: Derived(
        "A turn belongs to its conversation, which stores the org.",
        parent=support.SupportConversation,
        fk_columns=("conversation_id",),
        on_delete="CASCADE",
    ),
    support.SupportDocsMiss: Derived(
        "A docs miss belongs to the conversation it was logged in, which stores the org. message_id (the "
        "assistant turn, in the same conversation) and requester_id (the asker) are second references "
        "classified in the unkeyed-reference register.",
        parent=support.SupportConversation,
        fk_columns=("conversation_id",),
        on_delete="CASCADE",
    ),
    task_extras.TaskComment: Derived(
        "A comment is written against one task XOR one ticket (ck_task_comments_one_subject), so it "
        "derives through whichever is set — role_bindings' shape. author_id is an ACTOR reference into "
        "admin_users and not the derivation — a second tenant-owned reference held by no key, classified "
        "in the unkeyed-reference register with every other actor column on "
        "tasks.created_by_id's terms (on a ticket the author may be an operator in another org).",
        parent=(tasks.Task, support.Ticket),
        fk_columns=("task_id", "ticket_id"),
        on_delete="CASCADE",
    ),
    task_extras.TaskCommentMention: Derived(
        "A mention belongs to the TASK or TICKET its comment is on (an XOR, "
        "ck_task_comment_mentions_one_subject), not to the comment: the one-hop invariant "
        "keeps a derivation pointing at a Stored parent, and task_comments is itself Derived. "
        "comment_id is therefore a second tenant-owned reference rather than the derivation, and it "
        "is classified in the unkeyed-reference register on task_tags.tag_id's "
        "terms. user_id is an actor reference and is classified there too.",
        parent=(tasks.Task, support.Ticket),
        fk_columns=("task_id", "ticket_id"),
        on_delete="CASCADE",
    ),
    task_extras.TaskCommentReaction: Derived(
        "A reaction belongs to the TASK or TICKET its comment is on (an XOR, "
        "ck_task_comment_reactions_one_subject) — task_comment_mentions' shape and for its reason: "
        "task_comments is itself Derived. comment_id and user_id are second tenant-owned references "
        "classified in the unkeyed-reference register on the mention's terms.",
        parent=(tasks.Task, support.Ticket),
        fk_columns=("task_id", "ticket_id"),
        on_delete="CASCADE",
    ),
    tasks.TaskDependency: Derived(
        "BOTH ends are tasks and both are NOT NULL, so both are named here. A cross-org edge is a "
        "document-exfiltration path rather than a modelling error — the pipeline threads a "
        "prerequisite's settled output into its dependant's prompt — so it also carries a same-org "
        "check, not merely a parent foreign key.",
        parent=tasks.Task,
        fk_columns=("task_id", "depends_on_task_id"),
        on_delete="CASCADE",
    ),
    # Living Governance (revision 0378): the child rows of a Stored governance parent.
    governance.GovPackRelease: Derived(
        "A snapshot of an org pack, belonging to it.", parent=governance.GovPack, fk_columns=("pack_id",), on_delete="CASCADE",
    ),
    governance.GovRule: Derived(
        "A rule of an org pack, belonging to it.", parent=governance.GovPack, fk_columns=("pack_id",), on_delete="CASCADE",
    ),
    governance.GovEdge: Derived(
        "An edge joins two graph entities — an AND, like task_ratings: both ends store org_id and must agree.",
        parent=(governance.GovEntity, governance.GovEntity),
        fk_columns=("src_id", "dst_id"),
        on_delete="CASCADE",
    ),
    governance.GovFact: Derived(
        "A fact about one graph entity.", parent=governance.GovEntity, fk_columns=("entity_id",), on_delete="CASCADE",
    ),
    governance.GovScanTarget: Derived(
        "One project of a scan. Its project_id is a second reference into projects, held by a same-org key.",
        parent=governance.GovScan,
        fk_columns=("scan_id",),
        on_delete="CASCADE",
        same_org=(
            _project("A scan targets a project of the scanning org.", on_delete="CASCADE", null_is_legitimate=False),
        ),
    ),
    governance.GovCampaignItem: Derived(
        "One project of a campaign. Its project_id is a second reference into projects, held by a same-org key.",
        parent=governance.GovCampaign,
        fk_columns=("campaign_id",),
        on_delete="CASCADE",
        same_org=(
            _project("A campaign fixes projects of its own org.", on_delete="CASCADE", null_is_legitimate=False),
        ),
    ),
    tasks.TaskRating: Derived(
        "A rating is a (task, user) pair — an AND, not an XOR: both columns are NOT NULL, both store "
        "org_id, and the two must AGREE on it.",
        parent=(tasks.Task, identity.AdminUser),
        fk_columns=("task_id", "user_id"),
        on_delete="CASCADE",
    ),
    tasks.TaskDoneView: Derived(
        "A seen mark is a (task, user) pair — task_ratings' AND: both columns are NOT NULL, both store "
        "org_id, and the two must AGREE on it.",
        parent=(tasks.Task, identity.AdminUser),
        fk_columns=("task_id", "user_id"),
        on_delete="CASCADE",
    ),
    tasks.TaskQuestionDraft: Derived(
        "An unsent answer is a (task, user) pair — task_ratings' AND: both columns are NOT NULL, both store "
        "org_id, and the two must AGREE on it. question_id is a second tenant-owned reference (task_questions "
        "is itself Derived), classified in the unkeyed-reference register on the mention's comment_id terms.",
        parent=(tasks.Task, identity.AdminUser),
        fk_columns=("task_id", "user_id"),
        on_delete="CASCADE",
    ),
    task_extras.Tag: Derived(
        "A tag belongs to the taxonomy that defines it.",
        parent=task_extras.TagGroup,
        fk_columns=("tag_group_id",),
        on_delete="CASCADE",
    ),
    task_extras.TaskTag: Derived(
        "The tagging belongs to the task. Its NOT NULL tag_id reaches tags — a second reference "
        "needing a same-org key of its own, not a second derivation, and one this entry can now DECLARE: "
        "the mechanism it was waiting for exists (`same_org`, above). tags is itself Derived and so "
        "carries a local org_id, so the key is spellable as (tag_id, org_id) REFERENCES tags(id, org_id) "
        "as soon as tags carries a UNIQUE(id, org_id) — a Derived parent may carry one "
        "(app/models/base.py::org_unique). What is missing is a `same_org` entry here plus a revision "
        "building that target, and until then same-org is held by nothing — see app/tenancy/keys.py's "
        "unkeyed-reference register, the single home for that hole.",
        parent=tasks.Task,
        fk_columns=("task_id",),
        on_delete="CASCADE",
    ),
    task_extras.TaskAttachment: Derived(
        "An attachment belongs to exactly one of its task, its ticket, the support conversation it was "
        "pasted into, the automation whose Task action carries it, or the org template whose image it is "
        "(ck_task_attachments_one_subject). "
        "source_task_id is a nullable provenance pointer and is not the derivation.",
        parent=(
            tasks.Task, support.Ticket, support.SupportConversation, automations.Automation,
            task_templates.OrgTaskTemplate,
        ),
        fk_columns=("task_id", "ticket_id", "conversation_id", "automation_id", "org_template_id"),
        on_delete="CASCADE",
    ),
    tasks.TaskBodyVersion: Derived(
        "A body version belongs to its task.",
        parent=tasks.Task,
        fk_columns=("task_id",),
        on_delete="CASCADE",
    ),
    operator_dashboard.OutcomeReview: Derived(
        "An AI quality review belongs to the task it reviewed.",
        parent=tasks.Task,
        fk_columns=("task_id",),
        on_delete="CASCADE",
    ),
    task_extras.TaskPrStats: Derived(
        "A merged PR's diff stats belong to the task that settled it.",
        parent=tasks.Task,
        fk_columns=("task_id",),
        on_delete="CASCADE",
    ),
    task_extras.TaskPrChanges: Derived(
        "A merged PR's provenance belongs to the task that settled it.",
        parent=tasks.Task,
        fk_columns=("task_id",),
        on_delete="CASCADE",
    ),
    task_extras.TaskPr: Derived(
        "A PR a task opened belongs to that task. Its `project_repo_id` -> project_repos is set "
        "only by `app/task_prs.py`, from a repository of the task's own project.",
        parent=tasks.Task,
        fk_columns=("task_id",),
        on_delete="CASCADE",
    ),
    task_extras.ComplianceManifest: Derived(
        "A task's sealed compliance manifest belongs to the task it describes.",
        parent=tasks.Task,
        fk_columns=("task_id",),
        on_delete="CASCADE",
    ),
    projects.ProjectRepo: Derived(
        "A project's connected repositories belong to the project.",
        parent=projects.Project,
        fk_columns=("project_id",),
        on_delete="CASCADE",
    ),
    # Live Cast (`app/live_cast/models.py`): a project's tab and its items' verdicts and overrides.
    live_cast.LiveCastProject: Derived(
        "A project's Live Cast settings belong to the project.",
        parent=projects.Project,
        fk_columns=("project_id",),
        on_delete="CASCADE",
    ),
    live_cast.LiveCastItem: Derived(
        "A Live Cast verdict or override on one of the project's tasks belongs to the project; "
        "item_id/task_id are unkeyed (an item row outliving its task is never visible).",
        parent=projects.Project,
        fk_columns=("project_id",),
        on_delete="CASCADE",
    ),
    credentials.AccountObservation: Derived(
        "An observation is made on one node; account_id is nullable and so cannot be the derivation.",
        parent=nodes.Node,
        fk_columns=("node_id",),
        on_delete="CASCADE",
    ),
    notifications.NotificationChannel: Derived(
        "One place a membership's alerts go: owned by that membership, so it is that membership's tenant. "
        "The Slack and Telegram destinations it names are identifiers, not tenant rows; telegram_link_id and "
        "email_id point at the PERSON's shared rows (telegram_links, notification_emails), never an org source.",
        parent=identity.AdminUser,
        fk_columns=("admin_user_id",),
        on_delete="CASCADE",
    ),
    credentials.UserGitConnection: Derived(
        "A git connection says which of a person's credentials their work uses on one forge root, so it "
        "is that person's tenant. credential_id is a second NOT NULL reference into credentials, which "
        "stores org_id: `same_org` below holds it, since a connection naming another org's credential "
        "would put one tenant's secret behind another tenant's push.",
        parent=identity.AdminUser,
        fk_columns=("user_id",),
        on_delete="CASCADE",
        same_org=(
            _credential(
                "A connection must name a credential in its person's own org.",
                on_delete="CASCADE", null_is_legitimate=False,
            ),
        ),
    ),
    credentials.ConsoleApiKeyLease: Derived(
        "A console key lease authorizes one account's key, so it is that account's tenant — the org the "
        "key-serving route's read seam binds. node_id is a second NOT NULL reference into nodes, which "
        "stores org_id; only the account can be the derivation (one hop), and the writer takes both off "
        "one console pick whose account the node's org floor already confined (routes/console.py). It "
        "is classified in the unkeyed-reference register, as is created_by into "
        "admin_users (an actor reference: the user whose console create wrote the lease).",
        parent=credentials.Account,
        fk_columns=("account_id",),
        on_delete="CASCADE",
    ),
    agent_sessions.FetchStamp: Derived(
        "A fetch stamp is a per-node fact; the node is the resource-side root. Nothing reads or writes the table "
        "(a later migration drops it); its NOT NULL project_id carries no same-org key, as when a carried-over "
        "runner's node (`app/carryover.py`) stamped the project that moved away.",
        parent=nodes.Node,
        fk_columns=("node_id",),
        on_delete="CASCADE",
    ),
    nodes.NodeHostMetric: Derived(
        "A host metric is a measurement of one node.",
        parent=nodes.Node,
        fk_columns=("node_id",),
        on_delete="CASCADE",
    ),
    nodes.NodeSessionUsage: Derived(
        "A session usage minute is a measurement on one node, written under the node's org. task_id is "
        "node-attested and has no FK, so it is not a derivation: readers join tasks only where the task's org "
        "equals the row's (`app/node_session_usage.py`).",
        parent=nodes.Node,
        fk_columns=("node_id",),
        on_delete="CASCADE",
    ),
    nodes.TaskSessionUsagePeak: Stored(
        "A session's lifetime peaks, written under the node's org. Neither task_id nor node_id has an FK (the "
        "peak outlives a deleted node, and is run telemetry), so there is no parent to derive through; readers "
        "join tasks only where the task's org equals the row's (`app/node_session_usage.py`)."
    ),
    nodes.NodeDiskEvent: Derived(
        "A disk event is a reclaim or a gate decision on one node.",
        parent=nodes.Node,
        fk_columns=("node_id",),
        on_delete="CASCADE",
    ),
    nodes.Microvm: Derived(
        "A microVM runs on one node; task_id is nullable (a console session's VM has none) and so cannot be "
        "the derivation. project_id -> projects is the second NOT NULL reference, held by its same-org key.",
        parent=nodes.Node,
        fk_columns=("node_id",),
        on_delete="CASCADE",
        same_org=(
            _project("A VM runs a project of its node's org.", on_delete="CASCADE", null_is_legitimate=False),
        ),
    ),
    nodes.MicrovmIntent: Derived(
        "A MicroVM order is addressed to one node, exactly as a slot intent is, and the minters are fleet-wide "
        "passes with no org on the request context. vm_id is deliberately no foreign key (a task delete's "
        "destroy must outlive the cascaded microvms row), so the node is the only reference.",
        parent=nodes.Node,
        fk_columns=("node_id",),
        on_delete="CASCADE",
    ),
    credentials.CloudConnection: Derived(
        "A cloud connection signs with one AWS credential, and a credential is a tenant's own.",
        parent=credentials.Credential,
        fk_columns=("credential_id",),
        on_delete="CASCADE",
    ),
    automations.CronRun: Derived(
        "A run belongs to the cron that produced it; task_id is nullable and so cannot be the "
        "derivation.",
        parent=automations.Cron,
        fk_columns=("cron_id",),
        on_delete="CASCADE",
    ),
    automations.TriggerDelivery: Stored(
        "A delivery is written into its trigger's org by the one writer (`app/trigger_intake.py`), which reads "
        "the org off the trigger under `bind_from`. Stored, not Derived: an unmatched Slack mention or operator "
        "email has no trigger (trigger_id NULL) and is written into org zero, so the trigger cannot be the "
        "derivation."
    ),
    automations.TriggerNotice: Stored(
        "A notice to one trigger owner, written by `app/trigger_notices.py` into the trigger's org under that "
        "trigger's `bind_from`. Stored, not Derived: a failure notice outlives a deleted trigger (trigger_id "
        "SET NULL), so the trigger cannot be the derivation."
    ),
    automations.TriggerDedup: Derived(
        "A dedup reservation belongs to its trigger; task_id is nullable provenance.",
        parent=automations.TaskTrigger,
        fk_columns=("trigger_id",),
        on_delete="CASCADE",
    ),
    node_groups.NodeGroupCapacity: Derived(
        "A capacity line belongs to its group. ONE hop, because node_groups stores org_id — deriving "
        "through the group's credential instead would be two. Landed after the specification's census "
        "was taken.",
        parent=node_groups.NodeGroup,
        fk_columns=("group_id",),
        on_delete="CASCADE",
    ),
    node_groups.NodeGroupProject: Derived(
        "A group's project entitlement belongs to the group; project_id is NOT NULL too, but the group "
        "is the owning side. Landed after the specification's census was taken.",
        parent=node_groups.NodeGroup,
        fk_columns=("group_id",),
        on_delete="CASCADE",
        same_org=(
            _project("A group is entitled to its own org's projects.", on_delete="CASCADE", null_is_legitimate=False),
        ),
    ),
    node_groups.NodeProject: Derived(
        "An assignment of a project to a node belongs to the node; node_id is NOT NULL. Its NOT NULL "
        "project_id reaches projects, which stores org_id — a second reference needing a same-org key "
        "of its own, not a second derivation.",
        parent=nodes.Node,
        fk_columns=("node_id",),
        on_delete="CASCADE",
        same_org=(
            _project("A node is assigned to its own org's projects.", on_delete="CASCADE", null_is_legitimate=False),
        ),
    ),
    placement_groups.PlacementGroupNode: Derived(
        "A node's membership of a node group belongs to the group. Its NOT NULL node_id reaches nodes, which "
        "stores org_id — a second reference needing a same-org key of its own, not a second derivation.",
        parent=placement_groups.PlacementGroup,
        fk_columns=("group_id",),
        on_delete="CASCADE",
        same_org=(
            SameOrg(
                column="node_id",
                parent=nodes.Node,
                on_delete="CASCADE",
                null_is_legitimate=False,
                reason="A node group holds only nodes of its own org.",
            ),
        ),
    ),
    placement_groups.ProjectPlacementGroup: Derived(
        "A project's allocation to a node group belongs to the group; project_id is NOT NULL too, but the "
        "group is the owning side, as for a node-group project line.",
        parent=placement_groups.PlacementGroup,
        fk_columns=("group_id",),
        on_delete="CASCADE",
        same_org=(
            _project("A node group runs only its own org's projects.", on_delete="CASCADE", null_is_legitimate=False),
        ),
    ),
    node_groups.GhaRunnerPool: Derived(
        "A pool of self-hosted GitHub Actions runners belongs to the NODE that hosts them; node_id is "
        "NOT NULL and UNIQUE, so one node has at most one pool. Its NOT NULL project_id and "
        "credential_id are SECOND references into tenant-owned tables rather than a second derivation — "
        "the node is the owning side, and it is the only hop that is total. project_id carries a same-org "
        "key; credential_id does not, since the operator may lend their own personal credential to any "
        "org's pool (the unkeyed register in test_tenancy_composite_keys.py).",
        parent=nodes.Node,
        fk_columns=("node_id",),
        on_delete="CASCADE",
        same_org=(
            _project("A pool serves a project of its node's org.", on_delete="RESTRICT", null_is_legitimate=False),
        ),
    ),
    orgs.OrgSignup: Derived(
        "The self-service sign-up behind an org belongs to the user who signed up, and that user is the "
        "org's own: owner_id is NOT NULL, so the composite key holds the owner inside the org the row "
        "names.",
        parent=identity.AdminUser,
        fk_columns=("owner_id",),
        on_delete="RESTRICT",
    ),
    nodelinks.OrgSharedStore: Derived(
        "An org's shared-accounts store lives in the AWS account its credential authenticates to, and "
        "that credential is the org's own — CloudConnection's derivation, for the same reason.",
        parent=credentials.Credential,
        fk_columns=("credential_id",),
        on_delete="CASCADE",
    ),
    nodelinks.NodeLink: Stored(
        "A NodeLink is an org's own shared directory, created inside one org. STORED rather than derived "
        "through its store: org_shared_stores is itself Derived (through its credential), so deriving "
        "here would be two hops. store_id stays in the unkeyed-reference register; it crosses orgs on purpose for a "
        "Jaah-managed store only (the operator plane's, `managed_location` set)."
    ),
    nodelinks.NodeNodeLink: Derived(
        "An attachment says one node mounts one NodeLink, so it belongs to the node. nodelink_id is a "
        "second NOT NULL reference into nodelinks, which stores org_id: `same_org` holds it, so no node "
        "can ever be attached to another org's NodeLink (AC2.4).",
        parent=nodes.Node,
        fk_columns=("node_id",),
        on_delete="CASCADE",
        same_org=(
            SameOrg(
                column="nodelink_id",
                parent=nodelinks.NodeLink,
                on_delete="CASCADE",
                null_is_legitimate=False,
                reason="A node may mount only a NodeLink of its own org.",
            ),
        ),
    ),
    nodelinks.NodeGroupNodeLink: Derived(
        "A group attachment belongs to its group, as its capacity lines do. nodelink_id is held in the "
        "group's org by `same_org`, as a node attachment is.",
        parent=node_groups.NodeGroup,
        fk_columns=("node_group_id",),
        on_delete="CASCADE",
        same_org=(
            SameOrg(
                column="nodelink_id",
                parent=nodelinks.NodeLink,
                on_delete="CASCADE",
                null_is_legitimate=False,
                reason="A group's nodes may mount only a NodeLink of the group's own org.",
            ),
        ),
    ),
    # -- Multi-Node Hosts: an operator host's rows live in org zero, the host's org, so the seam hides them
    # from every tenant-bound read; a tenant host's rows live in the tenant's own org.
    worktrees.VnodeHost: Derived(
        "The marker that makes a node a Multi-Node Host belongs to that node; node_id is the primary key "
        "and NOT NULL, so the host is in its node's org: org zero for an operator host (every row the "
        "operator's, invisible to a tenant-bound read) or a tenant's own org for a tenant host "
        "(routes/nodes/cloud.py::launch_cloud_node_row / register_self_hosted_row, run by an Add-node order).",
        parent=nodes.Node,
        fk_columns=("node_id",),
        on_delete="RESTRICT",
    ),
    worktrees.VirtualNode: Stored(
        "One co-located node's placement on a host. STORED, not derived: its NOT NULL reference is "
        "host_node_id -> vnode_hosts, itself Derived, so a derivation would be two hops. Its org is the "
        "HOST's, written explicitly at create — never the node's, whose org is the data column "
        "tenant_org_id. For an operator host (org zero) that is the whole point: served under a tenant's "
        "binding it would reveal the host and every sibling (R3), and the seam's org predicate is what "
        "makes that impossible. A tenant host carries only its own org's nodes, so there the two agree. "
        "host_node_id and node_id are second references classified in the unkeyed-reference register — node_id "
        "crosses into the TENANT's org by design on an operator host, which a same-org key would forbid."
    ),
    nodes.NodeOrder: Stored(
        "One Add-node submission belongs to the org that ordered it, which is the org its node is made in. "
        "STORED: it exists before any node does (and outlives a failed one), so there is no parent to "
        "derive through. host_node_id and node_id are second references classified in the unkeyed-reference register — "
        "host_node_id names an operator host in org zero for a Shared hosting by design."
    ),
    hosting.NodeHosting: Stored(
        "One hosting belongs to the org that orders (and pays for) it, which is the org its machine or node is "
        "made in. STORED: it exists before its machine does (it is charged first) and outlives a terminated "
        "one, so there is no parent to derive through. host_node_id and node_id are second references "
        "classified in the unkeyed-reference register."
    ),
    hosting.HostingChange: Derived(
        "One product change belongs to the hosting it changes. new_host_node_id is a second reference "
        "(a reprovision's new host, made in the hosting's own org) classified in the unkeyed-reference register.",
        parent=hosting.NodeHosting,
        fk_columns=("rental_id",),
        on_delete="CASCADE",
    ),
    orgs.OrgInvitation: Stored(
        "An invitation belongs to the org that invites (the org the accept makes the membership in). STORED: "
        "its invitee is a PERSON (`identities`, Shared), not a row of any org, so there is no parent to derive "
        "through. The invitee reads their own under `seam.identity_scope()` filtered by `identity_id`, never "
        "by org (`app/org_invitations.py`)."
    ),
    worktrees.VnodeEnrolment: Derived(
        "One sealed enrolment envelope per vnode; virtual_node_id is the primary key and NOT NULL, so the "
        "envelope is its vnode's and in its vnode's org (zero). Read once by the carrying host's token.",
        parent=worktrees.VirtualNode,
        fk_columns=("virtual_node_id",),
        on_delete="CASCADE",
    ),
    # -- Tenant-shared by design ---------------------------------------------------------------------
    hosted_apps.HostingCell: Shared(
        "A database cell of the apps account (one Postgres instance), mirrored from its config: every org's apps"
        " are placed in the same cells, so a cell belongs to no org and holds no tenant data."
    ),
    hosted_apps.HostedAppDomainMonth: Shared(
        "A custom hostname that served an app in a metered month, keyed by environment and hostname for the apps "
        "account's usage query: it names a hostname and an app slug, both public, and no org."
    ),
    credentials.CredentialIdTombstone: Shared(
        "A deleted credential's id, kept so it is never minted again (its old envelopes would open under a"
        " re-created row). The id space is deployment-wide — a row carries no org and serves no reader."
    ),
    access.Role: Shared(
        "The built-in role catalog, shipped by revision 0286 from app/access_catalog.py — permissions' "
        "reason: a tenant cannot add or edit one, so a per-org copy would be N copies of one constant."
    ),
    tasks.BuiltInPlaybook: Shared(
        "The built-in catalog, one global copy (D1), seeded by revision 0310 from app/playbook_built_ins.py "
        "— a tenant cannot add or edit one (it duplicates one into `playbooks` instead), so a per-org copy "
        "would be N copies of one constant."
    ),
    task_templates.TaskTemplate: Shared(
        "The operator's global template library, one copy every org reads — built_in_playbooks' reason: a tenant "
        "cannot add or edit one (it duplicates one into `org_task_templates` instead)."
    ),
    task_templates.TaskTemplateAsset: Shared(
        "An image of a global template, shared with it: one copy every org reads, written only by the operator."
    ),
    task_templates.TaskTemplateStartDay: Shared(
        "A global template's daily start count summed over every org, deliberately carrying no org: popularity "
        "(PRD D8) is read without a cross-tenant read of `tasks`, and a row says nothing about which org started what."
    ),
    access.RolePermission: Shared("The catalog's expansion of each role into keys; the same catalog, the same verdict."),
    # The PERSON's security records (revision 0361): each is keyed on `identity_id` and read only for
    # the caller's own identity, resolved by a sign-in door or a session — never listed by an org-scoped
    # route, never written for anyone but that person (or by an administrator's MFA reset on their member).
    # `user_id` stays as the membership that acted until revision 2 of the identity contraction.
    identity.UserMfaFactor: Shared(
        "A person's second factor: one per identity, so a person enrols once and every membership is "
        "challenged by and passes the same factor. An org's MFA policy is the org's "
        "(`Org.mfa_skip`); the factor that satisfies it is the person's, like the password.",
    ),
    identity.UserRecoveryCode: Shared(
        "A person's recovery code, issued with and standing in for their one factor; per identity for the "
        "factor's reason.",
    ),
    identity.UserTrustedDevice: Shared(
        "A browser the person proved their factor on: it skips the person's factor, so it is theirs, and a "
        "password change or an MFA reset forgets every one of them.",
    ),
    identity.NotificationEmail: Shared(
        "An email address a PERSON proved for notifications: one proof per person serves every org they act "
        "in, as TelegramLink's chat does, and each membership's own Email channel decides whether that org "
        "sends it anything. Read and written only for the caller's own identity or by the public verify link."
    ),
    identity.TelegramLink: Shared(
        "The Telegram chat a PERSON proved through the bot's deep link: one link per person serves "
        "every org they act in, as their sign-in does, and each membership's own Telegram method decides "
        "whether that org sends it anything. Read and written only for the caller's own identity or by the "
        "bot's webhook; `code_admin_user_id` is a pointer to the asking membership, never an org source."
    ),
    orgs.SsoSpentRequest: Shared(
        "A SAML request id an ACS already answered (`app/sso_signin.py::spend_request`): an opaque id the SP minted, "
        "no tenant data, written on an unbound sign-in door before any org's row is read. The id is random, so one "
        "global key set refuses a replay whichever org's door it reaches."
    ),
    identity.PasswordReset: Shared(
        "A link that sets the person's password, which is identity-level (`identities.password_hash`): "
        "rate-limited and spent per identity. `user_id` is the membership whose eligibility the link is "
        "checked against; the door binds that membership's org before reading it.",
    ),
    identity.LegalAcceptance: Shared(
        "The person accepting the Terms and Privacy Policy, append-only evidence about a person; read only to "
        "ask whether a person still owes it (`signup.terms_pending`), inside the owner's org.",
    ),
    identity.UserIdentity: Shared(
        "The PERSON behind one or more memberships: an `admin_users` row is one membership in one "
        "org, and every org-owned fact keeps deriving through it, so this row carries no org of its own and "
        "is never filtered. Read only to resolve the caller under `seam.identity_scope()` (its own sign-in, "
        "its own `/me`) and never listed by an org-scoped route; `last_active_admin_id` is a pointer that "
        "sign-in reads, never an org source."
    ),
    access.Permission: Shared(
        "The keys are a derived cross-product of FEATURES x ACTIONS computed in code and pinned by a "
        "test. A tenant cannot add one, so a per-org copy would be N copies of a single code constant."
    ),
    control_plane.SystemState: Shared(
        "Operator-global AS A TABLE, and NOT because every key is operator data. `operator_settings` "
        "and `seed_bootstrap` are; `pipeline:stamp:*` is NOT — app/alert_stamps.py keys it on a task, "
        "epic, question or slack-thread id, so the KEY SET is a cross-tenant census of ids and alert "
        "state even though the value is always {}. SHARED IS THE VERDICT ONLY BECAUSE NOTHING SERVES "
        "THIS TABLE — no route reads it — and it genuinely mixes operator-global rows (seed_bootstrap, "
        "operator_settings) with per-entity keyed ones, so Stored would put the operator-global rows "
        "out of a tenant context's reach. The pipeline:stamp:* family must move to a home of its own "
        "BEFORE anything org-scoped reads this table; no boundary in this epic owns that today, and it "
        "is raised rather than left implied. Two further consequences: the per-org knobs move to an "
        "org-scoped settings home rather than growing an org segment inside a 64-char KV key, and "
        "alert_stamps.retain()'s fleet-wide prefix DELETE must STAY fleet-wide — an org-scoped caller "
        "would re-arm every other org's stamps. "
        "The managed-settings consent hash is NOT kept here: a fleet-wide key let one org's nodes set "
        "the value seeded into every other org's, and a tenant on another Anthropic org reports a "
        "different one, so it is per org in `org_consent_hashes`."
    ),
    notifications.SlackEvent: Shared(
        "An at-most-once dedup record for an inbound envelope, keyed on Slack's event_id and written "
        "BEFORE routing, when no org is yet known — the row is (id, event_id, created_at) and carries "
        "no payload. The cleanest genuinely-shared row in the schema. One consequence, the same one "
        "system_state records: once the channel is per-org the ROW SET is a per-org message-arrival "
        "timeline, so its retention prune must stay fleet-wide rather than become an org-scoped call."
    ),
    metering.ModelPrice: Shared(
        "Token rates are the vendor's and identical for everyone. What makes that SAFE is write "
        "authority, not the values: every write goes through require_operator (app/auth.py — the "
        "superadmin flag AND an operator-plane org, since a superadmin inside a tenant org is that "
        "tenant's administrator and not the fleet's). The day a per-org admin exists, one tenant "
        "editing a rate reprices every other tenant's cost figures globally and non-retroactively — so "
        "this verdict is contingent on the write door staying operator-plane, and is revisited then or "
        "when a tenant on its own API key "
        "negotiates its own rates."
    ),
    notifications.EmailTemplate: Shared(
        "The text of the deployment's own outgoing mail — invitations and notifications sent from the "
        "deployment mailbox, which is the operator's, not a tenant's. What makes that SAFE is write "
        "authority: every route, reads included, goes through require_operator (app/auth.py), so ModelPrice's "
        "argument holds — the day a per-org admin could write one, one tenant would reword every other "
        "tenant's mail, so this verdict is contingent on the write door staying operator-plane, and is "
        "revisited when a tenant gets a mailbox or branding of its own."
    ),
    orgs.TenancyExclusion: Shared(
        "THE BOUNDARY'S OWN TRAIL, and it must be operator-global for the same reason a per-org copy "
        "would defeat it: the log's whole use is an operator comparing what one org's traffic was "
        "refused against what every other org's was, and a tenant-scoped copy would show a tenant only "
        "the exclusions it caused. `bound_org` is a DATA column recording which org was bound (NULL "
        "meaning none was), never a tenancy column — hence no `OrgScoped` mixin and no `org_id`. One "
        "consequence, the same one `system_state` and `slack_events` record: the ROW SET is a "
        "cross-tenant census of which orgs' reads were refused, so its retention prune must stay "
        "fleet-wide (it rides `activity_events_retention_days`, `app/cron_retention.py`), and the route "
        "serving it is `@superadmin_only(Tenancy.OPERATOR)` — operator-plane on the PERMISSION axis "
        "and not only on this one. It was `system:view` until THIS verdict forced the tightening: "
        "`app/routes/tenancy_exclusions.py`'s header is the single home for that argument; "
        "`app/models/orgs.py::TenancyExclusion` is the single home for the rest."
    ),
    # ── The control plane's own runtime state: fleet-plane rows about API PROCESSES
    # and node SOCKETS, which no tenant owns. Their prunes are peer heartbeats, fleet-wide by construction.
    control_plane.CpPeer: Shared(
        "One row per running API process (address + heartbeat), the registry that lets N tasks be one "
        "control plane. It names no org and no tenant object; `app/cp_peers.py` is its single home."
    ),
    control_plane.CpEcsTask: Shared(
        "One row per ECS task of the control plane's own service (its stop reason and exit codes), kept past "
        "ECS's hour. It names no org and no tenant object; `app/cp_ecs.py` is its single home."
    ),
    nodes.NodeBusSocket: Shared(
        "Which peer holds a node's live WebSocket, keyed on the bus machine key the node presented — "
        "a routing fact about a socket, read by every peer to forward a command and to answer "
        "presence. Not a `nodes` reference: an unregistered node is present on the bus too, and which "
        "org a node belongs to is `nodes.org_id`'s answer, found by `app/node_identity.py`, never here."
    ),
    control_plane.ConsoleTicket: Shared(
        "One console session's rendezvous between the API and the console relay: the hashes of its two "
        "single-use leg tickets, the node the dial-back must come from, and the relay task that holds it. "
        "The authorization it stands for was decided in the console route that minted it; the viewer's "
        "id and session epochs bind the browser leg's claim to that login and let the API's sweep revoke it once ended."
    ),
    control_plane.RateLimitHit: Shared(
        "One counted request for a rate-limit bucket and key (a login IP, a public route), kept so the "
        "limit holds fleet-wide rather than per process. The key is a request property, never an org's; "
        "day-old rows are pruned by the peer heartbeat."
    ),
    control_plane.RequestIdempotency: Shared(
        "One console write's idempotency key and the answer it got (`app/idempotency.py`), keyed by principal — "
        "the session's `admin_id` — like `rate_limit_hits`. Read only by the same principal's next copy of that "
        "write, after the route's own authz already answered the first; it carries no `org_id` and is pruned "
        "after 24 hours by the `request-idempotency-prune` cron."
    ),
    identity.LoginFailure: Shared(
        "One counted refused sign-in for the lockout (`app/login_lockout.py`): an identifier's keyed hash, a "
        "source IP and a time, kept so the lock holds fleet-wide. A sign-in attempt precedes any org — an "
        "unknown identifier has none — and the row names no user; the org-filed `login_failed` activity "
        "row is the readable record. Hour-old rows are pruned by the peer heartbeat."
    ),
    billing.BillingState: Shared(
        "The deployment's one billing machinery row: which Stripe account is connected and how far the event "
        "poll got. It names no org; the operator Settings > Billing panel renders it, and an org's plan read and "
        "caps read only its pinned account id and mode (`app/billing/state.py::pinned`)."
    ),
    billing.BillingEvent: Shared(
        "The deployment's Stripe event log, deduplicated on Stripe's id. An event is about ONE account, not "
        "one org: its org is resolved when it is processed and lands on the mirror row, never here."
    ),
    billing.BillingStripeWrite: Shared(
        "The deployment's log of every write it sent to Stripe (`app/billing/gateway.py`). A write is the "
        "deployment's act on its own Stripe account; the customer's org is a copied pointer for the operator's "
        "filter, and only the operator's Settings > Billing > Stripe log serves the table."
    ),
    billing.BillingReconcileRun: Shared(
        "One read-only comparison of the deployment's Stripe account with what Jaah expects, across every "
        "customer at once; only the operator's Settings > Billing > Reconcile serves it."
    ),
    billing.BillingReconcileDiff: Shared(
        "One difference a deployment-wide reconcile run found (billing_reconcile_runs' reason); the customer's org "
        "is a copied pointer, and only the operator's Reconcile screen serves the table."
    ),
    products.ProductKind: Shared(
        "The product kinds the engine sells: a lookup every engine table references, the same for every org."
    ),
    products.Product: Shared(
        "The product catalogue every org buys from, plus typed products an operator makes for one org: "
        "customer_org_id is a copied pointer, never the tenancy column (a catalogue product has no org) — "
        "billing_plans' reason."
    ),
    products.ProductOption: Shared(
        "One priced choice of a catalogue product (an instance type, a storage size) — products' reason."
    ),
    products.ProductPrice: Shared(
        "One price of a catalogue product or option — products' reason."
    ),
    products.ProductLine: Shared(
        "What a plan includes, plus one org's override and comp lines in the same table: customer_org_id is a "
        "copied pointer, never the tenancy column (a plan's line has no org) — billing_plans' reason."
    ),
    hosting.AllowedInstanceType: Shared(
        "The EC2 instance types the deployment allows (the cost control on every cloud launch and the menu its "
        "hosting products sell from). A tenant cannot add one, so a per-org copy would be N copies of one list — "
        "billing_plans' reason."
    ),
    billing.BillingPlan: Shared(
        "The plan catalogue every org chooses from (seeded `team`, `enterprise`). A tenant cannot add one, so "
        "a per-org copy would be N copies of one catalogue — model_prices' reason."
    ),
    governance.GovManagedPack: Shared(
        "A managed compliance pack (Living Governance, revision 0378): one global copy every org may scan with, "
        "seeded from a TOML. A tenant cannot edit one (it forks one into `gov_packs` instead), so a per-org copy "
        "would be N copies of one constant — built_in_playbooks' reason."
    ),
    governance.GovManagedPackRelease: Shared("One released version of a managed pack; gov_managed_packs' reason."),
    orgs.OrgTransferCarryover: Shared(
        "Operator machinery for a project move: one live runner's sanctioned tail on the "
        "org-A node it was running on when its task moved to org B. It is read by `routes/agent/auth.py::"
        "_check_token` BEFORE any org is bound — the read DECIDES which org the request binds — so it cannot "
        "itself be org-filtered, and a row names two orgs (`from_org_id`, `to_org_id`) by design. Written only "
        "by the operator's transfer engine and closed by the existing end edges (`app/carryover.py`).",
    ),
    live_cast.LiveViewerEvent: Shared(
        "One page view, action or console click by an anonymous Live Cast viewer: the viewer belongs "
        "to no org. The project it names (`viewed_org_id`, `project_id`) is a published page's, read by the "
        "operator's Live tab alone, fleet-wide — no tenant reads it, so there is nothing to scope."
    ),
    live_cast.LiveViewerMinute: Shared(
        "An anonymous Live Cast viewer's presence in one minute: live_viewer_events' reason."
    ),
    support.SupportKbChunk: Shared(
        "The support agent's knowledge base: one embedded passage of the product's OWN "
        "documentation, extracted from the shipped image at build time. The same corpus for every org by "
        "construction — a tenant contributes nothing to it and a per-org copy would be N copies of one "
        "file — so no org_id. What a TENANT may retrieve is not tenancy but AUDIENCE: `audience` marks the "
        "operator-only passages (docs naming control-plane hosts and live-DB procedures) and "
        "`app/support_kb/search.py` filters on the caller's `Principal.is_operator`, never on a scope. "
        "Written only by the `support-kb-index` and `support-kb-site` system crons, on the operator plane."
    ),
}

# An edition without project memory does not map its tables, so the census omits them.
if edition_features.enabled("project_memory"):
    from app.project_memory import models as project_memory

    TENANCY.update({
        # ── Project memory (revision 0391; `app/project_memory/`) ────────────────────────────────────
        project_memory.ProjectMemory: Stored(
            "A learned unit belongs to its project's org. Stored, not Derived from projects: its sources and votes "
            "derive through it, and a derived parent would put them two hops from orgs. org_id is copied off the "
            "project by the one writer (`app/project_memory/ingest.py`).",
            same_org=(
                _project(
                    "A unit is filed under a project of its own org.",
                    on_delete="CASCADE", null_is_legitimate=False,
                ),
            ),
        ),
        project_memory.MemorySource: Derived(
            "A unit's provenance row belongs to the unit; the contributing task must be in the same org.",
            parent=project_memory.ProjectMemory,
            fk_columns=("unit_id",),
            on_delete="CASCADE",
            same_org=(
                SameOrg(
                    column="task_id", parent=tasks.Task, on_delete="SET NULL", null_is_legitimate=True,
                    reason="Provenance outlives a deleted task: SET NULL keeps the unit's history.",
                ),
            ),
        ),
        project_memory.MemoryVote: Derived(
            "A vote belongs to the unit voted on (unit_id); task_id, the voting task, must be in the same org.",
            parent=project_memory.ProjectMemory,
            fk_columns=("unit_id",),
            on_delete="CASCADE",
            same_org=(
                SameOrg(
                    column="task_id", parent=tasks.Task, on_delete="CASCADE", null_is_legitimate=False,
                    reason="Every vote is cast by a task of the unit's project.",
                ),
            ),
        ),
        project_memory.MemoryEvent: Derived(
            "A delivery/use event belongs to the task it happened in (task_id); unit_id must be in the same org.",
            parent=tasks.Task,
            fk_columns=("task_id",),
            on_delete="CASCADE",
            same_org=(
                SameOrg(
                    column="unit_id", parent=project_memory.ProjectMemory, on_delete="CASCADE",
                    null_is_legitimate=False,
                    reason="An event names a unit of the task's own project.",
                ),
            ),
        ),
        project_memory.MemoryCaptureLog: Derived(
            "A feed's capture record belongs to the task it ran for.",
            parent=tasks.Task,
            fk_columns=("task_id",),
            on_delete="CASCADE",
        ),
        project_memory.MemoryRejection: Derived(
            "A rejection count belongs to its project.",
            parent=projects.Project,
            fk_columns=("project_id",),
            on_delete="CASCADE",
        ),
        project_memory.MemoryClaim: Derived(
            "A per-commit job claim belongs to its project.",
            parent=projects.Project,
            fk_columns=("project_id",),
            on_delete="CASCADE",
        ),
        project_memory.ProjectMemoryState: Derived(
            "A project's memory counters belong to the project.",
            parent=projects.Project,
            fk_columns=("project_id",),
            on_delete="CASCADE",
        ),
        project_memory.ProjectMemoryRepoState: Derived(
            "A connected repository's memory pointers belong to its project. Its `project_repo_id` -> "
            "project_repos is set only by `capture.resolve_repo`'s answer, a repository of that same project.",
            parent=projects.Project,
            fk_columns=("project_id",),
            on_delete="CASCADE",
        ),
        project_memory.ProjectMemoryCanon: Derived(
            "A project's cached canon directives belong to the project.",
            parent=projects.Project,
            fk_columns=("project_id",),
            on_delete="CASCADE",
        ),
    })


TENANT_OWNED: frozenset[type[Base]] = frozenset(
    cls for cls, verdict in TENANCY.items() if isinstance(verdict, (Stored, Derived))
)
