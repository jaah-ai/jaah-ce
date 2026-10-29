// The Access surface's wire: role bindings at a scope, the Add dialog's picker, the role catalog, and a
// credential's pool. Mirrors `apps/api/app/routes/access.py` (members/bindings/principals/roles) and
// `apps/api/app/routes/credentials/` (a credential's access + project attach/detach) — those modules
// own what each field means; this one owns only the request and response shapes the web reads.

import { del, get, post } from '../../api'

/** The scope types a binding can name (`app/access.py::SCOPES`). A project id is an integer, the rest
 *  uuids — carried as strings here, since both are only ever interpolated. */
export type ScopeType = 'org' | 'project' | 'account'

export type AccessScope = { type: ScopeType; id: string }

export type PrincipalKind = 'user' | 'group'

/** One row of `GET /api/access/members`. `source.kind`: `direct` (bound here), `inherited` (bound on an
 *  enclosing scope, named by `via_*`) or `group` (a member of the group named by `via_*`). */
export type MemberRow = {
  principal: { kind: PrincipalKind; id: string; name: string; member_count: number | null }
  role: { slug: string; name: string }
  source: { kind: 'direct' | 'inherited' | 'group'; via_id: string | null; via_name: string | null }
  binding_id: string
  expires_at: string | null
  status: 'active' | 'expired'
  last_active_at: string | null
  editable: boolean
}

/** One built-in role (`GET /api/access/roles`). `capabilities` are human sentences, never raw keys. */
export type AccessRole = {
  id: string
  slug: string
  name: string
  description: string
  level: string
  rank: number
  valid_scopes: string[]
  capabilities: string[]
}

/** One entry of the Add dialog's picker (`GET /api/access/principals`). */
export type PickerEntry = {
  kind: PrincipalKind
  id: string
  name: string
  email?: string | null
  member_count?: number | null
}

/** `GET /api/credentials/{id}/access`: who owns the credential and which projects it is attached to. */
export type CredentialAccess = {
  owner: { id: string; name: string } | null
  /** `home`: the credential's home project — listed, never detached (the API 409s); its Home select moves it. */
  projects: { id: number | string; name: string; home?: boolean }[]
  /** The control plane's own row: never attached to a project. Optional so older payloads parse. */
  system?: boolean
}

/** The `<type>:<id>` query form the read routes take. */
export const scopeParam = (scope: AccessScope) => encodeURIComponent(`${scope.type}:${scope.id}`)

export const listMembers = (scope: AccessScope) =>
  get<MemberRow[]>(`/api/access/members?scope=${scopeParam(scope)}`)

export const listRoles = () => get<AccessRole[]>('/api/access/roles')

/** The roles a binding at `scope` may carry, highest rank first (the catalog's own order). */
export const rolesForScope = (roles: AccessRole[], scope: ScopeType) =>
  roles.filter(role => role.valid_scopes.includes(scope))

export const searchPrincipals = (scope: AccessScope, q: string) =>
  get<PickerEntry[]>(`/api/access/principals?scope=${scopeParam(scope)}&q=${encodeURIComponent(q)}`)

/** One submit, one binding per principal, all or nothing (`routes/access.py::add_bindings`). `headers`: an operator's
 *  one-request act-as on another org's scope (`lib/actAs.ts::actAsOnce`), as the two below. */
export const addBindings = (body: {
  principals: { kind: PrincipalKind; id: string }[]
  role: string
  scope: AccessScope
  expires_at?: string | null
}, headers?: HeadersInit) =>
  headers ? post<unknown[]>('/api/access/bindings', body, headers) : post<unknown[]>('/api/access/bindings', body)

export const removeBinding = (bindingId: string, headers?: HeadersInit) => {
  const path = `/api/access/bindings/${encodeURIComponent(bindingId)}`
  return headers ? del(path, headers) : del(path)
}

export const getCredentialAccess = (credentialId: string) =>
  get<CredentialAccess>(`/api/credentials/${encodeURIComponent(credentialId)}/access`)

/* `headers`: an operator's one-request act-as on another org's credential (`lib/actAs.ts::actAsOnce`). */
export const attachCredential = (projectId: number | string, credentialId: string, headers?: HeadersInit) => {
  const path = `/api/projects/${encodeURIComponent(String(projectId))}/credentials`
  return headers ? post<unknown>(path, { credential_id: credentialId }, headers) : post<unknown>(path, { credential_id: credentialId })
}

export const detachCredential = (projectId: number | string, credentialId: string, headers?: HeadersInit) => {
  const path = `/api/projects/${encodeURIComponent(String(projectId))}/credentials/${encodeURIComponent(credentialId)}`
  return headers ? del(path, headers) : del(path)
}

/** The role the Add dialog starts on (the DM1 path): Project Developer, where the scope carries it. */
export const DEFAULT_ROLE_SLUG = 'project_developer'

// ── The principal side: what a user reaches, Check access, and a group's members ────────────────────

/** One binding of `GET /api/access/reaches/{user_id}` (`app/access.py::_binding_out`). `source.kind` is
 *  `direct` or `group` — reach carries no `inherited` rows; inheritance is the grouping itself. */
export type ReachBinding = {
  binding_id: string
  role: { slug: string; name: string }
  scope: { type: ScopeType; id: string; name: string }
  source: { kind: 'direct' | 'group'; via_id: string | null; via_name: string | null }
  expires_at: string | null
  status: 'active' | 'expired'
}

export type ReachScope = { id: string; name: string; bindings: ReachBinding[] }

/** `GET /api/access/reaches/{user_id}`: live bindings grouped org → project, plus accounts. */
export type Reaches = {
  org: ReachBinding[]
  projects: ReachScope[]
  accounts: ReachScope[]
}

export const getReaches = (userId: string) =>
  get<Reaches>(`/api/access/reaches/${encodeURIComponent(userId)}`)

/** A scope as the explainer names it (`app/access.py::Scope.wire`). */
export type WireScope = { type: ScopeType; id: string; name: string }

/** One step of a Check-access chain (`app/access.py::explain`). */
export type ChainNode =
  | { kind: 'user' | 'group' | 'operator' | 'owner'; id: string; name: string }
  | {
    kind: 'binding'
    binding_id: string
    role: string
    scope: WireScope
    expires_at: string | null
    status: 'active' | 'expired'
  }
  | { kind: 'inherited'; via: WireScope }
  | ({ kind: 'resource' } & WireScope)

/** `POST /api/access/check`. `chain` is the deciding path when allowed; `expired` the paths a lapsed
 *  binding would have given (never a path); `missing` the nearest missing link when denied. */
export type CheckResult = {
  allowed: boolean
  action_label: string
  chain: ChainNode[]
  expired: ChainNode[][]
  missing: { reason: string; scope: WireScope; message: string } | null
}

export const checkAccess = (body: {
  user_id: string
  resource: { type: ScopeType; id: string }
  action: { feature: string; action: string }
}) => post<CheckResult>('/api/access/check', body)

/** One route class of the permission catalog (`GET /api/permissions`). `description` is the human label;
 *  `key` is never shown. `feature_label` names the feature as a grid row ("credential accounts"); `honoured` is
 *  false for a key no route enforces yet, which a grant screen omits. */
export type CatalogAction = {
  key: string
  feature: string
  action: string
  description: string
  feature_label: string
  honoured: boolean
}

export const listActions = () => get<CatalogAction[]>('/api/permissions')

export const addGroupMember = (groupId: string, userId: string) =>
  post<unknown>(`/api/groups/${encodeURIComponent(groupId)}/members/${encodeURIComponent(userId)}`, {})

export const removeGroupMember = (groupId: string, userId: string) =>
  del(`/api/groups/${encodeURIComponent(groupId)}/members/${encodeURIComponent(userId)}`)
