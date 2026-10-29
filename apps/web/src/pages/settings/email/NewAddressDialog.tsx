import { type JSX, useState } from 'react'
import { useSubmit } from '../../../hooks/useSubmit'
import { FormDialog } from '../../../ui/FormDialog'
import FormTextField from '../../../ui/FormTextField'
import { createTenantAddress, type TenantAddress } from './api'

/* New address: a one-shot act with one optional field, so a dialog. The server picks the address. One home for the
 * Addresses tab (`EmailSection.tsx`) and an Email automation's "Receives at" (`automations/EventSourceFields.tsx`). */

const MAX_LABEL = 200

export default function NewAddressDialog({ orgId, onClose, onCreated }: {
  orgId: string
  onClose: () => void
  onCreated: (row: TenantAddress) => void
}): JSX.Element {
  const [name, setName] = useState('')
  const { busy, formError, setFormError, run } = useSubmit()
  const submit = () => void run('Could not create the address. Retry.', async () => {
    onCreated(await createTenantAddress(orgId, name.trim()))
  })
  return (
    <FormDialog
      title="New address"
      submitLabel="Create"
      busyLabel="Creating…"
      busy={busy}
      error={formError}
      onSubmit={submit}
      onClose={onClose}
      modal
      maxWidth="xs"
      testId="email-address-dialog"
      submitTestId="email-address-submit"
    >
      <FormTextField label="Label" id="email-address-label-input" value={name} autoFocus placeholder="Support inbox"
        onChange={v => { setName(v); setFormError('') }} testId="email-address-label-input"
        slotProps={{ htmlInput: { maxLength: MAX_LABEL } }} />
    </FormDialog>
  )
}
