/* THE FLOATING WINDOWS' SHARED CHROME — what makes the Help panel (`SupportChat.tsx`) and the onboarding
 * widget (`onboarding/OnboardingWidget.tsx`) read as one family: the header's height and gradient, and its
 * icon buttons. Single-homed so the two headers cannot drift; the window itself is `FloatingWindow.tsx`. */
import type { Ref } from 'react'
import IconButton from '@mui/material/IconButton'
import Tip from './Tip'
import { Icon } from './icons'
import { PHONE } from './listStyles'
import { SIZE, tint } from '../theme'

/** A finger-sized target at phone width; desktop keeps MUI's small button. */
export const TAP_SX = { [PHONE]: { minHeight: SIZE.tapTarget, minWidth: SIZE.tapTarget } } as const

/** The header's height, which is also a minimized window's (plus its 1px borders). */
export const HEADER_HEIGHT = 56

/** The header's wash: primary into violet (`info`), as `tint()`s so each stop follows the colour scheme. */
export const HEADER_GRADIENT =
  `linear-gradient(135deg, ${tint('primary', 0.14)}, ${tint('primary', 0.1)} 55%, ${tint('info', 0.14)})`

/** The header bar both windows draw; each adds its own `gap`. */
export const WINDOW_HEADER_SX = {
  flex: '0 0 auto',
  height: HEADER_HEIGHT,
  display: 'flex',
  alignItems: 'center',
  pl: 1.5,
  pr: 0.5,
  borderBottom: 1,
  borderColor: 'divider',
  backgroundImage: HEADER_GRADIENT,
} as const

/* Material `Close`, `Remove`, `ExpandLess` (`@mui/icons-material` is not a dependency). */
export { CLOSE_PATH, EXPAND_LESS_PATH as RESTORE_PATH } from './icons'
export const MINIMIZE_PATH = 'M19 13H5v-2h14z'

export function HeaderButton({ label, tip, path, testId, disabled, onClick, buttonRef }: {
  label: string
  buttonRef?: Ref<HTMLButtonElement>
  /** The tooltip, only when it says more than the standard glyph (a reason, what undoes it). */
  tip?: string
  path: string
  testId: string
  disabled?: boolean
  onClick: () => void
}) {
  return (
    <Tip describeChild={false} title={tip}>
      {/* The span keeps the tooltip working on a disabled button. */}
      <span>
        <IconButton ref={buttonRef} aria-label={label} data-testid={testId} disabled={disabled} onClick={onClick} size="small" sx={TAP_SX}>
          <Icon viewBox="0 0 24 24" size={SIZE.iconMd} sx={{ fill: 'currentColor' }}>
            <path d={path} />
          </Icon>
        </IconButton>
      </span>
    </Tip>
  )
}
