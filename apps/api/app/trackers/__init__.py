"""Start Jaah tasks from issue trackers (Linear, Jira, Backlog): assign or delegate an issue to Jaah, @mention Jaah in
a comment, or add the start label, and one task is filed in the Jaah project the issue's team/project is primarily
linked to.

The modules:
- `vocabulary` — the words (trackers, auth kinds, event kinds, write-back kinds, settings defaults);
- `protocol` — `IssueEvent` and the `TrackerAdapter` contract, and the `ADAPTERS` registry;
- `connections` — connect, configure and disconnect (an org admin's; `routes/trackers.py`);
- `project_links` — a project's links to its org's connections and the primary rule (`routes/project_trackers.py`);
- `intake` — an inbound event to a trigger delivery, a follow-up comment or a stop;
- `replies` — a comment on an issue with a live task: an answer to its open question, or a follow-up;
- `writeback` — what a task tells its issue (acknowledgement, progress, questions, PR, outcome, done state), swept
  by every `trigger-intake` pass.

Tracker events ride the trigger machinery: each connection owns one trigger (source kind = the tracker) whose parent
automation files the task (`automation_fire`), so every start shows in Automations with its run. Per issue, one live
task: a repeated event dedups on the delivery's key, a mention on an issue with a live task becomes a comment on it,
and unassigning the issue or removing the label stops it.

This package holds no adapter: each tracker's lives in its own module and registers itself (`protocol.register`).
"""
