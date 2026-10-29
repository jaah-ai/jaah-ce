import { useId, useState, type MouseEvent } from 'react'
import Box from '@mui/material/Box'
import Button, { type ButtonProps } from '@mui/material/Button'
import IconButton from '@mui/material/IconButton'
import Menu from '@mui/material/Menu'
import MenuItem from '@mui/material/MenuItem'
import { Link as RouterLink } from 'react-router-dom'
import type { SxProps, Theme } from '@mui/material/styles'
import { CompactVerbLabel, compactVerbProps } from './CompactVerb'
import { MoreHorizIcon } from './icons'
import { PHONE } from './listStyles'

// The disabled verb's fade — MUI's own disabled opacity, applied to the label and glyph, not the item.
const DIM_SX = { opacity: (t: Theme) => t.palette.action.disabledOpacity }

// One shape for a menu item. A union: an item either navigates or calls back, never neither.
export type OverflowItem = {
  key: string
  /** A glyph before the label; omitted, the label stands alone. */
  icon?: string
  label: string
  ariaLabel: string
  testId?: string
  danger?: boolean
  /* A verb the row offers but cannot perform (e.g. deleting the last superadmin): rendered disabled with
     its reason announced, never a pressable no-op. */
  disabled?: boolean
  /** Why it is disabled, in words: a visible line under the label, and appended to the accessible name. Required
   *  when `disabled`. */
  disabledReason?: string
} & (
  // `href`: an external URL as a real anchor in a new tab (middle-click, copy link keep working).
  // `to`: an in-app route via `RouterLink`, same tab. `download` for a file whose server sets no
  // `Content-Disposition`.
  | { href: string; download?: boolean; to?: never; onClick?: never }
  | { href?: never; to: string; onClick?: never }
  | { href?: never; to?: never; onClick: () => void }
)

/** A trigger and its Menu of `items`. The trigger is the ⋯ icon, or a text button when `button` is given.
 *  `busy` disables the trigger and every item. An empty list renders nothing. */
export function OverflowMenu({
  items,
  busy = false,
  ariaLabel,
  testId,
  size,
  sx,
  button,
}: {
  items: OverflowItem[]
  busy?: boolean
  /** The ⋯ trigger's accessible name (a text button is named by its label). */
  ariaLabel?: string
  testId?: string
  size?: 'small' | 'medium'
  sx?: SxProps<Theme>
  /** `compact`: a tab strip's verb, a wordless ＋ on a phone (`ui/CompactVerb`). */
  button?: { label: string; variant?: ButtonProps['variant']; compact?: boolean }
}): JSX.Element | null {
  const [anchor, setAnchor] = useState<HTMLElement | null>(null)
  const menuId = useId()
  if (items.length === 0) return null
  const trigger = {
    'data-testid': testId,
    'aria-haspopup': 'menu' as const,
    'aria-expanded': Boolean(anchor),
    'aria-controls': anchor ? menuId : undefined,
    disabled: busy,
    size,
    onClick: (e: MouseEvent<HTMLElement>) => setAnchor(e.currentTarget),
    sx,
  }
  return (
    <>
      {button?.compact ? (
        <Button variant={button.variant} {...trigger} {...compactVerbProps(button.label, sx)}>
          <CompactVerbLabel label={button.label} />
        </Button>
      ) : button ? (
        <Button variant={button.variant} {...trigger}>{button.label}</Button>
      ) : (
        <IconButton aria-label={ariaLabel} {...trigger}>
          <MoreHorizIcon />
        </IconButton>
      )}
      <Menu id={menuId} anchorEl={anchor} open={Boolean(anchor)} onClose={() => setAnchor(null)}>
        {items.map((item) => {
          // Shared by every arm. `key` stays out: React warns on a key through a spread.
          const common = {
            'data-testid': item.testId,
            // The reason rides the accessible name.
            'aria-label': item.disabled && item.disabledReason
              ? `${item.ariaLabel} — ${item.disabledReason}`
              : item.ariaLabel,
            title: item.disabled ? item.disabledReason : undefined,
            disabled: busy || Boolean(item.disabled),
            onClick: () => {
              setAnchor(null)
              item.onClick?.()
            },
            /* 44px target at phone width. A disabled item dims its VERB only (`DIM_SX`): MUI's own opacity
               fades the whole item, reason included, and the reason is the line that must stay readable. */
            sx: [{ [PHONE]: { minHeight: 44 }, '&.Mui-disabled': { opacity: 1 } }, item.danger ? { color: 'error.main' } : {}],
          }
          const dim = busy || Boolean(item.disabled)
          const body = (
            <>
              {item.icon && (
                <Box component="span" aria-hidden="true" sx={[{ width: 18, textAlign: 'center', mr: 1 }, dim && DIM_SX]}>
                  {item.icon}
                </Box>
              )}
              {/* A disabled item prints its reason under the label: MUI's disabled item takes no pointer
                  events, so a `title` tooltip never shows. */}
              {item.disabled && item.disabledReason ? (
                <Box component="span" sx={{ display: 'flex', flexDirection: 'column', minWidth: 0 }}>
                  <Box component="span" sx={DIM_SX}>{item.label}</Box>
                  <Box
                    component="span"
                    data-testid="overflow-item-reason"
                    sx={{ typography: 'caption', color: 'text.secondary', whiteSpace: 'normal', maxWidth: 240 }}
                  >
                    {item.disabledReason}
                  </Box>
                </Box>
              ) : <Box component="span" sx={dim ? DIM_SX : undefined}>{item.label}</Box>}
            </>
          )
          // Separate elements, not a conditional `component` prop: each arm's props only type-check on
          // its own branch. A disabled item takes the plain arm — a disabled anchor is still followable.
          return item.href && !item.disabled ? (
            <MenuItem
              key={item.key}
              component="a"
              href={item.href}
              download={item.download ? '' : undefined}
              target="_blank"
              rel="noreferrer"
              {...common}
            >
              {body}
            </MenuItem>
          ) : item.to && !item.disabled ? (
            <MenuItem key={item.key} component={RouterLink} to={item.to} {...common}>
              {body}
            </MenuItem>
          ) : (
            <MenuItem key={item.key} {...common}>
              {body}
            </MenuItem>
          )
        })}
      </Menu>
    </>
  )
}
