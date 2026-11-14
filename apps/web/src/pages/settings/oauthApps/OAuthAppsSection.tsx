import { useEffect, useState, type JSX } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import Box from '@mui/material/Box'
import Typography from '@mui/material/Typography'
import { ApiError, apiDetail, put } from '../../../api'
import { useAuth } from '../../../auth'
import { useApiRead } from '../../../hooks/useApiRead'
import { useSubmit } from '../../../hooks/useSubmit'
import { groupedCount } from '../../../lib/humanize'
import { UNFILTERED_READ } from '../../../lib/orgScope'
import { FONT_WEIGHT, SIZE } from '../../../theme'
import type { SettingsPayload } from '../../../types'
import CredentialSelect, { type CredentialOption } from '../../../ui/CredentialSelect'
import { credentialMark, VendorLogo } from '../../../ui/credentialMarks'
import FeedList from '../../../ui/feed/FeedList'
import type { FeedColumns, FeedRowSpec } from '../../../ui/feed/types'
import { ELLIPSIS_SX } from '../../../ui/listStyles'
import LoadFailedState from '../../../ui/LoadFailedState'
import LoadingState from '../../../ui/LoadingState'
import { getCredential, loadCredentialOptions } from '../../credentials/api'
import {
  INTEGRATION_APPS_KEY, INTEGRATIONS_KEY, listIntegrationApps, type IntegrationApp,
} from '../../credentials/integrations/api'
import { knobCredentialScope, SETTINGS_QUERY_KEY } from '../KnobSection'
import Tip from '../../../ui/Tip'

/* SETTINGS › INTEGRATIONS › OAUTH APPS — every OAuth vendor of the catalog, most voted first: whether it is Beta (no
 * Jaah app, so members see Vote), its votes and connections across every org (`GET /api/integrations/apps`), and the
 * picker for its `oauth_app.<slug>` knob. A pick saves at once (`PUT /api/settings`, that one key), so the row's
 * status follows; the page's Save bar never carries these knobs (`Settings.tsx` keeps them off the knob cards).
 * One `ui/feed/FeedList` with a column row: the vendor is the title, Beta/Ready its disc, the picker a trailing slot
 * (no record page: a vendor's app is this one knob). A row is `feed-row` with `data-slug`; its disc carries
 * `data-status`. */

/** The credential type a vendor's Jaah app is saved as (`integrations/catalog.py::OAUTH_APP_TYPE`). */
const OAUTH_APP_TYPE = 'oauth_app'

type Props = {
  /** A knob's saved value: the panel's host reads the settings payload. */
  saved: (key: string) => unknown
}

export default function OAuthAppsSection({ saved }: Props): JSX.Element {
  const { data, loading, error, reload } = useApiRead(INTEGRATION_APPS_KEY, listIntegrationApps, 'Failed to load OAuth apps')
  if (loading) return <LoadingState label="Loading OAuth apps…" />
  if (error) return <LoadFailedState title="OAuth apps unavailable" error={error} onRetry={() => void reload()} />
  // A list, or nothing to draw: never let a malformed answer take the whole Settings page down with it.
  const rows = Array.isArray(data) ? data : []
  if (rows.length === 0) return <Typography variant="body2" sx={{ color: 'text.secondary' }}>No OAuth vendors.</Typography>
  return (
    <FeedList
      ariaLabel="OAuth apps"
      testId="oauth-apps"
      sections={[{ key: 'apps', rail: false, rows: rows.map(row => rowOf(row, saved, () => void reload())) }]}
      columns={COLUMNS}
    />
  )
}

const COLUMNS: FeedColumns = {
  title: { key: 'vendor', label: 'Vendor' },
  slots: [{ key: 'votes', label: 'Votes' }, { key: 'connections', label: 'Connections' }, { key: 'app', label: 'Jaah app' }],
}

/* Slot widths (px): a grouped count in the cluster's mono ("1,234"), Connections sized to its header word (100px at the header's 11px caps), the picker
   field. The counts yield first (`id`, `meta`): on a phone the picker is the row's point. */
const VOTES_SLOT = 48
const CONNECTIONS_SLOT = 100
const APP_SLOT = 224

function rowOf(row: IntegrationApp, saved: Props['saved'], onSaved: () => void): FeedRowSpec {
  const testId = `oauth-app-${row.slug}`
  return {
    key: row.slug,
    indicator: row.beta
      ? { tone: 'neutral', glyph: '○', label: 'Beta', title: 'Beta: no Jaah app yet, so members see Vote', data: { status: 'beta' } }
      : { tone: 'success', glyph: '●', label: 'Ready', data: { status: 'ready' } },
    title: (
      <Box component="span" sx={{ display: 'inline-flex', alignItems: 'center', gap: 1, maxWidth: '100%', verticalAlign: 'bottom', fontWeight: FONT_WEIGHT.semibold }}>
        <VendorLogo url={row.logo_url} label={row.label} size={SIZE.iconMd} />
        <Box component="span" sx={ELLIPSIS_SX}>{row.label}</Box>
      </Box>
    ),
    titleText: row.label,
    href: null,
    data: { slug: row.slug },
    slots: [
      {
        key: 'votes', width: VOTES_SLOT, gate: 'id', align: 'right', mono: true, testId: `${testId}-votes`,
        node: <Box component="span" sx={{ fontWeight: row.votes ? FONT_WEIGHT.semibold : undefined }}>{groupedCount(row.votes)}</Box>,
      },
      { key: 'connections', width: CONNECTIONS_SLOT, gate: 'meta', align: 'right', mono: true, testId: `${testId}-connections`, node: groupedCount(row.connections) },
      {
        key: 'app', width: APP_SLOT, align: 'left',
        node: row.app_knob
          ? <AppPicker knob={row.app_knob} label={row.label} value={String(saved(row.app_knob) ?? '')} onSaved={onSaved} />
          : <Tip title="Set in this vendor's own section, or people connect with their own app"><Typography variant="body2" sx={{ color: 'text.secondary' }}>—</Typography></Tip>,
      },
    ],
  }
}

/** One vendor's `oauth_app.<slug>` picker, saved on pick. */
function AppPicker({ knob, label, value, onSaved }: { knob: string; label: string; value: string; onSaved: () => void }): JSX.Element {
  const queryClient = useQueryClient()
  const [selected, setSelected] = useState<CredentialOption | null>(null)
  const { busy, formError: errorText, run } = useSubmit()
  const { admin } = useAuth()

  // The saved id's name, past the operator's org filter (the app is an org-zero row).
  useEffect(() => {
    if (!value) {
      setSelected(null)
      return
    }
    let alive = true
    getCredential(value, UNFILTERED_READ)
      .then(row => alive && setSelected({ id: value, name: row.name }))
      .catch((err: unknown) => alive && setSelected({ id: value, name: err instanceof ApiError && err.status === 404 ? 'Deleted credential' : 'Unknown credential' }))
    return () => {
      alive = false
    }
  }, [value])

  // A refusal carries the server's sentence: it says why and where the fix is (`_validated_credential`).
  const pick = (next: string | null) => run('the server refused this app.', async () => {
    // Cleared: the knob's default (blank), which turns the Jaah app off.
    const data = await put<SettingsPayload>('/api/settings', { values: { [knob]: next } })
    queryClient.setQueryData(SETTINGS_QUERY_KEY, data)
    void queryClient.invalidateQueries({ queryKey: INTEGRATIONS_KEY })
    onSaved()
  }, { mapError: (err, fallback) => (err instanceof ApiError ? `Not saved: ${apiDetail(err, fallback)}` : 'Not saved.') })

  return (
    <Box sx={{ '& .MuiFormControl-root': { my: 0 } }} data-testid={`oauth-app-${knob.split('.').pop()}-picker`}>
      <CredentialSelect
        id={`oauth-app-picker-${knob}`}
        value={value === '' ? null : value}
        selected={selected}
        onChange={next => void pick(next)}
        fetchOptions={(query, signal) => loadCredentialOptions(
          query, signal,
          credential => ({ id: credential.id, name: credential.name, hint: credential.description?.trim() || credential.type_label, icon: credentialMark(credential.type, credential.type_label) }),
          // Saved through `PUT /api/settings`: only the viewer's own org's rows are accepted.
          { types: [OAUTH_APP_TYPE], sort: true, ...knobCredentialScope(admin?.org?.id) },
        )}
        disabled={busy}
        placeholder={busy ? 'Saving…' : `Choose ${label}'s app`}
      />
      {errorText && (
        <Typography variant="caption" role="alert" sx={{ color: 'error.main' }}>{errorText}</Typography>
      )}
    </Box>
  )
}
