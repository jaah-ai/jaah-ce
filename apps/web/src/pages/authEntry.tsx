import { type FormEvent, type MouseEvent, type ReactNode, useEffect, useRef } from 'react'
import Box from '@mui/material/Box'
import Button, { type ButtonProps } from '@mui/material/Button'
import CircularProgress from '@mui/material/CircularProgress'
import Divider from '@mui/material/Divider'
import Paper from '@mui/material/Paper'
import Typography from '@mui/material/Typography'
import type { Theme } from '@mui/material/styles'
import type { SidebarColors } from '../theme'
import { get } from '../api'
import { useApiRead } from '../hooks/useApiRead'
import { useColorMode } from '../hooks/useColorMode'
import { safeHref } from '../lib/safeHref'
import BusyButton from '../ui/BusyButton'
import Banner from '../ui/Banner'
import BrandWordmark from '../components/BrandWordmark'
import { GitHubMark, MicrosoftMark } from '../components/ProviderMarks'

/* The pre-auth entry pages' shared kit — `/login` and the `/signup` pair (`signup/`). Single home for the
 * card, the provider buttons and the GSI mount, so the sign-in and sign-up doors read as one designed set. */

/* The Google OAuth client id is fetched at RUNTIME (GET /api/public/settings), not a `VITE_` build var:
 * one image serves every environment, and the API serves the same `GOOGLE_CLIENT_ID` it verifies against
 * (`app/routes/public.py`). Null/absent → no button (as POST /api/auth/google's 503). The GitHub and
 * Microsoft flags are the same fail-closed switch for their redirect doors: false/absent → no button
 * (the start door 503s then). */
type PublicSettings = {
  google_client_id?: string | null
  github_sign_in?: boolean
  microsoft_sign_in?: boolean
  /* Where "Sign in with SSO" finishes (`FRONTEND_URL`'s origin); a tailnet console's SSO lands there. */
  sso_origin?: string
  /* The sign-up flow's links and the legal version an applicant accepts (operator settings, "Organizations" >
   * "Sign-up"). Absent from an older API → null, and the caller leaves that link out. */
  tutorials_url?: string
  home_url?: string
  docs_url?: string
  terms_url?: string
  privacy_url?: string
  legal_version?: string
}

/** The sign-up flow's operator-set links; each is null until read (or when the read failed). */
export type SignupLinks = {
  tutorialsUrl: string | null
  homeUrl: string | null
  docsUrl: string | null
  termsUrl: string | null
  privacyUrl: string | null
  legalVersion: string | null
}

const NO_SIGNUP_LINKS: SignupLinks = {
  tutorialsUrl: null, homeUrl: null, docsUrl: null, termsUrl: null, privacyUrl: null, legalVersion: null,
}

/* The redirect providers: a plain full-page navigation to the API's start door (303 to the vendor), no
 * SDK, no poll, no spinner — so nothing here can hang. */
export const REDIRECT_PROVIDERS = [
  { key: 'github', name: 'GitHub', flag: 'github_sign_in', Mark: GitHubMark },
  { key: 'microsoft', name: 'Microsoft', flag: 'microsoft_sign_in', Mark: MicrosoftMark },
] as const
export type RedirectProvider = (typeof REDIRECT_PROVIDERS)[number]['key']

/** The configured providers and the sign-up links, from the pre-auth settings read. A failed read leaves every
 *  button absent (the password form, where there is one, still works) and every link null. Each link passes
 *  `safeHref` here, the one read every sign-up page shares, so a stored value that is not a web URL never
 *  reaches an `href`. `settled` turns true once a read has answered or failed; `reload` reads again. */
export function usePublicSettings(): {
  googleClientId: string | null
  redirectProviders: RedirectProvider[]
  signupLinks: SignupLinks
  ssoOrigin: string | null
  settled: boolean
  reload: () => void
} {
  /* `refresh`, not `reload`: a re-read keeps `settled` true, and a failed one keeps the last answer. */
  const { data: s, loading, refresh } = useApiRead(
    ['public-settings'], () => get<PublicSettings>('/api/public/settings'), 'Could not load the sign-in settings')
  return {
    googleClientId: s?.google_client_id || null,
    redirectProviders: s ? REDIRECT_PROVIDERS.filter(p => s[p.flag] === true).map(p => p.key) : [],
    signupLinks: s
      ? {
        tutorialsUrl: safeHref(s.tutorials_url) ?? null,
        homeUrl: safeHref(s.home_url) ?? null,
        docsUrl: safeHref(s.docs_url) ?? null,
        termsUrl: safeHref(s.terms_url) ?? null,
        privacyUrl: safeHref(s.privacy_url) ?? null,
        legalVersion: s.legal_version || null,
      }
      : NO_SIGNUP_LINKS,
    ssoOrigin: s?.sso_origin || null,
    settled: !loading,
    reload: refresh,
  }
}

/** Is self-service sign-up open? `null` while unknown (loading, or the read failed): callers treat unknown
 *  as open, because `/signup` re-checks and the API refuses a closed sign-up itself. */
export function useSignupOpen(): boolean | null {
  const { data } = useApiRead(['signup-status'], () => get<{ open?: unknown }>('/api/public/signup-status'), 'Could not load sign-up status')
  return typeof data?.open === 'boolean' ? data.open : null
}

/** The copy for a closed sign-up, shared by the refusal banner and the pages that pre-empt it. */
export const SIGNUP_CLOSED_COPY = 'Sign-up is closed — join the waitlist'

/** Strip a redirect refusal's params (`login_error`, `login_provider`, `login_detail`) and a sign-out's
 *  `reason` from the address once read, so a reload does not replay the banner or the idle sign-out notice
 *  (`Login.tsx::signOutNotice`). The router's own history state is kept. */
export function useStripLoginParams() {
  useEffect(() => {
    const url = new URL(window.location.href)
    const params = ['login_error', 'login_provider', 'login_detail', 'reason']
    if (!params.some(k => url.searchParams.has(k))) return
    for (const k of params) url.searchParams.delete(k)
    window.history.replaceState(window.history.state, '', url.pathname + url.search + url.hash)
  }, [])
}

/* One height for every entry point: the password submit, GSI's `large` button (44px), and the two
   redirect buttons. */
export const ENTRY_HEIGHT_PX = 44

/* The ONE primary action's geometry: the shared entry-point height at every width, so it lines up with the
 * provider buttons (the ≤sm tap floor is the same 44px). No local focus ring: `theme.ts`'s `MuiButtonBase`
 * override owns it. */
export const submitSx = { minHeight: ENTRY_HEIGHT_PX }

/* The ≤sm 44px tap floor (`theme.custom.tapTargetPx`), per instance because `theme.ts` pins it only for
 * controls without natural height; small inputs are 40px, small buttons ~31px. `minHeight` only raises.
 * `boxSizing: border-box` because `InputBase` is `content-box`. */
export const tapFloorInput = (t: Theme) => ({
  [t.breakpoints.down('sm')]: {
    '& .MuiOutlinedInput-input': { minHeight: t.custom.tapTargetPx, boxSizing: 'border-box' },
    /* Re-centre the resting label: MUI pins outlined+small at `translate(14px, 9px)` for a 40px control,
       so at 44px it sits 1.5px high. Same breakpoint as the floor. */
    '& .MuiInputLabel-outlined:not(.MuiInputLabel-shrink)': {
      transform: 'translate(14px, 10.5px) scale(1)',
    },
  },
})
/* The wordmark's `--sk-mark-*` stops, per scheme — the rail's tokens, set here without `schemed()` so this
   page stays outside the rail's colour-literal census (its provider buttons carry their brands' own). */
const markVars = (c: SidebarColors) => ({
  '--sk-mark-0': c.mark[0],
  '--sk-mark-1': c.mark[1],
  '--sk-mark-2': c.mark[2],
  '--sk-mark-core': c.markCore,
})

/* GSI renders its iframe 20px WIDER than the `width` passed — the visible button is `width`, padded 10px each
 * side — and clamps `width` to [200, 400], so a fixed width hung the button off a 320px viewport. The mount is
 * pulled out by half that chrome each side (`googleMount`) and asks for its width less the chrome, within GSI's
 * own bounds: the iframe overhangs the column, and the visible button spans exactly the column the fields, the
 * submit and the GitHub/Microsoft buttons fill. */
const GSI_CHROME_PX = 20
const GSI_MIN_WIDTH = 200
const GSI_MAX_WIDTH = 400

/* The GSI mount: pulled out by half GSI's chrome each side (above). Height floor for the third-party button (no
 * height option), applied TO THE IFRAME: the `[role=button]` inside is in a cross-origin document our CSS cannot
 * reach. GSI documents 40px for `size: 'large'` but ships 44px today, so this is a GUARD. `border-box`; only
 * raises; every width, so the set shares one height on desktop too. */
export const googleMount = {
  display: 'flex',
  justifyContent: 'center',
  mx: `${-GSI_CHROME_PX / 2}px`,
  '& iframe': { minHeight: ENTRY_HEIGHT_PX, boxSizing: 'border-box' },
}

/* GitHub/Microsoft drawn in GSI's `large`/`rectangular` geometry so the three read as one set: the column's full
 * width (the fields' and the submit's, which GSI's visible button spans too — `googleMount`), 44px, 4px
 * radius, 1px border, 18px mark 12px from the left, label centred at 14px/500. Light: GSI's
 * `outline` (white surface, dark text); dark: its `filled_black`, the theme `useGoogleButton` asks for there.
 * The symmetric 42px padding keeps the label centred in the button and clear of the mark; at the 448px
 * phone width the content box is ~316px, far more than the longest label needs, so `nowrap` never clips.
 * The colours are GSI's own values, not theme tokens, for the same reason. */
export const providerButtonSx = (t: Theme) => ({
  position: 'relative',
  height: ENTRY_HEIGHT_PX,
  px: '42px',
  borderRadius: '4px',
  border: '1px solid #dadce0',
  bgcolor: '#ffffff',
  color: '#3c4043',
  fontSize: '0.875rem',
  fontWeight: 500,
  letterSpacing: '0.25px',
  textTransform: 'none',
  whiteSpace: 'nowrap',
  '&:hover': { bgcolor: '#f8faff', borderColor: '#d2e3fc' },
  '& .provider-mark': {
    position: 'absolute',
    left: 12,
    top: '50%',
    transform: 'translateY(-50%)',
    display: 'flex',
    color: '#24292f',
  },
  ...t.applyStyles('dark', {
    '& .provider-mark': { color: '#ffffff' },
    bgcolor: '#202124',
    borderColor: '#5f6368',
    color: '#e8eaed',
    '&:hover': { bgcolor: '#2d2e31', borderColor: '#8e918f' },
  }),
} as const)

/** Every entry page's provider buttons, in the ONE order both `/login` and `/signup` show: Google (`google`, the
 *  page's own GSI mount), then each GitHub/Microsoft button in `REDIRECT_PROVIDERS` order — an
 *  anchor to the API's start door, `?intent=signup` on the sign-up page. `verb` is the label's lead ("Sign in",
 *  "Sign up"); the testid is `<key>-<verb as one word>`. */
export function ProviderButtons({
  google,
  providers,
  verb,
  intent,
}: {
  google: ReactNode
  providers: RedirectProvider[]
  verb: 'Sign in' | 'Sign up'
  intent?: 'signup'
}) {
  const suffix = verb === 'Sign in' ? 'signin' : 'signup'
  const query = intent ? `?intent=${intent}` : ''
  return (
    <>
      {google}
      {REDIRECT_PROVIDERS.filter(p => providers.includes(p.key)).map(({ key, name, Mark }) => (
        <ProviderLink key={key} label={`${verb} with ${name}`} href={`/api/auth/oauth/${key}/start${query}`}
          testId={`${key}-${suffix}`} mark={<Mark />} />
      ))}
    </>
  )
}

/** One provider anchor in `providerButtonSx`. The visible label is also its `aria-label`, so the link's
 *  accessible name is exactly that label whatever the mark beside it carries. */
export function ProviderLink({ label, href, testId, mark, onClick }: {
  label: string
  href: string
  testId: string
  mark: ReactNode
  onClick?: (e: MouseEvent) => void
}) {
  return (
    <Button
      component="a"
      href={href}
      onClick={onClick}
      aria-label={label}
      fullWidth
      disableElevation
      data-testid={testId}
      sx={providerButtonSx}
    >
      <span className="provider-mark">{mark}</span>
      {label}
    </Button>
  )
}

function gsiButtonWidth(mount: HTMLElement) {
  const fit = Math.round(mount.getBoundingClientRect().width) - GSI_CHROME_PX
  return Math.max(GSI_MIN_WIDTH, Math.min(GSI_MAX_WIDTH, fit))
}

/* The provider buttons' group on a wide (sign-up) card, at the fields' width like every entry control. The card
 * caps its content at GSI's widest button (`EntryCard`), so the Google button matches the fields at every
 * viewport. */
export const providerColumnSx = {
  display: 'flex',
  flexDirection: 'column',
  gap: 2,
} as const

/** Mount the GSI button into the returned ref once its async SDK (`<script async defer>`, `index.html`) is
 *  ready. NO DEADLINE: a capped poll left the mount permanently empty on slow networks (reproducible at any
 *  viewport). Wait on the script's `load`; the uncapped 10Hz interval is the backstop (no tag, jsdom). Both
 *  torn down on mount and unmount. `enabled` false (the page shows no Google button) or no client id skips it
 *  entirely. `text` is GSI's own label option (`signup_with` on the sign-up page). GSI titles its iframe
 *  "Sign in with Google Button" whatever `text` says, and that title is the control's accessible name, so the
 *  mount retitles it to the button's own label ("Sign up with Google") and keeps it so if GSI renders again. */
export function useGoogleButton(
  clientId: string | null,
  enabled: boolean,
  onCredential: (credential: string) => void,
  text?: 'signin_with' | 'signup_with',
) {
  const ref = useRef<HTMLDivElement>(null)
  // The latest handler, so the one-time GSI callback never calls a stale closure.
  const handler = useRef(onCredential)
  handler.current = onCredential
  // GSI's `outline` is white in both schemes; on a dark card that is a white box, so dark asks for `filled_black`.
  const theme = useColorMode() === 'dark' ? 'filled_black' : 'outline'
  const title = text === 'signup_with' ? 'Sign up with Google' : 'Sign in with Google'

  useEffect(() => {
    if (!clientId || !enabled) return
    let timer = 0
    let mounted = false
    let retitler: MutationObserver | undefined
    const script = document.querySelector<HTMLScriptElement>('script[src*="accounts.google.com/gsi/"]')
    const tryMount = () => {
      const gsi = window.google?.accounts?.id
      const el = ref.current
      if (mounted || !gsi || !el) return
      mounted = true
      window.clearInterval(timer)
      script?.removeEventListener('load', tryMount)
      gsi.initialize({
        client_id: clientId,
        callback: response => handler.current(response.credential),
      })
      // A scheme change renders again into the same mount: drop the previous button first.
      el.replaceChildren()
      // Watching before GSI renders, so its iframe never keeps GSI's own title.
      const retitle = () => {
        for (const frame of el.querySelectorAll('iframe')) if (frame.title !== title) frame.title = title
      }
      retitler = new MutationObserver(retitle)
      retitler.observe(el, { childList: true, subtree: true, attributes: true, attributeFilter: ['title'] })
      gsi.renderButton(el, {
        theme,
        size: 'large',
        width: gsiButtonWidth(el),
        ...(text ? { text } : {}),
      })
    }
    timer = window.setInterval(tryMount, 100)
    script?.addEventListener('load', tryMount)
    // The SDK may already be loaded before this effect runs.
    tryMount()
    return () => {
      window.clearInterval(timer)
      script?.removeEventListener('load', tryMount)
      retitler?.disconnect()
    }
  }, [clientId, enabled, text, theme, title])

  return ref
}

/** The entry pages' one card: a centred Paper on the theme ground with the wordmark on top. A `form` when
 *  `onSubmit` is given, a plain block otherwise. `wide`: a sign-up step with a form of its own (the profile, the
 *  billing address, the card), whose two-column rows need more than a sign-in's width. */
export function EntryCard({ onSubmit, wide = false, children }: {
  onSubmit?: (e: FormEvent) => void
  wide?: boolean
  children: ReactNode
}) {
  return (
    <Box
      sx={{
        // dvh, not vh: the phone URL bar makes 100vh taller than the visible viewport.
        minHeight: '100dvh',
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        // `p: 2`: a gutter so the full-width card never touches the viewport edges below ~380px.
        p: 2,
        /* The theme's `background.default` (flips with the scheme) instead of a hand-rolled dark gradient
           (AC4(e)); card separation comes from `elevation={3}`. */
        bgcolor: 'background.default',
      }}
    >
      <Paper
        component={onSubmit ? 'form' : 'div'}
        onSubmit={onSubmit}
        // A wide (sign-up) form names every problem inline itself; the browser's bubble would only race it.
        noValidate={wide && onSubmit ? true : undefined}
        elevation={3}
        sx={{
          // `p: 3` / `gap: 2` — reductions from the legacy card's spacing.
          p: 3,
          width: '100%',
          // Wide: content no wider than GSI's widest button (`providerColumnSx`), plus the `p: 3` gutters.
          maxWidth: wide ? (t: Theme) => `calc(${GSI_MAX_WIDTH}px + ${t.spacing(6)})` : 380,
          display: 'flex',
          flexDirection: 'column',
          gap: 2,
        }}
      >
        {/* The sidebar's wordmark (`components/Sidebar.tsx`), larger — one brand treatment, with the rail's
            per-scheme stops so it reads on the card's light and dark grounds alike. */}
        <Box
          data-testid="login-brand"
          sx={(t: Theme) => ({
            display: 'flex',
            justifyContent: 'center',
            ...markVars(t.custom.sidebar.light),
            ...t.applyStyles('dark', markVars(t.custom.sidebar.dark)),
          })}
        >
          <BrandWordmark height={56} />
        </Box>
        {children}
      </Paper>
    </Box>
  )
}

/** The card's footer line — a lead-in and one link-styled action ("New to Jaah? Sign up"). A text button,
 *  not an inline link, so the action keeps the 44px tap floor on a phone. */
export function EntryFooter({ lead, children }: { lead: string; children: ReactNode }) {
  return (
    <Box
      sx={{
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        flexWrap: 'wrap',
        columnGap: 0.25,
        typography: 'body2',
        color: 'text.secondary',
      }}
    >
      <span>{lead}</span>
      {children}
    </Box>
  )
}

/* The footer action's geometry: the entry height, text-weight emphasis, no uppercase. */
export const footerActionSx = {
  minHeight: ENTRY_HEIGHT_PX,
  px: 1,
  textTransform: 'none',
  fontWeight: 600,
} as const

/** A card's primary action: full width, contained, a spinner while `busy`. `aria-hidden` keeps the accessible
 *  name exactly the label ("Sign in"), which the Playwright login helpers match. */
export function EntrySubmit(props: ButtonProps & { busy: boolean }) {
  return <BusyButton variant="contained" fullWidth sx={submitSx} {...props} />
}

/** The one rule between an entry page's provider buttons and its own form, on `/login` and `/signup` alike. */
export function EntryDivider() {
  return <Divider sx={{ typography: 'caption', color: 'text.secondary' }}>or</Divider>
}

/** The page's one heading and its line beneath (none when `children` is empty). */
export function EntryHeading({ title, children }: { title: string; children?: ReactNode }) {
  return (
    <Box sx={{ textAlign: 'center' }}>
      <Typography variant="h6" component="h1">
        {title}
      </Typography>
      {children && (
        <Typography variant="body2" sx={{ color: 'text.secondary', mt: 0.5, overflowWrap: 'anywhere' }}>
          {children}
        </Typography>
      )}
    </Box>
  )
}

/** A card waiting on its first read: a spinner named `label`, or `error` in its place once the read failed. */
export function EntryLoading({ label = 'Loading', error }: { label?: string; error?: string }) {
  return (
    <EntryCard>
      {error ? (
        <Banner severity="error">{error}</Banner>
      ) : (
        <Box sx={{ display: 'flex', justifyContent: 'center', py: 2 }}>
          <CircularProgress size={28} aria-label={label} />
        </Box>
      )}
    </EntryCard>
  )
}

/** The org's mark on an invitation: its initial on a filled rounded square (orgs carry no logo). */
function OrgMark({ name }: { name: string }) {
  const initial = name.trim().charAt(0).toUpperCase() || '?'
  return (
    <Box
      aria-hidden="true"
      data-testid="invitation-org-mark"
      sx={{
        width: 64,
        height: 64,
        borderRadius: 2,
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        bgcolor: 'primary.main',
        color: 'primary.contrastText',
        fontSize: '1.75rem',
        fontWeight: 600,
        lineHeight: 1,
        mx: 'auto',
      }}
    >
      {initial}
    </Box>
  )
}

/** An invitation's heading — who invites, into which org — the way a forge's org invitation reads: the org's
 *  mark, "<inviter> invited you to join", the org's name large, then `children` (the account line). The
 *  invitation review page (`InvitationPage.tsx`) and a new invitee's create-password page (`ResetPassword.tsx`)
 *  both open with it. `inviterName` null: the line names no one. */
export function InvitationHeading({
  orgName,
  inviterName,
  children,
}: {
  orgName: string
  inviterName: string | null
  children?: ReactNode
}) {
  return (
    <Box sx={{ textAlign: 'center', display: 'flex', flexDirection: 'column', gap: 1.5 }}>
      <OrgMark name={orgName} />
      <Box>
        <Typography variant="body2" sx={{ color: 'text.secondary', overflowWrap: 'anywhere' }}>
          {inviterName ? (
            <>
              <Box component="strong" sx={{ color: 'text.primary', fontWeight: 600 }}>{inviterName}</Box>{' '}
              {/* One phrase: a narrow card wraps before it, never inside it ("invited you to / join"). */}
              <Box component="span" sx={{ whiteSpace: 'nowrap' }}>invited you to join</Box>
            </>
          ) : (
            "You're invited to join"
          )}
        </Typography>
        <Typography variant="h5" component="h1" sx={{ fontWeight: 600, overflowWrap: 'anywhere', mt: 0.25 }}>
          {orgName}
        </Typography>
      </Box>
      {children && (
        <Typography variant="body2" sx={{ color: 'text.secondary', overflowWrap: 'anywhere' }}>
          {children}
        </Typography>
      )}
    </Box>
  )
}

/** The sign-up flow's onward links for someone waiting on review: the tutorials as the main action, then the
 *  product's home and its documentation side by side. Each opens its operator-set URL in a new tab and is left
 *  out when unset; with none set, nothing renders. The thank-you page and the under-review screen share it;
 *  `secondary` (the welcome page, whose main action is Sign in) outlines the tutorials too. */
export function SignupLinkButtons({ links, secondary = false }: { links: SignupLinks; secondary?: boolean }) {
  const { tutorialsUrl, homeUrl, docsUrl } = links
  if (!tutorialsUrl && !homeUrl && !docsUrl) return null
  const newTab = { component: 'a', target: '_blank', rel: 'noopener noreferrer' } as const
  return (
    <Box sx={{ display: 'flex', flexDirection: 'column', gap: 1.5 }}>
      {tutorialsUrl && (
        <Button {...newTab} href={tutorialsUrl} variant={secondary ? 'outlined' : 'contained'} fullWidth sx={submitSx}>
          Watch the tutorials
        </Button>
      )}
      {(homeUrl || docsUrl) && (
        <Box sx={{ display: 'flex', gap: 1.5 }}>
          {homeUrl && (
            <Button {...newTab} href={homeUrl} variant="outlined" fullWidth sx={submitSx}>
              Jaah Home
            </Button>
          )}
          {docsUrl && (
            <Button {...newTab} href={docsUrl} variant="outlined" fullWidth sx={submitSx}>
              Documentation
            </Button>
          )}
        </Box>
      )}
    </Box>
  )
}
