import { useEffect, useState } from 'react'
import DialogContentText from '@mui/material/DialogContentText'
import { ORG_SWITCHED_EVENT, tabOrg, type ActiveOrg } from '../lib/activeOrg'
import { dropPersistedQueries } from '../queryClient'
import { FormDialog } from '../ui/FormDialog'

/* A TAB LEFT BEHIND BY AN ORG SWITCH (R4). Another tab of this browser switched org, so this tab's page
 * belongs to an org the session no longer acts as: the API refuses its requests (409 `org_switched`) and
 * the switching tab's broadcast says so at once (`lib/activeOrg.ts`). The only way on is a reload into the
 * new org, so the dialog has no close: a true `modal` with no Cancel — Escape and the backdrop do nothing.
 * The page stays visible behind a light scrim — what was on screen is still readable — but nothing under it
 * can be used. */
export default function StaleTabDialog() {
  const [org, setOrg] = useState<ActiveOrg | null>(null)

  useEffect(() => {
    const onSwitched = (evt: Event) => {
      const active = (evt as CustomEvent<ActiveOrg>).detail
      // Switched away and back again: this tab's org is the session's once more, so it may carry on.
      if (active.id === tabOrg()) {
        setOrg(null)
        return
      }
      // This page's cache is the org left behind: never written back over the one every tab shares.
      dropPersistedQueries()
      setOrg(active)
    }
    window.addEventListener(ORG_SWITCHED_EVENT, onSwitched)
    return () => window.removeEventListener(ORG_SWITCHED_EVENT, onSwitched)
  }, [])

  return (
    <FormDialog
      open={org !== null}
      title={`You switched to ${org?.name ?? ''} in another tab`}
      submitLabel="Reload"
      onSubmit={() => window.location.assign('/')}
      onClose={() => {}}
      modal
      hideCancel
      submitAutoFocus
      maxWidth="xs"
      testId="stale-tab-dialog"
      submitTestId="stale-tab-reload"
      describedBy="stale-tab-body"
      backdropSx={{ backgroundColor: 'rgba(0, 0, 0, 0.25)' }}
    >
      <DialogContentText id="stale-tab-body">
        This tab still shows the org you left. Reload to continue in {org?.name}.
      </DialogContentText>
    </FormDialog>
  )
}
