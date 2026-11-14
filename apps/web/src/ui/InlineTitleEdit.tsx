import { useRef, useState, type KeyboardEvent, type ReactNode } from 'react'
import Box from '@mui/material/Box'
import InputBase from '@mui/material/InputBase'
import { ABOVE_PHONE, PHONE, UNSTYLED_BUTTON_SX, ELLIPSIS_SX } from './listStyles'
import { FONT_SIZE, RADIUS } from '../theme'
import Tip from './Tip'

/* THE CONSOLE'S ONE CLICK-TO-EDIT TITLE: a breadcrumb's last crumb (the page's `<h1>`) that turns into an inline
 * multiline input on click. Enter or blur commits, Escape reverts. The task editor's title commits to its form's
 * draft (only Save persists); the project page's commits at once (`PUT {name}`): WHEN it saves is the host's.
 * · `value` — what the field opens on; a commit equal to it is no change and calls nothing.
 * · `blank` — what a blanked field does: `'commit'` passes it on, `'revert'` drops it silently, `'refuse'` drops it
 *   and calls `onBlankRefused` so the host can say why.
 * · `lead` — drawn before the title in both states (an icon, an id prefix); never part of the edit.
 * The `16px` font floor below is the iOS auto-zoom floor (Safari zooms the editor on focus under it). */
export default function InlineTitleEdit({
  value,
  shown,
  onCommit,
  editable,
  blank = 'commit',
  onBlankRefused,
  lead,
  tip,
  inputLabel,
  editLabel,
  testId,
}: {
  value: string
  /** The title as read (`value` may be stored text the reader sees otherwise). */
  shown: string
  onCommit: (next: string) => void
  editable: boolean
  blank?: 'commit' | 'revert' | 'refuse'
  onBlankRefused?: () => void
  lead?: ReactNode
  /** The whole title on hover: above the phone a one-line trail ellipsises it. */
  tip: string
  /** The input's accessible name. */
  inputLabel: string
  /** The button's accessible name. */
  editLabel: string
  /** `testId` on the read-only crumb, `${testId}-input` on the editor. */
  testId: string
}) {
  // `null` = not editing, distinct from editing an empty title.
  const [draft, setDraft] = useState<string | null>(null)
  // Set by Escape so the blur it causes does not commit the rejected value.
  const escapedRef = useRef(false)
  const inputRef = useRef<HTMLInputElement>(null)

  function commit() {
    if (escapedRef.current) {
      escapedRef.current = false
      setDraft(null)
      return
    }
    // An unedited field is no change: committing what it opened on would rewrite the stored title.
    if (draft !== null && draft !== value) {
      if (draft.trim() !== '' || blank === 'commit') onCommit(draft)
      else if (blank === 'refuse') onBlankRefused?.()
    }
    setDraft(null)
  }

  function onKeyDown(e: KeyboardEvent<HTMLInputElement>) {
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

  if (draft !== null) {
    return (
      <Box sx={{ display: 'flex', alignItems: 'baseline', gap: 0, minWidth: 0 }}>
        {lead && <Box component="span" sx={{ flex: '0 0 auto' }}>{lead}</Box>}
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
          onKeyDown={onKeyDown}
          inputProps={{ 'aria-label': inputLabel }}
          data-testid={`${testId}-input`}
          sx={theme => ({
            flex: '1 1 auto',
            minWidth: 0,
            font: 'inherit',
            px: 0.5,
            border: 1,
            borderColor: 'primary.main',
            borderRadius: RADIUS.sm,
            // `InputBase` has no focus ring; a second hairline inside the border, so the baseline does not shift.
            '&.Mui-focused': { outline: '1px solid', outlineColor: 'primary.main', outlineOffset: '-2px' },
            // `theme.custom.tapTargetPx` is the touch floor's single home; 16px (`lg`) is the iOS auto-zoom floor.
            [PHONE]: { fontSize: FONT_SIZE.lg, minHeight: theme.custom.tapTargetPx },
          })}
        />
      </Box>
    )
  }

  return (
    <Tip title={tip}><Box
      component={editable ? 'button' : 'span'}
      type={editable ? 'button' : undefined}
      onClick={editable ? () => setDraft(value) : undefined}
      data-testid={testId}
      aria-label={editable ? editLabel : undefined}
      sx={theme => ({
        ...UNSTYLED_BUTTON_SX,
        cursor: 'default',
        textAlign: 'left',
        m: 0,
        /* A `<button>` is an atomic inline box: the `<h1>`'s ellipsis would clip it mid-glyph, so the box itself
           ellipsises on the one-line trail. */
        [ABOVE_PHONE]: { display: 'block', maxWidth: '100%', ...ELLIPSIS_SX },
        ...(editable
          ? {
              cursor: 'text',
              // Dotted underline on hover only: the crumb is the page's identifier first.
              '&:hover': { textDecoration: 'underline dotted' },
              /* The 44px tap floor as padding cancelled by an equal negative margin (`Breadcrumb`'s crumb hit
                 box): a `minHeight` would centre a one-line title 12px below the `›` that leads it. */
              [PHONE]: {
                py: `calc((${theme.spacing(theme.custom.tapTargetPx / 6)} - 1lh) / 2)`,
                my: `calc((1lh - ${theme.spacing(theme.custom.tapTargetPx / 6)}) / 2)`,
              },
            }
          : {}),
      })}
    >
      {lead}
      {shown}
    </Box></Tip>
  )
}
