import { type FormEvent, type JSX, useState } from 'react'
import { Link as RouterLink } from 'react-router-dom'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import Link from '@mui/material/Link'
import { apiDetail } from '../../../api'
import { useAuth } from '../../../auth'
import { useBusyIds } from '../../../hooks/useBusyIds'
import { useSubmit } from '../../../hooks/useSubmit'
import Banner from '../../../ui/Banner'
import { DetailSection } from '../../../ui/detail/DetailPage'
import EmptyState from '../../../ui/EmptyState'
import FeedList from '../../../ui/feed/FeedList'
import type { FeedRowSpec } from '../../../ui/feed/types'
import FormTextField from '../../../ui/FormTextField'
import { FORM_SX, TEXT_INPUT } from '../../../ui/formDensity'
import { StatusChip } from '../../../ui/StatusChip'
import type { Presentation } from '../../../ui/statusTone'
import SubmitButton from '../../../ui/SubmitButton'
import OwnershipRadios from '../OwnershipRadios'
import { createsOrgRows, mintCredentialId } from '../ownership'
import { fetchSealKey, sealSecret, type CredentialSealKey } from '../../../lib/credentialSeal'
import { isHttpUrl } from '../../../lib/urlShape'
import { grid } from '../../../theme'
import {
  type ConnectionBody,
  connectionUrl,
  createConnection,
  type CustomConnection,
  type CustomConnector,
  getConnector,
  SEALED_INPUTS,
  type SecretInput,
  testConnection,
} from './api'

/* A connector's Connections tab: the connections (secrets) to it the caller sees, each linking to its secret page
 * (`/credentials/:id`) with its health and a Test, and Connect — a new secret of the connector's generic type
 * (`POST …/connections`). Only the inputs the scheme takes are asked for (`secret_inputs`). An OAuth2 REST
 * connection finishes on its secret's page, where Connect lives (`CredentialEditPage`). */

const INPUT_LABEL: Record<SecretInput, string> = {
  api_key: 'API key',
  login: 'Login',
  password: 'Password',
  client_id: 'Client ID',
  client_secret: 'Client secret',
  bearer_token: 'Token',
}
/** What each input is used for, by the connector's Auth. */
const INPUT_HELP: Record<SecretInput, string> = {
  api_key: 'Sent on each request the way Auth says.',
  login: 'Sent with Password on each request.',
  password: 'Sent with Login on each request.',
  client_id: 'Your OAuth app\'s client, used to sign in.',
  client_secret: 'Your OAuth app\'s secret, used to sign in.',
  bearer_token: 'Sent to the MCP server as a bearer token.',
}
/** Asked in this order; every one but the client id is a secret. */
const INPUT_ORDER: readonly SecretInput[] = ['login', 'password', 'api_key', 'bearer_token', 'client_id', 'client_secret']

const OWNER_LABEL = { org: 'Org', personal: 'Personal' } as const

export function healthOf(row: CustomConnection): Presentation {
  if (row.needs_reconnect) return { label: 'Needs reconnect', tone: 'warning', glyph: '!' }
  if (row.last_test_ok === true) return { label: 'OK', tone: 'success', glyph: '●' }
  if (row.last_test_ok === false) return { label: 'Test failed', tone: 'error', glyph: '✕' }
  return { label: 'Untested', tone: 'neutral', glyph: '○' }
}

type TestNote = { ok: boolean; text: string }

export default function ConnectionsTab({ connector, onChange }: {
  connector: CustomConnector
  /** The connector re-read after a write, so its connections and their health are current. */
  onChange: (row: CustomConnector) => void
}): JSX.Element {
  const { admin } = useAuth()
  const orgOwner = createsOrgRows(admin)
  const inputs = INPUT_ORDER.filter(key => connector.secret_inputs[key] !== undefined)
  // OAuth sign-in (a REST OAuth2 client, or an MCP server's own OAuth) finishes on the secret's page, where Connect lives.
  const oauth = connector.auth_scheme === 'oauth2_client'
  // A free-form Base URL that is not http(s) saves, but Test cannot dial it: disabled, with no message (asked for).
  const dialable = connector.kind !== 'rest' || isHttpUrl(connector.base_url)

  const [name, setName] = useState('')
  const [owner, setOwner] = useState<'org' | 'personal'>('personal')
  const [values, setValues] = useState<Partial<Record<SecretInput, string>>>({})
  const [created, setCreated] = useState<CustomConnection | null>(null)
  const [note, setNote] = useState<TestNote | null>(null)
  const { busy, formError, setFormError, run } = useSubmit()
  const [testing, withTesting] = useBusyIds()

  const refresh = async () => {
    try { onChange(await getConnector(connector.id)) } catch { /* the rows stay; the next visit re-reads */ }
  }

  const complete = name.trim() !== '' && inputs.every(key => !connector.secret_inputs[key] || (values[key] ?? '').trim() !== '')

  const submit = (event: FormEvent) => {
    event.preventDefault()
    void run('Could not connect. Retry.', async () => {
      // Sealed here, under an id this page mints, so the API never sees a secret in the clear (`credentialSeal.ts`).
      const id = mintCredentialId()
      const body: ConnectionBody = { name: name.trim(), owner, id }
      let sealKey: CredentialSealKey | null = null
      for (const key of inputs) {
        const value = (values[key] ?? '').trim()
        if (!value) continue
        if (SEALED_INPUTS.has(key)) {
          sealKey ??= await fetchSealKey()
          body[key] = await sealSecret(sealKey, { credentialId: id, field: key, destination: connector.sealing_destination }, value)
        } else {
          body[key] = value
        }
      }
      const row = await createConnection(connector.id, body)
      setCreated(row)
      setName('')
      setValues({})
      await refresh()
    })
  }

  const test = (row: CustomConnection) => withTesting(row.id, async () => {
    setNote(null)
    try {
      const result = await testConnection(connector.id, row.id)
      setNote({ ok: result.ok, text: `${row.name}: ${result.message}` })
      await refresh()
    } catch (err) {
      setNote({ ok: false, text: `${row.name}: ${apiDetail(err, 'Test failed. Retry.')}` })
    }
  })

  const rows: FeedRowSpec[] = connector.connections.map(row => {
    const health = healthOf(row)
    return {
      key: row.id,
      title: row.name,
      titleText: row.name,
      href: connectionUrl(row.id),
      data: { connection: row.id },
      slots: [
        { key: 'owner', width: grid(12), gate: 'phone', align: 'left', node: OWNER_LABEL[row.owner] },
        {
          key: 'health', width: grid(21), align: 'left', testId: `connection-health-${row.id}`,
          node: (
            <StatusChip label={health.label} tone={health.tone} glyph={health.glyph}
              title={row.last_test_message ?? health.label} data-status={health.label} />
          ),
        },
        {
          key: 'test', width: grid(10.5), align: 'right', compact: true,
          node: row.editable && (
            <Button size="small" onClick={() => void test(row)} disabled={testing.has(row.id) || !dialable}
              aria-label={`Test ${row.name}`} data-testid={`connection-test-${row.id}`}>
              {testing.has(row.id) ? 'Testing…' : 'Test'}
            </Button>
          ),
        },
      ],
    }
  })

  return (
    <Box sx={{ display: 'flex', flexDirection: 'column', gap: 2, pt: 1.5 }}>
      {note && (
        <Banner severity={note.ok ? 'success' : 'error'} role={note.ok ? 'status' : undefined}
          onDismiss={() => setNote(null)} testId="connection-test-note">
          {note.text}
        </Banner>
      )}
      {rows.length === 0
        ? <EmptyState title="No connections yet" />
        : (
          <FeedList
            ariaLabel="Connections"
            testId="connection-list"
            sections={[{ key: 'connections', rows, rail: false }]}
            columns={{ title: { key: 'name', label: 'Name' }, slots: [{ key: 'owner', label: 'Owner' }, { key: 'health', label: 'Health' }, { key: 'test', label: '' }] }}
          />
        )}

      <DetailSection title="Connect" divided>
        <Box component="form" onSubmit={submit} sx={{ ...FORM_SX, pt: 0 }} data-testid="connection-form" noValidate>
          {formError && <Banner severity="error" testId="connection-form-error">{formError}</Banner>}
          {created && oauth && (
            <Banner severity="info" role="status" testId="connection-oauth-next">
              {created.name} saved.{' '}
              <Link component={RouterLink} to={connectionUrl(created.id)}>Connect it</Link>
            </Banner>
          )}
          <FormTextField label="Name" id="connection-name" value={name} required placeholder={connector.name}
            help="Tasks find the secret by this name."
            onChange={v => { setName(v); setFormError('') }} />
          {orgOwner && (
            <OwnershipRadios labelId="connection-owner" value={owner} options={['personal', 'org']} oneLine
              onChange={next => setOwner(next === 'org' ? 'org' : 'personal')} />
          )}
          {inputs.map(key => (
            <FormTextField key={key} label={INPUT_LABEL[key]} id={`connection-${key}`} value={values[key] ?? ''} help={INPUT_HELP[key]}
              type={key === 'client_id' || key === 'login' ? 'text' : 'password'}
              required={connector.secret_inputs[key] === true}
              onChange={v => { setValues(prev => ({ ...prev, [key]: v })); setFormError('') }}
              slotProps={TEXT_INPUT} />
          ))}
          <Box sx={{ display: 'flex', justifyContent: 'flex-end' }}>
            <SubmitButton testId="connection-submit" busy={busy} busyLabel="Connecting…" disabled={!complete}>
              Connect
            </SubmitButton>
          </Box>
          {oauth && !created && (
            <Box component="p" sx={{ m: 0, typography: 'body2', color: 'text.secondary' }}>
              Sign-in continues on the secret’s page.
            </Box>
          )}
        </Box>
      </DetailSection>
    </Box>
  )
}
