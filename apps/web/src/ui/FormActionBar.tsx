import { useEffect, useRef, type JSX, type ReactNode, type RefObject } from 'react'
import Box from '@mui/material/Box'
import Stack from '@mui/material/Stack'
import { alpha, type Theme } from '@mui/material/styles'
import { paperGround } from '../theme'

/* The console's form ACTION BAR — the sticky bottom row for Discard/Save/Go, shared so the two measured
 * notes below are single-homed. Consumers: `components/tasks/TaskEditForm.tsx`, `pages/ProjectEditPage.tsx`,
 * `pages/UserEditPage.tsx`.
 */

/* A testid built into a selector must be word characters and dashes; anything else is DROPPED: it becomes
 * an emotion object KEY, so a `}` would break out of the rule. Not `CSS.escape` (jsdom lacks it); a
 * whitelist fails closed. */
const STRETCH_TEST_ID = /^[\w-]+$/

/** Marks the bar for chrome sharing its bottom edge: the floating Help launcher
 *  (`components/support/HelpLauncher.tsx`) sits level with it, and the phone onboarding bar
 *  (`components/onboarding/OnboardingWidget.tsx`) stands down for it. Any other sticky bottom bar wears it
 *  too (the task board's bulk dock, `pages/Tasks.tsx`). */
export const FORM_ACTION_BAR_ATTR = 'data-form-action-bar'

/** The CSS variable a host sets to put the bar on its own ground (default `background.paper`). */
export const FORM_BAR_GROUND = '--form-bar-ground'

/** On the bar while it is STUCK: held at its scrollport's foot with content scrolled beneath it, not sitting in the
 *  flow. Only then does it draw its edge and the panel strip below it. */
export const FORM_BAR_STUCK_ATTR = 'data-stuck'

/** How far the stuck bar's ground reaches past each side of its box: past an overhanging Switch (~12px). */
const SIDE_COVER = '1rem'

/** The stuck bar's ground: the host's `--form-bar-ground`, else its paper's (`theme.ts`'s `paperGround`). */
const groundOf = (theme: Theme): string => `var(${FORM_BAR_GROUND}, ${paperGround(theme)})`

/** The nearest ancestor that scrolls vertically: what a sticky bar sticks to. */
export function scrollportOf(el: HTMLElement): HTMLElement | null {
  for (let node = el.parentElement; node; node = node.parentElement) {
    if (/auto|scroll/.test(getComputedStyle(node).overflowY)) return node
  }
  return null
}

/** Keeps `FORM_BAR_STUCK_ATTR` on `bar` true: stuck is the bar at its scrollport's sticky edge (the content box's
 *  foot, or its head for `edge: 'top'` — a sticky tab strip, `components/Tabs.tsx`) while more content lies beyond it, since a bar ending the content sits at that edge in the flow too once
 *  scrolled to the end. A page that pins the bar `fixed` (`TaskDetailPage` on a phone) always floats it over
 *  content. The scrollport is looked up per measure: a page may move it (the same page's card scrolls only above
 *  the phone). Re-measured on any scroll, a window resize, and a resize of the scrollport or the bar's form. */
export function useStuck(bar: RefObject<HTMLElement | null>, active: boolean, edge: 'top' | 'bottom' = 'bottom'): void {
  useEffect(() => {
    const el = bar.current
    if (!el || !active) return undefined
    let frame = 0
    let observed: HTMLElement | null = null
    const resizes = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(() => schedule())
    if (el.parentElement) resizes?.observe(el.parentElement)
    const measure = () => {
      frame = 0
      const port = scrollportOf(el)
      if (port !== observed) {
        if (observed && observed !== el.parentElement) resizes?.unobserve(observed)
        if (port) resizes?.observe(port)
        observed = port
      }
      if (getComputedStyle(el).position === 'fixed') {
        el.toggleAttribute(FORM_BAR_STUCK_ATTR, true)
        return
      }
      if (!port) {
        el.toggleAttribute(FORM_BAR_STUCK_ATTR, false)
        return
      }
      const portTop = port.getBoundingClientRect().top + port.clientTop
      if (edge === 'top') {
        const head = portTop + parseFloat(getComputedStyle(port).paddingTop || '0')
        el.toggleAttribute(FORM_BAR_STUCK_ATTR, port.scrollTop > 1 && Math.abs(el.getBoundingClientRect().top - head) < 1)
        return
      }
      const foot = portTop + port.clientHeight - parseFloat(getComputedStyle(port).paddingBottom || '0')
      const below = port.scrollHeight - port.scrollTop - port.clientHeight
      el.toggleAttribute(FORM_BAR_STUCK_ATTR, below > 1 && Math.abs(el.getBoundingClientRect().bottom - foot) < 1)
    }
    const schedule = () => { if (frame === 0) frame = requestAnimationFrame(measure) }
    measure()
    // Scroll does not bubble: captured at the document, it reaches us from whichever box scrolls.
    document.addEventListener('scroll', schedule, { capture: true, passive: true })
    window.addEventListener('resize', schedule)
    return () => {
      cancelAnimationFrame(frame)
      document.removeEventListener('scroll', schedule, { capture: true })
      window.removeEventListener('resize', schedule)
      resizes?.disconnect()
    }
  }, [bar, active, edge])
}

/** `& [data-testid="a"], & [data-testid="b"]` — empty (no rule to emit) when nothing survives the filter. */
function stretchSelectorFor(testIds: readonly string[]): string {
  return testIds
    .filter(id => STRETCH_TEST_ID.test(id))
    .map(id => `& [data-testid="${id}"]`)
    .join(', ')
}

export function FormActionBar({
  children,
  stretchTestIds,
  testId = 'form-actions',
  plain = false,
  note,
}: {
  children: ReactNode
  /** A line about the actions, on its own row above them and left-aligned, inside the bar: so it sticks with them
   *  rather than scroll under a stuck bar (a phone's task form). */
  note?: ReactNode
  /** A plain row under the fields — not sticky, no rule or panel (the wizards, whose steps are short). */
  plain?: boolean
  /** Testids of actions that take the row's spare width at <=480px. IDS, not a selector, so callers do not
   *  spell MUI's `&` nesting from outside. */
  stretchTestIds?: readonly string[]
  testId?: string
}): JSX.Element {
  const stretchSelector = stretchSelectorFor(stretchTestIds ?? [])
  const ref = useRef<HTMLDivElement | null>(null)
  useStuck(ref, !plain)
  return (
    <Stack
      ref={ref}
      direction="row"
      data-testid={testId}
      {...{ [FORM_ACTION_BAR_ATTR]: '' }}
      sx={{
        gap: '0.5rem',
        justifyContent: 'flex-end',
        // The note takes the first line whole; the actions wrap under it.
        ...(note ? { flexWrap: 'wrap', '& > [data-form-bar-note]': { flex: '1 0 100%' } } : {}),
        ...(plain ? {} : {
          position: 'sticky',
          bottom: 0,
          zIndex: 3,
          pt: '0.5rem',
          borderTop: '1px solid',
          borderColor: 'divider',
          /* The bar's ground, one value for its fill, its side covers and the strip below: a host on another ground
             (the page's, `pages/ProfilePage.tsx`) sets `--form-bar-ground` and all follow. Only while stuck: in the
             flow nothing lies under the bar, and a paper fill there drew a panel on a host's own ground. */
          [`&[${FORM_BAR_STUCK_ATTR}]`]: {
            bgcolor: (theme: Theme) => groundOf(theme),
            /* A sticky row stops at `.app-main`'s padding edge (App.tsx's shell contract: 1rem above the phone's
               bottom nav, 1.75rem above the viewport's foot), and content scrolled through that padding showed
               under it. The last, solid shadow paints the bar's own ground down over that strip; the first is the
               edge that parts it from a row it covers. Both only while stuck: in the flow the edge
               parted it from nothing and the strip was empty panel under the buttons. The two side
               shadows widen the ground past the form column: an MUI Switch's root (its `FormControlLabel`'s negative
               margin) overhangs it by ~12px and showed beside the bar. Shadows, not margins or padding: they take
               no layout and add no scroll overflow. `vars` first: the static palette is light. */
            boxShadow: (theme: Theme) =>
              `0 -6px 8px -6px ${alpha(theme.palette.common.black, 0.35)}, `
              + `-${SIDE_COVER} 0 0 0 ${groundOf(theme)}, ${SIDE_COVER} 0 0 0 ${groundOf(theme)}, `
              + `0 1.75rem 0 ${SIDE_COVER} ${groundOf(theme)}`,
          },
        }),
        // STICKY AT EVERY WIDTH (the form scrolls at ≤480px too; `bgcolor` hides prose underneath).
        // What this row CANNOT do: sticky needs a scrollport, and on a phone with the keyboard DOWN there is
        // none (Go measured 827->871 in an 844px viewport). Only a page sees `[data-keyboard]`, so a consumer
        // that must stay reachable owes its own `fixed` rule — `TaskDetailPage`'s PAGE_SX
        // `:not([data-keyboard="up"])` block is the worked example.
        '@media (max-width:480px)': {
          /* Give the two real actions (Save/Go) the spare width so the escape hatch is not the most
             legible control; `space-between` keeps Discard its own size. */
          justifyContent: 'space-between',
          ...(stretchSelector === '' ? {} : { [stretchSelector]: { flex: 1 } }),
        },
      }}
    >
      {note && <Box data-form-bar-note="">{note}</Box>}
      {children}
    </Stack>
  )
}

export default FormActionBar
