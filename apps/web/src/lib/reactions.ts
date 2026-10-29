// THE COMMENT-REACTION VOCABULARY: a fixed quick set, in palette and chip order.
//
// MIRRORS `app/models/task_extras.py::REACTION_EMOJIS` — the wire carries the key, never the glyph, and a server
// test pins these keys to this file line by line, so keep each entry on its own line in this literal form.
// 👍 is simply the first entry: there is no separate Like.
//
// `label` is the button's name and the palette's tooltip: one word.

export type ReactionKey =
  | 'thumbs_up'
  | 'heart'
  | 'laugh'
  | 'tada'
  | 'surprised'
  | 'sad'
  | 'eyes'
  | 'rocket'

export const REACTIONS: readonly { key: ReactionKey; glyph: string; label: string }[] = [
  { key: 'thumbs_up', glyph: '👍', label: 'Like' },
  { key: 'heart', glyph: '❤️', label: 'Love' },
  { key: 'laugh', glyph: '😄', label: 'Laugh' },
  { key: 'tada', glyph: '🎉', label: 'Celebrate' },
  { key: 'surprised', glyph: '😮', label: 'Surprised' },
  { key: 'sad', glyph: '😢', label: 'Sad' },
  { key: 'eyes', glyph: '👀', label: 'Eyes' },
  { key: 'rocket', glyph: '🚀', label: 'Rocket' },
]

/** The glyph for a wire key; `''` for a key this build does not know (a newer server's). */
export function reactionGlyph(key: string): string {
  return REACTIONS.find(r => r.key === key)?.glyph ?? ''
}

/** The label for a wire key; the key itself for one this build does not know. */
export function reactionLabel(key: string): string {
  return REACTIONS.find(r => r.key === key)?.label ?? key
}
