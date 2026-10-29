import { type FormEvent, useEffect, useState } from 'react'
import { Link as RouterLink, useNavigate, useParams } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import MenuItem from '@mui/material/MenuItem'
import Stack from '@mui/material/Stack'
import TextField from '@mui/material/TextField'
import { ApiError, apiDetail, get, isNotFound, put, retryUnlessNotFound } from '../api'
import Breadcrumb from '../components/Breadcrumb'
import FeatureIcon from '../components/tasks/FeatureIcon'
import FeatureIconPicker from '../components/tasks/FeatureIconPicker'
import MuteSwitch from '../components/tasks/MuteSwitch'
import { PANE_QUERY_KEY, featureQueryKey } from '../components/tasks/pane/queryKeys'
import { useDeleteFeature } from '../components/tasks/useDeleteFeature'
import { TASK_TREE_QUERY_KEY } from '../hooks/useTaskTree'
import { parseFeatureIcon } from '../lib/featureIcon'
import { scopeHref } from '../lib/taskScope'
import { fmtTimestamp } from '../lib/relativeTime'
import type { FeatureOut, FeatureUserRef } from '../types'
import Banner from '../ui/Banner'
import { DetailPage } from '../ui/detail/DetailPage'
import DetailStatus from '../ui/detail/DetailStatus'
import FormField from '../ui/FormField'
import FormTextField from '../ui/FormTextField'
import { fieldTapTargetSx, tapTargetSx } from '../ui/listStyles'
import { MetaItem, MetaRow } from '../ui/MetaRow'
import SectionCard from '../ui/SectionCard'
import SettingCheckbox from '../ui/SettingCheckbox'
import SubmitButton from '../ui/SubmitButton'

/* THE FEATURE EDIT PAGE — `/features/:id`, opened by the Feature header's pencil (`pane/FeatureHeader.tsx`)
 * or a direct link. It edits Name, Description, Icon, Owner and Show progress in ONE `PUT /api/features/{id}`
 * carrying only the changed keys, and shows Created read-only. "Mute for me" is the user's own view, saved apart
 * (`components/tasks/MuteSwitch.tsx`). A routed page rather than a dialog: more feature data
 * lands here later. Save and Cancel both return to the feature's board; Delete (`useDeleteFeature`) to its
 * project's. Authority is the server's `can_edit` (`tasks:edit` on the project): without it every field is
 * read-only and there is no Save.
 * It shares the header's `featureQueryKey` cache, so arriving from the board paints with no fetch. */

// `app/features.py`'s FEATURE_NAME_MAX / FEATURE_DESCRIPTION_MAX — the server re-checks both.
const NAME_MAX = 200
const DESCRIPTION_MAX = 2000
const PROGRESS_HELP = 'Completion ring in the sidebar and done share in the header, for everyone.'

const FORM_SX = { mt: 1, maxWidth: 640 } as const

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

export default function FeatureEditPage() {
  const { id = '' } = useParams<{ id: string }>()
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const back = scopeHref({ kind: 'feature', featureId: id })

  const featureQuery = useQuery({
    queryKey: featureQueryKey(id),
    queryFn: () => get<FeatureOut>(`/api/features/${encodeURIComponent(id)}`),
    retry: retryUnlessNotFound,
  })
  const feature = featureQuery.data
  const canEdit = feature?.can_edit === true

  const candidates = useQuery({
    queryKey: [...featureQueryKey(id), 'lead-candidates'],
    queryFn: () => get<FeatureUserRef[]>(`/api/features/${encodeURIComponent(id)}/lead-candidates`),
    enabled: canEdit,
  })

  /* Seeded once per feature: a background refetch must never overwrite what the user is typing. `base` is
   * what the form was seeded from, and Save diffs against IT — diffing against a refetched `feature` would
   * send a field the user never touched and undo someone else's change to it. */
  const [draft, setDraft] = useState<Draft | null>(null)
  const [base, setBase] = useState<Draft | null>(null)
  useEffect(() => {
    if (feature && base === null) {
      setDraft(draftOf(feature))
      setBase(draftOf(feature))
    }
  }, [feature, base])

  const [errors, setErrors] = useState<FieldErrors>({})
  const [formError, setFormError] = useState('')

  const save = useMutation({
    mutationFn: (body: Partial<Record<Field, string | boolean | null>>) =>
      put<FeatureOut>(`/api/features/${encodeURIComponent(id)}`, body),
    onSuccess: updated => {
      queryClient.setQueryData(featureQueryKey(id), updated)
      void queryClient.invalidateQueries({ queryKey: featureQueryKey(id) })
      void queryClient.invalidateQueries({ queryKey: TASK_TREE_QUERY_KEY })
      void queryClient.invalidateQueries({ queryKey: PANE_QUERY_KEY })
      navigate(back)
    },
    onError: err => {
      const detail = apiDetail(err, 'Could not save the feature.')
      const field = err instanceof ApiError && err.status === 422 ? fieldOfDetail(detail) : null
      if (field) setErrors({ [field]: detail })
      else setFormError(detail)
    },
  })

  const remove = useDeleteFeature({
    onDeleted: projectId => navigate(scopeHref({ kind: 'project', projectId }), { replace: true }),
    onError: setFormError,
  })

  function update<K extends Field>(key: K, value: Draft[K]) {
    setDraft(d => (d ? { ...d, [key]: value } : d))
    setErrors(e => ({ ...e, [key]: undefined }))
  }

  function onSubmit(e: FormEvent) {
    e.preventDefault()
    if (!draft || !base || !canEdit) return
    const found = validate(draft)
    setErrors(found)
    setFormError('')
    if (Object.keys(found).length > 0) return
    // Only the changed keys: `PUT` reads `model_fields_set`, so an omitted key is left alone.
    const body: Partial<Record<Field, string | boolean | null>> = {}
    const name = draft.name.trim()
    if (name !== base.name) body.name = name
    if (draft.description !== base.description) body.description = draft.description
    if (draft.lead_id !== base.lead_id) body.lead_id = draft.lead_id
    if (draft.icon !== base.icon) body.icon = draft.icon
    if (draft.show_progress !== base.show_progress) body.show_progress = draft.show_progress
    if (Object.keys(body).length === 0) {
      navigate(back)
      return
    }
    save.mutate(body)
  }

  const notFound = isNotFound(featureQuery.error)
  if (notFound || !feature || !draft) {
    return (
      <DetailStatus
        trail={<Breadcrumb items={[{ label: 'Tasks', to: '/tasks' }, { label: 'Feature' }]} />}
        loading={!featureQuery.isError}
        notFound={notFound && 'Feature not found'}
        error="Could not load this feature."
        locate={{ objectType: 'feature', objectId: id }}
      />
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
        <Box component="span" sx={{ display: 'inline-flex', alignItems: 'flex-start', gap: 0.75, minWidth: 0, maxWidth: '100%' }}>
          {/* One title line tall (PageHeader's h6 line-height), so the icon sits on the first line of a wrapped name. */}
          {parseFeatureIcon(feature.icon) && (
            <Box component="span" sx={{ flex: '0 0 auto', height: '1.25em', display: 'flex', alignItems: 'center' }}>
              <FeatureIcon icon={feature.icon} testId="feature-edit-title-icon" />
            </Box>
          )}
          <Box component="span" sx={{ minWidth: 0, overflowWrap: 'anywhere' }}>{feature.name}</Box>
        </Box>
      }
    >
      <Box component="form" noValidate onSubmit={onSubmit} sx={FORM_SX} data-testid="feature-edit-form">
        <SectionCard>
          <Stack spacing={2}>
            {formError && <Banner severity="error">{formError}</Banner>}
            {!canEdit && <Banner severity="info">You can view this feature but not edit it.</Banner>}

            {/* The title row: the icon (before the name, as everywhere it shows) then the name. */}
            <Box sx={{ display: 'flex', alignItems: 'flex-start', gap: 1 }}>
              <Box sx={{ pt: 0.25 }}>
                <FeatureIconPicker
                  value={draft.icon}
                  onChange={icon => update('icon', icon)}
                  disabled={!canEdit}
                  describedBy={errors.icon ? 'feature-edit-icon-error' : undefined}
                />
              </Box>
              <Box sx={{ flex: '1 1 auto', minWidth: 0 }}>
                <FormTextField
                  label="Name" required error={errors.name}
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

            <FormTextField
              label="Description" error={errors.description}
              id="feature-edit-description"
              multiline
              minRows={3}
              value={draft.description}
              onChange={v => update('description', v)}
              testId="feature-edit-description"
              slotProps={{ htmlInput: { readOnly: !canEdit } }}
            />

            {canEdit ? (
              <FormField
                label="Owner"
                error={errors.lead_id}
                hint={candidates.isError ? 'Could not load the other possible owners.' : undefined}
              >
                <TextField
                  id="feature-edit-owner"
                  select
                  sx={fieldTapTargetSx}
                  value={draft.lead_id}
                  onChange={e => update('lead_id', e.target.value)}
                  slotProps={{ htmlInput: { 'data-testid': 'feature-edit-owner' } }}
                >
                  {owners.map(o => (
                    <MenuItem key={o.id} value={o.id}>{o.name}</MenuItem>
                  ))}
                </TextField>
              </FormField>
            ) : (
              <FormTextField
                label="Owner"
                id="feature-edit-owner"
                value={feature.lead.name}
                onChange={() => {}}
                testId="feature-edit-owner"
                slotProps={{ htmlInput: { readOnly: true } }}
              />
            )}

            {/* What the switch shows is a hover on its label, not a caption under it. */}
            <SettingCheckbox
              control="switch"
              name="show_progress"
              label="Show progress"
              help={PROGRESS_HELP}
              checked={draft.show_progress}
              disabled={!canEdit}
              onChange={v => update('show_progress', v)}
              testId="feature-edit-show-progress"
            />

            <MuteSwitch scope={{ kind: 'feature', featureId: feature.id }} noun="feature" />

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
                  disabled={remove.busy || save.isPending}
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
                <SubmitButton busy={save.isPending} testId="feature-edit-save">Save</SubmitButton>
              )}
            </Box>
          </Stack>
        </SectionCard>
        {remove.confirmDialog}
      </Box>
    </DetailPage>
  )
}
