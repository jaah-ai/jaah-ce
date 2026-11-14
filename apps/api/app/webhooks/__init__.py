"""Outgoing webhooks — an org admin points HTTPS URLs at event types and Jaah POSTs each event to them, signed.

THE PIECES: `catalog` (the event types, read from the event catalog's `[webhooks]` table, their single home),
`engine` (event → delivery, signing, sending, retries, the `webhooks` cron's pass), the doors in
`app/routes/webhooks.py`, the tables in `app/models/webhooks.py`. Plan switch `webhooks_enabled`, ON on every plan.

FROM EVENT TO DELIVERY: the source is `activity_events` — a task's lifecycle rows (`task:<action>`,
`user_activity.record_task_transition`) and the `pr:opened` / `pr:merged` rows (`app/task_prs.py`,
`app/outcome_ledger/writer.py`). The per-minute `webhooks` system cron (`app/cron_system_activities.py`) is the only
guaranteed path: for each enabled endpoint it reads the org's rows past the endpoint's cursor that are at least
`engine.SETTLE` old (the SIEM export's cursor rule), inserts one `webhook_deliveries` row per
chosen type in the endpoint's projects (unique `(endpoint_id, event_id)`, `ON CONFLICT DO NOTHING`, the endpoint row
locked `FOR UPDATE SKIP LOCKED`), then sends the due deliveries it claims under `FOR UPDATE SKIP LOCKED`. A task or PR
write nudges the cron to run again just after `SETTLE` (`engine.nudge_after_commit`), so a delivery leaves within
seconds; a lost nudge costs latency only.

RETRIES: a non-2xx answer or no answer is retried after 1 m, 5 m, 30 m, 2 h, 6 h and 12 h (seven attempts), then the
delivery is `failed`. Redeliver starts a fresh round. Deliveries are deleted after 30 days.

THE WIRE: `POST <url>`, a JSON body, no redirects followed, dialled only at an address `http_bounded.vetted_addresses`
passed (https, public: a private, link-local or metadata address is refused at save with 422 `url_refused` and
never dialled). Headers: `X-Jaah-Event` (the type), `X-Jaah-Delivery` (the delivery id, the same on every retry),
`X-Jaah-Timestamp` (unix seconds), `X-Jaah-Signature: sha256=<hex HMAC-SHA256(secret, "<timestamp>.<raw body>")>`
— during a rotation's 24 h overlap a second, comma-separated `sha256=` signed with the previous secret. Body:
`{"id", "type", "created_at", "org": {"id", "slug"}, "project": {"id", "name"} | null, "data": {…}}`, where `data`
holds ids, titles, states and URLs — never a task body or a transcript.

THE SECRET: minted by Jaah (`whsec_…`), shown once at create and at each rotation, stored as a row envelope bound to
the endpoint's id; nothing reads it back out.
"""
