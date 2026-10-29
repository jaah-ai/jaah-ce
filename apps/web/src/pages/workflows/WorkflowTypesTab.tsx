import Box from '@mui/material/Box'
import { useApiRead } from '../../hooks/useApiRead'
import { DBOS_STATUS_GLYPH, DBOS_STATUS_HELP, DBOS_STATUS_TONE, isInFlight } from '../../lib/dbosStatus'
import { absoluteTime, relativeAge } from '../../lib/relativeTime'
import Banner from '../../ui/Banner'
import LoadFailedState from '../../ui/LoadFailedState'
import FeedList from '../../ui/feed/FeedList'
import type { FeedIndicator, FeedRowSpec } from '../../ui/feed/types'
import LoadingState from '../../ui/LoadingState'
import { listWorkflowTypes, type WorkflowType } from './api'
import {
  ENGINE_DOWN_TYPES,
  NEVER_RAN,
  RUNS_COLUMN_HEADER,
  readRunCount,
  windowCaptionShort,
} from './vocabulary'

/* Audit > Workflows > Catalogue body: the five durable workflows and what the engine has seen
 * of each. Headerless: the shell owns the `<h1>`.
 * Never empty and no empty state: all five are registry facts (`app/workflow_registry.py`) returned
 * whether or not they ever ran or the engine answered — "never fired" is the actionable reading. Only
 * a FAILED READ renders empty (the three non-happy states per `CronList`).
 * Rows link to `/workflows/:name`, so identity is the TITLE. A feed (`ui/feed/FeedList`): the last-run
 * pill is the MARK; each row's count carries `windowCaptionShort` on its `title`, since with no header
 * the number must not read as a lifetime total. */

/** Exactly the settled row count: the registry is closed. */
const SKELETON_ROWS = 5

/* The row's mark. Tones and sentences are single-homed in `lib/dbosStatus.ts` (DBOS statuses have no
 * `badge-*` tokens, so `ui/statusTone` does not apply). Glyphs: ✓ SUCCESS, ✕ ERROR /
 * MAX_RECOVERY_ATTEMPTS_EXCEEDED, » in flight, ■ CANCELLED (intent, not health), ○ registered and quiet,
 * ? engine did not answer (the pair `engine_available` separates). Unknown status → `?`, labelled by
 * its token. */
const UNKNOWN_GLYPH = '?'
const QUIET_GLYPH = '○'
const IN_FLIGHT_GLYPH = '»'

const ENGINE_UNAVAILABLE_LABEL = 'unavailable'

/** Three answers, not two (see `vocabulary.ts::NEVER_RAN`): an unreachable engine cannot say "No run in
 *  the window". */
function statusMark(type: WorkflowType, engineAvailable: boolean): FeedIndicator {
  if (!engineAvailable) {
    return {
      tone: 'neutral',
      glyph: UNKNOWN_GLYPH,
      label: ENGINE_UNAVAILABLE_LABEL,
      title: ENGINE_DOWN_TYPES,
      data: { status: 'unavailable' },
    }
  }
  if (!type.last_status) {
    return {
      tone: 'neutral',
      glyph: QUIET_GLYPH,
      label: NEVER_RAN,
      data: { status: 'none' },
    }
  }
  return {
    tone: DBOS_STATUS_TONE[type.last_status] ?? 'neutral',
    glyph: DBOS_STATUS_GLYPH[type.last_status] ?? (isInFlight(type.last_status) ? IN_FLIGHT_GLYPH : UNKNOWN_GLYPH),
    label: type.last_status,
    title: DBOS_STATUS_HELP[type.last_status],
    // The row's state oracle.
    data: { status: type.last_status },
  }
}

export default function WorkflowTypesTab({ enabled = true }: {
  /* Whether this read is wanted yet: `TabPanel` keeps every panel mounted, and this read is expensive (a
   * 500-run aggregate scan). `WorkflowsPage` latches it false→true. Its rows feed only this tab. Defaults to
   * true for non-tab callers (`WorkflowTypesTab.test.tsx` mounts it bare). */
  enabled?: boolean
} = {}) {
  const { data: payload, loading, error, reload: load } = useApiRead(
    ['workflow-types'],
    () => listWorkflowTypes(),
    'Failed to load the workflow catalogue',
    { enabled },
  )

  /* An unopened panel renders NOTHING: a never-resolving skeleton stays in the DOM and fails
   * `e2e/app-shell-scroll.spec.ts`'s no-surviving-`loading-state` sweep. */
  if (!enabled) return null

  /* No rows means the read failed: this list is never legitimately empty. */
  if (loading) return <LoadingState rows={SKELETON_ROWS} />

  if (payload === null) {
    return <LoadFailedState title="Workflow catalogue unavailable" error={error} onRetry={() => void load()} />
  }

  const { workflows, window: scanWindow, engine_available: engineAvailable } = payload

  /* Ordered by gate strength (`ui/feed/geometry.ts::SlotGate`): raw name, queue and description drop in
   * that order; a phone keeps the last-run age. */
  const typeRows: FeedRowSpec[] = workflows.map((type): FeedRowSpec => ({
    key: type.name,
    indicator: statusMark(type, engineAvailable),
    title: type.label,
    titleText: type.label,
    href: `/workflows/${type.name}`,
    slots: [
      {
        /* The raw `name` is what the runs filter, API and logs use. */
        key: 'name',
        width: 132,
        gate: 'meta',
        align: 'left',
        mono: true,
        dim: true,
        testId: 'workflow-type-name',
        title: type.name,
        node: type.name,
      },
      {
        /* Trigger + queue in one slot: queue name (or `direct` — a direct workflow can never sit ENQUEUED) as
         * the value, trigger sentence on hover. */
        key: 'trigger',
        width: 96,
        gate: 'meta',
        align: 'center',
        testId: 'workflow-type-trigger',
        title: type.trigger,
        node: type.queue ?? 'direct',
      },
      {
        /* The widest slot, ellipsised; full text on hover and on the workflow's page. */
        key: 'description',
        width: 240,
        gate: 'id',
        align: 'left',
        testId: 'workflow-type-description',
        title: type.description,
        node: type.description,
      },
      {
        /* `runs` counts the last N runs across ALL workflows, so the window qualifier rides the slot `title`.
         * `vocabulary.ts` single-homes `RUNS_COLUMN_HEADER` and `windowCaptionShort`. */
        key: 'runs',
        width: 56,
        gate: 'id',
        align: 'right',
        /* Mono already gives digits one width. */
        mono: true,
        /* Dimmed when not a measurement (the engine-less `—`). */
        dim: !engineAvailable,
        testId: 'workflow-type-runs',
        title: `${RUNS_COLUMN_HEADER} — ${readRunCount(type, engineAvailable)} ${windowCaptionShort(scanWindow)}`,
        node: readRunCount(type, engineAvailable),
      },
      {
        /* The age alone (`lib/relativeTime` pairing, absolute on hover); non-age readings are WORDS so they
         * stay distinct. */
        key: 'last',
        width: 108,
        align: 'right',
        dim: true,
        testId: 'workflow-type-last-run',
        title: type.last_run_at ? absoluteTime(type.last_run_at) : undefined,
        node: !engineAvailable
          ? ENGINE_UNAVAILABLE_LABEL
          : type.last_run_at
            ? relativeAge(type.last_run_at)
            : NEVER_RAN,
      },
    ],
  }))

  return (
    <Box>
      {error && <Banner severity="error">{error}</Banner>}
      {/* The engine-down banner sits above a fully populated list: identities are registry facts; only counts
      are missing. */}
      {!engineAvailable && (
        <Banner severity="warning" testId="workflows-engine-unavailable">
          {ENGINE_DOWN_TYPES}
        </Banner>
      )}

      {/* `workflow-types-table` is read by `WorkflowsPage.test.tsx`. */}
      <FeedList
        ariaLabel="Workflows"
        testId="workflow-types-table"
        sections={[{ key: 'workflows', rows: typeRows }]}
      />
    </Box>
  )
}
