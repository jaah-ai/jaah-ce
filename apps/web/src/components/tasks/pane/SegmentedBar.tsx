import Box from '@mui/material/Box'
import type { TaskTreeCounts } from '../../../types'

/* THE SEGMENTED BAR — a node's Stopped / In progress / Draft / Done split, one proportional segment each. The
 * container rows and the feature header draw it from the SAME `Counts` the rail's pill reads, so the three
 * surfaces agree for one selection (AC-6.5). Never colour alone: the words ride `aria-label` and `title`, and
 * every caller prints the numbers beside it. An empty node draws the empty track. */

const TONES = [
  { field: 'stopped', label: 'stopped', color: 'error.main' },
  { field: 'in_progress', label: 'in progress', color: 'primary.main' },
  { field: 'draft', label: 'draft', color: 'text.disabled' },
  { field: 'done', label: 'done', color: 'success.main' },
] as const

/** "2 stopped, 5 in progress, 1 draft, 10 done" — Draft named only when it holds any. */
export function countsPhrase(counts: TaskTreeCounts): string {
  return TONES.filter(t => t.field !== 'draft' || (counts.draft ?? 0) > 0)
    .map(t => `${counts[t.field] ?? 0} ${t.label}`)
    .join(', ')
}

export default function SegmentedBar({
  counts,
  width = 96,
  testId = 'segmented-bar',
}: {
  counts: TaskTreeCounts
  width?: number | string
  testId?: string
}) {
  // `?? 0`: an API from before the Draft section sends no `draft`.
  const total = counts.stopped + counts.in_progress + (counts.draft ?? 0) + counts.done
  const phrase = countsPhrase(counts)
  return (
    <Box
      component="span"
      role="img"
      aria-label={phrase}
      title={phrase}
      data-testid={testId}
      sx={{
        display: 'inline-flex',
        flex: '0 0 auto',
        width,
        height: 6,
        borderRadius: 3,
        overflow: 'hidden',
        bgcolor: 'action.hover',
        verticalAlign: 'middle',
      }}
    >
      {total > 0 &&
        TONES.map(t =>
          (counts[t.field] ?? 0) > 0 ? (
            <Box
              key={t.field}
              component="span"
              data-segment={t.field}
              sx={{ width: `${(counts[t.field] / total) * 100}%`, bgcolor: t.color, height: '100%' }}
            />
          ) : null,
        )}
    </Box>
  )
}
