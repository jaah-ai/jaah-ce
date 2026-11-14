"""Production QA — the single home of this epic's rules: deploys and incidents are recorded, linked to the tasks
that shipped them, and turned into tasks only where an org and project have opted in.

THE TWO HALVES
* Measurement is always collected, whatever any switch says: deploys (`deployments` and children, `branch_pushes`),
  incidents (`incidents`, `incident_events`), suspect linkage (`incident_suspects`), regression measurements
  (`deployment_metrics`), merge-lane stage timings and the outcome-ledger facts (`production_qa_edges`).
* Behaviour — filing a task, filing a revert, running probes — happens only when `gate.state` is enabled: the plan
  switch (`billing_plans.production_qa`, overridden per org by `orgs.production_qa_override`, the
  `merge_lane_enabled` pattern) AND the project's own opt-in (`projects.production_qa_enabled`). All three are off
  by default; no code, seed or revision turns any of them on.

TURNING IT ON FOR A PROJECT (console only, in this order)
1. Switch it on: the org's plan carries `production_qa`, or an operator sets the org override (operator plane,
   Customers › the org › Overrides); then the project opts in (Project › Production QA › the Production QA toggle).
2. Connect deploys — it creates the project's Deploys trigger; add the webhook it shows on the repo when the console
   does not register it itself. On every plan (a release waits for its deploy only once it is connected): also on
   Customize › CI pipeline and Pipeline's Production stage, as Deploy tracking.
3. Project › Production QA › Create default automations — the incident → task automations, created Active.

WHERE EACH PART LIVES
* Schema: `app/models/production_qa.py`; vocabularies the CHECKs render from: `vocabulary.py`; the project, plan, org
  and delivery columns sit on their own models. Revision 202611012000 is the epic's only DDL.
* Reads: `app/routes/production_qa.py` — Deployments and Incidents, list and detail, scoped to the projects the
  caller holds `projects:view` on, and only while the org's plan has the switch (the operator always): a project
  not opted in still shows its record.
* Pinned interfaces, one owning module each: `gate.py` (the switch), `recorder.py` (the trigger-intake entry point),
  `deployments.py` (deploy ingestion and `reconcile`; GitLab's hooks onto it: `gitlab_deploys.py`, Azure DevOps'
  service hooks: `azure_devops.py`, Bitbucket's: `bitbucket.py`), `incidents.py` (`record` and the state machine), `events.py`
  (incident → task: the after-commit raise onto the `incident`/`deploy_failed`/`health_breach` automation kinds, their
  checks, the dedup claim, quiet-hours holds), `templates.py` (the raised event and the default Task body),
  `adapters/` (monitor `verify` + `normalise`),
  `linkage.py` (suspects and the fix loop), `regression.py` (post-deploy windows, breach, resolve) with
  `metric_pulls.py` (the monitors' metric reads), `probes.py` (native probes),
  `edges.py` (ledger facts), `ledger.py` (applies them to the outcome ledger), `cron.py` (the `production-qa` system activity and its job registry), `fingerprints.py`
  (every incident key). A function whose owner has not landed is a stub that records and files nothing.
* Live: `incidents.record` (the state machine, its docstring), the recorder frame in `trigger_intake._Processor`
  (`recorder.py`), the `held` delivery state, the `production-qa` cron, and incident → task (`events.py`, with the
  "Create default automations" route in `app/routes/production_qa.py`). Kinds reach the recorder as their
  owners `recorder.register` a handler (each owner module listed in `_OWNERS`, imported by `load_owners`); a
  record-only trigger with no handler yet records nothing. Deploys: `deployments.py`'s docstring. Native probes (a
  hidden `deploy_probe` run per successful production deploy, results → `health_breach`): `probes.py`'s docstring.

FIXED RULES
* A recorder writes inside a savepoint and never files a task; task-raising events are flushed after commit in
  their own session (`events.flush`). A record-only trigger never adopts an automation (`recorder.records_only`).
* A `held` delivery is never picked by the processor nor counted by the hourly cap; a received one waits out its
  `not_before`.
* Incident severities, lowest first: `vocabulary.SEVERITIES`; a threshold admits its own word and every word after.
* At most one unreleased non-revert `incident_tasks` claim per incident (the partial unique index): one open task
  per incident; a revert claim never blocks.
* No path merges a revert: a revert is an ordinary task whose PR goes through the merge lane.
"""

# The modules that `recorder.register` a handler or `cron.register` a job when imported; `load_owners` imports them
# where a delivery is recorded or a cron pass runs (an import here would cycle through `app.models`).
_OWNERS = (
    "app.production_qa.deployments", "app.production_qa.gitlab_deploys", "app.production_qa.azure_devops",
    "app.production_qa.bitbucket", "app.production_qa.adapters", "app.production_qa.events",
    "app.production_qa.lane_timings", "app.production_qa.regression", "app.production_qa.ledger",
)


def load_owners() -> None:
    """Import every registering owner once (idempotent: Python caches the import)."""
    import importlib

    for name in _OWNERS:
        importlib.import_module(name)
