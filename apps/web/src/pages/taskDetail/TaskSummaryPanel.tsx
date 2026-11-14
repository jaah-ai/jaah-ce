// The Task page's Details tab: everything ABOUT the run — fields, Runner, Outcome, manifest, notes, Blocks.
import type { ComponentType } from 'react'
import { Link } from 'react-router-dom'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import MuiLink from '@mui/material/Link'
import ExternalLink from '../../ui/ExternalLink'
import Stack from '@mui/material/Stack'
import Typography from '@mui/material/Typography'
import type { Admin } from '../../auth'
import { TabPanel } from '../../components/Tabs'
import { artifactKindLabel, isReportOutcome } from '../../components/tasks/artifactKinds'
import FinishSection from '../../components/tasks/FinishSection'
import { memoryRecordLine } from '../../components/tasks/memoryRecord'
import { safeWorktreeHref } from '../../components/tasks/rowActions'
import {
  doneWarnReason,
  elapsedLabel,
  pastSpawnWindow,
  taskStatus,
} from '../../components/tasks/shared'
import { TaskDependents } from '../../components/tasks/TaskDependencies'
import { isChatTask, isEpicPersona } from '../../components/tasks/taskPersona'
import { TaskPrList, legacyPr, taskPrs } from '../../components/tasks/TaskPrList'
import TaskLanguageControl from '../../components/tasks/TaskLanguageControl'
import TaskStateControl, { offersStateMove } from '../../components/tasks/TaskStateControl'
import { TaskTags } from '../../components/tasks/TaskTags'
import { isEnabled } from '../../lib/editionFeatures'
import { editionModule } from '../../lib/editionModule'
import { isOrgAdminSeat } from '../../lib/orgAdmin'
import { safeHref } from '../../lib/safeHref'
import { triggerBudgetText } from '../../lib/taskCost'
import { countOf } from '../../lib/humanize'
import type { PipelineTask } from '../../types'
import Banner from '../../ui/Banner'
import RelativeTime from '../../ui/RelativeTime'
import { DetailField, DetailSection } from '../../ui/detail/DetailPage'
import { FactsGrid } from '../../ui/FactsGrid'
import { FONT_SIZE, FONT_WEIGHT, LINE_HEIGHT, RADIUS, grid } from '../../theme'
import { ABOVE_PHONE, PHONE, TASK_FIELDS_CONTAINER, TASK_FIELDS_NARROW, TASK_FIELDS_WIDE, monoSx } from '../../ui/listStyles'
import LinkButton from '../../ui/LinkButton'
import type { DetailsSubtab } from './taskDetailConstants'
import { TaskLaneStages } from './TaskLaneStages'
import { TaskMergeGates } from './TaskMergeGates'
import type { TaskDetailAccess } from './useTaskDetailAccess'
import type { TaskDetailActions } from './useTaskDetailActions'
import type { TaskDetailDerived } from './useTaskDetailDerived'
import type { TaskDetailTabsState } from './useTaskDetailTabs'
import Tip from '../../ui/Tip'

/* The Details cells' explanations (`ui/HelpLabel`). */
const STATE_HINT = 'Where the task stands in its lifecycle.'
const LANGUAGE_HINT = 'Questions and reports are written in it.'

/* The task's compliance manifest panel: the Compliance feature's (`compliance`) alone, loaded where present
 * (`lib/editionModule.ts`). */
type ManifestPanel = ComponentType<{ taskId: number; taskState?: string; canEdit: boolean }>
const ComplianceManifest: ManifestPanel | null = editionModule(isEnabled('compliance'),
  () => import.meta.glob<ManifestPanel>('../../components/tasks/ComplianceManifest.tsx', { eager: true, import: 'default' })) ?? null

/** The automation that filed a task (or its epic), by name — a link only for an org admin (`/automations` is
 * `@org_admin`) while it still exists. */
function AutomationName({ task, linked }: { task: PipelineTask; linked: boolean }) {
  const name = task.created_by_automation_name || 'Automation'
  return task.created_by_automation_id && linked ? (
    <MuiLink component={Link} to={`/automations/${task.created_by_automation_id}`}>
      {name}
    </MuiLink>
  ) : (
    <>{name}</>
  )
}

/** What an active epic with no running stage waits on, from its open stages' words: "waiting on 2 queued stages",
 * "waiting on 1 asking stage, 1 paused stage". */
function epicWaitingLine(task: PipelineTask): string {
  const counts = new Map<string, number>()
  for (const c of task.children ?? []) {
    // A failed stage is open (one rule with the rollup fallback below): it may be the very thing the epic waits on.
    if (c.state === 'done') continue
    const word = taskStatus(c).label.toLowerCase()
    counts.set(word, (counts.get(word) ?? 0) + 1)
  }
  // A payload without `children`: the rollup counts still say how many stages are open.
  const open = (task.child_count ?? 0) - (task.done_child_count ?? 0)
  if (counts.size === 0) return open > 0 ? `waiting on ${countOf(open, 'open stage')}` : 'no open stages'
  return `waiting on ${[...counts].map(([word, n]) => countOf(n, `${word} stage`)).join(', ')}`
}

/* Everything ABOUT the run: field grid, runner block, unverified-done warning, Outcome, Finisher notes, and the
   read-only `Blocks` list: Details' Summary subtab. The run figures are Details › Metrics. (Prerequisites are edited as `Depends on` in the
   Task form.) */
export default function TaskSummaryPanel({ current, task, admin, viewer, script, formDirty, access, derived, tabState, actions }: {
  current: DetailsSubtab
  task: PipelineTask
  admin: Admin | null
  viewer: boolean
  script: boolean
  formDirty: boolean
  access: TaskDetailAccess
  derived: TaskDetailDerived
  tabState: TaskDetailTabsState
  actions: TaskDetailActions
}) {
  const { can, canEditThisTask, canRunThisTask } = access
  const { runnerWorktree, wedgedRunnerSlot } = derived
  const { hasArtifacts, selectTab } = tabState
  const { pageActions, acting, confirm, holdUpdated } = actions
  const warnReason = doneWarnReason(task)

  const runnerHref = safeWorktreeHref(runnerWorktree)
  const memoryLine = memoryRecordLine(task)
  // An older row's lone `outcome_pr_url` reads as the same `owner/name #n` entry (`legacyPr`).
  const legacy = taskPrs(task).length === 0 ? legacyPr(task.outcome_pr_url) : null
  const prs = legacy ? [legacy] : taskPrs(task)
  const running = task.state === 'active'
  /* A chat parked on its turn boundary: its runner is alive at the prompt, so the Runner field must not
     say "Not running" beside the live Web Console button. */
  const chatParked = task.state === 'awaiting_response' && isChatTask(task)
  // An answer on its way to a runner alive inside its question call: not "Not running" either.
  const delivering = task.state === 'awaiting_response' && task.answer_delivering === true
  /* AN ASKING RUN WITH A LIVE RUNNER SHOWS IT: parked in its question call, the runner still holds its
     node · slot (or VM) and session — "Not running" beside them was false. */
  const askingLive = task.state === 'awaiting_response'
    && Boolean(task.ran_on?.live || task.live_session || task.microvm)
  /* The run's session: the runner's own id, else the session ROW the detail read found running
     (`live_session`): an ACP run records its session there, and the Runner row said "no session" beside it. */
  const runnerSessionId = task.runner_session_id || task.live_session?.id || null
  // A run with no session long past its spawn: the Runner row stops saying "starting…".
  const spawnLapsed = !runnerSessionId && !chatParked && !askingLive && !script && pastSpawnWindow(task.updated_at)
  // Filed by an automation, or a stage of an epic one filed (`TaskOut.created_by_automation_id`).
  const fromAutomation = Boolean(task.created_by_automation_id || task.created_by_automation_name)
  /* AN EPIC RUNS NO RUNNER OF ITS OWN: its state stays `active` while a stage works, so its Runner row
     said "starting…" or "no session" forever. It names the running stage instead, from the detail read's `children`
     (no request of its own); with none running, what it waits on (below). */
  const epic = isEpicPersona(task.persona)
  const runningChild = epic ? (task.children ?? []).find(c => c.state === 'active') ?? null : null
  /* AN ACTIVE EPIC WITH NO STAGE RUNNING SAYS WHAT IT WAITS ON: the fallback above read "starting…" or
     "no session" — a runner it never has. Its open stages, by their own words ("waiting on 2 queued stages"). */
  // Only where its stages are known and it holds no runner of its own (a session or a live slot would be the truth).
  const stagesKnown = task.children != null || (task.child_count ?? 0) > 0
  const epicWaiting = epic && running && !runningChild && stagesKnown && !runnerSessionId && !task.ran_on?.live
    ? epicWaitingLine(task)
    : null
  // The State select only where it offers a move; otherwise the read-only badge, as a reader sees it.
  const showStateControl = canRunThisTask && offersStateMove(task)
  return (
    <TabPanel id="summary" current={current} idPrefix="details">
      {/* A done Task that is not a confirmed success is flagged loudly, FIRST (it
          qualifies everything below). */}
      {warnReason && (
        <Banner severity="warning" role="status">
          <Box component="span" data-testid="task-outcome-warn">
            Unverified — {warnReason}.
          </Box>
        </Banner>
      )}
      {/* ── WHY YOU WERE NEVER ASKED ─────────────────────────────────────────────────────────────
          For an operator who cleared Full Auto and found no question: the server's sentence, printed
          as-is (this is its only renderer; never re-derived client-side). Below the unverified-done
          banner (a defect claim outranks a note), above every card. Guarded on the value only (null =
          nothing to say, `types.ts`). `role="status"`, `info` severity (nothing to act on), and the same
          `78ch` reading measure as the terminal-cause banner. */}
      {task.interview_skipped_reason && (
        <Banner severity="info" role="status" testId="interview-skipped">
          <Box component="span" sx={{ display: 'block', maxWidth: '78ch', lineHeight: LINE_HEIGHT.normal }}>{task.interview_skipped_reason}</Box>
        </Banner>
      )}
      {/* ── Tags ─────────────────────────────────────────────────────────────────────────
          FIRST card: a tag is an identity fact an operator scans by, and the one control on this tab.
          Edit mode only by construction. `readOnly` withholds writes; readers still see every tag. */}
      <DetailSection testId="task-page-panel">
        <TaskTags taskId={task.id} orgId={task.org?.id ?? null} tags={task.tags ?? []} readOnly={!canEditThisTask} />
      </DetailSection>

      <DetailSection testId="task-page-panel">
        {/* The field grid (`FactsGrid`, 138px tiles), by the GRID'S OWN width (a container query, `TASK_FIELDS_CONTAINER`) —
            the page is often the list's split pane at a wide viewport, where a viewport breakpoint crushed six tracks
            into ~500px. Under 720px: two tracks, each wide enough that a `wide` field takes ONE (a stamp,
            an email); from 720px the tiles with `wide` spanning two; from 1000px pinned to six. One column at a
            ≤480px viewport (the grid's own phone rule), where a `wide` span would open an implicit second column. */}
        <Box sx={{ containerType: 'inline-size', containerName: TASK_FIELDS_CONTAINER }}>
        <FactsGrid
          minTile={grid(23)}
          testId="task-page-fields"
          sx={{
            gap: 2,
            // Nested under the phone floor, so the one-column phone rule below is never contested.
            [ABOVE_PHONE]: {
              [TASK_FIELDS_NARROW]: {
                gridTemplateColumns: 'repeat(2, minmax(0, 1fr))',
                '& > [data-wide="true"]': { gridColumn: 'auto' },
              },
            },
            [TASK_FIELDS_WIDE]: { gridTemplateColumns: 'repeat(6, minmax(0, 1fr))' },
            [PHONE]: {
              gap: 1,
              '& > [data-wide="true"]': { gridColumn: 'auto' },
            },
          }}
        >
          {/* ── State: the current state beside its any-state "Change…", for a viewer who may run the task (the run
              gate, `tasks:edit` on the row, as Start/Stop are); a STOPPED script run (the merge lane), which moves
              only by its lane, offers its Start instead. Readers get no cell: with nothing to change, the header's
              pill already says the state. */}
          {script ? (
            task.state === 'paused' && canRunThisTask && pageActions?.onPillStart && (
              <DetailField label="State" help={STATE_HINT} testId="task-page-state">
                <Button
                  size="small"
                  variant="outlined"
                  disabled={acting}
                  onClick={pageActions.onPillStart}
                  data-testid="task-page-script-start"
                >
                  Start
                </Button>
              </DetailField>
            )
          ) : showStateControl && (
            <DetailField label="State" help={STATE_HINT} wide testId="task-page-state">
              <TaskStateControl
                task={task}
                confirm={confirm}
                onChanged={holdUpdated}
                disabled={formDirty}
                inField
              />
            </DetailField>
          )}
          {/* Persona and Priority are the Task tab's (set there, shown there): not repeated here. */}
          <DetailField label="Language" help={LANGUAGE_HINT} testId="task-page-language">
            <TaskLanguageControl
              task={task}
              editable={canEditThisTask && !viewer && task.parent_task_id === null}
              disabled={formDirty}
              onChanged={holdUpdated}
            />
          </DetailField>
          {/* AD-12: the `#` is KEPT — the field is named "Parent", so the sigil marks a task id. */}
          <DetailField label="Parent">
            {task.parent_task_id !== null ? (
              <MuiLink
                component={Link}
                to={`/tasks/${task.parent_task_id}`}
                sx={monoSx}
              >
                #{task.parent_task_id}
              </MuiLink>
            ) : (
              '—'
            )}
          </DetailField>
          {/* Timestamps and actor cells are `wide` (two tracks: one ≥200px track under a 720px grid holds the
              unbroken stamp), packing the six-column grid exactly; the four who/when cells stay adjacent — one question. */}
          <DetailField label="Created" wide>
            <RelativeTime iso={task.created_at} absolute />
          </DetailField>
          <DetailField label="Updated" wide>
            <RelativeTime iso={task.updated_at} absolute />
          </DetailField>
          {/* Resolved display names printed straight (`types.ts` `Provenance`); null → em-dash. `wide`:
              an actor may be a long email that a single track hyphen-breaks. Not on the Live Cast viewer:
              people are hidden there (the API sends both null), and a dash would read as "no creator". */}
          {!viewer && (
            <DetailField label="Created by" wide>
              {task.created_by
                || (fromAutomation ? <AutomationName task={task} linked={isOrgAdminSeat(admin)} /> : '—')}
            </DetailField>
          )}
          {!viewer && (
            <DetailField label="Updated by" wide>
              {task.updated_by || '—'}
            </DetailField>
          )}
          {/* AC-17.2: where the row came from, in the server's word. An automation's row (or its epic's child)
              names the automation, linked for an org admin; a row filed by a trigger older than automations
              names the trigger, which has no page of its own: while it exists, an org admin opens the Automations
              list from it. */}
          <DetailField label="Source" wide testId="task-page-source">
            {fromAutomation ? (
              <>
                {task.created_by_automation_name ? 'Automation · ' : ''}
                <AutomationName task={task} linked={isOrgAdminSeat(admin)} />
              </>
            ) : (
              task.source_label || (task.created_by_trigger_name ? '' : '—')
            )}
            {!fromAutomation && task.created_by_trigger_name ? (
              <>
                {task.source_label ? ' · Trigger ' : 'Trigger '}
                {task.created_by_trigger_id && isOrgAdminSeat(admin) ? (
                  <MuiLink component={Link} to="/automations">{task.created_by_trigger_name}</MuiLink>
                ) : (
                  task.created_by_trigger_name
                )}
              </>
            ) : null}
            {task.trigger_budget_exceeded_usd != null ? (
              /* R11: advisory only — the run carried on; this says it cost more than its trigger allows. */
              <Typography
                component="div"
                variant="body2"
                data-testid="task-page-trigger-budget"
                sx={{ color: 'warning.main', fontWeight: FONT_WEIGHT.semibold }}
              >
                {triggerBudgetText(task.trigger_budget_exceeded_usd, task.trigger_budget_usd ?? null)}
              </Typography>
            ) : null}
          </DetailField>

          {/* The template it was started from: a link onto the gallery's search for it while the template exists, plain
              text once deleted (the name is kept). */}
          {task.template && (
            <DetailField label="From template" wide testId="task-page-template">
              {task.template.id ? (
                <MuiLink component={Link} to={`/templates?q=${encodeURIComponent(task.template.name)}`}>
                  {task.template.name}
                </MuiLink>
              ) : task.template.name}
            </DetailField>
          )}

          {/* AD-5: the runner block (session id · elapsed · worktree link · Web Console), in the grid and
              rendered at every state, so "is anything running?" is always answered. */}
          {/* The whole row: its status, node · slot and links read as one line even in a split pane. */}
          <DetailField label="Runner" fullRow testId="task-page-runner">
            {/* One text size for the whole row: every part `inherit`s the field value's size, the same
                size as the fields beside it. */}
            <Box sx={{ display: 'flex', alignItems: 'center', flexWrap: 'wrap', gap: 1 }}>
              {runningChild ? (
                <Typography variant="inherit" data-testid="task-page-runner-child" sx={{ color: 'text.secondary' }}>
                  running{' '}
                  <MuiLink component={Link} to={`/tasks/${runningChild.id}`} variant="inherit">
                    #{runningChild.id} {runningChild.title}
                  </MuiLink>
                </Typography>
              ) : epicWaiting ? (
                <Typography variant="inherit" data-testid="task-page-runner-epic-wait" sx={{ color: 'text.secondary' }}>
                  {epicWaiting}
                </Typography>
              ) : running || chatParked || delivering || askingLive ? (
                <>
                  {runnerSessionId ? (
                    <Tip title={runnerSessionId}><Typography variant="mono" data-testid="task-page-runner-session">
                      {runnerSessionId.slice(0, 8)}
                    </Typography></Tip>
                  ) : (
                    // "starting…" for a runner mid-spawn (the header badge's "Starting"). A parked chat with
                    // no id is a degraded payload, not a spawn, so it says "waiting on you". A merge-lane run never
                    // reads "starting": it runs, on a slot or with none, and the header pill says the same
                    // (`taskStatus`: "Running", tooltip "Running. No slot"). Past the spawn window no session is coming: say so, with no
                    // elapsed time counting up beside it.
                    <Typography variant="inherit" sx={{ color: 'text.secondary' }} data-testid="task-page-runner-wait">
                      {chatParked || askingLive
                        ? 'waiting on you'
                        : script ? (task.ran_on?.live ? 'running' : 'no slot') : spawnLapsed ? 'no session' : 'starting…'}
                    </Typography>
                  )}
                  {!spawnLapsed && (
                    <Typography variant="inherit" sx={{ color: 'text.secondary' }}>
                      · {elapsedLabel(task.updated_at)}
                    </Typography>
                  )}
                </>
              ) : (
                // A done task has finished, not stalled: "Not running" reads as a fault there.
                <Typography variant="inherit" sx={{ color: 'text.secondary' }}>
                  {task.state === 'done' ? 'Finished' : 'Not running'}
                </Typography>
              )}
              {/* Where it runs, or last ran — the node and slot, at every state. */}
              {task.ran_on && (
                <Tip title={`${task.ran_on.live ? 'Running on' : 'Last ran on'} node ${task.ran_on.node}, slot ${task.ran_on.slot}`}><Typography
                  variant="inherit"
                  data-testid="task-page-ran-on"
                  sx={{ color: 'text.secondary' }}
                >
                  {task.ran_on.live ? 'on' : 'last ran on'}{' '}
                  {/* One unbroken unit: a hyphenated node name must not wrap away from its slot. */}
                  <Box component="span" sx={{ whiteSpace: 'nowrap' }}>
                    <Typography variant="mono" sx={{ fontSize: 'inherit', color: 'text.primary' }}>{task.ran_on.node}</Typography>
                    {' · slot '}{task.ran_on.slot}
                  </Box>
                </Typography></Tip>
              )}
              {/* The one link into Audit > Sessions for this task. Gated on `sessions:view`, the
                  destination's own key (`App.tsx` `/sessions` RequireFeature; `apps/api/app/routes/sessions.py`
                  `@feature("sessions", "view")`) — see `apps/api/app/authz/`'s "THE TIER KEY BECOMES THE
                  DOOR". The tab's download (`sessions:edit`) gates itself. */}
              {can('sessions:view') && (
                <MuiLink
                  component={Link}
                  to={`/sessions?task=${task.id}`}
                  variant="inherit"
                  aria-label={`Sessions recorded for task #${task.id}`}
                  sx={{ whiteSpace: 'nowrap' }}
                >
                  Sessions
                </MuiLink>
              )}
              {runnerHref && (
                <ExternalLink
                  href={runnerHref}
                  variant="inherit"
                  aria-label={`Open the live slot for task #${task.id}`}
                  sx={{ whiteSpace: 'nowrap' }}
                >
                  🌐 slot ↗
                </ExternalLink>
              )}
              {/* The console button is the header's, beside the state pill. */}
            </Box>
            {/* ── THE LAUNCHED RUNNER THAT NEVER REPORTED ────────────────────────────────
                Inside the Runner field, beside the console button that answers it. `role="status"` (as
                above); `warning`, since the CP owns the dead-runner verdict (`app/runner_liveness.py`). The
                copy states the fact and the action, never a guessed cause. */}
            {wedgedRunnerSlot && (
              <Box sx={{ mt: 1 }}>
                <Banner severity="warning" role="status" testId="task-runner-wedged">
                  Launched {elapsedLabel(wedgedRunnerSlot.since)} ago, still silent — open the console
                  to see what it is waiting on.
                </Banner>
              </Box>
            )}
          </DetailField>
          {/* The per-task project-memory record, only once a dispatch stamped the arm (`memoryRecordLine`). */}
          {memoryLine && (
            <DetailField label="Project memory" wide testId="task-page-memory">{memoryLine}</DetailField>
          )}
        </FactsGrid>
        </Box>
      </DetailSection>

      {/* The terminal report, at EVERY state that has one — an `active` row may already have reported.
          The only renderer of `no_pr_reason` and its evidence (the anti-spoof floor's proof). */}
      {/* PRs show at any state too: an open PR on an active task is where its work is. */}
      {(task.outcome_summary || task.outcome_pr_url || task.outcome_no_pr_reason || prs.length > 0) && (
        <DetailSection testId="task-page-panel" title="Outcome" headingLevel="h3">
          <Stack spacing={1} data-testid="task-page-outcome">
            {task.outcome_summary && (
              <Typography variant="body2" sx={{ color: 'text.primary' }}>
                {task.outcome_summary}
              </Typography>
            )}
            <Stack
              direction="row"
              spacing={1}
              useFlexGap
              sx={{ alignItems: 'baseline', flexWrap: 'wrap' }}
            >
              {/* Every PR, one per repository (`TaskPrList`); a bare link only for an older row's
                  `outcome_pr_url` whose path names no PR. GUARDED: it is runner-reported and the link text
                  is ours, so an unusable URL renders as no link. */}
              {/* No PR applies to a task with no repository (`types.ts::PipelineTask.has_repo`). */}
              {task.has_repo !== false && prs.length > 0 && <TaskPrList prs={prs} />}
              {task.has_repo !== false && prs.length === 0 && safeHref(task.outcome_pr_url) && (
                <ExternalLink href={task.outcome_pr_url} variant="caption" sx={monoSx}>
                  merged PR ↗
                </ExternalLink>
              )}
              {/* A task with no repository has nothing to merge, and a plan with nothing to build files no
                  children: either way the deliverable is the report. A link only while the Artifacts tab
                  exists (`hasArtifacts`); `selectTab` keeps the other query keys. */}
              {isReportOutcome(task.outcome_no_pr_reason) && (hasArtifacts ? (
                <LinkButton
                  variant="caption"
                  onClick={() => selectTab('artifacts')}
                  data-testid="task-outcome-report"
                >
                  {artifactKindLabel('report')}
                </LinkButton>
              ) : (
                <Typography variant="caption" sx={{ color: 'text.secondary' }} data-testid="task-outcome-report">
                  {artifactKindLabel('report')}
                </Typography>
              ))}
              {/* A phrase, not a raw enum token. */}
              {task.outcome_no_pr_reason && !isReportOutcome(task.outcome_no_pr_reason) && (
                <Typography variant="caption" sx={{ color: 'text.secondary' }} data-testid="task-outcome-no-pr">
                  closed with no PR — {task.outcome_no_pr_reason.replace(/_/g, ' ')}
                </Typography>
              )}
            </Stack>
            {/* Evidence gets its own scrollport; `pre-wrap` keeps reproduction steps' line breaks. */}
            {task.outcome_no_pr_evidence && (
              <Box>
                <Typography variant="caption" sx={{ color: 'text.secondary' }}>Evidence</Typography>
                <Box
                  data-testid="task-outcome-evidence"
                  sx={{
                    mt: 0,
                    p: 1,
                    maxHeight: grid(37),
                    overflowY: 'auto',
                    overflowWrap: 'anywhere',
                    whiteSpace: 'pre-wrap',
                    borderRadius: RADIUS.sm,
                    bgcolor: 'action.hover',
                    fontFamily: theme => theme.typography.mono.fontFamily,
                    fontSize: FONT_SIZE.sm,
                    color: 'text.primary',
                  }}
                >
                  {task.outcome_no_pr_evidence}
                </Box>
              </Box>
            )}
          </Stack>
        </DetailSection>
      )}

      {/* The merge-lane gate runs that tested this task's PRs, stage by stage. Not on the Live Cast viewer. */}
      {!viewer && !script && prs.length > 0 && task.project_id !== null && (
        <TaskMergeGates projectId={task.project_id} taskId={task.id} />
      )}
      {!viewer && !script && prs.length > 0 && <TaskLaneStages taskId={task.id} updatedAt={task.updated_at} />}

      {/* The signed AI-provenance manifest (task or epic): its own status read, sealed once the task is done.
          Regenerate is a `tasks:edit` write on this row (`canEditThisTask`); downloads are `tasks:view`. Not on
          the Live Cast viewer: the sealed file names people by email. */}
      {!viewer && ComplianceManifest && (
      <DetailSection testId="task-page-panel" title="Compliance manifest" headingLevel="h3">
        <ComplianceManifest taskId={task.id} taskState={task.state} canEdit={canEditThisTask} />
      </DetailSection>
      )}

      {/* The Finisher's findings, off `task.metrics`; only an epic that ran a finish phase has any, and the
          panel renders its own "nothing yet" line (AC5.2). Its computed sibling, Task Run Metrics, is the
          Metrics tab. */}
      <DetailSection testId="task-page-panel" title="Finisher Notes" headingLevel="h3">
        <FinishSection metrics={task.metrics} />
      </DetailSection>

      {/* What this task BLOCKS (spec R1). Only the reverse edge: prerequisites are a setting (`Depends on`
          in the Task form); what it blocks is a consequence edited elsewhere. The list is a `tasks:view`
          read; `readOnly` withholds writes and Re-run also needs the stricter `canRerun`. */}
      {/* Not on the Live Cast viewer: cross-task edges are an operator exception. */}
      {!viewer && (
      <DetailSection testId="task-page-panel">
        {/* Two keys: edges fall back to `tasks:create` on an authored draft; Re-run POSTs `start` and never
            does (see `canEditThisTask`). */}
        <TaskDependents
          taskId={task.id}
          readOnly={!canEditThisTask}
          canRerun={canRunThisTask}
        />
      </DetailSection>
      )}

    </TabPanel>
  )
}
