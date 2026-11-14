import { type JSX, useEffect, useRef, useState } from 'react'
import { Link as RouterLink } from 'react-router-dom'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import MuiLink from '@mui/material/Link'
import Stack from '@mui/material/Stack'
import Typography from '@mui/material/Typography'
import { ApiError, apiDetail, get, put } from '../../api'
import { useConfirm } from '../ConfirmDialog'
import { sameJson as same } from '../../hooks/useServerDraft'
import { useSubmit } from '../../hooks/useSubmit'
import {
  ACTION_LABEL, actionAt, type FreezeNowOut, getPipeline, impactText, levelFor, type Pipeline, type PipelineDraft, pipelineHistoryKey,
  type PipelineIn, pipelineKey, previewPipeline, putPipeline, rulesOf, stageProblems, type StepAction, unlinkTemplate, type Who,
} from '../../lib/pipeline'
import { shortDate } from '../../lib/relativeTime'
import { ROSTER_STALE_MS } from '../../lib/cachedRead'
import { AUDIT_TAB_PATHS } from '../../routeGroups'
import type { ProjectMember } from '../../types'
import { getReleaseTab, releaseKey } from '../../lib/releases'
import {
  gateDraftOf, gateIn, gateProblems, type Target, targetIn, targetProblems, urlSourceLabel,
} from '../../lib/pipelineGates'
import { FORGE_LABELS, type Project } from '../../types'
import GateList from '../pipeline/gates/GateList'
import ProductionQaConnectDeploys, { deploysConnected, useDeployTriggers } from './ProductionQaConnectDeploys'
import TargetFields from '../pipeline/gates/TargetFields'
import { viewerZone } from '../../lib/zonedTime'
import PipelineEditor, { type EditorRow, type LinkedField, type LockField, newKey } from './PipelineEditor'
import PipelineHistory from './PipelineHistory'
import { FONT_SIZE, FONT_WEIGHT, grid, RADIUS, SIZE } from '../../theme'
import ButtonBase from '@mui/material/ButtonBase'
import { DetailSection } from '../../ui/detail/DetailPage'
import Banner from '../../ui/Banner'
import { tapTargetSx } from '../../ui/listStyles'
import CancelSaveBar from '../../ui/CancelSaveBar'
import LoadingState from '../../ui/LoadingState'
import PipelineFreezes, { freezeBody, freezeIncomplete, freezeNowText, freezeRowOf, type FreezeRow, newFreezeRow } from './PipelineFreezes'
import PipelineStepPanel from './PipelineStepPanel'
import HealthDot, { HEALTH_POLL_MS } from '../pipeline/HealthDot'
import { LinkTemplateDialog, StartFromDialog, StartFromMenu, type StartMode } from './PipelineStartFrom'

/* PROJECT SETTINGS › PIPELINE (`lib/pipeline.ts`, the API `releases/pipeline.py`): the shared stage and step editor
 * (`PipelineEditor.tsx`) plus what is the project's own — each step as a panel of its rules (`PipelineStepPanel.tsx`:
 * who, approvals and approvers, timing, concurrency, and a pause, each linking to the
 * tab that owns it; a merge step also links to the project's lane in Audit › Merge lanes), the freezes with Freeze now (`PipelineFreezes.tsx`)
 * and one Save. A refusal naming a step's or a freeze's field shows under that field. A step whose `who` is
 * null shows its action's default roles and keeps sending null until the picker changes it. A save never waits for a
 * running release (it applies from the next run, said beside Save). The first branch stage's branch is the project's
 * integration branch and the work stage's pattern its prefix: governance fields, so they read only for someone the
 * server would refuse them (`/governance-authority`, as Project settings › Governance). One Save is one PUT (the
 * freezes ride along when changed) and one History row (`PipelineHistory.tsx`); before it, a dirty draft lists what it
 * changes for work already on the pipeline (`lib/pipeline.ts::impactText`). The org's pipeline rules (Settings ›
 * Pipeline rules) check every save: one set to Block refuses it on the card of the stage its step enters; one set to
 * Warn saves, shows there, and a "Saved with N warnings" banner. Linked to an org template (`data.template`): a line
 * names it with Unlink; its stages are fixed, each field it locks reads only, and an open field changed here offers the
 * template's value back (`PipelineEditor`'s `linkedField`); Start from… waits for Unlink. Unlinked, Link to template…
 * (`PipelineStartFrom.tsx`) links one after showing what changes. */

type StageRow = EditorRow
/** A refused save's message on one field: of the step into the stage `key`, or of the freeze `key`. */
type FieldError = { on: 'step' | 'freeze'; key: string; field: string; message: string }
/** A refused save's message on one gate (by its key) or on a stage's deploy target (by the stage's key). */
type GateError = { on: 'gate' | 'target'; key: string; message: string }
/** `workflows`: the project's deploy workflows, one a line, edited on the Production stage's forge target. */
type Draft = { stages: StageRow[]; freezes: FreezeRow[]; workflows: string }

function draftOf(p: Pipeline, workflows: readonly string[] | undefined): Draft {
  const steps = new Map(p.steps.map(s => [s.to_stage_id, s]))
  return {
    stages: p.stages.map(s => {
      const step = steps.get(s.id)
      return {
        key: s.id, id: s.id, name: s.name, branch: s.branch, url: s.url ?? '',
        who: step?.who ?? null, approvals: step?.approvals ?? 0, auto: step?.auto ?? false, rules: rulesOf(step),
        gates: (step?.gates ?? []).map(gateDraftOf), after: (s.after ?? []).map(gateDraftOf),
        target: s.target ?? { kind: 'none', config: null, credential: null },
      }
    }),
    freezes: p.freezes.map(freezeRowOf),
    workflows: (workflows ?? []).join('\n'),
  }
}

const NO_TARGET: Target = { kind: 'none', config: null, credential: null }
const GATED: ReadonlySet<StepAction> = new Set(['merge', 'start_merge', 'release'])
const lines = (text: string) => text.split('\n').map(line => line.trim()).filter(Boolean)
/** Whether any gate or target in the rows holds what the save would refuse (`lib/pipelineGates.ts`). */
function gatesInvalidNow(rows: readonly StageRow[]): boolean {
  const levels = rows.map(r => levelFor(r.branch))
  return rows.some((r, i) => {
    const action = i > 0 ? actionAt(levels, i) : null
    const step = action !== null && GATED.has(action) ? r.gates ?? [] : []
    const after = levels[i] === 'branch' ? r.after ?? [] : []
    return step.some(g => Object.keys(gateProblems(g, r.auto)).length > 0)
      || after.some(g => Object.keys(gateProblems(g, true)).length > 0)
      || (levels[i] === 'branch' && Object.keys(targetProblems(r.target ?? NO_TARGET)).length > 0)
  })
}
/** The Production stage (`releases/stages.py::production_index`): the last branch stage when there are two or more. */
function productionIndex(rows: readonly StageRow[]): number | null {
  const found = rows.flatMap((r, i) => (levelFor(r.branch) === 'branch' ? [i] : []))
  return found.length >= 2 ? found[found.length - 1] : null
}

/* Concurrency is a release step's rule: a step a stage edit turned into another action sends the default, which the
   API requires there (`concurrency_not_release`). */
/* A stage's target and After list go out only on a branch stage, a step's gates only on a merge or release step (D1):
   anywhere else the server refuses them. A stage whose target reports the address leaves `url` out (kept as stored).
   Each list and target goes out only when it differs from `base` (the read), so a save that edits none of them writes
   none of them. */
const pipelineBody = (rows: StageRow[], base: readonly StageRow[] = []): PipelineIn => {
  const levels = rows.map(r => levelFor(r.branch))
  const was = new Map(base.map(r => [r.key, r]))
  const edited = <K extends 'gates' | 'after' | 'target'>(r: StageRow, key: K): boolean =>
    r[key] !== undefined && !same(r[key], was.get(r.key)?.[key])
  return {
    stages: rows.map((r, i) => {
      const branch = levels[i] === 'branch'
      const target = r.target ?? NO_TARGET
      return {
        ...(r.id ? { id: r.id } : {}), name: r.name.trim(), branch: r.branch.trim(),
        ...(branch && target.kind !== 'none' ? {} : { url: r.url.trim() || null }),
        ...(branch && r.target && edited(r, 'target') ? { target: targetIn(r.target) } : {}),
        ...(branch && r.after && edited(r, 'after') ? { after: r.after.map(gateIn) } : {}),
      }
    }),
    steps: rows.slice(1).map((r, i) => ({
      who: r.who, approvals: r.approvals, auto: r.auto, ...r.rules,
      ...(actionAt(levels, i + 1) === 'release' ? {} : { concurrency: 'newest' as const }),
      ...(GATED.has(actionAt(levels, i + 1)) && r.gates && edited(r, 'gates') ? { gates: r.gates.map(gateIn) } : {}),
    })),
  }
}
/** A draft from Start from… (`PipelineStartFrom.tsx`) as editor rows: a stage the server matched keeps its id, and its
 *  gates, After list and target as read (`saved`): the draft names none of them, and the save keeps them. */
const rowsOf = (body: PipelineIn, saved: readonly StageRow[] = []): StageRow[] => body.stages.map((s, index) => {
  const step = index > 0 ? body.steps[index - 1] : undefined
  const was = s.id ? saved.find(r => r.id === s.id) : undefined
  return {
    key: s.id ?? newKey(), ...(s.id ? { id: s.id } : {}), name: s.name, branch: s.branch, url: s.url ?? was?.url ?? '',
    who: step?.who ?? null, approvals: step?.approvals ?? 0, auto: step?.auto ?? false, rules: rulesOf(step),
    ...(was ? { gates: was.gates, after: was.after, target: was.target } : {}),
  }
})
const LINK_SX = { fontSize: FONT_SIZE.sm, flexShrink: 0 } as const
const AFTER_PANEL_SX = { border: '1px solid', borderColor: 'divider', borderRadius: RADIUS.md, bgcolor: 'background.paper', minWidth: 0 } as const
const AFTER_TOGGLE_SX = {
  width: '100%', display: 'flex', flexWrap: 'wrap', alignItems: 'baseline', columnGap: 1, rowGap: 0.5, justifyContent: 'flex-start',
  textAlign: 'left', px: 1.5, py: 1, minHeight: SIZE.tapTarget, borderRadius: RADIUS.sm, '&:focus-visible': { bgcolor: 'action.hover' },
} as const
const LINKED_LINE_SX = { display: 'flex', flexWrap: 'wrap', alignItems: 'center', columnGap: 1, rowGap: 0.5 } as const

/** Two Whos name the same people, in any order (null: the action's default). */
const sameWho = (a: Who | null, b: Who | null): boolean => (a === null || b === null ? a === b
  : same([...a.roles].sort(), [...b.roles].sort()) && same([...a.users].sort(), [...b.users].sort()))
const MERGES: ReadonlySet<StepAction> = new Set(['merge', 'start_merge'])
/** The step rules a refusal's `field` may name (its part before a dot), each drawn under its control in the panel. */
const STEP_FIELDS: ReadonlySet<string> = new Set([
  'approvers', 'approval_timeout_minutes', 'instructions', 'soak_minutes', 'allowed_hours', 'concurrency',
])
const APPROVAL_FIELDS: ReadonlySet<string> = new Set(['approvers', 'approval_timeout_minutes', 'instructions'])
/** The freeze fields a refusal may name that `PipelineFreezes.tsx` draws an error under. */
const FREEZE_FIELDS: ReadonlySet<string> = new Set(['name', 'starts_at', 'ends_at', 'repeat', 'stage_ids'])


export default function PipelineSettings({ projectId, canEdit, mergeLane = false, project }: {
  projectId: number
  canEdit: boolean
  /** The project as read: its forge (the address's source line) and deploy workflows (the Production target's). */
  project?: Project
  /** The project uses the merge lane: its merge step links to Audit › Merge lanes, filtered to the project. */
  mergeLane?: boolean
}): JSX.Element {
  const queryClient = useQueryClient()
  const query = useQuery({
    queryKey: pipelineKey(projectId), queryFn: () => getPipeline(projectId),
    // While a stage shows a health dot, once a minute: the dot stays live (`app/releases/health.py`).
    refetchInterval: q => (q.state.data?.stages.some(stage => stage.health) ? HEALTH_POLL_MS : false),
  })
  // The same read and key as Project settings › Customize › Branches (`ProjectEditPage.tsx`).
  const authority = useQuery({
    queryKey: ['project-governance-authority', projectId],
    queryFn: () => get<{ may_author_governance: boolean }>(`/api/projects/${projectId}/governance-authority`),
    enabled: canEdit,
  })
  if (query.isPending) return <LoadingState label="Loading the pipeline" />
  if (query.isError) return <Banner severity="error">{apiDetail(query.error, 'Could not load the pipeline')}</Banner>
  return <Editor projectId={projectId} canEdit={canEdit} governance={authority.data?.may_author_governance === true}
    mergeLane={mergeLane} data={query.data} project={project} onSaved={fresh => {
    // Save never writes Freeze now: keep the cached one, which a switch pressed while Save ran already holds.
    queryClient.setQueryData<Pipeline>(pipelineKey(projectId), prev => (prev ? { ...fresh, freeze_now: prev.freeze_now } : fresh))
    // The integration branch and prefix live on the project row; the pipeline page reads the stages.
    void queryClient.invalidateQueries({ queryKey: ['project', projectId] })
    void queryClient.invalidateQueries({ queryKey: releaseKey(projectId) })
    void queryClient.invalidateQueries({ queryKey: pipelineHistoryKey(projectId) })
  }} />
}

function Editor({ projectId, canEdit, governance, mergeLane, data, project, onSaved }: {
  projectId: number
  project?: Project
  canEdit: boolean
  mergeLane: boolean
  /** May write the project's integration branch and prefix (the first branch stage's and the work stage's branch). */
  governance: boolean
  data: Pipeline
  onSaved: (fresh: Pipeline) => void
}): JSX.Element {
  const workflowsRead = project?.deploy_workflows
  const [base, setBase] = useState(() => draftOf(data, workflowsRead))
  const [draft, setDraft] = useState(base)
  const [stageError, setStageError] = useState<{ key: string; message: string } | null>(null)
  const [fieldError, setFieldError] = useState<FieldError | null>(null)
  const [gateError, setGateError] = useState<GateError | null>(null)
  const [workflowsError, setWorkflowsError] = useState('')
  /* The last body Save sent: which gate lists a refusal can name. */
  const sentRef = useRef<PipelineIn | null>(null)
  /* The step panels open, by their stage's key: none at first, so the page reads as the list of steps. */
  const [openSteps, setOpenSteps] = useState<ReadonlySet<string>>(() => new Set())
  const toggleStep = (key: string) => setOpenSteps(prev => {
    const next = new Set(prev)
    if (!next.delete(key)) next.add(key)
    return next
  })
  /* The After-arrival lists open, by their stage's key. */
  const [openAfter, setOpenAfter] = useState<ReadonlySet<string>>(() => new Set())
  const toggleAfter = (key: string) => setOpenAfter(prev => {
    const next = new Set(prev)
    if (!next.delete(key)) next.add(key)
    return next
  })
  /* Users picked in a Who picker this session, by id: `data.names` holds only the stored ones. */
  const [picked, setPicked] = useState<Record<string, string>>({})
  const names = { ...data.names, ...picked }
  /* The same cache entry as Project settings › General's members (`ProjectEditPage.tsx`): one GET for both. */
  const queryClient = useQueryClient()
  const members = () => queryClient.fetchQuery({
    queryKey: ['project-members', projectId], queryFn: () => get<ProjectMember[]>(`/api/projects/${projectId}/members`),
    staleTime: ROSTER_STALE_MS,
  })
  const [startMode, setStartMode] = useState<StartMode | null>(null)
  const [linking, setLinking] = useState(false)
  const [confirm, confirmDialog] = useConfirm()
  const template = data.template
  /** The preset or project the unsaved draft came from, for the banner. */
  const [fromSource, setFromSource] = useState<string | null>(null)
  // How many org rules set to Warn the last save broke: "Saved with N warnings" until the next edit.
  const [savedWarnings, setSavedWarnings] = useState(0)
  const { busy, formError, setFormError, run, saved } = useSubmit()
  const dirty = !same(draft, base)
  // A fresh read (a poll, another tab's save) reseeds a clean draft only.
  useEffect(() => {
    const next = draftOf(data, workflowsRead)
    setBase(prev => (same(prev, next) ? prev : next))
    setDraft(prev => (same(prev, base) ? next : prev))
  }, [data, workflowsRead])

  const rows = draft.stages
  const levels = rows.map(r => levelFor(r.branch))
  const stepsById = new Map(data.steps.map(s => [s.to_stage_id, s]))
  const storedById = new Map(data.stages.map(s => [s.id, s]))
  const firstBranch = levels.indexOf('branch')
  /* A stored stage holding a governance field keeps its branch for someone without the authority. */
  const branchLocked = (index: number) => !governance && rows[index].id !== undefined
    && (index === firstBranch || levels[index] === 'work')

  const setRows = (next: (rs: StageRow[]) => StageRow[]) => {
    setStageError(null)
    setFieldError(null)
    setGateError(null)
    setSavedWarnings(0)
    setDraft(d => ({ ...d, stages: next(d.stages) }))
  }
  const applyDraft = (picked: PipelineDraft, sourceName: string) => {
    const stages = rowsOf(picked.draft, base.stages)
    setDraft(d => ({ ...d, stages }))
    setFormError('')
    setFieldError(null)
    // What Save would refuse (a branch clash) shows on its stage card before Save.
    const problem = picked.problems.find(p => typeof p.stage_index === 'number' && stages[p.stage_index])
    setStageError(problem ? { key: stages[problem.stage_index as number].key, message: problem.message } : null)
    setFromSource(sourceName)
    setStartMode(null)
  }
  /* Linked: by index, each field is locked by the template or open; an open one differing from it is changed here. */
  const patchRow = (key: string, patch: Partial<StageRow>) => setRows(rs => rs.map(r => (r.key === key ? { ...r, ...patch } : r)))
  const linkedField = template ? (index: number, field: LockField): LinkedField => {
    const row = rows[index]
    if (field === 'branch' || field === 'url') {
      const locked = template.locks.stages[index]?.[field] ?? false
      const value = template.values.stages[index]
      if (!row || !value) return { locked, changed: false, reset: () => {} }
      // An open address the template leaves empty is each project's own: never "changed here".
      const changed = field === 'branch' ? row.branch.trim() !== value.branch
        : value.url != null && (row.url.trim() || null) !== value.url
      return {
        locked, changed: !locked && changed,
        reset: () => patchRow(row.key, field === 'branch' ? { branch: value.branch } : { url: value.url ?? '' }),
      }
    }
    const locked = template.locks.steps[index - 1]?.[field] ?? false
    const value = template.values.steps[index - 1]
    if (!row || !value) return { locked, changed: false, reset: () => {} }
    const changed = field === 'who' ? !sameWho(row.who, value.who) : row[field] !== value[field]
    return { locked, changed: !locked && changed, reset: () => patchRow(row.key, { [field]: value[field] }) }
  } : undefined
  /** A link or unlink saved: the pipeline as read, the draft (and any unsaved edit) replaced. */
  const relinked = (fresh: Pipeline) => {
    const next = draftOf(fresh, workflowsRead)
    setBase(next)
    setDraft(next)
    setStageError(null)
    setFromSource(null)
    setSavedWarnings(0)
    onSaved(fresh)
  }
  const unlink = async () => {
    if (!template) return
    const out: { fresh?: Pipeline } = {}
    const yes = await confirm({
      title: 'Unlink?',
      message: `Unlink from ${template.name}? The pipeline keeps its saved values and every field becomes editable.${dirty ? ' Your unsaved edits are discarded.' : ''}`,
      confirmLabel: 'Unlink',
      run: { action: async () => { out.fresh = await unlinkTemplate(projectId) }, failed: 'Could not unlink. Retry.' },
    })
    if (yes && out.fresh) relinked(out.fresh)
  }
  const setFreezes = (next: (fs: FreezeRow[]) => FreezeRow[]) => {
    setFieldError(null)
    setDraft(d => ({ ...d, freezes: next(d.freezes) }))
  }

  const freezesIncomplete = draft.freezes.some(freezeIncomplete)
  /* One save, one History row: the freezes ride along only when they changed. */
  const saveBody = (d: Draft): PipelineIn => ({
    ...pipelineBody(d.stages, base.stages), ...(same(d.freezes, base.freezes) ? {} : { freezes: freezeBody(d.freezes) }),
  })
  /* Before Save: what the draft changes for work already on the pipeline (`lib/pipeline.ts::impactText`), read a
     moment after the last edit. An incomplete freeze is left out until it is complete. */
  const [previewBody, setPreviewBody] = useState<PipelineIn | null>(null)
  useEffect(() => {
    if (!dirty || !canEdit || gatesInvalidNow(draft.stages)) { setPreviewBody(null); return }
    const timer = setTimeout(() => setPreviewBody(freezesIncomplete ? pipelineBody(draft.stages, base.stages) : saveBody(draft)), 400)
    return () => clearTimeout(timer)
  }, [draft, dirty, canEdit])
  const preview = useQuery({
    queryKey: ['pipeline-preview', projectId, previewBody],
    queryFn: () => previewPipeline(projectId, previewBody as PipelineIn),
    enabled: previewBody !== null, retry: false, staleTime: 30_000,
  })
  const impacts = dirty && previewBody !== null ? preview.data?.impacts ?? [] : []
  const problems = stageProblems(rows)
  const gatesInvalid = gatesInvalidNow(rows)
  /* The deploy workflows show (and save) only on Production's forge target. */
  const production = productionIndex(rows)
  const forgeProduction = production !== null && rows[production].target?.kind === 'forge_env'
  /* Until deploys are connected, Deploy tracking's own field names the workflows (one field, not two); once connected,
     the target's Deploy workflows field edits them. */
  const deployTriggers = useDeployTriggers(projectId, canEdit)
  const workflowsShown = forgeProduction && deploysConnected(deployTriggers.data)
  const saveReason = problems.size ? 'Fix the stages marked in red first.'
    : gatesInvalid ? 'Fix the gates and targets marked in red first.'
    : freezesIncomplete ? 'Every freeze needs a name, a start, an end and its stages.' : undefined
  /* The org rules the saved pipeline breaks, on the card of the stage each step enters (`stage_index`). */
  const warningsByKey = new Map<string, string[]>()
  for (const w of data.warnings ?? []) {
    const id = data.stages[w.stage_index]?.id
    if (id) warningsByKey.set(id, [...(warningsByKey.get(id) ?? []), w.message])
  }
  const save = () => void run('Could not save the pipeline', async () => {
    setStageError(null)
    setFieldError(null)
    setGateError(null)
    setSavedWarnings(0)
    const body = saveBody(draft)
    sentRef.current = body
    const fresh = await putPipeline(projectId, body)
    setSavedWarnings(fresh.warnings?.length ?? 0)
    // The pipeline is saved: the draft is the read from here, whatever the workflows' own write answers.
    const next = draftOf(fresh, workflowsRead)
    const workflows = workflowsShown ? draft.workflows : base.workflows
    setBase(next)
    setDraft({ ...next, workflows })
    setFromSource(null)
    onSaved(fresh)
    // The deploy workflows' one home is the project row (Production QA reads them): its own write, after the pipeline's.
    // A refusal shows on the field and keeps it unsaved.
    if (workflows !== next.workflows) {
      try {
        await put<Project>(`/api/projects/${projectId}`, { deploy_workflows: lines(workflows) })
        setBase(b => ({ ...b, workflows }))
        void queryClient.invalidateQueries({ queryKey: ['project', projectId] })
      } catch (err) {
        setWorkflowsError(apiDetail(err, 'Could not save the deploy workflows'))
      }
    }
  }, {
    /* A refusal naming a step's field shows under that field (its panel opened), one naming a freeze's field under
       that field, one naming only a stage on its card; never in the page banner alone. */
    mapError: (err, fallback) => {
      const detail = err instanceof ApiError
        ? err.detail as { stage_index?: unknown; index?: unknown; field?: unknown; message?: unknown } | null : null
      const index = typeof detail?.stage_index === 'number' ? detail.stage_index : null
      const field = typeof detail?.field === 'string' ? detail.field : null
      const message = typeof detail?.message === 'string' ? detail.message : null
      /* A gate's refusal (`gate_index`) names the stage, not the list: it is in the list the save sent (the step's
         checked first). One naming no gate but a target's field shows on that target. */
      const gateIndex = typeof (detail as { gate_index?: unknown } | null)?.gate_index === 'number'
        ? (detail as { gate_index: number }).gate_index : null
      const stageRow = index !== null ? draft.stages[index] : undefined
      if (stageRow && message && gateIndex !== null && index !== null) {
        // Only a list the save sent can be refused; with both sent, the step's is checked first.
        const sent = sentRef.current
        const sentStep = index > 0 && sent?.steps[index - 1]?.gates !== undefined
        const sentAfter = sent?.stages[index]?.after !== undefined
        const gate = (sentStep ? stageRow.gates?.[gateIndex] : undefined) ?? (sentAfter ? stageRow.after?.[gateIndex] : undefined)
        if (gate) {
          setGateError({ on: 'gate', key: gate.key, message })
          if (stageRow.gates?.includes(gate)) setOpenSteps(prev => new Set([...prev, stageRow.key]))
          else setOpenAfter(prev => new Set([...prev, stageRow.key]))
          return ''
        }
      }
      const code = typeof (detail as { code?: unknown } | null)?.code === 'string' ? (detail as { code: string }).code : ''
      if (stageRow && message && gateIndex === null && (code.startsWith('target') || code === 'url_https' || code === 'credential_required' || code === 'credential_invalid')) {
        setGateError({ on: 'target', key: stageRow.key, message })
        return ''
      }
      // Only a field the panel draws: the approval group's fields hide while no approval is needed.
      // Only a field the panel draws: the approval group's fields hide while no approval is needed, Concurrency off a
      // release step.
      const levels = draft.stages.map(r => levelFor(r.branch))
      const shown = (i: number, name: string) => STEP_FIELDS.has(name)
        && (draft.stages[i].approvals > 0 || !APPROVAL_FIELDS.has(name))
        && (name !== 'concurrency' || actionAt(levels, i) === 'release')
      if (index !== null && index > 0 && draft.stages[index] && field && message && shown(index, field.split('.')[0])) {
        const key = draft.stages[index].key
        setFieldError({ on: 'step', key, field, message })
        setOpenSteps(prev => new Set([...prev, key]))
        return ''
      }
      const freeze = typeof detail?.index === 'number' ? draft.freezes[detail.index] : undefined
      if (freeze && field && message && FREEZE_FIELDS.has(field)) {
        setFieldError({ on: 'freeze', key: freeze.key, field, message })
        return ''
      }
      if (index !== null && draft.stages[index] && typeof detail?.message === 'string') {
        setStageError({ key: draft.stages[index].key, message: detail.message })
        return ''
      }
      return apiDetail(err, fallback)
    },
  })

  /* A Forge target names the stage's environment as stored (the release read's; only fetched when one is shown). */
  const forgeShown = rows.some(r => r.target?.kind === 'forge_env')
  const releaseTab = useQuery({ queryKey: releaseKey(projectId), queryFn: () => getReleaseTab(projectId), enabled: forgeShown })
  const environments = new Map((releaseTab.data?.stages ?? []).map(st => [st.id, st.deploy_environment]))
  const urlSources = new Map(data.stages.map(st => [st.id, st.url_source ?? null]))
  const forgeLabel = project?.forge ? FORGE_LABELS[project.forge] : null
  const gateErrors = gateError?.on === 'gate' ? new Map([[gateError.key, gateError.message]]) : undefined
  const patchStage = (key: string, patch: Partial<StageRow>) =>
    setRows(rs => rs.map(r => (r.key === key ? { ...r, ...patch } : r)))
  const branchRows = rows.flatMap((r, i) => (levels[i] === 'branch' ? [{ r, i }] : []))

  /* The freezes' stage choice: stored stages that deploy to a branch (D-9); "after" names the integration stage. */
  const branchStages = data.stages.filter(st => st.level === 'branch').map(st => ({ id: st.id, name: st.name }))
  const integration = branchStages[0]?.name ?? null
  const freezeErrors = fieldError?.on === 'freeze' ? { [fieldError.key]: { [fieldError.field]: fieldError.message } } : {}
  /* Freeze now applies at once, apart from Save: the switch's answer goes straight into the read. */
  const onFreezeNow = async (out: FreezeNowOut) => {
    // A re-read sent before the switch (the health poll) would answer with the old state: drop it.
    await queryClient.cancelQueries({ queryKey: pipelineKey(projectId) })
    queryClient.setQueryData<Pipeline>(pipelineKey(projectId), prev => prev && ({
      ...prev, freeze_now: out.on && out.at ? { by_name: out.by_name, at: out.at } : null,
    }))
    void queryClient.invalidateQueries({ queryKey: releaseKey(projectId) })
  }

  return (
    <Stack spacing={2} data-testid="pipeline-settings">
      {template && (
        <Box sx={LINKED_LINE_SX} data-testid="pipeline-template-linked">
          <Typography variant="body2" sx={{ overflowWrap: 'anywhere', minWidth: 0 }}>
            Linked to template <Box component="span" sx={{ fontWeight: FONT_WEIGHT.semibold }}>{template.name}</Box>
            {template.published_at ? ` · version published ${shortDate(template.published_at)}` : ''}
          </Typography>
          {canEdit && (
            <Button size="small" variant="outlined" disabled={busy} onClick={() => void unlink()} sx={tapTargetSx}
              data-testid="pipeline-template-unlink">
              Unlink
            </Button>
          )}
        </Box>
      )}
      {data.freeze_now && (
        <Banner severity="warning" role="status" testId="pipeline-freeze-now-banner">{freezeNowText(data.freeze_now, integration)}</Banner>
      )}
      {fromSource && dirty && (
        <Banner severity="info" testId="pipeline-from-source">{`Unsaved changes from ${fromSource}. Review, then Save or Discard.`}</Banner>
      )}
      <PipelineEditor rows={rows} setRows={setRows} canEdit={canEdit} busy={busy} defaults={data.defaults} names={names}
        onName={o => setPicked(p => (p[o.key] === o.label ? p : { ...p, [o.key]: o.label }))} members={members}
        errors={stageError ? new Map([[stageError.key, stageError.message]]) : undefined} warnings={warningsByKey}
        linkedField={linkedField}
        stageMark={r => {
          // The stored stage's dot, while its address is the one checked: an edited address has not been checked yet.
          const stored = r.id ? storedById.get(r.id) : undefined
          return stored?.health && (r.url.trim() || null) === stored.url
            ? <HealthDot health={stored.health} target={stored.target?.kind} /> : null
        }}
        stageTarget={(r, index) => levels[index] === 'branch' && (<>
          <TargetFields id={r.key} target={r.target ?? NO_TARGET} canEdit={canEdit} busy={busy}
            environment={r.id ? environments.get(r.id) ?? null : null}
            error={gateError?.on === 'target' && gateError.key === r.key ? gateError.message : undefined}
            onChange={target => patchStage(r.key, { target })}
            {...(index === production && workflowsShown ? {
              workflows: draft.workflows,
              onWorkflows: (workflows: string) => { setWorkflowsError(''); setDraft(d => ({ ...d, workflows })) },
              workflowsError,
            } : {})} />
          {/* Production, whatever its target: whether its deploys are recorded, so a release waits for the deploy. */}
          {index === production && r.id && (
            <ProductionQaConnectDeploys projectId={projectId} canEdit={canEdit} title="Deploy tracking" />
          )}
        </>)}
        addressOf={(r, index) => {
          if (levels[index] !== 'branch') return undefined
          const kind = (r.target ?? NO_TARGET).kind
          const stored = r.id ? data.stages.find(st => st.id === r.id) : undefined
          // The stored source while the target and address are as read; a None stage's own typing is Typed.
          const asRead = stored !== undefined && (stored.target?.kind ?? 'none') === kind && (stored.url ?? '') === r.url
          const source = asRead ? urlSources.get(stored.id) ?? null : kind === 'none' ? 'manual' : null
          if (kind === 'none' && !r.url.trim()) return { readOnly: false, source: '' }
          return { readOnly: kind !== 'none', source: urlSourceLabel(source, kind, forgeLabel) }
        }}
        stageFooter={(r, index) => levels[index] === 'branch' && (
          <Button size="small" variant="text" onClick={() => {
            setOpenAfter(prev => new Set([...prev, r.key]))
            document.getElementById(`pipeline-after-${r.key}`)?.scrollIntoView({ behavior: 'smooth', block: 'nearest' })
          }} sx={[tapTargetSx, { alignSelf: 'flex-start' }]} data-testid="pipeline-stage-after">
            After arrival · {(r.after ?? []).length}
          </Button>
        )}
        stagesActions={canEdit && (
          <Box sx={LINKED_LINE_SX}>
            <StartFromMenu busy={busy} onPick={setStartMode}
              disabledReason={template ? `Linked to template ${template.name}; unlink to start over` : undefined} />
            {!template && (
              <Button variant="outlined" disabled={busy} onClick={() => setLinking(true)} sx={[tapTargetSx, { whiteSpace: 'nowrap' }]}
                data-testid="pipeline-link-template">
                Link to template…
              </Button>
            )}
          </Box>
        )} branchLocked={branchLocked}
        stepPanel={(r, index, action, patch) => {
          const stored = r.id ? stepsById.get(r.id) : undefined
          const gates = r.gates ?? []
          return (
            <PipelineStepPanel projectId={projectId} stepKey={r.key} step={r} action={action}
              fromName={rows[index - 1].name} toName={r.name} defaultWho={{ roles: data.defaults[action] ?? [], users: [] }}
              stored={stored} names={names} members={members} canEdit={canEdit} busy={busy}
              open={openSteps.has(r.key)} onToggle={() => toggleStep(r.key)}
              errors={fieldError?.on === 'step' && fieldError.key === r.key ? { [fieldError.field]: fieldError.message } : {}}
              onName={o => setPicked(p => (p[o.key] === o.label ? p : { ...p, [o.key]: o.label }))}
              onChange={patch}
              gates={GATED.has(action) ? {
                count: gates.length,
                list: (
                  <GateList gates={gates} onChange={next => patch({ gates: next })} canEdit={canEdit} busy={busy}
                    autoStep={r.auto} projectId={projectId} errors={gateErrors} testId="pipeline-step-gates"
                    title={`Gates of ${ACTION_LABEL[action]}: ${rows[index - 1].name} → ${r.name}`}
                    empty="No gates." />
                ),
              } : null}
              laneLink={MERGES.has(action) && mergeLane
                ? (
                  <MuiLink component={RouterLink} to={`${AUDIT_TAB_PATHS.mergeLanes}?project=${projectId}`} underline="hover"
                    sx={LINK_SX} data-testid="pipeline-step-lane" title="This step runs in the merge lane">
                    Merge lane
                  </MuiLink>
                )
                : null}
              linked={linkedField && (field => linkedField(index, field))}
              onResumed={() => void queryClient.invalidateQueries({ queryKey: pipelineKey(projectId) })} />
          )
        }} />

      {branchRows.length > 0 && (
        <DetailSection title="After arrival" divided testId="pipeline-after"
          help="Gates that run once a change reaches a stage. The step out of it opens only when the required ones pass.">
          <Stack spacing={1}>
            {branchRows.map(({ r }) => {
              const after = r.after ?? []
              const open = openAfter.has(r.key)
              return (
                <Box key={r.key} id={`pipeline-after-${r.key}`} sx={AFTER_PANEL_SX} data-testid="pipeline-after-stage" role="group"
                  aria-label={`${r.name} · After arrival`}>
                  <ButtonBase sx={AFTER_TOGGLE_SX} onClick={() => toggleAfter(r.key)} aria-expanded={open} data-testid="pipeline-after-toggle">
                    <Box component="span" aria-hidden sx={{ color: 'text.secondary', width: grid(1.5), flexShrink: 0 }}>{open ? '▾' : '▸'}</Box>
                    <Box component="span" sx={{ fontWeight: FONT_WEIGHT.semibold }}>{r.name || 'Stage'}</Box>
                    <Box component="span" sx={{ color: 'text.secondary', fontSize: FONT_SIZE.sm }}>
                      {after.length ? `${after.length} ${after.length === 1 ? 'gate' : 'gates'}` : 'No gates'}
                    </Box>
                  </ButtonBase>
                  {open && (
                    <Box sx={{ px: 1.5, pb: 1.5 }}>
                      <GateList gates={after} onChange={next => patchStage(r.key, { after: next })} canEdit={canEdit} busy={busy}
                        autoStep projectId={projectId} errors={gateErrors} testId="pipeline-after-gates"
                        title={`${r.name} · After arrival gates`} empty="No gates: a change is verified on arrival." />
                    </Box>
                  )}
                </Box>
              )
            })}
          </Stack>
        </DetailSection>
      )}

      <PipelineFreezes projectId={projectId} rows={draft.freezes} canEdit={canEdit} busy={busy}
        branchStages={branchStages} integration={integration} freezeNow={data.freeze_now ?? null} errors={freezeErrors}
        onAdd={() => setFreezes(fs => [...fs, newFreezeRow(newKey(), viewerZone())])}
        onPatch={(key, patch) => setFreezes(fs => fs.map(f => (f.key === key ? { ...f, ...patch } : f)))}
        onRemove={key => setFreezes(fs => fs.filter(x => x.key !== key))}
        onFreezeNow={onFreezeNow} />

      {impacts.length > 0 && (
        <Banner severity="info" testId="pipeline-impact">
          <Box component="span" sx={{ fontWeight: FONT_WEIGHT.semibold }}>This save affects</Box>
          <Box component="ul" sx={{ m: 0, pl: 2.5 }}>
            {impacts.map(impact => <li key={impactText(impact)} data-testid="pipeline-impact-item">{impactText(impact)}</li>)}
          </Box>
        </Banner>
      )}
      {formError && <Banner severity="error" testId="pipeline-error">{formError}</Banner>}
      {savedWarnings > 0 && !dirty && (
        <Banner severity="warning" role="status" testId="pipeline-saved-warnings">
          Saved with {savedWarnings} warning{savedWarnings === 1 ? '' : 's'}.
        </Banner>
      )}
      {canEdit && (
        <Box>
          <CancelSaveBar plain testId="pipeline-actions" saveTestId="pipeline-save" dirty={dirty} busy={busy} saved={saved && !dirty}
            onDiscard={() => { setStageError(null); setFieldError(null); setGateError(null); setWorkflowsError(''); setFromSource(null); setSavedWarnings(0); setDraft(base) }} disabled={saveReason !== undefined}
            saveReason={saveReason} saveProps={{ onClick: save }}>
            {data.release_running && (
              <Typography variant="body2" sx={{ color: 'text.secondary', alignSelf: 'center', mr: 'auto' }}
                data-testid="pipeline-next-run">
                A release is running and keeps its old settings. Changes apply from the next run.
              </Typography>
            )}
          </CancelSaveBar>
        </Box>
      )}

      {startMode && (
        <StartFromDialog projectId={projectId} mode={startMode} onClose={() => setStartMode(null)} onUse={applyDraft} />
      )}

      {linking && (
        <LinkTemplateDialog projectId={projectId} dirty={dirty} onClose={() => setLinking(false)}
          onLinked={fresh => { setLinking(false); relinked(fresh) }} />
      )}
      {confirmDialog}

      <PipelineHistory projectId={projectId} canEdit={canEdit} dirty={dirty} onRestored={onSaved} />
    </Stack>
  )
}
