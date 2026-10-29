import { ApiError, apiDetail, post } from '../api'
import { clearAuthSnapshot } from '../authStore'
import { dropPersistedQueries } from '../queryClient'
import type { OrgRef } from '../types'
import { announceOrgSwitch, pinTabOrg } from './activeOrg'
import { IP_SHAPE } from './ipAllowlist'

/* `POST /api/auth/switch` for the switcher (`account/OrgSwitcherList`), the interstitial (`OtherOrgGate`) and the
 * invitation page: makes the navigations all make alike and returns the rest to render; a refusal throws, worded by
 * `switchRefusal`. `leaving`: the page is navigating away, so its controls stay disabled until it unloads. */
export type FactorError = 'bad_factor' | 'mfa_throttled'
type Door = 'otp' | 'enrol' | 'google' | 'microsoft' | 'github' | 'sso'
type SwitchOut = { outcome: 'session' | 'reauth_required'; org?: OrgRef; door?: Door; start_url?: string; error?: FactorError }

/** Why a typed code did not switch. */
export const FACTOR_ERRORS: Record<FactorError, string> = {
  bad_factor: "That code didn't work. Try again.",
  mfa_throttled: 'Too many attempts. Wait a minute, then try again.',
}

const PROVIDER_NAMES: Record<string, string> = { google: 'Google', microsoft: 'Microsoft', github: 'GitHub' }

/** An `idp` answer this page cannot follow, as a sentence. `sso`: the org enforces its own SAML/OIDC IdP, whose
 *  door is the sign-in page's "Sign in with SSO" (`/login?sso=1`, `pages/Login.tsx::SSO_LOGIN_PATH`). */
export function idpSentence(org: string, door?: Door): string {
  if (door === 'sso') return `${org} needs SSO. Sign out, then use Sign in with SSO to switch.`
  const provider = PROVIDER_NAMES[door ?? ''] ?? 'your identity provider'
  return `${org} needs a ${provider} sign-in. Sign out, then sign in with ${provider} to switch.`
}

/** One switch refusal (what `switchOrg` threw) as a sentence the person can act on. */
export function switchRefusal(err: unknown, org: string): string {
  if (err instanceof ApiError && err.status === 404) return `You are no longer a member of ${org}.`
  if (err instanceof ApiError && err.status === 409 && err.detail === 'org_pending') return `${org} is awaiting approval.`
  if (err instanceof ApiError && err.status === 409 && err.detail === 'org_not_approved') return `${org} was not approved.`
  if (err instanceof ApiError && err.status === 409 && err.detail === 'ip_not_allowed') {
    const ip = (err.body as { ip?: unknown } | null)?.ip
    const address = typeof ip === 'string' && IP_SHAPE.test(ip) ? `this address: ${ip}` : 'your current address'
    return `${org} only allows access from approved networks. Ask its admin to add ${address}.`
  }
  return apiDetail(err, `Could not switch to ${org}.`)
}

export async function switchOrg(orgId: string, code?: string): Promise<
  { kind: 'session'; org?: OrgRef } | { kind: 'otp'; error?: FactorError } | { kind: 'leaving' } | { kind: 'idp'; door?: Door }
> {
  const out = await post<SwitchOut>('/api/auth/switch', code ? { org_id: orgId, code } : { org_id: orgId })
  if (out.outcome === 'session') {
    // The snapshot is the org left behind: painted on the reload, its `is_operator` would mount operator-only
    // reads (the dashboard, the org roster) in a tenant org until `/me` answers. Load cold instead.
    clearAuthSnapshot()
    return { kind: 'session', org: out.org }
  }
  if (out.door === 'otp') return { kind: 'otp', error: out.error }
  // No authenticator to type a code from: set one up; its confirm signs in as the target org. A start_url
  // is followed same-origin only: the server builds it (`routes/auth.py::switch_org`); never anything else.
  const next = out.door === 'enrol' ? '/login/mfa/enrol' : out.start_url
  if (!next?.startsWith('/') || next.startsWith('//')) return { kind: 'idp', door: out.door }
  window.location.assign(next)
  return { kind: 'leaving' }
}

/** Into `org` once `switchOrg` answered `session`: the switcher's and the invitation page's one landing
 *  (`landingPathAfterSwitch`). Re-pinned
 *  first, so a request of this tab still in flight never reads as a stale tab's; the persisted cache is the org
 *  left behind, never restored into the new one. */
export function enterOrg(org: OrgRef, { stay = false }: { stay?: boolean } = {}): void {
  pinTabOrg(org.id)
  announceOrgSwitch(org)
  dropPersistedQueries()
  // `stay`: a page that already knows the target org holds what was asked for reloads this very URL there.
  if (stay) window.location.reload()
  else window.location.assign(landingPathAfterSwitch(window.location.pathname))
}

const ENTITY_ID = /^(\d+|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})$/i
/* Two-segment parents whose next segment names one entity by key, not by id. */
const KEYED_PARENTS = new Set([
  'compliance/repos', 'compliance/rules', 'compliance/packs', 'nodes/hosts', 'audit/events', 'support/tickets',
  'workflows/runs', 'accounts/login',
])
/* Sections with no list page of their own. */
const NO_LIST = new Set(['features', 'invitations'])

/** Where `enterOrg` lands: the same section, never the old org's entity. A section path (`/tasks`,
 *  `/compliance`, `/settings/...`) is kept; a path naming one entity (an id segment, or a keyed child)
 *  falls back to its top-level section, else `/`. The query is dropped: it may name an entity. The target org's
 *  route guards handle a section it cannot open. */
export function landingPathAfterSwitch(pathname: string): string {
  const segs = pathname.split('/').filter(Boolean)
  const keyed = segs.length > 2 && KEYED_PARENTS.has(segs.slice(0, 2).join('/'))
  // `/workflows/<name>` names one workflow by its name.
  const entity = keyed || segs.some(seg => ENTITY_ID.test(seg)) || (segs[0] === 'workflows' && segs.length === 2 && segs[1] !== 'new')
  if (!entity) return `/${segs.join('/')}`
  return NO_LIST.has(segs[0]) ? '/' : `/${segs[0]}`
}
