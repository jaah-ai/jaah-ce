import { type JSX, useEffect, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import Banner from '../ui/Banner'

/* ONE OAuth connect-return reader for the app. A provider's callback lands back on the console with
 * `?<param>=connected|error&reason=<slug>` (Slack: `components/SlackWorkspaceCard.tsx`; Gmail:
 * `pages/settings/email/connectReturn.tsx`), each with its own closed slug vocabulary. Read once and stripped in
 * one write (`replace`), or the banner returns on every reload. */

const REASON_PARAM = 'reason'

export type ConnectReturned = { connected: true } | { connected: false; reason: string }

/** Consume the return flags once: what came back (`null` when none), the sentence a failure says (`errors[reason]`,
 * else `fallback`; `null` on success or none), and a way to dismiss it. A success is the record's to say, never
 * the URL's: anyone can send `?<param>=connected`. */
export function useConnectReturn(
  param: string,
  errors: Record<string, string>,
  fallback: string,
): [ConnectReturned | null, string | null, () => void] {
  const [searchParams, setSearchParams] = useSearchParams()
  const [returned, setReturned] = useState<ConnectReturned | null>(null)
  const returnParam = searchParams.get(param)
  const reasonParam = searchParams.get(REASON_PARAM)
  useEffect(() => {
    if (returnParam === null) return
    // Anything but the server's `connected` is a failure: saying nothing is the state to avoid.
    setReturned(returnParam === 'connected' ? { connected: true } : { connected: false, reason: reasonParam ?? '' })
    setSearchParams(
      prev => {
        const next = new URLSearchParams(prev)
        next.delete(param)
        next.delete(REASON_PARAM)
        return next
      },
      { replace: true },
    )
  }, [param, returnParam, reasonParam, setSearchParams])
  // Own keys only: a crafted `reason` such as `constructor` must not read a prototype property as the message.
  const error = returned && !returned.connected
    ? (Object.hasOwn(errors, returned.reason) ? errors[returned.reason] : fallback)
    : null
  return [returned, error, () => setReturned(null)]
}

/** The return's error alone, where the connecting card itself is not offered — an org whose plan leaves the
 * integration out, which is exactly when the callback answers `off_plan` (`pages/settings/ownOrg/OwnOrgPanels.tsx`). */
export function ConnectReturnBanner({ param, errors, fallback }: {
  param: string
  errors: Record<string, string>
  fallback: string
}): JSX.Element | null {
  const [, error] = useConnectReturn(param, errors, fallback)
  return error ? <Banner severity="error">{error}</Banner> : null
}
