/* THE COMMENT RENDERER — one task comment's body drawn from `lib/commentText.ts`'s tokens; NOTHING here
 * parses. Change what a comment MEANS in the lib (Slack's `commentPlainText` reads the same tokens);
 * change what it LOOKS LIKE here.
 *
 * NOTHING BECOMES MARKUP: every token's `text` is a React child, and there is no
 * `dangerouslySetInnerHTML` — adding one would be the injection hole the tokenizer exists to close (a
 * comment is text a stranger typed).
 *
 * DENSITY IS INHERITED: no `fontSize`, so it matches its host row. `lineHeight: 1.35` is set (MUI's 1.5
 * makes a two-line comment look like two). `pre-wrap` draws the newlines the tokenizer keeps; `break-word`
 * stops a pasted long URL widening the column.
 *
 * A MENTION IS A TINTED SPAN, NOT A `Chip` (24px is taller than the line). The tint uses the CHANNEL FORM
 * (`--mui-palette-*Channel`), never `alpha(theme.palette.…)`, which cannot parse a CSS variable and
 * freezes at light.
 */
import Box from '@mui/material/Box'
import Link from '@mui/material/Link'
import type { SxProps, Theme } from '@mui/material/styles'
import { tokenizeComment } from '../../lib/commentText'
import type { TaskCommentMention } from '../../types'

const ROOT_SX: SxProps<Theme> = {
  whiteSpace: 'pre-wrap',
  wordBreak: 'break-word',
  lineHeight: 1.35,
}

/* `action.selected`, not a named hue: a code span is not a STATUS. It is scheme-aware on its own.
   `0.9em` is relative; the host owns the size. */
const CODE_SX = (theme: Theme) => ({
  fontFamily: theme.typography.mono.fontFamily,
  fontSize: '0.9em',
  bgcolor: 'action.selected',
  borderRadius: '3px',
  px: '0.25rem',
})

/* No border: at this size a 1px outline reads as a text-input, and the tint already bounds the chip. */
const MENTION_SX: SxProps<Theme> = {
  color: 'primary.main',
  backgroundColor: 'rgba(var(--mui-palette-primary-mainChannel) / 0.12)',
  borderRadius: '3px',
  px: '0.1875rem',
  fontWeight: 500,
}

const BOLD_SX: SxProps<Theme> = { fontWeight: 600 }

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
      {tokenizeComment(body, mentions).map((token, index) => {
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
            /* `rel` is required beside `target="_blank"`: without `noopener` a stranger-supplied link gets
               `window.opener`. */
            return (
              <Link key={index} href={token.href} target="_blank" rel="noopener noreferrer">
                {token.text}
              </Link>
            )
          case 'mention':
            /* `title` is the name without the `@` (the chip shows the sigil). */
            return (
              <Box key={index} component="span" title={token.text.slice(1)} sx={MENTION_SX}>
                {token.text}
              </Box>
            )
          default:
            return token.text
        }
      })}
    </Box>
  )
}
