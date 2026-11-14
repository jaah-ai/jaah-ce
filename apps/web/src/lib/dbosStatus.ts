/* DBOS workflow status vocabulary and its console rendering, shared by `/workflows` and `ui/DetailFields`
 * so both color a status the same. The set is DBOS's, not ours: `dbosStatus` falls back, so a status a newer
 * DBOS invents renders as itself in a grey pill. */
import { presentationOf, type Presentation } from '../ui/statusTone'

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

/* Status → its word (the status itself), tone and row mark: ✓ success, ✕ fault, ■ cancelled (grey: a deliberate
 * stop is intent, not a fault), » in flight. */
export const DBOS_STATUS: Record<string, Presentation> = {
  SUCCESS: { label: 'SUCCESS', tone: 'success', glyph: '✓' },
  ERROR: { label: 'ERROR', tone: 'error', glyph: '✕' },
  MAX_RECOVERY_ATTEMPTS_EXCEEDED: { label: 'MAX_RECOVERY_ATTEMPTS_EXCEEDED', tone: 'error', glyph: '✕' },
  CANCELLED: { label: 'CANCELLED', tone: 'neutral', glyph: '■' },
  PENDING: { label: 'PENDING', tone: 'info', glyph: '»' },
  ENQUEUED: { label: 'ENQUEUED', tone: 'info', glyph: '»' },
  DELAYED: { label: 'DELAYED', tone: 'info', glyph: '»' },
}

/* Tooltip text, only where the status word hides a consequence: `MAX_RECOVERY_ATTEMPTS_EXCEEDED` never finishes. */
export const DBOS_STATUS_HELP: Record<string, string> = {
  MAX_RECOVERY_ATTEMPTS_EXCEEDED: 'The engine gave up. It will not be retried; its work is unfinished.',
}

/** A status as the console shows it; one a newer DBOS invents renders as itself in grey with `?`. */
export const dbosStatus = (status: string): Presentation =>
  presentationOf(DBOS_STATUS, status, { label: status, tone: 'neutral', glyph: '?' })
