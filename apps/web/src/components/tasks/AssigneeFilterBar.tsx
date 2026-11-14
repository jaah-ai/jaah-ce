/* The `/tasks` board's ASSIGNEE PICKER — author badges, one per person with tasks here, toggling the
 * assignee filter (Jira's board avatar strip). On the page header's own row via `ui/PageHeader`'s
 * `titleAside` (whose board it is sits beside the project name).
 *
 * A SHORTCUT, NEVER THE ONLY HOME: the same filter lives in `TaskToolbar.tsx`'s Filter & view menu (what a
 * phone gets, and what the badge counts) — operator's call, "Both — panel + strip".
 *
 *   · MULTI-SELECT: two picks show the UNION; none is the unfiltered board (no "Everyone" entry). Each badge
 *     is an `aria-pressed` TOGGLE, never `aria-current`.
 *   · THE CURRENT USER IS ALWAYS FIRST AND PRESENT, even at zero (stated in the tooltip). The rule lives in
 *     `lib/taskAssignees.ts`, applied by the page, so the panel's Assignee section matches.
 *   · THREE BADGES, THEN `+N`: the current user, then the picked, then the rest (`splitAssigneeStrip`), so
 *     bounded width keeps the title's line safe (no shredded title) and a pick stays in view; the `+N`
 *     lights when a pick is still in the overflow, whose list has a filter field.
 *   · AT EVERY WIDTH. Below 600px each 26px disc keeps its size but takes a 44px hit area (`TAP_SX`) and the
 *     gap widens so neighbouring hit areas never overlap.
 *
 * The badge is the Activity Feed's (`./AuthorBadge`), so a person is the same letters and hue everywhere;
 * this file owns only the pressable frame.
 */
import { useState } from 'react'
import Box from '@mui/material/Box'
import InputBase from '@mui/material/InputBase'
import Popover from '@mui/material/Popover'
import Typography from '@mui/material/Typography'
import { alpha } from '@mui/material/styles'
import type { Theme } from '@mui/material/styles'
import { authorHue } from '../../lib/authorBadge'
import { countOf } from '../../lib/humanize'
import { assigneeLabel, rosterOffersAChoice, splitAssigneeStrip, toggleAssigneeId } from '../../lib/taskAssignees'
import { matchesAny } from '../../ui/FilterRow'
import { TAP_FLOOR_DOWN } from '../../ui/listStyles'
import { FONT_SIZE, FONT_WEIGHT, LETTER_SPACING, LINE_HEIGHT, RADIUS, SIZE } from '../../theme'
import type { TaskAssignee } from '../../types'
import AuthorBadge from './AuthorBadge'
import { ITEM_SX, LIST_SX, MENU_SX, NAME_SX, NOTE_SX, SEARCH_SX } from './peopleMenuSx'
import { NO_AUTOFILL_INPUT } from '../../ui/noAutofill'
import Tip from '../../ui/Tip'

/* Badges before the `+N`, counting the current user's — the operator's number. */
export const ASSIGNEE_STRIP_CAP = 3

/* The disc: the largest that still reads as a row of avatars, not buttons. */
const DISC = 26

/* The 44px tap floor below `ui/listStyles.ts`'s 600px boundary: the disc stays 26px, an invisible `::after`
   (`SIZE.tapTarget` square, centred) reaches 9px past each OUTER edge, and the gap is 18px (3 units) so two
   hit areas meet without overlapping. */

/* No margins: the header row's `columnGap` spaces it. `flex: '0 0 auto'`: free space belongs to the title. */
const STRIP_SX = {
  display: 'flex',
  alignItems: 'center',
  flex: '0 0 auto',
  gap: 0.5,
  flexWrap: 'nowrap',
  [TAP_FLOOR_DOWN]: { gap: 3, px: 1.5 },
} as const

/* A `<button>`, not a checkbox (a box beside each avatar would compete with the ring). */
const DISC_SX = {
  display: 'inline-flex',
  alignItems: 'center',
  justifyContent: 'center',
  flex: '0 0 auto',
  width: DISC,
  height: DISC,
  p: 0,
  borderRadius: RADIUS.round,
  border: '1px solid',
  cursor: 'pointer',
  /* Overrides the UA's `buttonface` grey (the `+N` takes no tint). */
  backgroundColor: 'transparent',
  transition: 'outline-color 120ms, border-color 120ms, opacity 120ms, background-color 120ms',
  '&:focus-visible': { outline: '2px solid', outlineColor: 'primary.main', outlineOffset: 2 },
} as const

/* The strip's buttons only (the popover's discs are decoration inside a full-width row). */
const TAP_SX = {
  position: 'relative',
  [TAP_FLOOR_DOWN]: {
    '&::after': {
      content: '""',
      position: 'absolute',
      top: '50%',
      left: '50%',
      width: SIZE.tapTarget,
      height: SIZE.tapTarget,
      transform: 'translate(-50%, -50%)',
    },
  },
} as const

/* The disc tinted in the author's own hue (0.12 alpha): off a table column, bare coloured letters read as
   tags, not PEOPLE. `alpha()` over the theme's paper follows a theme flip; the letters keep full strength. */
const DISC_TINT_SX = (name: string) => (theme: Theme) => {
  const hue = authorHue(name)
  return {
    backgroundColor: alpha(hue.light, 0.12),
    ...theme.applyStyles('dark', { backgroundColor: alpha(hue.dark, 0.14) }),
  }
}

/* The picked ring in the author's own hue (a single accent would erase "different people"). Never colour
   alone: `aria-pressed`, and unpicked badges dim. */
const DISC_PICKED_SX = (name: string) => (theme: Theme) => {
  const hue = authorHue(name)
  return {
    borderColor: hue.light,
    outline: '1px solid',
    outlineColor: hue.light,
    ...theme.applyStyles('dark', {
      borderColor: hue.dark,
      outlineColor: hue.dark,
    }),
  }
}

const DISC_IDLE_SX = { borderColor: 'transparent' } as const

/* Unpicked badges dim while any pick is on — dimmed, never hidden (they still widen the filter). */
const DISC_DIMMED_SX = { opacity: 0.45, '&:hover': { opacity: 1 } } as const

/* The badge's geometry inside the disc (the host half of `AuthorBadge`'s split). */
const BADGE_SX = {
  fontSize: FONT_SIZE.xs,
  fontWeight: FONT_WEIGHT.bold,
  letterSpacing: LETTER_SPACING.none,
  lineHeight: LINE_HEIGHT.tight,
  userSelect: 'none',
} as const

/* The `+N` is shaped like a disc: a member of the set, not a tacked-on control. */
const MORE_SX = {
  ...DISC_SX,
  /* Neutral ground: it stands for many people, so no one person's hue. */
  backgroundColor: 'action.hover',
  borderColor: 'divider',
  color: 'text.secondary',
  fontSize: FONT_SIZE.xxs,
  fontWeight: FONT_WEIGHT.bold,
  lineHeight: LINE_HEIGHT.tight,
} as const

/* The `+N`'s picked ring is `primary`: it can only honestly say "a pick is in here". */
const MORE_PICKED_SX = (theme: Theme) => ({
  borderColor: theme.palette.primary.main,
  outline: '1px solid',
  outlineColor: theme.palette.primary.main,
  color: theme.palette.primary.main,
})

const MENU_COUNT_SX = { flex: '0 0 auto', color: 'text.secondary' } as const

/** One badge's tooltip and name: identity, count, what pressing does. "IN SCOPE", not "on this board":
 *  the roster takes no date window (`buildTaskAssigneesUrl`), so a tight period can show nothing while the
 *  count still reads 9 — and an avatar that vanished could not widen the filter that hid it. */
function assigneeHint(assignee: TaskAssignee, currentUserId: string | null, picked: boolean): string {
  const count = countOf(assignee.task_count, 'task')
  return `${picked ? 'Stop filtering by' : 'Filter by'} ${assigneeLabel(assignee, currentUserId)} — ${count} in scope`
}

export default function AssigneeFilterBar({
  assignees,
  currentUserId,
  selectedIds,
  onChange,
}: {
  /* The roster (`GET /api/tasks/assignees`, current project scope), ordered once by the page through
     `lib/taskAssignees.ts` so strip and panel agree. Not derived from loaded rows: the board is a lazy
     window. */
  assignees: TaskAssignee[]
  /* Only for "(you)" (`assigneeLabel` in `lib/taskAssignees.ts`, shared with the panel). `null` before
     `/api/auth/me` resolves: everyone is named plainly. */
  currentUserId: string | null
  /* Picked ids; `[]` is the off state (no "all" sentinel). */
  selectedIds: string[]
  onChange: (ids: string[]) => void
}) {
  const [moreAnchor, setMoreAnchor] = useState<HTMLElement | null>(null)
  const [search, setSearch] = useState('')

  /* No choice, no strip (and no panel section): `rosterOffersAChoice` owns why. */
  if (!rosterOffersAChoice(assignees)) return null

  const { shown, overflow } = splitAssigneeStrip(assignees, currentUserId, selectedIds, ASSIGNEE_STRIP_CAP)
  const picked = new Set(selectedIds)
  const anyPicked = picked.size > 0
  const overflowPicked = overflow.some(assignee => picked.has(assignee.id))

  /* Empty field: the popover lists the people the cap hid. Typed: it searches the WHOLE roster — answering
     "no match" for a name visible on the strip reads as broken search. On-strip matches are drawn again
     as pressable rows. */
  const listed = search.trim()
    ? assignees.filter(assignee => matchesAny(search, assignee.name))
    : overflow

  return (
    <>
      <Box
        data-testid="assignee-filter-bar"
        role="group"
        aria-label="Filter by assignee"
        sx={STRIP_SX}
      >
        {shown.map(assignee => {
          const isPicked = picked.has(assignee.id)
          return (
            <Tip key={assignee.id} title={assigneeHint(assignee, currentUserId, isPicked)}><Box
              component="button"
              type="button"
              onClick={() => onChange(toggleAssigneeId(selectedIds, assignee.id, assignees))}
              aria-pressed={isPicked}
              aria-label={assigneeHint(assignee, currentUserId, isPicked)}
              data-testid={`assignee-filter-${assignee.id}`}
              sx={[
                DISC_SX,
                TAP_SX,
                DISC_TINT_SX(assignee.name),
                isPicked ? DISC_PICKED_SX(assignee.name) : DISC_IDLE_SX,
                anyPicked && !isPicked ? DISC_DIMMED_SX : {},
              ]}
            >
              {/* The badge stays `aria-hidden`: the button's name already carries the person. */}
              <AuthorBadge name={assignee.name} sx={BADGE_SX} testId={`assignee-badge-${assignee.id}`} />
            </Box></Tip>
          )
        })}

        {overflow.length > 0 && (
          <Tip title={`${overflow.length} more assignees in scope`}><Box
            /* Lit when a pick is still in the overflow (more picks than the strip has room for). */
            data-picked={overflowPicked ? 'true' : 'false'}
            component="button"
            type="button"
            onClick={e => setMoreAnchor(e.currentTarget)}
            aria-haspopup="dialog"
            aria-expanded={moreAnchor !== null}
            aria-label={`Show ${overflow.length} more assignees`}
            data-testid="assignee-filter-more"
            sx={[MORE_SX, TAP_SX, overflowPicked ? MORE_PICKED_SX : {}]}
          >
            +{overflow.length}
          </Box></Tip>
        )}
      </Box>

      <Popover
        open={moreAnchor !== null}
        anchorEl={moreAnchor}
        /* The field clears on close: a stale filter would hide most of the roster unannounced. */
        onClose={() => {
          setMoreAnchor(null)
          setSearch('')
        }}
        anchorOrigin={{ vertical: 'bottom', horizontal: 'left' }}
        transformOrigin={{ vertical: 'top', horizontal: 'left' }}
      >
        <Box data-testid="assignee-filter-menu" role="dialog" aria-label="More assignees" sx={MENU_SX}>
          {/* `InputBase`, not `TextField`: a floating label would spend a line the placeholder covers. */}
          <InputBase
            value={search}
            onChange={e => setSearch(e.target.value)}
            placeholder="Find an assignee"
            inputProps={{ ...NO_AUTOFILL_INPUT, 'aria-label': 'Find an assignee' }}
            data-testid="assignee-filter-search"
            sx={SEARCH_SX}
          />
          <Box sx={LIST_SX}>
            {listed.map(assignee => {
              const isPicked = picked.has(assignee.id)
              return (
                <Box
                  key={assignee.id}
                  component="button"
                  type="button"
                  onClick={() => onChange(toggleAssigneeId(selectedIds, assignee.id, assignees))}
                  aria-pressed={isPicked}
                  data-testid={`assignee-filter-menu-${assignee.id}`}
                  sx={[ITEM_SX, isPicked ? { bgcolor: 'action.selected' } : {}]}
                >
                  <Box sx={[DISC_SX, DISC_TINT_SX(assignee.name), isPicked ? DISC_PICKED_SX(assignee.name) : DISC_IDLE_SX]}>
                    <AuthorBadge name={assignee.name} sx={BADGE_SX} testId={`assignee-menu-badge-${assignee.id}`} />
                  </Box>
                  <Typography component="span" variant="body2" sx={NAME_SX}>
                    {assignee.name}
                  </Typography>
                  <Typography component="span" variant="caption" sx={MENU_COUNT_SX}>
                    {assignee.task_count}
                  </Typography>
                </Box>
              )
            })}
            {listed.length === 0 && (
              <Typography variant="body2" sx={NOTE_SX}>
                No assignee matches “{search.trim()}”.
              </Typography>
            )}
          </Box>
        </Box>
      </Popover>
    </>
  )
}
