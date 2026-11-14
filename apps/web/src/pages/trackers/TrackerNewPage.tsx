import { useEffect, useRef, useState, type JSX } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { useNavigate } from 'react-router-dom'
import Box from '@mui/material/Box'
import { ApiError, apiDetail } from '../../api'
import { useAuth } from '../../auth'
import Breadcrumb from '../../components/Breadcrumb'
import { useLeaveGuard } from '../../hooks/useLeaveGuard'
import { useSubmit } from '../../hooks/useSubmit'
import { useActingAs } from '../../lib/actAs'
import { useOrgScope } from '../../lib/orgScope'
import { FONT_SIZE } from '../../theme'
import Banner from '../../ui/Banner'
import CancelSaveBar from '../../ui/CancelSaveBar'
import CredentialSelect, { type CredentialOption } from '../../ui/CredentialSelect'
import { credentialMark } from '../../ui/credentialMarks'
import DetailPage, { DETAIL_SECTIONS_SX, DetailSection } from '../../ui/detail/DetailPage'
import FormField from '../../ui/FormField'
import FormBox from '../../ui/FormBox'
import FormTextField from '../../ui/FormTextField'
import ExternalLink from '../../ui/ExternalLink'
import { OrgAside, useOrgVisible } from '../../ui/OrgIndicator'
import SecretOnceDialog from '../../ui/SecretOnceDialog'
import { LeaveGuardDialog } from '../../ui/UnsavedChangesDialog'
import { loadCredentialOptions, type Credential } from '../credentials/api'
import type { OrgRef } from '../../types'
import {
  createTrackerConnection,
  trackerKey,
  trackerLabel,
  trackerPath,
  TRACKERS_KEY,
  TRACKERS_PATH,
  type TrackerConnected,
} from './api'
import { WebhookUrlField } from './WebhookUrlField'

/* ADD A TRACKER — Issue trackers' New, the connection's page in create mode (`/integrations/trackers/new`). Tracker,
 * Secret (the org's credential of that tracker's type; a Jira Forge app holds its own token, so it takes none), and
 * Site (Jira and Backlog). Create is `POST /api/tracker-connections`; it lands on the new connection's Mapping tab.
 * What the tracker must be given to post to Jaah is answered this once: the URL and signing secret (a Jira bot user)
 * or the claim code (a Jira Forge app) show in a one-time dialog before the page moves on; Backlog needs neither, as
 * Jaah adds its webhooks itself. A tracker whose
 * adapter is not shipped answers 409: its sentence is the Tracker field's error.
 *
 * A connection is made in the caller's org, and its automation runs as the caller: an operator connects only into its
 * own org (filtered to exactly it). Under another org's view, or acting as another org, the page says a member of that
 * org connects it — the API refuses an owner from outside the org (`trackers/connections.py::connect`), since every
 * start would be refused. The title names the org. */

type Tracker = 'linear' | 'jira' | 'backlog'
type JiraAuth = 'basic' | 'forge'

const TRACKERS: readonly Tracker[] = ['linear', 'jira', 'backlog']
const HAS_SITE: readonly Tracker[] = ['jira', 'backlog']
/** Trackers whose adapter registers its own webhooks at connect (`app/trackers/backlog.py`): nothing to show once. */
const SELF_HOOKED: readonly string[] = ['backlog']

const FORM_ID = 'tracker-new-form'

type Draft = { tracker: Tracker; jiraAuth: JiraAuth; credentialId: string | null; site: string }
const EMPTY: Draft = { tracker: 'linear', jiraAuth: 'basic', credentialId: null, site: '' }

/** The connect's `auth_kind`: Jira by an API token or its Forge app; Linear and Backlog by whichever their secret
 *  holds — an API key, else its OAuth sign-in. */
function authKindOf(draft: Draft, credential: Credential | undefined): string {
  if (draft.tracker === 'jira') return draft.jiraAuth
  return credential?.secrets_set.includes('api_key') ? 'basic' : 'oauth'
}

/** The 409 of a tracker whose adapter is not shipped (`connections.connect`); every other 409 is the banner's. */
const NOT_YET = /^tracker:\s*(.+can't be connected yet)$/

const needsSecret = (draft: Draft) => !(draft.tracker === 'jira' && draft.jiraAuth === 'forge')

/** A secret's site: the host of its base URL (`base_url`), or empty. */
function siteOf(credential: Credential | undefined): string {
  const url = credential?.config.base_url
  if (!url) return ''
  try {
    return new URL(url).host
  } catch {
    return ''
  }
}

/** The one-time values, by kind: what the dialog shows. */
type Reveal = { id: string; name: string; secret: string; kind: 'webhook' | 'claim'; ingressUrl: string; installUrl: string | null }

function revealOf(answer: TrackerConnected): Reveal | null {
  const { connection } = answer
  if (answer.claim_code) return { id: connection.id, name: connection.name, secret: answer.claim_code, kind: 'claim', ingressUrl: answer.ingress_url, installUrl: connection.install_url ?? null }
  if (answer.webhook_secret) return { id: connection.id, name: connection.name, secret: answer.webhook_secret, kind: 'webhook', ingressUrl: answer.ingress_url, installUrl: null }
  return null
}

const mappingPath = (id: string) => `${trackerPath(id)}?tab=mapping`

export default function TrackerNewPage(): JSX.Element {
  const { admin } = useAuth()
  const operator = useOrgVisible()
  const acting = useActingAs()
  const scope = useOrgScope()
  const own = admin?.org ?? null
  const ownOnly = own !== null && scope?.length === 1 && scope[0].id === own.id
  const actingElsewhere = acting !== null && acting.id !== own?.id
  if (operator && (actingElsewhere || (!acting && !ownOnly))) {
    return (
      <DetailPage
        testId="tracker-new"
        breadcrumb={<Breadcrumb items={[{ label: 'Issue trackers', to: TRACKERS_PATH }, { label: 'Add tracker' }]} />}
        title="Add tracker"
      >
        <Banner severity="info" testId="tracker-org-member-only">
          {`An Owner or Admin of the tracker’s org connects it, so its tasks start as one of them${own ? `. To add one to ${own.name}, filter to it` : ''}.`}
        </Banner>
      </DetailPage>
    )
  }
  // An unacting operator's connect says it lands in the operator org (the API refuses it otherwise) — by role, not by
  // the org picker's visibility: an edition without multi-org has no org to act as.
  return <TrackerNewForm org={acting ?? own} inOperatorOrg={admin?.is_operator === true && !acting} />
}

function TrackerNewForm({ org, inOperatorOrg }: { org: OrgRef | null; inOperatorOrg: boolean }): JSX.Element {
  const client = useQueryClient()
  const navigate = useNavigate()
  const [draft, setDraft] = useState<Draft>(EMPTY)
  const [trackerError, setTrackerError] = useState('')
  const [reveal, setReveal] = useState<Reveal | null>(null)
  /** The connection once made: the form is spent, and leaving asks nothing. */
  const [createdId, setCreatedId] = useState<string | null>(null)
  const created = createdId !== null
  /** Every secret the picker has listed, by id: the chosen one's type of sign-in and site. */
  const seen = useRef(new Map<string, Credential>())
  const { busy, formError, setFormError, run } = useSubmit()
  const secretNeeded = needsSecret(draft)
  const problem = secretNeeded && !draft.credentialId ? 'Pick a secret' : null
  const dirty = !created && (draft.credentialId !== null || draft.site.trim() !== '' || draft.tracker !== EMPTY.tracker)

  const edit = (patch: Partial<Draft>) => {
    setDraft(d => ({ ...d, ...patch }))
    setTrackerError('')
    setFormError('')
  }

  // A NodeVault secret opens only on the org's nodes, so the connect refuses it: not offered.
  const fetchSecrets = (query: string, signal: AbortSignal): Promise<CredentialOption[]> =>
    loadCredentialOptions(query, signal, row => {
      seen.current.set(row.id, row)
      return { id: row.id, name: row.name, hint: row.description?.trim() || row.type_label, icon: credentialMark(row.type, row.type_label) }
    }, { types: [draft.tracker], keep: rows => rows.filter(row => !row.nodevault) })

  const pickSecret = (id: string | null) => {
    const credential = id ? seen.current.get(id) : undefined
    // A secret names its site: fill an empty Site from it.
    edit({ credentialId: id, ...(draft.site.trim() === '' && HAS_SITE.includes(draft.tracker) ? { site: siteOf(credential) } : {}) })
  }

  // A tracker whose adapter is not shipped is the Tracker field's sentence; the rest is the banner's.
  const mapError = (err: unknown, fallback: string): string => {
    const detail = apiDetail(err, fallback)
    const notYet = err instanceof ApiError && err.status === 409 ? NOT_YET.exec(detail) : null
    if (notYet) {
      setTrackerError(notYet[1]!)
      return ''
    }
    return detail
  }

  const save = async (): Promise<boolean> => {
    if (problem) return false
    const credential = draft.credentialId ? seen.current.get(draft.credentialId) : undefined
    const landed = await run('Could not connect. Retry.', () => createTrackerConnection({
      tracker: draft.tracker,
      auth_kind: authKindOf(draft, credential),
      credential_id: secretNeeded ? draft.credentialId : null,
      site: HAS_SITE.includes(draft.tracker) ? draft.site.trim() || null : null,
      ...(inOperatorOrg ? { in_operator_org: true } : {}),
    }), { mapError })
    if (!landed) return false
    const answer = landed.value
    client.setQueryData(trackerKey(answer.connection.id), answer.connection)
    void client.invalidateQueries({ queryKey: TRACKERS_KEY })
    setReveal(SELF_HOOKED.includes(answer.connection.tracker) ? null : revealOf(answer))
    setCreatedId(answer.connection.id)
    return true
  }
  // Saving from the leave prompt never leaves: the page shows the one-time values, then moves to Mapping itself.
  const guard = useLeaveGuard(dirty, async () => {
    await save()
    return false
  })

  // Moves on once the guard sees the form spent and any one-time values were shown. `replace`: Back lands on the
  // list, never on a create form that would connect a second time.
  useEffect(() => {
    if (createdId && !reveal) navigate(mappingPath(createdId), { replace: true })
  }, [createdId, reveal, navigate])

  const closeReveal = () => setReveal(null)

  const disabled = busy || created
  const trackerOptions = TRACKERS.map(t => ({ value: t, label: trackerLabel(t) }))

  return (
    <DetailPage
      testId="tracker-new"
      breadcrumb={<Breadcrumb items={[{ label: 'Issue trackers', to: TRACKERS_PATH }, { label: 'Add tracker' }]} />}
      title="Add tracker"
      titleAside={<OrgAside label="Org" org={org} chipTestId="tracker-new-org" />}
      actionBar={
        <CancelSaveBar
          testId="tracker-new-actions"
          saveTestId="tracker-new-save"
          creating
          saveLabel="Connect"
          busyLabel="Connecting…"
          cancelTo={TRACKERS_PATH}
          dirty={dirty}
          busy={busy}
          disabled={problem !== null || created}
          saveReason={problem ?? undefined}
          saveProps={{ form: FORM_ID }}
        />
      }
    >
      {formError && <Banner severity="error" testId="tracker-new-error">{formError}</Banner>}
      <FormBox id={FORM_ID} onSubmit={save} sx={DETAIL_SECTIONS_SX}>
        <DetailSection fields testId="tracker-new-fields">
          <FormTextField
            label="Tracker"
            value={draft.tracker}
            options={trackerOptions}
            disabled={disabled}
            error={trackerError || undefined}
            testId="tracker-new-tracker"
            onChange={value => edit({ tracker: value as Tracker, credentialId: null, site: '' })}
          />
          {draft.tracker === 'jira' && (
            <FormTextField
              label="Sign-in"
              help="API token: a Jira secret, and a webhook. Forge app: Jaah's Jira app, claimed with a code."
              value={draft.jiraAuth}
              options={[{ value: 'basic', label: 'API token' }, { value: 'forge', label: 'Forge app' }]}
              disabled={disabled}
              testId="tracker-new-auth"
              onChange={value => edit({ jiraAuth: value as JiraAuth, credentialId: null })}
            />
          )}
          {secretNeeded && (
            <FormField label="Secret" required help={`The ${trackerLabel(draft.tracker)} secret Jaah acts with.`}>
              <CredentialSelect
                // A new tracker is a new roster: remount, so the box forgets the last tracker's rows.
                key={draft.tracker}
                id="tracker-new-credential"
                value={draft.credentialId}
                onChange={pickSecret}
                fetchOptions={fetchSecrets}
                minChars={0}
                placeholder={`Search ${trackerLabel(draft.tracker)} secrets`}
                noOptionsText={`No ${trackerLabel(draft.tracker)} secret`}
                disabled={disabled}
                testId="tracker-new-credential"
              />
            </FormField>
          )}
          {HAS_SITE.includes(draft.tracker) && (
            <FormTextField
              label="Site"
              help={secretNeeded ? "The tracker's host. Filled from the secret." : "The tracker's host."}
              value={draft.site}
              placeholder={draft.tracker === 'jira' ? 'acme.atlassian.net' : 'acme.backlog.com'}
              disabled={disabled}
              testId="tracker-new-site"
              onChange={value => edit({ site: value })}
            />
          )}
        </DetailSection>
      </FormBox>
      <SecretOnceDialog
        secret={reveal?.secret ?? null}
        title={reveal ? `${reveal.name}: ${reveal.kind === 'claim' ? 'claim code' : 'webhook'}` : ''}
        notice={reveal?.kind === 'claim'
          ? 'Install Jaah’s Jira app, then enter this code on its Jira admin page (Apps › Jaah) within 24 hours. You won’t see it again.'
          : 'Add a webhook in the tracker with this URL and secret. You won’t see the secret again.'}
        submitLabel="Done"
        testIdPrefix="tracker-secret"
        copyLabel={reveal?.kind === 'claim' ? 'Copy code' : 'Copy secret'}
        onClose={closeReveal}
      >
        {reveal?.kind === 'claim' && reveal.installUrl && (
          <Box sx={{ display: 'flex', flexDirection: 'column', gap: 0.5 }}>
            <ExternalLink href={reveal.installUrl} data-testid="tracker-secret-install">Install Jaah’s Jira app</ExternalLink>
            <Box component="span" sx={{ fontSize: FONT_SIZE.sm, mt: 1 }}>Code</Box>
          </Box>
        )}
        {reveal?.kind === 'webhook' && <WebhookUrlField url={reveal.ingressUrl} testId="tracker-secret-url" />}
      </SecretOnceDialog>
      <LeaveGuardDialog leave={guard} message="This tracker is not connected yet. Connect it before leaving?" />
    </DetailPage>
  )
}
