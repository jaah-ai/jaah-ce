import type { Artifact, PipelineTask } from '../types'

/* ── The task page's COLD-LOAD BUNDLE (`GET /api/tasks/{id}/page`) ───────────────────────────────────
 * A cold `/tasks/:id` used to issue ~12 task-scoped GETs, one per panel. The page now reads them in ONE request and
 * SEEDS each panel's first read from it: a panel's fetcher is `takeTaskPageSeed(id, part) ?? <its own GET>`.
 * Each part is EXACTLY the body of that panel's standalone GET; a part is ABSENT where that GET would refuse
 * (403/404), and the panel then reads its own GET as before, so error and empty semantics are unchanged.
 *
 * FRESHNESS — the seed only ever answers a panel's FIRST read after a cold load, never a re-read:
 *  - A part is one-shot. It stays readable only until the current macrotask ends after its first take, which
 *    covers React.StrictMode's double effect (`main.tsx`: mount → unmount → mount, run synchronously in one
 *    flush) without serving any later read — a live event, a poll, or a write's reload all run in a later task.
 *  - No clock ends it: a panel mounting late on a starved host still finds it (a wall-clock expiry made the task
 *    page's reads depend on load). EVENTS end it — `dropTaskPageSeed(id)` discards it at once: the page
 *    calls it the moment it hears an event naming the task, a quiet one its re-read match ignores included (a
 *    control message, a metered tick), or one that moves it (before any debounced re-read, its panels' included),
 *    and at the start of every read, so nothing read after an event answers from the seed. An own-task event heard
 *    while `GET …/page` is still in flight leaves nothing to drop, so the page then never stores that bundle (its
 *    panels read their own GETs).
 *  - `clearTaskPageSeeds()` discards every seed on an identity change (sign-in, sign-out, act-as), beside
 *    `queryClient.clear()`: no seed outlives the caller it was read for. That is why this module imports nothing
 *    at runtime: `lib/actAs.ts` reaches it, and `api.ts` reaches `lib/actAs.ts` (the page reads the bundle itself).
 * A panel that mounts BEFORE the seed is stored (none on the task page today: every panel mounts under the loaded
 * task, and the run-metrics read waits for it) simply finds nothing and reads its own GET. */

export type TaskPageBundle = {
  task: PipelineTask
  // The page's own row type (`pages/taskDetail/ActivityLog.tsx::ActivityLogEntry`).
  activity?: unknown[]
  artifacts?: Artifact[]
  run_metrics?: unknown
  rating?: unknown
  comments?: unknown
  body_versions?: unknown
  dependencies?: unknown
  compliance_manifest?: unknown
  control_messages?: unknown
  live_cast?: unknown
  /* The task's project and feature BY NAME, so the trail names them on a cold page before the roster
     (`GET /api/projects`) or the task tree has landed. Absent on an older API; `null` = the API could not name it. */
  trail?: TaskPageTrail
}

export type TaskPageTrail = { project_name: string | null; feature_name: string | null }

/** The panel parts the seed holds (the page itself reads `task`, `activity`, `artifacts` and `trail` straight off the bundle). */
export type TaskPagePart = Exclude<keyof TaskPageBundle, 'task' | 'activity' | 'artifacts' | 'trail'>

type Seed = { storedAt: number; parts: Partial<Record<TaskPagePart, unknown>>; taken: Set<TaskPagePart> }
const seeds = new Map<number, Seed>()

export function seedTaskPage(id: number | string, bundle: TaskPageBundle): void {
  const { task: _task, activity: _activity, artifacts: _artifacts, trail: _trail, ...parts } = bundle
  seeds.set(Number(id), { storedAt: Date.now(), parts, taken: new Set() })
}

export function takeTaskPageSeed<T>(id: number | string, part: TaskPagePart): T | undefined {
  const key = Number(id)
  const seed = seeds.get(key)
  if (seed === undefined) return undefined
  if (!(part in seed.parts)) return undefined
  const value = seed.parts[part] as T
  if (!seed.taken.has(part)) {
    seed.taken.add(part)
    // Readable for the rest of this task only (StrictMode's second effect), then gone.
    setTimeout(() => {
      if (seeds.get(key) === seed) delete seed.parts[part]
    }, 0)
  }
  return value
}

/** When the task's seed was stored (`Date.now()`), or undefined: a seeded query's `initialDataUpdatedAt`. */
export function taskPageSeedStoredAt(id: number | string): number | undefined {
  return seeds.get(Number(id))?.storedAt
}

export function dropTaskPageSeed(id: number | string): void {
  seeds.delete(Number(id))
}

export function clearTaskPageSeeds(): void {
  seeds.clear()
  landed.clear()
  landedParts.clear()
  boardRows.clear()
  identityEpoch += 1
}

/* ── The BOARD'S ROWS, for the task page's first paint ─────────────────────────────────────────────────
 * The board's flat feed keeps its rows in component state, not react-query, so a page opened from it could not see
 * them: the trail sat on a skeleton until the page's own read landed. The board hands each list read here; the page
 * paints its trail (project, feature, title) from the row while its own read is in flight. Display only: the page's
 * state is always its own read. Cleared with the seeds on an identity change; overwritten by every later list read.
 * A read started before that change and landing after it is dropped: the board passes the epoch it started under. */
const boardRows = new Map<number, PipelineTask>()
let identityEpoch = 0

/** The identity epoch a list read starts under, handed back to `rememberBoardRows` when it lands. */
export function boardRowsEpoch(): number {
  return identityEpoch
}

export function rememberBoardRows(rows: readonly PipelineTask[], startedAt = identityEpoch): void {
  if (startedAt !== identityEpoch) return
  for (const row of rows) {
    boardRows.set(row.id, row)
    if (row.children) rememberBoardRows(row.children, startedAt)
  }
}

export function boardRow(id: number): PipelineTask | undefined {
  return boardRows.get(id)
}

/* ── The page's LANDED READ, handed across a REMOUNT ─────────────────────────────────────────────────
 * Crossing the split width with a task open moves the page between the pane and `<main>` (components/ListDetail.tsx),
 * which remounts it: the new mount used to read `GET …/page` again. Instead a mounted page REGISTERS what it has
 * read, and the arriving mount — rendered before the leaving one unmounts — starts from it, with no request.
 *  - The leaving page unregisters as it unmounts, so the read never answers a later visit to the task.
 *  - It is asked at take time (`current`), and answers only while it is current: nothing in flight, no live
 *    re-read pending, no failure. Otherwise the new mount reads as ever.
 *  - Keyed on the org scope (`scope`): a scope change remounts the page ON PURPOSE, to read under the new scope.
 * The run metrics ride along in the read. The PANELS (comments, body versions, compliance manifest, control
 * messages) register their own last answer beside it (`registerLandedTaskPart`, while mounted), asked at
 * take time whether it is still current (no read in flight or heard pending), and the take seeds them too, so their
 * remounts read no GET either. */
export type LandedTaskRead = {
  scope: string
  /** `null` with `notFound`: the API did not find it (deleted, or hidden by the org filter). */
  task: PipelineTask | null
  notFound: boolean
  activity: unknown[]
  artifacts: Artifact[]
  artifactsError: boolean
  /** The run-metrics answer (`GET …/run-metrics`'s body), seeded for the new mount's read; `null` = read it. */
  runMetrics: unknown
}

const landed = new Map<number, () => LandedTaskRead | null>()
const landedParts = new Map<number, Map<TaskPagePart, { value: unknown; busy?: () => boolean }>>()

/** While the page is mounted: offer its read to a remount. Returns the unregister, for the unmount. */
export function registerLandedTaskRead(id: number | string, current: () => LandedTaskRead | null): () => void {
  const key = Number(id)
  landed.set(key, current)
  return () => {
    if (landed.get(key) === current) landed.delete(key)
  }
}

/** The read a mounted page holds for `id` under `scope`, run metrics and landed panels seeded; undefined = read as ever. */
export function takeLandedTaskRead(id: number | string, scope: string): LandedTaskRead | undefined {
  const read = landed.get(Number(id))?.()
  if (!read || read.scope !== scope) return undefined
  if (read.task !== null) {
    const parts: Partial<Record<TaskPagePart, unknown>> = {}
    for (const [part, entry] of landedParts.get(Number(id)) ?? []) if (!entry.busy?.()) parts[part] = entry.value
    if (read.runMetrics !== null) parts.run_metrics = read.runMetrics
    if (Object.keys(parts).length > 0) seedTaskPage(id, { task: read.task, ...parts })
  }
  return read
}

/** While a panel is mounted with an answer (`value`): offer it to its remount unless `busy()` says a re-read is in
 *  flight or pending at take time. Returns the unregister. */
export function registerLandedTaskPart(
  id: number | string, part: TaskPagePart, value: unknown, busy?: () => boolean,
): () => void {
  const key = Number(id)
  const parts = landedParts.get(key) ?? new Map<TaskPagePart, { value: unknown; busy?: () => boolean }>()
  landedParts.set(key, parts)
  // A fresh box per registration: two registrations of the same value must not unregister each other.
  const entry = { value, busy }
  parts.set(part, entry)
  return () => {
    if (parts.get(part) !== entry) return
    parts.delete(part)
    if (parts.size === 0 && landedParts.get(key) === parts) landedParts.delete(key)
  }
}
