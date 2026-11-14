import { forwardRef } from 'react'
import Box from '@mui/material/Box'
import Link, { type LinkProps } from '@mui/material/Link'
import { safeHref } from '../lib/safeHref'

/* THE CONSOLE'S ONE LINK TO AN OUTSIDE PAGE: a MUI `Link` opened in a new tab, so unsaved edits stay behind.
 * The two fixed props are what every site used to spell out (three spellings of `rel` drifted): `_blank`,
 * and `noopener noreferrer` — `noreferrer` alone leaves the opener reachable in older engines; `noopener`
 * alone leaks the referrer. `href` is the console's ONE verdict (`lib/safeHref.ts`): an absent, relative or
 * non-http(s) one renders the children as plain text, never a link — the href is usually runner-reported,
 * typed or scraped. Everything else (`sx`, `underline`, `variant`, `color`, `title`, `onClick`, `aria-*`,
 * `data-testid`) is the caller's. The ref reaches the `<a>`, so a `Tip` can hold it as a bare `Link` would. */
export type ExternalLinkProps = Omit<LinkProps<'a'>, 'component' | 'href' | 'target' | 'rel' | 'ref'> & {
  href: string | null | undefined
  'data-testid'?: string
}

const ExternalLink = forwardRef<HTMLAnchorElement, ExternalLinkProps>(function ExternalLink({ href, ...props }, ref) {
  const safe = safeHref(href)
  // Refused: the children as text, in the caller's `sx` (a mono PR number stays mono) and under its testid.
  if (!safe) return (props.sx || props['data-testid'])
    ? <Box component="span" sx={props.sx} data-testid={props['data-testid']}>{props.children}</Box>
    : <>{props.children}</>
  // The caller's props first: the fixed ones win at runtime too, not only in the type.
  return <Link {...props} ref={ref} href={safe} target="_blank" rel="noopener noreferrer" />
})

export default ExternalLink
