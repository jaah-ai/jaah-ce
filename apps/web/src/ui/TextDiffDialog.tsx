import { type JSX, Fragment, useEffect, useMemo, useRef, useState } from 'react'
import Box from '@mui/material/Box'
import ButtonBase from '@mui/material/ButtonBase'
import Typography from '@mui/material/Typography'
import useMediaQuery from '@mui/material/useMediaQuery'
import { alpha, type Theme } from '@mui/material/styles'
import FormDialog from './FormDialog'
import { withAdded } from '../lib/sets'
import { PHONE_QUERY, tapTargetSx } from './listStyles'
import { type DiffCell, type DiffRow, diffRows, diffStats, foldRows } from './textDiff'

/* A git-like comparison of two texts (`ui/textDiff.ts`): side-by-side on a wide screen, one unified column on
 * a phone, where two columns of prose would be a few words wide. Unchanged runs fold to three lines of
 * context around each change; a fold opens in place. Lines WRAP rather than scroll sideways — this compares
 * prose, and a paragraph is one line. */

type Tone = 'removed' | 'added'

const CELL_PAD = '0.125rem 0.5rem'

function toneColor(theme: Theme, tone: Tone) {
  return tone === 'removed' ? theme.palette.error.main : theme.palette.success.main
}

/* The empty side of a pure insert or delete — hatched, as git GUIs draw "no line here". */
const emptySx = (theme: Theme) => ({
  backgroundImage: `repeating-linear-gradient(-45deg, transparent 0 4px, ${theme.palette.divider} 4px 5px)`,
})

const gutterSx = {
  p: CELL_PAD,
  color: 'text.disabled',
  textAlign: 'right' as const,
  userSelect: 'none' as const,
  whiteSpace: 'nowrap' as const,
}

const textSx = { p: CELL_PAD, whiteSpace: 'pre-wrap' as const, overflowWrap: 'anywhere' as const, minWidth: 0 }

function lineSx(tone: Tone | null) {
  return (theme: Theme) => tone ? { bgcolor: alpha(toneColor(theme, tone), 0.1) } : {}
}

function CellText({ cell, tone }: { cell: DiffCell; tone: Tone | null }) {
  if (cell.segments.every(s => s.text === '')) return <>{' '}</>
  return (
    <>
      {cell.segments.map((s, i) => s.changed && tone ? (
        <Box
          key={i}
          component={tone === 'removed' ? 'del' : 'ins'}
          sx={(theme: Theme) => ({
            textDecoration: 'none',
            borderRadius: '2px',
            bgcolor: alpha(toneColor(theme, tone), 0.3),
          })}
        >
          {s.text}
        </Box>
      ) : <Fragment key={i}>{s.text}</Fragment>)}
    </>
  )
}

/* Side-by-side: one grid row per diff row, so a wrapped line on one side keeps its partner level with it. */
function SplitRow({ row }: { row: DiffRow }) {
  const changed = row.kind === 'change'
  const side = (cell: DiffCell | undefined, tone: Tone) => cell ? (
    <>
      <Box sx={[gutterSx, lineSx(changed ? tone : null)]}>{cell.no}{changed ? (tone === 'removed' ? ' −' : ' +') : ''}</Box>
      <Box sx={[textSx, lineSx(changed ? tone : null)]}><CellText cell={cell} tone={changed ? tone : null} /></Box>
    </>
  ) : (
    <Box sx={[{ gridColumn: 'span 2' }, emptySx]} aria-hidden="true" />
  )
  return (
    <Box data-testid="text-diff-row" data-kind={row.kind} sx={{ display: 'contents' }}>
      {side(row.before, 'removed')}
      <Box sx={{ bgcolor: 'divider' }} aria-hidden="true" />
      {side(row.after, 'added')}
    </Box>
  )
}

/* Unified: a modified row becomes its removed line then its added line. */
function UnifiedRow({ row }: { row: DiffRow }) {
  const line = (cell: DiffCell, tone: Tone | null, mark: string) => (
    <>
      <Box sx={[gutterSx, lineSx(tone)]}>{cell.no}{mark}</Box>
      <Box sx={[textSx, lineSx(tone)]}><CellText cell={cell} tone={tone} /></Box>
    </>
  )
  return (
    <Box data-testid="text-diff-row" data-kind={row.kind} sx={{ display: 'contents' }}>
      {row.kind === 'same'
        ? line(row.after, null, '')
        : (
          <>
            {row.before && line(row.before, 'removed', ' −')}
            {row.after && line(row.after, 'added', ' +')}
          </>
        )}
    </Box>
  )
}

/* `rows` from `diffRows`. Fold state is by block position, so a caller swapping texts remounts (`key`). */
export function TextDiff({ rows, unified }: { rows: DiffRow[]; unified: boolean }): JSX.Element {
  const blocks = useMemo(() => foldRows(rows), [rows])
  const [opened, setOpened] = useState<ReadonlySet<number>>(new Set())
  /* The fold just opened: its button is gone, so focus moves to the grid rather than falling to the body. */
  const gridRef = useRef<HTMLDivElement>(null)
  const [justOpened, setJustOpened] = useState(false)
  useEffect(() => {
    if (justOpened) { gridRef.current?.focus(); setJustOpened(false) }
  }, [justOpened])

  if (!rows.some(r => r.kind === 'change')) {
    return (
      <Typography variant="body2" sx={{ color: 'text.secondary', p: 2 }} data-testid="text-diff-none">
        No differences.
      </Typography>
    )
  }
  const Row = unified ? UnifiedRow : SplitRow
  return (
    <Box
      ref={gridRef}
      tabIndex={-1}
      data-testid="text-diff"
      data-layout={unified ? 'unified' : 'split'}
      sx={(theme: Theme) => ({
        display: 'grid',
        gridTemplateColumns: unified ? 'auto minmax(0, 1fr)' : 'auto minmax(0, 1fr) 1px auto minmax(0, 1fr)',
        ...theme.typography.mono,
        fontSize: '0.75rem',
        lineHeight: 1.6,
        outline: 'none',
      })}
    >
      {blocks.map((block, i) => block.kind === 'rows' || opened.has(i)
        ? block.rows.map((r, k) => <Row key={`${i}:${k}`} row={r} />)
        : (
          <ButtonBase
            key={i}
            data-testid="text-diff-fold"
            onClick={() => { setOpened(prev => withAdded(prev, i)); setJustOpened(true) }}
            sx={(theme: Theme) => ({
              ...tapTargetSx(theme),
              gridColumn: '1 / -1',
              justifyContent: 'flex-start',
              p: CELL_PAD,
              font: 'inherit',
              color: 'primary.main',
              bgcolor: alpha(theme.palette.primary.main, 0.06),
              '&:hover, &.Mui-focusVisible': { bgcolor: alpha(theme.palette.primary.main, 0.12) },
            })}
          >
            ⋯ {block.rows.length} unchanged lines
          </ButtonBase>
        ))}
    </Box>
  )
}

export default function TextDiffDialog({
  title,
  before,
  after,
  beforeLabel = 'Before',
  afterLabel = 'After',
  onClose,
}: {
  title: string
  before: string
  after: string
  beforeLabel?: string
  afterLabel?: string
  onClose: () => void
}): JSX.Element {
  const phone = useMediaQuery(PHONE_QUERY)
  const rows = useMemo(() => diffRows(before, after), [before, after])
  const stats = useMemo(() => diffStats(rows), [rows])
  return (
    <FormDialog
      title={title}
      submitLabel="Close"
      hideCancel
      onSubmit={onClose}
      onClose={onClose}
      fullScreenOnPhone
      maxWidth="lg"
      testId="text-diff-dialog"
      contentSx={{ p: 0 }}
      header={
        <Box sx={{ px: 2, pb: 1, display: 'flex', flexWrap: 'wrap', gap: 1.5, alignItems: 'baseline' }}>
          <Typography variant="body2" sx={{ color: 'text.secondary' }}>
            {beforeLabel} → {afterLabel}
          </Typography>
          <Typography variant="body2" data-testid="text-diff-stats" sx={{ fontWeight: 600 }}>
            <Box component="span" sx={{ color: 'error.main' }}>−{stats.removed}</Box>{' '}
            <Box component="span" sx={{ color: 'success.main' }}>+{stats.added}</Box>
          </Typography>
        </Box>
      }
    >
      <TextDiff key={`${before}\u0000${after}`} rows={rows} unified={phone} />
    </FormDialog>
  )
}
