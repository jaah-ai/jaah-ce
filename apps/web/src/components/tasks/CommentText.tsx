/* THE COMMENT RENDERER — one task comment's body (or one website-chat message, `WebsiteChatsTab`) drawn
 * from `lib/commentText.ts`'s blocks and tokens; NOTHING here parses. Change what a comment MEANS in the
 * lib; change what it LOOKS LIKE here.
 *
 * NOTHING BECOMES MARKUP: every token's `text` is a React child, and there is no
 * `dangerouslySetInnerHTML` — adding one would be the injection hole the tokenizer exists to close (a
 * comment is text a stranger typed).
 *
 * DENSITY IS INHERITED: no `fontSize`, so it matches its host row. `LINE_HEIGHT.snug` is set (MUI's 1.5
 * makes a two-line comment look like two). `pre-wrap` draws the newlines the tokenizer keeps; `break-word`
 * stops a pasted long URL widening the column.
 *
 * A MENTION IS A TINTED SPAN, NOT A `Chip` (24px is taller than the line). The tint uses the CHANNEL FORM
 * (`--mui-palette-*Channel`), never `alpha(theme.palette.…)`, which cannot parse a CSS variable and
 * freezes at light.
 */
import { Fragment, type ReactNode } from 'react'
import Box from '@mui/material/Box'
import ExternalLink from '../../ui/ExternalLink'
import type { SxProps, Theme } from '@mui/material/styles'
import { commentBlocks, tokenizeComment } from '../../lib/commentText'
import { FONT_SIZE, FONT_WEIGHT, LINE_HEIGHT, RADIUS, tint } from '../../theme'
import type { TaskCommentMention } from '../../types'
import Tip from '../../ui/Tip'

const ROOT_SX: SxProps<Theme> = {
  whiteSpace: 'pre-wrap',
  wordBreak: 'break-word',
  lineHeight: LINE_HEIGHT.snug,
}

/* `action.selected`, not a named hue: a code span is not a STATUS. It is scheme-aware on its own.
   `FONT_SIZE.xs`: a step under the dense host rows it sits in. */
const CODE_SX = (theme: Theme) => ({
  fontFamily: theme.typography.mono.fontFamily,
  fontSize: FONT_SIZE.xs,
  bgcolor: 'action.selected',
  borderRadius: RADIUS.sm,
  px: 0.5,
})

/* No border: at this size a 1px outline reads as a text-input, and the tint already bounds the chip. */
const MENTION_SX: SxProps<Theme> = {
  color: 'primary.main',
  backgroundColor: tint('primary', 0.12),
  borderRadius: RADIUS.sm,
  px: 0.5,
  fontWeight: FONT_WEIGHT.medium,
}

const BOLD_SX: SxProps<Theme> = { fontWeight: FONT_WEIGHT.semibold }

/* No margin: the host spaces blocks (a transcript's `& > * + *`), and a list inside pre-wrap text must sit
   on the line the bullets occupy. */
const LIST_SX: SxProps<Theme> = { m: 0, pl: 2.5 }

/** One run of text as React children, a token each. */
function marks(text: string, mentions: readonly TaskCommentMention[]): ReactNode[] {
  return tokenizeComment(text, mentions).map((token, index) => {
    /* THE INDEX IS THE KEY, correctly: tokens are re-derived whole each render and hold no state. */
    switch (token.kind) {
      case 'bold':
        return (
          <Box key={index} component="strong" sx={BOLD_SX}>
            {token.text}
          </Box>
        )
      case 'code':
        return (
          <Box key={index} component="code" sx={CODE_SX}>
            {token.text}
          </Box>
        )
      case 'link':
        /* A label that is not the URL shows the real host, so a comment cannot dress one address as another. */
        return (
          <Tip key={index} title={token.href}><ExternalLink href={token.href}>
            {token.text}
            {token.text !== token.href && <Box component="span" sx={{ color: 'text.secondary' }}> ({new URL(token.href).host})</Box>}
          </ExternalLink></Tip>
        )
      case 'mention':
        return (
          <Box key={index} component="span" sx={MENTION_SX}>
            {token.text}
          </Box>
        )
      default:
        return token.text
    }
  })
}

export default function CommentText({
  body,
  mentions = [],
  sx,
  testId = 'comment-text',
}: {
  /** The RAW text the author typed, marks and all — never a pre-rendered string. */
  body: string
  /**
   * The people the SERVER resolved. Absent means "nobody": every `@word` renders literal, never a guessed
   * roster (the tokenizer's header is the single home).
   */
  mentions?: readonly TaskCommentMention[]
  /** The host's geometry, applied AFTER the defaults so it may override them. */
  sx?: SxProps<Theme>
  /** The spec handle; a surface with two bodies passes its own. */
  testId?: string
}): JSX.Element {
  return (
    /* An empty body still renders this box, so "no body" and "not loaded" look different. */
    <Box data-testid={testId} sx={[ROOT_SX, ...(Array.isArray(sx) ? sx : [sx])]}>
      {commentBlocks(body).map((block, index) =>
        block.kind === 'list' ? (
          <Box key={index} component="ul" sx={LIST_SX}>
            {block.items.map((item, i) => (
              <li key={i}>{marks(item, mentions)}</li>
            ))}
          </Box>
        ) : (
          <Fragment key={index}>{marks(block.text, mentions)}</Fragment>
        ),
      )}
    </Box>
  )
}
