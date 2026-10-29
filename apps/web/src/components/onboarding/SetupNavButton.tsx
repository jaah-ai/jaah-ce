import type { JSX } from 'react'
import Box from '@mui/material/Box'
import ButtonBase from '@mui/material/ButtonBase'
import Typography from '@mui/material/Typography'
import type { Theme } from '@mui/material/styles'
import { DoneBadge } from '../../ui/DoneBadge'
import { PHONE, schemed } from '../../ui/listStyles'
import { ProgressRing, progressPct } from '../../ui/ProgressRing'
import { checklistProgress, progressReading, progressSpoken } from '../setupChecklist/SetupChecklist'
import { isGuideFinished, openOnboarding, useOnboardingGuide, usePhoneGuideFolded } from './OnboardingWidget'

/* THE SIDEBAR'S SETUP BUTTON — onboarding's door that never closes until setup is done. Shown to
 * anyone the widget has a guide for (`OnboardingWidget.tsx::useOnboardingGuide`: the org's "Set up your
 * company" or the user's "Get started"), the ✕-hidden one included, with the guide's progress as a ring and
 * its one reading ("1/5", `progressReading`). A click shows the guide again (clears the stored hide) and opens the widget, on the page the user
 * is on — a setup page of its own lost the user's place. Once every step is done it reads ✓ Done for the rest of the
 * load (the widget celebrates), and the next load draws nothing.
 * Three shapes, as the rail's: `rail` (expanded: ring, "Setup", "1/5"), `collapsed` (ring over "1/5") and `sheet` (the phone's More sheet row).
 * The desktop shapes stand down while the guide is shown: the docked widget (open or on its rail) already
 * carries the same progress beside them, and the button is the way back to a ✕-hidden guide. */

export type SetupNavShape = 'rail' | 'collapsed' | 'sheet'

export default function SetupNavButton({ shape }: { shape: SetupNavShape }): JSX.Element | null {
  const guide = useOnboardingGuide()
  if (guide === null || (shape !== 'sheet' && !guide.hidden)) return null
  const finished = isGuideFinished(guide)
  const progress = checklistProgress(guide.steps)
  const pct = progressPct(progress)
  const reading = progressReading(progress)

  const open = (): void => {
    if (guide.hidden) void guide.onShow()
    openOnboarding()
  }

  const glyph = finished ? (
    <DoneBadge size={22} testId="sidebar-setup-done" />
  ) : (
    <ProgressRing pct={pct} variant="gradient" size={22} testId="sidebar-setup-ring" />
  )

  const collapsed = shape === 'collapsed'
  return (
    <ButtonBase
      onClick={open}
      data-testid="sidebar-setup"
      data-shape={shape}
      aria-label={`Setup, ${progressSpoken(progress)}`}
      title={guide.title}
      sx={(t: Theme) => ({
        flex: '0 0 auto',
        display: 'flex',
        flexDirection: collapsed ? 'column' : 'row',
        alignItems: 'center',
        justifyContent: 'flex-start',
        gap: collapsed ? '2px' : '0.5rem',
        margin: shape === 'sheet' ? '0 0 0.375rem' : collapsed ? '0.25rem 0.25rem 0.375rem' : '0.25rem 0.5rem 0.5rem',
        padding: collapsed ? '0.375rem 0' : '0.4375rem 0.625rem',
        minHeight: shape === 'sheet' ? 44 : undefined,
        borderRadius: '8px',
        border: '1px solid',
        textAlign: 'left',
        fontFamily: 'inherit',
        ...schemed(t, c => ({ borderColor: c.accent, backgroundColor: c.bannerBg, color: c.textStrong })),
        '&:hover': schemed(t, c => ({ backgroundColor: c.bannerHoverBg })),
        '&.Mui-focusVisible': {
          ...schemed(t, c => ({ outline: `2px solid ${c.accent}` })),
          outlineOffset: '2px',
        },
        [PHONE]: shape === 'sheet' ? {} : { display: 'none' },
      })}
    >
      {glyph}
      {collapsed ? (
        <Typography component="span" data-testid="sidebar-setup-pct" sx={{ fontSize: '0.625rem', fontWeight: 700, lineHeight: 1 }}>
          {reading}
        </Typography>
      ) : (
        <>
          <Typography component="span" sx={{ flex: 1, minWidth: 0, fontSize: '0.875rem', fontWeight: 600 }}>
            Setup
          </Typography>
          <Typography component="span" data-testid="sidebar-setup-pct" sx={{ fontSize: '0.8125rem', fontWeight: 700 }}>
            {reading}
          </Typography>
        </>
      )}
    </ButtonBase>
  )
}

/** The folded phone guide's progress for the More slot, or null while the bar is drawn (or there is no guide):
 *  "1/5", or "done" once every step is. The badge draws it; the More button's label says it. */
export function useFoldedGuideProgress(): string | null {
  const guide = useOnboardingGuide()
  const folded = usePhoneGuideFolded()
  if (!folded || guide === null || guide.hidden) return null
  if (isGuideFinished(guide)) return 'done'
  return progressReading(checklistProgress(guide.steps))
}

/** The phone's More slot's mark while the onboarding bar is folded into the navigation
 *  (`OnboardingWidget.tsx::usePhoneGuideFolded`): the guide's count as a badge at the slot's corner ("1/5", a ✓
 *  once done), so the folded guide still shows it is there and how far along; the More sheet's Setup row brings
 *  the bar back. Sight-only: the More button's label says it (`useFoldedGuideProgress`). */
export function SetupNavBadge(): JSX.Element | null {
  const progress = useFoldedGuideProgress()
  if (progress === null) return null
  return (
    <Box
      component="span"
      data-testid="nav-more-setup-badge"
      aria-hidden="true"
      sx={{ position: 'absolute', top: 1, right: 2, display: 'flex', lineHeight: 0 }}
    >
      {progress === 'done' ? (
        <DoneBadge size={16} />
      ) : (
        <Box
          component="span"
          sx={{
            px: '5px',
            borderRadius: '8px',
            fontSize: '0.625rem',
            fontWeight: 700,
            lineHeight: '16px',
            bgcolor: 'primary.main',
            color: 'primary.contrastText',
            whiteSpace: 'nowrap',
          }}
        >
          {progress}
        </Box>
      )}
    </Box>
  )
}
