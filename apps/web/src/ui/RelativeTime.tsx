import Typography, { type TypographyProps } from '@mui/material/Typography'
import type { JSX } from 'react'
import { absoluteTime, compactAge, relativeAge } from '../lib/relativeTime'

/* AN AGE THE READER CAN PIN DOWN: the age as text ("4m ago", or `compact` "4m"), the full locale timestamp
 * on hover, and a `<time dateTime>` for machines. The ONE home for that pairing (`lib/relativeTime` owns
 * the formats); `variant` defaults to `inherit` so it drops into a running caption unchanged. */
export function RelativeTime({ iso, compact = false, prefix, variant = 'inherit', sx }: {
  iso: string | null | undefined
  /** The fixed-slot form (`compactAge`) instead of prose (`relativeAge`). */
  compact?: boolean
  /** Words before the age ("Updated"), inside the hover target. */
  prefix?: string
} & Pick<TypographyProps, 'variant' | 'sx'>): JSX.Element {
  const age = compact ? compactAge(iso) : relativeAge(iso)
  return (
    <Typography
      component="time"
      variant={variant}
      dateTime={iso ?? undefined}
      title={absoluteTime(iso)}
      sx={sx}
    >
      {prefix ? `${prefix} ${age}` : age}
    </Typography>
  )
}

export default RelativeTime
