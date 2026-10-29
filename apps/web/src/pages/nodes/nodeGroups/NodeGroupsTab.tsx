import { type JSX, useState } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import { apiDetail } from '../../../api'
import { useIsOrgAdmin } from '../../../hooks/useIsOrgAdmin'
import { useConfirm } from '../../../components/ConfirmDialog'
import { useChangeOrgAction } from '../../../components/ChangeOrgDialog'
import { useRowActions } from '../../../hooks/useBusyIds'
import { useEventStream } from '../../../hooks/useEventStream'
import { useStreamedQuery } from '../../../hooks/useStreamedQuery'
import { absoluteTime, compactAge } from '../../../lib/relativeTime'
import Banner from '../../../ui/Banner'
import ListStates from '../../../ui/feed/ListStates'
import { AGE_SLOT } from '../../../ui/feed/geometry'
import type { FeedNote, FeedRowSpec } from '../../../ui/feed/types'
import { orgSlots, useOrgVisible } from '../../../ui/OrgIndicator'
import type { AssignableProject, CloudConnectionOptions, NodeGroupRow } from '../../../types'
import { listCloudConnectionOptions } from '../../settings/cloudConnections/api'
import NodeGroupDialog from './NodeGroupDialog'
import {
  convergence,
  groupAccount,
  groupPath,
  NO_EDIT_REASON,
  projectsTitle,
  shapesLabel,
  shapesTitle,
} from './groupFacts'
import { DisabledReason } from '../../../ui/DisabledReason'
import { useAwaitingApprovalReason } from '../../../lib/orgLifecycle'
import { listAssignableProjects, listNodeGroups } from './api'
import { deleteGroup } from './deleteGroup'
import { deleteItem, editItem } from '../../../components/tasks/rowActions'

/* /nodes › Groups — DYNAMIC NODE GROUPS, capacity the control plane holds by launching and terminating
 * EC2 nodes on a timer. A tab of /nodes because its output IS the roster next door.
 * The only question a group raises is whether reality matches the instruction: `node_count /
 * desired_total` is the ungated headline, the disc says which side of the gap, the reconcile age says
 * whether the loop runs, and `last_error` hangs UNDER the row in full, never behind a tooltip.
 * Fetch posture is `Nodes.tsx`'s: TanStack Query, refetch on SSE `entity: 'node_group'`, poll only while
 * the stream is down.
 */

/* Poll fallback while the stream is DOWN. Re-declared, not imported from `Nodes.tsx` (circular; a
   per-surface budget, equal by choice). */
const POLL_MS = 30000

/* ── The row's trailing cluster, priced by gate strength (`ui/feed/geometry`) ──────────────────────
   Beside an open group or node the roster narrows (the `group*` gates): shapes go first, then the AWS
   account, then the org, so the name is never what gives.
   THE CONVERGENCE FRACTION SURVIVES EVERY WIDTH. 76px: `12 / 12` ≈ 47px at 11px mono, with room for three
   digits. */
const COUNT_SLOT = 76
/* 148: `On-demand c6g.2xlarge` (21 glyphs, ~139px) is the longest single-line form. Gated `meta` (≤899px). */
const SHAPES_SLOT = 148
/* The AWS account, `meta` (⋯ → Edit shows it at every width). 140 holds `jaah-production`. */
const CREDENTIAL_SLOT = 140

/** The action's right-aligned row. */
const HEADER_SX = { display: 'flex', justifyContent: 'flex-end' } as const

const PANEL_SX = { display: 'flex', flexDirection: 'column', gap: 2, pt: 2 } as const

export function NodeGroupsTab(): JSX.Element {
  /* Org admin, matching the server: every route in `routes/node_groups.py` is
     `@org_admin(Tenancy.CALLER_PLANE)` — an Org Owner/Admin scoped to their own org (the operator sees
     every org). */
  const canEdit = useIsOrgAdmin()
  const awaitingApproval = useAwaitingApprovalReason()
  /* For the operator `GET /api/node-groups` is FLEET-WIDE (every tenant's groups), so each row names its
     tenant through the one org indicator; `ui/OrgIndicator.tsx::useOrgVisible` is the single home for the
     verdict. */
  const showOrg = useOrgVisible()
  const stream = useEventStream()
  const [confirm, confirmDialog] = useConfirm()
  const changeOrg = useChangeOrgAction('node_group')
  const [dialog, setDialog] = useState<{ row: NodeGroupRow | null } | null>(null)
  const { busy: busyIds, withBusy, error: rowError, setError: setRowError } = useRowActions<string>()

  const queryClient = useQueryClient()
  /* A write here also refreshes an open group's page (`NodeGroupDetailPage`'s `['node-group', id]`). */
  const refetchGroups = () => Promise.all([
    groupsQuery.refetch(),
    queryClient.invalidateQueries({ queryKey: ['node-group'] }),
  ])
  // Live on `entity: 'node_group'` (the reconcile loop's upsert signal), as Nodes is on `entity: 'node'`.
  const groupsQuery = useStreamedQuery(stream, { pollMs: POLL_MS, entity: 'node_group' }, {
    queryKey: ['node-groups'],
    queryFn: listNodeGroups,
  })

  /* The form's reference lists, fetched only once the dialog opens (`assignable-projects` is a per-call
     scan), then cached so later openings are instant. */
  const optionsQuery = useQuery({
    queryKey: ['node-group-options'],
    queryFn: listCloudConnectionOptions,
    enabled: dialog !== null,
  })
  const projectsQuery = useQuery({
    queryKey: ['node-group-assignable-projects'],
    queryFn: listAssignableProjects,
    enabled: dialog !== null,
  })

  const groups = groupsQuery.data ?? []
  const loadError = groupsQuery.isError
    ? apiDetail(groupsQuery.error, 'Failed to load the node groups.')
    : ''

  /* SHAPE-CHECKED: `get<T>` is an unchecked cast, and a `.map is not a function` would white-screen /nodes. */
  const rows: NodeGroupRow[] = Array.isArray(groups) ? groups : []
  const shapeError = !groupsQuery.isLoading && !Array.isArray(groups) && !groupsQuery.isError
    ? 'The node groups came back in an unexpected shape.'
    : ''

  const options: CloudConnectionOptions | undefined = optionsQuery.data
  const assignable: AssignableProject[] = Array.isArray(projectsQuery.data) ? projectsQuery.data : []
  /* BOTH FAILURES ARE SAID — the two reads fail independently, and naming only the first leaves an empty
     dropdown unexplained. One joined banner. */
  const optionsError = [
    optionsQuery.isError
      ? apiDetail(optionsQuery.error, 'Failed to load the AWS accounts and instance types.')
      : '',
    projectsQuery.isError
      ? apiDetail(projectsQuery.error, 'Failed to load the projects a group can serve.')
      : '',
  ].filter(message => message !== '').join(' ')

  // See `openDialog`: the banner dies when the next action starts, even a cancelled confirm.
  const removeRow = (row: NodeGroupRow) =>
    deleteGroup(row, { confirm, setError: setRowError, busy: (work) => withBusy(row.id, work), onDeleted: refetchGroups })

  /** Every entry into the form, clearing `rowError` on the way in: a row banner is about the write that
   *  failed and must not outlive it. Cleared at START, so a cancelled confirm clears it too. */
  function openDialog(row: NodeGroupRow | null) {
    setRowError('')
    setDialog({ row })
  }

  /* A pending org may not create a group (capacity; the API 403s it). Permission first: its reason is the
     more fundamental one. */
  const newReason = canEdit ? awaitingApproval : NO_EDIT_REASON
  const newButton = (
    <DisabledReason reason={newReason}>
      <Button
        type="button"
        variant="contained"
        color="primary"
        size="small"
        data-testid="node-groups-new"
        disabled={newReason !== undefined}
        title={newReason}
        onClick={() => openDialog(null)}
        // One-line label; phone `minHeight` is the AC11 touch floor (as `Nodes.tsx`'s Add node).
        sx={{ flex: '0 0 auto', whiteSpace: 'nowrap', minHeight: { xs: 44, sm: 'auto' } }}
      >
        New group
      </Button>
    </DisabledReason>
  )

  const gate = { disabled: !canEdit, disabledReason: canEdit ? undefined : NO_EDIT_REASON }
  const feedRows: FeedRowSpec[] = rows.map((row): FeedRowSpec => {
    const state = convergence(row)
    const account = groupAccount(row)
    /* `(org_id set, org null)` = the id names no `orgs` row. */
    const orgWords = row.org ? `org ${row.org.name}` : `org ${row.org_id} (no such org)`
    /* THE REASON IN FULL on the row's hover: AWS sentences put the actionable part (quota, subnet, wait) at the
       END, which a slot would eat. Muted on a disabled group (history, not live). */
    const notes: FeedNote[] = row.last_error !== ''
      ? [{
          key: 'error',
          tone: row.enabled ? 'error' : 'muted',
          text: row.enabled
            ? row.last_error
            : `While it was running: ${row.last_error}`,
        }]
      : []
    const changeOrgItem = changeOrg.menuItem({ id: row.id, name: row.name, org: row.org, href: groupPath(row.id) })
    return {
      key: row.id,
      indicator: {
        tone: state.tone,
        glyph: state.glyph,
        label: state.label,
        title: `${state.label} — ${row.node_count} of ${row.desired_total} instances exist`,
        // The state for oracles; `FeedList` adds the `data-` prefix.
        data: { state: state.label },
      },
      /* THE NAME, AS A NODE ROW'S: plain, and the link to the group's page (`NodeGroupDetailPage`), where the
         description, capacity table and projects live. The description stays in the accessible name. */
      title: row.name,
      titleText: row.name,
      href: groupPath(row.id),
      /* THE WHOLE ROW IN ONE SENTENCE — it silences the disc and markers, so it carries state and error. */
      ariaLabel:
        `${row.name}${row.description ? ` — ${row.description}` : ''} — ${state.label} — `
        + `${row.node_count} of ${row.desired_total} instances — ${account}`
        + (showOrg ? ` — ${orgWords}` : '')
        + (row.last_error !== '' ? ` — last reconcile failed: ${row.last_error}` : ''),
      muted: !row.enabled,
      busy: busyIds.has(row.id),
      notes,
      slots: [
        /* THE TENANT LEADS THE CLUSTER, composed via `orgSlots`. `meta`, not `squeeze`: `ui/feed/geometry.ts`
           places `squeeze` only left of the ungated tail, and the head wants the strongest gate. */
        ...orgSlots(showOrg, row.org, { gate: 'groupOrg', unresolvedId: row.org ? undefined : row.org_id }),
        {
          key: 'shapes',
          width: SHAPES_SLOT,
          gate: 'groupShapes',
          align: 'right',
          dim: true,
          mono: true,
          title: `${shapesTitle(row)} — serves ${projectsTitle(row)}`,
          node: shapesLabel(row),
        },
        {
          key: 'credential',
          width: CREDENTIAL_SLOT,
          gate: 'groupAccount',
          align: 'right',
          dim: true,
          title: `Launches into ${account}`,
          node: account,
        },
        {
          /* `—` is a STATE: the loop has never run for this group — the failure with no other symptom. The
             `title` spells it. */
          key: 'reconciled',
          width: AGE_SLOT,
          align: 'right',
          dim: true,
          mono: true,
          title: row.last_reconciled_at
            ? `Last reconciled ${absoluteTime(row.last_reconciled_at)}`
            : 'Never reconciled',
          node: compactAge(row.last_reconciled_at),
          data: { reconciled: row.last_reconciled_at ?? 'never' },
        },
        {
          /* THE HEADLINE FACT, ungated (see header). */
          key: 'count',
          width: COUNT_SLOT,
          align: 'right',
          mono: true,
          title: `${row.node_count} of ${row.desired_total} desired instances exist`,
          testId: 'node-group-count',
          data: { 'node-count': row.node_count, desired: row.desired_total },
          node: (
            <Box component="span" sx={{ fontVariantNumeric: 'tabular-nums' }}>
              {row.node_count}
              {/* Separator and target dimmer, so the moving number is where the eye lands. */}
              <Box component="span" sx={{ color: 'text.disabled' }}> / {row.desired_total}</Box>
            </Box>
          ),
        },
      ],
      /* Write verbs are offered DISABLED without permission, never hidden (`components/tasks/rowActions.tsx`). */
      actions: [
        editItem(`the node group ${row.name}`, () => openDialog(row), gate),
        ...(changeOrgItem ? [changeOrgItem] : []),
        deleteItem(`the node group ${row.name}`, () => void removeRow(row), gate),
      ],
    }
  })

  const showEmptyState = !groupsQuery.isLoading && loadError === '' && shapeError === '' && rows.length === 0

  return (
    <Box component="section" data-testid="node-groups-panel" sx={PANEL_SX}>
      {/* ONE call-to-action at a time: the empty state carries the button when empty (two same-named
          buttons are indistinguishable to a screen reader). */}
      {!showEmptyState && <Box sx={HEADER_SX}>{newButton}</Box>}

      {loadError && <Banner severity="error">{loadError}</Banner>}
      {shapeError && <Banner severity="error">{shapeError}</Banner>}
      {rowError && <Banner severity="error">{rowError}</Banner>}

      {/* A failed read with no rows: its banner is above, nothing below. No caption about slot gating,
          deliberately: chrome height above the first row is measured, and everything a narrow row drops is in
          ⋯ → Edit. */}
      <ListStates
        loading={groupsQuery.isLoading}
        skeleton={{ rows: 4 }}
        failed={rows.length === 0 && (loadError !== '' || shapeError !== '')}
        empty={rows.length === 0}
        noMatch={false}
        emptyTitle="No node groups yet"
        emptyScoped
        emptyAction={newButton}
        feed={{ ariaLabel: 'Node groups', testId: 'node-groups-list', sections: [{ key: 'groups', rows: feedRows }] }}
      />

      {dialog && (
        <NodeGroupDialog
          row={dialog.row}
          connections={options?.connections ?? []}
          instanceTypes={options?.instance_types ?? []}
          projects={assignable}
          optionsError={optionsError}
          onClose={() => setDialog(null)}
          onSaved={async () => { await refetchGroups() }}
        />
      )}

      {confirmDialog}
      {changeOrg.dialog}
    </Box>
  )
}

export default NodeGroupsTab
