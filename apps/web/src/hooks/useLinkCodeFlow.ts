import { type Dispatch, type SetStateAction, useEffect, useRef, useState } from 'react'
import { get, post } from '../api'
import { usePoll } from './usePoll'
import { type Landed, useSubmit } from './useSubmit'
import { RUNNING_POLL_MS } from '../lib/pollIntervals'

/* Issue a one-time link code and wait for the bot to redeem it — the shape `components/SlackConnect` and
 * `components/TelegramConnect` share. `issue` POSTs `issuePath` and shows the code; while one is shown
 * `statusPath` is polled every `RUNNING_POLL_MS` (`usePoll`: a hidden tab waits). The server says which (no clock comparison, so a skewed browser
 * cannot claim a link): `read` names the status
 * `pending`, `expired` or `linked`, and a linked one reaches `onLinked` once. A failed poll is retried on the
 * next tick; past the expiry (with a minute's grace for clock skew) the wait ends even if every poll failed.
 * The adapter is read at fire time, so an inline one never re-arms the timers. `phase` starts at `initial`
 * (`loading` for a host that reads its link first); `setPhase` moves it on the host's own reads. The rest is
 * the `useSubmit` behind `issue` (`run` for the host's other writes, `busy`, `formError`). */

/** What an issue route answers: the code, its expiry, and the link(s) that carry it (`url`, or a host's own). */
export type IssuedCode = { code: string; expires_at: string; url?: string | null } & Record<string, unknown>

export type LinkCodePhase =
  | { kind: 'loading' }
  | { kind: 'idle' }
  | { kind: 'code'; code: string; url: string | null; expiresAt: number; issued: IssuedCode }
  | { kind: 'expired' }
  | { kind: 'done' }

/** What a link's status route reports: `state` is what `read` maps, `Linked` its redeemed spelling (Slack says
 *  `redeemed`, Telegram `linked`); each host adds what its bot stored. */
export type LinkStatus<Linked extends string = 'linked'> = { state: 'none' | 'pending' | 'expired' | Linked }

function useLinkCodeFlow<S extends LinkStatus<string>>(adapter: {
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
  issue: () => Promise<Landed<void>>
} {
  const [phase, setPhase] = useState<LinkCodePhase>({ kind: adapter.initial ?? 'idle' })
  const submit = useSubmit()
  const ref = useRef(adapter)
  ref.current = adapter

  const issue = () =>
    submit.run(adapter.issueError, async () => {
      // `url` opens the bot; null when the API cannot name it. A host reads any other link off `issued`.
      const issued = await post<IssuedCode>(adapter.issuePath)
      setPhase({ kind: 'code', code: issued.code, url: issued.url ?? null, expiresAt: Date.parse(issued.expires_at), issued })
    })

  const expiresAt = phase.kind === 'code' ? phase.expiresAt : 0
  const expiresRef = useRef(expiresAt)
  expiresRef.current = expiresAt
  // A read that lands after the code it polled for is gone (linked, expired, a new one issued, the host
  // unmounted) says nothing. -1 can equal no expiry, so an unmounted host matches no read.
  useEffect(() => () => { expiresRef.current = -1 }, [])
  const stale = () => expiresRef.current !== expiresAt
  usePoll(() =>
    get<S>(ref.current.statusPath)
      .then(status => {
        if (stale()) return
        const reading = ref.current.read(status)
        if (reading === 'pending') return
        setPhase({ kind: reading === 'linked' ? 'done' : 'expired' })
        if (reading === 'linked') ref.current.onLinked(status)
      })
      .catch(() => {
        if (!stale() && Date.now() > expiresAt + 60_000) setPhase({ kind: 'expired' })
      }),
  expiresAt ? RUNNING_POLL_MS : null)

  return { ...submit, phase, setPhase, issue }
}

/** What the Alerts panel's Add channel flows (`components/SlackConnect`, `components/TelegramConnect`) take: told
 *  once the bot linked the chat, and once the flow is over — with the problem to show, if it ended on one. */
export type ConnectFlowProps = {
  onLinked: () => void
  onEnd: (problem?: string) => void
}

/** A one-shot flow's end, reported once: linked (`done`), cancelled (`idle`), `expired` (its sentence) or refused
 *  (`formError`). The flow starts at `loading`, so its first paint ends nothing. */
function useLinkFlowEnd(phase: LinkCodePhase, formError: string, expired: string, onEnd: ConnectFlowProps['onEnd']): void {
  const ref = useRef(onEnd)
  ref.current = onEnd
  useEffect(() => {
    if (formError) ref.current(formError)
    else if (phase.kind === 'expired') ref.current(expired)
    else if (phase.kind === 'done' || phase.kind === 'idle') ref.current()
  }, [phase.kind, formError, expired])
}

/** A host's whole one-shot flow (`ConnectFlowProps`): mounted is started, issuing one code (a ref, so a development
 *  double effect issues one), and it ends once through `useLinkFlowEnd` with `expired` as the expiry's sentence. */
export function useConnectFlow<S extends LinkStatus<string>>(
  adapter: Omit<Parameters<typeof useLinkCodeFlow<S>>[0], 'onLinked' | 'initial'>,
  expired: string,
  { onLinked, onEnd }: ConnectFlowProps,
): Pick<ReturnType<typeof useLinkCodeFlow<S>>, 'phase' | 'setPhase'> {
  const { phase, setPhase, issue, formError } = useLinkCodeFlow({ ...adapter, initial: 'loading', onLinked: () => onLinked() })
  useLinkFlowEnd(phase, formError, expired, onEnd)
  const started = useRef(false)
  useEffect(() => {
    if (started.current) return
    started.current = true
    void issue()
  }, [issue])
  return { phase, setPhase }
}
