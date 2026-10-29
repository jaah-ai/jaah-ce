import { useState, type FormEvent, type JSX } from 'react'
import Box from '@mui/material/Box'
import TextField from '@mui/material/TextField'
import SubmitButton from '../../ui/SubmitButton'

/** The authenticator-code step of a switch whose target org asked for one (`lib/orgSwitch.ts`, door `otp`): one
 *  field (a TOTP or a recovery code, so not `TotpField`'s six digits) and its submit. The deep-link interstitial
 *  (`OtherOrgGate`) and the invitation page; the account menu's switcher keeps its own row-sized one. */
export default function SwitchCodeForm({
  busy,
  error,
  onCode,
  testIdPrefix,
}: {
  busy: boolean
  /** Why the last code did not switch (`FACTOR_ERRORS`). */
  error?: string
  onCode: (code: string) => void
  testIdPrefix: string
}): JSX.Element {
  const [code, setCode] = useState('')
  function submit(e: FormEvent) {
    e.preventDefault()
    if (code.trim()) onCode(code.trim())
  }
  return (
    <Box component="form" onSubmit={submit} sx={{ display: 'flex', flexDirection: 'column', gap: 1.5 }}>
      <TextField
        label="Authenticator code"
        value={code}
        onChange={e => setCode(e.target.value)}
        autoFocus
        autoComplete="one-time-code"
        error={Boolean(error)}
        helperText={error}
        slotProps={{ htmlInput: { inputMode: 'numeric', maxLength: 64, 'data-testid': `${testIdPrefix}-code` } }}
        size="small"
      />
      <SubmitButton busy={busy} busyLabel="Switching…" disabled={!code.trim()} testId={`${testIdPrefix}-verify`} sx={{ alignSelf: 'flex-start' }}>
        Verify and switch
      </SubmitButton>
    </Box>
  )
}
