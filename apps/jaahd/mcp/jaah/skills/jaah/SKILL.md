---
name: jaah
description: Use when the user asks how to do something in Jaah, or about Jaah tasks, epics, chats, schedules, automations, triggers, credentials or secrets, nodes, projects, worktrees or slots, notifications, personas, playbooks or templates — and whenever the agent could do it itself through the jaah control tools (mcp__plugin_jaah_control__*). Maps a goal to the Jaah-native way, links the docs portal, and states the rules for acting on the user's behalf.
---

# Jaah

You run on a Jaah node. When the user wants something Jaah already does, steer them to the Jaah way, and do
it yourself with a control tool when one exists. Never invent a feature: if it is not here or in the docs,
say you are not sure and link the docs.

Docs portal: `https://docs.jaah.ai`. Every link below is a page there.

## What Jaah is

A control plane for AI agents doing work in your repositories. You describe work as a **task**; Jaah hands
it to a **runner** on one of your **nodes**, in a **worktree** of the task's **project**, and the result
comes back to the task: a merged pull request, a document, or an answer. Everything runs under the
permissions of the people in the organization.
Overview: [What is Jaah](https://docs.jaah.ai/get-started/what-is-jaah) ·
[Find your way around](https://docs.jaah.ai/get-started/find-your-way-around).

## Vocabulary

| Term | Means |
|---|---|
| Task | One piece of work: a description, its settings, its runs and result. Belongs to one project. |
| Draft | A saved task nobody started yet. Waits on the board until someone presses Go. |
| Epic | A task too big for one runner. Split into subtasks run in stages: Analyze → Design → Plan → Coder × N → Finish. Never runs itself. |
| Feature | A folder for related tasks inside a project. |
| Persona | The kind of expert a runner acts as: Coder (default), Business analyst, Architect, Researcher, Test optimizer, Repo Wizard, Business Wizard, Translate, Prototyper, Advisor. |
| Playbook | An ordered list of stages, each an expert plus a deliverable. Built-ins: Task, Sketch, Chat, Epic. |
| Involvement | Unattended (decides alone), Guided (asks before key decisions), Chat (advisor conversation). |
| Advisor chat | A task run by the Advisor persona: a conversation about a project, no change. Stays open until **Complete**. |
| Runner | The AI agent working a task, on a node, in a worktree. |
| Node | A machine tasks run on. Online + enabled + assigned to the project, or nothing starts. |
| Worktree / slot | A working copy of one project on one node. A task holds its slot until completed, deleted or reset. |
| Project | Where Jaah works: one or more repositories (or none), plus gate commands, env, memory, access. |
| Automation | **When → Target → Action**: a schedule or event, on projects or nodes, files a task per target. |
| Schedule (cron) | An automation whose When is a cron expression or a fixed interval. |
| Trigger | An automation whose When is an event: webhook, GitHub, Slack, email, new user. |
| Credential / secret | A stored connection to another service (Jira, GitHub, AWS…). Written once, never shown again. |
| NodeVault | Optional seal: only the org's nodes can open a secret's values. |
| Model account | The AI account a runner signs in as. Not a secret. |
| NodeLink | A folder several nodes share, mounted at the same path on each. |
| Template | A task written once and reused from the gallery. |

## Rules of engagement

1. **You act with the user's console authority.** A control tool reaches exactly what the human your task
   acts for may reach in the console. A refusal (`rejected` with 403) means they lack that permission:
   say which, and who could grant it ([How team and access work](https://docs.jaah.ai/using-jaah/team-and-access/how-team-and-access-work)).
2. **Do it, don't hand back a to-do.** If a tool below covers the goal, call it and report the result.
3. **Running work needs the user's yes.** File a task as a **draft** by default. File it running, start a
   board task, or create, resume or run an automation, schedule or trigger only after the user approved
   that exact action — ask with the question tool first.
4. **Loop guard.** A task whose chain was started by a machine (a schedule, trigger, automation or
   another agent) may read, file drafts, pause, and message members, but may not start, stop, edit or
   comment on tasks, write credentials or project settings, or create, change, resume, run or delete
   schedules, triggers or automations. The tool refuses; don't retry. File a draft and tell the user.
5. **Never echo a secret.** Credential tools write values; never print, log, comment, upload or settle
   one. A trigger's signing secret is revealed only in the console: relay the tool's `next_step` verbatim.
6. **No tool? Give the exact console path and the docs link** (sidebar item → tab → button), as named in
   the tables below.

## Goal → Jaah-native way

Tool names below drop the prefix `mcp__plugin_jaah_control__`. Each tool's own description holds its
arguments; read it before the call.

### Tasks and the board

| Goal | Console | Docs | Tool |
|---|---|---|---|
| File a task | Board → **New task…** row, or **+** → describe → Save (draft) or Go | [Start a task](https://docs.jaah.ai/using-jaah/tasks/start-a-task) | `task_create` (draft unless approved), `task_create_batch` for several |
| Write one that comes back right | — | [Write a good task](https://docs.jaah.ai/using-jaah/tasks/write-a-good-task) · [Task sizing](https://docs.jaah.ai/tutorials/task-sizing) | — |
| Change, delete a draft you filed | Task **⋯** menu | [Stop, restart or reset a task](https://docs.jaah.ai/using-jaah/tasks/stop-restart-or-reset-a-task) | `task_update`, `task_delete` |
| Find or read other tasks | Board, search, filters | [Task board](https://docs.jaah.ai/console-reference/tasks/board) | `task_search`, `task_read`, `task_get` (your own row); org-wide: `board_list` |
| Start or stop a board task | Task **⋯** → **Start** / **Stop** | [Stop, restart or reset a task](https://docs.jaah.ai/using-jaah/tasks/stop-restart-or-reset-a-task) | `board_start` (needs approval, loop guard), `board_stop` |
| Edit any task the user may edit | Task page | [Task page](https://docs.jaah.ai/console-reference/tasks/task-detail) | `board_update` |
| Read or post comments, @mention | Task → **Comments** tab | [Comment on a task](https://docs.jaah.ai/using-jaah/tasks/comment-on-a-task) | `board_comments`, `board_comment` |
| Answer a runner's question | Task → **Q&A** tab, or the Slack card | [Answer a question](https://docs.jaah.ai/using-jaah/tasks/answer-a-question) | — |
| Read a task's result | Task → **Details** → **Outcome**, **Artifacts** | [Read a task's result](https://docs.jaah.ai/using-jaah/tasks/read-a-tasks-result) | `artifact_read`; deliver your own: `artifact_upload` |
| Group tasks | Sidebar → project → feature | [Organize tasks into features](https://docs.jaah.ai/using-jaah/tasks/organize-tasks-into-features) | — |
| Make one task wait for another | New task → **Advanced** → **Depends on** | [How tasks work](https://docs.jaah.ai/using-jaah/tasks/how-tasks-work) | — |

### Epics, chats, personas, templates

| Goal | Console | Docs | Tool |
|---|---|---|---|
| Big or unclear work | New task → size **Epic** → Save → **Start** | [How epics and features work](https://docs.jaah.ai/using-jaah/epics/how-epics-and-features-work) · [Start an epic](https://docs.jaah.ai/using-jaah/epics/start-an-epic) | `task_create` with persona `epic` (draft) |
| Steer an epic's subtasks | Epic → **Subtasks** tab | [Steer an epic's subtasks](https://docs.jaah.ai/using-jaah/epics/steer-an-epics-subtasks) | `board_update`, `board_comment` |
| Talk a question through, no change | **▼** beside **+** → **Chat**, or type `Chat:` | [Chat with the advisor](https://docs.jaah.ai/using-jaah/chat/chat-with-the-advisor) · [Finish a chat](https://docs.jaah.ai/using-jaah/chat/finish-a-chat) | — |
| Pick the right expert | New task → **Advanced** → **Persona** | [Choose a persona](https://docs.jaah.ai/using-jaah/personas-and-playbooks/choose-a-persona-for-a-task) · [Personas and models](https://docs.jaah.ai/tutorials/personas-and-models) | `task_create` `persona` |
| A repeatable multi-stage chain | **Automation** → **Playbooks** (Owner/Admin) | [Create a playbook](https://docs.jaah.ai/using-jaah/personas-and-playbooks/create-a-playbook) · [Run a playbook](https://docs.jaah.ai/using-jaah/personas-and-playbooks/run-a-playbook-on-a-task) | — |
| Reuse a well-written task | Sidebar **Templates** → **Start** / **Customize** | [Start a task from a template](https://docs.jaah.ai/using-jaah/templates/start-a-task-from-a-template) | — |

### Automations, schedules, triggers

Automations live under **Automation** → **Automations**, shown to Org Owners and Admins only.
A schedule and a trigger are each one kind of automation (its When).

| Goal | Console | Docs | Tool |
|---|---|---|---|
| Recurring work ("every weekday at 9") | **Automation** → **＋** → When **Schedule** | [Create an automation](https://docs.jaah.ai/using-jaah/automations/create-an-automation) · [Scheduled tasks](https://docs.jaah.ai/tutorials/scheduled-tasks) | `automation_create`; in this project only: `schedule_create` |
| React to an event (webhook, GitHub, Slack, email, new user) | **Automation** → **＋** → When **Webhook** / **GitHub** / **Slack** / **New user** | [How automations work](https://docs.jaah.ai/using-jaah/automations/how-automations-work) | `automation_create`; in this project only: `trigger_create` |
| Run on many projects or every node | Automation → **Target**: All / Specific / All except | [Choose what an automation runs on](https://docs.jaah.ai/using-jaah/automations/choose-what-an-automation-runs-on) | `automation_create` / `automation_update` |
| See what exists, what failed | **Automations** list → automation → **Runs** | [Run, pause or change an automation](https://docs.jaah.ai/using-jaah/automations/run-pause-or-change-an-automation) | `automation_list`, `automation_get`; actions and options: `automation_catalogue` |
| Change, pause, resume, run now, delete | Automation page → **Pause** / **Resume** / **Run now** / **⋯** | [Run, pause or change an automation](https://docs.jaah.ai/using-jaah/automations/run-pause-or-change-an-automation) | `automation_update`, `automation_pause`, `automation_resume`, `automation_run`, `automation_delete`; your own: `schedule_update/delete`, `trigger_update/delete` |

Create, resume and run need the user's approval and are refused under the loop guard. A schedule skips a
missed run and a target whose previous task is still open.

### Credentials and secrets

| Goal | Console | Docs | Tool |
|---|---|---|---|
| See which secrets exist or your task may use | **Directory** → **Secrets** | [How secrets work](https://docs.jaah.ai/using-jaah/credentials/how-credentials-work) · [Secrets](https://docs.jaah.ai/console-reference/directory/credentials) | `credential_list`, `credential_get` (the value reaches you; never echo it) |
| Store a new token or key | **Directory** → **Secrets** → **Add Secret** | [Add a secret](https://docs.jaah.ai/using-jaah/credentials/add-a-credential) | `credential_types` (fields per type), then `credential_create` |
| Rotate or remove one | Open it → type the new value / delete | [Replace or delete a secret](https://docs.jaah.ai/using-jaah/credentials/replace-or-delete-a-credential) | `credential_update`, `credential_delete` |
| Give every task on a project a secret | **Directory** → **Secrets** → secret → **Access** → **Project** | [Attach a secret to a project](https://docs.jaah.ai/using-jaah/credentials/attach-a-credential-to-a-project) | — |
| Seal secrets so only your nodes open them | **Settings** → **NodeVault** (Owner/Admin) | [How NodeVault works](https://docs.jaah.ai/using-jaah/credentials/how-nodevault-works) · [Set up NodeVault](https://docs.jaah.ai/using-jaah/credentials/set-up-nodevault) | — |
| The AI account a runner signs in as | **Directory** → **Agents** | [How model accounts work](https://docs.jaah.ai/using-jaah/model-accounts/how-model-accounts-work) · [Add a model account](https://docs.jaah.ai/using-jaah/model-accounts/add-a-model-account) | — |

Never put a secret in a task description, project commands or agent instructions: anyone who can view
them reads it.

### Projects, worktrees, nodes

| Goal | Console | Docs | Tool |
|---|---|---|---|
| Connect a repository | **Directory** → **Projects** → **Add project** | [Add a project](https://docs.jaah.ai/using-jaah/projects/add-a-project) · [Adding a project](https://docs.jaah.ai/tutorials/adding-a-project) | — |
| List the org's projects | **Directory** → **Projects** | [How projects work](https://docs.jaah.ai/using-jaah/projects/how-projects-work) | `org_projects` |
| Read or change this project's settings (gate commands, agent instructions, branch…) | Project → **General** / **Governance** / **Advanced** | [Projects](https://docs.jaah.ai/console-reference/directory/projects) | `project_get`, `project_update` |
| Give tasks an environment variable | Project → **Environment** | [Add an environment variable](https://docs.jaah.ai/using-jaah/projects/add-an-environment-variable) | `project_env_set`, `project_env_delete` |
| Give each slot its own host port (`API_PORT`, …) | Project → **Environment** → type **Port** (auto, or a preferred port a slot uses when free) | [Add an environment variable](https://docs.jaah.ai/using-jaah/projects/add-an-environment-variable) | `project_env_set` with `source: port` (optional `port_preferred`) |
| Let someone use a project | Project → **Access** | [Give someone access to a project](https://docs.jaah.ai/using-jaah/projects/give-someone-access-to-a-project) | — |
| Run more tasks at once | Project → **Slots** (Org Admin) | [Give a project more slots](https://docs.jaah.ai/using-jaah/worktrees/give-a-project-more-slots) · [How worktrees work](https://docs.jaah.ai/using-jaah/worktrees/how-worktrees-work) | `org_worktrees` (who holds what) |
| Add a machine | **Directory** → **Machines** → **Add node** | [Add a node](https://docs.jaah.ai/using-jaah/nodes/add-a-node) · [Machines and capacity](https://docs.jaah.ai/tutorials/machines-and-capacity) | — |
| Check nodes | **Directory** → **Machines** → node → **Health** | [Read a node's status](https://docs.jaah.ai/using-jaah/nodes/read-a-nodes-status) | `org_nodes` |
| Take a node out of service | Node → **Drain** / **Disable** | [Drain or disable a node](https://docs.jaah.ai/using-jaah/nodes/drain-or-disable-a-node) | — |
| Share files between nodes | **Directory** → **Networks** → NodeLink | [How NodeLink works](https://docs.jaah.ai/using-jaah/nodelink/how-nodelink-works) | — |
| Watch production after merge | Project → **Production QA** | [How Production QA works](https://docs.jaah.ai/using-jaah/production-qa/how-production-qa-works) | — |

### People, notifications, oversight

| Goal | Console | Docs | Tool |
|---|---|---|---|
| Who is in the org | **Directory** → **People** | [Invite a person and grant access](https://docs.jaah.ai/using-jaah/team-and-access/invite-a-person-and-grant-access) | `org_members` |
| Tell someone, or a Slack channel | Comment with `@name` | [How notifications work](https://docs.jaah.ai/using-jaah/notifications/how-notifications-work) | `org_notify`; mail: `email_send` |
| Get alerts in Slack | **Settings** → **Organizations** → **Slack** | [Connect Slack](https://docs.jaah.ai/using-jaah/team-and-access/connect-slack) · [Staying in the loop](https://docs.jaah.ai/tutorials/staying-in-the-loop) | — |
| What needs attention today | **Home** (Owner/Admin) | [How the command center works](https://docs.jaah.ai/using-jaah/command-center/how-the-command-center-works) | — |
| Who did what; a run's transcript | **Audit** → **Activity** / **Sessions** | [How audit works](https://docs.jaah.ai/using-jaah/audit/how-audit-works) | — |
| Score repos against engineering rules | **Compliance** | [How compliance works](https://docs.jaah.ai/using-jaah/compliance/how-compliance-works) | — |
| Plan, limits, spend | **Billing** (Owner/Admin) | [How plans and billing work](https://docs.jaah.ai/using-jaah/billing-and-plan/how-plans-and-billing-work) | — |
| Bring your own agent | **Directory** → **Agents** → **Custom agents** | [How agents work](https://docs.jaah.ai/using-jaah/agents/how-agents-work) | — |
| Call Jaah from code | API key → REST API | [Jaah API](https://docs.jaah.ai/jaah-api) · [Event stream](https://docs.jaah.ai/real-time/event-stream) | — |

## Facts that save a wrong answer

- A task waits **Queued**, not refused, when every slot or agent is busy. More throughput = more slots or
  nodes, not retries.
- Stopping a task keeps its slot and work; **Reset** starts it over from a clean draft.
- A secret is never shown again after saving; a model account is not a secret.
- A feature the plan leaves out is absent from the console, not broken
  ([Check your plan and limits](https://docs.jaah.ai/using-jaah/billing-and-plan/check-your-plan-and-limits)).
- A missing sidebar item or tab means the user lacks the role
  ([Check what someone can access](https://docs.jaah.ai/using-jaah/team-and-access/check-what-someone-can-access)).
- Common questions: [FAQ](https://docs.jaah.ai/faq).

## Read more

- `https://docs.jaah.ai/llms.txt` — every page, one line each, linking a plain-Markdown twin of the page.
- `https://docs.jaah.ai/llms-full.txt` — the whole portal in one file.
- Fetch those before answering anything this file does not cover.
