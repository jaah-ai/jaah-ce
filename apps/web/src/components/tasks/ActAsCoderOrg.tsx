import { useState } from 'react'
import Button from '@mui/material/Button'
import Menu from '@mui/material/Menu'
import MenuItem from '@mui/material/MenuItem'
import { apiDetail } from '../../api'
import { useAuth } from '../../auth'
import { enterActAs, useActingAs } from '../../lib/actAs'
import { ORG_IDS_MAX, useOrgSearch } from '../../lib/orgs'
import type { OrgRef } from '../../types'

/** The way past a `no_account` dispatch block for an operator on their own plane: the operator plane
 *  has no coding agent account, but an org they can act as may. Lists the live tenant orgs a coding agent can run
 *  for (`Org.has_coder`); picking one acts as it in this tab (`lib/actAs.ts`). Nothing
 *  for anyone else, while already acting (the roster is the operator's read), or on a tenant org's project picked
 *  from "All orgs": switching away would drop that project, whose own org is what lacks the account.
 *  `projectOrg`: the blocked project's org. */
export default function ActAsCoderOrg({ projectOrg }: { projectOrg: OrgRef | null | undefined }) {
  const { admin } = useAuth()
  const acting = useActingAs()
  const [anchor, setAnchor] = useState<HTMLElement | null>(null)
  if (admin?.is_operator !== true || acting) return null
  if (projectOrg && projectOrg.id !== admin.org?.id) return null
  return (
    <>
      <Button
        size="small"
        color="warning"
        aria-haspopup="menu"
        aria-expanded={anchor !== null}
        onClick={e => setAnchor(e.currentTarget)}
        data-testid="task-form-act-as-coder"
        sx={{ minWidth: 0, py: 0, ml: 1, verticalAlign: 'baseline', textTransform: 'none' }}
      >
        Act as an org that has one…
      </Button>
      {anchor && <CoderOrgMenu anchor={anchor} onClose={() => setAnchor(null)} />}
    </>
  )
}

/* Mounted only while open, so the roster is read on demand, never on every New Chat. */
function CoderOrgMenu({ anchor, onClose }: { anchor: HTMLElement; onClose: () => void }) {
  const roster = useOrgSearch({ per: ORG_IDS_MAX }).query
  const orgs = (roster.data?.items ?? []).filter(org => !org.is_operator_plane && org.enabled && org.has_coder)
  return (
    <Menu open anchorEl={anchor} onClose={onClose} data-testid="task-form-act-as-coder-menu">
      {roster.isPending ? <MenuItem disabled>Loading…</MenuItem>
        : roster.error ? <MenuItem disabled>{apiDetail(roster.error, 'The orgs could not be read.')}</MenuItem>
        : orgs.length === 0 ? <MenuItem disabled>No org has an enabled coding agent account</MenuItem>
        : orgs.map(org => (
          <MenuItem
            key={org.id}
            title={`Switches this tab to ${org.name}'s Owner seat`}
            onClick={() => { onClose(); enterActAs({ id: org.id, slug: org.slug, name: org.name }) }}
          >
            Act as {org.name}
          </MenuItem>
        ))}
    </Menu>
  )
}
