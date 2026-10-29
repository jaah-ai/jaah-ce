import Box from '@mui/material/Box'
import Link from '@mui/material/Link'
import TableBody from '@mui/material/TableBody'
import TableCell from '@mui/material/TableCell'
import TableHead from '@mui/material/TableHead'
import TableRow from '@mui/material/TableRow'
import Typography from '@mui/material/Typography'
import type { JSX } from 'react'
import { Link as RouterLink } from 'react-router-dom'
import { get } from '../api'
import { useApiRead } from '../hooks/useApiRead'
import { useEventStream } from '../hooks/useEventStream'
import { useLiveRefetch } from '../hooks/useLiveRefetch'
import { formatBytes } from '../lib/formatBytes'
import type { NodeSessionUsage, NodeSessionsUsagePayload } from '../types'
import Banner from '../ui/Banner'
import CellLabel from '../ui/CellLabel'
import DataTable from '../ui/DataTable'
import { DetailSection } from '../ui/detail/DetailPage'
import LoadingState from '../ui/LoadingState'
import { ELLIPSIS_SX, monoSx, PHONE, phoneCardSx } from '../ui/listStyles'
import { NONE, NONE_SX } from '../ui/NoneMark'
import { formatCorePct } from './NodeLoadMeter'

/* ══ THE SESSIONS RUNNING ON A NODE ══════════════════════════════════════════════════════
 * `GET /api/nodes/{id}/sessions-usage`: each live session's newest bucket on this node AND every node
 * co-located on it the reader can see, fresh ones only, RAM-heaviest first (the server's order). One home
 * for the host page's Status tab and the node page's Health tab.
 * Live: re-read every 15s (the observation pass emits no event) and on any `task` event (a run starting or
 * ending changes the set). The last good table stays under a banner when a re-read fails.
 * A figure the daemon did not send is the none mark, never `0`.
 */

const POLL_MS = 15_000
const LOAD_ERROR = 'Failed to load sessions'
const COLUMNS = ['Task', 'Node', 'CPU', 'RAM', 'Processes'] as const
const NUMERIC = new Set<string>(['CPU', 'RAM', 'Processes'])

/* On a phone the task takes the card's first line; node and figures wrap beneath it. */
const PHONE_SX = phoneCardSx({ '& tbody td[data-col="task"]': { flex: '1 1 100%' } }, { py: 0.5 })

/* A name cell's text: BLOCK so the ellipsis binds (an inline link overran into the next column), full text on
   hover. `maxWidth: 0` + a share on the cell lets the task column take the room left and never more. */
const CLIP_SX = { ...ELLIPSIS_SX, display: 'block' } as const
const TASK_CELL_SX = { width: '55%', maxWidth: 0, [PHONE]: { width: 'auto', maxWidth: '100%' } } as const
const NODE_CELL_SX = { width: '20%', maxWidth: 0, [PHONE]: { width: 'auto', maxWidth: '100%' } } as const

function none(): JSX.Element {
  return <Box component="span" sx={NONE_SX}>{NONE}</Box>
}

function TaskCell({ session }: { session: NodeSessionUsage }): JSX.Element {
  if (session.task) {
    return (
      <Link
        component={RouterLink}
        to={`/tasks/${session.task.id}`}
        underline="hover"
        title={session.task.title}
        sx={CLIP_SX}
      >
        {session.task.title || `Task #${session.task.id}`}
      </Link>
    )
  }
  // The task is gone or another org's: the session id is the one fact left.
  return session.session_id
    ? <Box component="span" title={session.session_id} sx={[monoSx, CLIP_SX]}>{session.session_id}</Box>
    : none()
}

export function NodeSessionsTable({ sessions }: { sessions: readonly NodeSessionUsage[] }): JSX.Element {
  if (sessions.length === 0) {
    return (
      <Typography variant="body2" data-testid="node-sessions-empty" sx={{ color: 'text.secondary' }}>
        No sessions running.
      </Typography>
    )
  }
  return (
    <Box sx={PHONE_SX}>
      <DataTable ariaLabel="Sessions" testId="node-sessions-table">
        <TableHead>
          <TableRow>
            {COLUMNS.map(h => <TableCell key={h} align={NUMERIC.has(h) ? 'right' : undefined}>{h}</TableCell>)}
          </TableRow>
        </TableHead>
        <TableBody>
          {sessions.map(s => (
            <TableRow key={`${s.node.id}-${s.task?.id ?? ''}-${s.session_id}`} data-testid="node-session-row">
              <TableCell data-col="task" sx={TASK_CELL_SX}>
                <CellLabel>Task</CellLabel>
                <TaskCell session={s} />
              </TableCell>
              <TableCell data-col="node" sx={NODE_CELL_SX}>
                <CellLabel>Node</CellLabel>
                <Link
                  component={RouterLink}
                  to={`/nodes/${encodeURIComponent(s.node.id)}`}
                  underline="hover"
                  title={s.node.name}
                  sx={CLIP_SX}
                >
                  {s.node.name}
                </Link>
              </TableCell>
              <TableCell data-col="cpu" align="right" sx={[monoSx, { whiteSpace: 'nowrap' }]}>
                <CellLabel>CPU</CellLabel>
                {s.cpu_pct == null ? none() : formatCorePct(s.cpu_pct)}
              </TableCell>
              <TableCell data-col="ram" align="right" sx={[monoSx, { whiteSpace: 'nowrap' }]}>
                <CellLabel>RAM</CellLabel>
                {s.rss_kb == null ? none() : formatBytes(s.rss_kb * 1024)}
              </TableCell>
              <TableCell data-col="procs" align="right" sx={[monoSx, { whiteSpace: 'nowrap' }]}>
                <CellLabel>Processes</CellLabel>
                {s.procs == null ? none() : String(s.procs)}
              </TableCell>
            </TableRow>
          ))}
        </TableBody>
      </DataTable>
    </Box>
  )
}

export default function NodeSessionsUsage({ nodeId }: {
  /** The node's id or name, as `/api/nodes/{id}/…` takes it. */
  nodeId: string
}): JSX.Element {
  const { data, error, refresh } = useApiRead(
    ['node-sessions-usage', nodeId],
    () => get<NodeSessionsUsagePayload>(`/api/nodes/${encodeURIComponent(nodeId)}/sessions-usage`),
    LOAD_ERROR,
    { refetchInterval: POLL_MS },
  )
  // Throttled: `task` events are fleet-wide, and only a run starting or ending moves this table.
  useLiveRefetch(useEventStream(), () => void refresh(), null, { entity: 'task', minIntervalMs: 5_000 })

  return (
    <DetailSection title="Sessions" testId="node-sessions">
      {error && <Banner severity="error" testId="node-sessions-error">{LOAD_ERROR}</Banner>}
      {data ? <NodeSessionsTable sessions={data.sessions ?? []} /> : !error && <LoadingState rows={2} />}
    </DetailSection>
  )
}
