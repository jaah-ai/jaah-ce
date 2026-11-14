import { useState, type JSX } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useParams } from 'react-router-dom'
import Box from '@mui/material/Box'
import { apiDetail, del, get, post, put } from '../../api'
import { useAuth, useCanOnProject } from '../../auth'
import Breadcrumb from '../../components/Breadcrumb'
import { useConfirm } from '../../components/ConfirmDialog'
import { bodyOf, draftFromRow, emptyDraft, EnvVarForm, localRefusal, type Draft } from '../../components/projects/EnvVarForm'
import {
  DELETE_FALLBACK,
  deleteEnvVarConfirm,
  mayWriteEnvVar,
  envListHref,
  envPreviewKey,
  envVarHref,
  envVarsKey,
  scopeLabel,
  STATUS_OK,
  unresolvedReason,
  type ProjectEnvVar,
} from '../../components/projects/projectEnvVars'
import { useBaselineDirty } from '../../hooks/useBaselineDirty'
import { useLeaveGuard } from '../../hooks/useLeaveGuard'
import { useSavedRow } from '../../hooks/useSavedRow'
import { useSubmit } from '../../hooks/useSubmit'
import type { Project } from '../../types'
import Banner from '../../ui/Banner'
import CancelSaveBar from '../../ui/CancelSaveBar'
import FormBox from '../../ui/FormBox'
import DetailPage, { DETAIL_SECTIONS_SX, DetailSection } from '../../ui/detail/DetailPage'
import DetailStatus from '../../ui/detail/DetailStatus'
import { OverflowMenu, type OverflowItem } from '../../ui/OverflowMenu'
import StatusChip from '../../ui/StatusChip'
import { LeaveGuardDialog } from '../../ui/UnsavedChangesDialog'

/* ONE PROJECT ENVIRONMENT VARIABLE (`/projects/:id/env/:varId`, and `/projects/:id/env/new` for the same page in
 * create mode), opened beside its project's Environment tab (`lib/listDetail.ts`). It replaced the tab's add/edit
 * dialog: a record is edited on its page, never in a dialog (the UI style guide's § Detail pages).
 *
 * Keyed by the row's `id`, not its name: one name has two rows (Everyone and an Only me override). There is no
 * single-row read, so the page picks its row from the tab's own list query (`envVarsKey`), and every write
 * invalidates that and the preview, so the list beside it re-reads rather than being patched. The server's verdicts
 * (`status`, `warning`) are rendered — a chip in the header, the sentence in full above the fields — never re-derived.
 *
 * Every row opens, writable or not: a row the viewer may not write (a project default without `projects:edit`,
 * another member's override — `projects:edit` does not lift that) opens read-only, Back and no Save or Delete. A
 * create is always writable: any `projects:view` holder may set their own override.
 *
 * The fields are `components/projects/EnvVarForm.tsx`'s; this file is the frame — trail, chips, the ⋯ with Delete,
 * the Save bar (disabled until the draft moved) and the leave guard. A create leaves for the new row's page and a
 * delete for the list, both through `leaveNow`, so their own navigation never asks. One section, so no tab strip. */

const FORM_ID = 'project-env-form'

export default function ProjectEnvVarPage({ mode = 'edit' }: { mode?: 'create' | 'edit' }): JSX.Element {
  const { id = '', varId = '' } = useParams<{ id: string; varId: string }>()
  const projectId = Number(id)
  const canEdit = useCanOnProject()('projects:edit', projectId)
  const viewerId = useAuth().admin?.id ?? null
  const project = useQuery({
    queryKey: ['project', projectId],
    queryFn: () => get<Project>(`/api/projects/${projectId}`),
  })
  const vars = useQuery({
    queryKey: envVarsKey(projectId),
    queryFn: () => get<ProjectEnvVar[]>(`/api/projects/${projectId}/env-vars`),
    enabled: mode === 'edit',
  })
  // A save remounts the editor at the saved row and says so on that row only (`hooks/useSavedRow.ts`).
  const savedRow = useSavedRow(varId)

  const row = mode === 'edit' ? vars.data?.find(r => r.id === varId) : undefined
  /* The trail ends in the record, and the page's title repeats it, as on a node's page (`pages/NodeDetailPage.tsx`). */
  const crumbs = (last: string) => (
    <Breadcrumb
      items={[
        { label: 'Projects', to: '/projects' },
        { label: project.data?.name ?? 'Project', to: `/projects/${projectId}` },
        { label: 'Environment', to: envListHref(projectId) },
        { label: last },
      ]}
    />
  )

  if (mode === 'create') {
    return <EnvVarEditor key="new" projectId={projectId} row={null} canEdit={canEdit} viewerId={viewerId} crumbs={crumbs} saved={false} onSaved={() => {}} />
  }
  /* Data wins over `isError`: react-query persists this key, so a reload while the API is down has both. */
  if (!vars.data && vars.isPending) return <DetailStatus trail={crumbs('…')} loading />
  if (!vars.data) {
    return <DetailStatus trail={crumbs('…')} error={apiDetail(vars.error, 'Could not load environment variables.')} />
  }
  if (!row) return <DetailStatus trail={crumbs('Not found')} notFound="Variable not found" />

  return (
    <EnvVarEditor
      key={savedRow.editorKey(row.id)}
      projectId={projectId}
      row={row}
      canEdit={canEdit}
      viewerId={viewerId}
      crumbs={crumbs}
      saved={savedRow.saved(row.id)}
      onSaved={() => savedRow.onSaved(row.id)}
    />
  )
}

function EnvVarEditor({
  projectId,
  row,
  canEdit,
  viewerId,
  crumbs,
  saved,
  onSaved,
}: {
  projectId: number
  /** `null` on the create page. */
  row: ProjectEnvVar | null
  canEdit: boolean
  viewerId: string | null
  crumbs: (last: string) => JSX.Element
  saved: boolean
  onSaved: () => void
}): JSX.Element {
  const queryClient = useQueryClient()
  const [confirm, confirmDialog] = useConfirm()
  const [draft, setDraft] = useState<Draft>(() => (row === null ? emptyDraft() : draftFromRow(row)))
  const [nameError, setNameError] = useState<string | null>(null)
  const [portError, setPortError] = useState<string | null>(null)
  const { busy, formError, setFormError, run } = useSubmit()
  const { dirty } = useBaselineDirty(JSON.stringify(draft))

  const writable = row === null || mayWriteEnvVar(row, canEdit, viewerId)

  const invalidate = async () => {
    await queryClient.invalidateQueries({ queryKey: envVarsKey(projectId) })
    await queryClient.invalidateQueries({ queryKey: envPreviewKey(projectId) })
  }

  /** Resolves true when the write landed (the leave guard's Save leaves only then). */
  async function save(): Promise<boolean> {
    if (busy || !writable) return false
    const refusal = localRefusal(draft, canEdit, viewerId)
    if (refusal !== null) {
      setNameError(refusal.where === 'name' ? refusal.message : null)
      setPortError(refusal.where === 'port' ? refusal.message : null)
      setFormError(refusal.where === 'form' ? refusal.message : '')
      return false
    }
    setNameError(null)
    setPortError(null)
    /* The server's sentence verbatim: 403 tier, 409 duplicate, 422 grammar/credential — different remedies. A
       refused write keeps the draft, so one field is corrected rather than the row retyped. */
    const landed = await run('Could not save the environment variable.', async () => {
      const body = bodyOf(draft, canEdit, viewerId)
      if (row === null) {
        const created = await post<ProjectEnvVar>(`/api/projects/${projectId}/env-vars`, body)
        /* Into the cached list BEFORE leaving: the create page never reads it, so the invalidate below refetches
           nothing while no list is on screen (a phone), and the new row's page would read "not found". */
        queryClient.setQueryData<ProjectEnvVar[]>(envVarsKey(projectId), prev => [...(prev ?? []).filter(r => r.id !== created.id), created])
        await invalidate()
        guard.leaveNow(envVarHref(projectId, created.id), { replace: true })
      } else {
        await put<ProjectEnvVar>(`/api/projects/${projectId}/env-vars/${row.id}`, body)
        await invalidate()
        onSaved()
      }
    })
    return landed !== null
  }
  const guard = useLeaveGuard(dirty, save)

  async function handleDelete(target: ProjectEnvVar): Promise<void> {
    if (busy) return
    const ok = await confirm(deleteEnvVarConfirm(target.name))
    if (!ok) return
    await run(DELETE_FALLBACK, async () => {
      await del(`/api/projects/${projectId}/env-vars/${target.id}`)
      // Leave first: the list without this row would read as "not found" here.
      guard.leaveNow(envListHref(projectId), { replace: true })
      void invalidate()
    })
  }

  const viewerOwnsRow = row !== null && viewerId !== null && row.user_id === viewerId
  const reason = row === null ? null : unresolvedReason(row, viewerOwnsRow)
  const items: OverflowItem[] = row !== null && writable
    ? [{
      key: 'delete',
      label: 'Delete',
      ariaLabel: `Delete environment variable ${row.name}`,
      testId: 'project-env-delete',
      danger: true,
      onClick: () => void handleDelete(row),
    }]
    : []

  return (
    <DetailPage
      testId="project-env-page"
      title={row?.name ?? 'New variable'}
      breadcrumb={crumbs(row?.name ?? 'New variable')}
      titleAside={row !== null && (
        <Box component="span" sx={{ display: 'inline-flex', gap: 0.5 }}>
          <Box component="span" data-testid="project-env-scope" sx={{ display: 'inline-flex' }}>
            <StatusChip tone={row.scope === 'project' ? 'info' : 'neutral'} label={scopeLabel(row, viewerId)} data-status={row.scope} />
          </Box>
          {row.status !== STATUS_OK && (
            <Box component="span" data-testid="project-env-status" sx={{ display: 'inline-flex' }}>
              <StatusChip tone="error" label="Not resolved" title={reason ?? undefined} data-status={row.status} />
            </Box>
          )}
        </Box>
      )}
      actions={items.length > 0 && (
        <OverflowMenu items={items} busy={busy} ariaLabel={`Environment variable ${row?.name ?? ''} actions`} testId="project-env-actions" />
      )}
      actionBar={
        <CancelSaveBar
          testId="project-env-form-actions"
          saveTestId="project-env-submit"
          cancelTo={envListHref(projectId)}
          creating={row === null}
          dirty={dirty}
          busy={busy}
          saved={saved && !dirty}
          readOnly={!writable}
          saveProps={{ form: FORM_ID }}
        />
      }
    >
      <Box sx={DETAIL_SECTIONS_SX}>
        {/* The server's sentences in full: the chip's hover is sight-only, and a phone has none. */}
        {reason !== null && <Banner severity="error" testId="project-env-reason">⚠ {reason}</Banner>}
        {row?.warning && <Banner severity="warning" testId="project-env-warning">⚑ {row.warning}</Banner>}
        <DetailSection>
          <FormBox id={FORM_ID} onSubmit={() => { if (dirty) void save() }}>
            <EnvVarForm
              draft={draft}
              setDraft={setDraft}
              canEdit={canEdit}
              readOnly={!writable}
              nameError={nameError}
              portError={portError}
              formError={formError}
            />
          </FormBox>
        </DetailSection>
      </Box>
      {confirmDialog}
      <LeaveGuardDialog leave={guard} message="This variable has changes that have not been saved. Save them before leaving?" />
    </DetailPage>
  )
}
