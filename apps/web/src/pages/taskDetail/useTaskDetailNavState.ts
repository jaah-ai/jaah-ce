// The Task page's navigation inputs: what `location.state` and `?persona=&project_id=` hand a create.
import { useState } from 'react'
import { useLocation, useSearchParams } from 'react-router-dom'
import { readTaskPrefillParams } from '../../components/tasks/taskPrefill'
import { duplicateSourceId, type NewTaskNavState } from '../../lib/taskDuplicate'
import { isUuidShaped } from '../../lib/uuidShape'

export type TaskDetailNavState = ReturnType<typeof useTaskDetailNavState>

/** A project id as navigation state may carry one: a positive integer. `location.state` is untrusted. */
function isProjectIdShaped(value: unknown): value is number {
  return typeof value === 'number' && Number.isInteger(value) && value > 0
}

export function useTaskDetailNavState(isCreate: boolean, isChat: boolean) {
  // The inline create row's Description, via navigation STATE (never a URL — prose gets copied and
  // logged). `location.state` is untrusted: non-strings mean no prefill.
  const navState = useLocation().state as NewTaskNavState | null
  const rawBody: unknown = navState?.body
  const prefilledBody = typeof rawBody === 'string' ? rawBody : ''
  /* …and the board's PROJECT (`navState.boardProjectId`; see `Tasks.tsx`'s `leadingRow`). `null` is "no
     scope", so a direct visit behaves as before. Narrowed by `isProjectIdShaped`: anything not shaped like
     a project id degrades to no scope. Staleness is not judged here; the ladder below re-checks the fetched
     projects and grants. */
  /* …and which row this create copies (`duplicateSourceId` owns the shape test; `null` = blank create).
     Its scope rides `boardProjectId` above. Not a query param: a URL can be handed to someone who may not
     read the source row. Note navigation state persists on the history entry (Back re-reads the row;
     benign, since reads are authorized per row). */
  const duplicateOf = duplicateSourceId(navState)
  const rawBoardProject: unknown = navState?.boardProjectId
  const boardProjectId: number | null = isProjectIdShaped(rawBoardProject) ? rawBoardProject : null
  /* …and the board's FEATURE scope (`navState.boardFeatureId`, `Tasks.tsx`'s ⤢): the create files there when the
     tree confirms it is active and in the resolved project (the seed effect below). Shape-checked
     (`isUuidShaped`); anything else is "No feature". */
  const rawFeatureScope: unknown = navState?.boardFeatureId
  /* …and the composer's own "Change" pick (`navState.filingTarget`), which outranks both: ⤢ must file where the
     row said it would. Shape-checked; the seeds below re-check it against the projects, grants and tree. */
  const rawFilingTarget = navState?.filingTarget as { projectId?: unknown; featureId?: unknown } | null | undefined
  const pickedProject: number | null =
    isProjectIdShaped(rawFilingTarget?.projectId) ? rawFilingTarget.projectId : null
  const pickedFeature: string | null =
    pickedProject !== null && isUuidShaped(rawFilingTarget?.featureId) ? rawFilingTarget.featureId : null
  const boardFeatureScope: string | null = pickedProject !== null
    ? pickedFeature
    : isUuidShaped(rawFeatureScope) ? rawFeatureScope : null
  const [searchParams] = useSearchParams()
  /* ── `?persona=&project_id=`: the create-only SEED (`components/tasks/taskPrefill.ts`) ─────────────────
     Captured ONCE at mount, never re-read (a seed must not re-fire on later navigation). Not for a chat,
     whose persona is its whole point. Validated below once the roster lands; the form fences it to create. */
  const [prefillParams] = useState(() => (isCreate && !isChat ? readTaskPrefillParams(searchParams) : null))
  return { prefilledBody, duplicateOf, boardProjectId, pickedProject, boardFeatureScope, prefillParams }
}
