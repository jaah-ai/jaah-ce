import { type KeyboardEvent, useEffect, useMemo, useRef, useState } from 'react'
import { useLocation, useNavigate } from 'react-router-dom'
import { useQueryClient } from '@tanstack/react-query'
import Box from '@mui/material/Box'
import InputBase from '@mui/material/InputBase'
import Menu from '@mui/material/Menu'
import MenuItem from '@mui/material/MenuItem'
import Typography from '@mui/material/Typography'
import type { Theme } from '@mui/material/styles'
import { apiDetail, get, post, put } from '../../../api'
import { useAuth } from '../../../auth'
import { useBoardScope } from '../../../lib/boardScope'
import { FOCUSED_PROJECT_PREF, readFocusedProjectId } from '../../../lib/focusedProject'
import { withRemoved } from '../../../lib/sets'
import { MUTED_PREF, readMuted, toggledMuted } from '../../../lib/taskMute'
import { parseScope, SCOPE_PARAM, scopeKey, type TaskScope } from '../../../lib/taskScope'
import { useDebouncedCallback } from '../../../hooks/useDebouncedCallback'
import { useSubmit } from '../../../hooks/useSubmit'
import { TASK_TREE_QUERY_KEY } from '../../../hooks/useTaskTree'
import type { TaskTree as TaskTreeData } from '../../../types'
import { schemed } from '../../../ui/listStyles'
import Toast from '../../../ui/Toast'
import { useDeleteFeature } from '../useDeleteFeature'
import TaskTreeRow, { type DropEdge } from './TaskTreeRow'
import {
  ALL_KEY,
  ancestorKeys,
  filterTree,
  isMutedNode,
  keyInTree,
  nodeForKey,
  pillCounts,
  railKey,
  reorderedIds,
  visibleRows,
  withFeatureOrder,
  type RailNode,
} from './railModel'
import { NO_AUTOFILL_INPUT } from '../../../ui/noAutofill'
import { SystemChip } from '../../../ui/MarkChip'
import { orgQualifierFor } from '../../../lib/projectLabel'

/* THE TASK RAIL'S TREE (Architecture §5.1): Pinned, then All projects › Project › Feature /
 * Shipped. Selecting a node navigates the board to that scope (`?scope=`, pushed); the board is the
 * one publishing which scope shows (`lib/boardScope.ts`), and the rail lights it. Selecting a project also
 * opens it, persisted like a hand toggle; only the disclosure (or ArrowLeft) closes it.
 *
 * Per-user state lives in `admin_users.preferences` (`apps/api/app/preferences.py`): `task_rail_pins` (written on
 * each pin) and `task_rail_expanded` (debounced 1s). Keys are `scopeKey`s; stale ones are dropped on read. The
 * selected node's ancestors are opened for it, never persisted.
 *
 * One component, three hosts (`components/Sidebar.tsx`): the expanded rail, the collapsed rail's flyout, and the
 * phone's bottom sheet (`touch`: 44px rows, an always-visible "+"). */

/** A navigation's `state` asking the board to focus its inline composer (a feature's "+"). */
export const FOCUS_COMPOSER_STATE = 'focusComposer'

const EXPAND_WRITE_MS = 1_000

function readKeys(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((v): v is string => typeof v === 'string') : []
}

type Creating = { parentKey: string; level: number; projectId: number }

type TaskTreeProps = {
  tree: TaskTreeData | undefined
  /** Phone sheet rows: the 44px floor and an always-visible "+". */
  touch?: boolean
  /** After a selection (the flyout and the sheet close). */
  onNavigate?: () => void
}

export default function TaskTree({ tree, touch = false, onNavigate }: TaskTreeProps) {
  const { admin, updatePreferences } = useAuth()
  const navigate = useNavigate()
  const { pathname, search } = useLocation()
  const client = useQueryClient()
  const onBoard = pathname === '/tasks'
  const boardScope = useBoardScope()
  const selectedKey = onBoard ? boardScope : null
  // The row that lights for it (`railKey`: a project's no-feature scope lights the project).
  const litKey = selectedKey === null ? null : railKey(selectedKey)

  // ── Per-user state ───────────────────────────────────────────────────────────────────────────────────
  const prefs = admin?.preferences
  const storedPins = readKeys(prefs?.task_rail_pins)
  const storedExpanded = readKeys(prefs?.task_rail_expanded)
  // Once this rail has toggled a node, its own list is authoritative until the write lands.
  const [localExpanded, setLocalExpanded] = useState<string[] | null>(null)
  const expandedList = useMemo(() => {
    const list = localExpanded ?? storedExpanded
    return tree ? list.filter(k => keyInTree(k, tree)) : list
  }, [localExpanded, storedExpanded.join('|'), tree])
  const pins = useMemo(() => (tree ? storedPins.filter(k => keyInTree(k, tree)) : []), [storedPins.join('|'), tree])

  // The selection's ancestors, opened for it (transient): recomputed when the selection moves, and a hand
  // collapsing one of them wins until it moves again.
  const [forced, setForced] = useState<Set<string>>(new Set())
  const treeLoaded = tree !== undefined
  useEffect(() => {
    if (!tree || selectedKey === null) return
    setForced(new Set(ancestorKeys(parseScope(selectedKey), tree)))
  }, [selectedKey, treeLoaded])

  const expandedSet = useMemo(() => new Set(expandedList), [expandedList])
  const isOpen = (key: string) => key === ALL_KEY || expandedSet.has(key) || forced.has(key)

  // A pending write is FLUSHED when the tree unmounts: the flyout and the sheet close on the selection that
  // usually follows a toggle, inside the debounce.
  const pendingExpandWrite = useRef(false)
  // The latest toggle, set synchronously: a select that toggles and closes the sheet in one event unmounts the
  // tree before it re-renders, so a render-time closure would flush the list from before the toggle.
  const latestExpanded = useRef<string[] | null>(null)
  function writeExpanded() {
    pendingExpandWrite.current = false
    if (latestExpanded.current !== null) void updatePreferences({ task_rail_expanded: latestExpanded.current })
  }
  const flushRef = useRef(writeExpanded)
  flushRef.current = writeExpanded
  useEffect(() => () => { if (pendingExpandWrite.current) flushRef.current() }, [])
  const { trigger: triggerExpandWrite } = useDebouncedCallback(writeExpanded, EXPAND_WRITE_MS)
  function scheduleExpandWrite() {
    pendingExpandWrite.current = true
    triggerExpandWrite()
  }

  function setOpen(key: string, open: boolean) {
    const next = expandedList.filter(k => k !== key)
    if (open) next.push(key)
    latestExpanded.current = next
    setLocalExpanded(next)
    if (!open && forced.has(key)) setForced(prev => withRemoved(prev, key))
    scheduleExpandWrite()
  }

  function togglePin(key: string) {
    const next = pins.includes(key) ? pins.filter(k => k !== key) : [...pins, key]
    void updatePreferences({ task_rail_pins: next })
  }

  // Mute (`lib/taskMute.ts`): this user's alone. The stored list, not one filtered by the tree, so a mute of a
  // node this user cannot see right now is kept.
  function toggleMute(key: string) {
    void updatePreferences({ [MUTED_PREF]: toggledMuted(readMuted(prefs), key) })
  }

  // The focused project (`lib/focusedProject.ts`): one per user, so setting one replaces the last.
  const focusedProjectId = readFocusedProjectId(prefs)
  function toggleFocus(projectId: number) {
    void updatePreferences({ [FOCUSED_PROJECT_PREF]: focusedProjectId === projectId ? null : projectId })
  }

  // ── Navigation ───────────────────────────────────────────────────────────────────────────────────────
  function hrefFor(scope: TaskScope): string {
    // On the board the rail KEEPS the URL's filters (search, period, creators, hidden), as the header does.
    const params = new URLSearchParams(onBoard ? search : '')
    params.set(SCOPE_PARAM, scopeKey(scope))
    return `/tasks?${params.toString()}`
  }

  function select(scope: TaskScope, state?: Record<string, unknown>) {
    // Re-selecting the node showing adds no history entry.
    navigate(hrefFor(scope), { replace: onBoard && scopeKey(scope) === selectedKey, ...(state ? { state } : {}) })
    onNavigate?.()
  }

  /** A row's click / Enter: selecting a project also opens it (and keeps it open — it never collapses it). */
  function activate(node: RailNode) {
    // The SAVED list, not `isOpen`: a project open only as the selection's ancestor (`forced`) drops out of
    // that set once it is itself selected. A leaf or a pinned (flattened) row has nothing here to open.
    if (node.kind === 'project' && node.expandable && !expandedSet.has(node.key)) setOpen(node.key, true)
    if (node.scope) select(node.scope)
    else if (node.expandable) setOpen(node.key, !node.expanded)
  }

  // ── Filter ───────────────────────────────────────────────────────────────────────────────────────────
  const [filter, setFilter] = useState('')
  const matches = useMemo(() => (tree ? filterTree(tree, filter) : []), [tree, filter])
  // Two orgs' same-named projects: the rail draws each one's org slug (`lib/projectLabel.tsx`).
  const qualifierOf = useMemo(() => orgQualifierFor(tree?.projects ?? []), [tree])
  const filtering = filter.trim() !== ''

  // ── Rows ─────────────────────────────────────────────────────────────────────────────────────────────
  type Row = { id: string; node: RailNode; level: number; pinned?: boolean; subtitle?: string; highlight?: { start: number; length: number } }
  const rows: Row[] = useMemo(() => {
    if (!tree) return []
    if (filtering) {
      const length = filter.trim().length
      return matches.map(m => ({ id: `match|${m.node.key}`, node: m.node, level: 1, subtitle: m.path || undefined, highlight: { start: m.start, length } }))
    }
    const pinnedRows: Row[] = pins.flatMap(key => {
      const node = nodeForKey(key, tree)
      return node ? [{ id: `pin|${key}`, node: { ...node, expandable: false, expanded: false }, level: 1, pinned: true }] : []
    })
    const treeRows: Row[] = visibleRows(tree, isOpen).map(node => ({ id: `node|${node.key}`, node, level: node.level }))
    return [...pinnedRows, ...treeRows]
  }, [tree, filtering, matches, pins.join('|'), expandedSet, forced])

  // ── Roving focus ─────────────────────────────────────────────────────────────────────────────────────
  const [focusId, setFocusId] = useState<string | null>(null)
  const rowEls = useRef(new Map<string, HTMLElement>())
  const tabStop = rows.find(r => r.id === focusId)?.id ?? rows.find(r => r.node.key === litKey)?.id ?? rows[0]?.id

  function focusRow(id: string | undefined) {
    if (!id) return
    setFocusId(id)
    rowEls.current.get(id)?.focus()
  }

  function onRowKeyDown(e: KeyboardEvent<HTMLElement>, row: Row, index: number) {
    const { node } = row
    switch (e.key) {
      case 'ArrowDown':
        e.preventDefault()
        focusRow(rows[index + 1]?.id)
        break
      case 'ArrowUp':
        e.preventDefault()
        if (index === 0 && filtering) filterRef.current?.focus()
        else focusRow(rows[index - 1]?.id)
        break
      case 'Home':
        e.preventDefault()
        focusRow(rows[0]?.id)
        break
      case 'End':
        e.preventDefault()
        focusRow(rows[rows.length - 1]?.id)
        break
      case 'ArrowRight':
        e.preventDefault()
        if (node.expandable && !node.expanded) setOpen(node.key, true)
        else if (node.expandable) focusRow(rows[index + 1]?.id)
        break
      case 'ArrowLeft': {
        e.preventDefault()
        if (node.expandable && node.expanded && node.key !== ALL_KEY) setOpen(node.key, false)
        else if (!row.pinned && node.parentKey) focusRow(`node|${node.parentKey}`)
        break
      }
      case 'Enter':
      case ' ':
        e.preventDefault()
        activate(node)
        break
      case 'Escape':
        if (filtering) {
          e.preventDefault()
          setFilter('')
          filterRef.current?.focus()
        }
        break
      case 'F10':
      case 'ContextMenu':
        if (e.key === 'F10' && !e.shiftKey) break
        if (menuFor(node)) {
          e.preventDefault()
          setMenu({ anchor: e.currentTarget, node, rowId: row.id })
        }
        break
    }
  }

  // ── Row menu (Pin/Unpin, Mute/Unmute, Focus/Unfocus, Up/Down, New, Edit) ───────────────────────────────────────────────────────────────────────────
  const [menu, setMenu] = useState<{ anchor: HTMLElement; node: RailNode; rowId: string } | null>(null)
  function menuFor(node: RailNode): boolean {
    if (node.kind === 'all') return createActionFor(node) !== undefined
    return node.kind === 'project' || node.kind === 'feature'
  }
  const pinnable = (node: RailNode) => node.kind !== 'all' && node.kind !== 'shipped'

  // ── Create ───────────────────────────────────────────────────────────────────────────────────────────
  const [creating, setCreating] = useState<Creating | null>(null)
  const [createName, setCreateName] = useState('')
  const { busy: createBusy, formError: createError, setFormError: setCreateError, run: runCreate } = useSubmit()

  function startCreate(next: Creating) {
    setCreating(next)
    setCreateName('')
    setCreateError('')
  }

  /** What "+" does on a node, or `undefined` where the viewer may not create (the API refuses it too). */
  function createActionFor(node: RailNode): { label: string; run: (anchor: HTMLElement) => void } | undefined {
    if (!tree) return undefined
    switch (node.kind) {
      case 'all':
        return tree.can_create_project
          ? { label: 'New project', run: () => { navigate('/projects/new'); onNavigate?.() } }
          : undefined
      case 'project': {
        const project = node.project!
        return project.can_create_tasks
          ? { label: `New feature in ${project.name}`, run: () => startCreate({ parentKey: node.key, level: 3, projectId: project.id }) }
          : undefined
      }
      case 'feature':
        // Filing into a shipped feature is refused, so it offers no "+".
        return node.feature?.status === 'active' && node.project?.can_create_tasks && node.scope
          ? { label: `New task in ${node.label}`, run: () => select(node.scope!, { [FOCUS_COMPOSER_STATE]: Date.now() }) }
          : undefined
      default:
        return undefined
    }
  }

  async function submitCreate() {
    if (!creating || createBusy) return
    const name = createName.trim()
    if (name === '') return
    await runCreate('Could not create the feature', async () => {
      const created = await post<{ id: string }>('/api/features', { project_id: creating.projectId, name })
      await client.invalidateQueries({ queryKey: TASK_TREE_QUERY_KEY })
      setOpen(creating.parentKey, true)
      setCreating(null)
      select({ kind: 'feature', featureId: created.id })
    })
  }

  // ── Drop a pane task onto a feature, or onto a project ─────────────────────────────────────────────────
  // A drop names a project as well as a placement, so a task dropped into another project's tree MOVES
  // there with its whole subtree (`PUT /api/tasks/{id}` `{project_id}`); the server refuses a child, a live
  // subtree and a project the viewer may not edit, and its reason is the toast.
  const [toast, setToast] = useState<{ severity: 'success' | 'error'; text: string } | null>(null)
  // Deleting the lit feature moves the board to its project; deleting another leaves the board where it is.
  const remove = useDeleteFeature({
    onDeleted: (projectId, featureId) => { if (litKey === `feature:${featureId}`) select({ kind: 'project', projectId }) },
    onError: text => setToast({ severity: 'error', text }),
  })
  async function fileTask(taskId: number, node: RailNode) {
    const project = node.project!
    try {
      // Resending the task's own project is a no-op server-side, so the one write serves both cases.
      const before = await get<{ project_id: number }>(`/api/tasks/${taskId}`)
      await put(`/api/tasks/${taskId}`, { project_id: project.id, feature_id: node.feature!.id })
      void client.invalidateQueries({ queryKey: TASK_TREE_QUERY_KEY })
      setToast({
        severity: 'success',
        text: before.project_id === project.id
          ? `Task ${taskId} filed into ${node.label}`
          : `Task ${taskId} moved to ${project.name} › ${node.label}`,
      })
    } catch (err) {
      setToast({ severity: 'error', text: apiDetail(err, `Could not file task ${taskId} into ${node.label}`) })
    }
  }

  async function unfileTask(taskId: number, node: RailNode) {
    const project = node.project!
    let moving = false
    try {
      const task = await get<{ project_id: number; feature_id?: string | null }>(`/api/tasks/${taskId}`)
      moving = task.project_id !== project.id
      if (moving) {
        // Leaving a project leaves its feature: the move lands the task with no feature.
        await put(`/api/tasks/${taskId}`, { project_id: project.id })
        void client.invalidateQueries({ queryKey: TASK_TREE_QUERY_KEY })
        setToast({ severity: 'success', text: `Task ${taskId} moved to ${project.name}` })
        return
      }
      if (!task.feature_id) {
        setToast({ severity: 'success', text: `Task ${taskId} already has no feature` })
        return
      }
      await put(`/api/tasks/${taskId}`, { feature_id: null })
      void client.invalidateQueries({ queryKey: TASK_TREE_QUERY_KEY })
      setToast({ severity: 'success', text: `Task ${taskId} removed from its feature` })
    } catch (err) {
      setToast({ severity: 'error', text: apiDetail(
        err,
        moving ? `Could not move task ${taskId} to ${project.name}` : `Could not remove task ${taskId} from its feature`,
      ) })
    }
  }

  // ── Reorder a project's active features (drag, or the row menu's Move up / Move down) ─────────────────
  // Who is dragging: a drag-over sees only the types, so the tree says which rows are the dragged one's siblings.
  const [draggingFeature, setDraggingFeature] = useState<{ projectId: number; featureId: string } | null>(null)
  // The row's own `dragend` never arrives when a refetch unmounts it mid-drag, so the document's clears it too.
  useEffect(() => {
    if (!draggingFeature) return
    const clear = () => setDraggingFeature(null)
    document.addEventListener('dragend', clear)
    document.addEventListener('drop', clear)
    return () => {
      document.removeEventListener('dragend', clear)
      document.removeEventListener('drop', clear)
    }
  }, [draggingFeature])
  const canReorder = (node: RailNode) =>
    !touch && node.kind === 'feature' && node.feature?.status === 'active' && node.project?.can_edit_tasks === true

  async function moveFeature(projectId: number, featureId: string, targetId: string, edge: DropEdge) {
    if (!tree) return
    const ids = reorderedIds(tree, projectId, featureId, targetId, edge)
    if (!ids) return
    // Optimistic: the rail shows the new order at once, and the refetch settles it either way. An in-flight
    // read is cancelled first, so it cannot land the old order over this one.
    await client.cancelQueries({ queryKey: TASK_TREE_QUERY_KEY })
    client.setQueryData<TaskTreeData>(TASK_TREE_QUERY_KEY, prev => (prev ? withFeatureOrder(prev, projectId, ids) : prev))
    try {
      await put('/api/features/order', { project_id: projectId, feature_ids: ids })
    } catch (err) {
      setToast({ severity: 'error', text: apiDetail(err, 'Could not reorder the features') })
    } finally {
      void client.invalidateQueries({ queryKey: TASK_TREE_QUERY_KEY })
    }
  }

  /** The active sibling one step up or down, for the row menu; `undefined` at that end. */
  function neighbour(node: RailNode, step: -1 | 1): string | undefined {
    const active = node.project!.features.filter(f => f.status === 'active')
    return active[active.findIndex(f => f.id === node.feature!.id) + step]?.id
  }

  const filterRef = useRef<HTMLInputElement | null>(null)

  if (!tree) {
    return (
      <Typography component="div" data-testid="rail-tree-loading" sx={{ px: 1.5, py: 1, fontSize: '0.75rem', opacity: 0.7 }}>
        Loading projects…
      </Typography>
    )
  }

  const creatingRow = creating && (
    <Box
      key="rail-create"
      component="form"
      data-testid="rail-create"
      onSubmit={e => { e.preventDefault(); void submitCreate() }}
      sx={{ margin: '2px 4px', padding: `0 6px 0 ${22 + Math.max(0, creating.level - 2) * 12}px` }}
    >
      <InputBase
        autoFocus
        fullWidth
        value={createName}
        disabled={createBusy}
        onChange={e => setCreateName(e.target.value)}
        onKeyDown={e => {
          if (e.key === 'Escape') { e.preventDefault(); setCreating(null) }
        }}
        onBlur={() => { if (createName.trim() === '' && !createBusy) setCreating(null) }}
        placeholder="Feature name"
        inputProps={{ 'aria-label': 'New feature name', 'data-testid': 'rail-create-input' }}
        sx={(t: Theme) => ({
          fontSize: '0.8125rem',
          padding: '1px 6px',
          borderRadius: '4px',
          border: '1px solid',
          ...schemed(t, c => ({ borderColor: c.accent, color: c.textStrong, backgroundColor: c.bg })),
          ...(touch && { minHeight: t.custom.tapTargetPx }),
        })}
      />
      {createError && (
        <Typography role="alert" data-testid="rail-create-error" sx={{ fontSize: '0.6875rem', color: 'error.main', px: 0.75 }}>
          {createError}
        </Typography>
      )}
    </Box>
  )

  let pinnedHeaderShown = false
  return (
    <Box data-testid="rail-tree-host" sx={{ display: 'flex', flexDirection: 'column', minHeight: 0 }}>
      <Box sx={{ padding: '2px 8px 6px' }}>
        <InputBase
          inputRef={filterRef}
          fullWidth
          value={filter}
          onChange={e => setFilter(e.target.value)}
          onKeyDown={e => {
            if (e.key === 'Escape' && filter !== '') { e.preventDefault(); setFilter('') }
            if (e.key === 'ArrowDown' && rows.length > 0) { e.preventDefault(); focusRow(rows[0].id) }
          }}
          placeholder="Find a project or feature"
          inputProps={{ ...NO_AUTOFILL_INPUT, 'aria-label': 'Find a project or feature', 'data-testid': 'rail-filter' }}
          endAdornment={
            filter !== '' && (
              <Box
                component="button"
                type="button"
                aria-label="Clear filter"
                title="Clear"
                data-testid="rail-filter-clear"
                onClick={() => { setFilter(''); filterRef.current?.focus() }}
                sx={(t: Theme) => ({
                  flex: '0 0 auto',
                  display: 'inline-flex',
                  alignItems: 'center',
                  justifyContent: 'center',
                  width: touch ? 32 : 20,
                  height: touch ? 32 : 20,
                  p: 0,
                  border: 0,
                  borderRadius: '4px',
                  background: 'none',
                  cursor: 'pointer',
                  fontSize: '0.75rem',
                  color: 'inherit',
                  opacity: 0.7,
                  '&:hover': { opacity: 1, ...schemed(t, c => ({ backgroundColor: c.hoverBg })) },
                })}
              >
                ✕
              </Box>
            )
          }
          sx={(t: Theme) => ({
            fontSize: '0.8125rem',
            padding: '2px 8px',
            borderRadius: '4px',
            border: '1px solid',
            ...schemed(t, c => ({ borderColor: c.border, color: c.textStrong, backgroundColor: c.overlayHover })),
            ...(touch && { minHeight: t.custom.tapTargetPx }),
          })}
        />
      </Box>
      <Box
        role="tree"
        aria-label="Tasks"
        data-testid="rail-tree"
        sx={{ display: 'flex', flexDirection: 'column' }}
      >
        {filtering && rows.length === 0 && (
          <Typography data-testid="rail-filter-empty" sx={{ px: 1.5, py: 0.5, fontSize: '0.75rem', opacity: 0.8 }}>
            Nothing matches “{filter.trim()}”
          </Typography>
        )}
        {rows.map((row, index) => {
          const { node } = row
          const create = row.pinned || filtering ? undefined : createActionFor(node)
          const heading = row.pinned && !pinnedHeaderShown
          if (heading) pinnedHeaderShown = true
          const allHeading = !filtering && node.kind === 'all' && pins.length > 0
          return (
            <Box key={row.id} role="none" sx={{ display: 'contents' }}>
              {(heading || allHeading) && (
                <Typography
                  aria-hidden="true"
                  data-testid={heading ? 'rail-pinned-heading' : 'rail-all-heading'}
                  sx={{ px: 1.5, pt: allHeading ? 1 : 0.25, pb: 0.25, fontSize: '0.6875rem', fontWeight: 600, letterSpacing: '0.04em', textTransform: 'uppercase', opacity: 0.75 }}
                >
                  {heading ? 'Pinned' : 'Projects'}
                </Typography>
              )}
              <TaskTreeRow
                id={`rail-${row.id}`}
                nodeKey={node.key}
                testId={row.pinned ? 'rail-pinned-node' : filtering ? 'rail-match' : 'rail-node'}
                level={row.level}
                label={node.label}
                labelAside={node.kind === 'project' && node.project?.system_kind ? <SystemChip /> : undefined}
                qualifier={node.kind === 'project' && node.project ? qualifierOf(node.project) : null}
                counts={isMutedNode(node) ? undefined : pillCounts(node, row.pinned || filtering)}
                muted={isMutedNode(node)}
                subtitle={row.subtitle}
                highlight={row.highlight}
                expandable={node.expandable}
                expanded={node.expanded}
                selected={node.key === litKey}
                tabbable={row.id === tabStop}
                touch={touch}
                rowRef={el => { if (el) rowEls.current.set(row.id, el); else rowEls.current.delete(row.id) }}
                onFocus={() => setFocusId(row.id)}
                onSelect={() => {
                  setFocusId(row.id)
                  activate(node)
                }}
                href={node.scope ? hrefFor(node.scope) : undefined}
                onToggle={() => setOpen(node.key, !node.expanded)}
                onKeyDown={e => onRowKeyDown(e, row, index)}
                onCreate={create?.run}
                createLabel={create?.label}
                onMenu={menuFor(node) && pinnable(node) && !filtering ? anchor => setMenu({ anchor, node, rowId: row.id }) : undefined}
                menuOpen={menu?.rowId === row.id}
                onDropTask={
                  touch
                    ? undefined
                    : node.kind === 'feature' && node.feature?.status === 'active'
                      ? taskId => void fileTask(taskId, node)
                      : node.kind === 'project'
                        ? taskId => void unfileTask(taskId, node)
                        : undefined
                }
                icon={node.kind === 'feature' ? node.feature?.icon : undefined}
                progress={
                  node.kind === 'feature' && node.feature?.show_progress && !isMutedNode(node) && node.counts.total > 0
                    ? { done: node.counts.done, total: node.counts.total }
                    : undefined
                }
                reorder={
                  canReorder(node) && !row.pinned && !filtering
                    ? {
                        onStart: () => setDraggingFeature({ projectId: node.project!.id, featureId: node.feature!.id }),
                        onEnd: () => setDraggingFeature(null),
                      }
                    : undefined
                }
                onReorderDrop={
                  draggingFeature && canReorder(node) && !row.pinned && !filtering
                    && node.project!.id === draggingFeature.projectId
                    ? edge => {
                        const { projectId, featureId } = draggingFeature
                        setDraggingFeature(null)
                        void moveFeature(projectId, featureId, node.feature!.id, edge)
                      }
                    : undefined
                }
              />
              {creating && !row.pinned && creating.parentKey === node.key && creatingRow}
            </Box>
          )
        })}
      </Box>
      <Menu
        anchorEl={menu?.anchor ?? null}
        open={menu !== null}
        onClose={() => setMenu(null)}
        slotProps={{ list: { dense: true } }}
      >
        {menu && pinnable(menu.node) && (
          <MenuItem
            data-testid="rail-pin-toggle"
            onClick={() => { togglePin(menu.node.key); setMenu(null) }}
          >
            {pins.includes(menu.node.key) ? 'Unpin' : 'Pin'}
          </MenuItem>
        )}
        {menu && (menu.node.kind === 'project' || menu.node.kind === 'feature') && (
          <MenuItem
            data-testid="rail-mute-toggle"
            onClick={() => { toggleMute(menu.node.key); setMenu(null) }}
          >
            {readMuted(prefs).includes(menu.node.key) ? 'Unmute' : 'Mute'}
          </MenuItem>
        )}
        {menu && menu.node.kind === 'project' && (
          <MenuItem
            data-testid="rail-focus-toggle"
            onClick={() => { toggleFocus(menu.node.project!.id); setMenu(null) }}
          >
            {focusedProjectId === menu.node.project!.id ? 'Unfocus' : 'Focus'}
          </MenuItem>
        )}
        {menu && canReorder(menu.node) && (['up', 'down'] as const).map(dir => {
          const node = menu.node
          const target = neighbour(node, dir === 'up' ? -1 : 1)
          return (
            <MenuItem
              key={dir}
              data-testid={`rail-move-${dir}`}
              disabled={!target}
              onClick={() => {
                setMenu(null)
                if (target) void moveFeature(node.project!.id, node.feature!.id, target, dir === 'up' ? 'before' : 'after')
              }}
            >
              {dir === 'up' ? 'Up' : 'Down'}
            </MenuItem>
          )
        })}
        {menu && (() => {
          const create = createActionFor(menu.node)
          return create ? (
            <MenuItem data-testid="rail-menu-create" onClick={() => { const a = menu.anchor; setMenu(null); create.run(a) }}>
              New
            </MenuItem>
          ) : null
        })()}
        {/* A feature is edited by whoever may edit the project's tasks (`routes/features.py`'s `can_edit`). */}
        {menu && menu.node.kind === 'feature' && menu.node.project?.can_edit_tasks && (
          <MenuItem
            data-testid="rail-menu-edit"
            onClick={() => { const id = menu.node.feature!.id; setMenu(null); navigate(`/features/${encodeURIComponent(id)}`); onNavigate?.() }}
          >
            Edit
          </MenuItem>
        )}
        {menu && menu.node.kind === 'feature' && menu.node.project?.can_edit_tasks && (
          <MenuItem
            data-testid="rail-menu-delete"
            disabled={remove.busy}
            sx={{ color: 'error.main' }}
            onClick={() => {
              const { node } = menu
              setMenu(null)
              void remove.deleteFeature({ id: node.feature!.id, name: node.feature!.name, project_id: node.project!.id })
            }}
          >
            Delete
          </MenuItem>
        )}
        {menu && menu.node.kind === 'project' && menu.node.project!.can_edit_project && (
          <MenuItem
            data-testid="rail-menu-edit"
            onClick={() => { const id = menu.node.project!.id; setMenu(null); navigate(`/projects/${id}`); onNavigate?.() }}
          >
            Edit
          </MenuItem>
        )}
      </Menu>
      <Toast toast={toast} onClose={() => setToast(null)} testId="rail-toast" />
      {remove.confirmDialog}
    </Box>
  )
}
