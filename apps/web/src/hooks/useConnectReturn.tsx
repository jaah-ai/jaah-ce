import { type JSX, useEffect, useRef, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import Banner from '../ui/Banner'

/* ONE OAuth connect-return reader for the app. A provider's callback lands back on the console with
 * `?<param>=connected|error&reason=<slug>` (Slack: `components/SlackWorkspaceCard.tsx`; Gmail:
 * `pages/settings/email/connectReturn.tsx`; the forges: `pages/ProfilePage.tsx`, with the `forge`) or, the
 * credential connect's spelling, `?connected=1` / `?connect_error=<slug>` (`pages/credentials/CredentialEditPage.tsx`),
 * each with its own closed slug vocabulary. Read once and stripped in one write (`replace`), or the banner returns
 * on every reload. */

const REASON_PARAM = 'reason'

export type ConnectReturnSpec = {
  /** The param the callback answers on. */
  param: string
  /** Its one success spelling (`connected`). */
  success?: string
  /** The failure's own param, carrying the slug (`connect_error`): present, it is a failure on its own. Without one
   *  the slug rides `reason` beside `param`. */
  error?: string
  /** Other params the callback carries (`forge`): stripped with the rest, handed back on `params`. */
  carries?: readonly string[]
  /** Written in the same strip (Profile's switch to the Git tab), so neither write clobbers the other. */
  set?: Record<string, string>
}

export type ConnectReturned = { connected: boolean; reason: string; params: Record<string, string> }

/** Consume the return flags once: what came back (`null` when none), the sentence a failure says (`errors[reason]`,
 * else `fallback`; `null` on success or none), and a way to dismiss it. A success is the record's to say, never
 * the URL's: anyone can send `?<param>=connected`. */
export function useConnectReturn(
  spec: ConnectReturnSpec,
  errors: Record<string, string> = {},
  fallback = '',
): [ConnectReturned | null, string | null, () => void] {
  const [searchParams, setSearchParams] = useSearchParams()
  const [returned, setReturned] = useState<ConnectReturned | null>(null)
  // Read at effect time, so a spec built in render does not re-run the read.
  const live = useRef(spec)
  live.current = spec
  useEffect(() => {
    const { param, success = 'connected', error, carries = [], set = {} } = live.current
    const value = searchParams.get(param)
    const slug = searchParams.get(error ?? REASON_PARAM)
    if (value === null && (error === undefined || slug === null)) return
    // Anything but the server's success spelling is a failure: saying nothing is the state to avoid.
    const connected = value === success
    setReturned({
      connected,
      reason: connected ? '' : slug ?? '',
      params: Object.fromEntries(carries.map(key => [key, searchParams.get(key) ?? ''])),
    })
    setSearchParams(
      prev => {
        const next = new URLSearchParams(prev)
        for (const key of [param, error ?? REASON_PARAM, ...carries]) next.delete(key)
        for (const [key, to] of Object.entries(set)) next.set(key, to)
        return next
      },
      { replace: true },
    )
  }, [searchParams, setSearchParams])
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
  const [, error] = useConnectReturn({ param }, errors, fallback)
  return error ? <Banner severity="error">{error}</Banner> : null
}
