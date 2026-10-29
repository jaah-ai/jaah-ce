import Box from '@mui/material/Box'
import Drawer from '@mui/material/Drawer'
import IconButton from '@mui/material/IconButton'
import Typography from '@mui/material/Typography'
import { alpha, type Theme } from '@mui/material/styles'
import { useMemo, useState, type JSX, type ReactNode } from 'react'
import { FORM_BAR_STUCK_ATTR, useStuck } from './FormActionBar'
import { CloseIcon } from './icons'
import { paperGround } from '../theme'

/* The console's ONE right-anchored detail drawer: Escape/backdrop close via MUI's Modal, full-width at
 * `xs`. `testId` is the structural handle suites address the paper by; `width` is the `sm` width, or a
 * full responsive object. `title` renders the ONE drawer header — an h3 title, an optional `subtitle` line
 * under it (chips, a timestamp), and a ✕ close button named `closeLabel` (`<testId>-close`). `footer` is pinned
 * to the drawer's foot — at the bottom of a short drawer, stuck to the bottom edge while a long one scrolls, where
 * a top shadow says content continues beneath it (`useStuck`). */
export default function SideDrawer({
  testId,
  label,
  width = 520,
  pad = 2,
  title,
  subtitle,
  closeLabel = 'Close',
  onClose,
  footer,
  children,
}: {
  testId: string
  label: string
  width?: number | Record<string, number | string>
  pad?: number
  title?: ReactNode
  subtitle?: ReactNode
  closeLabel?: string
  onClose: () => void
  footer?: ReactNode
  children: ReactNode
}): JSX.Element {
  /* State, not a plain ref: the Drawer's portal mounts its children a render after ours, so a ref is still
     empty when `useStuck`'s effect first runs and its deps never change to run it again. */
  const [footerEl, setFooterEl] = useState<HTMLDivElement | null>(null)
  useStuck(useMemo(() => ({ current: footerEl }), [footerEl]), footerEl !== null)
  return (
    <Drawer
      anchor="right"
      open
      onClose={onClose}
      slotProps={{
        // `as object`: the Paper slot type has no index signature for `data-*`, nor admits `role`.
        paper: {
          'data-testid': testId,
          role: 'dialog',
          'aria-label': label,
          /* No bottom padding under a footer: the footer is the paper's foot, so `useStuck` finds it at the
             content box's edge. */
          sx: {
            width: typeof width === 'number' ? { xs: '100%', sm: width } : width,
            maxWidth: '100%',
            p: pad,
            ...(footer ? { pb: 0 } : {}),
          },
        } as object,
      }}
    >
      {title !== undefined && (
        <Box sx={{ display: 'flex', alignItems: 'flex-start', gap: 1, mb: 1 }}>
          <Box sx={{ flex: '1 1 auto', minWidth: 0 }}>
            <Typography variant="subtitle1" component="h3" sx={{ fontWeight: 600, overflowWrap: 'anywhere' }}>
              {title}
            </Typography>
            {subtitle && <Box sx={{ mt: 0.5 }}>{subtitle}</Box>}
          </Box>
          <IconButton onClick={onClose} aria-label={closeLabel} title={closeLabel} color="inherit" data-testid={`${testId}-close`}>
            <CloseIcon />
          </IconButton>
        </Box>
      )}
      {/* One box for the body and its foot: the foot's parent is what `useStuck` watches for size, so a body
          that grows after open (an async select, an expanded editor) re-measures the shadow. A flex column
          like the paper, so the body lays out as before and `mt: auto` still drops the foot to the bottom. */}
      <Box sx={{ display: 'flex', flexDirection: 'column', flex: '1 0 auto' }}>
        {children}
        {/* The body's last line keeps a gap above the foot at the scroll's end: the paper has no bottom padding. */}
        {footer && <Box aria-hidden="true" sx={{ flex: '0 0 auto', height: theme => theme.spacing(1.5) }} />}
        {footer && (
          /* `mt: auto` takes a short drawer's slack; sticky holds it in view on a long one. The side margins reach
             the paper's edges so content scrolling under it stays hidden. */
          <Box
            ref={setFooterEl}
            data-testid={`${testId}-footer`}
            sx={(theme: Theme) => ({
              mt: 'auto',
              mx: -pad,
              px: pad,
              py: 1.5,
              position: 'sticky',
              bottom: 0,
              /* Above the body's positioned descendants (an outlined field's floating label is `z-index: 1`),
                 or they paint over the foot as it scrolls past them. */
              zIndex: 2,
              // The drawer paper's own ground (`theme.ts`'s `paperGround`): plain paper reads darker in dark.
              bgcolor: paperGround(theme),
              borderTop: '1px solid',
              borderColor: 'divider',
              /* Content continues beneath: the same edge as a stuck `FormActionBar`, deeper on dark paper, where
                 that one barely shows. */
              [`&[${FORM_BAR_STUCK_ATTR}]`]: {
                boxShadow: `0 -6px 8px -6px ${alpha(theme.palette.common.black, 0.35)}`,
                ...theme.applyStyles('dark', { boxShadow: `0 -8px 12px -6px ${alpha(theme.palette.common.black, 0.8)}` }),
              },
            })}
          >
            {footer}
          </Box>
        )}
      </Box>
    </Drawer>
  )
}
