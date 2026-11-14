import type { JSX, ReactNode } from 'react'
import Box from '@mui/material/Box'
import { useTheme } from '@mui/material/styles'
import { VISUALLY_HIDDEN } from '../../../ui/visuallyHidden'
import { NODEVAULT_FLOW_CONTAINER_SX, NODEVAULT_FLOW_NARROW } from '../../../ui/listStyles'
import { FONT_SIZE, FONT_WEIGHT } from '../../../theme'

/* The Explanation step's diagram of the actual NodeVault flow (AC-4.1): how the key reaches nodes, and how
 * a credential travels. ONE description (`LANES`), drawn twice: a row layout for a wide container and a
 * column layout below 600px, switched by a CSS container query on the diagram's own width (the Settings
 * card is narrower than the viewport on a desktop), so the text is drawn at ~1:1 and never shrinks out of
 * legibility at 400px. Colours are the theme's CSS variables, so it follows light and dark (an un-themed
 * picture would not — architecture §6 "naive-but-passing traps"). */

type Party = 'you' | 'jaah'
type Box_ = { title: string; sub: string; party: Party }
type Lane = { title: string; boxes: [Box_, Box_, Box_]; edges: [string[], string[]] }

export const LANES: Lane[] = [
  {
    title: 'Setup: the key reaches your nodes',
    boxes: [
      { title: 'Your browser', sub: 'makes the key pair', party: 'you' },
      { title: 'Jaah', sub: 'relays ciphertext only', party: 'jaah' },
      { title: 'Your nodes', sub: 'open and store the key', party: 'you' },
    ],
    edges: [['key, wrapped', 'per node'], ['wrapped key']],
  },
  {
    title: 'Every NodeVault credential',
    boxes: [
      { title: 'Your browser', sub: 'seals with the public key', party: 'you' },
      { title: 'Jaah', sub: 'stores ciphertext only', party: 'jaah' },
      { title: 'Broker on a node', sub: 'opens it for the task', party: 'you' },
    ],
    edges: [['ciphertext'], ['ciphertext']],
  },
]

type Palette = { fg: string; muted: string; accent: string; paper: string; jaahFill: string }

function BoxShape({ x, y, w, h, box, pal }: { x: number; y: number; w: number; h: number; box: Box_; pal: Palette }) {
  const jaah = box.party === 'jaah'
  return (
    <g>
      <Box
        component="rect"
        x={x} y={y} width={w} height={h} rx={8}
        sx={{
          fill: jaah ? pal.jaahFill : pal.paper,
          stroke: jaah ? pal.muted : pal.accent,
          strokeWidth: 1.5,
          strokeDasharray: jaah ? '5 4' : undefined,
        }}
      />
      <Box component="text" x={x + w / 2} y={y + h / 2 - 4} textAnchor="middle" sx={{ fill: pal.fg, fontSize: FONT_SIZE.base, fontWeight: FONT_WEIGHT.semibold }}>
        {box.title}
      </Box>
      <Box component="text" x={x + w / 2} y={y + h / 2 + 14} textAnchor="middle" sx={{ fill: pal.muted, fontSize: FONT_SIZE.sm }}>
        {box.sub}
      </Box>
    </g>
  )
}

function Arrow({ x1, y1, x2, y2, pal, id }: { x1: number; y1: number; x2: number; y2: number; pal: Palette; id: string }) {
  return <Box component="line" x1={x1} y1={y1} x2={x2} y2={y2} markerEnd={`url(#${id})`} sx={{ stroke: pal.accent, strokeWidth: 1.75 }} />
}

/** One layout's picture: the labelled svg at its layout's width, with the arrowhead (`markerId`) its edges point with. */
function Frame({ w, h, titleId, descId, testId, markerId, pal, children }: {
  w: number; h: number; titleId: string; descId: string; testId: string; markerId: string; pal: Palette; children: ReactNode
}) {
  return (
    <Box component="svg" viewBox={`0 0 ${w} ${h}`} width="100%" role="img" aria-labelledby={`${titleId} ${descId}`} data-testid={testId} sx={{ display: 'block', maxWidth: w, fontFamily: 'inherit' }}>
      <defs>
        <marker id={markerId} viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
          <Box component="path" d="M0,0 L10,5 L0,10 z" sx={{ fill: pal.accent }} />
        </marker>
      </defs>
      {children}
    </Box>
  )
}

/** A lane's title, set on its band's top line in both layouts. */
function LaneTitle({ top, title, pal }: { top: number; title: string; pal: Palette }) {
  return <Box component="text" x={0} y={top + 15} sx={{ fill: pal.fg, fontSize: FONT_SIZE.md, fontWeight: FONT_WEIGHT.semibold }}>{title}</Box>
}

/* Row layout: 3 boxes of BOX_W, 2 edges of EDGE_W, one lane per band. */
const ROW = { W: 720, BOX_W: 186, EDGE_W: 81, BOX_H: 60, LANE_H: 112, TOP: 22 }

function RowDiagram({ pal, titleId, descId }: { pal: Palette; titleId: string; descId: string }) {
  const h = ROW.LANE_H * LANES.length
  return (
    <Frame w={ROW.W} h={h} titleId={titleId} descId={descId} testId="nodevault-diagram-row" markerId="nv-arrow-row" pal={pal}>
      {LANES.map((lane, li) => {
        const top = li * ROW.LANE_H
        const boxY = top + ROW.TOP + 8
        return (
          <g key={lane.title}>
            <LaneTitle top={top} title={lane.title} pal={pal} />
            {lane.boxes.map((box, bi) => (
              <BoxShape key={bi} x={bi * (ROW.BOX_W + ROW.EDGE_W)} y={boxY} w={ROW.BOX_W} h={ROW.BOX_H} box={box} pal={pal} />
            ))}
            {lane.edges.map((label, ei) => {
              const x1 = (ei + 1) * ROW.BOX_W + ei * ROW.EDGE_W + 4
              const x2 = x1 + ROW.EDGE_W - 8
              const y = boxY + ROW.BOX_H / 2 + 8
              return (
                <g key={ei}>
                  <Arrow x1={x1} y1={y} x2={x2} y2={y} pal={pal} id="nv-arrow-row" />
                  {label.map((line, i) => (
                    <Box component="text" key={i} x={(x1 + x2) / 2} y={y - 8 - (label.length - 1 - i) * 14} textAnchor="middle" sx={{ fill: pal.muted, fontSize: FONT_SIZE.sm }}>
                      {line}
                    </Box>
                  ))}
                </g>
              )
            })}
          </g>
        )
      })}
    </Frame>
  )
}

/* Column layout: boxes stacked full width, the arrow down the middle and its label beside it. */
const COL = { W: 340, BOX_H: 54, EDGE_H: 40, LANE_GAP: 22, TITLE_H: 26 }

function ColumnDiagram({ pal, titleId, descId }: { pal: Palette; titleId: string; descId: string }) {
  const laneH = COL.TITLE_H + 3 * COL.BOX_H + 2 * COL.EDGE_H
  const h = LANES.length * laneH + (LANES.length - 1) * COL.LANE_GAP
  return (
    <Frame w={COL.W} h={h} titleId={titleId} descId={descId} testId="nodevault-diagram-column" markerId="nv-arrow-col" pal={pal}>
      {LANES.map((lane, li) => {
        const top = li * (laneH + COL.LANE_GAP)
        return (
          <g key={lane.title}>
            <LaneTitle top={top} title={lane.title} pal={pal} />
            {lane.boxes.map((box, bi) => (
              <BoxShape key={bi} x={1} y={top + COL.TITLE_H + bi * (COL.BOX_H + COL.EDGE_H)} w={COL.W - 2} h={COL.BOX_H} box={box} pal={pal} />
            ))}
            {lane.edges.map((label, ei) => {
              const y1 = top + COL.TITLE_H + (ei + 1) * COL.BOX_H + ei * COL.EDGE_H + 3
              const y2 = y1 + COL.EDGE_H - 6
              const x = 96
              return (
                <g key={ei}>
                  <Arrow x1={x} y1={y1} x2={x} y2={y2} pal={pal} id="nv-arrow-col" />
                  <Box component="text" x={x + 14} y={(y1 + y2) / 2 + 4} sx={{ fill: pal.muted, fontSize: FONT_SIZE.sm }}>
                    {label.join(' ')}
                  </Box>
                </g>
              )
            })}
          </g>
        )
      })}
    </Frame>
  )
}

export default function FlowDiagram(): JSX.Element {
  const theme = useTheme()
  const v = theme.vars?.palette ?? theme.palette
  const pal: Palette = {
    fg: v.text.primary,
    muted: v.text.secondary,
    accent: v.primary.main,
    paper: v.background.paper,
    jaahFill: v.action.hover,
  }
  return (
    <Box data-testid="nodevault-diagram" sx={[NODEVAULT_FLOW_CONTAINER_SX, { width: '100%' }]}>
      <span id="nv-diagram-title" style={VISUALLY_HIDDEN} aria-hidden="true">How NodeVault moves your key and your secrets</span>
      <span id="nv-diagram-desc" style={VISUALLY_HIDDEN} aria-hidden="true">
        Your browser makes the key pair and wraps the private key for each node; Jaah relays only that
        ciphertext; your nodes open and store it. A NodeVault credential is sealed in your browser with the
        public key; Jaah stores only ciphertext; the broker on a node opens it for the task.
      </span>
      <Box sx={{ display: 'block', [NODEVAULT_FLOW_NARROW]: { display: 'none' } }}>
        <RowDiagram pal={pal} titleId="nv-diagram-title" descId="nv-diagram-desc" />
      </Box>
      <Box sx={{ display: 'none', [NODEVAULT_FLOW_NARROW]: { display: 'block' } }}>
        <ColumnDiagram pal={pal} titleId="nv-diagram-title" descId="nv-diagram-desc" />
      </Box>
    </Box>
  )
}
