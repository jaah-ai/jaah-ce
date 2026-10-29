// The `/tasks/new?persona=<name>&project_id=<id>` seed: a link (the company-setup card's "Prepare repo" row)
// can open the create form already set up for one persona on one project. `project=<id>` is an alias (the
// shorter spelling people type); `project_id` wins when both are given. React-free.
// A SEED, never a setting: read once when the create page mounts, CREATE ONLY (the form fences it like the
// chat preset), and every value is checked against what the server itself would accept — the persona
// against the operator-pickable roster (`GET /api/tasks/personas`), the project against `GET /api/projects`.
// Anything else is dropped SILENTLY: a hand-edited URL degrades to the ordinary blank form, never an error.
// `?template=<id>` is a gallery card's Customize: the host fetches the template (`GET /api/templates/{id}`) and the
// whole seed comes from it — description, persona, involvement and model — and no project (a template belongs to
// none): the form takes the scope's project, else leaves it for the author to pick (`TaskEditForm`'s `ownProjectId`). Its persona is the server's (validated as creatable when the
// template was saved), not the picker roster's: an epic template seeds the epic depth.
import type { AttachmentResolver } from '../richtext/attachmentResolver'
import type { Project } from '../../types'
import { ONBOARD_PERSONA } from '../../lib/companySetup'
import type { PersonaOption } from './personaRegistry'
import { ADVISOR_PERSONA } from './taskPersona'

export const PREFILL_PERSONA_PARAM = 'persona'
export const PREFILL_PROJECT_PARAM = 'project_id'
export const PREFILL_PROJECT_ALIAS = 'project'
export const PREFILL_TEMPLATE_PARAM = 'template'

/** The raw params, captured once at mount. `null` = absent. */
export type TaskPrefillParams = { persona: string | null; projectId: string | null; templateId: string | null }

/** What the form takes from a template (`TemplateDetail`, `pages/templates/api.ts`). */
export type TemplateSeedSource = {
  id: string
  name: string
  persona: string
  body: string
  autonomous: boolean
  model: string | null
}

/** The template a create is seeded from: sent back as `template_id` on the create; `resolve` paints the body's
 *  `attachment:` tokens from the template's own images (nothing is re-uploaded: the create copies them). */
export type TemplateSeed = Pick<TemplateSeedSource, 'id' | 'name' | 'autonomous' | 'model'> & { resolve?: AttachmentResolver }

/** What survived validation. Each field is independent: a good persona on a bad project keeps the persona.
 *  Only the onboard persona on a valid project writes the Description. */
export type TaskPrefill = {
  /** A pickable persona name, and its picker label for the read-only chip. */
  persona?: { value: string; label: string }
  projectId?: number
  /** The Description the form opens with. `TaskCreate` has no `title`: the server derives it from the body,
   *  so this line IS the seeded title. */
  body?: string
  template?: TemplateSeed
}

/** `null` when the URL names neither, so an ordinary `/tasks/new` never waits on a seed. */
export function readTaskPrefillParams(params: URLSearchParams): TaskPrefillParams | null {
  const persona = params.get(PREFILL_PERSONA_PARAM)
  const projectId = params.get(PREFILL_PROJECT_PARAM) ?? params.get(PREFILL_PROJECT_ALIAS)
  const template = params.get(PREFILL_TEMPLATE_PARAM)
  // A UUID or nothing: anything else is a hand-edited URL, dropped like the rest.
  const templateId = template !== null && UUID_RE.test(template) ? template : null
  return persona === null && projectId === null && templateId === null ? null : { persona, projectId, templateId }
}

const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i

/** The seed a fetched template makes: every field from the template, no project. */
export function templatePrefill(
  template: TemplateSeedSource,
  personaLabel: (persona: string) => string,
  resolve?: AttachmentResolver,
): TaskPrefill {
  const { id, name, autonomous, model } = template
  return {
    persona: { value: template.persona, label: personaLabel(template.persona) },
    body: template.body,
    template: { id, name, autonomous, model, ...(resolve ? { resolve } : {}) },
  }
}

/** Validate the raw params; `null` when nothing survived (the form then opens exactly as it would unseeded). */
export function resolveTaskPrefill(
  params: Pick<TaskPrefillParams, 'persona' | 'projectId'>,
  projects: readonly Project[],
  pickable: readonly PersonaOption[],
): TaskPrefill | null {
  // Never the advisor: a chat has its own create surface (`/tasks/new-chat`) and its own grant.
  const persona = pickable.find(option => option.value === params.persona && option.value !== ADVISOR_PERSONA)
  // Digits only: `Number('')` is 0 and `Number('1e3')` is 1000, neither of which anyone linked to.
  const project = params.projectId !== null && /^\d+$/.test(params.projectId)
    ? projects.find(p => p.id === Number(params.projectId))
    : undefined
  if (!persona && !project) return null
  return {
    ...(persona ? { persona: { value: persona.value, label: persona.label } } : {}),
    ...(project ? { projectId: project.id } : {}),
    ...(persona?.value === ONBOARD_PERSONA && project ? { body: `Prepare ${project.name} for Jaah` } : {}),
  }
}
