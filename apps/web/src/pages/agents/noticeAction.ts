import type { Notice } from './agentFields'
import { refusalSentences } from './api'

/* The agent pages' ONE row action: clear the page notice, run the call, then say `done` on success or the
 * refusal's sentences (`refusalSentences`, never an HTTP code) under `fallback`. Each page wraps it in its
 * `withBusy` and refreshes after. Before this, Agents, Catalog and Registry each re-spelled the nine lines. */
export async function runWithNotice(
  setNotice: (notice: Notice | null) => void,
  run: () => Promise<unknown>,
  done: string,
  fallback: string,
): Promise<void> {
  setNotice(null)
  try {
    await run()
    setNotice({ severity: 'success', lines: [done] })
  } catch (err) {
    setNotice({ severity: 'error', lines: refusalSentences(err, fallback) })
  }
}
