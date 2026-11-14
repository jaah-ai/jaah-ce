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

/** A lane run's own numbers (`TaskOut.lane_run`, `app/merge_lane_state.py::lane_run_view`): gates run, wall time
 *  in seconds (a live run's elapsed so far), and whether it is past the stall bound. */
export type LaneRunStats = { gates: number; wall_s: number; slow: boolean }

/** A run's wall time, short and whole: `45s`, `72m` (minutes up to two hours, so a long gate reads exactly), then
 *  `2h 05m`. */
export function laneWallLabel(seconds: number): string {
  const s = Math.max(0, Math.floor(seconds))
  if (s < 60) return `${s}s`
  const minutes = Math.floor(s / 60)
  if (minutes < 120) return `${minutes}m`
  return `${Math.floor(minutes / 60)}h ${String(minutes % 60).padStart(2, '0')}m`
}

/** `9 gates · 72m`: the run's facts as one short line. */
export function laneRunLabel(run: Pick<LaneRunStats, 'gates' | 'wall_s'>): string {
  return `${run.gates} ${run.gates === 1 ? 'gate' : 'gates'} · ${laneWallLabel(run.wall_s)}`
}
