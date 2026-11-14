import { useState, type ReactNode } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import DialogContentText from '@mui/material/DialogContentText'
import Typography from '@mui/material/Typography'
import { post } from '../../api'
import { useAuth } from '../../auth'
import { useLeaveGuard } from '../../hooks/useLeaveGuard'
import { useSubmit } from '../../hooks/useSubmit'
import { downloadText } from '../../lib/downloadText'
import { isOrgAdminSeat } from '../../lib/orgAdmin'
import type { Account, ProviderChoice } from '../../types'
import FormDialog from '../../ui/FormDialog'
import FormTextField from '../../ui/FormTextField'
import { JsonFilePicker, useJsonFilePick } from '../../ui/JsonFilePicker'
import { tapTargetSx } from '../../ui/listStyles'
import { accountEditPath, accountLoginPath, ACCOUNTS_PATH } from './accountModel'
import { ACCOUNTS_KEY, RESOURCES_KEY } from './api'
import { FONT_SIZE } from '../../theme'
import Tip from '../../ui/Tip'
import Banner from '../../ui/Banner'
import CancelSaveBar from '../../ui/CancelSaveBar'
import FormBox from '../../ui/FormBox'
import { LeaveGuardDialog } from '../../ui/UnsavedChangesDialog'

/* Export / Import of one runner account between consoles (`POST /api/accounts/{id}/export`,
 * `POST /api/accounts/import`; the bundle's contract is `apps/api/app/account_transfer.py`). An API-key account's
 * key leaves sealed under a passphrase typed here and on the target; the key itself never reaches this page. An
 * OAuth account carries no login, so its import lands on Log in. */

/** The server's floor (`account_transfer.MIN_PASSPHRASE_LENGTH`). */
const MIN_PASSPHRASE = 12

/** New account's choice between setting one up and importing a file: it unmounts the form, so it is leaving. */
const VIA_KEYS = ['via'] as const

/** The download's name: the account's slug, never anything from its key. */
export function exportFileName(slug: string): string {
  return `jaah-account-${slug}.json`
}

/** "Export" on an account's tab strip: a passphrase dialog for an API key, a plain confirm for OAuth. */
export function useExportAccountAction(record: Account | undefined): { button: ReactNode; dialog: ReactNode } {
  const [open, setOpen] = useState(false)
  const { busy, formError, setFormError, run } = useSubmit()
  if (!record) return { button: null, dialog: null }
  const apiKey = record.auth_kind === 'api_key'
  const exportWith = (passphrase: string | null) => run('Export failed', async () => {
    const bundle = await post<unknown>(`/api/accounts/${record.id}/export`, { passphrase })
    downloadText(exportFileName(record.slug), JSON.stringify(bundle, null, 2))
    setOpen(false)
  })
  const button = (
    <Button
      type="button"
      variant="outlined"
      size="small"
      disabled={busy}
      onClick={() => { setFormError(''); setOpen(true) }}
      data-testid="account-export"
      sx={[{ whiteSpace: 'nowrap' }, tapTargetSx]}
    >
      Export
    </Button>
  )
  const dialog = open ? (
    <ExportDialog apiKey={apiKey} busy={busy} error={formError} onClose={() => setOpen(false)}
      onSubmit={p => void exportWith(apiKey ? p : null)} />
  ) : null
  return { button, dialog }
}

function ExportDialog({ apiKey, busy, error, onClose, onSubmit }: {
  apiKey: boolean
  busy: boolean
  error: string
  onClose: () => void
  onSubmit: (passphrase: string) => void
}) {
  const [passphrase, setPassphrase] = useState('')
  const [repeat, setRepeat] = useState('')
  const short = passphrase.length < MIN_PASSPHRASE
  const mismatch = repeat !== '' && repeat !== passphrase
  return (
    <FormDialog
      title="Export account"
      submitLabel="Export"
      busyLabel="Exporting…"
      busy={busy}
      error={error}
      onSubmit={() => onSubmit(passphrase)}
      onClose={onClose}
      modal
      maxWidth="xs"
      testId="account-export-dialog"
      submitTestId="account-export-submit"
      submitDisabled={apiKey && (short || repeat !== passphrase)}
    >
      <DialogContentText sx={{ fontSize: FONT_SIZE.base }}>
        {apiKey ? 'The key is sealed with this passphrase. Import needs it.'
          : 'The record only: the login stays on its nodes. Log in again after import.'}
      </DialogContentText>
      {apiKey && (
        <>
          <FormTextField label="Passphrase" type="password" autoComplete="new-password" value={passphrase}
            onChange={setPassphrase} hint={`${MIN_PASSPHRASE}+ characters`} testId="account-export-passphrase" autoFocus
            help={{ tip: 'Seals the key in the file; import needs it.', docs: 'using-jaah/model-accounts/move-a-model-account-between-consoles' }} />
          <FormTextField label="Confirm passphrase" type="password" autoComplete="new-password" value={repeat}
            onChange={setRepeat} error={mismatch ? 'Does not match' : undefined} testId="account-export-repeat"
            help="Catches a typo before the key is sealed." />
        </>
      )}
    </FormDialog>
  )
}

type ImportResult = { account: Account; needs_login: boolean }
type Bundle = { auth_kind?: unknown; name?: unknown; provider?: unknown }

/** New account's "Import a file" (`/accounts/new?via=import`): pick an export file, type its passphrase, create the
 *  account here, then open it — its Log in when the bundle carried no login. */
/* `providers`: the page's catalogue (its spinner holds until it settles), named on the bundle's summary line. */
export function ImportAccountForm({ providers }: { providers: readonly ProviderChoice[] }) {
  const queryClient = useQueryClient()
  const { admin } = useAuth()
  const [bundle, setBundle] = useState<Bundle | null>(null)
  const [passphrase, setPassphrase] = useState('')
  const { busy, formError, setFormError, run } = useSubmit()
  const apiKey = bundle?.auth_kind === 'api_key'
  // `create_account`'s rule: only an administrator records a login-based account (a key-based one anyone may).
  const loginBlocked = bundle && !apiKey && !isOrgAdminSeat(admin) ? 'Only an admin can import a subscription' : undefined
  const ready = !!bundle && !loginBlocked && (!apiKey || passphrase.length >= MIN_PASSPHRASE)

  const [fileName, pick] = useJsonFilePick({
    clear: () => { setFormError(''); setBundle(null) },
    text: content => {
      const parsed: unknown = JSON.parse(content)
      if (parsed === null || typeof parsed !== 'object') throw new Error('not an object')
      setBundle(parsed as Bundle)
    },
    fail: () => setFormError('Not a Jaah account export.'),
  })

  const importNow = async (): Promise<ImportResult | null> => {
    if (!ready) return null
    let result: ImportResult | null = null
    await run('Import failed', async () => {
      result = await post<ImportResult>('/api/accounts/import', { bundle, passphrase: apiKey ? passphrase : null })
      void queryClient.invalidateQueries({ queryKey: ACCOUNTS_KEY })
      void queryClient.invalidateQueries({ queryKey: RESOURCES_KEY })
    })
    return result
  }
  const destination = ({ account, needs_login }: ImportResult) =>
    needs_login ? accountLoginPath(account.slug, null, true) : accountEditPath(account.id)
  // A picked file is work a stray click would lose; the dialog's Save is Import. `via` unmounts this form.
  const guard = useLeaveGuard(!!fileName, async () => (await importNow()) !== null, VIA_KEYS)

  return (
    <FormBox onSubmit={async () => {
      const result = await importNow()
      if (result) guard.leaveNow(destination(result), { replace: true })
    }} sx={{ display: 'flex', flexDirection: 'column', gap: 1.5 }} data-testid="account-import-form">
      {formError && <Banner severity="error">{formError}</Banner>}
      <Box sx={{ display: 'flex', alignItems: 'center', gap: 1.5, minWidth: 0 }}>
        <JsonFilePicker label={fileName ? 'Change file' : 'Choose file'} testId="account-import-file" onPick={pick} />
        {fileName && (
          <Tip title={fileName}><Typography variant="body2" noWrap sx={{ minWidth: 0 }}>{fileName}</Typography></Tip>
        )}
      </Box>
      {bundle && typeof bundle.name === 'string' && (
        <Typography variant="body2" sx={{ overflowWrap: 'anywhere' }}>
          {bundle.name} · {providers.find(p => p.value === bundle.provider)?.label ?? String(bundle.provider ?? '')}
          {' · '}{apiKey ? 'API key' : 'Subscription'}
        </Typography>
      )}
      {apiKey && (
        <FormTextField label="Passphrase" type="password" autoComplete="off" value={passphrase}
          onChange={setPassphrase} testId="account-import-passphrase" autoFocus
          help="Opens the sealed key; must match the export’s." />
      )}
      <CancelSaveBar testId="account-import-actions" saveTestId="account-import-submit" cancelTo={ACCOUNTS_PATH}
        creating saveLabel="Import" busy={busy} disabled={!ready} saveReason={loginBlocked} />
      <LeaveGuardDialog leave={guard} message="This account has not been imported yet. Import it before leaving?" />
    </FormBox>
  )
}
