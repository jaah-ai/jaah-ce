import { type FormEvent, type JSX, useEffect, useMemo, useRef, useState } from 'react'
import { Link as RouterLink, useNavigate, useParams } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import MenuItem from '@mui/material/MenuItem'
import TextField from '@mui/material/TextField'
import { ApiError, apiDetail, isNotFound, retryUnlessNotFound } from '../../api'
import Breadcrumb from '../../components/Breadcrumb'
import { TabPanel, Tabs, type TabDef } from '../../components/Tabs'
import { useConfirm } from '../../components/ConfirmDialog'
import { useChangeOrgAction } from '../../components/ChangeOrgDialog'
import { useSettled } from '../../hooks/useSettled'
import { useUrlTab } from '../../hooks/useUrlTab'
import Banner from '../../ui/Banner'
import { DetailPage, DetailSection } from '../../ui/detail/DetailPage'
import DetailStatus from '../../ui/detail/DetailStatus'
import FormActionBar from '../../ui/FormActionBar'
import FormField from '../../ui/FormField'
import FormTextField from '../../ui/FormTextField'
import SubmitButton from '../../ui/SubmitButton'
import { PHONE, fieldTapTargetSx, tapTargetSx } from '../../ui/listStyles'
import ChainStrip from './ChainStrip'
import PromptPreviewDialog from './PromptPreviewDialog'
import StageCard, { SELECT_WRAP_SX, withoutFieldWords } from './StageCard'
import { LockGlyph } from './PlaybooksPage'
import StatusChip from '../../ui/StatusChip'
import {
  PLAYBOOKS_KEY,
  VOCABULARY_KEY,
  type Derived,
  type Issue,
  type PlaybookIn,
  type PlaybookOut,
  type StageIn,
  createPlaybook,
  deletePlaybook,
  duplicatePlaybook,
  getPlaybook,
  getVocabulary,
  inputOf,
  issuesOf,
  labelOf,
  playbookKey,
  updatePlaybook,
  validatePlaybook,
} from './api'

/* THE PLAYBOOK EDITOR — `/playbooks/:id`, and `/playbooks/new` (`mode="create"`: a blank draft, one stage, that
 * Save POSTs), on `FeatureEditPage`'s pattern: the form is seeded ONCE from the
 * fetched row (`base`), and Save sends only when the draft differs from it (the `PUT` is a full replace, so
 * "diffed" is whether anything changed at all). Save and Cancel return to the list. A built-in, or any
 * playbook for a viewer who may not mutate, opens read-only; a built-in offers Duplicate, an editable copy
 * Duplicate and Delete.
 *
 * VALIDATION IS THE SERVER'S: every edit re-runs `POST /api/playbooks/validate` (debounced), whose issues and
 * derived facts paint before Save. Each stage carries a client id (`cid`) so a response — computed for the
 * draft AS SENT — lands on the right card even after a later reorder: an issue's `stage_index` is resolved
 * against the snapshot's cids, never against the current order. Errors block Save; warnings do not (D7).
 *
 * TWO TABS, ONE FORM: Playbook (its own fields and the Chain summary) and Stages. Both panels stay mounted, so
 * an unsaved edit survives a tab switch, and the Save bar sits below them, reachable from either. A refused
 * Save selects the tab holding the first error and focuses its field (`META_INPUTS`, `STAGE_INPUTS`, `focusIssue`). */

type Meta = Omit<PlaybookIn, 'stages'>
type Row = { cid: number; stage: StageIn }
type Checked = { json: string; issues: (Issue & { cid: number | null })[]; derived: Map<number, Derived> }

const VALIDATE_DEBOUNCE_MS = 350
const META_FIELDS = ['name', 'description', 'entry', 'full_auto_default', 'default_seniority'] as const

type PlaybookTab = 'playbook' | 'stages'

/** The element, unless a `hidden` tab panel holds it (its controls cannot take focus). */
const visible = (el: HTMLElement | null) => (el !== null && el.closest('[hidden]') === null ? el : null)

/* An issue's `field` → the input that shows it: the playbook's own ids, and each stage card's
 * `stage-<index>-<suffix>` (`StageCard`). A field with no input of its own (e.g. `may_file`) falls back to the
 * card's toggle, so focus still lands on the stage at fault. */
const META_INPUTS: Record<string, string> = {
  name: 'playbook-name',
  description: 'playbook-description',
  entry: 'playbook-entry',
  full_auto_default: 'playbook-full-auto',
  default_seniority: 'playbook-seniority',
}
const STAGE_INPUTS: Record<string, readonly string[]> = {
  label: ['label'],
  key: ['key'],
  expert_kind: ['expert-kind'],
  expert: ['expert', 'expert-default'],
  deliverable: ['deliverable'],
  seniority: ['seniority'],
  created: ['created'],
  runs: ['runs'],
  depends_on: ['depends'],
  interviews: ['interviews'],
  extra_instructions: ['instructions'],
}

let nextCid = 1
const newCid = () => nextCid++

function inputFrom(meta: Meta, rows: readonly Row[]): PlaybookIn {
  return { ...meta, stages: rows.map(r => r.stage) }
}

function checkedFrom(json: string, cids: readonly number[], issues: Issue[], stages: { derived: Derived }[]): Checked {
  const derived = new Map<number, Derived>()
  stages.forEach((s, i) => cids[i] !== undefined && derived.set(cids[i], s.derived))
  return {
    json,
    issues: issues.map(i => ({ ...i, cid: i.stage_index === null ? null : cids[i.stage_index] ?? null })),
    derived,
  }
}

/** `/playbooks/new`'s starting row: unnamed, one engineer stage. Editable on the client; the server 403s a
 *  member's Save (the list offers "New playbook" only to who may create). */
function blankPlaybook(): PlaybookOut {
  return {
    id: '',
    origin: 'tenant',
    org: null,
    editable: true,
    may_duplicate: false,
    name: '',
    description: '',
    entry: 'new_task',
    full_auto_default: true,
    default_seniority: 'mid',
    stages: [],
    issues: [],
    // Server-derived facts; a blank is never offered to a task, so these are placeholders.
    runnable: false,
    not_runnable_reason: null,
    simple: false,
    default_persona: null,
    created_at: null,
    updated_at: null,
  }
}

function freshStage(rows: readonly Row[]): StageIn {
  const keys = new Set(rows.map(r => r.stage.key))
  let n = rows.length + 1
  while (keys.has(`stage_${n}`)) n++
  return {
    key: `stage_${n}`,
    label: `Stage ${n}`,
    expert: { kind: 'pinned', expert: 'engineer' },
    deliverable: 'pull_request',
    seniority: 'inherit',
    created: { kind: 'at_start' },
    runs: 'once',
    depends_on: rows.length > 0 ? { kind: 'all_earlier' } : { kind: 'none' },
    interviews: 'never',
    extra_instructions: '',
    may_file: null,
  }
}

/* THE PREVIEW CHIP STAYS BESIDE THE TRAIL: `PageHeader`'s aside row wraps, and on a phone (or a narrow pane) the
   lock and chip took a row of their own under a two-crumb trail. One line here; the trail wraps beside them. */
const TITLE_ROW_SX = { '& > [data-slot="page-header"] [data-slot="title-row"]': { flexWrap: 'nowrap' } } as const

export default function PlaybookEditPage({ mode }: { mode?: 'create' } = {}): JSX.Element {
  const creating = mode === 'create'
  const { id = '' } = useParams<{ id: string }>()
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const [confirm, confirmDialog] = useConfirm()
  const changeOrg = useChangeOrgAction('playbook')

  const query = useQuery({
    queryKey: playbookKey(id),
    queryFn: () => getPlaybook(id),
    enabled: !creating,
    retry: retryUnlessNotFound,
  })
  const vocabQuery = useQuery({ queryKey: VOCABULARY_KEY, queryFn: getVocabulary, staleTime: Infinity })
  const vocab = vocabQuery.data
  const [blank] = useState(blankPlaybook)
  const pb = creating ? blank : query.data
  const readOnly = !pb?.editable

  // Seeded once per playbook: a background refetch must never overwrite the edit in progress.
  const [meta, setMeta] = useState<Meta | null>(null)
  const [rows, setRows] = useState<Row[]>([])
  const [base, setBase] = useState<string | null>(null)
  const [checked, setChecked] = useState<Checked | null>(null)
  const [collapsed, setCollapsed] = useState<Set<number>>(new Set())
  const [previewCid, setPreviewCid] = useState<number | null>(null)
  const [formError, setFormError] = useState('')
  // A new playbook's empty name is not an error to paint until the operator has typed in it.
  const [nameTouched, setNameTouched] = useState(!creating)
  const listRef = useRef<HTMLOListElement>(null)
  const [focusAfterMove, setFocusAfterMove] = useState<{ cid: number; dir: -1 | 1 } | null>(null)

  const tabs: readonly TabDef<PlaybookTab>[] = [
    { id: 'playbook', label: 'Playbook' },
    { id: 'stages', label: `Stages (${rows.length})` },
  ]
  const [tab, selectTab] = useUrlTab(tabs, 'playbook')

  /* Focus a field once its tab is shown (and its card expanded): a control inside a `hidden` panel or a
     collapsed card cannot take focus, and selecting the tab is a router navigation that commits a render
     later. So the request — a resolver, re-run each commit — stays pending until it finds a visible control. */
  const [focusTarget, setFocusTarget] = useState<{ find: () => HTMLElement | null } | null>(null)
  useEffect(() => {
    if (focusTarget === null) return
    const el = focusTarget.find()
    if (!el) return
    el.focus()
    setFocusTarget(null)
  }, [focusTarget, tab, collapsed])

  function seed(from: PlaybookOut) {
    const input = inputOf(from)
    if (creating) input.stages = [freshStage([])]
    const seeded = input.stages.map(stage => ({ cid: newCid(), stage }))
    const { stages: _s, ...m } = input
    const json = JSON.stringify(input)
    setMeta(m)
    setRows(seeded)
    setBase(json)
    // A blank draft has no verdict yet: leaving it unchecked runs `/validate` straight away.
    setChecked(creating ? null : checkedFrom(json, seeded.map(r => r.cid), from.issues, from.stages))
    setCollapsed(new Set(seeded.length > 1 ? seeded.map(r => r.cid) : []))
  }

  useEffect(() => {
    if (pb && base === null) seed(pb)
    // seeded once per playbook, by design (above)
  }, [pb, base])

  const input = useMemo(() => (meta ? inputFrom(meta, rows) : null), [meta, rows])
  const json = useMemo(() => (input ? JSON.stringify(input) : ''), [input])
  const dirty = base !== null && json !== base

  // Re-validate each edit once it has settled; a response for a superseded draft is dropped.
  const settledJson = useSettled(json, VALIDATE_DEBOUNCE_MS)
  const latest = useRef('')
  useEffect(() => {
    // `latest` moves on EVERY draft change, before the early return: an undo back to the checked draft must
    // still drop the response for the draft it undid.
    latest.current = json
    if (settledJson !== json || !input || readOnly || checked?.json === json) return
    const cids = rows.map(r => r.cid)
    const sent = json
    validatePlaybook(input)
      .then(res => {
        if (latest.current === sent) setChecked(checkedFrom(sent, cids, res.issues, res.stages))
      })
      .catch(() => undefined) // Save re-validates server-side; a failed preview check is not an error.
    // keyed on the draft's JSON and its settled copy
  }, [json, settledJson, readOnly])

  // After a move, keep focus on the moved card's arrow (or its other arrow, once it reaches an end).
  useEffect(() => {
    if (!focusAfterMove || !listRef.current) return
    const card = listRef.current.querySelector<HTMLElement>(`[data-cid="${focusAfterMove.cid}"]`)
    const want = card?.querySelector<HTMLButtonElement>(`[data-testid$="-${focusAfterMove.dir < 0 ? 'up' : 'down'}"]`)
    const other = card?.querySelector<HTMLButtonElement>(`[data-testid$="-${focusAfterMove.dir < 0 ? 'down' : 'up'}"]`)
    ;(want && !want.disabled ? want : other)?.focus()
    setFocusAfterMove(null)
  }, [focusAfterMove, rows])

  const save = useMutation({
    mutationFn: (sent: { body: PlaybookIn; cids: number[] }) => (creating ? createPlaybook(sent.body) : updatePlaybook(id, sent.body)),
    onSuccess: saved => {
      queryClient.setQueryData(playbookKey(saved.id), saved)
      void queryClient.invalidateQueries({ queryKey: PLAYBOOKS_KEY })
      navigate('/playbooks')
    },
    onError: (err, sent) => {
      const issues = err instanceof ApiError && err.status === 422 ? issuesOf(err.body) : null
      if (issues) {
        const prev = checked?.derived
        const placed = checkedFrom(JSON.stringify(sent.body), sent.cids, issues, [])
        setChecked({ ...placed, derived: prev ?? new Map() })
        focusIssue(placed.issues, sent.cids)
      } else {
        setFormError(apiDetail(err, 'Could not save the playbook.'))
      }
    },
  })

  const duplicate = useMutation({
    mutationFn: () => duplicatePlaybook(id),
    onSuccess: copy => {
      void queryClient.invalidateQueries({ queryKey: PLAYBOOKS_KEY })
      navigate(`/playbooks/${copy.id}`)
    },
    onError: err => setFormError(apiDetail(err, 'Could not duplicate the playbook.')),
  })

  const destroy = useMutation({
    mutationFn: () => deletePlaybook(id),
    onSuccess: () => {
      // Leave first: removing the query under its mounted observer would refetch the deleted row.
      navigate('/playbooks')
      queryClient.removeQueries({ queryKey: playbookKey(id) })
      void queryClient.invalidateQueries({ queryKey: PLAYBOOKS_KEY })
    },
    onError: err => setFormError(apiDetail(err, 'Could not delete the playbook.')),
  })

  // A new id (Duplicate navigates here) re-seeds from the new row.
  const seededFor = useRef(id)
  useEffect(() => {
    if (seededFor.current !== id) {
      seededFor.current = id
      setBase(null)
      setChecked(null)
      setFormError('')
    }
  }, [id])

  const notFound = isNotFound(query.error)
  if (notFound || !pb || !meta || !input) {
    return (
      <DetailStatus
        trail={<Breadcrumb items={[{ label: 'Playbooks', to: '/playbooks' }, { label: 'Playbook' }]} />}
        loading={!query.isError}
        notFound={notFound && 'Playbook not found'}
        error="Could not load this playbook."
      />
    )
  }

  const builtIn = pb.origin === 'built_in'
  const issues = checked?.issues ?? []
  const current = checked?.json === json
  const hasErrors = issues.some(i => i.severity === 'error')
  const topIssues = issues.filter(i => i.cid === null && !(META_FIELDS as readonly string[]).includes(i.field ?? ''))
  const metaError = (field: string) =>
    field === 'name' && !nameTouched ? undefined : issues.filter(i => i.cid === null && i.field === field && i.severity === 'error').map(i => i.message).join(' ') || undefined
  const derivedOf = (cid: number) => checked?.derived.get(cid)
  const previewRow = rows.find(r => r.cid === previewCid)

  /* THE FIRST ERROR WITH A PLACE TO GO: select its tab, expand its card, focus its field. */
  function focusIssue(placed: Checked['issues'], cids: readonly number[]) {
    for (const issue of placed) {
      if (issue.severity !== 'error') continue
      if (issue.cid === null) {
        const inputId = META_INPUTS[issue.field ?? '']
        if (!inputId) continue
        selectTab('playbook')
        setFocusTarget({ find: () => visible(document.getElementById(inputId)) })
        return
      }
      if (!cids.includes(issue.cid)) continue
      const cid = issue.cid
      selectTab('stages')
      setCollapsed(c => {
        const next = new Set(c)
        next.delete(cid)
        return next
      })
      // Found by cid and its index read at focus time: a reorder while the Save was in flight moves the index.
      const suffixes = STAGE_INPUTS[issue.field ?? ''] ?? []
      setFocusTarget({
        find: () => {
          const card = document.querySelector<HTMLElement>(`[data-cid="${cid}"]`)
          if (!card) return null
          const index = card.dataset.index
          const ids = suffixes.map(suffix => `stage-${index}-${suffix}`)
          const field = ids.map(id => card.querySelector<HTMLElement>(`[id="${id}"]`)).find(found => found !== null)
          return visible(field ?? card.querySelector<HTMLElement>(`[data-testid="stage-${index}-toggle"]`))
        },
      })
      return
    }
  }

  function patchMeta(p: Partial<Meta>) {
    setMeta(m => (m ? { ...m, ...p } : m))
  }
  function setStage(cid: number, stage: StageIn) {
    setRows(rs => rs.map(r => (r.cid === cid ? { ...r, stage } : r)))
  }
  function move(cid: number, dir: -1 | 1) {
    setRows(rs => {
      const i = rs.findIndex(r => r.cid === cid)
      const j = i + dir
      if (i < 0 || j < 0 || j >= rs.length) return rs
      const next = [...rs]
      ;[next[i], next[j]] = [next[j], next[i]]
      return next
    })
    setFocusAfterMove({ cid, dir })
  }
  function remove(cid: number) {
    setRows(rs => rs.filter(r => r.cid !== cid))
  }
  function add() {
    const cid = newCid()
    setRows(rs => [...rs, { cid, stage: freshStage(rs) }])
    setCollapsed(c => {
      const next = new Set(c)
      next.delete(cid)
      return next
    })
  }
  function toggle(cid: number) {
    setCollapsed(c => {
      const next = new Set(c)
      if (next.has(cid)) next.delete(cid)
      else next.add(cid)
      return next
    })
  }
  const allCollapsed = rows.length > 0 && rows.every(r => collapsed.has(r.cid))

  function onSubmit(e: FormEvent) {
    e.preventDefault()
    if (readOnly || !input) return
    setFormError('')
    setNameTouched(true)
    if (!dirty && !creating) {
      navigate('/playbooks')
      return
    }
    save.mutate({ body: input, cids: rows.map(r => r.cid) })
  }

  async function onDelete() {
    if (!pb) return
    const ok = await confirm({
      title: 'Delete playbook?',
      message: 'This cannot be undone.',
      identity: pb.name,
      confirmLabel: 'Delete',
      destructive: true,
    })
    if (ok) destroy.mutate()
  }

  const busy = save.isPending || duplicate.isPending || destroy.isPending
  /* A built-in belongs to no org; another org's copy (read-only here) still moves. */
  const changeOrgButton = creating || !pb.org
    ? null
    : changeOrg.button({ id: pb.id, name: pb.name, org: pb.org, href: `/playbooks/${pb.id}` })
  const headerActions = creating ? undefined : builtIn ? (
    pb.may_duplicate ? (
      <Button variant="contained" onClick={() => duplicate.mutate()} disabled={busy} data-testid="playbook-edit-duplicate" sx={tapTargetSx}>
        Duplicate
      </Button>
    ) : undefined
  ) : pb.editable ? (
    <Box sx={{ display: 'flex', gap: 1, flexWrap: 'wrap' }}>
      {/* Duplicate copies the SAVED row: with unsaved edits it would silently drop them, so it waits for Save. */}
      <Button
        variant="outlined"
        onClick={() => duplicate.mutate()}
        disabled={busy || dirty}
        title={dirty ? 'Save or cancel your changes first' : undefined}
        data-testid="playbook-edit-duplicate"
        sx={tapTargetSx}
      >
        Duplicate
      </Button>
      {changeOrgButton}
      <Button variant="outlined" color="error" onClick={() => void onDelete()} disabled={busy} data-testid="playbook-edit-delete" sx={tapTargetSx}>
        Delete
      </Button>
    </Box>
  ) : changeOrgButton ?? undefined

  const readOnlyNote = builtIn
    ? 'Built-in playbooks are read-only. Duplicate it to edit a copy.'
    : 'You can view this playbook but not edit it.'

  return (
    <DetailPage
      breadcrumb={<Breadcrumb items={[{ label: 'Playbooks', to: '/playbooks' }, { label: creating ? 'New playbook' : pb.name }]} />}
      titleAside={
        <Box sx={{ display: 'flex', alignItems: 'center', gap: 0.75, flexShrink: 0 }} data-testid="playbook-edit-aside">
          {builtIn && <LockGlyph />}
          <StatusChip label="Preview" tone="info" />
        </Box>
      }
      actions={headerActions}
      testId="playbook-edit-page"
      sx={TITLE_ROW_SX}
    >
      <Box component="form" noValidate onSubmit={onSubmit} data-testid="playbook-edit-form" sx={{ display: 'grid', gap: 2.5, mt: 1, minWidth: 0 }}>
        {formError && <Banner severity="error">{formError}</Banner>}
        {readOnly && <Banner severity="info">{readOnlyNote}</Banner>}
        {topIssues.length > 0 && (
          <Box sx={{ display: 'grid', gap: 0.75 }} data-testid="playbook-issues">
            {topIssues.map((i, k) => (
              <Banner key={k} severity={i.severity === 'error' ? 'error' : 'warning'}>{i.message}</Banner>
            ))}
          </Box>
        )}

        <Tabs tabs={tabs} tab={tab} onSelect={selectTab} label="Playbook sections" idPrefix="playbook" />

        <TabPanel id="playbook" current={tab} idPrefix="playbook">
          <Box sx={{ display: 'grid', gap: 2.5, minWidth: 0 }}>
            <DetailSection title="Playbook">
              <Box
                sx={{
                  display: 'grid',
                  gap: 1.5,
                  gridTemplateColumns: 'repeat(auto-fill, minmax(15rem, 1fr))',
                  [PHONE]: { gridTemplateColumns: '1fr' },
                }}
              >
                <FormTextField
                  id="playbook-name"
                  label="Name"
                  required
                  error={metaError('name')}
                  value={meta.name}
                  autoFocus={creating}
                  onBlur={() => setNameTouched(true)}
                  onChange={v => {
                    setNameTouched(true)
                    patchMeta({ name: v })
                  }}
                  testId="playbook-edit-name" slotProps={{ htmlInput: { readOnly } }}
                />
                {selectField('playbook-entry', 'Starts from', meta.entry, ['new_task', 'new_chat'], 'entry', v => patchMeta({ entry: v }))}
                {selectField('playbook-full-auto', 'Full Auto', meta.full_auto_default ? 'on' : 'off', ['on', 'off'], 'full_auto',
                  v => patchMeta({ full_auto_default: v === 'on' }), 'full_auto_default')}
                {selectField('playbook-seniority', 'Default seniority', meta.default_seniority, ['junior', 'mid', 'senior'], 'seniority',
                  v => patchMeta({ default_seniority: v }), 'default_seniority')}
                <Box sx={{ gridColumn: '1 / -1' }}>
                  <FormTextField
                    id="playbook-description" label="Description"
                    error={metaError('description')}
                    multiline
                    minRows={2}
                    value={meta.description}
                    onChange={v => patchMeta({ description: v })}
                    testId="playbook-edit-description" slotProps={{ htmlInput: { readOnly } }}
                  />
                </Box>
              </Box>
            </DetailSection>

            <DetailSection title="Chain">
              <ChainStrip stages={rows.map(r => r.stage)} vocab={vocab} />
            </DetailSection>
          </Box>
        </TabPanel>

        <TabPanel id="stages" current={tab} idPrefix="playbook">
          <DetailSection title={`Stages (${rows.length})`}>
            {rows.length > 1 && (
              <Box sx={{ mt: -1 }}>
                <Button
                  type="button"
                  size="small"
                  onClick={() => setCollapsed(allCollapsed ? new Set() : new Set(rows.map(r => r.cid)))}
                  data-testid="stages-toggle-all"
                  sx={tapTargetSx}
                >
                  {allCollapsed ? 'Expand all' : 'Collapse all'}
                </Button>
              </Box>
            )}
            <Box component="ol" ref={listRef} sx={{ m: 0, p: 0, display: 'grid', gap: 1.25, minWidth: 0 }} data-testid="stage-list">
              {rows.map((r, i) => (
                  <StageCard
                    key={r.cid}
                    cid={r.cid}
                    stage={r.stage}
                    index={i}
                    count={rows.length}
                    stages={rows.map(x => x.stage)}
                    derived={derivedOf(r.cid)}
                    issues={issues.filter(x => x.cid === r.cid)}
                    vocab={vocab}
                    readOnly={readOnly}
                    collapsed={collapsed.has(r.cid)}
                    onToggle={() => toggle(r.cid)}
                    onChange={s => setStage(r.cid, s)}
                    onMove={dir => move(r.cid, dir)}
                    onRemove={() => remove(r.cid)}
                    onPreview={() => setPreviewCid(r.cid)}
                  />
              ))}
            </Box>
            {!readOnly && (
              <Box>
                <Button type="button" variant="outlined" onClick={add} data-testid="stage-add" sx={tapTargetSx}>
                  Add stage
                </Button>
              </Box>
            )}
          </DetailSection>
        </TabPanel>

        <FormActionBar testId="playbook-form-actions" stretchTestIds={['playbook-edit-save']}>
          <Button component={RouterLink} to="/playbooks" color="inherit" data-testid="playbook-edit-cancel" sx={tapTargetSx}>
            {readOnly ? 'Back' : 'Cancel'}
          </Button>
          {!readOnly && (
            <SubmitButton
              testId="playbook-edit-save"
              busy={save.isPending}
              // Errors block Save; warnings do not (D7). A stale check with no errors lets Save through: the
              // server re-validates and a refusal lands on the cards the same way.
              disabled={busy || hasErrors}
              data-checked={current ? 'current' : 'stale'}
            >
              {creating ? 'Create' : 'Save'}
            </SubmitButton>
          )}
        </FormActionBar>
      </Box>

      {previewRow && (
        <PromptPreviewDialog playbook={input} stage={previewRow.stage} vocab={vocab} onClose={() => setPreviewCid(null)} />
      )}
      {confirmDialog}
      {changeOrg.dialog}
    </DetailPage>
  )

  function selectField(
    fieldId: string,
    label: string,
    value: string,
    keys: readonly string[],
    labelField: string,
    onPick: (v: string) => void,
    issueField: string = labelField,
  ) {
    return (
      <FormField label={label} error={metaError(issueField)}>
        <TextField
          id={fieldId}
          select={!readOnly}
          sx={[fieldTapTargetSx, SELECT_WRAP_SX]}
          value={readOnly ? withoutFieldWords(labelOf(vocab, labelField, value), label) : value}
          onChange={e => onPick(e.target.value)}
          slotProps={{ htmlInput: { readOnly, 'data-testid': fieldId } }}
        >
          {!readOnly && keys.map(k => <MenuItem key={k} value={k}>{withoutFieldWords(labelOf(vocab, labelField, k), label)}</MenuItem>)}
        </TextField>
      </FormField>
    )
  }
}
