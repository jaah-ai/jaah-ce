import { type FormEvent, useEffect, useState } from 'react'
import { Link as RouterLink, useNavigate } from 'react-router-dom'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import Checkbox from '@mui/material/Checkbox'
import FormControlLabel from '@mui/material/FormControlLabel'
import Typography from '@mui/material/Typography'
import { type Admin, type MfaStage, mfaPath, useAuth } from '../auth'
import { ApiError, apiDetail, get, post } from '../api'
import { useSubmit } from '../hooks/useSubmit'
import Banner from '../ui/Banner'
import FormTextField from '../ui/FormTextField'
import { EntryCard, EntryFooter, EntryHeading, EntryLoading, EntrySubmit, footerActionSx, submitSx, tapFloorInput } from './authEntry'

/* `/login/mfa` — the second factor. Public: the pending-MFA cookie a first factor set is the only
 * credential, and it lives 10 minutes. `MfaEnrol` borrows this file's kit. */

/* `remember_days`: how long a trusted browser skips the code (`apps/api/app/mfa.py`); 0 or absent = the
 * deployment offers no trust, so the checkbox is withheld. */
/* `org_name`: the org whose sign-in policy asks for the factor (`routes/mfa.py::PendingOut`), named on the enrol page. */
type PendingMfa = { stage: MfaStage; email: string; remember_days?: number; org_name?: string | null }

export const BAD_FACTOR_COPY = "That code didn't work. Try again."
/* The server's per-user window (`apps/api/app/mfa.py::FAILURE_WINDOW`). */
export const THROTTLED_COPY = 'Too many wrong codes. Try again in 15 minutes.'

type MfaRefusal = 'bad_factor' | 'mfa_expired' | 'mfa_throttled'
const REFUSALS = new Set<unknown>(['bad_factor', 'mfa_expired', 'mfa_throttled'])

/** The second-factor refusal `err` carries (`{detail: {code}}` on a 401), or null for any other failure. */
export function mfaRefusal(err: unknown): MfaRefusal | null {
  if (!(err instanceof ApiError) || err.status !== 401) return null
  const code = (err.detail as { code?: unknown } | null)?.code
  return REFUSALS.has(code) ? (code as MfaRefusal) : null
}

/** `useSubmit` for a code submit: expired/throttled latches `expired`, a wrong code says so and `onBadFactor` clears the field. */
export function useMfaCodeSubmit() {
  const [expired, setExpired] = useState<false | MfaRefusal>(false)
  const submit = useSubmit<'code'>()
  const submitCode = (fallback: string, send: () => Promise<void>, onBadFactor: () => void) =>
    submit.run(fallback, async () => {
      try {
        await send()
      } catch (err) {
        const refusal = mfaRefusal(err)
        if (refusal === 'mfa_expired' || refusal === 'mfa_throttled') setExpired(refusal)
        else if (refusal === 'bad_factor') {
          submit.setFormError(BAD_FACTOR_COPY)
          onBadFactor()
        } else throw err
      }
    })
  return { ...submit, expired, setExpired, submitCode }
}

/** Reads the pending sign-in, sending the viewer to `/login` without one and to the other MFA page when
 *  `stage` is not this page's. */
export function usePendingMfa(stage: MfaStage): { pending: PendingMfa | null; loadError: string } {
  const navigate = useNavigate()
  const [pending, setPending] = useState<PendingMfa | null>(null)
  const [loadError, setLoadError] = useState('')
  useEffect(() => {
    let cancelled = false
    get<PendingMfa>('/api/auth/mfa/pending')
      .then(p => {
        if (cancelled) return
        if (p.stage !== stage) navigate(mfaPath(p.stage), { replace: true })
        else setPending(p)
      })
      .catch(err => {
        if (cancelled) return
        if (err instanceof ApiError && err.status === 401) navigate('/login', { replace: true })
        else setLoadError(apiDetail(err, 'Could not load your sign-in'))
      })
    return () => {
      cancelled = true
    }
  }, [stage, navigate])
  return { pending, loadError }
}

export function MfaExpired({ throttled = false }: { throttled?: boolean }) {
  return (
    <EntryCard>
      <Banner severity="error">{throttled ? THROTTLED_COPY : 'Your sign-in expired.'}</Banner>
      <Button component={RouterLink} to="/login" variant="contained" fullWidth sx={submitSx}>
        Sign in again
      </Button>
    </EntryCard>
  )
}

/** The way off an MFA page: back to the first factor. There is no skip. */
export function MfaFooter() {
  return (
    <EntryFooter lead="Not you?">
      <Button component={RouterLink} to="/login" variant="text" sx={footerActionSx}>
        Sign in again
      </Button>
    </EntryFooter>
  )
}

/** The 6-digit TOTP field (the MFA pages, Login's step-up). Digits only; the one-time-code hint lets a phone
 *  offer an SMS/app autofill. */
export function TotpField({
  value,
  onChange,
  error,
  autoFocus = true,
  id = 'mfa-code',
  label = 'Code',
  testId,
}: {
  value: string
  onChange: (v: string) => void
  error?: string
  autoFocus?: boolean
  id?: string
  label?: string
  testId?: string
}) {
  return (
    <FormTextField
      label={label} required error={error} id={id} testId={testId}
      value={value}
      onChange={v => onChange(v.replace(/\D/g, '').slice(0, 6))}
      autoFocus={autoFocus}
      autoComplete="one-time-code"
      margin="none"
      slotProps={{ htmlInput: { inputMode: 'numeric', maxLength: 6 } }}
      sx={tapFloorInput}
    />
  )
}

/** "Trust this device for N days" — unchecked by default; the page sends it as `remember_device`. Renders
 *  nothing when the server offers no trust window. Label after the box (the sign-in-form convention, unlike
 *  `ui/SettingCheckbox`'s settings column); the box keeps its 44px tap floor. */
export function TrustDeviceCheckbox({
  days,
  checked,
  onChange,
  disabled,
}: {
  days: number | undefined
  checked: boolean
  onChange: (checked: boolean) => void
  disabled?: boolean
}) {
  if (!days || days <= 0) return null
  return (
    <FormControlLabel
      // Pulls the box's padding back so its edge sits on the field's left edge.
      sx={{ ml: '-0.5rem', mr: 0, alignSelf: 'flex-start' }}
      control={
        <Checkbox
          id="mfa-remember-device"
          checked={checked}
          disabled={disabled}
          onChange={e => onChange(e.target.checked)}
          sx={t => ({ p: '0.5rem', minWidth: t.custom.tapTargetPx, minHeight: t.custom.tapTargetPx })}
        />
      }
      label={
        <Typography component="span" variant="body2">
          Trust this device for {days} {days === 1 ? 'day' : 'days'}
        </Typography>
      }
    />
  )
}

export default function MfaVerify() {
  const { completeSignIn } = useAuth()
  const navigate = useNavigate()
  const { pending, loadError } = usePendingMfa('verify')
  const [useRecovery, setUseRecovery] = useState(false)
  const [code, setCode] = useState('')
  const [recoveryCode, setRecoveryCode] = useState('')
  const [rememberDevice, setRememberDevice] = useState(false)
  const { busy, errors, setErrors, formError, setFormError, expired, submitCode } = useMfaCodeSubmit()

  async function handleSubmit(e: FormEvent) {
    e.preventDefault()
    const factor = useRecovery ? { recovery_code: recoveryCode.trim() } : { code }
    if (useRecovery ? !factor.recovery_code : code.length !== 6) {
      setErrors({ code: useRecovery ? 'Enter a recovery code' : 'Enter the 6-digit code' })
      return
    }
    await submitCode('Verification failed', async () => {
      completeSignIn(await post<Admin>('/api/auth/mfa/verify', { ...factor, remember_device: rememberDevice }))
      navigate('/', { replace: true })
    }, () => {
      setCode('')
      setRecoveryCode('')
    })
  }

  function toggleRecovery() {
    setUseRecovery(r => !r)
    setErrors({})
    setFormError('')
  }

  if (expired) return <MfaExpired throttled={expired === 'mfa_throttled'} />
  if (!pending) return <EntryLoading error={loadError} />

  return (
    <EntryCard onSubmit={handleSubmit}>
      <EntryHeading title="Two-factor sign-in">
        {useRecovery ? 'Enter a recovery code for ' : 'Enter the code from your authenticator app for '}
        <Box component="span" sx={{ color: 'text.primary', fontWeight: 500 }}>
          {pending.email}
        </Box>
      </EntryHeading>

      {formError && <Banner severity="error">{formError}</Banner>}

      {useRecovery ? (
        <FormTextField
          label="Recovery code" required error={errors.code} id="mfa-recovery-code"
          value={recoveryCode}
          onChange={setRecoveryCode}
          autoFocus
          autoComplete="off"
          margin="none"
          slotProps={{ htmlInput: { autoCapitalize: 'none', spellCheck: false } }}
          sx={tapFloorInput}
        />
      ) : (
        <TotpField value={code} onChange={setCode} error={errors.code} />
      )}

      <TrustDeviceCheckbox
        days={pending.remember_days}
        checked={rememberDevice}
        onChange={setRememberDevice}
        disabled={busy}
      />

      <EntrySubmit type="submit" disabled={busy} busy={busy}>Verify</EntrySubmit>
      <Button type="button" variant="text" fullWidth onClick={toggleRecovery} sx={{ ...submitSx, textTransform: 'none' }}>
        {useRecovery ? 'Use an authenticator code' : 'Use a recovery code'}
      </Button>

      <MfaFooter />
    </EntryCard>
  )
}
