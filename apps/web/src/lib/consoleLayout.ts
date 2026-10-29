// The console layout designer's PURE CORE: the tile tree, its geometry, the hit-test that turns a
// pointer into a drop zone, and the reducer that applies one. No React/DOM/measurement, so jsdom tests
// can prove the geometry. The popup (LayoutDesigner.tsx) owns pointers and CSS; the multi-tile runtime
// owns live terminals and is the only thing that can measure a cell — hence `splitRefusal` takes CellMetrics.
//
// Model (Visual Studio editor-group semantics, D2): a Tile is a rectangle with a TAB STRIP (one tab
// visible); a Split divides a rectangle into two SIMULTANEOUSLY VISIBLE children. Tiles are disjoint and,
// with gutters, cover the canvas exactly — no overlay, floating window or z-order.

/** A tmux window as the enumeration endpoint reports it. `id` (the tmux `@N`) is THE identity. */
export type WindowRef = { id: string; index: number; name: string }

/** One rectangle of the grid. `tabs` is never empty in a reachable model (see `applyDrop`). */
export type Tile = { key: string; tabs: WindowRef[]; activeTab: number }

/** `dir: 'row'` = side by side, `'col'` = stacked; `a` is always top/left. `ratio` is `a`'s share of the
 *  space after the gutter, strictly inside (0,1). */
export type Split = { key: string; dir: 'row' | 'col'; a: LayoutNode; b: LayoutNode; ratio: number }

/** Named LayoutNode, never `Node`: `Node` is a DOM global and shadowing it makes every DOM type lie. */
export type LayoutNode = Tile | Split

export type LayoutModel = { version: 1; root: LayoutNode }

/** Measured by the caller. `chromeH` is per-tile non-terminal height, so a tile of height `h` yields
 *  `(h - chromeH) / cellH` rows. */
export type CellMetrics = { cellW: number; cellH: number; chromeH: number }

export type Rect = { x: number; y: number; w: number; h: number }

export type Edge = 'top' | 'bottom' | 'left' | 'right'

/** Where a release would land the dragged window. `tab` groups; `edge` splits. */
export type DropZone =
  | { kind: 'tab'; tileKey: string }
  | { kind: 'edge'; tileKey: string; edge: Edge }

// ── Constants that the popup and the tests both read, so neither can drift from the other ──────────

/**
 * THE GUTTER MODEL (AC3.4's tiling invariant): every Split contributes EXACTLY ONE gutter, `GUTTER` px
 * wide, spanning its own box's cross axis. The suite predicts the total from the tree alone, so
 * "tiles + gutters == canvas" is a real cross-check of `layoutRects`.
 */
export const GUTTER = 6

/**
 * The minimum usable terminal, in CHARACTERS — a FIXED INPUT: when the grid does not fit, refuse the split,
 * never shrink these.
 */
export const MIN_COLS = 80
export const MIN_ROWS = 20

/** Capped at 4 by legibility arithmetic: a fifth MIN_COLS x MIN_ROWS terminal fits no reachable panel.
 *  Tabs per tile are unlimited. */
export const MAX_VISIBLE_TILES = 4

/** The top band counted as a tile's TAB STRIP for hit-testing; the popup styles the strip from this. */
export const TAB_ZONE_H = 28

/** Edge-zone depth as a fraction of the body; the centre is a TAB drop (VS Code behaviour), so no dead zone. */
export const EDGE_BAND = 0.25

/**
 * Widest tab label, and the floor before the strip scrolls. The FLOOR was set at 20 by LOOKING at renders:
 * at 12-13 the tail was a word FRAGMENT (`acme-6|…shment`), naming the console but not the task; at 20 it
 * reads (`acme-6|task…ablishment`). A crowded strip scrolls (`overflow-x: auto`) instead of shredding labels.
 */
export const TAB_CHARS_MAX = 24
export const TAB_CHARS_MIN = 20

/** Per-tab horizontal chrome (padding + border) and the monospace advance the strip is styled at. */
export const TAB_CHROME_PX = 16
export const TAB_CHAR_PX = 6.6

const ELLIPSIS = '…'

// Opaque tile/split identity, NEVER a window id (windows move between tiles). Monotonic so re-renders never
// reshuffle reconciliation.
let keySeq = 0
function mintKey(): string {
  keySeq += 1
  return `k${keySeq}`
}

/**
 * Raise the mint counter past every key in `node`. REQUIRED on every RESTORED model: `keySeq` restarts at 0
 * per page load, so the next drop would mint a duplicate key (collapsing rects in `layoutRects`, duplicate
 * React keys). `parseLayoutModel` rejects duplicates within a model; this covers the rest.
 */
function adoptKeys(node: LayoutNode): void {
  const seen = (key: string) => {
    const m = /^k(\d+)$/.exec(key)
    if (m) keySeq = Math.max(keySeq, Number(m[1]))
  }
  const walk = (n: LayoutNode) => {
    seen(n.key)
    if (isTile(n)) return
    walk(n.a)
    walk(n.b)
  }
  walk(node)
}

export function isTile(node: LayoutNode): node is Tile {
  return 'tabs' in node
}

// ── Construction ───────────────────────────────────────────────────────────────────────────────────

/**
 * The on-open arrangement and Reset's target (D8): ONE tile holding every window as a tab. Empty `windows`
 * yields the ONE model with an empty tile — unreachable by drop, and the popup refuses to open on it.
 */
export const TABBED_DEFAULT = (windows: WindowRef[]): LayoutModel => ({
  version: 1,
  root: { key: mintKey(), tabs: windows.map((w) => ({ ...w })), activeTab: 0 },
})

// ── Labels ─────────────────────────────────────────────────────────────────────────────────────────

/**
 * Shorten `name` to at most `maxChars` by removing the MIDDLE: the head names the SLOT (`acme-7|…`) and the
 * tail the TASK, and only a middle elision keeps both (a pure function lets tests assert that). Split:
 * `head = ceil((maxChars - 1) / 2)`, tail the rest, `…` the missing character. A fitting name is untouched.
 */
export function elideMiddle(name: string, maxChars: number): string {
  const limit = Math.floor(maxChars)
  if (!(limit > 0)) return ''
  if (name.length <= limit) return name
  if (limit === 1) return ELLIPSIS
  const available = limit - 1
  const head = Math.ceil(available / 2)
  const tail = available - head
  return name.slice(0, head) + ELLIPSIS + (tail > 0 ? name.slice(name.length - tail) : '')
}

/** Characters per tab, clamped: the floor (`TAB_CHARS_MIN`) makes the strip scroll rather than shred labels;
 *  the ceiling stops one tab spanning the tile. */
export function tabLabelChars(widthPx: number, tabCount: number): number {
  if (tabCount <= 0) return TAB_CHARS_MAX
  const perTab = widthPx / tabCount - TAB_CHROME_PX
  const chars = Math.floor(perTab / TAB_CHAR_PX)
  return Math.min(TAB_CHARS_MAX, Math.max(TAB_CHARS_MIN, chars))
}

// ── Geometry ───────────────────────────────────────────────────────────────────────────────────────

function splitBoxes(node: Split, r: Rect): [Rect, Rect] {
  if (node.dir === 'row') {
    const inner = Math.max(0, r.w - GUTTER)
    const aw = inner * node.ratio
    return [
      { x: r.x, y: r.y, w: aw, h: r.h },
      { x: r.x + aw + GUTTER, y: r.y, w: inner - aw, h: r.h },
    ]
  }
  const inner = Math.max(0, r.h - GUTTER)
  const ah = inner * node.ratio
  return [
    { x: r.x, y: r.y, w: r.w, h: ah },
    { x: r.x, y: r.y + ah + GUTTER, w: r.w, h: inner - ah },
  ]
}

/** Every tile's rect, keyed by tile key. UN-ROUNDED: rounding boxes independently opens 1px seams/overlaps. */
export function layoutRects(model: LayoutModel, canvas: { w: number; h: number }): Map<string, Rect> {
  const out = new Map<string, Rect>()
  const walk = (node: LayoutNode, r: Rect) => {
    if (isTile(node)) {
      out.set(node.key, r)
      return
    }
    const [ra, rb] = splitBoxes(node, r)
    walk(node.a, ra)
    walk(node.b, rb)
  }
  walk(model.root, { x: 0, y: 0, w: canvas.w, h: canvas.h })
  return out
}

/** The half of `r` that a drop on `edge` would claim — the same box the split then produces. */
export function edgeHalfRect(r: Rect, edge: Edge): Rect {
  if (edge === 'left' || edge === 'right') {
    const w = Math.max(0, r.w - GUTTER) / 2
    return { x: edge === 'left' ? r.x : r.x + r.w - w, y: r.y, w, h: r.h }
  }
  const h = Math.max(0, r.h - GUTTER) / 2
  return { x: r.x, y: edge === 'top' ? r.y : r.y + r.h - h, w: r.w, h }
}

// ── Queries ────────────────────────────────────────────────────────────────────────────────────────

export function tiles(model: LayoutModel): Tile[] {
  const out: Tile[] = []
  const walk = (node: LayoutNode) => {
    if (isTile(node)) {
      out.push(node)
      return
    }
    walk(node.a)
    walk(node.b)
  }
  walk(model.root)
  return out
}

export function countTiles(model: LayoutModel): number {
  return tiles(model).length
}

export function findTile(model: LayoutModel, tileKey: string): Tile | null {
  return tiles(model).find((t) => t.key === tileKey) ?? null
}

/**
 * The `{Project}-{N}` prefix every window of ONE pool slot carries — a CROSS-BOUNDARY FORMAT CONTRACT with
 * its single composer `apps/jaahd/fleet_launch.py::slot_ref` (via `slot_window_name`). The main clone is
 * bare `{Project}`, never `{Project}-0`. The node's sanitising arms are unneeded for a numeric `number`.
 */
export function slotWindowPrefix(project: string, number: number): string {
  return number === 0 ? project : `${project}-${number}`
}

/**
 * Does the LIVE window list contain a window of this slot — does it have a session of its own?
 * A restore must ask this about the LIVE LIST: tiles showing a neighbour's window are legitimate, but
 * restoring any layout onto a slot with no window of its own strands it — the view path is attach-only
 * (`apps/jaahd/console.py::_open_view_pty`), so the console attaches only to others' runners.
 * `restoreLayout` cannot catch this (a neighbour's window is not gone).
 * MATCHED ON THE `|` BOUNDARY, never a bare `startsWith`: `acme-1` is a prefix of `acme-19`.
 */
export function hasSlotWindow(windows: WindowRef[], project: string, number: number): boolean {
  const ref = slotWindowPrefix(project, number)
  return windows.some((w) => w.name === ref || w.name.startsWith(`${ref}|`))
}

export function findWindow(model: LayoutModel, windowId: string): WindowRef | null {
  for (const t of tiles(model)) {
    const w = t.tabs.find((x) => x.id === windowId)
    if (w) return w
  }
  return null
}

/** Every window in the model, in tile order then tab order. */
export function allWindows(model: LayoutModel): WindowRef[] {
  return tiles(model).flatMap((t) => t.tabs)
}

// ── Hit-test ───────────────────────────────────────────────────────────────────────────────────────

const EDGE_ORDER: Edge[] = ['left', 'right', 'top', 'bottom']

/**
 * Pointer (canvas coords) + rect map -> the drop zone under it, or null outside every tile.
 * The top `tabBandH` px group; below, the nearest edge within `EDGE_BAND` claims it, else the centre groups.
 * Ties resolve left/right/top/bottom (fixed). `tabBandH` is a parameter for callers drawing at a scale.
 */
export function hitTest(
  point: { x: number; y: number },
  rects: Map<string, Rect>,
  tabBandH: number = TAB_ZONE_H,
): DropZone | null {
  for (const [tileKey, r] of rects) {
    const inside = point.x >= r.x && point.x < r.x + r.w && point.y >= r.y && point.y < r.y + r.h
    if (!inside) continue
    const bandH = Math.min(tabBandH, r.h)
    if (point.y < r.y + bandH) return { kind: 'tab', tileKey }
    const bodyY = r.y + bandH
    const bodyH = r.h - bandH
    if (!(bodyH > 0) || !(r.w > 0)) return { kind: 'tab', tileKey }
    const fx = (point.x - r.x) / r.w
    const fy = (point.y - bodyY) / bodyH
    const depth: Record<Edge, number> = { left: fx, right: 1 - fx, top: fy, bottom: 1 - fy }
    let best: Edge = 'left'
    for (const e of EDGE_ORDER) if (depth[e] < depth[best]) best = e
    if (depth[best] < EDGE_BAND) return { kind: 'edge', tileKey, edge: best }
    return { kind: 'tab', tileKey }
  }
  return null
}

export function edgeDir(edge: Edge): 'row' | 'col' {
  return edge === 'left' || edge === 'right' ? 'row' : 'col'
}

// ── The floor ──────────────────────────────────────────────────────────────────────────────────────

/** Why a split is refused, as the text the disabled zone shows — or null when allowed. A string, because a
 *  zone that silently does nothing is the failure this avoids. */
export function splitRefusal(
  model: LayoutModel,
  tileKey: string,
  dir: 'row' | 'col',
  canvas: { w: number; h: number },
  cells: CellMetrics,
): string | null {
  const r = layoutRects(model, canvas).get(tileKey)
  if (!r) return 'That tile is no longer part of this layout.'
  if (countTiles(model) >= MAX_VISIBLE_TILES) {
    return `At most ${MAX_VISIBLE_TILES} tiles fit side by side — group a window into an existing tile instead.`
  }
  const half =
    dir === 'row'
      ? { w: Math.max(0, r.w - GUTTER) / 2, h: r.h }
      : { w: r.w, h: Math.max(0, r.h - GUTTER) / 2 }
  const cols = Math.floor(half.w / cells.cellW)
  const rows = Math.floor((half.h - cells.chromeH) / cells.cellH)
  if (cols < MIN_COLS || rows < MIN_ROWS) {
    return `Enlarge the console to split further — a tile needs ${MIN_COLS}×${MIN_ROWS} characters and this split gives ${Math.max(0, cols)}×${Math.max(0, rows)}.`
  }
  return null
}

// ── The drop reducer ───────────────────────────────────────────────────────────────────────────────

function mapNode(node: LayoutNode, fn: (n: LayoutNode) => LayoutNode | null): LayoutNode | null {
  if (isTile(node)) return fn(node)
  const a = mapNode(node.a, fn)
  const b = mapNode(node.b, fn)
  // A split that lost a child collapses to the survivor, keeping the grid gapless.
  if (a && b) return { ...node, a, b }
  return a ?? b
}

function withoutWindow(root: LayoutNode, windowId: string): LayoutNode | null {
  return mapNode(root, (n) => {
    const t = n as Tile
    const at = t.tabs.findIndex((w) => w.id === windowId)
    if (at < 0) return t
    const tabs = t.tabs.filter((_, i) => i !== at)
    if (tabs.length === 0) return null
    const activeTab = at < t.activeTab ? t.activeTab - 1 : Math.min(t.activeTab, tabs.length - 1)
    return { ...t, tabs, activeTab }
  })
}

function replaceTile(root: LayoutNode, tileKey: string, fn: (t: Tile) => LayoutNode): LayoutNode {
  return (
    mapNode(root, (n) => {
      const t = n as Tile
      return t.key === tileKey ? fn(t) : t
    }) ?? root
  )
}

/**
 * Apply a drop: MOVE `windowId` to `zone` (removed before inserted), so "a window appears in at most one
 * tile" (AC3.6) is the reducer's property. Returns the SAME model for an impossible drop (unknown window,
 * target dissolved by the removal, tree would empty). The floor (`splitRefusal`) is the caller's check.
 */
export function applyDrop(model: LayoutModel, windowId: string, zone: DropZone): LayoutModel {
  const win = findWindow(model, windowId)
  if (!win) return model
  const stripped = withoutWindow(model.root, windowId)
  if (!stripped) return model
  const base: LayoutModel = { version: 1, root: stripped }
  if (!findTile(base, zone.tileKey)) return model

  if (zone.kind === 'tab') {
    return {
      version: 1,
      root: replaceTile(stripped, zone.tileKey, (t) => ({
        ...t,
        tabs: [...t.tabs, win],
        activeTab: t.tabs.length,
      })),
    }
  }

  const fresh: Tile = { key: mintKey(), tabs: [win], activeTab: 0 }
  const first = zone.edge === 'left' || zone.edge === 'top'
  return {
    version: 1,
    root: replaceTile(stripped, zone.tileKey, (t) => ({
      key: mintKey(),
      dir: edgeDir(zone.edge),
      ratio: 0.5,
      a: first ? fresh : t,
      b: first ? t : fresh,
    })),
  }
}

/**
 * The drop PREVIEW rect, computed on the POST-REMOVAL tree: if the dragged window was a neighbour's last
 * tab, that tile vanishes and the target grows before splitting.
 */
export function dropPreview(
  model: LayoutModel,
  windowId: string,
  zone: DropZone,
  canvas: { w: number; h: number },
): Rect | null {
  if (!findWindow(model, windowId)) return null
  const stripped = withoutWindow(model.root, windowId)
  if (!stripped) return null
  const base: LayoutModel = { version: 1, root: stripped }
  const r = layoutRects(base, canvas).get(zone.tileKey)
  if (!r) return null
  return zone.kind === 'tab' ? r : edgeHalfRect(r, zone.edge)
}

/** Out-of-range indexes are ignored, not clamped. */
export function setActiveTab(model: LayoutModel, tileKey: string, index: number): LayoutModel {
  const t = findTile(model, tileKey)
  if (!t || index < 0 || index >= t.tabs.length || index === t.activeTab) return model
  return { version: 1, root: replaceTile(model.root, tileKey, (tile) => ({ ...tile, activeTab: index })) }
}

// ── Validation ─────────────────────────────────────────────────────────────────────────────────────

function parseNode(raw: unknown, seen: Set<string>): LayoutNode | null {
  if (!raw || typeof raw !== 'object') return null
  const o = raw as Record<string, unknown>
  if (typeof o.key !== 'string' || o.key === '' || seen.has(o.key)) return null
  seen.add(o.key)

  if (Array.isArray(o.tabs)) {
    const tabs: WindowRef[] = []
    for (const t of o.tabs) {
      if (!t || typeof t !== 'object') return null
      const w = t as Record<string, unknown>
      if (typeof w.id !== 'string' || w.id === '') return null
      if (typeof w.index !== 'number' || !Number.isFinite(w.index)) return null
      if (typeof w.name !== 'string') return null
      tabs.push({ id: w.id, index: w.index, name: w.name })
    }
    if (tabs.length === 0) return null
    if (typeof o.activeTab !== 'number' || !Number.isInteger(o.activeTab)) return null
    if (o.activeTab < 0 || o.activeTab >= tabs.length) return null
    return { key: o.key, tabs, activeTab: o.activeTab }
  }

  if (o.dir !== 'row' && o.dir !== 'col') return null
  if (typeof o.ratio !== 'number' || !(o.ratio > 0) || !(o.ratio < 1)) return null
  const a = parseNode(o.a, seen)
  const b = parseNode(o.b, seen)
  if (!a || !b) return null
  return { key: o.key, dir: o.dir, a, b, ratio: o.ratio }
}

/**
 * Validate an untrusted (stored) model — `localStorage` is writable by extensions, old builds, devtools.
 * `version` other than exactly 1 is DISCARDED, not migrated (Reset is one keystroke away).
 */
export function parseLayoutModel(raw: unknown): LayoutModel | null {
  if (!raw || typeof raw !== 'object') return null
  const o = raw as Record<string, unknown>
  if (o.version !== 1) return null
  const root = parseNode(o.root, new Set<string>())
  if (!root) return null
  return { version: 1, root }
}

/** What `restoreLayout` made of a stored model: the usable layout, and what it had to drop. */
export type RestoreResult = { model: LayoutModel | null; dropped: WindowRef[] }

/**
 * Reconcile a STORED model against the LIVE window list (what `loadLayout` cannot do).
 * MATCHING IS BY `@id`, NEVER BY INDEX: `renumber-windows` is ON in the host's tmux config, so an index can
 * name a live but DIFFERENT window — silently showing someone else's session. A miss drops the tile.
 * Surviving tabs take the LIVE row's name/index. `model: null` = nothing usable (every window gone, or more
 * tiles than can be shown); the caller falls back to its default.
 */
export function restoreLayout(stored: LayoutModel, live: WindowRef[]): RestoreResult {
  const byId = new Map(live.map((w) => [w.id, w]))
  const dropped: WindowRef[] = []

  const root = mapNode(stored.root, (n) => {
    const t = n as Tile
    const activeId = t.tabs[t.activeTab]?.id
    const tabs: WindowRef[] = []
    for (const tab of t.tabs) {
      const liveRow = byId.get(tab.id)
      if (liveRow) tabs.push({ ...liveRow })
      else dropped.push(tab)
    }
    if (tabs.length === 0) return null
  // The ACTIVE TAB follows the window, not the slot position.
    const stillActive = tabs.findIndex((w) => w.id === activeId)
    return { ...t, tabs, activeTab: stillActive >= 0 ? stillActive : Math.min(t.activeTab, tabs.length - 1) }
  })

  if (!root) return { model: null, dropped }
  const model: LayoutModel = { version: 1, root }
  if (countTiles(model) > MAX_VISIBLE_TILES) return { model: null, dropped }
  adoptKeys(root)
  return { model, dropped }
}

/**
 * The arrangement the DESIGNER opens on, from the layout the console is CURRENTLY rendering, reconciled in
 * BOTH directions:
 *   - windows KILLED since Apply are pruned by id (`restoreLayout`), never re-attached to a dead `@id`;
 *   - windows CREATED since Apply are appended as tabs on the first tile, so they are draggable.
 * `null` → the caller uses the tabbed default. Differs from `TABBED_DEFAULT(live)` only in ARRANGEMENT.
 */
export function openingModel(current: LayoutModel, live: WindowRef[]): LayoutModel | null {
  const { model } = restoreLayout(current, live)
  if (!model) return null

  const placed = new Set(allWindows(model).map((w) => w.id))
  const unplaced = live.filter((w) => !placed.has(w.id))
  if (unplaced.length === 0) return model

  const first = tiles(model)[0]
  if (!first) return null
  return {
    version: 1,
    root: replaceTile(model.root, first.key, (t) => ({
      ...t,
      tabs: [...t.tabs, ...unplaced.map((w) => ({ ...w }))],
    })),
  }
}

// ── Handing the model to tmux ────────────────────────────────────────────────────────────────────
// The node builds ONE tmux window whose PANES are the tiles and returns one PTY. `encodeLayoutSpec` is the
// ONLY writer of the wire form; `apps/jaahd/console.py`'s `parse_layout_spec` is the only reader:
//
//   node  := tile | split
//   tile  := '@' <digits>
//   split := ('r'|'c') <percent> '(' node ',' node ')'      'r' = side by side, 'c' = stacked
//
// A string (one URL query parameter beside `view`). `percent` is the FIRST child's share, like `Split.ratio`.

/** The percentage bounds the node enforces. Mirrored so a spec this app emits is never one it refuses. */
export const SPEC_MIN_PERCENT = 10
export const SPEC_MAX_PERCENT = 90

/**
 * The node's `layout=` parameter, or null when inexpressible (a tile missing its active tab — unreachable).
 * ONLY EACH TILE'S ACTIVE TAB IS SENT; switching tabs is tmux's own window list inside the pane.
 */
export function encodeLayoutSpec(model: LayoutModel): string | null {
  const encode = (node: LayoutNode): string | null => {
    if (isTile(node)) {
      const active = node.tabs[Math.min(node.activeTab, node.tabs.length - 1)]
      return active ? active.id : null
    }
    const first = encode(node.a)
    const second = encode(node.b)
    if (first === null || second === null) return null
    // Clamped, not rejected: tmux would clamp anyway, and refusing over rounding is worse.
    const percent = Math.min(
      SPEC_MAX_PERCENT,
      Math.max(SPEC_MIN_PERCENT, Math.round(node.ratio * 100)),
    )
    return `${node.dir === 'row' ? 'r' : 'c'}${percent}(${first},${second})`
  }
  return encode(model.root)
}

/**
 * The one place that decides the terminal's font size. A layout uses 11 (one terminal holds the whole grid;
 * ~15% more cells, so a quadrant clears the 80x20 floor with room); the single-pane console keeps 13.
 */
export const MULTI_TILE_FONT_SIZE = 11
export const SINGLE_TILE_FONT_SIZE = 13

export function tileFontSize(tileCount: number): number {
  return tileCount > 1 ? MULTI_TILE_FONT_SIZE : SINGLE_TILE_FONT_SIZE
}

/** Per-tile chrome in ROWS: tmux's own one-row status line (label + clickable window list). */
export const TILE_STATUS_ROWS = 1
