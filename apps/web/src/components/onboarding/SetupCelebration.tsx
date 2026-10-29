import { useEffect, useMemo, useRef, type JSX } from 'react'
import Box from '@mui/material/Box'
import Paper from '@mui/material/Paper'
import Portal from '@mui/material/Portal'
import Typography from '@mui/material/Typography'
import { DoneBadge } from '../../ui/DoneBadge'

/* THE SETUP CELEBRATION — the full-screen moment the last onboarding step is done. Mounted by
 * `OnboardingWidget.tsx` once, when a guide it watched unfinished reads finished. Confetti falls over the whole
 * screen behind a small "You're all set" card, then it all goes on its own; a click or Escape ends it sooner.
 * CSS only, no dependency. Under reduced motion nothing falls: the card alone shows, then goes. */

/** How long it stays, in ms. */
export const CELEBRATION_MS = 4200

const PIECES = 90
/* The ring's hues (`ui/ProgressRing.tsx`'s gradient) plus a warm pair, so it reads as Jaah's and as a party. */
const COLORS = ['#38bdf8', '#3b82f6', '#a855f7', '#22c55e', '#f59e0b', '#f43f5e']

/** A stable pseudo-random in [0, 1) per piece and salt: the same pieces on every render. */
function rand(i: number, salt: number): number {
  const x = Math.sin(i * 12.9898 + salt * 78.233) * 43758.5453
  return x - Math.floor(x)
}

export default function SetupCelebration({ onDone }: { onDone: () => void }): JSX.Element {
  /* A ref, so a host re-render (a new `onDone`) never restarts the clock. */
  const done = useRef(onDone)
  done.current = onDone
  useEffect(() => {
    const timer = window.setTimeout(() => done.current(), CELEBRATION_MS)
    const onKey = (e: KeyboardEvent): void => {
      if (e.key === 'Escape') done.current()
    }
    window.addEventListener('keydown', onKey)
    return () => {
      window.clearTimeout(timer)
      window.removeEventListener('keydown', onKey)
    }
  }, [])

  const pieces = useMemo(
    () =>
      Array.from({ length: PIECES }, (_, i) => ({
        left: rand(i, 1) * 100,
        delay: rand(i, 2) * 900,
        duration: 2200 + rand(i, 3) * 1600,
        drift: (rand(i, 4) - 0.5) * 30,
        spin: 360 + rand(i, 5) * 720,
        width: 6 + rand(i, 6) * 6,
        round: rand(i, 7) > 0.7,
        color: COLORS[i % COLORS.length],
      })),
    [],
  )

  return (
    <Portal>
      <Box
        data-testid="setup-celebration"
        role="status"
        aria-live="polite"
        onClick={onDone}
        sx={{
          position: 'fixed',
          inset: 0,
          /* Over everything, the Sidebar's sheets and dialogs included: it is the moment, and it is brief. */
          zIndex: 2000,
          overflow: 'hidden',
          display: 'grid',
          placeItems: 'center',
          bgcolor: 'rgba(15, 23, 42, 0.18)',
          animation: `celebration-out 400ms ease-in ${CELEBRATION_MS - 400}ms forwards`,
          '@keyframes celebration-out': { to: { opacity: 0 } },
          '@keyframes celebration-fall': {
            '0%': { transform: 'translate3d(0, -10vh, 0) rotate(0deg)', opacity: 1 },
            '85%': { opacity: 1 },
            '100%': { transform: 'translate3d(var(--drift), 105vh, 0) rotate(var(--spin))', opacity: 0 },
          },
          '@keyframes celebration-pop': {
            '0%': { transform: 'scale(0.6)', opacity: 0 },
            '60%': { transform: 'scale(1.06)', opacity: 1 },
            '100%': { transform: 'scale(1)', opacity: 1 },
          },
        }}
      >
        <Box
          aria-hidden="true"
          sx={{ position: 'absolute', inset: 0, pointerEvents: 'none', '@media (prefers-reduced-motion: reduce)': { display: 'none' } }}
        >
          {pieces.map((p, i) => (
            <Box
              key={i}
              data-testid="setup-celebration-piece"
              style={{
                left: `${p.left}%`,
                width: p.width,
                height: p.round ? p.width : p.width * 1.6,
                backgroundColor: p.color,
                borderRadius: p.round ? '50%' : 2,
                animationDelay: `${p.delay}ms`,
                animationDuration: `${p.duration}ms`,
                ['--drift' as string]: `${p.drift}vw`,
                ['--spin' as string]: `${p.spin}deg`,
              }}
              sx={{
                position: 'absolute',
                top: 0,
                transform: 'translate3d(0, -10vh, 0)',
                animationName: 'celebration-fall',
                animationTimingFunction: 'cubic-bezier(.25, .6, .45, 1)',
                animationFillMode: 'both',
              }}
            />
          ))}
        </Box>
        <Paper
          elevation={12}
          sx={{
            position: 'relative',
            mx: 2,
            px: 4,
            py: 3,
            borderRadius: 3,
            textAlign: 'center',
            maxWidth: 360,
            animation: 'celebration-pop 520ms cubic-bezier(.2, .8, .2, 1) both',
            '@media (prefers-reduced-motion: reduce)': { animation: 'none' },
          }}
        >
          <Box sx={{ display: 'flex', justifyContent: 'center', mb: 1.5 }}>
            <DoneBadge size={48} />
          </Box>
          <Typography variant="h5" component="p" sx={{ fontWeight: 700, lineHeight: 1.2 }}>
            You’re all set
          </Typography>
          <Typography variant="body2" sx={{ color: 'text.secondary', mt: 0.75 }}>
            Setup is complete. Jaah is ready.
          </Typography>
        </Paper>
      </Box>
    </Portal>
  )
}
