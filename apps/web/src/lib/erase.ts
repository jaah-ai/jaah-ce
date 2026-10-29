// Readers for the pool panel's structured refusal bodies: `POST /api/projects/{id}/erase` (names every
// slot it could not erase) and `POST /api/worktrees/{project}/{number}/release` (the live-session guard).
// Read structurally: a refusal arrives inside FastAPI's `detail` on one path and top-level on another,
// and the failure list may be a string or rows. A drifted body degrades to the plain error message,
// never an empty panel.

// Reads one field off anything error-shaped, not only an `ApiError`.
function errField(err: unknown, key: string): unknown {
  return err && typeof err === 'object' && key in err ? (err as Record<string, unknown>)[key] : null
}

/* A message carried by a refusal object (`apiDetail` reads only a string detail). `message` is tried
before `reason` because `reason` may be a machine discriminator (`"live_session"`), not a sentence. */
export function refusalMessage(err: unknown): string {
  const detail = errField(err, 'detail')
  if (typeof detail === 'string') return detail
  if (detail && typeof detail === 'object') {
    for (const key of ['message', 'reason', 'error']) {
      const value = (detail as Record<string, unknown>)[key]
      if (typeof value === 'string' && value) return value
    }
  }
  return ''
}

/* The CP's live-session refusal of a release, or null — the only release failure a force retry can
resolve. Read off `reason`, never the status code: `release_slot` also 409s for the main clone, which
no force can help. `reason` is matched exactly; an unknown value gets no retry (fail closed). */
export function releaseLiveSessionRefusal(err: unknown): { session_status: string } | null {
  for (const candidate of [errField(err, 'detail'), errField(err, 'body')]) {
    if (!candidate || typeof candidate !== 'object') continue
    const rec = candidate as Record<string, unknown>
    if (rec['reason'] !== 'live_session') continue
    // The node's verbatim `claude agents --json` status; absent degrades to `''`.
    const status = rec['session_status']
    return { session_status: typeof status === 'string' ? status : '' }
  }
  return null
}

function describeRow(row: unknown): string {
  if (typeof row === 'string') return row
  if (!row || typeof row !== 'object') return ''
  const rec = row as Record<string, unknown>
  const node = rec['node_id'] ?? rec['node']
  const number = rec['number']
  const reason = rec['reason'] ?? rec['detail'] ?? rec['error'] ?? rec['message']
  const where = [typeof node === 'string' ? node : '', number != null ? `#${number}` : '']
    .filter(Boolean).join(' ')
  const said = typeof reason === 'string' ? reason : ''
  return [where, said].filter(Boolean).join(': ') || JSON.stringify(row)
}

function joinRows(rows: unknown[]): string {
  return rows.map(describeRow).filter(Boolean).join('; ')
}

// The cascade's partial failure as one line, or `null` to fall back. Never drops the list: the erased
// slots are gone irreversibly, so which one blocked it is what the operator needs.
export function cascadeFailureText(err: unknown): string | null {
  const detail = errField(err, 'detail')
  if (typeof detail === 'string') return detail
  if (Array.isArray(detail)) return joinRows(detail) || null
  if (detail && typeof detail === 'object') {
    const rec = detail as Record<string, unknown>
    const message = typeof rec['message'] === 'string' ? rec['message'] : ''
    for (const key of ['failed', 'slots', 'errors']) {
      const rows = rec[key]
      if (Array.isArray(rows)) return [message, joinRows(rows)].filter(Boolean).join(' — ') || null
    }
    return message || null
  }
  return null
}
