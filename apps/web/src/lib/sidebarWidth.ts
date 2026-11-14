// THE SIDEBAR'S DRAGGED WIDTH: the rail's right edge is a `SplitDivider`. Dragging sets the expanded
// width, remembered per browser; dragging inward past `SIDEBAR_COLLAPSE_BELOW_PX` collapses it, as '‹' does, and
// dragging a collapsed rail's edge outward past the same line expands it. Pure, so it is settled as arithmetic.

/** The narrowest expanded rail: the wordmark, the status cluster and the toggle still fit on one line. */
export const SIDEBAR_MIN_PX = 160
/** The widest rail: past this the page layouts, measured against the default rail, start to crowd. */
export const SIDEBAR_MAX_PX = 320
/** One line for both directions, so a drag held near it never flickers between the two shapes. */
export const SIDEBAR_COLLAPSE_BELOW_PX = 120

/** The local-storage key holding the dragged width; absent = the theme's default width. */
export const SIDEBAR_WIDTH_KEY = 'sidebar-width'

export type SidebarDrag = { collapsed: true } | { collapsed: false; width: number }

/** Where a drag that would put the rail's right edge `desiredPx` from its left lands. */
export function resolveSidebarDrag(desiredPx: number): SidebarDrag {
  if (!Number.isFinite(desiredPx) || desiredPx < SIDEBAR_COLLAPSE_BELOW_PX) return { collapsed: true }
  return { collapsed: false, width: Math.round(Math.min(Math.max(desiredPx, SIDEBAR_MIN_PX), SIDEBAR_MAX_PX)) }
}

/** A stored width, or `null` (absent, garbled or out of range) for the theme's default. */
export function parseSidebarWidth(raw: string | null): number | null {
  if (raw === null) return null
  const n = Number(raw)
  return Number.isFinite(n) && n >= SIDEBAR_MIN_PX && n <= SIDEBAR_MAX_PX ? n : null
}
