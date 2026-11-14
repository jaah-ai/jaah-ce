/* The comment box: the only surface that authors the micro-format `lib/commentText.ts` reads back. One
 * component for both the new comment and an in-place edit, so `@` autocomplete behaves the same;
 * `onCancel`'s presence IS edit mode. Fully controlled on the body and the images (the host owns the
 * draft; staging and upload are `useCommentImages`); this file owns only the autocomplete state.
 * ── The mention token ──
 * The run from the last `@` at a word boundary (start or whitespace — keeps email addresses out) to the
 * caret, with no newline. It may contain spaces ("First Last"), so it is fenced twice, both needed:
 * `MAX_MENTION_WORDS`, and "no match closes it". A settled run (accepted, or Esc) is also shut, since
 * "@Ann Lee " still matches Ann Lee — see `settled`.
 * ── The keyboard ──
 * Enter submits (in edit mode: saves); Shift+Enter is a newline; Ctrl/Cmd+Enter also submits. On a coarse
 * pointer Enter is a newline and only Send submits: a phone keyboard has no Shift+Enter anyone finds.
 * While the list is open, Enter/Tab (and Ctrl+Enter) accept the highlighted name. Nothing fires during
 * an IME composition — that Enter picks the candidate. Esc closes the list first, and only otherwise
 * reaches `onCancel`.
 * ── Reply ──
 * `handle.mention(name)` (a comment's Reply) prepends "@Name " to the draft and focuses the box after it,
 * the run settled so the picker stays shut; a draft that already mentions the name is left as is.
 * ── Images ──
 * A paste carrying image files and no text attaches them; a paste with any `text/plain` is left to the
 * box as text, image or not — Excel and Word put a PNG of the selection next to its text, and the text
 * is what was meant. A drop hands over every file, and the host refuses what is not an image. Send waits
 * for every upload and stays off while a failed one is staged (remove it); the body may then be empty.
 * ── Phone width (448px) ──
 * The popup takes the anchor's width and names ellipsize; a content-sized list would overflow.
 * Thumbnails wrap rather than scroll.
 */
import { useCallback, useEffect, useImperativeHandle, useLayoutEffect, useMemo, useRef, useState, type Ref } from 'react'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import CircularProgress from '@mui/material/CircularProgress'
import IconButton from '@mui/material/IconButton'
import MenuItem from '@mui/material/MenuItem'
import MenuList from '@mui/material/MenuList'
import Paper from '@mui/material/Paper'
import Popper from '@mui/material/Popper'
import TextField from '@mui/material/TextField'
import useMediaQuery from '@mui/material/useMediaQuery'
import { alpha, type Theme } from '@mui/material/styles'
import { clipboardFiles } from '../../lib/clipboardFiles'
import { escapeRegExp } from '../../lib/commentText'
import type { MentionCandidate } from '../../types'
import type { StagedImage } from './useCommentImages'
import { ApiImg } from '../../net/href'
import { ELLIPSIS_SX, PHONE, phoneHitAreaSx, tapTargetSx } from '../../ui/listStyles'
import { SendIcon } from '../../ui/icons'
import { isImeKey } from '../../ui/modEnter'
import { FONT_SIZE, grid, LINE_HEIGHT, RADIUS, SIZE } from '../../theme'
import Tip from '../../ui/Tip'

/** Ceiling on a mention query, in words. Three: "First Middle Last" must be mentionable. */
const MAX_MENTION_WORDS = 3

/** How many names the popup offers. Six is a glance; a scrolling picker over a text box is a dialog. */
const MAX_SUGGESTIONS = 6

/** Thumbnail edge, px: legible as a picture, small enough that a row of them costs one line. */
const THUMB = 56

/* A staged image's ×: an 18px mark on the thumbnail's corner, its 44px hit area an invisible square around it at
≤480px (`phoneHitAreaSx`). The theme's IconButton floor would instead paint a 44px dark square over most of the
picture, so it is pulled back over the same band (a media query, which beats the theme's: `TAP_FLOOR_DOWN`); at
481-599px the theme's painted 44px square stays, as before. The square overhangs the thumbnail by 9px above
and to the right: the row's 12px phone padding and gap keep it off the thread above and the next picture. */
const IMAGE_REMOVE_SX = [
  {
    position: 'absolute',
    // Inset off the corner, so the 44px square keeps clear of the thread above (the tap floor wins over the fold).
    top: grid(0.5),
    right: grid(0.5),
    p: 0,
    width: grid(3),
    height: grid(3),
    fontSize: FONT_SIZE.md,
    lineHeight: LINE_HEIGHT.tight,
    color: 'common.white',
    bgcolor: (theme: Theme) => alpha(theme.palette.common.black, 0.55),
    '&:hover': { bgcolor: (theme: Theme) => alpha(theme.palette.common.black, 0.75) },
    [PHONE]: { minWidth: 0, minHeight: 0 },
  },
  phoneHitAreaSx,
] as const

/** What a host may ask of the box imperatively. */
export type CommentComposerHandle = { mention: (name: string) => void }

/** Where the caret's mention token starts, and what it reads. `at` indexes the `@` itself. */
type MentionToken = { at: number; query: string }

/* The token the caret sits in, or null. With the `matches` memo, the single home of the header's fences. */
function mentionTokenAt(value: string, caret: number): MentionToken | null {
  const before = value.slice(0, caret)
  const at = before.lastIndexOf('@')
  if (at < 0) return null
  // Word boundary only: `a@b.dev` is an address, not a mention.
  const preceding = at === 0 ? '' : before[at - 1]
  if (preceding !== '' && !/\s/.test(preceding)) return null
  const query = before.slice(at + 1)
  if (query.includes('\n')) return null
  if (query.split(/\s+/).filter(Boolean).length > MAX_MENTION_WORDS) return null
  return { at, query }
}

/* Contains, ranked prefix-first (Enter takes index 0); ties alphabetical so order is stable. */
function rankCandidates(candidates: readonly MentionCandidate[], query: string): MentionCandidate[] {
  const needle = query.trim().toLowerCase()
  return candidates
    .filter((candidate) => candidate.name.toLowerCase().includes(needle))
    .sort((a, b) => {
      const aPrefix = a.name.toLowerCase().startsWith(needle) ? 0 : 1
      const bPrefix = b.name.toLowerCase().startsWith(needle) ? 0 : 1
      return aPrefix - bPrefix || a.name.localeCompare(b.name)
    })
    .slice(0, MAX_SUGGESTIONS)
}

export default function CommentComposer({
  value,
  onChange,
  onSubmit,
  onCancel,
  candidates,
  placeholder,
  busy = false,
  autoFocus = false,
  testId = 'comment-composer',
  images = [],
  onAddImages,
  onRemoveImage,
  handle,
}: {
  /** The draft, owned by the host. */
  value: string
  onChange: (next: string) => void
  /** Enter, Ctrl/Cmd+Enter and the Send control. The host decides whether that is a POST or a PATCH. */
  onSubmit: () => void
  /** Presence is edit mode: puts Cancel on screen and gives Esc somewhere to go. */
  onCancel?: () => void
  /** The roster this comment may mention. May be empty, in which case `@` is just a character. */
  candidates: readonly MentionCandidate[]
  placeholder?: string
  /** Request in flight: blocks a second send without blanking the box. */
  busy?: boolean
  autoFocus?: boolean
  testId?: string
  /** The draft's images. Absent handlers: pasting and dropping images is off. */
  images?: readonly StagedImage[]
  onAddImages?: (files: File[]) => void
  onRemoveImage?: (key: string) => void
  /** Receives the box's `mention` (a comment's Reply). */
  handle?: Ref<CommentComposerHandle>
}): JSX.Element {
  const inputRef = useRef<HTMLTextAreaElement | null>(null)
  const anchorRef = useRef<HTMLDivElement | null>(null)
  const [caret, setCaret] = useState(value.length)
  const [highlight, setHighlight] = useState(0)
  /* The settled token (offset + query), written by both Esc and accept. The list stays shut while the
  caret is in the same run and the query extends the settled one; backspacing into the name shortens it
  and re-offers the picker. A boolean or an offset alone gets one of those wrong. */
  const [settled, setSettled] = useState<MentionToken | null>(null)
  /* Where the caret lands after an accept, restored on the render that brings the host's new text back. */
  const pendingCaret = useRef<number | null>(null)
  const coarsePointer = useMediaQuery('(pointer: coarse)')

  const token = useMemo(() => mentionTokenAt(value, caret), [value, caret])
  const matches = useMemo(
    () => (token ? rankCandidates(candidates, token.query) : []),
    [token, candidates],
  )
  const open =
    token !== null &&
    matches.length > 0 &&
    !(settled !== null && settled.at === token.at && token.query.startsWith(settled.query))

  useEffect(() => setHighlight(0), [token?.at, token?.query])

  useLayoutEffect(() => {
    const position = pendingCaret.current
    if (position === null) return
    pendingCaret.current = null
    const field = inputRef.current
    if (!field) return
    field.focus()
    field.setSelectionRange(position, position)
    setCaret(position)
  }, [value])

  const syncCaret = useCallback((field: HTMLTextAreaElement) => {
    setCaret(field.selectionStart ?? field.value.length)
  }, [])

  const uploading = images.some(image => image.status === 'uploading')
  const failed = images.some(image => image.status === 'failed')
  const hasImage = images.some(image => image.status === 'ready')
  const canSend = !busy && !uploading && !failed && (value.trim() !== '' || hasImage)

  /* Replace the whole `@query` run with the full name plus one space, which ends the token and closes the
  list. Text after the caret is untouched. */
  const accept = useCallback(
    (candidate: MentionCandidate) => {
      if (!token) return
      const inserted = `@${candidate.name} `
      pendingCaret.current = token.at + inserted.length
      setSettled({ at: token.at, query: `${candidate.name} ` })
      onChange(value.slice(0, token.at) + inserted + value.slice(caret))
    },
    [token, value, caret, onChange],
  )

  useImperativeHandle(handle, () => ({
    mention: (name: string) => {
      const inserted = `@${name} `
      const field = inputRef.current
      if (new RegExp(`(^|\\s)@${escapeRegExp(name)}(?![\\p{L}\\p{N}])`, 'u').test(value)) {
        // Already addressed: just take the person to the end of their draft.
        field?.focus()
        field?.setSelectionRange(value.length, value.length)
        if (field) setCaret(value.length)
        return
      }
      pendingCaret.current = inserted.length
      setSettled({ at: 0, query: `${name} ` })
      onChange(inserted + value)
    },
  }), [value, onChange])

  const onKeyDown = (event: React.KeyboardEvent<HTMLDivElement>): void => {
    if (isImeKey(event)) return
    if (open) {
      if (event.key === 'ArrowDown') {
        event.preventDefault()
        setHighlight((current) => (current + 1) % matches.length)
        return
      }
      if (event.key === 'ArrowUp') {
        event.preventDefault()
        setHighlight((current) => (current - 1 + matches.length) % matches.length)
        return
      }
      if (event.key === 'Enter' || event.key === 'Tab') {
        event.preventDefault()
        accept(matches[highlight] ?? matches[0])
        return
      }
      if (event.key === 'Escape') {
        event.preventDefault()
        // An ancestor dialog or drawer must not also close on this key.
        event.stopPropagation()
        setSettled(token)
        return
      }
    }
    if (event.key === 'Enter' && (event.ctrlKey || event.metaKey || (!event.shiftKey && !coarsePointer))) {
      event.preventDefault()
      if (canSend) onSubmit()
      return
    }
    if (event.key === 'Escape' && onCancel) {
      event.preventDefault()
      onCancel()
    }
  }

  return (
    <Box
      data-testid={testId}
      sx={{ width: '100%' }}
      onDragOver={(event) => {
        // Accepting the drag is what lets the drop land here instead of navigating to the file.
        if (onAddImages && event.dataTransfer.types.includes('Files')) event.preventDefault()
      }}
      onDrop={(event) => {
        const files = Array.from(event.dataTransfer.files)
        if (!onAddImages || files.length === 0) return
        event.preventDefault()
        onAddImages(files)
      }}
    >
      {images.length > 0 && (
        <Box sx={{ display: 'flex', flexWrap: 'wrap', gap: 0.5, mb: 0.5, [PHONE]: { gap: 2, pt: 2 } }} data-testid={`${testId}-images`}>
          {images.map((image) => (
            <Tip key={image.key} title={image.status === 'failed' ? 'Upload failed' : image.filename}><Box
              data-testid={`${testId}-image`}
              data-status={image.status}
              sx={{
                position: 'relative',
                width: THUMB,
                height: THUMB,
                flexShrink: 0,
                borderRadius: RADIUS.sm,
                // No `overflow: hidden`: it would clip the ×'s hit area; the picture rounds its own corners.
                border: '1px solid',
                borderColor: image.status === 'failed' ? 'error.main' : 'divider',
              }}
            >
              <Box
                component={ApiImg}
                src={image.src}
                alt={image.filename}
                sx={{
                  width: '100%',
                  height: '100%',
                  objectFit: 'cover',
                  display: 'block',
                  // Inside the 1px border: the frame's radius less the border, so no corner of the picture pokes out.
                  borderRadius: RADIUS.sm,
                  opacity: image.status === 'ready' ? 1 : 0.4,
                }}
              />
              {image.status === 'uploading' && (
                <CircularProgress
                  size={SIZE.iconMd}
                  aria-label="Uploading"
                  sx={{ position: 'absolute', inset: 0, m: 'auto' }}
                />
              )}
              {onRemoveImage && (
                <IconButton
                  size="small"
                  aria-label={`Remove ${image.filename}`}
                  onClick={() => onRemoveImage(image.key)}
                  sx={IMAGE_REMOVE_SX}
                >
                  <Box component="span" aria-hidden>×</Box>
                </IconButton>
              )}
            </Box></Tip>
          ))}
        </Box>
      )}
      <Box sx={{ display: 'flex', alignItems: 'flex-end', gap: 0.5 }}>
        <TextField
          inputRef={inputRef}
          ref={anchorRef}
          value={value}
          onChange={(event) => {
            onChange(event.target.value)
            syncCaret(event.target as HTMLTextAreaElement)
          }}
          // The token is a function of the caret, which clicks and arrows move without input events.
          onSelect={(event) => syncCaret(event.target as HTMLTextAreaElement)}
          onKeyDown={onKeyDown}
          onPaste={(event) => {
            // Any text wins: see the header.
            if (!onAddImages || event.clipboardData.getData('text/plain') !== '') return
            const files = clipboardFiles(event.clipboardData)
            if (files.length === 0) return
            event.preventDefault()
            onAddImages(files)
          }}
          placeholder={placeholder}
          autoFocus={autoFocus}
          multiline
          minRows={1}
          maxRows={8}
          size="small"
          fullWidth
          slotProps={{ htmlInput: { 'data-testid': `${testId}-input` } }}
          /* Denser than a form field: it starts at one line and grows; `maxRows` keeps Send on a phone screen.
             On a phone one line is the 44px touch floor, bought with the textarea's PADDING, not a min-height:
             autosize's hidden shadow textarea shares the input's class, so a min-height there is measured and
             the padding added on top (a 62px box). */
          sx={(theme) => ({
            '& .MuiInputBase-root': { py: 0.5, px: 1, fontSize: FONT_SIZE.md, lineHeight: LINE_HEIGHT.snug },
            [theme.breakpoints.down('sm')]: {
              // The root is the field a finger hits (a click anywhere focuses the textarea): it holds the floor
              // exactly, since the autosized line can land a fraction under it.
              '& .MuiInputBase-root': { py: 0, minHeight: theme.custom?.tapTargetPx ?? 44 },
              '& .MuiInputBase-input': { py: `calc((${theme.spacing((theme.custom?.tapTargetPx ?? SIZE.tapTarget) / 6)} - 1lh) / 2)` },
            },
          })}
        />
        <Popper
          open={open}
          anchorEl={anchorRef.current}
          placement="top-start"
          modifiers={[{ name: 'preventOverflow', options: { padding: 8 } }]}
          // The anchor's width — see the header.
          sx={{ zIndex: (theme) => theme.zIndex.modal, width: anchorRef.current?.offsetWidth }}
          data-testid={`${testId}-mentions`}
        >
          <Paper elevation={4} sx={{ maxHeight: grid(36), overflowY: 'auto' }}>
            {/* No `autoFocusItem`: focus stays in the text box while the person types. */}
            <MenuList dense disablePadding>
              {matches.map((candidate, index) => (
                <MenuItem
                  key={candidate.id}
                  selected={index === highlight}
                  // `onMouseDown`, not `onClick`: click fires after blur, which has lost the caret.
                  onMouseDown={(event) => {
                    event.preventDefault()
                    accept(candidate)
                  }}
                  sx={{ fontSize: FONT_SIZE.md, minHeight: 0, py: 0.5 }}
                >
                  <Box component="span" sx={ELLIPSIS_SX}>
                    {candidate.name}
                  </Box>
                </MenuItem>
              ))}
            </MenuList>
          </Paper>
        </Popper>
        {onCancel && (
          <Button size="small" onClick={onCancel} sx={[{ minWidth: 0, px: 1 }, tapTargetSx]}>
            Cancel
          </Button>
        )}
        <IconButton
          size="small"
          color="primary"
          // One word where one word does.
          aria-label="Send"
          disabled={!canSend}
          onClick={() => onSubmit()}
          data-testid={`${testId}-send`}
        >
          <SendIcon size={SIZE.iconMd} />
        </IconButton>
      </Box>
    </Box>
  )
}
