import { useCallback, useMemo, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import Box from '@mui/material/Box'
import Typography from '@mui/material/Typography'
import WorktreePool, {
  deleteSlot,
  isSyncEligible,
  stopSlotStack,
  syncSlotGit,
} from '../components/WorktreePool'
import { releaseSlot } from '../lib/slotRelease'
import WorktreeToolbar from '../components/worktrees/WorktreeToolbar'
import { useConfirm } from '../components/ConfirmDialog'
import { useAuth, useCan } from '../auth'
import { isOrgAdminSeat } from '../lib/orgAdmin'
import BulkActionBar, { type BulkAction } from '../ui/BulkActionBar'
import BulkRunReports from '../ui/BulkRunReports'
import { useBulkRuns } from '../hooks/useBulkRuns'
import { useRowSelection } from '../hooks/useRowSelection'
import { bulkIdentity } from '../lib/bulkRun'
import { useNodesRoster, workNodes } from '../hooks/useNodesRoster'
import { shortNode } from '../lib/nodeName'
import type { WorktreeGroupBy } from '../lib/worktreeGrouping'
import {
  activeWorktreeFilterCount,
  readWorktreeFilters,
  writeWorktreeFilters,
  type WorktreeFilters,
} from '../lib/worktreeFilters'
import {
  groupIsCollapsed,
  readGroupFolds,
  readGroupBy,
  withGroupFold,
  type GroupFolds,
  writeGroupFolds,
  writeGroupBy,
} from '../lib/worktreeViewPrefs'
import type { NodeRecord, Worktree } from '../types'
import PageHeader from '../ui/PageHeader'
import { slotIdentity } from '../lib/poolSlot'

/* ── /worktrees — the fleet's pool ─────────────────────────────────────────────────────────────────
 *
 * A page of its own because rows carry their node (grouped, filtered, a column), so fleet-wide questions
 * ("which trees are stale", "what is holding a slot") answer in one read. The node page links its
 * worktree count into `?node=`.
 *
 * Gated on `worktrees:view` (`App.tsx`), the key the pool's API routes declare. The node roster it joins
 * is `nodes:view` (`apps/api/app/routes/nodes/reads.py::list_nodes`; `apps/api/app/authz/`'s "THE TIER KEY
 * BECOMES THE DOOR"): without it, roster uses degrade (raw node ids, no Add-form picker), never block.
 *
 * ── WHO OWNS WHAT, AND WHY THE SLOT QUERY IS NOT HERE ─────────────────────────────────────────────
 * This page owns the READER'S VIEW (filters, grouping, folds, selection). `components/WorktreePool.tsx`
 * owns the data and rendering: the `['worktrees']` query, table, drawer, Add form, row commands. The
 * query stays there: `components/ConsoleDock.tsx` subscribes to that cache key and needs a live owner, and
 * its lifecycle (debounced stream refetch, stream-down interval, post-command read) should not be split.
 * The derived view comes back up via `onViewChange`.
 *
 * Filters live in the URL, view preferences do not (`lib/worktreeFilters.ts`, `lib/worktreeViewPrefs.ts`).
 */

/** What the panel reports about the list it drew — TWO row sets:
 *  `visible` survived the filter (what the header's `N of M` counts);
 *  `onScreen` was EMITTED (minus rows in collapsed groups, `lib/worktreeGrouping.ts::buildWorktreeRows`) —
 *  the only rows a selection may hold and a bulk verb may act on (select-all over `visible` once fanned
 *  out over folded rows). Whole slots, since action availability is decided per ROW.
 *  `nodeIds`/`projects` are facets of the UNFILTERED payload, or an option list collapses to the pick.
 *  `nodeIds` are node uuids; `nodeNames` maps each to the name its slots carry (the label fallback). */
export type WorktreePoolView = {
  visible: Worktree[]
  onScreen: Worktree[]
  total: number
  nodeIds: string[]
  nodeNames: Record<string, string>
  projects: string[]
}

/* Bulk verbs fan out per row with NO bulk endpoint, by design — `lib/bulkRun.ts` holds the why and the
 * machinery; `hooks/useBulkRuns.ts` the runs/reports/batch state; `ui/BulkRunReports.tsx` the banners. */

/** The verbs as one closed set: each id keys three MAPS (button, greying run, report), so a drifted
 *  literal would fail silently — no progress, no report, double-firable mid-run. */
const SYNC = 'sync'
const STOP = 'stop-stack'
const RELEASE = 'release'
/** `delete`, the word on screen (the route path is still `POST .../erase` for external clients). */
const DELETE = 'delete'
type BulkVerbId = typeof SYNC | typeof STOP | typeof RELEASE | typeof DELETE

/* The host joins the identity once a selection spans nodes: `<project>#<number>` repeats across hosts, so
   two nodes' slots read as the same four named twice — and a Delete would erase both. Qualified only when
   it disambiguates, and then every entry (partial qualification reads as "these are special"). */
function identityOf(slots: readonly Worktree[], nodeLabelFor?: (nodeId: string) => string): string {
  const spansNodes = new Set(slots.map(slot => slot.node_uuid)).size > 1
  const name = (slot: Worktree) =>
    spansNodes && nodeLabelFor ? `${slotIdentity(slot)} on ${nodeLabelFor(slot.node_uuid)}` : slotIdentity(slot)
  return bulkIdentity(slots, name)
}

/** The roster keyed for lookup; its own hook so the `nodes:view` degradation is stated once. */
function useNodeRoster(): { nodes: NodeRecord[]; labels: ReadonlyMap<string, string> } {
  /* `/api/nodes` is a `nodes:view` read; denial is a DEGRADATION (labels and the picker), never an error:
     `enabled` false gives a denied account a terminal empty roster. */
  const canReadNodes = useCan()('nodes:view')
  const nodesQuery = useNodesRoster(canReadNodes)

  const nodes = useMemo(() => nodesQuery.data ?? [], [nodesQuery.data])

  /* Names by node uuid (the slot's `node_uuid`); a rowless roster entry has no uuid and labels nothing. */
  const labels = useMemo(() => {
    const map = new Map<string, string>()
    for (const n of nodes) if (n.id && n.name.trim()) map.set(n.id, n.name)
    return map
  }, [nodes])

  return { nodes, labels }
}

/** Filters read through from the query string — one owner per value. Not `hooks/useUrlFilters` (one string per key,
 *  merged onto the location): these are typed, `node`/`project`/`occupancy` repeat, and the set is written whole. */
function useWorktreeUrlFilters(): { filters: WorktreeFilters; setFilters: (next: WorktreeFilters) => void } {
  const [searchParams, setSearchParams] = useSearchParams()
  const filters = useMemo(() => readWorktreeFilters(searchParams), [searchParams])
  const setFilters = useCallback(
    (next: WorktreeFilters) => {
      /* `replace`: intermediate filters do not flood history; the deep link that brought them is kept. */
      setSearchParams(writeWorktreeFilters(next), { replace: true })
    },
    [setSearchParams],
  )
  return { filters, setFilters }
}

export default function Worktrees() {
  const { nodes, labels } = useNodeRoster()
  const { filters, setFilters } = useWorktreeUrlFilters()

  const [groupBy, setGroupByState] = useState<WorktreeGroupBy>(readGroupBy)
  const [groupFolds, setGroupFolds] = useState<GroupFolds>(readGroupFolds)
  const [view, setView] = useState<WorktreePoolView>(
    { visible: [], onScreen: [], total: 0, nodeIds: [], nodeNames: {}, projects: [] },
  )
  /* Picked rows by worktree id (`project#number` is not fleet-unique); here because the bulk bar is here.
     Pruned to what is ON SCREEN (`view.onScreen`, not `visible`, `hooks/useRowSelection.ts`): a row filtered
     out, erased, or inside a folded group leaves the pick. A fold is reversible, so nothing is lost. */
  const onScreenIds = useMemo(() => new Set(view.onScreen.map(s => s.id)), [view.onScreen])
  const selection = useRowSelection(onScreenIds)
  const { selected, clear: clearSelection } = selection
  /* A node's label by uuid: the roster's name, else the short form of the name its own slots carry (a
     reader without `nodes:view`) — a fact about this reader's grants, still matchable against a log. */
  const nodeLabelFor = useCallback(
    (nodeId: string) => labels.get(nodeId) ?? shortNode(view.nodeNames[nodeId] ?? nodeId),
    [labels, view.nodeNames],
  )
  const [confirm, confirmDialog] = useConfirm()
  const can = useCan()
  const { admin } = useAuth()
  /* The org-admin verdict off the principal (not a grantable key; `pool_slots.require_slot_writer` requires
     it beyond `worktrees:delete`), as the panel's own Delete. Named `canErase` after the route (`.../erase`). */
  const canErase = can('worktrees:delete') && isOrgAdminSeat(admin)

  const setGroupBy = useCallback((next: WorktreeGroupBy) => {
    setGroupByState(next)
    writeGroupBy(next)
  }, [])

  /* The persist runs outside the updater: StrictMode double-invokes updaters, and a side effect hiding in
     one survives only while it stays idempotent. */
  const toggleGroup = useCallback((groupKey: string, collapsed: boolean) => {
    const next = withGroupFold(groupFolds, groupKey, !collapsed)
    writeGroupFolds(next)
    setGroupFolds(next)
  }, [groupFolds])

  const isCollapsed = useCallback(
    (groupKey: string, slotCount: number) => groupIsCollapsed(groupFolds, groupKey, slotCount),
    [groupFolds],
  )

  /* Derived from the on-screen rows as they are NOW (not stashed at tick time), so each verb decides
     against current state and no verb can receive an off-page slot even between a fold and the prune. */
  const selectedSlots = useMemo(
    () => view.onScreen.filter(s => selected.has(s.id)),
    [view, selected],
  )

  /* ── The bulk verbs ─────────────────────────────────────────────────────────────────────────────
     Each imports the row's own per-slot call so bulk and single presses cannot drift. Offered only while a
     picked row can take it, and saying why when none can. */
  const syncable = selectedSlots.filter(isSyncEligible)
  const stoppable = selectedSlots.filter(s => s.stack_state === 'up' && s.may_edit)
  const releasable = selectedSlots.filter(s => s.state !== 'free' && s.may_edit)

  /* A clean batch clears the selection; a refused or degraded one keeps it — those are the rows to look at
     again. The report names each slot by `slotIdentity` (the node joins only in the confirm, `identityOf`). */
  const { runs, reports, verb } = useBulkRuns<Worktree, BulkVerbId>({
    identity: slotIdentity,
    onBatchClean: clearSelection,
  })

  const bulkActions: BulkAction[] = [
    verb({
      id: SYNC, label: 'Sync', icon: '⟳', rows: syncable,
      noneWhy: 'No picked slot is behind its base with a clean tree you may drive',
      doing: 'Syncing', done: 'Synced', run: syncSlotGit,
    }),
    verb({
      id: STOP, label: 'Stop', icon: '⏹', destructive: true, rows: stoppable,
      /* Both halves of `stoppable` (`stack_state === 'up' && may_edit`), so the reason matches the row. */
      noneWhy: 'No picked slot has a stack running that you may drive',
      confirm: () => confirm({
        title: `Stop ${stoppable.length} stack${stoppable.length === 1 ? '' : 's'}?`,
        message: 'The containers stop; their data volumes and the slots are untouched.',
        identity: identityOf(stoppable, nodeLabelFor),
        confirmLabel: 'Stop',
        destructive: true,
      }),
      doing: 'Stopping', done: 'Stopped', run: stopSlotStack,
    }),
    verb({
      id: RELEASE, label: 'Release', icon: '⏏', destructive: true, rows: releasable,
      noneWhy: 'No picked slot is held by a task or a person you may drive',
      confirm: () => confirm({
        title: `Release ${releasable.length} slot${releasable.length === 1 ? '' : 's'}?`,
        /* Says what Release does NOT do (delete), so it is pressed when it should be. */
        message: 'The slots, their uncommitted work and their stacks survive; the next dispatch '
          + 'resets a tree. A task holding one is NOT completed or deleted by this.',
        identity: identityOf(releasable, nodeLabelFor),
        confirmLabel: 'Release',
        destructive: true,
      }),
      /* WRAPPED, never passed bare: `releaseSlot(slot, force = false)` under a fan-out calling
         `run(slot)` is one arity change from force-releasing every row, and BULK MUST NEVER FORCE
         (`lib/slotRelease.ts::releaseSlot`). */
      doing: 'Releasing', done: 'Released', run: slot => releaseSlot(slot),
    }),
    /* DELETE IS SUPERADMIN-ONLY, as the row's Delete and `erase_slot` (`require_superadmin` +
       `worktrees:delete`); withheld, not disabled — it is somebody else's button. No `deletable` filter:
       the route has no occupancy, liveness or per-slot `may_edit` check left, so every ticked row is acted on.
       No `noneWhy`: only the running reason is reachable (the bar renders nothing with zero picked). */
    ...(canErase
      ? ([verb({
          id: DELETE, label: 'Delete', icon: '🗑', destructive: true, rows: selectedSlots,
          confirm: () => confirm({
            title: `Delete ${selectedSlots.length} slot${selectedSlots.length === 1 ? '' : 's'}?`,
            /* The confirm states what CANNOT stop this press (no occupancy refusal): every declaration
               goes, any runner is killed; a directory on an unreachable node stays and is named. */
            message: 'De-declares each slot AND deletes its directory on its node. Nothing is '
              + 'refused: every picked row is de-declared, and anything holding it — a '
              + 'task runner, a console session — is ENDED. Unpushed work is lost. A directory stays '
              + 'only if its node cannot be reached; each is named in the report. This cannot be undone.',
              // Same caveat as the drawer's copy (`WorktreePool.tsx`): one node-side behaviour.
            identity: identityOf(selectedSlots, nodeLabelFor),
            confirmLabel: 'Delete',
            destructive: true,
          }),
          /* `deleteSlot` takes only a slot (the route dropped `force`), so no wrapper is needed. */
          doing: 'Deleting', done: 'Deleted', run: deleteSlot,
          /* What a row's 200 still owes the operator:
               · `node_erased: false` — the row went, the DIRECTORY DID NOT: a warning, never a
                 failure (the ask was carried out), and it degrades the run (an orphan to clean by hand).
               · `holder_ended` — this Delete killed a runner: reported, not degrading.
             Both names (slot and host) appear: this report is their last place together. The prefixed
             word, not the colour, distinguishes warning from remark from refusal. */
          inspect: (slot, result) => {
            const orphan = result.node_erased ? '' : 'its directory is still on '
              + `${nodeLabelFor(slot.node_uuid)} — `
              + (result.node_detail || 'the node did not confirm the erase and gave no reason')
            const killed = result.holder_ended
              ? `the ${result.holder_ended} hold on it was ended`
              : ''
            if (!orphan && !killed) return null
            return {
              line: `${orphan ? 'Warning' : 'Note'} — ${slotIdentity(slot)} is de-declared; `
                + [killed, orphan].filter(Boolean).join('; '),
              degraded: !!orphan,
            }
          },
        })] satisfies BulkAction[])
      : []),
  ]

  /* The filtered count, silent when nothing is filtered. On `visible`, deliberately: it answers the
     FILTER question, so folding must not claim rows were filtered out. It rides the actions, not a subtitle:
     a header with no title portals its actions onto the Automation strip, and a subtitle would pull them back
     into a row of their own whenever a filter is set. */
  const filteredCount = activeWorktreeFilterCount(filters) === 0
    ? undefined
    : `${view.visible.length} of ${view.total} slots`

  return (
    <Box>
      <PageHeader
        testId="worktrees-header"
        actions={
          <>
            {filteredCount && (
              <Typography variant="body2" sx={{ color: 'text.secondary', whiteSpace: 'nowrap' }}>
                {filteredCount}
              </Typography>
            )}
            <WorktreeToolbar
              filters={filters}
              onFiltersChange={setFilters}
              groupBy={groupBy}
              onGroupByChange={setGroupBy}
              nodeIds={view.nodeIds}
              projects={view.projects}
              nodeLabelFor={nodeLabelFor}
            />
          </>
        }
      />
      <BulkActionBar
        count={selectedSlots.length}
        onClear={clearSelection}
        actions={bulkActions}
      />
      <BulkRunReports actions={bulkActions} runs={runs} reports={reports} />
      <WorktreePool
        nodes={workNodes(nodes)}
        nodeLabelFor={nodeLabelFor}
        filters={filters}
        groupBy={groupBy}
        isCollapsed={isCollapsed}
        onToggleGroup={toggleGroup}
        selection={selection}
        onViewChange={setView}
      />
      {confirmDialog}
    </Box>
  )
}
