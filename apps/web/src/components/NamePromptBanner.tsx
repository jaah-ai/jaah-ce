import { useEffect, useRef, useState, type JSX } from 'react'
import { Link as RouterLink, useLocation } from 'react-router-dom'
import Box from '@mui/material/Box'
import Link from '@mui/material/Link'
import useMediaQuery from '@mui/material/useMediaQuery'
import { useAuth } from '../auth'
import { useActingAs } from '../lib/actAs'
import { isEnabled } from '../lib/editionFeatures'
import { ADD_NAME, isNameless } from '../lib/memberLabel'
import { useOrgPending } from '../lib/orgLifecycle'
import { planNotice } from '../lib/orgPlan'
import {
  isNamePromptDismissedLocally,
  NAME_PROMPT_DISMISSED_STEP,
  readOnboardingStepsDone,
  rememberNamePromptDismissed,
  withOnboardingStepDone,
} from '../lib/onboarding'
import { PHONE_QUERY } from '../ui/listStyles'
import { Banner } from '../ui/Banner'

/* The shell-wide name prompt, beside `PlanEndedBanner` in `App.tsx::AuthLayout`. An account with neither a first nor
   a last name (an operator seeded without one never met sign-up's name fields) shows its email wherever the console
   names a person. In flow, never a dialog, so it blocks nothing. It asks ONCE: its ✕ records
   `NAME_PROMPT_DISMISSED_STEP` server-side (`lib/onboarding.ts`, cross-device); a saved name ends it too. Not on
   `/profile`, where the name fields already are, nor on `/no-access`, whose card folds the ask in beside its own
   profile button (`pages/NoAccess.tsx`). Folded at every width, not only the phone: one fix asked twice
   is redundant on any screen, and a width gate would only add a second behaviour to keep true.
   CLOSED STAYS CLOSED. The ✕ used to wait on the server: `updatePreferences` rolls a refused write
   back, so the prompt returned, and the record lives in the shared `onboarding_steps_done` list that any writer
   holding an older `preferences` copy (another tab's Settings save, a concurrent write) replaces wholesale. Now the
   ✕ hides at once (component state), remembers per user in this browser (`lib/onboarding.ts`), and records
   server-side for other devices; a browser that remembers a close the server lost re-sends it once per load.
   NOT ON A PHONE'S TASK PAGE. There a task's own reason banner (`pages/taskDetail/TaskDetailBanners.tsx`)
   is the page's point and a phone screen is short: even the one-line fold pushes it down. The ask waits for the
   next page; the board, `/tasks/new` and every other page still carry it.
   NOT WHILE ACTING AS AN ORG. The act-as bar already holds the top of every page, and the ask is about
   the operator's own account, not the org they are in: it waits until they leave act-as.
   ONE SHELL NOTICE AT A TIME. A pending org's or a plan's notice above it is about the org's state; the
   ask waits until neither shows, so no page opens on two stacked banners.
   NOT IN THE COMMUNITY EDITION: its one user is the only one who sees anything, so there is no "others" to ask for. */
const NAME_PROMPT_CONTAINER = 'name-prompt'
/* Narrow (a list column beside an open record at 1024px, a phone), the sentence wrapped to two cramped lines,
   but the link alone gave no reason to act: there a shorter reason takes its place. */
const NARROW = `@container ${NAME_PROMPT_CONTAINER} (max-width: 519px)`
const WIDE_ONLY_SX = { [NARROW]: { display: 'none' } }
const NARROW_ONLY_SX = { display: 'none', [NARROW]: { display: 'inline' } }
/* FOLDED INTO THE SIDEBAR in that narrow column while the expanded rail's footer reads "No name set" beside its
   account menu (My Profile): a full-height row above a list column only repeated it. A folded rail or a
   phone shows no name there, so the banner stays. Off the rail's own `data-collapsed`, not a copy of its state. */
const FOLDS_INTO_SIDEBAR_SX = {
  [NARROW]: { 'body:has([data-testid="sidebar"][data-collapsed="false"]) &': { display: 'none' } },
}
/* One task's page — `/tasks/:id` — not the board nor the create surfaces (`/tasks/new`, `/tasks/new-chat`). */
const TASK_PAGE = /^\/tasks\/(?!new(?:-chat)?\/?$)[^/]+/

export function NamePromptBanner(): JSX.Element | null {
  const { admin, updatePreferences } = useAuth()
  const { pathname } = useLocation()
  const phone = useMediaQuery(PHONE_QUERY)
  const acting = useActingAs()
  const pending = useOrgPending()
  /* The user id closed in this mount: hides at once, before (and regardless of) the server's answer. */
  const [closedFor, setClosedFor] = useState<string | null>(null)
  const healSent = useRef(false)
  const id = admin?.id ?? null
  const recorded = readOnboardingStepsDone(admin?.preferences).has(NAME_PROMPT_DISMISSED_STEP)
  const closedHere = id !== null && (closedFor === id || isNamePromptDismissedLocally(id))
  /* Remembered here but missing server-side (refused or clobbered): re-record once per load, for other devices. */
  useEffect(() => {
    if (!admin || !isNameless(admin) || recorded || !closedHere || healSent.current) return
    healSent.current = true
    void updatePreferences(prefs => withOnboardingStepDone(prefs, NAME_PROMPT_DISMISSED_STEP))
  }, [admin, recorded, closedHere, updatePreferences])
  if (!isEnabled('users_invites') || !admin || !isNameless(admin) || pathname === '/profile' || pathname === '/no-access') return null
  if (recorded || closedHere || acting) return null
  if (pending || (isEnabled('billing') && planNotice(admin.org, new Date()))) return null
  if (phone && TASK_PAGE.test(pathname)) return null
  const close = (): void => {
    setClosedFor(admin.id)
    rememberNamePromptDismissed(admin.id)
    healSent.current = true
    void updatePreferences(prefs => withOnboardingStepDone(prefs, NAME_PROMPT_DISMISSED_STEP))
  }
  return (
    <Box
      data-testid="name-prompt-banner"
      sx={{
        containerType: 'inline-size',
        containerName: NAME_PROMPT_CONTAINER,
        '& .MuiAlert-message': { whiteSpace: 'normal', overflowWrap: 'anywhere' },
      }}
    >
      {/* The margin rides the inner box: a container query styles descendants only, so the fold hides it too. */}
      <Box data-testid="name-prompt-body" sx={[{ mb: 1.5 }, phone ? null : FOLDS_INTO_SIDEBAR_SX]}>
        <Banner
          severity="info"
          onDismiss={close}
        >
          <Box component="span" data-testid="name-prompt-why" sx={WIDE_ONLY_SX}>Others see your email, not a name. </Box>
          <Box component="span" data-testid="name-prompt-why-short" sx={NARROW_ONLY_SX}>Others see your email. </Box>
          <Link component={RouterLink} to="/profile" data-testid="name-prompt-link">{ADD_NAME}</Link>
        </Banner>
      </Box>
    </Box>
  )
}

export default NamePromptBanner
