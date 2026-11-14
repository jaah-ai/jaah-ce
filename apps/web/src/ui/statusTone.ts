import type { Tone } from './StatusChip'

/** A vocabulary token as the console shows it: its word, its tone, and its state-mark glyph — one record per
 *  vocabulary, never parallel label/tone/glyph maps. */
export type Presentation = { label: string; tone: Tone; glyph: string }

/* The one badge-token → tone map for the console: a page reads the tone, never the hue. Each entry
 * transcribes its `styles.css` hue onto the nearest MUI semantic:
 *   green → success   red → error   orange → warning   blue/purple → info   gray → neutral
 * Blue and purple share `info` (MUI has one informational colour); distinguish them by label (AC4a).
 * Keys are the `styles.css` badge tokens verbatim. An unknown token yields `undefined`; callers fall back
 * to `'neutral'`, as the stylesheet's plain `.badge` did.
 */
export const STATUS_TONE: Record<string, Tone> = {
  // Worktree slot occupancy is not here: `lib/poolSlot.ts::stateTone` owns it. `badge-orchestrator` is
  // kept for the tree table's "system task" marker.
  'badge-orchestrator': 'info',

  // ── Command lifecycle ───────────────────────────────────────────────────────────────────────────
  'badge-queued': 'neutral',
  'badge-running': 'info',
  'badge-done': 'success',
  'badge-failed': 'error',
  'badge-rejected': 'warning',
  'badge-idle': 'neutral',
  /* Neutral: `dispatched` means no reply came back inside the timeout (`routes/commands.py`), so the
  outcome is unknown; green or red would be a guess. */
  'badge-dispatched': 'neutral',

  // ── Task / phase / epic status (the tracker vocabulary) ─────────────────────────────────────────
  'badge-not-done': 'neutral',
  'badge-in-progress': 'info',
  'badge-blocked': 'warning',
  'badge-awaiting-answer': 'info',

  // ── Node presence (tri-state: online / offline / unknown, never conflated) ──────────────────────
  'badge-node-online': 'success',
  'badge-node-offline': 'error',
  'badge-node-unknown': 'neutral',

  /* ── Node host pressure: the CPU/RAM meter's levels ─────────────────────
  `unknown` is neutral because a stale reading reports `unknown`; green would let a silent host read as
  healthy. There is no token per percentage: which level a percentage is belongs to the server
  (`apps/api/app/node_host_metrics.py::classify`). */
  'badge-node-host-ok': 'success',
  'badge-node-host-warn': 'warning',
  'badge-node-host-critical': 'error',
  'badge-node-host-unknown': 'neutral',
}

/** Tone → its palette PATH (never a hex or a `theme.palette` read: under CSS-variable theming that is a
 *  frozen light-mode literal). `neutral` reads as muted text. */
export const TONE_COLOR: Record<Tone, string> = {
  neutral: 'text.secondary',
  info: 'info.main',
  success: 'success.main',
  warning: 'warning.main',
  error: 'error.main',
}

/** A status word → its tone through one map, with the console's one fallback: an unknown or absent word is `neutral`. */
export function toneOf(map: Readonly<Partial<Record<string, Tone>>>, key: string | null | undefined, fallback: Tone = 'neutral'): Tone {
  return (key != null && Object.hasOwn(map, key) && map[key]) || fallback
}

/** A status word → its presentation through one map, with the caller's one fallback: an unknown, absent or
 *  inherited word reads as `fallback` (a newer vocabulary renders as itself in grey, never as a crash). */
export function presentationOf(map: Readonly<Partial<Record<string, Presentation>>>, key: string | null | undefined, fallback: Presentation): Presentation {
  return (key != null && Object.hasOwn(map, key) && map[key]) || fallback
}

/** A badge token → its tone, with the stylesheet's own "no rule matched" fallback made explicit. */
export function toneFor(badgeToken: string | null | undefined): Tone {
  return toneOf(STATUS_TONE, badgeToken)
}
