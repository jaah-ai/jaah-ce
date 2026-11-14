// The single-artifact address: `/tasks/:id?tab=artifacts&artifact=<id>` opens the Artifacts tab with THAT
// artifact shown. `components/tasks/TaskArtifacts.tsx` reads and writes the param. Keep the literal and the
// filename stable: a link that names an artifact is only as durable as this address.

// The param carrying the artifact to show.
export const ARTIFACT_PARAM = 'artifact'
