// THE ARITHMETIC OF A RIGHT-HAND PANE BESIDE THE APP — shared by the web console's dock
// (`lib/consoleDock.ts`) and the list/detail split (`lib/listDetail.ts`). Pure, so it is settled as
// arithmetic (jsdom has no layout). Each caller owns its floors and its storage; the clamp and the divider
// geometry are here once, so the two splits cannot drift apart in how a drag or a resize resolves.

/**
 * A divider's width, in px: hit area, with a 1px visible rule (components/SplitDivider.tsx). Here because
 * every clamp must subtract it, or the left pane settles `DIVIDER_PX` under its floor.
 */
export const DIVIDER_PX = 8

/** The side inset each scrollport keeps against a split's divider: flush, rings and edges got clipped. */
export const SPLIT_INSET_PX = 4

/**
 * Clamp a right pane's desired width so that BOTH floors hold inside `room` (the px the pane and what
 * sits left of it share, divider already subtracted). When the room cannot satisfy both, the PANE's floor
 * wins and the left side is allowed under its own: a pane narrower than its floor is unreadable, which is
 * the worse failure. Unusable numbers (a NaN from an unmeasured element) degrade to the floor.
 */
export function clampPaneWidth(desiredPx: number, roomPx: number, paneMinPx: number, restMinPx: number): number {
  const room = Number.isFinite(roomPx) && roomPx > 0 ? roomPx : 0
  const desired = Number.isFinite(desiredPx) ? desiredPx : paneMinPx
  const maxPane = room - restMinPx
  // Negated `>=` stays correct for NaN.
  if (!(maxPane >= paneMinPx)) return paneMinPx
  return Math.min(Math.max(desired, paneMinPx), maxPane)
}

/**
 * The width a right pane asks for when its divider's pointer is at `clientX`. The divider precedes the
 * pane, so `x_strip_left = clientX - grabOffset` and the pane runs from the strip's right edge to
 * `rightEdge`. `grabOffset` is where in the strip the pointer went down (the strip does not jump on the
 * first move); a keyboard step passes 0. Unclamped: the caller clamps against its own floors.
 */
export function paneWidthFromDividerX(clientX: number, rightEdge: number, grabOffset = 0): number {
  const x = Number.isFinite(clientX) ? clientX : 0
  const grab = Number.isFinite(grabOffset) ? grabOffset : 0
  return rightEdge - (x - grab) - DIVIDER_PX
}

/** A stored split ratio worth trusting: a finite fraction strictly inside (0, 1). The OPEN interval because 0
 *  and 1 are unreachable by drag and unrecoverable without clearing storage. */
export function isRatio(value: unknown): value is number {
  return typeof value === 'number' && Number.isFinite(value) && value > 0 && value < 1
}
