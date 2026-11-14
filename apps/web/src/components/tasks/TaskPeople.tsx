/* THE BOARD ROW'S CREATOR → ASSIGNEE CELL: `RZ➜JS`, each side a person's initials in their hue
 * (`./AuthorBadge`, the same letters as the assignee strip). A row filed by a schedule, trigger, agent or the
 * system shows its origin glyph in the creator's place (`lib/taskOrigin.ts`: ⏱ ⚡ ✦ ⚙, in its title's grey),
 * so the class costs no width of its own. A person who filed their own task shows ONCE: one badge, no arrow.
 *
 * THE ASSIGNEE IS A CONTROL for whoever may edit the row (`onAssign` present): pressing it opens an inline
 * lookup of the row's project members (`GET /api/projects/{id}/members`, the roster the PUT accepts), with a
 * filter field. Without `onAssign` it is plain initials. An unassigned row shows a dim `·`.
 * Presses never reach the row (its body opens the console).
 */
import { useState } from 'react'
import type { SyntheticEvent } from 'react'
import Box from '@mui/material/Box'
import InputBase from '@mui/material/InputBase'
import Popover from '@mui/material/Popover'
import Typography from '@mui/material/Typography'
import { get } from '../../api'
import { useApiRead } from '../../hooks/useApiRead'
import { ROSTER_STALE_MS } from '../../lib/cachedRead'
import { memberName } from '../../lib/memberLabel'
import type { TaskOrigin } from '../../lib/taskOrigin'
import type { ProjectMember } from '../../types'
import { matchesAny } from '../../ui/FilterRow'
import LoadingState from '../../ui/LoadingState'
import { UNSTYLED_BUTTON_SX } from '../../ui/listStyles'
import { NO_AUTOFILL_INPUT } from '../../ui/noAutofill'
import AuthorBadge from './AuthorBadge'
import { FONT_SIZE, FONT_WEIGHT, LETTER_SPACING, LINE_HEIGHT, RADIUS, SIZE } from '../../theme'
import { ITEM_SX, LIST_SX, MENU_SX, NAME_SX, NOTE_SX, SEARCH_SX } from './peopleMenuSx'
import Tip from '../../ui/Tip'

/* Weight and tracking: a 400-weight `RZ` at 10px is a smudge. */
const BADGE_SX = { fontWeight: FONT_WEIGHT.bold, letterSpacing: LETTER_SPACING.none } as const

const CELL_SX = { display: 'inline-flex', alignItems: 'center', gap: 0, whiteSpace: 'nowrap' } as const

/* A heavy `➜` in the secondary tone at the letters' size: the thin disabled-grey `→` vanished between two badges. */
const ARROW_SX = { color: 'text.secondary', fontSize: FONT_SIZE.xs, lineHeight: LINE_HEIGHT.tight, px: 0 } as const

/* The origin glyph, in the grey of a machine-filed row's title (`FeedRowSpec.quiet`), a notch larger than the
   letters it stands in for. */
const GLYPH_SX = { fontSize: FONT_SIZE.xs, lineHeight: LINE_HEIGHT.tight, color: 'text.secondary' } as const

const UNASSIGNED_SX = { color: 'text.disabled', px: 0.5 } as const

const ASSIGNEE_BUTTON_SX = {
  ...UNSTYLED_BUTTON_SX,
  display: 'inline-flex',
  alignItems: 'center',
  borderRadius: RADIUS.sm,
  px: 0,
  cursor: 'pointer',
  '&:hover': { bgcolor: 'action.hover' },
  '&:focus-visible': { outline: '2px solid', outlineColor: 'primary.main', outlineOffset: 1 },
} as const

const ITEM_BADGE_SX = { ...BADGE_SX, width: SIZE.chip, textAlign: 'center', fontSize: FONT_SIZE.xs, flex: '0 0 auto' } as const

/** The lookup: the project's members, filtered by the field, the current assignee ticked. */
function AssigneeMenu({ projectId, isCurrent, onPick }: {
  projectId: number
  isCurrent: (member: ProjectMember) => boolean
  onPick: (member: ProjectMember) => void
}) {
  /* The same entry `TaskSpecFields` reads imperatively (`cachedRead`): one GET for both. */
  const roster = useApiRead(['project-members', projectId],
    () => get<ProjectMember[]>(`/api/projects/${projectId}/members`), 'Members failed to load.', { staleTime: ROSTER_STALE_MS })
  const [search, setSearch] = useState('')

  const members = roster.data ?? []
  const listed = search.trim() ? members.filter(m => matchesAny(search, memberName(m), m.email)) : members
  return (
    <Box data-testid="assignee-picker" role="dialog" aria-label="Assign to" sx={MENU_SX}>
      <InputBase
        autoFocus
        value={search}
        onChange={e => setSearch(e.target.value)}
        placeholder="Find a member"
        inputProps={{ ...NO_AUTOFILL_INPUT, 'aria-label': 'Find a member' }}
        data-testid="assignee-picker-search"
        sx={SEARCH_SX}
      />
      <Box sx={LIST_SX}>
        {roster.loading && !roster.error && <LoadingState />}
        {roster.error && <Typography variant="body2" color="error" sx={NOTE_SX}>{roster.error}</Typography>}
        {listed.map(member => {
          const name = memberName(member)
          const current = isCurrent(member)
          return (
            <Tip key={member.id} title={member.email}><Box
              component="button"
              type="button"
              aria-pressed={current}
              data-testid={`assignee-picker-${member.id}`}
              onClick={() => onPick(member)}
              sx={[ITEM_SX, current ? { bgcolor: 'action.selected' } : {}]}
            >
              <AuthorBadge name={name} sx={ITEM_BADGE_SX} testId={`assignee-picker-badge-${member.id}`} />
              <Typography component="span" variant="body2" sx={NAME_SX}>{name}</Typography>
              {current && <Typography component="span" variant="caption" aria-hidden="true">✓</Typography>}
            </Box></Tip>
          )
        })}
        {roster.data && listed.length === 0 && (
          <Typography variant="body2" sx={NOTE_SX}>
            {search.trim() ? `No member matches “${search.trim()}”.` : 'No members.'}
          </Typography>
        )}
      </Box>
    </Box>
  )
}

export default function TaskPeople({ creator, origin, assignee, assigneeId, projectId, taskTitle, onAssign }: {
  /** The creator's resolved name, or null. Ignored when `origin` is not human (the glyph stands in). */
  creator: string | null
  origin: TaskOrigin
  assignee: string | null
  /** The assignee's id where the payload carries it (a detail read); null on a board row. */
  assigneeId: string | null
  /** The row's project; null offers no lookup (nothing to list members of). */
  projectId: number | null
  taskTitle: string
  /** Present = the viewer may reassign this row. */
  onAssign?: (assigneeId: string) => void
}) {
  const [anchor, setAnchor] = useState<HTMLElement | null>(null)
  const editable = onAssign !== undefined && projectId !== null
  /* A board row carries the assignee's NAME only (no actor uuid on a list row), so the current member is matched by
     id where the caller has one and by the server's one label derivation (`memberName`'s) otherwise. */
  const isCurrent = (member: ProjectMember) =>
    assigneeId !== null ? member.id === assigneeId : assignee !== null && memberName(member) === assignee

  /* Filed for oneself: matched by name, the only creator identity on the wire (`types.ts`'s `Provenance`). */
  const solo = origin.kind === 'human' && creator !== null && creator === assignee
  const assigneeTitle = solo ? `Created by and assigned to ${assignee}` : assignee ? `Assigned to ${assignee}` : 'Unassigned'

  const creatorNode =
    origin.kind !== 'human' ? (
      <Tip title={origin.label}><Box
        component="span"
        aria-hidden="true"
        data-testid="feed-origin"
        data-origin={origin.kind}
        sx={GLYPH_SX}
      >
        {origin.glyph}
      </Box></Tip>
    ) : creator ? (
      <AuthorBadge name={creator} sx={BADGE_SX} />
    ) : (
      <Box component="span" aria-hidden="true" sx={UNASSIGNED_SX}>·</Box>
    )

  const assigneeFace = assignee ? (
    <AuthorBadge name={assignee} sx={BADGE_SX} testId="feed-assignee" />
  ) : (
    <Box component="span" aria-hidden="true" data-testid="feed-assignee" sx={UNASSIGNED_SX}>·</Box>
  )

  const stop = (event: SyntheticEvent) => event.stopPropagation()

  return (
    <Box component="span" data-testid="feed-people" sx={CELL_SX}>
      {!solo && (
        <>
          {creatorNode}
          <Box component="span" aria-hidden="true" sx={ARROW_SX}>➜</Box>
        </>
      )}
      {editable ? (
        <>
          <Tip title={assigneeTitle}><Box
            component="button"
            type="button"
            data-testid="feed-assignee-button"
            aria-haspopup="dialog"
            aria-expanded={anchor !== null}
            aria-label={`Assignee of ${taskTitle}: ${assignee ?? 'nobody'}. Change`}
            onPointerDown={stop}
            onKeyDown={stop}
            onClick={event => {
              event.stopPropagation()
              setAnchor(event.currentTarget)
            }}
            sx={ASSIGNEE_BUTTON_SX}
          >
            {assigneeFace}
          </Box></Tip>
          <Popover
            open={anchor !== null}
            anchorEl={anchor}
            onClose={() => setAnchor(null)}
            /* A portal still bubbles React events to the row: its press opens the console, its long-press picks. */
            onClick={stop}
            onPointerDown={stop}
            onKeyDown={stop}
            anchorOrigin={{ vertical: 'bottom', horizontal: 'right' }}
            transformOrigin={{ vertical: 'top', horizontal: 'right' }}
          >
            {anchor !== null && (
              <AssigneeMenu
                projectId={projectId}
                isCurrent={isCurrent}
                onPick={member => {
                  setAnchor(null)
                  if (!isCurrent(member)) onAssign(member.id)
                }}
              />
            )}
          </Popover>
        </>
      ) : (
        <Tip title={assigneeTitle}><Box component="span">{assigneeFace}</Box></Tip>
      )}
    </Box>
  )
}
