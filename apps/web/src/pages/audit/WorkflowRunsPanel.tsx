import { useCallback, useState } from 'react'
import Box from '@mui/material/Box'
import Stack from '@mui/material/Stack'
import { useApiRead } from '../../hooks/useApiRead'
import { compactAge, NO_VALUE } from '../../lib/relativeTime'
import Banner from '../../ui/Banner'
import WorkflowRunsTab from '../workflows/WorkflowRunsTab'
import { listPendingCronTicks, listWorkflowTypes } from '../workflows/api'

/* AUDIT > WORKFLOWS > RUNS — the durable-workflow run ledger's mounting here.
 *
 * NOT A SECOND LEDGER: `pages/workflows/WorkflowRunsTab.tsx` is its single home (see its header). This
 * supplies only the workflow-type catalogue for its type filter. The ledger TAKES its options rather than
 * fetching them, so a screen that also shows the catalogue never doubles the `/api/workflows` read (two
 * 500-run aggregate scans); the read lives at the mount point, not in the shared component or in
 * `AuditPage` (which holds no fetches).
 *
 * A FAILED CATALOGUE READ IS NOT AN ERROR STATE: the filter degrades to "All workflows" and the ledger
 * banners its own failure (`WorkflowsPage` holds the same position).
 *
 * PENDING CRON TICKS sit above it: the ledger excludes `cron_tick`, which hid a tick its executor abandoned.
 * Shown only when there is one older than `STUCK_TICK_MS` (a healthy tick is PENDING for seconds, so younger
 * ones would flash on load); a failed read shows nothing, as the catalogue's does. RE-READ WITH THE LEDGER (each
 * of its reads lands, `onRead`), so a tick that sticks after the panel opened shows on the next refresh.
 */

const STUCK_TICK_MS = 60_000

/* NO `enabled` GATE: `AuditPage` mounts each leaf only once opened (`{opened && <Leaf />}`, a false→true
 * latch), so the read below fires once, on first open. */
export default function WorkflowRunsPanel() {
  // The error is SWALLOWED ON PURPOSE (a filter, not the table).
  const { data } = useApiRead(['workflow-runs-panel-types'], listWorkflowTypes, '')
  /* The ledger's first read opens this read; each later one repeats it (one cache entry, refreshed in place). */
  const [opened, setOpened] = useState(false)
  const { data: pending, error: pendingError, refresh } = useApiRead(['pending-cron-ticks'], listPendingCronTicks, '', { enabled: opened })
  const onRead = useCallback(() => { if (opened) void refresh(); else setOpened(true) }, [opened, refresh])
  // A failed re-read keeps the last data; it must not keep an old banner up.
  const ticks = (pendingError ? [] : pending?.runs ?? []).filter(t => !t.created_at || Date.now() - Date.parse(t.created_at) > STUCK_TICK_MS)

  /* `pt: 1` matches the sibling sub-panels, so switching does not shift the first row. */
  return (
    <Box sx={{ pt: 1 }}>
      {ticks.length > 0 && (
        <Box sx={{ mb: 1 }}>
          <Banner severity="warning" testId="pending-cron-ticks">
            <Stack spacing={0.25}>
              <span>Pending cron ticks</span>
              {ticks.map(tick => (
                <span key={tick.workflow_id} data-testid="pending-cron-tick">
                  <code>{tick.workflow_id}</code>
                  {` · ${tick.executor_id ?? NO_VALUE} · ${compactAge(tick.created_at)}`}
                </span>
              ))}
            </Stack>
          </Banner>
        </Box>
      )}
      <WorkflowRunsTab types={data?.workflows} onRead={onRead} />
    </Box>
  )
}
