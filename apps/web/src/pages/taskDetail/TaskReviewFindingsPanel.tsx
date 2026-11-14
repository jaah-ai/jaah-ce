// The Task page's Details › Review subtab: what review lanes found in this task's change, most severe first. Reads
// `GET /api/tasks/{id}/review-findings` (`app/routes/review_findings.py`). Advisory: nothing blocks on a finding.
import Box from '@mui/material/Box'
import Stack from '@mui/material/Stack'
import Typography from '@mui/material/Typography'
import { get } from '../../api'
import { TabPanel } from '../../components/Tabs'
import { useApiRead } from '../../hooks/useApiRead'
import { humanize } from '../../lib/humanize'
import { FONT_WEIGHT } from '../../theme'
import type { PipelineTask } from '../../types'
import { DetailSection } from '../../ui/detail/DetailPage'
import { Mono } from '../../ui/DetailFields'
import { StatusChip, type Tone } from '../../ui/StatusChip'
import type { DetailsSubtab } from './taskDetailConstants'

export interface ReviewFinding {
  id: string
  severity: string
  category: string
  file: string | null
  line: number | null
  title: string
  detail: string
  detecting_lane: string
  detecting_task_id: number | null
  detecting_config_label: string
  authoring_config_label: string
  head_sha: string | null
  created_at: string
}

export interface TaskReviewFindingsResponse { rows: ReviewFinding[] }

/** The task's findings: one read per page, shared by the Details strip (does the subtab exist) and the panel. Keyed on the
 *  task's `updated_at` and state, so a settle re-reads it. Not on the Live Cast viewer. */
export function useTaskReviewFindings(task: PipelineTask | null, enabled: boolean) {
  return useApiRead<TaskReviewFindingsResponse>(
    ['task-review-findings', task?.id, task?.updated_at, task?.state],
    () => get<TaskReviewFindingsResponse>(`/api/tasks/${task?.id ?? 0}/review-findings`),
    'Failed to load the review findings',
    { enabled: enabled && task !== null },
  )
}

export type TaskReviewFindingsRead = ReturnType<typeof useTaskReviewFindings>

// The API's closed sets (`app/models/review.py`).
const SEVERITY: Record<string, { label: string; tone: Tone }> = {
  blocker: { label: 'Blocker', tone: 'error' },
  major: { label: 'Major', tone: 'warning' },
  minor: { label: 'Minor', tone: 'info' },
  info: { label: 'Info', tone: 'neutral' },
}
const CATEGORY: Record<string, string> = { security: 'Security', correctness: 'Correctness', test: 'Test' }
const LANE: Record<string, string> = {
  blind_reviewer: 'Blind reviewer',
  blind_security_reviewer: 'Blind security reviewer',
  blind_tester: 'Blind tester',
  in_session_reviewer: 'Reviewer',
  in_session_security_reviewer: 'Security reviewer',
  in_session_verifier: 'Verifier',
  ci_scan: 'CI scan',
}

/** `file:line`, `file`, or null when the finding names no place. */
export function findingPlace(f: Pick<ReviewFinding, 'file' | 'line'>): string | null {
  if (!f.file) return null
  return f.line != null ? `${f.file}:${f.line}` : f.file
}

function Fact({ name, children }: { name: string; children: string }) {
  return (
    <Typography variant="body2" component="div" sx={{ minWidth: 0, overflowWrap: 'anywhere' }}>
      <Box component="span" sx={{ color: 'text.secondary' }}>{name}</Box> {children}
    </Typography>
  )
}

function FindingRow({ finding }: { finding: ReviewFinding }) {
  const severity = SEVERITY[finding.severity] ?? { label: humanize(finding.severity), tone: 'neutral' as Tone }
  const place = findingPlace(finding)
  return (
    <Box component="li" data-testid="review-finding" data-severity={finding.severity}
      sx={{ listStyle: 'none', py: 1, borderTop: 1, borderColor: 'divider', minWidth: 0,
        display: 'flex', flexDirection: 'column', gap: 0.5 }}>
      <Stack direction="row" sx={{ flexWrap: 'wrap', alignItems: 'center', columnGap: 1, rowGap: 0.5, minWidth: 0 }}>
        <span data-testid="review-finding-severity"><StatusChip label={severity.label} tone={severity.tone} /></span>
        <Typography variant="body2" sx={{ color: 'text.secondary' }} data-testid="review-finding-category">
          {CATEGORY[finding.category] ?? humanize(finding.category)}
        </Typography>
        <Typography variant="body2" sx={{ fontWeight: FONT_WEIGHT.semibold, minWidth: 0, overflowWrap: 'anywhere' }}>
          {finding.title}
        </Typography>
      </Stack>
      {place && <Box data-testid="review-finding-place"><Mono>{place}</Mono></Box>}
      {finding.detail && (
        <Typography variant="body2" sx={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }}>{finding.detail}</Typography>
      )}
      <Box data-testid="review-finding-configs">
        <Fact name="Found by">
          {`${LANE[finding.detecting_lane] ?? humanize(finding.detecting_lane)} · ${finding.detecting_config_label}`}
        </Fact>
        <Fact name="Authored by">{finding.authoring_config_label}</Fact>
      </Box>
    </Box>
  )
}

export function TaskReviewFindingsPanel({ current, read }: {
  current: DetailsSubtab
  read: TaskReviewFindingsRead
}) {
  const rows = read.data?.rows ?? []
  return (
    <TabPanel id="review" current={current} idPrefix="details">
      <DetailSection testId="task-page-panel" title="Review findings" headingLevel="h3">
        {read.error && <Typography color="error" variant="body2">{read.error}</Typography>}
        <Box component="ul" sx={{ m: 0, p: 0, minWidth: 0 }} data-testid="review-findings">
          {rows.map(f => <FindingRow key={f.id} finding={f} />)}
        </Box>
      </DetailSection>
    </TabPanel>
  )
}

export default TaskReviewFindingsPanel
