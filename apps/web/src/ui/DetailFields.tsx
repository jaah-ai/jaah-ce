import type { JSX, ReactNode } from 'react'
import Link, { type LinkProps } from '@mui/material/Link'
import Stack from '@mui/material/Stack'
import Typography from '@mui/material/Typography'
import { Link as RouterLink } from 'react-router-dom'
import { DBOS_STATUS_HELP, DBOS_STATUS_TONE } from '../lib/dbosStatus'
import { NO_VALUE } from '../lib/relativeTime'
import { CopyButton } from './CopyButton'
import { DetailField } from './detail/DetailPage'
import { monoSx, PRE_WRAP_SX } from './listStyles'
import StatusChip from './StatusChip'

/* THE PRIMITIVES A RUN PAGE TAKES FROM HERE (`Mono`, and the DBOS run pieces the cron and workflow pages
 * share), so they read as one panel. The grouper is `ui/detail/DetailPage`'s `DetailSection` and the label/value
 * pair its `DetailField` — one of each for the app; do not re-add a second one here. */

/** THE console's machine token (an id, a uuid, a path, an executor name): monospace to match log lines.
 *  `wrap` is where a long one may break — `anywhere` (default) so a uuid wraps rather than widening the panel;
 *  `word` only at a hyphen, slash or dot, never mid-token (an IP in a narrow pane); `pre` keeps the token's own
 *  newlines and spaces (transcript event text). `muted` reads as secondary text; `block` is its own line. */
export function Mono({ children, testId, wrap = 'anywhere', muted, block }: {
  children: ReactNode
  testId?: string
  wrap?: 'anywhere' | 'word' | 'pre'
  muted?: boolean
  block?: boolean
}): JSX.Element {
  return (
    <Typography
      variant="body2"
      component={block ? 'div' : 'span'}
      data-testid={testId}
      sx={[
        monoSx,
        { fontSize: '0.8125rem', overflowWrap: wrap === 'word' ? 'break-word' : 'anywhere' },
        wrap === 'pre' ? PRE_WRAP_SX : null,
        muted ? { color: 'text.secondary' } : null,
      ]}
    >
      {children}
    </Typography>
  )
}

/** An id field: the token in mono plus a Copy button. */
export function CopyableIdField({ label, id, testId, ariaLabel }: {
  label: string
  id: string
  testId: string
  ariaLabel: string
}): JSX.Element {
  return (
    <DetailField label={label}>
      <Stack direction="row" sx={{ alignItems: 'center', gap: 0.5, flexWrap: 'wrap' }}>
        <Mono>{id}</Mono>
        <CopyButton testId={testId} label="Copy" ariaLabel={ariaLabel} text={() => id} />
      </Stack>
    </DetailField>
  )
}

/** A DBOS workflow status pill; a status a newer DBOS invents renders as itself in grey. */
export function DbosStatusChip({ status }: { status: string }): JSX.Element {
  return (
    <StatusChip label={status} tone={DBOS_STATUS_TONE[status] ?? 'neutral'} title={DBOS_STATUS_HELP[status]} data-status={status} />
  )
}

/** The engine-record fields both DBOS drawers show, in one order. */
export function DbosEngineFields({ engine, queueFallback = NO_VALUE }: {
  engine: { queue_name: string | null; executor_id: string | null; app_version: string | null; recovery_attempts: number | null }
  queueFallback?: string
}): JSX.Element {
  return (
    <>
      <DetailField label="Queue"><Mono>{engine.queue_name ?? queueFallback}</Mono></DetailField>
      <DetailField label="Executor"><Mono>{engine.executor_id ?? NO_VALUE}</Mono></DetailField>
      <DetailField label="Build"><Mono>{engine.app_version ?? NO_VALUE}</Mono></DetailField>
      <DetailField label="Attempts">
        <Typography variant="body2">{engine.recovery_attempts ?? NO_VALUE}</Typography>
      </DetailField>
    </>
  )
}

/** A task's own page, reading "#N" or "<label> #N" ("Open task #N" under a drawer's reading sentence).
 *  Other `Link` props (`sx`, `aria-label`) pass through. */
export function TaskLink({ taskId, label, testId, ...rest }: {
  taskId: number
  label?: string
  testId?: string
} & Omit<LinkProps, 'children' | 'href'>): JSX.Element {
  return (
    <Link component={RouterLink} to={`/tasks/${taskId}`} underline="hover" data-testid={testId} {...rest}>
      {label ? `${label} #${taskId}` : `#${taskId}`}
    </Link>
  )
}
