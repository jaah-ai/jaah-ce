// A NODE'S `failure_reason` IN WORDS — the vnodehelper reports `code: detail`, which
// reads as a log line. The codes a person meets when adding a node get a short label; the raw text stays as the
// detail under it, so nothing the Nodes row shows is lost. The server reads the same codes for the
// order's sentence (`node_failed_sentence`); an unknown one has no label. React-free.

/** The label for a node's raw `failure_reason`, or null when the reason is none the person can be told about. */
export function nodeFailureLabel(reason: string | null | undefined): string | null {
  const text = reason ?? ''
  // `hosts.ENGINE_FAILURE`.
  if (/^docker_failed\b/.test(text)) return 'No container engine'
  // `node_orders._IDS_HELD`.
  if (/^(slot_collision|isolation_violation)\b/.test(text)) return 'Ids held by another account'
  // This helper's words, or an older helper's bare `command_timeout: mount …`.
  const timedOut = /timed out/.test(text)
  if (text.includes('NodeLink mount') || text.startsWith('command_timeout: mount ')) {
    return timedOut ? 'NodeLink mount timed out' : 'NodeLink didn’t mount'
  }
  return null
}
