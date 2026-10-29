import type { JSX } from 'react'
import Box from '@mui/material/Box'
import Banner from './Banner'
import type { BulkReport, BulkRun } from '../lib/bulkRun'

/* The lines under a `BulkActionBar`, one per verb in the bar's order (`actions` is the bar's own list, so
 * the two cannot disagree): progress while running (`role="status"`, polite), then a per-row report — the
 * server's own refusal per row says whether to retry. State comes from `hooks/useBulkRuns.ts`; the
 * `bulk-progress-<id>` / `bulk-report-<id>` / `bulk-report-note-<id>` handles are what a page's tests read. */
export default function BulkRunReports({
  actions,
  runs,
  reports,
}: {
  actions: readonly { id: string }[]
  runs: Record<string, BulkRun>
  reports: Record<string, BulkReport>
}): JSX.Element {
  return (
    <>
      {actions.map(action => {
        const live = runs[action.id]
        if (live) {
          return (
            <Box key={action.id} sx={{ mb: 1 }}>
              <Banner severity="info" testId={`bulk-progress-${action.id}`}>
                <Box component="div" role="status">
                  {`${live.verb} ${live.done} of ${live.total}…`}
                </Box>
              </Banner>
            </Box>
          )
        }
        const report = reports[action.id]
        if (!report) return null
        return (
          <Box key={action.id} sx={{ mb: 1 }}>
            {/* Three severities in precedence: a refusal is an error; all-succeeded-but-something-left-behind
                (the pool Delete's orphaned directories) is amber; else success. Each line carries its own
                prefixed word, so colour is never the only signal. */}
            <Banner
              severity={report.failures.length > 0 ? 'error' : report.degraded ? 'warning' : 'success'}
              testId={`bulk-report-${action.id}`}
            >
              <Box component="div">{`${report.verb} ${report.done} of ${report.total}.`}</Box>
              {/* Keyed by position: messages repeat (one node's 503 refuses every row identically), and the
                  list is built once and never reordered. Notes likewise. */}
              {report.failures.map((failure, index) => (
                <Box component="div" key={index} sx={{ typography: 'caption' }}>{failure}</Box>
              ))}
              {/* After the refusals (the more urgent read); two lists because both can be non-empty. */}
              {report.notes.map((note, index) => (
                <Box
                  component="div"
                  key={index}
                  data-testid={`bulk-report-note-${action.id}`}
                  sx={{ typography: 'caption' }}
                >
                  {note}
                </Box>
              ))}
            </Banner>
          </Box>
        )
      })}
    </>
  )
}
