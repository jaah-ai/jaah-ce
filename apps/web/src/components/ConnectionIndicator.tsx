import { useEffect, useState } from 'react'
import Box from '@mui/material/Box'
import Typography from '@mui/material/Typography'
import type { EventStreamState } from '../hooks/useEventStream'
import type { ComponentHealth } from '../hooks/useHealth'
import { VISUALLY_HIDDEN } from '../ui/visuallyHidden'
import StatusDot from '../ui/StatusDot'
import { FONT_SIZE, SIZE } from '../theme'
import Tip from '../ui/Tip'

// The console header badge: SSE state plus composite 'degraded' (amber); with `components` from
// useHealth(), a per-component breakdown on hover. It shows only when something is off: a healthy connection, and the
// first connect on load, render nothing.
export type IndicatorState = EventStreamState | 'degraded'

/** The states that render: everything but a healthy connection and the first connect. */
type OffState = Exclude<IndicatorState, 'connected'>

const LABEL: Record<OffState, string> = {
  degraded: 'Degraded',
  reconnecting: 'Reconnecting…',
  down: 'Disconnected — polling',
}

// Palette tokens, not literals, so both colour schemes follow the theme (AC8).
const STATE_COLOR: Record<OffState, string> = {
  degraded: 'warning.main',
  reconnecting: 'warning.main',
  down: 'error.main',
}


/* "Reconnecting…" only once a drop outlasts this: the stream re-opens within a second or two on a deploy
 * or a network blip, and the badge flashed amber on every one. Until then the badge keeps what it last said
 * (nothing, when that was healthy); a first connect, which the stream also reports as `reconnecting`, shows nothing.
 * "Disconnected — polling" has its own, longer clock (`useEventStream`'s `DOWN_AFTER_MS`). */
export const RECONNECT_GRACE_MS = 4000

type Shown = IndicatorState | 'connecting'

function useGraced(state: IndicatorState): Shown {
  const [settled, setSettled] = useState<Shown>(state === 'reconnecting' ? 'connecting' : state)
  const [graceOver, setGraceOver] = useState(false)
  useEffect(() => {
    if (state !== 'reconnecting') {
      setSettled(state)
      setGraceOver(false)
      return
    }
    const timer = window.setTimeout(() => setGraceOver(true), RECONNECT_GRACE_MS)
    return () => window.clearTimeout(timer)
  }, [state])
  return state === 'reconnecting' && !graceOver ? settled : state
}

// ✓ healthy · ✗ down · – not asserted (jaahd 'unknown' — e.g. postgres mode).
function glyph(value: boolean | ComponentHealth['jaahd']): string {
  if (value === 'unknown') return '–'
  if (value === true || value === 'online') return '✓'
  return '✗'
}

// The overall word leads: the badge is a bare dot, so hover is the only place the state is spelled out.
function Breakdown({
  state,
  components,
  showDaemon,
}: {
  state: OffState
  components: ComponentHealth
  showDaemon: boolean
}) {
  const rows: Array<[string, boolean | ComponentHealth['jaahd']]> = [
    ['API', components.api],
    ['WS', components.sse],
  ]
  // The daemon row is the operator's: `GET /api/health/components` answers tenants `'unknown'`, so the
  // row could only read "–". The Sidebar passes `is_operator`, stricter than the API's `is_tenant_org`;
  // this component only honours the verdict.
  if (showDaemon) rows.push(['jaahd', components.jaahd])
  const rowSx = { display: 'flex', justifyContent: 'space-between', gap: 2, py: 0 } as const
  return (
    <Box data-testid="health-breakdown" sx={{ display: 'flex', flexDirection: 'column', gap: 0, minWidth: SIZE.fieldSm }}>
      <Box data-testid="hovercard-row" sx={rowSx}>
        <Typography component="span" variant="body2" data-testid="hovercard-label">
          {LABEL[state]}
        </Typography>
      </Box>
      {rows.map(([label, value]) => (
        <Box key={label} data-testid="hovercard-row" sx={rowSx}>
          <Typography component="span" variant="body2" data-testid="hovercard-label">
            {label}
          </Typography>
          <Typography component="span" variant="body2" data-testid="hovercard-value">
            {glyph(value)}
          </Typography>
        </Box>
      ))}
    </Box>
  )
}

export default function ConnectionIndicator({
  state,
  components,
  compact = false,
  showDaemon = true,
}: {
  state: IndicatorState
  components?: ComponentHealth
  /** Whether the breakdown names `jaahd`. The Sidebar passes `false` for a non-operator. */
  showDaemon?: boolean
  /** Render as a bare coloured dot; the label stays in the DOM visually hidden (assistive tech, tests).
      Used in the Sidebar, including the 60px mobile bar. */
  compact?: boolean
}) {
  const graced = useGraced(state)
  if (graced === 'connected' || graced === 'connecting') return null
  const shown: OffState = graced
  const badge = (
    <Box
      component="span"
      data-testid="connection-indicator"
      // Under Emotion the class name is a hash, so tests read the state off `data-state`.
      data-state={shown}
      // The bare dot's name.
      aria-label={compact ? `Connection: ${LABEL[shown]}` : undefined}
      sx={{
        display: 'inline-flex',
        alignItems: 'center',
        gap: 1,
        fontSize: FONT_SIZE.base,
        color: STATE_COLOR[shown],
      }}
    >
      <StatusDot data-testid="connection-indicator-dot" color="currentColor" />
      {/* Wrapped (not a bare text node) so `compact` can hide it without hiding the dot. */}
      <Typography
        component="span"
        variant="body2"
        data-testid="connection-indicator-label"
        // jsdom applies no layout, so unit tests assert the hidden rendering was chosen; e2e measures it.
        data-visually-hidden={compact ? 'true' : 'false'}
        sx={compact ? VISUALLY_HIDDEN : undefined}
      >
        {LABEL[shown]}
      </Typography>
    </Box>
  )

  return (
    <Tip title={components
      ? <Breakdown state={shown} components={components} showDaemon={showDaemon} />
      : `Connection: ${LABEL[shown]}`}
    >
      {badge}
    </Tip>
  )
}
