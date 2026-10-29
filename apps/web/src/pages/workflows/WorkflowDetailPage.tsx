import { useState } from 'react'
import { useParams } from 'react-router-dom'
import Box from '@mui/material/Box'
import Stack from '@mui/material/Stack'
import Typography from '@mui/material/Typography'
import { ApiError } from '../../api'
import Breadcrumb from '../../components/Breadcrumb'
import { TabPanel, Tabs, type TabDef } from '../../components/Tabs'
import { useApiRead } from '../../hooks/useApiRead'
import { useUrlTab } from '../../hooks/useUrlTab'
import { AUDIT_TAB_PATHS, AUDIT_WORKFLOW_TAB_PATHS } from '../../routeGroups'
import { DBOS_STATUS_HELP, DBOS_STATUS_TONE } from '../../lib/dbosStatus'
import { relativeAge } from '../../lib/relativeTime'
import { formatZoned } from '../../lib/zonedTime'
import Banner from '../../ui/Banner'
import { KeyValue, KeyValueList } from '../../ui/KeyValueList'
import LoadFailedState from '../../ui/LoadFailedState'
import MissingRecordState from '../../ui/MissingRecordState'
import { monoSx } from '../../ui/listStyles'
import LoadingState from '../../ui/LoadingState'
import PageHeader from '../../ui/PageHeader'
import SectionCard from '../../ui/SectionCard'
import { StatusChip } from '../../ui/StatusChip'
import WorkflowRunsTab from './WorkflowRunsTab'
import { getWorkflow, type WorkflowDetail } from './api'
import {
  ENGINE_DOWN_TYPES,
  NEVER_RAN,
  statusBreakdown,
  windowCaption,
} from './vocabulary'

/* Audit > Workflows > ONE WORKFLOW — `/workflows/:name`. Superadmin-only at the route (`App.tsx`); the
 * boundary is `require_superadmin` plus `@superadmin_only` on every `/api/workflows` handler.
 *
 * Overview leads with the descriptor and rollup (what is it, what starts it, is this normal); the Runs tab
 * holds the evidence. `?tab=runs` addresses it — a run's Close returns there (`?from=`), so the id is fixed. The runs table IS `WorkflowRunsTab`, pinned to this name, not a copy (its header says what
 * pinning changes). An unregistered name renders a named not-found (`NodeDetailPage`'s posture).
 */

/** The crumb's target: the catalogue this row was clicked out of (Audit > Workflows > Catalogue), not the
 *  Workflows tab's default Runs leaf. */
const WORKFLOWS_TAB = AUDIT_WORKFLOW_TAB_PATHS.catalogue

/** Audit, then its Workflows catalogue, then this workflow (plain text per `components/Breadcrumb`). */
function Trail({ current }: { current: string }) {
  return (
    <Breadcrumb
      items={[
        { label: 'Audit', to: AUDIT_TAB_PATHS.users },
        { label: 'Workflows', to: WORKFLOWS_TAB },
        { label: current },
      ]}
    />
  )
}

/** NOT-FOUND IS ITS OWN RESOLVED VALUE, keyed on `ApiError`'s STATUS, never on a reworded `detail`
 *  sentence. The read stays a SUCCESS: no error banner beside the named not-found. */
const MISSING = 'missing'

type WorkflowTab = 'overview' | 'runs'
const WORKFLOW_TABS: readonly TabDef<WorkflowTab>[] = [
  { id: 'overview', label: 'Overview' },
  { id: 'runs', label: 'Runs' },
]

export default function WorkflowDetailPage() {
  const { name = '' } = useParams<{ name: string }>()
  const [tab, selectTab] = useUrlTab(WORKFLOW_TABS, 'overview')
  /* THE LEDGER IS READ ON FIRST SHOW, then kept: latched false→true so the panel stays mounted (and its
     filters, paging and open run survive a trip to Overview) without paying the read for a tab never opened.
     Derived during render, not in an effect, so a `?tab=runs` landing reads on its first paint. */
  const [runsShown, setRunsShown] = useState(false)
  if (tab === 'runs' && !runsShown) setRunsShown(true)
  const { data: detail, loading, error, reload: load } = useApiRead<WorkflowDetail | typeof MISSING>(
    ['workflow-detail', name],
    () => getWorkflow(name).catch(err => {
      if (err instanceof ApiError && err.status === 404) return MISSING
      throw err
    }),
    'Failed to load this workflow',
  )

  if (loading) return <LoadingState rows={4} />

  if (detail === MISSING) {
    return (
      <MissingRecordState
        breadcrumb={<Trail current={name} />}
        heading="Unknown workflow"
        title={`No workflow is registered as “${name}”`}
        headerTestId="workflow-detail-header"
        backTo={WORKFLOWS_TAB}
        backLabel="Back to workflows"
      />
    )
  }

  if (detail === null) {
    return <LoadFailedState title="Workflow unavailable" error={error} onRetry={() => void load()} />
  }

  return (
    <Box>
      <PageHeader
        breadcrumb={
          <Trail current={detail.label} />
        }
        title={detail.label}
        subtitle={detail.description}
        testId="workflow-detail-header"
      />

      {error && <Banner severity="error">{error}</Banner>}
      {!detail.engine_available && (
        <Banner severity="warning" testId="workflow-detail-engine-unavailable">
          {ENGINE_DOWN_TYPES}
        </Banner>
      )}

      <Tabs tabs={WORKFLOW_TABS} tab={tab} onSelect={selectTab} label="Workflow sections" idPrefix="workflow" />

      <TabPanel id="overview" current={tab} idPrefix="workflow">
        <Box sx={{ pt: 1.5 }}>
          <SectionCard title="This workflow">
            <Facts detail={detail} />
          </SectionCard>
        </Box>
      </TabPanel>

      {/* THE RUNS, AS EVIDENCE. */}
      <TabPanel id="runs" current={tab} idPrefix="workflow">
        <Box sx={{ pt: 1.5 }}>
          <SectionCard title="Runs" dense>
            <WorkflowRunsTab pinnedName={detail.name} enabled={runsShown} />
          </SectionCard>
        </Box>
      </TabPanel>
    </Box>
  )
}

/* THE DESCRIPTOR'S FACTS, AS A GRID, not `ui/MetaRow`'s caption strip: these are primary content (a
 * trigger sentence, a pill row, a qualified count), at body scale, reflowing to one column on a phone.
 */
function Facts({ detail }: { detail: WorkflowDetail }) {
  const breakdown = statusBreakdown(detail.by_status)

  return (
    <KeyValueList testId="workflow-detail-facts">
      <KeyValue label="Started by">
        <Typography variant="body2">{detail.trigger}</Typography>
      </KeyValue>

      <KeyValue label="Queue">
        <StatusChip
          label={detail.queue ?? 'direct'}
          tone={detail.queue ? 'info' : 'neutral'}
          data-status={detail.queue ? 'queued' : 'direct'}
        />
      </KeyValue>

      <KeyValue label="Runs">
        {/* THE QUALIFIER IS NOT OPTIONAL, AND IT NAMES WHICH WINDOW: a count over THIS WORKFLOW'S OWN
            newest runs (not the types tab's shared window) and never a lifetime total. Wording:
            `vocabulary.ts`'s `windowCaption`; why this window: `app/routes/workflows.py::WorkflowDetailOut`. */}
        {detail.engine_available ? (
          <>
            <Typography variant="body2" sx={{ fontVariantNumeric: 'tabular-nums' }} data-testid="workflow-detail-runs">
              {detail.runs.toLocaleString()} {windowCaption(detail.window, 'this-workflow')}
            </Typography>
            {breakdown.length > 0 && (
              <Stack direction="row" spacing={0.5} sx={{ mt: 0.5, flexWrap: 'wrap', rowGap: 0.5 }}>
                {breakdown.map(entry => (
                  <StatusChip
                    key={entry.status}
                    label={`${entry.status} ${entry.count}`}
                    tone={DBOS_STATUS_TONE[entry.status] ?? 'neutral'}
                    title={DBOS_STATUS_HELP[entry.status]}
                    data-status={entry.status}
                  />
                ))}
              </Stack>
            )}
          </>
        ) : (
          <Typography variant="body2" sx={{ color: 'text.secondary' }}>
            Unavailable — the durable engine did not answer.
          </Typography>
        )}
      </KeyValue>

      <KeyValue label="Last run">
        {!detail.engine_available ? (
          <Typography variant="body2" sx={{ color: 'text.secondary' }}>unavailable</Typography>
        ) : detail.last_run_at === null ? (
          <Typography variant="body2" sx={{ color: 'text.secondary' }}>{NEVER_RAN}</Typography>
        ) : (
          <Stack direction="row" spacing={0.75} sx={{ alignItems: 'center', flexWrap: 'wrap' }}>
            {detail.last_status && (
              <StatusChip
                label={detail.last_status}
                tone={DBOS_STATUS_TONE[detail.last_status] ?? 'neutral'}
                title={DBOS_STATUS_HELP[detail.last_status]}
                data-status={detail.last_status}
              />
            )}
            <Typography variant="body2">{formatZoned(detail.last_run_at)}</Typography>
            <Typography variant="body2" sx={{ color: 'text.secondary' }}>
              ({relativeAge(detail.last_run_at)})
            </Typography>
          </Stack>
        )}
      </KeyValue>

      {/* THE RAW NAME, last and mono: what the API takes and a log line carries. */}
      <KeyValue label="Name">
        <Typography variant="body2" sx={[monoSx, { overflowWrap: 'anywhere' }]}>{detail.name}</Typography>
      </KeyValue>
    </KeyValueList>
  )
}
