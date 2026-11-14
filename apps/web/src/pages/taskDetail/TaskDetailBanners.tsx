// The banners above the Task page's tab panels: a pause, action failures, why it failed.
// An answer the agent never picked up is the Q&A tab's (`StalledAnswerBanner`), where delivery is shown; a task with no
// Q&A tab (a chat) shows it here.
// An org's hold on a ready task is the header pill's alone (its full sentence on its hover, `waitReasonHover`).
import Box from '@mui/material/Box'
import Link from '@mui/material/Link'
import Typography from '@mui/material/Typography'
import { Link as RouterLink } from 'react-router-dom'
import { SYSTEM_PAUSE_CAUSE } from '../../components/tasks/pane/stopReason'
import type { PipelineTask } from '../../types'
import { FONT_WEIGHT } from '../../theme'
import Banner from '../../ui/Banner'
import { AdvancedSection } from '../../ui/AdvancedSection'
import type { StalledAnswer } from '../../lib/answerPickup'
import StalledAnswerBanner from './StalledAnswerBanner'
import type { TaskDetailActions } from './useTaskDetailActions'
import type { FixLink, TaskDetailDerived } from './useTaskDetailDerived'
import type { TaskDetailRead } from './useTaskDetailLoad'

export default function TaskDetailBanners({ task, canRunThisTask, unheardAnswer, read, derived, actions }: {
  task: PipelineTask
  canRunThisTask: boolean
  /** An answer the agent never picked up on a task with no Q&A tab to show it (`useStalledAnswer`), else `null`. */
  unheardAnswer: StalledAnswer | null
  read: TaskDetailRead
  derived: TaskDetailDerived
  actions: TaskDetailActions
}) {
  const { artifactsError, setArtifactsError } = read
  const { failureCause } = derived
  const { actionError, setActionError, savedNotice, setSavedNotice } = actions
  return (
    <>
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
          <Box sx={{ display: 'flex', flexDirection: 'column', gap: 0, minWidth: 0 }}>
            <Typography variant="body2" sx={{ fontWeight: FONT_WEIGHT.semibold }}>{failureCause.label}</Typography>
            {/* `overflowWrap: 'anywhere'`: hostnames and slugs are unbreakable. The `ch` cap is a reading
                measure for wide desktops; phones are width-bound first. */}
            <Typography variant="body2" sx={{ overflowWrap: 'anywhere', maxWidth: '78ch' }}>
              <SummaryWithLink summary={failureCause.summary} link={failureCause.link} />
            </Typography>
            {/* What the agent wrote below its plain first paragraph (a log tail, an error) is for whoever
                digs in: folded, never in the reader's way (runner charter § How you write for a person). */}
            {failureCause.details && (
              <AdvancedSection dense label="Details" testId="task-page-terminal-cause-details">
                <Typography
                  variant="body2"
                  component="pre"
                  sx={{ m: 0, whiteSpace: 'pre-wrap', overflowWrap: 'anywhere', maxWidth: '78ch', fontFamily: 'inherit' }}
                >
                  {failureCause.details}
                </Typography>
              </AdvancedSection>
            )}
          </Box>
        </Banner>
      )}


      <StalledAnswerBanner task={task} canRunThisTask={canRunThisTask} stalledAnswer={unheardAnswer} actions={actions} />

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

/* The summary with its fix words linked to the page that fixes it; plain when they are absent from it. */
function SummaryWithLink({ summary, link }: { summary: string; link: FixLink | null }) {
  const at = link ? summary.indexOf(link.text) : -1
  if (link === null || at < 0) return <>{summary}</>
  return (
    <>
      {summary.slice(0, at)}
      <Link component={RouterLink} to={link.href} data-testid="task-page-terminal-cause-link">{link.text}</Link>
      {summary.slice(at + link.text.length)}
    </>
  )
}
