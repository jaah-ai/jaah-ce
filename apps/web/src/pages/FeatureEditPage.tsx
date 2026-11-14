import { useRef, useState } from 'react'
import { Link as RouterLink, useLocation, useParams } from 'react-router-dom'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import Stack from '@mui/material/Stack'
import { ApiError, apiDetail, get, post, put, retryUnlessNotFound } from '../api'
import Breadcrumb from '../components/Breadcrumb'
import FeatureIcon from '../components/tasks/FeatureIcon'
import FeatureIconPicker from '../components/tasks/FeatureIconPicker'
import { PANE_QUERY_KEY, featureQueryKey } from '../components/tasks/pane/queryKeys'
import { useDeleteFeature } from '../components/tasks/useDeleteFeature'
import { useBaselineDirty } from '../hooks/useBaselineDirty'
import { useEditRecord } from '../hooks/useEditRecord'
import { useLeaveGuard } from '../hooks/useLeaveGuard'
import { useNestedReturn } from '../hooks/useNestedCreate'
import { useSubmit } from '../hooks/useSubmit'
import { TASK_TREE_QUERY_KEY, useTaskTree } from '../hooks/useTaskTree'
import { parseFeatureIcon } from '../lib/featureIcon'
import { returnUrl } from '../lib/nestedCreate'
import { orgQualifierFor, spokenProjectName } from '../lib/projectLabel'
import { actAsOnce } from '../lib/actAs'
import { withoutKey } from '../lib/sets'
import { creatableFor, scopeHref } from '../lib/taskScope'
import { fmtTimestamp } from '../lib/relativeTime'
import { SIZE } from '../theme'
import type { FeatureOut, FeatureUserRef } from '../types'
import Banner from '../ui/Banner'
import CancelSaveBar from '../ui/CancelSaveBar'
import { DetailPage } from '../ui/detail/DetailPage'
import { FullPageDetail } from '../components/ListDetail'
import { EditStatus } from '../ui/detail/DetailStatus'
import { EntityDescriptionField, EntityNameField } from '../ui/EntityNameFields'
import FormBox from '../ui/FormBox'
import FormTextField from '../ui/FormTextField'
import { tapTargetSx } from '../ui/listStyles'
import { MetaItem, MetaRow } from '../ui/MetaRow'
import { useOtherOrgOf } from '../ui/OrgIndicator'
import SectionCard from '../ui/SectionCard'
import SettingCheckbox from '../ui/SettingCheckbox'
import SubmitButton from '../ui/SubmitButton'
import { LeaveGuardDialog } from '../ui/UnsavedChangesDialog'

/* THE FEATURE EDIT PAGE — `/features/:id`, opened by the rail row menu's Edit (`rail/TaskTree.tsx`)
 * or a direct link. It edits Name, Description, Icon, Owner and Show progress in ONE `PUT /api/features/{id}`
 * carrying only the changed keys, and shows Created read-only. A routed page rather than a dialog: more feature data
 * lands here later. Save and Cancel both return to the feature's board; Delete (`useDeleteFeature`) to its
 * project's. Authority is the server's `can_edit` (`tasks:edit` on the project): without it every field is
 * read-only and there is no Save.
 * Loaded through `useEditRecord` under the pane's `featureQueryKey`, saved through `useSubmit`.
 * `/features/new` (mode="create") is the same page creating one: `NewFeatureForm` below. */

// `app/features.py`'s FEATURE_NAME_MAX / FEATURE_DESCRIPTION_MAX — the server re-checks both.
const NAME_MAX = 200
const DESCRIPTION_MAX = 2000
const NAME_HELP = 'Names the feature on the board and in the sidebar.'
const DESCRIPTION_HELP = "Shown above the feature's tasks."
// The project field's hover, also its accessible description.
const PROJECT_HINT = "Only this project's tasks can be filed under it."
/** `/features/new?project=<id>`: the project the opener already knows (the pane header's selection). */
export const NEW_FEATURE_PROJECT_PARAM = 'project'
const OWNER_HELP = 'Who leads the feature; grants no access.'
const PROGRESS_HELP = { tip: 'Shows everyone a completion ring beside the feature in the sidebar.', more: 'Hover the ring to see how much is done.' }

const FORM_SX = { mt: 1, maxWidth: SIZE.measure } as const

type Field = 'name' | 'description' | 'lead_id' | 'icon' | 'show_progress'
type Draft = { name: string; description: string; lead_id: string; icon: string | null; show_progress: boolean }
type FieldErrors = Partial<Record<Field, string>>

function draftOf(f: FeatureOut): Draft {
  return { name: f.name, description: f.description, lead_id: f.lead.id, icon: f.icon ?? null, show_progress: f.show_progress === true }
}

function validate(d: Draft): FieldErrors {
  const errors: FieldErrors = {}
  const name = d.name.trim()
  if (!name) errors.name = 'A feature needs a name.'
  else if (name.length > NAME_MAX) errors.name = `At most ${NAME_MAX} characters (${name.length} now).`
  if (d.description.length > DESCRIPTION_MAX) errors.description = `At most ${DESCRIPTION_MAX} characters.`
  return errors
}

/* The field a server 422 names (`app/routes/features.py`'s details: "a feature name…", "a feature
 * description…", "lead_id…"/"a feature needs a lead", "unknown icon"), or null for a form-level banner. */
export function fieldOfDetail(detail: string): Field | null {
  const d = detail.toLowerCase()
  // `lead` first: its refusal also says "must name someone".
  if (d.includes('lead')) return 'lead_id'
  if (d.includes('icon')) return 'icon'
  if (d.includes('show_progress')) return 'show_progress'
  if (d.includes('description')) return 'description'
  if (d.includes('name')) return 'name'
  return null
}

export default function FeatureEditPage({ mode = 'edit' }: { mode?: 'create' | 'edit' }) {
  return mode === 'create' ? <NewFeatureForm /> : <EditFeatureForm />
}

/* NEW FEATURE — a task's feature crumb › New feature… (`taskDetail/useTaskDetailFiling.ts`), opened through the
 * nested-create seam (`hooks/useNestedCreate`): a `?project=` fixes the project, else the user picks one of the projects
 * they may create tasks in. Create sends `POST /api/features` and goes back to the opener with the new id, or, opened
 * directly, to the new feature's board. */
function NewFeatureForm() {
  const queryClient = useQueryClient()
  const { search } = useLocation()
  const { tree, failed } = useTaskTree(true)
  const projects = creatableFor({ kind: 'all' }, tree)
  const qualifierOf = orgQualifierFor(projects)
  const presetRaw = new URLSearchParams(search).get(NEW_FEATURE_PROJECT_PARAM)
  const presetId = presetRaw !== null && /^\d+$/.test(presetRaw) ? Number(presetRaw) : null
  const fixed = presetId !== null && projects.some(p => p.id === presetId)
  const [projectId, setProjectId] = useState<number | ''>(presetId ?? '')
  const [name, setName] = useState('')
  const [description, setDescription] = useState('')
  const project = projects.find(p => p.id === projectId)
  const otherOrgOf = useOtherOrgOf()

  const { busy, errors, setErrors, formError, setFormError, run } = useSubmit<'name' | 'description'>(
    detail => {
      const field = fieldOfDetail(detail)
      return field === 'name' || field === 'description' ? field : null
    },
  )
  // Seeded once the tree is in, so a project the URL names is the baseline, not an edit.
  const { dirty } = useBaselineDirty(tree ? JSON.stringify([projectId, name, description]) : null)

  /* The id once the write landed, null when refused or invalid (the field or banner says why). */
  async function create(): Promise<string | null> {
    if (!project) return null
    // The edit form's rules; a new feature has no lead or icon to check yet.
    const { name: nameError, description: descriptionError } = validate({ name, description, lead_id: '', icon: null, show_progress: false })
    setErrors({ ...(nameError && { name: nameError }), ...(descriptionError && { description: descriptionError }) })
    if (nameError || descriptionError) return null
    let createdId: string | null = null
    await run('Could not create the feature.', async () => {
      const body: { project_id: number; name: string; description?: string } = { project_id: project.id, name: name.trim() }
      if (description !== '') body.description = description
      // Another org's project (an operator's): its feature is that org's, made as it for the one request.
      const asOrg = actAsOnce(otherOrgOf(project.org))
      const created = await (asOrg ? post<{ id: string }>('/api/features', body, asOrg) : post<{ id: string }>('/api/features', body))
      await queryClient.invalidateQueries({ queryKey: TASK_TREE_QUERY_KEY })
      createdId = created.id
    })
    return createdId
  }

  const guard = useLeaveGuard(dirty, async () => (await create()) !== null)
  const nestedReturn = useNestedReturn(guard.leaveNow)
  const cancelTo = returnUrl(search, window.location.origin, null) ?? '/tasks'

  async function onSubmit() {
    const id = await create()
    if (id === null) return
    if (nestedReturn !== null) nestedReturn(id)
    else guard.leaveNow(`/features/${encodeURIComponent(id)}`, { replace: true })
  }

  const trail = <Breadcrumb items={[{ label: 'Tasks', to: '/tasks' }, { label: 'New feature' }]} />
  if (!tree) {
    return (
      <EditStatus trail={trail} loading={!failed} notFound={false} loadError={failed ? 'Could not load your projects.' : ''}
        notFoundTitle="Feature not found" />
    )
  }

  return (
    <DetailPage
      breadcrumb={trail}
      title="New feature"
      actionBar={
        <CancelSaveBar
          testId="new-feature-actions" saveTestId="new-feature-submit" cancelTo={cancelTo} creating busy={busy}
          dirty={dirty} disabled={project === undefined || name.trim() === ''} saveProps={{ form: NEW_FEATURE_FORM_ID }}
        />
      }
    >
      <FormBox id={NEW_FEATURE_FORM_ID} onSubmit={onSubmit} sx={FORM_SX}
        data-testid="new-feature-form">
        <SectionCard>
          <Stack spacing={2}>
            {formError && <Banner severity="error">{formError}</Banner>}
            {projects.length === 0 && <Banner severity="info">You cannot create features in any project.</Banner>}
            <FormTextField
              label="Project"
              help={PROJECT_HINT}
              id="new-feature-project"
              // A preset the user may not create in shows as unpicked, not as whichever option the browser falls to.
              value={project ? String(project.id) : ''}
              onChange={v => { setProjectId(Number(v)); setFormError('') }}
              readOnly={fixed}
              autoFocus={!fixed}
              testId="new-feature-project"
              slotProps={{ htmlInput: { 'aria-description': PROJECT_HINT } }}
              options={projects.map(p => ({ value: String(p.id), label: spokenProjectName(p.name, qualifierOf(p)) }))}
            >
              {project === undefined && <option value="" disabled />}
            </FormTextField>
            <EntityNameField
              help={NAME_HELP}
              error={errors.name}
              id="new-feature-name"
              value={name}
              onChange={v => { setName(v); setErrors(e => withoutKey(e, 'name')) }}
              autoFocus={fixed}
              disabled={project === undefined}
              maxLength={NAME_MAX}
              testId="new-feature-name"
            />
            <EntityDescriptionField
              help={DESCRIPTION_HELP}
              error={errors.description}
              id="new-feature-description"
              minRows={3}
              value={description}
              onChange={v => { setDescription(v); setErrors(e => withoutKey(e, 'description')) }}
              disabled={project === undefined}
              testId="new-feature-description"
            />
          </Stack>
        </SectionCard>
        <LeaveGuardDialog leave={guard} message="This feature has not been created yet. Create it before leaving?" />
      </FormBox>
    </DetailPage>
  )
}

const NEW_FEATURE_FORM_ID = 'new-feature-form'

function EditFeatureForm() {
  const { id = '' } = useParams<{ id: string }>()
  const queryClient = useQueryClient()
  const back = scopeHref({ kind: 'feature', featureId: id })

  /* Seeded once per feature: a re-read must never overwrite what the user is typing. `base` is what the form
   * was seeded from, and Save diffs against IT — diffing against a re-read `feature` would send a field the
   * user never touched and undo someone else's change to it. */
  const [feature, setFeature] = useState<FeatureOut | null>(null)
  const [draft, setDraft] = useState<Draft | null>(null)
  const [base, setBase] = useState<Draft | null>(null)
  const seededFor = useRef<string | null>(null)
  const { loading, notFound, loadError } = useEditRecord(
    id,
    key => get<FeatureOut>(`/api/features/${encodeURIComponent(key)}`),
    row => {
      setFeature(row)
      if (seededFor.current === row.id) return
      seededFor.current = row.id
      setDraft(draftOf(row))
      setBase(draftOf(row))
    },
    {
      loadFallback: 'Could not load this feature.', fixedLoadError: true, notFoundStatuses: [404, 422],
      cacheKey: featureQueryKey, retry: retryUnlessNotFound,
    },
  )
  const canEdit = feature?.can_edit === true

  const candidates = useQuery({
    queryKey: [...featureQueryKey(id), 'lead-candidates'],
    queryFn: () => get<FeatureUserRef[]>(`/api/features/${encodeURIComponent(id)}/lead-candidates`),
    enabled: canEdit,
  })

  // A refusal lands on the field it names only when it is a 422 (`fieldOfDetail`); any other status is the banner.
  const refusedStatus = useRef(0)
  const { busy: saving, errors, setErrors, formError, setFormError, run } = useSubmit<Field>(
    detail => (refusedStatus.current === 422 ? fieldOfDetail(detail) : null),
  )
  /* Resolves true once the write landed; where to go after is the caller's (the form's Save, the leave dialog's). */
  const saveFeature = async (body: Partial<Record<Field, string | boolean | null>>): Promise<boolean> => {
    const landed = await run(
      'Could not save the feature.',
      async () => {
        const updated = await put<FeatureOut>(`/api/features/${encodeURIComponent(id)}`, body)
        queryClient.setQueryData(featureQueryKey(id), updated)
        void queryClient.invalidateQueries({ queryKey: featureQueryKey(id) })
        void queryClient.invalidateQueries({ queryKey: TASK_TREE_QUERY_KEY })
        void queryClient.invalidateQueries({ queryKey: PANE_QUERY_KEY })
      },
      { mapError: (err, fallback) => {
        refusedStatus.current = err instanceof ApiError ? err.status : 0
        return apiDetail(err, fallback)
      } },
    )
    return landed !== null
  }

  const remove = useDeleteFeature({
    onDeleted: projectId => guard.leaveNow(scopeHref({ kind: 'project', projectId }), { replace: true }),
    onError: setFormError,
  })

  function update<K extends Field>(key: K, value: Draft[K]) {
    setDraft(d => (d ? { ...d, [key]: value } : d))
    setErrors(e => withoutKey(e, key))
  }

  /* The body a Save sends: only the changed keys (`PUT` reads `model_fields_set`, so an omitted key is left alone).
   * Non-empty is "dirty", which the leave gate asks about. */
  function changedBody(): Partial<Record<Field, string | boolean | null>> {
    const body: Partial<Record<Field, string | boolean | null>> = {}
    if (!draft || !base) return body
    const name = draft.name.trim()
    if (name !== base.name) body.name = name
    if (draft.description !== base.description) body.description = draft.description
    if (draft.lead_id !== base.lead_id) body.lead_id = draft.lead_id
    if (draft.icon !== base.icon) body.icon = draft.icon
    if (draft.show_progress !== base.show_progress) body.show_progress = draft.show_progress
    return body
  }
  const dirty = canEdit && Object.keys(changedBody()).length > 0

  /* false when nothing was written: invalid (the fields say why) or refused (the banner says why). */
  async function save(): Promise<boolean> {
    if (!draft || !base || !canEdit) return false
    const found = validate(draft)
    setErrors(found)
    setFormError('')
    if (Object.keys(found).length > 0) return false
    const body = changedBody()
    return Object.keys(body).length === 0 || saveFeature(body)
  }

  // Leaving with unsaved edits asks first (`hooks/useLeaveGuard`); its Save is this form's Save.
  const guard = useLeaveGuard(dirty, save)

  async function onSubmit() {
    if (await save()) guard.leaveNow(back)
  }

  // A cached row paints at once (`useEditRecord`'s `cacheKey`); one for another id never shows under this one's Save.
  if (notFound || feature?.id !== id || !draft) {
    return (
      <>
        {/* Not found: the page itself, never one line in a pane beside the board (`FullPageDetail`). */}
        {notFound && <FullPageDetail />}
        <EditStatus
          trail={<Breadcrumb items={[{ label: 'Tasks', to: '/tasks' }, { label: 'Feature' }]} />}
          loading={loading} notFound={notFound} loadError={loadError} notFoundTitle="Feature not found"
          locate={{ objectType: 'feature', objectId: id }}
        />
      </>
    )
  }

  // The current owner stays listed even if they are no longer a candidate, so the select never goes blank.
  const owners: FeatureUserRef[] = [
    ...(candidates.data?.some(c => c.id === feature.lead.id) ? [] : [feature.lead]),
    ...(candidates.data ?? []),
  ]

  return (
    <DetailPage
      breadcrumb={
        <Breadcrumb
          items={[
            { label: feature.project_name, to: scopeHref({ kind: 'project', projectId: feature.project_id }) },
            { label: feature.name, to: back },
            { label: 'Edit' },
          ]}
        />
      }
      title={
        <Box component="span" sx={{ display: 'inline-flex', alignItems: 'flex-start', gap: 0.5, minWidth: 0, maxWidth: '100%' }}>
          {/* One title line tall (PageHeader's h6 line-height), so the icon sits on the first line of a wrapped name. */}
          {parseFeatureIcon(feature.icon) && (
            <Box component="span" sx={{ flex: '0 0 auto', height: '1lh', display: 'flex', alignItems: 'center' }}>
              <FeatureIcon icon={feature.icon} testId="feature-edit-title-icon" />
            </Box>
          )}
          <Box component="span" sx={{ minWidth: 0, overflowWrap: 'anywhere' }}>{feature.name}</Box>
        </Box>
      }
    >
      <FormBox onSubmit={onSubmit} sx={FORM_SX} data-testid="feature-edit-form">
        <SectionCard>
          <Stack spacing={2}>
            {formError && <Banner severity="error">{formError}</Banner>}
            {!canEdit && <Banner severity="info">You can view this feature but not edit it.</Banner>}

            {/* The title row: the icon (before the name, as everywhere it shows) then the name. */}
            <Box sx={{ display: 'flex', alignItems: 'flex-start', gap: 1 }}>
              <Box sx={{ pt: 0 }}>
                <FeatureIconPicker
                  value={draft.icon}
                  onChange={icon => update('icon', icon)}
                  disabled={!canEdit}
                  describedBy={errors.icon ? 'feature-edit-icon-error' : undefined}
                />
              </Box>
              <Box sx={{ flex: '1 1 auto', minWidth: 0 }}>
                <EntityNameField
                  help={NAME_HELP}
                  error={errors.name}
                  id="feature-edit-name"
                  value={draft.name}
                  onChange={v => update('name', v)}
                  testId="feature-edit-name"
                  slotProps={{ htmlInput: { readOnly: !canEdit } }}
                />
              </Box>
            </Box>
            {errors.icon && (
              <Box
                id="feature-edit-icon-error"
                role="alert"
                sx={{ color: 'error.main', typography: 'caption' }}
                data-testid="feature-edit-icon-error"
              >
                {errors.icon}
              </Box>
            )}

            <EntityDescriptionField
              help={DESCRIPTION_HELP}
              error={errors.description}
              id="feature-edit-description"
              minRows={3}
              value={draft.description}
              onChange={v => update('description', v)}
              testId="feature-edit-description"
              slotProps={{ htmlInput: { readOnly: !canEdit } }}
            />

            {canEdit ? (
              <FormTextField
                label="Owner"
                help={OWNER_HELP}
                error={errors.lead_id}
                hint={candidates.isError ? 'Could not load the other possible owners.' : undefined}
                id="feature-edit-owner"
                value={draft.lead_id}
                onChange={v => update('lead_id', v)}
                testId="feature-edit-owner"
                options={owners.map(o => ({ value: o.id, label: o.name }))}
              />
            ) : (
              <FormTextField
                label="Owner"
                help={OWNER_HELP}
                id="feature-edit-owner"
                value={feature.lead.name}
                onChange={() => {}}
                testId="feature-edit-owner"
                slotProps={{ htmlInput: { readOnly: true } }}
              />
            )}

            {/* What the switch shows is a hover on its label, not a caption under it. */}
            <SettingCheckbox
              dense
              name="show_progress"
              label="Show progress"
              help={PROGRESS_HELP}
              checked={draft.show_progress}
              disabled={!canEdit}
              onChange={v => update('show_progress', v)}
              testId="feature-edit-show-progress"
            />

            <MetaRow>
              <MetaItem
                label="Created"
                value={`${fmtTimestamp(feature.created_at)} by ${feature.created_by.name}`}
                title={feature.created_at}
              />
            </MetaRow>

            <Box sx={{ display: 'flex', justifyContent: 'flex-end', gap: 1, flexWrap: 'wrap' }}>
              {canEdit && (
                <Button
                  color="error"
                  data-testid="feature-edit-delete"
                  disabled={remove.busy || saving}
                  onClick={() => void remove.deleteFeature(feature)}
                  sx={[tapTargetSx, { mr: 'auto' }]}
                >
                  Delete
                </Button>
              )}
              <Button component={RouterLink} to={back} color="inherit" data-testid="feature-edit-cancel" sx={tapTargetSx}>
                {canEdit ? 'Cancel' : 'Back'}
              </Button>
              {canEdit && (
                <SubmitButton busy={saving} testId="feature-edit-save">Save</SubmitButton>
              )}
            </Box>
          </Stack>
        </SectionCard>
        {remove.confirmDialog}
        <LeaveGuardDialog leave={guard} message="This feature has changes that have not been saved. Save them before leaving?" />
      </FormBox>
    </DetailPage>
  )
}
