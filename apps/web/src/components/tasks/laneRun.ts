/* A MERGE-LANE RUN AS THE TASK UI SHOWS IT: the task the lane files for each batch is a script run (read-only, its
 * gate log linked from its runner rows). Here, not in `components/projects/mergeLane.ts`, because the task pages and
 * the board ship in the community edition, whose tree leaves the lane's own modules out (`merge_lane`,
 * packages/editions/features.toml); there no task carries the persona. `mergeLane.ts` re-exports these. */

/** The persona every lane run's task carries (`app/merge_lane.py::LANE_PERSONA`). */
export const LANE_PERSONA = 'merge_lane'

/** A lane task's artifact, opened on its Artifacts tab (`lib/taskArtifactLink.ts`); the tab alone without one. */
export function laneArtifactHref(taskId: number, artifactId: number | null): string {
  return `/tasks/${taskId}?tab=artifacts${artifactId === null ? '' : `&artifact=${artifactId}`}`
}

/** A gate log's filename as a lane run uploads it:
 *  `[focus-]gate-<n>-<tree>-<green|red>.log`. */
const GATE_LOG_NAME = /^(focus-)?gate-\d+-[0-9a-f]+-(green|red)\.log$/

/** The newest gate log among a lane task's artifacts, or null when its run uploaded none. */
export function latestGateLog<A extends { id: number; kind: string; filename: string; created_at: string }>(
  artifacts: readonly A[],
): A | null {
  let latest: A | null = null
  for (const a of artifacts) {
    if (a.kind !== 'file' || !GATE_LOG_NAME.test(a.filename)) continue
    if (latest === null || a.created_at > latest.created_at || (a.created_at === latest.created_at && a.id > latest.id)) {
      latest = a
    }
  }
  return latest
}
