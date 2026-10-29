import type { JSX } from 'react'
import Box from '@mui/material/Box'
import { STAGE_LABELS } from '../../../components/addNode/api'
import { absoluteTime, relativeAge } from '../../../lib/relativeTime'
import { DetailSection } from '../../../ui/detail/DetailPage'
import type { HostView } from './api'
import { failedStageWords, type MachinePresence } from './hostFacts'

/* RECENT EVENTS — what happened to this host lately, newest first, so a stuck Add node reads as a
 * sequence ("Adding node x", "Waiting for your machine", "Machine last reported") instead of one chip. There is no
 * event history table by design: every line is SYNTHESIZED from timestamps the hosts and vnode-hosts reads already
 * carry. A reason is a sentence the person acts on, so it wraps at 390px and is never cut. */

type Tone = 'info' | 'error'
type HostEvent = { key: string; text: string; at: string | null; tone: Tone }

/** `: <error>`, else ` while <stage>.`, else `.` — the end of "Adding x failed". */
function failedCause(order: NonNullable<HostView['order']>): string {
  if (order.error) return `: ${order.error}`
  const stage = failedStageWords(order)
  return stage ? ` while ${stage}.` : '.'
}

const atMs = (iso: string | null) => (iso ? Date.parse(iso) : NaN)

function hostEvents(host: HostView, machine: MachinePresence, hostFault: string | null | undefined): HostEvent[] {
  const events: HostEvent[] = []
  const order = host.order
  if (order && order.stage !== 'ready') {
    const name = order.name ?? 'a node'
    if (order.created_at) events.push({ key: 'order-created', text: `Adding node ${name}`, at: order.created_at, tone: 'info' })
    // Its stage's own line, unless it entered that stage the instant it was created.
    if (order.updated_at && atMs(order.updated_at) !== atMs(order.created_at ?? null)) {
      events.push(order.stage === 'failed'
        ? { key: 'order-stage', text: `Adding ${name} failed${failedCause(order)}`, at: order.updated_at ?? null, tone: 'error' }
        : { key: 'order-stage', text: STAGE_LABELS[order.stage]?.active ?? 'In progress', at: order.updated_at ?? null, tone: 'info' })
    }
  }
  if (machine?.last_seen) events.push({ key: 'machine', text: 'Machine last reported', at: machine.last_seen, tone: 'info' })
  if (hostFault) events.push({ key: 'host-fault', text: hostFault, at: null, tone: 'error' })
  for (const n of host.nodes) {
    if (n.failure_reason) events.push({ key: `node:${n.id}`, text: `${n.name}: ${n.failure_reason}`, at: n.updated_at ?? null, tone: 'error' })
  }
  // Newest first; an untimed fault is the host's state now, so it leads.
  const sortKey = (e: HostEvent) => (Number.isNaN(atMs(e.at)) ? Infinity : atMs(e.at))
  return events.sort((a, b) => sortKey(b) - sortKey(a) || 0)
}

const ROW_SX = {
  display: 'flex', flexWrap: 'wrap', alignItems: 'baseline', justifyContent: 'space-between', columnGap: 1.5, rowGap: 0.25,
} as const
const TEXT_SX = { flex: '1 1 14rem', minWidth: 0, overflowWrap: 'anywhere' } as const
const TIME_SX = { flex: '0 0 auto', fontSize: '0.75rem', color: 'text.secondary', whiteSpace: 'nowrap' } as const

function When({ at, now }: { at: string | null; now: number }): JSX.Element | null {
  if (!at) return null
  return <Box component="span" title={absoluteTime(at)} sx={TIME_SX}>{relativeAge(at, new Date(now))}</Box>
}

export function HostEvents({ host, machine, hostFault, now }: {
  host: HostView
  /** The machine's presence (its door's or its roster record's). */
  machine: MachinePresence
  /** The door's host-level fault, when the viewer reads one. */
  hostFault?: string | null
  now: number
}): JSX.Element | null {
  const events = hostEvents(host, machine, hostFault)
  if (!events.length) return null
  const lastError = events.find(e => e.tone === 'error')
  return (
    <DetailSection title="Recent events">
      <Box data-testid="host-events" sx={{ display: 'flex', flexDirection: 'column', gap: 0.75, fontSize: '0.875rem' }}>
        {lastError && (
          <Box data-testid="host-last-error" sx={{ ...ROW_SX, color: 'error.main', fontWeight: 600 }}>
            <Box component="span" sx={TEXT_SX}>Last error: {lastError.text}</Box>
            <When at={lastError.at} now={now} />
          </Box>
        )}
        {/* The lead line IS the newest error: listed once, not again below it. */}
        {events.filter(e => e !== lastError).map(e => (
          <Box key={e.key} data-testid="host-event" data-tone={e.tone} sx={{ ...ROW_SX, color: e.tone === 'error' ? 'error.main' : 'text.primary' }}>
            <Box component="span" sx={TEXT_SX}>{e.text}</Box>
            <When at={e.at} now={now} />
          </Box>
        ))}
      </Box>
    </DetailSection>
  )
}
