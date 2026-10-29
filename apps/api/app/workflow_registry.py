"""The curated catalogue of durable workflows — the one place a workflow's operator-facing identity lives.

Hand-maintained, not derived from DBOS rows: a workflow that never ran, or whose runs were pruned, must
still appear (absence is not a state). `tests/test_workflow_registry.py` AST-walks `app/durable/` and
fails unless this set plus `CRON_TICK_WORKFLOW` is exactly what it finds.

`cron_tick` is excluded: `cron_runs` and the per-cron Runs tab are a richer ledger, and the ten-second
dispatch ticks would bury everything else. `queue` is a string rather than imported from the queue
modules, because importing them would register their workflows with DBOS as a side effect.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from app import edition_features

# The one workflow deliberately not listed; one literal shared with the registry guard.
CRON_TICK_WORKFLOW = "cron_tick"


@dataclass(frozen=True)
class WorkflowDescriptor:
    """One durable workflow as an operator meets it.

    `name` is the DBOS function name `list_workflows` filters on; the other fields are what DBOS
    does not know.
    """

    name: str
    label: str
    #: What this workflow is responsible for and what a failure means, in operator-actionable terms.
    description: str
    #: What makes a run start.
    trigger: str
    #: The DBOS queue it is enqueued on, or None when started directly. Only a queued workflow can sit
    #: ENQUEUED behind a concurrency or rate limit.
    queue: Optional[str]


REGISTRY: tuple[WorkflowDescriptor, ...] = (
    WorkflowDescriptor(
        name="deliver_key",
        label="NodeVault key delivery",
        description=(
            "Puts one NodeVault key on one node: it uses the rotation's chain blob or asks an online node "
            "that holds the key to wrap it for this one, then installs it, waiting while the node is offline "
            "or no holder is online. The key counts as delivered only when the node reports holding it."
        ),
        trigger=(
            "Started for each node that lacks a live NodeVault key: when a node registers its identity or "
            "connects, on a credential fetch the node could not open, and on a delivery Retry."
        ),
        queue=None,
    ),
    WorkflowDescriptor(
        name="retire_node_key",
        label="NodeVault key removal",
        description=(
            "Takes one NodeVault key off one node, waiting while the node is offline; a deleted node counts "
            "as done. Once no node still holds an abandoned key, its records are deleted."
        ),
        trigger="Started for each node of a key being retired or abandoned, and on a removal Retry.",
        queue=None,
    ),
    WorkflowDescriptor(
        name="setup_operation",
        label="NodeVault setup",
        description=(
            "Waits until a node reports holding the new NodeVault key, then makes it the current key, waits for "
            "the admin to choose which credentials to move, and moves them: each is re-sealed under the key, a "
            "node checks it opens, and only then is the old copy replaced."
        ),
        trigger="Started when an org admin starts NodeVault setup.",
        queue=None,
    ),
    WorkflowDescriptor(
        name="migrate_operation",
        label="NodeVault migration",
        description=(
            "Moves the chosen credentials to the current NodeVault key, one at a time: re-sealed under the "
            "key, checked by a node that holds it, then swapped. A credential that fails never stops the others."
        ),
        trigger="Started when an org admin moves credentials to NodeVault after setup.",
        queue=None,
    ),
    WorkflowDescriptor(
        name="rotate_operation",
        label="NodeVault rotation",
        description=(
            "Delivers the new NodeVault key to every node (wrapped under the current key), makes it current once "
            "every online node holds it, moves any credentials the admin chose onto it, then has a node rewrap "
            "every credential still on the old key; the old key stays on the nodes until it is retired."
        ),
        trigger="Started when an org admin starts a NodeVault rotation.",
        queue=None,
    ),
    WorkflowDescriptor(
        name="retire_operation",
        label="NodeVault key retirement",
        description=(
            "Removes an old NodeVault key that no credential uses from every node, waiting for offline nodes to "
            "come back, and marks it retired once every node confirmed."
        ),
        trigger="Started when an org admin retires an old NodeVault key.",
        queue=None,
    ),
    WorkflowDescriptor(
        name="migrate_item_retry",
        label="NodeVault migration retry",
        description="Moves one credential that failed to move to NodeVault, the same way a migration does.",
        trigger="Started by the Retry on a failed credential in a NodeVault result.",
        queue=None,
    ),
    WorkflowDescriptor(
        name="authenticate_account",
        label="Account login",
        description=(
            "Drives one agent account through a login or re-authentication on a node: starts the CLI's "
            "login, relays the sealed authorize link and waits for the operator's sealed code (or, for a "
            "device-authorization provider, for credentials to land), then finalizes and writes the "
            "roster row. A failure leaves the account unusable for dispatch until it is retried."
        ),
        trigger=(
            "Started when an operator adds or re-authenticates an account in the Accounts wizard "
            "(POST /api/commands)."
        ),
        queue=None,
    ),
    WorkflowDescriptor(
        name="retire_account",
        label="Account retirement",
        description=(
            "Removes one account's credentials from every node that holds them. It is awaited by the "
            "request that started it, and the roster row is deleted by that caller only after this "
            "reports success — so a partial failure keeps the account visible rather than orphaning "
            "credentials on a node."
        ),
        trigger="Started and awaited when an account is retired (DELETE /api/accounts/{slug}).",
        queue=None,
    ),
    WorkflowDescriptor(
        name="deliver_task_notification",
        label="Task notification delivery",
        description=(
            "Delivers one alert for one committed task transition — it re-reads the task, its project "
            "and its recipient from the database, so a run recovered after a deploy renders the task as "
            "it is now. Losing one costs the operator that notification; the needs-attention sweep is "
            "the backstop that re-announces what this never delivered."
        ),
        trigger=(
            "Enqueued by the cockpit-events listener when a task transition lands in a new alert dedup "
            "window."
        ),
        queue="task-notify",
    ),
    WorkflowDescriptor(
        name="deliver_support_notification",
        label="Support notification delivery",
        description=(
            "Sends one person the Slack DM for one support ticket event — a new ticket, a reply, or a "
            "linked task done — through their own notification preferences. The bell does not depend on "
            "it: its items derive from the ticket rows, so losing one costs only the Slack message."
        ),
        trigger=(
            "Enqueued once per recipient after a ticket write commits (`support_activity.record_event`), "
            "and by the task-done workflow for a linked task."
        ),
        queue="support-notify",
    ),
    WorkflowDescriptor(
        name="deliver_slack",
        label="Slack notification send",
        description=(
            "Posts one notification to Slack — a person's DM or the operator channel — for a sender that ran "
            "outside a workflow (a mention, a reaction, an alarm, a node's alert). The bot token is read here, "
            "never carried; a refusal is retried twice, then logged and the alert's dedup row withdrawn."
        ),
        trigger="Enqueued by `notify_outbox.slack` once the sender has resolved the recipient.",
        queue="notify-slack",
    ),
    WorkflowDescriptor(
        name="deliver_system_email",
        label="System email send",
        description=(
            "Sends one already-rendered system email through SES — a notification, an onboarding email or an "
            "invitation. The body is carried sealed (it can hold a reset link) and the outcome is written to the "
            "delivery log; an unreachable or throttled SES is retried twice, a refusal is final."
        ),
        trigger="Enqueued by `notify_outbox.email` after the sender rendered it.",
        queue="notify-system-email",
    ),
    WorkflowDescriptor(
        name="deliver_telegram",
        label="Telegram notification send",
        description=(
            "Sends one plain-text message to a person's linked Telegram chat as the deployment's bot, whose "
            "token is read here. A chat that blocked the bot is unlinked."
        ),
        trigger="Enqueued by `notify_outbox.telegram` once the sender resolved the chat and the text.",
        queue="notify-telegram",
    ),
    WorkflowDescriptor(
        name="deliver_forward",
        label="Lead and access-request forward",
        description=(
            "Runs one named send that records its own outcome on its row: a site enquiry's, sign-up's or trust "
            "request's Slack and HubSpot forward, or an access request's approver cards."
        ),
        trigger="Enqueued by `notify_outbox.forward` when the lead or request is stored.",
        queue="notify-forward",
    ),
    WorkflowDescriptor(
        name="deliver_after_answer",
        label="Sign-up and password-reset follow-up",
        description=(
            "Runs the work a public door owes after it answers: the sign-up and password-reset emails, the "
            "operators' new sign-up notices and the auto-approval; and an org decision's: the subscription an "
            "approval starts on the card saved at sign-up (a failure is shown on the org's sign-up as its billing "
            "error), and a rejection's email and card detach. Its arguments are carried sealed (an address, a "
            "sign-up code, an IP); each email it sends is a retried step of its own."
        ),
        trigger=(
            "Enqueued by `notify_outbox.after_answer` once the sign-up or reset request has been answered, or an "
            "org's approval or rejection has committed."
        ),
        queue="notify-email",
    ),
    WorkflowDescriptor(
        name="run_token_billing_effect",
        label="Token billing effect",
        description=(
            "Runs one outside effect of a Jaah AI billing write: the Bedrock deny-policy rebuild that stops a "
            "revoked org, an auto-recharge charge, or a balance notice. A failure is retried by a delayed "
            "successor and alarms the operator on the third (deny policy) or fifth failure."
        ),
        trigger="Enqueued inside the billing write's own transaction, so it exists only if that write commits.",
        queue="token-billing",
    ),
    WorkflowDescriptor(
        name="compliance_seal",
        label="Compliance manifest seal",
        description=(
            "Seals one version of a finished task's signed compliance manifest: waits for its session transcripts "
            "(and an epic's child manifests) up to the org's caps, then builds, signs and stores it. Every run "
            "ends in a sealed or a failed version on the task, the failure naming why."
        ),
        trigger=(
            "Enqueued by the task-done workflow, by the compliance-seal sweep for a done task with no manifest, "
            "and by Regenerate on the task page."
        ),
        queue="compliance-seal",
    ),
    WorkflowDescriptor(
        name="compliance_erase",
        label="Compliance manifest erase",
        description=(
            "Deletes every stored version of a deleted task's (or org's) compliance manifests through the "
            "bucket's retention. A failure leaves the objects in the write-once bucket until it is retried."
        ),
        trigger="Enqueued inside the task or org delete's own transaction, so it exists only if the delete commits.",
        queue="compliance-erase",
    ),
    WorkflowDescriptor(
        name="evaluate_project",
        label="Live Cast evaluation",
        description=(
            "Judges a published Live Cast project's tasks and text files against its rules, one item at a time, "
            "and records each verdict and the rules it was judged under. An item that fails stays hidden and is "
            "retried later."
        ),
        trigger=(
            "Enqueued when a project is published, and by the Live Cast sweep or a task change in a published "
            "project when an item's rules or content changed. One runs per project at a time."
        ),
        queue="live-cast",
    ),
    WorkflowDescriptor(
        name="push_org_plan",
        label="Org plan push",
        description=(
            "Puts an org on the plan an operator chose for it in Stripe. Retried through a Stripe outage; a "
            "refusal is final and lands in the org's history. Declines once the operator has moved on or "
            "Stripe already agrees."
        ),
        trigger="Enqueued after an operator changes an org's plan on its Plan tab.",
        queue="org-plan-push",
    ),
    WorkflowDescriptor(
        name="retag_org",
        label="Org tailnet retag",
        description=(
            "Moves a renamed org's tailnet devices from its old tag to its current one. Retried for about "
            "seventeen hours, since headscale refuses a tag its policy does not yet list."
        ),
        trigger="Enqueued after an operator renames a tenant org's slug, when it has tailnet devices.",
        queue="org-retag",
    ),
    WorkflowDescriptor(
        name="converge_org",
        label="Org tailnet converge",
        description=(
            "Moves a tenant org's existing tailnet devices onto its own tags, in place of the fleet tag or a "
            "stale org tag, keeping every other tag. Retried for about seventeen hours, since headscale refuses "
            "a tag its policy does not yet list."
        ),
        trigger=("Enqueued when an operator asks to retag a tenant org's tailnet devices, and when an operator "
                 "moves a node into the org."),
        queue="org-retag",
    ),
    WorkflowDescriptor(
        name="project_cron_schedules",
        label="Cron schedule sync",
        description=(
            "Brings the engine's schedules into line with the Crons page: adds or updates each cron's "
            "schedule, pauses disabled ones, resumes re-enabled ones and drops schedules whose cron is gone. "
            "A failure raises one alarm and leaves the schedules as they were until the next sync."
        ),
        trigger="Enqueued when a cron or automation schedule is saved or deleted, the cron timezone changes, "
                "and once when each API process starts.",
        queue="cron-projection",
    ),
    WorkflowDescriptor(
        name="generate_task_title",
        label="Task title generation",
        description=(
            "Derives a readable title for a newly created task from its description. Best-effort by "
            "construction: a slow or broken model costs the generated title and nothing else, and the "
            "task keeps the placeholder it was created under."
        ),
        trigger="Enqueued after a task is created, by an operator, a cron or a runner that filed it, and "
                "after a runner rewrites a filed draft's title.",
        queue="task-title",
    ),
    WorkflowDescriptor(
        name="reword_question_round",
        label="Question rewording",
        description=(
            "Rewords a runner's question round in plain terms (a headline, one line of why, short option "
            "labels) for the task page. Display only and best-effort: a failure leaves the runner's own "
            "wording on screen, and answers always post the original labels."
        ),
        trigger="Enqueued after a runner asks a question round, from either question route.",
        queue="task-title",
    ),
    WorkflowDescriptor(
        name="sweep_filed_titles",
        label="Filed task title sweep",
        description=(
            "Finds runner-filed drafts still wearing their filer's long title and queues a title generation "
            "for each. A failure leaves those titles as they are until the next start."
        ),
        trigger="Once when each API process starts.",
        queue="task-title",
    ),
    WorkflowDescriptor(
        name="task_done",
        label="Task completion actions",
        description=(
            "Runs every action a finished task is owed and records whether they converged. Each action "
            "is wrapped independently, so one permanent failure never blocks the others — the run's "
            "output is the per-action status map, which is the single most useful thing on this page "
            "when a shipment did not fully land."
        ),
        trigger=(
            "Enqueued inside the transaction that moves a Task to `done`, so it exists exactly "
            "when that `done` commits. No cron or sweep starts it."
        ),
        queue="task-done",
    ),
    WorkflowDescriptor(
        name="refresh_git_token",
        label="GitHub token refresh",
        description=(
            "Renews one expiring Connect user token (GitHub, GitLab, Azure DevOps). A refresh token is "
            "SINGLE-USE where the forge rotates it — the old one is retired as the new is issued — so the "
            "rotation is checkpointed (as ciphertext) and the write "
            "that records it is a separate step. A failure here means the credential could not be "
            "renewed and its owner must reconnect GitHub from Profile > Git."
        ),
        trigger=(
            "Started, and awaited, whenever a piece of work needs a token whose grant is close enough "
            "to expiry to renew (app/git_access.py::token_for)."
        ),
        queue=None,
    ),
    WorkflowDescriptor(
        name="provision_cloud_node",
        label="Cloud node provisioning",
        description=(
            "Mints one node's tailnet key, asks AWS to start its instance, and records the instance on "
            "the node row. The launch is checkpointed, so a control plane that dies mid-provision "
            "resumes at the write instead of starting a second machine. A permanent failure unwinds "
            "the node row it was provisioning rather than leaving a node that never booted."
        ),
        trigger=(
            "Enqueued when an operator creates a cloud node, and by the node-groups cron when a "
            "dynamic group is below its desired capacity."
        ),
        queue="cloud-node",
    ),
    WorkflowDescriptor(
        name="mint_bootstrap_key",
        label="Bootstrap tailnet key",
        description=(
            "Mints the single-use Tailscale key a self-hosted node's bootstrap script carries. The key "
            "is spent the moment it exists and lives about fifteen minutes, so the mint is "
            "checkpointed per node and per key lifetime — a re-fetch inside that window replays the "
            "same key rather than burning a second one."
        ),
        trigger="Started when a node fetches its bootstrap script inside its enrolment window.",
        queue=None,
    ),
    WorkflowDescriptor(
        name="converge_shared_account",
        label="Shared account convergence",
        description=(
            "Converges one shared account's per-node layer on ONE node — the managed settings, their "
            "consent record, OMC, the HUD wrapper and the MCP config. One run per account per node, so "
            "a node that is down is retried on its own rather than dropping the whole fan-out; until "
            "it converges, a runner placed on that node cannot claim work."
        ),
        trigger=(
            "Enqueued once per share-enabled node when a shared account finishes logging in. The node "
            "that ran the login is skipped — its own wizard tail already converged it."
        ),
        queue="account-converge",
    ),
    WorkflowDescriptor(
        name="erase_account_dirs",
        label="Moved node cleanup",
        description=(
            "Erases the old org's runner-account credential directories from a node an operator moved to "
            "another org, purges what that org left on it (task files, test runs, pulled images, build and "
            "package caches), re-converges its shared-account mount, then lets it take tasks again. One run per "
            "moved node; an offline node is asked again, backing off to every ten minutes, for about a week."
        ),
        trigger="Enqueued once per moved node when an operator's Change org of a node or host commits.",
        queue="org-transfer-node",
    ),
    WorkflowDescriptor(
        name="change_machine",
        label="Hosting product change",
        description=(
            "Carries a paid Dedicated hosting product change to its machine: grows the disk and waits for the "
            "host's report, changes the instance type in place (stop, modify, start), or launches a new machine "
            "and moves the hosting's nodes onto it. A machine step that fails reverses the billing; a disk the "
            "host has not reported grown is shown stuck and waited for, for up to a week."
        ),
        trigger="Enqueued when a hosting product change is paid, and again by the hosting lifecycle pass.",
        queue="hosting-change",
    ),
    WorkflowDescriptor(
        name="echo_answer_to_slack",
        label="Slack card settle",
        description=(
            "Mirrors a resolution made in the console back onto the Slack card that asked — reads the "
            "card back, rewrites it, and posts the note. One step per Slack call, so a partial echo "
            "resumes where it stopped. When this never lands, the card keeps live answer buttons on a "
            "question that is already settled."
        ),
        trigger="Enqueued when a question round is answered, dismissed or closed in the console.",
        queue="slack-echo",
    ),
    WorkflowDescriptor(
        name="finish_transcript_upload",
        label="Transcript upload completion",
        description=(
            "Writes one session transcript's manifest to S3 and marks the row uploaded. The manifest "
            "write is checkpointed because the two halves used to be able to disagree: the bytes and "
            "the manifest could be complete in the bucket while the lease reclaim recorded the "
            "transcript as failed, telling an operator it was unavailable when it was not."
        ),
        trigger="Started by the node's own report that it has finished uploading a transcript.",
        queue=None,
    ),
    WorkflowDescriptor(
        name="converge_gha_pool",
        label="GitHub runner pool",
        description=(
            "Proves a runner pool's credential can administer the repo's runners, and pokes the node "
            "to apply the pool. The proof spends a one-time GitHub registration token, which is the "
            "part worth checkpointing; the poke rides along and is genuinely backstopped by the node's "
            "own convergence pass, so a lost one costs latency rather than the pool."
        ),
        trigger=(
            "The proof runs before a pool is saved; the poke is enqueued after it commits, and again "
            "when a pool is paused or removed."
        ),
        queue="gha-pool",
    ),
    WorkflowDescriptor(
        name="change_cloud_node_type",
        label="Cloud node type change",
        description=(
            "Changes a Your Cloud node's machine type: stop, set the type, start. Each step reads the "
            "machine before it writes, so a resumed run sends no second stop, set or start. When this "
            "never lands, the node stays stopped and out of dispatch."
        ),
        trigger="Enqueued when an operator changes a Your Cloud node's machine type in the console.",
        queue="cloud-change",
    ),
    WorkflowDescriptor(
        name="wait_cloud_node_power",
        label="Cloud node stop wait",
        description=(
            "Polls a Your Cloud node the cloud is still stopping until it reads stopped, then records it "
            "stopped — or starts it, when a Start came meanwhile. When this never lands, the node reads "
            "stopping until Start or Stop is pressed again."
        ),
        trigger="Enqueued when an operator stops a Your Cloud node and the cloud has not finished stopping it.",
        queue="cloud-change",
    ),
)

#: The names, in registry order, for `list_workflows`. The explicit list is also what keeps cron ticks
#: out, since DBOS has no exclusion filter.
NAMES: tuple[str, ...] = tuple(descriptor.name for descriptor in REGISTRY)

#: The workflows only a cloud surface starts, each by the feature that starts it (`packages/editions/features.toml`):
#: an edition without that feature omits it from its list (`listed`) and looks it up as unknown (`get`). An edition
#: without the feature never registers the workflow either: its module does not ship there.
FEATURE_OF: dict[str, str] = {
    "compliance_seal": "compliance",
    "compliance_erase": "compliance",
    "deliver_support_notification": "support_assistant",
    "evaluate_project": "live_cast",
    "deliver_slack": "slack",
    "echo_answer_to_slack": "slack",
    "deliver_system_email": "system_mail",
    "deliver_telegram": "telegram",
    "deliver_forward": "signup",
    "deliver_after_answer": "signup",
    "run_token_billing_effect": "jaah_ai_credit",
    "push_org_plan": "billing",
    "retag_org": "tailnet",
    "converge_org": "tailnet",
    "mint_bootstrap_key": "tailnet",
    "provision_cloud_node": "cloud_nodes",
    "change_cloud_node_type": "cloud_nodes",
    "wait_cloud_node_power": "cloud_nodes",
    "converge_shared_account": "nodelink",
    "change_machine": "hostings",
    "converge_gha_pool": "gha_runners",
}
CLOUD_ONLY_NAMES: frozenset[str] = frozenset(FEATURE_OF)

_BY_NAME: dict[str, WorkflowDescriptor] = {d.name: d for d in REGISTRY}


def _in_edition(name: str) -> bool:
    feature = FEATURE_OF.get(name)
    return feature is None or edition_features.enabled(feature)


def get(name: str) -> Optional[WorkflowDescriptor]:
    """The descriptor for `name`, or None — a cloud-only one too in the community edition, so every route that looks a
    name up refuses it as unknown; callers phrase their own 422/404 for an unknown name."""
    return _BY_NAME.get(name) if _in_edition(name) else None


def listed() -> tuple[WorkflowDescriptor, ...]:
    """The registry as the Workflows list shows it: without a workflow whose feature this edition lacks."""
    return tuple(d for d in REGISTRY if _in_edition(d.name))


def listed_names() -> tuple[str, ...]:
    """`listed()`'s names: what the runs page and a run lookup may read."""
    return tuple(d.name for d in listed())
