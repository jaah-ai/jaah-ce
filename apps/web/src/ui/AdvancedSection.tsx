import { type JSX, type ReactNode, type SyntheticEvent, useLayoutEffect, useRef, useState } from 'react'
import Box from '@mui/material/Box'
import { FONT_SIZE, FONT_WEIGHT, LINE_HEIGHT, SIZE } from '../theme'

/* The console's ONE disclosure: where a form keeps every field a first pass does not need
 * (PRD R3 — the default view shows only the minimal journey; nothing is removed, it is only folded), and —
 * `dense` — where a long or secondary thing (a captured body, a build log, a list of ids) sits behind one line.
 * Labelled "Advanced" unless a caller names its fold (`label`), collapsed by default, and never a place an error can hide:
 *   • `forceOpen` (a caller that knows an inner field is invalid) opens it without
 *     recording a choice, so it closes again when the force ends;
 *   • an inner field that is INVALID opens it by itself — MUI marks an errored input `aria-invalid`,
 *     and a native constraint failure fires `invalid` (captured here) — and that open is sticky, since
 *     the operator has to see the field to fix it.
 * Form consumers: `components/tasks/TaskSpecFields.tsx`, `pages/credentials/CredentialEditPage.tsx`,
 * `pages/credentials/AccountEditPage.tsx`, the node page's Troubleshooting (`pages/NodeDetailPage.tsx`) and
 * `pages/customers/SubscriptionPanel.tsx`, `components/projects/EasyModeFields.tsx`, each section of
 * `components/projects/customize/CustomizeTab.tsx`; `dense` ones: `pages/audit/AuditEventPage.tsx`,
 * `components/microvm/NodeMicrovmTab.tsx`, `components/agentEvents.tsx`, `pages/production-qa/DeploymentDetailPage.tsx`,
 * `pages/settings/nodelinkLocations/NodeLinkLocationsSection.tsx`, `components/ChangeOrgDialog.tsx`. */

/** A `<details>` whose open state has three inputs: a default (closed), a force-open, and the operator's
 *  click, which wins from the moment it happens. The DOM fires `toggle` for a React-driven `open` change
 *  too, so compare against the last rendered value to tell the operator's click apart. */
function useDisclosure(
  forceOpen: boolean, defaultOpen: boolean, onOpenChange?: (open: boolean, details: HTMLDetailsElement) => void,
) {
  // `defaultOpen` seeds the operator's choice once (a link that names this fold); a click still closes it.
  const [choice, setChoice] = useState<boolean | undefined>(defaultOpen ? true : undefined)
  const open = forceOpen || (choice ?? false)
  const renderedRef = useRef(open)
  renderedRef.current = open
  return {
    open,
    reveal: () => setChoice(true),
    onToggle: (e: SyntheticEvent<HTMLDetailsElement>) => {
      if (e.currentTarget.open !== renderedRef.current) setChoice(e.currentTarget.open)
      onOpenChange?.(e.currentTarget.open, e.currentTarget)
    },
  }
}

export function AdvancedSection({
  children,
  testId = 'form-advanced',
  forceOpen = false,
  defaultOpen = false,
  label = 'Advanced',
  dense = false,
  onOpenChange,
}: {
  children: ReactNode
  /** The `<details>`'s testid; the summary's is `<testId>-summary`. */
  testId?: string
  /** Open while true (a caller-known error inside). Not recorded as the operator's choice. */
  forceOpen?: boolean
  /** Open on mount (a link naming this fold); read once, and the operator may close it. */
  defaultOpen?: boolean
  /** The summary's word (or line). */
  label?: ReactNode
  /** A quieter fold inside content rather than a form's: the summary in secondary metadata size and colour,
   *  the margins a line's, no phone tap floor (it sits in a table cell or a list item). */
  dense?: boolean
  /** Told each time the section opens or closes (with the `details`), so a caller can defer a costly read
   *  until it is open, or scroll what it reveals. */
  onOpenChange?: (open: boolean, details: HTMLDetailsElement) => void
}): JSX.Element {
  const disclosure = useDisclosure(forceOpen, defaultOpen, onOpenChange)
  const ref = useRef<HTMLDetailsElement>(null)

  /* After every render: an invalid field inside reveals the section. `reveal` only sets state when
     closed, so this settles in one extra render. */
  useLayoutEffect(() => {
    if (!disclosure.open && ref.current?.querySelector('[aria-invalid="true"]')) disclosure.reveal()
  })

  return (
    <Box
      component="details"
      ref={ref}
      open={disclosure.open}
      onToggle={disclosure.onToggle}
      onInvalidCapture={disclosure.reveal}
      data-testid={testId}
      sx={theme => (dense ? {
        mt: 0.5,
        // A long body (a captured JSON, a log) scrolls inside itself, never the page.
        minWidth: 0,
        maxWidth: '100%',
        '&[open] > summary::before': { transform: 'rotate(90deg)' },
        '&[open] > summary': { mb: 0.5 },
      } : {
        mb: 2.5,
        /* Closed, the summary is the whole section: the full margin (and the summary's own, now open-only) left a
         * loose band before the next field. Half stays, for a consumer whose layout has no gap of its own. */
        '&:not([open])': { mb: 1 },
        // The chevron's open state (the `details` carries `[open]`).
        '&[open] > summary::before': { transform: 'rotate(90deg)' },
        /* The summary's gap to its fields only while there are fields under it: closed, it left a loose band. */
        '&[open] > summary': { mb: 1, [theme.breakpoints.down(360)]: { mb: 0.5 } },
        [theme.breakpoints.down(481)]: { mb: 1 },
      })}
    >
      <Box
        component="summary"
        data-testid={`${testId}-summary`}
        sx={theme => ({
          cursor: 'pointer',
          display: 'flex',
          alignItems: 'center',
          /* An affordance, not a heading: a rotating leading chevron with control weight and colour. The UA
           * marker is suppressed (it would render a second, differently styled triangle). */
          gap: dense ? 0.5 : 1,
          width: dense ? 'fit-content' : undefined,
          color: dense ? 'text.secondary' : 'text.primary',
          fontSize: dense ? FONT_SIZE.sm : FONT_SIZE.base,
          fontWeight: dense ? FONT_WEIGHT.regular : FONT_WEIGHT.semibold,
          '&::-webkit-details-marker': { display: 'none' },
          '&::before': {
            content: '"\\203A"',
            display: 'inline-block',
            fontSize: dense ? FONT_SIZE.base : SIZE.iconSm,
            lineHeight: LINE_HEIGHT.tight,
            transition: 'transform 120ms',
          },
          // The 44px touch floor on a phone.
          ...(dense ? null : { [theme.breakpoints.down(481)]: { minHeight: SIZE.tapTarget } }),
        })}
      >
        {/* One flex item, so a rich label (`<strong>` and text) keeps its own spacing. */}
        <span>{label}</span>
      </Box>
      {children}
    </Box>
  )
}

export default AdvancedSection
