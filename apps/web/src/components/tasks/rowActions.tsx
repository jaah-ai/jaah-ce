import type { SxProps, Theme } from '@mui/material/styles'
import { enterActAs } from '../../lib/actAs'
import { LINE_HEIGHT, RADIUS, SIZE } from '../../theme'
import { safeHref } from '../../lib/safeHref'
import type { OrgRef, PipelineTask, Worktree } from '../../types'
import { OverflowMenu, type OverflowItem } from '../../ui/OverflowMenu'
import { can, CONTROLS, startIntentFor, type StartIntent } from './controls'
import { ACTION_ICON, CONSOLE_WORD, isArchiveRow, isEditable } from './shared'
import { isChatTask } from './taskPersona'

// A task row's action vocabulary: which verbs a row offers, their names, and the ⋯ menu. Read by
// the Activity rows (`listViews/feedRow.tsx`) and the task page (`pages/taskDetail/`). `taskRowActionFlags` answers "which verbs?" once;
// the host's inline controls and `taskOverflowItems` both read that value.
// An absent handler withholds its affordance. Presentation (inline cluster, cell, width budget) stays
// with the host; this module renders only the ⋯ trigger and its Menu.

// A top-level row links its worktree only while a dispatched runner holds one (the task page too, A5/R5).
export const IN_PROGRESS_STATES = new Set(['ready', 'active', 'awaiting_response'])

// Defense-in-depth: `worktree.urls` is node-reported and React does not block `javascript:`/`data:`
// hrefs. Delegates to `safeHref` (which also rejects userinfo smuggles like
// `https://github.com@evil.example/`). Never throws.
export function safeWorktreeHref(worktree: Worktree | undefined): string | undefined {
  return safeHref(worktree?.urls?.['web'])
}

/* The ⋯ trigger — every row's only action target, a 44x44 square. */
const ROW_OVERFLOW_SX = {
  display: 'inline-flex',
  width: SIZE.tapTarget,
  minWidth: SIZE.tapTarget,
  minHeight: SIZE.tapTarget,
  justifyContent: 'center',
  fontSize: (t: Theme) => t.typography.h6.fontSize,
  lineHeight: LINE_HEIGHT.tight,
  borderRadius: RADIUS.sm,
} as const

/* Row shape: a top-level row (`'root'`) or a subtask (`'leaf'`). Both offer Delete/Archive in every state. */
export type TaskRowVariant = 'root' | 'leaf'

/* Row-action callbacks and worktree resolvers; each optional, absent withholds its affordance (a
   read-only surface passes `{}`). Two resolvers on purpose: `worktreeFor` is stack-gated
   (`pages/Tasks.tsx`: stack up and serving `urls.web`) for the 🌐 link; `consoleWorktreeFor` is the
   console's. */
export type TaskRowActionHandlers = {
  onEdit?: (task: PipelineTask) => void
  onStart?: (task: PipelineTask, intent: StartIntent) => void
  onStop?: (task: PipelineTask) => void
  onReset?: (task: PipelineTask) => void
  onComplete?: (task: PipelineTask) => void
  onDelete?: (task: PipelineTask) => void
  /* Discard on a Jaah suggestion (`SuggestionActions`): Delete with no confirm; shown where Delete is. */
  onDiscard?: (task: PipelineTask) => void
  /* Opens the create surface pre-filled from this row (`lib/taskDuplicate.ts`); writes nothing here, so
     no state or persona gate — handler presence is the whole gate, supplied on `tasks:create`. */
  onDuplicate?: (task: PipelineTask) => void
  /* This user's pin (`lib/taskPin.ts`, `hooks/useTaskPins.ts`): a preference write, so no permission gate — handler
     presence is the whole gate. A top-level row only: a subtask stays in its epic's order. */
  isPinned?: (task: PipelineTask) => boolean
  onTogglePin?: (task: PipelineTask) => void
  /* The row's Creator → Assignee cell reassigns in place (`../TaskPeople.tsx`): a `PUT` edit, so supplied on
     `tasks:edit` and narrowed per row by `canEditRow`, as Edit. */
  onAssign?: (task: PipelineTask, assigneeId: string) => void
  /* Per-row refinements for Edit and Delete: a `tasks:create` holder without `tasks:edit` may edit and
     delete their own `draft` rows (`authored_draft`; rule in `hooks/useTaskAccess.ts`). Absent means
     always-true (the opposite of the handlers). They only narrow, folded in with `&&` after the
     handler-presence test. */
  canEditRow?: (task: PipelineTask) => boolean
  canDeleteRow?: (task: PipelineTask) => boolean
  /* Run-verb refinement (Start/Stop, Reset, Complete): a write needs `tasks:edit` on that row's project;
     rule in `hooks/useTaskAccess.ts::useCanRunTaskRow`. Absent means always-true. */
  canRunRow?: (task: PipelineTask) => boolean
  /* Why Start is offered but cannot be pressed on ANY row (a pending org — `lib/orgLifecycle.ts`), or
     absent. Start stays shown, disabled with these words; it never hides the verb. */
  startBlockedReason?: string
  /* The row's org when it is another org's (an operator's cross-org view), else null — the host passes
     `ui/OrgIndicator.tsx::useOtherOrgOf()` as is. Such a row's ⋯ leads with "Act as <org>" (the whole tab), and its
     Duplicate names the org it copies into; its other verbs go as that org (the host's
     `lib/actAs.ts::useActAsRows`). Absent: every row is the viewer's own. */
  otherOrgOf?: (org: OrgRef | null | undefined) => OrgRef | null
  /* The org a Duplicate press acts as, where the host leads with its own notice instead of `otherOrgOf`'s ⋯ lines
     (the detail page's banner). Defaults to `otherOrgOf`. */
  duplicateAsOf?: (org: OrgRef | null | undefined) => OrgRef | null
  worktreeFor?: (task: PipelineTask) => Worktree | undefined
  consoleWorktreeFor?: (task: PipelineTask) => Worktree | undefined
  onOpenConsole?: (worktree: Worktree) => void
}

/* Which verbs a row offers plus the resolved `consoleSlot`/`stackHref`, so a host cannot re-resolve
   and offer what the flags refused. */
export type TaskRowActionFlags = {
  /* Start and Stop are separate flags: a `ready` row may take either (`stop`'s sources include `ready`). */
  showStart: boolean
  /* Set only with `showStart`: Start renders DISABLED with this reason (`TaskRowActionHandlers.startBlockedReason`). */
  startBlockedReason: string | undefined
  showStop: boolean
  showEdit: boolean
  showDuplicate: boolean
  /* Pin or Unpin offered; `pinned` is the row's current pin (false when not offered). */
  showPin: boolean
  pinned: boolean
  consoleSlot: Worktree | undefined
  showConsole: boolean
  stackHref: string | undefined
  showReset: boolean
  showComplete: boolean
  showCleanup: boolean
  hasActions: boolean
}

// Whether this row's state says its runner console may be opened — the one rule behind `consoleSlotFor`
// and the task page's own slot memo. `active`, plus an advisor chat parked `awaiting_response` (its runner is alive at its prompt, and the
// console is its only screen — `taskPersona.ts`'s `isChatTask`), plus a row DELIVERING an answer (its
// runner is alive inside the question call). An ordinary asking row resolves none: its `?` has an answer
// screen.
export function consoleStateLive(task: PipelineTask): boolean {
  return task.state === 'active'
    || (task.state === 'awaiting_response' && (isChatTask(task) || task.answer_delivering === true))
}

// The console slot this row offers, or `undefined`. One derivation for the State and Actions cells. An
// epic never has a runner.
function consoleSlotFor(
  task: PipelineTask,
  variant: TaskRowVariant,
  consoleWorktreeFor: TaskRowActionHandlers['consoleWorktreeFor'],
): Worktree | undefined {
  if (!consoleStateLive(task)) return undefined
  if (variant === 'root' && task.persona === 'epic') return undefined
  return consoleWorktreeFor?.(task)
}

// The 🌐 stack link's URL, read by the `#` cell and the Actions ⋯ — one derivation. A top-level row
// links while `IN_PROGRESS_STATES`, a subtask only while `active`. `worktreeFor` is the stack-gated
// resolver.
function stackHrefFor(
  task: PipelineTask,
  variant: TaskRowVariant,
  worktreeFor: TaskRowActionHandlers['worktreeFor'],
): string | undefined {
  const eligible =
    variant === 'root' ? IN_PROGRESS_STATES.has(task.state) : task.state === 'active'
  return eligible ? safeWorktreeHref(worktreeFor?.(task)) : undefined
}

/* The one derivation of which verbs a row offers. Read once per row and pass the value to both the
   inline controls and `taskOverflowItems`. */
export function taskRowActionFlags(
  task: PipelineTask,
  variant: TaskRowVariant,
  handlers: TaskRowActionHandlers,
): TaskRowActionFlags {
  const {
    onEdit,
    onStart,
    onStop,
    onReset,
    onComplete,
    onDelete,
    onDuplicate,
    canEditRow,
    canDeleteRow,
    canRunRow,
    startBlockedReason,
    worktreeFor,
    consoleWorktreeFor,
    onOpenConsole,
    otherOrgOf,
  } = handlers
  // Lifecycle verbs from the transition table, one question per verb: `ready` offers both Start and
  // Stop; `active`/`awaiting_response` Stop only; `draft`/`paused`/`done`/`system_failed` Start only.
  const canStart = can(task, 'start')
  const canStop = can(task, 'stop')
  // `!== false`: an absent predicate means no opinion. `isEditable` (row-state rule) comes first.
  const showEdit = onEdit !== undefined && isEditable(task) && canEditRow?.(task) !== false
  /* Handler presence alone: Duplicate writes nothing to this row. */
  const showDuplicate = onDuplicate !== undefined
  const showPin = handlers.onTogglePin !== undefined && variant === 'root'
  const pinned = showPin && handlers.isPinned?.(task) === true
  // Run-verb refinement narrows after handler presence; Stop included (the API 404s otherwise).
  const mayRun = canRunRow?.(task) !== false
  const showStart = mayRun && canStart && onStart !== undefined
  const showStop = mayRun && canStop && onStop !== undefined
  // The console is an Actions-cell item at phone width, so it counts toward `hasActions`.
  const consoleSlot = consoleSlotFor(task, variant, consoleWorktreeFor)
  const showConsole = consoleSlot !== undefined && onOpenConsole !== undefined
  // The stack link is a `#`-cell anchor above `NUM_FIELD` and a ⋯ item below it, so it counts too.
  const stackHref = stackHrefFor(task, variant, worktreeFor)
  // An epic's Reset cascades to its whole tree (`routes/tasks/transitions.py::reset_task`).
  const showReset = mayRun && onReset !== undefined && can(task, 'reset')
  const showComplete = mayRun && onComplete !== undefined && can(task, 'complete')
  // Any row, any state: the operator delete route kills a live row's runner (`delete_task`).
  const showCleanup = onDelete !== undefined && canDeleteRow?.(task) !== false
  // Another org's row always has its ⋯: Act as lives there.
  const hasActions =
    otherOrgOf?.(task.org) != null ||
    showEdit ||
    showDuplicate ||
    showPin ||
    showStart ||
    showStop ||
    showConsole ||
    stackHref !== undefined ||
    showReset ||
    showComplete ||
    showCleanup
  return {
    showStart,
    startBlockedReason: showStart ? startBlockedReason : undefined,
    showStop,
    showEdit,
    showDuplicate,
    showPin,
    pinned,
    consoleSlot,
    showConsole,
    stackHref,
    showReset,
    showComplete,
    showCleanup,
    hasActions,
  }
}

/* The ⋯ items, in the inline cluster's order (Edit, lifecycle, console, Reset/Complete/Cleanup). Every
   row control collapses here at phone width, built as data from the same `flags` the caller used for
   its inline controls. */
export function taskOverflowItems(
  task: PipelineTask,
  name: string,
  handlers: TaskRowActionHandlers,
  flags: TaskRowActionFlags,
): OverflowItem[] {
  const {
    onEdit, onStart, onStop, onReset, onComplete, onDelete, onDuplicate, onOpenConsole, otherOrgOf, duplicateAsOf,
  } = handlers
  const otherOrg = otherOrgOf?.(task.org) ?? null
  const duplicateAs = (duplicateAsOf ?? otherOrgOf)?.(task.org) ?? null
  const overflowItems: OverflowItem[] = []
  /* Another org's row leads with the way into that org for this whole tab (`lib/actAs.ts`); its verbs below already
     go as the org one request at a time. */
  if (otherOrg) {
    overflowItems.push({
      key: 'act-as',
      label: `Act as ${otherOrg.name}`,
      ariaLabel: `Act as ${otherOrg.name}`,
      onClick: () => enterActAs(otherOrg),
    })
  }
  if (flags.showEdit) {
    overflowItems.push(editItem(name, () => onEdit!(task)))
  }
  /* Duplicate under Edit: both open a form; everything below acts on the row. Another org's row is copied into that
     org, so the label names the switch the press makes (act-as) before it is pressed. */
  if (flags.showDuplicate) {
    overflowItems.push({
      key: 'duplicate',
      icon: ACTION_ICON.duplicate,
      label: duplicateAs ? `Duplicate as ${duplicateAs.name}` : 'Duplicate',
      ariaLabel: duplicateAs ? `Duplicate ${name} as ${duplicateAs.name}` : `Duplicate ${name}`,
      onClick: () => onDuplicate!(task),
    })
  }
  /* Pin under Duplicate: the last item that changes nothing about the task itself (only where this user sees it). */
  if (flags.showPin) {
    const verb = flags.pinned ? 'Unpin' : 'Pin'
    overflowItems.push({
      key: 'pin',
      icon: ACTION_ICON.pin,
      label: verb,
      ariaLabel: `${verb} ${name}`,
      onClick: () => handlers.onTogglePin!(task),
    })
  }
  // Words and Start's intent come from the control registry (`startIntentFor`). Only `ready` pushes
  // both: Start, then Stop as its undo.
  if (flags.showStart) {
    overflowItems.push({
      key: 'start',
      icon: CONTROLS.start.icon,
      label: CONTROLS.start.label,
      ariaLabel: `${CONTROLS.start.label} ${name}`,
      ...(flags.startBlockedReason ? { disabled: true, disabledReason: flags.startBlockedReason } : {}),
      onClick: () => onStart!(task, startIntentFor(task)),
    })
  }
  if (flags.showStop) {
    overflowItems.push({
      key: 'stop',
      icon: CONTROLS.stop.icon,
      label: CONTROLS.stop.label,
      ariaLabel: `${CONTROLS.stop.label} ${name}`,
      danger: true,
      onClick: () => onStop!(task),
    })
  }
  if (flags.showConsole) {
    overflowItems.push({
      key: 'console',
      icon: ACTION_ICON.console,
      label: CONSOLE_WORD,
      ariaLabel: `Open web console for ${name}`,
      onClick: () => onOpenConsole!(flags.consoleSlot!),
    })
  }
  // Stack link right after the console; the full name is on `ariaLabel`.
  if (flags.stackHref) {
    overflowItems.push({
      key: 'stack',
      icon: ACTION_ICON.stack,
      label: 'Stack',
      // Contains the visible label (WCAG 2.5.3 Label in Name); the inline anchor uses the same name.
      ariaLabel: `Open Local Stack for ${name}`,
      href: flags.stackHref,
    })
  }
  if (flags.showReset) {
    overflowItems.push({
      key: 'reset',
      icon: CONTROLS.reset.icon,
      label: CONTROLS.reset.label,
      ariaLabel: `${CONTROLS.reset.label} ${name}`,
      onClick: () => onReset!(task),
    })
  }
  if (flags.showComplete) {
    overflowItems.push({
      key: 'complete',
      icon: CONTROLS.complete.icon,
      label: CONTROLS.complete.label,
      ariaLabel: `${CONTROLS.complete.label} ${name}`,
      onClick: () => onComplete!(task),
    })
  }
  if (flags.showCleanup) {
    const archive = isArchiveRow(task)
    overflowItems.push({
      key: 'cleanup',
      icon: archive ? ACTION_ICON.archive : ACTION_ICON.delete,
      label: archive ? 'Archive' : 'Delete',
      ariaLabel: `${archive ? 'Archive' : 'Delete'} ${name}`,
      danger: true,
      onClick: () => onDelete!(task),
    })
  }
  return overflowItems
}

export type { OverflowItem }

// The stock Edit / Delete items. `name` completes the accessible name (`Edit ${name}`), so it may carry a
// noun (`host web-1`); `opts` adds a label override or the disabled pair.
type VerbOpts = Partial<Pick<OverflowItem, 'label' | 'disabled' | 'disabledReason'>>
function verbItem(key: string, icon: string, verb: string, name: string, target: (() => void) | { to: string }, opts?: VerbOpts): OverflowItem {
  const base = { key, icon, label: verb, ariaLabel: `${verb} ${name}`, ...opts }
  return typeof target === 'function' ? { ...base, onClick: target } : { ...base, to: target.to }
}
export const editItem = (name: string, target: (() => void) | { to: string }, opts?: VerbOpts): OverflowItem =>
  verbItem('edit', ACTION_ICON.edit, 'Edit', name, target, opts)
export const deleteItem = (name: string, onClick: () => void, opts?: VerbOpts): OverflowItem =>
  ({ ...verbItem('delete', '✕', 'Delete', name, onClick, opts), danger: true })

export function RowActionsOverflow({
  name,
  busy,
  items,
  /* Trigger box; defaults to `ROW_OVERFLOW_SX`. Hosts outside a table cell pass their own. */
  sx = ROW_OVERFLOW_SX,
}: {
  name: string
  busy: boolean
  items: OverflowItem[]
  sx?: SxProps<Theme>
}): JSX.Element | null {
  return (
    <OverflowMenu
      items={items}
      busy={busy}
      ariaLabel={`More actions for ${name}`}
      testId="row-actions-overflow-toggle"
      sx={sx}
    />
  )
}
