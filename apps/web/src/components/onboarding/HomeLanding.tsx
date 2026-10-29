import type { JSX } from 'react'
import { Navigate } from 'react-router-dom'

/* THE CONSOLE HOME (`/`) — where sign-in, the `*` catch-all and every stale link land: the board, for everyone.
 * An org owner/admin whose setup is unfinished finds it in the docked checklist the shell draws on every page
 * (`OnboardingWidget.tsx`), so nothing here asks the server or waits. */

const HOME_PATH = '/tasks'

export default function HomeLanding(): JSX.Element {
  return <Navigate to={HOME_PATH} replace />
}
