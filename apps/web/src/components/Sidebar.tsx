import { useState, type MouseEvent, type ReactNode } from 'react'
import useMediaQuery from '@mui/material/useMediaQuery'
import Popover from '@mui/material/Popover'
import { Link, useLocation } from 'react-router-dom'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import ButtonBase from '@mui/material/ButtonBase'
import Divider from '@mui/material/Divider'
import IconButton from '@mui/material/IconButton'
import List from '@mui/material/List'
import ListItemButton from '@mui/material/ListItemButton'
import Menu from '@mui/material/Menu'
import MenuItem from '@mui/material/MenuItem'
import Paper from '@mui/material/Paper'
import Toolbar from '@mui/material/Toolbar'
import Typography from '@mui/material/Typography'
import type { Theme } from '@mui/material/styles'
import { useAuth, useCan } from '../auth'
import { CLOUD } from '../lib/cloudSurfaces'
import { CLOUD_SIGNUP_URL, SITE_URL } from '../lib/edition'
import { isEnabled } from '../lib/editionFeatures'
import { useCeLimits } from '../lib/communityEdition'
import { isOrgAdminSeat } from '../lib/orgAdmin'
import { planSwitchOn } from '../lib/planSwitches'
import { managesAccess } from '../lib/accessRequests'
import { ADD_NAME, isNameless, memberName } from '../lib/memberLabel'
import { SCOPE_PARAM } from '../lib/taskScope'
import { useAutomationTabs } from '../lib/automationTabs'
import { isInDirectory, useDirectoryTabs } from '../lib/directoryTabs'
import { useNotFoundShown } from '../lib/notFound'
import { useTaskTree } from '../hooks/useTaskTree'
import { usePendingOrgCount, usePendingRequestCount, usePendingTrustRequestCount } from '../hooks/usePendingCount'
import { useHealth } from '../hooks/useHealth'
import { useVersionCheck } from '../hooks/useVersionCheck'
import type { PermissionKey } from '../types'
import { ABOVE_PHONE, BOTTOM_NAV_HEIGHT, PHONE, PHONE_QUERY, schemed } from '../ui/listStyles'
import { APP_VERSION } from '../version'
import BrandMark from './BrandMark'
import BrandWordmark from './BrandWordmark'
import CloudUpsell from './CloudUpsell'
import { HelpButton } from '../lib/helpSurface'
import ConnectionIndicator from './ConnectionIndicator'
import MentionsBell from './MentionsBell'
import OrgScopeControl from '../ui/OrgScopeControl'
import StatusDot from '../ui/StatusDot'
import AuthorBadge from './tasks/AuthorBadge'
import TaskTree from './tasks/rail/TaskTree'
import SetupNavButton, { SetupNavBadge, useFoldedGuideProgress } from './onboarding/SetupNavButton'
import { stoppedPhrase } from './tasks/rail/railModel'
import {
  BoltIcon, CogIcon, ContrastIcon, FolderIcon, LogIcon, MoreHorizIcon,
  PersonIcon, RefreshIcon, TasksIcon, TemplatesIcon,
} from '../ui/icons'
import { ThemeField } from './UserSettingsFields'
import type { UserSettingsDraft } from '../lib/userSettings'
import { readString, writeString } from '../lib/safeStorage'

/* THE RAIL — two lists split by one divider:
 *   · the task tree (`tasks/rail/TaskTree.tsx`): Pinned, All projects › Project › Feature, each
 *     node with its Stopped count. Collapsed, it hides behind the Tasks entry (a flyout); on a phone the Tasks
 *     slot opens it in a sheet once the board is showing;
 *   · the destinations: Directory, Automation, Audit, Customers and Products (the operator's), Settings — pinned to the rail's bottom,
 *     above the footer; the operator's Dashboard alone heads the rail, above the tree. Support (and its Requests tab) is no rail row: it lives in the user badge's menu, which
 *     carries the pending-requests count.
 * Above both, while onboarding is unfinished: the Setup button with its percentage (`onboarding/SetupNavButton.tsx`;
 * the phone's More sheet heads with it).
 * Header: brand, connection dot, bell, collapse toggle. Above the footer, during a per-agent plan's free trial: the
 * trial meter (`TrialAgentsMeter.tsx`; the phone's sits in the More sheet). Footer: ONE line — the user badge (a menu: the org
 * switcher, then Support, My Profile, Sign out), the first name, the version, Help (the support chat).
 * Three shapes: the expanded rail, the collapsed rail (abbreviations), and the ≤480px bottom bar (four
 * slots + "More", a sheet carrying everything else and the account actions).
 *
 * THE LIVE CAST VIEWER'S RAIL (`live/LiveApp.tsx`, the `live` prop): the same rail, holding what a signed-out visitor
 * has — the brand (to jaah.ai), the published projects in the tree's place, a "Try it yourself" card in the
 * destinations' place, and a footer of Theme (`live/viewerTheme.ts`), Log in and Sign up — the last three into the
 * console at `LiveRail.consoleUrl`, and absent when the API names none (a relative `/signup` on the live host is a
 * 404). Collapsed, one Account button's menu holds all three; the phone's More sheet carries the card, the two rows
 * and the Theme. No destinations, status cluster or Help.
 * Nav visibility is presentation only, never the security boundary (`components/RequireFeature.tsx`). Every
 * gate below is the twin of a route guard in `App.tsx`; they move together, or the rail offers a row the
 * API refuses (or hides one it would serve). */

export type Destination = {
  /* One of `NAV_ORDER`: a row whose key it does not list fails to compile, rather than vanishing from the rail. */
  key: typeof NAV_ORDER[number]
  to: string
  label: string
  abbr: string
  /* The phone bar's mark (`ui/icons`); the collapsed rail keeps the monogram. */
  icon: ReactNode
  /* The phone bar's word when `label` would not fit its ~50px slot at 360px. */
  barLabel?: string
  /* The addresses this row is active on: `path` owns itself and everything under it; `exact` only itself;
     `under` only what is under it; `except` lists addresses under it that belong to no row. Kept beside the
     row so a rename sees it. */
  owns: readonly { path: string; exact?: boolean; under?: boolean; except?: readonly string[] }[]
  /* A member of a TENANT org; never the operator plane, which has no plan. */
  tenantOnly?: boolean
  /* The operator plane only — `tenantOnly`'s inverse. */
  operatorOnly?: boolean
  /* The org's Owner/Admin only (`lib/orgAdmin.ts::isOrgAdminSeat`). */
  orgAdminOnly?: boolean
  requires?: PermissionKey
  /* Either-of, for a destination whose tabs need different keys; OR-ed, then ANDed with the other terms. */
  requiresAny?: readonly PermissionKey[]
  /* Offered only while the viewer's org has Portfolio Compliance (`Admin.compliance_enabled`). */
  requiresCompliance?: boolean
}

/* Hand-ordered (`NAV_ORDER`); one word each, naming the area — its pages carry no title repeating it. Directory's and Automation's `to` and gate are resolved
   per viewer from `lib/directoryTabs.ts` / `lib/automationTabs.ts` (the first offered tab; offered when any
   tab is), so their entries here carry neither. */
const CORE_DESTINATIONS: readonly Destination[] = [
  { key: 'directory', to: '', label: 'Directory', abbr: 'Di', icon: <FolderIcon />, owns: [] },
  /* The template gallery, for every member (`pages/templates/TemplatesPage.tsx`); its manage and editor pages sit under it. */
  { key: 'templates', to: '/templates', label: 'Templates', abbr: 'Tp', icon: <TemplatesIcon />, barLabel: 'Templ.', owns: [{ path: '/templates' }] },
  /* Automations, Playbooks and Slots, plus the retired Schedule/Triggers addresses that redirect into them. EXACT on
     `/workflows`: the operator-only `/workflows/:name` and `/workflows/personas/:persona` detail pages belong to Audit. */
  {
    key: 'automation',
    to: '',
    label: 'Automation',
    abbr: 'At',
    icon: <BoltIcon />,
    barLabel: 'Auto',
    owns: [{ path: '/automations' }, { path: '/workflows', exact: true }, { path: '/crons' }, { path: '/triggers' }, { path: '/playbooks' }, { path: '/worktrees' }],
  },
  /* Twin of `App.tsx`'s guards (`audit:view`/`sessions:view`/`tasks:create` over `/audit*`, `sessions:view`
     over `/sessions*`); the shell (`pages/audit/AuditPage.tsx`) gates each tab on its own key. A tenant is offered
     Activity and Sessions alone, each while its plan switch is on (`auditTenant` below). */
  {
    key: 'audit',
    to: '/audit',
    label: 'Audit',
    abbr: 'Au',
    icon: <LogIcon />,
    // `/workflows/:name` and `/workflows/personas/:persona`: the catalogue's and Personas' detail pages.
    owns: [{ path: '/audit' }, { path: '/sessions' }, { path: '/workflows', under: true }],
    requiresAny: ['audit:view', 'sessions:view', 'tasks:create'],
  },
  /* Read by `system:view`, saved by the operator only (`routes/settings.py::is_operator`). */
  { key: 'settings', to: '/settings', label: 'Settings', abbr: 'St', icon: <CogIcon />, owns: [{ path: '/settings' }], requires: 'system:view' },
]

/* The rail's order, over both homes: these rows and the cloud console's own (`routesCloud.tsx`'s `cloudNavItems` —
   Dashboard, Customers, Products, Billing and Compliance — absent from the community edition, `lib/cloudSurfaces.ts`). */
const NAV_ORDER = ['dashboard', 'directory', 'templates', 'automation', 'compliance', 'audit', 'customers', 'products', 'settings', 'plan'] as const
const DESTINATIONS: readonly Destination[] = NAV_ORDER.flatMap(key => {
  const d = [...CORE_DESTINATIONS, ...(CLOUD?.cloudNavItems ?? [])].find(row => row.key === key)
  return d ? [d] : []
})
/* The cloud console's own rail chrome (`lib/cloudSurfaces.ts`): absent from the community edition. */
const OrgSwitcherList = CLOUD?.OrgSwitcherList
const TrialAgentsMeter = CLOUD?.TrialAgentsMeter

/* The ≤480px bar's four slots (operator decision): Tasks, Directory, Audit, Settings; everything else sits in
   the "More" sheet. The length is also the budget the active-route swap-in spends against. */
const BAR_KEYS = ['tasks', 'directory', 'audit', 'settings'] as const

/* Above the phone bar and below 900px (Settings' compact breakpoint): the rail starts collapsed whatever was saved. */
const NARROW_QUERY = '(min-width:480.05px) and (max-width:899.95px)'

/** The phone's bottom sheets (the tree and More): docked above the bar, scrolling within the viewport above it. */
const BOTTOM_SHEET_SX = (t: Theme) => ({
  display: 'flex',
  flexDirection: 'column',
  position: 'fixed',
  left: 0,
  right: 0,
  bottom: BOTTOM_NAV_HEIGHT,
  zIndex: 41,
  maxHeight: `calc(100dvh - ${2 * BOTTOM_NAV_HEIGHT}px)`,
  overflowY: 'auto',
  borderRadius: 0,
  borderTop: '1px solid',
  boxShadow: '0 -4px 16px rgba(0, 0, 0, 0.25)',
  padding: '0.5rem 0.5rem calc(0.5rem + env(safe-area-inset-bottom, 0px))',
  ...schemed(t, c => ({ backgroundColor: c.bg, borderColor: c.border, color: c.text })),
} as const)

/** A row as rendered: a board scope or a destination, resolved for this viewer. */
type RailRow = {
  key: string
  to: string
  /* A row that leaves this router (a Live Cast project, mounted under its own basename): a plain link to `to`
     whose plain click calls this instead. */
  onSelect?: () => void
  label: string
  abbr: string
  /* The phone bar's mark above its word, in the monogram's place; a row without one (a Live Cast project) keeps it. */
  icon?: ReactNode
  /* The phone bar's word in place of `label` (`Destination.barLabel`). */
  barLabel?: string
  active: boolean
  /* The trailing slot beside the label (counts), and the dot the abbreviation carries when it has no room. */
  trailing?: ReactNode
  dot?: ReactNode
  /* An accessible suffix, e.g. ", 3 pending". */
  srSuffix?: string
  testId?: string
}

function owned(pathname: string, owns: Destination['owns']): boolean {
  return owns.some(
    ({ path, exact, under, except }) =>
      !except?.includes(pathname) && ((!under && pathname === path) || (!exact && pathname.startsWith(`${path}/`))),
  )
}

/* AC11: a visible 2px focus ring in `custom.sidebar.accent`, picked against the rail's own ground (≥3:1 in
   both schemes) — a deliberate exception to the theme's `background.paper`-tuned default. */
function focusRing(t: Theme) {
  return {
    '&.Mui-focusVisible': {
      ...schemed(t, c => ({ outline: `2px solid ${c.accent}` })),
      outlineOffset: '2px',
    },
  }
}

/** The one visual definition of a rail row — condensed: 28px tall, no gap between rows. */
function railRow(t: Theme) {
  return {
    display: 'flex',
    alignItems: 'center',
    gap: '0.375rem',
    padding: '0.25rem 0.75rem',
    margin: '0 0.375rem',
    borderRadius: '4px',
    fontSize: '0.8125rem',
    fontWeight: 400,
    textDecoration: 'none',
    ...schemed(t, c => ({ color: c.text })),
    ...focusRing(t),
    '&:hover': schemed(t, c => ({ backgroundColor: c.hoverBg, color: c.textStrong })),
    '&.Mui-selected': {
      ...schemed(t, c => ({ backgroundColor: c.activeBg, color: c.textActive })),
      fontWeight: 500,
      position: 'relative',
      // The 3px left accent.
      '&::before': {
        content: '""',
        position: 'absolute',
        left: 0,
        top: 0,
        bottom: 0,
        width: '3px',
        ...schemed(t, c => ({ backgroundColor: c.accent })),
        borderRadius: '0 2px 2px 0',
      },
      '&:hover': schemed(t, c => ({ backgroundColor: c.activeBg })),
    },
    // Neutralises the theme's `down('sm')` 48px tap floor above the phone band (a mouse-driven rail), and
    // `ListItemButton`'s own flex-grow, which would stretch the rows down the rail's column.
    [ABOVE_PHONE]: { minHeight: 0, flex: '0 0 auto' },
  }
}

/** The collapsed rail's and the bar's Stopped total on the Tasks entry: a small filled count, aria-hidden (the
 *  row's label names it). */
function StoppedBadge({ count }: { count: number }) {
  if (count <= 0) return null
  return (
    <Box
      component="span"
      data-testid="nav-tasks-stopped"
      aria-hidden="true"
      sx={{
        position: 'absolute',
        top: 1,
        right: 2,
        minWidth: 14,
        height: 14,
        padding: '0 3px',
        borderRadius: '7px',
        fontSize: '0.5625rem',
        fontWeight: 700,
        lineHeight: '14px',
        textAlign: 'center',
        boxSizing: 'border-box',
        color: 'warning.contrastText',
        backgroundColor: 'warning.main',
      }}
    >
      {count > 99 ? '99+' : count}
    </Box>
  )
}

/** A row's "waiting on you" count: the expanded rail's trailing pill and the collapsed rail's dot. Both
 *  aria-hidden — the row's `srSuffix` names the count. */
function countBadge(count: number, testId: string): Pick<RailRow, 'trailing' | 'dot'> {
  return {
    trailing: (
      <Box
        component="span"
        data-testid={testId}
        aria-hidden="true"
        sx={{
          flex: '0 0 auto',
          marginLeft: 'auto',
          minWidth: 16,
          height: 16,
          padding: '0 4px',
          borderRadius: '8px',
          fontSize: '0.625rem',
          fontWeight: 700,
          lineHeight: '16px',
          textAlign: 'center',
          boxSizing: 'border-box',
          color: 'error.contrastText',
          backgroundColor: 'error.main',
        }}
      >
        {count > 50 ? '50+' : count}
      </Box>
    ),
    dot: (
      <StatusDot
        data-testid={`${testId}-dot`}
        tone="error"
        size={7}
        sx={{ position: 'absolute', top: 3, right: 5 }}
      />
    ),
  }
}

/** The Live Cast viewer's rail content: the published projects, the open one `active`, and the visitor's Theme. */
export type LiveRail = {
  projects: readonly { key: string; label: string; href: string; active: boolean }[]
  /** Opens a project: outside the open project's router, so the host moves the address. */
  go: (href: string) => void
  theme: UserSettingsDraft['theme']
  onThemeChange: (next: UserSettingsDraft['theme']) => void
  /** What the Theme's "Default" resolves to. */
  defaultTheme: string
  /** The console's origin (production's; its `/login` and `/signup`), or null — then no link into it renders. */
  consoleUrl: string | null
}

/** A menu row holding a form control: `role="none"`, and `muiSkipListHighlight` (read by MUI's MenuList, consumed
 *  here so it never reaches the DOM) keeps it from being the menu's focused item, as `OrgSwitcherList`. */
function MenuFormRow({ children }: { children: ReactNode; muiSkipListHighlight?: boolean }) {
  return <Box component="li" role="none" data-testid="sidebar-viewer-profile" sx={{ px: 2, py: 1 }}>{children}</Box>
}

/** A project's collapsed-rail abbreviation: its first two letters. */
const abbreviate = (label: string) => label.replace(/[^\p{L}\p{N}]/gu, '').slice(0, 2) || '·'

export default function Sidebar({ live }: { live?: LiveRail } = {}) {
  const { admin, logout, resolving } = useAuth()
  const can = useCan()
  const { pathname, search } = useLocation()
  // Guarded: the Live Cast viewer mounts this for visitors whose storage may throw (blocked site data).
  const [savedCollapsed, setSavedCollapsed] = useState(() => readString('local', 'sidebar-collapsed') === 'true')
  /* Between the phone and 900px the rail folds by itself, so a page's compact band (Settings' search, Category and
     Save) keeps one line. The saved choice stands: a click there expands this tab only, never stored,
     and lapses once the window widens past the band. */
  const narrow = useMediaQuery(NARROW_QUERY)
  const [narrowExpanded, setNarrowExpanded] = useState(false)
  if (!narrow && narrowExpanded) setNarrowExpanded(false)
  const collapsed = narrow ? !narrowExpanded : savedCollapsed
  const [moreOpen, setMoreOpen] = useState(false)
  const foldedGuide = useFoldedGuideProgress()
  const [menuAnchor, setMenuAnchor] = useState<HTMLElement | null>(null)
  // The one useVersionCheck() for the authenticated app: Sidebar mounts once per session (AuthLayout,
  // sibling of the Outlet); /login renders no Sidebar. Same single-detector rule for connection health.
  const updateAvailable = useVersionCheck()
  const health = useHealth()
  const pendingRequests = usePendingRequestCount(isEnabled('users_invites') && managesAccess(admin))
  /* Operator-only reads wait for the live identity: a load seeded from another seat's snapshot (an org switch, a
     second sign-in) would ask as the operator and draw a 403. */
  const decidesOrgs = isEnabled('multi_org_tenancy') && admin?.is_operator === true && !resolving
  const pendingOrgs = usePendingOrgCount(decidesOrgs)
  const pendingTrust = usePendingTrustRequestCount(decidesOrgs)
  const canTasks = can('tasks:view')
  /* Without Users and invitations there is no team: in the user menu one line points to Jaah Cloud (`CloudUpsell.tsx`, upsell 4). */
  const ceLimits = useCeLimits()
  const inviteUpsell = !isEnabled('users_invites') && (
    <CloudUpsell cta="Invite your team on Jaah Cloud" href={ceLimits.data?.upgrade_url ?? CLOUD_SIGNUP_URL} testId="upsell-invite" sx={{ px: 2, py: 0.75 }} />
  )
  const { tree } = useTaskTree(canTasks)
  const directoryTabs = useDirectoryTabs()
  const automationTabs = useAutomationTabs()
  const phone = useMediaQuery(PHONE_QUERY)
  // The collapsed rail's flyout anchor, and the phone's tree sheet.
  const [flyoutAnchor, setFlyoutAnchor] = useState<HTMLElement | null>(null)
  const [treeSheetOpen, setTreeSheetOpen] = useState(false)

  /* The path rows light against: none on a not-found page, which no row owns however its path reads. */
  const litPath = useNotFoundShown() ? '' : pathname
  const onBoard = pathname === '/tasks'
  const inTasks = litPath === '/tasks' || litPath.startsWith('/tasks/') || (live !== undefined && litPath === '/')
  /* The Tasks entry: the board's own URL while on it (so a click keeps its scope and filters), a bare `/tasks` from
     elsewhere — the user's focused project, else All projects (`lib/focusedProject.ts`). Expanded, the tree stands in for it; collapsed and on the bar it is the tree's handle. */
  const stopped = tree?.counts.stopped ?? 0
  const scopeRows: RailRow[] = live
    /* Never lit itself (the project row is); the phone bar lights its slot on the board (`barLit`). */
    ? [{ key: 'tasks', to: '/', label: 'Tasks', abbr: 'Tk', icon: <TasksIcon />, active: false, testId: 'nav-live-tasks' }]
    : canTasks
    ? [
        {
          key: 'tasks',
          to: onBoard && new URLSearchParams(search).has(SCOPE_PARAM) ? `/tasks${search}` : '/tasks',
          label: 'All projects',
          // The bar says "Tasks" (its slot is the board); the rail names the scope.
          barLabel: 'Tasks',
          abbr: 'Tk',
          icon: <TasksIcon />,
          active: inTasks,
          dot: <StoppedBadge count={stopped} />,
          srSuffix: tree && stopped > 0 ? `, ${stoppedPhrase(tree.counts)}` : undefined,
          testId: 'nav-project-all',
        },
      ]
    : []

  const auditTenant = (can('audit:view') && planSwitchOn(admin, 'audit_enabled'))
    || (can('sessions:view') && planSwitchOn(admin, 'session_transcripts_enabled'))
  // A visitor opens none of the console's areas.
  const destinationRows: RailRow[] = live ? [] : DESTINATIONS.flatMap(d => {
    if (d.key === 'directory') {
      if (directoryTabs.length === 0) return []
      return [{ key: d.key, to: directoryTabs[0].path, label: d.label, abbr: d.abbr, icon: d.icon, active: isInDirectory(litPath) }]
    }
    if (d.key === 'automation') {
      if (automationTabs.length === 0) return []
      return [{ key: d.key, to: automationTabs[0].path, label: d.label, barLabel: d.barLabel, abbr: d.abbr, icon: d.icon, active: owned(litPath, d.owns) }]
    }
    const visible =
      (d.key !== 'audit' || admin?.is_operator === true || auditTenant) &&
      (!d.tenantOnly || (admin?.tenant !== false && admin?.is_operator !== true)) &&
      (!d.operatorOnly || admin?.is_operator === true) &&
      (!d.orgAdminOnly || isOrgAdminSeat(admin)) &&
      (!d.requiresCompliance || admin?.compliance_enabled === true) &&
      (!d.requires || can(d.requires)) &&
      (!d.requiresAny || d.requiresAny.some(key => can(key)))
    if (!visible) return []
    const row: RailRow = { key: d.key, to: d.to, label: d.label, barLabel: d.barLabel, abbr: d.abbr, icon: d.icon, active: owned(litPath, d.owns) }
    // Sign-ups and trust document requests awaiting a decision are both decided on Customers, so they badge this row.
    if (d.key === 'customers' && pendingOrgs + pendingTrust > 0) {
      Object.assign(row, countBadge(pendingOrgs + pendingTrust, 'nav-orgs-pending-count'))
      row.srSuffix = ', ' + [
        pendingOrgs > 0 ? `${pendingOrgs} org${pendingOrgs === 1 ? '' : 's'} awaiting approval` : '',
        pendingTrust > 0 ? `${pendingTrust} document request${pendingTrust === 1 ? '' : 's'} awaiting a decision` : '',
      ].filter(Boolean).join(', ')
    }
    return [row]
  })

  /* The Live Cast viewer's projects, in the tree's place. */
  const projectRows: RailRow[] = (live?.projects ?? []).map(p => ({
    key: `project:${p.key}`,
    to: p.href,
    onSelect: () => live?.go(p.href),
    label: p.label,
    abbr: abbreviate(p.label),
    active: p.active,
    testId: 'nav-live-project',
  }))

  /* The Dashboard heads the rail; every other destination stays pinned to its foot. */
  const topRows = destinationRows.filter(r => r.key === 'dashboard')
  const footRows = destinationRows.filter(r => r.key !== 'dashboard')

  /* THE BAR: its slots, narrowed to what this viewer may open. If the active row is not a slot it replaces the
     last one, so the current page always has a lit tab — only when the bar is full, so nothing overflows at
     320px. Project rows never take a slot: on the board, the Tasks slot is the lit one. */
  const allRows = [...scopeRows, ...projectRows, ...destinationRows]
  const barRows = BAR_KEYS.map(key => allRows.find(r => r.key === key)).filter((r): r is RailRow => r !== undefined)
  const barKeys = new Set(barRows.map(r => r.key))
  const activeDestination = destinationRows.find(r => r.active)
  if (activeDestination && !barKeys.has(activeDestination.key)) {
    if (barKeys.size >= BAR_KEYS.length) barKeys.delete(barRows[barRows.length - 1].key)
    barKeys.add(activeDestination.key)
  }

  /* Access requests live under Support (its Requests tab), so the count of those waiting on this viewer rides
     the Support links. */
  const supportCount = pendingRequests > 0 && (
    <Box component="span" sx={{ display: 'inline-flex', ml: 1 }}>{countBadge(pendingRequests, 'sidebar-support-requests-count').trailing}</Box>
  )
  const supportName = pendingRequests > 0 ? `Support, ${pendingRequests} pending request${pendingRequests === 1 ? '' : 's'}` : undefined

  /* A nameless account shows its email whole rather than passing its prefix off as a name (`lib/memberLabel.ts::isNameless`). */
  const nameless = admin ? isNameless(admin) : false
  const firstName = admin?.first_name?.trim() || admin?.last_name?.trim() || ''
  const fullName = admin ? memberName(admin) : ''
  /* The footer badge's circle — shared by the initials and the nameless glyph, so both draw the same ring. */
  const userBadgeSx = {
    flexShrink: 0,
    width: 24,
    height: 24,
    borderRadius: '50%',
    border: '1.5px solid currentColor',
    boxSizing: 'border-box',
    display: 'inline-flex',
    alignItems: 'center',
    justifyContent: 'center',
    fontSize: '0.625rem',
    fontWeight: 700,
    letterSpacing: '0.02em',
  } as const
  const themeField = live && <ThemeField value={live.theme} onChange={live.onThemeChange} defaultTheme={live.defaultTheme} />
  /* The viewer's two doors into the console, built once; none when the API named no console (then nothing links). */
  const auth = live?.consoleUrl ? { login: `${live.consoleUrl}/login`, signup: `${live.consoleUrl}/signup` } : null
  /* Collapsed, the footer is one column: one Account button whose menu holds Log in, Sign up and the Theme — one
     control for one visitor slot, rather than two stacked icons opening two popovers. With no console to link, the
     Theme button stands alone there too. */
  const accountMenu = live !== undefined && collapsed && auth !== null

  function toggle() {
    if (narrow) {
      setNarrowExpanded(e => !e)
      return
    }
    setSavedCollapsed(c => {
      const next = !c
      writeString('local', 'sidebar-collapsed', String(next))
      return next
    })
  }

  function openMenu(e: MouseEvent<HTMLElement>) {
    setMenuAnchor(e.currentTarget)
  }

  /* The Tasks entry's click. Collapsed: the tree in a flyout. On the phone bar, once the board is showing: the
     tree in a sheet (a second tap closes it) — so the tree is at most two taps away. Otherwise a link. */
  function openTree(e: MouseEvent<HTMLElement>) {
    if (phone) {
      if (!onBoard) return
      e.preventDefault()
      setMoreOpen(false)
      setTreeSheetOpen(o => !o)
      return
    }
    if (!collapsed) return
    e.preventDefault()
    setFlyoutAnchor(e.currentTarget)
  }

  function railItem(row: RailRow, { sheet = false } = {}) {
    const active = row.active
    // On the board the BAR's Tasks slot is lit whatever the scope (project rows never sit in the bar); the
    // rail lights the scope's own row instead, so this is a phone-only style.
    const barLit = row.key === 'tasks' && !sheet && inTasks && !active
    const overflow = !barKeys.has(row.key)
    return (
      <ListItemButton
        key={row.key}
        {...(row.onSelect
          ? {
              component: 'a',
              href: row.to,
              onClick: (e: MouseEvent<HTMLElement>) => {
                if (e.metaKey || e.ctrlKey || e.shiftKey || e.button !== 0) return
                e.preventDefault()
                row.onSelect?.()
              },
            }
          : {
              component: Link,
              to: row.to,
              onClick: row.key === 'tasks' && !sheet && !live ? (e: MouseEvent<HTMLElement>) => openTree(e) : undefined,
            })}
        aria-haspopup={row.key === 'tasks' && !sheet && !live && (collapsed || phone) ? 'tree' : undefined}
        title={row.label}
        // The accessible name in every shape: the collapsed rail and the bar show only an abbreviation, and
        // the counts only as an aria-hidden dot.
        aria-label={`${row.label}${row.srSuffix ?? ''}`}
        selected={active}
        aria-current={active ? 'page' : undefined}
        data-testid="nav-item"
        data-row={row.testId ?? row.key}
        // State as an attribute, always written, so "NOT in overflow" is not a vacuous assertion.
        data-overflow={overflow ? 'true' : 'false'}
        sx={(t: Theme) => ({
          ...railRow(t),
          position: 'relative',
          ...(collapsed && !sheet && { justifyContent: 'center', padding: '0.3125rem 0' }),
          // Expanded, the tree IS the Tasks entry.
          ...(row.key === 'tasks' && !collapsed && !sheet && { [ABOVE_PHONE]: { display: 'none' } }),
          [PHONE]: sheet
            ? {
                flex: '0 0 auto',
                justifyContent: 'flex-start',
                gap: '0.625rem',
                padding: '0 1rem',
                margin: '1px 0',
              }
            : {
                display: overflow ? 'none' : 'flex',
                flex: 1,
                flexDirection: 'column',
                justifyContent: 'center',
                alignItems: 'center',
                gap: '2px',
                minWidth: 0,
                // 1px gutter: five slots at 320px are 54px each; the label keeps its size.
                margin: '2px 1px',
                padding: '4px 1px',
                ...(barLit && schemed(t, c => ({ backgroundColor: c.activeBg, color: c.textActive }))),
              },
        })}
      >
        {/* All render; width picks: the icon on the collapsed rail and the phone bar (the monogram for a row with no
            icon), the label elsewhere. The label has its own element (ellipsis on a flex container's text is
            unreliable). */}
        <Typography
          component="span"
          data-testid="nav-abbr"
          sx={(t: Theme) => ({
            fontFamily: t.typography.mono.fontFamily,
            fontSize: '0.6875rem',
            fontWeight: 700,
            letterSpacing: 0,
            display: 'none',
            ...(collapsed && !sheet && !row.icon && { display: 'inline' }),
            [PHONE]: { display: sheet || row.icon ? 'none' : 'inline', fontSize: '0.8125rem' },
          })}
        >
          {row.abbr}
        </Typography>
        {row.icon && (
          <Box
            component="span"
            data-testid="nav-icon"
            sx={{
              display: collapsed && !sheet ? 'inline-flex' : 'none',
              fontSize: '1.25rem',
              [PHONE]: { display: sheet ? 'none' : 'inline-flex' },
            }}
          >
            {row.icon}
          </Box>
        )}
        <Typography
          component="span"
          noWrap
          data-testid="nav-label"
          sx={{
            minWidth: 0,
            fontSize: 'inherit',
            fontWeight: 'inherit',
            ...(collapsed && !sheet && { display: 'none' }),
            [PHONE]: sheet
              ? { display: 'inline', fontSize: '0.875rem' }
              : { display: 'block', fontSize: '0.5625rem', maxWidth: '100%' },
          }}
        >
          {row.barLabel ? (
            <>
              <Box component="span" sx={{ [PHONE]: { display: sheet ? 'inline' : 'none' } }}>{row.label}</Box>
              <Box component="span" sx={{ display: 'none', [PHONE]: { display: sheet ? 'none' : 'inline' } }}>{row.barLabel}</Box>
            </>
          ) : row.label}
        </Typography>
        {row.trailing && (
          <Box
            component="span"
            sx={{
              display: 'contents',
              ...(collapsed && !sheet && { display: 'none' }),
              [PHONE]: { display: sheet ? 'contents' : 'none' },
            }}
          >
            {row.trailing}
          </Box>
        )}
        {row.dot && (
          <Box
            component="span"
            sx={{
              display: 'none',
              ...(collapsed && !sheet && { display: 'contents' }),
              [PHONE]: { display: sheet ? 'none' : 'contents' },
            }}
          >
            {row.dot}
          </Box>
        )}
      </ListItemButton>
    )
  }

  const statusCluster = (
    <Box
      data-testid="sidebar-connection"
      sx={(t: Theme) => ({
        /* On the rail's light ground `palette.success/warning.main` fall below 3:1, and this dot is the only
           connection signal, so the two variables are re-pointed for this slot only. */
        ...schemed(t, c => ({
          '--mui-palette-success-main': c.statusOk,
          '--mui-palette-warning-main': c.statusWarn,
        })),
        display: 'flex',
        alignItems: 'center',
        gap: '0.125rem',
        // Expanded: the auto margin pushes the cluster right, beside the toggle.
        ...(collapsed ? { flexDirection: 'column' } : { marginLeft: 'auto' }),
        [PHONE]: { flex: '0 0 auto', flexDirection: 'row', padding: '0 0.5rem', marginLeft: 0 },
      })}
    >
      {/* `showDaemon` off `is_operator` (`ConnectionIndicator.tsx`'s `Breakdown` says why). */}
      <ConnectionIndicator
        state={health.overall}
        components={health.components}
        compact
        showDaemon={admin?.is_operator === true}
      />
      {/* Help rides the footer above phone width; the phone hides the footer, and its bar is the bottom. */}
      {phone && HelpButton && <HelpButton />}
      <MentionsBell />
    </Box>
  )

  const accountButtonSx = (t: Theme) => ({
    flexShrink: 0,
    width: 'auto',
    minHeight: 44,
    padding: '0 12px',
    border: '1px solid',
    borderRadius: '4px',
    fontSize: '0.8125rem',
    ...schemed(t, c => ({ color: c.text, borderColor: c.buttonBorder })),
    ...focusRing(t),
    '&:hover': schemed(t, c => ({
      backgroundColor: c.overlayHover,
      borderColor: c.buttonHoverBorder,
      color: c.textStrong,
    })),
  })

  /* THE VIEWER'S CONTROLS. Rail chrome, not paper: `palette.primary` is tuned for paper, so the filled button takes
     the rail's own `activeBg` (the lit row's ground), and its hover `bannerHoverBg` (opaque on light: the Reload
     pill's lesson). */
  const filledSx = (t: Theme) => ({
    minWidth: 0,
    minHeight: 32,
    padding: '0.25rem 0.75rem',
    borderRadius: '6px',
    fontSize: '0.8125rem',
    fontWeight: 600,
    lineHeight: 1.2,
    ...schemed(t, c => ({ backgroundColor: c.activeBg, color: c.textActive })),
    ...focusRing(t),
    '&:hover': schemed(t, c => ({ backgroundColor: c.bannerHoverBg, color: c.textActive })),
  })
  const signupSx = (t: Theme) => ({ ...filledSx(t), flexShrink: 0, minHeight: 28, padding: '0.125rem 0.625rem' })
  const loginSx = (t: Theme) => ({
    flexShrink: 0,
    minWidth: 0,
    minHeight: 28,
    padding: '0.125rem 0.375rem',
    borderRadius: '6px',
    fontSize: '0.8125rem',
    fontWeight: 500,
    lineHeight: 1.2,
    ...schemed(t, c => ({ color: c.textStrong })),
    ...focusRing(t),
    '&:hover': schemed(t, c => ({ backgroundColor: c.overlayHover, color: c.textStrong })),
  })
  /* The footer's bare icon buttons (Theme; collapsed, Account), the collapse toggle's shape. */
  const glyphButtonSx = (t: Theme) => ({
    flexShrink: 0,
    color: 'inherit',
    fontSize: '1.125rem',
    padding: '3px',
    borderRadius: '6px',
    ...schemed(t, c => ({ color: c.textStrong })),
    ...focusRing(t),
    '&:hover': schemed(t, c => ({ backgroundColor: c.overlayHover, color: c.textStrong })),
    [ABOVE_PHONE]: { minWidth: 0, minHeight: 0 },
  })
  const sheetLinkSx = (t: Theme) => ({
    ...railRow(t),
    [PHONE]: { flex: '0 0 auto', justifyContent: 'flex-start', padding: '0 1rem', margin: '1px 0', fontSize: '0.875rem' },
  })

  /* THE "TRY IT YOURSELF" CARD: the one lit surface a visitor's rail holds — the Setup card's frame, with the rail's
     filled button. Expanded, it sits where the destinations would (sticky at the rail's foot); the phone's More sheet
     heads with it; the collapsed rail omits it (its Account menu's Sign up is the same door). Not dismissable. */
  const tryBanner = (shape: 'rail' | 'sheet') => auth && (
    <Box
      data-testid="sidebar-try-banner"
      data-shape={shape}
      sx={(t: Theme) => ({
        display: 'flex',
        flexDirection: 'column',
        gap: '0.5rem',
        flex: '0 0 auto',
        margin: shape === 'sheet' ? '0 0 0.375rem' : '0.25rem 0.5rem 0.375rem',
        padding: '0.625rem 0.625rem 0.5625rem',
        borderRadius: '8px',
        border: '1px solid',
        ...schemed(t, c => ({ borderColor: c.accent, backgroundColor: c.bannerBg, color: c.textStrong })),
        [PHONE]: shape === 'sheet' ? {} : { display: 'none' },
      })}
    >
      <Typography component="p" sx={{ m: 0, fontSize: '0.8125rem', lineHeight: 1.35, fontWeight: 500 }}>
        Jaah builds this project on its own.
      </Typography>
      <Button component="a" href={auth.signup} data-testid="sidebar-try-btn" sx={(t: Theme) => ({ ...filledSx(t), width: '100%' })}>
        Try it yourself
      </Button>
    </Box>
  )

  return (
    <Box
      component="aside"
      data-testid="sidebar"
      // State as an attribute: under Emotion the class name is a hash.
      data-collapsed={collapsed ? 'true' : 'false'}
      sx={(t: Theme) => ({
        width: collapsed ? t.custom.sidebar.widthCollapsed : t.custom.sidebar.width,
        ...(!collapsed && {
          [`@media (max-width:${t.custom.sidebar.compactBelowPx - 0.05}px)`]: { width: t.custom.sidebar.widthCompact },
        }),
        flexShrink: 0,
        display: 'flex',
        flexDirection: 'column',
        // The rail must not be the scroll container: `min-height: 0` plus the nav's `overflow-y: auto` keeps
        // the footer pinned on a short window.
        minHeight: 0,
        boxShadow: '2px 0 8px rgba(0, 0, 0, 0.12)',
        /* The `--sk-mark-*` properties feed `BrandMark` and `BrandWordmark` their stops: neither has scheme
           logic, so the rail (and the login card) supply them. */
        ...schemed(t, c => ({
          backgroundColor: c.bg,
          color: c.text,
          '--sk-mark-0': c.mark[0],
          '--sk-mark-1': c.mark[1],
          '--sk-mark-2': c.mark[2],
          '--sk-mark-core': c.markCore,
        })),
        [PHONE]: {
          position: 'fixed',
          left: 0,
          right: 0,
          bottom: 0,
          top: 'auto',
          width: '100%',
          minHeight: BOTTOM_NAV_HEIGHT,
          flexDirection: 'row',
          alignItems: 'stretch',
          zIndex: 40,
          boxShadow: '0 -2px 8px rgba(0, 0, 0, 0.12)',
        },
      })}
    >
      <Toolbar
        variant="dense"
        disableGutters
        data-testid="sidebar-header"
        sx={(t: Theme) => ({
          minHeight: 0,
          display: 'flex',
          alignItems: 'center',
          gap: '0.25rem',
          padding: '0.625rem 0.5rem 0.5rem 0.875rem',
          borderBottom: '1px solid',
          ...schemed(t, c => ({ borderColor: c.border })),
          ...(collapsed && {
            flexDirection: 'column',
            padding: '0.625rem 0 0.5rem',
            gap: '0.25rem',
          }),
          // On phones the header dissolves: brand and toggle hide, and the status cluster becomes a slot of
          // the bottom bar (the one mount serves all three shapes).
          [PHONE]: { display: 'contents' },
        })}
      >
        <Box
          data-testid="sidebar-brand"
          sx={{
            display: 'flex',
            alignItems: 'center',
            flexShrink: 0,
            ...(collapsed && { justifyContent: 'center', width: '100%' }),
            [PHONE]: { display: 'none' },
          }}
        >
          {/* Collapsed: the J mark alone; expanded: the wordmark. The viewer's opens the main website. */}
          <Box
            {...(live ? { component: 'a', href: SITE_URL, 'aria-label': 'jaah.ai', 'data-testid': 'sidebar-brand-link' } : {})}
            sx={(t: Theme) => ({ display: 'inline-flex', lineHeight: 0, borderRadius: '4px', '&:focus-visible': focusRing(t)['&.Mui-focusVisible'] })}
          >
            {collapsed
              ? <BrandMark size={24} />
              : <Box data-testid="sidebar-brand-text"><BrandWordmark height={32} /></Box>}
          </Box>
        </Box>
        {!live && statusCluster}
        <IconButton
          data-testid="sidebar-toggle"
          onClick={toggle}
          title={collapsed ? 'Expand sidebar' : 'Collapse sidebar'}
          aria-label={collapsed ? 'Expand sidebar' : 'Collapse sidebar'}
          sx={(t: Theme) => ({
            color: 'inherit',
            fontSize: collapsed ? '1.125rem' : '1rem',
            lineHeight: 1,
            padding: '2px 5px',
            borderRadius: '3px',
            ...focusRing(t),
            '&:hover': schemed(t, c => ({ backgroundColor: c.overlayHover, color: c.textStrong })),
            [ABOVE_PHONE]: { minWidth: 0, minHeight: 0 },
            [PHONE]: { display: 'none' },
          })}
        >
          {collapsed ? '›' : '‹'}
        </IconButton>
      </Toolbar>
      {/* The operator's global org filter, under the brand on every page (`ui/OrgScopeControl.tsx`; renders
          nothing for anyone else). The phone carries it as a strip above the page instead. Without Multiple orgs, one
          org, so nothing to filter. */}
      {admin?.is_operator === true && !phone && isEnabled('multi_org_tenancy') && (
        <Box
          data-testid="sidebar-org-scope"
          sx={(t: Theme) => ({
            display: 'flex',
            justifyContent: collapsed ? 'center' : 'stretch',
            padding: collapsed ? '0.375rem 0' : '0.5rem 0.5rem 0.5rem 0.75rem',
            borderBottom: '1px solid',
            ...schemed(t, c => ({ borderColor: c.border })),
          })}
        >
          <OrgScopeControl variant={collapsed ? 'collapsed' : 'rail'} />
        </Box>
      )}
      {updateAvailable && !live && (
        // Non-destructive (D4/AC5): reload only on this explicit click, never automatically.
        <Box
          data-testid="sidebar-update-banner"
          sx={(t: Theme) => ({
            display: 'flex',
            alignItems: 'center',
            justifyContent: 'space-between',
            gap: '0.5rem',
            padding: '0.375rem 0.875rem',
            borderBottom: '1px solid',
            ...schemed(t, c => ({ backgroundColor: c.bannerBg, borderColor: c.border })),
            ...(collapsed && { justifyContent: 'center', padding: '0.375rem 0' }),
            [PHONE]: {
              flex: '0 0 auto',
              padding: 0,
              backgroundColor: 'transparent',
              borderBottom: 'none',
            },
          })}
        >
          {!collapsed && (
            <Typography
              component="span"
              noWrap
              data-testid="sidebar-update-text"
              sx={(t: Theme) => ({
                fontSize: '0.75rem',
                minWidth: 0,
                ...schemed(t, c => ({ color: c.textStrong })),
                [PHONE]: { display: 'none' },
              })}
            >
              Update available
            </Typography>
          )}
          <Button
            data-testid="sidebar-update-btn"
            onClick={() => window.location.reload()}
            title="Reload to load the new version"
            aria-label="Reload to load the new version"
            sx={(t: Theme) => ({
              flexShrink: 0,
              minWidth: 0,
              padding: '0.125rem 0.5rem',
              border: '1px solid',
              borderRadius: '4px',
              fontSize: '0.75rem',
              fontWeight: 500,
              ...schemed(t, c => ({
                borderColor: c.accent,
                color: c.textActive,
                backgroundColor: c.activeBg,
              })),
              ...focusRing(t),
              '&:hover': schemed(t, c => ({ backgroundColor: c.bannerHoverBg })),
              [PHONE]: { minWidth: 44, minHeight: 44, margin: '2px' },
            })}
          >
            {/* Both spans render; width picks one, as with the nav items. */}
            <Box
              component="span"
              data-testid="sidebar-update-icon"
              aria-hidden="true"
              sx={{
                display: 'none',
                ...(collapsed && { display: 'inline' }),
                [PHONE]: { display: 'inline', fontSize: '1rem' },
              }}
            >
              <RefreshIcon />
            </Box>
            <Box
              component="span"
              data-testid="sidebar-update-label"
              sx={{
                ...(collapsed && { display: 'none' }),
                [PHONE]: { display: 'none' },
              }}
            >
              Reload
            </Box>
          </Button>
        </Box>
      )}
      {/* The divider is a sibling of the rows: the ≤480px bar lays out this nav's direct children as flex
          slots (the divider hides there), and the >480px scrollport stays on this element. */}
      <List
        component="nav"
        disablePadding
        data-testid="sidebar-nav"
        sx={{
          flex: '1 1 auto',
          minHeight: 0,
          overflowY: 'auto',
          padding: '0.375rem 0',
          display: 'flex',
          flexDirection: 'column',
          // A row scrolled into view by focus must land above the pinned destinations, not under them
          // (six rows and the divider, rounded up).
          [ABOVE_PHONE]: { scrollPaddingBottom: '12rem' },
          [PHONE]: {
            flex: '1 1 0%',
            minHeight: 'auto',
            overflowY: 'visible',
            display: 'flex',
            flexDirection: 'row',
            alignItems: 'stretch',
            padding: 0,
          },
        }}
      >
        {/* Onboarding's door, above everything while setup is unfinished (`onboarding/SetupNavButton.tsx`);
            the phone carries it in the More sheet. */}
        <SetupNavButton shape={collapsed ? 'collapsed' : 'rail'} />
        {topRows.map(row => railItem(row))}
        {scopeRows.map(row => railItem(row))}
        {projectRows.map(row => railItem(row))}
        {canTasks && !collapsed && (
          <Box data-testid="sidebar-tree" sx={{ flex: '0 0 auto', [PHONE]: { display: 'none' } }}>
            <TaskTree tree={tree} />
          </Box>
        )}
        {/* The destinations sit at the rail's bottom: the auto margin takes the free space on a short roster,
            and `sticky` pins them to the scrollport's foot on a long one (the nav stays the scroll container).
            On phones the wrapper dissolves, so its rows stay slots of the bar. */}
        <Box
          data-testid="sidebar-destinations"
          sx={(t: Theme) => ({
            marginTop: 'auto',
            position: 'sticky',
            bottom: '-0.375rem',
            paddingBottom: '0.375rem',
            display: 'flex',
            flexDirection: 'column',
            ...schemed(t, c => ({ backgroundColor: c.bg })),
            [PHONE]: { display: 'contents' },
          })}
        >
          {live && !collapsed && tryBanner('rail')}
          {scopeRows.length > 0 && footRows.length > 0 && (
            <Divider
              data-testid="sidebar-nav-sep"
              sx={(t: Theme) => ({
                height: '1px',
                border: 0,
                margin: '0.375rem 0.75rem',
                ...schemed(t, c => ({ backgroundColor: c.border })),
                [PHONE]: { display: 'none' },
              })}
            />
          )}
          {footRows.map(row => railItem(row))}
        </Box>
        <ListItemButton
          component="button"
          data-testid="nav-more-btn"
          onClick={() => { setTreeSheetOpen(false); setMoreOpen(o => !o) }}
          aria-expanded={moreOpen}
          aria-label={`More navigation${foldedGuide ? `, setup ${foldedGuide}` : ''}`}
          sx={(t: Theme) => ({
            display: 'none',
            [PHONE]: {
              display: 'flex',
              flex: 1,
              flexDirection: 'column',
              justifyContent: 'center',
              alignItems: 'center',
              gap: '2px',
              minWidth: 0,
              // Same slot metrics as a nav item, so the toggle is not an odd width.
              margin: '2px 1px',
              padding: '4px 1px',
              borderRadius: '4px',
              background: 'none',
              border: 'none',
              fontFamily: 'inherit',
              cursor: 'pointer',
              ...schemed(t, c => ({ color: c.text })),
              ...focusRing(t),
              // Mirror the active treatment while the sheet is open.
              '&[aria-expanded="true"]': schemed(t, c => ({
                backgroundColor: c.activeBg,
                color: c.textActive,
              })),
            },
          })}
        >
          <Typography component="span" data-testid="nav-abbr" sx={{ fontSize: '1.25rem', display: 'inline-flex' }}>
            <MoreHorizIcon />
          </Typography>
          {/* The onboarding bar folded into the navigation shows its count here; the sheet's Setup row restores it. */}
          <SetupNavBadge />
          <Typography component="span" data-testid="nav-label" sx={{ fontSize: '0.5625rem' }}>
            More
          </Typography>
        </ListItemButton>
      </List>
      {treeSheetOpen && phone && (
        // A tap above the sheet closes it (the bar stays live below: its Tasks slot toggles it too).
        <Box
          data-testid="nav-tree-sheet-backdrop"
          onClick={() => setTreeSheetOpen(false)}
          sx={{ position: 'fixed', left: 0, right: 0, top: 0, bottom: BOTTOM_NAV_HEIGHT, zIndex: 40 }}
        />
      )}
      {treeSheetOpen && phone && (
        /* The phone's tree: the More sheet's frame, 44px rows. A selection, Escape, a tap above it or the Tasks
           slot closes it. */
        <Paper
          data-testid="nav-tree-sheet"
          role="dialog"
          aria-label="Tasks"
          onKeyDown={e => { if (e.key === 'Escape') setTreeSheetOpen(false) }}
          elevation={0}
          sx={(t: Theme) => ({
            ...BOTTOM_SHEET_SX(t),
            padding: '0.5rem 0.25rem calc(0.5rem + env(safe-area-inset-bottom, 0px))',
          })}
        >
          <TaskTree tree={tree} touch onNavigate={() => setTreeSheetOpen(false)} />
        </Paper>
      )}
      <Popover
        open={flyoutAnchor !== null && collapsed}
        anchorEl={flyoutAnchor}
        onClose={() => setFlyoutAnchor(null)}
        anchorOrigin={{ vertical: 'top', horizontal: 'right' }}
        transformOrigin={{ vertical: 'top', horizontal: 'left' }}
        slotProps={{
          paper: {
            'data-testid': 'nav-tree-flyout',
            sx: (t: Theme) => ({
              width: t.custom.sidebar.width,
              maxHeight: 'calc(100vh - 32px)',
              overflowY: 'auto',
              py: 0.75,
              ...schemed(t, c => ({ backgroundColor: c.bg, color: c.text })),
            }),
          } as object,
        }}
      >
        <TaskTree tree={tree} onNavigate={() => setFlyoutAnchor(null)} />
      </Popover>
      {moreOpen && (
        // Click bubbling from any link/button inside closes the sheet.
        <Paper
          data-testid="nav-more-sheet"
          elevation={0}
          onClick={() => setMoreOpen(false)}
          sx={(t: Theme) => ({ display: 'none', [PHONE]: BOTTOM_SHEET_SX(t) })}
        >
          <SetupNavButton shape="sheet" />
          {live && tryBanner('sheet')}
          {allRows.filter(row => row.key !== 'tasks' && !barKeys.has(row.key)).map(row => railItem(row, { sheet: true }))}
          {/* The visitor's doors into the console, as rows above its Theme. */}
          {auth && [
            <ListItemButton key="login" component="a" href={auth.login} data-testid="sidebar-login-link" sx={sheetLinkSx}>
              Log in
            </ListItemButton>,
            <ListItemButton key="signup" component="a" href={auth.signup} data-testid="sidebar-signup-link" sx={sheetLinkSx}>
              Sign up
            </ListItemButton>,
          ]}
          {live && (
            <Box
              onClick={e => e.stopPropagation()}
              data-testid="nav-more-sheet-theme"
              sx={(t: Theme) => ({ marginTop: '0.375rem', padding: '0.5rem', borderTop: '1px solid', ...schemed(t, c => ({ borderColor: c.border })) })}
            >
              {themeField}
            </Box>
          )}
          {/* The org switcher, above the account line. Its clicks stay inside it: accepting an invitation or
              typing a code must not close the sheet (a switch reloads the page anyway). */}
          {!live && <Box
            onClick={e => e.stopPropagation()}
            sx={(t: Theme) => ({ marginTop: '0.375rem', borderTop: '1px solid', ...schemed(t, c => ({ borderColor: c.border })) })}
          >
            {inviteUpsell}
            {OrgSwitcherList && isEnabled('multi_org_tenancy') && <OrgSwitcherList />}
          </Box>}
          {/* A per-agent plan's free trial, above the account line (`TrialAgentsMeter.tsx`). */}
          {!live && TrialAgentsMeter && <TrialAgentsMeter shape="sheet" />}
          <Box
            data-testid="nav-more-sheet-footer"
            sx={(t: Theme) => ({
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'space-between',
              /* Wraps: a long account line (a nameless account's email) takes its own line above the buttons. */
              flexWrap: 'wrap',
              gap: '0.5rem',
              marginTop: '0.375rem',
              padding: '0.5rem 0.5rem 0',
              borderTop: '1px solid',
              ...schemed(t, c => ({ borderColor: c.border })),
            })}
          >
            {/* The phone footer is hidden, so the sheet carries the account line and its three actions; a visitor
                has no account, so only the version. */}
            <Box sx={{ display: 'flex', flexDirection: 'column', minWidth: 0 }}>
              {/* A nameless account's email wraps rather than clipping: the sheet has no menu to read it whole in. */}
              {!live && <Typography
                component="span"
                noWrap={!nameless}
                data-testid="sidebar-user-name-mobile"
                data-nameless={nameless || undefined}
                sx={(t: Theme) => ({
                  fontSize: '0.8125rem',
                  minWidth: 0,
                  ...(nameless && { overflowWrap: 'anywhere' }),
                  ...schemed(t, c => ({ color: nameless ? c.text : c.textStrong })),
                })}
              >
                {nameless ? admin?.email : firstName}
              </Typography>}
              <Typography
                component="span"
                noWrap
                data-testid="sidebar-version-mobile"
                sx={(t: Theme) => ({ fontSize: '0.75rem', minWidth: 0, ...schemed(t, c => ({ color: c.textStrong })) })}
              >
                {APP_VERSION}
              </Typography>
            </Box>
            {/* Profile is never a nav row: it is a zero-grant account's only entry point, and no grant filter
                may drop it. A visitor has no account: its Theme sits above. */}
            {!live && <Box sx={{ display: 'flex', gap: '0.375rem', flexShrink: 0 }}>
              {isEnabled('support_assistant') && <Button component={Link} to="/support" data-testid="sidebar-support-link" aria-label={supportName} sx={accountButtonSx}>
                Support
                {supportCount}
              </Button>}
              <Button component={Link} to="/profile" data-testid="sidebar-profile-btn" sx={accountButtonSx}>
                My Profile
              </Button>
              <Button onClick={() => void logout()} data-testid="sidebar-signout-btn" sx={accountButtonSx}>
                Sign out
              </Button>
            </Box>}
          </Box>
        </Paper>
      )}
      {/* A per-agent plan's free trial, directly above the footer (`TrialAgentsMeter.tsx`); the phone's is in the More sheet. */}
      {!live && TrialAgentsMeter && <TrialAgentsMeter shape={collapsed ? 'collapsed' : 'rail'} />}
      {/* THE FOOTER — one line: the user badge (opens the org switcher, then Support / My Profile / Sign out), the first name, the
          version right-aligned, and Help to its right. Collapsed: the badge over Help, the version on the
          badge's title. THE VISITOR'S: the Theme button, Log in, Sign up, the version; it wraps on the 192px rail
          (the version is never clipped). Collapsed: one Account button, the version on its title. */}
      <Box
        data-testid="sidebar-footer"
        sx={(t: Theme) => ({
          display: 'flex',
          alignItems: 'center',
          gap: live ? '0.375rem' : '0.5rem',
          ...(live && { flexWrap: 'wrap' }),
          /* Right padding is small: Help's own button padding already insets the glyph. */
          padding: '0.375rem 0.25rem 0.375rem 0.625rem',
          borderTop: '1px solid',
          ...schemed(t, c => ({ borderColor: c.border })),
          ...(collapsed && { flexDirection: 'column', gap: '0.25rem', padding: '0.375rem 0' }),
          [PHONE]: { display: 'none' },
        })}
      >
        {live && (
          <IconButton
            data-testid={accountMenu ? 'sidebar-account-btn' : 'sidebar-theme-btn'}
            onClick={openMenu}
            aria-haspopup="menu"
            aria-expanded={menuAnchor !== null}
            aria-controls={menuAnchor ? 'sidebar-user-menu' : undefined}
            aria-label={accountMenu ? 'Account' : 'Theme'}
            title={collapsed ? `${accountMenu ? 'Account' : 'Theme'} · ${APP_VERSION}` : 'Theme'}
            sx={glyphButtonSx}
          >
            {accountMenu ? <PersonIcon /> : <ContrastIcon />}
          </IconButton>
        )}
        {live && !collapsed && auth && [
          <Button key="login" component="a" href={auth.login} data-testid="sidebar-login-link" sx={loginSx}>
            Log in
          </Button>,
          <Button key="signup" component="a" href={auth.signup} data-testid="sidebar-signup-link" sx={signupSx}>
            Sign up
          </Button>,
        ]}
        {!live && <ButtonBase
          data-testid="sidebar-user-badge"
          // Who is signed in, for a spec that must wait for identity to hydrate (the label shows a first name).
          data-user-email={admin?.email}
          onClick={openMenu}
          aria-haspopup="menu"
          aria-expanded={menuAnchor !== null}
          aria-controls={menuAnchor ? 'sidebar-user-menu' : undefined}
          aria-label={`Account menu for ${fullName}${pendingRequests > 0 ? `, ${pendingRequests} pending request${pendingRequests === 1 ? '' : 's'}` : ''}`}
          title={collapsed ? `${fullName} · ${APP_VERSION}` : fullName}
          sx={(t: Theme) => ({
            position: 'relative',
            display: 'flex',
            alignItems: 'center',
            gap: '0.5rem',
            minWidth: 0,
            padding: '2px 6px 2px 2px',
            borderRadius: '14px',
            ...schemed(t, c => ({ color: c.textStrong })),
            ...focusRing(t),
            '&:hover': schemed(t, c => ({ backgroundColor: c.overlayHover })),
            ...(collapsed && { padding: '2px' }),
          })}
        >
          {/* A nameless account draws a neutral head-and-shoulders glyph, not its email's initials ("DE" for
              dev-owner@…) in a per-author hue: those would pass the email prefix off as a name. The
              button's label and title still carry the email. */}
          {nameless ? (
            <Box component="span" data-testid="sidebar-user-initials" data-nameless="true" aria-hidden="true" sx={userBadgeSx}>
              <PersonIcon size={14} />
            </Box>
          ) : (
            <AuthorBadge name={fullName || '?'} testId="sidebar-user-initials" sx={userBadgeSx} />
          )}
          {!collapsed && (
            <Typography
              component="span"
              noWrap
              data-testid="sidebar-user-name"
              data-nameless={nameless || undefined}
              sx={(t: Theme) => ({
                minWidth: 0,
                fontSize: '0.8125rem',
                fontWeight: 500,
                ...(nameless && { fontWeight: 400, ...schemed(t, c => ({ color: c.text })) }),
              })}
            >
              {nameless ? admin?.email : firstName}
            </Typography>
          )}
          {/* Requests waiting on this viewer: the menu's Support item names the count; the badge carries a dot. */}
          {pendingRequests > 0 && countBadge(pendingRequests, 'sidebar-user-requests').dot}
        </ButtonBase>}
        {!collapsed && (
          /* From version.ts (build-time APP_VERSION), never a literal; `textStrong` at full opacity
             (`text` falls short of contrast at this size). */
          <Typography
            component="span"
            noWrap
            data-testid="sidebar-version"
            title={APP_VERSION}
            /* Never shrinks: a clipped version ("1.1…") is a wrong answer, and on the 192px rail the
               first name is what gives way — the badge beside it still identifies the user. */
            sx={(t: Theme) => ({
              marginLeft: 'auto',
              // Sits beside Help: pulls the footer's 0.5rem gap in to 0.125rem. The viewer has no Help.
              marginRight: live ? '0.375rem' : '-0.375rem',
              flexShrink: 0,
              fontSize: '0.75rem',
              ...schemed(t, c => ({ color: c.textStrong })),
            })}
          >
            {APP_VERSION}
          </Typography>
        )}
        {!phone && !live && HelpButton && <HelpButton />}
      </Box>
      <Menu
        id="sidebar-user-menu"
        data-testid="sidebar-user-menu"
        anchorEl={menuAnchor}
        open={menuAnchor !== null}
        onClose={() => setMenuAnchor(null)}
        anchorOrigin={{ vertical: 'top', horizontal: 'left' }}
        transformOrigin={{ vertical: 'bottom', horizontal: 'left' }}
        slotProps={{
          list: { dense: true },
          /* As wide as its widest row, so a short org name sits next to its "Current" / switch control instead
             of across a gap; never wider than 340px or the viewport minus a gutter, where a long org name gives
             way to an ellipsis (`account/OrgSwitcherList.tsx`). */
          paper: { sx: { width: 'max-content', maxWidth: 'min(340px, calc(100vw - 32px))', maxHeight: 'calc(100dvh - 32px)' } },
        }}
      >
        {/* The visitor's: its Theme (`live/viewerTheme.ts`) — and, collapsed, Log in and Sign up above it (the
            expanded footer holds those itself). */}
        {accountMenu && auth && [
          <MenuItem key="login" component="a" href={auth.login} data-testid="sidebar-login-link" onClick={() => setMenuAnchor(null)}>
            Log in
          </MenuItem>,
          <MenuItem key="signup" component="a" href={auth.signup} data-testid="sidebar-signup-link" onClick={() => setMenuAnchor(null)}>
            Sign up
          </MenuItem>,
          <Divider key="divider" />,
        ]}
        {/* `muiSkipListHighlight` + `role="none"`: a form row, never the menu's focused item (as `OrgSwitcherList`). */}
        {live && <MenuFormRow muiSkipListHighlight>{themeField}</MenuFormRow>}
        {/* The org switcher first; Support / My Profile / Sign out below it, unchanged. An array, not a fragment:
            MUI's Menu reads its children one by one. */}
        {/* Who is signed in, the address whole and wrapping: the footer's name line clips it. */}
        {!live && [
          <Box
            key="who"
            component="li"
            role="none"
            data-testid="sidebar-user-email"
            sx={{ px: 2, pt: 0.75, pb: 0.5, fontSize: '0.8125rem', color: 'text.secondary', overflowWrap: 'anywhere' }}
          >
            {admin?.email}
          </Box>,
          <Divider key="who-divider" />,
          ...(inviteUpsell ? [<Box key="invite" component="li" role="none">{inviteUpsell}</Box>] : []),
          ...(OrgSwitcherList && isEnabled('multi_org_tenancy') ? [<OrgSwitcherList key="orgs" muiSkipListHighlight />] : []),
          <Divider key="divider" />,
          ...(isEnabled('support_assistant') ? [
            <MenuItem key="support" component={Link} to="/support" data-testid="sidebar-support-link" aria-label={supportName} onClick={() => setMenuAnchor(null)}>
              Support
              {supportCount}
            </MenuItem>,
          ] : []),
          <MenuItem key="profile" component={Link} to="/profile" data-testid="sidebar-profile-btn" onClick={() => setMenuAnchor(null)}>
            My Profile
            {nameless && (
              <Typography component="span" data-testid="sidebar-profile-add-name" sx={{ ml: 1, fontSize: '0.75rem', color: 'text.secondary' }}>
                {ADD_NAME}
              </Typography>
            )}
          </MenuItem>,
          <MenuItem
            key="signout"
            data-testid="sidebar-signout-btn"
            onClick={() => {
              setMenuAnchor(null)
              void logout()
            }}
          >
            Sign out
          </MenuItem>,
        ]}
      </Menu>
    </Box>
  )
}
