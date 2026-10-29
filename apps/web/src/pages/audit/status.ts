import type { Tone } from '../../ui/StatusChip'

/* HTTP status -> chip tone, shared by the table's Action cell and the drawer so they never disagree.
 * 3xx is `info` (routing, not success); 4xx is `warning`, not `error`: a 401/403 is often the row an
 * operator is looking for, and must stay distinct from a 500. */
export function statusTone(status: number): Tone {
  if (status >= 500) return 'error'
  if (status >= 400) return 'warning'
  if (status >= 300) return 'info'
  if (status >= 200) return 'success'
  return 'neutral'
}

/* HTTP status as a glyph on the disc, over the bands above, so outcomes are not tint-only:
 * ✓ 2xx, » 3xx, ⊘ 4xx ("the system said no" — often the row sought), ✕ 5xx. `null` is its own
 * state `·`: status is NULL on every non-middleware row (reads, `task:*`, hand-written records). */
export function statusGlyph(status: number | null): string {
  if (status === null) return '·'
  if (status >= 500) return '✕'
  if (status >= 400) return '⊘'
  if (status >= 300) return '»'
  if (status >= 200) return '✓'
  return '·'
}
