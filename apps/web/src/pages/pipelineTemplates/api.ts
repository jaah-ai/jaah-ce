import { del, get, patch, post } from '../../api'
import type { PipelineChange, PipelineProblem, StepAction, Who } from '../../lib/pipeline'

/* ORG PIPELINE TEMPLATES ON THE WIRE — `apps/api/app/routes/pipeline_templates.py`, field for field. Reads are any
 * project viewer's in the org; every write is an Org Owner/Admin's. A template's draft is a pipeline document
 * (`apps/api/app/releases/pipeline_doc.py`): stage branches are tokens (`*` each project's work branches,
 * `$integration` each project's integration branch, `''` none, else a named branch), `steps[i]` is the step into
 * `stages[i + 1]`, and each overridable field carries a lock mark (true: a linked project can't change it). */

export type TemplateState = 'never_published' | 'published' | 'draft_changes'

export type StageLocks = { branch: boolean; url: boolean }
export type StepLocks = { who: boolean; approvals: boolean; auto: boolean }
export type TemplateStage = { key?: string; name: string; branch: string; url: string | null; locks: StageLocks }
export type TemplateStep = { who: Who | null; approvals: number; auto: boolean; locks: StepLocks }
export type TemplateDoc = { stages: TemplateStage[]; steps: TemplateStep[] }

export type PublishedVersion = { id: string; number: number; published_at: string | null; published_by: string | null }
export type LinkedProject = { project_id: number; name: string }
/** A linked project on the template's page: the version it is on (null: none yet), and whether that is the published one. */
export type LinkedProjectVersion = LinkedProject & { version_number: number | null; up_to_date: boolean }
/** An org rule set to Warn that an updated project breaks. */
export type ApplyWarning = { rule: string; message: string; stage_index?: number; mode: string }

export type PipelineTemplateSummary = {
  id: string
  slug: string
  name: string
  description: string
  created_by: string | null
  created_at: string | null
  updated_by: string | null
  updated_at: string | null
  /** "Ideas → Building → Dev → Production", the draft's stages. */
  chain: string
  linked_count: number
  /** Linked projects not on the published version. */
  lagging_count: number
  state: TemplateState
  published: PublishedVersion | null
}

export type PipelineTemplate = PipelineTemplateSummary & {
  draft: TemplateDoc
  published_body: TemplateDoc | null
  linked: LinkedProjectVersion[]
  /** Each action's default roles: what a step's `who: null` resolves to. */
  defaults: Record<StepAction, string[]>
}

export type PublishPreview = { changes: PipelineChange[]; linked: LinkedProject[]; problems: PipelineProblem[] }
export type PublishResult = {
  version: number
  updated: (LinkedProject & { warnings?: ApplyWarning[] })[]
  not_updated: (LinkedProject & { reason: string })[]
}

export const STATE_LABEL: Record<TemplateState, string> = {
  never_published: 'Never published', published: 'Published', draft_changes: 'Draft changes',
}

export const TEMPLATES_KEY = ['pipeline-templates'] as const
export const templateKey = (id: string) => ['pipeline-template', id] as const
/** The list: Settings › Projects › Pipeline templates (`lib/listDetail.ts`); each template's page sits beside it. */
export const PIPELINE_TEMPLATES_SETTINGS_URL = '/settings?area=projects&section=pipeline-templates'
export const NEW_PIPELINE_TEMPLATE_PATH = '/settings/pipeline-templates/new'
export const templatePath = (id: string) => `/settings/pipeline-templates/${id}`

const BASE = '/api/org/pipeline-templates'
export const listTemplates = async (): Promise<PipelineTemplateSummary[]> =>
  (await get<{ templates: PipelineTemplateSummary[] }>(BASE)).templates
export const getTemplate = (id: string) => get<PipelineTemplate>(`${BASE}/${id}`)
export const createTemplate = (body: { name: string; description: string }) => post<PipelineTemplate>(BASE, body)
export const updateTemplate = (id: string, body: { name?: string; description?: string; draft?: TemplateDoc }) =>
  patch<PipelineTemplate>(`${BASE}/${id}`, body)
export const deleteTemplate = (id: string) => del<void>(`${BASE}/${id}`)
export const getPublishPreview = (id: string) => get<PublishPreview>(`${BASE}/${id}/publish-preview`)
export const publishTemplate = (id: string) => post<PublishResult>(`${BASE}/${id}/publish`)
/** Updates every linked project still behind to the published version again. */
export const applyTemplate = (id: string) => post<PublishResult>(`${BASE}/${id}/apply`)
