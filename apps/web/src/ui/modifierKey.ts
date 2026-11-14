/* The platform's shortcut modifier as a glyph: ⌘ (Command) on Apple platforms, ⌃ (Control) elsewhere. Every
   shortcut the console names in prose (a button's key hint, a hover title) writes its key with these, never the
   words Cmd or Ctrl. */
const APPLE_PLATFORM = /^(Mac|iPhone|iPad|iPod)/

export function isApplePlatform(platform: string | undefined): boolean {
  return APPLE_PLATFORM.test(platform ?? '')
}

export function modifierGlyph(apple: boolean): string {
  return apple ? '⌘' : '⌃'
}

/** This browser's modifier glyph (⌃ where there is no `navigator`). */
export const MOD_KEY = modifierGlyph(typeof navigator !== 'undefined' && isApplePlatform(navigator.platform))

/** The Enter key's glyph. */
export const ENTER_KEY = '↵'

/** The submit shortcut (Cmd/Ctrl+Enter) as a button names it, e.g. ⌘↵. */
export const MOD_ENTER_LABEL = `${MOD_KEY}${ENTER_KEY}`
