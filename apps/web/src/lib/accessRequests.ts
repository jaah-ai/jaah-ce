// Access requests on the web (access-model C10): the refusal hint, the route → resource map, the request
// and inbox wire shapes, and who is shown the Requests page. The rules themselves are the API's —
// `apps/api/app/access_requests.py` (who may ask, who decides) and `apps/api/app/routes/inbox.py` (the
// bell's four kinds); this module owns only what the web reads and sends.

import { ApiError, get, post } from '../api'
import type { Admin } from '../auth'
import type { BillingNotice } from './billing'
import { isOrgAdminSeat } from './orgAdmin'

/** A scope a request can name (`access_requests.REQUESTABLE_SCOPES`, plus the org the route map knows). */
export type RequestScopeType = 'org' | 'project' | 'account'

export type RequestScope = { type: RequestScopeType; id: string }

/** The `request` field of a hinted 404/403: which role at which scope would let the caller in. `role` is
 *  null when the page derived the scope itself and the API will pick the least role. */
export type RequestHint = { scope: RequestScope; role: string | null }

export type RequestState = 'pending' | 'approved' | 'denied'

/** One request as `GET /api/access/requests` serves it (`access_requests.wire`). `scope.name` is null when
 *  the caller may not see the scope — asking discloses no name. */
export type AccessRequest = {
  id: string
  requester: { id: string; name: string }
  role: { slug: string; name: string }
  scope: { type: RequestScopeType; id: string; name: string | null }
  note: string | null
  state: RequestState
  created_at: string
  decided_at: string | null
  decided_by: { id: string; name: string } | null
  binding_id: string | null
}

export type InboxKind =
  | 'mention' | 'reaction' | 'access_request' | 'access_decision' | 'support_ticket' | 'support_task_done' | 'billing'
  | 'trigger_failed' | 'trigger_digest' | 'automation_alert' | 'api_key_expiring'

/** A trigger owner's notice, or an Owner's/Admin's automation alert (`app/trigger_notices.py::wire`): the
 *  server's sentence, a second line, and the
 *  console path it opens. */
export type TriggerNotice = {
  kind: 'trigger_failed' | 'trigger_digest' | 'automation_alert'
  trigger_id: string | null
  text: string
  detail: string
  href: string
}

/** An API key owner's expiry reminder (`app/api_keys.py::reminder_wire`): the server's sentence, a second line, and
 *  the key's console path. */
export type ApiKeyNotice = {
  text: string
  detail: string
  href: string
}

/** One bell entry (`routes/inbox.py::InboxItem`); `kind` decides which optional fields are set. */
export type InboxItem = {
  kind: InboxKind
  id: string
  created_at: string | null
  read_at: string | null
  task_id?: number | null
  task_title?: string | null
  /** Set instead of `task_id` for a mention (or reaction) in a support-ticket comment. */
  ticket_id?: number | null
  ticket_title?: string | null
  /** A support item's ticket status (`support_ticket`, `support_task_done`). */
  ticket_status?: string | null
  comment_id?: number | null
  /** A mention's commenter; a reaction's REACTOR. */
  author_name?: string | null
  /** The comment's opening words — for a reaction, the comment reacted to. */
  excerpt?: string | null
  /** A reaction's `lib/reactions.ts` key. */
  emoji?: string | null
  request?: AccessRequest | null
  /** A `billing` item's notice; `id` is the notice's. */
  notice?: BillingNotice | null
  /** A `trigger_failed` / `trigger_digest` / `automation_alert` item's notice; `id` is the notice's. */
  trigger_notice?: TriggerNotice | null
  /** An `api_key_expiring` item's reminder; `id` is the key's. */
  api_key_notice?: ApiKeyNotice | null
}

export type RequestBox = 'pending' | 'mine' | 'history'

const SCOPE_TYPES: readonly RequestScopeType[] = ['org', 'project', 'account']

function isScopeType(value: unknown): value is RequestScopeType {
  return typeof value === 'string' && (SCOPE_TYPES as readonly string[]).includes(value)
}

/** The hint a refusal carries, or null — for any other error, and for a body that does not have the shape
 *  (a cross-org or nonexistent id carries none at all, `request_hint`). */
export function hintOf(err: unknown): RequestHint | null {
  if (!(err instanceof ApiError) || (err.status !== 403 && err.status !== 404)) return null
  const request = (err.body as { request?: unknown } | null)?.request as
    | { scope?: { type?: unknown; id?: unknown }; role?: unknown }
    | null
    | undefined
  const scope = request?.scope
  if (!scope || !isScopeType(scope.type) || (typeof scope.id !== 'string' && typeof scope.id !== 'number')) return null
  return {
    scope: { type: scope.type, id: String(scope.id) },
    role: typeof request.role === 'string' ? request.role : null,
  }
}

/* ── The route → resource map ──────────────────────────────────────────────────────────────────────
   Which resource a console address is about. Most specific first; `new` and `login` are forms, not
   resources. Org routes are mapped but never probed: an org role is handed out, not asked for. */
const ROUTES: readonly { pattern: RegExp; resource: (m: RegExpMatchArray) => RequestScope | null }[] = [
  { pattern: /^\/projects\/(\d+)\/?$/, resource: m => ({ type: 'project', id: m[1] }) },
  { pattern: /^\/accounts\/([^/]+)\/?$/, resource: m => (m[1] === 'new' ? null : { type: 'account', id: m[1] }) },
  { pattern: /^\/orgs\/([^/]+)\/?$/, resource: m => ({ type: 'org', id: m[1] }) },
]

export function resourceForPath(pathname: string): RequestScope | null {
  for (const { pattern, resource } of ROUTES) {
    const m = pathname.match(pattern)
    if (m) return resource(m)
  }
  return null
}

/** Ask the API whether `resource` is one the caller may request, and at which role.
 *
 * A project is asked through an object-scoped read, whose refusal carries the API's own hint (the least
 * role, same-org only). An account has no such read, so it asks `GET /api/access/members`,
 * which resolves the scope under the caller's own org: 403 means the scope exists there and is not
 * readable (requestable; the API picks the least role), 404 means another org's or none (no hint). A
 * success means the caller already reads it — nothing to request. That route's floor is `projects:view`:
 * without it every id answers 403, another org's included, so `holdsMembersFloor` false asks nothing. */
export async function probeHint(resource: RequestScope, holdsMembersFloor: boolean): Promise<RequestHint | null> {
  if (resource.type === 'org') return null
  if (resource.type !== 'project' && !holdsMembersFloor) return null
  const path =
    resource.type === 'project'
      ? `/api/projects/${encodeURIComponent(resource.id)}/members`
      : `/api/access/members?scope=${encodeURIComponent(`${resource.type}:${resource.id}`)}`
  try {
    await get<unknown>(path)
    return null
  } catch (err) {
    if (resource.type === 'project') return hintOf(err)
    return err instanceof ApiError && err.status === 403 ? { scope: resource, role: null } : null
  }
}

/* ── The wire ────────────────────────────────────────────────────────────────────────────────────── */

/** A request box is the state of asks and decisions the reader just made or is waiting on: re-read on
 *  every mount, never served from the app's 15s-fresh persisted cache (`queryClient.ts`). */
export const REQUESTS_STALE_MS = 0

export const listRequests = (box: RequestBox) => get<AccessRequest[]>(`/api/access/requests?box=${box}`)

export const createRequest = (body: { scope: RequestScope; role?: string | null; note?: string | null }) =>
  post<AccessRequest>('/api/access/requests', {
    scope: body.scope,
    ...(body.role ? { role: body.role } : {}),
    ...(body.note ? { note: body.note } : {}),
  })

/** `expiresAt` null → never (A2: an approval is not forced to be time-boxed). */
export const approveRequest = (id: string, expiresAt: string | null) =>
  post<AccessRequest>(`/api/access/requests/${encodeURIComponent(id)}/approve`, { expires_at: expiresAt })

export const denyRequest = (id: string) => post<AccessRequest>(`/api/access/requests/${encodeURIComponent(id)}/deny`, {})

export const INBOX_LIMIT = 50

/** The most `GET /api/inbox` returns (`comment_mentions.MAX_READ_IDS`): the Notifications page's last page. */
export const INBOX_MAX = 200

/** `unreadOnly` false is the Unread | All switch's All: read items of the kinds that keep a read stamp too
 *  (`routes/inbox.py`'s docstring). */
export const getInbox = (unreadOnly = true, limit = INBOX_LIMIT) =>
  get<InboxItem[]>(`/api/inbox?unread_only=${unreadOnly}&limit=${limit}`)

/** `all.mentions` / `all.reactions` mark EVERY unread one of that kind read server-side — not only the ids
 *  on screen, which may be capped at `INBOX_LIMIT` or stale. A flag is sent only when set. */
export const markSeen = (
  items: { kind: InboxKind; id: string }[],
  all: { mentions?: boolean; reactions?: boolean } = {},
) =>
  post<unknown>('/api/inbox/seen', {
    items,
    ...(all.mentions ? { all_mentions: true } : {}),
    ...(all.reactions ? { all_reactions: true } : {}),
  })

/** "Mark as unread": clears the read stamp of the listed items; a kind with no stamp is skipped server-side. */
export const markUnseen = (items: { kind: InboxKind; id: string }[]) => post<unknown>('/api/inbox/unseen', { items })

/** The longest note the API accepts (`access_requests.NOTE_MAX`). */
export const NOTE_MAX = 500

/* ── Presentation ────────────────────────────────────────────────────────────────────────────────── */

const TYPE_LABEL: Record<RequestScopeType, string> = {
  org: 'Organization',
  project: 'Project',
  account: 'Account',
}

export const scopeTypeLabel = (type: RequestScopeType) => TYPE_LABEL[type] ?? 'Resource'

/** What a request is for, in words: its name when the caller may see it, else its kind alone. */
export function scopeLabel(scope: AccessRequest['scope']): string {
  if (scope.name) return `${scopeTypeLabel(scope.type)} ${scope.name}`
  const noun = scopeTypeLabel(scope.type).toLowerCase()
  return `${/^[aeiou]/.test(noun) ? 'an' : 'a'} ${noun}`
}

/** Where a decided request's scope opens, or null when the console has no page for it. */
export function scopeHref(scope: { type: RequestScopeType; id: string }): string | null {
  if (scope.type === 'project') return `/projects/${encodeURIComponent(scope.id)}`
  if (scope.type === 'account') return `/accounts/${encodeURIComponent(scope.id)}`
  return null
}

/** Does the caller decide requests anywhere? Mirrors `access.may_manage`'s holders: an org admin seat, or a
 *  Project Maintainer somewhere — the one project role holding `projects:edit`. Presentation only: the
 *  Pending box is the server's own answer. */
export function managesAccess(admin: Admin | null | undefined): boolean {
  if (!admin) return false
  if (isOrgAdminSeat(admin)) return true
  return Object.values(admin.project_permissions ?? {}).some(keys => keys.includes('projects:edit'))
}
