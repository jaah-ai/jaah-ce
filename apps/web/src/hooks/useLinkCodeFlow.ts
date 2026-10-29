import { type Dispatch, type SetStateAction, useEffect, useRef, useState } from 'react'
import { get, post } from '../api'
import { useSubmit } from './useSubmit'

/* Issue a one-time link code and wait for the bot to redeem it — the shape `components/SlackConnect` and
 * `components/TelegramConnect` share. `issue` POSTs `issuePath` and shows the code; while one is shown
 * `statusPath` is polled every `POLL_MS`. The server says which (no clock comparison, so a skewed browser
 * cannot claim a link): `read` names the status
 * `pending`, `expired` or `linked`, and a linked one reaches `onLinked` once. A failed poll is retried on the
 * next tick; past the expiry (with a minute's grace for clock skew) the wait ends even if every poll failed.
 * The adapter is read at fire time, so an inline one never re-arms the timers. `phase` starts at `initial`
 * (`loading` for a host that reads its link first); `setPhase` moves it on the host's own reads. The rest is
 * the `useSubmit` behind `issue` (`run` for the host's other writes, `busy`, `formError`). */

export type LinkCodePhase =
  | { kind: 'loading' }
  | { kind: 'idle' }
  | { kind: 'code'; code: string; url: string | null; expiresAt: number }
  | { kind: 'expired' }
  | { kind: 'done' }

const POLL_MS = 3000

export function useLinkCodeFlow<S>(adapter: {
  issuePath: string
  /** The refusal shown when no code could be issued. */
  issueError: string
  statusPath: string
  read: (status: S) => 'pending' | 'expired' | 'linked'
  onLinked: (status: S) => void
  initial?: 'idle' | 'loading'
}): ReturnType<typeof useSubmit> & {
  phase: LinkCodePhase
  setPhase: Dispatch<SetStateAction<LinkCodePhase>>
  issue: () => Promise<void>
} {
  const [phase, setPhase] = useState<LinkCodePhase>({ kind: adapter.initial ?? 'idle' })
  const submit = useSubmit()
  const ref = useRef(adapter)
  ref.current = adapter

  const issue = () =>
    submit.run(adapter.issueError, async () => {
      // `url` opens the bot; null when the API cannot name it.
      const issued = await post<{ code: string; expires_at: string; url?: string | null }>(adapter.issuePath)
      setPhase({ kind: 'code', code: issued.code, url: issued.url ?? null, expiresAt: Date.parse(issued.expires_at) })
    })

  const expiresAt = phase.kind === 'code' ? phase.expiresAt : 0
  useEffect(() => {
    if (!expiresAt) return
    let stopped = false
    const poll = window.setInterval(() => {
      get<S>(ref.current.statusPath)
        .then(status => {
          if (stopped) return
          const reading = ref.current.read(status)
          if (reading === 'pending') return
          setPhase({ kind: reading === 'linked' ? 'done' : 'expired' })
          if (reading === 'linked') ref.current.onLinked(status)
        })
        .catch(() => {
          if (!stopped && Date.now() > expiresAt + 60_000) setPhase({ kind: 'expired' })
        })
    }, POLL_MS)
    return () => {
      stopped = true
      window.clearInterval(poll)
    }
  }, [expiresAt])

  return { ...submit, phase, setPhase, issue }
}
