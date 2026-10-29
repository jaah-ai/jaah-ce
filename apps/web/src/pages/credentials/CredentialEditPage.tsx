import { type FormEvent, type ReactNode, useEffect, useMemo, useRef, useState } from 'react'
import { Link as RouterLink, useLocation, useNavigate, useParams } from 'react-router-dom'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import Checkbox from '@mui/material/Checkbox'
import FormControl from '@mui/material/FormControl'
import FormControlLabel from '@mui/material/FormControlLabel'
import FormLabel from '@mui/material/FormLabel'
import Radio from '@mui/material/Radio'
import RadioGroup from '@mui/material/RadioGroup'
import Typography from '@mui/material/Typography'
import { apiDetail, ApiError } from '../../api'
import { useAuth, useCan } from '../../auth'
import Breadcrumb from '../../components/Breadcrumb'
import { useChangeOrgAction } from '../../components/ChangeOrgDialog'
import CredentialAccessTab from '../../components/access/CredentialAccessTab'
import { TabPanel, Tabs, type TabDef } from '../../components/Tabs'
import { useApiRead } from '../../hooks/useApiRead'
import { useEditRecord } from '../../hooks/useEditRecord'
import { useRemountDraft } from '../../hooks/useRemountDraft'
import { useSubmit } from '../../hooks/useSubmit'
import { useUrlFilters } from '../../hooks/useUrlFilters'
import { useUrlTab } from '../../hooks/useUrlTab'
import { actAsOnce, enterActAs } from '../../lib/actAs'
import { credentialTypeLabel } from '../../lib/credentialRow'
import { useOrgScope } from '../../lib/orgScope'
import { isEnabled } from '../../lib/editionFeatures'
import { isSealedSecretEnvelope, sealingDestination } from '../../lib/credentialSeal'
import { shortHash } from '../../lib/nodeVaultKeys'
import { navigateExternal } from '../../lib/externalNavigate'
import { safeReturnPath } from '../../lib/listDetail'
import { safeHref } from '../../lib/safeHref'
import { isUuidShaped } from '../../lib/uuidShape'
import AsyncLookup, { type LookupOption } from '../../ui/AsyncLookup'
import AdvancedSection from '../../ui/AdvancedSection'
import Banner from '../../ui/Banner'
import { DetailPage } from '../../ui/detail/DetailPage'
import DetailStatus from '../../ui/detail/DetailStatus'
import { DisabledReason } from '../../ui/DisabledReason'
import { matchesAny } from '../../ui/FilterRow'
import CopyableValue from '../../ui/CopyableValue'
import { credentialMark } from '../../ui/credentialMarks'
import FormActionBar from '../../ui/FormActionBar'
import FormField from '../../ui/FormField'
import FormTextField from '../../ui/FormTextField'
import SectionCard from '../../ui/SectionCard'
import { monoSx, tapTargetSx } from '../../ui/listStyles'
import { ActingForNotice, useOtherOrg } from '../../ui/OrgIndicator'
import SubmitButton from '../../ui/SubmitButton'
import {
  createCredential,
  getCredential,
  getNodeVaultEligibility,
  getNodeVaultSealKey,
  getNodeVaultState,
  startCredentialConnect,
  testCredential,
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
  withoutKey,
} from './credentialForm'
import { createsOrgRows, mintCredentialId, SEAL_UNAVAILABLE } from './ownership'
import OwnershipRadios from './OwnershipRadios'
import OwnerMemberLookup, { type OwnerMember } from './OwnerMemberLookup'
import { useCredentialTypes } from './useCredentialTypes'

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
const TAP_FLOOR = [tapTargetSx, { minWidth: { xs: 44, sm: 'auto' } }]

/** The form's measure, narrower than `DetailPage`'s `read`; a 1440px-wide credential form reads as unfinished. */
const FORM_MEASURE = { maxWidth: 640 } as const

/** The tail of the server's duplicate-name refusal (`a credential named <name> already exists in this scope`). */
const DUPLICATE_NAME = 'already exists in this scope'

/** An email inside a member's lookup label (`lib/users.ts::userOption` drops the hint when the label spells it). */
const EMAIL_IN_LABEL = /[^\s()<>,]+@[^\s()<>,]+/

/** The create route's type preselection (`/credentials/new?type=aws`), from the links that know which type
 *  the operator needs. Ignored on edit (the row's type is a fact) and when the registry has no such slug. */
const TYPE_PARAM = 'type'

/** Where Save and Cancel land on the create route instead of the roster (`?return=/nodes/nodelinks/…`), for a
 *  page that sent the user here to add the credential it lacks. Only an in-app path (`safeReturnPath`). */
const RETURN_PARAM = 'return'

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
})

/** Unrecognised or absent slug: still says the one thing to do next. */
const CONNECT_FALLBACK = 'The connection did not complete. Connect again.'

/** The callback's params; the effect reading them also strips them. */
const CONNECTED_PARAM = 'connected'
const CONNECT_ERROR_PARAM = 'connect_error'

/** The secret a connect FILLS — this page's only unforgeable evidence one landed. The connect flow's own
 *  contract (`app/credentials/oauth_wizards.py`), not a per-type field table. */
const CONNECT_SECRET_KEY = 'refresh_token'
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
  return { key: spec.slug, label: credentialTypeLabel(spec.label, short), icon: credentialMark(spec.slug, spec.label) }
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

/** The input `useRemountDraft` carries across a layout switch. */
type CredentialDraft = {
  name: string
  description: string
  chosenType: string
  values: Record<string, string>
  ownership: CredentialOwnership
  ownerMember: OwnerMember | null
  homeProjectId: number | null
  nvChoice: boolean | null
}

function BannerText({ children }: { children: ReactNode }) {
  return <Box sx={{ overflowWrap: 'anywhere', minWidth: 0 }}>{children}</Box>
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
type CredentialTab = 'details' | 'access'
const CREDENTIAL_TABS: readonly TabDef<CredentialTab>[] = [
  { id: 'details', label: 'Details' },
  { id: 'access', label: 'Access' },
]

export default function CredentialEditPage() {
  const { id } = useParams<{ id: string }>()
  const isNew = id === undefined
  const navigate = useNavigate()
  const can = useCan()
  const { types, loading: typesLoading, error: typesError } = useCredentialTypes()

  const rosterTo = ROSTER_PATH
  const { search } = useLocation()
  const leaveTo = (isNew ? safeReturnPath(search, window.location.origin, RETURN_PARAM) : null) ?? rosterTo

  /* The connection tester is the one control withheld: `POST /api/credentials/test` stays
     `credentials:edit` because it makes the CONTROL PLANE dial an operator-supplied URL from our network
     — a fleet capability, not a row one. Not drawn (not disabled): it could only ever 403. Also keyed on
     `is_operator`: `routes/credentials/` refuses a tenant org outright, and a tenant superadmin holds
     every key. Coarser than the server, knowingly: operator-plane non-superadmin editors lose Test here
     while the route still runs it for them. */
  const { admin } = useAuth()
  const canTest = can(TEST_KEY) && admin?.is_operator === true
  // The operator's "Change org…": an existing credential only, gated inside the hook.
  const changeOrg = useChangeOrgAction('credential')
  /* G2: an Org Owner/Admin or an operator picks who owns a NEW credential — the server's
     `authz.creates_org_rows`, so an operator-plane org admin chooses too; anyone else sends nothing and
     the server makes it theirs. Personal by default, as on the server. Presentation only. The community edition
     has one user on one machine: no owner to choose. */
  const choosesOwner = isNew && createsOrgRows(admin) && isEnabled('multi_org_tenancy')
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
  /* The operator's input, carried across a resize that moves this form between the pane and the full page
     (`hooks/useRemountDraft.ts`); every input below seeds from it. */
  const { restored: draft, keep: keepDraft } = useRemountDraft<CredentialDraft>()
  const [ownership, setOwnership] = useState<CredentialOwnership>(draft?.ownership ?? 'personal')
  /* D6: which member a Personal credential is for; null (or yourself) sends nothing — the caller's own. */
  const [ownerMember, setOwnerMember] = useState<OwnerMember | null>(draft?.ownerMember ?? null)
  /* An org row's home: the org itself (null) or one of its projects. Seeded from the record on edit. */
  const [homeProjectId, setHomeProjectId] = useState<number | null>(draft?.homeProjectId ?? null)
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

  const [name, setName] = useState(draft?.name ?? '')
  const [description, setDescription] = useState(draft?.description ?? '')
  // The explicit dropdown choice; the type in force is DERIVED below (an effect would paint one frame
  // with no type-specific fields).
  const [chosenType, setChosenType] = useState(draft?.chosenType ?? '')
  // Type-specific values by field key. Secrets always START empty (the server never returns them).
  const [values, setValues] = useState<Record<string, string>>(draft?.values ?? {})
  const [fieldErrors, setFieldErrors] = useState<Record<string, string>>({})

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
  const [nvChoice, setNvChoice] = useState<boolean | null>(draft?.nvChoice ?? null)

  const { busy: saving, formError: saveError, setFormError: setSaveError, run: runSave } = useSubmit()
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

  /* `connectError`: what THIS page just did (a refused `start`, an unopenable URL). `connectResult`:
     what happened at the provider, read off the return URL. */
  const { busy: connecting, formError: connectError, setFormError: setConnectError, run: runConnect } = useSubmit()
  /* Through the router, so the strip below stays renderable under test. */
  const [searchParams, setFilters] = useUrlFilters()
  const [connectResult, setConnectResult] = useState<'connected' | 'error' | null>(null)
  const [connectResultReason, setConnectResultReason] = useState('')

  useEffect(() => {
    // Not before a saved row has filled the form: an empty draft would come back as a blanked record.
    if (!isNew && record === null) return
    keepDraft({ name, description, chosenType, values, ownership, ownerMember, homeProjectId, nvChoice })
  })

  /* A non-uuid segment is a stale link (`credentials.id` is a uuid; the API would 422), so
     not-found without a fetch. The guard must track the id domain exactly. The hook's fence drops a late
     response (StrictMode's first mount, a superseded id) that would re-seed `values` over typing. */
  const staleId = id !== undefined && !isUuidShaped(id)
  const { loading, notFound: missing, loadError } = useEditRecord(
    isNew || staleId ? null : id, getCredential, detail => {
      setRecord(detail)
      // A carried-over draft already holds the operator's edits of these.
      if (draft !== null) return
      setName(detail.name)
      setDescription(detail.description ?? '')
      setHomeProjectId(detail.home_project_id ?? null)
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
  const [activeTab, selectTab] = useUrlTab(isNew ? [CREDENTIAL_TABS[0]] : CREDENTIAL_TABS, 'details')
  /* The outcome of a round trip this component did not live through: the callback returns here with
     `?connected=1` or `?connect_error=<slug>`. Consumed and stripped in one pass (`replace: true`, as
     `pages/ProfilePage.tsx`) so reloads and Back do not replay it; the banner state outlives the strip. */
  const connectedParam = searchParams.get(CONNECTED_PARAM)
  const connectErrorParam = searchParams.get(CONNECT_ERROR_PARAM)
  useEffect(() => {
    if (connectedParam === null && connectErrorParam === null) return
    // Anything but the success spelling is a failure: something happened, so say something.
    setConnectResult(connectedParam === '1' ? 'connected' : 'error')
    setConnectResultReason(connectErrorParam ?? '')
    setFilters({ [CONNECTED_PARAM]: '', [CONNECT_ERROR_PARAM]: '' })
    // `setFilters` listed for ProfilePage's reason: no eslint config here to catch a missing dep.
  }, [connectedParam, connectErrorParam, setFilters])

  const typeOptions = useMemo(() => {
    if (record === null || types.some(spec => spec.slug === record.type)) return types
    return [retiredTypeOption(record), ...types]
  }, [types, record])

  // The record's type until another is picked; a new credential opens on `?type=` when the registry knows
  // it, else on the registry's first type.
  const presetType = isNew ? searchParams.get(TYPE_PARAM) : null
  const type = chosenType || record?.type
    || (isNew ? (types.find(spec => spec.slug === presetType) ?? types[0])?.slug ?? '' : '')

  const spec = typeOptions.find(option => option.slug === type)
  /** Why Test is disabled for this type: the server's sentence (its Connect remedy included), or `null` when
   *  the type is testable or names no reason (a retired type, where Test is withheld instead). */
  const untestableReason = spec?.testable === false ? spec.untestable_reason ?? null : null
  const fields: FieldSpec[] = spec?.fields ?? []
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


  /* A row this viewer may only READ (a `credentials:view` holder on someone else's row; the PATCH 404s).
     The server's own `editable` verdict, which no `can(...)` read can derive. `undefined` is NOT a
     refusal — list rows and older clients lack it, and locking them would shut the /profile door. */
  const readOnly = !isNew && record?.editable === false

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
          : createOwnership === 'org' ? (homeProjectName ? [homeProjectName] : [])
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
  /* The community edition has no plans: NodeVault off there is no box at all, never "not in your plan". */
  const nvOffered = !(!isEnabled('billing') && nvInfo?.state === 'not_in_plan' && !wasNodevault)
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
  const connect = spec?.oauth_connect ?? null

  /* Connect needs a saved row: the client id/secret live in the record and the endpoint takes its id. */
  const connectReady = record !== null

  /* A connected claim needs BOTH halves: `?connected=1` is forgeable query string, so it also requires the
     server's `secrets_set` from the load just done (as ProfilePage's success gate). */
  const connected = connectResult === 'connected'
    && (record?.secrets_set.includes(CONNECT_SECRET_KEY) ?? false)

  function changeType(next: string) {
    setChosenType(next)
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
  function validate(): Record<string, string> {
    const errors: Record<string, string> = {}
    if (!name.trim()) errors.name = 'Name is required.'
    for (const field of fields) {
      if ((values[field.key] ?? '').trim()) continue
      if (field.kind === 'secret' && storedSecrets.includes(field.key)) {
        // A blank secret on a record that already holds it means "keep it" — unless the URL moved, or the
        // NodeVault tick changed the key it is sealed under.
        if (nvFlipped) errors[field.key] = `Re-enter ${field.label} to turn NodeVault ${nodevault ? 'on' : 'off'}.`
        else if (destinationChanged) errors[field.key] = `${field.label} must be re-entered because the ${hostLabel} changed.`
        continue
      }
      if (field.required) errors[field.key] = `${field.label} is required.`
    }
    return errors
  }

  /** `buildSealedConfig`, its destination computed from the same `values` the request carries. */
  function buildConfig(sealKey: () => Promise<CredentialSealKey>) {
    const destination = spec === undefined ? '' : sealingDestination(spec, values)
    return buildSealedConfig({ fields, values, credentialId, destination, sealKey })
  }

  async function handleSubmit(event: FormEvent) {
    event.preventDefault()
    // Enter still submits a read-only row, and the PATCH can only 404. Nor before the org is the operator's own.
    if (readOnly || orgBlocked) return
    const errors = validate()
    setFieldErrors(errors)
    setSaveError('')
    if (Object.keys(errors).length > 0) {
      setRefusals(n => n + 1)
      return
    }

    await runSave('', async () => {
      const body = {
        name: name.trim(),
        type,
        description: description.trim() ? description : null,
        // A NodeVault row's secrets go under the org's NodeVault key, so the control plane never reads them.
        config: await buildConfig(nodevault ? nodeVaultKey : sealKeyPromise),
        nodevault,
      }
      if (record !== null) {
        // The home is sent only when it moved: an unchanged PATCH leaves it as stored.
        await updateCredential(record.id, homeMoved ? { ...body, home_project_id: homeProjectId } : body, asOrg)
      } else {
        // The minted id: the envelopes just built are bound to it.
        await createCredential({ ...body, id: newId, ...ownerFields })
      }
      navigate(leaveTo)
    }, {
      mapError: err => {
        setRefusals(n => n + 1)
        const detail = apiDetail(err, '')
        /* The name is taken in this home (409, `DUPLICATE_NAME`) — the server's sentence, on the name it is about.
           Any other 409 keeps the banner. */
        const inline = err instanceof ApiError && err.status === 409 && detail.includes(DUPLICATE_NAME) ? { name: detail }
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

  /** Hand the browser to the provider; the callback writes the row and reports via query params. The URL
   *  goes through `safeHref` even though the CP built it: for a generic OAuth2 row the authorize endpoint
   *  is an operator-typed field. A refused URL is REPORTED, never a silent no-op. */
  async function handleConnect(credentialId: string) {
    /* The previous round trip's verdict is spent once a new one starts. */
    setConnectResult(null)
    setConnectResultReason('')
    await runConnect('Failed to start the connection', async () => {
      const started = await startCredentialConnect(credentialId)
      const href = safeHref(started.authorize_url)
      if (href) navigateExternal(href)
      else setConnectError('The authorization link the server returned is not a usable URL.')
    })
  }

  /* The trail names the roster; Save and Cancel go to `leaveTo` (the roster, or the page that sent the user). */
  const crumbs = (
    <Breadcrumb
      items={[
        { label: 'Secrets', to: rosterTo },
        { label: isNew ? 'New secret' : record?.name ?? 'Secret' },
      ]}
    />
  )

  if (loading || typesLoading || notFound || loadError || typesError) {
    return (
      <DetailStatus
        trail={crumbs}
        loading={loading || typesLoading}
        notFound={notFound && 'Secret not found'}
        error={<BannerText>{loadError || typesError}</BannerText>}
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
        field={field}
        // Only when the record holds this secret; `FormField` wires it into the helper slot for a11y.
        // Once the URL moved the stored value is unkeepable (`destinationChanged`), and the hint says so.
        hint={!holdsStored ? undefined
          : nvFlipped ? `Re-enter to turn NodeVault ${nodevault ? 'on' : 'off'}.`
          : destinationChanged ? `Re-enter: the ${hostLabel} changed.` : 'Leave blank to keep the stored value.'}
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
     other screens read it (Share subscriptions, Connections), so it is seen before Save. */
  const oneOfKeys = new Set((spec?.require_one_of ?? []).flat())
  const outright = (field: FieldSpec) => field.required || oneOfKeys.has(field.key) || field.default != null
  const requiredFields = fields.filter(outright).map(renderField)
  const optionalFields = fields.filter(field => !outright(field)).map(renderField)


  return (
    <DetailPage
      sx={FORM_MEASURE}
      breadcrumb={crumbs}
      title={isNew ? 'New secret' : record?.name}
      actions={!isNew && record ? changeOrg.button({
        id: record.id, name: record.name, org: record.org, href: `/credentials/${record.id}`,
      }) : undefined}
    >
      {!isNew && (
        <Tabs tabs={CREDENTIAL_TABS} tab={activeTab} onSelect={selectTab} label="Secret sections" idPrefix="credential" />
      )}

      <TabPanel id="details" current={activeTab} idPrefix="credential" label={isNew ? 'Details' : undefined}>
      <SectionCard>
        <Box
          component="form"
          ref={formRef}
          onSubmit={event => void handleSubmit(event)}
          sx={{ display: 'flex', flexDirection: 'column' }}
        >
          {saveError && (
            <Banner severity="error">
              <BannerText>{saveError}</BannerText>
            </Banner>
          )}

          {/* No seal key: nothing typed into a secret could leave this page encrypted, so Save and Test
              are withheld below. `testId` because `error-banner` is shared with `saveError`. */}
          {sealKeyError && (
            <Banner severity="error" testId="credential-seal-unavailable">
              <BannerText>{sealKeyError}</BannerText>
            </Banner>
          )}

          <ActingForNotice org={otherOrg} testId="credential-acting-for" />
          {/* Why every control is dead, said once, naming who can act — `routes/credentials/guards.py::_may_edit`:
              an operator edits any row; otherwise a personal row is its owner's alone, an org row needs `credentials:edit`
              org-wide, and a system one also an Org Owner/Admin (`sees_system_credentials`). `testId` because `info-banner` is shared with
              the banners below. */}
          {readOnly && (
            <Banner severity="info" testId="credential-read-only">
              <BannerText>
                {/* An org row's editors are named by the seat that holds the key by default (Owner/Admin), never
                    by the key itself — a permission id is not words. */}
                Read-only — only {record?.ownership === 'personal' ? (record.owner ?? 'its owner') : 'an admin'} can
                {' '}edit it.
              </BannerText>
            </Banner>
          )}

          {/* Delete lives on the roster, which Back/Cancel reaches. */}
          {retired && record && (
            <Banner severity="warning" testId="credential-retired-type">
              <BannerText>
                This console no longer supports the “{record.type_label}” type, so its fields can’t be shown. Delete
                it, or create a secret of a supported type.
              </BannerText>
            </Banner>
          )}

          {choosesOrg && ownOrg && (
            <FormControl sx={{ mb: 1 }}>
              <FormLabel id="credential-org-label">Org</FormLabel>
              <RadioGroup row aria-labelledby="credential-org-label" value={orgChoice} onChange={event => setOrgChoice(event.target.value)}>
                {[ownOrg, ...scopedOthers].map(org => (
                  <FormControlLabel key={org.id} value={org.id} control={<Radio size="small" />} label={org.name} />
                ))}
              </RadioGroup>
            </FormControl>
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
              <BannerText>Act as {actAsOrg.name} to create it there.</BannerText>
            </Banner>
          )}

          {/* The home sits above the name, outside the box; names are lowercase, so typing is lowercased rather than refused. */}
          <CredentialHomeLine path={homePathShown} />
          <FormTextField
            label="Name"
            hint="Name inside this home. Use / for folders."
            error={fieldErrors.name}
            id="credential-name"
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

          {choosesOwner && (
            <OwnershipRadios
              labelId="credential-ownership-label"
              value={ownership}
              onChange={setOwnership}
              sx={{ my: 1 }}
              personalOwner={<OwnerMemberLookup value={ownerMember} onChange={setOwnerMember} selfId={admin?.id} />}
              orgHome={homeField}
            />
          )}
          {/* A saved org row: its home stays editable; ownership itself is create-only. */}
          {!isNew && homeField}

          <FormField label="Type">
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
              onChange={next => {
                if (next !== null && next.key !== type) changeType(next.key)
              }}
              fetchOptions={matchTypes}
              fetchOnFocus
              noOptionsText="No matching secret type"
            />
          </FormField>

          {/* ── NodeVault ────────────────────────────────────────────────────────────────────────────
              Directly under Type: eligibility is the type's first. One line under the box says why it
              cannot be ticked, or, ticked, which key seals it. */}
          {nvOffered && <Box data-testid="credential-nodevault" data-nodevault={nodevault} sx={{ mt: 0.5, mb: 1 }}>
            <FormControlLabel
              sx={{ mr: 0 }}
              control={(
                <Checkbox
                  checked={nodevault}
                  disabled={nvDisabled}
                  onChange={event => changeNodevault(event.target.checked)}
                  slotProps={{ input: { 'aria-describedby': 'credential-nodevault-note' } }}
                />
              )}
              label="NodeVault"
            />
            <Typography
              id="credential-nodevault-note"
              data-testid="credential-nodevault-note"
              variant="caption"
              sx={{ display: 'block', color: 'text.secondary', pl: 4, mt: -0.5, overflowWrap: 'anywhere' }}
            >
              {nodevault
                ? (
                  <>
                    Only your nodes can open its secrets.
                    {nvKeyHash && (
                      <>
                        {' Key '}
                        <Box component="span" data-testid="credential-nodevault-key" title={nvKeyHash} sx={monoSx}>
                          {shortHash(nvKeyHash)}
                        </Box>
                      </>
                    )}
                  </>
                )
                : nvDisabled && !readOnly ? nvBlocker : null}
            </Typography>
          </Box>}

          {/* ── The connect, for a type the control plane can fill ────────────────────────────────
              Same inset ground as the sign-in panel (`google` offers both: two ways to do one job).
              First where both exist: the token is bound to this row's client, refreshable, no node. */}
          {connect && (
            <Box
              sx={theme => ({
                my: 1,
                p: 1.5,
                border: 1,
                borderColor: 'divider',
                borderRadius: 1,
                bgcolor: 'action.hover',
                '& .MuiButton-root': tapTargetSx(theme),
              })}
            >
              {/* Named by where it runs, not the vendor, so it stays distinct from "<provider> sign-in"
                  below (and true for the generic OAuth2 type). */}
              <Typography variant="subtitle2" component="h3" sx={{ fontWeight: 600 }}>
                Browser sign-in
              </Typography>
              {!connectReady && (
                <Typography variant="body2" sx={{ color: 'text.secondary', mt: 0.25 }}>
                  Save the secret first — the flow reads its client id and client secret.
                </Typography>
              )}
              {otherOrg && (
                <Typography variant="body2" sx={{ color: 'text.secondary', mt: 0.25 }} data-testid="credential-connect-act-as">
                  Act as {otherOrg.name} to sign in.
                </Typography>
              )}

              {/* The one setup step this flow cannot do (`api.ts::OAuthConnectSpec.redirect_uri`): the
                  vendor refuses the exchange unless this exact URL is registered, and a wrong guess
                  surfaces as `exchange_failed` after a whole consent round trip. A static prerequisite, so
                  above the banners and button. `ui/CopyableValue` (as `ui/AuthorizeLink`): an insecure
                  context refuses the Clipboard API, so selectable text is the exit that always works. */}
              <Box sx={{ mt: 1 }}>
                <Typography variant="caption" sx={{ display: 'block', color: 'text.secondary' }}>
                  Register this exact URL with the OAuth client — the vendor refuses the exchange
                  otherwise.
                </Typography>
                <CopyableValue
                  value={connect.redirect_uri}
                  copyLabel="Copy redirect URI"
                  sx={{ flex: '1 1 12rem', fontSize: '0.75rem', wordBreak: 'break-all' }}
                />
              </Box>

              {/* What came back from the provider, above `connectError` (it is the more recent event).
                  Success is gated on the record (`connected`); errors claim nothing about stored state. */}
              {connected && (
                <Box sx={{ mt: 1 }}>
                  <Banner severity="success">
                    <BannerText>Connected — a refresh token is stored.</BannerText>
                  </Banner>
                </Box>
              )}
              {connectResult === 'error' && (
                <Box sx={{ mt: 1 }}>
                  <Banner severity="error">
                    <BannerText>{CONNECT_ERRORS[connectResultReason] ?? CONNECT_FALLBACK}</BannerText>
                  </Banner>
                </Box>
              )}
              {connectError && (
                <Box sx={{ mt: 1 }}>
                  <Banner severity="error">
                    <BannerText>{connectError}</BannerText>
                  </Banner>
                </Box>
              )}

              {/* Disabled rather than withheld: the button is the panel's whole subject. `readOnly`
                  because the flow writes this row through the gate that verdict comes from. */}
              <Button
                type="button"
                onClick={() => { if (record !== null) void handleConnect(record.id) }}
                disabled={!connectReady || connecting || saving || readOnly || otherOrg !== null}
                variant="contained"
                color="primary"
                sx={{ mt: 1.25 }}
              >
                {connecting ? 'Connecting…' : connect.action_label}
              </Button>
            </Box>
          )}

          {/* ── The registry field set: the required fields, outright ─────────────────────────────── */}
          {requiredFields}

          {/* ── Advanced: Description and the type's optional fields ─────────────────────────────────
              Folded, never removed (PRD R3): a first credential needs only its name, type and required
              fields. An invalid field inside opens it (`ui/AdvancedSection`). */}
          <AdvancedSection testId="credential-advanced">
            <Box sx={{ display: 'flex', flexDirection: 'column' }}>
              <FormTextField
                label="Description"
                id="credential-description"
                disabled={readOnly}
                multiline
                rows={3}
                value={description}
                onChange={setDescription}
              />
              {optionalFields}
            </Box>
          </AdvancedSection>

          {/* Why this type cannot be tested, BEFORE Test is pressed. The sentence is the registry's
              (`CredentialTypeSpec.untestable_reason`); withheld when a type has none (only a retired,
              synthesised type). Withdrawn once a result or error shows. Deliberately blind to `connect`:
              for a connectable type the reason (with its Connect remedy, served only to who has Connect) is
              what sends the operator there. Test stays drawn but DISABLED, the same sentence on its hover. */}
          {/* NodeVault first: whatever the type, Test would open the secrets on the control plane, which
              cannot (the server's sentence, `untestable_reason`). Only where Test is drawn. */}
          {nodevault && canTest && !readOnly && nvInfo && (
            <Banner severity="info" testId="credential-nodevault-untestable">
              <BannerText>{nvInfo.untestable_reason}</BannerText>
            </Banner>
          )}

          {!nodevault && untestableReason !== null && testResult === null && !testError && (
            /* `testId` because `info-banner` is shared with the result banners below. */
            <Banner severity="info" testId="untestable-reason">
              <BannerText>{untestableReason}</BannerText>
            </Banner>
          )}

          {/* One of the three server outcomes, forked on `status` (`ok` cannot tell failure from unsupported). */}
          {testError && (
            <Banner severity="error">
              <BannerText>{testError}</BannerText>
            </Banner>
          )}
          {testResult?.status === 'success' && (
            <Banner severity="success">
              <BannerText>{testResult.message}</BannerText>
            </Banner>
          )}
          {testResult?.status === 'failure' && (
            <Banner severity="error">
              <BannerText>
                {testResult.message}
                {failureSuffix(testResult)}
              </BannerText>
            </Banner>
          )}
          {testResult?.status === 'unsupported' && (
            <Banner severity="info">
              <BannerText>{testResult.message}</BannerText>
            </Banner>
          )}
          {/* The fork is TOTAL: an unknown fourth status renders neutrally with the server's message. */}
          {testResult !== null &&
            !['success', 'failure', 'unsupported'].includes(testResult.status) && (
              <Banner severity="info">
                <BannerText>{testResult.message}</BannerText>
              </Banner>
            )}

          {/* The form's one action row (`ui/FormActionBar`): one Save, labelled as on every journey form. */}
          <FormActionBar testId="credential-actions" stretchTestIds={['credential-save']}>
            {/* "Back" on a read-only row, "Cancel" where there is an edit to discard. */}
            <Button component={RouterLink} to={leaveTo} variant="text" sx={TAP_FLOOR}>
              {readOnly ? 'Back' : 'Cancel'}
            </Button>
            {/* Withheld, not disabled (see `canTest`); also on a read-only row. Disabled for an untestable type,
                whose reason the banner above already shows. */}
            {canTest && !readOnly && !retired && !otherOrg && (
            <DisabledReason reason={untestableReason ?? undefined}>
              <Button
                type="button"
                onClick={() => void handleTest()}
                data-testid="credential-test"
                title={untestableReason ?? undefined}
                disabled={testing || saving || sealKeyError !== '' || nodevault || nvPending || untestableReason !== null}
                variant="text"
                sx={TAP_FLOOR}
              >
                {testing ? 'Testing…' : 'Test'}
              </Button>
            </DisabledReason>
            )}
            {/* Withheld on a read-only row: the PATCH 404s for this viewer. */}
            {!readOnly && (
            <DisabledReason reason={orgBlocked && !actAsOrg ? 'Choose an org' : undefined}>
            <SubmitButton
              testId="credential-save"
              busy={saving}
              // A NodeVault save never uses the KMS key, so only its own failure withholds it.
              disabled={(sealKeyError !== '' && !nodevault) || nvPending || retired || orgBlocked}
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
      {changeOrg.dialog}
    </DetailPage>
  )
}
