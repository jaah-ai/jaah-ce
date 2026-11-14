import { useEffect, useMemo, useRef, type JSX } from 'react'
import Box from '@mui/material/Box'
import Paper from '@mui/material/Paper'
import Portal from '@mui/material/Portal'
import Typography from '@mui/material/Typography'
import { DoneBadge } from '../../ui/DoneBadge'
import { FONT_WEIGHT, LINE_HEIGHT, RADIUS, SIZE, grid } from '../../theme'

/* THE SETUP CELEBRATION — the full-screen moment the last onboarding step is done. Mounted by
 * `OnboardingWidget.tsx` once, when a guide it watched unfinished reads finished. Confetti falls over the whole
 * screen behind a small "You're all set" card, then it all goes on its own; a click or Escape ends it sooner.
 * CSS only, no dependency. Under reduced motion nothing falls: the card alone shows, then goes. Its words are the
 * caller's: the integrations gallery thanks a Beta vote with it (`IntegrationGallery.tsx`). */

/** How long it stays, in ms. */
export const CELEBRATION_MS = 4200

const PIECES = 90
/* The palette's six hues, so it reads as Jaah's and as a party in either colour scheme. */
const COLORS = ['info.main', 'primary.main', 'secondary.main', 'success.main', 'warning.main', 'error.main']

/** A stable pseudo-random in [0, 1) per piece and salt: the same pieces on every render. */
function rand(i: number, salt: number): number {
  const x = Math.sin(i * 12.9898 + salt * 78.233) * 43758.5453
  return x - Math.floor(x)
}

export default function SetupCelebration({
  onDone,
  title = 'You’re all set',
  message = 'Setup is complete. Jaah is ready.',
  testId = 'setup-celebration',
}: {
  onDone: () => void
  title?: string
  message?: string
  testId?: string
}): JSX.Element {
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
        /* 1, 1.5 or 2 spacing units wide; a strip is about 1.6 times as tall. */
        units: 1 + Math.floor(rand(i, 6) * 3) / 2,
        round: rand(i, 7) > 0.7,
        color: COLORS[i % COLORS.length],
      })),
    [],
  )

  return (
    <Portal>
      <Box
        data-testid={testId}
        role="status"
        aria-live="polite"
        onClick={onDone}
        sx={[{
          position: 'fixed',
          inset: 0,
          /* Over everything, the Sidebar's sheets and dialogs included: it is the moment, and it is brief. */
          zIndex: t => t.zIndex.tooltip,
          overflow: 'hidden',
          display: 'grid',
          placeItems: 'center',
          // A dim wash in both schemes: the ink colour in light, the (black) ground in dark.
          bgcolor: t => `color-mix(in srgb, ${t.vars.palette.common.onBackground} 18%, transparent)`,
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
        }, t => t.applyStyles('dark', { bgcolor: `color-mix(in srgb, ${t.vars.palette.common.background} 40%, transparent)` })]}
      >
        <Box
          aria-hidden="true"
          sx={{ position: 'absolute', inset: 0, pointerEvents: 'none', '@media (prefers-reduced-motion: reduce)': { display: 'none' } }}
        >
          {pieces.map((p, i) => (
            <Box
              key={i}
              data-testid="setup-celebration-piece"
              sx={{
                left: `${p.left}%`,
                width: grid(p.units),
                height: grid(p.round ? p.units : Math.ceil(p.units * 3.2) / 2),
                bgcolor: p.color,
                borderRadius: p.round ? RADIUS.round : RADIUS.sm,
                animationDelay: `${p.delay}ms`,
                animationDuration: `${p.duration}ms`,
                '--drift': `${p.drift}vw`,
                '--spin': `${p.spin}deg`,
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
            borderRadius: RADIUS.md,
            textAlign: 'center',
            maxWidth: SIZE.measureNarrow,
            animation: 'celebration-pop 520ms cubic-bezier(.2, .8, .2, 1) both',
            '@media (prefers-reduced-motion: reduce)': { animation: 'none' },
          }}
        >
          <Box sx={{ display: 'flex', justifyContent: 'center', mb: 1.5 }}>
            <DoneBadge size={2 * SIZE.iconLg} />
          </Box>
          <Typography variant="h5" component="p" sx={{ fontWeight: FONT_WEIGHT.bold, lineHeight: LINE_HEIGHT.snug }}>
            {title}
          </Typography>
          <Typography variant="body2" sx={{ color: 'text.secondary', mt: 0.5 }}>
            {message}
          </Typography>
        </Paper>
      </Box>
    </Portal>
  )
}
