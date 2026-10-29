// THE WEB CONSOLE'S HOME IN THE SHELL. Hoisted to `AuthLayout` (kept mounted across navigation) so the
// terminal and its socket survive route changes; pages only say WHICH slot to open. The lazy chunk, the
// fallback scrim, the live title lookup, the view mode and the split are single-homed here.
//
// ── THE FOUR PRESENTATIONS ─────────────────────────────────────────────────────────────────────────
//   docked      default on a wide viewport: an IN-FLOW flex child of `.app-layout` right of `<main>`, with a
//               draggable divider. No scrim — the app stays usable.
//   floating    a free window, dragged/resized by its header/grip. Modeless: no scrim, no scroll lock.
//   maximized   the whole viewport; Restore returns to `docked`.
//   modal       BELOW `DOCK_MIN_VIEWPORT`: scrim, scroll lock, edge-to-edge (a half-width terminal at 700px is
//               ~40 columns).
//
// ── ONE MOUNT, FOUR PRESENTATIONS ──────────────────────────────────────────────────────────────────
// `<ConsoleTerminal>` is rendered from EXACTLY ONE place, never swapped between branches: React reconciles by
// position, so a branch swap would remount it (socket, xterm, scrollback) on every dock/undock. The
// presentation reaches it as a PROP; wrappers change their own CSS.
import {
  Suspense,
  createContext,
  lazy,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  useSyncExternalStore,
} from 'react'
import { useQueryClient } from '@tanstack/react-query'
import Box from '@mui/material/Box'
import { useTheme } from '@mui/material/styles'
import {
  DEFAULT_RATIO,
  DEFAULT_SPLIT_RATIO,
  DIVIDER_PX,
  MAIN_MIN_PX,
  canDock,
  consoleWidthFor,
  loadDockState,
  loadSplitRatio,
  ratioFromDividerX,
  saveDockState,
  saveSplitRatio,
  type ConsolePresentation,
  type ConsoleTarget,
  type ConsoleView,
} from '../lib/consoleDock'
import { resolveTitle } from '../lib/worktree'
import { SPLIT_MIN_PX } from '../lib/listDetail'
import { useListDetail } from './ListDetail'
import { usePersistedState } from '../hooks/usePersistedState'
import { WORKTREES_KEY } from '../hooks/useRosters'
import SplitDivider from './SplitDivider'
import { useViewportWidth } from '../hooks/useViewportWidth'
import type { Worktree } from '../types'
import { useConsoleRuntime } from './console/consoleRuntime'

const ConsoleTerminal = lazy(() => import('./ConsoleTerminal'))

/** What a page hands over when it opens a console.
 *  `mayEdit` is REQUIRED — the server's per-row `may_edit` verdict; fail-closed (see ConsoleTerminal.tsx): a
 *  caller must not be able to forget it.
 *  `hadLiveSession` is OPTIONAL: the opener's `lib/worktree.ts::hasLiveSession` answer, used only to word a
 *  4502 close (`ConsolePane.tsx`'s SLOT_EMPTY_REASON is the single home). Omitted = prior behaviour; forcing a
 *  boolean would force a guess. */
export type ConsoleRequest = ConsoleTarget & { mayEdit: boolean; title?: string; hadLiveSession?: boolean }

/** The chevron pair, registered by the page that owns the slot order (only the Worktrees table). */
export type ConsoleNav = {
  onPrev: () => void
  onNext: () => void
  hasPrev: boolean
  hasNext: boolean
}

type ConsoleDockValue = {
  request: ConsoleRequest | null
  open: (request: ConsoleRequest) => void
  close: () => void
  setNav: (nav: ConsoleNav | null) => void
  /** A console is open AND docked in flow: `<main>` beside it keeps only `SPLIT_INSET_PX` on that side (App.tsx). */
  docked: boolean
}

const ConsoleDockContext = createContext<ConsoleDockValue | null>(null)
/* Its own context: the open console's shown body changes often, and only a few readers care. */
const ConsoleChatTaskContext = createContext<number | null>(null)

/** The pages' whole API: `open(...)` and `close()`. A no-op without a provider (isolated page tests). */
export function useConsoleDock(): ConsoleDockValue {
  return useContext(ConsoleDockContext) ?? FALLBACK_VALUE
}

const FALLBACK_VALUE: ConsoleDockValue = {
  request: null,
  open: () => {},
  close: () => {},
  setNav: () => {},
  docked: false,
}

/** The task whose Chat view the open console is showing; null when closed, on the terminal, or without a
 *  provider. Lets a page tell whether its task's chat is already on screen. */
export function useConsoleChatTask(): number | null {
  return useContext(ConsoleChatTaskContext)
}

/** Registered while the page is mounted; withdrawn on unmount so chevrons never walk an unseen list. */
export function useConsoleNav(nav: ConsoleNav | null): void {
  const { setNav } = useConsoleDock()
  // Depend on the four values, not the per-render nav object.
  const { onPrev, onNext, hasPrev, hasNext } = nav ?? {}
  useEffect(() => {
    if (!onPrev || !onNext) {
      setNav(null)
      return
    }
    setNav({ onPrev, onNext, hasPrev: !!hasPrev, hasNext: !!hasNext })
    return () => setNav(null)
  }, [setNav, onPrev, onNext, hasPrev, hasNext])
}

/**
 * Holds the open console and its layout. Mounted ONCE, in `AuthLayout` — see App.tsx.
 * `request` is ephemeral (a reload never silently re-dials a node); `ratio` and `view` are persisted
 * preferences. The workspace shape survives F5, the session does not.
 */
export function ConsoleDockProvider({ children }: { children: React.ReactNode }) {
  const [request, setRequest] = useState<ConsoleRequest | null>(null)
  const [nav, setNav] = useState<ConsoleNav | null>(null)
  // Reported up by the mounted ConsoleTerminal; it reports null on unmount, so a closed console reads null.
  const [chatTask, setChatTask] = useState<number | null>(null)
  const [{ ratio: twoPaneRatio, view }, setDock] = usePersistedState(loadDockState, saveDockState)
  /* WHILE AN EDITOR IS SPLIT BESIDE A PAGE (components/ListDetail.tsx) the console is one of THREE panes and
     keeps a ratio of its own (a third by default), so the page and editor are not pinned to their floors by
     a console sized for two. Each arrangement remembers its own width. A MAXIMIZED editor hides the page, so
     the console is back to two panes. */
  const listDetail = useListDetail()
  const threePanes = listDetail?.detailLocation != null && !listDetail.pane.maximized
  const [splitRatio, setSplitRatio] = usePersistedState(loadSplitRatio, saveSplitRatio)
  const ratio = threePanes ? splitRatio : twoPaneRatio

  const open = useCallback((next: ConsoleRequest) => {
    // ONE CONSOLE AT A TIME, by replacement: the socket closes, but the node's tmux session keeps running, so
    // reopening the slot reattaches where it was. Copied, so re-opening with the same object is still a new
    // ask (`body` re-applies).
    setRequest({ ...next })
  }, [])

  const close = useCallback(() => setRequest(null), [])

  const setView = useCallback((view: ConsoleView) => setDock((prev) => ({ ...prev, view })), [setDock])
  const setRatio = useCallback((ratio: number) => {
    if (threePanes) setSplitRatio(ratio)
    else setDock((prev) => ({ ...prev, ratio }))
  }, [threePanes, setDock, setSplitRatio])

  // The host's own `docked` test (ConsoleDockHost), read here for the pages beside it.
  const wide = canDock(useViewportWidth())
  const docked = request !== null && view === 'docked' && wide

  const value = useMemo<ConsoleDockValue>(
    () => ({ request, open, close, setNav, docked }),
    [request, open, close, docked],
  )

  return (
    <ConsoleDockContext.Provider value={value}>
      <ConsoleChatTaskContext.Provider value={request ? chatTask : null}>
        {children}
        <ConsoleDockHost
          request={request}
          nav={nav}
          ratio={ratio}
          threePanes={threePanes}
          view={view}
          onClose={close}
          onSetView={setView}
          onSetRatio={setRatio}
          onChatTask={setChatTask}
        />
      </ConsoleChatTaskContext.Provider>
    </ConsoleDockContext.Provider>
  )
}

/* ── The host ───────────────────────────────────────────────────────────────────────────────────────
   The LAST child of `.app-layout`'s flex row: docked, the divider and panel are in-flow siblings of `<main>`
   and take width from it; otherwise both are `position: fixed` with no flow space. */
function ConsoleDockHost({
  request,
  nav,
  ratio,
  threePanes,
  view,
  onClose,
  onSetView,
  onSetRatio,
  onChatTask,
}: {
  request: ConsoleRequest | null
  nav: ConsoleNav | null
  ratio: number
  threePanes: boolean
  view: ConsoleView
  onClose: () => void
  onSetView: (view: ConsoleView) => void
  onSetRatio: (ratio: number) => void
  onChatTask: (taskId: number | null) => void
}) {
  const theme = useTheme()
  const viewportWidth = useViewportWidth()
  const wide = canDock(viewportWidth)
  const presentation: ConsolePresentation = wide ? view : 'modal'
  const docked = presentation === 'docked'

  /* Measured off the rendered rail, not its collapsed flag (Sidebar-owned state; a second reader could
     disagree). Falls back to the expanded token when the rail is absent (≤480px, never docked). */
  const sidebarWidth = useSidebarWidth(theme.custom.sidebar.width)

  /* Three panes raise the app's floor to list + editor, so the console narrows instead of the editor hiding
     the list. */
  const mainMin = threePanes ? SPLIT_MIN_PX : MAIN_MIN_PX
  const width = consoleWidthFor(ratio, viewportWidth, sidebarWidth, mainMin)

  const onDividerMove = useCallback(
    (clientX: number, grabOffset: number) =>
      onSetRatio(ratioFromDividerX(clientX, window.innerWidth, sidebarWidth, grabOffset, mainMin)),
    [onSetRatio, sidebarWidth, mainMin],
  )

  /* NOTHING BELOW MOUNTS UNTIL A CONSOLE IS OPEN — a requirement: the panel subscribes to react-query, and
     the shell must not make a QueryClient a hard dependency of every route. */
  if (!request) return null

  return (
    <OpenConsole
      request={request}
      nav={nav}
      presentation={presentation}
      docked={docked}
      width={width}
      ratio={ratio}
      /* What the divider's reset returns to: each arrangement's own default. */
      defaultRatio={threePanes ? DEFAULT_SPLIT_RATIO : DEFAULT_RATIO}
      onClose={onClose}
      onSetView={onSetView}
      onSetRatio={onSetRatio}
      onDividerMove={onDividerMove}
      onChatTask={onChatTask}
    />
  )
}

/**
 * SUBSCRIBE to the `['worktrees']` cache without ever fetching it: `GET /api/worktrees` needs
 * `worktrees:view`, which a Tasks-board console opener may lack. The owners (a `useQuery` on this key) are
 * exactly two — the node's Worktrees tab (components/WorktreePool.tsx) and Projects — and neither may be
 * mounted, hence `isLive`.
 * Not a `useQuery`: a `skipToken` observer does NOT re-render on `setQueryData` (react-query 5.101), and a
 * decoy `queryFn` could hijack an owner's refetch. ConsoleDock.test.tsx's "turns a read-only console
 * writable when the row it was waiting for arrives" guards this. `useSyncExternalStore` over the cache
 * subscribes, reads, never owns.
 */
function useWorktreesCache(): { rows: Worktree[] | undefined; isLive: boolean } {
  const client = useQueryClient()
  /* THE SNAPSHOT IS DERIVED FROM THE FACTS READ, never a bump-per-event counter: owner pages emit
     `observerOptionsUpdated` on every render, so a counter looks like a torn store and React restarts the
     render forever (seconds of dead screen). The composed string moves only when data/error writes or
     observer count/staleness change.
     `dataUpdatedAt` alone would NOT do (same-millisecond writes collide and could drop a `may_edit`
     revocation); `dataUpdateCount`/`errorUpdateCount` are monotonic.
     Loop-freedom needs a clock-free `getSnapshot`: `Query.isStale()` reads memoized state (not
     `isStaleByTime()`). Re-verify on a react-query upgrade. */
  const snapshot = useCallback(() => {
    const found = client.getQueryCache().find<Worktree[]>({ queryKey: WORKTREES_KEY })
    if (!found) return 'absent'
    const { dataUpdatedAt, dataUpdateCount, errorUpdateCount } = found.state
    return `${dataUpdatedAt}|${dataUpdateCount}|${errorUpdateCount}|${found.getObserversCount()}|${found.isStale() ? 1 : 0}`
  }, [client])
  const subscribe = useCallback(
    (onChange: () => void) => client.getQueryCache().subscribe((event) => {
      if (event.query.queryKey[0] !== 'worktrees') return
      onChange()
    }),
    [client],
  )
  useSyncExternalStore(subscribe, snapshot)

  const entry = client.getQueryCache().find<Worktree[]>({ queryKey: WORKTREES_KEY })
  return {
    rows: entry?.state.data,
    /* "SOMEBODY IS ACTIVELY KEEPING THIS TRUE". `getObserversCount() > 0`: an owner page is mounted and
       will refetch (a localStorage-rehydrated payload on an unrelated page has no observer — fail closed).
       `!isStale()` (15s `staleTime`) bounds an observed payload's age, since the console outlives the owner
       page. Both are facts about THIS tab that nothing persisted can forge — unlike wall-clock timestamps. */
    isLive: !!entry && entry.getObserversCount() > 0 && !entry.isStale(),
  }
}

/** The open console, its divider, and the live-row refinement that needs a QueryClient. */
function OpenConsole({
  request,
  nav,
  presentation,
  docked,
  width,
  ratio,
  defaultRatio,
  onClose,
  onSetView,
  onSetRatio,
  onDividerMove,
  onChatTask,
}: {
  request: ConsoleRequest
  nav: ConsoleNav | null
  presentation: ConsolePresentation
  docked: boolean
  width: number
  ratio: number
  defaultRatio: number
  onClose: () => void
  onSetView: (view: ConsoleView) => void
  onSetRatio: (ratio: number) => void
  onDividerMove: (clientX: number, grabOffset: number) => void
  onChatTask: (taskId: number | null) => void
}) {
  /* The live worktree row: Title and the server's `may_edit`. Trust rules: `useWorktreesCache`. */
  const { rows: cachedWorktrees, isLive: cacheIsLive } = useWorktreesCache()

  /* Believing a stale row could let a revoked grant's `may_edit: true` override the opener's fail-closed
     `false` — the one direction this must never fail. `isLive` is the guard. (The relay and the console's
     key predicate remain the real enforcement; this keeps the UI truthful.) */
  const liveRow = cacheIsLive && !request.view /* a VM target has no slot; number 0 could match one */
    ? cachedWorktrees?.find(
      (wt) => wt.project_id === request.projectId && wt.number === request.number && wt.node_uuid === request.nodeId,
    )
    : undefined
  /* The cache REFINES the opener's snapshot, never replaces it: an absent row (the Tasks board never loads
     the query) leaves the opener's values. `mayEdit` tracks the server live where visible — a revoked
     grant loses the cursor, a late-loading row gains it. */
  const title = liveRow ? resolveTitle(liveRow) : request.title
  const mayEdit = liveRow ? liveRow.may_edit : request.mayEdit
  /* The slot's live agent session, which picks Chat or the terminal. Read here, where a QueryClient is
     guaranteed; a VM view has no slot to ask about. */
  const runtime = useConsoleRuntime({
    projectId: request.projectId,
    number: request.number,
    nodeId: request.nodeId,
    enabled: !request.view,
  })

  return (
    <>
      {docked && (
        <SplitDivider
          testId="console-dock-divider"
          ariaLabel="Resize console"
          valueNow={ratio * 100}
          /* The strip's left edge at the CLAMPED width (a stored ratio can sit outside the clamp). */
          edge={() => window.innerWidth - width - DIVIDER_PX}
          onMove={onDividerMove}
          onReset={() => onSetRatio(defaultRatio)}
        />
      )}
      <Box
        data-testid="console-dock"
        data-view={presentation}
        sx={
          docked
            ? { width, flexShrink: 0, minWidth: 0, height: '100%', display: 'flex' }
            /* `display: contents`: non-docked presentations position against the viewport; ConsoleTerminal
               owns that positioning (incl. the modeless `pointerEvents` pair). */
            : { display: 'contents' }
        }
      >
        <Suspense
          fallback={
            /* The ~2.7MB lazy chunk's stand-in: the DIM, only for `modal` (a scrim would flash over a
               docked/floating app). This is now its single home. */
            <Box
              data-testid="console-overlay"
              aria-label="Loading console…"
              sx={
                presentation === 'modal'
                  ? { position: 'fixed', inset: 0, zIndex: 60, bgcolor: 'rgba(0, 0, 0, 0.55)' }
                  : docked
                    /* Fills the dock-sized wrapper, so the app reflows once, on open. */
                    ? { flex: 1, bgcolor: 'background.default' }
                    /* Floating/maximized take NO FLOW SPACE, nor may their placeholder (under `display:
                       contents` a `flex: 1` would squeeze `<main>` while the chunk loads). Paints nothing. */
                    : { position: 'fixed', inset: 0, pointerEvents: 'none' }
              }
            />
          }
        >
          <ConsoleTerminal
            /* KEYED ON THE SLOT (clean terminal per slot), never on `presentation` — see header. */
            key={`${request.projectId}/${request.number}/${request.nodeId ?? 'cp'}/${request.kind ?? 'clauded'}/${request.provider ?? ''}/${request.view ?? ''}`}
            project={request.project}
            projectId={request.projectId}
            number={request.number}
            nodeId={request.nodeId}
            kind={request.kind}
            title={title}
            mayEdit={mayEdit}
            /* THE OPENER'S ANSWER, NOT REFINED from `liveRow`: it describes the slot at the click, only to
               explain an empty dial; re-reading a moved row could flip a justified `false` to `true`. */
            hadLiveSession={request.hadLiveSession}
            provider={request.provider}
            view={request.view}
            onClose={onClose}
            presentation={presentation}
            onSetView={onSetView}
            onPrev={nav?.onPrev}
            onNext={nav?.onNext}
            hasPrev={nav?.hasPrev ?? false}
            hasNext={nav?.hasNext ?? false}
            runtime={runtime}
            onChatTask={onChatTask}
          />
        </Suspense>
      </Box>
    </>
  )
}

/** The rail's rendered width. Measured, not inferred — see the call site. */
function useSidebarWidth(fallback: number): number {
  const [width, setWidth] = useState(fallback)
  useEffect(() => {
    const rail = document.querySelector('[data-testid="sidebar"]')
    const measure = () => {
      const measured = rail?.getBoundingClientRect().width ?? 0
      // Zero means no laid-out rail (jsdom): use the token, or the clamp would overlap the rail. The ≤480px
      // bottom bar measures ~the viewport, and is irrelevant (`canDock` forces `modal` below 1024px).
      setWidth(measured > 0 ? measured : fallback)
    }
    measure()
      // The rail resizes under clicks this never sees; `ResizeObserver` catches it without polling.
    if (!rail || typeof ResizeObserver === 'undefined') return
    const observer = new ResizeObserver(measure)
    observer.observe(rail)
    return () => observer.disconnect()
    // NOT keyed on the viewport: the observer already reports every width change.
  }, [fallback])
  return width
}
