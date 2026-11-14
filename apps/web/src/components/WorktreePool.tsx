import { type ReactNode, useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Link as RouterLink, useNavigate } from 'react-router-dom'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import Card from '@mui/material/Card'
import CardContent from '@mui/material/CardContent'
import Link from '@mui/material/Link'
import ExternalLink from '../ui/ExternalLink'
import Typography from '@mui/material/Typography'
import { Mono } from '../ui/DetailFields'
import { ApiError, apiDetail, del, get, post } from '../api'
import { usePoll } from '../hooks/usePoll'
import { listProjects, useWorktrees } from '../hooks/useRosters'
import type { RowSelection } from '../hooks/useRowSelection'
import { useSubmit } from '../hooks/useSubmit'
import { useCan } from '../auth'
import { useIsOrgAdmin } from '../hooks/useIsOrgAdmin'
import { useOtherOrgOf } from '../ui/OrgIndicator'
import { enterActAs } from '../lib/actAs'
import type { NewTaskNavState } from '../lib/taskDuplicate'
import { useCommand } from './CommandButton'
import { danger, useConfirm } from './ConfirmDialog'
import { useConsoleDock, useConsoleNav } from './ConsoleDock'
import { useDebouncedCallback } from '../hooks/useDebouncedCallback'
import { useEventStream } from '../hooks/useEventStream'
import { useEventsSinceSeen } from '../hooks/useLiveRefetch'
import { slotConsoleRequest, useCanDriveWorktreeSession, useCanEditWorktrees } from '../hooks/useWorktreeAccess'
import { refusalMessage, releaseLiveSessionRefusal } from '../lib/erase'
import { countOf } from '../lib/humanize'
import { releaseConfirmMessage, releasedNotice, releaseSlot } from '../lib/slotRelease'
import { fmtModelBrief } from '../lib/models'
import { isAuthRejected, isQuotaHigh } from '../lib/quota'
import { safeHref } from '../lib/safeHref'
import { briefTimestamp, fmtTimestamp } from '../lib/relativeTime'
import { FREE_TITLE, hasLiveSession, isFolderSlot, resolveTitle, titleShown } from '../lib/worktree'
import {
  AWAITING_ANSWER_WORDS,
  type SlotSignal,
  type SlotTone,
  DESTROY_STUCK_HOURS,
  gitSummary,
  gitWords,
  idleLabel,
  isAwaitingAnswer,
  isHeld,
  isProtected,
  poolRollup,
  poolSlotState,
  retiringHours,
  retiringLabel,
  sessionAge,
  sessionSignal,
  slotIdentity,
  slotNumberLabel,
  stackChipWord,
  stackSignal,
  stackWords,
  stateSignal,
  stateTone,
} from '../lib/poolSlot'
import { taskDeepLinkPath } from '../lib/taskDeepLink'
import { matchesWorktreeFilters, type WorktreeFilters } from '../lib/worktreeFilters'
import { VM_START_POLL_MS } from '../lib/pollIntervals'
import {
  GROUP_BY_FLAT,
  buildWorktreeRows,
  type IsGroupCollapsed,
  type NodeLabelFor,
  type WorktreeGroupBy,
} from '../lib/worktreeGrouping'
import { OrgQualifier } from '../lib/projectLabel'
/* Type-only (erased; no module cycle). The shape is the contract with the host page, documented there. */
import type { WorktreePoolView } from '../pages/Worktrees'
import type { ClaudeAccount, Command, NodeRecord, Project, SlotDeleteResult, Worktree } from '../types'
import { getResources, RESOURCES_KEY } from '../pages/credentials/api'
import Banner from '../ui/Banner'
import CloneOwnerControl from './CloneOwnerControl'
import { CopyButton } from '../ui/CopyButton'
import EmptyState from '../ui/EmptyState'
import NoMatchesState from '../ui/NoMatchesState'
import LoadingState from '../ui/LoadingState'
import FeedList, { ACTIONS_SPACER_SX } from '../ui/feed/FeedList'
import type { FeedMarker, FeedRowSpec, FeedSection, FeedSlot, OverflowItem } from '../ui/feed/types'
import { KeyValue, KeyValueList } from '../ui/KeyValueList'
import {
  ELLIPSIS_SX, FEED_NO_META, PHONE, POOL_LIST_CONTAINER_SX, POOL_NO_PROJECT, monoSx,
  tapTargetSx,
} from '../ui/listStyles'
import { OverflowMenu } from '../ui/OverflowMenu'
import MetaRow, { MetaActors, MetaField } from '../ui/MetaRow'
import { SELECT_SLOT, SelectAllBox, SelectRowBox, selectSlot } from '../ui/RowSelect'
import { BreakAfter } from '../ui/BreakAfter'
import SideDrawer from '../ui/SideDrawer'
import PageHeader from '../ui/PageHeader'
import StatusChip, { type Tone } from '../ui/StatusChip'
import { TONE_COLOR } from '../ui/statusTone'
import { FONT_SIZE, FONT_WEIGHT, grid, LINE_HEIGHT, RADIUS } from '../theme'
import Tip from '../ui/Tip'

/* ══ THE FLEET'S WORKTREE POOL ═════════════════════════════════════════════════════════════════════
 *
 * The `/worktrees` page's body: a dense status table plus a right-side detail drawer, answering "what is
 * up with each slot WITHOUT opening anything?" across every node (Tasks 57, 2452).
 *
 * The node is a property of the ROW, never of the panel: `(project, number)` names a slot on EVERY node,
 * so every dispatch/erase/name reads `slot.node_id`. The node roster prop supplies names, offline
 * verdicts and the Add form's target, and may be EMPTY (see the prop).
 *
 * The page owns the reader's view — filters (`lib/worktreeFilters.ts`, URL-owned), grouping
 * (`lib/worktreeGrouping.ts` + `lib/worktreeViewPrefs.ts`), selection and bulk verbs — and passes them
 * down; `onViewChange` reports survivors, totals and facets back. `nodeLabelFor` comes from the page so
 * the filter predicate and the row agree on a node's name.
 *
 * It also DRIVES the pool (declare → create → lease → work → release; the API owns each boundary).
 * Driving controls sit in the DRAWER (a row column would shred Title); only scan-for controls stay on
 * the row. Release (`POST .../release`) sits beside Delete. Delete (`POST .../erase`, see
 * `apps/api/app/routes/pool_slots.py`) cannot be refused: the panel REPORTS whether the row
 * went, the directory went, and a holder was killed. API 409s are surfaced verbatim (`apiDetail`).
 *
 * It owns the `['worktrees']` react-query key: `components/ConsoleDock.tsx` SUBSCRIBES to it for live
 * titles and `may_edit`, resolving by `(project, number, node)`. Cache and render are the same set;
 * neither may be narrowed.
 */

/* ── WHAT THE ROW SPENDS ITS WIDTH ON ──────────────────────────────────────────
 * `ui/feed/geometry.ts` owns the row's two bands, `ui/listStyles.ts` the two slot gates — `meta` (≤899px)
 * and `id` (≤767px). This block is the single home for the arguments below.
 *   · THE DISC IS THE STATE (`slotMark`): row tints were unreadable in greyscale/forced colours.
 *   · `State` is a `FeedMarker` (`◌`), reserving no width on rows that lack it; the quota `⚠` rides it.
 *   · `Stack`/`Session` render once. The disc says WHETHER held, `occupancyHint` says BY WHOM.
 *   · A third marker, `!`, says the hold awaits the READER's answer.
 *   · Every inline control lives in the ⋯: `theme.ts` raises `MuiIconButton` to 44x44 below `sm`, so a
 *     control in an ungated slot would set the row's height (44 vs the phone band's 24).
 *   · SELECT stays (`FeedSlot.compact`): the only way to pick a row for the bulk bar.
 */

/** The slot number leading the title. Not `FeedRowSpec.lead`, which is gated at `FEED_NO_ID`: many
 *  titles are `free`, so a phone list without numbers is identical rows. */
const SLOT_NUMBER_SX = { mr: 0.5, color: 'text.disabled' } as const

/* The flat row's project, prefixed to the slot number wherever the Project slot drops (its window gate or
   `POOL_NO_PROJECT`, `ui/feed/geometry.ts`'s `poolProject`), so a narrow pool still reads `acme#1`, not a bare
   `#1`. Generated content: `textContent` (what oracles read) stays the number.
   The Title keeps priority: a long project ellipsises within 40% of the title box. */
const SLOT_PROJECT_PREFIX_SX = {
  '&::before': {
    content: 'attr(data-project-prefix)',
    display: 'none',
    maxWidth: '40%',
    ...ELLIPSIS_SX,
    verticalAlign: 'bottom',
  },
  [FEED_NO_META]: { '&::before': { display: 'inline-block' } },
  [POOL_NO_PROJECT]: { '&::before': { display: 'inline-block' } },
} as const

/* The row's destroy tag (`destroySignals`): a WORD in a hairline box, never a colour alone (AC2.1). */
const DESTROY_TAG_SX = {
  display: 'inline-block',
  mr: 0.5,
  px: 0.5,
  border: 1,
  borderRadius: RADIUS.sm,
  fontSize: FONT_SIZE.xs,
  lineHeight: LINE_HEIGHT.snug,
  verticalAlign: 'text-bottom',
  whiteSpace: 'nowrap',
} as const

type DestroySignal = { key: string; label: string; tone: Tone; title: string }

/** Whether the slot's destroy is under way, stuck or refused, with how long it has run; none otherwise. A
 *  row from before `retiring_since` reads a bare `Destroying`. */
function destroySignals(slot: Worktree): DestroySignal[] {
  if (slot.destroy_error) {
    return [{ key: 'destroy', label: 'Destroy failed', tone: 'error', title: `Destroy failed: ${slot.destroy_error}` }]
  }
  if (!slot.retiring) return []
  const hours = retiringHours(slot)
  if (hours === null) return [{ key: 'destroy', label: 'Destroying', tone: 'warning', title: 'Being destroyed' }]
  const age = retiringLabel(hours)
  return hours >= DESTROY_STUCK_HOURS
    ? [{ key: 'destroy', label: `Stuck ${age}`, tone: 'error', title: `Destroy ordered ${age} ago; the node has not confirmed it` }]
    : [{ key: 'destroy', label: `Destroying ${age}`, tone: 'warning', title: `Destroy ordered ${age} ago` }]
}

/** A removal past `DESTROY_STUCK_HOURS` with no refusal on record. */
function isDestroyStuck(slot: Worktree): boolean {
  const hours = retiringHours(slot)
  return !slot.destroy_error && hours !== null && hours >= DESTROY_STUCK_HOURS
}

/** Why the node has not destroyed the slot, in its own words; `destroy_error` for an API without the field. */
function destroyNodeError(slot: Worktree): string | null {
  return slot.destroy_node_error || slot.destroy_error || null
}

/** A destroy that needs a hand — refused, failed, or stuck: the drawer offers Retry and Forget. */
function needsDestroyHand(slot: Worktree): boolean {
  return !!slot.retiring && (destroyNodeError(slot) !== null || isDestroyStuck(slot))
}

function SlotMarks({ slot }: { slot: Worktree }) {
  return (
    <>
      {destroySignals(slot).map(signal => (
        <Tip key={signal.key} title={signal.title}><Box
          component="span"
          data-testid={`slot-${signal.key}-tag`}
          sx={{ ...DESTROY_TAG_SX, color: TONE_COLOR[signal.tone], borderColor: 'currentColor' }}
        >
          {signal.label}
        </Box></Tip>
      ))}
    </>
  )
}

/** The testid-carrying value must itself clip: `scrollWidth` of an inline span in a clipping parent is 0. */
const SLOT_TEXT_SX = { ...ELLIPSIS_SX, display: 'block', maxWidth: '100%' } as const

/* ── The card's header line: rollup, select-all. Wraps rather than crowds, never
   leaving the checkbox alone on a line; `rowGap` is half the column gap. */
const POOL_HEADER_SX = {
  display: 'flex',
  flexWrap: 'wrap',
  alignItems: 'center',
  columnGap: 1,
  rowGap: 0.5,
} as const

/* The rollup's group. `0 1 auto`: select-all's `auto` margin owns the slack. */
const POOL_HEADING_GROUP_SX = {
  flex: '0 1 auto',
  display: 'flex',
  flexWrap: 'wrap',
  alignItems: 'baseline',
  columnGap: 1,
  rowGap: 0,
  minWidth: 0,
} as const

/** The card's padding. */
const CARD_CONTENT_SX = {
  /* The pool's own width drives the slot gates (`ui/feed/geometry.ts`'s `pool*`): a docked console narrows it
     while the window stays wide. */
  ...POOL_LIST_CONTAINER_SX,
  display: 'flex',
  flexDirection: 'column',
  gap: 1,
  p: 1.5,
  '&:last-child': { pb: 1.5 },
  [PHONE]: { p: 1, '&:last-child': { pb: 1 } },
} as const

/* ── THE TRAILING CLUSTER ─────────────────────────────────────────────────────────────
   Ordered by gate strength (meta, id, ungated; see `slotRow`) so slots fall off the LEFT as the viewport
   narrows. Widths are the ink each value needs at 11px, taken from the title. PROJECT/NODE only in the
   flat outline; GIT fits `↑12 ↓3 ✎`. At 320px the title keeps ~195px. The tightest desktop case is flat
   at 900px (all seven slots), hence 76/72. Floors pinned in `e2e/worktrees-desktop.spec.ts`. */
const SLOT_W = {
  project: 76,
  node: 72,
  git: 58,
  stack: 48,
  /* `● waiting`/`● starting` whole: at 52 the word ellipsised. */
  session: 60,
  age: 30,
} as const

/* ── SELECT-ALL over the column it ticks, stating its own COUNT. The cluster reproduces the row's
   trailing geometry (gap, `pr`, select-slot width, a blank ⋯ slot) so it aligns by construction;
   `e2e/worktrees-desktop.spec.ts` pins the two centres. */
const SELECT_ALL_SX = {
  /* `auto` margin, not a spacer element, which would take its own line on wrap. */
  ml: 'auto',
  display: 'flex',
  alignItems: 'center',
  gap: 1,
  pr: 0.5,
  fontSize: FONT_SIZE.xs,
  /* NOT `text.disabled`: 2.7:1 on the light card, under AA at 11px. */
  color: 'text.secondary',
  [PHONE]: { gap: 0.5 },
} as const

const SELECT_ALL_COLUMN_SX = {
  flex: '0 0 auto',
  width: SELECT_SLOT,
  display: 'flex',
  justifyContent: 'center',
} as const

/* The bulk verbs act on the page's SELECTION (`ui/BulkActionBar`) via the exported per-slot runners below:
 * `lib/worktreeGrouping.ts::buildWorktreeRows` OMITS folded rows, so a fan-out by mounted rows would skip them. */
/** The `/api/stream` entities whose events can change this panel: slots, their runners, and the
 *  accounts the drawer joins for quota rows. */
const LIVE_ENTITIES = new Set(['worktree', 'runner', 'account'])

/** `POST /api/console/{project_id}/new-session` (`routes/console.py::ConsoleNewSessionOut`): a slot, or on a MicroVM
 *  node a VM (`vm_id`, `number` 0). */
type NewSession = { project: string; project_id: number; number: number; node_uuid: string; vm_id?: string }

/* ── Per-slot action runners the page fans out over its selection ──────────────────────────────
 * Plain async functions (a bulk bar runs one per row). Each performs EXACTLY the row/drawer control's
 * request, so bulk and single cannot drift. Each THROWS on failure so a fan-out can report "6 of 8".
 * `slot.node_uuid` is in every request: without it the CP addresses its own machine.
 */

/* The command bus as `components/CommandButton.tsx::useCommand` submits it: a failure unless the node
   says `done`; `result.reason` is the node's words. `useCommand` is a hook and cannot be called per row.
   A third caller should move this read into `CommandButton.tsx`, not copy it. */
async function runSlotCommand(slot: Worktree, commandType: string): Promise<void> {
  const cmd = await post<Command>('/api/commands', {
    command_type: commandType,
    args: { project_id: slot.project_id, number: slot.number },
    node_id: slot.node_uuid,
  })
  if (cmd.status === 'done') return
  const reason = cmd.result && typeof cmd.result['reason'] === 'string' ? cmd.result['reason'] : cmd.status
  throw new Error(`${slotIdentity(slot)}: ${reason}`)
}

/** Take ONE slot's stack down — the drawer's Stop. Data volumes are kept; see `stackWords`. */
export function stopSlotStack(slot: Worktree): Promise<void> {
  return runSlotCommand(slot, 'stack_down')
}

/* Delete one slot — de-declare it and delete its directory, best effort. `POST .../erase` (path spelling
   kept for external clients) always forces and cannot be refused:
   a user may always delete a worktree. There is no `force` parameter.
   Answers 200 with a body: callers render `node_detail` as a WARNING and `holder_ended` as part of the
   outcome (`handleDelete`, `pages/Worktrees.tsx`'s bulk report). `number` is encoded, not trusted.
   **THE BODY IS NORMALISED HERE**: `api()` gives `undefined` (204) or `null` (unparseable 200) across a
   rolling deploy or proxy, and the row IS already deleted — callers reading raw fields would throw and
   report a completed delete as failed. */
export async function deleteSlot(slot: Worktree): Promise<SlotDeleteResult> {
  const body = await post<SlotDeleteResult | null>(
    `/api/worktrees/${slot.project_id}/${encodeURIComponent(String(slot.number))}/erase` +
    `?node_id=${encodeURIComponent(slot.node_uuid)}`,
  )
  if (!body || typeof body !== 'object') {
    return {
      // Always true on a success (`SlotDeleteOut`'s contract).
      row_deleted: true,
      // FALSE AND EXPLAINED: never claim the directory went without knowing.
      node_erased: false,
      node_detail: 'this deploy answered without a report, so what the node managed is not known here — '
        + 'check the host before assuming the directory is gone',
      holder_ended: null,
    }
  }
  return {
    row_deleted: body.row_deleted !== false,
    node_erased: body.node_erased === true,
    node_detail: typeof body.node_detail === 'string' ? body.node_detail : null,
    holder_ended: typeof body.holder_ended === 'string' ? body.holder_ended : null,
  }
}

/* Order a refused, failed or stuck destroy again (`POST .../destroy/retry`, `routes/pool_slots.py::
   retry_slot_destroy`): clears the refusal and mints a fresh order. 409 when the slot is not being destroyed. */
export function retryDestroy(slot: Worktree): Promise<void> {
  return post<void>(
    `/api/worktrees/${slot.project_id}/${encodeURIComponent(String(slot.number))}/destroy/retry` +
    `?node_id=${encodeURIComponent(slot.node_uuid)}`,
  )
}

/* Forget one slot: the row-only `DELETE` (`routes/pool_slots.py::delete_slot_row`). Nothing reaches the node, so
   its files stay; a still-pending destroy order is cancelled with the row. */
export function forgetSlot(slot: Worktree): Promise<void> {
  return del<void>(
    `/api/worktrees/${slot.project_id}/${encodeURIComponent(String(slot.number))}` +
    `?node_id=${encodeURIComponent(slot.node_uuid)}`,
  )
}

/* The ⚠ beside a slot's identity: running something AND its account near a quota wall. Deliberately
 * narrower than quota alone: a row mark means "this slot is spending the quota running out". The full
 * reading is in the drawer. */
function showQuotaBadge(slot: Worktree, account?: ClaudeAccount): boolean {
  return hasLiveSession(slot) && !!account?.quota && isQuotaHigh(account.quota)
}

/* The slot's own web app href, or nothing — one derivation for the Stack cell's 🌐 and the drawer's
   Launch. `safeHref` over the node-REPORTED URL (`lib/safeHref.ts`). An address is not a running server:
   callers AND `stack_state === 'up'` (and the drawer also withholds Launch on an offline node). */
function slotWebHref(slot: Worktree): string | undefined {
  return safeHref(slot.urls?.web)
}

/* `unknown` is table-only (recedes below `neutral`); a chip renders "not measured" as neutral. */
function chipTone(tone: SlotTone): Tone {
  return tone === 'unknown' ? 'neutral' : tone
}

/* The occupancy hover sentence, single-homed for the row, the drawer chip and the drawer detail row.
   Whether a runner is alive is `sessionSignal`'s question. */
function occupancyHint(slot: Worktree): string {
  if (!isHeld(slot)) return 'free'
  /* The task arm names the hold's reason: an unanswered question is the reader's to act on;
     wording single-homed in `AWAITING_ANSWER_WORDS`. Not "fleet task": a tenant renders this panel
     (`test/tenantCopy.ts`, `ui-copy-standards.md` §"Copy a tenant reads"). */
  const base = isAwaitingAnswer(slot)
    ? `held by a task that is ${AWAITING_ANSWER_WORDS}`
    : 'held by a task'
  return isProtected(slot)
    ? `${base} — protected: dispatch never touches it`
    : base
}

/* The node's reason words (`fleet_observe.ObservedKeptRepo`/`ObservedFailedRepo`); an unknown one shows raw. */
const REPO_REASON_LABEL: Record<string, string> = {
  uncommitted: 'Uncommitted changes',
  unpushed: 'Unpushed commits',
  credential_refused: 'Credential refused',
  clone_failed: 'Clone failed',
  fetch_failed: 'Fetch failed',
  reset_refused: 'Not reset',
  guard_failed: 'Push guard missing',
  unverified: 'Could not check',
}

/* One repo line: its forge path in full (wrapped, never truncated), then the secondary facts beneath. */
function RepoRow({ testId, repoPath, chip, lines }: {
  testId: string
  repoPath: string
  chip?: ReactNode
  lines: { text: string; mono?: boolean }[]
}) {
  return (
    <Box data-testid={testId} sx={{ py: 0.5, minWidth: 0 }}>
      <Box sx={{ display: 'flex', flexWrap: 'wrap', alignItems: 'center', gap: 0.5 }}>
        <Typography variant="mono" component="span" sx={{ overflowWrap: 'anywhere', minWidth: 0 }}>{repoPath}</Typography>
        {chip}
      </Box>
      {lines.filter(line => line.text !== '').map((line, index) => (
        <Typography
          key={index}
          variant={line.mono ? 'mono' : 'caption'}
          component="div"
          sx={{ color: 'text.secondary', overflowWrap: 'anywhere' }}
        >
          {line.text}
        </Typography>
      ))}
    </Box>
  )
}

/* A multi-repo slot's repositories, plus any the node kept (would lose work) or could not bring in. Renders
   nothing for a single-repo slot (`repos` absent), so those drawers are unchanged. */
export function SlotRepoList({ repos }: { repos: Worktree['repos'] }) {
  if (!repos) return null
  const groups: { key: string; title: string; rows: ReactNode[] }[] = [
    {
      key: 'repos', title: 'Repositories', rows: repos.repos.map(repo => (
        <RepoRow
          key={repo.id}
          testId="slot-repo"
          repoPath={repo.repo_path}
          chip={repo.primary ? <StatusChip label="Primary" tone="info" /> : undefined}
          lines={[{ text: repo.base }, { text: repo.path, mono: true }]}
        />
      )),
    },
    {
      key: 'kept', title: 'Kept', rows: repos.kept.map(repo => (
        <RepoRow
          key={repo.id}
          testId="slot-repo-kept"
          repoPath={repo.repo_path}
          lines={[{ text: REPO_REASON_LABEL[repo.reason] ?? repo.reason }, { text: repo.path, mono: true }]}
        />
      )),
    },
    {
      key: 'failed', title: 'Failed', rows: repos.failed.map(repo => (
        <RepoRow
          key={repo.id}
          testId="slot-repo-failed"
          repoPath={repo.repo_path}
          lines={[{ text: REPO_REASON_LABEL[repo.reason] ?? repo.reason }, { text: repo.detail ?? '' }]}
        />
      )),
    },
  ]
  return (
    <Box data-testid="slot-repos" sx={{ mt: 2, pt: 1.5, borderTop: '1px solid', borderColor: 'divider' }}>
      {groups.filter(group => group.rows.length > 0).map(group => (
        <Box key={group.key} sx={{ mb: 1 }}>
          <Typography variant="caption" component="div" sx={{ color: 'text.secondary' }}>{group.title}</Typography>
          {group.rows}
        </Box>
      ))}
    </Box>
  )
}

/* Palette PATHS, never `theme.palette` reads: under CSS-variable theming that is a frozen light-mode
   literal. `unknown` is a fifth tone below `neutral`. */
const SIGNAL_COLOR: Record<SlotTone, string> = { ...TONE_COLOR, unknown: 'text.disabled' }

/* One status atom. Glyph first (shape survives greyscale/forced colours); the sentence is on `title`. An
   `unknown` atom is its dimmed word alone: `? —` said "not measured" twice. */
function SignalText({ testId, signal }: { testId: string; signal: SlotSignal }) {
  return (
    <Box component="span" data-testid={testId} sx={[SLOT_TEXT_SX, { color: SIGNAL_COLOR[signal.tone] }]}>
      {signal.tone === 'unknown' ? signal.label : (
        <>
          <Box component="span" aria-hidden="true">{signal.glyph}</Box>
          {` ${signal.label}`}
        </>
      )}
    </Box>
  )
}

/** `flex-end`: an inline-flex box hugs the left of a right-aligned cell otherwise. */
const GIT_ROW_SX = { display: 'inline-flex', justifyContent: 'flex-end', gap: 0.5, width: '100%' } as const

/* The GIT slot. Four states: `—` (never scanned) and `✓` (in sync) differ, explained on `title`
   (`gitWords`). `dirty` rides alongside on its own three-valued axis. */
function GitCell({ slot }: { slot: Worktree }) {
  const summary = gitSummary(slot)
  /* Three answers: `true` is `✎`; `false` renders nothing; `null` (probe failed) gets the dimmed `?`
     "not measured" mark — never "clean" for an unexamined slot, and receding next to a real `✎`. */
  const dirty = summary.dirty === true ? (
    <Tip title="uncommitted work"><Box
      component="span"
      data-testid="slot-git-dirty"
      sx={{ color: 'warning.main', fontWeight: FONT_WEIGHT.semibold }}
    >
      ✎
    </Box></Tip>
  ) : summary.dirty === null ? (
    <Tip title="not measured"><Box
      component="span"
      data-testid="slot-git-dirty-unknown"
      sx={{ color: 'text.disabled' }}
    >
      ?
    </Box></Tip>
  ) : null
  if (summary.kind === 'unscanned') {
    // Nothing measured is one dimmed `—`, not `— ?`; real dirt (`✎`) still shows beside it.
    return (
      <Box component="span" sx={GIT_ROW_SX}>
        <Box
          component="span"
          data-testid="slot-git-none"
          sx={{ color: 'text.disabled' }}
        >
          —
        </Box>
        {summary.dirty === true && dirty}
      </Box>
    )
  }
  return (
    <Box component="span" data-testid="slot-git" sx={GIT_ROW_SX}>
      {summary.kind === 'sync' ? (
        <Box component="span" data-testid="slot-git-sync" sx={{ color: 'success.main' }}>
          ✓
        </Box>
      ) : (
        <>
          {summary.ahead > 0 && (
            <Box component="span" data-testid="slot-git-ahead" sx={{ color: 'success.main', fontWeight: FONT_WEIGHT.semibold }}>↑{summary.ahead}</Box>
          )}
          {summary.behind > 0 && (
            <Box component="span" data-testid="slot-git-behind" sx={{ color: 'warning.main', fontWeight: FONT_WEIGHT.semibold }}>↓{summary.behind}</Box>
          )}
        </>
      )}
      {dirty}
    </Box>
  )
}

/* ── THE ROW'S ONE MARK ────────────────────────────────────────────────────────────────────────────
 * Can I have this slot, and if not, what has it? Precedence is the operator's order: unreachable, in
 * use, taken, free. What the disc drops rides `occupancyHint` on `title` and the accessible name.
 * Shapes differ in FORM (`⊘ ● ◆ ○`), and no glyph repeats, so tone is never the only difference
 * (see `poolSlot.ts` on `stackSignal`). */
type SlotMark = { tone: Tone; glyph: string; label: string }

function slotMark(slot: Worktree, nodeOffline: boolean, nodeLabel: string): SlotMark {
  if (nodeOffline) {
    return { tone: 'neutral', glyph: '⊘', label: `${nodeLabel} is offline` }
  }
  // `hasLiveSession`, not `busy`: an `idle` session is still occupied. The PULSE is `busy` only (`slotRow`).
  if (hasLiveSession(slot)) {
    return { tone: 'info', glyph: '●', label: `live session (${slot.session_status})` }
  }
  if (isHeld(slot)) {
    return { tone: 'warning', glyph: '◆', label: 'held by a task' }
  }
  return { tone: 'success', glyph: '○', label: 'free' }
}

/* A drawer verb that does not go through `useCommand` (the lease and destroy verbs the parent runs): one
   `Button` shape, the aria-label naming the act on this slot. */
function SlotVerb({ label, testId, ariaLabel, onClick, disabled, variant, color, size }: {
  label: string
  testId: string
  ariaLabel: string
  onClick: () => void
  disabled: boolean
  variant: 'outlined' | 'contained'
  color?: 'inherit' | 'error'
  size?: 'small'
}) {
  return (
    <Button
      size={size}
      variant={variant}
      color={color}
      data-testid={testId}
      onClick={onClick}
      disabled={disabled}
      aria-label={ariaLabel}
      sx={tapTargetSx}
    >
      {label}
    </Button>
  )
}

/* ── One driving control in the drawer's Actions block ─────────────────────────────────────────────
 * A real `Button` (no MenuList context), all one size so the block reads as one cluster.
 * Transient phases replace the button IN PLACE so the block never reflows mid-click; `failed` keeps the
 * node's reason on screen (those refusals are instructions). `destructive` adds an inline "— sure?"
 * (Stop and Rebuild). `slot.node_uuid` is the dispatch address, read off the ROW: without it the command
 * runs on the CP's own node.
 */
function SlotCommandButton({ slot, commandType, label, icon, destructive = false, beforeRun, onRun }: {
  slot: Worktree
  commandType: string
  label: string
  icon: string
  destructive?: boolean
  beforeRun?: () => Promise<boolean>
  onRun: () => void
}) {
  const { phase, setPhase, errMsg, submit } = useCommand(
    commandType, { project_id: slot.project_id, number: slot.number }, onRun, slot.node_uuid,
  )

  if (phase === 'submitting' || phase === 'done' || phase === 'failed') {
    return (
      <Tip title={errMsg || undefined}><Typography
        component="span"
        variant="caption"
        data-testid="slot-action-status"
        data-command={commandType}
        sx={{
          alignSelf: 'center',
          color: phase === 'failed' ? 'error.main' : phase === 'done' ? 'success.main' : 'text.secondary',
        }}
      >
        {phase === 'submitting' ? `${label}…` : phase === 'done' ? `${label} ✓` : `${label} — ${errMsg || 'failed'}`}
      </Typography></Tip>
    )
  }

  if (phase === 'confirming') {
    return (
      <Box sx={{ display: 'inline-flex', alignItems: 'center', gap: 0.5 }}>
        <Typography component="span" variant="caption" data-testid="slot-action-confirm">{label} — sure?</Typography>
        <Button size="small" variant="contained" color="error" onClick={() => void submit()} sx={tapTargetSx}>Yes</Button>
        <Button size="small" variant="text" onClick={() => setPhase('idle')} sx={tapTargetSx}>No</Button>
      </Box>
    )
  }

  return (
    <Button
      size="small"
      variant="outlined"
      color={destructive ? 'error' : 'inherit'}
      data-testid={`slot-action-${commandType.replace(/_/g, '-')}`}
      onClick={async () => {
        if (beforeRun && !(await beforeRun())) return
        if (destructive) setPhase('confirming')
        else void submit()
      }}
      sx={tapTargetSx}
    >
      <Box component="span" aria-hidden="true" sx={{ mr: 0.5 }}>{icon}</Box>{label}
    </Button>
  )
}

/* ── The detail drawer ─────────────────────────────────────────────────────────────────────────────
 * A right-anchored MUI `Drawer`: Escape/backdrop close,
 * focus trap and restore, full-width at `xs`. Renders from the ROW ALREADY IN HAND so it can never
 * disagree with the row. The open slot is local state, not a URL param: a deep-linked row can be gone or
 * filtered out, a page-level state this panel lacks; the drawer closes when its row leaves a refetch.
 */
function SlotDetail({
  slot, nodeLabel, nodeOffline, account, onClose, onDelete, onRelease, onRetryDestroy, onForget, busy, canDelete,
  canEdit, canDriveSession, onRun, onOpenConsole, canSetCloneOwner,
}: {
  slot: Worktree
  /* This slot's node, already resolved to a name; commands read `slot.node_id` themselves. */
  nodeLabel: string
  /* Whether this row's node is offline; every driving control needs a live daemon. */
  nodeOffline: boolean
  account?: ClaudeAccount
  onClose: () => void
  /** DELETE this slot (row + directory, best effort). Cannot be refused. */
  onDelete: (slot: Worktree) => void
  /** End this slot's HOLD. Nothing on disk moves. */
  onRelease: (slot: Worktree) => void
  /** Order a refused, failed or stuck destroy again. */
  onRetryDestroy: (slot: Worktree) => void
  /** Drop the row only; the node's files stay. */
  onForget: (slot: Worktree) => void
  busy: boolean
  canDelete: boolean
  /* The command-bus tier — `worktrees:edit` OR `system:create` (what `POST /api/commands` accepts). Governs
     stack commands, Logs, Finder, Cursor. */
  canEdit: boolean
  /* The session tier — `worktrees:edit` alone: Release. See `hooks/useWorktreeAccess.ts`. */
  canDriveSession: boolean
  onRun: () => void
  onOpenConsole: (slot: Worktree) => void
  /* Superadmin only, like declaring (`PUT /api/worktrees/{project}/clone-owner`). */
  canSetCloneOwner: boolean
}) {
  const otherOrgOf = useOtherOrgOf()
  const identity = slotIdentity(slot)
  const lifecycle = poolSlotState(slot)
  const state = stateSignal(slot)
  const stack = stackSignal(slot)
  const session = sessionSignal(slot)
  const age = sessionAge(slot)
  const ports = slot.ports
    ? Object.entries(slot.ports).map(([role, port]) => `${role} :${port}`).join(' · ')
    : ''
  /* Per-row half of every driving gate, ANDed with the page-wide key: `worktrees:create`/`delete` widen
     what a principal SEES to the fleet without widening what they may drive. */
  const mayDrive = canEdit && slot.may_edit
  const maySession = canDriveSession && slot.may_edit
  const stackUp = slot.stack_state === 'up'
  /* One `safeHref` verdict per URL (`lib/safeHref.ts`), shared by the URLs row and the action buttons, so
     the same value is never rendered under two verdicts. */
  const apiHref = safeHref(slot.urls?.api)
  // The same derivation as the Stack cell's 🌐 (`slotWebHref`).
  const webHref = slotWebHref(slot)
  /* Release: only where there is a hold, on the SESSION tier (`routes/pool_slots.py::release_slot`
     declares `worktrees:edit`, not the command bus's wider OR). A `free` slot is excluded (a no-op 204).
     An offline node is NOT: the CP write commits first, and a dead box's hold is what this clears. */
  const canRelease = maySession && slot.state !== 'free'
  const quota = account?.quota
  /* The console only ATTACHES to a live session; nothing opens one on a slot. An offline node withholds it. */
  const canConsole = !nodeOffline && hasLiveSession(slot)

  /* ── The two ways a slot leaves the operator's hands ─────────
     Both are offered on a held slot. What separates them is the TREE: Release ends the
     hold and leaves the directory; Delete takes row AND directory. Each confirm says what it does to
     the files. Gates differ: Delete is superadmin, Release is `worktrees:edit` on this slot.
     Pinned to the drawer's foot (`SideDrawer`'s `footer`): under a short drawer they left a blank page.
     Styled by severity: Release (the tree stays) is neutral, Delete (irreversible) is the one filled red. */
  const leaveActions = (canRelease || canDelete) ? (
    <Box data-testid="slot-detail-leave" sx={{ display: 'flex', flexWrap: 'wrap', gap: 1 }}>
      {/* No captions: what Release leaves and ends, and what Delete destroys, each confirm states once. */}
      {canRelease && (
        <SlotVerb
          label="Release slot" testId="slot-action-release" ariaLabel={`Release ${slotIdentity(slot)}`}
          onClick={() => onRelease(slot)} disabled={busy}
          variant="outlined" color="inherit"
        />
      )}
      {canDelete && (
        <SlotVerb
          label="Delete slot" testId="slot-action-delete" ariaLabel={`Delete ${slotIdentity(slot)}`}
          onClick={() => onDelete(slot)} disabled={busy}
          variant="contained" color="error"
        />
      )}
    </Box>
  ) : undefined

  return (
    <SideDrawer
      testId="slot-detail-drawer"
      /* Names the node: `acme#1` exists on every machine. */
      label={`Slot details — ${identity} on ${nodeLabel}`}
      width={{ xs: '100%', sm: grid(76.5) }}
      pad={1.5}
      onClose={onClose}
      closeLabel="Dismiss slot details"
      footer={leaveActions}
      title={<Mono>{identity}</Mono>}
      subtitle={
        <Box sx={{ display: 'flex', flexWrap: 'wrap', gap: 0.5 }}>
          {/* A retiring slot shows its destroy state alone: `Declared`/`Ready` beside Destroying/Stuck contradicts it. */}
          {!slot.retiring && (
            <StatusChip label={state.label} tone={chipTone(state.tone)} glyph={state.glyph} title={state.hint} data-status={lifecycle} />
          )}
          {/* A chip shows a WORD (`stackChipWord`), where the table shows a mark; no ad hoc fallback word. An
              `unknown` chip drops its `?`, as `SignalText` does: "not reported" already says unknown. */}
          <StatusChip
            label={`Stack ${stackChipWord(slot)}`}
            tone={chipTone(stack.tone)}
            glyph={stack.tone === 'unknown' ? undefined : stack.glyph}
            title={stack.hint}
          />
          {/* The drawer's one occupancy surface (`state`): no `Occupancy` row repeats it. Dropped
              while retiring, like the lifecycle chip: who holds a slot being removed says nothing. */}
          {!slot.retiring && (
            <StatusChip label={slot.state} tone={stateTone(slot.state)} title={occupancyHint(slot)} />
          )}
          {destroySignals(slot).map(signal => (
            <StatusChip key={signal.key} label={signal.label} tone={signal.tone} title={signal.title} />
          ))}
          {isProtected(slot) && (
            <StatusChip
              label="Protected"
              tone="warning"
              glyph="⚑"
              title="Human-started — dispatch never touches it"
            />
          )}
        </Box>
      }
    >
      {/* The node's own words on a refused or failed destroy: a `title` never reaches a touch screen. */}
      {slot.retiring && destroyNodeError(slot) && (
        <Typography variant="body2" data-testid="slot-destroy-error" sx={{ mb: 1, color: 'error.main', overflowWrap: 'anywhere' }}>
          Destroy failed: {destroyNodeError(slot)}
        </Typography>
      )}
      {/* A removal the node never confirmed. Said in words: the row tag's `title` is hover-only. */}
      {isDestroyStuck(slot) && (
        <Typography variant="body2" data-testid="slot-destroy-stuck" sx={{ mb: 1, color: 'error.main' }}>
          {`Removal stuck — ordered ${retiringLabel(retiringHours(slot) ?? 0)} ago, not confirmed by ${nodeLabel}.`}
        </Typography>
      )}
      {/* The two ways out of a destroy that needs a hand. Retry orders it again; Forget drops the row and leaves
          the files. Forget replaced Dismiss, which ran the footer's Delete a second time — the one verb that
          waits on the same node the destroy is stuck on. */}
      {canDelete && needsDestroyHand(slot) && (
        <Box data-testid="slot-destroy-actions" sx={{ mb: 1, display: 'flex', flexWrap: 'wrap', gap: 1 }}>
          <SlotVerb
            label="Retry" testId="slot-action-retry-destroy" ariaLabel={`Retry destroy of ${identity}`}
            onClick={() => onRetryDestroy(slot)} disabled={busy} variant="outlined" size="small"
          />
          <SlotVerb
            label="Forget" testId="slot-action-forget" ariaLabel={`Forget ${identity}`}
            onClick={() => onForget(slot)} disabled={busy} variant="outlined" color="error" size="small"
          />
        </Box>
      )}
      {/* `Declared` is the one lifecycle state explained inline (it makes Add look like it did nothing);
          touch devices never show a `title`. */}
      {/* Keyed on the state TOKEN (`poolSlotState`, also `data-status`), never the rendered label. A retiring
          slot will never be built: the promise would contradict its Destroying/Stuck tag. */}
      {lifecycle === 'declared' && !slot.retiring && (
        <Box
          data-testid="slot-state-note"
          sx={{
            mb: 1,
            p: 1,
            borderRadius: RADIUS.sm,
            borderLeft: `${grid(0.5)} solid`,
            borderColor: 'divider',
            backgroundColor: 'action.hover',
          }}
        >
          <Typography variant="caption" component="div">{state.hint}</Typography>
        </Box>
      )}

      {/* ── ACTIONS ─────────────────────────────────────────────────────────────────────────────────
          Above the fact list, so the drawer's verbs show on open: below it, a 800px-tall drawer opened with
          the row under the sticky footer. Order: read, then repair — Session, stack
          lifecycle (coarsest first), Logs / Finder / Cursor, then clipboard items; the leaving verbs stay in
          the footer. Each control renders only when it could run (hidden, not disabled), except the
          offline-node and no-stack cases, which get a sentence. */}
      {(canConsole || mayDrive || slot.path) && (
        <Box
          data-testid="slot-detail-actions"
          sx={{ mb: 1.5, pb: 1.5, borderBottom: '1px solid', borderColor: 'divider' }}
        >
          <Typography variant="caption" component="div" sx={{ color: 'text.secondary', mb: 1 }}>
            Actions
          </Typography>
          {nodeOffline && (
            <Typography variant="caption" component="div" data-testid="slot-actions-offline" sx={{ color: 'text.secondary', mb: 1 }}>
              {nodeLabel} is offline — the console and every node command are withheld.
            </Typography>
          )}
          <Box sx={{ display: 'flex', flexWrap: 'wrap', alignItems: 'center', gap: 1 }}>
            {canConsole && (
              <Tip title={`Open the console on ${identity}`}><Button
                size="small"
                variant="outlined"
                color="inherit"
                data-testid="slot-action-session"
                onClick={() => onOpenConsole(slot)}
                sx={tapTargetSx}
              >
                <Box component="span" aria-hidden="true" sx={{ mr: 0.5 }}>🪟</Box>Session
              </Button></Tip>
            )}
            {mayDrive && !nodeOffline && (stackUp ? (
              <>
                <SlotCommandButton slot={slot} commandType="stack_down" label="Stop" icon="⏹" destructive onRun={onRun} />
                <SlotCommandButton slot={slot} commandType="stack_restart" label="Restart" icon="↻" onRun={onRun} />
                <SlotCommandButton slot={slot} commandType="stack_rebuild" label="Rebuild" icon="⤓" destructive onRun={onRun} />
              </>
            ) : (
              <SlotCommandButton slot={slot} commandType="stack_up" label="Start" icon="▶" onRun={onRun} />
            ))}
            {mayDrive && !nodeOffline && (
              <>
                <SlotCommandButton slot={slot} commandType="logs" label="Logs" icon="▤" onRun={onRun} />
                <SlotCommandButton slot={slot} commandType="open_finder" label="Finder" icon="🗁" onRun={onRun} />
                <SlotCommandButton slot={slot} commandType="open_cursor" label="Cursor" icon="↗" onRun={onRun} />
              </>
            )}
            {/* LAUNCH the slot's web app in a new tab — an action, beside the URLs row's value. Behind
                `stack_state === 'up'`, through `slotWebHref` (node-reported URL), and withheld on an
                offline node. */}
            {stackUp && webHref && !nodeOffline && (
              <Button
                size="small"
                variant="outlined"
                color="inherit"
                component="a"
                href={webHref}
                target="_blank"
                rel="noreferrer"
                data-testid="slot-action-launch"
                sx={tapTargetSx}
              >
                <Box component="span" aria-hidden="true" sx={{ mr: 0.5 }}>🌐</Box>Launch
              </Button>
            )}
            {/* Ungated: copying a displayed string needs no permission. Only when there is a string. */}
            {slot.path && (
              <CopyButton testId="slot-action-copy-path" label="Copy path" text={() => slot.path} />
            )}
            {stackUp && apiHref && (
              <CopyButton testId="slot-action-copy-api-url" label="Copy API URL" text={() => apiHref} />
            )}
          </Box>
        </Box>
      )}

      {/* `stack={false}`: one-or-two-word labels, so a phone keeps label-beside-value as the desktop drawer does;
          stacked, every fact took two lines and doubled the drawer's height. */}
      <KeyValueList labelWidth={grid(15)} stack={false}>
        {/* The CP's stored `name` (`<project>/<number>`), distinct from the client-side heading
            (`slotIdentity`). An absent value is `—` in the body font everywhere here:
            a monospace face draws `—` one column wide, where it reads as `–`. */}
        {/* The CP-composed Title, read only (`apps/api/app/slot_title.py`); the task names the slot. */}
        <KeyValue testId="slot-detail-row" label="Title">{titleShown(slot.title)}</KeyValue>
        {slot.description !== '' && <KeyValue testId="slot-detail-row" label="Description">{slot.description}</KeyValue>}
        {/* Not when it equals the Title: a slot titled by its own name drew one long string twice. */}
        {(slot.name ?? '').trim() !== titleShown(slot.title).trim() && (
          <KeyValue testId="slot-detail-row" label="Name" title={slot.name || undefined} mono={!!slot.name}>{slot.name || '—'}</KeyValue>
        )}
        {/* The whole path, wrapped. A tenant reader gets `""` (`routes/nodes/node_wire.py::_narrow_for_tenant`), which
            draws no row. */}
        {slot.path && <KeyValue testId="slot-detail-row" label="Path" mono title={slot.path}><BreakAfter text={slot.path} sep="/" /></KeyValue>}
        {/* Project and Node as labelled rows: every value folded out of the row must be reachable here. */}
        <KeyValue testId="slot-detail-row" label="Project" mono title={slot.project}>{slot.project}</KeyValue>
        <KeyValue testId="slot-detail-row" label="Node" mono>{nodeLabel}</KeyValue>
        {/* A folder slot has no repository: no Branch or Git row, rather than a `—` claiming one unknown. */}
        {!isFolderSlot(slot) && (
          <>
            <KeyValue testId="slot-detail-row" label="Branch" mono={slot.branch != null} title={slot.branch ?? undefined}>{slot.branch ?? '—'}</KeyValue>
            <KeyValue testId="slot-detail-row" label="Git">
              {`${gitWords(slot)}${slot.dirty === true ? ' · uncommitted work in the tree' : slot.dirty === null ? ' · dirty state unknown' : ''}`}
            </KeyValue>
          </>
        )}
        {/* Same switch as the chip and the row cell: `stackWords`. */}
        <KeyValue testId="slot-detail-row" label="Stack">{stackWords(slot)}</KeyValue>
        {ports !== '' && <KeyValue testId="slot-detail-row" label="Ports" mono>{ports}</KeyValue>}
        {(webHref || apiHref) && (
          <KeyValue testId="slot-detail-row" label="URLs">
            <Box component="span" sx={{ display: 'inline-flex', flexWrap: 'wrap', gap: 1 }}>
              {webHref && <ExternalLink href={webHref}>web</ExternalLink>}
              {apiHref && <ExternalLink href={apiHref}>api</ExternalLink>}
            </Box>
          </KeyValue>
        )}
        <KeyValue testId="slot-detail-row" label="Session" title={session.hint}>
          {slot.session_status ? session.label : 'none observed'}
        </KeyValue>
        {/* Two durations side by side, since each alone reads as the other: `Session age` (from
            `session_started_at`) and `Idle` (from `last_active`). Computed at render; the drawer re-renders
            on every `worktree` event, so no interval (a second clock would disagree). */}
        <KeyValue testId="slot-detail-row" label="Session age" title={age.hint}>{age.unreported ? 'not reported' : age.label}</KeyValue>
        {/* Beside a live session a bare `—` read as "no session"; it is a missing report. */}
        <KeyValue
          testId="slot-detail-row"
          label="Idle"
          title={slot.idle_hours == null
            ? 'the node has reported no activity in this slot, so there is no idle time to measure'
            : 'since the slot was last active'}
        >
          {slot.idle_hours == null && hasLiveSession(slot) ? 'not reported' : idleLabel(slot)}
        </KeyValue>
        <KeyValue testId="slot-detail-row" label="Task">
          {slot.task_id == null ? (
            '—'
          ) : slot.task_active ? (
            <Link component={RouterLink} to={taskDeepLinkPath(slot.task_id)}>#{slot.task_id}</Link>
          ) : (
            `#${slot.task_id} (not active)`
          )}
        </KeyValue>
        {/* The link is one click to the question. "An answer", not "you": the slot does not say whom the task
            asked, and a reader who is not its owner was told it was theirs. */}
        {isAwaitingAnswer(slot) && slot.task_id != null && (
          <KeyValue testId="slot-detail-row" label="Waiting on" title={AWAITING_ANSWER_WORDS}>
            <Link component={RouterLink} to={taskDeepLinkPath(slot.task_id)}>an answer — task #{slot.task_id}</Link>
          </KeyValue>
        )}
        {/* `fmtModelBrief`: a full model id does not fit the label gutter. */}
        <KeyValue testId="slot-detail-row" label="Model" mono={!!slot.model}>{fmtModelBrief(slot.model)}</KeyValue>
        {/* `slot.account` is a config DIRECTORY (the join key); joined against `/api/resources` for the account's
            name, else its email. An unjoined directory says so. */}
        <KeyValue testId="slot-detail-row" label="Account">{account?.name || account?.email || (slot.account ? 'Unknown account' : '—')}</KeyValue>
        <KeyValue testId="slot-detail-row" label="Config dir" mono={!!slot.account} title={slot.account ?? undefined}>{slot.account ?? '—'}</KeyValue>
        {/* Three arms: a reading, `auth_rejected` (null percents; needs a re-login, not a wait), and no
            reading — never simply withheld, since absence is not a state. `lib/quota.ts` owns the 80%
            boundary and the rejection exclusion. */}
        {!quota ? (
          <KeyValue testId="slot-detail-row" label="Quota">{slot.account ? 'No quota data' : '—'}</KeyValue>
        ) : (isAuthRejected(quota) ? (
          <KeyValue testId="slot-detail-row" label="Quota">Token rejected — needs re-login</KeyValue>
        ) : (
          <>
            <KeyValue testId="slot-detail-row" label="Quota 5h">{`${quota.fiveHourPercent ?? '—'}%`}</KeyValue>
            <KeyValue testId="slot-detail-row" label="Quota 7d">{`${quota.weeklyPercent ?? '—'}%`}</KeyValue>
            <KeyValue testId="slot-detail-row" label="Sonnet 7d">{`${quota.sonnetWeeklyPercent ?? '—'}%`}</KeyValue>
            {quota.overQuotaSpendPercent != null && (
              <KeyValue testId="slot-detail-row" label="Spend cap">{`${quota.overQuotaSpendPercent}%`}</KeyValue>
            )}
          </>
        ))}
        {/* `observed_at`: the CP has SEEN the tree on the node. Absent means never observed, not stale; a
            retiring slot will never be built, so it promises no build. */}
        <KeyValue testId="slot-detail-row" label="Observed" title={slot.observed_at ? fmtTimestamp(slot.observed_at) : undefined}>
          {slot.observed_at ? briefTimestamp(slot.observed_at) : slot.retiring ? 'never — being removed' : 'never — still a declaration'}
        </KeyValue>
      </KeyValueList>

      <SlotRepoList repos={slot.repos} />

      {/* Provenance via the shared `ui/MetaRow` (as `pages/NodeDetailPage.tsx`). Names arrive resolved
          (`app/actors.py::actor_names`); no `*_id` on the wire. `updated_at` is the CP's last ROW write,
          not `observed_at` (last sensed on disk). */}
      {/* `Created`, not `Declared`: `Declared` is a lifecycle state here. */}
      {/* The two "when"s, then the two "who"s (`UserEditPage`'s rule), so a wrap splits between groups. */}
      <MetaRow>
        <MetaField label="Created" iso={slot.created_at} brief />
        <MetaField label="Updated" iso={slot.updated_at} brief />
        <MetaActors createdBy={slot.created_by} updatedBy={slot.updated_by} />
      </MetaRow>
      {canSetCloneOwner && (
        <CloneOwnerControl
          clone={{
            project: slot.project, projectId: slot.project_id, nodeId: slot.node_uuid, nodeLabel,
            otherOrg: otherOrgOf(slot.org),
          }}
          onSaved={onRun}
        />
      )}
    </SideDrawer>
  )
}

// The pool-slot READ + WRITE surface for the whole fleet.
//
// A slot is keyed `(node_id, project, number)`. The dispatcher leases each slot on a node the project's node groups
// allow, made for a task and destroyed once freed; the node's reconcile materialises (`create_mechanics` refuses undictated ids). Nothing is declared here. Deleting is
// PHYSICAL.
//
// A delete commits the row FIRST; the node hop only reports. An orphan directory is a known
// one, named in the response, so every surface renders `node_erased: false` as a WARNING on a success.
export default function WorktreePool({
  nodes, nodeLabelFor, filters, groupBy, isCollapsed, onToggleGroup, selection, onViewChange,
}: {
  /* Every node, for what a bare `node_id` cannot do: name it, say if it is reachable. May be EMPTY:
     `/api/nodes` is a `system` read while this page is `worktrees:view`.
     A node absent from the roster is NOT offline: only an explicit `'offline'` counts (`isOffline`), so a
     reader without `system:view` still gets working controls. */
  nodes: NodeRecord[]
  /* A node's name, resolved by the PAGE: its filter predicate (`matchesWorktreeFilters`), grouping
     (`buildWorktreeRows`) and filter menu need the identical string, or a search would drop a row whose
     visible name matched. Never empty: falls back to the short id (`lib/nodeName.ts`). */
  nodeLabelFor: NodeLabelFor
  /** Which rows survive, from the URL (`lib/worktreeFilters.ts`). The panel applies it; the page owns it. */
  filters: WorktreeFilters
  /** How the surviving rows are arranged (`lib/worktreeGrouping.ts`), and so which columns are emitted. */
  groupBy: WorktreeGroupBy
  /** Is this group bar folded? Answered from the page's stored folds, else the group's size default
   *  (`lib/worktreeViewPrefs.ts`). */
  isCollapsed: IsGroupCollapsed
  onToggleGroup: (groupKey: string, collapsed: boolean) => void
  /* Picked rows by worktree id (`project#number` is not unique fleet-wide). Held by the page's bulk bar
     (`hooks/useRowSelection.ts` over `onScreen`'s ids), ticked by the table's checkboxes. */
  selection: RowSelection
  /** What this panel just rendered, reported back up — see `WorktreePoolView` and the effect below. */
  onViewChange: (view: WorktreePoolView) => void
}) {
  /* The roster indexed once BY UUID (a per-row `find` over hundreds of rows adds up); a slot joins on
     `node_uuid`. A presence-only row (null `id`) is no address and is dropped. */
  const nodesById = useMemo(
    () => new Map(nodes.flatMap(n => (n.id ? [[n.id, n] as const] : []))), [nodes])
  /* Offline only on an explicit measurement; unknown status or unreadable roster answer FALSE, keeping a
     working slot drivable. */
  const isOffline = useCallback(
    (nodeId: string) => nodesById.get(nodeId)?.status === 'offline',
    [nodesById],
  )
  const [projects, setProjects] = useState<Project[] | null>(null)
  /* Two loads, two error slots: a shared one was cleared by the next successful slot read, leaving an
     unexplained empty New session menu. */
  const [projectsError, setProjectsError] = useState('')
  const { busy, formError: actionError, setFormError: setActionError, run } = useSubmit()
  const [notice, setNotice] = useState('')
  const [slotConfirm, slotConfirmDialog] = useConfirm()
  // The row whose detail drawer is open (see `SlotDetail` for why local state).
  const [detail, setDetail] = useState<Worktree | null>(null)
  /* What a successful delete left on a node (the directory survived). Its own state beside `notice`: a
     success with a consequence is neither an error nor a green line. */
  const [nodeWarning, setNodeWarning] = useState('')
  /* The CP's live-session refusal of a Release, held with its slot so the Force retry re-issues the SAME
     request. Set only via `releaseLiveSessionRefusal`, so no other failure can offer a Force. Release keeps
     its guard because the next lease will `git reset --hard` the tree unseen. Never retried automatically. */
  const [releaseRefusal, setReleaseRefusal] = useState<{ slot: Worktree; session: string } | null>(null)

  /* Neither read is implied by the route (`/worktrees` is `worktrees:view`; `GET /api/projects` needs
     `projects:view`). Ask only what the account may read, and say plainly why a region is empty. */
  const can = useCan()
  const canReadWorktrees = can('worktrees:view')
  const canReadProjects = can('projects:view')
  /* The writes are OPERATOR-ONLY: `.../erase` carries `require_operator_acting`
     (`app.auth.require_operator`, `apps/api/app/routes/pool_slots.py`), as this page is.
     The page's own gate already admits only the operator, who is an org admin, so `useIsOrgAdmin` here only
     keeps a wide `worktrees:delete` holder from being offered a Delete that 403s. The verdict is read off the
     principal, not `useCan` — it is not grantable (`app/authz/`), as in `Projects.tsx`/`Accounts.tsx`.
     Presentation only. For an org admin `slot.may_edit` is always true, so it is not ANDed here. */
  const isOrgAdmin = useIsOrgAdmin()
  const otherOrgOf = useOtherOrgOf()
  const navigate = useNavigate()
  const canDeclareSlots = canReadProjects && isOrgAdmin
  const canDeleteSlots = canReadWorktrees && isOrgAdmin
  /* ── The two driving tiers, neither being the superadmin gate: see
     `hooks/useWorktreeAccess.ts` (`worktrees:view` + `system:create` may restart a stack but not type). */
  const canEdit = useCanEditWorktrees()
  const canDriveSession = useCanDriveWorktreeSession()

  /* ── The slot list, owned through react-query under `['worktrees']` ──────────────────────────────
     `components/ConsoleDock.tsx` SUBSCRIBES to this key for live titles and `may_edit`; a `useState` read
     would leave the dock with no owner here. Cache and render are the same set; narrow neither.
     `enabled` is the permission gate and resolves to `[]` (a terminal state, not an endless skeleton); it
     also stops the warm-reload race from re-firing a stale-granted 403 read.
     Stream down → 10s poll; the SSE refetch below is the live path. */
  const stream = useEventStream()
  const streamState = stream.state
  const slotsQuery = useWorktrees({
    refetchInterval: streamState === 'down' ? 10000 : false,
    enabled: canReadWorktrees,
  })
  /* When this tab last retried each slot's destroy (by `id`). The API keeps the first order's `retiring_since`, so
     without this a Retry's stuck line and Retry/Forget come straight back: the age is read from the retry. */
  const [retriedAt, setRetriedAt] = useState<Record<string, string>>({})
  /* Rows are the payload, whole, but for a retried destroy's age. A denied account resolves to `[]`; `null` is
     "still loading" (skeleton). */
  const slots = useMemo(
    () => {
      if (!canReadWorktrees) return [] as Worktree[]
      const rows = slotsQuery.data ?? null
      if (rows === null) return null
      return rows.map(s => {
        const retried = retriedAt[s.id]
        const since = s.retiring_since ? Date.parse(s.retiring_since) : NaN
        return s.retiring && retried && !(since >= Date.parse(retried)) ? { ...s, retiring_since: retried } : s
      })
    },
    [slotsQuery.data, canReadWorktrees, retriedAt],
  )
  /* The permission gate outranks a stale error: react-query keeps a failed query's error after `enabled`
     goes false, which would show a red "failed to load" beside the "cannot see worktrees" banner. */
  const slotsError = canReadWorktrees && slotsQuery.isError ? 'Failed to load the pool slots' : ''

  /* The imperative re-read ("the server just changed something"). The gate is repeated because
     `enabled: false` does NOT stop an imperative `refetch()` in react-query 5. */
  const loadSlots = useCallback(async () => {
    if (!canReadWorktrees) return
    await slotsQuery.refetch()
  }, [canReadWorktrees, slotsQuery])

  /* Re-read after a node command, plus a short follow-up: `stack_up` can land its URL projection a beat
     after the command is marked done. */
  /* One follow-up timer (a new command replaces it), cancelled on unmount so it never fires after the
     panel is gone. */
  const followUpRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  useEffect(() => () => { if (followUpRef.current) clearTimeout(followUpRef.current) }, [])
  const refreshSoon = useCallback(() => {
    void loadSlots()
    if (followUpRef.current) clearTimeout(followUpRef.current)
    followUpRef.current = setTimeout(() => { followUpRef.current = null; void loadSlots() }, 1500)
  }, [loadSlots])

  /* ── The account behind each slot ──────────────────────────────────────────────────────────────────
     `/api/resources` carries each account's `config_dir` and quota; a slot joins by `account`. Gated on
     `system:view`, a REAL gate here: without it the panel shows every row and command but no account or
     quota badges. No imperative refetch exists except the gated one below. */
  const canReadResources = can('system:view')
  const resourcesQuery = useQuery({
    queryKey: RESOURCES_KEY,
    queryFn: getResources,
    refetchInterval: streamState === 'down' ? 10000 : false,
    enabled: canReadResources,
  })
  const accountsByDir = useMemo(() => {
    const map = new Map<string, ClaudeAccount>()
    for (const a of resourcesQuery.data?.accounts ?? []) if (a.config_dir) map.set(a.config_dir, a)
    return map
  }, [resourcesQuery.data])
  const accountFor = useCallback(
    (slot: Worktree) => (slot.account ? accountsByDir.get(slot.account) : undefined),
    [accountsByDir],
  )

  useEffect(() => {
    // `[]`, not null (null would spin forever); the banner explains the empty roster.
    if (!canReadProjects) {
      setProjects([])
      setProjectsError('')
    } else {
      void (async () => {
        try {
          const all = await listProjects()
          setProjects(all.sort((a, b) => a.name.localeCompare(b.name)))
          setProjectsError('')
        } catch {
          setProjectsError('Failed to load the project list')
        }
      })()
    }
  }, [canReadProjects])

  /* Live: `worktree` (declarations, recomposed Titles, node observations), `account` (quota crossing the
     wall) and `runner` (session facts) events re-read the list — the poll runs only while the stream is
     DOWN, so a missing entity would have no backstop.
     Debounced (150ms, 1s ceiling): a reconcile pass emits dozens of events at once; the ceiling stops a
     chatty stream from starving the read. */
  const { trigger: refetchSlots } = useDebouncedCallback(
    () => {
      void loadSlots()
      // Gated like `loadSlots`: `enabled: false` does NOT stop an imperative `refetch()`.
      if (canReadResources) void resourcesQuery.refetch()
    },
    150,
    1000,
  )
  useEventsSinceSeen(stream, (events, overrun) => {
    if (overrun || events.some(event => LIVE_ENTITIES.has(event.entity))) refetchSlots()
  })

  // The open drawer follows the live row; a row that has gone closes it.
  useEffect(() => {
    if (detail === null || slots === null) return
    const fresh = slots.find(s => s.id === detail.id)
    if (fresh === undefined) setDetail(null)
    else if (fresh !== detail) setDetail(fresh)
  }, [slots, detail])

  /* Nothing clears `nodeWarning` on a downgrade: it records a write that already happened. It clears at
     the next Add or Delete, like `notice` and `actionError`.
     The capability, mirrored in a ref so an AWAITED continuation reads the value current at resume (see
     `confirmSlot`). */
  const canDeleteRef = useRef(canDeleteSlots)
  useEffect(() => {
    canDeleteRef.current = canDeleteSlots
  }, [canDeleteSlots])

  /* ── DELETE — the request, and what its 200 can say ─────────────────────────────────────
     No `force`, no retry: every 2xx is a success, and two consequences must be reported:
       · `node_detail` (`node_erased: false`) — the directory survived; a WARNING in its own banner, never
         an error (red would invite deleting a slot that no longer exists).
       · `holder_ended` — something was killed; on the success line. Irreversible, so it must be on screen.
     The catch is reached by a 404 (unknown project/slot) or a 403 (lost `is_superadmin` before Yes). */
  async function handleDelete(slot: Worktree) {
    // Its own catch: the refusal reads through `refusalMessage` first, and the drawer closes.
    await run('Failed to delete the slot', async () => {
      try {
        setNodeWarning('')
        setNotice('')
        // `deleteSlot` is shared with the bulk Delete; this owns only the busy flag, notice and warning.
        const result = await deleteSlot(slot)
        const where = nodeLabelFor(slot.node_uuid)
        setNotice(
          `Deleted ${slotIdentity(slot)} — the slot is gone from the pool` +
          (result.node_erased
            ? ` and its directory on ${where} was deleted.`
            // Not "the directory was deleted": the warning banner is about to say otherwise.
            : ` and ${where} was asked to delete its directory.`) +
          (result.holder_ended
            ? ` It was held by ${result.holder_ended}, and that hold was ended with it: the session ledger ` +
              'is closed and a teardown was minted at its runner.'
            : ''),
        )
        /* The node's own words, naming slot and host: the next move is on that machine and the slot no longer
           exists in the console. */
        if (!result.node_erased) {
          setNodeWarning(
            `${slotIdentity(slot)} is de-declared, but its directory is still on ${where}: ` +
            `${result.node_detail || 'the node did not confirm the erase and gave no reason'}. ` +
            'Nothing in the console addresses that tree any more — clearing it up is a job on the host.',
          )
        }
        setDetail(null)
        await loadSlots()
      } catch (err) {
        /* The drawer closes on failure too: MUI's `Modal` marks everything outside it `aria-hidden`, and the
           banners render in the card. On success the row is gone anyway. */
        setDetail(null)
        // The server's sentence through both readers (`refusalMessage`, then `apiDetail`), then a generic.
        setActionError(refusalMessage(err) || apiDetail(err, 'Failed to delete the slot'))
      }
    })
  }

  /* The slot confirms' one shape (`components/ConfirmDialog`): the identity in mono (`slotIdentity`, as in
     the heading — not a second spelling), Yes/No, destructive. `verb` is Delete's and
     Forget's capability gate, at the CONTINUATION, not at render: `useConfirm` settles only when its HOST
     unmounts (`ConfirmDialog.tsx`), so removing just the dialog would hang the await; read through the REF,
     since this resumes after an await with a stale captured `canDeleteSlots`; and said rather than returned
     quietly (silence would read as a delete that happened). Release passes none: its route re-evaluates the
     capability on every request (`handleRelease`). */
  async function confirmSlot(slot: Worktree, title: string, message: string, verb?: 'Delete' | 'Forget'): Promise<boolean> {
    if (!await slotConfirm(danger('Yes', { title, identity: <Mono>{slotIdentity(slot)}</Mono>, message, cancelLabel: 'No' }))) return false
    if (verb && !canDeleteRef.current) {
      setActionError(`${verb} was not attempted — this account may no longer delete pool slots.`)
      return false
    }
    return true
  }

  // The confirm, now the only friction: the delete is physical and unconditional.
  async function confirmAndDelete(slot: Worktree) {
    const key = slotIdentity(slot)
    /* Names what is inside: the route ends the holder, and this dialog is the last warning.
       Withheld on a free slot. */
    const holder = isHeld(slot)
      ? ` It is ${occupancyHint(slot)}, and that hold is ENDED by this — the runner or session in the tree` +
        ' is torn down.'
      : ''
    // Names the ROW's node, and states the best-effort half: the row goes whatever the node manages.
    if (!(await confirmSlot(slot, 'Delete pool slot',
      `Delete ${key}? The slot is de-declared and its directory on ${nodeLabelFor(slot.node_uuid)} is ` +
      `deleted.${holder} Unpushed work is lost. The row goes regardless; the directory stays only if the ` +
      'node cannot be reached — you will be told. This cannot be undone; re-declaring the ' +
      'slot gives you a fresh clone, not this tree.', 'Delete'))) return
    await handleDelete(slot)
  }

  /* ── A destroy that needs a hand: Retry, and Forget (the row only) ─────────────────
     Both close the drawer, like Delete: the Modal would hide the banner from assistive tech. */
  async function handleRetryDestroy(slot: Worktree) {
    await run('Failed to retry the destroy', async () => {
      try {
        setNotice('')
        await retryDestroy(slot)
        setRetriedAt(prev => ({ ...prev, [slot.id]: new Date().toISOString() }))
        setNotice(`Destroy of ${slotIdentity(slot)} ordered again on ${nodeLabelFor(slot.node_uuid)}.`)
        setDetail(null)
        await loadSlots()
      } catch (err) {
        setDetail(null)
        setActionError(apiDetail(err, 'Failed to retry the destroy'))
      }
    })
  }

  async function confirmAndForget(slot: Worktree) {
    const key = slotIdentity(slot)
    const where = nodeLabelFor(slot.node_uuid)
    // Names the slot and what stays: Forget never reaches the node, unlike Delete.
    if (!(await confirmSlot(slot, 'Forget pool slot',
      `Forget ${key}? It leaves the console and its destroy is cancelled. Nothing is sent to ${where}: its files there are not removed.`,
      'Forget'))) return
    await run('Failed to forget the slot', async () => {
      try {
        setNotice('')
        await forgetSlot(slot)
        setNotice(`Forgot ${key} — its files on ${where} were not removed.`)
        setDetail(null)
        await loadSlots()
      } catch (err) {
        setDetail(null)
        setActionError(apiDetail(err, 'Failed to forget the slot'))
      }
    })
  }

  /* ── RELEASE — the request, its two outcomes, and the confirm below ───────────────
     Split like `handleDelete`/`confirmAndDelete` so the Force retry re-issues the REQUEST without
     re-asking. The confirm says what survives AND what the next dispatch does (`reset_to_base` is a
     `git reset --hard` with no stash path); the CP's live-session guard makes the dangerous case need a
     second answer.
     No capability ref (unlike Delete): the route re-evaluates `worktrees:edit` + the per-slot probe on
     every request and `force` never waives it, so a downgrade is refused server-side in its own words.
     API refusals are surfaced verbatim. */
  async function handleRelease(slot: Worktree, force = false) {
    // Its own catch: the refusal reads through `refusalMessage` first, and may offer the Force retry.
    await run('Failed to release the slot', async () => {
      try {
        setReleaseRefusal(null)
        setNotice('')
        // Another org's slot goes as that org, for this one request (`releaseSlot`'s `asOrg`).
        await releaseSlot(slot, force, otherOrgOf(slot.org))
        setNotice(releasedNotice(slot, nodeLabelFor(slot.node_uuid), force))
        setDetail(null)
        await loadSlots()
      } catch (err) {
        /* Both readers: `refusalMessage` keeps the live-session refusal's structured paragraph intact;
           `apiDetail` reads string details (404, 409). */
        const message = refusalMessage(err) || apiDetail(err, 'Failed to release the slot')
        setActionError(message)
        /* Retry offered on the refusal's `reason`, NEVER its status (`lib/erase.ts::releaseLiveSessionRefusal`):
           a 409 a force cannot resolve gets none. The drawer closes (the Modal would hide the
           banner from assistive tech); the banner names its slot. */
        const live = force ? null : releaseLiveSessionRefusal(err)
        if (live) {
          setDetail(null)
          setReleaseRefusal({ slot, session: live.session_status })
        }
      }
    })
  }

  async function confirmAndRelease(slot: Worktree) {
    if (!(await confirmSlot(slot, 'Release pool slot', releaseConfirmMessage(slot, nodeLabelFor(slot.node_uuid))))) return
    await handleRelease(slot)
  }

  /* Memoised: a fresh `[]` would re-fire the view report, which the page answers with `setState` — a
     render loop. */
  const all = useMemo(() => slots ?? [], [slots])

  /* Filter, then arrange: `matchesWorktreeFilters` decides which rows exist, `buildWorktreeRows` places
     the SURVIVORS so bar counts are reachable counts (that module explains why). */
  const visible = useMemo(
    () => all.filter(s => matchesWorktreeFilters(s, filters, nodeLabelFor)),
    [all, filters, nodeLabelFor],
  )
  const rows = useMemo(
    () => buildWorktreeRows(visible, groupBy, nodeLabelFor, isCollapsed),
    [visible, groupBy, nodeLabelFor, isCollapsed],
  )
  /* Rows that reached the DOM, read off the emitted list (a folded group emits no rows) so "on screen"
     means one thing everywhere. */
  const onScreen = useMemo(
    () => rows.flatMap(row => (row.kind === 'slot' ? [row.slot] : [])),
    [rows],
  )
  /** Flat outline only: in either grouped outline the heading already says Project and Node. */
  const flatColumns = groupBy === GROUP_BY_FLAT

  /* ── What goes back up ────────────────────────────────────────
     The page's Node/Project filter menus are built from these facets (it never sees the slot list). Not
     from the `nodes` roster: slots can sit on nodes the roster lacks, and the roster lists slotless nodes.
     UNFILTERED, DELIBERATELY — taken over `all`, not `visible`, or an active filter would collapse its own
     options and the operator could never widen back out. Ordered by project name and resolved node LABEL,
     matching `buildWorktreeRows`' bars. */
  const facets = useMemo(() => ({
    nodeIds: [...new Set(all.map(s => s.node_uuid))].sort(
      (a, b) => nodeLabelFor(a).localeCompare(nodeLabelFor(b)) || a.localeCompare(b),
    ),
    projects: [...new Set(all.map(s => s.project))].sort((a, b) => a.localeCompare(b)),
  }), [all, nodeLabelFor])

  /* Reported from an effect, never during render (the page answers with `setState`). Memoised so it fires
     on a VIEW change; `onViewChange` is a stable setter, so it sits in the deps honestly. */
  /* Each node's NAME as its slots carry it, by uuid: the page's label fallback for a node the roster lacks
     (no `nodes:view`). Memoised on `all` alone, so it moves only when the slots do. */
  const nodeNames = useMemo(
    () => Object.fromEntries(all.map(s => [s.node_uuid, s.node_id])) as Record<string, string>, [all])
  const view = useMemo(
    () => ({ visible, onScreen, total: all.length, nodeIds: facets.nodeIds, nodeNames, projects: facets.projects }),
    [visible, onScreen, all, facets, nodeNames],
  )
  useEffect(() => {
    onViewChange(view)
  }, [view, onViewChange])

  const hasSlots = all.length > 0

  /* ── Select all, over the rows ON SCREEN — `onScreen`, NOT `visible` ──────────────────────────────
     Filter survivors include folded groups' rows, which once sent bulk verbs to rows the operator could
     not see. The page keys `selection` on `onScreen` (reported above), so it prunes to it and folding a
     group drops its picks. */
  const { selected, toggle: toggleSelected, toggleAll: toggleSelectAll, allShownSelected, someShownSelected } = selection

  // Composed in `poolSlot.ts` beside the group bar's summary (see `poolRollup`).
  const rollup = useMemo(() => poolRollup(all), [all])
  /* How many nodes the counts span — the headline must say so on a fleet-wide panel. Counted off the
     ROWS, never the roster (which may be unreadable, and includes slotless nodes). */
  const nodeSpan = useMemo(() => new Set(all.map(s => s.node_id)).size, [all])

  /* ── The console, and the order its chevrons step through ────────────────────────────────────────
     Only this table knows "next", so `useConsoleNav` is registered by the page owning the slot list.
     Live-session slots only (a free one has nothing to attach to), taken off the EMITTED rows so the
     chevrons never step into a folded group. */
  const { request: consoleRequest, open: openConsole } = useConsoleDock()
  const consoleOrder = useMemo(
    () => rows.flatMap(row => (row.kind === 'slot' && hasLiveSession(row.slot) ? [row.slot] : [])),
    [rows],
  )
  const consoleIndex = consoleRequest
    ? consoleOrder.findIndex(s =>
        s.project_id === consoleRequest.projectId && s.number === consoleRequest.number && s.node_uuid === consoleRequest.nodeId)
    : -1

  const openSlotConsole = useCallback((slot: Worktree) => {
    openConsole({ ...slotConsoleRequest(slot), kind: 'clauded' })
  }, [openConsole])

  function jumpConsole(delta: number) {
    if (!consoleRequest || consoleIndex === -1) return
    const target = consoleOrder[consoleIndex + delta]
    if (!target) return
    openSlotConsole(target)
  }

  /* `onPrev`/`onNext` must be stable: `useConsoleNav` re-registers (setting provider state) on change, which
     would loop. The latest `jumpConsole` is read through a ref. */
  const jumpConsoleRef = useRef(jumpConsole)
  jumpConsoleRef.current = jumpConsole
  const onConsolePrev = useCallback(() => jumpConsoleRef.current(-1), [])
  const onConsoleNext = useCallback(() => jumpConsoleRef.current(1), [])
  useConsoleNav({
    onPrev: onConsolePrev,
    onNext: onConsoleNext,
    hasPrev: consoleIndex > 0,
    hasNext: consoleIndex !== -1 && consoleIndex < consoleOrder.length - 1,
  })

  /* ── New session on a MicroVM node ─────────────────
     The CP starts a VM for this person (`routes/console.py::console_new_session`) and the console opens on it;
     no slot is held. The declare bar (`canDeclareSlots`) AND the typing tier (`canDriveSession`), since the
     open is two-way. */
  const vmProjects = useMemo(() => (projects ?? []).filter(p => p.on_microvm === true), [projects])

  /* The new session's VM, until it runs (the console opens) or ends (its node's sentence, in operator words). One
     wait at a time: a newer session replaces it, and an answer about the replaced VM is dropped (`vmStartRef`). */
  const [vmStart, setVmStart] = useState<{ made: NewSession & { vm_id: string }; name: string } | null>(null)
  const vmStartRef = useRef(vmStart)
  vmStartRef.current = vmStart

  function openNewSession(made: NewSession & { vm_id: string }) {
    openConsole({
      project: made.project, projectId: made.project_id, number: made.number, nodeId: made.node_uuid, kind: 'clauded',
      // A VM session holds no slot: the relay attaches its VM by `view` (number 0), as a task VM's
      // (`routes/console.py::_task_vm_console`'s opener arm), and the VM's own session is already running.
      view: `vm:${made.vm_id}`, hadLiveSession: true,
      // The caller opened the VM, so it is theirs to drive (`authz.worktree_editable_clause`'s owner arm).
      mayEdit: true,
    })
  }

  async function startVmSession(projectId: number, name: string) {
    await run(`Failed to start a session in ${name}`, async () => {
      setNotice('')
      setVmStart(null)
      const made = await post<NewSession>(`/api/console/${projectId}/new-session`, {})
      await loadSlots()
      // A VM session opens once its VM runs: a pane opened before then could only say no session is there.
      if (made.vm_id) setVmStart({ made: { ...made, vm_id: made.vm_id }, name })
      // No VM free on the project's nodes: the console only attaches, so nothing is opened.
      else setActionError(`No MicroVM node has room for a session in ${name}.`)
    })
  }

  usePoll(async () => {
    const waiting = vmStartRef.current
    if (!waiting) return
    const { made, name } = waiting
    const stillWaiting = () => vmStartRef.current?.made.vm_id === made.vm_id
    try {
      const vm = await get<{ state: string; reason: string | null }>(
        `/api/console/${made.project_id}/vms/${encodeURIComponent(made.vm_id)}`)
      if (!stillWaiting() || vm.state === 'provisioning') return
      setVmStart(null)
      if (vm.state === 'running') openNewSession(made)
      else setActionError(`The session in ${name} could not start: ${vm.reason ?? `its VM ended (${vm.state}) before it ran.`}`)
    } catch (err) {
      // Only a 404 ends the wait (the VM is gone); a dropped read is retried on the next tick.
      if (!stillWaiting() || !(err instanceof ApiError && err.status === 404)) return
      setVmStart(null)
      setActionError(`The session in ${name} could not start: its VM is gone.`)
    }
  }, vmStart ? VM_START_POLL_MS : null)

  const newSessionControl = canDeclareSlots && canDriveSession && vmProjects.length > 0 ? (
    <OverflowMenu
      button={{ label: 'New session', variant: 'outlined' }}
      size="small"
      busy={busy}
      testId="pool-new-session"
      sx={tapTargetSx}
      /* Another org's project: the open is a write, which binds the operator's own org and 403s
         (`authz/enforce.py::OtherOrgDenial`), and this page is the operator's alone, so the org's seat cannot reach
         it. One press goes where that seat can: this tab acts as the org and opens a new chat on the
         project — the session that org's own people would start. The label names the switch before the press. */
      items={vmProjects.map(p => {
        const otherOrg = otherOrgOf(p.org)
        if (otherOrg) {
          const label = `${p.name} — chat as ${otherOrg.name}`
          return {
            key: String(p.id), label, ariaLabel: label, testId: 'pool-new-session-project',
            onClick: () => {
              enterActAs(otherOrg)
              navigate('/tasks/new-chat', { state: { boardProjectId: p.id } satisfies NewTaskNavState })
            },
          }
        }
        return {
          key: String(p.id), label: p.name, ariaLabel: p.name, testId: 'pool-new-session-project',
          onClick: () => void startVmSession(p.id, p.name),
        }
      })}
    />
  ) : null

  /* ── One slot, one feed row (`ui/feed/FeedList` draws it) ──────────────────────────────────────────
       · The row body opens the drawer (`onRowClick`); `FeedList` guards control clicks, ⋯-sheet clicks
         that bubble back through React, and text selection. `Details` in the ⋯ is the second home.
       · The Title link is the only link.
       · `disclosure` qualifies `slotIdentity` (which repeats across nodes) with the node. Specs address rows
         by `[aria-label^="<identity> — "]`, so the identity stays the PREFIX. */
  function slotRow(slot: Worktree): FeedRowSpec {
    const identity = slotIdentity(slot)
    const nodeLabel = nodeLabelFor(slot.node_uuid)
    const disclosure = `${identity} — on ${nodeLabel}`
    const rowTitle = resolveTitle(slot)
    const account = accountFor(slot)
    const quotaWarned = showQuotaBadge(slot, account)
    const quotaWords = `${account?.name || account?.email || 'this slot’s account'} is near a quota limit`
    /* Names the task (the reader's next gesture). The null arm matters: `task_id` can dangle at a deleted
       row, and the drawer's `Waiting on` row is gated on the same pair — no `task #null`. */
    const awaitingWords = slot.task_id == null
      ? `this slot is ${AWAITING_ANSWER_WORDS}`
      : `task #${slot.task_id} is ${AWAITING_ANSWER_WORDS}`
    const lifecycle = poolSlotState(slot)
    const state = stateSignal(slot)
    const stack = stackSignal(slot)
    const session = sessionSignal(slot)
    const age = sessionAge(slot)
    // The offline verdict is per ROW: neighbours can be on different machines.
    const rowOffline = isOffline(slot.node_uuid)
    const mark = slotMark(slot, rowOffline, nodeLabel)
    // Attach only: a console opens on a live session, never a new one.
    const canConsole = !rowOffline && hasLiveSession(slot)
    // An address is not a running server, and an unreachable node has none.
    const webHref = slot.stack_state === 'up' && !rowOffline ? slotWebHref(slot) : undefined
    const picked = selected.has(slot.id)
    /* Two denials, two sentences (node down vs nothing to attach to), on the Session slot's `title`. */
    const consoleDenial = rowOffline
      ? `${nodeLabel} is offline`
      : 'no session is running'

    /* Markers, not slots (each is rare): `◌` lifecycle (declared, not built), `⚠` quota, `!` a hold
       awaiting the reader. Announced via `ariaLabel`. */
    const markers: FeedMarker[] = []
    if (lifecycle === 'declared') {
      markers.push({ key: 'declared', glyph: '◌', label: state.label, title: state.hint, testId: 'slot-state-declared' })
    }
    if (quotaWarned) markers.push({ key: 'quota', glyph: '⚠', label: quotaWords, testId: 'slot-quota-badge' })
    /* `!` — the hold awaits the READER: the row's ONE `!` (the Session cell says it in amber only), hugged
       to the Title it qualifies rather than a column at the title cell's far end, beside Git.
       A marker because the 60px Session cell is gated away below 900px. `!` not `?`: `?` already means "no
       measurement" in Stack/Git. No `title`: the feed falls back to `label` (`AWAITING_ANSWER_WORDS`). */
    if (isAwaitingAnswer(slot)) {
      markers.push({ key: 'awaiting', glyph: '!', label: awaitingWords, testId: 'slot-awaiting-answer', hug: true, tone: 'warning' })
    }

    const slots: FeedSlot[] = []
    if (flatColumns) {
      slots.push({
        key: 'project',
        width: SLOT_W.project,
        gate: 'poolProject',
        align: 'left',
        testId: 'col-slot-project',
        title: slot.project,
        node: <Box component="span" data-testid="slot-project-text" sx={SLOT_TEXT_SX}>{slot.project}</Box>,
      })
      slots.push({
        key: 'node',
        width: SLOT_W.node,
        gate: 'poolNode',
        align: 'left',
        testId: 'col-slot-node',
        /* The resolved label, never `node_id` (an opaque uuid); the raw id stays on `title`. */
        title: slot.node_id,
        node: <Box component="span" data-testid="slot-node-text" sx={SLOT_TEXT_SX}>{nodeLabel}</Box>,
      })
    }
    slots.push({
      key: 'git',
      width: SLOT_W.git,
      gate: 'poolMeta',
      mono: true,
      testId: 'col-slot-git',
      /* A folder slot keeps the column (alignment) but renders nothing in it: no git state to report. */
      title: isFolderSlot(slot) ? undefined : gitWords(slot),
      node: isFolderSlot(slot) ? null : <GitCell slot={slot} />,
    })
    slots.push({
      key: 'stack',
      width: SLOT_W.stack,
      gate: 'poolMeta',
      align: 'left',
      testId: 'col-slot-stack',
      title: stack.hint,
      node: <SignalText testId="slot-stack-word" signal={stack} />,
    })
    slots.push({
      key: 'session',
      width: SLOT_W.session,
      /* Beside an open console the pool narrows: the session word drops first, with Node, before Project and
         the Title give up width. The drawer still says it. */
      gate: flatColumns && consoleRequest ? 'poolNode' : 'poolId',
      align: 'left',
      testId: 'col-slot-session',
      title: canConsole ? session.hint : `${session.hint} — no console here: ${consoleDenial}`,
      node: <SignalText testId="slot-session-word" signal={session} />,
    })
    slots.push({
      key: 'age',
      width: SLOT_W.age,
      mono: true,
      dim: true,
      testId: 'col-slot-age',
      // Session run time, NOT idle time; the drawer shows both labelled.
      title: age.hint,
      node: age.label,
    })
    // The one control on the row; the ⋯'s `Select` carries the same verb. Named after the slot, node included.
    slots.push(selectSlot('col-slot-select',
      <SelectRowBox testId="slot-select" checked={picked} onChange={() => toggleSelected(slot.id)} name={disclosure} />))

    /* The ⋯, ordered as reached for: look (Details, console), leave (Launch), then Select.
       Stack commands, Logs, Finder, Cursor, clipboard, Release and Delete stay in the drawer. */
    const actions: OverflowItem[] = [
      {
        key: 'details',
        icon: '▤',
        label: 'Details',
        ariaLabel: `Show slot details for ${disclosure}`,
        onClick: () => setDetail(slot),
      },
    ]
    if (canConsole) {
      actions.push({
        key: 'console',
        icon: '🪟',
        label: 'Console',
        ariaLabel: `Open the console on ${disclosure}`,
        onClick: () => openSlotConsole(slot),
      })
    }
    if (webHref) {
      actions.push({
        key: 'launch',
        icon: '🌐',
        label: 'Launch',
        // The URL is in the name: "Launch" alone does not say which address.
        ariaLabel: `Launch ${disclosure} — open ${webHref} in a new tab`,
        href: webHref,
      })
    }
    actions.push({
      key: 'select',
      icon: picked ? '☑' : '☐',
      label: picked ? 'Deselect' : 'Select',
      ariaLabel: `${picked ? 'Deselect' : 'Select'} ${disclosure}`,
      onClick: () => toggleSelected(slot.id),
    })

    /* The whole accessible name (it makes the disc and markers decorative); everything the ink cannot say. */
    const ariaLabel = [
      disclosure,
      rowTitle,
      mark.label,
      lifecycle === 'declared' ? state.label : null,
      isAwaitingAnswer(slot) ? awaitingWords : null,
      quotaWarned ? quotaWords : null,
    ].filter(Boolean).join(' — ')

    return {
      key: slot.id,
      indicator: {
        tone: mark.tone,
        glyph: mark.glyph,
        label: mark.label,
        // WHO holds the slot — the half the disc folds away.
        title: occupancyHint(slot),
        // The PULSE (and the row's live wash) is `busy` only; the mark is any live session.
        live: slot.session_status === 'busy',
        data: { state: lifecycle, occupancy: slot.state },
      },
      title: (
        <>
          <Tip title={identity}><Box
            component="span"
            data-testid="slot-id-text"
            {...(flatColumns && { 'data-project-prefix': slot.project })}
            sx={[monoSx, SLOT_NUMBER_SX, SLOT_PROJECT_PREFIX_SX]}
          >
            {slotNumberLabel(slot)}
          </Box></Tip>
          <SlotMarks slot={slot} />
          {/* `—` is ink only: the accessible name keeps `free`, the one place the disc's occupancy is said aloud. */}
          {titleShown(rowTitle)}
        </>
      ),
      titleText: `${disclosure} — ${rowTitle === FREE_TITLE ? rowTitle : titleShown(rowTitle)}`,
      ariaLabel,
      /* The Title is CP-owned and arrives resolved (`apps/api/app/slot_title.py`); a live task makes the naming
         region the deep link. */
      href: slot.task_active && slot.task_id !== null ? taskDeepLinkPath(slot.task_id) : null,
      markers,
      slots,
      actions,
      onRowClick: () => setDetail(slot),
      rowClickLabel: `Show slot details for ${disclosure}`,
      // The row's own oracles.
      data: {
        slot: slot.id,
        state: lifecycle,
        occupancy: slot.state,
        'session-status': slot.session_status ?? '',
        'node-offline': rowOffline ? 'true' : undefined,
      },
    }
  }

  /* ── The outline, as feed sections ───────────────────────────────────────────────────────────────
     `lib/worktreeGrouping.ts::buildWorktreeRows` decides rows, order and depth; a folded group emits NO
     rows. A two-level outline is a flat run of sections (outer = a section with no rows); `data-section`
     carries the key (inner = parent key + `/`).
     The level is said twice: `depth` drives `FeedList`'s ladder (`headLevel`); the `↳` is a SHAPE that
     survives greyscale, speech and `textContent`, where four specs read the level. */
  const sections: FeedSection[] = []
  let bucket: FeedSection | null = null
  for (const row of rows) {
    if (row.kind === 'slot') {
      if (bucket === null) {
        // The flat outline emits no bars: one unlabelled run (per `FeedSection`'s note).
        bucket = { key: 'slots', rows: [] }
        sections.push(bucket)
      }
      bucket.rows.push(slotRow(row.slot))
      continue
    }
    bucket = {
      key: row.key,
      label: row.depth === 1 ? `↳ ${row.label}` : row.label,
      labelAside: <OrgQualifier org={row.org} />,
      depth: row.depth,
      /* The group's count before collapse, never the emitted rows: a shut bar must not read `0`. */
      count: row.count,
      collapsed: row.collapsed,
      onToggle: () => onToggleGroup(row.key, row.collapsed),
      rows: [],
    }
    sections.push(bucket)
  }

  /* The column header row: the Git cell's glyphs (`↑2 ✎`, `—`, `?`) mean nothing without a name over them. Each
     header rides its slot's width and gate (`FeedList`); the select slot is unnamed. */
  const poolColumns = {
    title: { key: 'title', label: 'Slot' },
    slots: [
      ...(flatColumns ? [{ key: 'project', label: 'Project' }, { key: 'node', label: 'Node' }] : []),
      { key: 'git', label: 'Git' },
      { key: 'stack', label: 'Stack' },
      { key: 'session', label: 'Session' },
      { key: 'age', label: 'Age', ariaLabel: 'Session age' },
    ],
  }

  /* Select-all over the rows ON SCREEN, naming its count. On the header line, still over the column its ticks
     land in (`SELECT_ALL_SX` reproduces the row's geometry). */
  const selectAllCluster = onScreen.length > 0 ? (
    /* `role="group"`: outside `FeedList`'s table roles, a bare `<div>` of controls announces as nothing. */
    <Box role="group" aria-label="Select slots" sx={SELECT_ALL_SX}>
      <Box component="span">{`${onScreen.length} shown`}</Box>
      <Box sx={SELECT_ALL_COLUMN_SX}>
        <SelectAllBox
          testId="pool-select-all"
          allSelected={allShownSelected}
          someSelected={someShownSelected}
          onChange={toggleSelectAll}
          shown={`${onScreen.length} slot(s) shown`}
        />
      </Box>
      <Box aria-hidden="true" sx={ACTIONS_SPACER_SX} />
    </Box>
  ) : null

  return (
    <Card variant="outlined">
      <CardContent sx={CARD_CONTENT_SX}>
        {/* The page's verbs ride the page header after the page's toolbar, the primary
            filled, as on Brains and Projects; `PageHeader` portals them. Both portals append to the
            strip, so this one lands after `pages/Worktrees.tsx`'s toolbar because that header mounts first and
            never unmounts (`e2e/worktrees-mobile.spec.ts` pins the order). */}
        <PageHeader
          testId="pool-actions"
          actions={newSessionControl ?? undefined}
        />
        {/* ── One header line (rollup, select-all) instead of stacked rows. No name: the Slots tab already
            names the pool. Wraps rather than crowds at 320px. */}
        {(hasSlots || selectAllCluster) && (
        <Box sx={POOL_HEADER_SX}>
          <Box sx={POOL_HEADING_GROUP_SX}>
            {hasSlots && (
              <Typography data-testid="pool-rollup" variant="caption" component="span" sx={{ color: 'text.secondary' }}>
                {rollup} · on {countOf(nodeSpan, 'node')}
              </Typography>
            )}
          </Box>
          {selectAllCluster}
        </Box>
        )}

        {/* Not an error: an empty list would falsely read as "the fleet has no pool". */}
        {!canReadWorktrees && (
          <Banner severity="info">
            This account cannot see slots, so none are shown.
          </Banner>
        )}
        {!canReadProjects && (
          <Banner severity="info">
            This account cannot see projects, so no session can be opened on one.
          </Banner>
        )}
        {slotsError && <Banner severity="error">{slotsError}</Banner>}
        {projectsError && <Banner severity="error">{projectsError}</Banner>}
        {actionError && <Banner severity="error">{actionError}</Banner>}
        {/* A successful write's node consequence: Delete — the directory survived; Add — the clone will
            authenticate as someone with no GitHub account. One banner (`slot-action-warning`): the two are
            mutually exclusive. `warning`, not `error`: the request succeeded. No dismiss; the next write
            clears it. */}
        {nodeWarning && <Banner severity="warning" testId="slot-action-warning">{nodeWarning}</Banner>}
        {/* The Release refusal's second question: what forcing costs, and WHEN (the CP's words are in the
            banner above). The cost is deferred: `reset_to_base` runs `git reset --hard` on a later,
            unwatched dispatch with no stash. Not capability-gated: the route's per-slot probe is
            re-evaluated on the retry and never waived by `force`. */}
        {releaseRefusal && (
          <Banner severity="warning">
            <Typography variant="body2" component="div">
              Release {slotIdentity(releaseRefusal.slot)} anyway? Forcing ends the hold now and leaves the
              {releaseRefusal.session ? ` ${releaseRefusal.session} ` : ' '}session running, but the slot
              becomes free — and the next dispatch into it resets the tree onto its base branch, so any
              uncommitted work in it is lost, with nothing stashed first.
            </Typography>
            <Box sx={{ display: 'flex', flexWrap: 'wrap', alignItems: 'center', gap: 1, mt: 0.5 }}>
              <Button
                variant="contained"
                color="error"
                onClick={() => void handleRelease(releaseRefusal.slot, true)}
                disabled={busy}
                sx={tapTargetSx}
              >
                Force release
              </Button>
              <Button
                variant="text"
                onClick={() => setReleaseRefusal(null)}
                disabled={busy}
                sx={tapTargetSx}
              >
                Keep the hold
              </Button>
            </Box>
          </Banner>
        )}
        {notice && <Banner severity="success">{notice}</Banner>}
        {vmStart && (
          <Banner severity="info" testId="pool-vm-starting">
            Starting a VM for a session in {vmStart.name}. The console opens when it runs.
          </Banner>
        )}

        {slots === null ? (
          <LoadingState rows={3} />
        ) : !hasSlots ? (
          /* Only a READER is told there are none (a denied account's `[]` would otherwise become a false
             claim). */
          canReadWorktrees ? <EmptyState title="No pool slots" scoped /> : null
        ) : visible.length === 0 ? (
          /* A filter matching nothing is not an empty pool. No `onClear`: the filters are the page's (URL);
             the toolbar can clear them. */
          <NoMatchesState title="No slots match" />
        ) : (
          /* One slot = one feed row; `FeedList` owns every pixel, band, gate, guard, ⋯ and grid role. */
          <FeedList
            ariaLabel="Pool slots"
            testId="pool-slot-list"
            sections={sections}
            columns={poolColumns}
          />
        )}
        {detail && (
          <SlotDetail
            /* Keyed per slot, so one slot's drawer state never carries into another's. */
            key={detail.id}
            slot={detail}
            nodeLabel={nodeLabelFor(detail.node_uuid)}
            nodeOffline={isOffline(detail.node_uuid)}
            account={accountFor(detail)}
            onClose={() => setDetail(null)}
            onDelete={s => void confirmAndDelete(s)}
            onRelease={s => void confirmAndRelease(s)}
            onRetryDestroy={s => void handleRetryDestroy(s)}
            onForget={s => void confirmAndForget(s)}
            busy={busy}
            canDelete={canDeleteSlots}
            canEdit={canEdit}
            canDriveSession={canDriveSession}
            onRun={refreshSoon}
            /* The console opens beside the page, and a drawer left open over it hid its header controls: the drawer
               hands over to it. */
            onOpenConsole={s => { setDetail(null); openSlotConsole(s) }}
            canSetCloneOwner={canDeclareSlots}
          />
        )}
        {slotConfirmDialog}
      </CardContent>
    </Card>
  )
}
