import { useLayoutEffect, useRef, useState, type MouseEvent, type ReactNode } from 'react'
import Box from '@mui/material/Box'
import Menu from '@mui/material/Menu'
import Skeleton from '@mui/material/Skeleton'
import MenuItem from '@mui/material/MenuItem'
import Breadcrumb, { type BreadcrumbItem } from '../Breadcrumb'
import ProjectMenu, { MENU_ITEM_SX, type ProjectMenuRow } from './ProjectMenu'
import type { FeatureSummary } from '../../types'
import { parseFeatureIcon } from '../../lib/featureIcon'
import FeatureIcon from './FeatureIcon'
import { deescapeMarkdownText, storedTitleAsShown } from './shared'
import { projectRootLabel } from './featureFiling'

import { ABOVE_PHONE, TRAIL_NO_ASIDE, ELLIPSIS_SX } from '../../ui/listStyles'
import { FONT_SIZE, LINE_HEIGHT, grid } from '../../theme'
import { SystemChip } from '../../ui/MarkChip'
import Tip from '../../ui/Tip'
import InlineTitleEdit from '../../ui/InlineTitleEdit'

// `TaskCreate` carries NO `title` (`create_task` derives it from the body), so on `/tasks/new` this crumb
// names the surface rather than offering an edit the API would discard.
export const NEW_TASK_CRUMB = 'New task'

/* The task editor's breadcrumb `{Project} › [{Feature}] › {id}: {Title}` — the
 * task's full path (AC-8.2, AC-1.1). It REPLACES the page `<h1>`, so it is the only thing saying which task
 * is open: it wraps rather than truncates.
 * · Project — the task's project; its menu lists every project (`ProjectMenu`). On `/tasks/new` it picks where
 *   the task is CREATED; on an existing task it stages a MOVE saved with the form (`project_id` on
 *   `TaskUpdate`). A project the host refuses (`pickRefusal`) is listed but unselectable (hiding it would
 *   hide why).
 * · Feature — the project's root ("acme/", the task in its project alone) or one of the staged project's
 *   features, a STAGED pick like the project (`feature_id` on the same PUT); plain text where not editable. Its
 *   menu also ends in "New feature…" (`onNewFeature`). A task in its project alone draws it only where it is a
 *   picker, the one place filing lives; absent for a reader and on `/tasks/new` while nothing is staged: a
 *   create from project scope is a project-level task (`acme › New task`). The host keeps project and feature in agreement.
 * · `{id}: {title}` — bare id; the title is click-to-edit, committing to the FORM's draft on blur (only
 *   Save persists; Escape reverts).
 * Editability is gated by `*Editable` props, not a permission hook, so tests need no permission mock. */
export default function TaskBreadcrumb({
  projects,
  projectId,
  onProjectChange,
  projectEditable,
  projectTo,
  projectPending = false,
  projectLabel,
  pickerPending = false,
  pickRefusal,
  taskId,
  title,
  onTitleChange,
  titleEditable,
  featureId = null,
  featureLabel,
  features = [],
  onFeatureChange,
  onNewFeature,
  featureEditable = false,
  featureIcon = null,
}: {
  projects: ReadonlyArray<ProjectMenuRow>
  /** `null` until the host resolves one (the project list has not landed). */
  projectId: number | null
  onProjectChange: (id: number) => void
  /** False for a principal who may read but not write, and while a save is in flight. */
  projectEditable: boolean
  /** Where a NON-editable project crumb links: the Live Cast viewer's project task list. Absent: plain text. */
  projectTo?: string
  /** The roster is still loading: an unresolved project draws a fixed-width skeleton, not a placeholder name that
   *  the real one replaces a moment later, shifting the one-line trail. */
  projectPending?: boolean
  /** The project's name where the host knows it without the roster (`GET …/page`'s `trail`): an
   *  unresolved project names itself at once instead of a skeleton or the "Project" placeholder. */
  projectLabel?: string
  /** The crumbs are not pickers YET (the page is still loading) but will be for this viewer: each reserves its ▾'s
   *  width unseen, so the trail does not shift when the picker arrives. */
  pickerPending?: boolean
  /** Forwarded to `ProjectMenu` untouched (its prop note is the single home); only the page knows whether
   *  the crumb is a create target or a move destination. */
  pickRefusal?: (project: ProjectMenuRow) => string | null
  /** Absent on `/tasks/new`. */
  taskId?: number
  title: string
  onTitleChange: (next: string) => void
  titleEditable: boolean
  /** The staged feature, `null` for the project itself. */
  featureId?: string | null
  /** Its name, where the host could resolve one (a deleted or unlisted feature still names itself). */
  featureLabel?: string
  /** What the feature picker offers: the staged project's features the caller may file into. */
  features?: FeatureSummary[]
  onFeatureChange?: (feature: FeatureSummary | null) => void
  /** The feature menu's last item, "New feature…": create one in the task's project, then file there. Absent: not
   *  offered (the caller may not create there). */
  onNewFeature?: () => void
  /** False for a reader, a stage task (it moves with its epic) and while a save is in flight. */
  featureEditable?: boolean
  /** The staged feature's icon, drawn before the feature crumb and the title; null draws nothing. */
  featureIcon?: string | null
}) {
  const [menuAnchor, setMenuAnchor] = useState<HTMLElement | null>(null)
  const [featureAnchor, setFeatureAnchor] = useState<HTMLElement | null>(null)
  const trailRef = useRef<HTMLElement | null>(null)

  /* `'Project'` is the unresolved fallback once the roster has landed without this project; before then, a skeleton —
     unless the host already knows its name (`projectLabel`). */
  const project = projects.find(p => p.id === projectId)
  const projectName = project?.name ?? projectLabel ?? 'Project'
  const shownFeature = featureId ? (featureLabel ?? 'Feature') : projectRootLabel(projectName)

  const idPrefix = taskId === undefined ? '' : `${taskId}: `
  // The icon belongs to a filed task only, so it follows the feature crumb's own condition.
  const titleIcon = featureId ? featureIcon : null
  /* The placeholder is for `/tasks/new` only; an existing task with an empty stored title reads
   * "(untitled)". Shown de-escaped (`\[ops\]` reads `[ops]`), in the edit field too; an unedited
   * blur commits nothing (`commit`). An epic child's stored `[<persona>] ` prefix reads as the board row reads it
   * (`storedTitleAsShown`); the edit field still opens on the stored title. */
  const shownTitle = title.trim() !== ''
    ? storedTitleAsShown(title)
    : taskId === undefined ? NEW_TASK_CRUMB : '(untitled)'

  /* The title, click-to-edit (`ui/InlineTitleEdit`): it commits to the FORM's draft (only Save persists). A blanked
   * title on an existing task reverts: `submit()` omits an empty `title` so the server keeps the stored one (AC2.2),
   * and the crumb must show what the save will do. On create a blank is carried. */
  const currentCrumb = (
    <InlineTitleEdit
      value={deescapeMarkdownText(title)}
      shown={shownTitle}
      onCommit={onTitleChange}
      editable={titleEditable}
      blank={taskId === undefined ? 'commit' : 'revert'}
      lead={(parseFeatureIcon(titleIcon) || idPrefix) ? <><FeatureIcon icon={titleIcon} inline />{idPrefix}</> : undefined}
      tip={`${idPrefix}${shownTitle}`}
      inputLabel="Task title"
      editLabel={`Edit title: ${shownTitle}`}
      testId="task-crumb-title"
    />
  )

  // Keyed on what changes a crumb's width or makes it a picker at all; a resize of the trail re-runs it too.
  useSnappedEllipsis(trailRef, [projectName, shownFeature, projectEditable, featureEditable, featureIcon, taskId].join('\u0000'))

  const items: BreadcrumbItem[] = [
    !project && projectLabel === undefined && projectPending
      ? { label: <Skeleton data-testid="task-crumb-project-pending" role="status" aria-label="Loading project" variant="text" sx={PENDING_CRUMB_SX} /> }
      : pickerCrumb(projectName, projectEditable, 'Project', 'project', setMenuAnchor, null, project?.system_kind ? <SystemChip /> : null, projectTo, pickerPending),
    /* A task in a feature names it. One in its project alone names none to a reader, but an editor's is the
       project-root picker that files it: Move to feature lives here now. */
    ...(featureId || (featureEditable && taskId !== undefined)
      ? [pickerCrumb(shownFeature, featureEditable, 'Feature', 'feature', setFeatureAnchor, featureIcon, null, undefined, pickerPending)]
      : []),
    { label: currentCrumb },
  ]

  return (
    <>
      {/* `currentComponent="h1"` is load-bearing: this trail replaced the page `<h1>`, and suites resolve the
      open task by `getByRole('heading', { level: 1 })`. */}
      <Breadcrumb items={items} currentComponent="h1" oneLine navRef={trailRef} />
      {/* The picker (`ProjectMenu`). A create/move pick: no "all projects" item, so `onPick`'s `null` arm is
      unreachable. An absent `pickRefusal` accepts every row. */}
      <ProjectMenu
        anchorEl={menuAnchor}
        onClose={() => setMenuAnchor(null)}
        projects={projects}
        projectId={projectId}
        onPick={id => { if (id !== null) onProjectChange(id) }}
        pickRefusal={pickRefusal}
        testId="task-crumb-project-menu"
        ariaLabel="All projects"
      />
      <Menu
        anchorEl={featureAnchor}
        open={featureAnchor !== null}
        onClose={() => setFeatureAnchor(null)}
        slotProps={{ list: { 'aria-label': 'Feature' } }}
        data-testid="task-crumb-feature-menu"
      >
        <MenuItem
          selected={!featureId}
          onClick={() => { setFeatureAnchor(null); if (featureId) onFeatureChange?.(null) }}
          sx={MENU_ITEM_SX}
        >
          {projectRootLabel(projectName)}
        </MenuItem>
        {features.map(f => (
          <MenuItem
            key={f.id}
            selected={f.id === featureId}
            onClick={() => { setFeatureAnchor(null); if (f.id !== featureId) onFeatureChange?.(f) }}
            sx={MENU_ITEM_SX}
          >
            {f.name}
          </MenuItem>
        ))}
        {onNewFeature && (
          <MenuItem
            onClick={() => { setFeatureAnchor(null); onNewFeature() }}
            data-testid="task-crumb-new-feature"
            sx={MENU_ITEM_SX}
          >
            New feature…
          </MenuItem>
        )}
      </Menu>
    </>
  )
}

// A typical project name's width, so the trail does not jump when the name lands.
const PENDING_CRUMB_SX = { display: 'inline-block', width: grid(10.5), flex: '0 0 auto' } as const

/* THE PICKER CRUMB ON THE ONE-LINE TRAIL: the crumb is an atomic inline-flex box, so the trail's own
   ellipsis clipped it mid-glyph and took the ▾ with it. Above the phone the box caps at its crumb's width and the
   NAME alone ellipsises; icon, chip and caret keep their size. The phone wraps, never truncates (`Breadcrumb`). */
const PICKER_CRUMB_SX = {
  display: 'inline-flex',
  alignItems: 'center',
  /* `top`: a box whose content clips sits on its bottom margin edge, not its text baseline, and would ride high. */
  [ABOVE_PHONE]: { maxWidth: '100%', verticalAlign: 'top' },
} as const
const PICKER_NAME_SX = {
  [ABOVE_PHONE]: ELLIPSIS_SX,
} as const
/* The mark after the name (the System chip, a `ui/StatusChip` pill that never shrinks its word) gives way FIRST:
   its box's shrink weight dwarfs the name's, so it narrows before the name loses a glyph — a cramped "c…" beside a
   whole chip told the reader less. The chip goes OUTRIGHT rather than leaving a border sliver, two ways:
   · on a trail narrower than 290px (`TRAIL_NO_ASIDE`) it is not drawn — the 1024px split's ~260px trail shows "acme ▾"
     with no blank where it was. A query on the trail's width (its header lead) (`ui/listStyles.ts`'s
     `TASK_TRAIL_CONTAINER`) cannot feed back on what it hides;
   · with a longer name the box is a one-line wrapping row led by a zero-width `::before`, so a chip that no longer
     fits beside it wraps to a second line, which the box's fixed height and `overflow` cut away. */
const PICKER_ASIDE_SX = {
  display: 'inline-flex',
  [ABOVE_PHONE]: {
    flex: '0 100000 auto',
    minWidth: 0,
    overflow: 'hidden',
    flexWrap: 'wrap',
    height: grid(4),
    '&::before': { content: '""', height: '100%' },
    [TRAIL_NO_ASIDE]: { display: 'none' },
  },
} as const

/* THE ELLIPSIS SITS AGAINST THE ▾: `text-overflow` drops whole glyphs, so an ellipsised name's box kept up
   to a glyph of blank before its caret ("No fea…  ▾"), and CSS cannot size a box to its ellipsis. So each pass caps
   the crumb at the width its drawn text needs, handing the rest to the title (whose shrink weight takes it), and runs
   again whenever the trail resizes or a crumb changes. The cap only ever narrows a crumb that already ellipsises; the
   phone's wrapping trail never does. Each pass first lifts every cap: MUI reuses an `<li>` by index, so a feature
   crumb's cap would otherwise land on the title once the trail drops a crumb. */
function useSnappedEllipsis(trailRef: { current: HTMLElement | null }, crumbs: string) {
  useLayoutEffect(() => {
    const trail = trailRef.current
    if (!trail || typeof ResizeObserver === 'undefined') return
    const observer = new ResizeObserver(() => {
      trail.querySelectorAll<HTMLElement>('.MuiBreadcrumbs-li').forEach(li => { li.style.maxWidth = li.style.minWidth = '' })
      trail.querySelectorAll<HTMLElement>('[data-snap-ellipsis]').forEach(snapEllipsis)
    })
    observer.observe(trail)
    return () => observer.disconnect()
  }, [trailRef, crumbs])
}

let measureCanvas: HTMLCanvasElement | null = null

function snapEllipsis(name: HTMLElement) {
  const crumb = name.closest<HTMLElement>('.MuiBreadcrumbs-li')
  if (!crumb) return
  const text = name.firstChild
  if (!(text instanceof Text) || name.scrollWidth <= name.clientWidth) return
  const box = name.getBoundingClientRect().width
  const ellipsis = (() => {
    measureCanvas ??= document.createElement('canvas')
    const ctx = measureCanvas.getContext('2d')
    if (!ctx) return null
    const style = getComputedStyle(name)
    // save/restore: the shared canvas starts every measure at its default (zero) letter spacing.
    ctx.save()
    ctx.font = style.font
    if (style.letterSpacing !== 'normal') ctx.letterSpacing = style.letterSpacing
    const width = ctx.measureText('…').width
    ctx.restore()
    return width
  })()
  if (ellipsis === null) return
  // The widest prefix that fits beside the ellipsis: the one the browser draws.
  const range = document.createRange()
  const prefixWidth = (end: number) => { range.setStart(text, 0); range.setEnd(text, end); return range.getBoundingClientRect().width }
  let lo = 0
  let hi = text.length
  while (lo < hi) {
    const mid = Math.ceil((lo + hi) / 2)
    if (prefixWidth(mid) + ellipsis <= box) lo = mid
    else hi = mid - 1
  }
  const blank = box - prefixWidth(lo) - ellipsis
  if (blank <= 0.5) return
  // A quarter pixel of slack, so rounding never costs the drawn prefix its last glyph. `minWidth` too: the first
  // crumb's floor (`Breadcrumb`'s `ONE_LINE_FIRST_FLOOR`) would otherwise outrank the cap.
  const cap = `${crumb.getBoundingClientRect().width - blank + 0.25}px`
  crumb.style.maxWidth = crumb.style.minWidth = cap
}

/* The caret distinguishes a picker crumb from a navigating one before the press. The small glyph has almost no side
   bearing, so its margins stand in for a letter's: the gap to the name and to the `›` then match a plain crumb's. */
const CARET_SX = { flex: '0 0 auto', fontSize: FONT_SIZE.xxs, lineHeight: LINE_HEIGHT.tight, ml: 0.5, mr: 0 } as const

/** A crumb that is a picker when `editable` (a caret and a menu), otherwise a link to `to`, or plain text.
 *  `pickerPending`: not editable YET — drawn in the picker's own box with its caret's width held unseen. */
function pickerCrumb(
  name: string,
  editable: boolean,
  noun: string,
  slug: string,
  open: (anchor: HTMLElement) => void,
  icon: string | null = null,
  aside: ReactNode = null,
  to?: string,
  pickerPending = false,
): BreadcrumbItem {
  const lead = <FeatureIcon icon={icon} inline />
  // The picker's box and name, shared by the live picker and the one still pending, so their widths agree.
  const pickerBox = (caret: ReactNode) => (
    <Tip title={name}><Box component="span" data-testid={`task-crumb-${slug}`} sx={PICKER_CRUMB_SX}>
      {/* No flex gap: the inline icon carries its own, so both crumb forms space it alike. */}
      {lead}
      <Box component="span" data-testid={`task-crumb-${slug}-name`} data-snap-ellipsis sx={PICKER_NAME_SX}>{name}</Box>
      {aside && <Box component="span" data-testid={`task-crumb-${slug}-aside`} sx={PICKER_ASIDE_SX}>{aside}</Box>}
      {caret}
    </Box></Tip>
  )
  if (!editable && pickerPending && !to) {
    return {
      label: pickerBox(
        <Box component="span" aria-hidden data-testid={`task-crumb-${slug}-caret-reserved`} sx={{ ...CARET_SX, visibility: 'hidden' }}>▾</Box>,
      ),
      text: name,
    }
  }
  if (!editable) return { label: <Tip title={name}><span data-testid={`task-crumb-${slug}`}>{lead}{name}{aside}</span></Tip>, to, text: name }
  return {
    label: pickerBox(<Box component="span" aria-hidden data-testid={`task-crumb-${slug}-caret`} sx={CARET_SX}>▾</Box>),
    ariaLabel: `${noun}: ${name} — change ${noun.toLowerCase()}`,
    text: name,
    onClick: (e: MouseEvent<HTMLElement>) => open(e.currentTarget),
  }
}
