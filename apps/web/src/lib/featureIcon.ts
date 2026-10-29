import type { TaskTree } from '../types'

/* A feature's optional icon: the token `"<shape>-<colour>"`, or null for "No icon".
 * The vocabulary's home is `apps/api/app/models/projects.py::FEATURE_ICONS` (the DDL check holds the column to it);
 * this is the console's copy, held to the same 24 by `featureIcon.test.ts`. Drawn by
 * `components/tasks/FeatureIcon.tsx`. */
export const FEATURE_ICON_SHAPES = ['dot', 'square', 'triangle'] as const
export const FEATURE_ICON_COLOURS = ['red', 'orange', 'yellow', 'green', 'teal', 'blue', 'purple', 'pink'] as const

export type FeatureIconShape = (typeof FEATURE_ICON_SHAPES)[number]
export type FeatureIconColour = (typeof FEATURE_ICON_COLOURS)[number]
export type ParsedFeatureIcon = { shape: FeatureIconShape; colour: FeatureIconColour }

/* Every token, shape-major — the order a palette grid lays them out in. */
export const FEATURE_ICONS: readonly string[] = FEATURE_ICON_SHAPES.flatMap((shape) =>
  FEATURE_ICON_COLOURS.map((colour) => `${shape}-${colour}`),
)

/* Per theme mode, each ≥3:1 (the WCAG non-text floor) against BOTH `background.default` and
 * `background.paper` of that mode (`theme.ts` RAW), and pairwise distinct — `featureIcon.test.ts` computes
 * both. Light takes darker tones (yellow is `#a16207`: a true yellow is ~1.5:1 on white). */
const HEX: Record<'light' | 'dark', Record<FeatureIconColour, string>> = {
  light: {
    red: '#dc2626',
    orange: '#ea580c',
    yellow: '#a16207',
    green: '#15803d',
    teal: '#0f766e',
    blue: '#2563eb',
    purple: '#9333ea',
    pink: '#db2777',
  },
  dark: {
    red: '#f87171',
    orange: '#fb923c',
    yellow: '#facc15',
    green: '#4ade80',
    teal: '#2dd4bf',
    blue: '#60a5fa',
    purple: '#c084fc',
    pink: '#f472b6',
  },
}

/* The token's shape and colour, or null for null, a blank or anything outside the 24. Never throws: a
 * token from a newer server renders as "No icon" rather than breaking the row. */
export function parseFeatureIcon(token: string | null | undefined): ParsedFeatureIcon | null {
  if (!token) return null
  const dash = token.indexOf('-')
  const shape = token.slice(0, dash) as FeatureIconShape
  const colour = token.slice(dash + 1) as FeatureIconColour
  if (dash < 0 || !FEATURE_ICON_SHAPES.includes(shape) || !FEATURE_ICON_COLOURS.includes(colour)) return null
  return { shape, colour }
}

export function featureIconColour(colour: FeatureIconColour, mode: 'light' | 'dark'): string {
  return HEX[mode][colour]
}

/* The icon a task row shows: its feature's, read off the task tree. The ONLY place a row gets one, so
 * every surface agrees. Null when the task has no feature, or the tree does not (yet) carry it. */
export function featureIconFor(
  tree: TaskTree | null | undefined,
  featureId: string | null | undefined,
): string | null {
  if (!tree || !featureId) return null
  for (const project of tree.projects) {
    const feature = project.features.find((f) => f.id === featureId)
    if (feature) return feature.icon ?? null
  }
  return null
}
