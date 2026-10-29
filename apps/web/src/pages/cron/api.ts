// The Cron surface's API client (`apps/api/app/routes/crons.py`): the activity catalogue, the schedule preview and
// the ledger row's shape. Nothing is recomputed here.

import { get, post } from '../../api'

/* Server vocabularies are typed `string`, not unions: a row from an older/newer API must render as
 * itself, so readers use a lookup with a fallback. */

/** One firing, as the ledger (`routes/crons.py::FleetCronRunOut`) serves it. `outcome`/`skip_reason` are machine tokens
 * (`app/crons.py::RUN_OUTCOMES`, `SKIP_REASONS`); `detail` is the runtime's sentence; `task_id` links
 * the work it created. */
export type CronRun = {
  id: string
  fired_at: string
  started_at: string | null
  finished_at: string | null
  outcome: string
  /** Set only on `skipped` rows. */
  skip_reason: string | null
  /** Possibly `''`; never null. */
  detail: string
  /** The task this firing filed (Runner activity), else null. */
  task_id: number | null
}

/* The server's cap (`routes/crons.py`'s `RUNS_MAX_LIMIT`); past it is a 422. */
export const RUNS_MAX_LIMIT = 200

/** One `params` JSON Schema property — the scalar subset `app/cron_activities.py` accepts (it rejects
 *  any constraint `validate_params` does not enforce). */
export type ParamsProperty = {
  /** `string`, `integer`, `number`, `boolean`; typed `string` so a newer schema renders, not crashes. */
  type: string
  title?: string
  description?: string
  enum?: string[]
  default?: unknown
  minLength?: number
  maxLength?: number
  minimum?: number
  maximum?: number
  /** `array` properties only: the scalar spec each item satisfies, and the list bounds. */
  items?: ParamsProperty
  minItems?: number
  maxItems?: number
  uniqueItems?: boolean
}

/** `app/cron_activities.py::validate_params_schema`'s shape. `additionalProperties` is always false. */
export type ParamsSchema = {
  type: string
  properties: Record<string, ParamsProperty>
  required?: string[]
  additionalProperties?: boolean
}

/** `routes/crons.py::ActivityTypeOut`. `params` is the form: rendered from the schema, never a
 *  hand-written field list. */
export type ActivityType = {
  type: string
  label: string
  description: string
  /** Control-plane machinery; params render read-only. */
  is_system: boolean
  /** Whether a cron may be created for it (also enforced in `routes/crons.py::_creatable_descriptor`). Independent
   *  of `is_system`: a creatable system activity is how a deleted seeded cron is restored. */
  creatable: boolean
  params: ParamsSchema
  /** The cadence a new cron gets, in `CronCreate`'s field names. Optional only for the rolling deploy
   *  (an older API omits it). */
  default_schedule?: {
    schedule_kind: string
    cron_expression: string | null
    interval_seconds: number | null
  }
  /** The activity's min-gap floor; `null` = none (`cron_runtime._effective_min_gap`'s "not wakeable",
   *  never zero). Lets Run-now disable itself: no effective floor (row's `min_gap_seconds`, else this)
   *  is a permanent 409 (`routes/crons.py::FIRE_NO_MIN_GAP`). Optional for the rolling deploy:
   *  `undefined` is unknown, not `null` — a reader treats it like an unloaded catalogue. */
  min_gap_seconds?: number | null
}

/** `routes/crons.py::ActivityTypesOut`. */
export type ActivityTypesPayload = {
  activity_types: ActivityType[]
}

/** `routes/crons.py::SchedulePreviewOut` — an unsaved schedule's firings, computed server-side on the
 *  engine's own parser and zone (a browser parser could disagree). */
export type SchedulePreview = {
  schedule_human: string
  timezone: string
  /** Chronological, `count` long (default 5). Never empty — no further firings is a 422. */
  next_fire_at: string[]
}

/** The schedule fields a cron row stores, so a previewed schedule saves as previewed. */
export type SchedulePreviewRequest = {
  schedule_kind: string
  cron_expression?: string
  interval_seconds?: number
  count?: number
}

/** The activity catalogue. */
export const listActivityTypes = () => get<ActivityTypesPayload>('/api/crons/activity-types')

export const previewSchedule = (body: SchedulePreviewRequest) =>
  post<SchedulePreview>('/api/crons/preview', body)
