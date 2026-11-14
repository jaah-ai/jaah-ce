/* ── A KEY/VALUE ROW EDITED IN PLACE — a `KeyValue` with a ✎ that swaps the value for an input ──────
 * The console's one in-place editor for a record's fact (a host's Name in `pages/nodes/hosts/HostDetailPage.tsx`,
 * an org's own value in `pages/orgs/OwnValueBlock.tsx`): a record is edited where it is read, never in a modal
 * (the style guide's edit-in-place rule). Enter commits, Escape cancels, blur commits, unchanged not sent, `''`
 * is a deliberate clear, closes when `canEdit` drops (`useCommitOnBlur` below holds that state machine; the
 * component draws it). Renders inside a `KeyValueList`. A refused save closes the editor and the page's banner
 * carries the server's sentence (commit-on-blur has no open form to keep the draft in); the value shown stays
 * the stored one, so the row never lies.
 */
import { type JSX, type KeyboardEvent, type ReactNode, useEffect, useRef, useState } from 'react'
import Box from '@mui/material/Box'
import IconButton from '@mui/material/IconButton'
import InputBase from '@mui/material/InputBase'
import { KeyValue } from './KeyValueList'
import { PHONE, fieldTapTargetSx, tapTargetSx } from './listStyles'
import { FONT_SIZE, RADIUS } from '../theme'

/** The commit-on-blur state machine. `exitHandled` stops the blur that follows Enter/Escape from writing again.
 *  Untouched in, untouched out: a value unchanged from `open(seed)` is not sent (trimmed on both sides — blanks-only
 *  is a no-op, an intentional clear reaches `onSave` as `''`), so a mis-click cannot write a value back. An open
 *  editor does not outlive the capability that opened it (`canEdit` dropping closes it, and a blur that lands
 *  before that effect runs is refused the same way); presentation only — the route is the boundary. */
function useCommitOnBlur({ canEdit, onSave }: { canEdit: boolean; onSave: (value: string) => void }) {
  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState('')
  const exitHandled = useRef(false)
  const openedWith = useRef('')

  useEffect(() => {
    if (!canEdit) setEditing(false)
  }, [canEdit])

  function open(seed: string) {
    openedWith.current = seed
    setDraft(seed)
    exitHandled.current = false
    setEditing(true)
  }

  function commit() {
    setEditing(false)
    // The awaited half: a blur can land before the `canEdit` effect runs.
    if (!canEdit) return
    const next = draft.trim()
    if (next !== openedWith.current.trim()) onSave(next)
  }

  function onKeyDown(e: KeyboardEvent) {
    if (e.key === 'Enter') { exitHandled.current = true; commit() }
    /* Escape stopped here: a surrounding MUI `Modal` would otherwise close on a field cancel; with no field open
       it keeps Escape. */
    else if (e.key === 'Escape') { e.stopPropagation(); exitHandled.current = true; setEditing(false) }
  }

  function onBlur() {
    if (exitHandled.current) { exitHandled.current = false; return }
    commit()
  }

  return { editing, draft, setDraft, open, onKeyDown, onBlur }
}

const EDIT_LINE_SX = { display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 1 } as const

export default function EditableRow({
  label, value, seed, editLabel, canEdit, onSave, testId, rowTestId = testId, maxLength,
}: {
  label: ReactNode
  /** The value as read. */
  value: ReactNode
  /** What the input opens with — not always the rendered value (an unset value seeds an empty box). */
  seed: string
  /** The ✎'s accessible name, and the input's. */
  editLabel: string
  canEdit: boolean
  /** The trimmed draft, only when it differs from the seed; `''` is a deliberate clear. */
  onSave: (value: string) => void
  /** The row's testid, and the prefix of its controls': `${testId}-edit` is the ✎, `${testId}-input` the editor. */
  testId: string
  /** The row's own testid where `testId` names the value (`OwnValueBlock`'s `-own`). */
  rowTestId?: string
  /** The server's length cap, stopped at the keyboard so a paste past it is not a 422 after the editor closed. */
  maxLength?: number
}): JSX.Element {
  const editor = useCommitOnBlur({ canEdit, onSave })

  if (editor.editing) {
    return (
      <KeyValue testId={rowTestId} label={label}>
        <InputBase
          autoFocus
          data-testid={`${testId}-input`}
          inputProps={{ 'aria-label': editLabel, ...(maxLength ? { maxLength } : {}) }}
          value={editor.draft}
          onChange={e => editor.setDraft(e.target.value)}
          onKeyDown={editor.onKeyDown}
          onBlur={editor.onBlur}
          /* 16px at phone width is the iOS-Safari auto-zoom floor (it zooms and never zooms back). Not a
             style choice; `fieldTapTargetSx` does not supply it. */
          sx={[
            fieldTapTargetSx,
            { fontSize: FONT_SIZE.base, border: '1px solid', borderColor: 'divider', borderRadius: RADIUS.sm, px: 0.5 },
            { [PHONE]: { fontSize: FONT_SIZE.lg } },
          ]}
        />
      </KeyValue>
    )
  }

  const edit = canEdit && (
    <IconButton
      size="small"
      data-testid={`${testId}-edit`}
      onClick={() => editor.open(seed)}
      aria-label={editLabel}
      /* The target overhangs its line rather than growing it, so an editable row is as tall as a read-only one
         (a 32px button in a 20px line on desktop). On a phone the 44px target reaches a few px into the row
         above, which holds no control at that x. */
      sx={[tapTargetSx, { my: -1, [PHONE]: { my: -2 } }]}
    >
      <span aria-hidden="true">✎</span>
    </IconButton>
  )
  /* The ✎ rides the row's right edge, beside the value (the list keeps label-beside-value at every width). */
  return (
    <KeyValue testId={rowTestId} label={label}>
      <Box sx={EDIT_LINE_SX}>
        <Box sx={{ minWidth: 0 }}>{value}</Box>
        {edit}
      </Box>
    </KeyValue>
  )
}
