import type { ReactNode } from 'react'
import Box from '@mui/material/Box'
import { useCan } from '../../auth'
import type { Account, AccountAuthKind, ClaudeAccount, ProviderChoice } from '../../types'
import type { NonBaaSurface } from '../../components/NonBaaWarning'
import type { ProviderCatalogue } from '../../hooks/useAgentProviders'
import { listNodes, workNodes } from '../../hooks/useNodesRoster'
import { DEFAULT_SUBSCRIPTION_SURFACE, isDefaultProvider } from '../../lib/providerCatalogue'
import { monoSx } from '../../ui/listStyles'

/* The runner-account facts both surfaces read: the roster (`Accounts.tsx`) and the account form page
 * (`AccountEditPage.tsx`, New / Edit / Login). One home so the two cannot drift. */

/* The form page's addresses. `login` is keyed by slug: a re-login or enrolment can start from a roster line
 * with no `accounts` row id yet. */
export const ACCOUNTS_PATH = '/accounts'
export const NEW_ACCOUNT_PATH = '/accounts/new'
export const accountEditPath = (id: string) => `/accounts/${encodeURIComponent(id)}`
export function accountLoginPath(slug: string, nodeId?: string | null, enrol = false): string {
  const q = new URLSearchParams()
  if (nodeId) q.set('node', nodeId)
  if (enrol) q.set('enrol', '1')
  const qs = q.toString()
  return `/accounts/login/${encodeURIComponent(slug)}${qs ? `?${qs}` : ''}`
}

// The slug, falling back to the config_dir basename (the agent's `_collect_resources()` convention).
export function slugOf(acct: ClaudeAccount): string | null {
  if (acct.slug) return acct.slug
  if (!acct.config_dir) return null
  const parts = acct.config_dir.split('/').filter(Boolean)
  return parts.length ? parts[parts.length - 1] : null
}

/* ── Auto-assign and the per-account runner cap ──
 * Auto-assign (`Account.auto_assign`) is on every account, so the words are shared by the three forms and
 * the roster chip. The cap (`Account.max_runners`) only for a provider declaring `per_account_runner_cap`. */
export const AUTO_ASSIGN_HINT = 'Runs tasks that allow any provider. Off: only tasks that pick this provider.'
export const EXPLICIT_ONLY_TITLE =
  'Auto-assign is off: this account runs only tasks and consoles that pick its provider. Turn it on from Edit.'
export const MAX_RUNNERS_HINT = 'Blank: the fleet limit.'
export const MAX_RUNNERS_INVALID = 'A whole number of 1 or more, or blank.'

/* The provider column's two hover sentences, one per absence `providerCell` distinguishes, shared by the
roster row, per-node child and edit dialog. Neither is an error. Pointer-only (no touch explanation at
390px): both need the catalogue GET to fail or a version skew, and the 80px slot has no room for prose. */
export const PROVIDER_UNKNOWN_TITLE = 'Provider unknown: the account record did not load.'
export const PROVIDER_UNLABELLED_TITLE = 'Stored value: this build has no display name for this provider.'


/* Which vendor's CLI this account logs into, in the catalogue's `label` (never the stored enum; PRD R1.2),
 * for the roster row, per-node child and edit dialog. Returns label, `kind` and hover together so no
 * surface re-spells the ternary; `title` only on the two answers that owe a sentence.
 *   • `null` ⇒ unknown: no joined record, so nothing to resolve. A dash, not the DB default: stating a
 *     vendor this browser never read is undetectable.
 *   • the raw value ⇒ vendor known, label not: the catalogue settled without this entry (failed GET or a
 *     newer control plane). True, merely less polished.
 *   • `pending` ⇒ still asking: the catalogue's first render is an unread empty list; without this the
 *     edit dialog painted the raw enum under a false "GET failed" hover. Same dash, no hover; kept apart
 *     so a test can tell the regression from the degraded state.
 */
export function providerCell(
  record: Pick<Account, 'provider'> | undefined, catalogue: ProviderCatalogue,
): { label: string | null; kind: 'catalogue' | 'raw' | 'pending' | 'unknown'; title?: string } {
  if (!record?.provider) return { label: null, kind: 'unknown', title: PROVIDER_UNKNOWN_TITLE }
  if (!catalogue.settled) return { label: null, kind: 'pending' }
  const row = catalogue.rows.find(p => p.value === record.provider)
  return row
    ? { label: row.label, kind: 'catalogue' }
    : { label: record.provider, kind: 'raw', title: PROVIDER_UNLABELLED_TITLE }
}

/* SOMEONE ELSE'S ACCOUNT: a node reports a colleague's personal account, but `GET /api/accounts` never
 * returns it (`app/authz/visibility.py::visible_account_clause` — it stays theirs), so there is no record and no
 * provider to read. The cell says whose it is — its owner (`ClaudeAccount.owner`), else the login the node reported —
 * rather than "Provider unknown". Only for a row the records answer DID come back without; a failed read stays unknown. */
export function othersAccountCell(
  acct: Pick<ClaudeAccount, 'owner' | 'display_name' | 'email'>,
): { label: string | null; kind: 'others'; title: string } {
  const who = acct.owner || acct.display_name || acct.email
  return who
    ? { label: `${who}'s`, kind: 'others', title: `${who}'s account` }
    : { label: null, kind: 'others', title: "Someone else's account" }
}

/* The HIPAA non-BAA surface an account of this provider and kind is (`components/NonBaaWarning`), or null.
 * The default provider only on a subscription: its vendor's BAA covers its API, not consumer plans. Any other provider is
 * its own value — `app/hipaa.py::SURFACES` keys an agent CLI's surface by the catalogue's provider value, so
 * which of them are warned stays the server's list. */
export function nonBaaSurfaceFor(provider: string | undefined, kind: AccountAuthKind): NonBaaSurface | null {
  if (isDefaultProvider(provider)) return kind === 'oauth' ? DEFAULT_SUBSCRIPTION_SURFACE : null
  return provider ?? null
}

/* ── Who may log an account in ──
 * Every login (add wizard, enrolment, re-login) is a `POST /api/commands` with an `account_*` verb. The
 * route declares `@feature_any("worktrees:edit", "system:create")`, but `app/routes/commands.py`'s
 * `_require_command_key` narrows via `COMMAND_TYPE_KEYS`, where every `account_*` verb maps to
 * `_SYSTEM_ONLY = {"system:create"}`. This is the narrowed key, one for the whole wizard.
 * DB writes are read off their own routes: `POST /api/accounts`, `PUT`/`DELETE /api/accounts/{id}` and both
 * deprovision routes carry `require_account_writer` (`app/routes/accounts.py`), so the page reads
 * `isOrgAdmin` (`useCan` is allow-all for one). */
export const LOGIN_PERMISSION = 'system:create'

/** Names the missing grant and its remedy (`components/NodeEnableToggle.tsx`'s shape); never "busy". */
export const NO_LOGIN_PERMISSION_REASON =
  `logging an account in needs the ${LOGIN_PERMISSION} permission, which this account does not hold `
  + '— ask an operator to grant it'

/** Whether this viewer may dispatch any account login. One read — see above. */
export function useCanLogIn(): boolean {
  return useCan()(LOGIN_PERMISSION)
}


// ── Which node a login lands on, shared by the add and re-login steps ──
/* One login target: the node's name plus `runner_clis` (`GET /api/nodes`). A provider login asks
 * whether the node has the vendor's CLI (`needs_attested_binary`). `null` stays `null`: never
 * attested is not an empty toolbox (`NodeRecord.runner_clis`; spent in `addLoginNodes`).
 */
export type NodeChoice = {
  /** The node uuid (`NodeRecord.id`): what a login's `node_id` and the account rows carry. */
  node_id: string
  /** Its name, for display only. */
  name: string
  runner_clis: string[] | null
  /** The node reports the accounts token folder mounted (`NodeRecord.accounts_nodelink`). */
  accounts_nodelink?: boolean
  /** `{capability: reason}` the node's OS cannot do (`NodeRecord.unsupported`); absent reads `{}`. */
  unsupported?: Record<string, string>
}

/** Exactly `['api_key']`: a provider whose accounts are a key and nothing else, so Add account skips the
 *  login wizard for the key form. Read off the catalogue (`ProviderChoice.auth_kinds`), never a name. */
export function isApiKeyOnly(p: ProviderChoice | null | undefined): boolean {
  return !!p && p.auth_kinds?.length === 1 && p.auth_kinds[0] === 'api_key'
}

/* The max-runners text as the wire wants it: `null` for blank (the fleet limit), a whole number of 1 or
more, or `undefined` for anything else (the form refuses to send it). The server re-checks
(`routes/accounts.py::_validated_max_runners`). */
export function parseMaxRunners(raw: string): number | null | undefined {
  const t = raw.trim()
  if (!t) return null
  if (!/^[0-9]+$/.test(t)) return undefined
  const n = Number(t)
  return n >= 1 && Number.isSafeInteger(n) ? n : undefined
}

/* A new login's Auto-assign, cap and description. The wizard dispatches `account_add`, which carries none
of them, and the row it makes is born with Auto-assign on, no cap and a blank description (`routes/commands.py::
_ensure_account_row` writes none, so the column defaults apply). So an operator's other answer is a
follow-up `PUT`, held by the PAGE (keyed by slug) and sent once the row appears in `/api/accounts` — not by
the wizard, which the operator may close while the login keeps running. Accepted: the row can take an Any
task in the seconds between being enabled and this write landing. */
export type PendingDispatch = { auto_assign: boolean; max_runners: number | null; description?: string }

/** The PUT body that makes `row` hold `wanted`, or null when it already does. */
export function dispatchBody(row: Account, wanted: PendingDispatch): Record<string, unknown> | null {
  const body: Record<string, unknown> = {}
  if (row.auto_assign !== wanted.auto_assign) body.auto_assign = wanted.auto_assign
  if ((row.max_runners ?? null) !== wanted.max_runners) body.max_runners = wanted.max_runners
  if (wanted.description && row.description !== wanted.description) body.description = wanted.description
  return Object.keys(body).length ? body : null
}

/** The one sentence for a held write that failed, from whichever page sent it (roster or form). */
export function pendingDispatchFailure(name: string, detail: string): string {
  return `${name} signed in, but its settings were not saved (${detail}). Set them from Edit.`
}

/* New logins' Auto-assign/cap/description, by slug, until their row appears. Module state, not React state: the form
 * page that took the answer can be left while the login keeps running, and the roster (which re-reads
 * `/api/accounts` on every `account` event) is what applies it (`Accounts.tsx`). */
const pendingDispatch = new Map<string, PendingDispatch>()
export function holdPendingDispatch(slug: string, wanted: PendingDispatch): void {
  pendingDispatch.set(slug, wanted)
}
/** Forgets a held answer whose login will never produce the row (failed, restarted, or replaced by an
 *  API-key create), so a later account with that slug is not written with it. */
export function dropPendingDispatch(slug: string): void {
  pendingDispatch.delete(slug)
}
/** Takes (removes) every held answer whose row is now in `records`, so none is sent twice. */
export function takePendingDispatch(records: Record<string, Account>): { slug: string; row: Account; wanted: PendingDispatch }[] {
  const out: { slug: string; row: Account; wanted: PendingDispatch }[] = []
  for (const [slug, wanted] of pendingDispatch) {
    const row = Object.values(records).find(r => r.slug === slug)
    if (!row) continue
    pendingDispatch.delete(slug)
    out.push({ slug, row, wanted })
  }
  return out
}

/* The nodes a login can target. Best-effort: `null`, never `[]`, on a failed read — the form must not
 * assert anything about the fleet off a GET that did not answer. `runner_clis` absent stays `null` (never
 * attested), never `[]`. */
export async function fetchNodeChoices(): Promise<NodeChoice[] | null> {
  try {
    const rows = workNodes(await listNodes())
    // A presence-only row has no uuid, so nothing can address it; a host runs no work, so no login targets it.
    return rows.flatMap(n => (n.id ? [{
      node_id: n.id,
      name: n.name,
      runner_clis: n.runner_clis ?? null,
      accounts_nodelink: n.accounts_nodelink === true,
      unsupported: n.unsupported ?? {},
    }] : []))
  } catch {
    return null
  }
}

/* An operator writes a description the way they write a commit (`auth_kind='oauth'`): a backticked span shows
 * as code, never as literal backticks. An odd backtick out has no partner and stays text. */
/* One pattern for the renderer and its gate: a pair with at least one character between, so `` (nothing between)
 * stays text and the preview below shows exactly when the rendering differs. */
const INLINE_CODE = /`([^`]+)`/
export function withInlineCode(text: string): ReactNode {
  const parts = text.split(INLINE_CODE)
  if (parts.length === 1) return text
  return parts.map((part, i) => (i % 2 === 1
    ? <Box key={i} component="code" sx={[monoSx, { fontSize: '0.95em', overflowWrap: 'anywhere' }]}>{part}</Box>
    : part))
}

/** Whether `withInlineCode` would render `text` differently from its source. */
export function hasInlineCode(text: string): boolean {
  return INLINE_CODE.test(text)
}
