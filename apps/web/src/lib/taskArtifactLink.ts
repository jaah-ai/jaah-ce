// The single-artifact address: `/tasks/:id?tab=artifacts&artifact=<id>` opens the Artifacts tab with THAT
// artifact shown. `components/tasks/TaskArtifacts.tsx` reads and writes the param. Keep the literal and the
// filename stable: a link that names an artifact is only as durable as this address.
import { parseTaskDeepLinkParam } from './taskDeepLink'

// The param carrying the artifact to show.
export const ARTIFACT_PARAM = 'artifact'

// A positive integer or null — validated, never coerced, exactly as a task id is read.
export function parseArtifactParam(raw: string | null): number | null {
  return parseTaskDeepLinkParam(raw)
}
