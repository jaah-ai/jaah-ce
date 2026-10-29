/* THE RATING BADGE — a done Task's average rating as one number and one star.
 *
 * Its hosts (the Activity rows, `TaskRating.tsx`'s collapsed state) share one drawing
 * so one task never shows two scores. Follows `AuthorBadge.tsx`'s conventions; only differences here:
 *   · Identity (this file): rounding (`lib/taskRating.ts:formatAverage`), star, mono face, amber, `title`.
 *   · Geometry (host `sx`): applied BETWEEN layout defaults (first, overridable — 13px is what both list
 *     surfaces measure at) and identity (last, not overridable).
 *
 * ── NOT ARIA-HIDDEN (unlike AuthorBadge): the number IS the fact. Only the STAR is hidden; "out of 5" is
 * on `title` (`ratingSummaryTitle`).
 * ── Draws NOTHING for an unrated task (`count === 0`): a `0.0` reads as a terrible score
 * (absence is not a state). A host needing width draws its own blank.
 */
import Box from '@mui/material/Box'
import type { SxProps, Theme } from '@mui/material/styles'
import { formatAverage, ratingSummaryTitle } from '../../lib/taskRating'
import { Icon } from '../../ui/icons'

/* The star tone as a literal light/dark pair via `theme.applyStyles('dark', …)` (`lib/authorBadge.ts`'s
   `AUTHOR_HUES` block says why a literal carries both schemes). Not `palette.warning.main`: on `/tasks`
   warning means "asking for an answer". `AUTHOR_HUES`' amber values, copied not imported (that table may
   be re-tuned). Amber-700 on white, amber-300 on `#1e293b`. */
const STAR_TONE = { light: '#b45309', dark: '#fcd34d' } as const

/* MUI's Material star PATH, inlined: `★` is a font glyph (weight and centring vary), `@mui/icons-material`
   is not a dependency, and the internal svg-icon path is private. Same shape as the `<Rating>` in
   `TaskRating.tsx`, so collapsing to the badge does not change the star. */
const STAR_PATH =
  'M12 17.27L18.18 21l-1.64-7.03L22 9.24l-7.19-.61L12 2 9.19 8.63 2 9.24l5.46 4.73L5.82 21z'

/* `em`-sized (tracks the badge font), tinted here so digits stay neutral. `0.9em`: the star's ink fills its
   viewBox, so at 1em it reads heavier than the digits (0.9 is the measured match). `display: block` kills
   the inline-SVG baseline gap (~3px). */
function StarGlyph() {
  return (
    <Icon
      viewBox="0 0 24 24"
      size="0.9em"
      sx={[
        { display: 'block' },
        theme => ({ fill: STAR_TONE.light, ...theme.applyStyles('dark', { fill: STAR_TONE.dark }) }),
      ]}
    >
      <path d={STAR_PATH} />
    </Icon>
  )
}

export default function RatingBadge({
  average,
  count,
  sx,
  title,
  testId = 'task-rating-badge',
}: {
  /**
   * The MEAN across every vote, straight off the server (`TaskRating.average` / `rating_average`); never
   * derived client-side (`lib/taskRating.ts`). `null` is a task nobody has rated.
   */
  average: number | null
  /**
   * How many votes the mean is over. `0` renders nothing; also completes the `title` sentence (a 5 from one
   * voter differs from a 5 from nine).
   */
  count: number
  /**
   * The host's geometry (width, margins, alignment, type scale), applied after layout defaults and before
   * identity, so it can place and size but not repaint the score.
   */
  sx?: SxProps<Theme>
  /**
   * OVERRIDE the `title` sentence, or `null` for none. `undefined` derives it (`ratingSummaryTitle`).
   * `null` exists for a host wrapping this in a MUI `Tooltip` (`TaskRating`'s collapsed state): the badge's
   * own descendant `title` would surface a second native tooltip.
   */
  title?: string | null
  /** The spec handle. Two badges for two tasks on one screen stay individually addressable. */
  testId?: string
}): JSX.Element | null {
  const shown = formatAverage(average)
  /* Both conditions: `formatAverage` gives `''` for a null/non-finite average, and count > 0 beside an
     unusable average is a wire contradiction (`ratingSummaryTitle`). Never paint an empty score. */
  if (count <= 0 || shown === '') return null

  return (
    <Box
      component="span"
      data-testid={testId}
      /* The whole rating as one sentence (average, denominator, vote count). `?? undefined`, not `?? ''`: an
         empty `title` attribute is announced by some readers. */
      title={(title === undefined ? ratingSummaryTitle(count, average) : title) ?? undefined}
      sx={[
        {
          /* LAYOUT DEFAULTS — overridable. `lineHeight: 1` keeps height = font size. `gap: 0.25` (1.5px on
             the 6px unit): "4.5" at 13px mono ~23px + star ~12px ≈ 37px, inside the ~40px both list
             surfaces budget. A wider gap breaks a measured column. */
          display: 'inline-flex',
          alignItems: 'center',
          gap: 0.25,
          fontSize: '0.8125rem',
          lineHeight: 1,
          // Never wrap between number and star.
          whiteSpace: 'nowrap',
        },
        ...(Array.isArray(sx) ? sx : [sx]),
        // MUI's merge idiom for an `sx` PROP (see AuthorBadge): the incoming value may itself be an array.
        theme => ({
          /* MONO: a measurement is mono (`TaskRunMetrics.tsx`), and stacked rows keep decimal points aligned. */
          fontFamily: theme.typography.mono.fontFamily,
          fontWeight: 600,
          /* The number is neutral and only the star amber: fifty amber numbers on a board emphasise nothing. */
          color: 'text.primary',
        }),
      ]}
    >
      {shown}
      <StarGlyph />
    </Box>
  )
}
