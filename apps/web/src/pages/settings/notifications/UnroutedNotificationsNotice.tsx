import { useState, type JSX } from 'react'
import { Link as RouterLink } from 'react-router-dom'
import Link from '@mui/material/Link'
import { useQuery } from '@tanstack/react-query'
import { get } from '../../../api'
import { useAuth } from '../../../auth'
import { isEnabled } from '../../../lib/editionFeatures'
import { readString, writeString } from '../../../lib/safeStorage'
import Banner from '../../../ui/Banner'
import RelativeTime from '../../../ui/RelativeTime'

/* Settings' one notice for task notifications nobody could receive (`GET /api/settings/unrouted-notifications`):
 * no recipient and no channel is set up, which is the org's setup gap, so it is said here once rather
 * than on every task's Activity. Absent when nothing went unsent; silent when the read fails (a notice, not a page).
 * It names the orgs they were in (most first) once one is not the viewer's own — the operator's view of every
 * org; a tenant's own org goes unsaid. Its ✕ hides it in this browser until a newer notification goes unrouted:
 * the dismissal stores the newest one's `last_at`, so a later `last_at` brings the notice back.
 * Never in the community edition: its one user is every task's recipient, and the Integrations it points to are absent. */

type Unrouted = {
  count: number
  last_at: string | null
  last_task_id: number | null
  window_days: number
  /** Optional: an older API sends none. */
  orgs?: { id: string; name: string }[]
}

/** Named orgs before "and N more": a notice line, not a roster. */
const ORGS_NAMED = 3

/** "Acme", "Acme and Beta", "Acme, Beta, Gamma and 2 more". */
function orgsText(names: string[]): string {
  const shown = names.slice(0, ORGS_NAMED)
  const rest = names.length - shown.length
  if (rest > 0) return `${shown.join(', ')} and ${rest} more`
  return shown.length > 1 ? `${shown.slice(0, -1).join(', ')} and ${shown[shown.length - 1]}` : shown.join('')
}

export const UNROUTED_NOTIFICATIONS_URL = '/api/settings/unrouted-notifications'

/** localStorage key holding the newest unrouted notification the viewer dismissed (its `last_at`). */
export const UNROUTED_DISMISSED_KEY = 'settings.unroutedNotifications.dismissed'

/** What identifies the newest unrouted notification: its time, or the count when the API sends none. */
function newestMark(data: Unrouted): string {
  return data.last_at ?? `count:${data.count}`
}

export default function UnroutedNotificationsNotice(): JSX.Element | null {
  const ownOrg = useAuth().admin?.org?.id
  const { data } = useQuery({
    queryKey: [UNROUTED_NOTIFICATIONS_URL],
    queryFn: () => get<Unrouted>(UNROUTED_NOTIFICATIONS_URL),
    retry: false,
    enabled: isEnabled('users_invites'),
  })
  const [dismissed, setDismissed] = useState(() => readString('local', UNROUTED_DISMISSED_KEY))
  if (!isEnabled('users_invites') || !(typeof data?.count === 'number' && data.count > 0)) return null
  const mark = newestMark(data)
  if (dismissed === mark) return null
  const dismiss = () => {
    writeString('local', UNROUTED_DISMISSED_KEY, mark)
    setDismissed(mark)
  }
  const orgs = data.orgs ?? []
  const named = orgs.some(o => o.id !== ownOrg) ? orgs.map(o => o.name) : []
  return (
    <Banner severity="warning" testId="settings-unrouted-notifications" onDismiss={dismiss}>
      {data.count} task notification{data.count === 1 ? '' : 's'} in the last {data.window_days} days reached no one
      {named.length > 0 && <> in <strong data-testid="settings-unrouted-orgs">{orgsText(named)}</strong></>}:
      no recipient or channel is set up.
      {data.last_at && <> Latest <RelativeTime iso={data.last_at} />
        {data.last_task_id !== null && <> on <Link component={RouterLink} to={`/tasks/${data.last_task_id}`}>
          task #{data.last_task_id}</Link></>}.</>}
      {' '}Set up a channel under <Link component={RouterLink} to="/settings?area=integrations">Integrations</Link>, or
      your own under <Link component={RouterLink} to="/profile?tab=notifications">Profile › Notifications</Link>.
    </Banner>
  )
}
