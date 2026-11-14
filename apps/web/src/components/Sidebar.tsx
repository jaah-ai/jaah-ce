import { useRef, useState, type MouseEvent, type ReactNode } from 'react'
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
import { useTheme, type Theme } from '@mui/material/styles'
import { useAuth, useCan } from '../auth'
import { CLOUD } from '../lib/cloudSurfaces'
import { CLOUD_SIGNUP_URL, SITE_URL } from '../lib/edition'
import { isEnabled, type FeatureName } from '../lib/editionFeatures'
import { countOf } from '../lib/humanize'
import { useCeLimits } from '../lib/communityEdition'
import { isOrgAdminSeat } from '../lib/orgAdmin'
import { planSurfaceShown, planSwitchOn, type RosterSwitch } from '../lib/planSwitches'
import { ADD_NAME, isNameless, memberName } from '../lib/memberLabel'
import { SCOPE_PARAM } from '../lib/taskScope'
import { isInCustomize, useCustomizeTabs } from '../lib/customizeTabs'
import { isInDirectory, useDirectoryTabs } from '../lib/directoryTabs'
import { useNotFoundShown } from '../lib/notFound'
import { useTaskTree } from '../hooks/useTaskTree'
import { useOpenTicketCount, usePendingOrgCount, usePendingTrustRequestCount } from '../hooks/usePendingCount'
import { useHealth } from '../hooks/useHealth'
import type { PermissionKey } from '../types'
import CountPill from '../ui/CountPill'
import { usePhone } from '../hooks/usePhone'
import { ABOVE_PHONE, BOTTOM_NAV_HEIGHT, PHONE, schemed, SIDEBAR_NARROW_QUERY as NARROW_QUERY } from '../ui/listStyles'
import { FONT_SIZE, FONT_WEIGHT, grid, LETTER_SPACING, LINE_HEIGHT, RADIUS, SIZE } from '../theme'
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
  AlertIcon, CogIcon, ContrastIcon, FolderIcon, LogIcon, MoreHorizIcon,
  PersonIcon, SlidersIcon, SupportIcon, TasksIcon,
} from '../ui/icons'
import { ThemeField } from './UserSettingsFields'
import type { UserSettingsDraft } from '../lib/userSettings'
import { readString, removeKey, writeString } from '../lib/safeStorage'
import { DIVIDER_PX } from '../lib/splitPane'
import { parseSidebarWidth, resolveSidebarDrag, SIDEBAR_MAX_PX, SIDEBAR_MIN_PX, SIDEBAR_WIDTH_KEY } from '../lib/sidebarWidth'
import SplitDivider from './SplitDivider'
import Tip from '../ui/Tip'
import { memoryOffered, mergeLanesOffered } from '../pages/audit/projectAuditTabs'

/* THE RAIL — two lists split by one divider:
 *   · the task tree (`tasks/rail/TaskTree.tsx`): Pinned, All projects › Project › Feature, each
 *     node with its Stopped count. Collapsed, it hides behind the Tasks entry (a flyout); on a phone the Tasks
 *     slot opens it in a sheet once the board is showing;
 *   · the destinations: Directory, Reports (an Owner's/Admin's and the operator's), Customize, Deployments, Incidents, Support (the operator's, with
 *     its open-ticket count), Compliance, Audit, Customers, Products and Agent index (the operator's), Settings — pinned to the rail's bottom,
 *     above the footer. Everyone else reaches Support from the user badge's menu.
 * Above both, while onboarding is unfinished: the Setup button with its percentage (`onboarding/SetupNavButton.tsx`;
 * the phone's More sheet heads with it).
 * Header: brand, connection dot, bell, collapse toggle. Above the footer, on a per-agent plan (trial or paid): the
 * agents throttle (`TrialAgentsMeter.tsx`; the phone's sits in the More sheet). Footer: ONE line — the user badge (a menu: the org
 * switcher, then Support (not the operator's: theirs is the rail row), My Profile, Sign out), the first name, the version, Help (the support chat).
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
     `under` only what is under it; `except` lists addresses (and what is under them) that belong to another row or none. Kept beside the
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
  /* A plan feature's surface: hidden from a tenant while this switch is off (`lib/planSwitches.tsx::planSurfaceShown`). */
  planSurface?: RosterSwitch
  /* An edition feature's surface: offered only while it is enabled (`lib/editionFeatures.ts::isEnabled`). */
  feature?: FeatureName
}

/* Hand-ordered (`NAV_ORDER`); one word each, naming the area — its pages carry no title repeating it. Directory's and Customize's `to` and gate are resolved
   per viewer from `lib/directoryTabs.ts` / `lib/customizeTabs.ts` (the first offered tab; offered when any
   tab is), so their entries here carry neither. */
const CORE_DESTINATIONS: readonly Destination[] = [
  { key: 'directory', to: '', label: 'Directory', abbr: 'Di', icon: <FolderIcon />, owns: [] },
  /* Playbooks, Templates, Agents and Compliance Packs, and every editor under them (`lib/customizeTabs.ts::isInCustomize`). */
  { key: 'customize', to: '', label: 'Customize', abbr: 'Cz', icon: <SlidersIcon />, barLabel: 'Custom', owns: [] },
  /* Production QA's incidents (`production_qa`). Twin of `App.tsx`'s guard on them. */
  { key: 'incidents', to: '/incidents', label: 'Incidents', abbr: 'In', icon: <AlertIcon />, barLabel: 'Incid.', owns: [{ path: '/incidents' }], requires: 'projects:view', planSurface: 'production_qa' },
  /* The operator's ticket queue (`routesCloud.tsx`'s `/support`), badged with every org's open tickets; offered while
     `support_assistant` is enabled. Not a phone-bar slot: it sits in the More sheet, whose button carries its dot. */
  { key: 'support', to: '/support', label: 'Support', abbr: 'Sup', icon: <SupportIcon />, barLabel: 'Support', owns: [{ path: '/support' }], operatorOnly: true, feature: 'support_assistant' },
  /* Twin of `App.tsx`'s guards (`audit:view`/`sessions:view`/`tasks:create` over `/audit*`, `sessions:view`
     over `/sessions*`); the shell (`pages/audit/AuditPage.tsx`) gates each tab on its own key. A tenant is offered
     Activity and Sessions alone, each while its plan switch is on (`auditTenant` below), Merge lanes and Memory to a
     `projects:view` holder (`pages/audit/projectAuditTabs.ts`), and an org admin Automations
     (`/automations`), the org-wide list; the operator also Slots (`/worktrees`). */
  {
    key: 'audit',
    to: '/audit',
    label: 'Audit',
    abbr: 'Au',
    icon: <LogIcon />,
    // `/workflows/:name` and `/workflows/personas/:persona`: the catalogue's and Personas' detail pages.
    owns: [{ path: '/audit' }, { path: '/sessions' }, { path: '/workflows', under: true }, { path: '/automations' }, { path: '/worktrees' }],
    requiresAny: ['audit:view', 'sessions:view', 'tasks:create', 'projects:view'],
  },
  /* Read by `system:view`, saved by the operator only (`tenancy/ids.py::is_operator_user`). `/integrations/trackers`
     is reached from Settings › Integrations, so it lights this row. */
  { key: 'settings', to: '/settings', label: 'Settings', abbr: 'St', icon: <CogIcon />, owns: [{ path: '/settings' }, { path: '/integrations' }], requires: 'system:view' },
]

/* The rail's order, over both homes: these rows and the cloud console's own (`routesCloud.tsx`'s `cloudNavItems` —
   Reports, Customers, Products, Billing and Compliance — absent from the community edition, `lib/cloudSurfaces.ts`). */
const NAV_ORDER = ['directory', 'reports', 'customize', 'incidents', 'support', 'compliance', 'audit', 'customers', 'products', 'agent-index', 'settings', 'plan'] as const
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

/* `NARROW_QUERY` — above the phone bar and below 900px (Settings' compact breakpoint): the rail starts collapsed
   whatever was saved. */

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
  borderRadius: RADIUS.none,
  borderTop: '1px solid',
  boxShadow: 8,
  padding: `${grid(1)} ${grid(1)} calc(${grid(1)} + env(safe-area-inset-bottom, ${grid(0)}))`,
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
      !except?.some(gone => pathname === gone || pathname.startsWith(`${gone}/`)) && ((!under && pathname === path) || (!exact && pathname.startsWith(`${path}/`))),
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
    gap: 1,
    padding: `${grid(0.5)} ${grid(2)}`,
    margin: `0 ${grid(1)}`,
    borderRadius: RADIUS.sm,
    fontSize: FONT_SIZE.md,
    fontWeight: FONT_WEIGHT.regular,
    textDecoration: 'none',
    ...schemed(t, c => ({ color: c.text })),
    ...focusRing(t),
    '&:hover': schemed(t, c => ({ backgroundColor: c.hoverBg, color: c.textStrong })),
    '&.Mui-selected': {
      ...schemed(t, c => ({ backgroundColor: c.activeBg, color: c.textActive })),
      fontWeight: FONT_WEIGHT.medium,
      position: 'relative',
      // The 3px left accent.
      '&::before': {
        content: '""',
        position: 'absolute',
        left: 0,
        top: 0,
        bottom: 0,
        width: grid(0.5),
        ...schemed(t, c => ({ backgroundColor: c.accent })),
        borderTopRightRadius: RADIUS.sm,
        borderBottomRightRadius: RADIUS.sm,
      },
      '&:hover': schemed(t, c => ({ backgroundColor: c.activeBg })),
    },
    // Neutralises the theme's `down('sm')` 48px tap floor above the phone band (a mouse-driven rail), and
    // `ListItemButton`'s own flex-grow, which would stretch the rows down the rail's column.
    [ABOVE_PHONE]: { minHeight: 0, flex: '0 0 auto' },
  }
}

/** The collapsed rail's and the bar's Stopped total on the Tasks entry: the amber count coin (`ui/CountPill`) on the
 *  icon's corner, aria-hidden (the row's label names it). */
function StoppedBadge({ count }: { count: number }) {
  return (
    <CountPill count={count} tone="warning" max={99} testId="nav-tasks-stopped"
      sx={{ position: 'absolute', top: 1, right: 0, minWidth: SIZE.iconXs, height: SIZE.iconXs }} />
  )
}

/** A row's "waiting on you" count: the expanded rail's trailing pill and the collapsed rail's dot. Both
 *  aria-hidden — the row's `srSuffix` names the count. */
function countBadge(count: number, testId: string): Pick<RailRow, 'trailing' | 'dot'> {
  return {
    trailing: <CountPill count={count} testId={testId} sx={{ marginLeft: 'auto' }} />,
    dot: (
      <StatusDot
        data-testid={`${testId}-dot`}
        tone="error"
        size={grid(1)}
        sx={{ position: 'absolute', top: grid(0.5), right: grid(0.5) }}
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
  // The dragged width (`lib/sidebarWidth.ts`); `null` keeps the theme's default.
  const [savedWidth, setSavedWidth] = useState(() => parseSidebarWidth(readString('local', SIDEBAR_WIDTH_KEY)))
  const asideRef = useRef<HTMLElement>(null)
  const theme = useTheme()
  // A drag's move handler is bound once at pointer-down, so it reads the shape through a ref.
  const collapsedRef = useRef(collapsed)
  collapsedRef.current = collapsed
  const [moreOpen, setMoreOpen] = useState(false)
  const foldedGuide = useFoldedGuideProgress()
  const [menuAnchor, setMenuAnchor] = useState<HTMLElement | null>(null)
  // The one connection-health detector for the authenticated app: Sidebar mounts once per session (AuthLayout,
  // sibling of the Outlet); /login renders no Sidebar. The update check's one call is `UpdateBanner`'s.
  const health = useHealth()
  /* Operator-only reads wait for the live identity: a load seeded from another seat's snapshot (an org switch, a
     second sign-in) would ask as the operator and draw a 403. */
  const decidesOrgs = isEnabled('multi_org_tenancy') && admin?.is_operator === true && !resolving
  const pendingOrgs = usePendingOrgCount(decidesOrgs)
  const pendingTrust = usePendingTrustRequestCount(decidesOrgs)
  const operatorSupport = isEnabled('support_assistant') && admin?.is_operator === true && !resolving
  const openTickets = useOpenTicketCount(operatorSupport)
  const canTasks = can('tasks:view')
  // The account menu's Support: everyone's but the operator's, whose way in is the rail row.
  const accountSupport = isEnabled('support_assistant') && admin != null && admin.is_operator !== true
  /* Without Users and invitations there is no team: in the user menu one line points to Jaah Cloud (`CloudUpsell.tsx`, upsell 4). */
  const ceLimits = useCeLimits()
  const inviteUpsell = !isEnabled('users_invites') && (
    <CloudUpsell cta="Invite your team on Jaah Cloud" href={ceLimits.data?.upgrade_url ?? CLOUD_SIGNUP_URL} testId="upsell-invite" sx={{ px: 2, py: 0.5 }} />
  )
  const { tree } = useTaskTree(canTasks)
  const directoryTabs = useDirectoryTabs()
  const customizeTabs = useCustomizeTabs()
  const phone = usePhone()
  // The collapsed rail's flyout anchor, and the phone's tree sheet.
  const [flyoutAnchor, setFlyoutAnchor] = useState<HTMLElement | null>(null)
  const [treeSheetOpen, setTreeSheetOpen] = useState(false)

  /* The path rows light against: none on a not-found page, which no row owns however its path reads. */
  const litPath = useNotFoundShown() ? '' : pathname
  const onBoard = pathname === '/tasks'
  const inTasks = litPath === '/tasks' || litPath.startsWith('/tasks/') || (live !== undefined && litPath === '/')
  /* The Tasks entry: the board's own URL while on it (so a click keeps its scope and filters), a bare `/tasks` from
     elsewhere, which opens on All projects. Expanded, the tree stands in for it; collapsed and on the bar it is the tree's handle. */
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
    // Merge lanes and Memory, by the shell's own rule.
    || mergeLanesOffered(can) || memoryOffered(admin, can)
  // A visitor opens none of the console's areas.
  const destinationRows: RailRow[] = live ? [] : DESTINATIONS.flatMap(d => {
    if (d.key === 'directory') {
      if (directoryTabs.length === 0) return []
      return [{ key: d.key, to: directoryTabs[0].path, label: d.label, abbr: d.abbr, icon: d.icon, active: isInDirectory(litPath) }]
    }
    if (d.key === 'customize') {
      if (customizeTabs.length === 0) return []
      return [{ key: d.key, to: customizeTabs[0].path, label: d.label, barLabel: d.barLabel, abbr: d.abbr, icon: d.icon, active: isInCustomize(litPath) }]
    }
    const visible =
      // An org admin always has Audit's Automations tab, whatever the plan's audit switches say.
      (d.key !== 'audit' || admin?.is_operator === true || auditTenant || isOrgAdminSeat(admin)) &&
      (!d.tenantOnly || (admin?.tenant !== false && admin?.is_operator !== true)) &&
      (!d.operatorOnly || admin?.is_operator === true) &&
      (!d.orgAdminOnly || isOrgAdminSeat(admin)) &&
      (!d.requiresCompliance || admin?.compliance_enabled === true) &&
      (!d.planSurface || planSurfaceShown(admin, d.planSurface)) &&
      (!d.feature || isEnabled(d.feature)) &&
      (!d.requires || can(d.requires)) &&
      (!d.requiresAny || d.requiresAny.some(key => can(key)))
    if (!visible) return []
    const row: RailRow = { key: d.key, to: d.to, label: d.label, barLabel: d.barLabel, abbr: d.abbr, icon: d.icon, active: owned(litPath, d.owns) }
    // Sign-ups and trust document requests awaiting a decision are both decided on Customers, so they badge this row.
    if (d.key === 'customers' && pendingOrgs + pendingTrust > 0) {
      Object.assign(row, countBadge(pendingOrgs + pendingTrust, 'nav-orgs-pending-count'))
      row.srSuffix = ', ' + [
        pendingOrgs > 0 ? `${countOf(pendingOrgs, 'org')} awaiting approval` : '',
        pendingTrust > 0 ? `${countOf(pendingTrust, 'document request')} awaiting a decision` : '',
      ].filter(Boolean).join(', ')
    }
    if (d.key === 'support' && openTickets > 0) {
      Object.assign(row, countBadge(openTickets, 'nav-support-open-count'))
      row.srSuffix = `, ${countOf(openTickets, 'open ticket')}`
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
  /* The More sheet's rows. A counted one (`countBadge`'s pill) dots the More button and names its count there. */
  const sheetRows = allRows.filter(row => row.key !== 'tasks' && !barKeys.has(row.key))
  const sheetCounts = sheetRows.filter(row => row.trailing !== undefined).map(row => row.srSuffix ?? '').join('')

  /* A nameless account shows its email whole rather than passing its prefix off as a name (`lib/memberLabel.ts::isNameless`). */
  const nameless = admin ? isNameless(admin) : false
  /* The name ask's quiet mark (a dot), where others see the email: not in the community edition, whose one user is
     the only one who sees anything. No page carries standing prose for it. */
  const askName = nameless && isEnabled('users_invites')
  const firstName = admin?.first_name?.trim() || admin?.last_name?.trim() || ''
  const fullName = admin ? memberName(admin) : ''
  /* The footer badge's circle — shared by the initials and the nameless glyph, so both draw the same ring. */
  const userBadgeSx = {
    flexShrink: 0,
    width: SIZE.iconLg,
    height: SIZE.iconLg,
    borderRadius: RADIUS.round,
    border: '1px solid currentColor',
    boxSizing: 'border-box',
    display: 'inline-flex',
    alignItems: 'center',
    justifyContent: 'center',
    fontSize: FONT_SIZE.xxs,
    fontWeight: FONT_WEIGHT.bold,
    letterSpacing: LETTER_SPACING.none,
  } as const
  const themeField = live && <ThemeField value={live.theme} onChange={live.onThemeChange} defaultTheme={live.defaultTheme} />
  /* The viewer's two doors into the console, built once; none when the API named no console (then nothing links). */
  const auth = live?.consoleUrl ? { login: `${live.consoleUrl}/login`, signup: `${live.consoleUrl}/signup` } : null
  /* Collapsed, the footer is one column: one Account button whose menu holds Log in, Sign up and the Theme — one
     control for one visitor slot, rather than two stacked icons opening two popovers. With no console to link, the
     Theme button stands alone there too. */
  const accountMenu = live !== undefined && collapsed && auth !== null

  function setCollapsed(next: boolean) {
    if (narrow) {
      setNarrowExpanded(!next)
      return
    }
    setSavedCollapsed(next)
    writeString('local', 'sidebar-collapsed', String(next))
  }

  const toggle = () => setCollapsed(!collapsed)

  /* The edge strip straddles the rail's right edge, so the edge sits half a strip right of the strip's left. */
  function onEdgeMove(position: number, grabOffset: number) {
    const left = asideRef.current?.getBoundingClientRect().left ?? 0
    const drag = resolveSidebarDrag(position - grabOffset + DIVIDER_PX / 2 - left)
    if (drag.collapsed) {
      if (!collapsedRef.current) setCollapsed(true)
      return
    }
    if (collapsedRef.current) setCollapsed(false)
    setSavedWidth(drag.width)
    writeString('local', SIDEBAR_WIDTH_KEY, String(drag.width))
  }

  function resetWidth() {
    setSavedWidth(null)
    removeKey('local', SIDEBAR_WIDTH_KEY)
    if (collapsed) setCollapsed(false)
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
      <Tip key={row.key} title={row.label}><ListItemButton
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
          ...(collapsed && !sheet && { justifyContent: 'center', padding: `${grid(0.5)} 0` }),
          // Expanded, the tree IS the Tasks entry.
          ...(row.key === 'tasks' && !collapsed && !sheet && { [ABOVE_PHONE]: { display: 'none' } }),
          [PHONE]: sheet
            ? {
                flex: '0 0 auto',
                justifyContent: 'flex-start',
                gap: 1.5,
                padding: `0 ${grid(2.5)}`,
                margin: 0,
              }
            : {
                display: overflow ? 'none' : 'flex',
                flex: 1,
                flexDirection: 'column',
                justifyContent: 'center',
                alignItems: 'center',
                gap: 0,
                minWidth: 0,
                // No gutter: five slots at 320px are 64px each; the label keeps its size.
                margin: 0,
                padding: `${grid(0.5)} 0`,
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
            fontSize: FONT_SIZE.xs,
            fontWeight: FONT_WEIGHT.bold,
            letterSpacing: LETTER_SPACING.none,
            display: 'none',
            ...(collapsed && !sheet && !row.icon && { display: 'inline' }),
            [PHONE]: { display: sheet || row.icon ? 'none' : 'inline', fontSize: FONT_SIZE.md },
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
              fontSize: SIZE.iconMd,
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
              ? { display: 'inline', fontSize: FONT_SIZE.base }
              : { display: 'block', fontSize: FONT_SIZE.xxs, maxWidth: '100%' },
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
      </ListItemButton></Tip>
    )
  }

  const statusCluster = (
    <Box
      data-testid="sidebar-connection"
      sx={(t: Theme) => ({
        /* On the rail's light ground `palette.warning.main` falls below 3:1, and this dot is the only connection
           signal, so the variable is re-pointed for this slot only. */
        ...schemed(t, c => ({
          '--mui-palette-warning-main': c.statusWarn,
        })),
        display: 'flex',
        alignItems: 'center',
        gap: 0,
        // Expanded: the auto margin pushes the cluster right, beside the toggle.
        ...(collapsed ? { flexDirection: 'column' } : { marginLeft: 'auto' }),
        [PHONE]: { flex: '0 0 auto', flexDirection: 'row', padding: `0 ${grid(1)}`, marginLeft: 0 },
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
    minHeight: SIZE.tapTarget,
    padding: `0 ${grid(2)}`,
    border: '1px solid',
    borderRadius: RADIUS.sm,
    fontSize: FONT_SIZE.md,
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
    minHeight: SIZE.thumb,
    padding: `${grid(0.5)} ${grid(2)}`,
    borderRadius: RADIUS.sm,
    fontSize: FONT_SIZE.md,
    fontWeight: FONT_WEIGHT.semibold,
    lineHeight: LINE_HEIGHT.snug,
    ...schemed(t, c => ({ backgroundColor: c.activeBg, color: c.textActive })),
    ...focusRing(t),
    '&:hover': schemed(t, c => ({ backgroundColor: c.bannerHoverBg, color: c.textActive })),
  })
  const signupSx = (t: Theme) => ({ ...filledSx(t), flexShrink: 0, minHeight: SIZE.avatar, padding: `0 ${grid(1.5)}` })
  const loginSx = (t: Theme) => ({
    flexShrink: 0,
    minWidth: 0,
    minHeight: SIZE.avatar,
    padding: `0 ${grid(1)}`,
    borderRadius: RADIUS.sm,
    fontSize: FONT_SIZE.md,
    fontWeight: FONT_WEIGHT.medium,
    lineHeight: LINE_HEIGHT.snug,
    ...schemed(t, c => ({ color: c.textStrong })),
    ...focusRing(t),
    '&:hover': schemed(t, c => ({ backgroundColor: c.overlayHover, color: c.textStrong })),
  })
  /* The footer's bare icon buttons (Theme; collapsed, Account), the collapse toggle's shape. */
  const glyphButtonSx = (t: Theme) => ({
    flexShrink: 0,
    color: 'inherit',
    fontSize: SIZE.iconSm,
    padding: 0.5,
    borderRadius: RADIUS.sm,
    ...schemed(t, c => ({ color: c.textStrong })),
    ...focusRing(t),
    '&:hover': schemed(t, c => ({ backgroundColor: c.overlayHover, color: c.textStrong })),
    [ABOVE_PHONE]: { minWidth: 0, minHeight: 0 },
  })
  const sheetLinkSx = (t: Theme) => ({
    ...railRow(t),
    [PHONE]: { flex: '0 0 auto', justifyContent: 'flex-start', padding: `0 ${grid(2.5)}`, margin: 0, fontSize: FONT_SIZE.base },
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
        gap: 1,
        flex: '0 0 auto',
        margin: shape === 'sheet' ? `0 0 ${grid(1)}` : `${grid(0.5)} ${grid(1)} ${grid(1)}`,
        padding: 1.5,
        borderRadius: RADIUS.md,
        border: '1px solid',
        ...schemed(t, c => ({ borderColor: c.accent, backgroundColor: c.bannerBg, color: c.textStrong })),
        [PHONE]: shape === 'sheet' ? {} : { display: 'none' },
      })}
    >
      <Typography component="p" sx={{ m: 0, fontSize: FONT_SIZE.md, lineHeight: LINE_HEIGHT.snug, fontWeight: FONT_WEIGHT.medium }}>
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
      ref={asideRef}
      data-testid="sidebar"
      // State as an attribute: under Emotion the class name is a hash.
      data-collapsed={collapsed ? 'true' : 'false'}
      sx={(t: Theme) => ({
        width: collapsed ? t.custom.sidebar.widthCollapsed : (savedWidth ?? t.custom.sidebar.width),
        /* A laptop keeps the compact rail unless the dragged one is narrower: the page layouts' breakpoints
           (`ui/listStyles.ts`) were measured against the default rail. */
        ...(!collapsed && {
          [`@media (max-width:${t.custom.sidebar.compactBelowPx - 0.05}px)`]: {
            width: Math.min(savedWidth ?? Infinity, t.custom.sidebar.widthCompact),
          },
        }),
        flexShrink: 0,
        display: 'flex',
        flexDirection: 'column',
        // The edge strip's anchor.
        position: 'relative',
        // The rail must not be the scroll container: `min-height: 0` plus the nav's `overflow-y: auto` keeps
        // the footer pinned on a short window.
        minHeight: 0,
        boxShadow: 2,
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
          boxShadow: 3,
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
          gap: 0.5,
          padding: `${grid(1.5)} ${grid(1)} ${grid(1)} ${grid(2)}`,
          borderBottom: '1px solid',
          ...schemed(t, c => ({ borderColor: c.border })),
          ...(collapsed && {
            flexDirection: 'column',
            padding: `${grid(1.5)} 0 ${grid(1)}`,
            gap: 0.5,
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
            sx={(t: Theme) => ({ display: 'inline-flex', lineHeight: LINE_HEIGHT.tight, borderRadius: RADIUS.sm, '&:focus-visible': focusRing(t)['&.Mui-focusVisible'] })}
          >
            {collapsed
              ? <BrandMark size={SIZE.iconLg} />
              : <Box data-testid="sidebar-brand-text"><BrandWordmark height={SIZE.thumb} /></Box>}
          </Box>
        </Box>
        {!live && statusCluster}
        <IconButton
          data-testid="sidebar-toggle"
          onClick={toggle}
          aria-label={collapsed ? 'Expand sidebar' : 'Collapse sidebar'}
          sx={(t: Theme) => ({
            color: 'inherit',
            fontSize: FONT_SIZE.lg,
            lineHeight: LINE_HEIGHT.tight,
            padding: `0 ${grid(0.5)}`,
            borderRadius: RADIUS.sm,
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
            padding: collapsed ? `${grid(1)} 0` : `${grid(1)} ${grid(1)} ${grid(1)} ${grid(2)}`,
            borderBottom: '1px solid',
            ...schemed(t, c => ({ borderColor: c.border })),
          })}
        >
          <OrgScopeControl variant={collapsed ? 'collapsed' : 'rail'} />
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
          padding: `${grid(1)} 0`,
          display: 'flex',
          flexDirection: 'column',
          // A row scrolled into view by focus must land above the pinned destinations, not under them
          // (six rows and the divider, rounded up).
          [ABOVE_PHONE]: { scrollPaddingBottom: grid(32) },
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
            bottom: `-${grid(1)}`,
            paddingBottom: 1,
            display: 'flex',
            flexDirection: 'column',
            ...schemed(t, c => ({ backgroundColor: c.bg })),
            [PHONE]: { display: 'contents' },
          })}
        >
          {live && !collapsed && tryBanner('rail')}
          {scopeRows.length > 0 && destinationRows.length > 0 && (
            <Divider
              data-testid="sidebar-nav-sep"
              sx={(t: Theme) => ({
                margin: `${grid(1)} ${grid(2)}`,
                ...schemed(t, c => ({ borderColor: c.border })),
                [PHONE]: { display: 'none' },
              })}
            />
          )}
          {destinationRows.map(row => railItem(row))}
        </Box>
        <ListItemButton
          component="button"
          data-testid="nav-more-btn"
          onClick={() => { setTreeSheetOpen(false); setMoreOpen(o => !o) }}
          aria-expanded={moreOpen}
          aria-label={`More navigation${sheetCounts}${foldedGuide ? `, setup ${foldedGuide}` : ''}`}
          sx={(t: Theme) => ({
            display: 'none',
            [PHONE]: {
              display: 'flex',
              flex: 1,
              flexDirection: 'column',
              justifyContent: 'center',
              alignItems: 'center',
              gap: 0,
              minWidth: 0,
              // Same slot metrics as a nav item, so the toggle is not an odd width.
              margin: 0,
              padding: `${grid(0.5)} 0`,
              borderRadius: RADIUS.sm,
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
          <Typography component="span" data-testid="nav-abbr" sx={{ fontSize: SIZE.iconMd, display: 'inline-flex', position: 'relative' }}>
            <MoreHorizIcon />
            {/* Left of the mark: the folded Setup badge holds the slot's top-right corner. */}
            {sheetCounts && <StatusDot data-testid="nav-more-count-dot" tone="error" size={grid(1)} sx={{ position: 'absolute', top: 0, left: `-${grid(1)}` }} />}
          </Typography>
          {/* The onboarding bar folded into the navigation shows its count here; the sheet's Setup row restores it. */}
          <SetupNavBadge />
          <Typography component="span" data-testid="nav-label" sx={{ fontSize: FONT_SIZE.xxs }}>
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
            padding: `${grid(1)} ${grid(0.5)} calc(${grid(1)} + env(safe-area-inset-bottom, ${grid(0)}))`,
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
              maxHeight: `calc(100vh - ${grid(5)})`,
              overflowY: 'auto',
              py: 0.5,
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
          {sheetRows.map(row => railItem(row, { sheet: true }))}
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
              sx={(t: Theme) => ({ marginTop: 1, padding: 1, borderTop: '1px solid', ...schemed(t, c => ({ borderColor: c.border })) })}
            >
              {themeField}
            </Box>
          )}
          {/* The org switcher, above the account line. Its clicks stay inside it: accepting an invitation or
              typing a code must not close the sheet (a switch reloads the page anyway). */}
          {!live && <Box
            onClick={e => e.stopPropagation()}
            sx={(t: Theme) => ({ marginTop: 1, borderTop: '1px solid', ...schemed(t, c => ({ borderColor: c.border })) })}
          >
            {inviteUpsell}
            {OrgSwitcherList && isEnabled('multi_org_tenancy') && <OrgSwitcherList onNavigate={() => setMoreOpen(false)} />}
          </Box>}
          {/* A per-agent plan's agents throttle, above the account line (`TrialAgentsMeter.tsx`). */}
          {!live && TrialAgentsMeter && <TrialAgentsMeter shape="sheet" />}
          <Box
            data-testid="nav-more-sheet-footer"
            sx={(t: Theme) => ({
              display: 'flex',
              alignItems: 'center',
              justifyContent: 'space-between',
              /* Wraps: a long account line (a nameless account's email) takes its own line above the buttons. */
              flexWrap: 'wrap',
              gap: 1,
              marginTop: 1,
              padding: `${grid(1)} ${grid(1)} 0`,
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
                  fontSize: FONT_SIZE.md,
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
                sx={(t: Theme) => ({ fontSize: FONT_SIZE.sm, minWidth: 0, ...schemed(t, c => ({ color: c.textStrong })) })}
              >
                {APP_VERSION}
              </Typography>
            </Box>
            {/* Profile is never a nav row: it is a zero-grant account's only entry point, and no grant filter
                may drop it. A visitor has no account: its Theme sits above. */}
            {!live && <Box sx={{ display: 'flex', gap: 1, flexShrink: 0 }}>
              {accountSupport && <Button component={Link} to="/support" data-testid="sidebar-support-link" sx={accountButtonSx}>
                Support
              </Button>}
              {/* `askName`'s dot rides My Profile here, as on the rail's badge; the label carries the ask. */}
              <Button
                component={Link}
                to="/profile"
                data-testid="sidebar-profile-btn"
                aria-label={askName ? `My Profile. ${ADD_NAME}` : undefined}
                sx={[accountButtonSx, { position: 'relative' }]}
              >
                My Profile
                {askName && <StatusDot data-testid="sidebar-add-name-dot-mobile" tone="info" size={grid(1)} sx={{ position: 'absolute', top: grid(0.5), right: grid(0.5) }} />}
              </Button>
              <Button onClick={() => void logout()} data-testid="sidebar-signout-btn" sx={accountButtonSx}>
                Sign out
              </Button>
            </Box>}
          </Box>
        </Paper>
      )}
      {/* A per-agent plan's agents throttle, directly above the footer (`TrialAgentsMeter.tsx`); the phone's is in the More sheet. */}
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
          gap: 1,
          ...(live && { flexWrap: 'wrap' }),
          /* Right padding is small: Help's own button padding already insets the glyph. */
          padding: `${grid(1)} ${grid(0.5)} ${grid(1)} ${grid(1.5)}`,
          borderTop: '1px solid',
          ...schemed(t, c => ({ borderColor: c.border })),
          ...(collapsed && { flexDirection: 'column', gap: 0.5, padding: `${grid(1)} 0` }),
          [PHONE]: { display: 'none' },
        })}
      >
        {live && (
          <Tip title={collapsed ? `${accountMenu ? 'Account' : 'Theme'} · ${APP_VERSION}` : 'Theme'}><IconButton
            data-testid={accountMenu ? 'sidebar-account-btn' : 'sidebar-theme-btn'}
            onClick={openMenu}
            aria-haspopup="menu"
            aria-expanded={menuAnchor !== null}
            aria-controls={menuAnchor ? 'sidebar-user-menu' : undefined}
            aria-label={accountMenu ? 'Account' : 'Theme'}
            sx={glyphButtonSx}
          >
            {accountMenu ? <PersonIcon /> : <ContrastIcon />}
          </IconButton></Tip>
        )}
        {live && !collapsed && auth && [
          <Button key="login" component="a" href={auth.login} data-testid="sidebar-login-link" sx={loginSx}>
            Log in
          </Button>,
          <Button key="signup" component="a" href={auth.signup} data-testid="sidebar-signup-link" sx={signupSx}>
            Sign up
          </Button>,
        ]}
        {!live && <Tip title={collapsed ? `${fullName} · ${APP_VERSION}` : fullName}><ButtonBase
          data-testid="sidebar-user-badge"
          // Who is signed in, for a spec that must wait for identity to hydrate (the label shows a first name).
          data-user-email={admin?.email}
          onClick={openMenu}
          aria-haspopup="menu"
          aria-expanded={menuAnchor !== null}
          aria-controls={menuAnchor ? 'sidebar-user-menu' : undefined}
          aria-label={askName ? `Account menu for ${fullName}. ${ADD_NAME}` : `Account menu for ${fullName}`}
          sx={(t: Theme) => ({
            position: 'relative',
            display: 'flex',
            alignItems: 'center',
            gap: 1,
            minWidth: 0,
            padding: `0 ${grid(1)} 0 0`,
            borderRadius: RADIUS.round,
            ...schemed(t, c => ({ color: c.textStrong })),
            ...focusRing(t),
            '&:hover': schemed(t, c => ({ backgroundColor: c.overlayHover })),
            ...(collapsed && { padding: 0 }),
          })}
        >
          {/* A nameless account draws a neutral head-and-shoulders glyph, not its email's initials ("DE" for
              dev-owner@…) in a per-author hue: those would pass the email prefix off as a name. The
              button's label and title still carry the email. */}
          {nameless ? (
            <>
              <Box component="span" data-testid="sidebar-user-initials" data-nameless="true" aria-hidden="true" sx={userBadgeSx}>
                <PersonIcon size={SIZE.iconXs} />
              </Box>
              {/* `askName`'s dot on the badge, whose menu's My Profile reads "Add your name"; the button's label
                  carries the ask for a screen reader. */}
              {askName && <StatusDot data-testid="sidebar-add-name-dot" tone="info" size={grid(1)} sx={{ position: 'absolute', top: 0, left: 0 }} />}
            </>
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
                fontSize: FONT_SIZE.md,
                fontWeight: FONT_WEIGHT.medium,
                ...(nameless && { fontWeight: FONT_WEIGHT.regular, ...schemed(t, c => ({ color: c.text })) }),
              })}
            >
              {nameless ? admin?.email : firstName}
            </Typography>
          )}
        </ButtonBase></Tip>}
        {!collapsed && (
          /* From version.ts (build-time APP_VERSION), never a literal; `textStrong` at full opacity
             (`text` falls short of contrast at this size). */
          <Typography
            component="span"
            noWrap
            data-testid="sidebar-version"
            /* Never shrinks: a clipped version ("1.1…") is a wrong answer, and on the 192px rail the
               first name is what gives way — the badge beside it still identifies the user. */
            sx={(t: Theme) => ({
              marginLeft: 'auto',
              // Sits beside Help: pulls the footer's gap in to nothing. The viewer has no Help.
              marginRight: live ? 1 : -1,
              flexShrink: 0,
              fontSize: FONT_SIZE.sm,
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
          paper: { sx: { width: 'max-content', maxWidth: `min(${grid(56.5)}, calc(100vw - ${grid(5)}))`, maxHeight: `calc(100dvh - ${grid(5)})` } },
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
            sx={{ px: 2, pt: 0.5, pb: 0.5, fontSize: FONT_SIZE.md, color: 'text.secondary', overflowWrap: 'anywhere' }}
          >
            {admin?.email}
          </Box>,
          <Divider key="who-divider" />,
          ...(inviteUpsell ? [<Box key="invite" component="li" role="none">{inviteUpsell}</Box>] : []),
          ...(OrgSwitcherList && isEnabled('multi_org_tenancy') ? [<OrgSwitcherList key="orgs" muiSkipListHighlight onNavigate={() => setMenuAnchor(null)} />] : []),
          <Divider key="divider" />,
          ...(accountSupport ? [
            <MenuItem key="support" component={Link} to="/support" data-testid="sidebar-support-link" onClick={() => setMenuAnchor(null)}>
              Support
            </MenuItem>,
          ] : []),
          <MenuItem key="profile" component={Link} to="/profile" data-testid="sidebar-profile-btn" onClick={() => setMenuAnchor(null)}>
            My Profile
            {nameless && (
              <Typography component="span" data-testid="sidebar-profile-add-name" sx={{ ml: 1, fontSize: FONT_SIZE.sm, color: 'text.secondary' }}>
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
      {/* The draggable right edge (`lib/sidebarWidth.ts`): resize, or drag in to collapse as '‹' does. Not on the
          phone bar, which has no edge, nor the Live Cast viewer's rail. */}
      {!live && (
        <Box
          data-testid="sidebar-edge"
          sx={{ position: 'absolute', top: 0, bottom: 0, right: -DIVIDER_PX / 2, width: DIVIDER_PX, display: 'flex', zIndex: 1, [PHONE]: { display: 'none' } }}
        >
          <SplitDivider
            testId="sidebar-divider"
            ariaLabel="Resize sidebar"
            valueNow={collapsed ? 0 : (((savedWidth ?? theme.custom.sidebar.width) - SIDEBAR_MIN_PX) / (SIDEBAR_MAX_PX - SIDEBAR_MIN_PX)) * 100}
            edge={() => (asideRef.current?.getBoundingClientRect().right ?? 0) - DIVIDER_PX / 2}
            onMove={onEdgeMove}
            onReset={resetWidth}
          />
        </Box>
      )}
    </Box>
  )
}
