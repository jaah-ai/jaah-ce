// The Task page's reads: the cold load and its re-reads, the roster and slots beside it, and the live refetch.
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import { ApiError, get } from '../../api'
import { useCan } from '../../auth'
import { forgetDeletedTasks } from '../../components/tasks/pane/queryKeys'
import { useTaskRunMetrics } from '../../components/tasks/TaskRunMetrics'
import { useEventStream } from '../../hooks/useEventStream'
import { PROJECTS_KEY, listProjects, listWorktrees } from '../../hooks/useRosters'
import { STREAM_DOWN_POLL_MS, useEventsSinceSeen, useLiveRefetch } from '../../hooks/useLiveRefetch'
import { useOrgScope } from '../../lib/orgScope'
import { loadTaskDuplicateSeed, type TaskDuplicateSeed } from '../../lib/taskDuplicate'
import { touchesScope } from '../../lib/taskEventScope'
import {
  boardRow,
  dropTaskPageSeed,
  registerLandedTaskRead,
  seedTaskPage,
  takeLandedTaskRead,
  type LandedTaskRead,
  type TaskPageTrail,
} from '../../lib/taskPageSeed'
import { queryClient } from '../../queryClient'
import type { Artifact, PipelineTask, Project, Worktree } from '../../types'
import type { ActivityLogEntry } from './ActivityLog'
import { forgetColdReads, readActivity, readArtifacts, readColdOnce } from './coldRead'
import { duplicateShortfall } from './duplicateSeed'

export type TaskDetailRead = ReturnType<typeof useTaskDetailLoad>

/** The cold read's trail names, with the row and filing they were read for: a name answers only while
 *  the page still shows that task in that project and feature (a move re-files it under names the API sent nowhere). */
export type TaskTrailNames = TaskPageTrail & { taskId: number; projectId: number | null; featureId: string | null }

/** `id`: the route's task id. `duplicateOf`: the row a create copies (`null` = a blank create). */
export function useTaskDetailLoad(
  { id, isCreate, duplicateOf }: { id: string | undefined; isCreate: boolean; duplicateOf: number | null },
) {
  /* The read a remount across the split width handed over (`lib/taskPageSeed.ts::takeLandedTaskRead`): the page
     starts from it, loaded, and skips its first read. Asked once, at mount. */
  const scopeKey = useOrgScope()?.map(org => org.id).join(',') ?? ''
  const [handed] = useState(() => (!isCreate && id ? takeLandedTaskRead(id, scopeKey) : undefined))
  // The id `handed` belongs to while the page still shows it; cleared once the page moves to another id.
  const handedIdRef = useRef(handed ? id : undefined)
  const [task, setTask] = useState<PipelineTask | null>(handed?.task ?? null)
  const [activity, setActivity] = useState<ActivityLogEntry[]>((handed?.activity as ActivityLogEntry[] | undefined) ?? [])
  /* Artifact metadata only (bytes load in the frame or child). Its own state: every action handler
     rebuilds `task` from a response that knows nothing about artifacts. */
  const [artifacts, setArtifacts] = useState<Artifact[]>(handed?.artifacts ?? [])
  /* The artifact LIST failed to load — a don't-know, not folded into `artifacts` (absence is not a
     state: unknown is never shown as empty). Said in a banner so a 500 does not read as "handed
     nothing back"; the tab still keys on `artifacts.length > 0`. */
  const [artifactsError, setArtifactsError] = useState(handed?.artifactsError ?? false)
  /* ── Has the Artifacts tab EVER been activated on this task? ──────────────────────────────────────
     A `display: none` iframe STILL FETCHES its `src`, so an always-rendered frame would download the
     blob (up to the per-file cap) on every visit. A LATCH, not `activeTab === 'artifacts'`: gating on the
     active tab would unmount on switch-away, re-fetching and losing scroll. `useState` from an effect,
     not a render-phase ref write (StrictMode double render). */
  const [artifactsEverShown, setArtifactsEverShown] = useState(false)
  const [loading, setLoading] = useState(handed === undefined)
  /* The row as the board's list holds it (`boardRow`): a cold open from the board paints its trail —
     project, feature, title — while the page's own read is in flight. Display only, and only until that read lands.
     Per id, not per mount: the page stays mounted from one task to the next (a row picked in the split), and a
     mount-time row would head the next task's load with the last one's trail. */
  const [handedId] = useState(() => (handed ? id : undefined))
  const cachedRow = useMemo(
    () => (isCreate || !id || id === handedId ? undefined : boardRow(Number(id))),
    [id, isCreate, handedId],
  )
  /* Task Run Metrics, read here (not in its panel) so the strip can gate the Metrics tab without mounting a
     hidden panel (the subtasks/artifacts rule). Read once this id's task has landed, so a cold load's first
     read is the `…/page` bundle's part, and the page waits for it (`loading` below): like
     Artifacts, a late tab would shift Activity under a travelling pointer. Live: a task that starts running
     grows the tab. */
  /* A DELETE IN FLIGHT ENDS THE PAGE'S READS: the row's own `task` event, or the cache drop, would
     re-read `…/run-metrics` and `…/live-cast` for a row already gone — two console 404s. Cleared if refused. */
  const [deleting, setDeleting] = useState(false)
  const runMetricsId = !isCreate && !deleting && task !== null && String(task.id) === id ? task.id : null
  const runMetrics = useTaskRunMetrics(runMetricsId)
  /* The copied row's parameters. `undefined` = not a copy or not landed — safe to conflate only because
     the create branch waits on `loading`, which the read rides. */
  const [duplicateSeed, setDuplicateSeed] = useState<TaskDuplicateSeed | undefined>(undefined)
  /* What the copy could NOT bring. Not `error`, which blanks the page: the form works, with a line saying
     what is missing. */
  const [duplicateWarning, setDuplicateWarning] = useState('')
  const [notFound, setNotFound] = useState(handed?.notFound ?? false)
  const [error, setError] = useState('')
  // Whether the editor holds unsaved work, reported up by the form. Holds the form's own row (`formTask`).
  const [formDirty, setFormDirty] = useState(false)
  // For the Details runner block; fire-and-forget (failure = no worktree link).
  const [worktrees, setWorktrees] = useState<Worktree[]>([])
  /* `GET /api/projects`: the breadcrumb's project menu, the create/move ladder, the runner block
     (`runnerProjectName`) and the From Jira control read it. */
  /* Starts from the roster another surface already read (`['projects']`, the board's), so a page opened from the board
     names its project on first paint; the page's own read below still replaces it. */
  const [projects, setProjects] = useState<Project[]>(() => queryClient.getQueryData<Project[]>(PROJECTS_KEY) ?? [])
  // Until `/api/projects` answers, the trail's project crumb is a skeleton (`TaskBreadcrumb`'s `projectPending`).
  const [projectsPending, setProjectsPending] = useState(() => !Array.isArray(queryClient.getQueryData(PROJECTS_KEY)))
  /* The project and feature NAMES `GET …/page` sent beside the row: the trail names both at once on a
     cold page, before the roster or the task tree lands. Set by each cold read (null without them: a fallback read,
     an older API); a re-read keeps them, and the breadcrumb asks them only for the filing they were read for. */
  const [trail, setTrail] = useState<TaskTrailNames | null>(null)
  // Generation guard: mount and SSE refetches can resolve out of order; only the latest applies.
  const fetchGenRef = useRef(0)
  // The id whose cold load (`GET …/page`) has landed: any later read of it is a re-read (see `load`).
  const coldIdRef = useRef<string | null>(handed && id ? id : null)
  // The latest read's generation once it has landed: below `fetchGenRef`, a read is in flight.
  const landedGenRef = useRef(0)
  // A live event moved this page and its debounced re-read has not started yet.
  const liveStaleRef = useRef(false)
  // Own-task events heard so far, quiet ones included (the effect below counts them): a cold load stores its bundle only if none arrived
  // while its `GET …/page` was in flight, since that bundle may predate the event.
  const ownEventsRef = useRef(0)

  const load = useCallback(async () => {
    // Create mode has no task. `loading` is NOT cleared here: the form resolves its project once at mount
    // from the roster, so the roster fetch below owns the flip.
    if (isCreate) return
    if (!id) return
    const gen = ++fetchGenRef.current
    liveStaleRef.current = false
    // A non-numeric segment is an unknown id (the API would 422, not 404).
    if (!/^\d+$/.test(id)) {
      setNotFound(true)
      setError('')
      setLoading(false)
      return
    }
    const cold = coldIdRef.current !== id
    try {
      /* COLD load (the first for this id): `readColdOnce` above. The task's own GET (or `…/page`'s propagated
         refusal) decides the page's error. Every later read (a live event, a refresh) drops the seed first and
         reads per route, so no re-read answers from it. An own-task event heard while `…/page` was in flight leaves
         its bundle unstored: the panels read their own GETs. */
      // An earlier visit's untaken parts never answer this read's panels (the seed has no clock).
      dropTaskPageSeed(id)
      const heard = ownEventsRef.current
      const { bundle, detail, activity: activityEvents, artifacts: artifactList, trail: trailNames } = cold
        ? await readColdOnce(id)
        : await Promise.all([get<PipelineTask>(`/api/tasks/${id}`), readActivity(id), readArtifacts(id)])
          .then(([detail, activity, artifacts]) => ({ bundle: null, detail, activity, artifacts, trail: null }))
      if (bundle !== null && gen === fetchGenRef.current && ownEventsRef.current === heard) seedTaskPage(id, bundle)
      if (gen !== fetchGenRef.current) return // superseded — discard
      coldIdRef.current = id
      if (cold) {
        setTrail(trailNames
          ? { ...trailNames, taskId: detail.id, projectId: detail.project_id, featureId: detail.feature_id ?? null }
          : null)
      }
      setTask(detail)
      setActivity(Array.isArray(activityEvents) ? activityEvents : [])
      setArtifacts(Array.isArray(artifactList) ? artifactList : [])
      // Set and cleared from the one place that knows.
      setArtifactsError(artifactList === null)
      setNotFound(false)
      setError('')
    } catch (err) {
      if (gen !== fetchGenRef.current) return
      if (err instanceof ApiError && (err.status === 404 || err.status === 422)) {
        // Gone for good: no cache keeps it (`forgetDeletedTasks`), and the live refetch stops (`notFound`).
        if (err.status === 404) forgetDeletedTasks(queryClient, [Number(id)])
        setNotFound(true)
        setError('')
      } else if (cold) {
        setError('Failed to load task detail')
      }
      /* A failed RE-read keeps the row on screen: the error page would unmount an editor with unsaved
         edits, and the next event or visit reads again. */
    } finally {
      if (gen === fetchGenRef.current) {
        landedGenRef.current = gen
        setLoading(false)
      }
    }
  }, [id, isCreate])

  useEffect(() => {
    // A handed-over read is this id's first read (StrictMode's second run included).
    if (handed !== undefined && handedIdRef.current === id) return
    handedIdRef.current = undefined
    setLoading(true)
    void load()
  }, [load, id])

  // `/tasks/A → /tasks/B` keeps the same component instance, so every per-task slice must reset with the
  // id (e.g. A's artifacts over B's). The tab is not among them: it is the new URL's `?tab=`. The page's other
  // per-task slices (the crumb drafts, the gate error) reset beside their owners, on the same guard (`keepsHandedRead`).
  useEffect(() => {
    // A handed-over mount is not a move: its artifacts are this id's.
    if (handed !== undefined && handedIdRef.current === id) return
    // A's artifacts belong to A. Not observable today (the `loading` branch blanks the strip), cleared anyway.
    setArtifacts([])
    // A's failure banner, and the eager-fetch latch (carried over it would mount B's frame unopened).
    setArtifactsError(false)
    setArtifactsEverShown(false)
    setFormDirty(false)
  }, [id])
  /* Whether this mount still shows the read a remount handed over — the guard every per-task reset shares, read
     inside an effect (after the load effect above has run). */
  const keepsHandedRead = () => handed !== undefined && handedIdRef.current === id

  // The runner block's inputs: fire-and-forget. Keyed on the task-loaded flip (not `task`), so a
  // not-found link issues nothing and SSE refetches do not re-pull worktrees.
  const hasTask = task !== null
  // `GET /api/worktrees` needs `worktrees:view`; skip rather than 403 on every open.
  const can = useCan()
  const canReadWorktrees = can('worktrees:view')
  const loadWorktrees = useCallback(() => {
    if (!canReadWorktrees) return
    void listWorktrees()
      .then(ws => setWorktrees(Array.isArray(ws) ? ws : []))
      .catch(() => { /* keep the last-known set; the link simply won't update this cycle */ })
  }, [canReadWorktrees])

  useEffect(() => {
    // Create mode fetches `projects` (the ladder, the crumb picker, and the From Jira control's credential),
    // but never worktrees.
    if (isCreate) {
      void Promise.allSettled([
        listProjects().then(ps => setProjects(Array.isArray(ps) ? ps : [])).finally(() => setProjectsPending(false)),
        /* The copied row rides the same gate: `TaskEditForm` seeds a copy in `useState` initialisers, so a
           late seed would be ignored. Its failure is a WARNING over a working form, never the page error. */
        duplicateOf === null
          ? Promise.resolve()
          : loadTaskDuplicateSeed(duplicateOf).then(
              seed => {
                setDuplicateSeed(seed)
                setDuplicateWarning(duplicateShortfall(duplicateOf, seed))
              },
              /* Says nothing was copied; the form may still show the board's prefill. */
              () => setDuplicateWarning(`Could not read task #${duplicateOf} — nothing was copied.`),
            ),
      ])
        /* `allSettled`, NOT `all`: `all` rejects on the first failure and `finally` would mount the form
           before the seed lands, silently discarding it (initialisers, no `key`). Each leg still reports
           its own outcome. */
        .finally(() => setLoading(false))
      return
    }
    if (!hasTask) return
    loadWorktrees()
  }, [hasTask, isCreate, loadWorktrees, duplicateOf])

  /* Fetched once per page view, BESIDE the task's own read rather than after it, so the trail names its project when
     the row lands. `projects` maps `project_id` to the runner block's repo name and feeds the crumb picker. */
  useEffect(() => {
    // Not for an id that is not one: that page is not-found without a read (the load effect).
    if (isCreate || !id || !/^\d+$/.test(id)) return
    void listProjects()
      .then(ps => setProjects(Array.isArray(ps) ? ps : []))
      .catch(() => { /* no worktree link or crumb picker this cycle */ })
      .finally(() => setProjectsPending(false))
  }, [isCreate, id])

  /* Live: refetch detail + activity on a `task` event THAT CAN MOVE THIS PAGE — this task, its parent
     or a child (`touchesScope`, births included), debounced so a burst costs one read, deferred while the tab is
     hidden; while the stream is down, the same read polls every `STREAM_DOWN_POLL_MS`. Every fleet event used to
     re-read four routes here, and a deleted task's page kept doing so.
     NEVER after a 404 (`notFound`): a deleted id stays deleted; only a new `id` loads again.
     An editor with unsaved work does NOT hold it: the state, the header and a raised question must reach
     the page whatever the form holds. The form keeps its own row instead (`formTask` below). */
  const eventStream = useEventStream()
  /* A task that WAITS on others reads their moves into its own payload (`unmet_prerequisite_count`, `wait_reason`'s
     slot count, `waiting_on_persona`), under ids this page does not hold: it keeps hearing every event (`null`). */
  const liveScope = useMemo<ReadonlySet<number> | null>(
    () =>
      task === null || (task.unmet_prerequisite_count ?? 0) > 0 || task.wait_reason || task.waiting_on_persona
        ? null
        : new Set([task.id, ...(task.parent_task_id !== null ? [task.parent_task_id] : []), ...(task.children ?? []).map(c => c.id)]),
    [task],
  )
  // A delete in flight reads as gone already: its own `task` event must not re-read the row.
  const gone = notFound || deleting
  const liveRef = useRef({ notFound: gone, liveScope })
  liveRef.current = { notFound: gone, liveScope }
  useLiveRefetch(
    eventStream,
    () => {
      if (!liveRef.current.notFound) void load()
    },
    // The stream down, the page polls as the board does: a question the board shows reaches it too.
    STREAM_DOWN_POLL_MS,
    {
      entity: 'task',
      match: event => {
        const moves = !liveRef.current.notFound && touchesScope(event, liveRef.current.liveScope)
        // At once, not after the debounce: neither a panel's own live re-read nor the page's may answer from the
        // cold load (its seed, or its read still in flight).
        if (moves && id) {
          dropTaskPageSeed(id)
          forgetColdReads(id)
        }
        if (moves) liveStaleRef.current = true
        return moves
      },
    },
  )
  /* EVERY event naming this task ends its seed, a QUIET one (`hooks/useLiveRefetch.ts::QUIET_ACTIONS`: a control
     message, a metered tick) included — it moves a panel but never this page's re-read — and keeps an in-flight cold
     read from storing its bundle: a panel mounting after it reads fresh. */
  useEventsSinceSeen(eventStream, (events, overrun) => {
    if (!id) return
    // Overrun: an unread event may have named this task, so the seed goes and an in-flight cold read stores none.
    const own = events.filter(event => event.entity === 'task' && Number(event.id) === Number(id)).length
    if (own === 0 && !overrun) return
    ownEventsRef.current += Math.max(own, 1)
    dropTaskPageSeed(id)
  })

  /* THE ROW THE EDITOR AND THE CRUMB DRAFTS ARE SEEDED FROM. `TaskEditForm` is keyed on
     `id:updated_at`, so a re-read that moves the row REMOUNTS it and loses the caret and unsaved text: while the form
     is dirty it keeps the row it opened on, and the rest of the page follows `task`. Clean (saved or put back), or
     on another id, it takes the latest. Adjusted during render, so the form never commits a frame on the wrong row. */
  const [formTask, setFormTask] = useState<PipelineTask | null>(task)
  if (formTask !== task && (!formDirty || task === null || formTask === null || formTask.id !== task.id)) {
    setFormTask(task)
  }

  /* OFFER the landed read to a remount across the split width (`takeLandedTaskRead` above) — only a current one:
     none in flight, no live re-read pending, no failure; the run metrics only while their own read is
     current. A NOT-FOUND read is final (the live refetch never re-reads it), so an event heard while it was in
     flight — any fleet event, before an answer gives the page a scope — leaves no re-read pending. Mirrored into a ref from the committed render, read at take time. Accepted gap: an event committed
     in the same render as the resize reaches neither mount (the new one's stream starts empty). */
  const landedRef = useRef<LandedTaskRead | null>(null)
  useLayoutEffect(() => {
    landedRef.current = !isCreate && !loading && !error && (task !== null || notFound)
      ? {
          scope: scopeKey, task, notFound, activity, artifacts, artifactsError,
          runMetrics: runMetrics.isCurrent() ? runMetrics.metrics : null,
        }
      : null
  })
  useEffect(() => {
    if (isCreate || !id) return
    return registerLandedTaskRead(id, () => {
      if (landedGenRef.current !== fetchGenRef.current) return null
      const read = landedRef.current
      return read?.notFound || !liveStaleRef.current ? read : null
    })
  }, [id, isCreate])

  // …and worktrees on any `worktree` event, so a task dispatched while open gains its link and console
  // button without a reload.
  useEventsSinceSeen(eventStream, (events, overrun) => {
    if (overrun || events.some(event => event.entity === 'worktree')) loadWorktrees()
  })

  return {
    task, setTask, formTask, activity, artifacts, artifactsError, setArtifactsError, artifactsEverShown,
    setArtifactsEverShown, loading, cachedRow, deleting, setDeleting, runMetricsId, runMetrics, duplicateSeed, duplicateWarning,
    setDuplicateWarning, notFound, error, formDirty, setFormDirty, worktrees, projects, projectsPending, trail, load,
    keepsHandedRead,
  }
}
