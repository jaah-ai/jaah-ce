/* DBOS workflow status vocabulary and its console rendering, shared by `/workflows` and `ui/DetailFields`
 * so both color a status the same. The set is DBOS's, not ours: lookups are `Record<string, …>` with a
 * call-site fallback, so a status a newer DBOS invents renders as itself in a grey pill. */
import type { Tone } from '../ui/StatusChip'

/* Lifecycle order (filter dropdown): in-flight first, then finished. Server home:
 * `app/workflow_engine.py::STATUSES`; the API 422s anything else. */
export const DBOS_STATUSES = [
  'ENQUEUED',
  'DELAYED',
  'PENDING',
  'SUCCESS',
  'ERROR',
  'MAX_RECOVERY_ATTEMPTS_EXCEEDED',
  'CANCELLED',
] as const

/* Status → tone. `CANCELLED` is grey: a deliberate stop is intent, not a fault. `ENQUEUED` is in flight. */
export const DBOS_STATUS_TONE: Record<string, Tone> = {
  SUCCESS: 'success',
  ERROR: 'error',
  MAX_RECOVERY_ATTEMPTS_EXCEEDED: 'error',
  CANCELLED: 'neutral',
  PENDING: 'info',
  ENQUEUED: 'info',
  DELAYED: 'info',
}

/* Tooltip text, only where the status word hides a consequence: `MAX_RECOVERY_ATTEMPTS_EXCEEDED` never finishes. */
export const DBOS_STATUS_HELP: Record<string, string> = {
  MAX_RECOVERY_ATTEMPTS_EXCEEDED: 'The engine gave up. It will not be retried; its work is unfinished.',
}

/* Row mark for a FINISHED status: ✓ success, ✕ fault, ■ cancelled (intent, not health). In flight and
 * unknown marks are the caller's (`/workflows` adds "no run" and "engine silent"). */
export const DBOS_STATUS_GLYPH: Record<string, string> = {
  SUCCESS: '✓',
  ERROR: '✕',
  MAX_RECOVERY_ATTEMPTS_EXCEEDED: '✕',
  CANCELLED: '■',
}

/** Whether the status can still change on its own (no `completed_at` yet). */
export function isInFlight(status: string): boolean {
  return status === 'PENDING' || status === 'ENQUEUED' || status === 'DELAYED'
}
