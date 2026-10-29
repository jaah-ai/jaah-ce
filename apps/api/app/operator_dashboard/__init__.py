"""The operator dashboard's API: fleet-wide usage, efficiency and results metrics, sliceable and trended.

Operator-only (every route `@superadmin_only(Tenancy.OPERATOR)`, so any tenant seat — owner, admin, member, a
tenant superadmin — is refused 403) and on by default: no flag, plan or setting gates it. Compute-on-read over
the source tables, reusing the existing metric functions; no rollup table.

Map:
  frame.py     query parsing (`range`, the seven filter dimensions, `group_by`), the bucket window, the
               Metric/Grouped/Envelope shapes and statuses, group-by top 7 + other, the 5-minute TTL cache
  facts.py     the rows read, once per request, and the §3 attribution rules (task → org/project/persona/
               user/model/provider/node; done/failed off the visible trail; retries)
  cost.py      tokens and dollars through `model_prices` + `task_run_metrics`' folds; `SYSTEM_COST_SOURCES`,
               where the control plane's own AI spend registers
  sections.py  `/usage`, `/efficiency`, `/results`, `/dimensions`
  quality.py   `/quality`, and the AI reviews' spend in `SYSTEM_COST_SOURCES`
  review.py    the weekly AI quality review (the `outcome-review` cron's writer); vocabulary.py its constants
  digest.py    the weekly AI insight digest (the `insight-digest` cron's writer), `/digests`, and its spend
  derived.py   `/autonomy`, `/funnel`, `/tenants`, `/anomalies` (R6, R7a): zero-touch, funnel, unit cost,
               waste, cycle time, tenant health (the `dashboard_*` knobs), forecast and anomaly flags
  anomaly.py   the pure detector and forecast arithmetic (§7), tested on fixtures
  routes.py    the router, under `/api/operator/dashboard`

Sibling sections (AI quality, digests) build on `frame` and register cost in `cost`; every
endpoint must pass the API test suite's operator-dashboard contract helper.
"""
