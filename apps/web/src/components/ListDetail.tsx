// THE LIST/DETAIL SPLIT — an editor opens as a resizable pane beside the page it was opened from, the way the
// web console docks beside the app. ONE mechanism for every list in the console; which paths are editors
// (and the list a cold load puts beside one) is `lib/listDetail.ts`'s table, the arithmetic is
// `lib/splitPane.ts`'s, and the divider and window buttons are the console's own (components/SplitDivider.tsx,
// components/PaneWindowButtons.tsx).
//
// ── HOW ONE URL RENDERS TWO PAGES ───────────────────────────────────────────────────────────────────
// The URL stays the editor's (`/tasks/41`). `useListDetailRouting` (once per shell: App.tsx, live/LiveApp.tsx) turns it
// into TWO locations:
//   main    the BACKGROUND: the page the operator opened the editor from (search included, so a list keeps
//           its filters) — an owner decision: a task opened from Audit > Sessions keeps Sessions
//           beside it. A cold load (deep link, reload) has no such page, so it gets the editor's own list.
//           Moving between editors keeps the background. The shell's `<Routes>` renders THIS, so the page is
//           the same element before and after an editor opens: never remounted, scroll and rows kept.
//   detail  the real location, rendered a second time into the pane by `DetailRoutes`.
// Below `DOCK_MIN_VIEWPORT` (where the console stops docking too) there is no split: `main` IS the real
// location and the editor is the full page it always was. Crossing that width with an editor open moves the
// editor between the pane and `<main>`, which remounts it; an editor carries its unsaved input across with
// `hooks/useRemountDraft.ts`, and the task page its landed read (`lib/taskPageSeed.ts::takeLandedTaskRead`) —
// keeping one mount would need the editor outside both trees.
//
// A PAGE THE VIEWER MAY NOT OPEN IS NEVER FORCED ON THEM: a route guard that would redirect in the
// background reports it instead (`useListBackground`), and the editor falls back to a full page.
//
// RESTS ON react-router 6's `UNSAFE_RouteContext` and `UNSAFE_NavigationContext` (6.30 at writing): the pane's
// second route table and the background's in-place moves. A router major upgrade re-verifies both here.
import {
  Suspense,
  createContext,
  useCallback,
  useContext,
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from 'react'
import {
  Routes,
  UNSAFE_NavigationContext,
  UNSAFE_RouteContext,
  parsePath,
  useLocation,
  useNavigate,
  type Location,
  type Navigator,
  type Path,
  type To,
} from 'react-router-dom'
import Box from '@mui/material/Box'
import type { SystemStyleObject } from '@mui/system'
import type { Theme } from '@mui/material/styles'
import ErrorBoundary from './ErrorBoundary'
import { canDock } from '../lib/consoleDock'
import {
  DEFAULT_DETAIL_RATIO,
  canSplit,
  detailRatioFromDividerX,
  detailWidthFor,
  listForDetail,
  loadDetailPaneState,
  safeReturnPath,
  saveDetailPaneState,
  type DetailPaneState,
} from '../lib/listDetail'
import { DIVIDER_PX, SPLIT_INSET_PX } from '../lib/splitPane'
import {
  BAR_SEAT_ATTR,
  LAUNCHER_CLEARANCE_SX,
  LAUNCHER_SEAT_FLUSH_PX,
  LAUNCHER_SEAT_VAR,
  useBarSeat,
} from '../ui/launcherSeat'
import { normalizePathname, pageKey } from '../routeGroups'
import { usePersistedState } from '../hooks/usePersistedState'
import { useViewportIs } from '../hooks/useViewportWidth'
import { ACT_AS_OVERLAP_VAR, MAIN_END_INSET_VAR } from './ActAsBar'
import { CloseButton, MaximizeButton } from './PaneWindowButtons'
import SplitDivider from './SplitDivider'

export type ListDetailRouting = {
  /** What the shell's `<Routes>` renders: the background while an editor is split beside it, else the URL. */
  mainLocation: Location
  /** The editor in the pane, or `null` when nothing is split. */
  detailLocation: Location | null
  /** The background page, where Close goes back to — as the operator left it. `null` when nothing is split. */
  listLocation: Location | null
  /** The background's guard refused the viewer: stop splitting over that page for this session. */
  denyList: (listPath: string) => void
  /** The background moved within its own address (a filter, a normalized query): it moves IN PLACE, and
   *  the URL keeps the editor (`ListBackground`). */
  moveList: (to: Path, state: unknown) => void
  /** The pane's persisted layout: its share of the room, and Maximize. Here, not in the pane, because the
   *  console's floor reads it too (a maximized editor needs no room for the list). */
  pane: DetailPaneState
  setPane: (update: (prev: DetailPaneState) => DetailPaneState) => void
}

const ListDetailContext = createContext<ListDetailRouting | null>(null)
/* THE OPEN EDITOR'S PATHNAME, ON ITS OWN: every list row reads it, and a string context re-renders them only
   when the open editor changes — not on every background move or pane drag. */
const OpenDetailPathContext = createContext<string | null>(null)

/** Provides the split, and the open editor's pathname for highlighting. */
export function ListDetailProvider({ value, children }: { value: ListDetailRouting; children: ReactNode }) {
  const open = value.detailLocation ? normalizePathname(value.detailLocation.pathname) : null
  return (
    <ListDetailContext.Provider value={value}>
      <OpenDetailPathContext.Provider value={open}>{children}</OpenDetailPathContext.Provider>
    </ListDetailContext.Provider>
  )
}

/** The split, or `null` outside the shell (isolated page tests). */
export function useListDetail(): ListDetailRouting | null {
  return useContext(ListDetailContext)
}

/** The editor open in the pane, as a normalized pathname, or `null` — what a list highlights (compare with
 *  `lib/listDetail.ts`'s `isOpenPath`). */
export function useOpenDetailPath(): string | null {
  return useContext(OpenDetailPathContext)
}

/* ── The wide request ──────────────────────────────────────────────────────────────────────────────
   A detail page whose body is a list of long rows (Project › Memory) asks, while it is mounted, for the widest
   split: the list keeps its floor and the editor takes the rest. Never persisted; the operator's own drag or
   reset of the divider ends it. */
const WidePaneContext = createContext<((wide: boolean) => void) | null>(null)

/** Inside the pane: widen it while the caller is mounted. A no-op outside a split (full page, Maximize). */
export function useWideDetailPane(): void {
  const request = useContext(WidePaneContext)
  useEffect(() => {
    if (!request) return
    request(true)
    return () => request(false)
  }, [request])
}

/* ── The background marker ──────────────────────────────────────────────────────────────────────────
   Provided around the main outlet only while it renders a page BEHIND an open editor. A guard (or a page
   that redirects on arrival) asks for it: present, it must not navigate — a redirect from the background
   would replace the editor the operator opened — and reports the refusal instead. */
const ListBackgroundContext = createContext<(() => void) | null>(null)

/** Inside the background page: the call that withdraws the split. `null` everywhere else. */
export function useListBackground(): (() => void) | null {
  return useContext(ListBackgroundContext)
}

/** Rendered by a guard in place of its redirect while it sits behind an open editor. */
export function DenyListBackground({ deny }: { deny: () => void }) {
  // An effect, never during render: it sets the routing state of the component above.
  useEffect(() => deny(), [deny])
  return null
}

/** One-shot location state is a message to the page's ARRIVAL ("focus the composer"); it must not replay
 *  when that page becomes a background. */
function asBackground(location: Location): Location {
  return { ...location, pathname: normalizePathname(location.pathname), state: null }
}

/**
 * Turn the URL into the main and detail locations. Called ONCE per shell (App.tsx, live/LiveApp.tsx), above its `<Routes>`.
 */
export function useListDetailRouting(): ListDetailRouting {
  const location = useLocation()
  const wide = useViewportIs(canDock)
  const [denied, setDenied] = useState<ReadonlySet<string>>(() => new Set())
  const [pane, setPaneState] = usePersistedState(loadDetailPaneState, saveDetailPaneState)
  /* `background` is a ref read during render; an in-place move writes it and needs a render to be seen. */
  const [, setMoves] = useState(0)

  /* THE TWO MEMORIES, written DURING RENDER so the very render that opens an editor already knows where it
     came from (an effect would land a commit late). Idempotent per location key: StrictMode's second render
     changes nothing. `lastPage` is the last non-editor page shown; `background` is what an open editor sits
     beside, kept while the operator moves between editors and dropped on leaving them. */
  const lastPage = useRef<Location | null>(null)
  const background = useRef<Location | null>(null)
  const pathname = normalizePathname(location.pathname)
  const coldList = listForDetail(pathname)
  if (coldList === null) {
    if (lastPage.current?.key !== location.key) lastPage.current = asBackground(location)
    background.current = null
  } else if (background.current === null) {
    // A list may carry its tab (`/products?tab=plans`): the search half rides along.
    const [listPath, listSearch = ''] = coldList.split('?')
    background.current = lastPage.current ?? {
      pathname: listPath,
      search: listSearch ? `?${listSearch}` : '',
      hash: '',
      state: null,
      key: `cold:${coldList}`,
    }
  }

  const behind = background.current
  const split = wide && behind !== null && !denied.has(behind.pathname)
  const listLocation = split ? behind : null

  const denyList = useCallback((path: string) => {
    setDenied(prev => (prev.has(path) ? prev : new Set([...prev, path])))
  }, [])

  const moveList = useCallback((to: Path, state: unknown) => {
    const current = background.current
    if (current === null) return
    background.current = { ...current, search: to.search, hash: to.hash, state, key: `moved:${Date.now()}` }
    setMoves(n => n + 1)
  }, [])

  const setPane = useCallback(
    (update: (prev: DetailPaneState) => DetailPaneState) => setPaneState(update),
    [setPaneState],
  )

  return useMemo(
    () => ({
      mainLocation: listLocation ?? location,
      detailLocation: listLocation ? location : null,
      listLocation,
      denyList,
      moveList,
      pane,
      setPane,
    }),
    [listLocation, location, denyList, moveList, pane, setPane],
  )
}

/* The router's ROOT context, for the pane's second `<Routes>`: it renders the shell's route table from
   the top again, and must not read as nested under the background's routes (react-router would match it
   against the background's path and warn). The value is the shape `<RouterProvider>`-less routers start from. */
const ROOT_ROUTE_CONTEXT = { outlet: null, matches: [], isDataRoute: false }

/** The shell's route table, rendered for the editor in the pane. */
export function DetailRoutes({ location, children }: { location: Location; children: ReactNode }) {
  return (
    <UNSAFE_RouteContext.Provider value={ROOT_ROUTE_CONTEXT}>
      <Routes location={location}>{children}</Routes>
    </UNSAFE_RouteContext.Provider>
  )
}

/** The workspace's measured width: the room left of the console. */
function useMeasuredWidth() {
  const ref = useRef<HTMLDivElement | null>(null)
  const [width, setWidth] = useState(0)
  // A layout effect, so the first paint already has the split (no list-hidden flash).
  useLayoutEffect(() => {
    const el = ref.current
    if (!el) return
    const measure = () => setWidth(el.getBoundingClientRect().width)
    measure()
    if (typeof ResizeObserver === 'undefined') return
    const observer = new ResizeObserver(measure)
    observer.observe(el)
    return () => observer.disconnect()
  }, [])
  return { ref, width }
}

/**
 * THE ROOM LEFT OF THE CONSOLE: the app's `<main>` (the background page) and, while an editor is open, the
 * divider and the editor's pane. `<main>` is ALWAYS the first child, so opening or closing an editor never
 * remounts the page behind it. `renderMain` learns whether that page is out of sight (Maximize, or too
 * little room).
 */
export function DetailWorkspace({
  renderMain,
  detail,
}: {
  renderMain: (listHidden: boolean) => ReactNode
  /** The editor's page, already wrapped for loading and errors; `null` = nothing split. */
  detail: ReactNode | null
}) {
  const routing = useListDetail()
  const navigate = useNavigate()
  const { ref, width: roomWidth } = useMeasuredWidth()
  const { ratio, maximized } = routing?.pane ?? { ratio: DEFAULT_DETAIL_RATIO, maximized: false }
  const setPane = routing?.setPane

  const open = detail !== null
  const roomy = canSplit(roomWidth)
  const listHidden = open && (maximized || !roomy)
  const [wide, setWide] = useState(false)
  // A ratio of 1 clamps to the list's floor: the widest split.
  const width = detailWidthFor(wide ? 1 : ratio, roomWidth)

  const listLocation = routing?.listLocation ?? null
  /* A pane opened from inside another pane names its opener in `?from=` (lib/listDetail.ts::safeReturnPath):
     Close goes back there, else to the background list. */
  const detailSearch = routing?.detailLocation?.search ?? ''
  const close = useCallback(() => {
    const from = safeReturnPath(detailSearch, window.location.origin)
    if (from !== null) navigate(from)
    else if (listLocation) navigate({ pathname: listLocation.pathname, search: listLocation.search, hash: listLocation.hash })
  }, [navigate, listLocation, detailSearch])

  /* The room's right edge read AT the move: an observer reports resizes, not moves (a rail collapsing while
     the console widens shifts the room without resizing it). */
  const onDividerMove = useCallback(
    (clientX: number, grabOffset: number) => {
      const rect = ref.current?.getBoundingClientRect()
      if (!rect || !setPane) return
      setWide(false)
      setPane(prev => ({ ...prev, ratio: detailRatioFromDividerX(clientX, rect.right, rect.width, grabOffset) }))
    },
    [ref, setPane],
  )

  /* FOCUS FOLLOWS THE OPENED RECORD: a screen reader learns the pane appeared, and the keyboard is in the
     editor. Once per editor opened (keyed on the pathname), never on a re-render; no scroll jump. */
  const paneRef = useRef<HTMLElement | null>(null)
  const openPath = routing?.detailLocation?.pathname ?? null
  useEffect(() => {
    if (openPath !== null) paneRef.current?.focus({ preventScroll: true })
  }, [openPath])

  const paneBodyRef = useRef<HTMLDivElement | null>(null)
  useBarSeat(paneBodyRef, open)

  return (
    <Box ref={ref} data-testid="detail-workspace" sx={{ display: 'flex', flex: 1, minWidth: 0, height: '100%' }}>
      {renderMain(listHidden)}
      {open && !listHidden && setPane && (
        <SplitDivider
          testId="detail-pane-divider"
          ariaLabel="Resize pane"
          valueNow={(width / Math.max(roomWidth - DIVIDER_PX, 1)) * 100}
          edge={() => (ref.current?.getBoundingClientRect().right ?? 0) - width - DIVIDER_PX}
          onMove={onDividerMove}
          onReset={() => {
            setWide(false)
            setPane(prev => ({ ...prev, ratio: DEFAULT_DETAIL_RATIO }))
          }}
        />
      )}
      {open && (
        <Box
          ref={paneRef}
          component="section"
          aria-label="Details"
          tabIndex={-1}
          data-testid="detail-pane"
          data-view={listHidden ? 'maximized' : 'split'}
          sx={{
            ...(listHidden ? { flex: 1 } : { width, flexShrink: 0 }),
            minWidth: 0,
            height: '100%',
            display: 'flex',
            flexDirection: 'column',
            bgcolor: 'background.default',
            '&:focus': { outline: 'none' },
          }}
        >
          <Box
            data-testid="detail-pane-header"
            sx={{ display: 'flex', justifyContent: 'flex-end', gap: 0.5, px: 1, pt: 0.5, flexShrink: 0 }}
          >
            {/* Restore needs room for both panes; without it there is nothing to restore to. */}
            {roomy && setPane && (
              <MaximizeButton
                noun="pane"
                maximized={maximized}
                onToggle={() => setPane(prev => ({ ...prev, maximized: !prev.maximized }))}
              />
            )}
            <CloseButton noun="pane" onClose={close} />
          </Box>
          {/* THE PANE'S SCROLLPORT, with `.app-main`'s insets — but only `SPLIT_INSET_PX` at the sides beside the list
              (the divider and the window edge already bound it; not flush, so nothing is clipped;
              maximized, it is a full page again): a page sizes itself against this box exactly as against
              `.app-main` (App.tsx's shell layout contract) — `height: 100%` resolves here. */}
          <Box
            ref={paneBodyRef}
            data-testid="detail-pane-body"
            sx={{
              flex: 1, minHeight: 0, overflow: 'auto', px: listHidden ? '0.5rem' : `${SPLIT_INSET_PX}px`, pb: '1.75rem', pt: 0.5,
              ...LAUNCHER_CLEARANCE_SX,
              // The bar keeps the launcher's seat only in the launcher's row; mid-pane Save keeps the content's right edge.
              [`&:not([${BAR_SEAT_ATTR}])`]: { [LAUNCHER_SEAT_VAR]: '0px' },
              ...(!listHidden && {
                [`&[${BAR_SEAT_ATTR}]`]: { [LAUNCHER_SEAT_VAR]: `${LAUNCHER_SEAT_FLUSH_PX - SPLIT_INSET_PX}px` },
              }),
              // The act-as bar sticks in `.app-main`, never here: a sticky strip in the pane takes no offset.
              [ACT_AS_OVERLAP_VAR]: '0px',
            }}
          >
            <WidePaneContext.Provider value={setWide}>{detail}</WidePaneContext.Provider>
          </Box>
        </Box>
      )}
    </Box>
  )
}

/**
 * Around the main outlet: marks it as the background of an open editor (see `useListBackground`), and
 * keeps the page's own navigations from closing that editor. A list rewrites its address all the time — a
 * filter, a scope, a query normalized after load — and from behind an editor each of those would replace
 * the editor's URL. So a push or replace to the BACKGROUND'S OWN pathname moves it in place (`moveList`,
 * state carried); anything else (a row's link, another page) navigates as ever. In place is not history:
 * Back from the editor returns to the page as it was when the editor opened. The Providers stay mounted
 * whether or not an editor is open, so toggling one never remounts the page.
 */
export function ListBackground({ active, children }: { active: boolean; children: ReactNode }) {
  const routing = useListDetail()
  const listPath = routing?.listLocation?.pathname ?? null
  const denyList = routing?.denyList
  const moveList = routing?.moveList
  const deny = useCallback(() => {
    if (listPath !== null && denyList) denyList(listPath)
  }, [listPath, denyList])

  const parent = useContext(UNSAFE_NavigationContext)
  const navigation = useMemo(() => {
    if (!active || listPath === null || !moveList) return parent
    /* `useNavigate` hands the navigator a path WITH the router's basename (the Live Cast viewer mounts a project
       under `/:org/:slug`); the background's pathname is without it. */
    const basename = parent.basename === '/' ? '' : parent.basename.replace(/\/$/, '')
    const inRouter = (pathname: string) => (pathname === basename || pathname.startsWith(`${basename}/`) ? pathname.slice(basename.length) || '/' : pathname)
    const inPlace = (to: To, state: unknown): boolean => {
      const path = typeof to === 'string' ? parsePath(to) : to
      if (path.pathname === undefined || normalizePathname(inRouter(path.pathname)) !== listPath) return false
      moveList({ pathname: listPath, search: path.search ?? '', hash: path.hash ?? '' }, state ?? null)
      return true
    }
    /* A prototype child, so `createHref`, `go` and `encodeLocation` stay the router's own. */
    const navigator: Navigator = Object.create(parent.navigator)
    navigator.push = (to, state, opts) => {
      if (!inPlace(to, state)) parent.navigator.push(to, state, opts)
    }
    navigator.replace = (to, state, opts) => {
      if (!inPlace(to, state)) parent.navigator.replace(to, state, opts)
    }
    return { ...parent, navigator }
  }, [active, listPath, moveList, parent])

  return (
    <ListBackgroundContext.Provider value={active ? deny : null}>
      <UNSAFE_NavigationContext.Provider value={navigation}>{children}</UNSAFE_NavigationContext.Provider>
    </ListBackgroundContext.Provider>
  )
}

/** `<main>` beside an open editor or a docked console: only `SPLIT_INSET_PX` on the right, published for full-bleed
 *  bars (`ActAsBar`). */
export const SPLIT_MAIN_SX = { paddingRight: `${SPLIT_INSET_PX}px`, [MAIN_END_INSET_VAR]: `${SPLIT_INSET_PX}px` }

/**
 * THE SHELL'S WORKSPACE: the page in `<main>` and, while an editor is split beside it, that editor in the pane —
 * each behind its own Suspense and error boundary, the page as the background (`ListBackground`). ONE composition
 * for every shell hosting the split: the console's (App.tsx) and the Live Cast viewer's (live/LiveApp.tsx), so a
 * task opens beside its list the same way in both. The caller brings what differs: its route table, the page
 * outlet, `<main>`'s insets and what sits above the page.
 *
 * Both boundaries sit INSIDE the shell, so its Sidebar survives a page crash (Suspense outside them, for the same
 * reason). They are KEYED ON THE PAGE (`pageKey`) plus `scopeKey`, not the pathname: two shells put their TAB in the
 * path, and a pathname key rebuilt the page on every tab click; they reset on the raw pathname, so a caught error
 * clears on any real navigation (components/ErrorBoundary.tsx). `ready` false holds both panes on `fallback`.
 *
 * The key sits on the Suspense, which remounts its error boundary with it, because navigation runs in a transition (`v7_startTransition`), and a lazy page that
 * suspends under an already-shown boundary makes React keep the OLD page up until its chunk lands: a project row
 * clicked below the split width left the list on screen under the new URL for a second. A boundary mounted for the
 * new page shows `fallback` at once instead; a tab click inside one page keeps its boundary, so nothing flashes.
 */
export function ListDetailShell({
  routes,
  page,
  mainSx,
  mainTop,
  fallback,
  detailFallback = fallback,
  ready = true,
  scopeKey = '',
}: {
  /** The route table the pane renders the editor from — the same one the page was matched against. */
  routes: ReactNode
  /** The page itself: the shell's `<Outlet />`, or `<Routes location={mainLocation}>`. */
  page: ReactNode
  mainSx: SystemStyleObject<Theme>
  /** Above every page, in flow inside `<main>`. */
  mainTop?: ReactNode
  fallback: ReactNode
  /** The pane's own fallback, when its editor has a skeleton of its own (the task page's); else `fallback`. */
  detailFallback?: ReactNode
  ready?: boolean
  scopeKey?: string
}) {
  const routing = useListDetail()
  const location = useLocation()
  /* The page's own pathname: the background's while an editor is split (the URL is then the editor's). */
  const pathname = routing?.mainLocation.pathname ?? location.pathname
  const detailLocation = routing?.detailLocation ?? null
  return (
    <DetailWorkspace
      detail={detailLocation && (
        <Suspense key={`${pageKey(detailLocation.pathname)}|${scopeKey}`} fallback={detailFallback}>
          <ErrorBoundary resetOn={detailLocation.pathname} label="this pane">
            {ready ? <DetailRoutes location={detailLocation}>{routes}</DetailRoutes> : detailFallback}
          </ErrorBoundary>
        </Suspense>
      )}
      renderMain={listHidden => (
        /* Hidden, never unmounted, while the editor is maximized: the list keeps its state. Beside an open
           editor the list runs near the divider: `SPLIT_INSET_PX` on the right, published for full-bleed bars. */
        <Box
          component="main"
          data-testid="app-main"
          sx={[mainSx, detailLocation !== null && SPLIT_MAIN_SX, listHidden && { display: 'none' }]}
        >
          {mainTop}
          <Suspense key={`${pageKey(pathname)}|${scopeKey}`} fallback={fallback}>
            <ErrorBoundary resetOn={pathname} label="this page">
              <ListBackground active={detailLocation !== null}>
                {ready ? page : fallback}
              </ListBackground>
            </ErrorBoundary>
          </Suspense>
        </Box>
      )}
    />
  )
}
