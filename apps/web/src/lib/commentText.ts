/* THE COMMENT MICRO-FORMAT — the whole grammar a task comment (and a website-chat transcript, the same
 * renderer) understands, as one pure tokenizer. SIX MARKS AND NOTHING ELSE: bold (`*x*` or `**x**`),
 * `code`, `[text](url)` links, bare URLs, line breaks, and `- ` bullet lines (`commentBlocks`); anything
 * else is a product decision, not a gap.
 * Returns DATA, never HTML: `components/tasks/CommentText.tsx` builds React elements, so nothing in a
 * comment reaches the DOM as markup. A rewrite must not lose that. A link is a link only when
 * `safeHref` approves its target (absolute http(s)); a refused one keeps its words, never the URL.
 * MENTIONS ARE RESOLVED SERVER-SIDE (`TaskComment.mentions`), never guessed: "@Ann Lee" is one person
 * or two words depending on a roster only the server holds.
 */
import { safeHref } from './safeHref'

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
export function escapeRegExp(value: string): string {
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

/** Plain text with only its bare URLs as links, every other character literal — for text that is not a comment
 *  (a control-plane question naming where to fix something, `checkout_gate.retry_refusal`). */
export function tokenizeUrls(text: string): CommentToken[] {
  const tokens: CommentToken[] = []
  const pushText = (value: string): void => {
    if (!value) return
    const previous = tokens[tokens.length - 1]
    if (previous?.kind === 'text') previous.text += value
    else tokens.push({ kind: 'text', text: value })
  }
  const scanner = new RegExp(URL_RE.source, 'g')
  let cursor = 0
  for (let match = scanner.exec(text); match !== null; match = scanner.exec(text)) {
    pushText(text.slice(cursor, match.index))
    cursor = match.index + match[0].length
    const trimmed = trimUrl(match[0])
    const href = safeHref(trimmed)
    if (href) tokens.push({ kind: 'link', text: trimmed, href })
    else pushText(trimmed)
    pushText(match[0].slice(trimmed.length))
  }
  pushText(text.slice(cursor))
  return tokens
}

/* A bullet line is `- x` or `* x`; a run of them is one list. */
const BULLET_RE = /^[ \t]*[-*] +/

export type CommentBlock = { kind: 'text'; text: string } | { kind: 'list'; items: string[] }

/** Split `body` into runs of plain lines (newlines kept, the renderer draws them) and bullet lists. */
export function commentBlocks(body: string): CommentBlock[] {
  const blocks: CommentBlock[] = []
  for (const line of body.split('\n')) {
    const last = blocks[blocks.length - 1]
    if (!BULLET_RE.test(line)) {
      if (last?.kind === 'text') last.text += `\n${line}`
      else blocks.push({ kind: 'text', text: line })
    } else if (last?.kind === 'list') last.items.push(line.replace(BULLET_RE, ''))
    else blocks.push({ kind: 'list', items: [line.replace(BULLET_RE, '')] })
  }
  return blocks
}

/**
 * Split `body` into display tokens.
 * PRECEDENCE IS CODE, MENTION, MARKDOWN LINK, BARE URL, BOLD: code is literal (`` `*x*` `` shows
 * asterisks), a mention like "Bob *Ray*" stays one chip, and bold is loosest (`*` must not eat a URL).
 * Newlines stay INSIDE text tokens; the renderer draws them (`white-space: pre-wrap`).
 */
export function tokenizeComment(body: string, mentions: readonly CommentMention[] = []): CommentToken[] {
  // Longest name first: alternation is first-match-wins, so "@Ann Lee" beats "Ann".
  const names = [...mentions].sort((a, b) => b.name.length - a.name.length)
  const mentionAlt = names.map((m) => escapeRegExp(m.name)).join('|')
  const parts = [
    '`[^`\\n]+`',
    ...(mentionAlt ? [`@(?:${mentionAlt})`] : []),
    '\\[([^[\\]\\n]{1,500})\\]\\(([^()\\s]{1,2000})\\)',
    URL_RE.source,
    '\\*\\*[^*\\n]+\\*\\*|\\*[^*\\n]+\\*',
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
    } else if (raw.startsWith('[')) {
      const [, label, url] = match
      const href = safeHref(url)
      if (href) tokens.push({ kind: 'link', text: label, href })
      else pushText(label)
    } else if (raw.startsWith('*')) {
      tokens.push({ kind: 'bold', text: raw.replace(/^\*+|\*+$/g, '') })
    } else {
      const trimmed = trimUrl(raw)
      const href = safeHref(trimmed)
      if (href) tokens.push({ kind: 'link', text: trimmed, href })
      else pushText(trimmed)
      pushText(raw.slice(trimmed.length))
    }
  }
  pushText(body.slice(cursor))
  return tokens
}
