/* THE LIST'S OWN WIDTH DROPS ITS WINDOW-GATED SLOTS. `meta`/`id`/`wide`/`squeeze` are priced on the
 * WINDOW (`./geometry`'s `GATE_SX`), so a list in a narrow column of a wide window — Settings › API keys beside the
 * category column and the open setup dock at 1280px, ~400px — kept every slot: the name went to nothing, the row
 * overflowed and its ⋯ was clipped. Here the list MEASURES its first row's name cell and drops whole bands, in the
 * window gates' own order (`wide`/`squeeze`, then `meta`, then `id`), while the name is under `NAME_FLOOR`; a band
 * comes back once the name would still hold the floor with its slots (and their gaps) restored, so it never
 * oscillates. A list-specific container gate (`BOARD_NO_*`, `NODES_NO_*`, …) is untouched: it already follows its
 * own list. jsdom lays nothing out: no drops there. */
import { type RefObject, useLayoutEffect, useMemo, useState } from 'react'
import type { SlotGate } from './geometry'
import type { FeedRowSpec } from './types'

/* The narrowest name a row may keep before a band goes: two lines of ~8 characters (`titleLines: 2`). Under the
 * API keys row's ~82px at 1280px beside the category column alone, which therefore keeps every slot as before. */
export const NAME_FLOOR = 64

/* Drop order, first to go first: the window gates' own order (`wide` ≤1023, `meta` ≤899, `id` ≤767). */
const BANDS: readonly (readonly SlotGate[])[] = [['wide', 'squeeze'], ['meta'], ['id']]

export const NO_DROPS: ReadonlySet<SlotGate> = new Set()

/* The width a band's cells take on the row, each plus one flex gap. A `cqw`-capped width (`contentSlotWidth`) counts
 * its first px term, the widest it can be: over-pricing only brings a band back later. */
const pxOf = (width: number | string | undefined, fallback: number): number =>
  typeof width === 'number' ? width : typeof width === 'string' ? Number(/(\d+(?:\.\d+)?)px/.exec(width)?.[1] ?? fallback) : fallback

/* What a band costs on the row, before gaps: its cells' widths and how many cells (each adds one flex gap). */
type Band = { gates: readonly SlotGate[]; width: number; cells: number }

function priceBands(row: FeedRowSpec): Band[] {
  return BANDS.map((gates) => {
    let width = 0
    let cells = 0
    for (const slot of [...(row.slots ?? []), ...(row.control ? [row.control] : [])]) {
      if (slot.gate && gates.includes(slot.gate)) { width += pxOf(slot.width, 0); cells += 1 }
    }
    if (row.lead && gates.includes(row.lead.gate ?? 'id')) { width += row.lead.width ?? 40; cells += 1 }
    return { gates, width, cells }
  }).filter((band) => band.cells > 0) // an absent band would spend a step dropping nothing
}

/* The row's gated geometry as a key: a route rebuilds its row objects every render, and the effect must not. */
const signature = (row: FeedRowSpec | undefined): string =>
  row
    ? JSON.stringify([
        row.slots?.map((slot) => [slot.gate, slot.width]),
        row.control && [row.control.gate, row.control.width],
        row.lead && [row.lead.gate ?? 'id', row.lead.width ?? 40],
        row.titleMaxWidth,
      ])
    : ''

const NAME_SELECTOR = ':scope > [data-testid="feed-bucket"] > [role="rowgroup"] > [data-testid="feed-row"] > [data-feed-name]'

/* The room a name could take: its cell, plus the row's spare width — a capped title (`titleMaxWidth`) leaves that
 * beside it, and it is where a restored band would go. The NARROWEST row rules: a child row's indent narrows its name. */
function nameRoom(root: HTMLElement): { room: number; gap: number } | null {
  let best: { room: number; gap: number } | null = null
  for (const name of root.querySelectorAll<HTMLElement>(NAME_SELECTOR)) {
    const row = name.parentElement
    if (!row) continue
    const style = getComputedStyle(row)
    const gap = parseFloat(style.columnGap) || 0
    const inner = row.clientWidth - (parseFloat(style.paddingLeft) || 0) - (parseFloat(style.paddingRight) || 0)
    let used = 0
    let shown = 0
    // A dropped cell measures 0, and so does the disc's `display: contents` cell: neither is a flex item with a gap.
    for (const child of row.children) {
      const width = child.getBoundingClientRect().width
      if (width > 0) { used += width; shown += 1 }
    }
    const spare = Math.max(0, inner - used - gap * Math.max(0, shown - 1))
    const room = name.getBoundingClientRect().width + spare
    if (!best || room < best.room) best = { room, gap }
  }
  return best
}

/** The gates this list's own width drops, priced off `row` (the first row: every row shares one slot set). */
export function useFittedDrops(rootRef: RefObject<HTMLElement | null>, row: FeedRowSpec | undefined): ReadonlySet<SlotGate> {
  const key = signature(row)
  const titleMaxWidth = row?.titleMaxWidth
  // eslint-disable-next-line react-hooks/exhaustive-deps -- `key` is `row`'s gated geometry
  const bands = useMemo(() => (row ? priceBands(row) : []), [key])
  const [level, setLevel] = useState(0)
  const depth = Math.min(level, bands.length)

  useLayoutEffect(() => {
    const root = rootRef.current
    if (!root || bands.length === 0 || typeof ResizeObserver === 'undefined') return
    const floor = titleMaxWidth === undefined ? NAME_FLOOR : Math.min(NAME_FLOOR, titleMaxWidth)
    const fit = () => {
      if (root.getBoundingClientRect().width === 0) return
      const measured = nameRoom(root)
      if (!measured) return
      const { room, gap } = measured
      setLevel((current) => {
        const at = Math.min(current, bands.length)
        if (room < floor && at < bands.length) return at + 1
        const back = bands[at - 1]
        if (back && room - back.width - gap * back.cells >= floor) return at - 1
        return at
      })
    }
    const observer = new ResizeObserver(fit)
    observer.observe(root)
    fit()
    return () => observer.disconnect()
  }, [rootRef, bands, depth, titleMaxWidth])

  return useMemo(() => (depth === 0 ? NO_DROPS : new Set(bands.slice(0, depth).flatMap((band) => band.gates))), [bands, depth])
}
