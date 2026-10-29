// Where the Task page files its task: the breadcrumb's staged title/project/feature, their pickers, the create seeds.
import { useCallback, useEffect, useMemo, useRef, useState, type Dispatch, type SetStateAction } from 'react'
import { useQuery } from '@tanstack/react-query'
import { apiDetail, put } from '../../api'
import { useCanOnProject } from '../../auth'
import type { ProjectMenuRow } from '../../components/tasks/ProjectMenu'
import { useFilingFeatures } from '../../components/tasks/featureFiling'
import { personaLabel, personaOptions } from '../../components/tasks/personaRegistry'
import { resolveCreateProject } from '../../components/tasks/shared'
import { resolveTaskPrefill, templatePrefill } from '../../components/tasks/taskPrefill'
import { getTemplate, templateAssetResolver, templateKey } from '../templates/api'
import { usePersonaRegistryStatus } from '../../components/tasks/usePersonaRegistry'
import { useFocusedProjectId } from '../../hooks/useFocusedProjectId'
import { useTaskTree } from '../../hooks/useTaskTree'
import type { OrgRef, PipelineTask, Project } from '../../types'
import { useOwnOrgRows } from '../../ui/OrgIndicator'
import { keepLoaded } from './useTaskDetailActions'
import type { TaskDetailNavState } from './useTaskDetailNavState'

export type TaskDetailFiling = ReturnType<typeof useTaskDetailFiling>

/* WHETHER A SAVED ROW'S PROJECT CRUMB IS A PICKER FOR THIS VIEWER: one predicate for the loaded page
   (`projectCrumbEditable` below) and the loading shell's held caret (`TaskDetailPage.tsx`'s `pickerPending`), so the
   two cannot disagree. A script run (the merge lane) has no form to save through; a child's move is refused. */
export function savedProjectCrumbEditable(canEdit: boolean, script: boolean, parentTaskId: number | null | undefined): boolean {
  return canEdit && !script && parentTaskId == null
}

export function useTaskDetailFiling({
  id, isCreate, task, formTask, setTask, formDirty, cachedRow, projects, loading, keepsHandedRead, otherOrg, nav, canEditThisTask,
  script,
}: {
  id: string | undefined
  isCreate: boolean
  task: PipelineTask | null
  /** The row the form is seeded from (`useTaskDetailLoad`): the drafts re-seed off it, so a re-read under unsaved
   *  edits keeps them. */
  formTask: PipelineTask | null
  /** The page's row (`useTaskDetailLoad`), replaced by a crumb move's response. */
  setTask: Dispatch<SetStateAction<PipelineTask | null>>
  /** Unsaved form edits: a crumb pick then STAGES (Save carries it) instead of moving at once. */
  formDirty: boolean
  /** The board's row painting the loading trail (`useTaskDetailLoad`), whose feature crumb needs the tree. */
  cachedRow?: PipelineTask

  projects: Project[]
  loading: boolean
  keepsHandedRead: () => boolean
  otherOrg: OrgRef | null
  nav: TaskDetailNavState
  canEditThisTask: boolean
  script: boolean
}) {
  const { prefillParams, pickedProject, boardProjectId, boardFeatureScope } = nav
  const canOnProject = useCanOnProject()
  /* ── The breadcrumb's two editable values, held HERE and handed down to the form ──────────────────
     The trail is outside the tab panels and survives the form's remount on
     `${task.id}:${task.updated_at}`, so the page owns the draft and `TaskEditForm` submits it. `null` =
     not seeded / nothing typed, distinct from `''` (a blanked title must reach the PUT). Re-seeded off
     the form's key so a second writer's SSE update replaces an untouched draft. */
  // The task the page shows now, for a move's late refusal (`moveNow`).
  const currentIdRef = useRef(id)
  currentIdRef.current = id
  const [draftTitle, setDraftTitle] = useState<string | null>(null)
  const [draftProjectId, setDraftProjectId] = useState<number | null>(null)
  /* …and the third: the staged FEATURE (`null` = "No feature", `undefined` = not seeded). Kept in agreement
     with `draftProjectId` by `stageProject`/`stageFeature` below, so a mismatched pair cannot be staged
     from the UI (AC-8.2). */
  const [draftFeatureId, setDraftFeatureId] = useState<string | null | undefined>(undefined)
  // A crumb move's PUT in flight (the crumbs lock), and its refusal, shown under the trail.
  const [moving, setMoving] = useState(false)
  const [moveError, setMoveError] = useState('')

  // Drafts belong to one task: `/tasks/A → /tasks/B` keeps this instance, and the seeding below picks up B's.
  useEffect(() => {
    setMoveError('')
    if (keepsHandedRead()) return
    setDraftTitle(null)
    setDraftProjectId(null)
    setDraftFeatureId(undefined)
  }, [id])

  /* Seed (and RE-seed) the breadcrumb's drafts off the same `id:updated_at` identity `TaskEditForm` is
     keyed on, so a second writer replaces an untouched draft in the same beat the form remounts.
     Unconditional: the identity changes only when the stored row did. Seeded DURING RENDER, not in an
     effect, so the trail never commits a frame with empty crumbs. `seededIdentity` is state, not a ref
     (StrictMode double render). */
  const taskIdentity = formTask === null ? null : `${formTask.id}:${formTask.updated_at}`
  const [seededIdentity, setSeededIdentity] = useState<string | null>(null)
  if (formTask !== null && taskIdentity !== seededIdentity) {
    setSeededIdentity(taskIdentity)
    setDraftTitle(formTask.title ?? '')
    setDraftProjectId(formTask.project_id)
    setDraftFeatureId(formTask.feature_id ?? null)
  }

  /* CREATE: the breadcrumb must NAME the target project, so the page resolves the default itself via
     `resolveCreateProject` (`components/tasks/shared`) once the projects land, and feeds it back as
     `projectIdDraft` so the trail and the POST agree. Only while nothing is picked (`=== null`).
     Arguments: the board's project from navigation state (`boardProjectId`; must not be hard-coded `null`,
     or ＋/Expand and the adjacent ▶ Start would file the same text into different projects). A direct visit
     passes `null` and lands on the first creatable project. No title is seeded (`titleEditable` excludes
     create). */
  /* The create seed's grant predicate — the API's `tasks:create` question. The project list alone is not
     enough (`components/tasks/shared.tsx::resolveCreateProject`): it includes projects the caller may view
     but not create in. */
  const canCreateInProject = useCallback(
    (id: number) => canOnProject('tasks:create', id),
    [canOnProject],
  )
  const focusedProjectId = useFocusedProjectId()
  /* The same grant question for the crumb picker the operator drives by hand, so Save is never refused.
     Two acts, two keys: create asks `tasks:create`; a move is `PUT project_id`, decided on the
     destination's `tasks:edit` (`hooks/useTaskAccess.ts`). */
  const crumbPickRefusal = useCallback(
    (project: ProjectMenuRow) =>
      canOnProject(isCreate ? 'tasks:create' : 'tasks:edit', project.id)
        ? null
        : isCreate ? 'no create access' : 'no move access',
    [canOnProject, isCreate],
  )
  /* A picker offers only rows a write may target: the operator's own org's projects, or on another org's row that
     org's, where the move goes as it (`useTaskDetailAccess`'s ANOTHER ORG'S ROW note). */
  const ownOrgRows = useOwnOrgRows()
  const pickerProjects = useMemo(
    () => (otherOrg ? projects.filter(p => p.org === undefined || p.org?.id === otherOrg.id) : ownOrgRows(projects)),
    [otherOrg, projects, ownOrgRows],
  )
  /* The seed, validated against the fetched projects and the persona vocabulary. The shell fetches the
     vocabulary too; asking here re-renders this page when it lands (a cold deep link may fetch it twice). */
  const personaStatus = usePersonaRegistryStatus()
  /* `?template=<id>` (Customize): the seed IS the template, fetched here; one that does not resolve (deleted,
     unpublished, another org's) drops to the ordinary blank form, like any other bad seed. */
  const templateId = prefillParams?.templateId ?? null
  const templateRead = useQuery({
    queryKey: templateKey(templateId ?? ''),
    queryFn: () => getTemplate(templateId as string),
    enabled: templateId !== null,
    retry: false,
  })
  const prefill = useMemo(() => {
    if (!prefillParams || personaStatus === 'loading') return null
    if (templateId !== null) {
      const template = templateRead.data
      return template ? templatePrefill(template, personaLabel, templateAssetResolver(template.id, template.assets)) : null
    }
    // A failed vocabulary judges every persona invalid — dropped, like any other bad value.
    const resolved = resolveTaskPrefill(prefillParams, pickerProjects, personaStatus === 'loaded' ? personaOptions() : [])
    /* A project this viewer cannot create in drops the WHOLE seed: the ladder would file it elsewhere, and an
       onboarding task titled for one repo would prepare another. */
    if (resolved?.projectId !== undefined
      && resolveCreateProject(pickerProjects, resolved.projectId, canCreateInProject).id !== resolved.projectId) return null
    return resolved
  }, [prefillParams, personaStatus, pickerProjects, canCreateInProject, templateId, templateRead.data])
  /* A seed holds the form until the vocabulary answers (or gives up): the form seeds in `useState`
     initialisers, so a late answer would be ignored. */
  const prefillPending = prefillParams !== null && (personaStatus === 'loading' || templateRead.isLoading)
  useEffect(() => {
    if (!isCreate || pickerProjects.length === 0) return
    // A seed waits for the projects and the vocabulary, or the ladder would win first.
    if (prefillParams !== null && (loading || prefillPending)) return
    setDraftProjectId(prev => {
      if (prev !== null) return prev
      // A template seed leaves the project for the author to pick: a template belongs to no project.
      if (prefill?.template !== undefined) return null
      // A seeded project names the target (`prefill` already dropped one this viewer cannot create in).
      if (prefill?.projectId !== undefined) return prefill.projectId
      // The composer's own pick, when this viewer may still create there.
      if (pickedProject !== null
        && resolveCreateProject(pickerProjects, pickedProject, canCreateInProject).id === pickedProject) {
        return pickedProject
      }
      // The grant map, not the project list (see `canCreateInProject`).
      return resolveCreateProject(pickerProjects, boardProjectId, canCreateInProject, focusedProjectId).id
    })
  }, [isCreate, pickerProjects, boardProjectId, canCreateInProject, prefill, prefillParams, loading, prefillPending,
    pickedProject, focusedProjectId])

  /* ── THE TRAIL IS THE TITLE NOW ─────────────────────────────────────────────────────────────────────
     The trail `{Project} › [{Feature}] › {id}: {title}` carries the page `<h1>` (`currentComponent`): a page
     identified only by a `<nav>` has no heading landmark. No `#` on the id. */
  /* A script run has no form to save the crumbs' edits through (`ScriptTaskBody`), so its crumbs are read-only.
     A title edit stages for Save. */
  const titleEditable = canEditThisTask && !isCreate && !script
  const onTitleChange = (next: string) => setDraftTitle(next)
  /* The project crumb is not live on a CHILD: a `project_id` PUT moves the task AND its subtree, so
     a child's move would be refused with a 400. Mirrors the form's `showsPersonaPicker = !isChildEdit`.
     A child's crumb still names its project. */
  const projectCrumbEditable = (isCreate ? task?.parent_task_id == null
    : savedProjectCrumbEditable(canEditThisTask, script, task?.parent_task_id)) && !moving

  /* ── THE PATH: Project › [Feature] (AC-1.1, AC-8.2) ─────────────────────────────────────────────────────
     The tree names features (shipped ones too); the feature picker offers the STAGED project's active features
     the caller may file into. The feature crumb is staged exactly like the project crumb (saved with the
     form), and has the same child rule. */
  // Only once there is something to file: a not-found or erroring page fetches nothing it cannot use.
  const { tree: taskTree } = useTaskTree(isCreate || task !== null || cachedRow !== undefined)
  const stagedProjectId: number | null = draftProjectId ?? task?.project_id ?? null
  /* A feature of ANOTHER project cannot ride the same PUT as the move into it: the API files before it moves and
     refuses a cross-project feature. So once the staged project leaves the task's project, the feature crumb
     offers "No feature" alone (file after saving the move). A create has no project yet — until auto-save files
     it, its project is whatever is staged. */
  const filedProjectId = isCreate ? undefined : task?.project_id
  const crossProjectMove = filedProjectId !== undefined && stagedProjectId !== filedProjectId
  const { features: projectFeatures } = useFilingFeatures(
    stagedProjectId ?? undefined, projectCrumbEditable && !crossProjectMove)
  const filingFeatures = crossProjectMove ? [] : projectFeatures
  const stagedFeatureId = draftFeatureId ?? null
  /* THE AGREEMENT RULE, in the only two places a pick is staged: a feature belongs to one project, so a project
     pick drops the staged feature, and a feature pick brings its own project. */
  const stageProject = (projectId: number) => {
    if (projectId === stagedProjectId) return   // re-picking the current one is no change at all
    setMoveError('')
    setDraftProjectId(projectId)
    if (stagedFeatureId !== null) setDraftFeatureId(null)
    /* AN UNTOUCHED FORM MOVES AT ONCE: a staged-only pick looked done, sent nothing, and a reload
       reverted it. A partial PUT (`project_id` alone; the API drops the feature on a plain move); the response
       re-seeds the drafts. Refused, the crumbs revert and the server's reason shows. With unsaved edits the pick
       stays staged: it is part of that edit, and Save carries it. */
    moveNow({ project_id: projectId })
  }
  /* The one immediate move, for both crumbs. A refusal that lands after the page moved on to another task must
     not rewrite THAT task's drafts (`currentIdRef`). */
  const moveNow = (body: { project_id: number; feature_id?: string | null }) => {
    if (isCreate || formDirty || task === null) return
    const from = task
    setMoving(true)
    put<PipelineTask>(`/api/tasks/${from.id}`, body)
      .then(updated => setTask(prev => (prev !== null && prev.id !== updated.id ? prev : keepLoaded(prev, updated))))
      .catch(err => {
        if (currentIdRef.current !== String(from.id)) return
        setDraftProjectId(from.project_id)
        setDraftFeatureId(from.feature_id ?? null)
        setMoveError(`Move refused: ${apiDetail(err, 'could not move the task')}`)
      })
      .finally(() => setMoving(false))
  }
  const stageFeature = (featureId: string | null, projectId?: number) => {
    setDraftFeatureId(featureId)
    if (projectId !== undefined) setDraftProjectId(projectId)
    /* A feature of ANOTHER project is a move too: on an untouched form it saves at once, like the project crumb. */
    if (projectId !== undefined && task !== null && projectId !== task.project_id) {
      setMoveError('')
      moveNow({ project_id: projectId, feature_id: featureId })
    }
  }
  /* CREATE: seed the feature from the board's scope once the project resolved and the tree can vouch for it
     (active, and in THAT project); anything else is "No feature". Only while nothing is picked. */
  useEffect(() => {
    if (!isCreate || draftFeatureId !== undefined || draftProjectId === null) return
    if (boardFeatureScope === null) { setDraftFeatureId(null); return }
    if (taskTree === undefined) return
    const active = taskTree.projects.some(p =>
      p.id === draftProjectId && p.features.some(f => f.id === boardFeatureScope && f.status === 'active'))
    setDraftFeatureId(active ? boardFeatureScope : null)
  }, [isCreate, draftFeatureId, draftProjectId, boardFeatureScope, taskTree])

  return {
    pickerProjects, crumbPickRefusal, prefill, prefillPending, draftTitle, setDraftTitle, draftProjectId,
    draftFeatureId, stagedProjectId, stagedFeatureId, stageProject, stageFeature, taskTree, filingFeatures,
    titleEditable, onTitleChange, projectCrumbEditable, moveError,
  }
}
