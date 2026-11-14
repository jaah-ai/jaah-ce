import Typography, { type TypographyProps } from '@mui/material/Typography'
import type { JSX } from 'react'
import { absoluteTime, compactAge, fmtTimestamp, relativeAge, shortDate } from '../lib/relativeTime'
import Tip from './Tip'

/* AN INSTANT THE READER CAN PIN DOWN: the age as text ("4m ago", or `compact` "4m") — or, `absolute`, the stamp
 * as-is, never broken mid-stamp — with the full locale timestamp on hover and a `<time dateTime>` for machines.
 * The ONE home for that pairing (`lib/relativeTime` owns the formats); `variant` defaults to `inherit` so it
 * drops into a running caption unchanged. */
export function RelativeTime({ iso, compact = false, absolute, prefix, suffix, now, testId, variant = 'inherit', sx }: {
  iso: string | null | undefined
  /** The fixed-slot form (`compactAge`) instead of prose (`relativeAge`). */
  compact?: boolean
  /** The date and minute (`fmtTimestamp`) instead of an age; `'short'` for a narrow cell (`shortDate` with the
   *  clock, the year only outside this one), `'date'` for the day alone. */
  absolute?: boolean | 'short' | 'date'
  /** Words before the age ("Updated"), inside the hover target. */
  prefix?: string
  /** Words after the age ("· Oct 19"), inside the hover target. */
  suffix?: string
  /** The clock the prose age is read against (a list ticking on one shared `now`); the wall clock otherwise. */
  now?: Date
  testId?: string
} & Pick<TypographyProps, 'variant' | 'sx'>): JSX.Element {
  const text = !absolute ? (compact ? compactAge(iso) : relativeAge(iso, now))
    : absolute !== true && iso ? shortDate(iso, new Date(), absolute === 'short') : fmtTimestamp(iso ?? null)
  return (
    <Tip title={absoluteTime(iso)}><Typography
      component="time"
      variant={variant}
      dateTime={iso ?? undefined}
      data-testid={testId}
      sx={absolute ? [{ whiteSpace: 'nowrap' }, ...(Array.isArray(sx) ? sx : [sx])] : sx}
    >
      {[prefix, text, suffix].filter(Boolean).join(' ')}
    </Typography></Tip>
  )
}

export default RelativeTime
