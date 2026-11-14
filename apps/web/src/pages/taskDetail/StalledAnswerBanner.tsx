// The Q&A tab's warning for an answer the agent never picked up: the header still says Running, and delivery is the
// Q&A tab's to show (the trail's "Sent to the runner", the gate's "Delivering…"). What the node said of it picks the
// sentence and the remedies — the ⋯'s own Stop and Reset (same gates, confirms and busy flag) and the header's console
// door, for a viewer who may run the task. The console only where the agent holds the answer: it shows nothing of one
// the node never took.
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import type { PipelineTask } from '../../types'
import type { StalledAnswer } from '../../lib/answerPickup'
import { clockTime, humanDuration } from '../../lib/relativeTime'
import Banner from '../../ui/Banner'
import type { OverflowItem } from '../../ui/OverflowMenu'
import type { TaskDetailActions } from './useTaskDetailActions'

export default function StalledAnswerBanner({ task, canRunThisTask, stalledAnswer, actions }: {
  task: PipelineTask
  canRunThisTask: boolean
  /** An answer the agent never picked up (`useStalledAnswer`), or `null`. */
  stalledAnswer: StalledAnswer | null
  actions: TaskDetailActions
}) {
  if (stalledAnswer === null) return null
  const { pageActions, openRunnerConsole, acting } = actions
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
}
