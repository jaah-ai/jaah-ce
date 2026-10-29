import { type FormEvent, type MouseEvent, useEffect, useState } from 'react'
import { Navigate, Link as RouterLink, useNavigate } from 'react-router-dom'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import Link from '@mui/material/Link'
import Typography from '@mui/material/Typography'
import { mfaPath, useAuth } from '../auth'
import { ApiError, post, REFUSED_ADDRESS_KEY } from '../api'
import { IP_SHAPE } from '../lib/ipAllowlist'
import { IS_CE } from '../lib/edition'
import { useCeSetup } from '../lib/communityEdition'
import { isEnabled } from '../lib/editionFeatures'
import { useSubmit } from '../hooks/useSubmit'
import Banner from '../ui/Banner'
import FormTextField from '../ui/FormTextField'
import { monoSx } from '../ui/listStyles'
import { ShieldIcon } from '../ui/icons'
import { TotpField } from './MfaVerify'
import {
  EntryCard,
  EntryDivider,
  EntryFooter,
  EntryHeading,
  EntrySubmit,
  ProviderLink,
  REDIRECT_PROVIDERS,
  ProviderButtons,
  SIGNUP_CLOSED_COPY,
  SignupLinkButtons,
  type SignupLinks,
  footerActionSx,
  googleMount,
  tapFloorInput,
  useGoogleButton,
  usePublicSettings,
  useSignupOpen,
  useStripLoginParams,
} from './authEntry'

/* A refused redirect sign-in (or sign-up) lands on `/login?login_error=<slug>&login_provider=<key>[&login_detail=<text>]`.
 * The slugs are a CLOSED set pinned with the API; any other slug reads as the generic failure — the raw slug
 * is never shown. `detail` is the server's reason, where a slug carries one. */
const LOGIN_ERROR_COPY: Record<string, (provider: string, detail: string | null) => string> = {
  no_account: p => `No Jaah account for this ${p} account`,
  already_linked: p => `Your Jaah account is already linked to another ${p} account`,
  ambiguous: p => `This ${p} account matches more than one Jaah account`,
  not_configured: p => `${p} sign-in isn't set up`,
  signup_closed: () => SIGNUP_CLOSED_COPY,
  user_limit_reached: () => 'The user limit is reached. Ask the owner to raise it.',
  account_disabled: () => 'This account is disabled. Ask an administrator.',
  superadmin_password: () => "Operator accounts can't sign in with a password. Use Google, GitHub or Microsoft.",
  password_off: () => "This account can't sign in with a password. Use Google, GitHub or Microsoft.",
  sso_required: () => 'Single sign-on is required. Use your Google or Microsoft account.',
  locked: () => 'Too many failed sign-ins. Try again in 15 minutes.',
  org_not_approved: (_, reason) =>
    reason ? `Your organization was not approved: “${reason}”` : 'Your organization was not approved',
  /* Never shown as a banner on `/login` (that page answers it with `UnderReview`); the line any other page
     that meets the slug falls back to. */
  org_pending: () => 'Your application is under review',
  org_deleted: () => 'Your organization was deleted',
  /* The org's IP allowlist refused this address. `/login` answers it with the `NetworkBlocked` panel; this is
     the line the sign-up pages fall back to. The detail is the address: the server's own (a POST door's
     `reason`) or the one `api.ts::signalIpNotAllowed` stored — never the URL's (`refusedAddress`). */
  /* The org's own SAML/OIDC door (`/api/auth/sso/`): every one points at what to do next. */
  sso_enforced: () => 'SSO is required. Use Sign in with SSO.',
  sso_not_provisioned: () => "You don't have access yet. Ask your admin to add you.",
  sso_domain_mismatch: () => "Your email domain isn't verified for SSO. Ask your admin to verify it.",
  sso_not_on_plan: () => "SSO isn't on your plan. Ask your admin, or sign in another way.",
  sso_subject_mismatch: () => "This identity provider account isn't the one linked to you. Ask your admin to reset your SSO link.",
  sso_link_unproven: () => "Your identity provider can't link this account yet. Ask your admin.",
  sso_failed: () => 'SSO sign-in failed. Try again, or ask your admin to run Test in SSO settings.',
  ip_not_allowed: (_, ip) =>
    'Access is allowed only from approved networks. ' +
    (ip && IP_SHAPE.test(ip) ? `Ask an admin to add this address: ${ip}.` : 'Ask an admin to add your current address.'),
}
/* The server caps a reason at 200 characters; a hand-made URL is capped here too, and the reason is always
 * QUOTED, so text a stranger put in a link reads as a quotation, never as the page's own words. */
const DETAIL_MAX = 200
/* The slugs about the ORG, not the provider: worded even when `login_provider` is absent or unknown. */
const PROVIDERLESS = new Set([
  'signup_closed', 'org_not_approved', 'org_pending', 'org_deleted', 'user_limit_reached', 'account_disabled',
  'superadmin_password', 'password_off', 'sso_required', 'locked', 'ip_not_allowed',
  'sso_enforced', 'sso_not_provisioned', 'sso_domain_mismatch', 'sso_not_on_plan', 'sso_subject_mismatch',
  'sso_link_unproven', 'sso_failed',
])

/* A pending org's user (`apps/api/app/org_gate.py`): their application awaits review, so no door signs them
 * in. Nothing failed, so it is never an error: `/login` answers it with the `UnderReview` panel, and the
 * sign-up pages that meet it send the visitor there (`UNDER_REVIEW_PATH`). */
const ORG_PENDING = 'org_pending'
export const UNDER_REVIEW_PATH = `/login?login_error=${ORG_PENDING}`

/** Whether a POST sign-in or sign-up refusal is the pending-org one (a 403 `{code: 'org_pending'}`). */
export function isUnderReview(err: unknown): boolean {
  return err instanceof ApiError && err.status === 403 &&
    (err.detail as { code?: unknown } | null)?.code === ORG_PENDING
}

const IP_NOT_ALLOWED = 'ip_not_allowed'
/* An address worth naming: the shape of one, else null (the panel says "your current address"). */
const shownAddress = (ip: unknown): string | null => (typeof ip === 'string' && IP_SHAPE.test(ip) ? ip : null)

/** A POST sign-in the org's IP allowlist refused (a 403 `{code: 'ip_not_allowed', reason: <address>}`): the
 *  address to name, null when it names none; undefined for any other failure. */
function ipRefusal(err: unknown): { ip: string | null } | undefined {
  if (!(err instanceof ApiError) || err.status !== 403) return undefined
  const detail = err.detail as { code?: unknown; reason?: unknown } | null
  return detail?.code === IP_NOT_ALLOWED ? { ip: shownAddress(detail.reason) } : undefined
}

const failedCopy = (p: string) => `${p} sign-in failed, try again`

/* `/login?reason=idle` — where a HIPAA org's idle logoff lands (`api.ts::IDLE_LOGIN_PATH`). Nothing failed,
 * so it is a notice, not an error. */
export const IDLE_SIGN_OUT_COPY = 'Signed out after 15 minutes of inactivity.'

/** The notice for a sign-out the server made, or null when there is none. Shown once: `reason` lives only in
 *  the address `useStripLoginParams` strips, so a reload or a later visit says nothing more. */
export function signOutNotice(search: string): string | null {
  return new URLSearchParams(search).get('reason') === 'idle' ? IDLE_SIGN_OUT_COPY : null
}

/** The address the IP allowlist refused, as the app stored it before coming here (`api.ts::signalIpNotAllowed`);
 *  null when there is none. A link names a slug, never the address: an admin must not be told to add one a
 *  stranger chose. */
function refusedAddress(): string | null {
  try {
    return window.sessionStorage.getItem(REFUSED_ADDRESS_KEY)
  } catch {
    return null
  }
}

/** The banner text for a redirect refusal in `search`, or null when there is none. */
export function loginErrorMessage(search: string): string | null {
  const params = new URLSearchParams(search)
  const slug = params.get('login_error')
  if (slug === null) return null
  const detail = slug === IP_NOT_ALLOWED ? refusedAddress() : params.get('login_detail')?.slice(0, DETAIL_MAX) || null
  if (PROVIDERLESS.has(slug)) return LOGIN_ERROR_COPY[slug]('', detail)
  const provider = REDIRECT_PROVIDERS.find(p => p.key === params.get('login_provider'))?.name
  if (!provider) return 'Sign-in failed, try again'
  return (Object.hasOwn(LOGIN_ERROR_COPY, slug) ? LOGIN_ERROR_COPY[slug] : failedCopy)(provider, detail)
}

/** The banner text for a POST sign-in or sign-up (password, Google, org naming) the API refused for an
 *  org-level reason — a 403 whose `detail` is `{code, reason?}` with `code` one of the provider-less slugs
 *  (`signup_closed`, `org_not_approved`, …) — worded like the redirect door's. Null for any other failure,
 *  which keeps `useSubmit`'s own handling. */
export function postRefusalMessage(err: unknown): string | null {
  if (!(err instanceof ApiError) || err.status !== 403) return null
  const detail = err.detail as { code?: unknown; reason?: unknown } | null
  if (typeof detail !== 'object' || detail === null) return null
  if (typeof detail.code !== 'string' || !PROVIDERLESS.has(detail.code)) return null
  return LOGIN_ERROR_COPY[detail.code](
    '', typeof detail.reason === 'string' ? detail.reason.slice(0, DETAIL_MAX) || null : null,
  )
}

/* A password sign-in the API refuses until a TOTP code comes with it: the identifier is under the lockout's
 * step-up (`apps/api/app/login_lockout.py`) — a 401 `{code: 'step_up_required'}`. One answer for every
 * refusal under it (a wrong password and a wrong code alike), so one line of copy; null for any other failure. */
const STEP_UP_COPY =
  'Too many failed sign-ins for this account. Enter your password and a current authenticator code.'

export function stepUpMessage(err: unknown): string | null {
  if (!(err instanceof ApiError) || err.status !== 401) return null
  return (err.detail as { code?: unknown } | null)?.code === 'step_up_required' ? STEP_UP_COPY : null
}

/** The answer to a pending org's sign-in: what happens next, and something worth doing meanwhile. Inside
 *  the same `EntryCard` as the form it replaces, so it reads as the console's own page, not an error. */
function UnderReview({ links, onBack }: { links: SignupLinks; onBack: () => void }) {
  return (
    <>
      <Box data-testid="under-review" sx={{ textAlign: 'center', display: 'flex', flexDirection: 'column', gap: 1 }}>
        <Typography variant="h6" component="h1">
          Your application is under review
        </Typography>
        <Typography variant="body2" sx={{ color: 'text.secondary' }}>
          You’ll get a welcome email once it’s approved.
        </Typography>
      </Box>
      <SignupLinkButtons links={links} />
      <EntryFooter lead="Used a different account?">
        <Button variant="text" onClick={onBack} sx={footerActionSx}>
          Back to sign in
        </Button>
      </EntryFooter>
    </>
  )
}

/** The answer to a sign-in (or a live session) the org's IP allowlist refused: the address to give an admin.
 *  Like `UnderReview`, the form's replacement, not an error over it — the form is no use from this network, but
 *  another network's sign-in is, so "Back to sign in" brings it back. */
function NetworkBlocked({ ip, onBack }: { ip: string | null; onBack: () => void }) {
  return (
    <>
      <Box data-testid="network-blocked" sx={{ textAlign: 'center', display: 'flex', flexDirection: 'column', gap: 1 }}>
        <Typography variant="h6" component="h1">
          Your network isn’t allowed
        </Typography>
        <Typography variant="body2" sx={{ color: 'text.secondary', overflowWrap: 'anywhere' }}>
          Sign-in is allowed only from approved networks. Ask an admin to add{' '}
          {ip ? <>this address: <Box component="span" sx={monoSx}>{ip}</Box>.</> : 'your current address.'}
        </Typography>
      </Box>
      <EntryFooter lead="On another network?">
        <Button variant="text" onClick={onBack} sx={footerActionSx}>
          Back to sign in
        </Button>
      </EntryFooter>
    </>
  )
}

/* `/login?sso=1` opens the SSO step directly (an org switch into an SSO-enforced org points here, `lib/orgSwitch`). */
const SSO_PARAM = 'sso'
export const SSO_LOGIN_PATH = `/login?${SSO_PARAM}=1`

/** Drop `?sso=1` from the address on Back, so a reload shows the form the visitor came back to. */
function stripSsoParam() {
  const url = new URL(window.location.href)
  if (!url.searchParams.has(SSO_PARAM)) return
  url.searchParams.delete(SSO_PARAM)
  window.history.replaceState(window.history.state, '', url.pathname + url.search + url.hash)
}

/** `POST /api/auth/sso/discover`: where an email's org starts SSO (a same-origin path). 404/403 carry the reason. */
const discoverSso = (email: string) => post<{ url: string }>('/api/auth/sso/discover', { email })

/** Leave for the org's IdP: POST the discovered start URL (a same-origin path) as a form. The server begins a sign-in
 *  only on a same-origin POST — a GET, which any link makes, asks "Continue to <org>?" (login CSRF). */
function postSsoStart(url: string): void {
  const target = new URL(url, window.location.origin)
  const form = document.createElement('form')
  form.method = 'post'
  form.action = target.pathname
  for (const [name, value] of target.searchParams) {
    const input = document.createElement('input')
    input.type = 'hidden'
    input.name = name
    input.value = value
    form.appendChild(input)
  }
  document.body.appendChild(form)
  form.submit()
}

/** The SSO step, in place of the password form: an email names its org (`POST /api/auth/sso/discover`), and the
 *  browser leaves for that org's IdP. A refusal (no SSO for the domain, off the plan) is the server's own line. */
function SsoEntry({ initialEmail, ssoOrigin, onBack }: {
  initialEmail: string
  /** Where the sign-in finishes (`FRONTEND_URL`); named when it is not this origin (a tailnet console). */
  ssoOrigin: string | null
  onBack: () => void
}) {
  const [email, setEmail] = useState(initialEmail)
  const [leaving, setLeaving] = useState(false)
  const { busy, formError, run } = useSubmit()
  const elsewhere = ssoOrigin && ssoOrigin !== window.location.origin ? hostOf(ssoOrigin) : null

  async function handleSubmit(e: FormEvent) {
    e.preventDefault()
    await run('Could not start SSO. Try again.', async () => {
      const { url } = await discoverSso(email.trim())
      // Same-origin only: the server builds it; never follow anything else.
      if (!url.startsWith('/') || url.startsWith('//')) throw new Error('Could not start SSO. Try again.')
      setLeaving(true)
      postSsoStart(url)
    })
  }

  return (
    <EntryCard onSubmit={handleSubmit}>
      <Typography variant="h6" component="h1" align="center">Sign in with SSO</Typography>
      {formError && <Banner severity="error" testId="sso-entry-error">{formError}</Banner>}
      <FormTextField
        label="Email" required
        id="sso-email"
        type="email"
        value={email}
        onChange={setEmail}
        autoFocus
        autoComplete="email"
        placeholder="you@company.com"
        margin="none"
        sx={tapFloorInput}
      />
      {elsewhere && (
        <Typography variant="body2" sx={{ color: 'text.secondary' }} data-testid="sso-entry-elsewhere">
          Signs you in at {elsewhere}.
        </Typography>
      )}
      <EntrySubmit type="submit" disabled={busy || leaving || !email.trim()} busy={busy || leaving}>Continue</EntrySubmit>
      <Button variant="text" onClick={onBack} disabled={leaving} sx={footerActionSx}>
        Back
      </Button>
    </EntryCard>
  )
}

/** `https://app.example.com` -> `app.example.com`; the raw value when it does not parse. */
function hostOf(origin: string): string {
  try {
    return new URL(origin).host
  } catch {
    return origin
  }
}

/** The email `/login?email=` names (a welcome email's sign-in link), or empty. */
function prefilledEmail(): string {
  return new URLSearchParams(window.location.search).get('email')?.trim() ?? ''
}

export default function Login() {
  const { login, loginWithGoogle, admin, loading, resolving } = useAuth()
  const navigate = useNavigate()
  // Pre-auth, unauthenticated read; failure just leaves the buttons absent (password still works).
  const { googleClientId, redirectProviders, signupLinks, ssoOrigin } = usePublicSettings()
  // Unknown (loading, failed) reads as open: `/signup` re-checks.
  const signupOpen = useSignupOpen()
  /* Read once from the landing URL; `useStripLoginParams` strips the params so a reload does not replay it.
     Cleared by the next password or Google attempt, whose own outcome then owns the banner. */
  const [underReview, setUnderReview] = useState(
    () => new URLSearchParams(window.location.search).get('login_error') === ORG_PENDING,
  )
  const [blocked, setBlocked] = useState(() =>
    new URLSearchParams(window.location.search).get('login_error') === IP_NOT_ALLOWED
      ? { ip: shownAddress(refusedAddress()) } : undefined,
  )
  const [redirectError, setRedirectError] = useState(() =>
    underReview || blocked ? null : loginErrorMessage(window.location.search),
  )
  const [notice, setNotice] = useState(() => signOutNotice(window.location.search))
  const [sso, setSso] = useState(() => new URLSearchParams(window.location.search).get(SSO_PARAM) === '1')

  /* An already signed-in visitor leaves for the console, but only on the LIVE answer: the stored snapshot
     outlives an expired session, and acting on it bounced `/login` -> `/` -> `/login` (the 401), remounting
     this form under whatever was being typed. */
  useEffect(() => {
    if (!loading && !resolving && admin) navigate('/', { replace: true })
  }, [admin, loading, resolving, navigate])
  const [email, setEmail] = useState(prefilledEmail)
  const [password, setPassword] = useState('')
  /* Shown only once the API asked for it (`stepUpMessage`); sent with every later attempt. */
  const [stepUp, setStepUp] = useState(false)
  const [totp, setTotp] = useState('')
  const ceSetup = useCeSetup()
  const { busy, formError: error, run } = useSubmit()

  useStripLoginParams()
  // Off while a panel replaces the form, so "Back to sign in" mounts the button afresh.
  const googleButtonRef = useGoogleButton(googleClientId, !underReview && !blocked && !sso, credential => {
    void handleGoogleCredential(credential)
  })

  async function handleSubmit(e: FormEvent) {
    e.preventDefault()
    setRedirectError(null)
    setNotice(null)
    await run('Login failed', async () => {
      let stage
      try {
        stage = stepUp ? await login(email, password, totp) : await login(email, password)
      } catch (err) {
        const stepUpCopy = stepUpMessage(err)
        if (stepUpCopy) {
          setStepUp(true)
          setTotp('')
          setRedirectError(stepUpCopy)
          return
        }
        if (!showRefusal(err)) throw err
        return
      }
      navigate(stage ? mfaPath(stage) : '/', { replace: true })
    })
  }

  async function handleGoogleCredential(credential: string) {
    setRedirectError(null)
    await run('Google sign-in failed', async () => {
      let stage
      try {
        stage = await loginWithGoogle(credential)
      } catch (err) {
        if (!showRefusal(err)) throw err
        return
      }
      navigate(stage ? mfaPath(stage) : '/', { replace: true })
    })
  }

  /* An org refusal takes the redirect door's banner (same copy) — a pending org the Under review panel, a
     refused network the Network blocked one; anything else is rethrown to `useSubmit`. */
  function showRefusal(err: unknown): boolean {
    if (isUnderReview(err)) {
      setUnderReview(true)
      return true
    }
    const refused = ipRefusal(err)
    if (refused) {
      setBlocked(refused)
      return true
    }
    const message = postRefusalMessage(err)
    if (message) setRedirectError(message)
    return message !== null
  }

  /* The community edition's fresh install has no user yet: its first run creates one (`pages/ce/CeSetupPage.tsx`). */
  if (ceSetup.data?.needs_setup === true) return <Navigate to="/setup" replace />
  // Until it answers, nothing: a fresh install would flash the sign-in form on its way to /setup.
  if (IS_CE && ceSetup.isPending) return null

  if (underReview) {
    return (
      <EntryCard>
        <UnderReview
          links={signupLinks}
          onBack={() => {
            // The email stays (the likely retry is another door); the refused password does not.
            setPassword('')
            setUnderReview(false)
          }}
        />
      </EntryCard>
    )
  }

  if (blocked) {
    return (
      <EntryCard>
        <NetworkBlocked
          ip={blocked.ip}
          onBack={() => {
            setPassword('')
            setBlocked(undefined)
          }}
        />
      </EntryCard>
    )
  }

  if (sso && isEnabled('sso')) {
    return (
      <SsoEntry
        // A typed address carries over; a username does not.
        initialEmail={email.includes('@') ? email : ''}
        ssoOrigin={ssoOrigin}
        onBack={() => {
          stripSsoParam()
          setSso(false)
        }}
      />
    )
  }

  return (
    <EntryCard onSubmit={handleSubmit}>
      <EntryHeading title="Sign in to your account" />
      {(error || redirectError) ? <Banner severity="error">{error || redirectError}</Banner>
        : notice && <Banner severity="info">{notice}</Banner>}

      <FormTextField
        label="Email / Username" required
        id="login-email"
        type="text"
        value={email}
        onChange={setEmail}
        autoFocus={email === ''}
        autoComplete="username"
        margin="none"
        sx={tapFloorInput}
      />
      <FormTextField
        label="Password" required
        id="login-password"
        type="password"
        value={password}
        onChange={setPassword}
        autoComplete="current-password"
        autoFocus={email !== ''}
        margin="none"
        sx={tapFloorInput}
      />
      {/* Right-aligned under the field it is about; `py` lifts the link to the ≤sm tap floor. The community edition
          sends no mail, so it offers no reset by mail (nor sign-up, providers or SSO, below). */}
      {!IS_CE && <Link
        component={RouterLink}
        to="/forgot-password"
        variant="body2"
        underline="hover"
        sx={{ alignSelf: 'flex-end', mt: -1, py: 0.5 }}
      >
        Forgot password?
      </Link>}
      {stepUp && <TotpField id="login-totp" label="Authenticator code" value={totp} onChange={setTotp} />}
      <EntrySubmit type="submit" disabled={busy} busy={busy}>{busy ? 'Signing in…' : 'Sign in'}</EntrySubmit>

      {/* One provider group under the password form, in a fixed order (Google, GitHub, Microsoft, SSO);
          only configured providers render; SSO always does, so the divider always shows. */}
      {!IS_CE && <>
      <EntryDivider />

      {/* Gated on `googleClientId`: with no client id this host offers no Google sign-in, so no mount. */}
      <ProviderButtons
        google={googleClientId && <Box ref={googleButtonRef} data-testid="google-signin" sx={googleMount} />}
        providers={redirectProviders}
        verb="Sign in"
      />

      {/* The org's own IdP. An anchor to `?sso=1` (so it opens in a new tab too); a plain click swaps the card. */}
      <ProviderLink
        label="Sign in with SSO"
        href={SSO_LOGIN_PATH}
        onClick={(e: MouseEvent) => {
          if (e.metaKey || e.ctrlKey || e.shiftKey || e.button !== 0) return
          e.preventDefault()
          setRedirectError(null)
          setNotice(null)
          setSso(true)
        }}
        testId="sso-signin"
        mark={<ShieldIcon size={18} />}
      />

      {/* The self-service door. Only a KNOWN closed status swaps the link for the closed line; while the
          status is unknown the link shows, and `/signup` re-checks. */}
      {signupOpen === false ? (
        <Typography variant="body2" sx={{ color: 'text.secondary' }} align="center" data-testid="signup-closed">
          {SIGNUP_CLOSED_COPY}
        </Typography>
      ) : (
        <EntryFooter lead="New to Jaah?">
          <Button component={RouterLink} to="/signup" variant="text" sx={footerActionSx}>
            Create an account
          </Button>
        </EntryFooter>
      )}
      </>}
    </EntryCard>
  )
}
