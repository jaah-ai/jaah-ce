import { type DragEvent, type FocusEvent, type KeyboardEvent, type MouseEvent, type ReactNode, useState } from 'react'
import Box from '@mui/material/Box'
import type { Theme } from '@mui/material/styles'
import { ABOVE_PHONE, schemed } from '../../../ui/listStyles'
import type { OrgRef, TaskTreeCounts } from '../../../types'
import { OrgQualifier, spokenProjectName } from '../../../lib/projectLabel'
import { stackedStopKinds, stoppedPhrase } from './railModel'
import FeatureIcon from '../FeatureIcon'
import { ProgressRing, progressPct } from '../../../ui/ProgressRing'
import { MoreHorizIcon } from '../../../ui/icons'

/* ONE RAIL ROW — a `treeitem`: disclosure (or a feature's progress ring), name (wrapped, never clipped: a project or feature is told apart by
 * the end of its name as often as the start; the full name also on `title`), the Stopped pill, and the
 * hover actions. Presentation only; `TaskTree.tsx` owns every decision.
 *
 * A row with an `href` carries a real link stretched over the whole row, so Cmd/Ctrl/middle-click and the
 * browser's "Open link in new tab" work anywhere on it; a plain click stays the row's own `onSelect`. */

/** The drag payload a pane task row carries: its task id. */
export const TASK_DRAG_TYPE = 'application/x-jaah-task'

/** The drag type of a rail feature being reordered. Its payload is never read: the tree holds who is dragging
 *  (a drag-over can see only the types). */
export const FEATURE_DRAG_TYPE = 'application/x-jaah-feature'

/** Where a reorder drop lands relative to the row under the pointer. */
export type DropEdge = 'before' | 'after'

/* 12px per level below the projects. All projects is the root and never folds, so its projects sit flush
   with it: that indent would cost every feature name 12px and buy nothing. */
const INDENT_PX = 12

export type TaskTreeRowProps = {
  id: string
  level: number
  label: string
  counts?: TaskTreeCounts
  /** A second line (a filter match's path). */
  subtitle?: string
  /** The label's matched range, highlighted. */
  highlight?: { start: number; length: number }
  expandable: boolean
  expanded: boolean
  selected: boolean
  /** The roving tab stop. */
  tabbable: boolean
  /** Phone rows: the 44px floor and an always-visible "+". */
  touch: boolean
  onSelect: () => void
  /** Where a plain click navigates, as a real link (new tab, copy address); absent draws none. */
  href?: string
  onToggle?: () => void
  onKeyDown: (e: KeyboardEvent<HTMLElement>) => void
  onFocus: () => void
  /** "+", or absent where the viewer may not create here. */
  onCreate?: (anchor: HTMLElement) => void
  createLabel?: string
  /** The row menu (Pin/Unpin), or absent. */
  onMenu?: (anchor: HTMLElement) => void
  /** This row's menu is open: its actions stay shown, so the menu's anchor keeps a box to sit under. */
  menuOpen?: boolean
  /** Accepts a dropped task. */
  onDropTask?: (taskId: number) => void
  /** A feature's done share, drawn as a ring left of the name; absent draws none (a feature with no tasks). */
  progress?: { done: number; total: number }
  /** The user muted this node (`lib/taskMute.ts`): its name greys. The caller withholds its pill and ring. */
  muted?: boolean
  /** A feature's icon, before the name; absent/null draws nothing and takes no space. */
  icon?: string | null
  /** A mark after the name (a built-in project's `ui/MarkChip::SystemChip`). */
  labelAside?: ReactNode
  /** The project's org, drawn and said after the name when its name collides (`lib/projectLabel.tsx`). */
  qualifier?: OrgRef | null
  /** Makes the row a reorder drag source. */
  reorder?: { onStart: () => void; onEnd: () => void }
  /** Accepts the feature being reordered, above or below this row. */
  onReorderDrop?: (edge: DropEdge) => void
  rowRef?: (el: HTMLElement | null) => void
  testId?: string
  nodeKey: string
  children?: ReactNode
}

/* Each layer behind the front pill peeks out this far to its right; the stack reserves that width. */
const PILL_PEEK_PX = 4

const PILL_LAYER_SX = {
  height: 16,
  minWidth: 16,
  borderRadius: '8px',
} as const

function Pill({ counts }: { counts: TaskTreeCounts }) {
  const [front, ...behind] = stackedStopKinds(counts)
  if (counts.stopped <= 0 || !front) return null
  const count = counts[front.field]
  return (
    <Box
      component="span"
      aria-hidden="true"
      data-testid="rail-stopped-pill"
      data-kind={front.field}
      title={stoppedPhrase(counts)}
      sx={{ flex: '0 0 auto', position: 'relative', display: 'inline-flex', isolation: 'isolate', mr: `${behind.length * PILL_PEEK_PX}px` }}
    >
      {/* Deepest first, so each later layer paints over the one behind it. */}
      {behind
        .map((kind, i) => (
          <Box
            key={kind.field}
            component="span"
            data-testid="rail-stopped-pill-behind"
            data-kind={kind.field}
            sx={{ ...PILL_LAYER_SX, position: 'absolute', inset: 0, transform: `translateX(${(i + 1) * PILL_PEEK_PX}px)`, backgroundColor: kind.bg }}
          />
        ))
        .reverse()}
      <Box
        component="span"
        sx={{
          ...PILL_LAYER_SX,
          position: 'relative',
          display: 'inline-flex',
          alignItems: 'center',
          justifyContent: 'center',
          padding: '0 5px',
          fontSize: '0.625rem',
          fontWeight: 700,
          lineHeight: '16px',
          color: front.fg,
          backgroundColor: front.bg,
        }}
      >
        {count > 99 ? '99+' : count}
      </Box>
    </Box>
  )
}

function Label({ label, highlight }: { label: string; highlight?: TaskTreeRowProps['highlight'] }) {
  if (!highlight || highlight.length === 0) return <>{label}</>
  const end = highlight.start + highlight.length
  return (
    <>
      {label.slice(0, highlight.start)}
      <Box component="mark" sx={{ backgroundColor: 'warning.light', color: 'inherit', borderRadius: '2px' }}>
        {label.slice(highlight.start, end)}
      </Box>
      {label.slice(end)}
    </>
  )
}

const actionSx = (t: Theme) => ({
  flex: '0 0 auto',
  display: 'inline-flex',
  alignItems: 'center',
  justifyContent: 'center',
  width: 22,
  height: 22,
  padding: 0,
  border: 0,
  borderRadius: '4px',
  background: 'none',
  font: 'inherit',
  fontSize: '0.9375rem',
  lineHeight: 1,
  cursor: 'pointer',
  color: 'inherit',
  '&:hover': schemed(t, c => ({ backgroundColor: c.overlayHover, color: c.textStrong })),
})

const touchActionSx = (t: Theme) => ({ ...actionSx(t), width: t.custom.tapTargetPx, height: t.custom.tapTargetPx })

export default function TaskTreeRow(props: TaskTreeRowProps) {
  const {
    id, level, label, counts, subtitle, highlight, expandable, expanded, selected, tabbable, touch, onSelect, href, onToggle, icon,
    onKeyDown, onFocus, onCreate, createLabel, onMenu, menuOpen = false, onDropTask, progress, reorder, onReorderDrop, rowRef, testId,
    nodeKey, children, muted = false, labelAside, qualifier = null,
  } = props
  const [dropOver, setDropOver] = useState(false)
  const [dropEdge, setDropEdge] = useState<DropEdge | null>(null)
  const [dragging, setDragging] = useState(false)
  const phrase = counts ? stoppedPhrase(counts) : ''
  const pct = progress ? progressPct(progress) : 0
  const pctPhrase = progress ? `${pct}% done` : ''
  const spoken = [spokenProjectName(label, qualifier), muted ? 'muted' : '', pctPhrase, phrase].filter(Boolean).join(', ')

  function acceptsDrag(e: DragEvent) {
    return onDropTask !== undefined && Array.from(e.dataTransfer.types).includes(TASK_DRAG_TYPE)
  }

  function acceptsReorder(e: DragEvent) {
    return onReorderDrop !== undefined && Array.from(e.dataTransfer.types).includes(FEATURE_DRAG_TYPE)
  }

  function edgeOf(e: DragEvent<HTMLElement>): DropEdge {
    const rect = e.currentTarget.getBoundingClientRect()
    return e.clientY < rect.top + rect.height / 2 ? 'before' : 'after'
  }

  function stop(e: MouseEvent) {
    e.stopPropagation()
    e.preventDefault()
  }

  // A Cmd/Ctrl/Shift click is the browser's (new tab / window) and never selects here; a plain one is the row's.
  // A middle click is an `auxclick`, which the row never handles, so the browser opens it too.
  function onLinkClick(e: MouseEvent) {
    if (e.metaKey || e.ctrlKey || e.shiftKey) e.stopPropagation()
    else e.preventDefault()
  }

  return (
    <Box
      component="div"
      id={id}
      ref={rowRef}
      role="treeitem"
      aria-level={level}
      aria-expanded={expandable ? expanded : undefined}
      aria-selected={selected}
      aria-label={spoken}
      tabIndex={tabbable ? 0 : -1}
      data-testid={testId ?? 'rail-node'}
      data-key={nodeKey}
      data-selected={selected ? 'true' : 'false'}
      data-muted={muted ? 'true' : undefined}
      data-drop-over={dropOver ? 'true' : undefined}
      data-drop-edge={dropEdge ?? undefined}
      data-dragging={dragging ? 'true' : undefined}
      data-menu-open={menuOpen ? 'true' : undefined}
      draggable={reorder ? true : undefined}
      onDragStart={reorder ? e => {
        e.dataTransfer.setData(FEATURE_DRAG_TYPE, nodeKey)
        e.dataTransfer.effectAllowed = 'move'
        setDragging(true)
        reorder.onStart()
      } : undefined}
      onDragEnd={reorder ? () => { setDragging(false); reorder.onEnd() } : undefined}
      onClick={onSelect}
      onKeyDown={onKeyDown}
      onFocus={e => { if (e.target === e.currentTarget) onFocus() }}
      onDragOver={e => {
        if (acceptsReorder(e)) {
          e.preventDefault()
          e.dataTransfer.dropEffect = 'move'
          setDropEdge(edgeOf(e))
          return
        }
        if (!acceptsDrag(e)) return
        e.preventDefault()
        e.dataTransfer.dropEffect = 'move'
        setDropOver(true)
      }}
      onDragLeave={e => {
        // Crossing onto the row's own label or pill is not leaving it.
        if (!e.currentTarget.contains(e.relatedTarget as Node | null)) {
          setDropOver(false)
          setDropEdge(null)
        }
      }}
      onDrop={e => {
        if (acceptsReorder(e)) {
          e.preventDefault()
          setDropEdge(null)
          onReorderDrop?.(edgeOf(e))
          return
        }
        if (!acceptsDrag(e)) return
        e.preventDefault()
        setDropOver(false)
        const taskId = Number(e.dataTransfer.getData(TASK_DRAG_TYPE))
        if (Number.isSafeInteger(taskId) && taskId > 0) onDropTask?.(taskId)
      }}
      sx={(t: Theme) => ({
        position: 'relative',
        display: 'flex',
        alignItems: 'center',
        gap: '4px',
        minHeight: 26,
        margin: '0 2px',
        padding: `2px 2px 2px ${4 + Math.max(0, level - 2) * INDENT_PX}px`,
        borderRadius: '4px',
        fontSize: '0.8125rem',
        cursor: 'pointer',
        outline: 'none',
        userSelect: 'none',
        ...schemed(t, c => ({ color: c.text })),
        '&:hover': schemed(t, c => ({ backgroundColor: c.hoverBg, color: c.textStrong })),
        '&:focus-visible': {
          ...schemed(t, c => ({ outline: `2px solid ${c.accent}` })),
          outlineOffset: '-2px',
        },
        '&[data-selected="true"]': {
          ...schemed(t, c => ({ backgroundColor: c.activeBg, color: c.textActive })),
          fontWeight: 500,
        },
        '&[data-drop-over="true"]': schemed(t, c => ({ outline: `2px dashed ${c.accent}`, outlineOffset: '-2px' })),
        '&[data-dragging="true"]': { opacity: 0.45 },
        // The reorder drop line, on the edge the feature lands against.
        '&[data-drop-edge]::before': {
          content: '""',
          position: 'absolute',
          left: 4,
          right: 4,
          height: 2,
          borderRadius: 1,
          pointerEvents: 'none',
          ...schemed(t, c => ({ backgroundColor: c.accent })),
        },
        '&[data-drop-edge="before"]::before': { top: -1 },
        '&[data-drop-edge="after"]::before': { bottom: -1 },
        // Actions show on hover or keyboard focus; a phone row always shows its "+". `:focus-visible`, not
        // `:focus-within`: a click focuses the row too, and that must not pin its actions once the pointer leaves.
        // An open menu keeps them too: the menu's backdrop takes the hover, and a hidden anchor has no box,
        // so the menu's next reposition would throw it into the window's top-left corner.
        '& .rail-actions': { display: touch ? 'inline-flex' : 'none' },
        '&[data-menu-open="true"] .rail-actions': { display: 'inline-flex' },
        [ABOVE_PHONE]: {
          '&:hover .rail-actions, &:focus-visible .rail-actions': { display: 'inline-flex' },
        },
        ...(touch && { minHeight: t.custom.tapTargetPx, fontSize: '0.875rem' }),
      })}
    >
      <Box
        component="span"
        aria-hidden="true"
        data-testid="rail-disclosure"
        onClick={expandable && onToggle ? (e: MouseEvent) => { stop(e); onToggle() } : undefined}
        sx={{
          flex: '0 0 auto',
          // Above the row's stretched link, so the triangle keeps its own click.
          position: 'relative',
          zIndex: 1,
          width: touch ? 28 : 14,
          alignSelf: 'stretch',
          display: 'inline-flex',
          alignItems: 'center',
          justifyContent: 'center',
          fontSize: '0.6875rem',
          lineHeight: 1,
          opacity: expandable || progress ? 0.9 : 0,
        }}
      >
        {/* Filled triangles, text-presentation (U+FE0E) so no platform swaps ▶ for an emoji. */}
        {/* A feature's progress draws as the feed draws an epic's: the one `ui/ProgressRing`. */}
        {progress && !expandable ? (
          <ProgressRing pct={pct} variant="rail" size={14} testId="rail-progress-ring" title={`${pctPhrase} (${progress.done} of ${progress.total})`} />
        ) : expanded ? '▼\uFE0E' : '▶\uFE0E'}
      </Box>
      {/* Sized to the name, not greedy, so the Stopped pill sits right after it rather than at the far edge. */}
      {/* A muted node greys only its name: its row still selects, hovers and opens like any other. */}
      <Box sx={{ flex: '0 1 auto', minWidth: 0, display: 'flex', flexDirection: 'column', opacity: muted ? 0.5 : undefined }}>
        <Box
          component="span"
          data-testid="rail-node-label"
          title={spokenProjectName(label, qualifier)}
          sx={{ minWidth: 0, whiteSpace: 'normal', overflowWrap: 'anywhere', lineHeight: 1.3 }}
        >
          {/* Inline, so a wrapped name's second line starts under the icon's left edge like any text. */}
          <FeatureIcon icon={icon} inline />
          {href ? (
            <Box
              component="a"
              href={href}
              tabIndex={-1}
              draggable={false}
              data-testid="rail-node-link"
              onClick={onLinkClick}
              // A click focuses the row, never the link: the treeitem is what keyboard and screen reader track.
              onFocus={(e: FocusEvent<HTMLElement>) => (e.currentTarget.closest('[role="treeitem"]') as HTMLElement | null)?.focus()}
              sx={{
                color: 'inherit',
                textDecoration: 'none',
                outline: 'none',
                // Stretched over the row (its positioned ancestor): right-click anywhere is the link's menu.
                '&::after': { content: '""', position: 'absolute', inset: 0 },
              }}
            >
              <Label label={label} highlight={highlight} />
            </Box>
          ) : (
            <Label label={label} highlight={highlight} />
          )}
          {/* Above the stretched link, so their own titles still show on hover. */}
          {(qualifier || labelAside) && (
            <Box component="span" sx={{ position: 'relative' }}>
              <OrgQualifier org={qualifier} />
              {labelAside}
            </Box>
          )}
        </Box>
        {subtitle && (
          <Box
            component="span"
            data-testid="rail-node-path"
            title={subtitle}
            sx={{ position: 'relative', minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', fontSize: '0.6875rem', opacity: 0.8 }}
          >
            {subtitle}
          </Box>
        )}
      </Box>
      {counts && <Pill counts={counts} />}
      {(onCreate || onMenu) && (
        <Box
          component="span"
          className="rail-actions"
          sx={(t: Theme) => ({
            flex: '0 0 auto',
            alignItems: 'center',
            gap: '2px',
            // Over the pill, on the row's own hover ground, so the name keeps its width.
            position: touch ? 'relative' : 'absolute',
            zIndex: 1,
            // Inline on a phone: the name no longer grows, so the actions push themselves to the far edge.
            ml: touch ? 'auto' : undefined,
            right: touch ? undefined : 2,
            top: touch ? undefined : '50%',
            transform: touch ? undefined : 'translateY(-50%)',
            borderRadius: '4px',
            ...(!touch && schemed(t, c => ({ backgroundColor: selected ? c.activeBg : c.hoverBg }))),
          })}
        >
          {onMenu && (
            <Box
              component="button"
              type="button"
              tabIndex={-1}
              aria-label={`More actions for ${label}`}
              title="More actions"
              data-testid="rail-node-menu"
              onClick={(e: MouseEvent<HTMLElement>) => { stop(e); onMenu(e.currentTarget) }}
              sx={touch ? touchActionSx : actionSx}
            >
              <MoreHorizIcon />
            </Box>
          )}
          {onCreate && (
            <Box
              component="button"
              type="button"
              tabIndex={-1}
              aria-label={createLabel ?? `New in ${label}`}
              title={createLabel ?? 'New'}
              data-testid="rail-node-create"
              onClick={(e: MouseEvent<HTMLElement>) => { stop(e); onCreate(e.currentTarget) }}
              sx={touch ? touchActionSx : actionSx}
            >
              +
            </Box>
          )}
        </Box>
      )}
      {children}
    </Box>
  )
}
