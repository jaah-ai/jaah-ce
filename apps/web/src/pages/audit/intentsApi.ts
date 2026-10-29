/* The fleet-wide slot-intent queue's API client — a page-local wire module (the `pages/workflows/api.ts`
 * precedent), and the home of the row shape now that this panel is its only reader (`src/types.ts`'s NODE
 * HEALTH block points here). Field meanings are settled server-side in
 * `apps/api/app/routes/nodes/intents.py::IntentRow` and `::IntentQueue`.
 * Computes and judges nothing: `state`, `age_seconds`, `withheld_seconds` and the refusal halves are
 * derived SERVER-side, since a skewed browser clock would misjudge due/withheld. */

import { get } from '../../api'

/** The queue's two halves: one parameter because one server predicate splits them (`routes/nodes/intents.py`
 *  single-homes which outcomes retire a row). */
export type IntentSet = 'pending' | 'retired'

/** ONE `slot_intents` ROW — `routes/nodes/intents.py::IntentRow`. `state` is derived, stored nowhere: `due`
 *  (next pool tick serves it), `withheld` (refused, in backoff), `retired` (kept until the purge). No
 *  command payload travels. */
export type FleetIntentRow = {
  id: number
  verb: string
  /** The node's NAME, for display: a fleet-wide page needs every row to name its host. */
  node: string
  /** The node's uuid — what `/nodes/{id}` takes (`node` is a label that may repeat). */
  node_id: string
  /** The project's NAME, resolved server-side; null when no project row matches. */
  project: string | null
  number: number
  slot: string
  reason: string | null
  created_at: string
  age_seconds: number
  acked_at: string | null
  outcome: string | null
  refusals: number
  state: 'due' | 'withheld' | 'retired'
  next_attempt_at: string | null
  withheld_seconds: number | null
  /** The refusal tally's two halves, recovered from `refusal_key`. `refusal_outcome` because a PARKED row's
   *  `outcome` is rewritten to `parked`; `refusal_detail` because a re-minted row carries the budget
   *  forward while its `reason` is the mint's sentence. Render these for a refusal, `reason` for the latest
   *  word. Both null if it never refused. */
  refusal_outcome: string | null
  refusal_detail: string | null
}

/** The queue in view, summarised — `routes/nodes/intents.py::IntentQueue`, the wedge detector. Intents are
 *  minted by a 60s pass and drained by a ~5s pool tick, so minutes of waiting means something is stuck.
 *  `refusing` is sharpest: identical refusals are parked after `fleet_verdicts.REFUSAL_PARK_AFTER`
 *  attempts. Scoped to the nodes the request selected, so the panel shows filter and summary
 *  together. */
export type IntentQueueCounts = {
  pending: number
  /** Pending AND due now. `pending - due` is `withheld`. */
  due: number
  withheld: number
  refusing: number
  oldest_pending_age_seconds: number | null
  /** `{verb: count}` over the PENDING set only. */
  by_verb: Record<string, number>
  /** `{outcome: count}` over the RETIRED set only. */
  recent_outcomes: Record<string, number>
  /** Retired FAILURES are purged past this age, so an empty retired set means "nothing recently", NEVER
   *  "nothing ever". The panel must say so. */
  retention_seconds: number
}

/** One page of the fleet-wide queue — `GET /api/nodes/intents`. Same pagination dialect as
 *  `GET /api/sessions`: 1-based `page`, `per`, `total` (filtered) and `total_unfiltered`, so an empty
 *  result can say "nothing matches" vs "nothing queued".
 *  `verbs`/`outcomes`/`nodes` are the vocabulary present in this set (server scan, never a client copy of
 *  `fleet_verdicts`); the panel unions the active value in so a filter cannot vanish from its control. */
export type FleetIntentPage = {
  items: FleetIntentRow[]
  total: number
  page: number
  per: number
  total_unfiltered: number
  set: IntentSet
  verbs: string[]
  outcomes: string[]
  /** Node NAMEs in this set; kept by the server for older readers. */
  nodes: string[]
  /** The node filter's options, `{id, name}` sorted by name: `?node=` takes the id (see `nodeOptions` in
   *  `SlotIntentsTab.tsx` for the growing union). */
  node_options: { id: string; name: string }[]
  counts: IntentQueueCounts
}

/** Filters; `''` equals absent, translated here so a blank `verb=` never goes on the wire. */
export type FleetIntentFilters = {
  set: IntentSet
  /** 1-based; omitted for the first page, so the first request is exactly `set=<set>&page=1`. */
  page?: number
  verb?: string
  /** Retired set only; the server 422s `state`/`outcome` against the wrong half. */
  outcome?: string
  /** Pending set only: `due` | `withheld`. Why not `retired`: `_INTENT_STATES` (routes/nodes/intents.py). */
  state?: string
  /** Rows that have refused at least once. Sent as `true` or not at all. */
  refusing?: boolean
  /** A node uuid (a name is a label that may repeat). */
  node?: string
}

/** The URL is built here so its first-load shape can be asserted; `per` is not sent, so the server's
 *  default comes back on the envelope. */
export function listFleetIntents(filters: FleetIntentFilters) {
  const params = new URLSearchParams({ set: filters.set })
  params.set('page', String(filters.page ?? 1))
  if (filters.verb) params.set('verb', filters.verb)
  if (filters.outcome) params.set('outcome', filters.outcome)
  if (filters.state) params.set('state', filters.state)
  if (filters.refusing) params.set('refusing', 'true')
  if (filters.node) params.set('node', filters.node)
  return get<FleetIntentPage>(`/api/nodes/intents?${params}`)
}
