import { type JSX, useState } from 'react'
import Box from '@mui/material/Box'
import Typography from '@mui/material/Typography'
import { useSubmit } from '../../../hooks/useSubmit'
import { FormDialog } from '../../../ui/FormDialog'
import FormTextField from '../../../ui/FormTextField'
import { JsonFilePicker, useJsonFilePick } from '../../../ui/JsonFilePicker'
import { connectServiceAccount, type GmailConnection } from './api'

/* Connect a Workspace mailbox by a service-account key with domain-wide delegation: a one-shot act, so a dialog.
 * The key file's text goes up whole as `key_json` and is never kept here past the dialog; the server checks it and
 * that Google lets it act as the mailbox, answering a 422 sentence naming what to fix (shown as it came). */

export default function ServiceAccountDialog({ orgId, onClose, onConnected }: {
  orgId: string
  onClose: () => void
  onConnected: (row: GmailConnection) => void
}): JSX.Element {
  const [address, setAddress] = useState('')
  const [keyJson, setKeyJson] = useState('')
  const { busy, formError, setFormError, run } = useSubmit()
  const [fileName, pick] = useJsonFilePick({
    clear: () => { setFormError(''); setKeyJson('') },
    text: setKeyJson,
    fail: file => setFormError(`Could not read ${file.name}. Choose the key file again.`),
  })

  const submit = () => void run('Could not connect the mailbox. Retry.', async () => {
    onConnected(await connectServiceAccount(orgId, { address: address.trim(), key_json: keyJson }))
  })

  return (
    <FormDialog
      title="Add service account"
      submitLabel="Connect"
      busyLabel="Connecting…"
      busy={busy}
      error={formError}
      onSubmit={submit}
      onClose={onClose}
      modal
      maxWidth="xs"
      testId="gmail-sa-dialog"
      submitTestId="gmail-sa-submit"
      errorTestId="gmail-sa-error"
      submitDisabled={address.trim() === '' || keyJson === ''}
    >
      <FormTextField label="Mailbox" id="gmail-sa-address" type="email" value={address} required autoFocus
        onChange={v => { setAddress(v); setFormError('') }} placeholder="ops@example.com" testId="gmail-sa-address"
        slotProps={{ htmlInput: { spellCheck: false, autoComplete: 'off', maxLength: 254 } }} />
      <Box sx={{ display: 'flex', alignItems: 'center', gap: 1.5, minWidth: 0, mt: 2 }}>
        <JsonFilePicker label={fileName ? 'Change key' : 'Choose key'} testId="gmail-sa-file" onPick={pick} />
        <Typography variant="body2" noWrap title={fileName || undefined}
          sx={{ minWidth: 0, color: fileName ? 'text.primary' : 'text.secondary' }}>
          {fileName || 'JSON key file'}
        </Typography>
      </Box>
    </FormDialog>
  )
}
