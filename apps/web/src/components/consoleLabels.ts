// Control-plane messages in a runner's console. The CP types some lines into a live runner's pane
// (`deliver_message`: the merge lane's batch/merged/failed/conflict/stop notices) and prefixes each with
// `CONTROL_PLANE_LABEL`. The pane is a tmux attach drawn by xterm, so nothing structured marks those lines:
// this module finds the label in the visible grid and lays a badge over it, plus a muted tint over the rest of
// its row, so the operator never reads a CP line as their own typing.
//
// An operator's answer (`deliver_answer`) is the third voice. Its text cannot carry a label (it is the answer to
// the runner's `AskUserQuestion`; a prefix would change the answer), but the node always types it inside a fixed
// frame (`apps/jaahd/answer_delivery.py::compose_answer_message`): that frame is badged `operator` in its own
// tint. So: `control plane` badge = the CP, `operator` badge = an answer from the task page, unbadged = the
// runner or someone typing in this console. A question-less answer (`Answer: …`) has no distinctive frame and
// stays unbadged.
//
// An HTML overlay, not xterm decorations: `registerDecoration` returns nothing on the alternate buffer, which is
// the only buffer a tmux attach ever shows.
import { RAW } from '../theme'

/** Mirror of `CONTROL_PLANE_LABEL` in apps/api/app/live_runner.py (its home); a parity test gates
 *  drift. */
export const CONTROL_PLANE_LABEL = '[control plane]'

/** Mirror of the frame `compose_answer_message` opens a delivered answer with (apps/jaahd/answer_delivery.py);
 *  drift is gated by the same parity test. */
export const OPERATOR_ANSWER_FRAME = "Answer to your question '"

export type LabelKind = 'control' | 'operator'

/** Each voice: the literal that opens its message, what the badge over that literal reads, and its colour. */
const MARKERS: readonly { kind: LabelKind; literal: string; badge: string; title: string; color: string; testId: string }[] = [
  { kind: 'control', literal: CONTROL_PLANE_LABEL, badge: 'control plane', title: 'Control-plane message', color: RAW.dark.warning, testId: 'cp' },
  { kind: 'operator', literal: OPERATOR_ANSWER_FRAME, badge: 'operator', title: 'Operator answer', color: RAW.dark.info, testId: 'op' },
]

/** Where a label sits in the visible grid: at most one hit per row, row 0 = the viewport's top row. `span` is
 *  how many rows the message covers: the label's row plus its wrapped rows. */
export type LabelHit = { row: number; col: number; span: number; kind: LabelKind }

// Only a label that OPENS a message counts: nothing but the prompt gutter (Claude Code's `>`/`❯`) to its left.
// A label quoted mid-row (a diff of this file, a runner echoing it) is not a CP message and gets no badge.
const MESSAGE_GUTTER = /^\s*[>❯]?\s*$/
// Rows that start the runner's own output (Claude Code's turn, tool-result and box glyphs) end a message.
const OUTPUT_MARKERS = new Set(['⏺', '●', '⎿', '│', '─'])

const hasMarker = (text: string) => MARKERS.some(m => text.includes(m.literal))

/** Every message-opening label in ROWS, each row spelled one character per CELL (see `rowCells`). A message
 *  ends at a blank row, a row indented left of the label, a row of runner output, or the next label. */
export function findLabelHits(rows: string[]): LabelHit[] {
  const hits: LabelHit[] = []
  rows.forEach((text, row) => {
    for (const { kind, literal } of MARKERS) {
      const col = text.indexOf(literal)
      if (col === -1 || !MESSAGE_GUTTER.test(text.slice(0, col))) continue
      let span = 1
      for (let next = rows[row + span]; next !== undefined; next = rows[row + span]) {
        const body = next.trimStart()
        const indent = next.length - body.length
        if (!body || indent < col || OUTPUT_MARKERS.has(body[0]) || hasMarker(next)) break
        span += 1
      }
      hits.push({ row, col, span, kind })
      return
    }
  })
  return hits
}

// The slice of xterm's API this module reads, so a test can hand it a plain grid.
type GridCell = { getChars(): string; getWidth(): number }
type GridLine = { translateToString(trimRight?: boolean): string; getCell(x: number, cell?: GridCell): GridCell | undefined }
export type BadgeTerminal = {
  cols: number
  rows: number
  element?: HTMLElement | null
  buffer: { active: { viewportY: number; getLine(y: number): GridLine | undefined } }
  onRender(cb: () => void): { dispose(): void }
}

/** One buffer line as exactly one character per cell, so a string index IS a column. A wide glyph's cell
 *  and its zero-width tail both read as non-label characters; the labels are ASCII. A row without a label
 *  anywhere is not walked cell by cell (the common case, on every repaint). */
function rowCells(line: GridLine | undefined, cols: number): string {
  if (!line || !hasMarker(line.translateToString(true))) return ''
  let out = ''
  let cell: GridCell | undefined
  for (let x = 0; x < cols; x += 1) {
    cell = line.getCell(x, cell)
    const chars = cell?.getChars() ?? ''
    out += chars.length === 1 && cell?.getWidth() === 1 ? chars : ' '
  }
  return out
}

function visibleRows(term: BadgeTerminal): string[] {
  const { viewportY } = term.buffer.active
  return Array.from({ length: term.rows }, (_, r) => rowCells(term.buffer.active.getLine(viewportY + r), term.cols))
}

function paintHit(layer: HTMLElement, hit: LabelHit, cellW: number, cellH: number, cols: number, fontSize: string) {
  const marker = MARKERS.find(m => m.kind === hit.kind) ?? MARKERS[0]
  const top = `${hit.row * cellH}px`
  const height = `${cellH}px`
  const labelW = marker.literal.length * cellW
  // The accent sits in the gutter left of the label (Claude Code's `> `), clear of the wrapped rows' text.
  const gutter = hit.col > 0 ? Math.min(4, cellW / 2) : 0

  const tint = document.createElement('div')
  tint.dataset.testid = `console-${marker.testId}-tint`
  Object.assign(tint.style, {
    position: 'absolute', top, height: `${hit.span * cellH}px`,
    left: `${hit.col * cellW - gutter}px`, width: `${Math.max(0, (cols - hit.col) * cellW + gutter)}px`,
    background: `${RAW.dark.textSecondary}1f`,
    borderLeft: `2px solid ${marker.color}`,
  })

  const badge = document.createElement('div')
  badge.dataset.testid = `console-${marker.testId}-badge`
  badge.textContent = marker.badge
  badge.title = marker.title
  Object.assign(badge.style, {
    position: 'absolute', top, height, lineHeight: height,
    left: `${hit.col * cellW}px`, width: `${labelW}px`,
    boxSizing: 'border-box', borderRadius: '3px', textAlign: 'center', overflow: 'hidden',
    background: RAW.dark.backgroundPaper, color: marker.color,
    border: `1px solid ${marker.color}80`,
    fontFamily: 'system-ui, -apple-system, sans-serif', fontSize, fontWeight: '600',
    letterSpacing: '0.02em', whiteSpace: 'nowrap',
  })
  layer.append(tint, badge)
}

/**
 * Keep TERM's visible control-plane labels and operator-answer frames badged: re-scan after every render (coalesced to one per frame),
 * repaint an overlay laid exactly over `.xterm-screen`. Inert until the terminal has a laid-out screen.
 * The overlay never takes pointer events, so selection, mouse reporting and copy are untouched.
 */
export function attachControlPlaneBadges(term: BadgeTerminal): { dispose(): void } {
  let layer: HTMLDivElement | null = null
  let frame: number | null = null
  // What the layer last drew: an unchanged grid of hits at an unchanged cell box is not rebuilt.
  let painted = ''

  const repaint = () => {
    frame = null
    const screen = term.element?.querySelector('.xterm-screen') as HTMLElement | null | undefined
    if (!screen || !(term.cols > 0) || !(term.rows > 0)) return
    const box = screen.getBoundingClientRect()
    if (!(box.width > 0) || !(box.height > 0)) return
    const hits = findLabelHits(visibleRows(term))
    if (!layer) {
      if (hits.length === 0) return
      layer = document.createElement('div')
      layer.dataset.testid = 'console-cp-labels'
      Object.assign(layer.style, { position: 'absolute', pointerEvents: 'none', zIndex: '5' })
      screen.parentElement?.appendChild(layer)
    }
    const cellW = box.width / term.cols
    const cellH = box.height / term.rows
    const key = JSON.stringify([hits, box.width, box.height, term.cols, term.rows])
    if (key === painted) return
    painted = key
    Object.assign(layer.style, {
      left: `${screen.offsetLeft}px`, top: `${screen.offsetTop}px`, width: `${box.width}px`, height: `${box.height}px`,
    })
    layer.replaceChildren()
    const fontSize = `${Math.max(9, Math.round(cellH * 0.7))}px`
    for (const hit of hits) paintHit(layer, hit, cellW, cellH, term.cols, fontSize)
  }

  const subscription = term.onRender(() => {
    if (frame === null) frame = window.requestAnimationFrame(repaint)
  })
  return {
    dispose() {
      subscription.dispose()
      if (frame !== null) window.cancelAnimationFrame(frame)
      layer?.remove()
      layer = null
    },
  }
}
