import { type JSX, useCallback, useState } from 'react'
import { useParams, useSearchParams } from 'react-router-dom'
import { useQueryClient } from '@tanstack/react-query'
import Box from '@mui/material/Box'
import Typography from '@mui/material/Typography'
import Breadcrumb from '../../../components/Breadcrumb'
import { TabPanel, Tabs, type TabDef } from '../../../components/Tabs'
import { useEditRecord } from '../../../hooks/useEditRecord'
import { useIsOrgAdmin } from '../../../hooks/useIsOrgAdmin'
import { useLeaveGuard } from '../../../hooks/useLeaveGuard'
import { useSubmit } from '../../../hooks/useSubmit'
import { useUrlTab } from '../../../hooks/useUrlTab'
import Banner from '../../../ui/Banner'
import { DetailField, DetailPage, DetailSection } from '../../../ui/detail/DetailPage'
import { EditStatus } from '../../../ui/detail/DetailStatus'
import CancelSaveBar from '../../../ui/CancelSaveBar'
import { EntityNameFields } from '../../../ui/EntityNameFields'
import FormBox from '../../../ui/FormBox'
import FormTextField from '../../../ui/FormTextField'
import { FORM_SX, TEXT_INPUT } from '../../../ui/formDensity'
import { monoSx } from '../../../ui/listStyles'
import { OverflowMenu } from '../../../ui/OverflowMenu'
import { LeaveGuardDialog } from '../../../ui/UnsavedChangesDialog'
import { strOrNull } from '../../../lib/strings'
import { urlShapeWarning } from '../../../lib/urlShape'
import {
  type AuthScheme,
  type ConnectorKind,
  connectorUrl,
  createConnector,
  CUSTOM_GALLERY_URL,
  type CustomConnector,
  type CustomConnectorBody,
  deleteConnector,
  FREE_FORM_SCHEMES,
  getConnector,
  KIND_LABEL,
  type McpTransport,
  SCHEME_LABEL,
  SCHEMES_BY_KIND,
  TRANSPORT_LABEL,
  updateConnector,
} from './api'
import ConnectionsTab from './ConnectionsTab'
import { danger } from '../../../components/ConfirmDialog'

/* One custom connector: `/credentials/custom/new?kind=rest|mcp` (`mode="create"`, the definition only) and
 * `/credentials/custom/:id` (Definition + Connections). Tabbed detail on NodeLinkEditPage's shape. The definition
 * edits in place for an Org Owner/Admin (the server's `@org_admin` decides again) and reads as facts for a member.
 * Which fields apply follows the kind and auth scheme (`custom.checked_fields`); a field that does not apply is
 * sent as null, and every refusal is the server's own sentence. */

type Tab = 'definition' | 'connections'

const TABS: readonly TabDef<Tab>[] = [
  { id: 'definition', label: 'Definition' },
  { id: 'connections', label: 'Connections' },
]

/** The form's fields, as typed. */
type Draft = {
  name: string
  description: string
  baseUrl: string
  scheme: AuthScheme
  headerName: string
  valuePrefix: string
  authorizeUrl: string
  tokenUrl: string
  scope: string
  testPath: string
  transport: McpTransport
}

const emptyDraft = (kind: ConnectorKind): Draft => ({
  name: '', description: '', baseUrl: '', scheme: SCHEMES_BY_KIND[kind][0], headerName: '',
  valuePrefix: kind === 'rest' ? 'Bearer' : '', authorizeUrl: '', tokenUrl: '', scope: '', testPath: '',
  transport: 'streamable_http',
})

const draftOf = (row: CustomConnector): Draft => ({
  name: row.name, description: row.description, baseUrl: row.base_url, scheme: row.auth_scheme,
  headerName: row.header_name ?? '', valuePrefix: row.value_prefix ?? '', authorizeUrl: row.oauth_authorize_url ?? '',
  tokenUrl: row.oauth_token_url ?? '', scope: row.oauth_scope ?? '', testPath: row.test_path ?? '',
  transport: row.mcp_transport ?? 'streamable_http',
})

/** What picking each Auth scheme makes a connection ask for (`ConnectionsTab`'s inputs). */
const SCHEME_HELP: Record<AuthScheme, string> = {
  bearer: 'Sends a key as a bearer token.',
  header_key: 'Sends a key in a header you name.',
  basic: 'Sends a login and password.',
  oauth2_client: 'Signs in at the service through OAuth.',
  none: 'Sends no credentials.',
}

/** What picking each MCP transport does. */
const TRANSPORT_HELP: Record<McpTransport, string> = {
  streamable_http: 'One HTTP endpoint, streaming replies.',
  sse: 'The older Server-Sent Events transport.',
}

/** Which optional fields this kind and scheme take. */
function applies(kind: ConnectorKind, scheme: AuthScheme) {
  const rest = kind === 'rest'
  return {
    header: scheme === 'header_key',
    prefix: rest && (scheme === 'bearer' || scheme === 'header_key'),
    endpoints: rest && scheme === 'oauth2_client',
    scope: scheme === 'oauth2_client',
    testPath: rest,
    transport: !rest,
  }
}

/** The whole definition the API takes: a field that does not apply is null. */
export function bodyOf(kind: ConnectorKind, draft: Draft): CustomConnectorBody {
  const on = applies(kind, draft.scheme)
  return {
    name: draft.name.trim(),
    description: draft.description.trim(),
    kind,
    base_url: draft.baseUrl.trim(),
    auth_scheme: draft.scheme,
    header_name: on.header ? strOrNull(draft.headerName) : null,
    value_prefix: on.prefix ? strOrNull(draft.valuePrefix) : null,
    oauth_authorize_url: on.endpoints ? strOrNull(draft.authorizeUrl) : null,
    oauth_token_url: on.endpoints ? strOrNull(draft.tokenUrl) : null,
    oauth_scope: on.scope ? strOrNull(draft.scope) : null,
    test_path: on.testPath ? strOrNull(draft.testPath) : null,
    mcp_transport: on.transport ? draft.transport : null,
  }
}

const kindOf = (raw: string | null): ConnectorKind => (raw === 'mcp' ? 'mcp' : 'rest')

const Trail = ({ current }: { current: string }) => (
  <Breadcrumb items={[{ label: 'Secrets', to: '/credentials' }, { label: 'Custom', to: CUSTOM_GALLERY_URL }, { label: current }]} />
)

export default function CustomConnectorPage({ mode }: { mode?: 'create' }): JSX.Element {
  const { id } = useParams<{ id: string }>()
  const [searchParams] = useSearchParams()
  const creating = mode === 'create'
  const admin = useIsOrgAdmin()
  const queryClient = useQueryClient()

  const [detail, setDetail] = useState<CustomConnector | null>(null)
  const kind: ConnectorKind = detail?.kind ?? kindOf(searchParams.get('kind'))
  const [draft, setDraft] = useState<Draft>(() => emptyDraft(kind))
  const { busy: saving, formError, setFormError, run, saved, clearSaved } = useSubmit()

  const seed = useCallback((row: CustomConnector) => {
    setDetail(row)
    setDraft(draftOf(row))
  }, [])

  /* Nothing is fetched for a connector already held: after a create the router reuses this instance. */
  const { loading, notFound, loadError, confirm, confirmDialog } = useEditRecord(
    creating || detail?.id === id ? null : id ?? null, getConnector, seed,
    { loadFallback: 'Could not load the connector.', notFoundStatuses: [404, 422] },
  )

  const [tab, selectTab] = useUrlTab(creating ? [TABS[0]] : TABS, 'definition')

  const body = bodyOf(kind, draft)
  const dirty = detail ? JSON.stringify(body) !== JSON.stringify(bodyOf(kind, draftOf(detail))) : body.name !== '' || body.base_url !== ''
  const complete = body.name !== '' && body.base_url !== ''

  const changed = useCallback(() => void queryClient.invalidateQueries({ queryKey: ['custom-connectors'] }), [queryClient])

  /** The Save; true once it landed (the leave dialog's Save waits on it). */
  const save = async (): Promise<boolean> => {
    let ok = false
    await run(detail ? 'Could not save. Retry.' : 'Could not create the connector. Retry.', async () => {
      if (detail) {
        seed(await updateConnector(detail.id, body))
      } else {
        const created = await createConnector(body)
        seed(created)
        // `replace`: Back after creating lands on the list, never a create form that would make a second one.
        leave.leaveNow(connectorUrl(created.id, 'connections'), { replace: true })
      }
      changed()
      ok = true
    })
    return ok
  }
  const leave = useLeaveGuard(admin && dirty, save)

  if (!creating && (loading || notFound || loadError || !detail)) {
    return (
      <EditStatus
        trail={<Trail current="Connector" />}
        loading={loading} notFound={notFound} loadError={loadError} notFoundTitle="Connector not found"
      />
    )
  }

  const edit = (patch: Partial<Draft>) => {
    setDraft(prev => {
      const next = { ...prev, ...patch }
      // A bearer token is sent as `Bearer <token>` unless the prefix says otherwise.
      if (patch.scheme === 'bearer' && kind === 'rest' && prev.valuePrefix.trim() === '') next.valuePrefix = 'Bearer'
      // A header key is usually sent alone: the bearer default does not follow it there.
      if (patch.scheme === 'header_key' && prev.valuePrefix.trim() === 'Bearer') next.valuePrefix = ''
      return next
    })
    setFormError('')
    clearSaved()
  }

  async function remove() {
    if (!detail) return
    if (!await confirm(danger('Delete', {
      title: `Delete ${detail.name}?`,
      identity: <Typography component="span" sx={monoSx}>{detail.slug}</Typography>,
      message: 'The definition is deleted. Refused while connections use it.',
      run: { action: () => deleteConnector(detail.id), failed: `Could not delete ${detail.name}. Retry.` },
    }))) return
    changed()
    leave.leaveNow(CUSTOM_GALLERY_URL, { replace: true })
  }

  const heading = detail?.name ?? (kind === 'mcp' ? 'New MCP server' : 'New connector')
  const on = applies(kind, draft.scheme)

  return (
    <DetailPage
      breadcrumb={<Trail current={heading} />}
      title={heading}
      subtitle={KIND_LABEL[kind]}
      actions={detail && admin && (
        <OverflowMenu
          ariaLabel={`${detail.name} actions`}
          testId="custom-actions"
          items={[{ key: 'delete', label: 'Delete', ariaLabel: `Delete ${detail.name}`, danger: true, testId: 'custom-delete', onClick: () => void remove() }]}
        />
      )}
      testId="custom-connector-page"
    >
      {!creating && <Tabs tabs={TABS} tab={tab} onSelect={selectTab} label="Connector sections" idPrefix="custom" />}

      <TabPanel id="definition" current={tab} idPrefix="custom" label={creating ? 'Definition' : undefined}>
        {admin ? (
          <FormBox onSubmit={save} sx={FORM_SX} data-testid="custom-form">
            {formError && <Banner severity="error" testId="custom-form-error">{formError}</Banner>}
            <EntityNameFields
              idPrefix="custom"
              name={{ value: draft.name, onChange: v => edit({ name: v }), autoFocus: creating, help: 'Shown in the connector list; new connections default to it.' }}
              description={{ value: draft.description, onChange: v => edit({ description: v }), help: 'Shown under its name in the connector list.' }}
            />
            <FormTextField label={kind === 'rest' ? 'Base URL' : 'Server URL'} id="custom-url" value={draft.baseUrl}
              onChange={v => edit({ baseUrl: v })} required
              help={kind === 'rest' ? 'Every connection\'s requests and Test go here.' : 'Where tasks reach the MCP server.'}
              warning={kind === 'rest' && FREE_FORM_SCHEMES.has(draft.scheme) ? urlShapeWarning(draft.baseUrl) : undefined}
              placeholder={kind === 'rest' ? 'https://api.example.com' : 'https://mcp.example.com/mcp'} slotProps={TEXT_INPUT} />
            <FormTextField label="Auth" id="custom-scheme" value={draft.scheme} onChange={v => edit({ scheme: v as AuthScheme })}
              help={{ tip: 'How a connection signs in; decides what it asks for.', docs: 'console-reference/directory/credentials' }}
              options={SCHEMES_BY_KIND[kind].map(s => ({ value: s, label: SCHEME_LABEL[s], help: SCHEME_HELP[s] }))} />
            {on.header && (
              <FormTextField label="Header" id="custom-header" value={draft.headerName} onChange={v => edit({ headerName: v })}
                required placeholder="X-API-Key" help="The request header the key is sent in." slotProps={TEXT_INPUT} />
            )}
            {on.prefix && (
              <FormTextField label="Prefix" id="custom-prefix" value={draft.valuePrefix} onChange={v => edit({ valuePrefix: v })}
                help="Sent before the key. Blank for none." slotProps={TEXT_INPUT} />
            )}
            {on.endpoints && (
              <>
                <FormTextField label="Authorize URL" id="custom-authorize" value={draft.authorizeUrl}
                  onChange={v => edit({ authorizeUrl: v })} required help="Where Connect sends the person to sign in." slotProps={TEXT_INPUT} />
                <FormTextField label="Token URL" id="custom-token" value={draft.tokenUrl}
                  onChange={v => edit({ tokenUrl: v })} required help="Where the sign-in code is exchanged for a token." slotProps={TEXT_INPUT} />
              </>
            )}
            {on.scope && (
              <FormTextField label="Scope" id="custom-scope" value={draft.scope} onChange={v => edit({ scope: v })}
                help={kind === 'mcp'
                  ? { tip: 'What sign-in asks for, space-separated.', more: 'Endpoints are discovered from the server URL.' }
                  : 'What sign-in asks for, space-separated.'}
                slotProps={TEXT_INPUT} />
            )}
            {on.testPath && (
              <FormTextField label="Test path" id="custom-test-path" value={draft.testPath} onChange={v => edit({ testPath: v })}
                placeholder="/v1/me" help="Test sends a GET here." slotProps={TEXT_INPUT} />
            )}
            {on.transport && (
              <FormTextField label="Transport" id="custom-transport" value={draft.transport}
                onChange={v => edit({ transport: v as McpTransport })}
                help="How tasks talk to the MCP server."
                options={(Object.keys(TRANSPORT_LABEL) as McpTransport[]).map(t => ({ value: t, label: TRANSPORT_LABEL[t], help: TRANSPORT_HELP[t] }))} />
            )}
            <CancelSaveBar testId="custom-form-actions" saveTestId="custom-save" cancelTo={CUSTOM_GALLERY_URL}
              creating={creating} busy={saving} disabled={!complete || !dirty} saved={saved} />
          </FormBox>
        ) : detail && <Facts row={detail} />}
      </TabPanel>

      {detail && tab === 'connections' && (
        <TabPanel id="connections" current={tab} idPrefix="custom">
          <ConnectionsTab connector={detail} onChange={row => { setDetail(row); changed() }} />
        </TabPanel>
      )}

      {confirmDialog}
      <LeaveGuardDialog leave={leave} message="This connector has unsaved changes. Save them before leaving?" />
    </DetailPage>
  )
}

/** A member's read-only view of the definition. */
function Facts({ row }: { row: CustomConnector }) {
  const on = applies(row.kind, row.auth_scheme)
  const mono = (text: string | null) => <Box component="span" sx={monoSx}>{text || '—'}</Box>
  return (
    <Box sx={{ pt: 2 }}>
      <DetailSection fields testId="custom-facts">
        {row.description && <DetailField label="Description">{row.description}</DetailField>}
        <DetailField label={row.kind === 'rest' ? 'Base URL' : 'Server URL'}>{mono(row.base_url)}</DetailField>
        <DetailField label="Auth">{SCHEME_LABEL[row.auth_scheme]}</DetailField>
        {on.header && <DetailField label="Header">{mono(row.header_name)}</DetailField>}
        {on.prefix && <DetailField label="Prefix">{mono(row.value_prefix)}</DetailField>}
        {on.endpoints && <DetailField label="Authorize URL">{mono(row.oauth_authorize_url)}</DetailField>}
        {on.endpoints && <DetailField label="Token URL">{mono(row.oauth_token_url)}</DetailField>}
        {on.scope && <DetailField label="Scope">{mono(row.oauth_scope)}</DetailField>}
        {on.testPath && <DetailField label="Test path">{mono(row.test_path)}</DetailField>}
        {on.transport && row.mcp_transport && <DetailField label="Transport">{TRANSPORT_LABEL[row.mcp_transport]}</DetailField>}
      </DetailSection>
    </Box>
  )
}
