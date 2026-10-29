/* The Comments tab: one task's conversation and its composer. Space efficiency is the requirement
 * (fewer words; one word where one word does):
 *   · `AuthorBadge` stands in for the name (full name on `title`).
 *   · One byline per comment: badge, relative age, "edited"; absolute time on `title`.
 *   · React/Edit/Delete sit right-aligned on the line under the message, always shown (no hover reveal).
 * Who may do what is the server's answer (`author_is_me`, `may_delete`) — presentation only; the route
 * re-decides, and the caller's identity is deliberately not on the wire.
 * Row actions reuse `ACTION_ICON` (./shared): `@mui/icons-material` is not a dependency (single home:
 * `ui/InfoTip.tsx`). Glyphs are `aria-hidden` inside named buttons.
 * Oldest first, composer at the bottom (unlike every other roster).
 * A task's images draw under the text as thumbnails, each a link to the full image in a new tab. Plain
 * `<img>` on the content route: same-origin, so the session cookie rides it and the 307 to storage needs no
 * CORS. A comment may be images only; `image_ids` on a PATCH REPLACES the set, so edit sends the whole list.
 * A ticket's thread has no images (the server refuses `image_ids`): its composer gets no image handlers.
 * The count is reported upward (`onCountChange`) for the tab label: the panel is mounted from first render.
 * `#comment-<id>` deep-links one row (mention DM, bell): scrolled and tinted ONCE per navigation, after
 * the list loads and while `active` — a later reload must not yank the reader, and a hidden panel's
 * scroll is lost.
 * One component for a Task and a support Ticket (`subject`, `lib/commentSubject`): only the route base
 * differs. A ticket adds what a task never has — the Reply/Internal note toggle (`canPostInternal`, staff),
 * rows tinted and labelled when `visibility` is internal, and an optional attachment row that mints
 * uploads under the current toggle. A task renders and sends exactly as before.
 * Reactions (`lib/reactions.ts`, a fixed set): Slack-style chips under the text, each toggling MY reaction,
 * its tooltip naming who; the "Add reaction" action opens the palette. Controls are drawn for everyone —
 * no field says who may react, so the route's 403 is the answer. The call answers the updated comment,
 * spliced in place; a full reload would re-run the deep-link and cost a round trip per click. */
import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import Box from '@mui/material/Box'
import ButtonBase from '@mui/material/ButtonBase'
import IconButton from '@mui/material/IconButton'
import Popover from '@mui/material/Popover'
import Stack from '@mui/material/Stack'
import Tooltip from '@mui/material/Tooltip'
import Typography from '@mui/material/Typography'
import { alpha, useTheme, type Theme } from '@mui/material/styles'
import { useLocation } from 'react-router-dom'

import { apiDetail, del, get, patch, post, put, useApiRewriterVersion } from '../../api'
import { useSubmit } from '../../hooks/useSubmit'
import { useConfirm } from '../ConfirmDialog'
import { absoluteTime, relativeAge } from '../../lib/relativeTime'
import { subjectBase, type CommentSubject, type CommentVisibility } from '../../lib/commentSubject'
import { REACTIONS, reactionGlyph, reactionLabel } from '../../lib/reactions'
import { takeTaskPageSeed } from '../../lib/taskPageSeed'
import { useLandedTaskPart } from '../../hooks/useLandedTaskPart'
import type { MentionCandidate, TaskComment, TaskCommentReaction } from '../../types'
import SegmentedControl from '../../ui/SegmentedControl'
import { ACTION_ICON } from './shared'
import AuthorBadge from './AuthorBadge'
import CommentComposer from './CommentComposer'
import CommentText from './CommentText'
import TaskAttachmentsField from './TaskAttachmentsField'
import { commentImageUrl, useCommentImages } from './useCommentImages'

/* Badge geometry (the host owns the box, `AuthorBadge` the identity). 22px: smallest legible disc here. */
const BADGE_SX = { width: 22, height: 22, fontSize: 10, flexShrink: 0 } as const

/* The actions sit right-aligned on the line under the message, so the text keeps the row's full width.
   Always shown: no hover reveal. */
const ACTIONS_SX = {
  display: 'flex',
  alignItems: 'center',
  flexShrink: 0,
  ml: 'auto',
  gap: 0.25,
} as const

/* Row-action button geometry (Edit/Delete/React): a 28px square with a 12px glyph on every width. Deliberately
   below the theme's 44px tap floor (`theme.ts`), which is restated in the breakpoint to lower it — the operator
   chose compact over the floor here; a 44px button would double each comment's footer. */
const ACTION_BUTTON_SX = (theme: Theme) => ({
  p: 0,
  width: 28,
  height: 28,
  fontSize: 12,
  lineHeight: 1,
  color: 'text.secondary',
  [theme.breakpoints.down('sm')]: { minHeight: 28, minWidth: 28 },
})

/* A chip is a 22px pill; below `sm` its BUTTON grows to the 44px tap floor while the pill stays 22px —
   a 44px pill per reaction would double every reacted comment's height on a phone. */
const chipSx = (theme: Theme) => ({
  borderRadius: 999,
  // The theme's own IconButton floor breakpoint (`theme.ts`), so a chip and its neighbours agree.
  [theme.breakpoints.down('sm')]: { minHeight: 44, minWidth: 44 },
  '&.Mui-focusVisible .reaction-pill': { outline: '2px solid', outlineOffset: 1 },
})

export default function TaskComments({
  taskId,
  subject: subjectProp,
  canPostInternal = false,
  attachments = false,
  onCountChange,
  testId = 'task-comments',
  active = true,
  readOnly = false,
}: {
  /** Shorthand for a task `subject`; one of the two is required. */
  taskId?: number
  /** What the thread hangs off; wins over `taskId`. */
  subject?: CommentSubject
  /** Staff on a ticket: offer the Reply/Internal note toggle and send `visibility` on post. */
  canPostInternal?: boolean
  /** Draw the subject's attachment row under the composer (a ticket thread). */
  attachments?: boolean
  /** Whether this panel is the visible tab; a deep-linked row scrolls only then. */
  active?: boolean
  /** Read the thread, write nothing: no composer, no reaction/edit/delete buttons. Another org's task for
   *  an operator (`pages/taskDetail/TaskDetailPage.tsx`), whose every comment write the server refuses. */
  readOnly?: boolean
  /** Reports the count to the tab strip. */
  onCountChange?: (count: number) => void
  testId?: string
}): JSX.Element {
  // Its hrefs are rewritten on the live host (`apiHref`): re-render when the rewriter is installed.
  useApiRewriterVersion()
  // Primitives for the hook deps: a caller's fresh `subject` literal per render must not refetch.
  const kind = subjectProp?.kind ?? 'task'
  const id = subjectProp?.id ?? (taskId as number)
  const base = subjectBase({ kind, id })
  const [visibility, setVisibility] = useState<CommentVisibility>('public')
  const internal = canPostInternal && visibility === 'internal'
  const theme = useTheme()
  const [comments, setComments] = useState<TaskComment[]>([])
  /* Offered to a remount across the split width (`hooks/useLandedTaskPart.ts`): the list shown once a read has
     landed, unless a read is in flight. */
  const [loaded, setLoaded] = useState(false)
  const reads = useRef(0)
  const reading = useMemo(() => ({ get current() { return reads.current > 0 } }), [])
  const [candidates, setCandidates] = useState<MentionCandidate[]>([])
  const [draft, setDraft] = useState('')
  const [editing, setEditing] = useState<{ id: number; body: string } | null>(null)
  /** The open reaction palette: its anchor (the row's React button) and the comment it reacts to. */
  const [palette, setPalette] = useState<{ anchor: HTMLElement; commentId: number } | null>(null)
  const { busy, formError: error, setFormError: setError, run } = useSubmit()
  /* Its own line: a reaction's failure must neither clear nor pose as the composer's. */
  const [reactError, setReactError] = useState('')
  // Hooks run for both subjects; only a task's composer is handed them (`withImages`).
  const withImages = kind === 'task'
  const draftImages = useCommentImages(id, setError)
  const editImages = useCommentImages(id, setError)
  const [confirm, confirmDialog] = useConfirm()
  const { hash, key: navigation } = useLocation()
  /** The deep-linked comment while its tint shows; `null` once it has faded. */
  const [highlighted, setHighlighted] = useState<number | null>(null)
  const scrolledFor = useRef<string | null>(null)

  /* `Array.isArray`, not trust: the panel is mounted on every task page, so a non-list answer throwing in
   * render would take down the whole page. */
  const load = useCallback(async () => {
    reads.current += 1
    let rows: TaskComment[] | undefined
    try {
      rows = (kind === 'task' ? takeTaskPageSeed<TaskComment[]>(id, 'comments') : undefined)
        ?? await get<TaskComment[]>(`${base}/comments`)
    } catch (err) {
      setLoaded(false)
      throw err
    } finally {
      reads.current -= 1
    }
    const list = Array.isArray(rows) ? rows : []
    setComments(list)
    setLoaded(true)
    onCountChange?.(list.length)
  }, [base, kind, id, onCountChange])

  useEffect(() => {
    load().catch(err => setError(apiDetail(err, 'Could not load comments')))
  }, [load])
  useLandedTaskPart(kind === 'task' ? id : null, 'comments', loaded ? comments : undefined, reading)

  useEffect(() => {
    const match = /^#comment-(\d+)$/.exec(hash)
    if (match === null || !active || scrolledFor.current === navigation) return
    const id = Number(match[1])
    // Not consumed until the row exists (the first render precedes the load).
    if (!comments.some(c => c.id === id)) return
    scrolledFor.current = navigation
    document.getElementById(`comment-${id}`)?.scrollIntoView({ block: 'center' })
    setHighlighted(id)
  }, [hash, navigation, active, comments])

  // Its own effect, so a reload mid-fade cannot cancel the timer and leave the tint on.
  useEffect(() => {
    if (highlighted === null) return
    const timer = setTimeout(() => setHighlighted(null), 2000)
    return () => clearTimeout(timer)
  }, [highlighted])

  /* The mention roster is fetched on first composer focus, not on mount: the server asks an access
   * question PER COLLEAGUE (`app/comment_mentions.py::mentionable_users`) and the panel mounts on every
   * task page. Filtered client-side after. Per task: the server narrows to people this task admits, so an
   * offered name always notifies. Failure is silent — mentions just stop autocompleting.
   * An internal note asks with `?visibility=internal`: its readers (staff) are not a reply's. */
  // While editing, the comment's own visibility picks the roster, not the toggle's.
  const editingComment = editing === null ? undefined : comments.find(c => c.id === editing.id)
  const rosterInternal = editingComment ? editingComment.visibility === 'internal' : internal
  const rosterUrl = `${base}/mention-candidates${rosterInternal ? '?visibility=internal' : ''}`
  const rosterAsked = useRef<string | null>(null)
  const ensureCandidates = useCallback(() => {
    if (rosterAsked.current === rosterUrl) return
    rosterAsked.current = rosterUrl
    get<MentionCandidate[]>(rosterUrl)
      // A toggle mid-flight must not land the other roster.
      .then(rows => { if (rosterAsked.current === rosterUrl) setCandidates(Array.isArray(rows) ? rows : []) })
      .catch(() => undefined)
  }, [rosterUrl])
  // A subject switch (or the visibility toggle) invalidates the roster.
  useEffect(() => {
    rosterAsked.current = null
    setCandidates([])
  }, [rosterUrl])

  async function submit() {
    const body = draft.trim()
    const imageIds = withImages ? draftImages.ids : []
    if (!body && imageIds.length === 0) return
    await run('Could not post comment', async () => {
      // `visibility` only where the toggle exists; `image_ids` only on a task.
      await post(
        `${base}/comments`,
        withImages ? { body, image_ids: imageIds } : canPostInternal ? { body, visibility } : { body },
      )
      // Cleared only on success, so a failed post never loses the text or the images; only the sent ones.
      setDraft('')
      draftImages.clear(imageIds)
      await load()
    })
  }

  function startEdit(comment: TaskComment) {
    setEditing({ id: comment.id, body: comment.body })
    editImages.reset(comment.images ?? [])
  }

  async function saveEdit() {
    if (editing === null) return
    const body = editing.body.trim()
    const imageIds = withImages ? editImages.ids : []
    if (!body && imageIds.length === 0) return
    await run('Could not save comment', async () => {
      await patch(`${base}/comments/${editing.id}`, withImages ? { body, image_ids: imageIds } : { body })
      setEditing(null)
      editImages.clear(imageIds)
      await load()
    })
  }

  const remove = useCallback(async (comment: TaskComment) => {
    /* Confirmed: deletion is unrecoverable (no tombstone, no undo). The message quotes the comment. */
    const ok = await confirm({
      title: 'Delete comment?',
      message: comment.body.length > 120 ? `${comment.body.slice(0, 119)}…` : comment.body || 'Image',
      confirmLabel: 'Delete',
      destructive: true,
    })
    if (!ok) return
    setError('')
    try {
      await del(`${base}/comments/${comment.id}`)
      await load()
    } catch (err) {
      setError(apiDetail(err, 'Could not delete comment'))
    }
  }, [confirm, base, load])

  /* Toggle: DELETE when mine, else PUT. The answer is the updated comment; anything else (an older server's
     204) falls back to a reload. */
  const react = useCallback(async (comment: TaskComment, key: string) => {
    setPalette(null)
    setReactError('')
    const mine = (comment.reactions ?? []).some(r => r.emoji === key && r.mine)
    const url = `${base}/comments/${comment.id}/reactions/${key}`
    try {
      const updated = mine ? await del<TaskComment>(url) : await put<TaskComment>(url, undefined)
      if (updated && typeof updated === 'object' && updated.id === comment.id) {
        setComments(prev => prev.map(c => (c.id === updated.id ? updated : c)))
      } else {
        await load()
      }
    } catch (err) {
      setReactError(apiDetail(err, 'Could not react'))
    }
  }, [base, load])

  const paletteComment = palette === null ? undefined : comments.find(c => c.id === palette.commentId)

  return (
    <Stack spacing={0.5} data-testid={testId} onFocusCapture={ensureCandidates}>
      {comments.length === 0 && editing === null && (
        <Typography variant="body2" sx={{ color: 'text.secondary' }} data-testid="task-comments-empty">
          No comments yet.
        </Typography>
      )}

      {comments.map(comment => {
        const isInternal = comment.visibility === 'internal'
        return (
          <Box
            key={comment.id}
            id={`comment-${comment.id}`}
            data-testid="task-comment"
            data-highlighted={highlighted === comment.id ? 'true' : undefined}
            data-internal={isInternal ? 'true' : undefined}
            sx={{
              display: 'flex',
              alignItems: 'flex-start',
              gap: 0.75,
              py: 0.4,
              // A hairline separates comments; a card each would waste space.
              borderTop: '1px solid',
              borderColor: 'divider',
              '&:first-of-type': { borderTop: 'none' },
              // Internal: the warning tint (as `pages/Settings.tsx`), so it never reads as a reply.
              bgcolor: highlighted === comment.id
                ? 'action.selected'
                : isInternal ? alpha(theme.palette.warning.main, 0.12) : 'transparent',
              transition: 'background-color 600ms',
              // And a solid rule down its edge, so it reads as staff-only at a glance, not by hue alone.
              ...(isInternal ? { borderLeft: '3px solid', borderLeftColor: 'warning.main', pl: 0.75 } : null),
            }}
          >
            <AuthorBadge
              name={comment.author_name}
              sx={BADGE_SX}
              testId="task-comment-author"
            />
            <Box sx={{ minWidth: 0, flex: 1 }}>
              <Stack direction="row" spacing={0.75} sx={{ alignItems: 'center', minHeight: 18 }}>
                {/* The name is shown where the byline has room; dropped below 480px, where the badge alone does it. */}
                <Typography
                  variant="caption"
                  sx={{
                    color: 'text.secondary',
                    fontWeight: 600,
                    whiteSpace: 'nowrap',
                    overflow: 'hidden',
                    textOverflow: 'ellipsis',
                    maxWidth: '14rem',
                    '@media (max-width:480px)': { display: 'none' },
                  }}
                >
                  {comment.author_name}
                </Typography>
                <Typography
                  variant="caption"
                  /* Who exactly and when exactly, on hover. */
                  title={`${comment.author_name} · ${absoluteTime(comment.created_at)}`}
                  sx={{ color: 'text.secondary', whiteSpace: 'nowrap' }}
                >
                  {relativeAge(comment.created_at)}
                </Typography>
                {comment.edited_at !== null && (
                  <Typography
                    variant="caption"
                    sx={{ color: 'text.disabled' }}
                    title={`Edited ${absoluteTime(comment.edited_at)}`}
                    data-testid="task-comment-edited"
                  >
                    edited
                  </Typography>
                )}
                {isInternal && (
                  <Typography
                    variant="caption"
                    sx={{ color: 'warning.dark', fontWeight: 600, whiteSpace: 'nowrap' }}
                    data-testid="task-comment-internal"
                  >
                    Internal note
                  </Typography>
                )}
              </Stack>
              {editing?.id === comment.id ? (
                /* Edit in place: the surrounding comments are the context. */
                <CommentComposer
                  value={editing.body}
                  onChange={body => setEditing({ id: comment.id, body })}
                  onSubmit={() => { void saveEdit() }}
                  onCancel={() => {
                    setEditing(null)
                    editImages.reset()
                  }}
                  candidates={candidates}
                  busy={busy}
                  autoFocus
                  testId="task-comment-editor"
                  {...(withImages
                    ? { images: editImages.images, onAddImages: editImages.add, onRemoveImage: editImages.remove }
                    : {})}
                />
              ) : (
                <>
                  {comment.body !== '' && (
                    <CommentText
                      body={comment.body}
                      mentions={comment.mentions}
                      sx={{ fontSize: '0.8125rem' }}
                    />
                  )}
                  {withImages && (comment.images ?? []).length > 0 && (
                    <Box sx={{ display: 'flex', flexWrap: 'wrap', gap: 0.5, mt: 0.5 }}>
                      {(comment.images ?? []).map(image => (
                        <Box
                          key={image.id}
                          component="a"
                          href={commentImageUrl(id, image.id)}
                          target="_blank"
                          rel="noopener"
                          title={image.filename}
                          sx={{ display: 'block', maxWidth: '100%', lineHeight: 0 }}
                        >
                          <Box
                            component="img"
                            src={commentImageUrl(id, image.id)}
                            alt={image.filename}
                            loading="lazy"
                            data-testid="task-comment-image"
                            sx={{
                              maxHeight: 160,
                              maxWidth: '100%',
                              borderRadius: 1,
                              border: '1px solid',
                              borderColor: 'divider',
                            }}
                          />
                        </Box>
                      ))}
                    </Box>
                  )}
                </>
              )}
              {/* One line under the message: reaction chips left, the row's actions right. Not while editing:
              the editor's own Save/Cancel own that spot, and a stray Edit would discard the draft. */}
              {editing?.id !== comment.id && (
                <Box sx={{ display: 'flex', alignItems: 'center', flexWrap: 'wrap', gap: 0.5, mt: 0.25 }}>
                  <ReactionChips
                    reactions={comment.reactions ?? []}
                    onToggle={key => { void react(comment, key) }}
                    disabled={readOnly}
                  />
                  {!readOnly && (
                  <Box sx={ACTIONS_SX}>
                    <Tooltip title="React">
                      <IconButton
                        size="small"
                        aria-label="Add reaction"
                        aria-haspopup="true"
                        aria-expanded={palette?.commentId === comment.id}
                        data-testid="task-comment-react"
                        onClick={e => setPalette({ anchor: e.currentTarget, commentId: comment.id })}
                        sx={ACTION_BUTTON_SX}
                      >
                        <Box component="span" aria-hidden>{ACTION_ICON.react}</Box>
                      </IconButton>
                    </Tooltip>
                    {comment.author_is_me && (
                      <Tooltip title="Edit">
                        <IconButton
                          size="small"
                          aria-label="Edit comment"
                          onClick={() => startEdit(comment)}
                          sx={ACTION_BUTTON_SX}
                        >
                          <Box component="span" aria-hidden>{ACTION_ICON.edit}</Box>
                        </IconButton>
                      </Tooltip>
                    )}
                    {comment.may_delete && (
                      <Tooltip title="Delete">
                        <IconButton
                          size="small"
                          aria-label="Delete comment"
                          onClick={() => { void remove(comment) }}
                          sx={ACTION_BUTTON_SX}
                        >
                          <Box component="span" aria-hidden>{ACTION_ICON.delete}</Box>
                        </IconButton>
                      </Tooltip>
                    )}
                  </Box>
                  )}
                </Box>
              )}
            </Box>
          </Box>
        )
      })}

      {/* One palette for the list, anchored to whichever row opened it. */}
      <Popover
        open={paletteComment !== undefined}
        anchorEl={palette?.anchor}
        onClose={() => setPalette(null)}
        anchorOrigin={{ vertical: 'bottom', horizontal: 'right' }}
        transformOrigin={{ vertical: 'top', horizontal: 'right' }}
        slotProps={{ paper: { role: 'group', 'aria-label': 'Reactions', sx: { p: 0.25 } } }}
      >
        {/* Wraps rather than overflows: 8 × the 44px floor is ~356px, a 390px phone's popover limit. */}
        <Box sx={{ display: 'flex', flexWrap: 'wrap' }}>
          {paletteComment && REACTIONS.map(r => {
            const mine = (paletteComment.reactions ?? []).some(x => x.emoji === r.key && x.mine)
            return (
              <Tooltip key={r.key} title={r.label}>
                <IconButton
                  aria-label={r.label}
                  aria-pressed={mine}
                  data-testid={`task-comment-reaction-pick-${r.key}`}
                  onClick={() => { void react(paletteComment, r.key) }}
                  sx={{
                    width: 32,
                    height: 32,
                    fontSize: 18,
                    lineHeight: 1,
                    borderRadius: 1,
                    // Already mine: picking it again removes it, so say so before the click.
                    bgcolor: mine ? alpha(theme.palette.primary.main, 0.12) : undefined,
                  }}
                >
                  <Box component="span" aria-hidden>{r.glyph}</Box>
                </IconButton>
              </Tooltip>
            )
          })}
        </Box>
      </Popover>

      {error && (
        <Typography variant="caption" color="error" role="alert">{error}</Typography>
      )}
      {reactError && (
        <Typography variant="caption" color="error" role="alert" data-testid="task-comment-react-error">
          {reactError}
        </Typography>
      )}

      {canPostInternal && (
        <SegmentedControl<CommentVisibility>
          value={visibility}
          ariaLabel="Visibility"
          testId="task-comment-visibility"
          sx={{ alignSelf: 'flex-start' }}
          buttonSx={{ py: 0.25, textTransform: 'none' }}
          onChange={setVisibility}
          options={[{ value: 'public', label: 'Reply' }, { value: 'internal', label: 'Internal note' }]}
        />
      )}

      {!readOnly && (
      <CommentComposer
        value={draft}
        onChange={setDraft}
        onSubmit={() => { void submit() }}
        candidates={candidates}
        placeholder={internal ? 'Internal note' : 'Comment'}
        busy={busy}
        testId="task-comment-new"
        {...(withImages
          ? { images: draftImages.images, onAddImages: draftImages.add, onRemoveImage: draftImages.remove }
          : {})}
      />
      )}
      {attachments && (
        <TaskAttachmentsField
          subject={{ kind, id }}
          visibility={canPostInternal ? visibility : undefined}
          testId="task-comment-attachments"
        />
      )}
      {confirmDialog}
    </Stack>
  )
}

/* The chips row: glyph + count, `mine` tinted primary (a second channel: `aria-pressed`). Nothing drawn for an
   unreacted comment. An emoji this build does not know (a newer server's) is skipped: a blank pill would be
   a control with no face. */
function ReactionChips({
  reactions,
  onToggle,
  disabled = false,
}: {
  reactions: TaskCommentReaction[]
  onToggle: (key: string) => void
  /** Shown, not pressable (a read-only thread). */
  disabled?: boolean
}): JSX.Element | null {
  const theme = useTheme()
  const known = reactions.filter(r => r.count > 0 && reactionGlyph(r.emoji) !== '')
  if (known.length === 0) return null
  return (
    <Box
      data-testid="task-comment-reactions"
      sx={{ display: 'flex', flexWrap: 'wrap', alignItems: 'center', gap: 0.5 }}
    >
      {known.map(r => (
        <Tooltip key={r.emoji} title={r.names.join(', ')}>
          <ButtonBase
            aria-label={`${reactionLabel(r.emoji)}, ${r.count}`}
            aria-pressed={r.mine}
            data-testid={`task-comment-reaction-${r.emoji}`}
            onClick={() => onToggle(r.emoji)}
            disabled={disabled}
            sx={chipSx}
          >
            <Box
              component="span"
              className="reaction-pill"
              sx={{
                display: 'inline-flex',
                alignItems: 'center',
                gap: 0.4,
                height: 22,
                px: 0.75,
                borderRadius: 999,
                border: '1px solid',
                borderColor: r.mine ? 'primary.main' : 'divider',
                bgcolor: r.mine ? alpha(theme.palette.primary.main, 0.12) : 'transparent',
                transition: 'background-color 120ms, border-color 120ms',
                '&:hover': {
                  borderColor: r.mine ? 'primary.main' : 'text.secondary',
                  bgcolor: alpha(theme.palette.primary.main, r.mine ? 0.18 : 0.06),
                },
              }}
            >
              <Box component="span" aria-hidden sx={{ fontSize: 13, lineHeight: 1 }}>{reactionGlyph(r.emoji)}</Box>
              <Typography
                component="span"
                variant="caption"
                aria-hidden
                sx={{
                  lineHeight: 1,
                  fontWeight: 600,
                  fontVariantNumeric: 'tabular-nums',
                  color: r.mine ? 'primary.main' : 'text.secondary',
                }}
              >
                {r.count}
              </Typography>
            </Box>
          </ButtonBase>
        </Tooltip>
      ))}
    </Box>
  )
}
