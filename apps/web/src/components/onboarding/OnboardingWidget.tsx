/* THE ONBOARDING WIDGET — the one floating checklist, on every authenticated page.
 * Mounted once in `App.tsx`'s `AuthLayout` (beside the Help panel's provider) so it survives navigation: a
 * step's link takes the user to the screen that does it, and the checklist is still there when they arrive.
 *
 * WHICH GUIDE — one at most, never both:
 *  · a viewer the company-setup guide is for (`GET /api/org/setup` says `visible`: a tenant's Org
 *    Owner/Admin) sees ONLY "Set up your company", until it is done — and then nothing: that
 *    viewer never gets "Get started" (operator decision: it is no longer needed once the company is set up,
 *    and not for the same user);
 *  · everyone else sees "Get started", until it is done.
 * Nothing is drawn until the setup verdict is in, so Get started never flashes first for an owner.
 * Each guide's own rules (steps, completion, hide pref) live in its hook (`companySetupGuide.tsx`,
 * `getStartedGuide.ts`); this file owns the choice and the frame.
 *
 * ALWAYS REACHABLE UNTIL DONE: the ✕ HIDES the guide (stored per user), it does not end it. The
 * sidebar's Setup button (`SetupNavButton.tsx`) counts the same guide and shows it again (`openOnboarding`);
 * the guide ends only when every step is done — with a full-screen celebration (`SetupCelebration.tsx`) when
 * this load watched it finish. This widget IS where setup is done: the Setup button opens it and leaves the
 * user on their page.
 *
 * THE FRAME, two shapes by width (`PHONE_QUERY`, the console's 480px):
 *  · DESKTOP: DOCKED, never floating — an in-flow column at the layout's right edge, so it never covers page
 *    content (a floating window sat over the Models table at 1500×950 until dismissed). The page
 *    narrows by the column instead. The Help panel's chrome (`ui/windowChrome.tsx`): the same header
 *    wash, title + caption ("5/9", `progressReading`), Minimize/Restore and a ✕ that dismisses for good. As wide
 *    as its widest row, as tall as its rows — a card at the top of the column, never a full-height strip with an
 *    empty tail; scrolls only when the viewport is too short. Minimized it is a narrow
 *    rail (ring over "N/M", Restore, ✕), and under `CRAMPED_QUERY` it starts that way unless the user left it
 *    open; while an editor is split beside its list, on Audit and Settings and while acting as an org it is the
 *    rail whatever was stored, until the user restores it. Every step done, the ring becomes a ✓ and the caption "Done"
 *    (`ProgressGlyph`), so it never reads as a spinner that will not stop; the ✕ puts it away.
 *  · PHONE: no dragging. A collapsed BAR pinned just above the Sidebar's 60px bottom bar — title and "N/M";
 *    a tap opens the checklist above it (and closes it again), ✕ dismisses. The page gets the bar's height as
 *    extra bottom padding (`ONBOARDING_BAR_VAR`), so the bar never covers its last row; a page
 *    with a form action bar DRAWN (Save/Go in that same strip) gets no bar at all while the form is up. Minimize
 *    FOLDS IT INTO THE BOTTOM NAVIGATION: nothing floats over the page, the bar's padding goes, and the
 *    Sidebar's More slot carries the guide's ring (`SetupNavBadge`) while its sheet heads with the Setup row
 *    that brings the bar back. A floating tab at the right edge covered the end of whatever card row was
 *    under it mid-scroll. Folded is remembered like the desktop rail
 *    (`ONBOARDING_MINIMIZED_KEY`). The OPERATOR (`admin.is_operator`: every page they see is an operator
 *    page — Customers, Clients, the desk dialogs) starts folded with no stored choice: the bar cost ~110px
 *    of each of those pages for a guide that is not their work. A tenant still starts on the bar — except
 *    on Settings (`SETTINGS_PATH`), where the bar over the bottom bar hid the last card's rows: there it is
 *    folded, whatever was stored, until the user restores it on that mount; so on Billing
 *    (`BILLING_PAGE`, its Subscription page included: the bar sat over the Limits table); and so while acting as an
 *    org.
 * PROGRESS STAYS LIVE: both guides are re-asked on every navigation, and a step under way is polled.
 */
import { useEffect, useId, useRef, useState, type JSX } from 'react'
import { useLocation } from 'react-router-dom'
import { useQueryClient } from '@tanstack/react-query'
import Box from '@mui/material/Box'
import ButtonBase from '@mui/material/ButtonBase'
import Paper from '@mui/material/Paper'
import Portal from '@mui/material/Portal'
import Typography from '@mui/material/Typography'
import useMediaQuery from '@mui/material/useMediaQuery'
import { useAuth } from '../../auth'
import { useActingAs } from '../../lib/actAs'
import { BILLING_PAGE, COMPANY_SETUP_QUERY } from '../../lib/companySetup'
import { createExternalStore, useExternalStore } from '../../lib/externalStore'
import { readJson, writeJson } from '../../lib/safeStorage'
import { DoneBadge } from '../../ui/DoneBadge'
import { EXPAND_LESS_PATH, EXPAND_MORE_PATH, Icon } from '../../ui/icons'
import { BOTTOM_NAV_HEIGHT, PHONE_QUERY } from '../../ui/listStyles'
import { ProgressRing, progressPct } from '../../ui/ProgressRing'
import { useFormBarShown } from '../../ui/useFormBarShown'
import { useOpenDetailPath } from '../ListDetail'
import SetupChecklist, { checklistProgress, progressReading, useChecklistDismiss } from '../setupChecklist/SetupChecklist'
import { MARGIN } from '../../ui/FloatingWindow'
import {
  CLOSE_PATH,
  HEADER_GRADIENT,
  HeaderButton,
  MINIMIZE_PATH,
  RESTORE_PATH,
  WINDOW_HEADER_SX,
} from '../../ui/windowChrome'
import { useCompanySetupGuide } from './companySetupGuide'
import { OWN_TASKS_QUERY, useGetStartedGuide } from './getStartedGuide'
import type { Guide } from './guide'
import SetupCelebration from './SetupCelebration'

/** Whether the dock (desktop rail, phone bar folded into the navigation) was left minimized: a per-browser
 *  convenience. */
export const ONBOARDING_MINIMIZED_KEY = 'onboarding-widget:minimized'
/** Too narrow for the open dock to leave the page its room: it starts as the rail there. 1599px, not a laptop's
 *  1440: there the open dock (~220px) cost the operator's wide tables a column (Products › Instance types lost
 *  Network). */
export const CRAMPED_QUERY = '(max-width: 1599px)'
/** The open dock's widest: the checklist's longest row, never a third of a laptop. */
const DOCK_MAX_WIDTH = 360
/** The bottom-right corner the Help launcher floats in, kept clear of the dock (`support/HelpLauncher.tsx`). */
const HELP_LAUNCHER_CLEARANCE = 72
/** The rail: the header's buttons, stacked. */
const RAIL_WIDTH = 52

/** The phone bar's height: one line at the 44px tap floor, since it rests over page content. The Sidebar's
 *  bottom bar (`BOTTOM_NAV_HEIGHT`) is under it. */
const BAR_HEIGHT = 44
/* Under the Sidebar's bottom bar and its sheet (both 40), so the navigation always wins; above page content. */
const BAR_Z_INDEX = 39

/** The phone bar's height while it shows, as a CSS variable on the document root. The layout's main column
 *  (`App.tsx`'s APP_MAIN_SX) adds it to the 60px it already reserves for the navigation, so a long page scrolls
 *  fully clear of the bar. A variable, not a selector override: no `:has()` to go unsupported. */
export const ONBOARDING_BAR_VAR = '--onboarding-bar-height'

/** Settings' pages: the phone bar folds into the navigation there, as on Billing's, and the desktop dock starts as
 *  the rail. A list there drops slots by its own width (`ui/feed/fit.ts`), which holds at 1280 (~400px
 *  beside the category column and the open dock), but at 1024 the open dock leaves it ~185px: no name at all. */
const SETTINGS_PATH = '/settings'

/** Audit's pages: the desktop dock starts as the rail there — its toolbar runs the page's full width, and the open
 *  dock took the date filters' row edge. */
const AUDIT_PATH = '/audit'

/** A one-shot "open the checklist" for a phone bar that mounts after the Setup button asked (a hidden guide). */
const ONBOARDING_EXPAND_ONCE_KEY = 'onboarding-widget:expand-once'

/** The window event the sidebar's Setup button raises to bring the guide up open: restored from the rail
 *  (desktop), unfolded from the navigation with its checklist open (phone). */
const OPEN_EVENT = 'onboarding:open'

/** Bring the widget up OPEN — the Setup button's half of "show it again" (the guide's `onShow` clears the
 *  stored hide). The minimized choice is cleared first, so a frame that mounts now starts open too; on a
 *  phone the checklist sheet opens with it. */
export function openOnboarding(): void {
  writeJson('local', ONBOARDING_MINIMIZED_KEY, false)
  /* For a phone bar not mounted yet (the guide was hidden): it reads this once, on mount. */
  writeJson('session', ONBOARDING_EXPAND_ONCE_KEY, true)
  window.dispatchEvent(new Event(OPEN_EVENT))
}

/** Runs `open` whenever `openOnboarding` is called. */
function useOpenRequest(open: () => void): void {
  const latest = useRef(open)
  latest.current = open
  useEffect(() => {
    const onOpen = (): void => {
      /* A mounted frame takes the request itself: the one-shot is for a frame that is not here yet. */
      writeJson('session', ONBOARDING_EXPAND_ONCE_KEY, false)
      latest.current()
    }
    window.addEventListener(OPEN_EVENT, onOpen)
    return () => window.removeEventListener(OPEN_EVENT, onOpen)
  }, [])
}

/* Whether the phone bar is folded into the navigation right now: the Sidebar's More slot draws the guide's ring
   then (`SetupNavBadge`). A module store, since the bar and the slot share no parent below the shell. */
const phoneFolded = createExternalStore(false)

/** Is the phone bar folded into the bottom navigation? */
export function usePhoneGuideFolded(): boolean {
  return useExternalStore(phoneFolded)
}

/** Is every step of `guide` done? */
export function isGuideFinished(guide: Guide | null): boolean {
  if (guide === null) return false
  const { done, total } = checklistProgress(guide.steps)
  return total > 0 && done === total
}

/** Picks the one guide to draw, or nothing. Exported for its tests: this is the rule "never both". */
export function useOnboardingGuide(): Guide | null {
  const setup = useCompanySetupGuide()
  /* Get started is asked only once the setup verdict says the viewer is not its audience. */
  const getStarted = useGetStartedGuide(!setup.pending && !setup.forViewer)
  if (setup.pending) return null
  if (setup.forViewer) return setup.guide
  return getStarted.pending ? null : getStarted.guide
}

/**
 * RE-ASK ON EVERY NAVIGATION. The widget mounts once, in the shell, so its queries' mount refetch fires once
 * per page LOAD — while the user's progress is made on the very pages its links open (a node added on
 * /nodes, a task filed on /tasks/new). A route change is the moment a step may have finished, so both
 * guides' questions are asked again then. Active queries only (a hidden guide's are active too: the Setup button counts it); a disabled query stays
 * silent. Not on the first render — the mount already asked.
 */
function useRefetchOnNavigation(): void {
  const client = useQueryClient()
  const { pathname } = useLocation()
  const first = useRef(true)
  useEffect(() => {
    if (first.current) {
      first.current = false
      return
    }
    void client.refetchQueries({ queryKey: [COMPANY_SETUP_QUERY], type: 'active' })
    void client.refetchQueries({ queryKey: [OWN_TASKS_QUERY], type: 'active' })
  }, [client, pathname])
}

export default function OnboardingWidget(): JSX.Element | null {
  useRefetchOnNavigation()
  const guide = useOnboardingGuide()
  const finished = isGuideFinished(guide)
  /* The guide only reads finished on a load that watched it unfinished (an arrived-finished one is null), so
     a flip to finished is the moment setup was completed: celebrate once. */
  const [celebrate, setCelebrate] = useState(false)
  const celebrated = useRef(false)
  useEffect(() => {
    if (finished && !celebrated.current) {
      celebrated.current = true
      setCelebrate(true)
    }
  }, [finished])
  return (
    <>
      {celebrate && <SetupCelebration onDone={() => setCelebrate(false)} />}
      {/* Keyed: a different guide starts with a fresh ✕ state and a closed bar. */}
      {guide !== null && !guide.hidden && <GuideFrame key={guide.testIdPrefix} guide={guide} />}
    </>
  )
}

/** One guide in its frame. Exported for the guide suites, which draw a guide without the choice above. */
export function GuideFrame({ guide }: { guide: Guide }): JSX.Element {
  const phone = useMediaQuery(PHONE_QUERY)
  const dismiss = useChecklistDismiss(guide.onDismiss)
  const body = (
    <SetupChecklist
      testIdPrefix={guide.testIdPrefix}
      steps={guide.steps}
      highlightNext={guide.highlightNext}
      labelLinks={guide.labelLinks}
      dismissFailed={dismiss.failed}
    />
  )
  return phone ? (
    <PhoneBar guide={guide} dismiss={dismiss}>{body}</PhoneBar>
  ) : (
    <DesktopWindow guide={guide} dismiss={dismiss}>{body}</DesktopWindow>
  )
}

type Dismiss = ReturnType<typeof useChecklistDismiss>

/** The guide's progress glyph: the ring while steps remain, a ✓ once every one is done. */
function ProgressGlyph({ guide, size }: { guide: Guide; size: number }): JSX.Element {
  const progress = checklistProgress(guide.steps)
  if (progress.total > 0 && progress.done === progress.total) {
    return <DoneBadge size={size} testId="onboarding-widget-done" />
  }
  return <ProgressRing pct={progressPct(progress)} variant="gradient" size={size} testId="onboarding-widget-ring" />
}

/** Title over its "N/M" caption ("Done" once every step is), or beside it on the one-line phone bar
 *  (`inline`). Never truncated: in a content-sized window the title is part of what sets the width. */
function TitleBlock({ guide, inline }: { guide: Guide; inline?: boolean }): JSX.Element {
  return (
    <Box sx={{ flex: 1, minWidth: 0, ...(inline && { display: 'flex', alignItems: 'baseline', gap: 1 }) }}>
      <Typography
        component="h2"
        variant={inline ? 'subtitle2' : 'subtitle1'}
        data-testid={`${guide.testIdPrefix}-title`}
        sx={{ fontWeight: 700, lineHeight: 1.2, whiteSpace: 'nowrap' }}
      >
        {guide.title}
      </Typography>
      <Typography
        variant="caption"
        component="div"
        data-testid={`${guide.testIdPrefix}-count`}
        sx={{ color: 'text.secondary', lineHeight: 1.3, whiteSpace: 'nowrap' }}
      >
        {progressReading(checklistProgress(guide.steps))}
      </Typography>
    </Box>
  )
}

function DismissButton({ guide, dismiss }: { guide: Guide; dismiss: Dismiss }): JSX.Element {
  return (
    <HeaderButton
      label={guide.dismissLabel}
      tip="Hide — Setup in the sidebar brings it back"
      path={CLOSE_PATH}
      testId={`${guide.testIdPrefix}-dismiss`}
      disabled={dismiss.dismissing}
      onClick={dismiss.dismiss}
    />
  )
}

function DesktopWindow({ guide, dismiss, children }: { guide: Guide; dismiss: Dismiss; children: JSX.Element }): JSX.Element {
  const cramped = useMediaQuery(CRAMPED_QUERY, { noSsr: true })
  /* The user's own choice wins; with none, a narrow viewport starts on the rail so the page keeps its width —
     except a guide that asks to start open (`Guide.startOpen`: the company setup, a new org's first screen). */
  const [storedMinimized, setMinimized] = useState(() => {
    const stored = readJson('local', ONBOARDING_MINIMIZED_KEY)
    return typeof stored === 'boolean' ? stored : cramped && guide.startOpen !== true
  })
  /* Acting as an org, on the rail whatever was stored, until the user restores it here — as the phone bar folds:
     opened, the dock sat over the editor's right column on every page for a guide that is not the operator's
     work. */
  const acting = useActingAs() !== null
  /* So while an editor is split beside its list (`ListDetail.tsx`: a task beside the board) — and afresh each time
     one opens: the open dock squeezed the board until nearly every title wrapped at 1280px, and a dock restored
     (or opened by Setup) over the bare list would otherwise stay open into every task. */
  const splitOpen = useOpenDetailPath() !== null
  /* And on Audit and Settings, until the user restores it there (`AUDIT_PATH`, `SETTINGS_PATH`). */
  const { pathname } = useLocation()
  const onRailPage = pathname.startsWith(AUDIT_PATH) || pathname.startsWith(SETTINGS_PATH)
  const railHere = acting || splitOpen || onRailPage
  const [restoredHere, setRestoredHere] = useState(false)
  const [wasSplit, setWasSplit] = useState(splitOpen)
  if (splitOpen !== wasSplit) {
    setWasSplit(splitOpen)
    if (splitOpen) setRestoredHere(false)
  }
  const [wasOnRailPage, setWasOnRailPage] = useState(onRailPage)
  if (onRailPage !== wasOnRailPage) {
    setWasOnRailPage(onRailPage)
    if (onRailPage) setRestoredHere(false)
  }
  const minimized = storedMinimized || (railHere && !restoredHere)
  const toggleMinimized = (next = !minimized): void => {
    setMinimized(next)
    setRestoredHere(!next)
    writeJson('local', ONBOARDING_MINIMIZED_KEY, next)
  }
  const restore = (): void => {
    setMinimized(false)
    setRestoredHere(true)
  }
  /* A refused ✕ is said in the body ("Could not save that"): a minimized dock would swallow it, and the
     guide would look dismissed until the next reload brought it back. */
  useEffect(() => {
    if (dismiss.failed) {
      setMinimized(false)
      setRestoredHere(true)
    }
  }, [dismiss.failed])
  useOpenRequest(restore)
  const ring = <ProgressGlyph guide={guide} size={30} />

  return (
    /* IN FLOW, at the layout row's right end (`order`): `App.tsx` mounts this before the Sidebar, and the row
       is the page's width, so the column takes its width from `<main>` rather than covering it. */
    <Paper
      component="aside"
      square
      elevation={0}
      role="complementary"
      aria-label={guide.title}
      data-testid="onboarding-widget"
      data-docked="true"
      data-minimized={minimized || undefined}
      onKeyDown={e => {
        if (e.key === 'Escape' && !minimized) {
          e.stopPropagation()
          toggleMinimized(true)
        }
      }}
      sx={{
        order: 2,
        flex: '0 0 auto',
        width: minimized ? RAIL_WIDTH : 'max-content',
        maxWidth: DOCK_MAX_WIDTH,
        /* As tall as its rows, at the column's top: a full-height strip left most of it empty. Never
           down to the Help launcher's corner (`support/HelpLauncher.tsx`); a viewport too short scrolls the body. */
        alignSelf: 'flex-start',
        maxHeight: `calc(100% - ${HELP_LAUNCHER_CLEARANCE}px)`,
        display: 'flex',
        flexDirection: 'column',
        borderLeft: 1,
        borderBottom: 1,
        borderColor: 'divider',
        borderBottomLeftRadius: 8,
        overflow: 'hidden',
      }}
    >
      {minimized ? (
        <Box
          data-testid="onboarding-widget-header"
          sx={{ display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 1, py: 1.25, backgroundImage: HEADER_GRADIENT }}
        >
          <Box title={guide.title} sx={{ display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 0.25 }}>
            {ring}
            {/* The rail's reading of the ring: without it a part-drawn gradient arc reads as a spinner. */}
            <Typography variant="caption" data-testid={`${guide.testIdPrefix}-rail-count`} sx={{ lineHeight: 1, fontWeight: 600, color: 'text.secondary' }}>
              {progressReading(checklistProgress(guide.steps))}
            </Typography>
          </Box>
          <HeaderButton label="Restore" path={RESTORE_PATH} testId="onboarding-widget-minimize" onClick={() => toggleMinimized(false)} />
          <DismissButton guide={guide} dismiss={dismiss} />
        </Box>
      ) : (
        <Box
          data-testid="onboarding-widget-header"
          onDoubleClick={e => {
            if (!(e.target as HTMLElement).closest('button')) toggleMinimized()
          }}
          sx={{ ...WINDOW_HEADER_SX, gap: 1.25 }}
        >
          {ring}
          <TitleBlock guide={guide} />
          {/* Clear of the title, so the buttons never crowd it at the narrowest content width. */}
          <Box sx={{ display: 'flex', ml: 1.5 }}>
            <HeaderButton label="Minimize" path={MINIMIZE_PATH} testId="onboarding-widget-minimize" onClick={() => toggleMinimized(true)} />
            <DismissButton guide={guide} dismiss={dismiss} />
          </Box>
        </Box>
      )}
      {/* Hidden, not unmounted, while minimized: an open plan picker keeps its state. Scrolls only when the
          viewport is shorter than the list. */}
      <Box
        data-testid="onboarding-widget-body"
        sx={{ display: minimized ? 'none' : 'block', flex: '0 1 auto', minHeight: 0, overflowY: 'auto', px: 1.75, pt: 1.5, pb: 1.25 }}
      >
        {children}
      </Box>
    </Paper>
  )
}

function PhoneBar({ guide, dismiss, children }: { guide: Guide; dismiss: Dismiss; children: JSX.Element }): JSX.Element | null {
  /* Closed, unless the Setup button asked for it open before this bar mounted (`openOnboarding`). */
  const [expanded, setExpanded] = useState(() => {
    const once = readJson('session', ONBOARDING_EXPAND_ONCE_KEY) === true
    writeJson('session', ONBOARDING_EXPAND_ONCE_KEY, false)
    return once
  })
  /* The user's own choice wins; with none, a tenant starts on the bar and the operator folded. */
  const operator = useAuth().admin?.is_operator === true
  const [minimized, setMinimized] = useState(() => {
    const stored = readJson('local', ONBOARDING_MINIMIZED_KEY)
    return typeof stored === 'boolean' ? stored : operator
  })
  /* `admin` may land after mount (the bar drew on a still-loading session): the operator's default then, still
     only while no choice is stored. */
  useEffect(() => {
    if (operator && typeof readJson('local', ONBOARDING_MINIMIZED_KEY) !== 'boolean') setMinimized(true)
  }, [operator])
  const { pathname } = useLocation()
  /* On Settings and while acting as an org folded, whatever was stored, until the user restores it here (the
     Setup button). Billing folds as Settings does. Acting, the act-as bar already holds the top of the page and the bar sat
     over the board's rows for an org's guide that is not the operator's work. */
  const acting = useActingAs() !== null
  const [restoredHere, setRestoredHere] = useState(false)
  const foldedHere = acting || pathname.startsWith(SETTINGS_PATH) || pathname.startsWith(BILLING_PAGE)
  const folded = minimized || (foldedHere && !restoredHere)
  const toggleMinimized = (next: boolean): void => {
    setMinimized(next)
    setRestoredHere(!next)
    setExpanded(false)
    writeJson('local', ONBOARDING_MINIMIZED_KEY, next)
  }
  const panelId = useId()
  /* A step's link navigates: fold the sheet so the destination is what the user sees. Not on mount, which
     must keep a checklist the Setup button asked open. */
  const shownPath = useRef(pathname)
  useEffect(() => {
    if (shownPath.current === pathname) return
    shownPath.current = pathname
    setExpanded(false)
  }, [pathname])
  /* A refused ✕ is said on the bar's checklist; folded would swallow it (as the desktop rail). */
  useEffect(() => {
    if (dismiss.failed) {
      setMinimized(false)
      setRestoredHere(true)
    }
  }, [dismiss.failed])
  /* The Setup button asks for the bar with its checklist open. */
  useOpenRequest(() => {
    setMinimized(false)
    setRestoredHere(true)
    setExpanded(true)
  })
  /* THE BAR STANDS DOWN FOR A FORM. A page's action bar owns the same strip above the navigation
     (`TaskDetailPage` pins `task-form-actions` fixed at bottom: 60px; every `FormActionBar` is sticky at the
     bottom), and its Save/Go must stay reachable — on /tasks/new above all, where the guide sends people. Hidden
     rather than lifted: stacked over a form bar the two would eat a third of a phone's height. Keyed on the
     marker every `FormActionBar` carries, so any future form gets this for free — but only a bar actually drawn
     (`useFormBarShown`). */
  const formBarShown = useFormBarShown()
  /* Reserve the frame's height in the page while it is drawn — the bar's, or with the checklist open the whole
     sheet's, so no list row ends up under it out of reach. Folded, the navigation carries it; stood
     down for a form, nothing is drawn; in both nothing is reserved. */
  const paperRef = useRef<HTMLElement | null>(null)
  useEffect(() => {
    phoneFolded.set(folded)
    if (folded || formBarShown) return undefined
    const root = document.documentElement.style
    /* +1: the Paper's top border sits above the bar's own height. Never less than that (unlaid-out, jsdom). */
    const reserve = () => {
      const height = Math.ceil(paperRef.current?.getBoundingClientRect().height ?? 0)
      root.setProperty(ONBOARDING_BAR_VAR, `${Math.max(BAR_HEIGHT + 1, height)}px`)
    }
    reserve()
    const observer = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(reserve)
    if (observer && paperRef.current) observer.observe(paperRef.current)
    return () => {
      observer?.disconnect()
      root.removeProperty(ONBOARDING_BAR_VAR)
    }
  }, [folded, expanded, formBarShown])
  useEffect(() => () => phoneFolded.set(false), [])

  return folded ? null : (
    <Portal>
      <Paper
        ref={paperRef}
        component="aside"
        aria-label={guide.title}
        data-testid="onboarding-widget"
        data-phone="true"
        data-expanded={expanded || undefined}
        data-stood-down={formBarShown || undefined}
        elevation={12}
        onKeyDown={e => {
          if (e.key === 'Escape' && expanded) {
            e.stopPropagation()
            setExpanded(false)
          }
        }}
        sx={{
          position: 'fixed',
          left: 0,
          right: 0,
          bottom: BOTTOM_NAV_HEIGHT,
          zIndex: BAR_Z_INDEX,
          display: 'flex',
          flexDirection: 'column',
          borderTopLeftRadius: 12,
          borderTopRightRadius: 12,
          borderBottomLeftRadius: 0,
          borderBottomRightRadius: 0,
          borderTop: 1,
          borderColor: 'divider',
          overflow: 'hidden',
          /* Hidden, not unmounted, for a form (`formBarShown`): the open sheet keeps its state. */
          ...(formBarShown && { display: 'none' }),
        }}
      >
        {/* The checklist opens ABOVE the bar, so the bar — and the ✕ on it — stays where the thumb found it. */}
        <Box
          id={panelId}
          data-testid="onboarding-widget-body"
          hidden={!expanded}
          sx={{
            /* Half the screen at most: the page keeps the other half above the sheet, its rows still in reach. */
            maxHeight: `min(50dvh, calc(100dvh - ${BOTTOM_NAV_HEIGHT + BAR_HEIGHT + 2 * MARGIN}px))`,
            overflowY: 'auto',
            px: 2,
            pt: 1.75,
            pb: 1.25,
            borderBottom: 1,
            borderColor: 'divider',
          }}
        >
          {children}
        </Box>
        <Box
          sx={{
            height: BAR_HEIGHT,
            display: 'flex',
            alignItems: 'center',
            pr: 0.5,
            backgroundImage: HEADER_GRADIENT,
          }}
        >
          <ButtonBase
            data-testid="onboarding-widget-bar"
            aria-expanded={expanded}
            aria-controls={panelId}
            onClick={() => setExpanded(e => !e)}
            sx={{ flex: 1, minWidth: 0, height: '100%', justifyContent: 'flex-start', gap: 1.25, pl: 1.75, pr: 1, textAlign: 'left' }}
          >
            <ProgressGlyph guide={guide} size={22} />
            <TitleBlock guide={guide} inline />
            <Icon viewBox="0 0 24 24" size={22} sx={{ fill: 'currentColor', color: 'text.secondary' }}>
              <path d={expanded ? EXPAND_MORE_PATH : EXPAND_LESS_PATH} />
            </Icon>
          </ButtonBase>
          <HeaderButton label="Minimize" path={MINIMIZE_PATH} testId="onboarding-widget-minimize" onClick={() => toggleMinimized(true)} />
          <DismissButton guide={guide} dismiss={dismiss} />
        </Box>
      </Paper>
    </Portal>
  )
}
