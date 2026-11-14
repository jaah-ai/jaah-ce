// The Task page's tab strip state: which tabs exist, the active one (in the URL), the counts and the rating's place.
import { useCallback, useEffect, useRef, useState } from 'react'
import { useStalledAnswer, useTaskAgentSessions } from '../../components/TaskActivity'
import { hasRunToShow, type RunMetricsState } from '../../components/tasks/TaskRunMetrics'
import { isChatTask } from '../../components/tasks/taskPersona'
import type { TabDef } from '../../components/Tabs'
import { useElementWidth } from '../../hooks/useHasWidth'
import { useUrlTab } from '../../hooks/useUrlTab'
import { useSearchParams } from 'react-router-dom'
import { useAuth } from '../../auth'
import { planSurfaceShown } from '../../lib/planSwitches'
import { isRatableTask } from '../../lib/taskRating'
import { useViewerMode } from '../../lib/viewerMode'
import { noticesOf, useTaskNotices } from './taskNotices'
import type { Artifact, PipelineTask } from '../../types'
import { useTaskBodyVersions } from '../../components/tasks/TaskVersions'
import {
  AGENT_TAB,
  ARTIFACTS_TAB,
  DETAILS_TAB,
  DETAILS_SUBTABS,
  QA_TAB,
  commentsTab,
  LEGACY_TAB_SUBTABS,
  RATING_INLINE_PX,
  ROUTING_TAB,
  SUBTASKS_TAB,
  TAB_COMPACT_PX,
  TASK_TAB,
  TASK_TAB_ALIASES,
  type DetailsSubtab,
  type TaskTab,
} from './taskDetailConstants'
import type { ActivityLogEntry } from './ActivityLog'
import { useTaskOutcomes } from './TaskOutcomePanel'
import { useTaskReviewFindings } from './TaskReviewFindingsPanel'

export type TaskDetailTabsState = ReturnType<typeof useTaskDetailTabs>

export function useTaskDetailTabs({ task, isCreate, activity, artifacts, runMetrics, setArtifactsEverShown, script, formDirty }: {
  task: PipelineTask | null
  isCreate: boolean
  activity: ActivityLogEntry[]
  artifacts: Artifact[]
  runMetrics: RunMetricsState
  setArtifactsEverShown: (shown: boolean) => void
  script: boolean
  /** The editor holds unsaved work (`useTaskDetailLoad`'s `formDirty`): the operator is typing on Task. */
  formDirty: boolean
}) {
  // A gate is open when the task is parked at one or a runner raised a question round without parking.
  // Computed above the early returns for the focus hook below.
  /* A CHAT IS NEVER "AT A GATE": `gated` feeds the tab's existence, auto-select and `GateForm`, so one
     clause withdraws all three. An advisor chat parks `awaiting_response` every turn with no question,
     and Submit would re-dispatch a SECOND runner into the live chat's slot (the API 409s). Chats open the
     Web Console instead (`components/tasks/shared.tsx` `answerTargetId`). */
  const gated = task !== null
    && !isChatTask(task)
    && (task.state === 'awaiting_response' || (task.questions ?? []).some(q => q.status === 'open'))
  /* `gated` (is it AT a gate) puts the gate's form on Q&A for either principal — the content is a
     `tasks:view` read, and this page is its only surface; `GateForm`'s `readOnly` withholds the write
     affordances, and either principal lands on it. */
  /* The Subtasks tab exists only with subtasks, keyed on `children` (what the panel renders; `child_count`
     is null on nested rows). `activeTab` handles the tab vanishing mid-visit. */
  const hasSubtasks = (task?.children ?? []).length > 0
  /* Likewise Artifacts, keyed on the array the panel lists. */
  const hasArtifacts = artifacts.length > 0
  /* Q&A exists at a gate (its form) or once the task has a Q&A trail; a chat has neither (`gated` is false). */
  const hasQA = gated || (task !== null && !isChatTask(task) && (task.qa_entries ?? []).length > 0)
  /* The count comes up from the panel (Comments is never `lazy`, `components/Tabs.tsx`), never a second request. */
  const [commentCount, setCommentCount] = useState(0)
  /* The control-plane notices, read HERE: merged into the Timeline, whose subtab exists only with a row to show. Not on
     the Live Cast viewer (`lib/viewerMode.ts`): runner traffic is an operator's. */
  const viewer = useViewerMode()
  const notices = useTaskNotices(task !== null && !isCreate && !viewer ? task.id : null)
  const hasRunMetrics = hasRunToShow(runMetrics)
  /* Details' Outcome exists once the ledger holds a row for the task (one read, handed to the panel). Not on the Live
     Cast viewer: the public API serves no ledger. */
  const outcomes = useTaskOutcomes(isCreate ? null : task, !viewer)
  const hasOutcome = (outcomes.data?.rows ?? []).length > 0 || Boolean(outcomes.data?.duel)
  /* Routing is every task's, read only once opened (`RoutingPanel`); not on the Live Cast viewer, nor for a tenant
     whose plan leaves `intelligent_routing` out. */
  const routingShown = planSurfaceShown(useAuth().admin, 'intelligent_routing')
  const hasRouting = task !== null && !isCreate && !viewer && routingShown
  /* Details' Review: once a review lane reported one (one read, handed to the panel). Not on the Live Cast viewer. */
  const findings = useTaskReviewFindings(isCreate ? null : task, !viewer)
  const hasFindings = (findings.data?.rows ?? []).length > 0
  // The query `TaskActivity` reads too (one request); empty without both session keys.
  const { hasSessions: hasAgentSessions } = useTaskAgentSessions(task?.id ?? 0, task?.state === 'active', task !== null && !isCreate)
  /* An ACP run whose handed-over answer drew no agent event: Running alone hid a swallowed answer. Read
     beside the sessions query it shares (`TaskActivity`); the banner is Q&A's (`StalledAnswerBanner`), cued on the tab. */
  const stalledAnswer = useStalledAnswer(isCreate ? null : task)
  /* A GATE OPENING MID-TYPING KEEPS THE OPERATOR ON TASK: the bare-path default follows the gate to
     Q&A, and the Task panel's `hidden` would blur the editor under the caret. Decided during render, at the flip
     itself (an effect would commit one frame on Answer), for the task already on screen: the operator was on Task
     and the editor is dirty or holds focus (read off the DOM during render, on purpose: moved into an effect it would
     come one frame late). The hold ends with the gate or the task; Q&A's cue (below) says where the question is. */
  const lastActiveTabRef = useRef<TaskTab | null>(null)
  const [seenGate, setSeenGate] = useState<{ id: number | null; gated: boolean }>({ id: task?.id ?? null, gated })
  const [heldOnTask, setHeldOnTask] = useState<number | null>(null)
  if (seenGate.id !== (task?.id ?? null) || seenGate.gated !== gated) {
    const typing = lastActiveTabRef.current === 'task'
      && (formDirty || document.getElementById('task-panel-task')?.contains(document.activeElement) === true)
    const arrivedMidTyping = task !== null && gated && !seenGate.gated && seenGate.id === task.id && typing
    setSeenGate({ id: task?.id ?? null, gated })
    setHeldOnTask(arrivedMidTyping ? task.id : null)
  }
  const held = heldOnTask === task?.id && gated
  const tabs: TabDef<TaskTab>[] = [
    TASK_TAB,
    ...(hasSubtasks ? [SUBTASKS_TAB] : []),
    ...(hasArtifacts ? [ARTIFACTS_TAB] : []),
    /* AN OPEN ROUND MARKS Q&A for every reader, on every tab, until it is answered: `gated` is the one
       "unanswered" signal, in the console's "asking" hue (`warning`). An answer the agent never picked up marks it
       too: its warning is on Q&A, where delivery is shown. */
    ...(hasQA
      ? [gated ? { ...QA_TAB, cue: 'unanswered', cueTone: 'warning' as const }
        : stalledAnswer ? { ...QA_TAB, cue: 'answer not picked up', cueTone: 'warning' as const } : QA_TAB]
      : []),
    commentsTab(commentCount),
    ...(hasAgentSessions ? [AGENT_TAB] : []),
    ...(hasRouting ? [ROUTING_TAB] : []),
    DETAILS_TAB,
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
  /* The tab lives in the URL (`hooks/useUrlTab.ts`), so a link lands on it (e.g. a mention → Comments); a retired
     id lands through `TASK_TAB_ALIASES`. An explicit `?tab=` wins; absent, a gate lands on Q&A for either
     principal, since the gate is what the task is waiting on, and a task opened done on Artifacts. */
  const [activeTab, selectTab] = useUrlTab(tabs, gated && !held ? 'qa' : landOnArtifacts ? 'artifacts' : 'task', 'tab', TASK_TAB_ALIASES)
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
  }, [activeTab])
  /* The rating's place (see `RATING_INLINE_PX`): beside the tabs while they fit together, else a field on the Task tab
     (`TaskBodyPanel`). Unmeasured (jsdom) it stays inline. */
  const [tabsRow, setTabsRow] = useState<HTMLDivElement | null>(null)
  const tabsRowWidth = useElementWidth(tabsRow)
  const rateable = task !== null && isRatableTask(task) && !script
  const ratingFolded = rateable && tabsRowWidth !== null
    && tabsRowWidth < tabs.length * TAB_COMPACT_PX + RATING_INLINE_PX
  /* THE FOLDED RATING STILL SHOWS ITS AVERAGE: with the stars on the Task tab nothing on the other tabs said how the
     result was rated, so the compact badge sits beside the state pill. The detail's own
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

  /* Flip the artifact latch the first time the tab shows (deep links and auto-select included); reset
     only by the `[id]` effect. */
  useEffect(() => {
    if (activeTab === 'artifacts') setArtifactsEverShown(true)
  }, [activeTab])

  // The gate closing unmounts the gate's form on Q&A (and an answerer's focused Submit); move focus to the tab that took
  // over. Only if focus was actually orphaned, so an SSE-arrived answer never yanks focus mid-typing. Keyed on `gated`:
  // a reader lands on Q&A too, so its focus can be orphaned the same way.
  /* The same closing edge drops the hold's `?tab=task` pin (above), so the NEXT question lands on Q&A again: the pin
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

  /* DETAILS' SUBTABS, only those with something to show: Summary always, Outcome once the ledger has a row, Metrics once
     there is a run, Timeline once anything was logged, Versions once the description has a snapshot (this read, handed
     to `TaskVersions`), Review once a review lane reported a finding. The subtab is in the URL as `?details=versions`;
     the first drawn is the default. */
  const bodyVersions = useTaskBodyVersions(task !== null && !isCreate ? task : null)
  const hasTimeline = activity.length > 0 || noticesOf(notices).length > 0
  const hasVersions = (bodyVersions.data ?? []).length > 0
  const shown: Record<DetailsSubtab, boolean> = {
    summary: true, outcome: hasOutcome, metrics: hasRunMetrics, timeline: hasTimeline, versions: hasVersions, review: hasFindings,
  }
  const detailsSubtabs: TabDef<DetailsSubtab>[] = DETAILS_SUBTABS.filter(t => shown[t.id])
  const [detailsSubtab, setDetailsSubtab] = useUrlTab(detailsSubtabs, 'summary', 'details')
  /* Details opened on one subtab, in ONE URL write: two strips' selects in a row would each start from the same
     query and the second would drop the first's key. */
  const [searchParams, setSearchParams] = useSearchParams()
  /* The retired sub-strip param (`?audit=timeline`, from when Details was Audit) moves to `?details=`, so an old link
     still lands on its subtab; its `details` subtab is Summary, the default, so it just goes. */
  const legacySubtab = searchParams.get('audit')
  useEffect(() => {
    if (legacySubtab === null) return
    setSearchParams(prev => {
      const params = new URLSearchParams(prev)
      params.delete('audit')
      if (!params.has('details') && legacySubtab !== 'details') params.set('details', legacySubtab)
      return params
    }, { replace: true })
  }, [legacySubtab, setSearchParams])
  /* Likewise a retired top-level tab that is now a subtab (`?tab=outcome`, `?tab=findings`): `TASK_TAB_ALIASES` lands
     it on Details, and this names its subtab. */
  const rawTab = searchParams.get('tab')
  const legacyTabSubtab = rawTab !== null && Object.hasOwn(LEGACY_TAB_SUBTABS, rawTab) ? LEGACY_TAB_SUBTABS[rawTab] : null
  useEffect(() => {
    if (legacyTabSubtab === null) return
    setSearchParams(prev => {
      const params = new URLSearchParams(prev)
      params.set('tab', 'details')
      if (!params.has('details')) params.set('details', legacyTabSubtab)
      return params
    }, { replace: true })
  }, [legacyTabSubtab, setSearchParams])
  const openDetails = useCallback((sub: DetailsSubtab) => {
    setSearchParams(prev => {
      const params = new URLSearchParams(prev)
      params.set('tab', 'details')
      if (sub === 'summary') params.delete('details')
      else params.set('details', sub)
      return params
    }, { replace: true })
  }, [setSearchParams])

  return {
    gated, hasSubtasks, hasArtifacts, hasQA, hasOutcome, outcomes, hasFindings, findings, hasRouting, hasRunMetrics, hasAgentSessions, hasTimeline, hasVersions,
    stalledAnswer, tabs, activeTab, selectTab, bodyVersions, detailsSubtabs, detailsSubtab, setDetailsSubtab, openDetails, commentCount, setCommentCount, notices, setTabsRow,
    rateable, ratingFolded, foldedRating, onRated,
  }
}
