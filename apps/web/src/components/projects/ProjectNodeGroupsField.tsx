import { type JSX, useEffect, useRef, useState } from 'react'
import { Link, useLocation } from 'react-router-dom'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import Box from '@mui/material/Box'
import MuiLink from '@mui/material/Link'
import Typography from '@mui/material/Typography'
import { apiDetail } from '../../api'
import { useAuth, useCan } from '../../auth'
import { SUBSCRIPTION_PAGE } from '../../lib/companySetup'
import { isEnabled } from '../../lib/editionFeatures'
import { isOrgAdminSeat } from '../../lib/orgAdmin'
import { planSurfaceShown } from '../../lib/planSwitches'
import {
  GROUPS_KEY, getProjectGroups, listGroups, projectGroupsKey, putProjectGroups, refreshAfterGroupWrite, type GroupRef, type ProjectGroups,
} from '../../pages/nodes/placementGroups/api'
import { confirmMove } from '../../pages/nodes/placementGroups/confirmMove'
import { GROUPS_TAB_PATH } from '../../pages/nodes/placementGroups/PlacementGroupsTab'
import { matchesAny } from '../../ui/FilterRow'
import { AsyncMultiLookup, CHIP_SX, CHIP_TEXT_SX } from '../../ui/AsyncMultiLookup'
import type { LookupOption } from '../../ui/AsyncLookup'
import Banner from '../../ui/Banner'
import LoadingState from '../../ui/LoadingState'
import { grid } from '../../theme'
import { useConfirm } from '../ConfirmDialog'

/* THE PROJECT PAGE'S NODE GROUPS (code name `placement_groups`): "All" while the project has no group, else a chip
 * per group. Each pick or removal applies AT ONCE (no Save). Picking a group while on All replaces All; removing the
 * last group puts All back (D12): an empty set always means All. A change that takes away nodes with running tasks
 * answers 409 and asks first (`confirmMove`). Off the plan, or in the community edition (no routes: nothing is
 * fetched), it reads "All"; kept groups the plan no longer applies read only. Under it, the nodes the project's tasks
 * can start on now, as the server's placement computes them (`ProjectGroups.nodes`); read only, it links to where
 * they change (the Nodes page, or its Groups tab on a group). */

/** The field's `id`: a waiting task's "Node groups" link lands here (the API's `NODE_GROUPS_ANCHOR`, same value). */
export const NODE_GROUPS_ANCHOR = 'node-groups'

const ID = 'project-node-groups'
const ALL: LookupOption = { key: '', label: 'All' }
const HELP = { tip: 'Which nodes run this project; changes apply at once.' }
/** Names past this many fold into "and N more". */
const NAMES_SHOWN = 8

/** The nodes line: each node a link (offline ones say so), and the ones not named, counted. */
function NodesLine({ view, linked }: { view: ProjectGroups; linked: boolean }): JSX.Element {
  if (view.node_count === 0) return <>No node runs this project.</>
  const shown = view.nodes.slice(0, NAMES_SHOWN)
  const rest = view.node_count - shown.length
  return (
    <>
      Runs on{' '}
      {shown.map((n, i) => (
        <Box component="span" key={n.id} data-testid={`${ID}-node`}>
          {i > 0 && ', '}
          {linked ? <MuiLink component={Link} to={`/nodes/${encodeURIComponent(n.id)}`}>{n.name}</MuiLink> : n.name}
          {n.status === 'offline' && ' (offline)'}
        </Box>
      ))}
      {rest > 0 && `${shown.length ? ' and ' : ''}${rest} ${shown.length ? 'more' : rest === 1 ? 'node' : 'nodes'}`}
    </>
  )
}

function ReadOnlyChips({ names }: { names: string[] }): JSX.Element {
  return (
    <Box data-testid={`${ID}-readonly`} sx={{ display: 'flex', flexWrap: 'wrap', alignItems: 'center', rowGap: 0.5, minWidth: 0 }}>
      <Typography variant="body2" component="span" sx={{ mr: 1, color: 'text.secondary' }}>
        Node groups
      </Typography>
      {names.map(name => (
        <Box key={name} component="span" data-testid="lookup-chip" sx={{ ...CHIP_SX, pr: 1 }}>
          <Box component="span" data-testid={`${ID}-chip`} sx={CHIP_TEXT_SX}>{name}</Box>
        </Box>
      ))}
    </Box>
  )
}

export default function ProjectNodeGroupsField({ projectId, projectName }: {
  projectId: number
  /** Names the project in the move confirm ("This project" without it). */
  projectName?: string
}): JSX.Element {
  const { admin } = useAuth()
  const can = useCan()
  const canReadNodes = can('nodes:view')
  const client = useQueryClient()
  const [confirm, confirmDialog] = useConfirm()
  // Community edition: the routes are not mounted, so nothing is asked.
  const inEdition = isEnabled('placement_groups')
  const onPlan = inEdition && planSurfaceShown(admin, 'placement_groups_allowed')
  const read = useQuery({
    queryKey: projectGroupsKey(projectId),
    queryFn: () => getProjectGroups(projectId),
    enabled: inEdition,
  })
  const view = read.data
  const editable = onPlan && view?.editable === true
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState('')

  const { hash } = useLocation()
  const ref = useRef<HTMLDivElement>(null)
  useEffect(() => {
    if (hash === `#${NODE_GROUPS_ANCHOR}`) ref.current?.scrollIntoView?.({ block: 'center' })
  }, [hash, read.isSuccess])

  async function save(groupIds: string[]) {
    setBusy(true)
    setError('')
    try {
      let next: ProjectGroups
      try {
        next = await putProjectGroups(projectId, groupIds)
      } catch (err) {
        const yes = await confirmMove(err, confirm, projectName || 'This project')
        if (yes === null) throw err
        if (!yes) return
        next = await putProjectGroups(projectId, groupIds, true)
      }
      client.setQueryData(projectGroupsKey(projectId), next)
      // Each group's project count and the task pin's node list moved.
      void refreshAfterGroupWrite(client, projectGroupsKey(projectId))
    } catch (err) {
      setError(apiDetail(err, 'Failed to change node groups'))
    } finally {
      setBusy(false)
    }
  }

  const picked: GroupRef[] = view?.mode === 'groups' ? view.groups : []
  let field: JSX.Element
  if (inEdition && read.isError) field = <Banner severity="error">Failed to load node groups</Banner>
  else if (inEdition && !view) field = <LoadingState rows={1} />
  else if (!editable) field = <ReadOnlyChips names={picked.length ? picked.map(g => g.name) : [ALL.label]} />
  else {
    const value: LookupOption[] = picked.length ? picked.map(g => ({ key: g.id, label: g.name })) : [ALL]
    field = (
      <AsyncMultiLookup
        id={ID}
        testId={ID}
        label="Node groups"
        help={HELP}
        itemNoun="group"
        chipTestId={`${ID}-chip`}
        placeholder="Add a group"
        noOptionsText="No other group"
        fetchOnFocus
        disabled={busy}
        value={value}
        fetchOptions={async q => {
          const { groups } = await client.ensureQueryData({ queryKey: GROUPS_KEY, queryFn: listGroups })
          return groups
            .filter(g => !picked.some(p => p.id === g.id) && matchesAny(q, g.name))
            .map(g => ({ key: g.id, label: g.name }))
        }}
        // All goes when a group is picked, and comes back when the last one is removed: never removed itself.
        chipLocked={option => (option.key === ALL.key ? 'Pick a group to run on some nodes only.' : null)}
        onChange={next => {
          if (busy) return
          void save(next.filter(o => o.key !== ALL.key).map(o => o.key))
        }}
      />
    )
  }
  const billingHint = inEdition && !onPlan && isEnabled('billing') && isOrgAdminSeat(admin)
  return (
    <Box id={NODE_GROUPS_ANCHOR} ref={ref} sx={{ minWidth: 0, maxWidth: grid(90.5) }} data-testid={`${ID}-field`}>
      {field}
      <Box aria-live="polite">
        {!inEdition && (
          <Typography variant="caption" component="p" data-testid={`${ID}-helper`} sx={{ mt: 0.5, color: 'text.secondary' }}>
            Runs on every node.
          </Typography>
        )}
        {view && (
          <Typography variant="caption" component="p" data-testid={`${ID}-helper`} sx={{ mt: 0.5, color: 'text.secondary' }}>
            {view.mode === 'groups' && !view.active && 'Not active on your plan. '}
            <NodesLine view={view} linked={canReadNodes} />
            {(!editable || view.node_count === 0) && can('nodes:edit') && (
              <>
                {' · '}
                <MuiLink component={Link} to={view.mode === 'groups' ? GROUPS_TAB_PATH : '/nodes'} data-testid={`${ID}-change`}>
                  Change
                </MuiLink>
              </>
            )}
          </Typography>
        )}
        {billingHint && (
          <Typography variant="caption" component="p" data-testid={`${ID}-plan-hint`} sx={{ color: 'text.secondary' }}>
            Choosing node groups is on the Business plan.{' '}
            <MuiLink component={Link} to={SUBSCRIPTION_PAGE}>Billing</MuiLink>
          </Typography>
        )}
        {error && (
          <Typography variant="caption" component="p" data-testid={`${ID}-error`} sx={{ mt: 0.5, color: 'error.main' }}>
            {error}
          </Typography>
        )}
      </Box>
      {confirmDialog}
    </Box>
  )
}
