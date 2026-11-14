/* THE LIST'S OWN WIDTH DROPS ITS WINDOW-GATED SLOTS. `meta`/`id`/`wide`/`squeeze` are priced on the
 * WINDOW (`./geometry`'s `GATE_SX`), so a list in a narrow column of a wide window — Settings › API keys beside the
 * category column and the open setup dock at 1280px, ~400px — kept every slot: the name went to nothing, the row
 * overflowed and its ⋯ was clipped. Here the list MEASURES its first row's name cell and drops whole bands, in the
 * window gates' own order (`wide`/`squeeze`, then `meta`, then `id`), while the name is under `NAME_FLOOR`; a band
 * comes back once the name would still hold the floor with its slots (and their gaps) restored, so it never
 * oscillates. THEN, while the name is still under the floor, every OTHER gated slot on the row goes, one gate at a
 * time from the left (`phone`, a list's own gate — Networks › Connections' image, a list-specific container gate),
 * then the ungated slots one at a time from the left: a
 * list beside an open record (`components/ListDetail.tsx`, a ~450px pane at 1440px) kept NodeLink's path, storage,
 * file system and count, and Connections' image, and the name got nothing. Beside an open record the floor is
 * `SPLIT_NAME_FLOOR`, ~12 characters. The state disc, the name and the ⋯ never drop; the row's `control` drops only
 * with its window band. jsdom lays nothing out: no drops there. */
import { type RefObject, useLayoutEffect, useMemo, useState } from 'react'
import type { SlotGate } from './geometry'
import type { FeedRowSpec, FeedSlot } from './types'

/* The narrowest name a row may keep before a band goes: two lines of ~8 characters (`titleLines: 2`). Under the
 * API keys row's ~82px at 1280px beside the category column alone, which therefore keeps every slot as before. */
export const NAME_FLOOR = 64

/* The floor for a list beside an open record (`components/ListDetail.tsx`): ~12 characters of the row's 13px name,
 * enough to tell its rows apart. Only there: a full-width list keeps `NAME_FLOOR`, so it looks as it did. */
export const SPLIT_NAME_FLOOR = 96

/* Drop order, first to go first: the window gates' own order (`wide` ≤1023, `meta` ≤899, `id` ≤767); then the
 * row's other gates one at a time, leftmost first (slots are ordered so the least needed sit left), then its ungated
 * slots the same way, each by its key (`ungatedDrop`). */
const WINDOW_BANDS: readonly (readonly SlotGate[])[] = [['wide', 'squeeze'], ['meta'], ['id']]

/** The drop key of a slot with no gate: its own key, so ungated slots go one at a time, last. */
export const ungatedDrop = (key: string) => `ungated:${key}` as const
export type FitDrop = SlotGate | ReturnType<typeof ungatedDrop>
const dropOf = (slot: Pick<FeedSlot, 'gate' | 'key'>): FitDrop => slot.gate ?? ungatedDrop(slot.key)
/** Whether this list's width dropped this slot. The row's `control` drops with a window band only (`priceBands`). */
export const isDropped = (drops: ReadonlySet<FitDrop>, slot: Pick<FeedSlot, 'gate' | 'key'>, control = false): boolean =>
  control ? slot.gate !== undefined && WINDOW_BANDS.flat().includes(slot.gate) && drops.has(slot.gate) : drops.has(dropOf(slot))

export const NO_DROPS: ReadonlySet<FitDrop> = new Set()

/* The width a band's cells take on the row, each plus one flex gap. A `cqw`-capped width (`contentSlotWidth`) counts
 * its first px term, the widest it can be: over-pricing only brings a band back later. */
const pxOf = (width: number | string | undefined, fallback: number): number => {
  if (typeof width === 'number') return width
  if (typeof width !== 'string') return fallback
  // theme.ts's `grid(n)`: `calc(n * var(--mui-spacing, <unit>px))` — n units, not the unit's fallback px.
  const units = /calc\((-?\d+(?:\.\d+)?) \* var\(--mui-spacing, (\d+(?:\.\d+)?)px\)\)/.exec(width)
  if (units) return Number(units[1]) * Number(units[2])
  return Number(/(\d+(?:\.\d+)?)px/.exec(width)?.[1] ?? fallback)
}

/* What a band costs on the row, before gaps: its cells' widths and how many cells (each adds one flex gap). */
type Band = { gates: readonly FitDrop[]; width: number; cells: number }

export function priceBands(row: FeedRowSpec): Band[] {
  const windowGates: readonly SlotGate[] = WINDOW_BANDS.flat()
  const otherGates = [...new Set((row.slots ?? []).flatMap((slot) => (slot.gate && !windowGates.includes(slot.gate) ? [slot.gate] : [])))]
  const ungated = (row.slots ?? []).flatMap((slot) => (slot.gate ? [] : [ungatedDrop(slot.key)]))
  const bands: readonly (readonly FitDrop[])[] = [...WINDOW_BANDS, ...[...otherGates, ...ungated].map((drop) => [drop])]
  return bands.map((gates) => {
    let width = 0
    let cells = 0
    // The row's `control` drops with its gate's window band only: it has no ⋯ to fall back on.
    const controls = row.control?.gate && windowGates.includes(row.control.gate) ? [row.control] : []
    for (const slot of [...(row.slots ?? []), ...controls]) {
      if (gates.includes(dropOf(slot))) { width += pxOf(slot.width, 0); cells += 1 }
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
export function useFittedDrops(
  rootRef: RefObject<HTMLElement | null>,
  row: FeedRowSpec | undefined,
  nameFloor: number = NAME_FLOOR,
): ReadonlySet<FitDrop> {
  const key = signature(row)
  const titleMaxWidth = row?.titleMaxWidth
  // eslint-disable-next-line react-hooks/exhaustive-deps -- `key` is `row`'s gated geometry
  const bands = useMemo(() => (row ? priceBands(row) : []), [key])
  const [level, setLevel] = useState(0)
  const depth = Math.min(level, bands.length)

  useLayoutEffect(() => {
    const root = rootRef.current
    if (!root || bands.length === 0 || typeof ResizeObserver === 'undefined') return
    const floor = titleMaxWidth === undefined ? nameFloor : Math.min(nameFloor, titleMaxWidth)
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
  }, [rootRef, bands, depth, titleMaxWidth, nameFloor])

  return useMemo(() => (depth === 0 ? NO_DROPS : new Set(bands.slice(0, depth).flatMap((band) => band.gates))), [bands, depth])
}
