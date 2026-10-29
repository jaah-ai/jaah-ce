/* The author badge component: a creator's initials in their hue, shared by the Activity Feed and the
 * creator filter picker — one drawing, so the picker and the row cannot show different badges for the
 * same person.
 * This file owns identity (initials, hue, full name on `title`); the host owns geometry via `sx`.
 * Why the hues are literals spread through `theme.applyStyles('dark', …)`: `lib/authorBadge.ts`'s
 * `AUTHOR_HUES` block. */
import Box from '@mui/material/Box'
import type { SxProps, Theme } from '@mui/material/styles'
import { authorHue, authorInitials } from '../../lib/authorBadge'

export default function AuthorBadge({
  name,
  sx,
  testId = 'feed-author',
}: {
  /**
   * The creator's RESOLVED DISPLAY NAME, never a uuid (see `types.ts`'s `Provenance`). Not nullable: a
   * caller with no name renders no badge, deciding that at the call site.
   */
  name: string
  /**
   * Host geometry. Applied BEFORE the hue, so a host cannot repaint the identity channel.
   */
  sx?: SxProps<Theme>
  /**
   * Test handle. Defaults to the Activity Feed's `feed-author`; a second host passes its own.
   */
  testId?: string
}): JSX.Element {
  const hue = authorHue(name)
  return (
    <Box
      component="span"
      data-testid={testId}
      title={name}
      /* Always `aria-hidden`, no opt-out: initials and a hue mean nothing to a screen reader. The HOST names
       * the author — the Activity Feed folds `created by <name>` into the row's accessible name. `title` on a generic span is
       * not a reliable accessible name, so there is no working "let the badge announce" variant. */
      aria-hidden="true"
      sx={[
        // MUI's `sx` merge idiom: spread, since a nested array is illegal. The hue goes LAST so it wins.
        ...(Array.isArray(sx) ? sx : [sx]),
        theme => ({ color: hue.light, ...theme.applyStyles('dark', { color: hue.dark }) }),
      ]}
    >
      {authorInitials(name)}
    </Box>
  )
}
