import { type JSX, useEffect, useMemo, useRef, useState } from 'react'
import { useNavigate, useParams } from 'react-router-dom'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import TextField from '@mui/material/TextField'
import { ApiError, apiDetail } from '../../api'
import Breadcrumb from '../../components/Breadcrumb'
import { TabPanel, Tabs, type TabDef } from '../../components/Tabs'
import { useChangeOrgAction } from '../../components/ChangeOrgDialog'
import { useBaselineDirty } from '../../hooks/useBaselineDirty'
import { useLeaveGuard } from '../../hooks/useLeaveGuard'
import { useEditRecord } from '../../hooks/useEditRecord'
import { useSubmit } from '../../hooks/useSubmit'
import { useSettled } from '../../hooks/useSettled'
import { useUrlTab } from '../../hooks/useUrlTab'
import Banner from '../../ui/Banner'
import { DetailPage, DetailSection } from '../../ui/detail/DetailPage'
import { EditStatus } from '../../ui/detail/DetailStatus'
import CancelSaveBar from '../../ui/CancelSaveBar'
import FormField from '../../ui/FormField'
import FormBox from '../../ui/FormBox'
import SelectField from '../../ui/SelectField'
import type { Explain, HelpProp } from '../../ui/explain'
import { EntityDescriptionField, EntityNameField } from '../../ui/EntityNameFields'
import { LeaveGuardDialog } from '../../ui/UnsavedChangesDialog'
import { PHONE, fieldTapTargetSx, tapTargetSx } from '../../ui/listStyles'
import ChainStrip from './ChainStrip'
import PromptPreviewDialog from './PromptPreviewDialog'
import StageCard, { withoutFieldWords } from './StageCard'
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
  updatePlaybook,
  validatePlaybook,
} from './api'
import { grid } from '../../theme'
import Tip from '../../ui/Tip'

/* The playbook fields' explanations (`ui/HelpLabel`): the engine reads them in `apps/api/app/playbook_engine.py`
   and `playbook_prompt.py`. */
const FULL_AUTO_HELP: Explain = {
  tip: 'Default for whether stages may stop to ask questions.',
  more: 'Only stages whose Asks questions is When Full Auto is off follow it; stages set to Never asks always run Full Auto.',
  docs: 'console-reference/automation/playbooks',
}
/** What picking each option does: a line of its select's pop-up, keyed by the vocabulary key. */
const META_OPTION_HELP: Record<string, Record<string, string>> = {
  full_auto: { on: 'Stages run without stopping to ask.', off: 'Stages set to ask may stop for your answer.' },
  seniority: { junior: 'Low model tier.', mid: 'Standard model tier.', senior: 'High model tier.' },
}

/* THE PLAYBOOK EDITOR — `/playbooks/:id`, and `/playbooks/new` (`mode="create"`: a blank draft, one stage, that
 * Save POSTs), on `FeatureEditPage`'s pattern: the form is seeded ONCE from the
 * fetched row, and Save sends only when the draft differs from it (`useBaselineDirty`) (the `PUT` is a full replace, so
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
const PAGE_SX = { '& > [data-slot="page-header"] [data-slot="title-row"]': { flexWrap: 'nowrap' } } as const
/* THE STAGES TAB TAKES THE FULL WIDTH: a stage card's facts are a five-across grid, and in a maximised pane the `read`
   cap stopped every card well short of the right edge. The Playbook tab's prose fields keep the reading measure. */
const STAGES_PAGE_SX = [PAGE_SX, { maxWidth: 'none' }] as const

export default function PlaybookEditPage({ mode }: { mode?: 'create' } = {}): JSX.Element {
  const creating = mode === 'create'
  const { id = '' } = useParams<{ id: string }>()
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const vocabQuery = useQuery({ queryKey: VOCABULARY_KEY, queryFn: getVocabulary, staleTime: Infinity })
  const vocab = vocabQuery.data
  const [blank] = useState(blankPlaybook)
  const [loaded, setLoaded] = useState<PlaybookOut | null>(null)
  // A move changes `org` and `editable` on the row; refresh the row alone, never the draft (`onLoad` would re-seed).
  const changeOrg = useChangeOrgAction('playbook', { onMoved: () => { if (!creating) void getPlaybook(id).then(setLoaded) } })
  const pb = creating ? blank : loaded
  const readOnly = !pb?.editable

  // Seeded once per playbook: a background refetch must never overwrite the edit in progress.
  const [meta, setMeta] = useState<Meta | null>(null)
  const [rows, setRows] = useState<Row[]>([])
  const [checked, setChecked] = useState<Checked | null>(null)
  const [collapsed, setCollapsed] = useState<Set<number>>(new Set())
  const [previewCid, setPreviewCid] = useState<number | null>(null)
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
    // A blank draft has no verdict yet: leaving it unchecked runs `/validate` straight away.
    setChecked(creating ? null : checkedFrom(json, seeded.map(r => r.cid), from.issues, from.stages))
    setCollapsed(new Set(seeded.length > 1 ? seeded.map(r => r.cid) : []))
  }

  useEffect(() => {
    if (creating && meta === null) seed(blank)
    // seeded once, by design (above)
  }, [creating, blank, meta])

  const input = useMemo(() => (meta ? inputFrom(meta, rows) : null), [meta, rows])
  const json = useMemo(() => (input ? JSON.stringify(input) : ''), [input])
  const { dirty, reset: forgetBase } = useBaselineDirty(input ? json : null)

  /* Save and Duplicate are each a `useSubmit` under one banner (Save's refusal first); each clears its own on a new
     attempt, and a load or a Save clears both. */
  const save = useSubmit()
  const duplicate = useSubmit()
  const formError = save.formError || duplicate.formError

  /* A new id (Duplicate navigates here) re-reads and re-seeds from the new row: the baseline moves with it
     (`forgetBase` in the same batch as the new draft). Delete asks, removes, and returns to the list. */
  const { loading, notFound, loadError, confirmDialog, deleting, deleteError, confirmAndDelete } = useEditRecord(
    creating ? null : id, getPlaybook,
    row => {
      setLoaded(row)
      seed(row)
      forgetBase()
      save.setFormError('')
      duplicate.setFormError('')
    },
    { loadFallback: 'Could not load this playbook.', fixedLoadError: true, listTo: '/playbooks', deleteFallback: 'Could not delete the playbook.' },
  )

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

  /* A 422 carrying issues is placed on the stages and fields it names (no banner); any other refusal is the banner.
     Resolves true once the write landed; where to go after is the caller's (the form's Save, the leave dialog's). */
  const saveDraft = async (sent: { body: PlaybookIn; cids: number[] }): Promise<boolean> => {
    const landed = await save.run(
      'Could not save the playbook.',
      async () => {
        await (creating ? createPlaybook(sent.body) : updatePlaybook(id, sent.body))
        void queryClient.invalidateQueries({ queryKey: PLAYBOOKS_KEY })
      },
      { mapError: (err, fallback) => {
        const issues = err instanceof ApiError && err.status === 422 ? issuesOf(err.body) : null
        if (!issues) return apiDetail(err, fallback)
        const prev = checked?.derived
        const placed = checkedFrom(JSON.stringify(sent.body), sent.cids, issues, [])
        setChecked({ ...placed, derived: prev ?? new Map() })
        focusIssue(placed.issues, sent.cids)
        return ''
      } },
    )
    return landed !== null
  }

  const duplicateSaved = () => duplicate.run('Could not duplicate the playbook.', async () => {
    const copy = await duplicatePlaybook(id)
    void queryClient.invalidateQueries({ queryKey: PLAYBOOKS_KEY })
    navigate(`/playbooks/${copy.id}`)
  })

  /* The form's Save, and the leave dialog's: true once there is nothing left to write (saved, or an unchanged edit). */
  async function submitDraft(): Promise<boolean> {
    if (readOnly || !input) return false
    save.setFormError('')
    duplicate.setFormError('')
    setNameTouched(true)
    if (!dirty && !creating) return true
    return saveDraft({ body: input, cids: rows.map(r => r.cid) })
  }
  /* Leaving with unsaved edits asks first (`hooks/useLeaveGuard`); a delete leaves on its own (`useEditRecord`). */
  const guard = useLeaveGuard(dirty && !readOnly && !deleting, submitDraft)

  if (loading || notFound || loadError || !pb || !meta || !input) {
    return (
      <EditStatus
        trail={<Breadcrumb items={[{ label: 'Playbooks', to: '/playbooks' }, { label: 'Playbook' }]} />}
        loading={loading} notFound={notFound} loadError={loadError} notFoundTitle="Playbook not found"
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

  const onSubmit = async () => { if (await submitDraft()) guard.leaveNow('/playbooks') }

  const onDelete = () => confirmAndDelete(
    { title: 'Delete playbook?', message: 'This cannot be undone.', identity: pb.name, confirmLabel: 'Delete', destructive: true },
    async () => {
      await deletePlaybook(id)
      void queryClient.invalidateQueries({ queryKey: PLAYBOOKS_KEY })
    },
  )

  const busy = save.busy || duplicate.busy || deleting
  /* A built-in belongs to no org; another org's copy (read-only here) still moves. */
  const changeOrgButton = creating || !pb.org
    ? null
    : changeOrg.button({ id: pb.id, name: pb.name, org: pb.org, href: `/playbooks/${pb.id}` })
  const headerActions = creating ? undefined : builtIn ? (
    pb.may_duplicate ? (
      <Button variant="contained" onClick={() => void duplicateSaved()} disabled={busy} data-testid="playbook-edit-duplicate" sx={tapTargetSx}>
        Duplicate
      </Button>
    ) : undefined
  ) : pb.editable ? (
    <Box sx={{ display: 'flex', gap: 1, flexWrap: 'wrap' }}>
      {/* Duplicate copies the SAVED row: with unsaved edits it would silently drop them, so it waits for Save. */}
      <Tip title={dirty ? 'Save or cancel your changes first' : undefined}><Button
        variant="outlined"
        onClick={() => void duplicateSaved()}
        disabled={busy || dirty}
        data-testid="playbook-edit-duplicate"
        sx={tapTargetSx}
      >
        Duplicate
      </Button></Tip>
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
        <Box sx={{ display: 'flex', alignItems: 'center', gap: 0.5, flexShrink: 0 }} data-testid="playbook-edit-aside">
          {builtIn && <LockGlyph />}
          <StatusChip label="Preview" tone="info" />
        </Box>
      }
      actions={headerActions}
      testId="playbook-edit-page"
      sx={tab === 'stages' ? STAGES_PAGE_SX : PAGE_SX}
    >
      <FormBox onSubmit={onSubmit} data-testid="playbook-edit-form" sx={{ display: 'grid', gap: 2.5, mt: 1, minWidth: 0 }}>
        {(formError || deleteError) && <Banner severity="error">{formError || deleteError}</Banner>}
        {readOnly && <Banner severity="info">{readOnlyNote}</Banner>}
        {topIssues.length > 0 && (
          <Box sx={{ display: 'grid', gap: 0.5 }} data-testid="playbook-issues">
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
                  gridTemplateColumns: `repeat(auto-fill, minmax(${grid(40)}, 1fr))`,
                  [PHONE]: { gridTemplateColumns: '1fr' },
                }}
              >
                <EntityNameField
                  id="playbook-name"
                  error={metaError('name')}
                  value={meta.name}
                  autoFocus={creating}
                  onBlur={() => setNameTouched(true)}
                  onChange={v => {
                    setNameTouched(true)
                    patchMeta({ name: v })
                  }}
                  testId="playbook-edit-name" slotProps={{ htmlInput: { readOnly } }}
                  help="Shown in the new task's Playbook menu; unique per organization."
                />
                {selectField('playbook-entry', 'Starts from', meta.entry, ['new_task', 'new_chat'], 'entry', v => patchMeta({ entry: v }),
                  undefined, "Where it's started: New Task or New Chat.")}
                {selectField('playbook-full-auto', 'Full Auto', meta.full_auto_default ? 'on' : 'off', ['on', 'off'], 'full_auto',
                  v => patchMeta({ full_auto_default: v === 'on' }), 'full_auto_default', FULL_AUTO_HELP)}
                {selectField('playbook-seniority', 'Default seniority', meta.default_seniority, ['junior', 'mid', 'senior'], 'seniority',
                  v => patchMeta({ default_seniority: v }), 'default_seniority',
                  'Model tier for every stage left at Playbook default.')}
                <Box sx={{ gridColumn: '1 / -1' }}>
                  <EntityDescriptionField
                    id="playbook-description"
                    error={metaError('description')}
                    value={meta.description}
                    onChange={v => patchMeta({ description: v })}
                    testId="playbook-edit-description" slotProps={{ htmlInput: { readOnly } }}
                    help="Shown under the name in the new task's Playbook menu."
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
            <Box component="ol" ref={listRef} sx={{ m: 0, p: 0, display: 'grid', gap: 1, minWidth: 0 }} data-testid="stage-list">
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

        {/* Errors block Save; warnings do not (D7). A stale check with no errors lets Save through: the server
            re-validates and a refusal lands on the cards the same way. */}
        <CancelSaveBar testId="playbook-form-actions" saveTestId="playbook-edit-save" cancelTo="/playbooks"
          creating={creating} busy={save.busy} disabled={busy || hasErrors} readOnly={readOnly}
          saveProps={{ 'data-checked': current ? 'current' : 'stale' }} />
      </FormBox>

      {previewRow && (
        <PromptPreviewDialog playbook={input} stage={previewRow.stage} vocab={vocab} onClose={() => setPreviewCid(null)} />
      )}
      {confirmDialog}
      {changeOrg.dialog}
      <LeaveGuardDialog leave={guard} message="This playbook has changes that have not been saved. Save them before leaving?" />
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
    help?: HelpProp,
  ) {
    const optionHelp = META_OPTION_HELP[labelField] ?? {}
    const choices = keys.flatMap(k =>
      optionHelp[k] ? [{ label: withoutFieldWords(labelOf(vocab, labelField, k), label), help: optionHelp[k] }] : [])
    return (
      <FormField label={label} help={help} choices={choices} error={metaError(issueField)}>
        {readOnly ? (
          <TextField
            id={fieldId}
            sx={fieldTapTargetSx}
            value={withoutFieldWords(labelOf(vocab, labelField, value), label)}
            slotProps={{ htmlInput: { readOnly, 'data-testid': fieldId } }}
          />
        ) : (
          <SelectField
            id={fieldId}
            value={value}
            onChange={onPick}
            testId={fieldId}
            options={keys.map(k => ({ value: k, label: withoutFieldWords(labelOf(vocab, labelField, k), label) }))}
          >
            {/* A stored value outside the vocabulary reads blank, as before, never as the first option. */}
            {!keys.includes(value) && <option value="" disabled />}
          </SelectField>
        )}
      </FormField>
    )
  }
}
