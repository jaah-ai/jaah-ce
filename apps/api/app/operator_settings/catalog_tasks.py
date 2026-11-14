"""The "Tasks", "Projects" and "Support" Settings groups of the Settings catalogue, in render order."""
from __future__ import annotations

from app.operator_settings.knobs import Knob, _Section, _sectioned
from app.persona_registry import ALWAYS_INTERVIEWING_PERSONAS, OPERATOR_PERSONAS

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
        help="New tasks run unattended to delivery, asking nobody.",
        more="Only prefills the new-task form: each task can still be changed before it is created.",
        docs="console-reference/tasks/new-task",
        # Default ON: full autonomy is the mode the fleet is built around.
        kind="bool", env=None, default=True,
    ),
    Knob(
        key="task_default_complex",
        tier="org",
        label="Epic",
        help="New tasks start as epics, analysed and split into subtasks.",
        more="An epic spends analysis, design and planning runs before any code is written: pure cost for a "
             "small fix. Only prefills the new-task form.",
        docs="using-jaah/epics/how-epics-and-features-work",
        # Default OFF: an epic spends analysis and planning runners, pure cost for a single-file fix.
        kind="bool", env=None, default=False,
    ),
    Knob(
        key="task_default_persona",
        tier="org",
        label="Persona",
        help="New tasks are run by this persona unless changed.",
        more="A project's own default persona comes first. Only prefills the new-task form.",
        # `app/persona_registry.py::OPERATOR_PERSONAS`, derived and in order, matching the create form's picker
        # (`GET /api/tasks/personas`) but for the always-interviewing ones. The registry is stdlib-only, so importing it is free; set
        # membership is argued there. `advisor` is a valid prefill: it grants nothing (the create gate still
        # needs `worktrees:edit`), unlike `cron_activity_runner.SCHEDULABLE_PERSONAS`, which drops it. An
        # always-interviewing persona (Business Wizard) is no default: every new task would open as an interview.
        kind="str", env=None, default="coder",
        choices=tuple(p for p in OPERATOR_PERSONAS if p not in ALWAYS_INTERVIEWING_PERSONAS),
        choice_help=(
            "Builds the change and opens a pull request.",
            "Pins down requirements; hands back a document, no code.",
            "Chooses the design; hands back a document, no code.",
            "Answers one question as a reviewed report.",
            "Improves the test suite and opens a pull request.",
            "Prepares a repository new to Jaah to take tasks.",
            "Localizes new and changed text and opens a pull request.",
            "Writes marketing copy in the project's voice, via pull request.",
            "Delivers a quick prototype as an artifact, never a PR.",
            "Talks with you in the console until you complete it.",
        ),
        docs="using-jaah/personas-and-playbooks/choose-a-persona-for-a-task",
    ),
    # Not a prefill, but in this section: it governs a form in front of a person (the task editor), and
    # no server path reads it.
    Knob(
        key="task_autosave_seconds",
        tier="org",
        label="Auto-save",
        # The help names the loss it bounds: a closed tab costs at most one interval of typing.
        help="A closed tab loses at most this much task-editor typing.",
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
        help="Members can pin a task to one node.",
        more="Off: a stored pin is ignored and tasks run on any of the project's nodes, except tasks aimed at a "
             "node itself. Operators always have the picker.",
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
        help="Finished tasks are re-classified into every active tag group.",
        more="One classifier call per tag group per finished task, visible tasks only. Off stops that traffic; "
             "the manual Re-tag button still works.",
        # Default ON: tags are the classifier's job, and Done is when the text is final. "Automation", not
        # "Tagging", beside the synthetic "Tags" card (`apps/web/src/pages/Settings.tsx`).
        kind="bool", env=None, default=True,
    ),
    # ── The two done-hook give-up bounds ──
    # Each bounds how often a permanently-bad row may cost a billable call; none fails the task, hence here
    # and not under "Runners". Read inside the step that charges the counter, so an edit takes effect on the
    # next `done`. Separate knobs (one per-action, one per-workflow) with matching defaults. The retired
    # done-comment pair (`task_comment_external_on_done`, `task_external_comment_max_attempts`) is ignored on
    # load if still stored (`_stored_candidates` reads the store by catalogue key).
    Knob(
        key="task_retag_max_attempts",
        tier="org",
        label="Re-tag attempts",
        # Each attempt is a billable classifier call; a setup refusal spends none, and manual Re-tag is
        # unlimited.
        help="Failed automatic re-tags stop after this many classifier calls.",
        more="Each attempt is a billable classifier call; a setup refusal spends none. The manual Re-tag button "
             "is unlimited. A change applies from the task's next finish.",
        kind="int", env=None, default=3, minimum=1, maximum=20, unit="attempts",
    ),
    Knob(
        key="task_done_workflow_max_attempts",
        tier="org",
        label="Done-action attempts",
        # The outer bound: stops a permanently-bad row starting a fresh done workflow on every edit.
        help="A task's done actions stop rerunning after this many failures.",
        more="Stops a permanently failing task starting its done actions again on every edit.",
        kind="int", env=None, default=3, minimum=1, maximum=20, unit="attempts",
    ),
    # The outcome scan's default cadence (`app/outcome_ledger/settings.py::scan_interval`): how often a due project
    # files a hidden `outcome_scan` task. Operator tier: an org's own Owner/Admin sets `orgs.outcome_scan_interval_
    # minutes` and a project its own override, so a per-org operator override would be a second control. Floor 15
    # (the ledger tick's own period), ceiling a day.
    Knob(
        key="outcome_scan_interval_minutes",
        tier="operator",
        label="Outcome scan interval",
        help="Merged work is re-scanned for reverts and fixes this often.",
        more="The default only: an org and each project can set their own interval. Minimum 15 minutes, the "
             "outcome ledger's own tick.",
        kind="int", env=None, default=60, minimum=15, maximum=1440, unit="minutes",
    ),
    # The compliance seal's two waits (`app/durable/compliance_seal.py`): a session still uploading at the cap is
    # sealed with its transcript recorded as pending, a child still unsealed is listed with its reason.
    Knob(
        key="compliance_seal_transcript_wait_minutes",
        tier="org",
        label="Manifest transcript wait",
        help="Manifests seal after this wait; late transcripts are recorded pending.",
        more="Counted from when the task finishes. A transcript still uploading at the cap shows as Pending at "
             "seal in the manifest.",
        docs="administration/security-and-audit/verify-compliance-manifest",
        kind="int", env=None, default=30, minimum=0, maximum=240, unit="minutes",
        feature="compliance",
    ),
    Knob(
        key="compliance_seal_epic_wait_minutes",
        tier="org",
        label="Manifest epic wait",
        help="Epic manifests seal after this wait, listing unsealed subtasks.",
        more="Counted from when the epic finishes. A subtask still unsealed by then is listed as Not sealed, "
             "with its reason.",
        docs="administration/security-and-audit/verify-compliance-manifest",
        kind="int", env=None, default=60, minimum=0, maximum=480, unit="minutes",
        feature="compliance",
    ),
    # Resolved server-side at dispatch (`app/project_memory/switch.py`), never a form prefill: a project left on
    # "Default" follows this value as it stands when it dispatches. Org tier, so a tenant sees it
    # and the operator may set it per org; the `runner_project_memory` kill switch outranks it.
    Knob(
        key="project_memory_default",
        tier="org",
        label="Project memory",
        help="Projects left on Default draw on what earlier runners learned.",
        more="Applies when the project does not set project memory, read as each task starts. With "
             "project memory off on the organization's plan or by the operator, it stays off regardless.",
        kind="bool", env=None, default=True,
    ),
    _Section("Tasks", "Chats"),
    # ── "Tasks" > "Chats": how many live advisor chats the fleet will carry ──
    # Its own section: two rows on one subject that refuse a create. Both are read on every call by
    # `routes/tasks/policy.py::_advisor_chat_cap_or_409` (the axes are reasoned there), so edits apply without a
    # restart. Defaults: 2 per project; 3 mirrors
    # `routes/pool_slots.py::MAX_USER_HOLDS`. The 50 ceiling is a typo guard. The floor is 1, not 0: a 0
    # would read as unset (`_means_unset`); turning chats off is a persona/permission decision.
    Knob(
        key="advisor_max_chats_per_project",
        tier="org",
        label="Per project",
        # Each open chat holds a worktree nothing reclaims; Completed or Stopped chats stop counting.
        help="Further chats are refused while a project has this many.",
        more="Open chats count; completing or stopping one frees room.",
        docs="using-jaah/chat/how-chat-works",
        kind="int", env=None, default=2, minimum=1, maximum=50, unit="chats",
    ),
    Knob(
        key="advisor_max_chats_per_creator",
        tier="org",
        label="Per person",
        # Unscoped by project, or one person could wedge every project one allowance at a time.
        help="Further chats are refused while one person has this many.",
        more="Counted across every project, so one person can't fill every project's allowance.",
        docs="using-jaah/chat/how-chat-works",
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
        help="New projects run this check as runners work.",
        more="Copied once into each new project; existing projects keep their own. Blank: no command.",
        docs="using-jaah/projects/how-projects-work",
        kind="str", env=None, default="", minimum=0, maximum=1000,
        tenant_editable=True, command_line=True,
    ),
    Knob(
        key="project_default_gate_full_cmd",
        tier="org",
        label="Full gate",
        help="New projects run this check before each pull request.",
        more="Copied once into each new project; existing projects keep their own. Blank: no command.",
        docs="using-jaah/projects/how-projects-work",
        kind="str", env=None, default="", minimum=0, maximum=1000,
        tenant_editable=True, command_line=True,
    ),
    Knob(
        key="project_default_stack_up_cmd",
        tier="org",
        label="Stack up",
        help="New projects start a slot's local stack with this.",
        more="Copied once into each new project; existing projects keep their own. Blank: no command.",
        docs="using-jaah/projects/how-projects-work",
        kind="str", env=None, default="", minimum=0, maximum=1000,
        tenant_editable=True, command_line=True,
    ),
    Knob(
        key="project_default_stack_down_cmd",
        tier="org",
        label="Stack down",
        help="New projects stop a slot's stack with this, keeping volumes.",
        more="Copied once into each new project; existing projects keep their own. Blank: no command.",
        docs="using-jaah/projects/how-projects-work",
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
        help="Embeds the docs the support assistant answers from.",
        more="A Gemini key, or a generic API key on a proxy host. Empty leaves the index empty, so the assistant "
             "only triages and offers a ticket.",
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
    # The four reserved `support_tools_<entity>_write` keys were retired (nothing read them); a stored override
    # under them is ignored on load, like the retired `email_*` keys (`catalog_integrations.py`).
    Knob(
        key="support_tools_enabled",
        tier="org",
        label="Enabled",
        help="Jarvis can look up console records the read switches allow.",
        more="Off turns every read switch below off, whatever each one says.",
        kind="bool", env=None, default=False, tenant_visible=False,
        feature="support_assistant",
    ),
    Knob(
        key="support_tools_tasks_read",
        tier="org",
        label="Tasks read",
        help="Jarvis can list and read tasks the user already sees.",
        kind="bool", env=None, default=False, tenant_visible=False,
        feature="support_assistant",
    ),
    Knob(
        key="support_tools_projects_read",
        tier="org",
        label="Projects read",
        help="Jarvis can list and read projects the user already sees.",
        kind="bool", env=None, default=False, tenant_visible=False,
        feature="support_assistant",
    ),
    Knob(
        key="support_tools_nodes_read",
        tier="org",
        label="Nodes read",
        help="Jarvis can list and read nodes the user already sees.",
        kind="bool", env=None, default=False, tenant_visible=False,
        feature="support_assistant",
    ),
    Knob(
        key="support_tools_credentials_read",
        tier="org",
        label="Credentials read",
        help="Jarvis can list visible credentials' names and types, never secrets.",
        kind="bool", env=None, default=False, tenant_visible=False,
        feature="support_assistant",
    ),
)
