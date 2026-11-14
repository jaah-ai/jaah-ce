import { type FormEvent, useEffect, useMemo, useRef, useState } from 'react'
import { Link as RouterLink, useLocation, useNavigate, useParams, useSearchParams } from 'react-router-dom'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import Typography from '@mui/material/Typography'
import { apiDetail, ApiError } from '../../api'
import { useAuth, useCan } from '../../auth'
import Breadcrumb from '../../components/Breadcrumb'
import { useChangeOrgAction } from '../../components/ChangeOrgDialog'
import CredentialAccessTab from '../../components/access/CredentialAccessTab'
import { TabPanel, Tabs, type TabDef } from '../../components/Tabs'
import { useApiRead } from '../../hooks/useApiRead'
import { useConnectReturn } from '../../hooks/useConnectReturn'
import { useBaselineDirty } from '../../hooks/useBaselineDirty'
import { useEditRecord } from '../../hooks/useEditRecord'
import { useLeaveGuard } from '../../hooks/useLeaveGuard'
import { useSubmit } from '../../hooks/useSubmit'
import { useUrlTab } from '../../hooks/useUrlTab'
import { actAsOnce, enterActAs } from '../../lib/actAs'
import { credentialTypeLabel } from '../../lib/credentialRow'
import { useOrgScope } from '../../lib/orgScope'
import { secretShapeWarning } from '../../lib/secretShape'
import { isEnabled } from '../../lib/editionFeatures'
import { isSealedSecretEnvelope, sealingDestination } from '../../lib/credentialSeal'
import { shortHash } from '../../lib/nodeVaultKeys'
import { navigateExternal } from '../../lib/externalNavigate'
import { leaveForAuthorize } from '../../lib/leaveForAuthorize'
import { returnUrl } from '../../lib/nestedCreate'
import { useNestedReturn } from '../../hooks/useNestedCreate'
import { safeHref } from '../../lib/safeHref'
import { isHttpUrl } from '../../lib/urlShape'
import { isUuidShaped } from '../../lib/uuidShape'
import AsyncLookup, { type LookupOption } from '../../ui/AsyncLookup'
import AdvancedSection from '../../ui/AdvancedSection'
import { ChoiceField } from '../../ui/ChoiceField'
import Banner from '../../ui/Banner'
import { DetailPage } from '../../ui/detail/DetailPage'
import DetailStatus from '../../ui/detail/DetailStatus'
import { DisabledReason } from '../../ui/DisabledReason'
import { matchesAny } from '../../ui/FilterRow'
import { CopyableValue } from '../../ui/CopyableValue'
import { credentialMark } from '../../ui/credentialMarks'
import FormActionBar from '../../ui/FormActionBar'
import FormField from '../../ui/FormField'
import { EntityNameField } from '../../ui/EntityNameFields'
import FormTextField from '../../ui/FormTextField'
import HelpLabel from '../../ui/HelpLabel'
import SectionCard from '../../ui/SectionCard'
import SettingCheckbox from '../../ui/SettingCheckbox'
import { tapTargetSx } from '../../ui/listStyles'
import { ActingForNotice, useOtherOrg } from '../../ui/OrgIndicator'
import SubmitButton from '../../ui/SubmitButton'
import { LeaveGuardDialog } from '../../ui/UnsavedChangesDialog'
import {
  createCredential,
  getCredential,
  getNodeVaultEligibility,
  getNodeVaultSealKey,
  getNodeVaultState,
  startCredentialConnect,
  testCredential,
  changeCredentialOwner,
  updateCredential,
  type CredentialOwnership,
  type FieldSpec,
  type Credential,
  type CredentialSealKey,
  type CredentialTestResult,
  type CredentialTypeSpec,
  type NodeVaultEligibility,
  type NodeVaultStateInfo,
} from './api'
import {
  buildSealedConfig, CredentialHomeLine, HomeProjectField, inlineFieldError, RegistryFieldInput, useHomeProjects, useSealKey,
} from './credentialForm'
import { withoutKey } from '../../lib/sets'
import { createsOrgRows, mintCredentialId, SEAL_UNAVAILABLE, testsCredentials } from './ownership'
import ConnectScopes, { initialScopePicks, scopeChoiceOf } from './ConnectScopes'
import OwnershipRadios from './OwnershipRadios'
import OwnerMemberLookup, { type OwnerMember } from './OwnerMemberLookup'
import { useCredentialTypes } from './useCredentialTypes'
import CredentialMcpTab from './integrations/CredentialMcpTab'
import JaahKeyField, { API_KEY_NAME_MAX, isThisConsole, JAAH_KEY_FIELDS, JAAH_TYPE, type JaahKeyMode } from './JaahKeyField'
import { createApiKey, revokeApiKey, type ApiKeyWithSecret } from '../settings/apiKeys/api'
import {
  CONNECTION_STATE, INTEGRATIONS_KEY, listIntegrations, startIntegrationConnect, type Integration, type WorstState,
} from './integrations/api'
import { VendorLogo } from '../../ui/credentialMarks'
import RelativeTime from '../../ui/RelativeTime'
import { StatusChip } from '../../ui/StatusChip'
import { FONT_SIZE, SIZE, grid } from '../../theme'
import Tip from '../../ui/Tip'
import ExternalLink from '../../ui/ExternalLink'

// The one Add/Edit page for /credentials/new and /credentials/:id (routed, not a modal). Every field
// below the common three renders from `GET /api/credentials/types` (see `useCredentialTypes`).
//
// Two kinds of viewer: an operator holding `credentials:*` keys, and a member holding none — both reach it
// from Directory > Secrets, open to every signed-in user (App.tsx's route block says why that is safe).
// The server decides visibility for both (`authz.owned_credential_clause`: created AND still granted, else 404), so
// the `can(...)` reads below decide only what this page may OFFER.

/** Where "back" goes (Cancel, the first crumb, the post-save landing): the roster, which App.tsx serves
 *  ungated, so every viewer can read it. */
const ROSTER_PATH = '/credentials'

/** The key `POST /api/credentials/test` still demands (see `canTest`). */
const TEST_KEY = 'credentials:edit' as const

/** The tap floor plus a width floor: at 390px `Test` measures ~35px wide on its own text. */
const TAP_FLOOR = [tapTargetSx, { minWidth: { xs: SIZE.tapTarget, sm: 'auto' } }]

/** The form's measure, narrower than `DetailPage`'s `read`; a 1440px-wide credential form reads as unfinished. */
const FORM_MEASURE = { maxWidth: SIZE.measure } as const

/** The tail of the server's duplicate-name refusal (`a credential named <name> already exists in this scope`). */
const DUPLICATE_NAME = 'already exists in this scope'

/** An email inside a member's lookup label (`lib/users.ts::userOption` drops the hint when the label spells it). */
const EMAIL_IN_LABEL = /[^\s()<>,]+@[^\s()<>,]+/

/** The create route's type preselection (`/credentials/new?type=aws`), from the links that know which type
 *  the operator needs. Ignored on edit (the row's type is a fact) and when the registry has no such slug. */
const TYPE_PARAM = 'type'

/** The create route's catalog vendor (`/credentials/new?integration=stripe`, from the gallery): the form opens on its
 *  credential type with its header and its methods (`methodsOf`), and the type is fixed. */
const INTEGRATION_PARAM = 'integration'

/** How a vendor's connection is made: its one-click Connect, a pasted key (its fields), or the person's own app. */
type ConnectMethod = 'connect' | 'key' | 'byo'

/** The fields an OAuth vendor's type carries for Connect (`integrations/catalog.py::_oauth_fields`): never typed on
 *  its key method; the person's own app's pair is asked by the BYO method instead. */
const OAUTH_FIELD_KEYS: ReadonlySet<string> = new Set([
  'client_id', 'client_secret', 'refresh_token', 'access_token', 'expiry_date', 'scope', 'account',
])

/** What a pasted method is called on its switch, by the spec's auth kind. */
const PASTED_METHOD: Record<string, string> = {
  key: 'an API key', basic: 'a username and password', service_account: 'a service account key',
  db: 'database credentials', mcp: 'the MCP server',
}

/** A vendor's methods in its `auth` order: Connect, the pasted fields (once, however many kinds), then the person's
 *  own OAuth app where the vendor accepts one. */
function methodsOf(integration: Integration): { method: ConnectMethod; label: string; unavailable?: string }[] {
  const out: { method: ConnectMethod; label: string; unavailable?: string }[] = []
  // No Jaah app (a Beta vendor): its one-click Connect is not offered and the missing app is never named, so the
  // key and the person's own app are the ways in. Only with neither is it kept, disabled, so the screen says why.
  const noApp = integration.oauth?.app_configured === false
  for (const auth of integration.auth_methods) {
    if (auth.kind === 'oauth') {
      if (!noApp && !out.some(item => item.method === 'connect')) out.push({ method: 'connect', label: `Connect ${integration.label}` })
    } else if (!out.some(item => item.method === 'key')) {
      const what = PASTED_METHOD[auth.kind] ?? 'an API key'
      out.push({ method: 'key', label: `Use ${what}` })
    }
  }
  if (integration.oauth?.byo_allowed) out.push({ method: 'byo', label: 'Use your own OAuth app' })
  if (noApp && out.length === 0) out.push({ method: 'connect', label: `Connect ${integration.label}`, unavailable: APP_NOT_CONFIGURED })
  // "instead" only after a first choice.
  return out.map((item, index) => (item.method === 'key' && index > 0 ? { ...item, label: `${item.label} instead` } : item))
}

const APP_NOT_CONFIGURED = 'Not available yet'

/** What picking each method does (`handleIntegrationConnect` for the two OAuth ones, Save for the pasted fields). */
const METHOD_HELP: Record<ConnectMethod, string> = {
  connect: 'Sign in at the vendor through Jaah\'s app.',
  key: 'Paste the vendor\'s credentials into the fields below.',
  byo: 'Sign in through an OAuth app you registered yourself.',
}

/** The owners a one-click Connect can make. */
const CONNECT_OWNERS: readonly CredentialOwnership[] = ['personal', 'org']

/** A saved row's state, worst first (as a gallery card's `worst_state`). */
function rowState(row: Credential): WorstState {
  if (row.needs_reconnect_at) return 'needs_reconnect'
  if (row.last_test_ok === false) return 'test_failed'
  if (row.last_test_ok === true) return 'ok'
  return 'untested'
}

/** The type a plain New secret opens on, when the registry serves it (else the registry's first). */
const DEFAULT_TYPE = 'generic_login'

/** How many numbered names (`<slug>-2` … ) an untouched default name tries before the refusal shows. */
const DEFAULT_NAME_TRIES = 20

/* ── The connect flow's closed error vocabulary ───────────────────────────────────────────────────
 *
 * A control-plane connect returns via the callback as `?connected=1` or `?connect_error=<slug>`; the
 * slug is from a closed server vocabulary, so nothing third-party is rendered. KEEP IT COMPLETE:
 * `routes/credentials/connect.py::_CONNECT_ERRORS` is the authority (one entry each, in its order); the server's
 * strings are log prose, these are copy. `Object.create(null)` because the key comes off the URL
 * (`pages/ProfilePage.tsx`'s `GIT_CONNECT_ERRORS` explains `__proto__`). No vendor is named: the generic
 * OAuth2 type's provider is unknown (`provider_label` null).
 */
const CONNECT_ERRORS: Record<string, string> = Object.assign(Object.create(null), {
  expired: 'The connect attempt timed out. Connect again.',
  state_mismatch: 'That response did not match the attempt this browser started. Connect again.',
  wrong_user: 'That connect attempt belongs to a different signed-in user. Connect again.',
  denied: 'The authorization was not completed, so nothing was stored.',
  no_code: 'The provider returned no authorization code. Connect again.',
  exchange_failed: 'The provider refused to exchange the authorization code. Connect again.',
  /* Names a cause: an access token with NO refresh token works today and can never renew. */
  no_refresh_token:
    'The provider returned no refresh token, so nothing was stored — check the scope and connect '
    + 'again.',
  /* Two causes: deleted (the page's own load usually 404s first) or no longer editable. Neither retries. */
  credential_gone: 'That secret was deleted, or is no longer yours to edit, so nothing was stored.',
  /* About the row, not the round trip: fields the flow needs are missing (the callback carries only the slug). */
  bad_request: 'This secret cannot be connected as it stands — check its client id, client secret and URLs.',
  /* The grant reached no organisation or site (Xero, Atlassian): connecting again lets the person pick one. */
  no_account: 'The provider named no organisation or site for this sign-in, so nothing was stored. Connect again '
    + 'and pick one.',
})

/** The router-state key that carries a refused connect start from the create form to the saved row. */
const START_REFUSAL_STATE = 'connectStartRefusal'

const UNUSABLE_AUTHORIZE_URL = 'The authorization link the server returned is not a usable URL.'

/** Unrecognised or absent slug: still says the one thing to do next. */
const CONNECT_FALLBACK = 'The connection did not complete. Connect again.'

/** The callback's params (`hooks/useConnectReturn` reads and strips them). */
const CONNECT_RETURN = { param: 'connected', success: '1', error: 'connect_error' } as const

/** The secret a connect FILLS — this page's only unforgeable evidence one landed. The connect flow's own
 *  contract (`app/credentials/oauth_wizards.py`), not a per-type field table. */
const CONNECT_SECRET_KEY = 'refresh_token'
/** A stored secret's empty input: blank keeps it. */
const STORED_PLACEHOLDER = 'Stored. Leave blank to keep.'

/** The stored type as a one-off option when the registry dropped it, so the dropdown never shows a
 *  different type than the row. */
function retiredTypeOption(record: Credential): CredentialTypeSpec {
  return {
    slug: record.type, label: record.type_label, testable: false, host_field: null, default_base_url: null,
    fields: [],
  }
}

/** One registry type as a lookup option (mark and label; the slug still matches a search but is not drawn —
 *  every label is distinct); builds offered and selected rows alike. `short`: the selected value's label, its
 *  trailing qualifier dropped (`lib/credentialRow.tsx::credentialTypeLabel`) so a narrow editor's box reads whole
 *  — the options keep the full label. */
function typeLookupOption(spec: CredentialTypeSpec, short = false): LookupOption {
  return { key: spec.slug, label: credentialTypeLabel(spec.label, short), icon: credentialMark(spec.slug, spec.label, spec.logo_url) }
}

/**
 * Banner copy that can WRAP mid-token: testers cap an identity at 120 chars with no whitespace required
 * (`_identity_text`), and MUI's `Alert` message is a flex item whose min-content is the whole token.
 * `anywhere` (not `break-word`, which ignores min-content) lets it shrink. No test covers it any more
 * (the e2e spec was retired: 480px is an unsupported width,
 * and jsdom has no layout). Lives here because `ui/Banner`'s signature is frozen.
 */
/** What a failed test's `detail` adds to its sentence. `detail` is a status (`HTTP 404`) or a fixed token
 *  (`timeout`, `transport_error`, an OAuth `error` code) whose meaning the sentence already states, so only a
 *  status the sentence does not already name is shown; a raw token never reaches the operator. */
function failureSuffix(result: CredentialTestResult): string {
  const detail = result.detail ?? ''
  if (!/^HTTP \d{3}$/.test(detail) || result.message.includes(detail)) return ''
  return ` (${detail})`
}

/** The server's 409 for a name already taken in this home (`DUPLICATE_NAME`); any other 409 is not. */
function isDuplicateName(err: unknown): boolean {
  return err instanceof ApiError && err.status === 409 && apiDetail(err, '').includes(DUPLICATE_NAME)
}

/* ── NodeVault ───────────────────────────────────────────────────────────────────────────────────────
 * The checkbox's one-line reasons. Each sentence the server owns (an ineligible type, a control-plane
 * consumer, why Test is withheld) is shown as served; these are only the states the page names itself. */
const NV_NOT_IN_PLAN = "NodeVault isn't included in your plan."
const NV_NOT_SET_UP = "NodeVault isn't set up yet."
const NV_STATE_UNAVAILABLE = "NodeVault status couldn't be loaded."
const NV_TYPE_INELIGIBLE = "This type can't use NodeVault."
const NV_KEY_CHANGED = 'The NodeVault key changed. Reload the page and try again.'

/** Thrown when the key `/api/nodevault/seal-key` serves is not the one the form names: a rotation landed
 *  while the page was open. Sealing stops rather than put the secret under a key the operator was not shown. */
class NodeVaultKeyChanged extends Error {}

/** A saved row's own eligibility read: the verdict, still in flight, failed, or not asked (a new row, or a
 *  type other than the stored one). */
type RowVerdict = NodeVaultEligibility | 'pending' | 'failed' | null

/** Why this credential cannot BECOME NodeVault, or `null` when it can. `undefined` while the state is still
 *  loading (the box waits, unexplained). The type comes first: it is a fact about the credential, whatever
 *  the plan; then the org's state; then what, on the control plane, opens this row. */
function nodeVaultBlocker(
  info: NodeVaultStateInfo | null,
  infoFailed: boolean,
  type: string,
  row: RowVerdict,
): string | null | undefined {
  if (infoFailed) return NV_STATE_UNAVAILABLE
  if (info === null) return undefined
  const verdict = info.types[type]
  if (verdict === undefined) return NV_TYPE_INELIGIBLE
  if (!verdict.eligible) return verdict.reason || NV_TYPE_INELIGIBLE
  if (info.state === 'not_in_plan' || info.state === 'frozen') return NV_NOT_IN_PLAN
  if (info.state === 'not_setup') return NV_NOT_SET_UP
  if (row === 'pending') return undefined
  if (row === 'failed') return NV_STATE_UNAVAILABLE
  if (row !== null && !row.eligible) return row.reason || NV_TYPE_INELIGIBLE
  return null
}

/* An existing credential's tabs, `?tab=` in the URL (as `pages/ProjectEditPage.tsx`); the new form has none. */
type CredentialTab = 'details' | 'access' | 'mcp'
const CREDENTIAL_TABS: readonly TabDef<CredentialTab>[] = [
  { id: 'details', label: 'Details' },
  { id: 'access', label: 'Access' },
]
/* An MCP connection's own tab: the projects that link it (`integrations/CredentialMcpTab.tsx`). */
const MCP_TAB: TabDef<CredentialTab> = { id: 'mcp', label: 'MCP' }

export default function CredentialEditPage() {
  const { id } = useParams<{ id: string }>()
  const isNew = id === undefined
  const navigate = useNavigate()
  const can = useCan()
  const { types, loading: typesLoading, error: typesError } = useCredentialTypes()

  const rosterTo = ROSTER_PATH
  const { search, state: locationState } = useLocation()
  /* Where Save and Cancel land on the create route instead of the roster (`?return=/nodes/nodelinks/…`), for a
     page that sent the user here to add the credential it lacks: the nested-create seam (`lib/nestedCreate.ts`). */
  const leaveTo = (isNew ? returnUrl(search, window.location.origin, null) : null) ?? rosterTo

  /* The connection tester is the one control withheld (`./ownership.ts::testsCredentials`): not drawn (not
     disabled), it could only ever 403. Coarser than the server, knowingly: operator-plane non-superadmin
     editors lose Test here while the route still runs it for them. */
  const { admin } = useAuth()
  const canTest = testsCredentials(admin, can(TEST_KEY))
  // The operator's "Change org…": an existing credential only, gated inside the hook.
  const changeOrg = useChangeOrgAction('credential')
  /* G2: an Org Owner/Admin or an operator picks who owns a NEW credential — the server's
     `authz.creates_org_rows`, so an operator-plane org admin chooses too; anyone else sends nothing and
     the server makes it theirs. Personal by default, as on the server. Presentation only. The community edition
     has one user on one machine: no owner to choose. */
  const choosesOwner = isNew && createsOrgRows(admin) && isEnabled('multi_org_tenancy')
  /* A saved row: an Org Owner/Admin or an operator moves it to another type or member (`PUT …/owner`), any row
     they read — a member's personal one included, which they cannot otherwise edit. */
  const movesOwner = !isNew && createsOrgRows(admin) && isEnabled('multi_org_tenancy')
  /* WHICH ORG OWNS A NEW CREDENTIAL. The org filter narrows reads only (`lib/orgScope.ts`): a create
     always lands in the operator's own org. So with the filter on another org, the page asks — the operator's own
     org saves here; another org is created by acting as it (`lib/actAs.ts`), whose seal key, NodeVault state and
     members are that org's. Nothing is pre-picked, so it never lands in the wrong org unasked. */
  const orgScope = useOrgScope()
  const ownOrg = isNew && admin?.is_operator === true ? admin.org ?? null : null
  const scopedOthers = ownOrg && orgScope ? orgScope.filter(org => org.id !== ownOrg.id) : []
  const choosesOrg = ownOrg !== null && scopedOthers.length > 0
  const [orgChoice, setOrgChoice] = useState('')
  const actAsOrg = scopedOthers.find(org => org.id === orgChoice) ?? null
  const orgBlocked = choosesOrg && orgChoice !== ownOrg?.id
  const [ownership, setOwnership] = useState<CredentialOwnership>('personal')
  /* D6: which member a Personal credential is for; null (or yourself) sends nothing — the caller's own. */
  const [ownerMember, setOwnerMember] = useState<OwnerMember | null>(null)
  /* An org row's home: the org itself (null) or one of its projects. Seeded from the record on edit. */
  const [homeProjectId, setHomeProjectId] = useState<number | null>(null)
  const ownerFields = choosesOwner
    ? {
        ownership,
        ...(ownership === 'personal' && ownerMember !== null && ownerMember.key !== admin?.id
          ? { owner_user_id: ownerMember.key }
          : {}),
        ...(ownership === 'org' && homeProjectId !== null ? { home_project_id: homeProjectId } : {}),
      }
    : {}

  const [record, setRecord] = useState<Credential | null>(null)
  /* ANOTHER ORG'S ROW: an operator edits it on that org's behalf. Every request whose answer or effect
     is the org's — the PATCH, the Access tab's attach/detach, the NodeVault state and key — goes as that org for the
     one request (`lib/actAs.ts::actAsOnce`), and `ActingForNotice` says so. Test and the browser sign-in are not
     offered there: Test is refused to every tenant org, and the provider's return lands on the operator's own org. */
  const otherOrg = useOtherOrg(record?.org)
  const otherOrgId = otherOrg?.id
  const asOrg = actAsOnce(otherOrg)

  const [name, setName] = useState('')
  /* A NEW row's name defaults to its type's slug, pre-selected so typing replaces it. `autoName` is the default
     last filled: while `name` still equals it (or is blank) the name follows a type change, and Save steps it to
     the next free `<slug>-N` when taken. A typed name is never touched. */
  const [autoName, setAutoName] = useState<string | null>(null)
  const nameInputRef = useRef<HTMLInputElement>(null)
  /* Each bump moves focus to Name with its text selected, after the render that wrote the name. */
  const [nameFocus, setNameFocus] = useState(0)
  useEffect(() => {
    if (nameFocus === 0) return
    nameInputRef.current?.focus()
    nameInputRef.current?.select()
  }, [nameFocus])
  const [description, setDescription] = useState('')
  // The explicit dropdown choice; the type in force is DERIVED below (an effect would paint one frame
  // with no type-specific fields).
  const [chosenType, setChosenType] = useState('')
  // Type-specific values by field key. Secrets always START empty (the server never returns them).
  const [values, setValues] = useState<Record<string, string>>({})

  /* SEALING (`lib/credentialSeal.ts`): every secret is encrypted here before the request is
     built. The AAD carries the row's id, so a NEW row's id is minted ONCE per mount and sent with the
     POST; nothing re-mints — a 409 on that id surfaces as the save error like any other rejection.
     The seal key is fetched once, as a promise held for the page's life: Save and Test await it (no
     "loading" window in which a click is swallowed), and one failure disables both for good — a reload
     is the retry, and the page stays readable. */
  const [newId] = useState(mintCredentialId)
  const sealKeyPromise = useSealKey()
  const [sealKeyError, setSealKeyError] = useState('')
  useEffect(() => {
    let active = true
    // No id could be minted: nothing sealed here could be bound to a row, whatever the key fetch says.
    if (newId === '') {
      setSealKeyError(SEAL_UNAVAILABLE)
      return undefined
    }
    sealKeyPromise().catch(() => {
      if (active) setSealKeyError(SEAL_UNAVAILABLE)
    })
    return () => {
      active = false
    }
  }, [newId, sealKeyPromise])

  /* NODEVAULT: the org's state (`GET /api/credentials/nodevault-state`) and, for a saved row, whether THIS
     row may be NodeVault (`/{id}/nodevault-eligibility` — a control-plane consumer blocks it). A failed read
     leaves the box unable to turn ON, with the reason; it never blocks Save. */
  const nvState = useApiRead(
    ['credential-nodevault-state', otherOrgId],
    () => getNodeVaultState(asOrg), 'Failed to load NodeVault state',
    // A saved row's org is known once it loads: another org's state is that org's (`asOrg`).
    { enabled: isNew || record !== null },
  )
  const nvInfo = nvState.data
  const nvInfoFailed = nvState.error !== ''
  /* The operator's explicit tick; `null` = the default (`nodevault` below). */
  const [nvChoice, setNvChoice] = useState<boolean | null>(null)

  /* The Save's lifecycle, its per-field map included (`hooks/useSubmit`): `validate` writes the map itself (client-side
     judgement stays at the call site), `runSave` clears it as each write starts and `mapError` fills it from a refusal
     the form can attribute to a field. Keyed by registry field key, so `string`. */
  const {
    busy: saving, errors: fieldErrors, setErrors: setFieldErrors, formError: saveError, setFormError: setSaveError, run: runSave,
  } = useSubmit<string>()
  /* A refused Save below the fold said nothing the operator could see: each refusal bumps this, and the page
     brings the first invalid field (focused) or else the banner into view, once they have rendered. */
  const formRef = useRef<HTMLFormElement>(null)
  const [refusals, setRefusals] = useState(0)
  useEffect(() => {
    if (refusals === 0) return
    const form = formRef.current
    const field = form?.querySelector<HTMLElement>('[aria-invalid="true"]')
    const target = field ?? form?.querySelector<HTMLElement>('[data-testid="error-banner"]')
    target?.scrollIntoView?.({ block: 'center' })
    field?.focus({ preventScroll: true })
  }, [refusals])
  const { busy: testing, formError: testError, setFormError: setTestError, run: runTest } = useSubmit()
  const [testResult, setTestResult] = useState<CredentialTestResult | null>(null)

  /* `connectError`: what THIS page just did (a refused `start`, an unopenable URL). `connectReturn`: the outcome of
     a round trip this component did not live through, read off the return URL and stripped (`replace: true`) so
     reloads and Back do not replay it; the banner state outlives the strip. */
  const { busy: connecting, formError: connectError, setFormError: setConnectError, run: runConnect } = useSubmit()
  /* A Connect pressed on the create form whose start was refused lands here with the reason (`handleConnect`). */
  const startRefusal = (locationState as Record<string, unknown> | null)?.[START_REFUSAL_STATE]
  useEffect(() => {
    if (typeof startRefusal !== 'string') return
    setConnectError(startRefusal)
    // Read once: a reload or Back must not replay it (as `useConnectReturn` strips its params).
    navigate({ search }, { replace: true, state: null })
  }, [startRefusal, setConnectError, navigate, search])
  const [connectReturn, connectReturnError, clearConnectReturn] = useConnectReturn(CONNECT_RETURN, CONNECT_ERRORS, CONNECT_FALLBACK)
  const [searchParams] = useSearchParams()

  /* Dirty is "the operator's input moved" (`hooks/useBaselineDirty`), keyed once the form
     is seeded — a saved row's values in, or a new row's default name and registry defaults filled (`autoName`, set in
     the same commit as the defaults). A landed save resets the baseline to what it saved. */
  const formKey = JSON.stringify({ name, description, chosenType, values, ownership, ownerMember, homeProjectId, nvChoice })
  const seeded = isNew ? autoName !== null || name !== '' : record !== null
  const { dirty, reset: resetBaseline } = useBaselineDirty(seeded ? formKey : null)
  /* Everything but the owner: when only the owner moved, Save moves it alone, never re-validating the rest. */
  const fieldsKey = JSON.stringify({ name, description, chosenType, values, homeProjectId, nvChoice })
  const { dirty: fieldsDirty, reset: resetFieldsBaseline } = useBaselineDirty(seeded ? fieldsKey : null)

  /* A non-uuid segment is a stale link (`credentials.id` is a uuid; the API would 422), so
     not-found without a fetch. The guard must track the id domain exactly. The hook's fence drops a late
     response (StrictMode's first mount, a superseded id) that would re-seed `values` over typing. */
  const staleId = id !== undefined && !isUuidShaped(id)
  const { loading, notFound: missing, loadError } = useEditRecord(
    isNew || staleId ? null : id, getCredential, detail => {
      setRecord(detail)
      setName(detail.name)
      setDescription(detail.description ?? '')
      setHomeProjectId(detail.home_project_id ?? null)
      setOwnership(detail.ownership ?? 'personal')
      setOwnerMember(detail.owner_id ? { key: detail.owner_id, label: detail.owner ?? detail.owner_id } : null)
      // `config` carries non-secret keys only.
      setValues({ ...detail.config })
    },
    { loadFallback: 'Failed to load secret', fixedLoadError: true, notFoundStatuses: [404, 422] },
  )
  const notFound = missing || staleId

  const recordId = record?.id
  const eligibility = useApiRead(
    ['credential-nodevault-eligibility', recordId, otherOrgId],
    () => getNodeVaultEligibility(recordId as string, asOrg),
    'Failed to load NodeVault eligibility',
    { enabled: recordId !== undefined },
  )
  // The box cannot turn ON without it; the server would refuse an ineligible row anyway.
  const nvRow: NodeVaultEligibility | 'failed' | null = eligibility.error ? 'failed' : eligibility.data

  // The tab lives in the URL (hooks/useUrlTab.ts); the new form offers Details only.
  const isMcpRow = record !== null && types.some(option => option.slug === record.type && option.mcp === true)
  const shownTabs = useMemo(
    () => (isNew ? [CREDENTIAL_TABS[0]] : isMcpRow ? [...CREDENTIAL_TABS, MCP_TAB] : CREDENTIAL_TABS),
    [isNew, isMcpRow],
  )
  const [activeTab, selectTab] = useUrlTab(shownTabs, 'details')
  const typeOptions = useMemo(() => {
    if (record === null || types.some(spec => spec.slug === record.type)) return types
    return [retiredTypeOption(record), ...types]
  }, [types, record])

  // The record's type until another is picked; a new credential opens on `?type=` when the registry knows
  // it, else on `DEFAULT_TYPE`, else on the registry's first type.
  /* A catalog vendor's Connect screen (`?integration=`): its type wins over `?type=`, and waits for the catalog. */
  const integrationSlug = isNew ? searchParams.get(INTEGRATION_PARAM) : null
  const integrations = useApiRead(INTEGRATIONS_KEY, listIntegrations, 'Failed to load integrations', {
    enabled: integrationSlug !== null,
  })
  const integration = integrationSlug === null ? null
    : integrations.data?.find(item => item.slug === integrationSlug) ?? null
  const integrationPending = integrationSlug !== null && integrations.data === null && integrations.error === ''
  /* An unknown vendor, or one whose credential type this console does not serve (edition, version skew): never
     the default type under the vendor's header. */
  const integrationMissing = integrationSlug !== null && !integrationPending && !typesLoading
    && (integration === null || !types.some(option => option.slug === integration.credential_type))
  const methods = useMemo(() => (integration ? methodsOf(integration) : []), [integration])
  const [chosenMethod, setChosenMethod] = useState<ConnectMethod | null>(null)
  const method: ConnectMethod | null = integration
    ? chosenMethod ?? (methods.find(item => !item.unavailable) ?? methods[0])?.method ?? 'key'
    : null
  /** Connect or BYO: the vendor's grant fills the row, so no field is typed and no Save or Test is offered. */
  const oauthMethod = method === 'connect' || method === 'byo'
  /** An origin-scoped vendor's host field, typed before Connect (`OAuthStateOut.origin_field`); null otherwise. */
  const originField = integration?.oauth?.origin_field ?? null
  const [byoClientId, setByoClientId] = useState('')
  const [byoClientSecret, setByoClientSecret] = useState('')

  const presetType = isNew ? integration?.credential_type ?? searchParams.get(TYPE_PARAM) : null
  const presetKnown = types.some(spec => spec.slug === presetType)
  const type = chosenType || record?.type
    || (isNew
      ? (types.find(spec => spec.slug === presetType) ?? types.find(spec => spec.slug === DEFAULT_TYPE) ?? types[0])?.slug ?? ''
      : '')

  /* The opening default name, once the type is known. A link that preset the type has decided it, so focus
     goes straight to Name (Type takes the initial focus otherwise, `autoFocus` below). */
  useEffect(() => {
    if (!isNew || autoName !== null || name !== '' || type === '' || integrationPending) return
    setName(type)
    setAutoName(type)
    if (presetKnown) setNameFocus(n => n + 1)
  }, [isNew, autoName, name, type, presetKnown, integrationPending])

  const spec = typeOptions.find(option => option.slug === type)
  /** Why Test is disabled for this type: the server's sentence (its Connect remedy included), or `null` when
   *  the type is testable or names no reason (a retired type, where Test is withheld instead). */
  const untestableReason = spec?.testable === false ? spec.untestable_reason ?? null : null
  /* A vendor's key method types only its own fields: the OAuth ones are Connect's (or BYO's pair, asked apart). */
  const integrationOAuth = integration?.auth_methods.some(auth => auth.kind === 'oauth') === true
  const fields: FieldSpec[] = useMemo(
    () => (spec?.fields ?? []).filter(field => !(integrationOAuth && OAUTH_FIELD_KEYS.has(field.key))),
    [spec, integrationOAuth],
  )
  /** The stored type the registry no longer serves (`retiredTypeOption`): no fields to show, nothing to save. */
  const retired = record !== null && type === record.type && !types.some(option => option.slug === type)

  /* A NEW row opens with each registry default already in (`FieldSpec.default`, e.g. the AWS region); a
     stored row shows what it stored. Keyed on the spec, so it runs once per type: on the derived opening
     type and after `changeType` cleared `values`. Fills only keys not yet present, so a field the operator
     blanked on purpose stays blank. */
  useEffect(() => {
    if (!isNew || spec === undefined) return
    setValues(current => {
      let next = current
      for (const field of spec.fields) {
        if (field.default && !(field.key in next)) next = { ...next, [field.key]: field.default }
      }
      /* A Jaah secret's URL starts as this console (it serves `/api`); the type serves no default, since a required
         url field is the row's own sealing destination (`registry.sealing_destination`). */
      if (spec.slug === JAAH_TYPE && !('base_url' in next)) next = { ...next, base_url: window.location.origin }
      return next
    })
  }, [isNew, spec])

  /* The lookup's displayed value; `null` only while `LoadingState` covers the field. MEMOISED: `AsyncLookup`
     watches it by identity, and a fresh object would re-fill the input right after ✕. */
  const typeValue = useMemo<LookupOption | null>(() => {
    const chosen = typeOptions.find(option => option.slug === type)
    return chosen ? typeLookupOption(chosen, true) : null
  }, [typeOptions, type])

  /* The lookup's seam, filled from memory. Matches the slug too, since the row renders it (and `git`
     then finds all three git types). */
  async function matchTypes(query: string): Promise<LookupOption[]> {
    return typeOptions
      .filter(option => matchesAny(query, `${option.label} ${option.slug}`))
      .map(option => typeLookupOption(option))
  }

  // Secrets the record holds under the type on screen — only its ORIGINAL type (a type change resets config).
  const storedSecrets = record !== null && record.type === type ? record.secrets_set : []
  /* A blank input KEEPS a stored secret — unless the URL moved. A stored secret is sealed to the
     ORIGINAL destination (`sealingDestination`'s AAD), so once the operator points the row's URL
     elsewhere it cannot be kept: the server 400s a blank one, and this page says so first (`validate`,
     the field hint). Compared on the same rule the request uses, over the record's stored config for
     the original side. */
  const destinationChanged = record !== null && record.type === type && spec !== undefined
    && sealingDestination(spec, values) !== sealingDestination(spec, record.config)
  /* What the hint calls the field that moved: the type's own label ("Base URL", OIDC's "Issuer"). */
  const hostLabel = fields.find(field => field.key === spec?.host_field)?.label ?? 'base URL'

  /* The id every secret is sealed under: the row's, or the one minted for the row about to be created. */
  const credentialId = record?.id ?? newId

  /* A Jaah secret's key (`JaahKeyField`): New (a new row's default) makes a Jaah API key on Save, offered only
     on create and while the URL is this console — on a saved row it would mint a second key and orphan the
     stored one; Existing takes a picked key and its pasted secret. */
  const jaahRow = type === JAAH_TYPE
  const jaahNewOffered = jaahRow && isNew && isThisConsole(values.base_url ?? '')
  const [jaahChoice, setJaahChoice] = useState<JaahKeyMode | null>(null)
  const jaahKeyMode: JaahKeyMode = jaahNewOffered ? jaahChoice ?? (isNew ? 'new' : 'existing') : 'existing'
  const jaahMint = jaahRow && jaahKeyMode === 'new'
  /* A saved row whose picked key moved: its stored secret is the old key's, so it must be pasted again. */
  const jaahKeyMoved = jaahRow && record !== null && record.type === type
    && (values.key_id ?? '') !== (record.config.key_id ?? '')


  /* A row this viewer may only READ (a `credentials:view` holder on someone else's row; the PATCH 404s).
     The server's own `editable` verdict, which no `can(...)` read can derive. `undefined` is NOT a
     refusal — list rows and older clients lack it, and locking them would shut the /profile door. */
  const readOnly = !isNew && record?.editable === false
  /* The owner picked differs from the saved row's: Save moves it (`changeCredentialOwner`), a read-only row too. */
  const ownerChanged = movesOwner && record !== null && (
    ownership !== (record.ownership ?? 'personal')
    || (ownership === 'personal' && ownerMember !== null && ownerMember.key !== (record.owner_id ?? null))
  )
  const ownerBody = (): { ownership: CredentialOwnership; owner_user_id?: string } => ({
    ownership,
    ...(ownership === 'personal' && ownerMember !== null ? { owner_user_id: ownerMember.key } : {}),
  })
  /* Leaving with unsaved edits asks first (`hooks/useLeaveGuard`); its Save is `persist`. Not under Connect, where
     nothing is saved here (`handleSubmit`), nor on a read-only row. */
  const guard = useLeaveGuard(((dirty && !readOnly) || ownerChanged) && !oauthMethod, async () => (await persist(name)) !== null)
  const nestedReturn = useNestedReturn(guard.leaveNow)

  /* ── THE HOME: the name's locked prefix (`home_path`) ──────────────────────────────────────────────
     A saved row shows the server's; a new one is computed here from the org a create lands in and the owner
     choice, as the server will. An org row's home is the org or one of its projects (`HomeProjectField`):
     chosen on create, editable on an org row (PATCH `home_project_id`), and the prefix follows the pick. */
  const createOrg = admin?.acting_as ?? admin?.org ?? null
  const homeOrgName = isNew ? createOrg?.name ?? '' : record?.home_path?.[0] ?? record?.org?.name ?? ''
  const homeOrgId = isNew ? createOrg?.id : record?.org?.id
  const isOrgRow = isNew ? choosesOwner && ownership === 'org' : record?.ownership === 'org'
  const homeEditable = isOrgRow && !readOnly
  const homeProjects = useHomeProjects(homeOrgId, homeEditable, otherOrg)
  const homeProjectName = homeProjectId === null ? null
    : homeProjects.find(project => project.id === homeProjectId)?.name
      ?? (homeProjectId === record?.home_project_id ? record?.home_project ?? null : null)
  const homeMoved = !isNew && homeProjectId !== (record?.home_project_id ?? null)
  /* A personal row's home names its owner by email: the picked member's (its hint, or the address inside its
     label when the label already spells it), else the caller's own. */
  const memberEmail = ownerMember !== null && ownerMember.key !== admin?.id
    ? ownerMember.hint ?? ownerMember.label.match(EMAIL_IN_LABEL)?.[0] ?? ownerMember.label
    : null
  const createOwnership: CredentialOwnership = choosesOwner ? ownership : 'personal'
  const homePath: string[] = !isNew
    ? (homeMoved ? [homeOrgName, ...(homeProjectName ? [homeProjectName] : [])] : record?.home_path ?? [])
    : [
        homeOrgName,
        ...(createOwnership === 'system' ? ['system']
          : createOwnership === 'org' ? (homeProjectName && !oauthMethod ? [homeProjectName] : [])
          : [memberEmail ?? admin?.email ?? '']),
      ]
  const homePathShown = homePath.filter(segment => segment !== '')
  const homeField = homeEditable ? (
    <HomeProjectField
      orgLabel={homeOrgName || 'Organization'}
      projects={homeProjects}
      value={homeProjectId}
      storedName={record?.home_project}
      onChange={setHomeProjectId}
    />
  ) : null

  /* NODEVAULT. `nvBlocker`: why this credential cannot become NodeVault (the row's own verdict only while
     its stored type is on screen). A saved row starts as it is — while its stored type is on screen; another
     type starts it over, so it stays on only where that type may be. A new one starts ticked whenever it may
     be (AC-5.1). Turning it OFF is never blocked — a frozen plan still lets a row leave NodeVault. */
  const sameType = record !== null && record.type === type
  const wasNodevault = sameType && record?.nodevault === true
  const rowVerdict: RowVerdict = !sameType || wasNodevault ? null : nvRow ?? 'pending'
  const nvBlocker = nodeVaultBlocker(nvInfo, nvInfoFailed, type, rowVerdict)
  const nodevault = nvChoice ?? (wasNodevault || ((record === null || record.nodevault === true) && nvBlocker === null))
  const nvDisabled = readOnly || (!nodevault && !wasNodevault && nvBlocker !== null)
  /* Until the state (and a saved row's verdict) is read, the box may still change, so Save and Test wait:
     a secret must not go out under KMS a moment before the box would have ticked. */
  const nvPending = nvBlocker === undefined && !wasNodevault
  /* Why Test is disabled, for its hover: a NodeVault row first (whatever the type, Test would open its secrets on
     the control plane, which cannot), then the type's own reason. */
  const testBlocked = (nodevault ? nvInfo?.untestable_reason : untestableReason) ?? undefined
  /* A free-form URL that is not http(s) saves, but Test cannot dial it: Test is disabled, with no message (asked
     for). Blank is left to the server's "required before testing". */
  const freeFormHost = spec?.free_form_url && spec.host_field ? (values[spec.host_field] ?? '').trim() : ''
  const undialable = freeFormHost !== '' && !isHttpUrl(freeFormHost)
  /* Drawn only where it can be used, or where the row already is NodeVault (so it can leave). An org without
     NodeVault, a plan without it, an ineligible type: no box, no sentence. */
  const nvOffered = wasNodevault || nvBlocker === null
  /* Flipping it on a saved row re-keys every secret: a stored one under the other key cannot be kept, so
     each must be typed again (the server 422s a kept one). */
  const nvFlipped = sameType && nodevault !== wasNodevault
  /* The key the row is (or will be) sealed under, for the line under the box. */
  const nvKeyHash = !nodevault ? null
    : !nvFlipped && wasNodevault ? record?.nodevault_key_hash ?? null
    : nvInfo?.current_key_hash ?? null

  /** The NodeVault seal key, refused unless it is the org's current key as the form read it. */
  async function nodeVaultKey(): Promise<CredentialSealKey> {
    const key = await getNodeVaultSealKey(asOrg)
    const expected = nvInfo?.current_key_hash
    if (expected && key.key_id !== `nv:${expected}`) throw new NodeVaultKeyChanged(NV_KEY_CHANGED)
    return key
  }

  /* The type's control-plane connect, or null. Read off the registry, never a slug list here. */
  const connect = integration ? null : spec?.oauth_connect ?? null

  /* Jaah's own client for this provider, not set up on this deployment: nothing for Connect to start. */
  const connectUnconfigured = connect?.platform_app === true && connect.configured === false
  /* Why Connect cannot start here, for its hover; undefined when it can. Another org's row: the provider's
     return lands on the operator's own org. */
  /* Unconfigured: an operator is told what to set up; a member, who cannot, is pointed at support. */
  const connectBlocked = connectUnconfigured
    ? admin?.is_operator === true ? `${connect?.provider_label ?? 'This'} connect isn't set up.` : 'Not available yet. Contact support.'
    : otherOrg ? `Act as ${otherOrg.name} to sign in.`
    : orgBlocked ? 'Choose an org'
    : sealKeyError && !nodevault ? sealKeyError
    : undefined

  /* The optional scopes kept, for a catalogue with a choice. Seeded from the saved grant, then the person's. */
  const scopeChoice = scopeChoiceOf((connect ?? (oauthMethod ? spec?.oauth_connect : null))?.scopes)
  const [scopeEdits, setScopeEdits] = useState<Set<string> | null>(null)
  const scopePicks = useMemo(
    () => scopeEdits ?? (scopeChoice ? initialScopePicks(scopeChoice, record?.config.scope) : new Set<string>()),
    [scopeEdits, scopeChoice, record],
  )

  /* A connected claim needs BOTH halves: `?connected=1` is forgeable query string, so it also requires the
     server's `secrets_set` from the load just done (as ProfilePage's success gate). */
  const connected = connectReturn?.connected === true
    && (record?.secrets_set.includes(CONNECT_SECRET_KEY) ?? false)

  function changeType(next: string) {
    setChosenType(next)
    if (isNew && (name === '' || name === autoName)) {
      setName(next)
      setAutoName(next)
    }
    setScopeEdits(null)
    // The NodeVault default is per type (an OAuth2 type may not be ticked), so a tick does not carry over.
    setNvChoice(null)
    // Type-specific values do not survive (the server resets `config` too; a carried `base_url` would
    // point a Jira row at the previous type's host).
    setValues({})
    setFieldErrors({})
    setTestResult(null)
    setTestError('')
  }

  function setValue(key: string, value: string) {
    setValues(current => ({ ...current, [key]: value }))
    // A test result describes the config as it was; leaving it up after an edit is a FALSE GREEN.
    setTestResult(null)
    setTestError('')
    setFieldErrors(current => withoutKey(current, key))
  }

  /** The NodeVault tick. Unticking clears every typed secret (AC-5.8): they go back to the browser's KMS
   *  sealing, and on a saved row each must be entered again. */
  function changeNodevault(next: boolean) {
    setNvChoice(next)
    setTestResult(null)
    setTestError('')
    if (next) return
    const secretKeys = fields.filter(field => field.kind === 'secret').map(field => field.key)
    setValues(current => {
      const cleared = { ...current }
      for (const key of secretKeys) delete cleared[key]
      return cleared
    })
    setFieldErrors(current => {
      const kept = { ...current }
      for (const key of secretKeys) delete kept[key]
      return kept
    })
  }

  /** Inline errors the form can judge itself. Registry-driven; `name` is the one field no type declares. */
  function validate(nameValue: string): Record<string, string> {
    const errors: Record<string, string> = {}
    if (!nameValue.trim()) errors.name = 'Name is required.'
    for (const field of fields) {
      if (jaahRow && JAAH_KEY_FIELDS.has(field.key)) {
        // New fills both on Save; Existing needs the key named whenever its secret is typed or not yet stored.
        const secretTyped = (values.api_key ?? '').trim() !== '' || !storedSecrets.includes('api_key')
        if (jaahMint) continue
        if (field.key === 'key_id' && secretTyped && !(values.key_id ?? '').trim()) errors.key_id = 'Key is required.'
        if (field.key === 'key_id') continue
        if (jaahKeyMoved && !(values.api_key ?? '').trim()) {
          errors.api_key = `${field.label} must be re-entered because the key changed.`
          continue
        }
      }
      if ((values[field.key] ?? '').trim()) continue
      if (field.kind === 'secret' && storedSecrets.includes(field.key)) {
        // A blank secret on a record that already holds it means "keep it" — unless the URL moved, or the
        // NodeVault tick changed the key it is sealed under. One Connect fills is dropped instead (`persist`).
        if (field.connect_filled) continue
        if (nvFlipped) errors[field.key] = `Re-enter ${field.label} to turn NodeVault ${nodevault ? 'on' : 'off'}.`
        else if (destinationChanged) errors[field.key] = `${field.label} must be re-entered because the ${hostLabel} changed.`
        continue
      }
      if (field.required) errors[field.key] = `${field.label} is required.`
    }
    return errors
  }

  /** `buildSealedConfig`, its destination computed from the same `values` the request carries. */
  function buildConfig(sealKey: () => Promise<CredentialSealKey>, sent: Record<string, string> = values) {
    const destination = spec === undefined ? '' : sealingDestination(spec, sent)
    return buildSealedConfig({ fields, values: sent, credentialId, destination, sealKey })
  }

  /** Validate, then write the row (create or PATCH) through `runSave`. The saved row's id, or null when the
   *  form refused it or the server did (the reason is already on screen). */
  async function persist(nameValue: string): Promise<string | null> {
    if (record !== null && (readOnly || (ownerChanged && !fieldsDirty))) {
      // Only the owner moved, or this viewer cannot edit the rest: the owner moves alone.
      const landed = await runSave('Failed to change the owner', async () => {
        if (ownerChanged) await changeCredentialOwner(record.id, ownerBody(), asOrg)
        resetBaseline()
        resetFieldsBaseline()
      })
      return landed === null ? null : record.id
    }
    const errors = validate(nameValue)
    setFieldErrors(errors)
    setSaveError('')
    if (Object.keys(errors).length > 0) {
      setRefusals(n => n + 1)
      return null
    }
    let savedId: string | null = null
    await runSave('', async () => {
      /* A Jaah secret on New: its key is made first (every permission its owner holds, no Org administration, no
         expiry), and revoked again if the secret is not saved, so no key is left behind. */
      let minted: ApiKeyWithSecret | null = null
      let sent = values
      if (jaahMint) {
        minted = await createApiKey({
          name: nameValue.trim().slice(0, API_KEY_NAME_MAX), description: '', grants_all: true, permissions: [], org_admin: false,
          ip_allowlist: [], expires_at: null,
        })
        sent = { ...values, api_key: minted.secret, key_id: minted.key.id }
      }
      try {
        // A NodeVault row's secrets go under the org's NodeVault key, so the control plane never reads them.
        const config = await buildConfig(nodevault ? nodeVaultKey : sealKeyPromise, sent)
        /* A stored secret Connect fills cannot be kept once the URL or the NodeVault key moves, and nobody types
           it: an explicit null deletes it, and the next Connect fills it again. */
        if (destinationChanged || nvFlipped) {
          for (const field of fields) {
            if (field.connect_filled && field.kind === 'secret' && storedSecrets.includes(field.key) && !(field.key in config)) {
              config[field.key] = null
            }
          }
        }
        const body = {
          name: nameValue.trim(),
          type,
          description: description.trim() ? description : null,
          config,
          nodevault,
        }
        if (record !== null) {
          // The home is sent only when it moved: an unchanged PATCH leaves it as stored.
          await updateCredential(record.id, homeMoved ? { ...body, home_project_id: homeProjectId } : body, asOrg)
          if (ownerChanged) await changeCredentialOwner(record.id, ownerBody(), asOrg)
          savedId = record.id
        } else {
          // The minted id: the envelopes just built are bound to it (the name is not, so a retry may rename).
          // An untouched default name that is taken steps to `<slug>-2`, `-3`, … ; a typed one is refused as typed.
          const stepsName = autoName !== null && body.name === autoName
          for (let tried = 1; ; tried += 1) {
            try {
              await createCredential({ ...body, id: newId, ...ownerFields })
              break
            } catch (err) {
              // The field shows the name the refusal is about.
              if (!stepsName || tried >= DEFAULT_NAME_TRIES || !isDuplicateName(err)) {
                if (body.name !== nameValue.trim()) setName(body.name)
                throw err
              }
              body.name = `${autoName}-${tried + 1}`
            }
          }
          if (body.name !== nameValue.trim()) setName(body.name)
          savedId = newId
        }
      } catch (err) {
        // The save's own refusal is what the form shows; a failed revoke leaves the key listed in Settings > API keys.
        if (minted !== null) await revokeApiKey(minted.key.id).catch(() => undefined)
        throw err
      }
      resetBaseline()
      resetFieldsBaseline()
    }, {
      mapError: err => {
        setRefusals(n => n + 1)
        const detail = apiDetail(err, '')
        /* The name is taken in this home (409, `DUPLICATE_NAME`) — the server's sentence, on the name it is about.
           Any other 409 keeps the banner. */
        const inline = isDuplicateName(err) ? { name: detail }
          : detail ? inlineFieldError(fields, detail) : null
        if (inline) {
          setFieldErrors(inline)
          return ''
        }
        // A non-API rejection here is the seal key or WebCrypto (`sealKeyError` withholds Save too).
        return detail || (err instanceof NodeVaultKeyChanged ? NV_KEY_CHANGED
          : err instanceof ApiError ? 'Failed to save secret' : SEAL_UNAVAILABLE)
      },
    })
    return savedId
  }

  async function handleSubmit(event: FormEvent) {
    event.preventDefault()
    // Enter still submits a read-only row, and the PATCH can only 404. Nor before the org is the operator's own.
    if ((readOnly && !ownerChanged) || orgBlocked) return
    // Under Connect nothing is saved here: Enter in Name starts the Connect, never a half-made row.
    if (oauthMethod) {
      void handleIntegrationConnect()
      return
    }
    const savedId = await persist(name)
    if (savedId === null) return
    if (isNew && nestedReturn !== null) nestedReturn(savedId)
    else guard.leaveNow(leaveTo)
  }

  async function handleTest() {
    setTestResult(null)
    await runTest('', async () => {
      // Not gated on `validate()`: the server reports missing fields in the same banner, and Test works
      // before a save. `id` is sent when the record is saved OR the config holds a sealed envelope: the
      // server reads an id beside an envelope as the AAD it was sealed under (a miss fills nothing), but
      // an id with NO envelope as a LOOKUP that 404s unless it names a row the caller may edit — so a
      // new form with a blank secret must send none.
      const config = await buildConfig(sealKeyPromise)
      const sendId = record !== null || Object.values(config).some(isSealedSecretEnvelope)
      const result = await testCredential({
        type,
        config,
        ...(sendId ? { id: credentialId } : {}),
      })
      setTestResult(result)
    }, {
      // Only a malformed REQUEST reaches here (a completed test is always a 200) — or the seal key.
      mapError: err => apiDetail(err, err instanceof ApiError ? 'Connection test could not be run' : SEAL_UNAVAILABLE),
    })
  }

  /** Save the form, then hand the browser to the provider; the callback writes the row and reports via query
   *  params. A blank name takes the type's slug, so a new row connects in one press. The URL goes through
   *  `safeHref` even though the CP built it: for a generic OAuth2 row the authorize endpoint is an
   *  operator-typed field. A refused URL is REPORTED, never a silent no-op. */
  async function handleConnect() {
    if (readOnly || connectBlocked !== undefined) return
    /* The previous round trip's verdict is spent once a new one starts. */
    clearConnectReturn()
    const nameValue = name.trim() || type
    if (nameValue !== name) setName(nameValue)
    const savedId = await persist(nameValue)
    if (savedId === null) return
    let refusal: string | null = null
    await runConnect('Failed to start the connection', async () => {
      // Picks in catalogue order; no catalogue choice sends no scopes (the server asks for all).
      const scopes = scopeChoice?.filter(scope => scope.optional && scopePicks.has(scope.name)).map(scope => scope.name)
      const reply = await startCredentialConnect(savedId, scopes)
      // Not `leaveForAuthorize`: the in-app replace below must land between the check and the leave.
      const href = safeHref(reply.authorize_url)
      if (!href) throw new Error(UNUSABLE_AUTHORIZE_URL)
      // A row just created: Back from the provider lands on the saved row, never this create form again.
      if (record === null) guard.leaveNow(`/credentials/${savedId}`, { replace: true })
      navigateExternal(href)
    }, {
      mapError: (err, fallback) => {
        refusal = err instanceof ApiError ? apiDetail(err, fallback) : err instanceof Error ? err.message : fallback
        return refusal
      },
    })
    /* A row just created here that did not reach the provider: open it as the saved row it now is (a second
       press on this create form would re-POST its id), carrying the refusal (`START_REFUSAL_STATE`). */
    if (refusal !== null && record === null) {
      // The gate's own way off this page (the row is saved); `leaveNow` hands its options to `navigate` whole.
      const landing = { replace: true, state: { [START_REFUSAL_STATE]: refusal } }
      guard.leaveNow(`/credentials/${savedId}`, landing)
    }
  }

  /* A vendor's one-click Connect (or the person's own app): nothing is saved here — the callback creates the row once
     the vendor grants, then lands on it (`routes/integrations.py::finish_pending_connect`); a refusal comes back
     to this screen as `?connect_error=`. Why it cannot start, for its hover and its caption; undefined when it can. */
  /* Mid-sentence, only the first letter drops ("space URL"), as the server's sentences do. */
  const originName = fields.find(field => field.key === originField)?.label ?? 'address'
  const originLabel = originName.charAt(0).toLowerCase() + originName.slice(1)
  const integrationConnectBlocked = !integration || !oauthMethod ? undefined
    : originField && !(values[originField] ?? '').trim() ? `Enter the ${originLabel}`
    : method === 'connect' && integration.oauth?.app_configured === false ? APP_NOT_CONFIGURED
    : method === 'byo' && (!byoClientId.trim() || !byoClientSecret) ? 'Enter the client ID and secret'
    : orgBlocked ? 'Choose an org'
    : createOwnership === 'system' ? 'Choose Personal or Organization'
    : undefined

  async function handleIntegrationConnect() {
    if (!integration || integrationConnectBlocked !== undefined) return
    clearConnectReturn()
    const nameValue = name.trim() || type
    if (nameValue !== name) setName(nameValue)
    await runConnect('Failed to start the connection', async () => {
      const scopes = scopeChoice?.filter(scope => scope.optional && scopePicks.has(scope.name)).map(scope => scope.name)
      // A hooked vendor fixes the owner itself (`OAuthStateOut.hooked`): it is sent as the default, with no project.
      const org = createOwnership === 'org' && !integration.oauth?.hooked
      const reply = await startIntegrationConnect(integration.slug, {
        name: nameValue,
        owner: org ? 'org' : 'personal',
        ...(org && homeProjectId !== null ? { project_ids: [homeProjectId] } : {}),
        ...(method === 'byo' ? { client_id: byoClientId.trim(), client_secret: byoClientSecret } : {}),
        ...(scopes ? { scopes } : {}),
        ...(originField ? { origin: (values[originField] ?? '').trim() } : {}),
      })
      leaveForAuthorize(reply.authorize_url, UNUSABLE_AUTHORIZE_URL)
    }, {
      mapError: (err, fallback) => (err instanceof ApiError ? apiDetail(err, fallback) : err instanceof Error ? err.message : fallback),
    })
  }

  /* The trail names the roster; Save and Cancel go to `leaveTo` (the roster, or the page that sent the user). */
  const crumbs = (
    <Breadcrumb
      items={[
        { label: 'Secrets', to: rosterTo },
        ...(integrationSlug !== null
          ? [{ label: 'New secret', to: '/credentials/new' }, { label: integration?.label ?? 'Integration' }]
          : [{ label: isNew ? 'New secret' : record?.name ?? 'Secret' }]),
      ]}
    />
  )

  if (loading || typesLoading || notFound || loadError || typesError || integrationPending || integrationMissing
    || integrations.error) {
    return (
      <DetailStatus
        trail={crumbs}
        loading={loading || typesLoading || integrationPending}
        notFound={(notFound && 'Secret not found') || (integrationMissing && !integrations.error && 'Integration not found')}
        error={loadError || typesError || integrations.error}
        locate={{ objectType: 'credential', objectId: id }}
      />
    )
  }

  /* The registry fields: the required ones render outright, the optional ones fold into Advanced. */
  const renderField = (field: FieldSpec) => {
    const holdsStored = field.kind === 'secret' && storedSecrets.includes(field.key)
    return (
      <RegistryFieldInput
        key={field.key}
        // An optional field drawn among the required ones says so, as they carry no marker of their own.
        field={!field.required && field.outright ? { ...field, label: `${field.label} (optional)` } : field}
        // Only when the record holds this secret; `FormField` wires it into the helper slot for a11y.
        // Once the URL moved the stored value is unkeepable (`destinationChanged`), and the hint says so.
        hint={!holdsStored ? undefined
          : nvFlipped ? `Re-enter to turn NodeVault ${nodevault ? 'on' : 'off'}.`
          : destinationChanged ? `Re-enter: the ${hostLabel} changed.`
          : jaahKeyMoved && field.key === 'api_key' ? 'Re-enter: the key changed.' : undefined}
        placeholder={holdsStored && !nvFlipped && !destinationChanged && !(jaahKeyMoved && field.key === 'api_key') ? STORED_PLACEHOLDER : undefined}
        freeFormUrl={spec?.free_form_url === true}
        error={fieldErrors[field.key]}
        id={`credential-field-${field.key}`}
        disabled={readOnly}
        // Secrets render empty: the server never returns them.
        value={values[field.key] ?? ''}
        onChange={v => setValue(field.key, v)}
      />
    )
  }
  /* A `require_one_of` field is optional only in that ANOTHER group may stand in for it — one group must be
     filled to save — so it renders outright with the required fields, never folded into Advanced. An optional
     field with a registry `default` (the AWS Region) renders outright too: the row carries that value, and
     other screens read it (Share subscriptions, Connections), so it is seen before Save. A field the registry marks
     `outright` (Generic API Key's URL) renders outright as well: the system needs it and it has no standard value. */
  const oneOfKeys = new Set((spec?.require_one_of ?? []).flat())
  const outright = (field: FieldSpec) =>
    field.required || oneOfKeys.has(field.key) || field.default != null || field.outright === true
  /* A field Connect fills (`FieldSpec.connect_filled`) is not drawn: Connect writes it, nobody types it. */
  const drawn = fields.filter(field => !field.connect_filled)
  /* Connect and BYO type no registry field: the grant fills the row. An origin-scoped vendor's Connect (Backlog) asks
     only for its host first (`oauth.origin_field`), since the sign-in page lives there. */
  const typed = oauthMethod ? drawn.filter(field => field.key === originField)
    : jaahRow ? drawn.filter(field => !JAAH_KEY_FIELDS.has(field.key))
    : drawn
  const jaahSecretField = jaahRow ? drawn.find(field => field.key === 'api_key') : undefined
  const requiredFields = typed.filter(outright).map(renderField)
  const optionalFields = typed.filter(field => !outright(field)).map(renderField)
  const state = record !== null ? CONNECTION_STATE[rowState(record)] : null
  const docsHref = safeHref(integration?.docs_url)


  return (
    <DetailPage
      sx={FORM_MEASURE}
      breadcrumb={crumbs}
      title={isNew ? integration?.label ?? 'New secret' : record?.name}
      titleAside={state ? <StatusChip label={state.label} tone={state.tone} testId="credential-state" />
        : integration ? <VendorLogo url={integration.logo_url} label={integration.label} size={SIZE.iconMd} tile /> : undefined}
      subtitle={integration ? (
        <Box component="span" sx={{ minWidth: 0 }} data-testid="integration-header">
          {integration.summary}
          {docsHref && (
            <>
              {' · '}
              <ExternalLink href={docsHref} data-testid="integration-docs">
                Docs
              </ExternalLink>
            </>
          )}
        </Box>
      ) : undefined}
      actions={!isNew && record ? changeOrg.button({
        id: record.id, name: record.name, org: record.org, href: `/credentials/${record.id}`,
      }) : undefined}
    >
      {!isNew && (
        <Tabs tabs={shownTabs} tab={activeTab} onSelect={selectTab} label="Secret sections" idPrefix="credential" />
      )}

      <TabPanel id="details" current={activeTab} idPrefix="credential" label={isNew ? 'Details' : undefined}>
      <SectionCard>
        <Box
          component="form"
          ref={formRef}
          onSubmit={event => void handleSubmit(event)}
          sx={{ display: 'flex', flexDirection: 'column' }}
        >
          {saveError && <Banner severity="error">{saveError}</Banner>}

          {/* No seal key: nothing typed into a secret could leave this page encrypted, so Save and Test
              are withheld below. `testId` because `error-banner` is shared with `saveError`. */}
          {sealKeyError && <Banner severity="error" testId="credential-seal-unavailable">{sealKeyError}</Banner>}

          <ActingForNotice org={otherOrg} testId="credential-acting-for" />
          {/* Why every control is dead, said once, naming who can act — `routes/credentials/guards.py::_may_edit`:
              an operator edits any row; otherwise a personal row is its owner's alone, an org row needs `credentials:edit`
              org-wide, and a system one also an Org Owner/Admin (`sees_system_credentials`). `testId` because `info-banner` is shared with
              the banners below. */}
          {readOnly && (
            <Banner severity="info" testId="credential-read-only">
              {/* An org row's editors are named by the seat that holds the key by default (Owner/Admin), never
                  by the key itself — a permission id is not words. */}
              Read-only — only {record?.ownership === 'personal' ? (record.owner ?? 'its owner') : 'an admin'} can
              {' '}edit it.
            </Banner>
          )}

          {/* Delete lives on the roster, which Back/Cancel reaches. */}
          {retired && record && (
            <Banner severity="warning" testId="credential-retired-type">
              This console no longer supports the “{record.type_label}” type, so its fields can’t be shown. Delete
              it, or create a secret of a supported type.
            </Banner>
          )}

          {choosesOrg && ownOrg && (
            <Box sx={{ mb: 1 }}>
              <ChoiceField
                label="Org"
                help="Where it is created; another org needs Act as."
                row
                value={orgChoice}
                onChange={setOrgChoice}
                options={[ownOrg, ...scopedOthers].map(org => ({ value: org.id, label: org.name, help: null }))}
              />
            </Box>
          )}
          {actAsOrg && (
            <Banner
              severity="info"
              testId="credential-org-act-as"
              action={
                <Button size="small" onClick={() => enterActAs(actAsOrg)} sx={[tapTargetSx, { whiteSpace: 'nowrap', textTransform: 'none' }]}>
                  Act as {actAsOrg.name}
                </Button>
              }
            >
              Act as {actAsOrg.name} to create it there.
            </Banner>
          )}

          {/* A saved row's last test: its sentence and age; a row the vendor refused for good offers Reconnect. */}
          {record !== null && (record.last_tested_at || record.needs_reconnect_at) && (
            <Box sx={{ display: 'flex', alignItems: 'center', flexWrap: 'wrap', gap: 1, mb: 1.5 }} data-testid="credential-last-test">
              <Typography variant="body2" sx={{ color: 'text.secondary', minWidth: 0, overflowWrap: 'anywhere' }}>
                {record.last_test_message || (record.needs_reconnect_at ? 'The vendor refused the stored grant.' : 'Tested.')}
                {record.last_tested_at && <> · <RelativeTime iso={record.last_tested_at} /></>}
              </Typography>
              {record.needs_reconnect_at && connect && !readOnly && (
                <DisabledReason reason={connectBlocked}>
                  <Button
                    size="small"
                    variant="contained"
                    onClick={() => void handleConnect()}
                    disabled={connectBlocked !== undefined || connecting || saving}
                    data-testid="credential-reconnect"
                    sx={tapTargetSx}
                  >
                    Reconnect
                  </Button>
                </DisabledReason>
              )}
            </Box>
          )}

          {/* The home sits above Type and Name, outside the box; names are lowercase, so typing is lowercased rather than refused. */}
          <CredentialHomeLine path={homePathShown} />
          {/* A vendor's methods, in its order; one method needs no switch. */}
          {integration && methods.length > 1 && (
            <Box sx={{ mb: 1.5 }}>
              <ChoiceField
                label="Connect with"
                help="How Jaah gets this vendor's access."
                value={method ?? 'key'}
                onChange={next => {
                  setChosenMethod(next)
                  setFieldErrors({})
                  setConnectError('')
                }}
                options={methods.map(item => ({
                  value: item.method,
                  label: item.unavailable ? `${item.label} (${item.unavailable})` : item.label,
                  help: METHOD_HELP[item.method],
                  disabled: item.unavailable !== undefined,
                  testId: `integration-method-${item.method}`,
                }))}
              />
            </Box>
          )}
          {!integration && (
          <FormField label="Type" help="Which service it connects to; decides its fields.">
            {/* ── THE TYPE LOOKUP ───────────────────────────────────────────────────────────────
                Searchable, with vendor marks. `ui/AsyncLookup` supplies the request discipline, including
                an Enter that cannot submit this form (which would CREATE a half-filled credential). Fed
                from memory (`useCredentialTypes` fetched the catalogue), so no route and no AbortSignal.
                No hint: labels are distinct (`GitHub` vs `GitHub OAuth App`), and a search still matches the slug.
                `onChange` swallows two non-changes, because `changeType` is DESTRUCTIVE:
                  · `null` from ✕ — a cleared search box, not a cleared type.
                  · the type already chosen — MUI compares by reference, so re-picking the same row
                    reaches here and would wipe the form (and on edit, Save would `null` stored config).
                    Compared against `type` (what is displayed), not `chosenType` (`''` until picked). */}
            <AsyncLookup
              id="credential-type"
              testId="credential-type-lookup"
              ariaLabel="Type"
              value={typeValue}
              disabled={readOnly}
              // A plain New secret opens here; a preset type opens on Name instead (the default-name effect).
              autoFocus={isNew && !presetKnown && !readOnly}
              onChange={next => {
                if (next === null) return
                if (next.key !== type) changeType(next.key)
                // A new row, picked (even the type already shown): on to Name, its default selected so typing
                // replaces it. An edit keeps focus where it is: its Name holds the saved name.
                if (isNew) setNameFocus(n => n + 1)
              }}
              fetchOptions={matchTypes}
              fetchOnFocus
              noOptionsText="No matching secret type"
            />
          </FormField>
          )}

          <EntityNameField
            required={false}
            help="Name inside this home. Use / for folders."
            error={fieldErrors.name}
            id="credential-name"
            inputRef={nameInputRef}
            disabled={readOnly}
            // `role="alert"` only while an error shows (see the registry fields).
            slotProps={{
              formHelperText: fieldErrors.name ? { role: 'alert' } : undefined,
            }}
            value={name}
            onChange={v => {
              setName(v.toLowerCase())
              setFieldErrors(current => withoutKey(current, 'name'))
            }}
          />

          {/* ── NodeVault ────────────────────────────────────────────────────────────────────────────
              Under Type and Name: eligibility is the type's first. What it means, and the key that seals
              it, on the label's hover. */}
          {nvOffered && <Box data-testid="credential-nodevault" data-nodevault={nodevault} sx={{ mt: 0.5, mb: 1 }}>
            <SettingCheckbox
              name="nodevault"
              label="NodeVault"
              help={`Only your nodes can open its secrets.${nvKeyHash ? ` Key ${shortHash(nvKeyHash)}` : ''}`}
              checked={nodevault}
              disabled={nvDisabled}
              onChange={changeNodevault}
              testId="credential-nodevault-setting"
            />
          </Box>}

          {/* ── The registry field set: the required fields, outright ─────────────────────────────── */}
          {requiredFields}

          {jaahRow && (
            <JaahKeyField
              mode={jaahKeyMode}
              newOffered={jaahNewOffered}
              onMode={next => {
                setJaahChoice(next)
                setFieldErrors(current => withoutKey(withoutKey(current, 'api_key'), 'key_id'))
              }}
              keyId={values.key_id ?? ''}
              onKeyId={next => setValue('key_id', next)}
              secret={values.api_key ?? ''}
              secretInput={jaahSecretField ? renderField(jaahSecretField) : null}
              error={fieldErrors.key_id}
              disabled={readOnly}
            />
          )}

          {/* ── A vendor's one-click Connect, or the person's own app (`handleIntegrationConnect`) ───────────── */}
          {integration && oauthMethod && (
            <Box sx={theme => ({ my: 1, '& .MuiButton-root': tapTargetSx(theme) })} data-testid="integration-connect">
              {method === 'byo' && (
                <>
                  {spec?.oauth_connect?.redirect_uri && (
                    <Box sx={{ mb: 1 }}>
                      <Typography variant="caption" sx={{ display: 'flex', alignItems: 'center', gap: 0.5, color: 'text.secondary' }}>
                        <HelpLabel label="Redirect URI" help={`Register this exact URL in your ${integration.label} OAuth app.`} />
                      </Typography>
                      <CopyableValue
                        value={spec.oauth_connect.redirect_uri}
                        copyLabel="Copy redirect URI"
                        sx={{ flex: `1 1 ${grid(32)}`, fontSize: FONT_SIZE.sm, wordBreak: 'break-all' }}
                      />
                    </Box>
                  )}
                  <FormTextField
                    label="Client ID"
                    id="integration-client-id"
                    help={`From your own ${integration.label} OAuth app.`}
                    value={byoClientId}
                    onChange={setByoClientId}
                  />
                  <FormTextField
                    label="Client secret"
                    id="integration-client-secret"
                    type="password"
                    help={`From the same ${integration.label} OAuth app.`}
                    value={byoClientSecret}
                    onChange={setByoClientSecret}
                  />
                </>
              )}
              {connectReturnError && <Banner severity="error">{connectReturnError}</Banner>}
              {connectError && <Banner severity="error">{connectError}</Banner>}
              <Box sx={{ display: 'flex', alignItems: 'center', flexWrap: 'wrap', gap: 1 }}>
                <DisabledReason reason={integrationConnectBlocked}>
                  <Button
                    type="button"
                    onClick={() => void handleIntegrationConnect()}
                    data-testid="integration-connect-button"
                    disabled={integrationConnectBlocked !== undefined || connecting}
                    variant="contained"
                    color="primary"
                  >
                    {connecting ? 'Connecting…' : `Connect ${integration.label}`}
                  </Button>
                </DisabledReason>
                {/* Why Connect is greyed, said beside it, not only on hover. */}
                {integrationConnectBlocked === APP_NOT_CONFIGURED ? (
                  <Typography variant="caption" sx={{ color: 'text.secondary' }} data-testid="integration-unconfigured">
                    Not available yet. Vote for it in the gallery.
                  </Typography>
                ) : integrationConnectBlocked && (
                  <Typography variant="caption" sx={{ color: 'text.secondary' }} data-testid="integration-connect-blocked">
                    {integrationConnectBlocked} first.
                  </Typography>
                )}
              </Box>
            </Box>
          )}

          {/* ── The connect, for a type the control plane can fill ────────────────────────────────
              Under the fields it reads (a client, URLs); the token is bound to this row's client, refreshable. Connect
              saves the form first (`handleConnect`), so a new row needs no separate Save. Its permissions sit
              in Advanced. */}
          {connect && (
            <Box sx={theme => ({ my: 1, '& .MuiButton-root': tapTargetSx(theme) })} data-testid="credential-connect">
              {/* The one setup step this flow cannot do (`api.ts::OAuthConnectSpec.redirect_uri`): the vendor
                  refuses the exchange unless this exact URL is registered. `ui/CopyableValue`: an insecure
                  context refuses the Clipboard API, so selectable text is the exit that always works. A
                  platform app's client is Jaah's own: its redirect URI is the operator's setup, not this row's. */}
              {!connect.platform_app && (
                <Box sx={{ mb: 1 }}>
                  <Typography variant="caption" sx={{ display: 'flex', alignItems: 'center', gap: 0.5, color: 'text.secondary' }}>
                    <HelpLabel label="Redirect URI" help="Register this exact URL with the OAuth client." />
                  </Typography>
                  <CopyableValue
                    value={connect.redirect_uri}
                    copyLabel="Copy redirect URI"
                    sx={{ flex: `1 1 ${grid(32)}`, fontSize: FONT_SIZE.sm, wordBreak: 'break-all' }}
                  />
                </Box>
              )}

              {/* What came back from the provider, above `connectError` (it is the more recent event).
                  Success is gated on the record (`connected`); errors claim nothing about stored state. */}
              {connected && (
                <Banner severity="success">
                  Connected.
                </Banner>
              )}
              {connectReturnError && <Banner severity="error">{connectReturnError}</Banner>}
              {connectError && <Banner severity="error">{connectError}</Banner>}

              {/* Disabled rather than withheld: the button is the panel's whole subject; why, on its hover and in a
                  caption under it.
                  `readOnly` because the flow writes this row through the gate that verdict comes from. */}
              <DisabledReason reason={connectBlocked}>
                <Button
                  type="button"
                  onClick={() => void handleConnect()}
                  data-testid="credential-connect-button"
                  disabled={connectBlocked !== undefined || connecting || saving || readOnly || nvPending || retired}
                  variant="contained"
                  color="primary"
                >
                  {connecting ? 'Connecting…' : connect.action_label}
                </Button>
              </DisabledReason>
              {/* Why it is greyed, said beside it as well as on hover — not a seal-key error, which its own banner
                  above already says. */}
              {connectBlocked && connectBlocked !== sealKeyError && (
                <Typography variant="caption" sx={{ display: 'block', mt: 0.5, color: 'text.secondary' }} data-testid="credential-connect-blocked">
                  {connectBlocked}
                </Typography>
              )}
            </Box>
          )}


          {/* ── Advanced: the owner, Connect's permissions, Description and the type's optional fields ─────
              Folded, never removed (PRD R3): a first credential needs only its name, type and required
              fields; the owner defaults to Personal, the permissions to every optional scope. An invalid
              field inside opens it (`ui/AdvancedSection`). */}
          <AdvancedSection testId="credential-advanced">
            <Box sx={{ display: 'flex', flexDirection: 'column' }}>
              {choosesOwner && !(oauthMethod && integration?.oauth?.hooked) && (
                <OwnershipRadios
                  labelId="credential-ownership-label"
                  /* Connect makes an org or a personal row (`IntegrationConnectStartIn.owner`): no System. */
                  options={oauthMethod ? CONNECT_OWNERS : undefined}
                  value={ownership}
                  onChange={setOwnership}
                  sx={{ mb: 1 }}
                  /* Connect makes the row the connecting person's own: no member to pick. */
                  personalOwner={oauthMethod ? undefined
                    : <OwnerMemberLookup value={ownerMember} onChange={setOwnerMember} selfId={admin?.id} />}
                  orgHome={homeField}
                />
              )}
              {movesOwner && (
                <OwnershipRadios
                  labelId="credential-ownership-label"
                  value={ownership}
                  onChange={setOwnership}
                  disabled={saving}
                  sx={{ mb: 1 }}
                  personalOwner={<OwnerMemberLookup value={ownerMember} onChange={setOwnerMember} selfId={admin?.id} disabled={saving} />}
                  orgHome={record?.ownership === 'org' ? homeField : undefined}
                />
              )}
              {/* A saved org row: its home stays editable. */}
              {!isNew && !movesOwner && homeField}
              {(connect || oauthMethod) && scopeChoice && (
                <Box sx={{ mb: 1 }}>
                  <ConnectScopes
                    scopes={scopeChoice}
                    picked={scopePicks}
                    onChange={setScopeEdits}
                    disabled={connecting || readOnly || (connect !== null && connectUnconfigured)}
                  />
                </Box>
              )}
              <FormTextField
                label="Description"
                id="credential-description"
                help="Helps people and tasks choose between secrets."
                disabled={readOnly}
                multiline
                rows={3}
                value={description}
                onChange={setDescription}
                warning={secretShapeWarning(description)}
              />
              {optionalFields}
            </Box>
          </AdvancedSection>

          {/* One of the three server outcomes, forked on `status` (`ok` cannot tell failure from unsupported). */}
          {testError && <Banner severity="error">{testError}</Banner>}
          {testResult?.status === 'success' && (
            <Banner severity="success">
              {testResult.message}
            </Banner>
          )}
          {testResult?.status === 'failure' && (
            <Banner severity="error">
              {testResult.message}
              {failureSuffix(testResult)}
            </Banner>
          )}
          {testResult?.status === 'unsupported' && (
            <Banner severity="info">
              {testResult.message}
            </Banner>
          )}
          {/* The fork is TOTAL: an unknown fourth status renders neutrally with the server's message. */}
          {testResult !== null &&
            !['success', 'failure', 'unsupported'].includes(testResult.status) && (
              <Banner severity="info">
                {testResult.message}
              </Banner>
            )}

          {/* The form's one action row (`ui/FormActionBar`): one Save, labelled as on every journey form. */}
          <FormActionBar testId="credential-actions" stretchTestIds={['credential-save']}>
            {/* "Back" on a read-only row, "Cancel" where there is an edit to discard. */}
            <Button component={RouterLink} to={leaveTo} variant="text" sx={TAP_FLOOR}>
              {readOnly ? 'Back' : 'Cancel'}
            </Button>
            {/* Withheld, not disabled (see `canTest`); also on a read-only row. Disabled for an untestable type or a
                NodeVault row (the control plane cannot open its secrets), the server's sentence on its hover. */}
            {canTest && !readOnly && !retired && !otherOrg && !oauthMethod && (
            <DisabledReason reason={testBlocked}>
              <Tip title={testBlocked}><Button
                type="button"
                onClick={() => void handleTest()}
                data-testid="credential-test"
                disabled={testing || saving || sealKeyError !== '' || nodevault || nvPending || untestableReason !== null || undialable}
                variant="text"
                sx={TAP_FLOOR}
              >
                {testing ? 'Testing…' : 'Test'}
              </Button></Tip>
            </DisabledReason>
            )}
            {/* Withheld on a read-only row: the PATCH 404s for this viewer; and under Connect, which saves nothing here. */}
            {(!readOnly || movesOwner) && !oauthMethod && (
            <DisabledReason reason={orgBlocked && !actAsOrg ? 'Choose an org' : undefined}>
            <SubmitButton
              testId="credential-save"
              busy={saving}
              // A NodeVault save never uses the KMS key, so only its own failure withholds it.
              disabled={readOnly ? !ownerChanged : (sealKeyError !== '' && !nodevault) || nvPending || retired || orgBlocked}
              sx={TAP_FLOOR}
            >
              Save
            </SubmitButton>
            </DisabledReason>
            )}
          </FormActionBar>
        </Box>
      </SectionCard>
      </TabPanel>

      {/* Rendered only with its tab: it fetches on mount. */}
      {id !== undefined && activeTab === 'access' && (
        <TabPanel id="access" current={activeTab} idPrefix="credential">
          <SectionCard>
            <CredentialAccessTab credentialId={id} otherOrg={otherOrg} />
          </SectionCard>
        </TabPanel>
      )}
      {id !== undefined && activeTab === 'mcp' && (
        <TabPanel id="mcp" current={activeTab} idPrefix="credential">
          <SectionCard>
            <CredentialMcpTab credentialId={id} />
          </SectionCard>
        </TabPanel>
      )}
      {changeOrg.dialog}
      <LeaveGuardDialog leave={guard} message="This secret has changes that have not been saved. Save them before leaving?" />
    </DetailPage>
  )
}
