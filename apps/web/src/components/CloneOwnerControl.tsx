import { useState } from 'react'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import Typography from '@mui/material/Typography'
import { saveCloneOwner, type CloneRef } from '../lib/cloneOwner'
import type { TitleNotice } from '../lib/slotTitle'
import { searchUserOptions } from '../lib/users'
import { AsyncLookup, type LookupOption } from '../ui/AsyncLookup'
import Banner from '../ui/Banner'

/**
 * "Clone as" — pick whose GitHub account this project's pool clone on this node uses.
 *
 * The clone is per (node, project), so this acts on EVERY slot of the pair, not the one the drawer is
 * showing; the help line says so. Collapsed until asked for: it is a repair. The person is a server search
 * (`lib/users.ts::searchUsers`), a capped page per keystroke, never the org's roster.
 */
export default function CloneOwnerControl({ clone, onSaved }: { clone: CloneRef; onSaved: () => void }) {
  const [open, setOpen] = useState(false)
  const [owner, setOwner] = useState<LookupOption | null>(null)
  const userId = owner?.key ?? ''
  const [saving, setSaving] = useState(false)
  const [notice, setNotice] = useState<TitleNotice | null>(null)

  async function save() {
    setSaving(true)
    const result = await saveCloneOwner(clone, userId)
    setSaving(false)
    setNotice(result.notice)
    if (result.ok) {
      setOpen(false)
      onSaved()
    }
  }

  return (
    /* `mt: 2`, the drawer sections' gap (`slot-repos`, `slot-detail-actions`), so it does not read as one more
       line of the MetaRow above. */
    <Box sx={{ mt: 2 }} data-testid="clone-owner">
      {notice && <Banner severity={notice.kind === 'error' ? 'error' : 'info'}>{notice.text}</Banner>}
      {!open ? (
        /* Pulled left by the small text button's 5px inline padding, so its label lines up with the drawer's rows. */
        <Button size="small" variant="text" onClick={() => { setNotice(null); setOpen(true) }} sx={{ ml: '-5px' }}>
          Change clone owner
        </Button>
      ) : (
        <Box>
          <Typography variant="caption" component="p" sx={{ color: 'text.secondary', mb: 1 }}>
            GitHub account that clones {clone.project} on {clone.nodeLabel}, for all its slots.
          </Typography>
          <Box sx={{ display: 'flex', gap: 1, alignItems: 'center', flexWrap: 'wrap' }}>
            <Box sx={{ minWidth: 220, flex: '1 1 220px' }}>
              <AsyncLookup
                id="clone-owner"
                testId="clone-owner-user"
                ariaLabel="Clone owner"
                placeholder="Find a person"
                value={owner}
                onChange={setOwner}
                fetchOptions={searchUserOptions}
                fetchOnFocus
                noOptionsText="No one matches"
              />
            </Box>
            <Button size="small" variant="contained" disabled={!userId || saving} onClick={() => void save()}>
              Save
            </Button>
            <Button size="small" onClick={() => setOpen(false)} disabled={saving}>Cancel</Button>
          </Box>
        </Box>
      )}
    </Box>
  )
}
