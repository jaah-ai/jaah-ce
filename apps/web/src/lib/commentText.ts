/* THE COMMENT MICRO-FORMAT — the whole grammar a task comment understands, as one pure tokenizer.
 * FOUR MARKS AND NOTHING ELSE (bold `*x*`, line breaks, bare URLs, `code`); anything else is a product
 * decision, not a gap.
 * Returns DATA, never HTML: `components/tasks/CommentText.tsx` builds React elements, so nothing in a
 * comment reaches the DOM as markup. A rewrite must not lose that.
 * MENTIONS ARE RESOLVED SERVER-SIDE (`TaskComment.mentions`), never guessed: "@Ann Lee" is one person
 * or two words depending on a roster only the server holds.
 */

/** One run of comment text, already classified. `text` is ALWAYS the literal to display. */
export type CommentToken =
  | { kind: 'text'; text: string }
  | { kind: 'bold'; text: string }
  | { kind: 'code'; text: string }
  | { kind: 'link'; text: string; href: string }
  /** The NAME is the whole token; the wire carries no id (`routes/task_comments.py::MentionOut`). */
  | { kind: 'mention'; text: string }

export interface CommentMention {
  name: string
}

/* Names are user-supplied; unescaped, "A. Lee" would be a wildcard. */
function escapeRegExp(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
}

/* A bare URL runs to whitespace, then gives back trailing sentence punctuation, and `)` unless the URL
 * opened one — "(https://x.dev/a)." links `https://x.dev/a`. */
const URL_RE = /https?:\/\/[^\s<>]+/

export function trimUrl(raw: string): string {
  /* Index backwards counting brackets once: slice-and-rescan is quadratic, and this runs on every
     render (`https://a` + 8000 `)` cost ~10^8 ops). */
  let open = 0
  let close = 0
  for (const character of raw) {
    if (character === '(') open += 1
    else if (character === ')') close += 1
  }
  let end = raw.length
  while (end > 0) {
    const last = raw[end - 1]
    if ('.,;:!?'.includes(last)) {
      end -= 1
      continue
    }
    if (last === ')' && close > open) {
      end -= 1
      close -= 1
      continue
    }
    break
  }
  return raw.slice(0, end)
}

/**
 * Split `body` into display tokens.
 * PRECEDENCE IS CODE, MENTION, LINK, BOLD: code is literal (`` `*x*` `` shows asterisks), a mention like
 * "Bob *Ray*" stays one chip, and bold is loosest (`*` must not eat a URL).
 * Newlines stay INSIDE text tokens; the renderer draws them (`white-space: pre-wrap`).
 */
export function tokenizeComment(body: string, mentions: readonly CommentMention[] = []): CommentToken[] {
  // Longest name first: alternation is first-match-wins, so "@Ann Lee" beats "Ann".
  const names = [...mentions].sort((a, b) => b.name.length - a.name.length)
  const mentionAlt = names.map((m) => escapeRegExp(m.name)).join('|')
  const parts = [
    '`[^`\\n]+`',
    ...(mentionAlt ? [`@(?:${mentionAlt})`] : []),
    URL_RE.source,
    '\\*[^*\\n]+\\*',
  ]
  const scanner = new RegExp(parts.join('|'), 'g')

  const tokens: CommentToken[] = []
  let cursor = 0
  const pushText = (text: string): void => {
    if (!text) return
    const previous = tokens[tokens.length - 1]
    if (previous?.kind === 'text') previous.text += text
    else tokens.push({ kind: 'text', text })
  }

  for (let match = scanner.exec(body); match !== null; match = scanner.exec(body)) {
    const raw = match[0]
    pushText(body.slice(cursor, match.index))
    cursor = match.index + raw.length

    if (raw.startsWith('`')) {
      tokens.push({ kind: 'code', text: raw.slice(1, -1) })
    } else if (raw.startsWith('@')) {
      tokens.push({ kind: 'mention', text: raw })
    } else if (raw.startsWith('*')) {
      tokens.push({ kind: 'bold', text: raw.slice(1, -1) })
    } else {
      const href = trimUrl(raw)
      tokens.push({ kind: 'link', text: href, href })
      pushText(raw.slice(href.length))
    }
  }
  pushText(body.slice(cursor))
  return tokens
}

/** Plain reading (Slack line, `title`): marks stripped, mentions keep `@Name`. */
export function commentPlainText(body: string, mentions: readonly CommentMention[] = []): string {
  return tokenizeComment(body, mentions)
    .map((token) => token.text)
    .join('')
}
