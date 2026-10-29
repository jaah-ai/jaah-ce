import { type FormEvent, type JSX, useEffect, useMemo, useRef, useState } from 'react'
import { Link as RouterLink, useLocation, useNavigate, useParams } from 'react-router-dom'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import Typography from '@mui/material/Typography'
import { apiDetail, isNotFound, retryUnlessNotFound } from '../../api'
import Breadcrumb from '../../components/Breadcrumb'
import { TabPanel, Tabs, type TabDef } from '../../components/Tabs'
import TaskSpecFields, { MODEL_INHERIT, PROVIDER_ANY, type TaskSpec } from '../../components/tasks/TaskSpecFields'
import { paintableImageUrl, releasePaintedUrl } from '../../components/richtext/attachmentResolver'
import { DEFAULT_PERSONA } from '../../components/tasks/taskPersona'
import { useUrlTab } from '../../hooks/useUrlTab'
import { formatBytes } from '../../lib/formatBytes'
import { TEMPLATE_PATHS } from '../../routeGroups'
import Banner from '../../ui/Banner'
import { DetailPage, DetailSection } from '../../ui/detail/DetailPage'
import DetailStatus from '../../ui/detail/DetailStatus'
import FormActionBar from '../../ui/FormActionBar'
import { FormTextField } from '../../ui/FormTextField'
import { SettingCheckbox } from '../../ui/SettingCheckbox'
import SubmitButton from '../../ui/SubmitButton'
import { PHONE, tapTargetSx } from '../../ui/listStyles'
import {
  BODY_IMAGE,
  CARD_IMAGE,
  CARD_IMAGE_TYPES,
  MANAGE_KEY,
  MAX_CARD_IMAGE_BYTES,
  MAX_NAME,
  MAX_SUMMARY,
  PURPOSES,
  type TemplateDetail,
  type TemplateFields,
  type TemplateOrigin,
  createTemplate,
  deleteTemplateAsset,
  getTemplate,
  templateAssetResolver,
  templateKey,
  updateTemplate,
  uploadTemplateAsset,
} from './api'
import { useTemplateDelete, useTemplateEditing } from './access'

/* THE TEMPLATE EDITOR — `/templates/:id`, and `/templates/new` (`mode="create"`, the same page with a blank draft that
 * Save POSTs), on `PlaybookEditPage`'s pattern: seeded ONCE from the fetched row (`base`), and Save sends only the
 * fields that differ (`PATCH`). TWO TABS, ONE FORM, the tab in the URL (`useUrlTab`):
 *  * Task — the task the template starts: Title, then `TaskSpecFields` under its `template` context, the very field
 *    components `TaskEditForm` renders (Description with rich text and pasted images, Depth, Involvement, Persona,
 *    Model). Pasted images STAGE; Save uploads them to `/api/templates/{id}/assets` (`body_image`) once the row
 *    exists, so a create uploads after its POST.
 *  * Library — how it shows in the gallery: name, summary, purpose group, card image (`card_image`, previewed;
 *    refused over `MAX_CARD_IMAGE_BYTES` before any upload), featured, and draft or published.
 * Both panels stay mounted, so an edit survives a tab switch. One Save bar below them, disabled until something
 * changes. Delete's confirm names the automations it stops (`useTemplateDelete`). */

type TemplateTab = 'task' | 'library'

/** The Library tab's own fields; the task half lives in the `TaskSpec` `TaskSpecFields` edits. */
type Library = Pick<TemplateFields, 'name' | 'summary' | 'purpose' | 'title' | 'featured' | 'published'>

const CARD_ACCEPT = CARD_IMAGE_TYPES.join(',')
const CARD_CAP_LABEL = `${MAX_CARD_IMAGE_BYTES / (1024 * 1024)} MiB`

function specOf(t: Pick<TemplateDetail, 'persona' | 'body' | 'model' | 'autonomous'>): TaskSpec {
  return {
    persona: t.persona,
    body: t.body,
    title_template: null,
    model: t.model ?? MODEL_INHERIT,
    bound_provider: PROVIDER_ANY,
    agent_id: null,
    pinned_node_id: null,
    autonomous: t.autonomous,
    project_memory: null,
    priority: 0,
    playbook_id: null,
    transition_mode: 'auto',
    assignee_id: null,
    attachment_ids: [],
    feature_id: null,
    hidden: false,
  }
}

function libraryOf(t: Library): Library {
  const { name, summary, purpose, title, featured, published } = t
  return { name, summary, purpose, title, featured, published }
}

/** The wire fields a draft stands for: the Library half and what the template keeps of the spec. */
function fieldsOf(lib: Library, spec: TaskSpec): TemplateFields {
  return {
    ...lib,
    body: spec.body,
    persona: spec.persona,
    autonomous: spec.autonomous,
    model: spec.model === MODEL_INHERIT ? null : spec.model,
  }
}

const BLANK: Library & Pick<TemplateDetail, 'persona' | 'body' | 'model' | 'autonomous'> = {
  name: '', summary: '', purpose: PURPOSES[0].value, title: '', featured: false, published: false,
  persona: DEFAULT_PERSONA, body: '', model: null, autonomous: true,
}

/** A card image the editor refuses before any upload: its type or size; null when it may go. */
export function cardImageProblem(file: File): string | null {
  if (!(CARD_IMAGE_TYPES as readonly string[]).includes(file.type)) {
    return `${file.name} is not a PNG, JPG, WebP or SVG image.`
  }
  if (file.size > MAX_CARD_IMAGE_BYTES) {
    return `${file.name} (${formatBytes(file.size)}) is over the ${CARD_CAP_LABEL} limit.`
  }
  return null
}

/** The route's element: one editor per template, so a row picked in the split pane opens a fresh form — nothing of
 *  the previous template's draft (a picked card, an error) carries over. */
export default function TemplateEditPage({ mode }: { mode?: 'create' } = {}): JSX.Element {
  const { id = '' } = useParams<{ id: string }>()
  return <TemplateEditor key={mode ?? id} mode={mode} id={id} />
}

/** What a create carries to its row's editor when a step after the POST failed: the row exists, so a retry PATCHes. */
type CreateCarry = { saveError?: string; cardError?: string }

function TemplateEditor({ mode, id }: { mode?: 'create'; id: string }): JSX.Element {
  const creating = mode === 'create'
  const carried = (useLocation().state ?? null) as CreateCarry | null
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const editing = useTemplateEditing()
  const [askDelete, deleteDialog] = useTemplateDelete()

  const query = useQuery({
    queryKey: templateKey(id),
    queryFn: () => getTemplate(id),
    enabled: !creating,
    retry: retryUnlessNotFound,
  })
  const row = creating ? null : query.data ?? null
  const readOnly = !creating && row !== null && !row.editable

  // Seeded once per template: a background refetch must never overwrite the edit in progress.
  const [lib, setLib] = useState<Library | null>(null)
  const [spec, setSpec] = useState<TaskSpec | null>(null)
  const [base, setBase] = useState<string | null>(null)
  const [stagedFiles, setStagedFiles] = useState<File[]>([])
  const [origin, setOrigin] = useState<TemplateOrigin>(editing.global ? 'global' : 'org')
  // The card: a picked file to upload, or the stored one removed.
  const [cardFile, setCardFile] = useState<File | null>(null)
  const [cardPreview, setCardPreview] = useState<string | null>(null)
  const [cardRemoved, setCardRemoved] = useState(false)
  const [cardError, setCardError] = useState(carried?.cardError ?? '')
  const [nameError, setNameError] = useState('')
  const [formError, setFormError] = useState(carried?.saveError ?? '')
  const [saving, setSaving] = useState(false)
  const cardInput = useRef<HTMLInputElement>(null)

  const tabs: readonly TabDef<TemplateTab>[] = [
    { id: 'task', label: 'Task' },
    { id: 'library', label: 'Library' },
  ]
  const [tab, selectTab] = useUrlTab(tabs, 'task')

  function seed(from: Library & Pick<TemplateDetail, 'persona' | 'body' | 'model' | 'autonomous'>) {
    const l = libraryOf(from)
    const s = specOf(from)
    setLib(l)
    setSpec(s)
    setBase(JSON.stringify(fieldsOf(l, s)))
  }

  useEffect(() => {
    if (base !== null) return
    if (creating) seed(BLANK)
    else if (row) seed(row)
    // seeded once per template, by design (above)
  }, [creating, row, base])

  useEffect(() => () => {
    if (cardPreview) releasePaintedUrl(cardPreview)
  }, [cardPreview])

  const resolveStoredSrc = useMemo(() => (row ? templateAssetResolver(row.id, row.assets) : undefined), [row])

  const fields = lib && spec ? fieldsOf(lib, spec) : null
  const json = fields ? JSON.stringify(fields) : ''
  const dirty = base !== null && (json !== base || stagedFiles.length > 0 || cardFile !== null || cardRemoved)

  const notFound = isNotFound(query.error)
  const trailRoot = { label: 'Templates', to: TEMPLATE_PATHS.manage }
  if (notFound || !lib || !spec || !fields || (!creating && !row)) {
    return (
      <DetailStatus
        trail={<Breadcrumb items={[trailRoot, { label: 'Template' }]} />}
        loading={!query.isError}
        notFound={notFound && 'Template not found'}
        error="Could not load this template."
      />
    )
  }

  function patchLib(p: Partial<Library>) {
    setLib(l => (l ? { ...l, ...p } : l))
  }

  function pickCard(file: File | undefined) {
    if (!file) return
    const problem = cardImageProblem(file)
    if (problem) {
      setCardError(problem)
      return
    }
    setCardError('')
    setCardFile(file)
    setCardRemoved(false)
    void paintableImageUrl(file, file.type).then(setCardPreview, () => setCardPreview(null))
  }

  function removeCard() {
    setCardFile(null)
    setCardPreview(null)
    setCardError('')
    setCardRemoved(true)
  }

  /* Staged body images up, each `attachment:<token>` then naming the asset the server stored (its filename, which a
   * clash would have changed). The body as it now reads, and the assets uploaded (to undo on a refused save). A failed
   * upload removes the ones before it, so a retry never stores an image twice. */
  async function uploadStaged(templateId: string, body: string): Promise<{ body: string; uploaded: string[] }> {
    const uploaded: string[] = []
    let next = body
    try {
      for (const file of stagedFiles) {
        const ref = `attachment:${file.name})`
        if (!next.includes(ref)) continue
        const asset = await uploadTemplateAsset(templateId, BODY_IMAGE, file)
        uploaded.push(asset.id)
        if (asset.filename !== file.name) next = next.split(ref).join(`attachment:${asset.filename})`)
      }
    } catch (err) {
      await dropAssets(templateId, uploaded)
      throw err
    }
    return { body: next, uploaded }
  }

  const dropAssets = (templateId: string, assetIds: string[]) =>
    Promise.allSettled(assetIds.map(assetId => deleteTemplateAsset(templateId, assetId)))

  /* The card: a picked file replaces the stored one (the server reaps the old); a removal drops an uploaded card's
   * asset, or a built-in image's key. */
  async function saveCard(saved: TemplateDetail): Promise<void> {
    if (cardFile) {
      await uploadTemplateAsset(saved.id, CARD_IMAGE, cardFile)
      return
    }
    if (!cardRemoved || saved.image_url === null) return
    const stored = saved.assets.find(a => a.kind === CARD_IMAGE)
    if (stored) await deleteTemplateAsset(saved.id, stored.id)
    else await updateTemplate(saved.id, { image_key: null })
  }

  async function save(): Promise<void> {
    if (!fields || !lib) return
    if (!fields.name.trim()) {
      setNameError('Name is required.')
      selectTab('library')
      return
    }
    setNameError('')
    setFormError('')
    setCardError('')
    setSaving(true)
    try {
      if (creating) await saveCreate(fields)
      else await saveEdit(fields)
    } catch (err) {
      setFormError(apiDetail(err, 'Could not save the template.'))
    } finally {
      setSaving(false)
    }
  }

  /* A create: POST, then the staged images and the card against the new row. Once the row exists a later failure
   * still lands on it (its editor shows what failed), so a retry PATCHes rather than POSTing a second template. */
  async function saveCreate(sent: TemplateFields): Promise<void> {
    let saved = await createTemplate({ ...sent, origin })
    const carry: CreateCarry = {}
    try {
      const { body, uploaded } = await uploadStaged(saved.id, sent.body)
      if (uploaded.length > 0 && body !== sent.body) saved = await updateTemplate(saved.id, { body })
    } catch (err) {
      carry.saveError = `Created, but an image did not save: ${apiDetail(err, 'upload failed')}`
    }
    try {
      await saveCard(saved)
    } catch (err) {
      carry.cardError = apiDetail(err, 'The image did not upload.')
    }
    void queryClient.invalidateQueries({ queryKey: MANAGE_KEY })
    navigate(`/templates/${saved.id}${carry.cardError ? '?tab=library' : ''}`, { replace: true, state: carry })
  }

  /* An edit: the staged images first (the body then names them), a PATCH of what changed, then the card. A refused
   * PATCH removes the images it would have referenced. */
  async function saveEdit(sent: TemplateFields): Promise<void> {
    const was = JSON.parse(base ?? '{}') as TemplateFields
    const { body, uploaded } = await uploadStaged(id, sent.body)
    const next = { ...sent, body }
    const diff: Partial<TemplateFields> = {}
    for (const key of Object.keys(next) as (keyof TemplateFields)[]) {
      if (JSON.stringify(next[key]) !== JSON.stringify(was[key])) (diff as Record<string, unknown>)[key] = next[key]
    }
    let saved: TemplateDetail
    try {
      saved = Object.keys(diff).length > 0 ? await updateTemplate(id, diff) : (row as TemplateDetail)
    } catch (err) {
      await dropAssets(id, uploaded)
      throw err
    }
    // The fields and images landed: from here the draft is the saved row, whatever the card does.
    setStagedFiles([])
    let cardFailed = ''
    try {
      await saveCard(saved)
      setCardFile(null)
      setCardPreview(null)
      setCardRemoved(false)
    } catch (err) {
      cardFailed = apiDetail(err, 'The image did not upload.')
    }
    void queryClient.invalidateQueries({ queryKey: MANAGE_KEY })
    // Read back what the server stored (renamed images, the card) and seed from it.
    const fresh = await getTemplate(id).catch(() => saved)
    queryClient.setQueryData(templateKey(id), fresh)
    seed(fresh)
    setCardError(cardFailed)
  }

  function onSubmit(e: FormEvent) {
    e.preventDefault()
    if (readOnly || saving || !dirty) return
    void save()
  }

  async function onDelete() {
    if (!row) return
    if (await askDelete(row)) navigate(TEMPLATE_PATHS.manage)
  }

  const storedImage = cardRemoved ? null : cardPreview ?? row?.image_url ?? null
  const headerActions = !creating && row?.editable ? (
    <Button variant="outlined" color="error" onClick={() => void onDelete()} disabled={saving} data-testid="template-edit-delete" sx={tapTargetSx}>
      Delete
    </Button>
  ) : undefined

  return (
    <DetailPage
      breadcrumb={<Breadcrumb items={[trailRoot, { label: creating ? 'New template' : lib.name || row?.name || 'Template' }]} />}
      actions={headerActions}
      testId="template-edit-page"
    >
      <Box component="form" noValidate onSubmit={onSubmit} data-testid="template-edit-form" sx={{ display: 'grid', gap: 2, mt: 1, minWidth: 0 }}>
        {formError && <Banner severity="error">{formError}</Banner>}
        {readOnly && <Banner severity="info">You can view this template but not edit it.</Banner>}

        <Tabs tabs={tabs} tab={tab} onSelect={selectTab} label="Template sections" idPrefix="template" />

        <TabPanel id="task" current={tab} idPrefix="template">
          <Box sx={{ display: 'flex', flexDirection: 'column', gap: 1.5, minWidth: 0 }}>
            <FormTextField
              id="template-title"
              label="Title"
              help="The task's title when it starts."
              value={lib.title}
              onChange={v => patchLib({ title: v })}
              testId="template-edit-title"
              slotProps={{ htmlInput: { readOnly, maxLength: 1000 } }}
            />
            <TaskSpecFields
              key={row?.id ?? 'new'}
              context="template"
              value={spec}
              onChange={setSpec}
              projectIds={[]}
              orgId={null}
              stagedFiles={stagedFiles}
              onStagedFilesChange={setStagedFiles}
              resolveStoredSrc={resolveStoredSrc}
              readOnly={readOnly}
              disabled={saving}
            />
          </Box>
        </TabPanel>

        <TabPanel id="library" current={tab} idPrefix="template">
          <DetailSection title="Library">
            <Box
              sx={{
                display: 'grid',
                gap: 1.5,
                gridTemplateColumns: 'repeat(auto-fill, minmax(15rem, 1fr))',
                [PHONE]: { gridTemplateColumns: '1fr' },
              }}
            >
              <FormTextField
                id="template-name"
                label="Name"
                required
                error={nameError || undefined}
                value={lib.name}
                autoFocus={creating && tab === 'library'}
                onChange={v => {
                  setNameError('')
                  patchLib({ name: v })
                }}
                testId="template-edit-name"
                slotProps={{ htmlInput: { readOnly, maxLength: MAX_NAME } }}
              />
              <FormTextField
                id="template-purpose"
                label="Purpose"
                select={!readOnly}
                value={lib.purpose}
                onChange={v => patchLib({ purpose: v })}
                testId="template-edit-purpose"
                options={PURPOSES.map(p => ({ value: p.value, label: p.label }))}
                slotProps={{ htmlInput: { readOnly } }}
              />
              <FormTextField
                id="template-status"
                label="Status"
                select={!readOnly}
                value={lib.published ? 'published' : 'draft'}
                onChange={v => patchLib({ published: v === 'published' })}
                testId="template-edit-status"
                help="Published templates show in the gallery."
                options={[{ value: 'draft', label: 'Draft' }, { value: 'published', label: 'Published' }]}
                slotProps={{ htmlInput: { readOnly } }}
              />
              {creating && editing.global && editing.org && (
                <FormTextField
                  id="template-origin"
                  label="Library"
                  select
                  value={origin}
                  onChange={v => setOrigin(v as TemplateOrigin)}
                  testId="template-edit-origin"
                  help="Global reaches every org; Org only yours."
                  options={[{ value: 'global', label: 'Global' }, { value: 'org', label: 'Org' }]}
                />
              )}
              <Box sx={{ gridColumn: '1 / -1' }}>
                <FormTextField
                  id="template-summary"
                  label="Summary"
                  help="One line on the gallery card."
                  value={lib.summary}
                  onChange={v => patchLib({ summary: v })}
                  testId="template-edit-summary"
                  slotProps={{ htmlInput: { readOnly, maxLength: MAX_SUMMARY } }}
                />
              </Box>
              <Box sx={{ gridColumn: '1 / -1' }}>
                <SettingCheckbox
                  name="template-featured"
                  testId="template-edit-featured"
                  label="Featured"
                  help="Shown first in the gallery."
                  checked={lib.featured}
                  disabled={readOnly || saving}
                  onChange={featured => patchLib({ featured })}
                />
              </Box>
            </Box>
          </DetailSection>

          <DetailSection title="Image">
            <Box sx={{ display: 'grid', gap: 1, maxWidth: '24rem', minWidth: 0 }} data-testid="template-card-image">
              {/* The gallery card's own frame: 16:9, covered, so the preview is what the card will show. */}
              <Box
                sx={{
                  aspectRatio: '16 / 9',
                  width: '100%',
                  borderRadius: 2,
                  border: '1px solid',
                  borderColor: 'divider',
                  bgcolor: 'action.hover',
                  overflow: 'hidden',
                  display: 'flex',
                  alignItems: 'center',
                  justifyContent: 'center',
                }}
              >
                {storedImage ? (
                  <Box
                    component="img"
                    src={storedImage}
                    alt=""
                    data-testid="template-card-preview"
                    sx={{ width: '100%', height: '100%', objectFit: 'cover', display: 'block' }}
                  />
                ) : (
                  <Typography variant="body2" sx={{ color: 'text.secondary' }} data-testid="template-card-placeholder">
                    No image: the card shows its purpose placeholder.
                  </Typography>
                )}
              </Box>
              {!readOnly && (
                <Box sx={{ display: 'flex', gap: 1, flexWrap: 'wrap', alignItems: 'center' }}>
                  <Button
                    type="button"
                    variant="outlined"
                    onClick={() => cardInput.current?.click()}
                    disabled={saving}
                    data-testid="template-card-upload"
                    sx={tapTargetSx}
                  >
                    {storedImage ? 'Replace image' : 'Upload image'}
                  </Button>
                  {storedImage && (
                    <Button type="button" color="inherit" onClick={removeCard} disabled={saving} data-testid="template-card-remove" sx={tapTargetSx}>
                      Remove
                    </Button>
                  )}
                  <Typography variant="caption" sx={{ color: 'text.secondary' }}>PNG, JPG, WebP or SVG, up to {CARD_CAP_LABEL}.</Typography>
                  <input
                    ref={cardInput}
                    type="file"
                    accept={CARD_ACCEPT}
                    hidden
                    data-testid="template-card-input"
                    onChange={e => {
                      pickCard(e.target.files?.[0])
                      e.target.value = ''
                    }}
                  />
                </Box>
              )}
              {cardError && <Typography role="alert" variant="caption" color="error" data-testid="template-card-error">{cardError}</Typography>}
            </Box>
          </DetailSection>
        </TabPanel>

        <FormActionBar testId="template-form-actions" stretchTestIds={['template-edit-save']}>
          <Button component={RouterLink} to={TEMPLATE_PATHS.manage} color="inherit" data-testid="template-edit-cancel" sx={tapTargetSx}>
            {readOnly ? 'Back' : 'Cancel'}
          </Button>
          {!readOnly && (
            <SubmitButton testId="template-edit-save" busy={saving} disabled={saving || !dirty}>
              {creating ? 'Create' : 'Save'}
            </SubmitButton>
          )}
        </FormActionBar>
      </Box>
      {deleteDialog}
    </DetailPage>
  )
}
