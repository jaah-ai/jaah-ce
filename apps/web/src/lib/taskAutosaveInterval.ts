/* How often the full task editor auto-saves, in seconds (the knob's unit):
 * `operator_settings.task_autosave_seconds`, delivered on the identity payload as
 * `admin.task_autosave_seconds` (`routes/auth.py::_admin_out_bound`) because `GET /api/settings` needs
 * `system:view`. The fallback duplicates the server default for the windows `officialZone.ts` names
 * (old snapshot, first paint, missing field); the server value wins as soon as it arrives. */
import { useAuth } from '../auth'

/** Cadence until the server answers — deliberately duplicates the catalogue default. */
export const DEFAULT_TASK_AUTOSAVE_SECONDS = 15

/* The knob's bounds (argued in `operator_settings/`). An out-of-range value came from a payload this
 * build does not understand; clamping keeps the intent and rejects 0 or 86400. */
const MIN_TASK_AUTOSAVE_SECONDS = 5
const MAX_TASK_AUTOSAVE_SECONDS = 600

/** The payload's value, clamped, or the default. A finiteness check, not a coercion: `Number('')` and
 *  `Number(null)` are 0, and `NaN`/`Infinity` are not delays. */
export function resolveTaskAutosaveSeconds(raw: unknown): number {
  if (!Number.isFinite(raw as number)) return DEFAULT_TASK_AUTOSAVE_SECONDS
  const seconds = raw as number
  return Math.min(MAX_TASK_AUTOSAVE_SECONDS, Math.max(MIN_TASK_AUTOSAVE_SECONDS, seconds))
}

/** How many SECONDS the task editor waits between auto-saves. The caller converts to milliseconds. */
export function useTaskAutosaveSeconds(): number {
  const { admin } = useAuth()
  return resolveTaskAutosaveSeconds(admin?.task_autosave_seconds)
}
