import { useState, type ReactNode } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import DialogContentText from '@mui/material/DialogContentText'
import Typography from '@mui/material/Typography'
import { useNavigate } from 'react-router-dom'
import { post } from '../../api'
import { useAuth } from '../../auth'
import { useAgentProviders } from '../../hooks/useAgentProviders'
import { useSubmit } from '../../hooks/useSubmit'
import { downloadText } from '../../lib/downloadText'
import { isOrgAdminSeat } from '../../lib/orgAdmin'
import type { Account } from '../../types'
import FormDialog from '../../ui/FormDialog'
import FormTextField from '../../ui/FormTextField'
import { JsonFilePicker, useJsonFilePick } from '../../ui/JsonFilePicker'
import { tapTargetSx } from '../../ui/listStyles'
import { accountEditPath, accountLoginPath } from './accountModel'
import { ACCOUNTS_KEY, RESOURCES_KEY } from './api'

/* Export / Import of one runner account between consoles (`POST /api/accounts/{id}/export`,
 * `POST /api/accounts/import`; the bundle's contract is `apps/api/app/account_transfer.py`). An API-key account's
 * key leaves sealed under a passphrase typed here and on the target; the key itself never reaches this page. An
 * OAuth account carries no login, so its import offers Log in. */

/** The server's floor (`account_transfer.MIN_PASSPHRASE_LENGTH`). */
const MIN_PASSPHRASE = 12

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
      <DialogContentText sx={{ fontSize: '0.875rem' }}>
        {apiKey ? 'The key is sealed with this passphrase. Import needs it.'
          : 'The record only: the login stays on its nodes. Log in again after import.'}
      </DialogContentText>
      {apiKey && (
        <>
          <FormTextField label="Passphrase" type="password" autoComplete="new-password" value={passphrase}
            onChange={setPassphrase} hint={`${MIN_PASSPHRASE}+ characters`} testId="account-export-passphrase" autoFocus />
          <FormTextField label="Confirm passphrase" type="password" autoComplete="new-password" value={repeat}
            onChange={setRepeat} error={mismatch ? 'Does not match' : undefined} testId="account-export-repeat" />
        </>
      )}
    </FormDialog>
  )
}

type ImportResult = { account: Account; needs_login: boolean }
type Bundle = { auth_kind?: unknown; name?: unknown; provider?: unknown }

/** "Import" on the Brains list header: pick an export file, type its passphrase, create the account here. */
export function ImportAccountAction() {
  const [open, setOpen] = useState(false)
  return (
    <>
      <Button
        type="button"
        variant="outlined"
        color="primary"
        onClick={() => setOpen(true)}
        aria-label="Import account"
        title="Import an account exported from another console"
        data-testid="account-import"
        sx={[{ whiteSpace: 'nowrap' }, tapTargetSx]}
      >
        Import
      </Button>
      {open && <ImportDialog onClose={() => setOpen(false)} />}
    </>
  )
}

function ImportDialog({ onClose }: { onClose: () => void }) {
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const { admin } = useAuth()
  const providers = useAgentProviders()
  const [bundle, setBundle] = useState<Bundle | null>(null)
  const [passphrase, setPassphrase] = useState('')
  const [imported, setImported] = useState<ImportResult | null>(null)
  const { busy, formError, setFormError, run } = useSubmit()
  const apiKey = bundle?.auth_kind === 'api_key'
  // `create_account`'s rule: only an administrator records a login-based account (a key-based one anyone may).
  const loginBlocked = bundle && !apiKey && !isOrgAdminSeat(admin) ? 'Only an admin can import a subscription' : undefined

  const [fileName, pick] = useJsonFilePick({
    clear: () => { setFormError(''); setBundle(null) },
    text: content => {
      const parsed: unknown = JSON.parse(content)
      if (parsed === null || typeof parsed !== 'object') throw new Error('not an object')
      setBundle(parsed as Bundle)
    },
    fail: () => setFormError('Not a Jaah account export.'),
  })

  if (imported) {
    const { account } = imported
    return (
      <FormDialog
        title="Imported"
        submitLabel={imported.needs_login ? 'Log in' : 'Open'}
        onSubmit={() => navigate(imported.needs_login ? accountLoginPath(account.slug, null, true) : accountEditPath(account.id))}
        onClose={onClose}
        cancelLabel="Close"
        maxWidth="xs"
        testId="account-import-done"
        submitTestId="account-import-open"
        submitAutoFocus
      >
        <DialogContentText sx={{ fontSize: '0.875rem', overflowWrap: 'anywhere' }}>
          {account.name}
          {imported.needs_login ? ' — log it in on a node to use it.' : ' — Auto-assign off.'}
        </DialogContentText>
      </FormDialog>
    )
  }

  return (
    <FormDialog
      title="Import account"
      submitLabel="Import"
      busyLabel="Importing…"
      busy={busy}
      error={formError}
      onSubmit={() => void run('Import failed', async () => {
        setImported(await post<ImportResult>('/api/accounts/import', { bundle, passphrase: apiKey ? passphrase : null }))
        void queryClient.invalidateQueries({ queryKey: ACCOUNTS_KEY })
        void queryClient.invalidateQueries({ queryKey: RESOURCES_KEY })
      })}
      onClose={onClose}
      modal
      maxWidth="xs"
      testId="account-import-dialog"
      submitTestId="account-import-submit"
      submitDisabled={!bundle || (apiKey && passphrase.length < MIN_PASSPHRASE)}
      submitDisabledReason={loginBlocked}
    >
      <Box sx={{ display: 'flex', alignItems: 'center', gap: 1.5, minWidth: 0 }}>
        <JsonFilePicker label={fileName ? 'Change file' : 'Choose file'} testId="account-import-file" onPick={pick} />
        {fileName && (
          <Typography variant="body2" noWrap title={fileName} sx={{ minWidth: 0 }}>{fileName}</Typography>
        )}
      </Box>
      {bundle && typeof bundle.name === 'string' && (
        <DialogContentText sx={{ fontSize: '0.875rem', overflowWrap: 'anywhere' }}>
          {bundle.name} · {providers.rows.find(p => p.value === bundle.provider)?.label ?? String(bundle.provider ?? '')}
          {' · '}{apiKey ? 'API key' : 'Subscription'}
        </DialogContentText>
      )}
      {apiKey && (
        <FormTextField label="Passphrase" type="password" autoComplete="off" value={passphrase}
          onChange={setPassphrase} testId="account-import-passphrase" autoFocus />
      )}
    </FormDialog>
  )
}
