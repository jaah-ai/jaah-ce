import type { AuditRow, AuditSubject } from './types'

/* What an audit row says, in pure functions: every string the Activity tab shows about a row comes from
 * here. The no-blank-cell rule must hold for every possible row, which only a unit test over the
 * served shapes can prove (`rows.test.ts`); the pipeline and repeat collapses are folds, so they sit outside the
 * render map. No dates: `formatZoned` at the call site owns the page's zone. */

/** An actorless row's actor cell. The chip's wire value is not spelled here: `GET /api/audit/suggest`
 *  returns the System option with its own `id`, which the page passes back. */
export const SYSTEM_ACTOR_LABEL = 'System'

/** The operator's org cell for a row that belongs to no org: a refused sign-in for an address no account holds,
 *  filed under the operator org's id only because no org owns it (the API serves it no org). Also that
 *  refusal's actor cell (`isUnattributed`): no person did it, and it is not the System's either. */
export const UNATTRIBUTED_LABEL = 'Unattributed'

/** The record a refused sign-in writes beside its request (`app/login_funnel.py::record_login_failure`): actorless,
 *  the attempted identifier in `data.identifier`. */
const SIGN_IN_REFUSAL_KIND = 'login_failed'

/** A refused sign-in's action, alone or folded: the head of its sentence. */
const SIGN_IN_REFUSED_LABEL = 'Sign-in refused'

/** A pipeline-written row? Keyed on `kind`, NOT `user_id === null`: a failed login has a null `user_id`
 *  but carries the attempted `actor_label` and a 401, and must not be filed under "System". */
export function isMachineRow(row: Pick<AuditRow, 'kind'>): boolean {
  return row.kind.startsWith('task:') || row.kind.startsWith('task_note:')
}

/** The refusal reasons that mean no account holds the attempted address (`SIGN_IN_REFUSAL_REASONS`' words). */
const UNKNOWN_ACCOUNT_REASONS: ReadonlySet<string> = new Set(['unknown_identifier', 'unknown_email', 'no_account'])

/** A refused sign-in for an address no account holds: no one to name. The typed address stays in `data.identifier`,
 *  never the actor — it names no account. */
export function isUnattributed(row: Pick<AuditRow, 'kind' | 'data'>): boolean {
  const reason = row.data['reason']
  return row.kind === SIGN_IN_REFUSAL_KIND && typeof reason === 'string' && UNKNOWN_ACCOUNT_REASONS.has(reason)
}

/** Who the row names as its actor, or null for none. A refusal record is actorless but names its attempt
 *  (`data.identifier`): it reads as that address, so a search that matches it alone (not its refused request)
 *  names the same person the folded line does — unless no account holds it (`isUnattributed`). */
export function actorLabel(row: Pick<AuditRow, 'actor_label' | 'kind' | 'data'>): string | null {
  if (row.actor_label) return row.actor_label
  const identifier = row.kind === SIGN_IN_REFUSAL_KIND && !isUnattributed(row) ? row.data['identifier'] : null
  return typeof identifier === 'string' && identifier.trim() ? identifier.trim() : null
}

/* A key's own lifecycle rows name it in `data` too, but were done by a console session, not with the key. */
const API_KEY_LIFECYCLE_KINDS: ReadonlySet<string> = new Set([
  'api_key_created', 'api_key_edited', 'api_key_rotated', 'api_key_revoked', 'api_key_expired',
])

/** The API key a request was made with (`request_caller.facts`' `api_key_name`; a refused key's own row too), or
 *  null for a console session. */
export function viaApiKey(row: Pick<AuditRow, 'kind' | 'data'>): string | null {
  const name = row.data['api_key_name']
  return typeof name === 'string' && name !== '' && !API_KEY_LIFECYCLE_KINDS.has(row.kind) ? name : null
}

/** The actor cell's text. Never empty: an actorless row reads as the System sentinel, an unknown address's refusal
 *  as Unattributed. A request made with an API key reads "<owner> via API key <name>", composed here, never stored. */
export function actorText(row: Pick<AuditRow, 'actor_label' | 'kind' | 'data'>): string {
  const key = viaApiKey(row)
  return key ? `${actorName(row)} via API key ${key}` : actorName(row)
}

/** The actor alone, without the key: the cell leads with the key, so a cut address never hides it. */
export function actorName(row: Pick<AuditRow, 'actor_label' | 'kind' | 'data'>): string {
  return actorLabel(row) ?? (isUnattributed(row) ? UNATTRIBUTED_LABEL : SYSTEM_ACTOR_LABEL)
}

/** Is this a refused sign-in's own record? It carries no status (it is not the captured request), so alone — a
 *  search that matches it and not its 4xx request — its disc must still say "refused", as the folded line does. */
export function isSignInRefusal(row: Pick<AuditRow, 'kind'>): boolean {
  return row.kind === SIGN_IN_REFUSAL_KIND
}

/** A refusal's `data.reason` in words, for the reasons a person reads often; any other code reads with its
 *  underscores spaced, never raw (`app/login_funnel.py::record_login_failure` callers set them). */
const SIGN_IN_REFUSAL_REASONS: Record<string, string> = {
  unknown_identifier: 'no account for this address',
  unknown_email: 'no account for this address',
  no_account: 'no account for this address',
  no_password: 'account has no password',
  bad_password: 'wrong password',
  bad_factor: 'wrong code',
  bad_credential: 'credential rejected',
  locked: 'too many attempts',
  step_up_required: 'too many attempts, second factor required',
  disabled: 'account disabled',
  account_disabled: 'account disabled',
  org_pending: 'account awaiting review',
  org_not_approved: 'account not approved',
  org_deleted: 'account deleted',
  superadmin_password: 'superadmin cannot use a password',
  password_off: 'password sign-in is off',
  sso_required: 'SSO required',
  sso_enforced: 'SSO required',
  ip_not_allowed: 'address not on the IP allowlist',
  already_linked: 'identity linked to another user',
  ambiguous: 'several accounts match',
  bad_callback: 'callback did not match',
  provider_error: 'provider refused',
  sso_not_on_plan: 'SSO not on the plan',
  sso_domain_mismatch: 'email domain not verified for SSO',
  sso_not_provisioned: 'no membership for this SSO user',
  user_limit_reached: 'user limit reached',
}

/** A sign-in door (`data.method`) as people name it. */
const SIGN_IN_METHODS: Record<string, string> = {
  google: 'Google',
  github: 'GitHub',
  microsoft: 'Microsoft',
  saml: 'SAML',
  oidc: 'OIDC',
}

/** A refusal record's sentence from its data — "Sign-in refused: wrong password (password)" — or null when it
 *  carries no reason, so the server's summary stands. */
function refusalSentence(row: Pick<AuditRow, 'data'>): string | null {
  const reason = row.data['reason']
  if (typeof reason !== 'string' || !reason.trim()) return null
  const words = SIGN_IN_REFUSAL_REASONS[reason] ?? reason.replace(/_/g, ' ')
  const method = row.data['method']
  const door = typeof method === 'string' && method ? ` (${SIGN_IN_METHODS[method] ?? method})` : ''
  return `${SIGN_IN_REFUSED_LABEL}: ${words}${door}`
}

/** The action cell's text: the row's label, except a refusal record's, which says what its sentence says
 *  ("Sign-in refused: …"), not the server's "Login failed". */
export function actionText(row: Pick<AuditRow, 'label' | 'kind'>): string {
  return isSignInRefusal(row) ? SIGN_IN_REFUSED_LABEL : row.label
}

/** A subject inline; never a bare identifier with no type word:
 *    • task, resolved → `#42 HCD activity log` (`#` is the type word).
 *    • other type, resolved → `project Acme portal` (id dropped: already a slug; the drawer has it).
 *    • unresolved → `project acme` / `task #42` (deleted, no resolver, or no grant). */
export function subjectText(subject: AuditSubject): string {
  if (subject.type === 'task') {
    const id = `#${subject.id}`
    return subject.label ? `${id} ${subject.label}` : `${subject.type} ${id}`
  }
  return `${subject.type} ${subject.label ?? subject.id}`
}

/** A subject after an action word ("Update account — Weekday rota"): the action already names the type, so a
 *  resolved non-task subject is its label alone. A task keeps `#id` (its type word); an unresolved subject keeps
 *  `subjectText`, since a bare id means nothing. */
export function subjectName(subject: AuditSubject): string {
  return subject.type !== 'task' && subject.label ? subject.label : subjectText(subject)
}

/** Does `sentence` name `subject` whole — not as the head of a longer id or word? */
export function saysSubject(sentence: string, subject: string): boolean {
  const escaped = subject.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
  return new RegExp(`(?<![\\w#])${escaped}(?![\\w#])`).test(sentence)
}

/** Does `sentence` already name `subject`: as `subjectText`, or as the `— subjectName` tail `rowSentence` writes? The
 *  bare label is matched only as that tail, so a short one ("acme") in an unrelated sentence hides nothing. */
export function namesSubject(sentence: string, subject: AuditSubject): boolean {
  return saysSubject(sentence, subjectText(subject)) || sentence.endsWith(` — ${subjectName(subject)}`)
}

/** The summary cell's sentence, never empty (AC7.1). Tiers, in order:
 *    • a refused (4xx) or failed (5xx) request — its label and status, never the raw `METHOD /path`.
 *    • a request whose summary is the raw `METHOD /path` (no sentence template) — its label and resolved subject
 *      ("Update account — Weekday rota"); the path stays on the event's Request tab. Not for the server's
 *      `unknown` kind: its label says nothing, so the path is the better sentence.
 *    • `summary` — every captured request row and `task_note:*` row.
 *    • the subject — `task:*` transition rows have a DELIBERATELY empty `summary`.
 *    • method + path — a captured row with no summary and no label to say it with.
 *    • `label`, then `kind` — `label` is a bare `str` on the wire, while `kind` is `NOT NULL` and is what
 *      `label` derives from, so the function is total by construction. */
export function rowSentence(row: AuditRow): string {
  /* A refusal's summary carries its raw reason code; the sentence says it in words. */
  if (isSignInRefusal(row)) {
    const refused = refusalSentence(row)
    if (refused) return refused
  }
  const raw = row.method && row.path ? `${row.method} ${row.path}` : null
  /* A refused or failed write keeps the raw `METHOD /path` summary (the server's past-tense sentence would claim it
     happened): its label says what was attempted ("Create task — refused 422"). */
  if (row.status !== null && row.status >= 400 && row.label && (!row.summary.trim() || row.summary === raw)) {
    return `${row.label} — ${row.status >= 500 ? 'failed' : 'refused'} ${row.status}`
  }
  if (raw && row.label && row.summary === raw && row.kind !== 'unknown') {
    return row.subject?.label ? `${row.label} — ${subjectName(row.subject)}` : row.label
  }
  if (row.summary.trim()) return row.summary
  if (row.subject) return subjectText(row.subject)
  if (row.path) return row.method ? `${row.method} ${row.path}` : row.path
  return row.label || row.kind
}

/** The step word a collapsed pipeline line shows for one machine row: `ready`, `runner session`. */
export function stepWord(row: Pick<AuditRow, 'kind'>): string {
  if (row.kind.startsWith('task:')) return row.kind.slice('task:'.length).replace(/_/g, ' ')
  if (row.kind.startsWith('task_note:')) return row.kind.slice('task_note:'.length).replace(/_/g, ' ')
  return row.kind.replace(/_/g, ' ')
}

/** The task id a machine row names, or null if it names none. */
function machineRowTaskId(row: AuditRow): string | null {
  const id = row.data['id']
  if (typeof id === 'number') return String(id)
  if (typeof id === 'string' && id) return id
  return null
}

/** A line's React key, shared by `toLines` and the renderer. */
export function rowKey(row: Pick<AuditRow, 'id'>): string {
  return `row-${row.id}`
}

/** A task known only by id; routed through `subjectText` so the unresolved format has one home. */
export function taskReference(taskId: string): string {
  return subjectText({ type: 'task', id: taskId, label: null })
}

/** One rendered line of the table: a single row, a collapsed run of machine rows, or a counted run of one
 *  person's identical self-writes. */
export type AuditLine =
  | { kind: 'row'; key: string; row: AuditRow }
  | {
      kind: 'pipeline'
      key: string
      /** The task these events all belong to. */
      taskId: string
      /** The newest row's subject, when it resolved. */
      subject: AuditSubject | null
      /** Newest first, as served. */
      rows: AuditRow[]
    }
  | {
      kind: 'repeat'
      key: string
      /** Newest first, as served; every row says the same sentence for the same person. */
      rows: AuditRow[]
    }
  | {
      kind: 'signin'
      key: string
      /** The sign-in request (`login`, `google_login`, `mfa_verify`): its status and actor are the line's. */
      request: AuditRow
      /** How the second factor was met without a code (`mfa_skipped`, `mfa_recovery_used`), or why the sign-in was
       *  refused (`login_failed`): its sentence is the line's. */
      factor: AuditRow
      /** Both, newest first, as served. */
      rows: AuditRow[]
    }

/** Kinds whose identical consecutive writes by one person fold into one counted line: a person's own account
 *  write fires once per preference saved, so one sign-in can leave four in a row. */
const REPEAT_FOLD_KINDS: ReadonlySet<string> = new Set(['update_me'])

/** What makes two neighbouring rows one repeat: same kind, same person, same outcome, same sentence. Null when the
 *  row never folds (another kind, or no person to attribute it to). */
function repeatKey(row: AuditRow): string | null {
  if (!REPEAT_FOLD_KINDS.has(row.kind) || row.user_id === null) return null
  return JSON.stringify([row.kind, row.user_id, row.status, rowSentence(row)])
}

/** The sign-in routes' capture rows (`app/user_activity.py::AUTH_WRITE_PATHS`, kind = handler name). */
const SIGN_IN_REQUEST_KINDS: ReadonlySet<string> = new Set(['login', 'google_login', 'mfa_verify'])

/** The second-factor records a sign-in writes beside its request (`app/login_funnel.py`, `app/routes/mfa.py`). */
const SIGN_IN_FACTOR_KINDS: ReadonlySet<string> = new Set(['mfa_skipped', 'mfa_recovery_used'])

/** Does the refusal record belong to the refused request beside it? A request that submitted an identifier (the
 *  password door) needs the refusal to name it. One that submitted none needs the refusal to be its door's: the second
 *  factor's (`bad_factor`, `app/routes/mfa.py`) for `mfa_verify`, any non-password door's for a provider callback —
 *  never a neighbouring password refusal, which names someone else's attempt. */
function sameAttempt(request: AuditRow, refusal: AuditRow): boolean {
  const submitted = request.actor_label?.trim().toLowerCase()
  if (submitted) {
    const identifier = refusal.data['identifier']
    return typeof identifier === 'string' && identifier.trim().toLowerCase() === submitted
  }
  if (request.kind === 'mfa_verify') return refusal.data['reason'] === 'bad_factor'
  return refusal.data['method'] !== 'password'
}

/** The two rows of ONE sign-in, as `{ request, factor }`, or null, for neighbours:
 *    • a succeeded sign-in request and its factor record, by the same person (otherwise each sign-in reads
 *      as a "Login" / "MFA skipped" pair);
 *    • a refused sign-in request and its `login_failed` record, for the same identifier (otherwise a
 *      "Login — refused 401" beside a System "Sign-in refused: …").
 *  A refused request never takes a factor record (it minted no session), nor a succeeded one a refusal. */
export function signInPair(a: AuditRow, b: AuditRow): { request: AuditRow; factor: AuditRow } | null {
  const request = SIGN_IN_REQUEST_KINDS.has(a.kind) ? a : SIGN_IN_REQUEST_KINDS.has(b.kind) ? b : null
  const factor = request === a ? b : a
  if (request === null) return null
  if (request.status !== null && request.status >= 400) {
    return factor.kind === SIGN_IN_REFUSAL_KIND && request.user_id === null && sameAttempt(request, factor)
      ? { request, factor }
      : null
  }
  if (!SIGN_IN_FACTOR_KINDS.has(factor.kind)) return null
  if (request.user_id === null || request.user_id !== factor.user_id) return null
  return { request, factor }
}

/** A sign-in line's sentence: the factor record's ("… signed in without a code on trusted Chrome", "Sign-in refused:
 *  wrong password (password)"), which names both the sign-in and how it went. */
export function signInSentence(line: Extract<AuditLine, { kind: 'signin' }>): string {
  return rowSentence(line.factor)
}

/** A sign-in line's action: "Sign-in refused" for a refusal (its sentence's head), else the request's label. */
export function signInAction(line: Extract<AuditLine, { kind: 'signin' }>): string {
  return line.factor.kind === SIGN_IN_REFUSAL_KIND ? actionText(line.factor) : line.request.label
}

/** Steps spelled out before eliding: what fits at 1280px beside the other columns. */
export const TRAIL_MAX_STEPS = 6

/** THE D24 COLLAPSE: a run of CONSECUTIVE machine rows for ONE task becomes one expandable line; a run of
 *  consecutive identical self-writes (`REPEAT_FOLD_KINDS`) by one person becomes one counted line; a sign-in's
 *  request and its factor record (`signInPair`) become one line. Consecutive only: grouping across an unrelated
 *  row would reorder the log. A run of ONE stays an ordinary row. */
export function toLines(items: AuditRow[]): AuditLine[] {
  const lines: AuditLine[] = []
  /* The open run and what groups it are one value, so a run always knows its kind by type. */
  let run: { kind: 'pipeline' | 'repeat'; group: string; rows: AuditRow[] } | null = null

  function flush() {
    if (run === null) return
    if (run.rows.length === 1) {
      lines.push({ kind: 'row', key: rowKey(run.rows[0]), row: run.rows[0] })
    } else if (run.kind === 'repeat') {
      lines.push({ kind: 'repeat', key: `repeat-${run.rows[0].id}`, rows: run.rows })
    } else {
      lines.push({
        kind: 'pipeline',
        key: `pipeline-${run.group}-${run.rows[0].id}`,
        taskId: run.group,
        subject: run.rows.find(r => r.subject !== null)?.subject ?? null,
        rows: run.rows,
      })
    }
    run = null
  }

  for (let i = 0; i < items.length; i++) {
    const row = items[i]
    const pair = i + 1 < items.length ? signInPair(row, items[i + 1]) : null
    if (pair) {
      flush()
      lines.push({ kind: 'signin', key: `signin-${row.id}`, ...pair, rows: [row, items[i + 1]] })
      i += 1
      continue
    }
    const taskId = isMachineRow(row) ? machineRowTaskId(row) : null
    const kind = taskId !== null ? 'pipeline' : 'repeat'
    const group = taskId ?? repeatKey(row)
    if (group !== null && run !== null && run.kind === kind && run.group === group) {
      run.rows.push(row)
      continue
    }
    flush()
    if (group !== null) run = { kind, group, rows: [row] }
    else lines.push({ kind: 'row', key: rowKey(row), row })
  }
  flush()
  return lines
}

/** A repeat line's sentence: the shared sentence and its count, `Updated their preferences ×4`. */
export function repeatSentence(line: Extract<AuditLine, { kind: 'repeat' }>): string {
  return `${rowSentence(line.rows[0])} ×${line.rows.length}`
}

/** The state trail, oldest first: `created -> ready -> active`. Consecutive identical steps collapse to
 *  `step x N` (one task can carry 24 consecutive `task_note:runner_session` rows). Elision keeps both
 *  ends. */
export function trailSteps(rows: AuditRow[]): string[] {
  const oldestFirst = [...rows].reverse()
  const steps: string[] = []
  /* `started`, not a truthiness test on `last`: a `kind` of exactly `task:` yields an EMPTY step word that
   * would otherwise vanish in the repeat branch. */
  let started = false
  let last = ''
  let repeats = 0

  function push() {
    if (!started) return
    steps.push(repeats > 1 ? `${last} ×${repeats}` : last)
  }

  for (const row of oldestFirst) {
    const word = stepWord(row)
    if (started && word === last) {
      repeats += 1
      continue
    }
    push()
    started = true
    last = word
    repeats = 1
  }
  push()

  if (steps.length <= TRAIL_MAX_STEPS) return steps
  /* The elision carries its count, so a 2-step and a 40-step overflow read differently. */
  const head = steps.slice(0, TRAIL_MAX_STEPS - 2)
  const elided = steps.length - head.length - 1
  return [...head, `+${elided} more`, steps[steps.length - 1]]
}
