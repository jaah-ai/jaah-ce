import { type FormEvent, type JSX, useCallback, useState } from 'react'
import { Link as RouterLink, useNavigate, useParams } from 'react-router-dom'
import { useQueryClient } from '@tanstack/react-query'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import Typography from '@mui/material/Typography'
import Breadcrumb from '../../../components/Breadcrumb'
import { TabPanel, Tabs, type TabDef } from '../../../components/Tabs'
import { useRowActions } from '../../../hooks/useBusyIds'
import { useEditRecord } from '../../../hooks/useEditRecord'
import { useSubmit } from '../../../hooks/useSubmit'
import { useUrlTab } from '../../../hooks/useUrlTab'
import { absoluteTime } from '../../../lib/relativeTime'
import { localInputToIso } from '../../../lib/zonedTime'
import Banner from '../../../ui/Banner'
import { ChoiceField } from '../../../ui/ChoiceField'
import { DetailPage } from '../../../ui/detail/DetailPage'
import DetailStatus from '../../../ui/detail/DetailStatus'
import FormActionBar from '../../../ui/FormActionBar'
import { FormDialog } from '../../../ui/FormDialog'
import FormTextField from '../../../ui/FormTextField'
import { KeyValue, KeyValueList } from '../../../ui/KeyValueList'
import { FORM_SX, TEXT_INPUT } from '../../../ui/formDensity'
import { ID_SX, tapTargetSx } from '../../../ui/listStyles'
import LoadingState from '../../../ui/LoadingState'
import SavedNote from '../../../ui/SavedNote'
import SecretOnceDialog from '../../../ui/SecretOnceDialog'
import StatusChip from '../../../ui/StatusChip'
import SubmitButton from '../../../ui/SubmitButton'
import {
  API_KEYS_KEY,
  API_KEYS_URL,
  type ApiKey,
  type ApiKeyUpdateBody,
  apiKeyUrl,
  createApiKey,
  getApiKey,
  revokeApiKey,
  rotateApiKey,
  type RotateOverlap,
  updateApiKey,
} from './api'
import ApiKeyActivity from './ApiKeyActivity'
import { useCanCreateKey } from './ApiKeysSection'
import { DEFAULT_OVERLAP, expiryText, isLive, lastUsedText, OVERLAP_OPTIONS, revokeConfirm, STATUS_LABEL, STATUS_TONE } from './keyFacts'
import KeyPermissionGrid from './KeyPermissionGrid'
import { fromKey, preset, toGrants, type GridValue } from './keyPermissions'
import { useGridRows } from './useGridRows'

/* One API key: `/settings/api-keys/new` (`mode="create"`) and `/settings/api-keys/:keyId`. Overview (name,
 * description, expiry, IP allowlist), Permissions (the grid) and Activity (its audit rows). One Save bar.
 * Only the owner edits or rotates; any Org Owner or Admin revokes. The secret is shown once, in a dialog, after
 * create and rotate — never again. Every rule is the server's (`app/api_keys.py`). */

type Tab = 'overview' | 'permissions' | 'activity'

const TABS: readonly TabDef<Tab>[] = [
  { id: 'overview', label: 'Overview' },
  { id: 'permissions', label: 'Permissions' },
  { id: 'activity', label: 'Activity' },
]

type ExpiryChoice = '7d' | '30d' | '90d' | '1y' | 'custom' | 'never'

const EXPIRY_OPTIONS: readonly { value: ExpiryChoice; label: string }[] = [
  { value: '7d', label: '7 days' },
  { value: '30d', label: '30 days' },
  { value: '90d', label: '90 days' },
  { value: '1y', label: '1 year' },
  { value: 'custom', label: 'Custom date' },
  { value: 'never', label: 'No expiration' },
]

const DAYS: Partial<Record<ExpiryChoice, number>> = { '7d': 7, '30d': 30, '90d': 90 }

/** The expiry a choice means, from `now`; `false` for a custom date that is missing or unparseable. */
export function expiryFor(choice: ExpiryChoice, custom: string, now: Date = new Date()): string | null | false {
  if (choice === 'never') return null
  if (choice === 'custom') return localInputToIso(custom) || false
  const at = new Date(now)
  if (choice === '1y') at.setFullYear(at.getFullYear() + 1)
  else at.setTime(at.getTime() + (DAYS[choice] ?? 0) * 86_400_000)
  return at.toISOString()
}

/** The allowlist box's lines: one address or CIDR each, blanks dropped. */
const allowlistOf = (text: string) => text.split(/[\n,]+/).map(s => s.trim()).filter(Boolean)

const sameSet = (a: readonly string[], b: readonly string[]) => a.length === b.length && [...a].sort().join('\n') === [...b].sort().join('\n')

export default function ApiKeyDetailPage({ mode }: { mode?: 'create' }): JSX.Element {
  const { keyId } = useParams<{ keyId: string }>()
  const creating = mode === 'create'
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const canCreate = useCanCreateKey()
  const { rows: grid, error: gridError } = useGridRows()

  const [detail, setDetail] = useState<ApiKey | null>(null)
  const [name, setName] = useState('')
  const [description, setDescription] = useState('')
  const [allowlist, setAllowlist] = useState('')
  /* Null until the catalog arrives: create starts with every box ticked, an existing key with its own grants. */
  const [grants, setGrants] = useState<GridValue | null>(null)
  const [expiry, setExpiry] = useState<ExpiryChoice>('1y')
  const [customExpiry, setCustomExpiry] = useState('')
  /** The secret just answered by create or rotate: shown once, dropped when the dialog closes. */
  const [secret, setSecret] = useState<string | null>(null)
  const [rotating, setRotating] = useState(false)
  const [overlap, setOverlap] = useState<RotateOverlap>(DEFAULT_OVERLAP)
  const { busy: saving, formError, setFormError, run, saved, clearSaved } = useSubmit()
  const { busy: acting, error: actionError, setError: setActionError, run: act } = useRowActions<'revoke' | 'rotate'>()

  const seed = useCallback((row: ApiKey) => {
    setDetail(row)
    setName(row.name)
    setDescription(row.description)
    setAllowlist(row.ip_allowlist.join('\n'))
    setGrants(null)
  }, [])

  const { loading, notFound, loadError, confirm, confirmDialog } = useEditRecord(
    creating || detail?.id === keyId ? null : keyId ?? null, getApiKey, seed,
    { loadFallback: 'Could not load the API key.', notFoundStatuses: [404, 422] },
  )

  const [tab, selectTab] = useUrlTab(creating ? TABS.slice(0, 2) : TABS, 'overview')

  // The grid's value: seeded from the key (or all ticked) on the first draw after the catalog arrives.
  const value: GridValue | null = grants ?? (grid ? (detail ? fromKey(grid, detail) : preset(grid, 'all')) : null)

  const changed = useCallback((row: ApiKey) => {
    setDetail(row)
    void queryClient.invalidateQueries({ queryKey: API_KEYS_KEY })
  }, [queryClient])

  if (!creating && (loading || notFound || loadError || !detail)) {
    return (
      <DetailStatus
        trail={<Breadcrumb items={[{ label: 'API keys', to: API_KEYS_URL }, { label: 'API key' }]} />}
        loading={loading || (!notFound && !loadError)}
        notFound={notFound && 'API key not found'}
        error={loadError || 'Could not load the API key.'}
      />
    )
  }

  // Once created, the create form is spent: the reveal closes onto the key's own page.
  const readOnly = creating ? !canCreate || detail !== null : !detail?.can_edit

  /* ── What a save sends ── */
  const changes: ApiKeyUpdateBody = {}
  if (detail) {
    if (name.trim() !== detail.name) changes.name = name.trim()
    if (description.trim() !== detail.description) changes.description = description.trim()
    if (!sameSet(allowlistOf(allowlist), detail.ip_allowlist)) changes.ip_allowlist = allowlistOf(allowlist)
  }
  // The grants only once the catalog is read: a failed read blocks a permission change, never a rename.
  if (detail && grid && value) {
    const next = toGrants(grid, value)
    const was = toGrants(grid, fromKey(grid, detail))
    if (next.grants_all !== was.grants_all || next.org_admin !== was.org_admin || !sameSet(next.permissions, was.permissions)) {
      Object.assign(changes, next)
    }
  }
  const expiresAt = creating ? expiryFor(expiry, customExpiry) : null
  const expiryPast = typeof expiresAt === 'string' && Date.parse(expiresAt) <= Date.now()
  const complete = name.trim() !== '' && (detail !== null || value !== null) && expiresAt !== false && !expiryPast
  const unchanged = detail !== null && Object.keys(changes).length === 0

  const edited = (fn: () => void) => {
    fn()
    setFormError('')
    clearSaved()
  }

  const submit = (event?: FormEvent) => {
    event?.preventDefault()
    if (readOnly || !complete) return
    void run(creating ? 'Could not create the key. Retry.' : 'Could not save. Retry.', async () => {
      if (creating) {
        if (!grid || !value) return
        const made = await createApiKey({
          name: name.trim(),
          description: description.trim(),
          ip_allowlist: allowlistOf(allowlist),
          expires_at: expiresAt,
          ...toGrants(grid, value),
        })
        seed(made.key)
        changed(made.key)
        // The page stays put under the reveal: moving to the key's own address can remount it (the live stack did),
        // which would drop the secret before it is read. Closing the dialog moves there (`closeReveal`).
        setSecret(made.secret)
      } else if (detail) {
        const row = await updateApiKey(detail.id, changes)
        seed(row)
        changed(row)
      }
    })
  }

  async function revoke() {
    if (!detail || !(await confirm(revokeConfirm(detail)))) return
    await act('revoke', `Could not revoke ${detail.name}. Retry.`, async () => {
      const row = await revokeApiKey(detail.id)
      seed(row)
      changed(row)
    })
  }

  async function rotate() {
    if (!detail) return
    setRotating(false)
    await act('rotate', `Could not rotate ${detail.name}. Retry.`, async () => {
      const made = await rotateApiKey(detail.id, overlap)
      changed(made.key)
      setSecret(made.secret)
    })
  }

  const closeReveal = () => {
    setSecret(null)
    // `replace`: Back lands on the list, never on a create form that would make a second key.
    if (creating && detail) navigate(apiKeyUrl(detail.id), { replace: true })
  }

  const live = detail ? isLive(detail) : true
  const heading = detail?.name ?? 'New API key'
  const actions = detail && (
    <Box sx={{ display: 'flex', flexWrap: 'wrap', gap: 1 }}>
      {detail.can_edit && (
        <Button variant="outlined" onClick={() => { setOverlap(DEFAULT_OVERLAP); setActionError(''); setRotating(true) }}
          disabled={acting.size > 0} data-testid="api-key-rotate" sx={tapTargetSx}>
          Rotate
        </Button>
      )}
      {detail.can_revoke && (
        <Button variant="outlined" color="error" onClick={() => void revoke()} disabled={acting.size > 0}
          data-testid="api-key-revoke" sx={tapTargetSx}>
          Revoke
        </Button>
      )}
    </Box>
  )

  const saveBar = !readOnly && (
    <FormActionBar testId="api-key-actions" stretchTestIds={['api-key-save']}>
      <SavedNote show={saved} sx={{ alignSelf: 'center', mr: 'auto' }} />
      <Button component={RouterLink} to={API_KEYS_URL} variant="text" sx={tapTargetSx}>Cancel</Button>
      <SubmitButton testId="api-key-save" busy={saving} busyLabel={creating ? 'Creating…' : 'Saving…'}
        disabled={!complete || unchanged} onClick={() => submit()}>
        {creating ? 'Create' : 'Save'}
      </SubmitButton>
    </FormActionBar>
  )

  return (
    <DetailPage
      breadcrumb={<Breadcrumb items={[{ label: 'API keys', to: API_KEYS_URL }, { label: heading }]} />}
      title={heading}
      titleAside={detail && (
        <StatusChip label={STATUS_LABEL[detail.status]} tone={STATUS_TONE[detail.status]} data-status={detail.status} />
      )}
      subtitle={detail && (
        <Box component="span" sx={ID_SX} data-testid="api-key-display">{detail.display}</Box>
      )}
      actions={actions}
      actionBar={saveBar}
      testId="api-key-editor"
    >
      {actionError && <Banner severity="error" testId="api-key-action-error">{actionError}</Banner>}
      {creating && !canCreate && (
        <Banner severity="info">An API key is its creator’s own. Sign in as an admin to create one.</Banner>
      )}
      {detail && !live && (
        <Banner severity="info" testId="api-key-ended">This key has ended. Create a new one.</Banner>
      )}
      {detail && !detail.can_edit && live && (
        <Banner severity="info" testId="api-key-not-owner">{detail.owner.label}’s key. Only its owner can change or rotate it.</Banner>
      )}
      {formError && <Banner severity="error" testId="api-key-form-error">{formError}</Banner>}

      <Tabs tabs={creating ? TABS.slice(0, 2) : TABS} tab={tab} onSelect={selectTab} label="API key sections" idPrefix="api-key" />

      <TabPanel id="overview" current={tab} idPrefix="api-key">
        <Box component="form" onSubmit={submit} sx={FORM_SX} data-testid="api-key-form">
          <FormTextField label="Name" id="api-key-name" value={name} onChange={v => edited(() => setName(v))}
            disabled={readOnly} required autoFocus={creating} testId="api-key-name"
            slotProps={{ htmlInput: { ...TEXT_INPUT.htmlInput, maxLength: 100 } }} />
          <FormTextField label="Description" id="api-key-description" value={description}
            onChange={v => edited(() => setDescription(v))} disabled={readOnly} multiline minRows={2}
            slotProps={{ htmlInput: { maxLength: 1000 } }} />
          {creating ? (
            <>
              <FormTextField label="Expires" id="api-key-expiry" value={expiry}
                onChange={v => edited(() => setExpiry(v as ExpiryChoice))} options={EXPIRY_OPTIONS} disabled={readOnly}
                hint="Can't be extended later." testId="api-key-expiry" />
              {expiry === 'custom' && (
                <FormTextField label="Expiry date" id="api-key-expiry-custom" type="datetime-local" value={customExpiry}
                  onChange={v => edited(() => setCustomExpiry(v))} required disabled={readOnly} testId="api-key-expiry-custom"
                  error={expiryPast ? 'Pick a time in the future.' : undefined} />
              )}
            </>
          ) : detail && (
            <KeyValueList testId="api-key-facts">
              <KeyValue label="Owner" valueTestId="api-key-owner">{detail.owner.label}</KeyValue>
              <KeyValue label="Expires" valueTestId="api-key-expires"
                title={detail.expires_at ? absoluteTime(detail.expires_at) : undefined}>
                {detail.expires_at ? `${absoluteTime(detail.expires_at)} · ${expiryText(detail)}` : 'Never'}
              </KeyValue>
              <KeyValue label="Created">{`${absoluteTime(detail.created_at)} by ${detail.created_by.label}`}</KeyValue>
              <KeyValue label="Last used" valueTestId="api-key-last-used">{lastUsedText(detail)}</KeyValue>
              {detail.previous_secret_valid_until && (
                <KeyValue label="Old secret">{`Works until ${absoluteTime(detail.previous_secret_valid_until)}`}</KeyValue>
              )}
              {detail.revoked_at && (
                <KeyValue label="Revoked">
                  {`${absoluteTime(detail.revoked_at)}${detail.revoked_by ? ` by ${detail.revoked_by.label}` : ''}`}
                </KeyValue>
              )}
            </KeyValueList>
          )}
          <FormTextField label="IP allowlist" id="api-key-allowlist" value={allowlist}
            onChange={v => edited(() => setAllowlist(v))} disabled={readOnly} multiline minRows={2}
            hint="One address or CIDR per line. Empty: any address." testId="api-key-allowlist"
            slotProps={{ htmlInput: { ...TEXT_INPUT.htmlInput } }} />
          {/* Enter in a field submits; the visible Save is the page's bar. */}
          <button type="submit" hidden />
        </Box>
      </TabPanel>

      <TabPanel id="permissions" current={tab} idPrefix="api-key">
        <Box sx={{ pt: 2 }}>
          {gridError && <Banner severity="error">{gridError}</Banner>}
          {!grid && !gridError && <LoadingState />}
          {grid && value && (
            <KeyPermissionGrid rows={grid} value={value} disabled={readOnly}
              onChange={next => edited(() => setGrants(next))} />
          )}
          <Typography variant="body2" sx={{ color: 'text.secondary', mt: 1.5 }}>
            A key never does more than its owner can.
          </Typography>
        </Box>
      </TabPanel>

      {detail && tab === 'activity' && (
        <TabPanel id="activity" current={tab} idPrefix="api-key">
          <ApiKeyActivity keyId={detail.id} />
        </TabPanel>
      )}

      {rotating && detail && (
        <FormDialog
          title={`Rotate ${detail.name}?`}
          submitLabel="Rotate"
          busyLabel="Rotating…"
          busy={acting.has('rotate')}
          onSubmit={() => void rotate()}
          onClose={() => setRotating(false)}
          testId="api-key-rotate-dialog"
          submitTestId="api-key-rotate-confirm"
        >
          <Typography variant="body2" sx={{ mb: 2 }}>
            A new secret replaces the current one. Settings and expiry stay the same.
          </Typography>
          <ChoiceField
            label="Old secret keeps working for"
            value={overlap}
            onChange={setOverlap}
            testId="api-key-overlap"
            options={OVERLAP_OPTIONS.map(o => ({ value: o.value, label: o.label, hint: null, testId: `api-key-overlap-${o.value}` }))}
          />
        </FormDialog>
      )}

      {secret !== null && detail && (
        <SecretOnceDialog
          secret={secret}
          title={`${detail.name}: your secret`}
          notice="Copy it now. You won’t see this secret again."
          submitLabel="Done"
          testIdPrefix="api-key-secret"
          valueTestId="api-key-secret"
          onClose={closeReveal}
        />
      )}

      {confirmDialog}
    </DetailPage>
  )
}
