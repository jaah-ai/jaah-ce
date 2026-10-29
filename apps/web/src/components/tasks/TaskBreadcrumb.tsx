import { useLayoutEffect, useRef, useState, type KeyboardEvent, type MouseEvent, type ReactNode } from 'react'
import Box from '@mui/material/Box'
import InputBase from '@mui/material/InputBase'
import Menu from '@mui/material/Menu'
import Skeleton from '@mui/material/Skeleton'
import MenuItem from '@mui/material/MenuItem'
import Breadcrumb, { type BreadcrumbItem } from '../Breadcrumb'
import ProjectMenu, { type ProjectMenuRow } from './ProjectMenu'
import type { FeatureSummary } from '../../types'
import { parseFeatureIcon } from '../../lib/featureIcon'
import FeatureIcon from './FeatureIcon'
import { deescapeMarkdownText, storedTitleAsShown } from './shared'

// `PHONE` is imported from `ui/listStyles.ts`, its single home. The `16px` font floor below is the iOS
// auto-zoom floor (Safari zooms the editor on focus under it).
import { ABOVE_PHONE, PHONE, TASK_TRAIL_CONTAINER } from '../../ui/listStyles'
import { SystemChip } from '../../ui/MarkChip'

// `TaskCreate` carries NO `title` (`create_task` derives it from the body), so on `/tasks/new` this crumb
// names the surface rather than offering an edit the API would discard.
export const NEW_TASK_CRUMB = 'New task'

// An unfiled task's feature crumb and menu row: a plain label, not the board's project-named group.
const NO_FEATURE_LABEL = 'No feature'

/* The task editor's breadcrumb `{Project} › [{Feature}] › {id}: {Title}` — the
 * task's full path (AC-8.2, AC-1.1). It REPLACES the page `<h1>`, so it is the only thing saying which task
 * is open: it wraps rather than truncates.
 * · Project — the task's project; its menu lists every project (`ProjectMenu`). On `/tasks/new` it picks where
 *   the task is CREATED; on an existing task it stages a MOVE saved with the form (`project_id` on
 *   `TaskUpdate`). A project the host refuses (`pickRefusal`) is listed but unselectable (hiding it would
 *   hide why).
 * · Feature — "No feature" or one of the staged project's active features, a STAGED pick like the
 *   project (`feature_id` on the same PUT); plain text where not editable. Absent on `/tasks/new` while
 *   no feature is staged: a create from project scope is a project-level task (`acme › New task`). The host
 *   keeps project and feature in agreement.
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
  /** The staged feature, `null` for none ("Other"). */
  featureId?: string | null
  /** Its name, where the host could resolve one (a shipped or unlisted feature still names itself). */
  featureLabel?: string
  /** What the feature picker offers: the staged project's active features the caller may file into. */
  features?: FeatureSummary[]
  onFeatureChange?: (feature: FeatureSummary | null) => void
  /** False for a reader, a stage task (it moves with its epic) and while a save is in flight. */
  featureEditable?: boolean
  /** The staged feature's icon, drawn before the feature crumb and the title; null draws nothing. */
  featureIcon?: string | null
}) {
  const [menuAnchor, setMenuAnchor] = useState<HTMLElement | null>(null)
  const [featureAnchor, setFeatureAnchor] = useState<HTMLElement | null>(null)
  // `null` = not editing, distinct from editing an empty title.
  const [draft, setDraft] = useState<string | null>(null)
  // Set by Escape so the blur it causes does not commit the rejected value.
  const escapedRef = useRef(false)
  const inputRef = useRef<HTMLInputElement>(null)
  const trailRef = useRef<HTMLElement | null>(null)

  /* `'Project'` is the unresolved fallback once the roster has landed without this project; before then, a skeleton —
     unless the host already knows its name (`projectLabel`). */
  const project = projects.find(p => p.id === projectId)
  const projectName = project?.name ?? projectLabel ?? 'Project'
  const shownFeature = featureId ? (featureLabel ?? 'Feature') : NO_FEATURE_LABEL

  function commit() {
    if (escapedRef.current) {
      escapedRef.current = false
      setDraft(null)
      return
    }
    /* A blanked title on an existing task reverts: `submit()` omits an empty `title` so the server keeps the
     * stored one (AC2.2), and the crumb must show what the save will do. On create a blank is carried. */
    /* An unedited field is no change: it opened on the de-escaped title, and committing that would rewrite the
     * stored one. */
    const unchanged = draft === deescapeMarkdownText(title)
    if (draft !== null && !unchanged && !(taskId !== undefined && draft.trim() === '')) onTitleChange(draft)
    setDraft(null)
  }

  function onTitleKeyDown(e: KeyboardEvent<HTMLInputElement>) {
    if (e.key === 'Enter') {
      e.preventDefault()
      // One commit path: blur. Committing on Enter as well would run twice, the second with a stale `draft`.
      inputRef.current?.blur()
    } else if (e.key === 'Escape') {
      e.preventDefault()
      escapedRef.current = true
      inputRef.current?.blur()
    }
  }

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

  const currentCrumb = draft !== null ? (
    <Box sx={{ display: 'flex', alignItems: 'baseline', gap: 0, minWidth: 0 }}>
      {parseFeatureIcon(titleIcon) && (
        <Box component="span" sx={{ flex: '0 0 auto' }}><FeatureIcon icon={titleIcon} inline /></Box>
      )}
      {idPrefix && <Box component="span" sx={{ flex: '0 0 auto' }}>{idPrefix}</Box>}
      <InputBase
        inputRef={inputRef}
        value={draft}
        autoFocus
        // Multiline + `fullWidth`: titles run 100+ chars, and without `fullWidth` (and `Breadcrumb`'s
        // `:last-of-type` grow rule) MUI sizes the input to ~180px. No select-all on open: the first keystroke
        // would destroy the title.
        multiline
        fullWidth
        onChange={e => setDraft(e.target.value)}
        onBlur={commit}
        onKeyDown={onTitleKeyDown}
        inputProps={{ 'aria-label': 'Task title' }}
        data-testid="task-crumb-title-input"
        sx={theme => ({
          flex: '1 1 auto',
          minWidth: 0,
          font: 'inherit',
          px: '4px',
          border: 1,
          borderColor: 'primary.main',
          borderRadius: 1,
          // `InputBase` has no focus ring; thicken the border, with an inset so the baseline does not shift.
          '&.Mui-focused': { borderWidth: 2, px: '3px' },
          // `theme.custom.tapTargetPx` is the touch floor's single home; 16px is the iOS auto-zoom floor.
          [PHONE]: { fontSize: '16px', minHeight: theme.custom.tapTargetPx },
        })}
      />
    </Box>
  ) : (
    <Box
      component={titleEditable ? 'button' : 'span'}
      type={titleEditable ? 'button' : undefined}
      onClick={titleEditable ? () => setDraft(deescapeMarkdownText(title)) : undefined}
      data-testid="task-crumb-title"
      aria-label={titleEditable ? `Edit title: ${shownTitle}` : undefined}
      // The whole title on hover: above the phone the one-line trail ellipsises it (`Breadcrumb`'s `oneLine`).
      title={`${idPrefix}${shownTitle}`}
      sx={theme => ({
        font: 'inherit',
        color: 'inherit',
        textAlign: 'left',
        p: 0,
        m: 0,
        border: 0,
        background: 'none',
        /* A `<button>` is an atomic inline box: the `<h1>`'s ellipsis would clip it mid-glyph, so the box itself
           ellipsises on the one-line trail. */
        [ABOVE_PHONE]: { display: 'block', maxWidth: '100%', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' },
        ...(titleEditable
          ? {
              cursor: 'text',
              // Dotted underline on hover only: the crumb is the page's identifier first.
              '&:hover': { textDecoration: 'underline dotted' },
              /* The 44px tap floor as padding cancelled by an equal negative margin (`Breadcrumb`'s crumb hit
                 box): a `minHeight` would centre a one-line title 12px below the `›` that leads it. */
              [PHONE]: { py: `${(theme.custom.tapTargetPx - 18) / 2}px`, my: `${(18 - theme.custom.tapTargetPx) / 2}px` },
            }
          : {}),
      })}
    >
      <FeatureIcon icon={titleIcon} inline />
      {idPrefix}
      {shownTitle}
    </Box>
  )

  // Keyed on what changes a crumb's width or makes it a picker at all; a resize of the trail re-runs it too.
  useSnappedEllipsis(trailRef, [projectName, shownFeature, projectEditable, featureEditable, featureIcon, taskId].join('\u0000'))

  const items: BreadcrumbItem[] = [
    !project && projectLabel === undefined && projectPending
      ? { label: <Skeleton data-testid="task-crumb-project-pending" role="status" aria-label="Loading project" variant="text" sx={PENDING_CRUMB_SX} /> }
      : pickerCrumb(projectName, projectEditable, 'Project', 'project', setMenuAnchor, null, project?.system_kind ? <SystemChip /> : null, projectTo, pickerPending),
    /* A filed task names its feature; an unfiled one names none: "No feature" was a crumb that said
       nothing. Filing an unfiled task stays on the page's "Move to feature…" action. */
    ...(featureId
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
          sx={FEATURE_ITEM_SX}
        >
          {NO_FEATURE_LABEL}
        </MenuItem>
        {features.map(f => (
          <MenuItem
            key={f.id}
            selected={f.id === featureId}
            onClick={() => { setFeatureAnchor(null); if (f.id !== featureId) onFeatureChange?.(f) }}
            sx={FEATURE_ITEM_SX}
          >
            {f.name}
          </MenuItem>
        ))}
      </Menu>
    </>
  )
}

const FEATURE_ITEM_SX = {
  whiteSpace: 'normal',
  flexWrap: 'wrap',
  gap: 0.75,
  [PHONE]: { minHeight: 44 },
} as const

// A typical project name's width, so the trail does not jump when the name lands.
const PENDING_CRUMB_SX = { display: 'inline-block', width: '4rem', flex: '0 0 auto' } as const

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
  [ABOVE_PHONE]: { minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' },
} as const
/* The mark after the name (the System chip) gives way FIRST: its shrink weight dwarfs the name's, so it
   narrows before the name loses a glyph — a cramped "c…" beside a whole chip told the reader less. The chip's own
   label ellipsises ("Sy…"), and below `ASIDE_FLOOR` (a letter and the ellipsis inside the chip's padding) the chip
   goes OUTRIGHT rather than leaving a border sliver, two ways:
   · on a trail narrower than `ASIDE_TRAIL_MIN` it is not drawn — measured with a short name ("acme", "No feature")
     the chip reaches its floor there, so the 1024px split's ~260px trail shows "acme ▾" with no blank where it was.
     A query on the trail's width (its header lead) (`ui/listStyles.ts`'s `TASK_TRAIL_CONTAINER`) cannot feed back on what it hides;
   · with a longer name it reaches the floor on a wider trail: the box is a one-line wrapping row led by a
     zero-width `::before`, so a chip whose floor no longer fits beside it wraps to a second line, which the box's
     fixed height and `overflow` cut away (the box keeps its leftover, at most the floor). 24px is MUI's small Chip. */
const ASIDE_TRAIL_MIN = 290
const ASIDE_FLOOR = '2rem'
const PICKER_ASIDE_SX = {
  display: 'inline-flex',
  [ABOVE_PHONE]: {
    flex: '0 100000 auto',
    minWidth: 0,
    overflow: 'hidden',
    flexWrap: 'wrap',
    height: 24,
    '&::before': { content: '""', height: '100%' },
    '& > .MuiChip-root': { flex: `1 1 ${ASIDE_FLOOR}`, minWidth: 0, maxWidth: 'max-content' },
    [`@container ${TASK_TRAIL_CONTAINER} (max-width:${ASIDE_TRAIL_MIN - 0.05}px)`]: { display: 'none' },
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
    ctx.font = style.font
    ctx.letterSpacing = style.letterSpacing === 'normal' ? '0px' : style.letterSpacing
    return ctx.measureText('…').width
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
const CARET_SX = { flex: '0 0 auto', fontSize: '0.7em', lineHeight: 1, ml: '4px', mr: '2px' } as const

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
    <Box component="span" data-testid={`task-crumb-${slug}`} title={name} sx={PICKER_CRUMB_SX}>
      {/* No flex gap: the inline icon carries its own, so both crumb forms space it alike. */}
      {lead}
      <Box component="span" data-testid={`task-crumb-${slug}-name`} data-snap-ellipsis sx={PICKER_NAME_SX}>{name}</Box>
      {aside && <Box component="span" data-testid={`task-crumb-${slug}-aside`} sx={PICKER_ASIDE_SX}>{aside}</Box>}
      {caret}
    </Box>
  )
  if (!editable && pickerPending && !to) {
    return {
      label: pickerBox(
        <Box component="span" aria-hidden data-testid={`task-crumb-${slug}-caret-reserved`} sx={{ ...CARET_SX, visibility: 'hidden' }}>▾</Box>,
      ),
      text: name,
    }
  }
  if (!editable) return { label: <span data-testid={`task-crumb-${slug}`} title={name}>{lead}{name}{aside}</span>, to, text: name }
  return {
    label: pickerBox(<Box component="span" aria-hidden data-testid={`task-crumb-${slug}-caret`} sx={CARET_SX}>▾</Box>),
    ariaLabel: `${noun}: ${name} — change ${noun.toLowerCase()}`,
    text: name,
    onClick: (e: MouseEvent<HTMLElement>) => open(e.currentTarget),
  }
}
