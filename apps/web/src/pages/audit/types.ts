/* THE AUDIT BROWSE SEAM — the one row definition shared by the Activity tab's feed and its event page,
 * mirroring `AuditEventOut` (`GET /api/audit`, `app/routes/audit.py`) FIELD FOR FIELD AND NULLABILITY FOR
 * NULLABILITY: promising `string` for a wire `null` moves the crash from compiler to render.
 * The Nodes tab does not use it: the command log is its own source (`Command` in `src/types.ts`; epic D2).
 */

/** The entity an action was performed ON. `label` is null when the subject no longer exists, has no
 *  resolver, or is ungranted; `type` and `id` are always served — a renderer must handle an unnamed subject. */
export type AuditSubject = {
  type: string
  id: string
  label: string | null
}

/** One row of the audit browse API's item list — the TypeScript mirror of `AuditEventOut`. */
export type AuditRow = {
  id: number
  /** The acting account. Null for an action with no authenticated user behind it. */
  user_id: string | null
  /** The actor for a human (email or name). NULL on a machine row (the System sentinel stands in) or an
   *  unauthenticated request. */
  actor_label: string | null
  /** `read` / `write` — the coarse class of what the request did. */
  action_type: string
  /** The fine-grained vocabulary term (the handler name, or a retired record-write kind). */
  kind: string
  /** The one-line label for this row's ACTION ("Runner started", or spaced `kind`) — what makes a machine
   *  row (empty `summary`) readable. Never empty because the server derives it from `kind`, not by schema
   *  (the wire is a bare `str`); `rows.ts::rowSentence` leans on it as its last tier. */
  label: string
  /** The humanised sentence; EMPTY on a `task:*` transition row (prose is `label` + `subject`).
   *  `rowSentence` in `./rows` decides what a cell shows then. */
  summary: string
  /** The request path, for a captured API call. */
  path: string | null
  /** The HTTP method, for a captured API call. */
  method: string | null
  /** The HTTP status. NULL on rows not from the request middleware, so a status filter cannot match them
   *  (and must say so). */
  status: number | null
  /** The captured, redacted payload. The server defaults it to `{}` rather than omitting it. */
  data: Record<string, unknown>
  subject: AuditSubject | null
  /** The org the row belongs to. Served ONLY to the operator (the API's operator scope); null for a
   *  tenant, and for a row with no org. */
  org: AuditOrg | null
  created_at: string
}

/** A row's org, as the operator sees it. */
export type AuditOrg = {
  id: string
  name: string
}

/** The list envelope (`AuditPageOut`). Keyset-paged: `next_cursor` is the handle on the next OLDER page,
 *  `null` at the end. `total_kind` says what `total` is: `exact` (one uncursored page, or a tenant's
 *  count within the cap), `estimate` (the planner's, operator only), or `at_least` (a floor: past the cap, or a
 *  count that ran too long). `any_visible` tells "no match" from "nothing captured yet". `filter_labels` names
 *  the request's own `user_id`/`org_id`/`subject`, so chips restored from a URL read as names. */
export type AuditPage = {
  items: AuditRow[]
  next_cursor: string | null
  total: number
  total_kind: AuditTotalKind
  any_visible: boolean
  filter_labels: AuditFilterLabels
}

export type AuditTotalKind = 'exact' | 'estimate' | 'at_least'

export type AuditFilterLabels = {
  users: Record<string, string>
  orgs: Record<string, string>
  subject: string | null
}

/** `GET /api/audit/suggest?q=` — facet values for the search box's chips (≤6 each). `orgs` is empty unless
 *  the caller is the operator. A user `id` is a uuid or the `system` sentinel; a subject is the
 *  `<type>:<id>` the `subject` param takes. */
export type AuditSuggestions = {
  users: { id: string; label: string; hint: string | null }[]
  orgs: { id: string; name: string; slug: string }[]
  subjects: { type: string; id: string; label: string }[]
}
