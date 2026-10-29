// The banners above the Task page's tab panels: waiting, a pause, action failures, why it failed, waits or went unheard.
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import Typography from '@mui/material/Typography'
import { Link as RouterLink } from 'react-router-dom'
import { nodesAddPath } from '../../components/addNode/deepLink'
import { SYSTEM_PAUSE_CAUSE } from '../../components/tasks/pane/stopReason'
import type { PipelineTask } from '../../types'
import type { StalledAnswer } from '../../lib/answerPickup'
import { clockTime, humanDuration } from '../../lib/relativeTime'
import { orgWaitSentence } from '../../components/tasks/shared'
import { NODE_WAITING_NOTE } from '../../lib/welcomeWizard'
import Banner from '../../ui/Banner'
import type { OverflowItem } from '../../ui/OverflowMenu'
import type { TaskDetailActions } from './useTaskDetailActions'
import type { TaskDetailDerived } from './useTaskDetailDerived'
import type { TaskDetailRead } from './useTaskDetailLoad'

/** The node wait's banner: the header pill already says "Waiting for your node", so this says when it ends, beside the
 *  way to the nodes. */
export const NODE_WAIT_REMEDY = 'Starts once your node is online.'

export default function TaskDetailBanners({ task, canRunThisTask, stalledAnswer, read, derived, actions }: {
  task: PipelineTask
  canRunThisTask: boolean
  /** An answer the agent never picked up (`useStalledAnswer`), or `null`. */
  stalledAnswer: StalledAnswer | null
  read: TaskDetailRead
  derived: TaskDetailDerived
  actions: TaskDetailActions
}) {
  const { formDirty, artifactsError, setArtifactsError } = read
  const { failureCause } = derived
  const { dismissWaiting, actionError, setActionError, savedNotice, setSavedNotice, pageActions, openRunnerConsole, acting } = actions
  const nodeWait = task.state === 'ready' && task.wait_reason === NODE_WAITING_NOTE
  /* The node wait's way out opens Add node when the task's org has no node at all. The task carries
     that itself, so an operator on another org's task gets the same action as its own members. */
  const orgHasNoNode = nodeWait && task.org_has_node === false
  return (
    <>
      {/* D-waiting: the reason SHOWS on the task, with its note, until a state change or its × clears it. */}
      {task.stop?.kind === 'waiting_on_customer' && (
        <Banner
          severity="info"
          testId="task-page-waiting"
          onDismiss={canRunThisTask && !formDirty ? () => void dismissWaiting() : undefined}
        >
          Waiting on customer{task.stop.text ? ` — ${task.stop.text}` : ''}
        </Banner>
      )}

      {/* The system paused this task's work on the Jaah account. It resumes on its own when the
          cause clears — except a chat, which its person reopens with Start. */}
      {(task.stop?.kind === 'balance' || task.stop?.kind === 'jaah_unavailable') && (
        <Banner severity="warning" testId="task-page-system-pause">
          Paused — {SYSTEM_PAUSE_CAUSE[task.stop.kind]}.{' '}
          {task.persona === 'advisor'
            ? 'Start the chat again once resolved.'
            : task.stop.kind === 'balance'
              ? 'Resumes once topped up.'
              : 'Resumes automatically.'}
        </Banner>
      )}

      {/* ── The edit form's ACTION failures, held HERE rather than inside the form ─────────────────
          Go runs save → stop → reset → start, and a step can fail after the save landed. The form's own
          banner dies with it: every write bumps `updated_at`, re-keying and remounting the form. Not
          `error` (which replaces the page). Dismissible. */}
      {actionError && (
        <Banner severity="error" testId="task-page-action-error" onDismiss={() => setActionError('')}>
          {actionError}
        </Banner>
      )}

      {/* A Go that ended as a save: the reset it needed was declined after the save landed. */}
      {savedNotice && (
        <Banner severity="info" testId="task-page-saved-notice" onDismiss={() => setSavedNotice('')}>
          {savedNotice}
        </Banner>
      )}

      {/* ── …and WHY the run died, for the user whose only other copy of it does not exist ───────────
          See `failureCause`. Ordered below `actionError`, above `artifactsError`. NOT DISMISSIBLE: it is
          the task's durable terminal reason. The lead-in is the server's label (`_ACTIVITY_NOTE_LABELS`)
          when a feed note matched; the summary is shown whole. */}
      {failureCause !== null && (
        <Banner severity="error" testId="task-page-terminal-cause">
          {/* The feed's label-over-prose stack, with `minWidth: 0` against long words. */}
          <Box sx={{ display: 'flex', flexDirection: 'column', gap: 0.35, minWidth: 0 }}>
            <Typography variant="body2" sx={{ fontWeight: 600 }}>{failureCause.label}</Typography>
            {/* `overflowWrap: 'anywhere'`: hostnames and slugs are unbreakable. The `ch` cap is a reading
                measure for wide desktops; phones are width-bound first. */}
            <Typography variant="body2" sx={{ overflowWrap: 'anywhere', maxWidth: '78ch' }}>
              {failureCause.summary}
            </Typography>
          </Box>
        </Banner>
      )}

      {/* ── WHY IT IS NOT STARTING — the org's hold on a ready task, the server's sentence as-is, above
          every tab: the header badge only says "Waiting". A sentence ending in "." carries its own remedy
          (`app/dispatch_wait.py::held_on_offline_node`), so it gets no capacity suffix. The node wait, whose pill already
          says it, gives when it ends and the way to the nodes instead. */}
      {task.state === 'ready' && task.wait_reason && (
        <Banner
          severity="info"
          testId="task-org-wait"
          action={nodeWait && (
            <Button
              component={RouterLink}
              to={orgHasNoNode ? nodesAddPath(task.org?.id) : '/nodes'}
              size="small"
              data-testid="task-org-wait-nodes"
            >
              {orgHasNoNode ? 'Add node' : 'Nodes'}
            </Button>
          )}
        >
          {task.wait_reason === NODE_WAITING_NOTE ? NODE_WAIT_REMEDY : orgWaitSentence(task.wait_reason)}
        </Banner>
      )}

      {/* ── AN ANSWER THE AGENT NEVER PICKED UP — above every tab: the header still says Running. ────── */}
      {/* What the node said of it picks the sentence and the remedies — the ⋯'s own Stop and Reset
          (same gates, confirms and busy flag) and the header's console door, for a viewer who may run the task.
          The console only where the agent holds the answer: it shows nothing of one the node never took. */}
      {stalledAnswer && (() => {
        const { handedAt, silentMs, receipt } = stalledAnswer
        const at = clockTime(handedAt)
        const silent = humanDuration(silentMs / 1000, true)
        const item = (key: string) => (canRunThisTask ? pageActions?.items.find(i => i.key === key) : undefined)
        const remedies: { key: string; label: string; ariaLabel: string; onClick: () => void }[] = []
        const offer = (it: OverflowItem | undefined) => {
          if (it?.onClick && !it.disabled) remedies.push({ key: it.key, label: it.label, ariaLabel: it.ariaLabel, onClick: it.onClick })
        }
        if (receipt !== 'none' && receipt !== 'dropped' && canRunThisTask && openRunnerConsole) {
          remedies.push({ key: 'console', label: 'Console', ariaLabel: `Open web console for task #${task.id}`, onClick: openRunnerConsole })
        }
        offer(item('stop'))
        offer(item('reset'))
        return (
          <Banner severity="warning" testId="task-page-answer-unheard">
            <Box component="span" data-receipt={receipt ?? 'unknown'}>
              {receipt === 'none'
                ? <>Answer not delivered — handed over at {at}; the node has not received it in {silent}.</>
                : receipt === 'dropped'
                  ? <>Answer dropped — handed over at {at}; the node did not pass it to the agent.</>
                  : receipt === 'received'
                    ? <>Agent has not resumed — the node received the answer sent at {at}; no agent activity in {silent}.</>
                    : <>Answer not picked up — sent to the agent at {at}, no agent activity in {silent}.</>}
            </Box>
            {remedies.length > 0 && (
              <Box sx={{ display: 'flex', flexWrap: 'wrap', gap: 1, mt: 0.5 }}>
                {remedies.map(r => (
                  <Button
                    key={r.key}
                    size="small"
                    variant="outlined"
                    color="inherit"
                    disabled={acting}
                    aria-label={r.ariaLabel}
                    data-testid={`task-page-answer-unheard-${r.key}`}
                    onClick={r.onClick}
                  >
                    {r.label}
                  </Button>
                ))}
              </Box>
            )}
          </Banner>
        )
      })()}

      {/* ── …and the artifact list not LOADING, which is a different claim from there being none ─────
          Page loaded, one list did not: a `warning` above the strip. The wording must never say the task
          has no artifact — that confusion made operators re-dispatch finished work. */}
      {artifactsError && (
        <Banner
          severity="warning"
          testId="task-page-artifacts-error"
          onDismiss={() => setArtifactsError(false)}
        >
          Could not load this task’s artifact list — an artifact may exist and not be shown here.
        </Banner>
      )}
    </>
  )
}
