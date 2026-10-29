/* ONE AUTOMATION RUN, IN FULL — `/automations/:id/runs/:runId`, an editor in the list/detail pane
 * (`lib/listDetail.ts`) opened from an `AutomationRuns` row.
 *
 * Opened from a row it paints from that row at once (`state.row`, the query's placeholder), so it agrees with it; a cold
 * load reads the run by id (`api.ts::getAutomationRun`). Opened from the automation's Runs tab it carries
 * `?from=` that tab, where Close returns (`components/ListDetail.tsx`). TABBED, first-hand first: Overview (the
 * counts or task, then the targets or This run) and, for an event run only, Delivery. The targets are grouped by outcome, what needs attention first (`TARGET_OUTCOME_ORDER`): errors and
 * skips open, filed folded when anything else is there (AC4.6). A run from before the fire engine has no targets
 * and shows its stored detail in full (AC5.6); an event run adds its delivery: dedup status and preview.
 */
import { useState, type JSX } from 'react'
import { useLocation, useParams } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import Box from '@mui/material/Box'
import ButtonBase from '@mui/material/ButtonBase'
import Stack from '@mui/material/Stack'
import Typography from '@mui/material/Typography'
import Breadcrumb from '../../components/Breadcrumb'
import { TabPanel, Tabs, type TabDef } from '../../components/Tabs'
import { useUrlTab } from '../../hooks/useUrlTab'
import StatusChip from '../../ui/StatusChip'
import { Mono, TaskLink } from '../../ui/DetailFields'
import { DetailField, DetailPage, DetailSection } from '../../ui/detail/DetailPage'
import { RunDetailStatus } from '../../ui/detail/RunDetailStatus'
import { formatZoned } from '../../lib/zonedTime'
import { humanize } from '../../lib/humanize'
import { detailLines } from '../cron/vocabulary'
import { getAutomation, getAutomationRun, type AutomationRun, type RunTarget } from './api'
import {
  CAUSE_LABELS,
  runLine,
  runOutcomeLabel,
  runOutcomeTone,
  TARGET_OUTCOME_LABELS,
  TARGET_OUTCOME_ORDER,
  TARGET_OUTCOME_TONE,
  targetSummary,
} from './vocabulary'
import { CodeBlock } from '../../ui/CodeBlock'

/* ── THE DETAIL, AND THE TWO SHAPES IT ARRIVES IN ───────────────────────────────────────────────────
 * One sentence, or a multi-line block: `lease-drain`'s DECISION JOURNAL (headline plus one ~70-95 char line per
 * decision, ≤2000 API-side) or a traceback. The row shows the headline only; this page shows it in full.
 * The `ui/CodeBlock` well (bordered, as the delivery preview), but one block span per line with a HANGING INDENT,
 * so a wrapped tail never looks like the next decision; line 1 is a headline over evidence. Split by
 * `cron/vocabulary.ts::detailLines`. Block boxes, so a copy still yields the newlines (the grep contract). */
const JOURNAL_LINE_SX = {
  display: 'block',
  // 1.7 (siblings use 1.55): this is read line by line, twenty at a time.
  lineHeight: 1.7,
  pl: '2.5ch',
  textIndent: '-2.5ch',
  /* A blank line keeps its height (an empty block is 0px): an explicit floor of this line's own height, in CSS
   * rather than an `&nbsp;`, so what prints is what the runtime wrote. */
  '&:empty': { minHeight: '1.275rem' },
}

const JOURNAL_HEADLINE_SX = {
  ...JOURNAL_LINE_SX,
  pl: 0,
  textIndent: 0,
  fontWeight: 600,
  pb: 0.6,
  mb: 0.6,
  borderBottom: 1,
  borderColor: 'divider',
}

/** The runtime's text verbatim — a phrase, or a block given the whole panel width. */
function RunDetail({ detail }: { detail: string }): JSX.Element {
  const lines = detailLines(detail)
  if (lines.length === 1) {
    return <DetailField label="Detail"><Mono>{detail}</Mono></DetailField>
  }
  return (
    <DetailField label="Detail">
      {/* NO `maxHeight`: a clipped well under overlay scrollbars looks like the end, and at phone width it is a
          scroll trap. ONE SCROLL SURFACE: the page's. */}
      <CodeBlock bordered sx={{ mt: 0.25, px: 1.25 }} testId="automation-run-detail-journal">
        {lines.map((line, index) => (
          // Index as key: ordered lines of one immutable string, replaced whole.
          <Box key={index} component="span" sx={index === 0 ? JOURNAL_HEADLINE_SX : JOURNAL_LINE_SX}>{line}</Box>
        ))}
      </CodeBlock>
    </DetailField>
  )
}

const KIND_NOUN: Record<string, string> = { nodes: 'Node', projects: 'Project' }

/* `trigger_intake.py::preview_of` stores `{text, truncated}` (already indented JSON); any other shape is printed
 * as JSON (as `pages/triggers/DeliveriesLog.tsx`). */
function previewText(preview: Record<string, unknown>): { text: string; truncated: boolean } {
  if (typeof preview.text === 'string') return { text: preview.text, truncated: preview.truncated === true }
  return { text: JSON.stringify(preview, null, 2), truncated: false }
}

const DEDUP_WORDS: Record<string, string> = { reserved: 'reserved', duplicate: 'duplicate', released: 'released' }

/** The outcomes present in this run, attention first; an outcome the vocabulary does not know goes last. */
function outcomesOf(run: AutomationRun): string[] {
  const present = new Set([...Object.keys(run.target_counts), ...run.targets.map(t => t.outcome)])
  const known = TARGET_OUTCOME_ORDER.filter(o => present.has(o)) as string[]
  return [...known, ...[...present].filter(o => !known.includes(o)).sort()]
}

function outcomeCount(run: AutomationRun, outcome: string): number {
  const reasons = run.target_counts[outcome]
  if (!reasons) return run.targets.filter(t => t.outcome === outcome).length
  return Object.values(reasons).reduce((sum, n) => sum + n, 0)
}

function TargetLine({ target }: { target: RunTarget }): JSX.Element {
  return (
    <Box
      component="li"
      data-testid="automation-run-target"
      data-outcome={target.outcome}
      sx={{ py: 0.75, borderBottom: 1, borderColor: 'divider', minWidth: 0, '&:last-of-type': { borderBottom: 0 } }}
    >
      <Box sx={{ display: 'flex', flexWrap: 'wrap', alignItems: 'baseline', columnGap: 1, minWidth: 0 }}>
        <Typography variant="caption" sx={{ color: 'text.secondary' }}>
          {KIND_NOUN[target.kind] ?? target.kind}
        </Typography>
        <Typography variant="body2" sx={{ fontWeight: 500, overflowWrap: 'anywhere', minWidth: 0 }}>
          {target.name || target.id}
        </Typography>
        {target.task_id !== null && <TaskLink taskId={target.task_id} variant="body2" />}
      </Box>
      {target.reason && (
        <Typography variant="body2" sx={{ color: 'text.secondary', overflowWrap: 'anywhere' }}>
          {target.reason}
        </Typography>
      )}
    </Box>
  )
}

function TargetGroup({ run, outcome, defaultOpen }: {
  run: AutomationRun
  outcome: string
  defaultOpen: boolean
}): JSX.Element {
  const [open, setOpen] = useState(defaultOpen)
  const targets = run.targets.filter(t => t.outcome === outcome)
  const listId = `automation-run-${run.id}-${outcome}`
  const label = TARGET_OUTCOME_LABELS[outcome] ?? humanize(outcome)
  const count = outcomeCount(run, outcome)
  return (
    <Box data-testid="automation-run-group" data-outcome={outcome} sx={{ mt: 1 }}>
      <ButtonBase
        aria-expanded={open}
        aria-controls={listId}
        onClick={() => setOpen(v => !v)}
        sx={{
          display: 'flex',
          width: '100%',
          justifyContent: 'flex-start',
          gap: 1,
          py: 0.5,
          px: 0.5,
          borderRadius: 1,
          textAlign: 'left',
          '&:hover': { bgcolor: 'action.hover' },
          '&.Mui-focusVisible': { outline: 2, outlineColor: 'primary.main' },
        }}
      >
        <Box component="span" aria-hidden="true" sx={{ width: '1ch', color: 'text.secondary' }}>
          {open ? '▾' : '▸'}
        </Box>
        <StatusChip label={`${label} · ${count}`} tone={TARGET_OUTCOME_TONE[outcome] ?? 'neutral'} data-status={outcome} />
      </ButtonBase>
      {open && (
        <Box id={listId} component="ul" sx={{ listStyle: 'none', m: 0, pl: 3.5, pr: 0 }}>
          {targets.map(target => <TargetLine key={`${target.kind}:${target.id}`} target={target} />)}
          {targets.length < count && (
            <Typography component="li" variant="caption" sx={{ color: 'text.secondary', py: 0.5, display: 'block' }}>
              {count - targets.length} more not loaded
            </Typography>
          )}
        </Box>
      )}
    </Box>
  )
}

type RunTab = 'overview' | 'delivery'
const OVERVIEW_TAB: TabDef<RunTab> = { id: 'overview', label: 'Overview' }
const DELIVERY_TAB: TabDef<RunTab> = { id: 'delivery', label: 'Delivery' }
/** Delivery (dedup, owed targets, preview) is an event run's; a schedule run has one part, so no strip. */
const tabsFor = (run: AutomationRun | undefined): TabDef<RunTab>[] =>
  run?.preview !== undefined ? [OVERVIEW_TAB, DELIVERY_TAB] : [OVERVIEW_TAB]

export default function AutomationRunPage(): JSX.Element {
  const { id = '', runId = '' } = useParams<{ id: string; runId: string }>()
  const location = useLocation()
  const carried = (location.state as { row?: AutomationRun } | null)?.row
  const runQuery = useQuery({
    queryKey: ['automation-run', id, runId],
    queryFn: () => getAutomationRun(id, runId),
    // A placeholder, not `initialData`: it paints at once, the read still runs, and the cache holds only reads.
    placeholderData: carried?.id === runId ? carried : undefined,
  })
  const automationQuery = useQuery({ queryKey: ['automation', id], queryFn: () => getAutomation(id) })
  const run = runQuery.data
  const tabs = tabsFor(run)
  const [tab, selectTab] = useUrlTab(tabs, OVERVIEW_TAB.id)

  const automationTo = `/automations/${encodeURIComponent(id)}?tab=runs`
  const crumbs = [
    { label: 'Automations', to: '/automations' },
    { label: automationQuery.data?.name ?? 'Automation', to: automationTo },
  ]

  if (run === undefined) return <RunDetailStatus crumbs={crumbs} query={runQuery} />

  const outcomes = outcomesOf(run)
  const hasTargets = run.targets_total > 0 || run.targets.length > 0
  const preview = run.preview ? previewText(run.preview) : null
  // The viewer's zone, named — as the list's last/next run and the editor's next runs.
  const time = formatZoned(run.fired_at, undefined, { seconds: true })

  return (
    <DetailPage
      testId="automation-run-page"
      /* The audit event pane's shape: a trail back to the automation's runs, then this run's own `<h1>`. */
      breadcrumb={<Breadcrumb items={[...crumbs, { label: 'Run' }]} />}
      title={`Run ${time}`}
      /* Beside the heading, not as `subtitle`: that is a `<p>`, and a Chip is a `<div>`. */
      titleAside={
        <Stack direction="row" spacing={0.5} sx={{ flexWrap: 'wrap' }}>
          <StatusChip label={CAUSE_LABELS[run.cause] ?? run.cause} tone="neutral" data-status={run.cause} />
          <StatusChip label={runOutcomeLabel(run)} tone={runOutcomeTone(run)} data-status={run.outcome} />
        </Stack>
      }
    >
      {tabs.length > 1 && (
        <Tabs tabs={tabs} tab={tab} onSelect={selectTab} label="Run sections" idPrefix="automation-run" />
      )}

      <TabPanel
        id="overview"
        current={tab}
        idPrefix="automation-run"
        label={tabs.length > 1 ? undefined : OVERVIEW_TAB.label}
      >
        {/* A fanned-out run filed one task per target: its counts here, each task under Targets. Only a run
            from before the fire engine has its one task. */}
        {hasTargets && targetSummary(run) !== '' ? (
          <Typography variant="body2" sx={{ mt: 0.5, overflowWrap: 'anywhere' }} data-testid="automation-run-counts">
            {targetSummary(run)}
          </Typography>
        ) : run.task_id !== null && (
          <TaskLink taskId={run.task_id} label="Open task" sx={{ display: 'inline-block', mt: 0.5 }} />
        )}

        {hasTargets ? (
          <DetailSection title="Targets" divided>
            {run.targets_total > run.targets.length && (
              <Typography variant="body2" sx={{ color: 'text.secondary' }} data-testid="automation-run-showing">
                Showing {run.targets.length} of {run.targets_total} targets
              </Typography>
            )}
            <Box>
              {outcomes.map(outcome => (
                <TargetGroup
                  key={outcome}
                  run={run}
                  outcome={outcome}
                  defaultOpen={outcome !== 'filed' || outcomes.length === 1}
                />
              ))}
            </Box>
          </DetailSection>
        ) : run.detail !== '' ? (
          <DetailSection title="This run" fields divided>
            <RunDetail detail={run.detail} />
          </DetailSection>
        ) : run.skip_reason ? (
          // A skip the runtime left no detail for, in Audit > Schedule's words (`runLine`).
          <DetailSection title="This run" fields divided>
            <Typography variant="body2" data-testid="automation-run-skip">{runLine(run, '')}</Typography>
          </DetailSection>
        ) : null}
      </TabPanel>

      {run.preview !== undefined && (
        <TabPanel id="delivery" current={tab} idPrefix="automation-run">
          <Box sx={{ pt: 1.5 }}>
            <DetailSection title="Delivery" fields>
              <DetailField label="Dedup">
                <Typography variant="body2" sx={{ overflowWrap: 'anywhere' }} data-testid="automation-run-dedup">
                  {run.dedup
                    ? `Dedup key ${run.dedup.key}: ${DEDUP_WORDS[run.dedup.status] ?? run.dedup.status}`
                    : 'No dedup key'}
                </Typography>
              </DetailField>
              {(run.owed_targets ?? 0) > 0 && (
                <DetailField label="Owed targets">
                  <Typography variant="body2">{run.owed_targets}</Typography>
                </DetailField>
              )}
              <DetailField label="Preview">
                {preview ? (
                  <>
                    <CodeBlock testId="automation-run-preview" bordered sx={{ mt: 0.25, px: 1.25 }}>
                      {preview.text}
                    </CodeBlock>
                    {preview.truncated && (
                      <Typography variant="caption" sx={{ color: 'text.secondary' }}>Preview truncated.</Typography>
                    )}
                  </>
                ) : (
                  <Typography variant="body2" sx={{ color: 'text.secondary' }}>No preview stored.</Typography>
                )}
              </DetailField>
            </DetailSection>
          </Box>
        </TabPanel>
      )}
    </DetailPage>
  )
}
