import { useEffect, useRef, type JSX } from 'react'
import { keyframes, type SxProps, type Theme } from '@mui/material/styles'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import IconButton from '@mui/material/IconButton'
import SvgIcon from '@mui/material/SvgIcon'
import { CHECK_PATH, CLOSE_PATH } from './icons'
import { tapTargetSx } from './listStyles'
import { useCopyFlash } from './useCopyFlash'
import { VISUALLY_HIDDEN } from './visuallyHidden'

/* ── A CLIPBOARD BUTTON THAT SAYS WHETHER IT WORKED ── the mechanism is `ui/useCopyFlash`'s.
 * THE LABEL FLIP IS NOT DECORATION: a refused clipboard is SAID in the label, never thrown. */

const FLASH_MS = 900

/* The visible text and the ACCESSIBLE NAME must be the same words: `aria-label` overrides content, so a
 * static one would hide the flip from screen readers. The live region below announces it. */
const STATE_LABELS = { copied: 'Copied ✓', failed: 'Copy unavailable' } as const

/* The `icon` faces, as 24-unit SVG paths (no icon package here — `ui/InfoTip.tsx`): the familiar
 * two-sheets copy mark; the check or cross replaces it for the flash. */
const COPY_PATH =
  'M16 1H4a2 2 0 0 0-2 2v14h2V3h12V1zm3 4H8a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h11a2 2 0 0 0 2-2V7a2 2 0 0 0-2-2zm0 16H8V7h11v14z'

/** WCAG 2.2 AA 2.5.8's 24x24 minimum (as `ui/feed/geometry.ts`'s `DISCLOSURE_TOUCH_MIN`), on both axes. */
const ICON_TARGET_MIN = 24

/* The standard "copied" beat: the check pops in (scale overshoot) where the copy mark was. */
const pop = keyframes`
  0% { transform: scale(0.4); opacity: 0; }
  60% { transform: scale(1.2); opacity: 1; }
  100% { transform: scale(1); opacity: 1; }
`

export function CopyButton({
  text,
  label,
  testId,
  title,
  ariaLabel,
  sx,
  variant = 'outlined',
  copiedLabel = STATE_LABELS.copied,
  failedLabel = STATE_LABELS.failed,
  flashMs = FLASH_MS,
  onFailed,
}: {
  /** Built ON CLICK (see the header). */
  text: () => string
  label: string
  testId?: string
  title?: string
  ariaLabel?: string
  sx?: SxProps<Theme>
  /** `text`: a bare verb beside the value it copies (`ui/CopyableValue`) — no glyph, no outline.
   *  `icon`: a 24px glyph that flips copy mark → green check / red cross; `label` is its accessible name AND
   *  title, flipping with the face (`lib/credentialRow`'s inline copy-name control). */
  variant?: 'outlined' | 'text' | 'icon'
  copiedLabel?: string
  /** A caller whose value is also selectable on screen keeps its verb (`label`); the refusal is still announced. */
  failedLabel?: string
  flashMs?: number
  /** Told once per refused copy: a caller that shows no selectable value can reveal one (`ui/AuthorizeLink`). */
  onFailed?: () => void
}): JSX.Element {
  const [state, copy] = useCopyFlash(flashMs)
  const onFailedRef = useRef(onFailed)
  onFailedRef.current = onFailed
  useEffect(() => {
    if (state === 'failed') onFailedRef.current?.()
  }, [state])

  const shown = state === 'idle' ? label : state === 'copied' ? copiedLabel : failedLabel
  // State-derived, never pinned to `label` — see `STATE_LABELS`.
  const said = state === 'idle' ? (ariaLabel ?? label) : shown
  const common = { 'data-testid': testId, 'data-copy-state': state, 'aria-label': said, onClick: () => copy(text) }
  const outlined = variant === 'outlined'
  return (
    <>
      {variant === 'icon' ? (
        <IconButton
          {...common}
          type="button"
          title={title ?? said}
          sx={[
            (theme) => ({
              width: ICON_TARGET_MIN,
              height: ICON_TARGET_MIN,
              /* The theme's 44px phone floor (`theme.ts` MuiIconButton) is for TOUCH; a `titleAction` is never
                 drawn there, and a narrow MOUSE window must not grow a one-line row to 44px. */
              [theme.breakpoints.down('sm')]: { minWidth: ICON_TARGET_MIN, minHeight: ICON_TARGET_MIN },
              p: 0,
              /* Quiet beside the text at rest; the state colours carry the answer. */
              color: state === 'copied' ? 'success.main' : state === 'failed' ? 'error.main' : 'text.secondary',
              '&:hover': { color: state === 'idle' ? 'text.primary' : undefined },
            }),
            ...(Array.isArray(sx) ? sx : [sx]),
          ]}
        >
          <SvgIcon
            /* Keyed on state so the pop replays on every flash. */
            key={state}
            viewBox="0 0 24 24"
            sx={{
              fontSize: 14,
              animation: state === 'idle' ? undefined : `${pop} 260ms ease-out`,
              '@media (prefers-reduced-motion: reduce)': { animation: 'none' },
            }}
          >
            <path d={state === 'copied' ? CHECK_PATH : state === 'failed' ? CLOSE_PATH : COPY_PATH} />
          </SvgIcon>
        </IconButton>
      ) : (
        <Button
          size={outlined ? 'small' : 'medium'}
          variant={variant}
          color={outlined ? 'inherit' : 'primary'}
          {...common}
          title={title}
          sx={[{ textTransform: 'none', whiteSpace: 'nowrap' }, tapTargetSx, ...(Array.isArray(sx) ? sx : [sx])]}
        >
          {outlined && (
            <Box component="span" aria-hidden="true" sx={{ mr: 0.5 }}>
              ⧉
            </Box>
          )}
          {shown}
        </Button>
      )}
      {/* The outcome, ANNOUNCED (a renamed button is not an announcement). Visually hidden, not
          `display: none`, which is not read at all. A refusal is said even where the label keeps its verb. */}
      <Box component="span" role="status" aria-live="polite" sx={VISUALLY_HIDDEN}>
        {state === 'idle' ? '' : state === 'failed' && failedLabel === label ? STATE_LABELS.failed : shown}
      </Box>
    </>
  )
}
