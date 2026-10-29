// THE LIST/DETAIL SPLIT'S MODEL: which list every editor belongs to, and how wide the editor's pane is.
// Pure (no React/MUI/DOM), so `listDetail.test.ts` settles it as data and arithmetic. The view is
// components/ListDetail.tsx; the console's own split is lib/consoleDock.ts, and both clamp through
// lib/splitPane.ts.
//
// THE CONTRACT: on a wide viewport, an editor's URL (`/tasks/41`) renders the editor in a resizable pane
// beside the page it was opened from (its list, on a cold load) — the web console docks to the right of
// both. The URL stays the editor's, so a link or a reload lands on a split.
//
// PERSISTENCE IS `localStorage`: the split ratio and Maximize are per-browser preferences, as the console's.

import { generatePath, matchPath } from 'react-router-dom'
import { readJsonObject, writeJson } from './safeStorage'
import { DIVIDER_PX, clampPaneWidth, isRatio, paneWidthFromDividerX } from './splitPane'
import { TEMPLATE_PATHS, normalizePathname } from '../routeGroups'
import { MAIN_MIN_PX } from './consoleDock'
import { REMOVED_CONSOLE_ROUTES } from './editionFeatures'

/**
 * EVERY EDITOR, AND THE LIST A COLD LOAD SHOWS BESIDE IT — the single home of both facts. Opened in-app,
 * an editor's pane sits beside whatever page it was opened from (components/ListDetail.tsx); this list is
 * only what a deep link or a reload puts there. An editor not listed here keeps opening as a full page.
 * Exact `matchPath` patterns, so a list's own address (`/workflows`) never matches its editor's
 * (`/workflows/:name`). A create page (`/tasks/new`, `/projects/new`, …) is matched by its editor's `:id`
 * pattern, so New opens in the pane too. A `list` may name the detail's own params (`:projectId`): a cold
 * load fills them from the detail's address, so a child record sits beside its parent page. A `list` may
 * carry its tab as a query (`/products?tab=plans`); the cold background takes it as its search.
 */
export const LIST_DETAIL_ROUTES: readonly { detail: string; list: string }[] = [
  { detail: '/tasks/:id', list: '/tasks' },
  { detail: '/features/:id', list: '/tasks' },
  { detail: '/projects/:id', list: '/projects' },
  { detail: '/projects/:id/docs', list: '/projects' },
  { detail: '/nodes/:nodeId', list: '/nodes' },
  { detail: '/nodes/groups/:groupId', list: '/nodes' },
  { detail: '/nodes/hosts/:hostName', list: '/nodes' },
  { detail: '/users/:id', list: '/users' },
  { detail: '/groups/:id', list: '/groups' },
  { detail: '/credentials/:id', list: '/credentials' },
  { detail: '/accounts/:id', list: '/accounts' },
  { detail: '/accounts/login/:slug', list: '/accounts' },
  { detail: '/automations/:id', list: '/automations' },
  { detail: '/automations/:id/runs/:runId', list: '/automations' },
  { detail: '/playbooks/:id', list: '/playbooks' },
  { detail: '/templates/:id', list: TEMPLATE_PATHS.manage },
  { detail: '/support/tickets/:id', list: '/support' },
  { detail: '/audit/events/:eventId', list: '/audit' },
  { detail: '/audit/nodes/commands/:commandId', list: '/audit/nodes' },
  { detail: '/workflows/personas/:persona', list: '/audit/personas' },
  { detail: '/workflows/runs/:runId', list: '/audit/workflows' },
  { detail: '/workflows/:name', list: '/audit/workflows/catalogue' },
  { detail: '/compliance/repos/:projectId/cells/:ruleKey', list: '/compliance/repos/:projectId' },
  { detail: '/compliance/rules/:ruleId', list: '/compliance?tab=packs' },
  { detail: '/compliance/packs/:packId/rules/new', list: '/compliance?tab=packs' },
  { detail: '/compliance/packs/:packId', list: '/compliance?tab=packs' },
  { detail: '/products/plans/:key', list: '/products?tab=plans' },
  { detail: '/org/billing/subscription/change', list: '/org/billing' },
  { detail: '/org/billing/subscription', list: '/org/billing' },
  { detail: '/org/billing/jaah-ai', list: '/org/billing' },
  { detail: '/org/billing/hosting/:id', list: '/org/billing' },
  { detail: '/org/billing/invoices/:id', list: '/org/billing' },
  { detail: '/org/billing/methods/:id', list: '/org/billing' },
  { detail: '/org/billing/details', list: '/org/billing' },
]

/** The list a cold load shows beside an editor's pathname; `null` for a page that is no editor. */
export function listForDetail(pathname: string): string | null {
  // A path of a feature this edition leaves out is the full-page upsell (`pages/ce/NotInEdition.tsx`), no editor.
  if (REMOVED_CONSOLE_ROUTES.some(({ path }) => matchPath(path, pathname) !== null)) return null
  for (const route of LIST_DETAIL_ROUTES) {
    const match = matchPath(route.detail, pathname)
    // A list whose address its editor's pattern also matches (`/templates/manage` against `/templates/:id`) is no
    // editor of itself.
    if (match !== null && normalizePathname(pathname) === route.list) return null
    if (match !== null) return route.list.includes(':') ? generatePath(route.list, match.params) : route.list
  }
  return null
}

/**
 * WHERE CLOSE GOES FOR A PANE OPENED FROM INSIDE ANOTHER PANE: the editor's `?from=` (the opener's own
 * pathname+search, `/automations/7?tab=runs`), or `null` when it is absent or not a safe in-app path — then
 * Close goes to the background list as ever (components/ListDetail.tsx). Safe = starts with `/`, not `//`,
 * no `\`, and resolves to `origin` itself, so a crafted link can never send Close off-site. `param` names
 * another query parameter with the same rule (`CredentialEditPage`'s `?return=`).
 */
export function safeReturnPath(search: string, origin: string, param = 'from'): string | null {
  const from = new URLSearchParams(search).get(param)
  if (from === null || !from.startsWith('/') || from.startsWith('//') || from.includes('\\')) return null
  try {
    const url = new URL(from, origin)
    const path = `${url.pathname}${url.search}${url.hash}`
    // Checked again AFTER resolution: `/..//evil.example` normalizes to `//evil.example`.
    return url.origin === origin && !path.startsWith('//') ? path : null
  } catch {
    return null
  }
}

/** True when a row's link (`/tasks/41`, `/tasks/41?tab=x`) is the editor open in the pane (`openPath`, a
 *  normalized pathname, or `null` with nothing open). Compared by pathname, so a query never breaks it. */
export function isOpenPath(openPath: string | null, href: string | undefined): boolean {
  return openPath !== null && href !== undefined && normalizePathname(href.split(/[?#]/)[0]) === openPath
}

/** The editor pane's floor, in px: a detail page's tab strip on one line and its fields readable. 440
 *  lets list, editor and the console's own floor share a 1440px window beside the expanded rail. */
export const DETAIL_MIN_PX = 440

/** The list's floor beside an open editor — the same floor the app pane keeps beside the console. */
export const LIST_MIN_PX = MAIN_MIN_PX

/** The room list + divider + editor need at their floors: the app's floor beside the console while an
 *  editor is open (components/ConsoleDock.tsx), and the threshold of `canSplit`. */
export const SPLIT_MIN_PX = LIST_MIN_PX + DIVIDER_PX + DETAIL_MIN_PX

/** The editor's share of the room when nothing has been persisted: three fifths. At half, a 1280px split left the task
 *  page's trail ~320px beside its pill, console and ⋯, and the title read "1: Retire the task form modal …";
 *  the list's rows ellipsise their titles anyway. */
export const DEFAULT_DETAIL_RATIO = 0.6

export const DETAIL_PANE_STORAGE_KEY = 'detail-pane'

export type DetailPaneState = { ratio: number; maximized: boolean }

/** True when the room left of the console can hold the list AND the editor at their floors. Below it the
 *  editor fills the room (as Maximize does) and the list stays mounted out of sight. */
export function canSplit(workspaceWidth: number): boolean {
  return Number.isFinite(workspaceWidth) && workspaceWidth >= SPLIT_MIN_PX
}

/** The editor pane's width in px for a stored ratio of the room (workspace minus the divider). */
export function detailWidthFor(ratio: number, workspaceWidth: number): number {
  const room = workspaceWidth - DIVIDER_PX
  const fraction = Number.isFinite(ratio) ? ratio : DEFAULT_DETAIL_RATIO
  return clampPaneWidth(fraction * room, room, DETAIL_MIN_PX, LIST_MIN_PX)
}

/**
 * A divider drag's pointer x as a ratio of the room. `workspaceRight` is the room's right edge in client
 * px (the console's divider, or the viewport). Clamped in PIXELS by the same floors as render, then
 * divided so the split survives a resize.
 */
export function detailRatioFromDividerX(
  clientX: number,
  workspaceRight: number,
  workspaceWidth: number,
  grabOffset = 0,
): number {
  const room = workspaceWidth - DIVIDER_PX
  if (!(room > 0)) return DEFAULT_DETAIL_RATIO
  const width = clampPaneWidth(paneWidthFromDividerX(clientX, workspaceRight, grabOffset), room, DETAIL_MIN_PX, LIST_MIN_PX)
  return Math.min(Math.max(width / room, 0.001), 0.999)
}

/** Read the persisted split. VALIDATED, NOT TRUSTED: one bad field discards the record. */
export function loadDetailPaneState(): DetailPaneState {
  const fallback: DetailPaneState = { ratio: DEFAULT_DETAIL_RATIO, maximized: false }
  const candidate = readJsonObject('local', DETAIL_PANE_STORAGE_KEY)
  if (!candidate) return fallback
  const { ratio, maximized } = candidate
  if (!isRatio(ratio) || typeof maximized !== 'boolean') return fallback
  return { ratio, maximized }
}

export function saveDetailPaneState(state: DetailPaneState): void {
  // Failing to remember must never stop applying it.
  writeJson('local', DETAIL_PANE_STORAGE_KEY, state)
}
