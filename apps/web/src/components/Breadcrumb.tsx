import Breadcrumbs from '@mui/material/Breadcrumbs'
import Link from '@mui/material/Link'
import Typography from '@mui/material/Typography'
import { useCallback, useLayoutEffect, useRef, type ElementType, type MouseEvent, type ReactNode, type Ref } from 'react'
import { Link as RouterLink } from 'react-router-dom'
import { ABOVE_PHONE } from '../ui/listStyles'

export type BreadcrumbItem = {
  // A `ReactNode`, so a crumb can BE a control (the task editor's click-to-edit title) without a second
  // Breadcrumbs shell. A plain string renders as before.
  label: ReactNode
  // A trail link. The last item is always rendered as plain current-page text regardless of `to`.
  to?: string
  // A crumb that ACTS (opens a menu, or intercepts navigation for unsaved work): a `<button>` in the same
  // `Link` skin and 44px target, not an `<a>` with a fake href. Ignored on the last item and when `to` is set.
  onClick?: (event: MouseEvent<HTMLElement>) => void
  /** Accessible name for an `onClick` crumb whose `label` is not plain text. */
  ariaLabel?: string
  /** The crumb's plain text when `label` is a node: a name no longer than the floor stays whole (`WHOLE_CHARS`). */
  text?: string
}

// The shared 44px tap-target geometry (AC11). `inline-block` gives one measurable fragment; equal negative
// margin cancels the padding from layout, keeping the ~33x18 footprint.
const CRUMB_HIT_SX = {
  display: 'inline-block',
  py: '14px',
  px: '8px',
  my: '-14px',
  mx: '-8px',
} as const

// A small trail: earlier items with `to` are links, the last is the current page. MUI `Breadcrumbs`
// supplies structure, separator and wrap. Suites resolve it via `data-testid="breadcrumb"` and
// `aria-current="page"`, never a class.
/* `oneLine`: ABOVE THE PHONE the trail keeps one line — the crumbs never wrap and each ellipsises (a crumb
   owes its full text on `title`). THE LAST GIVES WAY FIRST: its shrink weight dwarfs the earlier crumbs',
   so the title ellipsises while the project and feature names stay whole ("org-b-probe-p… › No fe…" beside a whole
   title told the reader less). The last never drops under `ONE_LINE_LAST_FLOOR` (7rem, or 30% of the trail): past
   it the earlier crumbs shrink instead, since a 1024px split once left the title "1: Retir…". A half-trail
   floor clipped both names in a 344px trail while the title it reserved for needed less, and so did 8rem/33%:
   at 344px the names stay whole and the title keeps ~100px.
   A split editor at 1024px left ~400px of header, and a wrapped "acme › No feature" / "› 32: title" cost the page
   a second line on every task. The phone keeps WRAP, NEVER TRUNCATE (below): there the title is the page's only
   identifier and a ~264px leftover would cut it to a word. A crumb holding an input still grows (`:has`). */
const ONE_LINE_LAST_FLOOR = 'min(7rem, 30%)'
/* The wrapping trail's: a leftover narrower than this sends the last crumb to its own line, so a phone title is never
   a three-line sliver beside the crumbs (6rem fitted only "7420: Retire"). */
const LAST_FLOOR = '10rem'
/* EVERY EARLIER CRUMB KEEPS A FEW CHARACTERS: the title's floor alone let the shrinking crumbs go to
   nothing, the feature crumb first ("acme ▾ › › 7420: …"). Four characters and the ellipsis, plus room for a picker
   crumb's ▾ (`TaskBreadcrumb`); a crumb after the first also carries its leading `›` (below), so one em more. */
const ONE_LINE_CRUMB_FLOOR = 'calc(4ch + 2em)'
/* …BUT A FLOOR IS A MINIMUM WIDTH, and a name shorter than it ("ops ▾") was padded out to it, a blank before its `›`.
   A crumb whose text fits the floor anyway is marked `data-crumb-whole` and never shrinks instead: it costs no more. */
const WHOLE_CHARS = 4
const ONE_LINE_SX = {
  [ABOVE_PHONE]: {
    '& .MuiBreadcrumbs-ol': { flexWrap: 'nowrap' },
    // Clipped at the crumb: a shrunk picker crumb (an atomic inline-flex) would otherwise paint over the next `›`.
    // A loose cap, only for a pathological name: a 500px feature name would otherwise pin the title at its floor.
    // Of the trail's QUERY CONTAINER (`cqi`, the task header's lead), not `%`: the trail shrink-wraps a short one, and
    // 45% of its own width clipped "mr-6382" to "mr-63…" with the header half empty.
    '& .MuiBreadcrumbs-li': {
      flex: '0 1 auto',
      maxWidth: 'min(20rem, 45cqi)',
      minWidth: ONE_LINE_CRUMB_FLOOR,
      whiteSpace: 'nowrap',
      overflow: 'hidden',
    },
    '& .MuiBreadcrumbs-li:not(:first-of-type)': { minWidth: `calc(${ONE_LINE_CRUMB_FLOOR} + 1em)` },
    '& .MuiBreadcrumbs-li:has(> [data-crumb-whole])': { minWidth: 'auto', flexShrink: 0 },
    '& .MuiBreadcrumbs-li:last-of-type': { flex: '1 100000 auto', maxWidth: 'none', minWidth: ONE_LINE_LAST_FLOOR },
    '& .MuiBreadcrumbs-li > *': { overflow: 'hidden', textOverflow: 'ellipsis' },
  },
} as const

// The `›` each later crumb draws; on a wrapped trail, the crumb ending a line draws it instead.
const SEPARATOR_SX = {
  // The plain glyph first: a browser without the `/ ""` alt-text syntax drops that declaration.
  content: ['"›"', '"›" / ""'],
  flex: '0 0 auto',
  mx: 0.5,
  userSelect: 'none',
} as const

/* Marks each crumb that starts a line of a wrapped trail (`data-line-start`) and the crumb ending the line above it
   (`data-line-end`), on every resize. Each pass starts from the unmarked layout, then re-measures after marking: the
   "›" a line-ending crumb takes on can push that crumb down too, and it is then marked as starting its line. A
   one-line trail never marks anything. */
function useLineBreaks(trailRef: { current: HTMLElement | null }, crumbCount: number, oneLine: boolean) {
  useLayoutEffect(() => {
    const trail = trailRef.current
    if (!trail) return
    const mark = () => {
      const crumbs = Array.from(trail.querySelectorAll<HTMLElement>('.MuiBreadcrumbs-li'))
      crumbs.forEach(li => { li.removeAttribute('data-line-start'); li.removeAttribute('data-line-end') })
      // At most one new mark per crumb, so the loop ends.
      for (let pass = 0; pass < crumbs.length; pass++) {
        const boxes = crumbs.map(li => li.getBoundingClientRect())
        let changed = false
        crumbs.forEach((li, i) => {
          if (i === 0 || li.hasAttribute('data-line-start')) return
          if (boxes[i].top <= boxes[i - 1].top || boxes[i].top < boxes[i - 1].bottom - 1) return
          li.setAttribute('data-line-start', '')
          crumbs[i - 1].setAttribute('data-line-end', '')
          changed = true
        })
        if (!changed) break
      }
    }
    mark()
    if (typeof ResizeObserver === 'undefined') return
    // Next frame: marking resizes the trail it observes, which in the callback itself is a ResizeObserver loop error.
    let frame = 0
    const observer = new ResizeObserver(() => { cancelAnimationFrame(frame); frame = requestAnimationFrame(mark) })
    observer.observe(trail)
    return () => { observer.disconnect(); cancelAnimationFrame(frame) }
  }, [trailRef, crumbCount, oneLine])
}

export default function Breadcrumb({
  items,
  currentComponent = 'span',
  oneLine = false,
  endsAtParent = false,
  navRef,
}: {
  items: BreadcrumbItem[]
  oneLine?: boolean
  /** The trail ends at the PARENT (the page's title names the page itself): every crumb, the last included, is a
   *  link and none is the current page. */
  endsAtParent?: boolean
  /** The trail's `<nav>`, for a caller that measures its crumbs (`TaskBreadcrumb`'s ellipsis snap). */
  navRef?: Ref<HTMLElement>
  /* The current crumb's element. `'span'` except in the task editor, where the trail replaced the page
     `<h1>` and must carry it (a heading landmark; `getByRole('heading', { level: 1 })` in three suites).
     Styled as a crumb either way. */
  currentComponent?: ElementType
}) {
  const trailRef = useRef<HTMLElement | null>(null)
  const setTrail = useCallback((node: HTMLElement | null) => {
    trailRef.current = node
    if (typeof navRef === 'function') navRef(node)
    else if (navRef) (navRef as { current: HTMLElement | null }).current = node
  }, [navRef])
  useLineBreaks(trailRef, items.length, oneLine)
  return (
    <Breadcrumbs
      ref={setTrail}
      data-testid="breadcrumb"
      aria-label="Breadcrumb"
      // The app's own separator glyph, not MUI's `/`.
      separator={<span>›</span>}
      sx={{
        fontSize: '0.8125rem',
        /* THE SEPARATOR SITS BETWEEN CRUMBS, NEVER AT A LINE'S START: MUI's separator is its own
           flex item, free to wrap alone, so it is hidden (it is `aria-hidden` already) and each later crumb draws it
           as a `::before`. A crumb that wrapped to a new line (`useLineBreaks`) hands its "›" to the crumb ending the
           line above, so a phone reads "acme › No feature ›" over "35: Answer…", never "› 35: Answer…". The last
           crumb's floor (below) is what keeps the title on the separator's line in the first place. */
        '& .MuiBreadcrumbs-separator': { display: 'none' },
        '& .MuiBreadcrumbs-li:not(:first-of-type)::before': SEPARATOR_SX,
        '& .MuiBreadcrumbs-li[data-line-start]::before': { content: 'none' },
        '& .MuiBreadcrumbs-li[data-line-end]::after': SEPARATOR_SX,
        // WRAP, NEVER TRUNCATE: also wrap WITHIN a crumb — a task title can be 100 chars and is the page's
        // only identifier on a phone. `anywhere` breaks at spaces first. A flex row, so the `::before`
        // separator sits beside its crumb rather than above it.
        '& .MuiBreadcrumbs-li': {
          display: 'flex',
          alignItems: 'flex-start',
          minWidth: 0,
          whiteSpace: 'normal',
          overflowWrap: 'anywhere',
          '& > *': { minWidth: 0 },
        },
        /* THE LAST CRUMB STAYS ON THE SEPARATOR'S LINE whenever `LAST_FLOOR` fits there: it starts after "›" and
           wraps within itself, rather than a whole 100-char title (its basis were `auto`) dropping below the trail.
           Short of the floor the crumb moves down, its "›" with it. Growing to the row's end is invisible (the
           text stays left). */
        '& .MuiBreadcrumbs-li:last-of-type': { flex: `1 1 ${LAST_FLOOR}` },
        /* A LINKED LAST CRUMB (`endsAtParent`) that wraps fills its item, and its hit box would bleed 8px past the trail's
           edge (a scrollbar): capped at the item's width less the padding. Only the last: its width is the trail's, so
           the percentage resolves; on a shrink-to-fit earlier crumb it would wrap a short name mid-word. */
        '& .MuiBreadcrumbs-li:last-of-type > a': { maxWidth: 'calc(100% - 16px)' },
        /* A crumb holding an INPUT takes real width (else a 108-char title edited in 170px, six lines).
           Gated on `:has(input, textarea)`: ungated, the read-only title wrapped in a ~250px column.
           `20rem` exceeds a phone's leftover (~264px at 390px) and is below a desktop's (~880px), so the
           editor wraps to its own line on phones and grows in place on desktop — no media query. */
        '& .MuiBreadcrumbs-li:last-of-type:has(input, textarea)': { flex: '1 1 20rem' },
        ...(oneLine ? ONE_LINE_SX : null),
      }}
    >
      {items.map((item, i) => {
        const isLast = !endsAtParent && i === items.length - 1
        const text = typeof item.label === 'string' ? item.label : item.text
        const whole = !isLast && text !== undefined && text.length <= WHOLE_CHARS ? { 'data-crumb-whole': '' } : null
        if (!isLast && !item.to && item.onClick) {
          return (
            <Link
              key={i}
              component="button"
              type="button"
              onClick={item.onClick}
              aria-label={item.ariaLabel}
              {...whole}
              underline="hover"
              color="inherit"
              // A `<button>` does not inherit the surrounding font as an `<a>` does (UA 13.33px otherwise).
              sx={{ ...CRUMB_HIT_SX, font: 'inherit', color: 'inherit', cursor: 'pointer' }}
            >
              {item.label}
            </Link>
          )
        }
        return item.to && !isLast ? (
          <Link
            key={i}
            component={RouterLink}
            to={item.to}
            {...whole}
            underline="hover"
            color="inherit"
            // AC11's 44px tap floor without AC5's chrome-height cost: `inline-block` (inline boxes fragment
            // and misreport `getBoundingClientRect`) with padding cancelled by an equal negative margin, so
            // the border box clears 44 while the layout box keeps the measured 32.89x18. The
            // hit box bleeds a few px into the page title below (no control there today).
            sx={CRUMB_HIT_SX}
          >
            {item.label}
          </Link>
        ) : (
          <Typography
            key={i}
            component={isLast ? currentComponent : 'span'}
            variant="inherit"
            // A heading brings UA margins; `variant="inherit"` covers only size, so normalise explicitly.
            sx={isLast ? { m: 0, fontSize: 'inherit', fontWeight: 'inherit' } : undefined}
            aria-current={isLast ? 'page' : undefined}
            {...whole}
          >
            {item.label}
          </Typography>
        )
      })}
    </Breadcrumbs>
  )
}
