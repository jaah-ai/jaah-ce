import { type JSX, useState } from 'react'
import { Link as RouterLink } from 'react-router-dom'
import { useQueryClient } from '@tanstack/react-query'
import Box from '@mui/material/Box'
import MuiLink from '@mui/material/Link'
import Stack from '@mui/material/Stack'
import Typography from '@mui/material/Typography'
import { useSubmit } from '../../hooks/useSubmit'
import { absoluteTime, NO_VALUE } from '../../lib/relativeTime'
import Banner from '../../ui/Banner'
import BusyButton from '../../ui/BusyButton'
import { KeyValue, KeyValueList } from '../../ui/KeyValueList'
import LoadingState from '../../ui/LoadingState'
import RelativeTime from '../../ui/RelativeTime'
import StatusChip from '../../ui/StatusChip'
import { tapTargetSx } from '../../ui/listStyles'
import { fileOutcomeScan, outcomeScanKey, useOutcomeScan, useOutcomeScanShown } from './outcomeScan'

/* PROJECT › GOVERNANCE › OUTCOME SCAN: the scan's state and one act, Scan now (`outcomeScan.ts`). Edit mode,
 * Owner/Admin, a project with a repository — the caller decides; this reads and files. A filed scan's returned
 * status replaces the cached one, so the running link appears without a refetch; a refusal (`filed: false`) shows
 * its `reason` beside the button. Nothing off the plan's agent performance (`useOutcomeScanShown`). */

/** The cursor's short form: a sha's first 12 characters, as git abbreviates. */
const SHORT_SHA = 12

export const NO_GIT_PERSON = "No git person — scans can't run"

export default function ProjectOutcomeScan({ projectId }: { projectId: number }): JSX.Element | null {
  const shown = useOutcomeScanShown()
  const queryClient = useQueryClient()
  const query = useOutcomeScan(projectId, true)
  const status = query.data
  const { busy, formError, run } = useSubmit()
  const [reason, setReason] = useState<string | null>(null)

  const scanNow = () => run('Failed to start the scan.', async () => {
    setReason(null)
    const res = await fileOutcomeScan(projectId)
    queryClient.setQueryData(outcomeScanKey(projectId), res.status)
    if (!res.filed) setReason(res.reason)
  })

  if (!shown) return null
  if (status === undefined) {
    return query.isError
      ? <Banner severity="error" testId="outcome-scan-load-error">Failed to load the outcome scan.</Banner>
      : <LoadingState rows={3} />
  }

  const running = status.open_task_id !== null
  return (
    <Stack spacing={1.5} data-testid="outcome-scan" sx={{ minWidth: 0 }}>
      {!status.git_person && <Banner severity="warning" testId="outcome-scan-no-git-person">{NO_GIT_PERSON}</Banner>}

      {/* The facts on one line with Scan now beside them: a row each (or Scan now on its own) pushed Governance
      past a 1440x900 window for a project with a repository (`e2e/project-edit-tabs.spec.ts`). */}
      <Box sx={{ display: 'flex', alignItems: 'center', columnGap: 3, rowGap: 1.5, flexWrap: 'wrap' }}>
        <KeyValueList inline>
          <KeyValue label="Last scan" valueTestId="outcome-scan-last">
            <Box component="span" sx={{ display: 'inline-flex', alignItems: 'center', gap: 1, flexWrap: 'wrap' }}>
              {status.last_filed_at === null ? 'Never' : <RelativeTime iso={status.last_filed_at} />}
              {status.stale && <StatusChip label="Stale" tone="warning" testId="outcome-scan-stale" />}
            </Box>
          </KeyValue>
          <KeyValue
            label="Cursor"
            mono={status.cursor_sha !== null}
            title={status.cursor_at === null ? undefined : absoluteTime(status.cursor_at)}
            valueTestId="outcome-scan-cursor"
          >
            {status.cursor_sha === null ? NO_VALUE : status.cursor_sha.slice(0, SHORT_SHA)}
          </KeyValue>
          <KeyValue label="Interval" valueTestId="outcome-scan-interval">
            {`${status.interval_minutes} min · ${status.interval_source}`}
          </KeyValue>
          {running && (
            <KeyValue label="Status" valueTestId="outcome-scan-running">
              <MuiLink component={RouterLink} to={`/tasks/${status.open_task_id}`}>Scan running</MuiLink>
            </KeyValue>
          )}
        </KeyValueList>
        <Box sx={{ display: 'flex', alignItems: 'center', gap: 1.5, flexWrap: 'wrap' }}>
          <BusyButton variant="outlined" size="small" busy={busy} disabled={running} onClick={scanNow} sx={tapTargetSx}>
            Scan now
          </BusyButton>
          {reason && (
            <Typography variant="body2" sx={{ color: 'text.secondary' }} role="status" data-testid="outcome-scan-reason">
              {reason}
            </Typography>
          )}
        </Box>
      </Box>
      {formError && <Banner severity="error" testId="outcome-scan-error">{formError}</Banner>}
    </Stack>
  )
}
