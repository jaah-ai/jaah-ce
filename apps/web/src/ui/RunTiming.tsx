import Typography from '@mui/material/Typography'
import type { JSX } from 'react'
import { NO_VALUE } from '../lib/relativeTime'
import { formatSpanBetween } from '../lib/spanTime'
import { formatZoned } from '../lib/zonedTime'
import { DetailField } from './detail/DetailPage'

/** A durable run's three instants and the two gaps: `start → dequeued` is queue wait, `dequeued → completed`
 *  is time holding a worker — a wall-clock Duration cannot split them. Read by
 *  `pages/workflows/WorkflowRunPage`. A gap renders only when both ends exist (`NO_VALUE` is truthy). */
export default function RunTiming({ startLabel, start, dequeued, completed, zone, notDequeued = NO_VALUE, notCompleted }: {
  startLabel: string
  start: string | null
  dequeued: string | null
  completed: string | null
  zone?: string
  notDequeued?: string
  notCompleted: string
}): JSX.Element {
  const at = (iso: string | null, missing: string) => (iso ? formatZoned(iso, zone, { millis: true }) : missing)
  const gap = (word: string, span: string) => span !== NO_VALUE && (
    <Typography component="span" variant="caption" sx={{ color: 'text.secondary', ml: 1 }}>{word} {span}</Typography>
  )
  return (
    <>
      <DetailField label={startLabel}><Typography variant="body2">{at(start, NO_VALUE)}</Typography></DetailField>
      <DetailField label="Dequeued">
        <Typography variant="body2">{at(dequeued, notDequeued)}{gap('waited', formatSpanBetween(start, dequeued))}</Typography>
      </DetailField>
      <DetailField label="Completed">
        <Typography variant="body2">
          {at(completed, notCompleted)}{gap('held a worker', formatSpanBetween(dequeued, completed))}
        </Typography>
      </DetailField>
    </>
  )
}
