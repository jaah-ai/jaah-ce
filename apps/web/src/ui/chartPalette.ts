/* The console's chart series colours (the operator dashboard's, `pages/dashboard/`, and the node and session usage
 * charts'): the dataviz reference categorical palette (validated for CVD separation in
 * BOTH schemes — the dark column is the same hues stepped for the dark surface, not an automatic flip), in its
 * fixed set. Seven named series is the API's ceiling (`frame.py::TOP_GROUPS`); the eighth, "Other", is
 * always neutral grey so it never reads as a peer of a named group. */

/** The id of a rollup series — the groups past the top seven, folded into one (the dashboard API's
 *  `frame.py::OTHER_GROUP`). Here, so the palette ships where the dashboard does not (the community edition). */
export const ROLLUP_GROUP = 'other'

/** Every chart's axis tick labels: 12px, the floor a label stays legible at. */
export const TICK_STYLE = { fontSize: 12 } as const

const CATEGORICAL = {
  light: ['#2a78d6', '#eb6834', '#1baf7a', '#eda100', '#e87ba4', '#008300', '#4a3aa7'],
  dark: ['#3987e5', '#d95926', '#199e70', '#c98500', '#d55181', '#008300', '#9085e9'],
} as const

const OTHER = { light: '#8a8f98', dark: '#7d828b' } as const

/** The single-series colour (sparklines, a one-group chart): slot 1. */
export function primarySeries(mode: 'light' | 'dark'): string {
  return CATEGORICAL[mode][0]
}

/** The `index`th hue of the fixed categorical order, for a chart whose lines are a fixed set (never hashed:
 *  slots 3 and 6 of the hash spread are both greens). Past the set it repeats; callers stay within it. */
export function categoricalSeries(mode: 'light' | 'dark', index: number): string {
  const hues = CATEGORICAL[mode]
  return hues[index % hues.length]
}

/** Each group's colour, keyed by the group's drawn id (`pages/dashboard/model.ts::drawnSeries`). COLOUR FOLLOWS THE ENTITY, NEVER ITS RANK: a group's slot comes
 *  from its id (a stable hash), so a group keeps its hue across charts and filters, whatever its rank. Seven
 *  slots cannot give every id a hue of its own: when two ids in ONE chart share a slot, the later in id order
 *  takes the next free one, so that pair's colours hold only while both are drawn. Deterministic either way. */
export function assignColors(mode: 'light' | 'dark', groups: readonly string[]): Map<string, string> {
  const hues = CATEGORICAL[mode]
  const out = new Map<string, string>()
  const taken = new Set<number>()
  for (const group of [...groups].sort()) {
    if (group === ROLLUP_GROUP) {
      out.set(group, OTHER[mode])
      continue
    }
    let slot = hash(group) % hues.length
    for (let i = 0; i < hues.length && taken.has(slot); i++) slot = (slot + 1) % hues.length
    taken.add(slot)
    out.set(group, hues[slot])
  }
  return out
}

/** FNV-1a: a stable, well-spread string hash. */
function hash(s: string): number {
  let h = 0x811c9dc5
  for (let i = 0; i < s.length; i++) {
    h ^= s.charCodeAt(i)
    h = Math.imul(h, 0x01000193)
  }
  return h >>> 0
}
