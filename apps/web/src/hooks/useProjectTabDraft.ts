import { useState } from 'react'
import { useQueryClient } from '@tanstack/react-query'
import { put } from '../api'
import type { Project } from '../types'
import { PROJECTS_KEY } from './useRosters'
import { sameJson, useSeededDraft } from './useServerDraft'
import { type Landed, useSubmit } from './useSubmit'

/* A project tab's own draft and Save outside the project form (Benchmark, Production QA): one partial
 * `PUT /api/projects/{id}` of what changed (`routes/projects.py::ProjectUpdate`). The draft is `draftOf` the page's
 * row, seeded during render and following the server while untouched (`useSeededDraft`). The latest write's answer
 * stands in for the row until the page's row is refetched (the save invalidates it), so a change made elsewhere
 * still reaches the tab. */
export function useProjectTabDraft<D extends object>(
  project: Project,
  draftOf: (row: Project) => D,
  same: (a: D, b: D) => boolean = sameJson,
): {
  row: Project
  draft: D
  seed: D
  dirty: boolean
  busy: boolean
  formError: string
  patch: (next: Partial<D>) => void
  discard: () => void
  save: (fallback: string, body: Record<string, unknown>) => Promise<Landed<void>>
} {
  const queryClient = useQueryClient()
  const [answer, setAnswer] = useState<Project | null>(null)
  const [projectSeen, setProjectSeen] = useState(project)
  if (project !== projectSeen) {
    setProjectSeen(project)
    setAnswer(null)
  }
  const row = answer && answer.id === project.id ? answer : project
  const { draft, setDraft, seed, dirty, reseed } = useSeededDraft(draftOf(row), same)
  const { busy, formError, run } = useSubmit()

  const save = (fallback: string, body: Record<string, unknown>) => run(fallback, async () => {
    const next = await put<Project>(`/api/projects/${project.id}`, body)
    setAnswer(next)
    reseed(draftOf(next))
    void queryClient.invalidateQueries({ queryKey: PROJECTS_KEY })
    void queryClient.invalidateQueries({ queryKey: ['project'] })
  })

  return {
    row, draft, seed, dirty, busy, formError, save,
    patch: next => setDraft(d => ({ ...d, ...next })),
    discard: () => setDraft(seed),
  }
}
