/* The AUTHOR badge: a task creator as two initials, tinted per author (the Author column is too narrow
 * for a name, and the title must not shrink to buy width). Full name rides `title`/the
 * accessible name.
 * Two letters collide; the per-author HUE disambiguates. Growing the label on collision is unstable (the
 * lazy-loaded window changes the author set as you scroll); a fixed three letters only moves the
 * collision. Colour is never the only channel: the accessible name and `title` carry the full name.
 * Pure of React so it is unit-testable without a render. */
import { firstCodepoints } from './strings'

/* The PERSON a label names: an operator acting as an org reads their colleagues' writes as "Debug Admin, acting
 * as Acme Corp" (`apps/api/app/actors.py::_acting_labels`); badge it as Debug Admin — `DA`, in Debug Admin's hue —
 * not as `DC` in a hue of its own. The task page's Assigned to prints the same person. */
export function personOf(name: string): string {
  return name.split(', acting as ')[0]
}

/**
 * A creator's display name → two upper-case letters; `''` for null/absent/whitespace (caller draws its
 * own placeholder).
 * Two+ words: first letters of the first and LAST word ("Ada B. Lovelace" → `AL`). One word: its first
 * two (`System` must not read as a truncated `S`).
 * Capped at two AFTER upper-casing: a ligature upper-cases into two chars (`'ﬁnance'` → `'FI'` + `'N'`).
 */
export function authorInitials(name: string | null | undefined): string {
  if (typeof name !== 'string') return ''
  const words = personOf(name).trim().split(/\s+/).filter(word => word !== '')
  if (words.length === 0) return ''
  const letters =
    words.length === 1
      ? firstCodepoints(words[0], 2)
      : `${firstCodepoints(words[0], 1)}${firstCodepoints(words[words.length - 1], 1)}`
  return firstCodepoints(letters.toUpperCase(), 2)
}

/** One author hue: the literal light/dark pair the badge paints in each colour scheme. */
export type AuthorHue = { light: string; dark: string }

/* Literal hex, not palette families: every semantic family already means a task state on this row, and
 * an author's hue must mean nothing. Each entry carries its dark twin because under `cssVariables` a
 * single literal in `sx` freezes at the rendering scheme; the badge applies it via
 * `theme.applyStyles('dark', …)`. Light ~600-step (on `#ffffff`), dark ~300-step (on `#1e293b`). */
export const AUTHOR_HUES: readonly AuthorHue[] = [
  { light: '#1d4ed8', dark: '#93b4fd' }, // blue
  { light: '#047857', dark: '#6ee7b7' }, // emerald
  { light: '#b45309', dark: '#fcd34d' }, // amber
  { light: '#a21caf', dark: '#f0abfc' }, // fuchsia
  { light: '#0e7490', dark: '#67e8f9' }, // cyan
  { light: '#c2410c', dark: '#fdba74' }, // orange
  { light: '#6d28d9', dark: '#c4b5fd' }, // violet
  { light: '#4d7c0f', dark: '#bef264' }, // lime
] as const

/**
 * A creator's display name → a stable index into `AUTHOR_HUES`. Hashes the WHOLE name, not the
 * initials, so two `RZ`s disagree. djb2: a colour, not cryptography.
 */
export function authorHueIndex(name: string | null | undefined): number {
  if (typeof name !== 'string' || name.trim() === '') return 0
  name = personOf(name)
  let hash = 5381
  for (let i = 0; i < name.length; i += 1) {
    // `| 0` keeps the hash 32-bit so long names hash consistently.
    hash = ((hash * 33) ^ name.charCodeAt(i)) | 0
  }
  return Math.abs(hash) % AUTHOR_HUES.length
}

/** The hue for a name — `authorHueIndex` resolved. The one function a renderer needs. */
export function authorHue(name: string | null | undefined): AuthorHue {
  return AUTHOR_HUES[authorHueIndex(name)]
}
