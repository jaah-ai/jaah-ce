import { type JSX, type ReactNode, type SyntheticEvent, useLayoutEffect, useRef, useState } from 'react'
import Box from '@mui/material/Box'

/* The console's ONE "Advanced" disclosure: where a form keeps every field a first pass does not need
 * (PRD R3 — the default view shows only the minimal journey; nothing is removed, it is only folded).
 * Labelled "Advanced" everywhere, collapsed by default, and never a place an error can hide:
 *   • `forceOpen` (a caller that knows an inner field is invalid) opens it without
 *     recording a choice, so it closes again when the force ends;
 *   • an inner field that is INVALID opens it by itself — MUI marks an errored input `aria-invalid`,
 *     and a native constraint failure fires `invalid` (captured here) — and that open is sticky, since
 *     the operator has to see the field to fix it.
 * Consumers: `components/tasks/TaskEditForm.tsx`, `pages/credentials/CredentialEditPage.tsx`,
 * `pages/settings/cloudConnections/ConnectionDialog.tsx`, `pages/credentials/AccountEditPage.tsx`. */

/** A `<details>` whose open state has three inputs: a default (closed), a force-open, and the operator's
 *  click, which wins from the moment it happens. The DOM fires `toggle` for a React-driven `open` change
 *  too, so compare against the last rendered value to tell the operator's click apart. */
function useDisclosure(forceOpen: boolean) {
  const [choice, setChoice] = useState<boolean | undefined>(undefined)
  const open = forceOpen || (choice ?? false)
  const renderedRef = useRef(open)
  renderedRef.current = open
  return {
    open,
    reveal: () => setChoice(true),
    onToggle: (e: SyntheticEvent<HTMLDetailsElement>) => {
      if (e.currentTarget.open !== renderedRef.current) setChoice(e.currentTarget.open)
    },
  }
}

export function AdvancedSection({
  children,
  testId = 'form-advanced',
  forceOpen = false,
}: {
  children: ReactNode
  /** The `<details>`'s testid; the summary's is `<testId>-summary`. */
  testId?: string
  /** Open while true (a caller-known error inside). Not recorded as the operator's choice. */
  forceOpen?: boolean
}): JSX.Element {
  const disclosure = useDisclosure(forceOpen)
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
      sx={theme => ({
        mb: '1rem',
        /* Closed, the summary is the whole section: the full margin (and the summary's own, now open-only) left a
         * loose band before the next field. Half stays, for a consumer whose layout has no gap of its own. */
        '&:not([open])': { mb: '0.5rem' },
        // The chevron's open state (the `details` carries `[open]`).
        '&[open] > summary::before': { transform: 'rotate(90deg)' },
        /* The summary's gap to its fields only while there are fields under it: closed, it left a loose band. */
        '&[open] > summary': { mb: '0.5rem', [theme.breakpoints.down(360)]: { mb: '0.25rem' } },
        [theme.breakpoints.down(481)]: { mb: '0.5rem' },
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
          gap: '0.375rem',
          color: 'text.primary',
          fontSize: '0.875rem',
          fontWeight: 600,
          '&::-webkit-details-marker': { display: 'none' },
          '&::before': {
            content: '"\\203A"',
            display: 'inline-block',
            fontSize: '1rem',
            lineHeight: 1,
            transition: 'transform 120ms',
          },
          // The 44px touch floor on a phone.
          [theme.breakpoints.down(481)]: { minHeight: '44px' },
        })}
      >
        Advanced
      </Box>
      {children}
    </Box>
  )
}

export default AdvancedSection
