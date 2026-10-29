// The GitHub-Actions-runner surface's ONE API client (precedent and rule: `pages/nodes/nodeGroups/api.ts`).
// Record shapes live in `src/types.ts`; this module owns the WRITE body. Every path is keyed on the node
// NAME (what `/api/nodes/{node_id}` takes, `routes/nodes/`), `encodeURIComponent`-d once here — a
// hostname may carry characters a path segment cannot.

import { del, get, put } from '../../../api'
import type { GhaRunnerPool, GhaRunnerPoolResponse } from '../../../types'

/* A COMPLETE UPSERT (no PATCH): `PUT /api/nodes/{node}/gha-runners` is `extra="forbid"` and reads all four
   fields, so a count change still sends the project and credential read off the row — the pool is a
   DECLARATION, and a partial one could re-register runners against a repo someone else re-pointed.
   No `undefined` anywhere: a dropped key is a 422 here. `state` excludes `absent` (DELETE sets it), so
   Apply can never remove the pool without its confirm. */
export type GhaRunnerPoolWrite = {
  /** `projects.id`, an INTEGER (`credential_id` is a uuid; `types.ts` says why). The project's `repo_url` is
   *  the registration scope; the server 422s one without it, and the picker disables such projects. */
  project_id: number
  /** `credentials.id` of a `github` credential; other types 422. */
  credential_id: string
  /** 1…32 (the server's CHECK); the form refuses out-of-range first. */
  count: number
  state: 'running' | 'paused'
}


/** `node` is the node uuid: the route takes no name. */
const poolPath = (node: string) => `/api/nodes/${encodeURIComponent(node)}/gha-runners`

/** `{ pool: null }` is a normal 200 (no runners set up); a 404 means the NODE is invisible.
 *  `orphaned_slots`: slots above the count GitHub still lists (`types.ts`: `[]` is not a clean bill). */
export const getGhaRunnerPool = (node: string) => get<GhaRunnerPoolResponse>(poolPath(node))

/** Upsert; answers `phase: 'pending'` — the instruction, not the host's state. */
export const putGhaRunnerPool = (node: string, body: GhaRunnerPoolWrite) =>
  put<GhaRunnerPool>(poolPath(node), body)

/** ASKS FOR REMOVAL: answers the row with `state: 'absent'`, `phase: 'pending'`; the node unregisters
 *  runners as they free and the report route deletes the row, so the pool keeps answering for a while and
 *  then goes `null` between polls.
 *  `force` (same route and permission, hence an argument): `?force=true` drops the row outright — the only
 *  way out of a removal that cannot complete — leaving anything still registered on the repo registered,
 *  with no credential left here to deregister it. The caller confirms. Defaulted off; the query is absent
 *  unless asked for. */
export const deleteGhaRunnerPool = (node: string, { force = false }: { force?: boolean } = {}) =>
  del<GhaRunnerPool>(force ? `${poolPath(node)}?force=true` : poolPath(node))
