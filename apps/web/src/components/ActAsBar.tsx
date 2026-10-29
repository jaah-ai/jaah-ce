import { useLayoutEffect, useRef, useState, type JSX, type RefObject } from 'react'
import { useNavigate } from 'react-router-dom'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import Tooltip from '@mui/material/Tooltip'
import Typography from '@mui/material/Typography'
import { Banner } from '../ui/Banner'
import { actAsReturnTo, dismissActAsNotice, exitActAs, useActAsRefused, useActingAs } from '../lib/actAs'

/** How far the stuck bar reaches below its scrollport's top padding, in px: a sticky record tab strip
 *  (`components/Tabs.tsx` `sticky`) sits that much lower, so the bar never covers its top. Set on the
 *  root while the bar is drawn; a nested scrollport the bar does not stick in (`ListDetail`'s pane) resets it. */
export const ACT_AS_OVERLAP_VAR = '--act-as-bar-overlap'

/** `.app-main`'s right inset when it is not the 2rem: `SPLIT_INSET_PX` while a record is split beside the list (`ListDetail`). */
export const MAIN_END_INSET_VAR = '--app-main-end-inset'

/** What acting means, under the bar's line — in its tooltip when the bar is narrow. */
const ACT_AS_NOTE = 'Changes land in this org, audited under your name.'
const ACT_AS_CONTAINER = 'act-as-bar'
/** The bar too narrow for its line, note and Exit side by side: a phone, or a list column beside a record. */
const NARROW = `@container ${ACT_AS_CONTAINER} (max-width: 519px)`

/** Publishes `ACT_AS_OVERLAP_VAR` from the bar's height less `.app-main`'s top padding, kept current on resize. */
function useActAsOverlap(bar: RefObject<HTMLElement | null>, shown: boolean): void {
  useLayoutEffect(() => {
    const el = bar.current
    if (!shown || !el) return undefined
    const root = document.documentElement.style
    const measure = () => {
      const scrollport = el.closest<HTMLElement>('[data-testid="app-main"]') ?? el.parentElement
      const padTop = scrollport ? parseFloat(getComputedStyle(scrollport).paddingTop) || 0 : 0
      root.setProperty(ACT_AS_OVERLAP_VAR, `${Math.max(0, Math.ceil(el.getBoundingClientRect().height - padTop))}px`)
    }
    measure()
    const observer = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(measure)
    observer?.observe(el)
    return () => {
      observer?.disconnect()
      root.removeProperty(ACT_AS_OVERLAP_VAR)
    }
  }, [bar, shown])
}

/* The shell-wide strip while this tab acts as an org (`lib/actAs.ts`): mounted once in `App.tsx::AuthLayout`
   above every page, full-bleed and sticky so it never scrolls away. A filled warning strip, never a chip — the
   operator's org FILTER (`ui/OrgScopeControl.tsx`) only narrows reads; this one writes as someone else. Exit
   returns to the page act-as was entered from — the operator's own, so it opens — else the Orgs list: not the
   page it leaves, which may be the org's own and gone with the seat.
   After a refusal ended act-as, a dismissible notice in its place. */
export function ActAsBar(): JSX.Element | null {
  const org = useActingAs()
  const refused = useActAsRefused()
  const navigate = useNavigate()
  const barRef = useRef<HTMLDivElement | null>(null)
  const noteRef = useRef<HTMLDivElement | null>(null)
  const [tipOpen, setTipOpen] = useState(false)
  useActAsOverlap(barRef, Boolean(org))
  if (!org) {
    if (!refused) return null
    return (
      <Box sx={{ mb: 1.5 }}>
        <Banner severity="warning" testId="act-as-refused" onDismiss={dismissActAsNotice}>
          Stopped acting as {refused.name}: org unavailable.
        </Banner>
      </Box>
    )
  }
  return (
    <Box
      ref={barRef}
      data-testid="act-as-bar"
      role="status"
      sx={{
        position: 'sticky',
        // Flush with the scrollport's edge, not `<main>`'s padding: nothing scrolls by above it.
        top: '-1.75rem',
        zIndex: 3,
        // Full-bleed: cancels `<main>`'s padding (`App.tsx::APP_MAIN_SX`).
        ml: '-2rem',
        mr: `calc(-1 * var(${MAIN_END_INSET_VAR}, 2rem))`,
        mt: '-1.75rem',
        mb: 1.5,
        px: '2rem',
        py: 0.75,
        display: 'flex',
        alignItems: 'center',
        flexWrap: 'wrap',
        columnGap: 1.5,
        rowGap: 0.5,
        bgcolor: 'warning.main',
        color: 'common.black',
        boxShadow: '0 1px 4px rgba(0, 0, 0, 0.2)',
        containerType: 'inline-size',
        containerName: ACT_AS_CONTAINER,
        '@media (max-width:480px)': { mx: '-1rem', mt: '-0.75rem', px: '1rem', top: '-0.75rem' },
      }}
    >
      {/* ONE LINE WHEN NARROW: three lines cost every page its first rows — a phone, or the list column
          beside an open record. There the note folds into the line's tooltip and a long org name ellipsizes,
          whole in the same tooltip. Keyed on the bar's own width (a container query), not the viewport's. */}
      <Tooltip
        title={`${org.name}: ${ACT_AS_NOTE}`}
        open={tipOpen}
        onOpen={() => setTipOpen(noteRef.current !== null && getComputedStyle(noteRef.current).display === 'none')}
        onClose={() => setTipOpen(false)}
        enterTouchDelay={0}
        leaveTouchDelay={4000}
      >
        <Box data-testid="act-as-line" tabIndex={0} sx={{ minWidth: 0, flex: '1 1 12rem', [NARROW]: { flexBasis: 0 } }}>
          <Typography
            component="div"
            sx={{ fontSize: '0.875rem', overflowWrap: 'anywhere', [NARROW]: { whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' } }}
          >
            {/* "· Owner seat" drops when narrow: every act-as seat is the Owner seat, and the org name needs the room. */}
            Acting as <Box component="strong" sx={{ fontWeight: 700 }}>{org.name}</Box>
            <Box component="span" data-testid="act-as-seat" sx={{ [NARROW]: { display: 'none' } }}> · Owner seat</Box>
          </Typography>
          <Typography ref={noteRef} component="div" data-testid="act-as-note" sx={{ fontSize: '0.75rem', opacity: 0.85, [NARROW]: { display: 'none' } }}>
            {ACT_AS_NOTE}
          </Typography>
        </Box>
      </Tooltip>
      <Button
        data-testid="act-as-exit"
        size="small"
        variant="outlined"
        color="inherit"
        onClick={() => {
          const back = actAsReturnTo() ?? '/orgs'
          exitActAs()
          navigate(back)
        }}
        sx={{ flexShrink: 0, borderColor: 'currentColor', fontWeight: 600, '@media (max-width:480px)': { minHeight: 40 } }}
      >
        Exit act-as
      </Button>
    </Box>
  )
}

export default ActAsBar
