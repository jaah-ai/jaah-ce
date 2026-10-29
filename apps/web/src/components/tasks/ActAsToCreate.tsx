import Button from '@mui/material/Button'
import { useAuth } from '../../auth'
import { enterActAs } from '../../lib/actAs'
import type { OrgRef } from '../../types'

/** The way into the org a create was refused in (`shared.tsx::refusedInOrg`): an operator acts as it in this
 *  tab (`lib/actAs.ts`), and the typed task is kept, so the next press files it there. Nothing for anyone else:
 *  only an operator's "All orgs" roster offers another org's project. */
/** `onActAs`: runs before the switch — the create row arms its held press with it. */
export default function ActAsToCreate({ org, onActAs }: { org: OrgRef | null; onActAs?: () => void }) {
  const { admin } = useAuth()
  if (!org || admin?.is_operator !== true) return null
  return (
    <Button
      size="small"
      color="warning"
      onClick={() => { onActAs?.(); enterActAs(org) }}
      title={`Switches this tab to ${org.name}'s Owner seat`}
      data-testid="create-act-as"
      sx={{ minWidth: 0, py: 0, ml: 1, verticalAlign: 'baseline' }}
    >
      Act as {org.name}
    </Button>
  )
}
