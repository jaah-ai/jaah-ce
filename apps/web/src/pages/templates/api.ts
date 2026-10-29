import { apiFetch, del, get, patch, post, postForm, put } from '../../api'
import { paintableImageUrl, type AttachmentResolver } from '../../components/richtext/attachmentResolver'
import type { PipelineTask } from '../../types'

/* THE TASK TEMPLATES WIRE — the types and calls of `/api/templates` (`apps/api/app/routes/task_templates.py` is the
 * contract's single home; these types mirror it). The manage list and the editor (`TemplatesManagePage`,
 * `TemplateEditPage`) read and write through here; so do the gallery (`TemplatesPage`), whose Start files a task from a
 * template through the ordinary `POST /api/tasks {template_id}` and `POST /start`, and Customize
 * (`/tasks/new?template=<id>`). */

export type TemplateOrigin = 'global' | 'org'

/** The purpose groups, in the gallery's order (`app/models/task_templates.py::PURPOSES`). */
export const PURPOSES = [
  { value: 'build_fix', label: 'Build & fix' },
  { value: 'quality', label: 'Quality' },
  { value: 'security', label: 'Security' },
  { value: 'maintenance', label: 'Maintenance' },
  { value: 'docs_release', label: 'Docs & release' },
  { value: 'research_planning', label: 'Research & planning' },
] as const

export const purposeLabel = (key: string): string => PURPOSES.find(p => p.value === key)?.label ?? key

/** Field bounds (`app/models/task_templates.py::MAX_NAME`/`MAX_SUMMARY`). */
export const MAX_NAME = 80
export const MAX_SUMMARY = 120
/** The card image's cap and types (`app/task_template_assets.py::MAX_CARD_IMAGE_SIZE`/`CARD_IMAGE_TYPES`). */
export const MAX_CARD_IMAGE_BYTES = 2 * 1024 * 1024
export const CARD_IMAGE_TYPES = ['image/png', 'image/jpeg', 'image/webp', 'image/svg+xml'] as const
export const CARD_IMAGE = 'card_image'
export const BODY_IMAGE = 'body_image'

/** One row of the gallery and the manage list (`_card`). */
export type TemplateCard = {
  id: string
  origin: TemplateOrigin
  slug: string
  name: string
  summary: string
  purpose: string
  persona: string
  depth: string
  involvement: string
  image_url: string | null
  featured: boolean
  published: boolean
  favorite: boolean
  starts_90d: number
  editable: boolean
  may_start: boolean
}

export type TemplateAsset = { id: string; kind: string; filename: string; content_type: string | null }

/** One template with its task fields (`_detail`). */
export type TemplateDetail = TemplateCard & {
  title: string
  body: string
  autonomous: boolean
  model: string | null
  assets: TemplateAsset[]
  created_at: string | null
  updated_at: string | null
}

/** What the editor writes (`TemplateIn`/`TemplatePatch`); a patch sends only what changed. */
export type TemplateFields = {
  name: string
  summary: string
  purpose: string
  title: string
  body: string
  persona: string
  autonomous: boolean
  model: string | null
  featured: boolean
  published: boolean
}

export const MANAGE_KEY = ['templates', 'manage'] as const
export const templateKey = (id: string) => ['templates', 'detail', id] as const

export const listManagedTemplates = (q: string) =>
  get<TemplateCard[]>(`/api/templates/manage${q.trim() ? `?q=${encodeURIComponent(q.trim())}` : ''}`)
export const getTemplate = (id: string) => get<TemplateDetail>(`/api/templates/${encodeURIComponent(id)}`)
export const createTemplate = (body: TemplateFields & { origin: TemplateOrigin }) => post<TemplateDetail>('/api/templates', body)
/** `image_key: null` drops a built-in card image (an uploaded one goes by deleting its asset). */
export const updateTemplate = (id: string, body: Partial<TemplateFields> & { image_key?: null }) =>
  patch<TemplateDetail>(`/api/templates/${encodeURIComponent(id)}`, body)
export const deleteTemplate = (id: string) => del(`/api/templates/${encodeURIComponent(id)}`)
export const templateUsage = (id: string) =>
  get<{ automations: number; tasks_90d: number }>(`/api/templates/${encodeURIComponent(id)}/usage`)

export function uploadTemplateAsset(id: string, kind: typeof CARD_IMAGE | typeof BODY_IMAGE, file: File) {
  const form = new FormData()
  form.append('kind', kind)
  form.append('file', file, file.name)
  return postForm<TemplateAsset & { url: string }>(`/api/templates/${encodeURIComponent(id)}/assets`, form)
}

export const deleteTemplateAsset = (id: string, assetId: string) =>
  del(`/api/templates/${encodeURIComponent(id)}/assets/${encodeURIComponent(assetId)}`)

export const assetContentUrl = (id: string, assetId: string) =>
  `/api/templates/${encodeURIComponent(id)}/assets/${encodeURIComponent(assetId)}/content`

/** `attachment:<token>` in a template's body → a paintable URL of the stored asset whose filename is the token
 *  (`components/richtext/attachmentResolver.ts`'s rules: raster re-typed, SVG as `data:`). Cached per resolver. */
export function templateAssetResolver(id: string, assets: readonly TemplateAsset[]): AttachmentResolver {
  const painted = new Map<string, Promise<string>>()
  return token => {
    const cached = painted.get(token)
    if (cached) return cached
    const asset = assets.find(a => a.filename === token)
    if (!asset) return Promise.reject(new Error(`no template image for token: ${token}`))
    const url = apiFetch(assetContentUrl(id, asset.id), { credentials: 'same-origin' }).then(async resp => {
      if (!resp.ok) throw new Error(`failed to fetch template image: ${token}`)
      const blob = await resp.blob()
      return paintableImageUrl(blob, asset.content_type || blob.type)
    })
    painted.set(token, url)
    url.catch(() => painted.delete(token))
    return url
  }
}

/** Whether a stored template id still resolves for the caller org (`GET /api/templates/refs`): `deleted` (no row
 *  anywhere) or `unavailable` (unpublished, switched off or another org's — `name` withheld). */
export type TemplateRef = { id: string; status: 'ok' | 'deleted' | 'unavailable'; name: string | null }
/** How many ids one `refs` call resolves (`task_templates.py::MAX_REFS`). */
export const MAX_TEMPLATE_REFS = 100
export const templateRefs = (ids: readonly string[]) =>
  get<TemplateRef[]>(`/api/templates/refs?ids=${ids.map(encodeURIComponent).join(',')}`)
/** A ref that does not resolve, in words ("Template deleted"); null when it resolves. */
export const templateRefProblem = (ref: TemplateRef | undefined): string | null =>
  !ref || ref.status === 'ok' ? null : ref.status === 'deleted' ? 'Template deleted' : 'Template unavailable'

/* ── The gallery ───────────────────────────────────────────────────────────────────────────────────── */

/** Every gallery read and write invalidates under this prefix (the manage list and the detail reads sit under it). */
export const TEMPLATES_KEY = ['templates'] as const

/** The gallery's cards: published and visible to the caller; with `q`, ranked by match. */
export const listTemplates = (q = '', purpose: string | null = null): Promise<TemplateCard[]> => {
  const params = new URLSearchParams()
  if (q.trim() !== '') params.set('q', q.trim())
  if (purpose !== null) params.set('purpose', purpose)
  const query = params.toString()
  return get<TemplateCard[]>(`/api/templates${query ? `?${query}` : ''}`)
}

export const setFavorite = (id: string, on: boolean) =>
  on ? put<void>(`/api/templates/${encodeURIComponent(id)}/favorite`, undefined)
    : del(`/api/templates/${encodeURIComponent(id)}/favorite`)

export const duplicateTemplate = (id: string) =>
  post<TemplateDetail>(`/api/templates/${encodeURIComponent(id)}/duplicate`)

/** Files a task from the template in `projectId`; the server copies title, description, persona, involvement,
 *  model and images, links the row and counts the start. The caller then starts it. */
export const createFromTemplate = (templateId: string, projectId: number) =>
  post<PipelineTask>('/api/tasks', { template_id: templateId, project_id: projectId })

/** Customize's address: the create form seeded from the template, its project left to pick. */
export const customizePath = (id: string) => `/tasks/new?template=${encodeURIComponent(id)}`
