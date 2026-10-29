// The Credentials surface's API client, shared by the list and add/edit pages.
//
// Types describe the WIRE only; there is deliberately no per-type field table. Types and fields come from
// `GET /api/credentials/types` (`useCredentialTypes`) because the server registry
// (`apps/api/app/credentials/registry.py`) also decides which keys are secret and stripped.

import { del, get, patch, post, put } from '../../api'
import { fetchSealKey, type CredentialSealKey, type SealedSecretEnvelope } from '../../lib/credentialSeal'
import { nodeVaultSealKey } from '../../lib/nodeVaultKeys'
import type { CredentialHealth, CredentialSort } from '../../lib/credentialFilterStorage'
import type { Account, ClaudeResources, OrgRef } from '../../types'
import type { CredentialOption } from '../../ui/CredentialSelect'

/* The seal key and the envelope live in `lib/credentialSeal.ts` (the construction's browser half);
 * re-exported so this client is the one import for the credentials surface. */
export { type CredentialSealKey, type SealedSecretEnvelope } from '../../lib/credentialSeal'
export const getCredentialSealKey = fetchSealKey

/* ── The Runners roster (`Accounts.tsx`): two reads, each a react-query key its writes invalidate ──── */

/** `GET /api/resources` — the roster's rows. Shared with `components/WorktreePool.tsx`'s account badges. */
export const RESOURCES_KEY = ['resources'] as const
export const getResources = () => get<ClaudeResources>('/api/resources')

/** `GET /api/accounts` — the records joined onto the roster by `account_id` (name, description, org). */
export const ACCOUNTS_KEY = ['accounts'] as const
export const listAccounts = () => get<Account[]>('/api/accounts')

/** `PUT /api/accounts/{id}` `{ enabled }` — the enable write, ONE body for its homes: the roster's inline toggle
 *  and ⋯ item (`Accounts.tsx`) and the account page's switch (`AccountEditPage.tsx`). */
export async function setAccountEnabled(accountId: string, enabled: boolean, headers?: HeadersInit): Promise<void> {
  // `headers`: an operator's one-request act-as on another org's account (`lib/actAs.ts::actAsOnce`).
  await (headers ? put(`/api/accounts/${accountId}`, { enabled }, headers) : put(`/api/accounts/${accountId}`, { enabled }))
}

/* ── NodeVault (wire contract: `apps/api/app/routes/nodevault.py`) ──────────────────────────────────── */

/** The org's NodeVault state as the credential form reads it. `active` is the only state that lets a
 *  credential BECOME NodeVault; `frozen` = set up, then dropped from the plan (existing ones keep working). */
export type NodeVaultState = 'not_setup' | 'active' | 'frozen' | 'not_in_plan'

export type NodeVaultStateInfo = {
  state: NodeVaultState
  current_key_hash: string | null
  /** Per type slug: may a credential of this type be NodeVault, and if not, the one-line reason. */
  types: Record<string, { eligible: boolean; reason: string | null }>
  /** Why Test is withheld for a NodeVault credential (the server's sentence). */
  untestable_reason: string
}

export type NodeVaultEligibility = {
  eligible: boolean
  reason: string | null
  /** The control-plane consumers that open this credential (each blocks NodeVault). */
  consumers: string[]
}

/* `headers` on the reads and writes below: an operator's one-request act-as on another org's row
 * (`lib/actAs.ts::actAsOnce`), so the org's own NodeVault state and key answer, and the write lands there. */
export const getNodeVaultState = (headers?: HeadersInit) =>
  get<NodeVaultStateInfo>('/api/credentials/nodevault-state', headers ? { headers } : undefined)

export const getNodeVaultEligibility = (id: string, headers?: HeadersInit) =>
  get<NodeVaultEligibility>(`/api/credentials/${id}/nodevault-eligibility`, headers ? { headers } : undefined)

/** The org's CURRENT NodeVault public key, in the seal-key shape `sealSecret` takes. The `key_id` is
 *  rebuilt from the public key (`nodeVaultSealKey`), never taken from the server. Same secure-context guard
 *  as the KMS key. */
export async function getNodeVaultSealKey(headers?: HeadersInit): Promise<CredentialSealKey> {
  if (typeof crypto === 'undefined' || !crypto.subtle) {
    throw new Error('WebCrypto is unavailable: secrets cannot be encrypted in this browser session')
  }
  const served = await get<CredentialSealKey>('/api/nodevault/seal-key', headers ? { headers } : undefined)
  return nodeVaultSealKey(served.public_key)
}

/* The credential types that can authenticate a git remote — the Git Access picker's filter.
 * A mirror of `apps/api/app/git_access.py::GIT_CREDENTIAL_TYPES` (the authority); drift can only
 * mis-OFFER, since every write is re-checked server-side (422). `github_app` is deliberately absent: it
 * authenticates no git remote. */
export const GIT_CREDENTIAL_TYPES: ReadonlySet<string> = new Set(['github', 'gitlab', 'bitbucket', 'azure_devops'])

/** `secret` is a security classification, not just an input type — see the registry module. */
export type FieldKind = 'text' | 'url' | 'email' | 'secret'

export type FieldSpec = {
  key: string
  label: string
  kind: FieldKind
  required: boolean
  placeholder?: string | null
  /** The value a NEW row opens with (the registry's `FieldSpec.default`, e.g. the AWS `region`); never
   *  applied to a stored row. Absent/null = opens blank. */
  default?: string | null
  /** Helper text under the input — an instruction (the registry's `FieldSpec.help`); a placeholder is only an example. */
  help?: string | null
}

/** The control-plane OAuth2 flow that can FILL a credential type: it runs the auth-code flow against the
 *  client stored in the row, so the token is refreshable (`CredentialTypeSpec.oauth_connect` is the single home). Display facts only;
 *  null for tenant callers. `provider_label` is null when the vendor is unknowable (generic OAuth2) —
 *  word around the action then. */
export type OAuthConnectSpec = {
  slug: string
  action_label: string
  provider_label: string | null
  /** The exact redirect URI the operator must register with the vendor's OAuth client; a wrong one
   *  fails only at `exchange_failed` after consent. Same for every type; built by
   *  `routes/credentials/credential_wire.py::_connect_redirect_uri`. Public, not a secret. */
  redirect_uri: string
}

export type CredentialTypeSpec = {
  slug: string
  label: string
  /** False for a type no connection test could make a true claim about — its Test returns the
   *  `unsupported` state. */
  testable: boolean
  /** The two inputs of the sealing destination (`lib/credentialSeal.ts::sealingDestination`, mirroring
   *  `registry.sealing_destination`): the key of the operator's host field (null for a fixed-host type)
   *  and the type's default host (null for a type with none). */
  host_field: string | null
  default_base_url: string | null
  /** Why this type has no connection test — server-owned text, rendered verbatim. Absent-but-untestable
   *  only for a RETIRED type synthesised from a row (`retiredTypeOption`). */
  untestable_reason?: string | null
  fields: FieldSpec[]
  /** Absent for every type with no control-plane connect flow. */
  oauth_connect?: OAuthConnectSpec | null
  /** Groups of OPTIONAL field keys; a row saves once one group is wholly filled. The form renders these
   *  fields outright, beside the required ones. Empty or absent for most types. */
  require_one_of?: string[][]
}

/* Deliberately shadows `lib.dom.d.ts`'s global `Credential`; nothing uses `navigator.credentials`. If
 * something ever does, alias THIS type at the import. */
export type Credential = {
  id: string
  name: string
  type: string
  /** The human label ("Jira"), server-derived. The UI never renders the raw slug. */
  type_label: string
  /** Never null (NOT NULL `''`; PATCH `null` clears to `''`). Optional so older API payloads parse. */
  description?: string
  /** Secret keys are ABSENT from this object — omitted by the server, never masked. */
  config: Record<string, string>
  /** Which secret keys currently hold a stored value — the only thing that may claim "leave blank". */
  secrets_set: string[]
  created_at: string | null
  updated_at: string | null
  /** Resolved display NAMES, never ids. `null` where the actor row was deleted, rendered as `NO_VALUE`
   *  (`lib/credentialRow.tsx`). */
  created_by?: string | null
  updated_by?: string | null
  /** Whose row it is, by name: a personal row's owner (who may not be its creator), else its creator
   *  (`CredentialOut.owner`). `null` when unresolved; absent on an older API. */
  owner?: string | null
  /* The org pair has three states (single home: `apps/api/app/routes/credentials/credential_wire.py::CredentialOut`):
   * `(null, null)` no org, `(set, set)` resolved, `(set, null)` an id naming no `orgs` row (no FK) — render
   * the id (`ui/OrgIndicator.tsx`'s `unresolvedId`). Optional so older API payloads parse. */
  org_id?: string | null
  org?: OrgRef | null
  /** The server's verdict on whether this caller's PATCH would be admitted (`credentials:edit`,
   *  superadmin, or creator). Served by every `_credential_out` call site, write echoes included.
   *  Only `false` changes what a page draws; absent/null means "not asked" and falls back to the
   *  caller's permission key. */
  editable?: boolean | null
  /** Who the row belongs to. Optional so older API payloads parse. */
  ownership?: CredentialOwnership
  /** Sealed under the org's NodeVault key: only the org's nodes can open its secrets. */
  nodevault?: boolean
  /** That key's hash (`hex(sha256(SPKI DER))`), read off the envelopes' own `k`; null for a KMS row. */
  nodevault_key_hash?: string | null
  /** An org row's home project (`CredentialOut.home_project_id`), else null; its name rides `home_project`. */
  home_project_id?: number | null
  home_project?: string | null
  /** The home as the server computes it — the name's locked prefix (`[org]`, `[org, 'system']`, `[org, project]`,
   *  `[org, owner email]`). Optional so older API payloads parse; absent draws no prefix. */
  home_path?: string[]
  /* Health (`CredentialOut`): machinery stamps, never edits. Optional so older API payloads parse; absent reads as
   * never used / never tested / no expiry. */
  last_used_at?: string | null
  last_tested_at?: string | null
  /** The last connection test's verdict; null when never tested. */
  last_test_ok?: boolean | null
  expires_at?: string | null
}

export type CredentialList = {
  items: Credential[]
  /** Needed so "Next" can know the last page. Keyset paging caps it (`total_capped`). */
  total: number
  /** Keyset paging: more rows match than `total` counts ("10,000+"). Absent on the OFFSET path. */
  total_capped?: boolean
  /** Keyset paging: the next page's `?cursor=`, null on the last page. Absent on the OFFSET path. */
  next_cursor?: string | null
  page: number
  per: number
}

export type CredentialTestResult = {
  ok: boolean
  status: 'success' | 'failure' | 'unsupported'
  message: string
  detail?: string | null
}

/** A create/update body. A non-secret `config` value is a string; a SECRET is a `SealedSecretEnvelope`
 *  the browser produced (never plaintext); an explicit `null` DELETES that key server-side. */
export type CredentialBody = {
  name: string
  type: string
  description: string | null
  config: Record<string, string | SealedSecretEnvelope | null>
  /** Create only: the row's UUID, minted by the browser BEFORE sealing (every envelope's AAD carries
   *  it). Refused on PATCH. */
  id?: string
  /** Create only, and only from an Org Owner/Admin or operator: who owns the row. Absent = personal. */
  ownership?: CredentialOwnership
  /** Create only, with `personal`: the org member it is for (`authz.validated_owner_user`). Absent = the caller. */
  owner_user_id?: string
  /** The NodeVault checkbox. Every secret in `config` must be sealed under the matching key: the org's
   *  NodeVault key when true (`getNodeVaultSealKey`), the deployment's KMS key when false. */
  nodevault?: boolean
  /** An org row's home project (create and PATCH); `null` homes it on the org itself. */
  home_project_id?: number | null
}

/** Who a credential belongs to (`routes/credentials/credential_wire.py::CredentialOut.ownership`): its creator alone, the
 *  org, or the control plane (`system` — never on a task's roster). */
export type CredentialOwnership = 'personal' | 'org' | 'system'


export const PER_PAGE = 25

/* The server's ceiling on `per` (`routes/credentials/`: `le=200`; more 422s) — the single home. Pickers
 * use it to get everything in one page, but it is a ceiling, not "all": callers must surface overflow. */
export const PICKER_PAGE = 200

/* The cap said out loud: a disabled last option when `total` exceeds what was shown, so a short list is
 * not read as "the credential doesn't exist". `total` is the server's count for the same filter; merged
 * pages sum their totals. The sentinel id is never pickable. Returns null when nothing was missed. */
export const OVERFLOW_OPTION_ID = '__overflow__'

export function overflowOption(total: number, shown: number): CredentialOption | null {
  const missed = total - shown
  if (missed <= 0) return null
  return { id: OVERFLOW_OPTION_ID, name: `…and ${missed} more`, hint: 'narrow the search', disabled: true }
}

/* Every picker's `fetchOptions`: one `PICKER_PAGE` per type (default: one untyped request), merged by id,
 * narrowed by `keep`, mapped by `toOption`, then the overflow row for what the server did not send (a row
 * `keep` drops is not "more": narrowing the search would never show it). */
export async function loadCredentialOptions(
  query: string,
  signal: AbortSignal,
  toOption: (row: Credential) => CredentialOption,
  { types = [undefined], keep = rows => rows, sort = false }: {
    types?: (string | undefined)[]
    keep?: (rows: Credential[]) => Credential[]
    sort?: boolean
  } = {},
): Promise<CredentialOption[]> {
  const pages = await Promise.all(types.map(type => listCredentials(1, PICKER_PAGE, { type, q: query, signal })))
  const kept = keep(pages.flatMap(page => page.items))
  const rows = [...new Map<string, CredentialOption>(kept.map(row => [row.id, toOption(row)])).values()]
  if (sort) rows.sort((a, b) => a.name.localeCompare(b.name))
  const overflow = overflowOption(
    pages.reduce((sum, page) => sum + page.total, 0),
    pages.reduce((sum, page) => sum + page.items.length, 0),
  )
  return overflow ? [...rows, overflow] : rows
}

/* One roster page, narrowed server-side. `type`/`q` apply to the page AND `total`, so pickers can report
 * what they missed; an unknown `type` is a 422. `signal` lets `ui/AsyncLookup` abandon superseded keystrokes.
 *
 * `mine` must be server-side: the wire carries only `created_by` display NAMES, which are not unique, so
 * no honest client filter exists. It is orthogonal to permission (usable from /profile) and grants nothing. */
export const listCredentials = (
  page: number,
  per: number = PER_PAGE,
  opts: {
    type?: string
    q?: string
    mine?: boolean
    /* The roster strip's narrowings: server-only, applied to page and `total`. None has a
       client form (actor ids not on the wire, an encrypted secrets column). No `org_id`: an operator's org
       narrowing rides the global filter's header (`lib/orgScope.ts`, R5.6). */
    /** Owners' user uuids, from `GET /api/credentials/owners` — the only route that yields one. Repeated
     *  `?owner_id=`, the roster their union. */
    ownerIds?: readonly string[]
    /** `'yes'`/`'no'`; a server `Literal`, so an unrecognised value 422s. */
    secrets?: 'yes' | 'no' | ''
    /** The server-side order; omitted for the route's default (`newest`). */
    sort?: CredentialSort
    /** Expired, expiring, never used or last test failed; server-side like the other narrowings. */
    health?: CredentialHealth | ''
    /** Keyset paging (the roster): `page` is ignored, `cursor` (a previous answer's `next_cursor`) moves on, and
     *  `total` comes back capped. Pickers stay on `page`. */
    keyset?: boolean
    cursor?: string | null
    signal?: AbortSignal
  } = {},
) => {
  const params = opts.keyset
    ? new URLSearchParams({ per: String(per), paging: 'keyset' })
    : new URLSearchParams({ page: String(page), per: String(per) })
  if (opts.keyset && opts.cursor) params.set('cursor', opts.cursor)
  // Omitted when empty: `?type=` is not a known type.
  if (opts.type) params.set('type', opts.type)
  if (opts.q) params.set('q', opts.q)
  // Load-bearing: an empty `?owner_id=` or `?secrets=` 422s the page. Unset must be ABSENT.
  for (const ownerId of opts.ownerIds ?? []) params.append('owner_id', ownerId)
  if (opts.secrets) params.set('secrets', opts.secrets)
  if (opts.health) params.set('health', opts.health)
  // Only ever `true`, so existing callers' URLs stay byte-identical (two suites assert them verbatim).
  if (opts.mine) params.set('mine', 'true')
  // The default is not sent, for the same byte-identical reason.
  if (opts.sort && opts.sort !== 'newest') params.set('sort', opts.sort)
  const url = `/api/credentials?${params}`
  // Omit the second argument when there is no signal: suites assert the call without a trailing `undefined`.
  return opts.signal === undefined
    ? get<CredentialList>(url)
    : get<CredentialList>(url, { signal: opts.signal })
}

/** The roster narrowings `listCredentialTree` sends, as `listCredentials` does. */
export type CredentialRosterFilters = {
  type?: string
  q?: string
  mine?: boolean
  ownerIds?: readonly string[]
  secrets?: 'yes' | 'no' | ''
  health?: CredentialHealth | ''
}

/* ── Tree mode, one level per request (`routes/credentials/tree.py`) ──────────────────────
 * Each node carries the count of credentials beneath it under the filters, capped at 1,000 (`count_capped`). */
export type CredentialTreeCount = { count: number; count_capped: boolean }
export type CredentialTenantNode = CredentialTreeCount & { org_id: string; name: string }
export type CredentialHomeNode = CredentialTreeCount & {
  /** The `home` of this home's folder requests. */
  key: string
  kind: 'org' | 'system' | 'project' | 'user'
  label: string
}
/** One segment, lowercase as stored; a child's `path` is its parent's plus `/name`. */
export type CredentialFolderNode = CredentialTreeCount & { name: string }
export type CredentialTreeLevel = {
  tenants: CredentialTenantNode[]
  homes: CredentialHomeNode[]
  folders: CredentialFolderNode[]
  leaves: Credential[]
  /** The same level's next page; null on the last. */
  next_cursor: string | null
}

/** Where a tree request reads: an operator's tenants, one org's homes (a tenant's root: no `orgId`), or one
 *  folder of a home (`path` `''` at its top). */
export type CredentialTreePlace =
  | { level: 'tenants' }
  | { level: 'homes'; orgId?: string }
  | { level: 'folder'; home: string; path: string }

export function listCredentialTree(
  place: CredentialTreePlace,
  filters: CredentialRosterFilters,
  { cursor, signal }: { cursor?: string | null; signal?: AbortSignal } = {},
) {
  const params = new URLSearchParams({ level: place.level })
  if (place.level === 'homes' && place.orgId) params.set('org_id', place.orgId)
  if (place.level === 'folder') {
    params.set('home', place.home)
    params.set('path', place.path)
  }
  if (cursor) params.set('cursor', cursor)
  // Unset must be ABSENT, as for the list: an empty `?owner_id=` or `?secrets=` 422s.
  if (filters.type) params.set('type', filters.type)
  if (filters.q) params.set('q', filters.q)
  for (const ownerId of filters.ownerIds ?? []) params.append('owner_id', ownerId)
  if (filters.secrets) params.set('secrets', filters.secrets)
  if (filters.health) params.set('health', filters.health)
  if (filters.mine) params.set('mine', 'true')
  return get<CredentialTreeLevel>(`/api/credentials/tree?${params}`, signal ? { signal } : undefined)
}

export const listCredentialTypes = () => get<CredentialTypeSpec[]>('/api/credentials/types')

/* ── The roster strip's owner option list ───────────────────────────────────────────────────────────
 * Gated more narrowly than the roster (a roster of people is not a roster of rows —
 * `routes/credentials/`): without `credentials:view`, `/owners` 403s while the list answers. So the
 * strip shows `mine` to everyone and the owner picker only when answered. Searched as the operator types
 * (`q`, at most `limit`, prefix matches first) and never narrowed by active filters, which would delete the
 * option needed to widen them; `ids` turns stored selections back into names. */

/** An Owner-filter entry (`routes/credentials/reads.py::CredentialOwnerOut`). `id` is the `?owner_id=`
 *  value. `credential_count` counts the caller's UNFILTERED roster, not the current page; on a search it stops
 *  at the cap, `count_capped` then true. */
export type CredentialOwner = {
  id: string
  name: string
  credential_count: number
  count_capped?: boolean
}

/** No options: the route's whole owner list. Any of `q`, `limit`, `ids`: its search (at most 50). */
export const listCredentialOwners = (
  opts: { q?: string; limit?: number; ids?: readonly string[]; signal?: AbortSignal } = {},
) => {
  const params = new URLSearchParams()
  if (opts.q) params.set('q', opts.q)
  if (opts.limit !== undefined) params.set('limit', String(opts.limit))
  for (const id of opts.ids ?? []) params.append('ids', id)
  const query = params.toString()
  const url = query ? `/api/credentials/owners?${query}` : '/api/credentials/owners'
  return opts.signal === undefined ? get<CredentialOwner[]>(url) : get<CredentialOwner[]>(url, { signal: opts.signal })
}

export const getCredential = (id: string, opts?: RequestInit) => get<Credential>(`/api/credentials/${id}`, opts)

export const createCredential = (body: CredentialBody) =>
  post<Credential>('/api/credentials', body)

export const updateCredential = (id: string, body: CredentialBody, headers?: HeadersInit) =>
  headers ? patch<Credential>(`/api/credentials/${id}`, body, headers) : patch<Credential>(`/api/credentials/${id}`, body)

/* Name and keys only (the connection dialog's "Edit credential"): the PATCH is partial, so the type,
 * description and NodeVault flag the form never shows stay as stored — a sent type that differed would
 * reset the config and drop the kept secrets. */
export const updateCredentialKeys = (id: string, body: Pick<CredentialBody, 'name' | 'config'>) =>
  patch<Credential>(`/api/credentials/${id}`, body)

/* Rename only. A full `CredentialBody` from a list row would lack the secret keys the browser
 * never sees; the PATCH is a genuine partial update (`update_credential` leaves absent fields and config
 * keys untouched), so sending only `name` cannot lose anything. Same endpoint and gate. */
export const renameCredential = (id: string, name: string, headers?: HeadersInit) =>
  headers ? patch<Credential>(`/api/credentials/${id}`, { name }, headers) : patch<Credential>(`/api/credentials/${id}`, { name })

/* ── Bulk actions (`routes/credentials/bulk.py`) ────────────────────────────────────────────────────────
 * ONE call per action over the whole set, all or nothing. The set is `ids` (rows picked) or `match` (the roster's
 * filters: every row they list); `expect_count` is the count the confirm named, so a set that changed since is
 * refused (409) rather than acted on. A refusal's `detail` carries `message` (`apiDetail` shows it). */

/** The most rows one bulk action takes (`bulk.BULK_CAP`). */
export const BULK_CAP = 10_000

/** The roster's filters on the wire (`roster.RosterFilterSet`): the same names as the list's query parameters. */
export type CredentialMatch = {
  type?: string
  q?: string
  owner_id?: string[]
  org_id?: string
  secrets?: 'yes' | 'no'
  health?: CredentialHealth
  mine?: boolean
}

export type CredentialBulkSet = { ids: string[] } | { match: CredentialMatch }

export const bulkDeleteCredentials = (set: CredentialBulkSet, expectCount: number) =>
  post<{ count: number }>('/api/credentials/bulk/delete', { ...set, expect_count: expectCount })

/** `to`: the folder every row lands in, keeping its own last name segment; empty is the top level. */
export const bulkMoveCredentials = (set: CredentialBulkSet, expectCount: number, to: string) =>
  post<{ count: number }>('/api/credentials/bulk/move-folder', { ...set, expect_count: expectCount, to })

/** Operator only. */
export const bulkChangeCredentialOrg = (set: CredentialBulkSet, expectCount: number, orgId: string) =>
  post<{ count: number }>('/api/credentials/bulk/change-org', { ...set, expect_count: expectCount, org_id: orgId })

/** `headers`: an operator's one-request act-as on another org's row (`lib/actAs.ts::actAsOnce`). */
export const deleteCredential = (id: string, headers?: HeadersInit) =>
  headers ? del(`/api/credentials/${id}`, headers) : del(`/api/credentials/${id}`)

/**
 * Test the connection as the form stands; nothing is saved. `id` travels when the record is saved (a
 * blank secret falls back to the stored one) or the config holds a sealed envelope (it opens only under
 * the id it was sealed for); an unsaved form with nothing sealed sends none, because an `id` beside no
 * envelope is a LOOKUP that 404s for a row the caller may not edit. A completed test is always 200; only
 * a malformed request is 4xx.
 */
export const testCredential = (body: {
  type: string
  config: Record<string, string | SealedSecretEnvelope | null>
  id?: string
}) => post<CredentialTestResult>('/api/credentials/test', body)


/* ── The control-plane connect: one call, then the browser leaves ───────────────────────
 * The tab goes to the provider and the control plane finishes; the outcome returns as a query param on
 * `/credentials/{id}`, read by `CredentialEditPage`.
 */

/** `POST /api/credentials/connect/start/{id}`'s answer. The server's echoed labels are deliberately not
 *  mirrored: the page navigates away at once and would never render them (panel labels come from
 *  `CredentialTypeSpec.oauth_connect`). Refusals are `HTTPException`s read with `apiDetail`. */
export type CredentialConnectStart = {
  authorize_url: string
}

/* Begin the control-plane flow for one saved row. POST because it mints per-attempt CSRF `state`.
 * The id is the LAST path segment on purpose — do not move it to `/api/credentials/{id}/connect/start`:
 * the PKCE cookie's `Path` must be the narrowest shared prefix (`/api/credentials/connect`,
 * `routes/credentials/connect.py::_CONNECT_COOKIE_PATH`). */
export const startCredentialConnect = (credentialId: string) =>
  post<CredentialConnectStart>(`/api/credentials/connect/start/${credentialId}`, {})


/** One account's Git Access roster (`GET /api/users/{id}/git-credentials`, superadmin-only;
 *  `routes/auth.py::GitCredentialOut`). Identity only, no config or secrets. */
export type GitCredential = {
  id: string
  name: string
  type: string
  /** The human label from the server's registry. The UI never renders the raw slug. */
  type_label: string
}

/* Git credentials the EDITED account can reach — not the reading admin's roster, which would let a
 * superadmin store a selection the user cannot use. The server re-applies the scope on write. */
export const listUserGitCredentials = (userId: string) =>
  get<{ credentials: GitCredential[] }>(`/api/users/${userId}/git-credentials`)

/** `POST /api/git-connect/start`'s answer (`routes/git_connect.py::GitConnectStartOut`). "No GitHub OAuth
 *  App configured" is a 503 rejection read via `apiDetail`, not a field. */
export type GitConnectStart = {
  authorize_url: string
}

/* ── Git Access per forge (`routes/git_connections.py`) ───────────────────────────────────────────────
 * SELF-SERVICE ONLY: every route 404s another user's id, so only /profile calls these. `root` is a
 * `forges.RepoRef.root` (github.com, gitlab.com, bitbucket.org, dev.azure.com, or a self-managed GitLab
 * `host[/sub-path]`), URL-encoded into the path. */

/** `GitConnectOffer`: whether Connect (OAuth) works for this root; `reason` names what to configure. */
export type GitConnectOffer = { available: boolean; reason: string | null }

/** `GitConnectionOut`. `credential` is the connected (or revoked) row, `null` when nothing is connected. */
export type GitConnection = {
  root: string
  forge: string
  forge_label: string
  self_managed: boolean
  state: 'connected' | 'needs_reconnect' | 'not_connected'
  /** Resolution's own sentence on why the root is not usable. */
  detail: string | null
  credential: GitCredential | null
  connect: GitConnectOffer
}

/** The pasted-token body, per forge (`routes/git_connections.py::_pat_config`): Bitbucket adds the
 *  account `username`, Azure DevOps the `test_org` it is tested against. */
export type GitPatBody = { token: string; username?: string; test_org?: string }

const gitConnectionsPath = (userId: string) => `/api/users/${userId}/git-connections`
const rootPath = (userId: string, root: string) => `${gitConnectionsPath(userId)}/${encodeURIComponent(root)}`

export const listGitConnections = (userId: string) =>
  get<{ connections: GitConnection[] }>(gitConnectionsPath(userId))

export const putGitConnection = (userId: string, root: string, credentialId: string) =>
  put<GitConnection>(rootPath(userId, root), { credential_id: credentialId })

export const deleteGitConnection = (userId: string, root: string) => del(rootPath(userId, root))

/* Tested at the forge first: a refused token is a 422 carrying the tester's reason. */
export const postGitConnectionPat = (userId: string, root: string, body: GitPatBody) =>
  post<GitConnection>(`${rootPath(userId, root)}/pat`, body)

/* The credentials that authenticate exactly `root` (`routes/users.py::list_user_git_credentials`). Behind
 * `users:view`, so a member without it is refused and the caller falls back to its own roster. */
export const listGitCredentialsForRoot = (userId: string, root: string) =>
  get<{ credentials: GitCredential[] }>(`/api/users/${userId}/git-credentials?root=${encodeURIComponent(root)}`)

/** The console pages a GitHub Connect may come back to (`routes/git_connect.py::RETURN_PATHS`). */
export type GitConnectReturnTo = '/profile' | '/git-connect/done'

/* Begin a forge's OAuth Connect (`routes/forge_connect.py`; `github` aliases `POST /api/git-connect/start`).
 * A forge whose app is not configured answers 409 with the offer's sentence. `returnTo` is GitHub's only;
 * the default is Profile. */
export const startForgeConnect = (forge: string, returnTo?: GitConnectReturnTo, app?: 'oauth_app') =>
  post<GitConnectStart>(`/api/git-connect/${encodeURIComponent(forge)}/start`, {
    ...(returnTo ? { return_to: returnTo } : {}), ...(app ? { app } : {}),
  })

/** `routes/git_connect.py::GitReachOwner`: one GitHub owner behind a project the viewer may view, probed
 *  with the viewer's own token. An owner GitHub's answer cannot classify is omitted, never listed. */
export type GitReachOwner = {
  owner: string
  reach: 'ok' | 'approval_required' | 'sso_required' | 'install_required'
}

/** `GET /api/git-connect/reach` (`routes/git_connect.py::GitReachOut`). Branch on `state` only:
 *  `'unknown'` renders NOTHING; only `'ok'` makes the other fields meaningful. OAuth App tokens:
 *  `approve_url` is GitHub's page where the user requests org approval or authorizes SSO. GitHub App tokens:
 *  `install_url` is the App's install page (owners install, non-owners request approval there), and
 *  `installed_count` the installations the token reaches (`0` = none at all). */
export type GitReach = {
  state: 'ok' | 'unknown'
  approve_url: string | null
  install_url: string | null
  installed_count: number | null
  owners: GitReachOwner[]
}

/* Whether an org's app restrictions, SSO, or a missing App install block the signed-in user's GitHub token.
 * Re-read after a connect returns: approval, installs and SSO authorization happen on github.com with no notification. */
export const getGitReach = () => get<GitReach>('/api/git-connect/reach')
