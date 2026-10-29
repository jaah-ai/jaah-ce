/* ONE WORKFLOW RUN, IN FULL — `/workflows/runs/:runId`, an editor in the list/detail pane (`lib/listDetail.ts`)
 * opened from a `WorkflowRunsTab` row.
 *
 * Opened from a row it paints from that row at once (`state.row`, the query's placeholder), so it agrees with it;
 * a cold load reads the run by id (`api.ts::getWorkflowRun`). Opened from a workflow's Runs tab it carries
 * `?from=` that tab, where Close returns (`components/ListDetail.tsx`). TABBED, first-hand first: Overview (the
 * reading, the recovered banner, this run's engine fields and timing) and Payloads. The three payloads are
 * VERBATIM and monospaced: the ledger shows only a one-line reading (`./vocabulary.ts::readRun`), and an operator
 * grepping logs needs the exact string.
 */
import { useState, type JSX } from 'react'
import { useLocation, useParams } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import Box from '@mui/material/Box'
import Typography from '@mui/material/Typography'
import Breadcrumb from '../../components/Breadcrumb'
import { TabPanel, Tabs, type TabDef } from '../../components/Tabs'
import { useUrlTab } from '../../hooks/useUrlTab'
import { NO_VALUE } from '../../lib/relativeTime'
import { formatSpanBetween } from '../../lib/spanTime'
import { formatZoned } from '../../lib/zonedTime'
import { AUDIT_TAB_PATHS, AUDIT_WORKFLOW_TAB_PATHS } from '../../routeGroups'
import Banner from '../../ui/Banner'
import { CodeBlock } from '../../ui/CodeBlock'
import RunTiming from '../../ui/RunTiming'
import { CopyableIdField, DbosEngineFields, DbosStatusChip, Mono, TaskLink } from '../../ui/DetailFields'
import { DetailField, DetailPage, DetailSection } from '../../ui/detail/DetailPage'
import { RunDetailStatus } from '../../ui/detail/RunDetailStatus'
import { getWorkflowRun, type WorkflowRun } from './api'
import { readRun, taskIdOf } from './vocabulary'

/** One verbatim payload, or nothing: absence is ordinary (most workflows return `None`), and an empty
 *  "Output" well would read as data loss. */
function Payload({ label, value }: { label: string; value: string | null }): JSX.Element | null {
  if (!value) return null
  return (
    <Box>
      <Typography variant="caption" sx={{ color: 'text.secondary' }}>{label}</Typography>
      {/* `maxHeight` so an unbounded traceback leaves the other payloads reachable. */}
      <CodeBlock maxHeight={220} sx={{ mt: 0.25 }} testId={`workflow-run-${label.toLowerCase()}`}>{value}</CodeBlock>
    </Box>
  )
}

type RunTab = 'overview' | 'payloads'
const RUN_TABS: readonly TabDef<RunTab>[] = [
  { id: 'overview', label: 'Overview' },
  { id: 'payloads', label: 'Payloads' },
]

export default function WorkflowRunPage(): JSX.Element {
  const { runId = '' } = useParams<{ runId: string }>()
  const location = useLocation()
  /* The row is HELD past the arrival: a tab switch rewrites the URL and drops `location.state`, and the page must
     not fall back to a spinner while the read is still out. */
  const arrived = (location.state as { row?: WorkflowRun } | null)?.row
  const [carried, setCarried] = useState(arrived)
  if (arrived !== undefined && arrived.workflow_id !== carried?.workflow_id) setCarried(arrived)
  const runQuery = useQuery({
    queryKey: ['workflow-run', runId],
    queryFn: () => getWorkflowRun(runId),
    // A placeholder, not `initialData`: it paints at once, the read still runs, and the cache holds only reads.
    placeholderData: carried?.workflow_id === runId ? carried : undefined,
  })
  const run = runQuery.data
  const [tab, selectTab] = useUrlTab(RUN_TABS, 'overview')

  const crumbs = [
    { label: 'Audit', to: AUDIT_TAB_PATHS.users },
    { label: 'Workflows', to: AUDIT_WORKFLOW_TAB_PATHS.runs },
  ]

  if (run === undefined) return <RunDetailStatus crumbs={crumbs} query={runQuery} />

  const reading = readRun(run)
  const taskId = taskIdOf(run)
  /* RECOVERED — an executor died holding this run and another resumed it, so work may have run twice.
     A BANNER because a `3` in a definition list goes unnoticed. `null` is not evidence of recovery. */
  const recovered = run.recovery_attempts != null && run.recovery_attempts > 1
  const time = formatZoned(run.created_at, undefined, { millis: true })

  return (
    <DetailPage
      testId="workflow-run-page"
      breadcrumb={
        <Breadcrumb
          /* The workflow leads, not the timestamp: the ledger mixes five workflows. */
          items={[...crumbs, { label: run.label, to: `/workflows/${encodeURIComponent(run.name)}?tab=runs` }, { label: `Run ${time}` }]}
          currentComponent="h1"
        />
      }
      /* Beside the heading, not as `subtitle`: that is a `<p>`, and a Chip is a `<div>`. */
      titleAside={<DbosStatusChip status={run.status} />}
    >
      <Tabs tabs={RUN_TABS} tab={tab} onSelect={selectTab} label="Run sections" idPrefix="workflow-run" />

      <TabPanel id="overview" current={tab} idPrefix="workflow-run">
        <Typography
          variant="body2"
          data-testid="workflow-run-sentence"
          sx={{ mt: 1, color: reading.kind === 'failure' ? 'error.main' : 'text.primary', overflowWrap: 'anywhere' }}
        >
          {reading.sentence}
        </Typography>
        {taskId !== null && (
          <TaskLink taskId={taskId} label="Open task" testId="workflow-run-task-link" sx={{ display: 'inline-block', mt: 0.5 }} />
        )}

        {recovered && (
          <Box sx={{ mt: 1.5 }}>
            <Banner severity="warning" testId="workflow-run-recovered">
              This run was recovered {run.recovery_attempts} times — an executor died holding the workflow
              and another picked it up. Whatever it does may therefore have been started more than once.
            </Banner>
          </Box>
        )}

        <DetailSection title="This run" fields divided>
          <DetailField label="Workflow"><Mono>{run.name}</Mono></DetailField>
          <CopyableIdField label="Run id" id={run.workflow_id} testId="workflow-run-copy" ariaLabel="Copy run id" />
          <DbosEngineFields engine={run} queueFallback={`${NO_VALUE} started directly`} />
          <RunTiming
            startLabel="Created"
            start={run.created_at}
            dequeued={run.dequeued_at}
            completed={run.completed_at}
            notDequeued="not yet"
            notCompleted="not yet"
          />
          <DetailField label="Wall clock">
            <Typography variant="body2">
              {run.completed_at ? formatSpanBetween(run.created_at, run.completed_at) : 'still running'}
            </Typography>
          </DetailField>
        </DetailSection>
      </TabPanel>

      <TabPanel id="payloads" current={tab} idPrefix="workflow-run">
        <Box sx={{ pt: 1.5 }}>
          <DetailSection title="Payloads">
            {/* All three can be absent at once; say so once rather than look like a failed load. */}
            {!run.input && !run.output && !run.error && (
              <Typography variant="body2" sx={{ color: 'text.secondary', py: 0.5 }} data-testid="workflow-run-no-payloads">
                No payloads recorded.
              </Typography>
            )}
            <Payload label="Input" value={run.input} />
            <Payload label="Output" value={run.output} />
            <Payload label="Error" value={run.error} />
          </DetailSection>
        </Box>
      </TabPanel>
    </DetailPage>
  )
}
