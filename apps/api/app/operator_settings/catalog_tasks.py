"""The "Tasks", "Projects" and "Support" Settings groups of the Settings catalogue, in render order."""
from __future__ import annotations

from app.operator_settings.knobs import Knob, _Section, _sectioned
from app.persona_registry import OPERATOR_PERSONAS

KNOBS: tuple[Knob, ...] = _sectioned(
    _Section("Tasks", "Defaults"),
    # ── "Tasks" > "Defaults": what the /tasks create form prefills ──
    # No server code reads these: they seed a blank create form (column defaults untouched), and clear the
    # admission bar on the web side. Only three fields get defaults: autonomy, epic-or-not, persona; title,
    # body and project are per-task.
    Knob(
        key="task_default_autonomous",
        tier="org",
        label="Full auto",
        # Prefill only.
        help="New tasks start with the runner deciding everything itself and running to delivery.",
        # Default ON: full autonomy is the mode the fleet is built around.
        kind="bool", env=None, default=True,
    ),
    Knob(
        key="task_default_complex",
        tier="org",
        label="Epic",
        help="New tasks start as an epic: analysed, designed and split into subtasks by the pipeline.",
        # Default OFF: an epic spends analysis and planning runners, pure cost for a single-file fix.
        kind="bool", env=None, default=False,
    ),
    Knob(
        key="task_default_persona",
        tier="org",
        label="Persona",
        help="The runner persona a new task starts with.",
        # Exactly `app/persona_registry.py::OPERATOR_PERSONAS`, derived and in order, matching the create
        # form's picker (`GET /api/tasks/personas`). The registry is stdlib-only, so importing it is free; set
        # membership is argued there. `advisor` is a valid prefill: it grants nothing (the create gate still
        # needs `worktrees:edit`), unlike `cron_activity_runner.SCHEDULABLE_PERSONAS`, which drops it.
        kind="str", env=None, default="coder",
        choices=OPERATOR_PERSONAS,
    ),
    # Not a prefill, but in this section: it governs a form in front of a person (the task editor), and
    # no server path reads it.
    Knob(
        key="task_autosave_seconds",
        tier="org",
        label="Auto-save",
        # The help names which editor and where the draft goes: a server save is a different promise.
        help="How often the editor saves a task in progress.",
        # Floor 5s, or it becomes a keystroke stream against the live row. Ceiling 600s, past which it stops
        # insuring against a closed tab. 15s default: the loss is a sentence, the traffic a few writes a minute.
        kind="int", env=None, default=15, minimum=5, maximum=600, unit="seconds",
    ),
    # The task editor's Node field for the org's own members; operators always have it. Read by the
    # pin write gate (`routes/tasks/create.py::_may_pin_node`) and by placement (`placement_grants.effective_pin`):
    # OFF, a stored pin places nothing, a node task's (`app/node_targets.py`) excepted. `tenant_editable`: the
    # org's own admin turns it on from Settings.
    Knob(
        key="task_node_pin_enabled",
        tier="org",
        label="Node picker",
        help="Anyone who edits a task can pick the node it runs on. Off: tasks run on any of the project's nodes.",
        kind="bool", env=None, default=False,
        tenant_editable=True,
    ),
    _Section("Tasks", "Automation"),
    # A behaviour knob, so "Automation": the control plane acts on it with no form in front of it.
    Knob(
        key="task_auto_retag_on_done",
        tier="org",
        label="Re-tag on done",
        # Visible tasks only; one classifier call per task, so OFF stops that traffic. Manual Re-tag unaffected.
        help="A finished task is re-classified into every active tag group, one classifier call each.",
        # Default ON: tags are the classifier's job, and Done is when the text is final. "Automation", not
        # "Tagging", beside the synthetic "Tags" card (`apps/web/src/pages/Settings.tsx`).
        kind="bool", env=None, default=True,
    ),
    # ── The three done-hook give-up bounds ──
    # Each bounds how often a permanently-bad row may cost a billable call or outbound write; none fails the
    # task, hence here and not under "Runners". Read inside the step that charges the counter, so an edit takes
    # effect on the next `done`. Separate knobs (two per-action, one per-workflow) with matching defaults.
    Knob(
        key="task_retag_max_attempts",
        tier="org",
        label="Re-tag attempts",
        # Each attempt is a billable classifier call; a setup refusal spends none, and manual Re-tag is
        # unlimited.
        help="How many times a failed re-tag is retried; the Re-tag button is unlimited.",
        kind="int", env=None, default=3, minimum=1, maximum=20, unit="attempts",
    ),
    Knob(
        key="task_comment_external_on_done",
        tier="org",
        label="Comment on linked issue",
        # Needs a Jira credential on the project and an issue on the task; OFF stops the writes.
        help="A finished task posts one comment, linking back, on the issue it was created from.",
        # Default ON although it writes to a third party: it needs two deliberate acts (a credential and a
        # linked issue) before it can write. The comment is additive and reversible; no status is ever changed.
        kind="bool", env=None, default=True,
    ),
    Knob(
        key="task_external_comment_max_attempts",
        tier="org",
        label="Comment attempts",
        # Each attempt is an outbound write, so an uncommentable issue costs at most this many calls.
        help="How many times a failed issue comment is retried.",
        kind="int", env=None, default=3, minimum=1, maximum=20, unit="attempts",
    ),
    Knob(
        key="task_done_workflow_max_attempts",
        tier="org",
        label="Done-action attempts",
        # The outer bound: stops a permanently-bad row starting a fresh done workflow on every edit.
        help="How many times the whole set of done actions is retried for one task.",
        kind="int", env=None, default=3, minimum=1, maximum=20, unit="attempts",
    ),
    # The compliance seal's two waits (`app/durable/compliance_seal.py`): a session still uploading at the cap is
    # sealed with its transcript recorded as pending, a child still unsealed is listed with its reason.
    Knob(
        key="compliance_seal_transcript_wait_minutes",
        tier="org",
        label="Manifest transcript wait",
        help="How long a finished task's compliance manifest waits for its session transcripts before sealing.",
        kind="int", env=None, default=30, minimum=0, maximum=240, unit="minutes",
        feature="compliance",
    ),
    Knob(
        key="compliance_seal_epic_wait_minutes",
        tier="org",
        label="Manifest epic wait",
        help="How long a finished epic's compliance manifest waits for its child tasks' manifests before sealing.",
        kind="int", env=None, default=60, minimum=0, maximum=480, unit="minutes",
        feature="compliance",
    ),
    # Resolved server-side at dispatch (`app/project_memory/switch.py`), never a create-form prefill: a task or
    # epic left on "Default" follows this value as it stands when it dispatches. Org tier, so a tenant sees it
    # and the operator may set it per org; the `runner_project_memory` kill switch outranks it.
    Knob(
        key="project_memory_default",
        tier="org",
        label="Project memory",
        help="Whether a task with no setting of its own, in an epic with none, uses project memory.",
        kind="bool", env=None, default=True,
    ),
    _Section("Tasks", "Chats"),
    # ── "Tasks" > "Chats": how many live advisor chats the fleet will carry ──
    # Its own section: two rows on one subject that refuse a create. Both are read on every call by
    # `routes/tasks/policy.py::_advisor_chat_cap_or_409` (the axes are reasoned there), so edits apply without a
    # restart. Defaults: 2 stays below the smallest port grid's index ceiling (3); 3 mirrors
    # `routes/pool_slots.py::MAX_USER_HOLDS`. The 50 ceiling is a typo guard. The floor is 1, not 0: a 0
    # would read as unset (`_means_unset`); turning chats off is a persona/permission decision.
    Knob(
        key="advisor_max_chats_per_project",
        tier="org",
        label="Per project",
        # Each open chat holds a worktree nothing reclaims; Completed or Stopped chats stop counting.
        help="Open chats one project may hold at once; each holds a slot.",
        kind="int", env=None, default=2, minimum=1, maximum=50, unit="chats",
    ),
    Knob(
        key="advisor_max_chats_per_creator",
        tier="org",
        label="Per person",
        # Unscoped by project, or one person could wedge every project one allowance at a time.
        help="Open chats one person may hold at once, across every project.",
        kind="int", env=None, default=3, minimum=1, maximum=50, unit="chats",
    ),
    _Section("Projects", "Defaults"),
    # ── "Projects" > "Defaults": the gate and stack commands a new project starts with ──
    # Seed only: read once by the project create path (`routes/projects.py`, via `project_create_defaults`);
    # existing projects are untouched. Blank, the platform default, means no command. `tenant_editable`: an
    # org's own admin sets them for its org from Settings.
    Knob(
        key="project_default_gate_quick_cmd",
        tier="org",
        label="Quick gate",
        help="Command new projects run on every commit. Blank: none.",
        kind="str", env=None, default="", minimum=0, maximum=1000,
        tenant_editable=True, command_line=True,
    ),
    Knob(
        key="project_default_gate_full_cmd",
        tier="org",
        label="Full gate",
        help="Command new projects run before a PR. Blank: none.",
        kind="str", env=None, default="", minimum=0, maximum=1000,
        tenant_editable=True, command_line=True,
    ),
    Knob(
        key="project_default_stack_up_cmd",
        tier="org",
        label="Stack up",
        help="Command new projects run to start a slot's stack. Blank: none.",
        kind="str", env=None, default="", minimum=0, maximum=1000,
        tenant_editable=True, command_line=True,
    ),
    Knob(
        key="project_default_stack_down_cmd",
        tier="org",
        label="Stack down",
        help="Command new projects run to stop a slot's stack, keeping volumes. Blank: none.",
        kind="str", env=None, default="", minimum=0, maximum=1000,
        tenant_editable=True, command_line=True,
    ),
    _Section("Support", "Knowledge base"),
    # ── "Support" > "Knowledge base": the support assistant's documentation index ──
    # One knob, operator-tier: the index is the deployment's, embedded once for every tenant, and its
    # reader (`support_kb.embeddings.embedder_from_settings`, called by the org-less `support-kb-index`
    # cron) would be stopped fleet-wide by `_refuse_unscoped_read` under an org override.
    Knob(
        key="support_kb_credential_id",
        tier="operator",
        label="Embeddings credential",
        help="The Gemini key that embeds the documentation the support assistant answers from. Empty leaves "
             "the assistant triage-only.",
        # A handle (see `cloud_node_tailscale_credential_id`); `env=None`, 0..36 a shape check. Empty is a
        # real setting (the index stays empty, the agent offers a ticket), so no floor. Both types carry an
        # `api_key` field (`credentials/registry.py`): `generic_api_key` admits a key on a proxy host.
        kind="str", env=None, default="", minimum=0, maximum=36,
        credential_type=("gemini_api_key", "generic_api_key"),
        feature="knowledge_base",
    ),
    _Section("Support", "Jarvis tools"),
    # ── "Support" > "Jarvis tools": which console records Jarvis may look up ──
    # Org-tier so an operator can override them per tenant (`routes/orgs.py::put_org_settings`), and
    # `tenant_visible=False`: a tenant neither sees nor saves them. Effective read access for an entity is the
    # master AND that entity's read switch, resolved for the caller's org (`support_agent/tools.py::available`).
    # The write switches are real settings that nothing reads yet: no write tool exists.
    Knob(
        key="support_tools_enabled",
        tier="org",
        label="Enabled",
        help="Lets Jarvis look up console records with the read switches below. Off turns every one of them off.",
        kind="bool", env=None, default=False, tenant_visible=False,
        feature="support_assistant",
    ),
    Knob(
        key="support_tools_tasks_read",
        tier="org",
        label="Tasks read",
        help="Jarvis may list and read the tasks the user can already see.",
        kind="bool", env=None, default=False, tenant_visible=False,
        feature="support_assistant",
    ),
    Knob(
        key="support_tools_tasks_write",
        tier="org",
        label="Tasks write",
        help="Reserved for changes to tasks. Grants nothing yet: Jarvis has no tool that writes.",
        kind="bool", env=None, default=False, tenant_visible=False,
        feature="support_assistant",
    ),
    Knob(
        key="support_tools_projects_read",
        tier="org",
        label="Projects read",
        help="Jarvis may list and read the projects the user can already see.",
        kind="bool", env=None, default=False, tenant_visible=False,
        feature="support_assistant",
    ),
    Knob(
        key="support_tools_projects_write",
        tier="org",
        label="Projects write",
        help="Reserved for changes to projects. Grants nothing yet: Jarvis has no tool that writes.",
        kind="bool", env=None, default=False, tenant_visible=False,
        feature="support_assistant",
    ),
    Knob(
        key="support_tools_nodes_read",
        tier="org",
        label="Nodes read",
        help="Jarvis may list and read the nodes the user can already see.",
        kind="bool", env=None, default=False, tenant_visible=False,
        feature="support_assistant",
    ),
    Knob(
        key="support_tools_nodes_write",
        tier="org",
        label="Nodes write",
        help="Reserved for changes to nodes. Grants nothing yet: Jarvis has no tool that writes.",
        kind="bool", env=None, default=False, tenant_visible=False,
        feature="support_assistant",
    ),
    Knob(
        key="support_tools_credentials_read",
        tier="org",
        label="Credentials read",
        help="Jarvis may list the credentials the user can already see: name and type, never a secret.",
        kind="bool", env=None, default=False, tenant_visible=False,
        feature="support_assistant",
    ),
    Knob(
        key="support_tools_credentials_write",
        tier="org",
        label="Credentials write",
        help="Reserved for changes to credentials. Grants nothing yet: Jarvis has no tool that writes.",
        kind="bool", env=None, default=False, tenant_visible=False,
        feature="support_assistant",
    ),
)
