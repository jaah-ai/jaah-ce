import { type FormEvent, useEffect, useMemo, useRef, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import qrcode from 'qrcode-generator'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import Typography from '@mui/material/Typography'
import { useTheme, type Theme } from '@mui/material/styles'
import { type Admin, useAuth } from '../auth'
import { ApiError, apiDetail, post } from '../api'
import Banner from '../ui/Banner'
import { CopyButton } from '../ui/CopyButton'
import { monoSx as sharedMonoSx } from '../ui/listStyles'
import { EntryCard, EntryHeading, EntryLoading, EntrySubmit, submitSx } from './authEntry'
import {
  MfaExpired,
  MfaFooter,
  TotpField,
  TrustDeviceCheckbox,
  mfaRefusal,
  useMfaCodeSubmit,
  usePendingMfa,
} from './MfaVerify'

/* `/login/mfa/enrol` — forced first-time TOTP set-up after a first factor. Two steps: scan + confirm, then
 * the recovery codes, shown once. No skip: the only way off is back to `/login`. */

type EnrolStart = { otpauth_uri: string; secret: string }
type EnrolConfirm = { recovery_codes: string[]; user: Admin }

/* Four modules of quiet zone (the QR spec's minimum) inside the white square. */
const QUIET = 4
const QR_PX = 180

/* Drawn as one SVG path from the module matrix: no innerHTML, no data URL. Black on white in both themes —
 * scanners expect dark-on-light. */
function QrCode({ value }: { value: string }) {
  const theme = useTheme()
  const { size, d } = useMemo(() => {
    const qr = qrcode(0, 'M')
    qr.addData(value)
    qr.make()
    const n = qr.getModuleCount()
    let path = ''
    for (let r = 0; r < n; r++) {
      for (let c = 0; c < n; c++) if (qr.isDark(r, c)) path += `M${c + QUIET} ${r + QUIET}h1v1h-1z`
    }
    return { size: n + QUIET * 2, d: path }
  }, [value])
  return (
    <Box
      component="svg"
      data-testid="mfa-qr"
      role="img"
      aria-label="QR code for your authenticator app"
      viewBox={`0 0 ${size} ${size}`}
      shapeRendering="crispEdges"
      sx={{ display: 'block', mx: 'auto', width: QR_PX, height: QR_PX, borderRadius: 1 }}
    >
      <rect width={size} height={size} fill={theme.palette.common.white} />
      <path d={d} fill={theme.palette.common.black} />
    </Box>
  )
}

/* Selectable, so a code can be copied out by hand. */
const monoSx = (t: Theme) => ({ ...sharedMonoSx(t), userSelect: 'text' as const })

export default function MfaEnrol() {
  const { completeSignIn } = useAuth()
  const navigate = useNavigate()
  const { pending, loadError } = usePendingMfa('enrol')
  const [factor, setFactor] = useState<EnrolStart | null>(null)
  const [startError, setStartError] = useState('')
  const [code, setCode] = useState('')
  const [rememberDevice, setRememberDevice] = useState(false)
  const [done, setDone] = useState<EnrolConfirm | null>(null)
  const { busy, errors, setErrors, formError, expired, setExpired, submitCode } = useMfaCodeSubmit()
  /* Each start mints a new secret; once per mount, so StrictMode's double effect cannot leave the QR
     showing a secret the server already replaced. */
  const started = useRef(false)

  useEffect(() => {
    if (!pending || started.current) return
    started.current = true
    post<EnrolStart>('/api/auth/mfa/enrol/start')
      .then(setFactor)
      .catch(err => {
        if (mfaRefusal(err) === 'mfa_expired') setExpired('mfa_expired')
        else if (err instanceof ApiError && err.status === 409) navigate('/login/mfa', { replace: true })
        else setStartError(apiDetail(err, 'Could not start set-up'))
      })
  }, [pending, navigate])

  async function handleConfirm(e: FormEvent) {
    e.preventDefault()
    if (code.length !== 6) {
      setErrors({ code: 'Enter the 6-digit code' })
      return
    }
    await submitCode('Set-up failed', async () => {
      setDone(await post<EnrolConfirm>('/api/auth/mfa/enrol/confirm', { code, remember_device: rememberDevice }))
    }, () => setCode(''))
  }

  function handleContinue() {
    if (!done) return
    completeSignIn(done.user)
    navigate('/', { replace: true })
  }

  if (expired) return <MfaExpired throttled={expired === 'mfa_throttled'} />

  if (done) {
    return (
      <EntryCard>
        <EntryHeading title="Recovery codes">Save these. Each works once.</EntryHeading>
        <Box
          component="ul"
          data-testid="recovery-codes"
          sx={t => ({
            ...monoSx(t),
            listStyle: 'none',
            m: 0,
            p: 1.5,
            display: 'grid',
            gridTemplateColumns: 'repeat(2, minmax(0, 1fr))',
            gap: 1,
            textAlign: 'center',
            fontSize: '0.875rem',
            border: 1,
            borderColor: 'divider',
            borderRadius: 1,
            bgcolor: 'action.hover',
          })}
        >
          {done.recovery_codes.map(c => (
            <li key={c}>{c}</li>
          ))}
        </Box>
        <CopyButton text={() => done.recovery_codes.join('\n')} label="Copy" testId="recovery-codes-copy" sx={{ alignSelf: 'center' }} />
        <Button variant="contained" color="primary" fullWidth onClick={handleContinue} sx={submitSx}>
          Continue
        </Button>
      </EntryCard>
    )
  }

  if (!pending || !factor) return <EntryLoading error={loadError || startError} />

  return (
    <EntryCard onSubmit={handleConfirm}>
      {/* Names whose policy forces the set-up (the user's org, `routes/mfa.py::PendingOut.org_name`): with no skip, the
          reader should know who to ask. */}
      <EntryHeading title="Set up two-factor sign-in">
        {pending.org_name && <>{pending.org_name} requires it. </>}
        Scan with an authenticator app, then enter the 6-digit code.
      </EntryHeading>

      {formError && <Banner severity="error">{formError}</Banner>}

      <QrCode value={factor.otpauth_uri} />
      <Box sx={{ textAlign: 'center' }}>
        <Typography variant="caption" sx={{ color: 'text.secondary' }} component="p">
          Can't scan? Enter this key:
        </Typography>
        <Box
          component="code"
          data-testid="mfa-secret"
          // `balance`: a narrow card breaks the key into even halves, not seven groups and a straggler.
          sx={t => ({ ...monoSx(t), display: 'block', fontSize: '0.875rem', textWrap: 'balance', wordBreak: 'break-word' })}
        >
          {factor.secret.match(/.{1,4}/g)?.join(' ')}
        </Box>
      </Box>

      {/* No autofocus: a phone keyboard would cover the QR. */}
      <TotpField value={code} onChange={setCode} error={errors.code} autoFocus={false} />

      <TrustDeviceCheckbox
        days={pending.remember_days}
        checked={rememberDevice}
        onChange={setRememberDevice}
        disabled={busy}
      />

      <EntrySubmit type="submit" disabled={busy} busy={busy}>Confirm</EntrySubmit>

      <MfaFooter />
    </EntryCard>
  )
}
