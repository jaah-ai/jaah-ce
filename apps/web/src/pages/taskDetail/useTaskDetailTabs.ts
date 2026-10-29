// The Task page's tab strip state: which tabs exist, the active one (in the URL), the counts and the rating's place.
import { useCallback, useEffect, useRef, useState } from 'react'
import { useStalledAnswer, useTaskAgentSessions } from '../../components/TaskActivity'
import { hasRunToShow, type RunMetricsState } from '../../components/tasks/TaskRunMetrics'
import { isChatTask } from '../../components/tasks/taskPersona'
import type { TabDef } from '../../components/Tabs'
import { useElementWidth } from '../../hooks/useHasWidth'
import { useUrlTab } from '../../hooks/useUrlTab'
import { useSearchParams } from 'react-router-dom'
import { isRatableTask } from '../../lib/taskRating'
import type { Artifact, PipelineTask } from '../../types'
import {
  ACTIVITY_TAB,
  ACTIVITY_TABS,
  AGENT_TAB,
  ANSWER_TAB,
  ARTIFACTS_TAB,
  commentsTab,
  DETAILS_TAB,
  METRICS_TAB,
  QUESTION_TAB,
  RATING_INLINE_PX,
  SUBTASKS_TAB,
  TAB_COMPACT_PX,
  TASK_TAB,
  type TaskTab,
} from './taskDetailConstants'

export type TaskDetailTabsState = ReturnType<typeof useTaskDetailTabs>

export function useTaskDetailTabs({ task, isCreate, artifacts, runMetrics, setArtifactsEverShown, canRunThisTask, script, formDirty }: {
  task: PipelineTask | null
  isCreate: boolean
  artifacts: Artifact[]
  runMetrics: RunMetricsState
  setArtifactsEverShown: (shown: boolean) => void
  canRunThisTask: boolean
  script: boolean
  /** The editor holds unsaved work (`useTaskDetailLoad`'s `formDirty`): the operator is typing on Task. */
  formDirty: boolean
}) {
  // The Activity subtab, in the URL as `?activity=qa` (`hooks/useUrlTab.ts`); own `Tabs` instance.
  const [activityTab, setActivityTab] = useUrlTab(ACTIVITY_TABS, 'timeline', 'activity')
  // A gate is open when the task is parked at one or a runner raised a question round without parking.
  // Computed above the early returns for the focus hook below.
  /* A CHAT IS NEVER "AT A GATE": `gated` feeds the tab's existence, auto-select and `GateForm`, so one
     clause withdraws all three. An advisor chat parks `awaiting_response` every turn with no question,
     and Submit would re-dispatch a SECOND runner into the live chat's slot (the API 409s). Chats open the
     Web Console instead (`components/tasks/shared.tsx` `answerTargetId`). `?tab=answer` falls back via
     `activeTab`. */
  const gated = task !== null
    && !isChatTask(task)
    && (task.state === 'awaiting_response' || (task.questions ?? []).some(q => q.status === 'open'))
  /* `gated` (is it AT a gate) decides whether the tab EXISTS for either principal — the content is a
     `tasks:view` read, and this page is its only surface; `GateForm`'s `readOnly` withholds the write
     affordances, and either principal lands on it. `answerable` (may THIS principal answer) decides the label
     and focus rescue.
     It uses `canRunThisTask`: answering is a RUN verb the `tasks:create` widening never unlocks,
     project-scoped. */
  const answerable = gated && canRunThisTask
  /* The Subtasks tab exists only with subtasks, keyed on `children` (what the panel renders; `child_count`
     is null on nested rows). `activeTab` handles the tab vanishing mid-visit. */
  const hasSubtasks = (task?.children ?? []).length > 0
  /* Likewise Artifacts, keyed on the array the panel lists. */
  const hasArtifacts = artifacts.length > 0
  /* The count comes up from the panel (kept mounted, `components/Tabs.tsx`), never a second request. */
  const [commentCount, setCommentCount] = useState(0)
  /* Likewise the control-plane messages' count, for the header chip (`TaskControlMessages`); back to none on another
     task, so its chip never shows this one's count while its list loads. */
  const [controlMessageCount, setControlMessageCount] = useState(0)
  useEffect(() => setControlMessageCount(0), [task?.id])
  const hasRunMetrics = hasRunToShow(runMetrics)
  // The query `TaskActivity` reads too (one request); empty without both session keys.
  const { hasSessions: hasAgentSessions } = useTaskAgentSessions(task?.id ?? 0, task?.state === 'active', task !== null && !isCreate)
  /* An ACP run whose handed-over answer drew no agent event: Running alone hid a swallowed answer. Read
     beside the sessions query it shares (`TaskActivity`); the banner is `TaskDetailBanners`'. */
  const stalledAnswer = useStalledAnswer(isCreate ? null : task)
  /* A GATE OPENING MID-TYPING KEEPS THE OPERATOR ON TASK: the bare-path default follows the gate to
     Answer, and the Task panel's `hidden` would blur the editor under the caret. Decided during render, at the flip
     itself (an effect would commit one frame on Answer), for the task already on screen: the operator was on Task
     and the editor is dirty or holds focus (read off the DOM during render, on purpose: moved into an effect it would
     come one frame late). The Answer tab is cued instead until visited; the hold ends with the gate or the task. */
  const lastActiveTabRef = useRef<TaskTab | null>(null)
  const [seenGate, setSeenGate] = useState<{ id: number | null; gated: boolean }>({ id: task?.id ?? null, gated })
  const [heldOnTask, setHeldOnTask] = useState<{ id: number; cue: boolean } | null>(null)
  if (seenGate.id !== (task?.id ?? null) || seenGate.gated !== gated) {
    const typing = lastActiveTabRef.current === 'task'
      && (formDirty || document.getElementById('task-panel-task')?.contains(document.activeElement) === true)
    const arrivedMidTyping = task !== null && gated && !seenGate.gated && seenGate.id === task.id && typing
    setSeenGate({ id: task?.id ?? null, gated })
    setHeldOnTask(arrivedMidTyping ? { id: task.id, cue: true } : null)
  }
  const held = heldOnTask !== null && heldOnTask.id === task?.id && gated
  const gateTab = answerable ? ANSWER_TAB : QUESTION_TAB
  const tabs: TabDef<TaskTab>[] = [
    ...(gated ? [held && heldOnTask.cue ? { ...gateTab, cue: 'new question' } : gateTab] : []),
    TASK_TAB,
    ...(hasSubtasks ? [SUBTASKS_TAB] : []),
    ...(hasArtifacts ? [ARTIFACTS_TAB] : []),
    commentsTab(commentCount),
    DETAILS_TAB,
    ...(hasAgentSessions ? [AGENT_TAB] : []),
    ...(hasRunMetrics ? [METRICS_TAB] : []),
    ACTIVITY_TAB,
  ]
  /* OPENED DONE lands on Artifacts: a finished task's result is what its reader came for. The landing is
     decided once per id, at the first read (the task and its artifacts land in one batch, `useTaskDetailLoad`), so
     neither a task turning `done` nor an artifact arriving mid-visit moves the reader; it ends if the task leaves
     `done` or the tab goes. */
  const [openedOn, setOpenedOn] = useState<{ id: number; artifacts: boolean } | null>(null)
  const landsNow = task?.state === 'done' && hasArtifacts
  useEffect(() => {
    if (task !== null && openedOn?.id !== task.id) setOpenedOn({ id: task.id, artifacts: landsNow })
  }, [task, openedOn, landsNow])
  const landOnArtifacts = landsNow && (openedOn?.id === task?.id ? openedOn?.artifacts === true : true)
  /* The tab lives in the URL (`hooks/useUrlTab.ts`), so a link lands on it (e.g. a mention → Comments). An
     explicit `?tab=` wins; absent, a gate lands on its tab for either principal — Answer for one who may answer,
     Question for a `tasks:view` reader, since the gate is what the task is waiting on (both are the
     `answer` id) — and a task opened done on Artifacts. */
  const [activeTab, selectTab] = useUrlTab(tabs, gated && !held ? 'answer' : landOnArtifacts ? 'artifacts' : 'task')
  /* THE HOLD OUTLIVES A REMOUNT: crossing the split width moves this page between panes, and the hold's
     state dies with the old instance, so the new one would land on Answer. While held on Task the URL says so (the
     cue does not survive the remount; the hold does). */
  const pinTask = held && activeTab === 'task'
  const pinnedTask = useSearchParams()[0].get('tab') === 'task'
  useEffect(() => {
    if (pinTask) selectTab('task', true)
  }, [pinTask, selectTab])
  useEffect(() => {
    lastActiveTabRef.current = activeTab
    // Visiting the cued tab is the cue's whole job done.
    if (activeTab === 'answer') setHeldOnTask(h => (h?.cue ? { ...h, cue: false } : h))
  }, [activeTab])
  /* The rating's place (see `RATING_INLINE_PX`): beside the tabs while they fit together, else an item in the header's
     ⋯ that opens it in a dialog. Unmeasured (jsdom) it stays inline. */
  const [tabsRow, setTabsRow] = useState<HTMLDivElement | null>(null)
  const tabsRowWidth = useElementWidth(tabsRow)
  const rateable = task !== null && isRatableTask(task) && !script
  const ratingFolded = rateable && tabsRowWidth !== null
    && tabsRowWidth < tabs.length * TAB_COMPACT_PX + RATING_INLINE_PX
  /* THE FOLDED RATING STILL SHOWS ITS AVERAGE: with the stars behind the ⋯ ("Rate result") nothing on
     the page said how the result was rated, so the compact badge sits beside the state pill. The detail's own
     `rating_average`/`rating_count` until the caller votes here; then the vote's answer (`onRated`), held only for
     the detail it was cast on: the next read of the task (a live event, others' votes) answers again. */
  const [ratedNow, setRatedNow] = useState<{ of: PipelineTask; average: number | null; count: number } | null>(null)
  const foldedRating = ratingFolded
    ? ratedNow !== null && ratedNow.of === task ? ratedNow : { average: task?.rating_average ?? null, count: task?.rating_count ?? 0 }
    : null
  const onRated = useCallback(
    (r: { task_id: number; average: number | null; count: number }) => {
      if (task !== null && r.task_id === task.id) setRatedNow({ of: task, average: r.average, count: r.count })
    },
    [task],
  )
  const [ratingOpen, setRatingOpen] = useState(false)

  /* Flip the artifact latch the first time the tab shows (deep links and auto-select included); reset
     only by the `[id]` effect. */
  useEffect(() => {
    if (activeTab === 'artifacts') setArtifactsEverShown(true)
  }, [activeTab])

  // The gate closing unmounts the `answer` tab (and an answerer's focused Submit); move focus to the tab that took
  // over. Only if focus was actually orphaned, so an SSE-arrived answer never yanks focus mid-typing. `gated`, not
  // `answerable`: a reader lands on the Question tab too, so its focus can be orphaned the same way.
  /* The same closing edge drops the hold's `?tab=task` pin (above), so the NEXT question lands on Answer again: the pin
     is dropped only while the URL carries it (a write on a bare path would re-navigate, over an answer's return to the
     list), whose key goes once Task is the fallback again. */
  const wasGatedRef = useRef(false)
  useEffect(() => {
    const orphaned = document.activeElement === null || document.activeElement === document.body
    if (wasGatedRef.current && !gated && orphaned) {
      document.getElementById(`task-tab-${activeTab}`)?.focus()
    }
    if (wasGatedRef.current && !gated && pinnedTask) selectTab('task')
    wasGatedRef.current = gated
  }, [gated, activeTab, selectTab, pinnedTask])

  return {
    gated, answerable, hasSubtasks, hasArtifacts, hasRunMetrics, hasAgentSessions, stalledAnswer, tabs, activeTab, selectTab,
    activityTab, setActivityTab, commentCount, setCommentCount, controlMessageCount, setControlMessageCount, setTabsRow,
    rateable, ratingFolded, foldedRating, onRated, ratingOpen, setRatingOpen,
  }
}
