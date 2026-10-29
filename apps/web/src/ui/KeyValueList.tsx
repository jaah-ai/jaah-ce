/* A KEY/VALUE LIST — a semantic `<dl>` of label-beside-value rows at body scale: a status panel's facts, a
 * dialog's review summary, a drawer's evidence. Labels share one track (`max-content` unless `labelWidth`
 * pins it), so every value starts on the same x; a long value wraps inside its column, never past it. At
 * phone width each label stacks over its value, so neither a long label nor a long value is squeezed —
 * unless `stack={false}`: a list of one-word labels (a dialog's facts) keeps label-beside-value there, since
 * stacking only doubles its height.
 *
 * Not `DetailPage`'s `DetailField` (a caption-scale strip, label over value, for a page's own field grid). Each row is a `div` around its `dt`/`dd` (valid in a `dl`) with
 * `display: contents`, so the pair sits on the list's grid. */
import type { JSX, ReactNode } from 'react'
import Box from '@mui/material/Box'
import Typography from '@mui/material/Typography'
import { PHONE, monoSx } from './listStyles'

export function KeyValueList({
  labelWidth,
  align = 'baseline',
  divided,
  stack = true,
  testId,
  children,
}: {
  /** A fixed label track (e.g. `'7rem'`, or a `minmax(...)` matching a neighbour's); `max-content` by default. */
  labelWidth?: string
  /** `center` when values hold controls or chips taller than a text line. */
  align?: 'baseline' | 'center'
  /** A hairline under every row, for a long list read as a record. */
  divided?: boolean
  /** Label over value at phone width (the default); false keeps the two columns for short labels. */
  stack?: boolean
  testId?: string
  children: ReactNode
}): JSX.Element {
  const cell = divided ? { py: 0.75, borderBottom: 1, borderColor: 'divider' } : null
  return (
    <Box
      component="dl"
      data-testid={testId}
      sx={{
        m: 0,
        display: 'grid',
        gridTemplateColumns: `${labelWidth ?? 'max-content'} minmax(0, 1fr)`,
        columnGap: 2.5,
        rowGap: divided ? 0 : 1,
        alignItems: align,
        ...(cell ? { '& > div > dt, & > div > dd': cell } : null),
        // Label over value: the pair's gap is tight, the gap between pairs carries the rhythm.
        ...(stack
          ? {
              [PHONE]: {
                gridTemplateColumns: 'minmax(0, 1fr)',
                rowGap: 0,
                '& > div > dt': divided ? { pb: 0, borderBottom: 0 } : { pt: 1 },
                '& > div:first-of-type > dt': divided ? null : { pt: 0 },
                '& > div > dd': { pt: 0.25 },
              },
            }
          : null),
      }}
    >
      {children}
    </Box>
  )
}

export function KeyValue({
  label,
  strong,
  mono,
  color,
  title,
  testId,
  valueTestId,
  children,
}: {
  label: ReactNode
  /** Semibold value — a count or headline figure the row exists to report. */
  strong?: boolean
  /** Monospace value — a path, branch or identifier. */
  mono?: boolean
  /** A value colour, e.g. `'error.main'` for a non-zero failure count. */
  color?: string
  /** A hover title on the value (an absolute time behind a relative one). */
  title?: string
  /** On the row. */
  testId?: string
  /** On the `dd`, the handle tests read the value through. */
  valueTestId?: string
  children: ReactNode
}): JSX.Element {
  return (
    <Box data-testid={testId} sx={{ display: 'contents' }}>
      <Typography component="dt" variant="body2" sx={{ color: 'text.secondary', minWidth: 0 }}>{label}</Typography>
      <Typography
        component="dd"
        variant="body2"
        title={title}
        data-testid={valueTestId}
        sx={[{ m: 0, minWidth: 0, overflowWrap: 'anywhere', fontWeight: strong ? 600 : undefined, color }, mono ? monoSx : {}]}
      >
        {children}
      </Typography>
    </Box>
  )
}
