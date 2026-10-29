import Box from '@mui/material/Box'
import Stack from '@mui/material/Stack'
import Typography from '@mui/material/Typography'
import { Children, Fragment, isValidElement, type JSX, type ReactNode } from 'react'
import { createPortal } from 'react-dom'
import { useTabActionsSlot } from '../components/TabActionsSlot'
import { PHONE } from './listStyles'

/* The console's ONE page header.
 *
 * The whole header wraps (`flexWrap: 'wrap'`) and the title is `flex: 1 1 auto` with a `minWidth` floor, so
 * at 390px a wide action cluster takes its own line and the title always leads
 * (`page-header-mobile.spec.ts`). The title is `h6`: chrome height is a measured criterion (AC5).
 *
 * TWO SLOTS: `actions` — what you DO here, pinned to the far edge; `titleAside` — beside the title, part of
 * WHAT you are looking at. Load-bearing: anything in `actions` lands at the opposite end whatever the JSX
 * order.
 *
 * NO TITLE where a tab or the sidebar already names the page: a header then carries only `actions`, and
 * under a layout-drawn tab strip those portal onto the strip's right end (`components/TabActionsSlot`) and
 * the header draws nothing — no row of its own. No subtitle taglines: `subtitle` is for live facts (a version,
 * a source ref), never a sentence describing the page.
 *
 * STYLING HOOKS: a host that reshapes the header (`components/tasks/pane/PaneHeader.tsx`) selects these
 * `data-slot`s, never its DOM nesting or MUI class names — `page-header` (the row), `lead` (the row's leading
 * group: the heading column, or the trail and aside beside it), `title-row` (the title or trail with its aside
 * beside it; only with an aside), `heading` (the heading column inside a title-less `title-row`) and `actions`.
 */
export function PageHeader({
  title,
  titleAside,
  subtitle,
  breadcrumb,
  actions,
  testId,
}: {
  /* Optional: the task editor's breadcrumb carries the `<h1>`. Tested with `!= null` so `title={null}` never
     renders an empty (nameless) `<h1>`. */
  title?: ReactNode
  /** Drawn right of the title, left of `actions`, on the title's line — must fit one (avatars, a count, a
   *  scope chip). Absent, nothing changes. */
  titleAside?: ReactNode
  subtitle?: ReactNode
  /** A `<Breadcrumb>` trail, stacked ABOVE the title (never beside it). */
  breadcrumb?: ReactNode
  /** The header's action cluster — buttons, filters, a search box. */
  actions?: ReactNode
  testId?: string
}): JSX.Element | null {
  const tabSlot = useTabActionsSlot()
  if (tabSlot !== null && title == null && breadcrumb == null && subtitle == null && titleAside == null) {
    return actions && tabSlot ? createPortal(<span data-testid={testId} style={{ display: 'contents' }}>{actions}</span>, tabSlot) : null
  }
  /* A header with nothing to show takes no space. */
  if (title == null && breadcrumb == null && !subtitle && titleAside == null && !actions) return null

  const titleText = title != null && (
    <Typography variant="h6" component="h1" sx={{ fontWeight: 600, lineHeight: 1.25, minWidth: 0 }}>
      {title}
    </Typography>
  )
  /* WITH A TITLE the aside shares the title's own row, centred on it, and the breadcrumb sits above both: centred
     on the whole column it floated between the two lines. With none (the trail carries the `<h1>`)
     the aside sits beside the trail instead, costing no row. */
  const asideRow = (lead: ReactNode) => (
    <Box data-slot="title-row" sx={{ display: 'flex', flexWrap: 'wrap', alignItems: 'center', columnGap: 1.5, rowGap: 0.5, minWidth: 0 }}>
      {lead}
      {titleAside}
    </Box>
  )
  const titled = title != null && titleAside != null

  /* The heading column (breadcrumb, title, subtitle). Beside a title-less trail's aside it is `flex: '0 1 auto'`
     and the growth moves to the wrapper below (keeping `minWidth: 0` so long names ellipsize).
     ON THE PHONE that trail WRAPS, so its `auto` basis is the whole title on one line: wider than any phone row,
     it always pushed the aside (the org-scope chip, `OrgScopeInline`) to a line of its own. A `10rem` basis lets
     the aside share the trail's row where both fit; the trail then wraps beside it. */
  const asideBeside = titleAside != null && !titled
  /* ONE ACTION STAYS ON THE TITLE ROW AT 390px: the lead's `auto` basis is the whole title on one line, so even a
     lone small button wrapped to a line of its own. With exactly one action the lead takes a `10rem` basis on the
     phone (the aside's trick above) and the title wraps beside the button; a wide one still wraps. */
  const oneAction = actionCount(actions) === 1
  const leadPhone = oneAction ? { [PHONE]: { flex: '1 1 10rem' } } : null
  const titleColumn = (slot?: string) => (
    <Box
      data-slot={slot}
      sx={{
        flex: asideBeside ? '0 1 auto' : '1 1 auto',
        ...(asideBeside ? { [PHONE]: { flex: '1 1 10rem' } } : slot === 'lead' ? leadPhone : null),
        minWidth: 0,
        display: 'flex',
        flexDirection: 'column',
        gap: 0.25,
      }}
    >
      {breadcrumb}
      {titled ? asideRow(titleText) : titleText}
      {subtitle && (
        <Typography variant="body2" sx={{ color: 'text.secondary' }}>
          {subtitle}
        </Typography>
      )}
    </Box>
  )

  return (
    <Box
      component="header"
      data-testid={testId}
      data-slot="page-header"
      sx={{
        display: 'flex',
        flexWrap: 'wrap',
        alignItems: 'center',
        justifyContent: 'space-between',
        columnGap: 1.5,
        rowGap: 1,
        mb: 1.5,
      }}
    >
      {titleAside == null || titled ? titleColumn('lead') : (
        /* Trail + aside are ONE group taking the free space, so the gap opens after the aside. */
        <Box data-slot="lead" sx={{ flex: '1 1 auto', ...leadPhone, minWidth: 0 }}>{asideRow(titleColumn('heading'))}</Box>
      )}
      {actions && (
        <Stack
          data-slot="actions"
          data-one-action={oneAction || undefined}
          direction="row"
          spacing={1}
          useFlexGap
          sx={{ flexWrap: 'wrap', alignItems: 'center', justifyContent: 'flex-end' }}
        >
          {actions}
        </Stack>
      )}
    </Box>
  )
}

/* Fragments are the usual wrapper of an action cluster, so they count as what they hold. */
function actionCount(node: ReactNode): number {
  return Children.toArray(node).reduce<number>((n, child) => (
    n + (isValidElement<{ children?: ReactNode }>(child) && child.type === Fragment ? actionCount(child.props.children) : 1)
  ), 0)
}

export default PageHeader
