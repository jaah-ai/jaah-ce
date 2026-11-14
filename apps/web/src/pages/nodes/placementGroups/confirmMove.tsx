import Box from '@mui/material/Box'
import Typography from '@mui/material/Typography'
import type { ConfirmOptions } from '../../../components/ConfirmDialog'
import { countOf } from '../../../lib/humanize'
import { moveNodes, type MoveNode } from './api'

/* THE MOVE CONFIRM (AC13, `node-groups-dialogs-mockup.html`): a group change that takes nodes away from a project
 * while its tasks run there answers 409 `MovePreview`; this asks, and the caller re-sends with `confirm: true`.
 * Running tasks always finish where they are (PRD D16), so the dialog says so and nothing more. One home for the
 * project field, the node page's chips and the group page's members. */

const taskWord = (n: number) => `${countOf(n, 'task')} running`

function nodeLine(node: MoveNode): string {
  const name = node.name || 'A node you can’t see'
  return `${name} · ${taskWord(node.tasks_in_use)}`
}

/** The confirm's options for `nodes`; `subject` names who stops using them ("Payments API", "Its projects"). */
export function moveConfirmOptions(nodes: MoveNode[], subject: string): ConfirmOptions {
  return {
    title: nodes.length === 1 ? 'Stop using 1 node?' : `Stop using ${nodes.length} nodes?`,
    message: (
      <Box component="span" sx={{ display: 'block' }}>
        <Typography component="span" sx={{ display: 'block' }}>{subject} will no longer start tasks on:</Typography>
        <Box component="ul" sx={{ my: 1, pl: 3 }}>
          {nodes.map((n, i) => <li key={n.id ?? `hidden-${i}`}>{nodeLine(n)}</li>)}
        </Box>
        <Typography component="span" sx={{ display: 'block', color: 'text.secondary' }}>Running tasks finish first.</Typography>
      </Box>
    ),
    confirmLabel: 'Change groups',
  }
}

/** When `err` is a move preview, asks and returns the answer; otherwise null (the caller shows the error). */
export async function confirmMove(
  err: unknown,
  confirm: (options: ConfirmOptions) => Promise<boolean>,
  subject: string,
): Promise<boolean | null> {
  const nodes = moveNodes(err)
  if (!nodes) return null
  return confirm(moveConfirmOptions(nodes, subject))
}
