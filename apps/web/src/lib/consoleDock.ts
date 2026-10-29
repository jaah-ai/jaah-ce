// The dockable console's MODEL: allowed width and the last arrangement. Pure (no React/MUI/DOM) so
// `consoleDock.test.ts` settles it as arithmetic — jsdom has no layout.
//
// The two competing FLOORS (a readable terminal vs a usable app pane) must resolve identically for
// initial render, divider drag and window resize — hence one home (`clampConsoleWidth`).
//
// PERSISTENCE IS `localStorage`: PER BROWSER, not synced across tabs until re-read.

import { readJsonObject, writeJson } from './safeStorage'
import { DIVIDER_PX, clampPaneWidth, isRatio, paneWidthFromDividerX } from './splitPane'

// Re-exported: the console's callers and tests read the divider's width from here.
export { DIVIDER_PX }

/** Viewport width (px) at or above which the console may dock beside the app. */
export const DOCK_MIN_VIEWPORT = 1024

/**
 * The console's floor, in px — set by the HEADER, not the terminal. The header's fixed controls MEASURE
 * 274px (207 actions + 67 transport); the Title gets the rest. 420 is the first measured width at which
 * the Title shows `theme.custom.minReadableCh` (12) characters (400px → 10, 440px → 15). At 320 the Title
 * rendered ZERO characters with every check green. Change only with a fresh measurement of the header.
 */
export const CONSOLE_MIN_PX = 420

/** The app content's floor, in px — what must remain readable beside a docked console. While an editor
 *  is split beside its list the floor is both panes' (`lib/listDetail.ts`'s `SPLIT_MIN_PX`), passed as
 *  `mainMinPx` below, so opening an editor narrows the console instead of hiding the list. */
export const MAIN_MIN_PX = 320

/** The console's share of the viewport when nothing has been persisted: the right HALF. */
export const DEFAULT_RATIO = 0.5

export const DOCK_STORAGE_KEY = 'console-dock'

/** The console's share while it is one of THREE panes (an editor split beside its list,
 *  components/ListDetail.tsx): a third, so list and editor get the rest. Its own preference, under its own
 *  key, so neither arrangement's width overwrites the other's. */
export const DEFAULT_SPLIT_RATIO = 1 / 3
export const SPLIT_RATIO_STORAGE_KEY = 'console-dock-split'

/** Docked = in-flow beside the app. Floating = a free window over it. Maximized = the whole viewport. */
export type ConsoleView = 'docked' | 'floating' | 'maximized'

/**
 * What the panel ACTUALLY presents: the stored `ConsoleView` plus `modal`, forced below
 * `DOCK_MIN_VIEWPORT`. NOT a `ConsoleView` because it is never stored — storing it would overwrite the
 * real preference on the first narrow window.
 */
export type ConsolePresentation = ConsoleView | 'modal'

/** Mirrors the payload pages already build (`components/WorktreePool.tsx`'s `openSlotConsole` etc.).
 *  `provider`: a catalogue value (`GET /api/accounts/providers`) for the account a NEW session runs on;
 *  absent = Any, the server's Auto-assign pick. */
/** `view`: a MicroVM task's `vm:<vm_id>` (with `number` 0). Such a target has no slot: attach-only, no
 *  saved layouts, no direct transport. */
export type ConsoleTarget = {
  // `project` is the display name; `projectId` addresses every console call. `nodeId` is the node uuid.
  project: string
  projectId: number
  number: number
  nodeId?: string
  kind?: 'clauded' | 'term'
  provider?: string
  view?: string
}

export type DockState = { ratio: number; view: ConsoleView }

/**
 * How close to an edge a stored ratio may get. On a viewport too narrow for both floors the clamped width
 * (`CONSOLE_MIN_PX`) can exceed the viewport, making the quotient >= 1, which `loadDockState` discards;
 * a drag must never produce state its own loader rejects. Sub-pixel at any real viewport.
 */
const RATIO_BOUND = 0.001

/** True when the viewport is wide enough for a side-by-side console. */
export function canDock(viewportWidth: number): boolean {
  // NaN (unmeasured element, early resize observer) reads as "cannot dock" by decision.
  return Number.isFinite(viewportWidth) && viewportWidth >= DOCK_MIN_VIEWPORT
}

/** Clamp a desired console width (px) so BOTH floors above are honored. When the viewport cannot
 *  satisfy both, the console floor wins and main is allowed to go under its floor — returning a
 *  width below CONSOLE_MIN_PX would render a terminal nobody can read, which is the worse failure. */
export function clampConsoleWidth(
  desiredPx: number,
  viewportWidth: number,
  sidebarWidth: number,
  mainMinPx: number = MAIN_MIN_PX,
): number {
  // All inputs come from the DOM at some point; one NaN would yield an ignored `width: NaNpx` (no width,
  // no error). Unusable numbers become zero, degrading to the floor.
  const viewport = Number.isFinite(viewportWidth) && viewportWidth > 0 ? viewportWidth : 0
  const sidebar = Number.isFinite(sidebarWidth) && sidebarWidth > 0 ? sidebarWidth : 0
  // THE PRODUCT DECISION (the pane's floor wins a collision) is `clampPaneWidth`'s, shared with the
  // list/detail split.
  return clampPaneWidth(desiredPx, viewport - sidebar - DIVIDER_PX, CONSOLE_MIN_PX, mainMinPx)
}

/** The console's width in px for a stored ratio at the current viewport, already clamped. */
export function consoleWidthFor(
  ratio: number,
  viewportWidth: number,
  sidebarWidth: number,
  mainMinPx: number = MAIN_MIN_PX,
): number {
  // THE RATIO IS OF THE FULL VIEWPORT, never of the space after the sidebar: the sidebar collapses under
  // the dock (theme.custom.sidebar.width / .widthCollapsed), and a sidebar-relative ratio would make the
  // console JUMP on every rail toggle. Only the clamp sees the sidebar.
  const viewport = Number.isFinite(viewportWidth) && viewportWidth > 0 ? viewportWidth : 0
  const fraction = Number.isFinite(ratio) ? ratio : DEFAULT_RATIO
  return clampConsoleWidth(fraction * viewport, viewport, sidebarWidth, mainMinPx)
}

/**
 * Turn a divider drag's pointer x into a ratio of the viewport.
 * `grabOffset` is where in the 8px strip the pointer went down, so the strip does not jump on the first
 * move; a keyboard step passes 0. The divider precedes the console: `x_strip_left = clientX - grabOffset`.
 * Clamped in pixels, then divided so the ratio survives a resize.
 */
export function ratioFromDividerX(
  clientX: number,
  viewportWidth: number,
  sidebarWidth: number,
  grabOffset = 0,
  mainMinPx: number = MAIN_MIN_PX,
): number {
  const viewport = Number.isFinite(viewportWidth) && viewportWidth > 0 ? viewportWidth : 0
  // Unmeasurable viewport → default ratio, not a sliver the user must drag back out.
  if (viewport === 0) return DEFAULT_RATIO

  // Clamp in PIXELS so drags obey the same floors as render and resize.
  const width = clampConsoleWidth(paneWidthFromDividerX(clientX, viewport, grabOffset), viewport, sidebarWidth, mainMinPx)

  // A FRACTION: "half", not "960px", across resizes. See RATIO_BOUND.
  return Math.min(Math.max(width / viewport, RATIO_BOUND), 1 - RATIO_BOUND)
}

/** Read persisted dock state. Returns DEFAULT_RATIO/'docked' when absent, unparseable, or invalid
 *  (ratio not a finite number in (0,1), or view not one of the three literals). */
export function loadDockState(): DockState {
  // Fresh per call: a shared object would let one caller's mutation leak into another's default.
  const fallback: DockState = { ratio: DEFAULT_RATIO, view: 'docked' }

  // VALIDATED, NOT TRUSTED — any past build, devtools or extension may have written this, and it lands in
  // a CSS width. One bad field discards the whole record.
  const candidate = readJsonObject('local', DOCK_STORAGE_KEY)
  if (!candidate) return fallback
  const { ratio, view } = candidate
  if (!isRatio(ratio)) return fallback
  if (view !== 'docked' && view !== 'floating' && view !== 'maximized') return fallback

  return { ratio, view }
}

export function saveDockState(state: DockState): void {
  // Result dropped deliberately: failing to remember (blocked, private, quota) must never stop applying it.
  writeJson('local', DOCK_STORAGE_KEY, state)
}

/** The three-pane ratio (`DEFAULT_SPLIT_RATIO`). Validated as `loadDockState`'s ratio: a number in (0,1). */
export function loadSplitRatio(): number {
  const candidate = readJsonObject('local', SPLIT_RATIO_STORAGE_KEY)?.ratio
  return isRatio(candidate) ? candidate : DEFAULT_SPLIT_RATIO
}

export function saveSplitRatio(ratio: number): void {
  writeJson('local', SPLIT_RATIO_STORAGE_KEY, { ratio })
}
