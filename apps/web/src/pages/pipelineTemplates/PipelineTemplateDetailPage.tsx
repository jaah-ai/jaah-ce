import { useEffect, useState, type JSX, type ReactNode } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useParams } from 'react-router-dom'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import Stack from '@mui/material/Stack'
import Typography from '@mui/material/Typography'
import { ApiError, apiDetail, retryUnlessNotFound } from '../../api'
import Breadcrumb from '../../components/Breadcrumb'
import { useConfirm } from '../../components/ConfirmDialog'
import PipelineEditor, { DEFAULT_LOCKS, type EditorRow, INTEGRATION_TOKEN, newKey, WORK_TOKEN } from '../../components/projects/PipelineEditor'
import { Tabs, TabPanel } from '../../components/Tabs'
import { sameJson as same } from '../../hooks/useServerDraft'
import { useLeaveGuard } from '../../hooks/useLeaveGuard'
import { useSubmit } from '../../hooks/useSubmit'
import { useUrlTab } from '../../hooks/useUrlTab'
import { changeText, roleLabel, rulesOf } from '../../lib/pipeline'
import { absoluteTime } from '../../lib/relativeTime'
import { FONT_WEIGHT, grid } from '../../theme'
import Banner from '../../ui/Banner'
import CancelSaveBar from '../../ui/CancelSaveBar'
import DetailPage, { DETAIL_SECTIONS_SX, DetailField, DetailSection } from '../../ui/detail/DetailPage'
import { detailStatus } from '../../ui/detail/DetailStatus'
import { DisabledReason } from '../../ui/DisabledReason'
import { EntityNameFields } from '../../ui/EntityNameFields'
import { FormDialog } from '../../ui/FormDialog'
import LoadingState from '../../ui/LoadingState'
import { NoneMark } from '../../ui/NoneMark'
import { OverflowMenu } from '../../ui/OverflowMenu'
import { StatusChip } from '../../ui/StatusChip'
import { LeaveGuardDialog } from '../../ui/UnsavedChangesDialog'
import { tapTargetSx } from '../../ui/listStyles'
import {
  applyTemplate, createTemplate, deleteTemplate, getPublishPreview, getTemplate, PIPELINE_TEMPLATES_SETTINGS_URL, publishTemplate, STATE_LABEL,
  templateKey, templatePath,
  TEMPLATES_KEY, updateTemplate, type PipelineTemplate, type PublishResult, type TemplateDoc, type TemplateState,
} from './api'

/* ONE ORG PIPELINE TEMPLATE (`GET /api/org/pipeline-templates/{id}`), opened from Settings › Pipeline templates;
 * `/settings/pipeline-templates/new` is the same page in create mode (name and description only, then its page). A tabbed
 * detail (ui-style-guide): Pipeline — the shared stage and step editor (`PipelineEditor.tsx`) with a lock mark on each
 * field a linked project could change (locked: it can't), and token-aware branches (each project's work branches, its
 * integration branch, a named branch, none); Details — name, description, and the published version. One Save draft
 * (`PATCH`, only the changed keys) stores the draft only: no linked project changes until Publish. Publish… opens the
 * review (`GET …/publish-preview`): each change in plain words, the linked projects it updates, and the org rules set
 * to Block the template breaks (any one holds the publish); Publish shows what was updated and what was not, and why.
 * Details lists the linked projects, each one behind the published version with the version it is on; while any is
 * behind, Update again (`POST …/apply`) retries them and shows the same result as Publish (updated, with any org rule
 * warnings, and not updated, with why). ⋯ deletes, confirmed; refused while projects are linked, naming how many.
 * Org Owner/Admin only (the route guard). */

const TABS = [
  { id: 'pipeline', label: 'Pipeline' },
  { id: 'details', label: 'Details' },
] as const
type TemplateTab = (typeof TABS)[number]['id']

type Draft = { name: string; description: string; rows: EditorRow[] }

const OWN_BRANCH = new Set([WORK_TOKEN, INTEGRATION_TOKEN])

function rowsOf(doc: TemplateDoc): EditorRow[] {
  return doc.stages.map((stage, index) => {
    const step = index > 0 ? doc.steps[index - 1] : undefined
    return {
      key: stage.key ?? newKey(), name: stage.name, branch: stage.branch, url: stage.url ?? '',
      who: step?.who ?? null, approvals: step?.approvals ?? 0, auto: step?.auto ?? false, rules: rulesOf(undefined),
      locks: { ...DEFAULT_LOCKS, ...stage.locks, ...(step?.locks ?? {}) },
    }
  })
}

/** The rows as the document the API stores: a new stage sends no key (one is minted); each project's own branch
 *  carries no branch lock. */
function docOf(rows: readonly EditorRow[]): TemplateDoc {
  return {
    stages: rows.map(r => {
      const locks = r.locks ?? DEFAULT_LOCKS
      const branch = r.branch.trim()
      return {
        ...(r.key.startsWith('new-') ? {} : { key: r.key }), name: r.name.trim(), branch, url: r.url.trim() || null,
        locks: { branch: OWN_BRANCH.has(branch) ? false : locks.branch, url: locks.url },
      }
    }),
    steps: rows.slice(1).map(r => {
      const locks = r.locks ?? DEFAULT_LOCKS
      return { who: r.who, approvals: r.approvals, auto: r.auto, locks: { who: locks.who, approvals: locks.approvals, auto: locks.auto } }
    }),
  }
}

const draftOf = (t: PipelineTemplate): Draft => ({ name: t.name, description: t.description, rows: rowsOf(t.draft) })

const STATE_TONE: Record<TemplateState, 'success' | 'warning' | 'neutral'> = {
  published: 'success', draft_changes: 'warning', never_published: 'neutral',
}

export default function PipelineTemplateDetailPage({ mode }: { mode?: 'create' }): JSX.Element {
  const { id = '' } = useParams<{ id: string }>()
  const creating = mode === 'create'
  const read = useQuery({
    queryKey: templateKey(id), queryFn: () => getTemplate(id), enabled: !creating, retry: retryUnlessNotFound,
    meta: { persist: false },
  })
  const crumbs = (name: string) => (
    <Breadcrumb items={[{ label: 'Pipeline templates', to: PIPELINE_TEMPLATES_SETTINGS_URL }, { label: name }]} />
  )
  if (creating) return <TemplateCreate crumbs={crumbs('New template')} />
  if (!read.data) return detailStatus(read, crumbs('Template'), 'template')
  // Keyed on the template, not its stamp: a fresh read reseeds a clean draft only, and an open publish result stays.
  return <TemplateEditor key={read.data.id} template={read.data} crumbs={crumbs(read.data.name)} />
}

/** Create mode: name and description; the template starts from the Standard pipeline, then opens on its page. */
function TemplateCreate({ crumbs }: { crumbs: ReactNode }): JSX.Element {
  const client = useQueryClient()
  const [name, setName] = useState('')
  const [description, setDescription] = useState('')
  const [made, setMade] = useState(false)
  const { busy, formError, run } = useSubmit()
  const dirty = !made && (name.trim() !== '' || description.trim() !== '')
  const create = async (): Promise<boolean> => {
    if (!name.trim()) return false
    const landed = await run('Could not create the template. Retry.', () => createTemplate({ name: name.trim(), description: description.trim() }))
    if (!landed) return false
    setMade(true)
    client.setQueryData(templateKey(landed.value.id), landed.value)
    void client.invalidateQueries({ queryKey: TEMPLATES_KEY })
    // `replace`: Back lands on the list, never on a create form that would make a second template.
    guard.leaveNow(templatePath(landed.value.id), { replace: true })
    return true
  }
  const guard = useLeaveGuard(dirty, create)
  return (
    <DetailPage testId="template-detail" breadcrumb={crumbs} title="New template"
      actionBar={(
        <CancelSaveBar testId="template-form-actions" saveTestId="template-save" creating dirty={dirty} busy={busy}
          cancelTo={PIPELINE_TEMPLATES_SETTINGS_URL} disabled={!name.trim() || made}
          saveReason={dirty && !name.trim() ? 'Name is empty' : undefined} saveProps={{ onClick: () => void create() }} />
      )}>
      {formError && <Banner severity="error" testId="template-form-error">{formError}</Banner>}
      <Box sx={DETAIL_SECTIONS_SX}>
        <DetailSection title="Template" help="Starts as the Standard pipeline; change it on the next page.">
          <Box sx={{ display: 'flex', flexDirection: 'column', gap: 1.5, maxWidth: grid(80) }}>
            <EntityNameFields idPrefix="template" disabled={busy}
              name={{ value: name, onChange: setName, autoFocus: true, testId: 'template-name', maxLength: 120 }}
              description={{ value: description, onChange: setDescription, maxLength: 2000, testId: 'template-description' }} />
          </Box>
        </DetailSection>
      </Box>
      <LeaveGuardDialog leave={guard} message="This template is not created yet. Create it before leaving?" />
    </DetailPage>
  )
}

function TemplateEditor({ template, crumbs }: { template: PipelineTemplate; crumbs: ReactNode }): JSX.Element {
  const client = useQueryClient()
  const [tab, selectTab] = useUrlTab<TemplateTab>(TABS, 'pipeline')
  const [saved, setSaved] = useState<Draft>(() => draftOf(template))
  const [draft, setDraft] = useState<Draft>(saved)
  // A fresh read (another admin's save, a publish) reseeds the draft only while it holds no unsaved edit.
  useEffect(() => {
    const next = draftOf(template)
    setSaved(prev => (same(prev, next) ? prev : next))
    setDraft(prev => (same(prev, saved) ? next : prev))
  }, [template])
  const [stageError, setStageError] = useState<{ key: string; message: string } | null>(null)
  const [publishing, setPublishing] = useState(false)
  const [applied, setApplied] = useState<PublishResult | null>(null)
  const { busy, formError, setFormError, run } = useSubmit()
  const [confirm, confirmDialog] = useConfirm()

  const docChanged = !same(docOf(draft.rows), docOf(saved.rows))
  const nameChanged = draft.name.trim() !== saved.name
  const descriptionChanged = draft.description.trim() !== saved.description
  const dirty = docChanged || nameChanged || descriptionChanged
  const problem = !draft.name.trim() ? 'Name is empty' : null

  const setRows = (next: (rows: EditorRow[]) => EditorRow[]) => {
    setStageError(null)
    setFormError('')
    setDraft(d => ({ ...d, rows: next(d.rows) }))
  }

  const save = async (): Promise<boolean> => {
    if (!dirty || problem) return false
    setStageError(null)
    const landed = await run('Could not save the template. Retry.', () => updateTemplate(template.id, {
      ...(nameChanged ? { name: draft.name.trim() } : {}),
      ...(descriptionChanged ? { description: draft.description.trim() } : {}),
      ...(docChanged ? { draft: docOf(draft.rows) } : {}),
    }), {
      // A refusal naming a stage shows on its card, not in the page banner.
      mapError: (err, fallback) => {
        const detail = err instanceof ApiError ? err.detail as { stage_index?: unknown; message?: unknown } | null : null
        const index = typeof detail?.stage_index === 'number' ? detail.stage_index : null
        if (index !== null && draft.rows[index] && typeof detail?.message === 'string') {
          setStageError({ key: draft.rows[index].key, message: detail.message })
          selectTab('pipeline')
          return ''
        }
        return apiDetail(err, fallback)
      },
    })
    if (!landed) return false
    const next = draftOf(landed.value)
    setSaved(next)
    setDraft(next)
    client.setQueryData(templateKey(template.id), landed.value)
    void client.invalidateQueries({ queryKey: TEMPLATES_KEY })
    return true
  }
  const guard = useLeaveGuard(dirty, save, ['tab'])

  const remove = async () => {
    const yes = await confirm({
      title: 'Delete template?',
      message: 'Its published versions go too.',
      identity: template.name,
      confirmLabel: 'Delete',
      destructive: true,
      run: { action: () => deleteTemplate(template.id), failed: 'Could not delete. Retry.' },
    })
    if (!yes) return
    void client.invalidateQueries({ queryKey: TEMPLATES_KEY })
    client.removeQueries({ queryKey: templateKey(template.id) })
    guard.leaveNow(PIPELINE_TEMPLATES_SETTINGS_URL)
  }

  const lagging = template.linked.some(p => !p.up_to_date)
  const apply = () => void run('Could not update. Retry.', async () => {
    setApplied(await applyTemplate(template.id))
    void client.invalidateQueries({ queryKey: templateKey(template.id) })
    void client.invalidateQueries({ queryKey: TEMPLATES_KEY })
  })

  const publishReason = dirty ? 'Save the draft first.' : template.state === 'published' ? 'Nothing changed since the last publish.' : undefined
  const linkedText = `${template.linked_count} linked ${template.linked_count === 1 ? 'project keeps' : 'projects keep'}`
  const stateNote = template.state === 'never_published'
    ? 'Never published. Projects can link to it once it is.'
    : template.state === 'draft_changes'
      ? `Draft changes. ${linkedText} version ${template.published?.number} until you publish.`
      : null

  return (
    <DetailPage
      testId="template-detail"
      breadcrumb={crumbs}
      title={template.name}
      titleAside={<StatusChip label={STATE_LABEL[template.state]} tone={STATE_TONE[template.state]} data-testid="template-status" />}
      actions={(
        <Box sx={{ display: 'flex', flexWrap: 'wrap', gap: 1 }}>
          <DisabledReason reason={publishReason}>
            <Button variant="contained" onClick={() => setPublishing(true)} disabled={publishReason !== undefined || busy}
              sx={tapTargetSx} data-testid="template-publish">
              Publish…
            </Button>
          </DisabledReason>
          {lagging && (
            <Button variant="outlined" onClick={apply} disabled={busy} sx={tapTargetSx} data-testid="template-apply">
              Update again
            </Button>
          )}
          <OverflowMenu ariaLabel={`${template.name} actions`} testId="template-actions"
            items={[{ key: 'delete', label: 'Delete', ariaLabel: `Delete ${template.name}`, danger: true, testId: 'template-delete', onClick: () => void remove() }]} />
        </Box>
      )}
      actionBar={(
        <CancelSaveBar testId="template-form-actions" saveTestId="template-save" discardTestId="template-discard"
          saveLabel="Save draft" dirty={dirty} busy={busy} disabled={problem !== null}
          saveReason={dirty && problem ? problem : undefined}
          onDiscard={() => { setDraft(saved); setStageError(null); setFormError('') }}
          saveProps={{ onClick: () => void save() }} />
      )}
    >
      {formError && <Banner severity="error" testId="template-form-error">{formError}</Banner>}
      <Tabs tabs={TABS} tab={tab} onSelect={selectTab} label="Template sections" idPrefix="template" />
      <TabPanel id="pipeline" current={tab} idPrefix="template">
        <Stack spacing={2}>
          {stateNote && <Banner severity="info" testId="template-state-note">{stateNote}</Banner>}
          <PipelineEditor template rows={draft.rows} setRows={setRows} canEdit busy={busy} defaults={template.defaults}
            errors={stageError ? new Map([[stageError.key, stageError.message]]) : undefined} />
        </Stack>
      </TabPanel>
      <TabPanel id="details" current={tab} idPrefix="template">
        <Box sx={DETAIL_SECTIONS_SX}>
          <DetailSection title="Template">
            <Box sx={{ display: 'flex', flexDirection: 'column', gap: 1.5, maxWidth: grid(80) }}>
              <EntityNameFields idPrefix="template" disabled={busy}
                name={{ value: draft.name, onChange: v => setDraft(d => ({ ...d, name: v })), testId: 'template-name', maxLength: 120 }}
                description={{ value: draft.description, onChange: v => setDraft(d => ({ ...d, description: v })), maxLength: 2000, testId: 'template-description' }} />
            </Box>
          </DetailSection>
          <DetailSection title="Published" fields testId="template-published">
            <DetailField label="Version">
              {template.published
                ? `${template.published.number} · ${absoluteTime(template.published.published_at)}${template.published.published_by ? ` by ${template.published.published_by}` : ''}`
                : 'Never'}
            </DetailField>
            <DetailField label="Linked projects">{String(template.linked_count)}</DetailField>
            <DetailField label="Created">{absoluteTime(template.created_at)}{template.created_by ? ` by ${template.created_by}` : ''}</DetailField>
          </DetailSection>
          <DetailSection title="Linked projects" testId="template-linked">
            {template.linked.length === 0 ? <NoneMark /> : (
              <Box component="ul" sx={{ m: 0, pl: 2.5, '& li': { overflowWrap: 'anywhere' } }}>
                {template.linked.map(p => (
                  <Typography component="li" variant="body2" key={p.project_id} data-testid="template-linked-project">
                    {p.name}
                    {!p.up_to_date && (
                      <Box component="span" sx={{ color: 'text.secondary' }} data-testid="template-linked-version">
                        {p.version_number === null ? ' · Not updated' : ` · On version ${p.version_number}`}
                      </Box>
                    )}
                  </Typography>
                ))}
              </Box>
            )}
          </DetailSection>
        </Box>
      </TabPanel>
      {publishing && <PublishDialog template={template} onClose={() => setPublishing(false)} />}
      {applied && <ResultDialog title={`Updated to version ${applied.version}`} result={applied} onClose={() => setApplied(null)} />}
      {confirmDialog}
      <LeaveGuardDialog leave={guard} message="This template has unsaved changes. Save them before leaving?" />
    </DetailPage>
  )
}

/** The publish review: what changes, which linked projects it updates, what holds it; then what was updated. */
function PublishDialog({ template, onClose }: { template: PipelineTemplate; onClose: () => void }): JSX.Element {
  const client = useQueryClient()
  const preview = useQuery({
    queryKey: ['pipeline-template-publish-preview', template.id, template.updated_at],
    queryFn: () => getPublishPreview(template.id), retry: false, meta: { persist: false },
  })
  const [result, setResult] = useState<PublishResult | null>(null)
  const { busy, formError, run } = useSubmit()
  const problems = preview.data?.problems ?? []
  const linked = preview.data?.linked ?? []
  const publish = () => void run('Could not publish. Retry.', async () => {
    const out = await publishTemplate(template.id)
    setResult(out)
    void client.invalidateQueries({ queryKey: templateKey(template.id) })
    void client.invalidateQueries({ queryKey: TEMPLATES_KEY })
  })

  if (result) return <ResultDialog title={`Published version ${result.version}`} result={result} onClose={onClose} />

  const submitLabel = linked.length ? `Publish to ${linked.length} ${linked.length === 1 ? 'project' : 'projects'}` : 'Publish'
  return (
    <FormDialog title={`Publish “${template.name}”`} submitLabel={submitLabel} busyLabel="Publishing…" busy={busy} error={formError}
      onSubmit={publish} onClose={onClose} testId="template-publish-review" submitTestId="template-publish-confirm"
      submitDisabled={!preview.data || problems.length > 0}
      submitDisabledReason={problems.length > 0 ? 'Fix what the org rules block first.' : undefined}>
      {preview.isPending ? <LoadingState rows={3} label="Loading the changes" /> : preview.isError ? (
        <Banner severity="error">{apiDetail(preview.error, 'Could not load the changes')}</Banner>
      ) : (
        <Stack spacing={1.5}>
          {problems.length > 0 && (
            <Banner severity="error" testId="template-publish-problems">
              <Box component="span" sx={{ fontWeight: FONT_WEIGHT.semibold }}>Org rules block this template</Box>
              <Box component="ul" sx={{ m: 0, pl: 2.5 }}>
                {problems.map(p => <li key={p.message} data-testid="template-publish-problem">{p.message}</li>)}
              </Box>
            </Banner>
          )}
          <Section title="Changes">
            {preview.data.changes.length === 0
              ? <li>None</li>
              : preview.data.changes.map((c, i) => <li key={i} data-testid="template-publish-change">{changeText(c, roleLabel)}</li>)}
          </Section>
          <Section title="Linked projects">
            {linked.length === 0
              ? <li data-testid="template-publish-no-linked">None yet</li>
              : linked.map(p => <li key={p.project_id} data-testid="template-publish-linked">{p.name}</li>)}
          </Section>
        </Stack>
      )}
    </FormDialog>
  )
}

/** What a publish or Update again did: each project updated (with any org rule warnings), and not updated, with why. */
function ResultDialog({ title, result, onClose }: { title: string; result: PublishResult; onClose: () => void }): JSX.Element {
  return (
    <FormDialog title={title} submitLabel="Done" onSubmit={onClose} onClose={onClose} hideCancel testId="template-publish-result">
      <Stack spacing={1.5}>
        {result.updated.length === 0 && result.not_updated.length === 0 && (
          <Typography variant="body2">No projects are linked yet.</Typography>
        )}
        {result.updated.length > 0 && (
          <Section title={`Updated (${result.updated.length})`}>
            {result.updated.map(p => (
              <li key={p.project_id} data-testid="template-publish-updated">
                {p.name}
                {(p.warnings ?? []).map(w => (
                  <Box key={w.message} sx={{ color: 'warning.dark' }} data-testid="template-publish-warning">{w.message}</Box>
                ))}
              </li>
            ))}
          </Section>
        )}
        {result.not_updated.length > 0 && (
          <Section title={`Not updated (${result.not_updated.length})`}>
            {result.not_updated.map(p => (
              <li key={p.project_id} data-testid="template-publish-not-updated"><b>{p.name}</b>: {p.reason}</li>
            ))}
          </Section>
        )}
      </Stack>
    </FormDialog>
  )
}

function Section({ title, children }: { title: string; children: ReactNode }): JSX.Element {
  return (
    <Box>
      <Typography variant="subtitle2">{title}</Typography>
      <Box component="ul" sx={{ m: 0, pl: 2.5, '& li': { overflowWrap: 'anywhere' } }}>{children}</Box>
    </Box>
  )
}
