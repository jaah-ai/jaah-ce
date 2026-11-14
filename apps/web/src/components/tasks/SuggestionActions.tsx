/* ACCEPT AND DISCARD ON A JAAH SUGGESTION (`listViews/boardSections.ts::isSuggestion`) — the board row's and the task
 * page's one control. Accept queues the draft (Start with the `queue` intent); Discard deletes it, no confirm. Each
 * shows where its verb already would: Accept on the row's Start gate, Discard on its Delete gate. */
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import type { PipelineTask } from '../../types'
import { isSuggestion } from './listViews/boardSections'
import { taskRowActionFlags, type TaskRowActionHandlers } from './rowActions'
import { FONT_SIZE, FONT_WEIGHT, LINE_HEIGHT, grid } from '../../theme'
import Tip from '../../ui/Tip'

// The row's 30px band: a tight button, so the row keeps its height — but never under the 24px tap floor (WCAG 2.5.8).
// On the row both are quiet text links: the title leads, Accept in the accent, Discard muted.
const ROW_BUTTON_SX = { minWidth: 0, minHeight: grid(4), py: 0, px: 1, lineHeight: LINE_HEIGHT.normal, fontSize: FONT_SIZE.sm, fontWeight: FONT_WEIGHT.regular } as const
const ROW_DISCARD_SX = { ...ROW_BUTTON_SX, color: 'text.secondary' } as const

export default function SuggestionActions({ task, title, handlers, busy, dense }: {
  task: PipelineTask
  /** The name the host shows, for the buttons' accessible names. */
  title: string
  handlers: TaskRowActionHandlers
  busy: boolean
  /** The board row's compact buttons; the task page's are its header's size. */
  dense?: boolean
}) {
  if (!isSuggestion(task)) return null
  const flags = taskRowActionFlags(task, 'root', handlers)
  // A blocked Start (a pending org) keeps Accept, disabled with its reason, as the ⋯ keeps Start.
  const accept = flags.showStart ? handlers.onStart : undefined
  const blocked = flags.startBlockedReason
  const discard = flags.showCleanup ? handlers.onDiscard : undefined
  if (!accept && !discard) return null
  return (
    <Box component="span" data-testid="suggestion-actions" sx={{ display: 'inline-flex', gap: dense ? 0 : 0.5 }}>
      {accept && (
        <Tip title={blocked}><Button
          size="small"
          variant={dense ? 'text' : 'contained'}
          disableElevation
          disabled={busy || blocked !== undefined}
          aria-label={`Accept ${title}`}
          onClick={() => accept(task, 'queue')}
          data-testid="suggestion-accept"
          sx={dense ? ROW_BUTTON_SX : undefined}
        >
          Accept
        </Button></Tip>
      )}
      {discard && (
        <Button
          size="small"
          variant={dense ? 'text' : 'outlined'}
          color="inherit"
          disabled={busy}
          aria-label={`Discard ${title}`}
          onClick={() => discard(task)}
          data-testid="suggestion-discard"
          sx={dense ? ROW_DISCARD_SX : undefined}
        >
          Discard
        </Button>
      )}
    </Box>
  )
}
