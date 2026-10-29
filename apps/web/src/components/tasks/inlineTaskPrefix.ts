/* ── The inline create row's TYPED PREFIXES ────────────────────────────────────────────────────────
 *
 * Type `Chat:` at the front of the create box and it becomes a **Chat** chip; Enter then launches the
 * advisor chat instead of a task. This module is the vocabulary plus two pure functions — no React, no
 * api — so it stays a LEAF (`taskPersona.ts`): it loads on `/tasks`, the busiest route.
 *
 * A TABLE (operator's call), so another prefix is a row of data, not a parser branch. A prefix carries no
 * permission: gating is per LAUNCH (`useInlineTaskCreate`'s `intentAllowed`).
 *
 * THE PREFIX IS NOT SEPARATE STATE: the persisted line (`lib/taskDraftStorage.ts`) keeps it
 * (`chat:fix the login bug`); chip and box are DERIVED by `splitInlinePrefix` and written back by
 * `joinInlinePrefix`, so a restore can never drop the chip and turn a chat into a task.
 */

/** The chat launch tooltip, one home for the 💬 button and a Chat chip's ▶ (the identical write).
 *  Accessible NAMES stay separate: different presses, one outcome. */
export const CHAT_LAUNCH_TITLE = 'Create an advisor chat from this and start it'

/** Which launch a prefix selects. Mirrors `InlineTaskCreateIntent` in `./useInlineTaskCreate`,
 *  spelled here so this module imports nothing; `./inlineTaskPrefix.test.ts` pins the pair. */
export type InlineTaskPrefixIntent = 'start' | 'chat'

export type InlineTaskPrefix = {
  /** The word before the colon, LOWERCASE (matching is case-insensitive; `joinInlinePrefix` writes this). */
  word: string
  /** The chip's text; `word` is what the parser matches. */
  label: string
  /** Which launch the chip selects — what Enter and ▶ then do. */
  intent: InlineTaskPrefixIntent
  /** Replaces the box placeholder (the default is "New task…"). */
  placeholder: string
  /** The launch button's accessible name while the chip is up — it must say what the press DOES. */
  startLabel: string
  /** Its tooltip (`CHAT_LAUNCH_TITLE`). */
  startTitle: string
  /** The box's accessible name while the chip is up: an input `aria-label` overrides the placeholder. */
  inputLabel: string
  /** How to remove the chip, as a sentence for `aria-describedby` (not a hover `title`). */
  clearHint: string
}

/* THE TABLE. `Chat:` launches the same advisor chat as the 💬 button, same payload
   (`useInlineTaskCreate`'s `createBodyFor`). */
export const INLINE_TASK_PREFIXES: readonly InlineTaskPrefix[] = [
  {
    word: 'chat',
    label: 'Chat',
    intent: 'chat',
    placeholder: 'Ask the advisor…',
    startLabel: 'Save and start this chat',
    startTitle: CHAT_LAUNCH_TITLE,
    inputLabel: 'Advisor chat message',
    clearHint: 'Chat prefix on. Backspace at the start removes it.',
  },
]

/* Letters then a colon, at position 0 only ("ask chat: x" stays a task; "12:30 standup" is never a
   candidate). The word must match a table row; unknown `foo:` is plain text. */
const PREFIX_PATTERN = /^([A-Za-z]+):/

export type InlineTaskPrefixSplit = {
  /** The prefix in force, or `null`. */
  prefix: InlineTaskPrefix | null
  /** What the operator typed — shown in the box and POSTed as the body. */
  body: string
}

/**
 * Read the stored line into chip and box. `available` (the prefixes this principal may use) is REQUIRED:
 * this function IS the gate, and a default would fail open (see `useInlineTaskCreate`'s `intentAllowed`).
 * Leading whitespace after the colon is swallowed, so `chat: x` ≡ `chat:x` and `join` round-trips.
 */
export function splitInlinePrefix(
  text: string,
  available: readonly InlineTaskPrefix[],
): InlineTaskPrefixSplit {
  const matched = PREFIX_PATTERN.exec(text)
  if (matched === null) return { prefix: null, body: text }
  const word = matched[1].toLowerCase()
  const prefix = available.find(candidate => candidate.word === word)
  if (prefix === undefined) return { prefix: null, body: text }
  return { prefix, body: text.slice(matched[0].length).replace(/^\s+/, '') }
}

/** Write chip and box back into the stored line, using `word` (canonical case). */
export function joinInlinePrefix(prefix: InlineTaskPrefix, body: string): string {
  return `${prefix.word}:${body}`
}

/**
 * Read a WHOLE line into the pair a surface renders. `splitInlinePrefix` is not injective: with
 * `chat:chat:x` stored, the box would show `chat:x` and Backspace-at-0 (drop the chip, keep the words)
 * would re-raise the chip and delete text. So every leading prefix token is consumed and the LAST wins;
 * the body can never re-parse and `joinInlinePrefix` is its exact inverse. Reached by pastes or a second
 * `chat:` typed under a chip.
 */
export function readInlinePrefixLine(
  text: string,
  available: readonly InlineTaskPrefix[],
): InlineTaskPrefixSplit {
  let prefix: InlineTaskPrefix | null = null
  let body = text
  for (;;) {
    const parsed = splitInlinePrefix(body, available)
    if (parsed.prefix === null) return { prefix, body }
    prefix = parsed.prefix
    body = parsed.body
  }
}
