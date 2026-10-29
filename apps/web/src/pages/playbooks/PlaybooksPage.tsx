import type { JSX } from 'react'
import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import Box from '@mui/material/Box'
import { apiDetail } from '../../api'
import { useAuth } from '../../auth'
import { PHONE, PHONE_STATUS_LINE_SX } from '../../ui/listStyles'
import { FilterRow, FilterSearch, matchesAny } from '../../ui/FilterRow'
import { useConfirm } from '../../components/ConfirmDialog'
import { useChangeOrgAction } from '../../components/ChangeOrgDialog'
import PaneModeToggle from '../../components/tasks/pane/PaneModeToggle'
import { loadListView, saveListView, usePersistedListView } from '../../lib/listViewStorage'
import { buildNameTree, type NameTreeKind } from '../../lib/nameTree'
import { NO_REFILING_DEPS, nameTreeRows } from '../../lib/nameTreeRows'
import { countOf } from '../../lib/humanize'
import { absoluteTime, compactAge } from '../../lib/relativeTime'
import Banner from '../../ui/Banner'
import EmptyState from '../../ui/EmptyState'
import NoMatchesState from '../../ui/NoMatchesState'
import ListStates from '../../ui/feed/ListStates'
import type { FeedIndicator, FeedRowSpec, FeedSection, OverflowItem } from '../../ui/feed/types'
import { deleteItem, editItem } from '../../components/tasks/rowActions'
import FilterBar from '../../ui/FilterBar'
import NewButton from '../../ui/NewButton'
import { orgSlots, useOrgVisible } from '../../ui/OrgIndicator'
import PageHeader from '../../ui/PageHeader'
import StatusChip from '../../ui/StatusChip'
import {
  ALL_ORGS_PLAYBOOKS_KEY,
  PLAYBOOKS_KEY,
  type PlaybookOut,
  deletePlaybook,
  duplicatePlaybook,
  listAllOrgsPlaybooks,
  listPlaybooks,
} from './api'

/* THE PLAYBOOKS LIST — `/playbooks`. The org's own playbooks run from the task form's Playbook depth while the
 * org's plan has Playbooks on (`apps/api/app/playbook_engine.py`); the built-ins are what the standard
 * depths already do.
 * THE STANDARD LIST SHAPE, SHARED WITH AUTOMATIONS (`pages/automations/AutomationList.tsx`): a titleless `PageHeader`
 * (the Automation tab names the page; its actions portal onto that strip) whose action cluster is the shared Flat/Tree toggle (`components/tasks/pane/PaneModeToggle.tsx`), then the wordless ＋ New playbook
 * (`ui/NewButton`, to `/playbooks/new`) — and one filter strip below it: Search (name and description). An
 * operator's org narrowing is the GLOBAL org filter's (`lib/orgScope.ts`, served server-side), never a
 * page-level Org select — one decision, one control. The view persists per viewer (`lib/listViewStorage.ts`).
 * One `ui/feed/FeedList` in sections — the four built-ins (locked: Open, Duplicate), the caller org's own
 * (Edit, Duplicate, Delete), and for an operator every OTHER org's (`?all_orgs=true`; Open only — the API
 * keeps every mutation own-org) — every verb in the row's ⋯ menu. Tree mode draws `/`-named copies as
 * folders within each section; it re-files nothing (a playbook rename is a whole-body save). Every org
 * member reads; an Org Owner/Admin mutates (`routes/playbooks.py::_may_edit`, served per row as
 * `may_duplicate` and `editable`) — a member sees no mutation control. The row's mark is the server's own
 * validity verdict (`issues`); the full description hangs under the name, wrapping, never truncated.
 * Duplicate opens the new copy's editor. */

export const LockGlyph = () => (
  <Box
    component="span"
    role="img"
    aria-label="Locked"
    title="Built-in: duplicate it to edit a copy"
    data-testid="playbook-lock"
    sx={{ display: 'inline-flex', color: 'text.secondary', alignSelf: 'center' }}
  >
    <svg width="14" height="14" viewBox="0 0 16 16" aria-hidden="true" focusable="false">
      <rect x="3" y="7" width="10" height="8" rx="1.5" fill="currentColor" />
      <path d="M5 7V5a3 3 0 0 1 6 0v2" fill="none" stroke="currentColor" strokeWidth="1.6" />
    </svg>
  </Box>
)

/** The row's mark: the server's validation verdict on the stored playbook (errors, then warnings). */
function validityMark(pb: PlaybookOut): FeedIndicator {
  const errors = pb.issues.filter(i => i.severity === 'error').length
  const warnings = pb.issues.length - errors
  if (errors > 0) {
    return { tone: 'error', glyph: '✕', label: countOf(errors, 'error'), data: { validity: 'error' } }
  }
  if (warnings > 0) {
    return { tone: 'warning', glyph: '!', label: countOf(warnings, 'warning'), data: { validity: 'warning' } }
  }
  return { tone: 'success', glyph: '✓', label: 'valid', data: { validity: 'valid' } }
}

const ENTRY_LABEL: Record<string, string> = { new_task: 'New task', new_chat: 'New chat' }

/** Tree mode's vocabulary; `nameMax` only bounds drags, and this roster offers none. */
const PLAYBOOK_TREE_KIND: NameTreeKind = { noun: { one: 'playbook', many: 'playbooks' }, nameMax: null, idPrefix: 'playbook' }

export default function PlaybooksPage(): JSX.Element {
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const [confirm, confirmDialog] = useConfirm()
  const changeOrg = useChangeOrgAction('playbook')
  const [error, setError] = useState('')
  const [busyId, setBusyId] = useState<string | null>(null)
  const { admin } = useAuth()
  const orgVisible = useOrgVisible()
  /* An operator reads every org's copies; anyone else the task form's own-org list (one cache entry, the
     server would ignore the flag for them anyway). */
  const list = useQuery<PlaybookOut[], Error, PlaybookOut[], readonly string[]>({
    queryKey: orgVisible ? ALL_ORGS_PLAYBOOKS_KEY : PLAYBOOKS_KEY,
    queryFn: orgVisible ? listAllOrgsPlaybooks : listPlaybooks,
  })

  /* ── The view (search, Flat/Tree, open folders), restored at mount and written through on each change. */
  const { view, patchView, toggleGroup } = usePersistedListView(
    () => loadListView('playbooks', admin?.id ?? null),
    next => saveListView('playbooks', admin?.id ?? null, next),
  )

  const duplicate = useMutation({
    mutationFn: duplicatePlaybook,
    onMutate: id => setBusyId(id),
    onSettled: () => setBusyId(null),
    onSuccess: copy => {
      void queryClient.invalidateQueries({ queryKey: PLAYBOOKS_KEY })
      navigate(`/playbooks/${copy.id}`)
    },
    onError: err => setError(apiDetail(err, 'Could not duplicate the playbook.')),
  })
  const remove = useMutation({
    mutationFn: deletePlaybook,
    onMutate: id => setBusyId(id),
    onSettled: () => setBusyId(null),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: PLAYBOOKS_KEY }),
    onError: err => setError(apiDetail(err, 'Could not delete the playbook.')),
  })

  async function onDelete(pb: PlaybookOut) {
    const ok = await confirm({
      title: 'Delete playbook?',
      message: 'This cannot be undone.',
      identity: pb.name,
      confirmLabel: 'Delete',
      destructive: true,
    })
    if (ok) remove.mutate(pb.id)
  }

  const rows = list.data ?? []
  // `may_duplicate` is the caller's `_may_edit` on every row of their own — and the four built-ins always load.
  const mayCreate = rows.some(pb => pb.may_duplicate)
  const copies = rows.filter(pb => pb.origin !== 'built_in')
  /* Search is the page's only narrowing: the org axis is the global filter's, applied by the server before
     the list arrives (a built-in belongs to no org and is served under any scope — every org runs it). */
  const filtering = view.q.trim() !== ''
  const shown = rows.filter(pb => matchesAny(view.q, pb.name, pb.description))
  const builtIns = shown.filter(pb => pb.origin === 'built_in')
  /* The caller org's own copies; an operator's other orgs' are their own section, told apart by the copy's org
     against the viewer's (`auth.tsx::Admin.org`). An unbound viewer org reads every copy as its own. */
  const ownOrgId = admin?.org?.id ?? null
  const isOwn = (pb: PlaybookOut) => !orgVisible || ownOrgId === null || pb.org?.id === ownOrgId
  const own = shown.filter(pb => pb.origin !== 'built_in' && isOwn(pb))
  const others = shown.filter(pb => pb.origin !== 'built_in' && !isOwn(pb))
  const ownTotal = copies.filter(isOwn).length
  const treeMode = view.mode === 'tree'

  function actionsOf(pb: PlaybookOut): OverflowItem[] | undefined {
    /* A built-in belongs to no org, so it has none to change; another org's copy (read-only here) still moves. */
    const changeOrgItem = pb.org
      ? changeOrg.menuItem({ id: pb.id, name: pb.name, org: pb.org, href: `/playbooks/${pb.id}` })
      : null
    if (!pb.may_duplicate) return changeOrgItem ? [changeOrgItem] : undefined
    const items: OverflowItem[] = [
      pb.editable
        ? editItem(pb.name, { to: `/playbooks/${pb.id}` })
        : { key: 'open', icon: '→', label: 'Open', ariaLabel: `Open ${pb.name}`, to: `/playbooks/${pb.id}` },
      { key: 'duplicate', icon: '⧉', label: 'Duplicate', ariaLabel: `Duplicate ${pb.name}`, onClick: () => duplicate.mutate(pb.id) },
    ]
    if (changeOrgItem) items.push(changeOrgItem)
    if (pb.editable) {
      items.push(deleteItem(pb.name, () => void onDelete(pb)))
    }
    return items
  }

  const rowOf = (pb: PlaybookOut): FeedRowSpec => {
    const builtIn = pb.origin === 'built_in'
    // A built-in's timestamps are the seed's, not an edit anyone made: its slot stays blank.
    const changed = builtIn ? null : pb.updated_at ?? pb.created_at
    return {
      key: pb.id,
      indicator: validityMark(pb),
      title: pb.name,
      titleText: pb.name,
      href: `/playbooks/${pb.id}`,
      busy: busyId === pb.id,
      markers: builtIn
        /* Hugs the name: as a column it sat at the row's far edge on a phone, read as nobody's. */
        ? [{ key: 'lock', glyph: '🔒\uFE0E', label: 'Built-in: duplicate it to edit a copy', testId: 'playbook-lock', hug: true }]
        : undefined,
      slots: [
        { key: 'entry', width: 72, gate: 'meta', align: 'left', dim: true, testId: 'playbook-entry', node: ENTRY_LABEL[pb.entry] ?? pb.entry },
        { key: 'stages', width: 64, gate: 'id', align: 'right', testId: 'playbook-stages', node: countOf(pb.stages.length, 'stage') },
        /* WHICH TENANT OWNS THE COPY, for an operator only (`useOrgVisible`), riding `squeeze` immediately left
           of the ungated tail, as `/projects`' does (`ui/feed/geometry.ts::SlotGate`). A built-in belongs to no
           org by design (one global copy), not by omission: its slot stays BLANK rather than the indicator's
           "no org" dash, which read as a stray mark beside the stage count. */
        ...orgSlots(orgVisible, pb.org, { gate: 'squeeze' }).map(slot => (builtIn ? { ...slot, node: null, title: undefined } : slot)),
        {
          key: 'updated',
          width: 40,
          align: 'right',
          dim: true,
          testId: 'playbook-updated',
          title: changed ? `Updated ${absoluteTime(changed)}` : undefined,
          node: changed ? compactAge(changed) : null,
        },
      ],
      // The description in full, on hover.
      notes: pb.description ? [{ key: 'description', text: pb.description, tone: 'muted' }] : undefined,
      actions: actionsOf(pb),
      data: { origin: pb.origin },
    }
  }

  /* One section's rows: flat, or Tree mode's folders (`lib/nameTreeRows`, the Schedule's engine) with
     re-filing off — `dragging: null` is its whole off switch. */
  const sectionRows = (items: PlaybookOut[]): FeedRowSpec[] =>
    treeMode
      ? nameTreeRows(buildNameTree(items), {
          ...NO_REFILING_DEPS,
          kind: PLAYBOOK_TREE_KIND,
          leafRow: (pb, busy) => ({ ...rowOf(pb), busy: busy || busyId === pb.id }),
          busyId,
          expanded: new Set(view.expanded),
          onToggleGroup: toggleGroup,
        })
      : items.map(rowOf)

  const sections: FeedSection[] = [
    ...(builtIns.length > 0 ? [{ key: 'built-in', label: 'Built-in', count: builtIns.length, rows: sectionRows(builtIns) }] : []),
    ...(own.length > 0 ? [{ key: 'own', label: 'Your playbooks', count: own.length, rows: sectionRows(own) }] : []),
    ...(others.length > 0 ? [{ key: 'other-orgs', label: 'Other orgs', count: others.length, rows: sectionRows(others) }] : []),
  ]

  return (
    <Box data-testid="playbooks-page">
      {/* Titleless: the Automation tab names the page, so the Preview mark rides the actions onto its strip. */}
      <PageHeader
        actions={
          <>
            {/* Off a phone: beside the strip's three tabs it would send one behind More; its phone home is
                the line under the strip. */}
            <Box component="span" sx={{ display: 'contents', [PHONE]: { display: 'none' } }}>
              <StatusChip label="Preview" tone="info" />
            </Box>
            <PaneModeToggle
              value={treeMode ? 'hierarchy' : 'flat'}
              onChange={next => patchView({ mode: next === 'hierarchy' ? 'tree' : 'flat' })}
            />
            {mayCreate && <NewButton label="New playbook" to="/playbooks/new" testId="playbook-new" />}
          </>
        }
      />

      <Box sx={PHONE_STATUS_LINE_SX} data-testid="playbooks-phone-status">
        <StatusChip label="Preview" tone="info" />
      </Box>

      {/* The strip: search alone — an operator's org narrowing is the top bar's global filter. */}
      <Box data-testid="playbook-filters">
        <FilterBar>
          <FilterRow>
            <FilterSearch value={view.q} onChange={q => patchView({ q })} placeholder="Name or description" ariaLabel="Search playbooks" />
          </FilterRow>
        </FilterBar>
      </Box>

      <Box sx={{ display: 'grid', gap: 1.5, minWidth: 0 }}>
        {error && <Banner severity="error">{error}</Banner>}
        <ListStates
          loading={list.isPending}
          skeleton={{ rows: 5 }}
          failed={list.isError && <Banner severity="error">{apiDetail(list.error, 'Could not load playbooks.')}</Banner>}
          empty={sections.length === 0}
          noMatch={rows.length > 0 && <NoMatchesState onClear={() => patchView({ q: '' })} />}
          emptyTitle="No playbooks"
          emptyScoped
          feed={{ ariaLabel: 'Playbooks', testId: 'playbook-list', sections }}
        >
          {ownTotal === 0 && !filtering && mayCreate && (
            <EmptyState title="No playbooks of your own yet" />
          )}
        </ListStates>
      </Box>
      {confirmDialog}
      {changeOrg.dialog}
    </Box>
  )
}
