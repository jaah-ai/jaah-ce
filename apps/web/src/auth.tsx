import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from 'react'
import { del, get, post, put } from './api'
import type { PlanPrice } from './lib/orgPlan'
import {
  AUTH_EXPIRED_EVENT,
  clearAuthSnapshot,
  loadAuthSnapshot,
  saveAuthSnapshot,
} from './authStore'
import { clearAllTaskDrafts } from './lib/taskDraftStorage'
import { clearTaskPageSeeds } from './lib/taskPageSeed'
import { actingAs, exitActAs, useActingAs } from './lib/actAs'
import { pinTabOrg } from './lib/activeOrg'
import { bindOrgScopeViewer } from './lib/orgScope'
import { isOrgAdminSeat } from './lib/orgAdmin'
import { colorSchemeFor, subscribeSystemColorScheme } from './lib/colorScheme'
import { queryClient, removePersistedQueries } from './queryClient'
import { forgetLoginReturn } from './lib/loginReturn'
import type {
  MeOrgRef,
  Membership,
  OrgMembership,
  OrgRef,
  PendingInvitation,
  PermissionKey,
  ProjectCreateDefaults,
  TaskCreateDefaults,
} from './types'

export type PreferencesChange =
  | Record<string, unknown>
  | ((current: Record<string, unknown>) => Record<string, unknown>)

export type Admin = {
  /* A uuid string: every comparison must be string-to-string, or `admin.id === user.id` is silently
  false and the self-delete/self-demote guards (`pages/users/UsersTab.tsx`, `UserEditPage`'s `isSelf`)
  open up. */
  id: string
  email: string
  role: 'admin' | 'member'
  first_name: string | null
  last_name: string | null
  preferences?: Record<string, unknown>
  /* The credential this operator's runners push with, or null. On the identity payload so
  /profile renders it without `credentials:view`. A pointer, never a capability: the server re-authorizes
  the row on every resolution. Optional because `loadAuthSnapshot` may replay an `Admin` from a build
  that predates it (as for every optional field below). */
  git_credential_id?: string | null
  /* The viewer's own org, or null while none is bound — for pages with no record of their own
  (`/projects/new`, `/tasks/new`, `/clients`). It is NOT where a new record will land: that stamp is
  `write_seam.stamp_org`'s, off the scope `authz_guard` binds per request, so announce it as the
  viewer's own only. */
  org?: MeOrgRef | null
  /* The org this tab acts as (`lib/actAs.ts`), or null: the operator then sees `org`'s Owner seat — not
  operator, not superadmin — while `memberships` stay the operator's own, so the session org stays pinned.
  Absent reads as not acting. */
  acting_as?: OrgRef | null
  version: string
  /* The bypass flag. NOT A CONSOLE GATE: read `lib/orgAdmin.ts::isOrgAdminSeat` (tenant administration) or `is_operator`
  (operator plane) instead — the census in `lib/orgAdmin.test.ts` pins every raw read that remains. */
  is_superadmin: boolean
  /* `is_superadmin` AND an operator-plane member, computed by `routes/auth.py::AdminOut.is_operator`.
  The gate for operator-only surfaces (`App.tsx`'s `RequireSuperadmin`, the rail's `operatorOnly` rows).
  Absent reads as NOT operator. */
  is_operator?: boolean
  /* The viewer's org is a tenant's (`routes/auth.py::AdminOut.tenant`). Absent reads as tenant. */
  tenant?: boolean
  /* The org role an org binding confers (`routes/auth.py::AdminOut.org_role`): `'owner'`, `'admin'` or
  null. Read through `isOrgAdmin`, never directly. Absent reads as none. */
  org_role?: 'owner' | 'admin' | null
  /* The orgs this person is a member of, the current one first (`routes/auth.py::OrgMembershipOut`). */
  memberships: OrgMembership[]
  /* Invitations into other orgs awaiting this person's answer (`routes/auth.py::PendingInvitationOut`),
  answered from the account menu. Optional: an older snapshot predates it. */
  pending_invitations?: PendingInvitation[]
  /* This membership's group memberships. */
  group_memberships?: Membership[]
  permissions: string[]
  /* Not this user's settings: the operator's "New task" defaults, here because their catalogue is
  `system:view`-gated (`routes/auth.py::TaskCreateDefaultsOut`). Consumed via
  `components/tasks/taskDefaults::useTaskCreateDefaults`, which owns the fences and the absent fallback. */
  task_create_defaults?: TaskCreateDefaults
  /* The org's new-project gate commands (`Projects > Defaults`). Read by `ProjectEditPage`'s create form;
  absent reads as blank. */
  project_create_defaults?: ProjectCreateDefaults
  /* The project-scoped half of `permissions`: project id (decimal string) -> sorted feature keys held ON
  that project. A strict same-subject-path intersection: a key counts on P only if the same subject (the
  user, or one group) also holds the object-grant on P, so it cannot be re-derived by crossing
  `permissions` with a project list. `{}` for the operator (answer from `isOrgAdminSeat`) and for no
  grants. Absent reads as deny-all. */
  project_permissions?: Record<string, string[]>
  /* The control plane's IANA zone, already resolved (`cron_runtime.resolved_timezone_name()`); here
  because its knob is `system:view`-gated. Consumed via `lib/officialZone::useOfficialZone`. */
  timezone?: string
  /* Full-editor autosave interval in seconds (`operator_settings.task_autosave_seconds`, per org).
  Consumed via `lib/taskAutosaveInterval::useTaskAutosaveSeconds`, which owns fallback and clamp. */
  task_autosave_seconds?: number
  /* Is the task editor's Node field on for this org (`routes/auth.py::AdminOut.task_node_pin_enabled`)? Read by
  `components/tasks/TaskSpecFields::taskNodePinShown`; absent = off. An operator has the field regardless. */
  task_node_pin_enabled?: boolean
  /* The largest file the org may upload, bytes (`org_quotas.file_cap_bytes`). Read only through
  `lib/maxFileBytes::useMaxFileBytes`, which owns the ceiling and the absent fallback. */
  max_file_bytes?: number
  /* The operator's default theme, for a user whose own `preferences.theme` is unset. Consumed via
  `lib/colorScheme::colorSchemeFor`, which owns the rule and the absent fallback. */
  default_theme?: 'light' | 'dark'
  /* The org's plan switches (`plan_terms`: the org's override, else its billing plan's; the operator org too).
  Read only through `lib/planSwitches.ts`; absent = off. Presentation only: the API refuses on its own.
  `playbooks_enabled` — playbooks; `jaah_ai_enabled` — the Jaah AI account. */
  playbooks_enabled?: boolean
  /* Org-authored task templates (`routes/auth.py::AdminOut.org_templates_enabled`). */
  org_templates_enabled?: boolean
  jaah_ai_enabled?: boolean
  /* The org's provider grid (the plan term `providers`): the `[row, auth]` cells it may use, Agents and ACP registry
  agents among them (`sdk`, `acp:<id>`). Read only through `lib/providerGrid.ts`; absent = nothing on. */
  providers?: string[][]
  /* Does this principal's org have Portfolio Compliance (`routes/auth.py::AdminOut.compliance_enabled`, the
  plan flag)? Read by the rail's Portfolio Compliance row and its route guard, `RequireCompliance`; absent = off.
  Presentation only: `/api/compliance/*` answers 404 on its own while it is off. */
  compliance_enabled?: boolean
  /* Does this session still owe the Terms acceptance (`routes/auth.py::AdminOut.terms_pending`)? Every other API
  answers 403 `terms_required` until it is accepted, so `App.tsx::RequireAuth` opens `/terms/accept` first. */
  terms_pending?: boolean
  /* Is the Architectural Guard available to this principal's org (`routes/auth.py::AdminOut.guard_enabled`:
  Compliance AND Advanced Compliance)? Guard UI shows only when true; absent = off. Presentation only: every guard
  API answers 404 on its own while it is off. */
  guard_enabled?: boolean
  /* Does this principal's org have NodeLink (`routes/auth.py::AdminOut.nodelink_enabled`, the plan term; true on
  the operator plane)? Gates the node wizard's NodeLink box, the node's NodeLink switch and a connection's EFS
  fields; absent = off. Presentation only: the writes refuse on their own. */
  nodelink_enabled?: boolean
  /* May this principal's org read NodeVault at all (`routes/auth.py::AdminOut.nodevault_enabled`: allowed, set up
  or mid-operation)? Settings' NodeVault panel skips its `/api/nodevault` read while off, rather than earn a 403;
  absent = off. Presentation only: the routes refuse on their own. */
  nodevault_enabled?: boolean
  /* MicroVM nodes and GitHub Actions runners on the org's plan (`routes/auth.py::AdminOut.microvm_allowed`,
  `github_runners_allowed`: true while the plan's `github_runners` cap is not 0). Read through `lib/planSwitches.ts`:
  a tenant's MicroVM and GitHub Runners tabs hide while off; absent = off. Presentation only: the writes refuse on their own. */
  microvm_allowed?: boolean
  github_runners_allowed?: boolean
  /* Which self-hosted node OS the org's plan lets it register (`routes/auth.py::AdminOut.ubuntu_nodes_allowed`,
  `macos_nodes_allowed`, `windows_nodes_allowed`). Read through `lib/planSwitches.ts::NODE_OS_SWITCH`: the node
  wizard hides an OS choice while its switch is off; absent = off. Presentation only: the API refuses on its own. */
  ubuntu_nodes_allowed?: boolean
  macos_nodes_allowed?: boolean
  windows_nodes_allowed?: boolean
  /* Project memory on the org's plan (`routes/auth.py::AdminOut.project_memory_enabled`). Read through
  `lib/planSwitches.ts`: a tenant's project Memory tab and memory knobs hide while off; absent = off. Presentation
  only: the memory routes refuse on their own. */
  project_memory_enabled?: boolean
  /* Audit's Activity tab and Sessions tab on the org's plan (`routes/auth.py::AdminOut.audit_enabled`,
  `session_transcripts_enabled`). Read through `lib/planSwitches.tsx`: a tenant's tab and its routes hide while off;
  absent = off. Presentation only: the API refuses on its own. */
  audit_enabled?: boolean
  session_transcripts_enabled?: boolean
  /* Your Cloud per provider on the org's plan (`routes/auth.py::AdminOut.aws_cloud_allowed`, …). Read through
  `lib/planSwitches.tsx` (`CLOUD_SWITCH`); the API refuses a provider that is off on its own. */
  aws_cloud_allowed?: boolean
  gcp_cloud_allowed?: boolean
  azure_cloud_allowed?: boolean
  /* Tenant email on the org's plan (`routes/auth.py::AdminOut.tenant_email_allowed`): Settings › Organizations ›
  Email hides while off. Read through `lib/planSwitches.tsx`; the API refuses a connect on its own. */
  tenant_email_allowed?: boolean
  /* The enterprise plan switches (`routes/auth.py::AdminOut`, `app/plan_terms.py::SWITCHES`): Slack, SSO, SCIM, SIEM
  export, data residency, IP allowlist, retention, staging org, budgets. Read through `lib/planSwitches.tsx`; absent =
  off. Presentation only: each door answers 403 on its own. */
  slack_enabled?: boolean
  sso_enabled?: boolean
  scim_enabled?: boolean
  siem_export_enabled?: boolean
  data_residency_enabled?: boolean
  ip_allowlist_enabled?: boolean
  org_retention_enabled?: boolean
  staging_org_enabled?: boolean
  budgets_enabled?: boolean
  /* Is this principal's org in HIPAA mode (`routes/auth.py::AdminOut.org_hipaa`)? Absent = off. Presentation only:
  every HIPAA safeguard is enforced server-side. */
  org_hipaa?: boolean
  /* The non-BAA surfaces this principal's org is warned about, each with whether an org admin acknowledged it
  (`routes/auth.py::AdminOut.hipaa_warnings`); empty while HIPAA is off. Read through `components/NonBaaWarning`.
  Absent reads as none. */
  hipaa_warnings?: { surface: string; acknowledged: boolean }[]
  /* Static worktrees this principal's org's plan lets a project hold on one node (`AdminOut.static_slots`): 0 = none,
  null = no cap (the operator plane). At 0 every control that adds a static slot is hidden or disabled (`staticSlotsOffered`);
  presentation only, the writes refuse on their own. */
  static_slots?: number | null
  /* Notification channels this deployment can deliver to now — `['slack']` or `[]`
  (`notify_targets.available_notification_methods`). A deployment capability, not a preference (one
  `slack` credential per deployment), so `/users/:id` reads it here too. Carries no credential id or
  name: this payload reaches every principal. Absent reads as "none available". */
  notification_methods_available?: string[]
  /* The events this user may see and set in Profile > Alerts (`app/preferences.subscribable_events`): billing and
  the node alerts are an org Owner's or Admin's. Absent (an older snapshot) lists every event. */
  notification_events?: string[]
  /* The login email was proven by an SSO sign-in (Google, Microsoft, GitHub); email notifications go only
  to such an address. Absent reads as unknown — no caption. */
  email_verified?: boolean
}

/* A first factor's 202: no session yet, only the pending-MFA cookie. `/login/mfa*` finishes the sign-in. */
export type MfaStage = 'verify' | 'enrol'
export const mfaPath = (stage: MfaStage) => (stage === 'enrol' ? '/login/mfa/enrol' : '/login/mfa')
type MfaDue = { mfa: MfaStage }
const isMfaDue = (answer: unknown): answer is MfaDue =>
  typeof answer === 'object' && answer !== null && 'mfa' in answer

/* What POST /api/auth/signup/google answers: signed straight in (an existing user, or a domain join), a
second factor due, or a new applicant sent on to the profile at `/signup` (the pending sign-up cookie). */
export type SignupNext =
  | { next: 'signed_in' }
  | { next: 'mfa'; mfa: MfaStage }
  | { next: 'apply' }

/* ── Self-service sign-up: profile → (email: code → password) → plan → (paid: billing → card) → Sign up ──
The doors are the API's `/api/signup/...` routes; the signup cookie authenticates every one but `email/start`. A
field problem is 422 `{invalid_fields: {field: message}}`. Only an auto-approved Sign up signs anyone in. */

export type AccountType = 'individual' | 'organization'

/** The profile page's answers. `org_name` only for an Organization; `email` only on the email path (a provider's is
 *  the cookie's). */
export type SignupProfile = {
  account_type: AccountType | null
  first_name: string
  last_name: string
  email: string
  country: string
  occupation: string
  org_name: string
  intended_use: string
  accept_terms: boolean
  legal_version: string
}

/** What `/email/start` and `/email/resend` answer — the same for every address, account or not. */
export type EmailSent = { next: 'verify'; email: string; resend_after: number }
/** Where a sign-up goes next (`signup_draft.STEPS`, plus `apply`: a profile still to give). */
export type SignupStep = 'verify' | 'password' | 'apply' | 'profile' | 'plan' | 'billing' | 'payment'
export type EmailVerified = { next: SignupStep }

export const signupEmailStart = (profile: SignupProfile, planKey: string | null) =>
  post<EmailSent>('/api/auth/signup/email/start', { profile, plan_key: planKey })
export const signupEmailResend = (email: string) => post<EmailSent>('/api/auth/signup/email/resend', { email })
export const signupEmailVerify = (email: string, code: string) =>
  post<EmailVerified>('/api/auth/signup/email/verify', { email, code })
export const signupEmailVerifyLink = (token: string) =>
  post<EmailVerified>('/api/auth/signup/email/verify-link', { token })
export const signupEmailPassword = (password: string) =>
  post<{ next: SignupStep }>('/api/auth/signup/email/password', { password })

/** GET /api/auth/signup/plans and `pending.plans`: one plan an applicant may choose (`app/signup_plans.py::offered`).
 *  `price` null is Free; `duration_days` null never ends; `products` the bundled products, in words; `trial_days`
 *  the free trial a purchase opens with; `paid` whether it charges once it starts. */
export type SignupPlan = {
  key: string
  name: string
  blurb: string
  price: PlanPrice | null
  duration_days: number | null
  products: string[]
  trial_days: number | null
  paid: boolean
}
export const signupPlans = () => get<SignupPlan[]>('/api/auth/signup/plans')

/** A billing address as the Customer holds it. */
export type SignupAddress = {
  line1: string
  line2?: string | null
  city: string
  state?: string | null
  postal_code: string
  country: string
}
/** The billing step's details: `company` for an Organization, `tax_id` an EU VAT number. */
export type SignupBilling = {
  name: string
  email: string
  company?: string | null
  address: SignupAddress | null
  tax_id: { type: string; value: string } | null
}

/** GET /api/auth/signup/pending: where this browser's sign-up stands. `entry` is how it started; `provider` a display
 *  label ("Google", "Email"); `publishable_key` what Stripe.js needs once `billing_connected`. */
export type PendingSignup = {
  step: SignupStep
  entry: 'email' | 'google' | 'github' | 'microsoft'
  email: string
  provider: string
  first_name: string
  last_name: string
  suggested_org_name: string | null
  needs_password: boolean
  profile: Partial<Record<keyof SignupProfile, string | boolean | null>>
  plan_key: string | null
  plans: SignupPlan[]
  billing: SignupBilling | null
  billing_connected: boolean
  publishable_key: string | null
}
/* The signed-out probe (`apps/api/app/auth.py::SIGNED_OUT_PROBE_HEADER`): with no live cookie for what it reads, the
   API answers 204 (`undefined` here) rather than a 401 the browser logs as a failed request on every first visit. */
const SIGNED_OUT_PROBE = { headers: { 'X-Jaah-Signed-Out-Ok': '1' } }
/** `undefined`: this browser carries no signup cookie. */
export const signupPending = () => get<PendingSignup | undefined>('/api/auth/signup/pending', SIGNED_OUT_PROBE)
/** Drop this browser's signup cookie ("Use a different account"): the next `/pending` is a fresh start. */
export const signupForget = () => del('/api/auth/signup/pending')

/** A proven identity's profile (provider path, or Back from the plan): the draft created or brought up to date. */
export const signupProfile = (profile: SignupProfile, planKey: string | null) =>
  post<{ next: 'plan' }>('/api/auth/signup/profile', { profile, plan_key: planKey })

/** The plan's terms, as the Sign up page words them: never a hard-coded day count or price. */
export type SignupTerms = {
  plan_key: string
  paid: boolean
  trial_days: number | null
  price: PlanPrice | null
  interval: string | null
}
export const signupChoosePlan = (planKey: string) => put<SignupTerms>('/api/auth/signup/plan', { plan_key: planKey })
export const signupSaveBilling = (details: SignupBilling) =>
  put<{ ok: true }>('/api/auth/signup/billing', { details })
/** The card step's SetupIntent. The secret lives in the page's state only: never stored or logged. */
export const signupSetupIntent = () => post<{ client_secret: string }>('/api/auth/signup/setup-intent', {})
/** Sign up: `activating` while the org awaits review, or signed in (auto-approved) with the second factor due. */
export type SignupDone = { next: 'activating' } | { next: 'signed_in'; stage: MfaStage | 'session' }
export const signupComplete = (legalVersion: string) =>
  post<SignupDone>('/api/auth/signup/complete', { legal_version: legalVersion })

/** POST /api/orgs/self-create's answers (the account menu's "Create organization"). Only the answers the account
 *  type asks for are sent. */
export type SignupApplication = {
  account_type: AccountType
  first_name: string
  last_name: string
  occupation?: string
  job_title?: string
  org_name?: string
  intended_use: string
  accept_terms: boolean
  legal_version: string
}

type AuthState = {
  admin: Admin | null
  loading: boolean
  /* Has this page load's first `GET /api/auth/me` settled? Distinct from `loading`, which is false at
  first paint when a snapshot exists. A snapshot can be stale downward (a new grant), so a guard HOLDS
  while `resolving` rather than denying. Holding is never granting — see `RequireFeature`. */
  resolving: boolean
  /** Resolves the second factor still due, or null when signed in. */
  /** `totp`: the step-up code, sent only once the API asked for one (`pages/Login.tsx::stepUpMessage`). */
  login: (email: string, password: string, totp?: string) => Promise<MfaStage | null>
  loginWithGoogle: (credential: string) => Promise<MfaStage | null>
  /** Adopt the identity a `/login/mfa*` page received with its session. */
  completeSignIn: (admin: Admin) => void
  /** Self-service sign-up with a Google credential; on `signed_in` the session is already adopted. */
  signupWithGoogle: (credential: string) => Promise<SignupNext>
  logout: () => Promise<void>
  refresh: () => Promise<void>
  /** Resolves `true` when the save landed, `false` when it was reverted — see the implementation. */
  /** A slice, or an updater given the LATEST stored preferences — for a slice built from what is already there
   *  (an onboarding step appended to the list), so two saves from one render never drop each other's. */
  updatePreferences: (changes: PreferencesChange) => Promise<boolean>
}

const AuthContext = createContext<AuthState>({
  admin: null,
  loading: true,
  resolving: true,
  login: async () => null,
  loginWithGoogle: async () => null,
  completeSignIn: () => {},
  signupWithGoogle: async () => ({ next: 'signed_in' }),
  logout: async () => {},
  refresh: async () => {},
  updatePreferences: async () => false,
})

/** Whose global org filter applies: an operator's own id, nobody's otherwise (`lib/orgScope.ts`). */
const orgScopeViewer = (admin: Admin | null): string | null => (admin?.is_operator === true ? admin.id : null)

const actingId = (): string | null => actingAs()?.id ?? null

/** Adopt a live `/api/auth/me` answer to a read sent while acting as `sent` (null: not acting). Dropped when act-as
 *  has moved since — the change's own read answers. An acting read answered without `acting_as` (the session is
 *  no longer an operator's) leaves act-as, which reads again. */
function adoptIdentity(sent: string | null, answer: Admin, setAdmin: (next: Admin) => void): void {
  if (actingId() !== sent) return
  if (sent !== null && !answer.acting_as) exitActAs()
  else setAdmin(answer)
}

export function AuthProvider({ children }: { children: ReactNode }) {
  /* The snapshot is shared by every tab and is the operator's: a tab acting as an org (`lib/actAs.ts`) never
     seeds from it, and so starts loading until its live answer lands. */
  const snapshot = actingAs() ? null : loadAuthSnapshot()
  /* The global org filter's viewer, bound before the first request so a warm reload's first reads already
     carry the remembered scope (`lib/orgScope.ts`). Idempotent, so StrictMode's double call is harmless. */
  const [admin, setAdminState] = useState<Admin | null>(() => {
    bindOrgScopeViewer(orgScopeViewer(snapshot))
    return snapshot
  })
  const [loading, setLoading] = useState(snapshot === null)
  // Always starts true — see `resolving` on AuthState.
  const [resolving, setResolving] = useState(true)
  // The identity the persisted react-query cache (and the unsaved task draft) was populated under; a
  // logout or different admin clears both so they never outlive their session.
  const cachedForIdRef = useRef<string | null>(snapshot?.id ?? null)
  /* THE LATEST IDENTITY, outside the render: `updatePreferences` merges against it, so two quick saves
     from one render (a banner's ✕ and the guide's step record) never revert each other's slice. */
  const latestAdminRef = useRef<Admin | null>(admin)
  const saveSeqRef = useRef(0)

  const setAdmin = useCallback((next: Admin | null) => {
    const nextId = next?.id ?? null
    if (nextId !== cachedForIdRef.current) {
      queryClient.clear()
      clearTaskPageSeeds()
      // An act-as tab never persists (`queryClient.ts`); the stored copy is the operator's own, so leave it.
      if (!actingAs()) removePersistedQueries()
      clearAllTaskDrafts()
    }
    cachedForIdRef.current = nextId
    latestAdminRef.current = next
    /* Every caller hands a LIVE answer (the snapshot seeds `useState` directly), so this is the one place
       the org this tab acts as is pinned (`lib/activeOrg.ts`). */
    pinTabOrg(next ? currentOrgId(next) : null)
    bindOrgScopeViewer(orgScopeViewer(next))
    setAdminState(next)
    if (next) {
      // Never the acting tab's Owner seat: the next tab to load would paint it.
      if (!actingAs()) saveAuthSnapshot(next)
    } else {
      clearAuthSnapshot()
      exitActAs()
    }
  }, [])

  /* Every page load, and again whenever this tab enters or leaves act-as (`lib/actAs.ts`): the shell holds until
     the identity it now acts as lands. A read act-as has since moved past settles nothing. The hold is DERIVED in
     the render that sees the change (`readFor` trails `acting` until its read settles), not set in the effect: the
     shell's queries would otherwise refetch the cleared cache under the new header with the old identity's gates
     (an operator's counts, 403 as the Owner seat). Never a render-phase setState: it leaves `useSyncExternalStore`
     holding a stale snapshot, so the NEXT act-as change never re-renders this provider (Exit after an in-page
     Act as then kept the Owner seat, routed to /no-access and stalled the next page's queries). */
  const acting = useActingAs()?.id ?? null
  const [readFor, setReadFor] = useState(acting)
  useEffect(() => {
    const current = () => actingId() === acting
    get<Admin | undefined>('/api/auth/me', SIGNED_OUT_PROBE)
      .then(answer => {
        if (answer) adoptIdentity(acting, answer, setAdmin)
        else if (current()) setAdmin(null)
      })
      .catch(() => {
        if (current()) setAdmin(null)
      })
      .finally(() => {
        if (!current()) return
        setReadFor(acting)
        setLoading(false)
        setResolving(false)
      })
  }, [acting, setAdmin])

  useEffect(() => {
    const onExpired = () => setAdmin(null)
    window.addEventListener(AUTH_EXPIRED_EVENT, onExpired)
    return () => window.removeEventListener(AUTH_EXPIRED_EVENT, onExpired)
  }, [setAdmin])

  // Apply the admin's colour scheme to the document root (`[data-theme="dark"]`); `lib/colorScheme.ts` owns the rule.
  // Signed out, the page follows the browser and repaints when the system scheme flips.
  useEffect(() => {
    const paint = () => {
      document.documentElement.dataset.theme = colorSchemeFor(admin)
    }
    paint()
    return admin ? undefined : subscribeSystemColorScheme(paint)
  }, [admin])

  const login = useCallback(
    async (email: string, password: string, totp?: string) => {
      const body = totp === undefined ? { email, password } : { email, password, totp }
      const answer = await post<Admin | MfaDue>('/api/auth/login', body)
      if (isMfaDue(answer)) return answer.mfa
      setAdmin(answer)
      return null
    },
    [setAdmin],
  )

  const loginWithGoogle = useCallback(
    async (credential: string) => {
      const answer = await post<Admin | MfaDue>('/api/auth/google', { credential })
      if (isMfaDue(answer)) return answer.mfa
      setAdmin(answer)
      return null
    },
    [setAdmin],
  )

  const signupWithGoogle = useCallback(
    async (credential: string) => {
      const answer = await post<SignupNext>('/api/auth/signup/google', { credential })
      // The session cookie is set but the answer carries no identity: read it the way a page load does.
      if (answer.next === 'signed_in') setAdmin(await get<Admin>('/api/auth/me'))
      return answer
    },
    [setAdmin],
  )

  const logout = useCallback(async () => {
    await post('/api/auth/logout')
    forgetLoginReturn()
    setAdmin(null)
  }, [setAdmin])

  const refresh = useCallback(async () => {
    const sent = actingId()
    adoptIdentity(sent, await get<Admin>('/api/auth/me'), setAdmin)
  }, [setAdmin])

  /* Merge-and-save a slice of the user's own preferences. Merge, never replace: PUT /api/auth/me swaps
  the whole `preferences` object. Applied optimistically, reconciled with the server's echo, reverted on
  failure. Returns a boolean rather than throwing: fire-and-forget knob callers (`components/Sidebar.tsx`,
  `pages/Projects.tsx`) would otherwise leak unhandled rejections, while `/profile`'s form needs to know
  the save failed. No reason is carried; no caller can act on one. */
  const updatePreferences = useCallback(
    async (change: PreferencesChange) => {
      // Nothing was stored, so the answer is still `false`.
      const base = latestAdminRef.current
      if (!base) return false
      const changes = typeof change === 'function' ? change(base.preferences ?? {}) : change
      // Only the newest save's echo is adopted: an older one answering late would drop the newer slice.
      const seq = ++saveSeqRef.current
      const merged = { ...(base.preferences ?? {}), ...changes }
      setAdmin({ ...base, preferences: merged })
      /* `notifications` is sent only when it is the change: the server keeps the stored one when it is
         omitted (`preferences.CARRIED_FORWARD_FIELDS`), so this tab's copy cannot undo a Slack link the
         bot wrote meanwhile (`components/SlackConnect`). */
      const { notifications: _stale, ...rest } = merged as Record<string, unknown>
      const body = 'notifications' in changes ? merged : rest
      try {
        const answer = await put<Admin>('/api/auth/me', { preferences: body })
        if (seq === saveSeqRef.current) setAdmin(answer)
        return true
      } catch {
        // Only this slice is undone: a save that landed meanwhile keeps its own.
        const now = latestAdminRef.current
        if (now) {
          const reverted = { ...(now.preferences ?? {}) }
          for (const key of Object.keys(changes)) {
            if (base.preferences && key in base.preferences) reverted[key] = base.preferences[key]
            else delete reverted[key]
          }
          setAdmin({ ...now, preferences: reverted })
        }
        return false
      }
    },
    [setAdmin],
  )

  return (
    <AuthContext.Provider
      value={{
        admin,
        loading: loading || readFor !== acting,
        resolving,
        login,
        loginWithGoogle,
        completeSignIn: setAdmin,
        signupWithGoogle,
        logout,
        refresh,
        updatePreferences,
      }}
    >
      {children}
    </AuthContext.Provider>
  )
}

export const useAuth = () => useContext(AuthContext)

/** Whether the console offers controls that add a static slot: not while the org's plan allows none
 *  (`Admin.static_slots` 0). Absent (a snapshot from before the field) offers them; the server still refuses. */
export function useStaticSlotsOffered(): boolean {
  return useAuth().admin?.static_slots !== 0
}

/** The plan's cap on static slots per project per node: a number, or null for no cap (absent reads as none). */
export function useStaticSlotsCap(): number | null {
  const cap = useAuth().admin?.static_slots
  return typeof cap === 'number' ? cap : null
}

/** The org this identity acts as: its current membership, else its `org`. */
export function currentOrgId(admin: Admin): string | null {
  const memberships = Array.isArray(admin.memberships) ? admin.memberships : []
  return memberships.find(m => m.is_current)?.org_id ?? admin.org?.id ?? null
}

/* ── The capability seam ──
The one question the console asks about the signed-in user: "may they do this?"
• An org admin (`isOrgAdminSeat`) is allow-all, within their own org (the server bounds it).
• Everyone else is deny-all unless granted; missing or not-yet-loaded answers `false`. The loading
window belongs to the caller (route guards hold).
Presentation only, never the security boundary; what the server enforces is single-homed in
`components/RequireFeature.tsx`. */
export function useCan(): (key: PermissionKey) => boolean {
  const { admin } = useAuth()
  // Memoized on the identity so the predicate is a stable hook dependency. `Array.isArray`, not
  // `?? []`: the persisted snapshot is cast after validating only `id`/`email`, and `new Set({})` would
  // throw inside the Sidebar, which renders outside the ErrorBoundary.
  const granted = useMemo(
    () => new Set(Array.isArray(admin?.permissions) ? admin.permissions : []),
    [admin?.permissions],
  )
  const orgAdmin = isOrgAdminSeat(admin)
  return useMemo(
    () => (key: PermissionKey) => orgAdmin || granted.has(key),
    [orgAdmin, granted],
  )
}

/* ── The project-keyed capability seam ──
"May they do this ON THIS PROJECT" / "…on at least one": `useCanOnProject`/`useCanOnAnyProject`, over
`project_permissions` — task access included. The server's same-subject-path intersection; never cross
`permissions` with a project list. An org admin (`isOrgAdminSeat`) is allow-all; everyone else deny-all
unless present. Presentation only; the routes re-derive server-side. */

// Shared coercion: the persisted snapshot is cast after validating only `id`/`email`, and a malformed
// value would throw inside a component rendered outside the ErrorBoundary.
function toGrantMap(raw: unknown): Map<string, Set<string>> {
  const byScope = new Map<string, Set<string>>()
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return byScope
  for (const [scopeId, keys] of Object.entries(raw as Record<string, unknown>)) {
    byScope.set(scopeId, new Set(Array.isArray(keys) ? keys : []))
  }
  return byScope
}

function useProjectGrants(): Map<string, Set<string>> {
  const { admin } = useAuth()
  return useMemo(() => toGrantMap(admin?.project_permissions), [admin?.project_permissions])
}

/** May this principal do `key` ON project `projectId`? The map is keyed by the id's decimal string; an
 * absent project answers false.
 */
export function useCanOnProject(): (key: PermissionKey, projectId: number) => boolean {
  const { admin } = useAuth()
  const byProject = useProjectGrants()
  const orgAdmin = isOrgAdminSeat(admin)
  // Memoized on the identity so the predicate is a stable hook dependency.
  return useMemo(
    () => (key: PermissionKey, projectId: number) =>
      orgAdmin || (byProject.get(String(projectId))?.has(key) ?? false),
    [orgAdmin, byProject],
  )
}

/** Does this principal hold `key` on at least one project? For a surface that does not yet know the
 * project; never a stand-in for `useCanOnProject` where the project is known.
 */
export function useCanOnAnyProject(): (key: PermissionKey) => boolean {
  const { admin } = useAuth()
  const byProject = useProjectGrants()
  const orgAdmin = isOrgAdminSeat(admin)
  return useMemo(
    () => (key: PermissionKey) => {
      if (orgAdmin) return true
      for (const keys of byProject.values()) if (keys.has(key)) return true
      return false
    },
    [orgAdmin, byProject],
  )
}
