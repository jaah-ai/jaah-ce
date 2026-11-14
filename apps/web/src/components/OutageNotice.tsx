import { useEffect, type JSX } from 'react'
import Box from '@mui/material/Box'
import { Banner } from '../ui/Banner'
import { RADIUS, grid } from '../theme'
import { noteAnswered, noteDown, probeApi, useReachability } from '../net/reachability'

/* THE CONSOLE'S ONE "CAN'T REACH THE SERVER" NOTICE, mounted once in `App.tsx` above every route, so it also covers
 * the sign-in pages and the identity read on boot, which the Sidebar's `ConnectionIndicator` never sees. The state is
 * `net/reachability.ts`'s; every request the console sends feeds it, `useHealth`'s polling while the stream is down
 * included.
 *
 * While the API has not answered, this probes `GET /api/health` (unauthenticated, never relayed): at once on the first
 * unanswered request, so a single blip clears before anything shows, then with backoff while it stays down. The first
 * answer from anywhere clears it. Fixed at the top centre, over the page rather than in it: the page under it may be
 * a spinner or the sign-in form, and it never covers the Sidebar's indicator (left rail, or the phone's bottom bar). */

/** Probe delays while down, ms: the last repeats. */
export const PROBE_BACKOFF_MS = [1_000, 2_000, 4_000, 8_000, 15_000, 30_000] as const

export const OUTAGE_COPY = 'Can’t reach the server — retrying…'

function useProbe(active: boolean): void {
  useEffect(() => {
    if (!active) return
    let stopped = false
    let attempt = 0
    let timer: ReturnType<typeof setTimeout> | undefined
    const run = async () => {
      const answered = await probeApi()
      if (stopped) return
      if (answered) {
        noteAnswered()
        return
      }
      noteDown()
      timer = setTimeout(run, PROBE_BACKOFF_MS[Math.min(attempt++, PROBE_BACKOFF_MS.length - 1)])
    }
    void run()
    return () => {
      stopped = true
      clearTimeout(timer)
    }
  }, [active])
}

export default function OutageNotice(): JSX.Element | null {
  const state = useReachability()
  useProbe(state !== 'ok')
  if (state !== 'down') return null
  return (
    <Box
      data-testid="outage-notice"
      sx={{
        position: 'fixed',
        top: grid(2),
        left: '50%',
        transform: 'translateX(-50%)',
        // Above the page's floats and the Help launcher (55); under MUI's modals (1300).
        zIndex: t => t.zIndex.drawer + 1,
        width: 'max-content',
        maxWidth: `calc(100vw - ${grid(5)})`,
        bgcolor: 'background.paper',
        borderRadius: RADIUS.sm,
        boxShadow: 3,
        '& .MuiAlert-message': { whiteSpace: 'normal', overflowWrap: 'anywhere' },
      }}
    >
      <Banner severity="warning" role="status">
        {OUTAGE_COPY}
      </Banner>
    </Box>
  )
}
