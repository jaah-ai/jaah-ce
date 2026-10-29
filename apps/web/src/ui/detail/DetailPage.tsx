/* THE CONSOLE'S ONE DETAIL PAGE — the skeleton `/tasks/:id`, `/projects/:id` and `/nodes/:id` render.
 * A prop exists only where they differ for a REASON (a reading measure vs a fill-the-viewport one);
 * everything else — grouping, read-only field, cell — is decided here, each at the thing it decides. */
import { forwardRef } from 'react'
import type { JSX, ReactNode, Ref } from 'react'
import Box from '@mui/material/Box'
import Typography from '@mui/material/Typography'
import type { SxProps, Theme } from '@mui/material/styles'
import { PHONE, SECTION_FILL_ATTR, SHELL_FILL_ATTR } from '../listStyles'
import PageHeader from '../PageHeader'
import HelpLabel from '../HelpLabel'

/* THE PAGE'S MEASURE. `read` caps the column at 64rem; `fill` makes the page a flex column owning the
 * viewport height, handed to the ONE section marked `fill` (a console, transcript or editor).
 * `fill` IS `height: '100%'`, NOT `flex: '1 1 auto'`: `App.tsx`'s `APP_MAIN_SX` has no `display: flex`, so
 * a flex shorthand on its child is inert; the percentage works because `.app-main` has a DEFINITE height.
 * `minHeight: 0` is required, or an inner scrollport silently becomes a PAGE scrollport. */
export type DetailMeasure = 'read' | 'fill'

const MEASURE_SX: Record<DetailMeasure, SxProps<Theme>> = {
  read: { maxWidth: '64rem' },
  fill: { display: 'flex', flexDirection: 'column', height: '100%', minHeight: 0, gap: 2 },
}

export const DetailPage = forwardRef(function DetailPage({
  breadcrumb,
  title,
  titleAside,
  subtitle,
  actions,
  measure = 'read',
  children,
  actionBar,
  testId,
  headerTestId,
  sx,
}: {
  breadcrumb?: ReactNode
  /* OPTIONAL: a trail ending in the record's name already carries the `<h1>`; one ending in a CATEGORY
     still owes a title (`ui/PageHeader` owns the rule). */
  title?: ReactNode
  titleAside?: ReactNode
  subtitle?: ReactNode
  actions?: ReactNode
  measure?: DetailMeasure
  children: ReactNode
  actionBar?: ReactNode
  testId?: string
  /** The header's own testid: pages pin it via `& > [data-testid="…"]`, and an e2e oracle finds the
   *  breadcrumb's `<h1>` through it. */
  headerTestId?: string
  /* THE PAGE'S OWN RULES, merged AFTER the measure: direct-child selectors (`& > …`) against this exact box
   * (scrollports, tab-strip pinning, the phone block), which a wrapper would break. Not for a value the
   * control should have learned instead. */
  sx?: SxProps<Theme>
}, ref: Ref<HTMLDivElement>): JSX.Element {
  return (
    <Box
      ref={ref}
      data-testid={testId}
      {...(measure === 'fill' && { [SHELL_FILL_ATTR]: '' })}
      sx={[MEASURE_SX[measure], ...(Array.isArray(sx) ? sx : [sx])]}
    >
      <PageHeader
        testId={headerTestId}
        breadcrumb={breadcrumb}
        title={title}
        titleAside={titleAside}
        subtitle={subtitle}
        actions={actions}
      />
      {children}
      {/* THE SAVE BAR IS PAGE-LEVEL, always: inside a tab panel it is unreachable from the other tabs. */}
      {actionBar}
    </Box>
  )
})

/* A tab panel's stack of sections, and the page's verbs ABOVE the tab strip (so they are on every tab). */
export const DETAIL_SECTIONS_SX = { display: 'flex', flexDirection: 'column', gap: 2, pt: 1.5 } as const
export const DETAIL_ACTIONS_SX = { display: 'flex', flexWrap: 'wrap', alignItems: 'center', gap: 1, mb: 1 } as const

/** A section title's look (`variant="subtitle2"` with this `sx`), for a panel heading a sub-section of its own
 *  that sits beside `DetailSection`s as their peer (`pages/orgs/SsoTab.tsx`). */
export const SECTION_TITLE_SX = { fontWeight: 600, lineHeight: 1.6, minWidth: 0 } as const

/* A GROUP OF FIELDS, BORDERLESS: the tab panel already draws the boundary (a card inside cost ~20px of
 * phone width). `ui/SectionCard` keeps its other callers.
 * `divided` DRAWS A TOP RULE and is OPT-IN (~22px per section on a page whose fit is measured).
 * `fill`: the one section on a `measure="fill"` page absorbing the remaining height; `flexShrink: 0` is
 * load-bearing (a shrinking panel clipped 112px into 87px). `components/tasks/GateForm.tsx` gets a
 * definite height only from a `fill` section. `gap: 1.75` is the former `.task-detail-section` 0.625rem.
 * `headingLevel`: `h2` directly under a tab strip, `h3` nested — the screen-reader heading outline.
 * `actions`: a control cluster on the title row (a page-level "Fix all"), wrapping under the title on a phone.
 * `fields`: the children are `DetailField`s, so they sit in a `<dl>` (a `dt`/`dd` pair outside one is invalid).
 * THE CONSOLE'S ONE SECTION GROUPER: do not hand-roll another titled `<section>`. */
export function DetailSection({
  title,
  titleId,
  help,
  divided,
  fill,
  headingLevel = 'h2',
  actions,
  fields,
  children,
  testId,
}: {
  title?: string
  /** Point a single field's `aria-labelledby` at the group when the group's name IS the field's name.
   *  Never for two or more fields — it renames every one. */
  titleId?: string
  /** A few words shown on hovering the title (`ui/HelpLabel`), for a section whose title names its field. */
  help?: string
  divided?: boolean
  fill?: boolean
  headingLevel?: 'h2' | 'h3'
  actions?: ReactNode
  fields?: boolean
  children: ReactNode
  testId?: string
}): JSX.Element {
  const heading = title && (
    <Typography id={titleId} variant="subtitle2" component={headingLevel} sx={SECTION_TITLE_SX}>
      <HelpLabel label={title} help={help} />
    </Typography>
  )
  return (
    <Box
      component="section"
      data-testid={testId}
      {...(fill && { [SECTION_FILL_ATTR]: '' })}
      sx={{
        ...(fill
          ? { flex: '1 1 auto', minHeight: 0, display: 'flex', flexDirection: 'column' }
          : { flexShrink: 0 }),
        display: 'flex',
        flexDirection: 'column',
        gap: 1.75,
        minWidth: 0,
        ...(divided ? { mt: 2, pt: 1.5, borderTop: '1px solid', borderColor: 'divider' } : {}),
      }}
    >
      {actions ? (
        <Box sx={{ display: 'flex', flexWrap: 'wrap', alignItems: 'center', justifyContent: 'space-between', gap: 1 }}>
          {heading}
          {actions}
        </Box>
      ) : heading}
      {fields ? (
        <Box component="dl" sx={{ m: 0, display: 'flex', flexDirection: 'column', gap: 1.75, minWidth: 0 }}>
          {children}
        </Box>
      ) : children}
    </Box>
  )
}

/* ONE READ-ONLY FACT — label above value when wide, BESIDE it at phone width (stacked, eight fields cost
 * ~447px); a `4.5rem` label minimum keeps values aligned. A `dt`/`dd` pair inside a `<dl>` (a `fields`
 * `DetailSection`'s, or the page's own grid). */
export function DetailField({
  label,
  wide,
  fullRow,
  hideOnPhone,
  testId,
  labelTestId,
  dense,
  inline,
  children,
}: {
  label: string
  /** Spans two grid columns, for a cluster rather than a word. Unconditional (≥1100px-only left a 1024px
   *  iPad wrapping); on the phone the row's own columns, since `span 2` in a one-column grid adds a column. */
  wide?: boolean
  /** Spans the whole grid row, for a cluster that reads as one line (a status, its place, its links). */
  fullRow?: boolean
  /** Dropped at phone width, for desktop-only context. */
  hideOnPhone?: boolean
  /** Overrides the default token; the grid-wide `data-field` stays, so geometry sweeps see every cell. */
  testId?: string
  /** A handle on the LABEL, so a label oracle need not re-derive which `dt`s are fields. */
  labelTestId?: string
  /** The value at `body2`, for a card whose other text is `body2` (a queue card), so values do not outsize it. */
  dense?: boolean
  /** On the phone, keeps the value beside its label, wrapping inside its column: for a short value with a note
   *  riding it ("8 · No task slot"), which at its content width would drop below the label. */
  inline?: boolean
  children: ReactNode
}): JSX.Element {
  return (
    <Box
      component="div"
      data-testid={testId ?? (wide ? 'detail-field-wide' : 'detail-field')}
      data-wide={wide ? 'true' : undefined}
      data-full-row={fullRow ? 'true' : undefined}
      data-field={label}
      sx={{
        display: 'flex',
        flexDirection: 'column',
        gap: 0.35,
        minWidth: 0,
        ...(wide ? { gridColumn: 'span 2' } : null),
        ...(fullRow ? { gridColumn: '1 / -1' } : null),
        [PHONE]: {
          ...(hideOnPhone ? { display: 'none' } : null),
          ...(wide ? { gridColumn: '1 / -1' } : null),
          flexDirection: 'row',
          flexWrap: 'wrap',
          alignItems: 'baseline',
          gap: 0.6,
        },
      }}
    >
      <Typography
        component="dt"
        variant="caption"
        data-testid={labelTestId}
        sx={{ color: 'text.secondary', [PHONE]: { flex: '0 0 auto', minWidth: '4.5rem' } }}
      >
        {label}
      </Typography>
      <Box
        component="dd"
        sx={{
          m: 0,
          color: 'text.primary',
          minWidth: 0,
          overflowWrap: 'anywhere',
          ...(dense ? { typography: 'body2' } : null),
          ...(inline ? { [PHONE]: { flex: '1 1 0' } } : null),
        }}
      >
        {children}
      </Box>
    </Box>
  )
}

/* A CELL INSIDE A `ui/FieldRow` — a content-driven basis, never a breakpointed CSS grid (`ui/FieldRow`'s
 * named anti-pattern). `basis` omitted takes the content's width. */
export function DetailCell({
  basis,
  max,
  children,
  sx,
}: {
  basis?: string
  max?: string
  children: ReactNode
  /* USUALLY THE ROW GUTTER: `ui/FieldRow` leaves `rowGap` undefined, so each cell carries its bottom
     margin (`rowGap` would drop it off the row's bottom edge). */
  sx?: SxProps<Theme>
}): JSX.Element {
  return (
    <Box
      sx={[
        { flex: basis ? `1 1 ${basis}` : '0 1 auto', minWidth: 0, maxWidth: max },
        ...(Array.isArray(sx) ? sx : [sx]),
      ]}
    >
      {children}
    </Box>
  )
}

export default DetailPage
