import { Fragment, useId, useState } from 'react'
import Box from '@mui/material/Box'
import ButtonBase from '@mui/material/ButtonBase'
import Tooltip from '@mui/material/Tooltip'
import Typography from '@mui/material/Typography'
import type { JSX } from 'react'
import { get } from '../api'
import { useApiRead } from '../hooks/useApiRead'
import { useEventStream } from '../hooks/useEventStream'
import { useLiveRefetch } from '../hooks/useLiveRefetch'
import { formatBytes } from '../lib/formatBytes'
import { clockTime, fmtTimestamp } from '../lib/relativeTime'
import type { NodeDiskEngine, NodeDiskEvent, NodeDiskEventsPayload, NodeDiskFreedKey, NodeDiskGate } from '../types'
import Banner from '../ui/Banner'
import { NONE, NONE_SX } from '../ui/NoneMark'
import LoadingState from '../ui/LoadingState'
import SectionCard from '../ui/SectionCard'
import StatusChip from '../ui/StatusChip'
import { monoSx } from '../ui/listStyles'
import { formatPct } from './NodeLoadMeter'

/* THE NODE'S DISK HISTORY (`GET /api/nodes/{id}/disk-events`, `app/node_disk_events.py`) and the disk-pause
 * mark (`NodeOut.disk_gate`). The Health tab's third read, under Metrics: usage is the chart, this is what
 * the node DID about it — each cleanup, what it freed, and each pause/resume of new work.
 *
 * READABLE OR NOTHING: plain category words, sizes through `lib/formatBytes`, only the categories that freed
 * something, newest first. The server's keys and raw byte counts never reach the screen. */

/** The page size asked for: the server's cap (it keeps 30 days). A scheduled pass that frees nothing lands every
 *  5–15 minutes, so a smaller page held only those within hours; runs of them collapse to one line below. */
export const DISK_EVENTS_LIMIT = 200

/** Plain words for `FREED_KEYS`, in the order a line lists them (biggest usual win first). */
const CATEGORY: Record<NodeDiskFreedKey, string> = {
  ci_images: 'CI images',
  build_cache: 'Build cache',
  runner_versions: 'Old runner versions',
  runner_temp: 'Runner temp',
  stale_tmp: 'Stale temp',
  orphan_volumes: 'Unused volumes',
  stopped_containers: 'Stopped containers',
  plugin_dedup: 'Plugin copies',
  orphan_users: 'Removed runner users',
}
const CATEGORY_ORDER = Object.keys(CATEGORY) as NodeDiskFreedKey[]

/** "Paused: disk 92% used" — the mark the roster row and the node header both wear. The figure is left out
 *  when withheld (a node on another org's host) or not fresh, never guessed. */
export function diskPausedLabel(gate: NodeDiskGate): string {
  return gate.used_pct == null ? 'Paused: disk full' : `Paused: disk ${formatPct(gate.used_pct)} used`
}

/** The disk-pause chip, or nothing while the gate is open. The label alone reads at a glance; when it started is
 *  its tooltip — focusable and tap-opened (as `GhaRunnersTab`'s state chip), never a native `title`. */
export function DiskPausedChip({ gate }: { gate: NodeDiskGate | null | undefined }): JSX.Element | null {
  if (!gate || gate.state !== 'paused') return null
  const label = diskPausedLabel(gate)
  const since = `Since ${fmtTimestamp(gate.since)}`
  return (
    <Tooltip title={since} enterTouchDelay={0} leaveTouchDelay={6000}>
      <Box
        component="span"
        tabIndex={0}
        aria-label={`${label}. ${since}`}
        data-testid="node-disk-paused"
        sx={{ display: 'inline-flex', whiteSpace: 'nowrap', borderRadius: 1 }}
      >
        <StatusChip label={label} tone="warning" data-status="disk-paused" />
      </Box>
    </Tooltip>
  )
}

/** What happened, and why, in words: "Scheduled cleanup", "Cleanup at 81% used → 63%", "Cleanup at warning level → 63%"
 *  (a warn reclaim that withheld its before-reading), "Paused at 92% used". */
function headline(event: NodeDiskEvent): string {
  const at = (pct: number | null | undefined) => (pct == null ? '' : ` at ${formatPct(pct)} used`)
  if (event.kind === 'pause') return `New work paused${at(event.used_pct)}`
  if (event.kind === 'resume') return `New work resumed${at(event.used_pct)}`
  const before = event.detail.used_pct_before
  const what =
    event.trigger === 'scheduled' ? 'Scheduled cleanup'
    : event.trigger === 'converge' && isPluginDedup(event) ? 'Shared plugin folder'
    : event.trigger === 'converge' ? 'Cleanup during setup'
    : before != null ? `Cleanup at ${formatPct(before)} used`
    : event.trigger === 'warn' ? 'Cleanup at warning level'
    : 'Cleanup'
  return event.used_pct == null ? what : `${what} → ${formatPct(event.used_pct)}`
}

/** A converge that swapped accounts' plugin copies for the shared folder (`plugin_repair.py`'s one report). */
function isPluginDedup(event: NodeDiskEvent): boolean {
  return event.freed_bytes.plugin_dedup != null || event.detail.remaining_bytes?.plugin_root != null
}

/** A reclaim that freed nothing and has nothing else to say: no removed user, no failed engine. Runs of these
 *  collapse into one line, so a quiet node's history still shows its real reclaims, pauses and resumes. */
function isNoop(event: NodeDiskEvent): boolean {
  return event.kind === 'reclaim'
    && event.total_freed_bytes <= 0
    && (event.detail.removed_users?.length ?? 0) === 0
    && !(event.detail.engines ?? []).some(engine => engine.error)
}

/** An engine that held nothing before or after: `0 B → 0 B`, folded into one line rather than listed. */
function isEmptyEngine(engine: NodeDiskEngine): boolean {
  return !engine.skipped && !engine.error && engine.bytes_before === 0 && engine.bytes_after === 0
}

/** The reclaim's non-zero categories, in `CATEGORY_ORDER`. */
function freedParts(event: NodeDiskEvent): { key: NodeDiskFreedKey; text: string }[] {
  return CATEGORY_ORDER
    .filter(key => (event.freed_bytes[key] ?? 0) > 0)
    .map(key => ({ key, text: `${CATEGORY[key]} ${formatBytes(event.freed_bytes[key] as number)}` }))
}

/** The newest reported size of what a reclaim measures but leaves in place (`detail.remaining_bytes`): the
 *  pool's git mirrors, the shared plugin folder. On the panel's header, so a quiet node, whose reclaims collapse
 *  into a "nothing to free" run, still shows them without a row to expand. `events` is newest first. */
export function latestRemaining(events: NodeDiskEvent[]): { key: string; text: string }[] {
  const mirrors = events.find(event => event.detail.remaining_bytes?.pool_mirrors != null)?.detail.remaining_bytes?.pool_mirrors
  const plugins = events.find(event => event.detail.remaining_bytes?.plugin_root != null)?.detail.remaining_bytes?.plugin_root
  return [
    ...(mirrors != null ? [{ key: 'pool-mirrors', text: `Git mirrors ${formatBytes(mirrors)}` }] : []),
    ...(plugins != null ? [{ key: 'plugin-root', text: `Plugin folder ${formatBytes(plugins)}` }] : []),
  ]
}

function hasDetail(event: NodeDiskEvent): boolean {
  const d = event.detail
  return (d.engines?.length ?? 0) > 0 || (d.removed_users?.length ?? 0) > 0
    || d.remaining_bytes?.plugin_root != null || d.remaining_bytes?.pool_mirrors != null
}


/** "10.0 GB → 2.0 GB", or the one size reported, or "no size reported" — never a placeholder. */
function engineSizes({ bytes_before: before, bytes_after: after }: NodeDiskEngine): string {
  if (before != null && after != null) return `${formatBytes(before)} → ${formatBytes(after)}`
  if (after != null) return `now ${formatBytes(after)}`
  if (before != null) return `was ${formatBytes(before)}`
  return 'no size reported'
}

/** An engine or user name in mono, kept WHOLE: the line breaks before it rather than inside it, and only a name
 *  longer than the line breaks within itself. */
function Name({ children }: { children: string }): JSX.Element {
  return <Box component="span" sx={[monoSx, NAME_SX]}>{children}</Box>
}

function Names({ names }: { names: string[] }): JSX.Element {
  return <>{names.map((name, i) => <Fragment key={name}>{i > 0 && ', '}<Name>{name}</Name></Fragment>)}</>
}

/** The per-event detail behind "Details": each engine's before → after, removed users, the plugin folder, the
 *  pool's git mirrors. */
function EventDetail({ event, id }: { event: NodeDiskEvent; id: string }): JSX.Element {
  const { engines = [], removed_users: removed = [], remaining_bytes: remaining } = event.detail
  const empty = engines.filter(isEmptyEngine)
  return (
    <Box id={id} component="ul" data-testid="disk-event-detail" sx={DETAIL_SX}>
      {engines.filter(engine => !isEmptyEngine(engine)).map(engine => (
        <li key={engine.user} data-testid="disk-event-engine">
          <Name>{engine.user}</Name>
          {': '}
          {engine.skipped === 'busy' ? 'skipped: busy'
            : engine.error ? `failed — ${engine.error}`
            : engineSizes(engine)}
        </li>
      ))}
      {empty.length > 0 && (
        <li data-testid="disk-event-empty-engines">
          {`${empty.length} empty ${empty.length === 1 ? 'engine' : 'engines'}: `}
          <Names names={empty.map(engine => engine.user)} />
        </li>
      )}
      {removed.length > 0 && (
        <li data-testid="disk-event-removed-users">
          {'Removed runner users: '}
          <Names names={removed} />
        </li>
      )}
      {remaining?.plugin_root != null && (
        <li data-testid="disk-event-plugin-root">Plugin folder now {formatBytes(remaining.plugin_root)}</li>
      )}
      {remaining?.pool_mirrors != null && (
        <li data-testid="disk-event-pool-mirrors">Git mirrors {formatBytes(remaining.pool_mirrors)}</li>
      )}
    </Box>
  )
}

function EventRow({ event, onHost }: { event: NodeDiskEvent; onHost: string | null }): JSX.Element {
  const [open, setOpen] = useState(false)
  const detailId = useId()
  const parts = event.kind === 'reclaim' ? freedParts(event) : []
  const expandable = event.kind === 'reclaim' && hasDetail(event)
  const tone = event.kind === 'pause' ? 'warning' : event.kind === 'resume' ? 'success' : 'neutral'
  const glyph = event.kind === 'pause' ? '■' : event.kind === 'resume' ? '▶' : '↺'
  return (
    <Box component="li" data-testid="disk-event" data-kind={event.kind} sx={ROW_SX}>
      <Box component="span" sx={TIME_SX}>{fmtTimestamp(event.at)}</Box>
      <Box sx={{ minWidth: 0 }}>
        <Box sx={HEAD_SX}>
          {/* The glyph rides INSIDE the headline's span, so a wrap never leaves it alone on a line. */}
          <Box component="span" sx={{ fontWeight: 500 }}>
            <Box component="span" aria-hidden="true" sx={{ color: tone === 'neutral' ? 'text.secondary' : `${tone}.main`, mr: 0.75 }}>
              {glyph}
            </Box>
            <span data-testid="disk-event-headline">{headline(event)}</span>
          </Box>
          {event.kind === 'reclaim' && (
            <Box component="span" data-testid="disk-event-total" sx={MUTED_SX}>
              {event.total_freed_bytes > 0 ? `freed ${formatBytes(event.total_freed_bytes)}` : 'nothing to free'}
            </Box>
          )}
          {onHost !== null && (
            <Box component="span" data-testid="disk-event-host" sx={MUTED_SX}>{onHost ? `on host ${onHost}` : 'on its host'}</Box>
          )}
          {expandable && (
            <ButtonBase
              aria-expanded={open}
              aria-controls={open ? detailId : undefined}
              aria-label={`${open ? 'Hide details' : 'Details'}: ${headline(event)}, ${fmtTimestamp(event.at)}`}
              data-testid="disk-event-toggle"
              onClick={() => setOpen(v => !v)}
              sx={TOGGLE_SX}
            >
              {open ? 'Hide details' : 'Details'}
            </ButtonBase>
          )}
        </Box>
        {parts.length > 0 && (
          <Box data-testid="disk-event-freed" sx={FREED_SX}>
            {parts.map(part => <Box component="span" key={part.key} sx={{ whiteSpace: 'nowrap' }}>{part.text}</Box>)}
          </Box>
        )}
        {expandable && open && <EventDetail event={event} id={detailId} />}
      </Box>
    </Box>
  )
}

/** "12 cleanups, nothing to free" across "Oct 11, 01:07 PM – 02:16 PM": a run of no-op reclaims as one line. */
function NoopRunRow({ run, onHost }: { run: NodeDiskEvent[]; onHost: string | null }): JSX.Element {
  const newest = run[0] as NodeDiskEvent
  const oldest = run[run.length - 1] as NodeDiskEvent
  return (
    <Box component="li" data-testid="disk-event" data-kind="noop-run" sx={ROW_SX}>
      <Box component="span" data-testid="disk-event-span" sx={TIME_SX}>{fmtSpan(oldest.at, newest.at)}</Box>
      <Box sx={HEAD_SX}>
        <Box component="span" sx={{ fontWeight: 500 }}>
          <Box component="span" aria-hidden="true" sx={{ color: 'text.secondary', mr: 0.75 }}>↺</Box>
          <span data-testid="disk-event-headline">{`${run.length} cleanups`}</span>
        </Box>
        <Box component="span" data-testid="disk-event-total" sx={MUTED_SX}>nothing to free</Box>
        {onHost !== null && (
          <Box component="span" data-testid="disk-event-host" sx={MUTED_SX}>{onHost ? `on host ${onHost}` : 'on its host'}</Box>
        )}
      </Box>
    </Box>
  )
}

/** Both ends of a run, in one clock form; the second drops its date when it is the same day as the first. */
function fmtSpan(from: string, to: string): string {
  const start = fmtTimestamp(from)
  const a = new Date(from)
  const b = new Date(to)
  const sameDay = !Number.isNaN(a.getTime()) && a.toDateString() === b.toDateString()
  const end = sameDay ? clockTime(to, { h23: false }) : fmtTimestamp(to)
  return `${start} – ${end}`
}

type Item = { kind: 'event'; event: NodeDiskEvent } | { kind: 'noop-run'; run: NodeDiskEvent[] }

/** Newest-first events → display items: two or more consecutive no-op reclaims from the same node become one. */
export function groupNoops(events: NodeDiskEvent[]): Item[] {
  const items: Item[] = []
  let run: NodeDiskEvent[] = []
  const flush = () => {
    if (run.length > 1) items.push({ kind: 'noop-run', run })
    else if (run.length === 1) items.push({ kind: 'event', event: run[0] as NodeDiskEvent })
    run = []
  }
  for (const event of events) {
    if (isNoop(event) && (run.length === 0 || run[0]?.source_node.id === event.source_node.id)) run.push(event)
    else {
      flush()
      if (isNoop(event)) run.push(event)
      else items.push({ kind: 'event', event })
    }
  }
  flush()
  return items
}

/* NEWEST FIRST is this component's property, not only the server's: a COPY, sorted by stamp then id. */
function newestFirst(events: NodeDiskEvent[]): NodeDiskEvent[] {
  return [...events].sort((a, b) => b.at.localeCompare(a.at) || b.id - a.id)
}

/** The node's disk history, read ONCE for the Health tab: this list and `NodeMetrics`' chart markers share it
 *  (siblings in one render). The caller keys its user per node — `useApiRead` keeps the last good list
 *  (and, on a failed refetch, shows it under the banner), so a node switch must remount. */
export function useNodeDiskEvents(nodeId: string): { events: NodeDiskEvent[] | null; error: string } {
  const { data, error, refresh } = useApiRead(
    ['node-disk-events', nodeId],
    () => get<NodeDiskEventsPayload>(`/api/nodes/${encodeURIComponent(nodeId)}/disk-events?limit=${DISK_EVENTS_LIMIT}`)
      .then(payload => (Array.isArray(payload?.events) ? newestFirst(payload.events) : [])),
    DISK_EVENTS_ERROR,
  )
  // The same live tick as the Metrics panel: a gate flip touches the node row.
  useLiveRefetch(useEventStream(), () => void refresh(), null, { entity: 'node' })
  // This panel's own words, never the server's detail.
  return { events: data, error: error ? DISK_EVENTS_ERROR : '' }
}

const DISK_EVENTS_ERROR = 'Failed to load this node’s disk history'

/** `ownId` is the node's row uuid (`NodeOut.id`): a row whose `source_node` is another node ran on the host
 *  this co-located node shares its disk with, and says so. Null (not loaded yet) marks no row. `events`/`error`
 *  are `useNodeDiskEvents`'. */
export default function NodeDiskEvents({ events, error, ownId }: {
  events: NodeDiskEvent[] | null
  error: string
  ownId: string | null
}): JSX.Element {
  // Nothing yet: the title row alone, its note on the header.
  if (events?.length === 0 && !error) {
    return (
      <Box data-testid="node-disk-events">
        <SectionCard
          title="Disk"
          actions={
            <Typography variant="body2" data-testid="disk-events-empty" sx={NONE_SX}>
              {NONE}
            </Typography>
          }
        />
      </Box>
    )
  }
  const remaining = events ? latestRemaining(events) : []
  return (
    <Box data-testid="node-disk-events">
      <SectionCard
        title="Disk"
        actions={remaining.length > 0 && (
          <Box data-testid="disk-remaining" sx={REMAINING_SX}>
            {remaining.map(part => (
              <Box component="span" key={part.key} data-testid={`disk-remaining-${part.key}`} sx={{ whiteSpace: 'nowrap' }}>
                {part.text}
              </Box>
            ))}
          </Box>
        )}
      >
        {error && <Banner severity="error" testId="disk-events-load-error">{error}</Banner>}
        {events === null ? (
          error ? null : <LoadingState rows={2} />
        ) : events.length === 0 ? null : (
          <Box component="ul" sx={LIST_SX}>
            {groupNoops(events).map(item => {
              const head = item.kind === 'event' ? item.event : item.run[0] as NodeDiskEvent
              const onHost = ownId && head.source_node.id !== ownId ? head.source_node.name : null
              return item.kind === 'event'
                ? <EventRow key={head.id} event={head} onHost={onHost} />
                : <NoopRunRow key={`run-${head.id}`} run={item.run} onHost={onHost} />
            })}
          </Box>
        )}
      </SectionCard>
    </Box>
  )
}

const LIST_SX = { listStyle: 'none', m: 0, p: 0, display: 'flex', flexDirection: 'column' } as const
/* STACKED, the stamp above the event, at every width: the Health tab can sit in a ~380px pane at a desktop
   viewport, where a viewport breakpoint's two columns squeezed each headline to a word per line. */
const ROW_SX = {
  display: 'flex',
  flexDirection: 'column',
  gap: 0.25,
  py: 0.75,
  fontSize: '0.875rem',
  minWidth: 0,
  '& + &': { borderTop: 1, borderColor: 'divider' },
} as const
const TIME_SX = { color: 'text.secondary', fontSize: '0.8125rem', whiteSpace: 'nowrap' } as const
const HEAD_SX = { display: 'flex', flexWrap: 'wrap', alignItems: 'baseline', columnGap: 1, rowGap: 0.25 } as const
const MUTED_SX = { color: 'text.secondary' } as const
const NAME_SX = { display: 'inline-block', maxWidth: '100%' } as const
const REMAINING_SX = {
  display: 'flex', flexWrap: 'wrap', justifyContent: 'flex-end', columnGap: 1.5,
  color: 'text.secondary', fontSize: '0.8125rem',
} as const
const FREED_SX = {
  display: 'flex', flexWrap: 'wrap', columnGap: 1.5, rowGap: 0.25, mt: 0.25,
  color: 'text.secondary', fontSize: '0.8125rem',
} as const
const TOGGLE_SX = {
  color: 'primary.main', fontSize: '0.8125rem', borderRadius: 0.5,
  '&:hover': { textDecoration: 'underline' },
  '&.Mui-focusVisible': { outline: 2, outlineColor: 'primary.main' },
} as const
const DETAIL_SX = {
  m: 0, mt: 0.5, pl: 2.5, fontSize: '0.8125rem', color: 'text.secondary',
  display: 'flex', flexDirection: 'column', gap: 0.25, overflowWrap: 'anywhere',
} as const
