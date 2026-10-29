import { PURPOSES, type TemplateCard } from './api'

/* THE GALLERY'S ORDER, React-free. The server sends the visible cards in purpose order then name (`GET
 * /api/templates`); these functions cut them into the sections the gallery draws. The Tasks board's starters are
 * its Most popular (`TemplateStarters.tsx`). */

export type GallerySection = { key: string; title: string; cards: TemplateCard[] }

/** How many cards Most popular shows, in the gallery and on the Tasks board. */
export const POPULAR_COUNT = 6

/** Past this many tasks (org-wide, unfiltered) the Tasks board is established: it draws no starters. */
export const STARTER_MAX_TASKS = 10

const byName = (a: TemplateCard, b: TemplateCard) => a.name.localeCompare(b.name)

/** The most-started cards, most starts first, ties by name. A card nobody started is not popular. */
export function mostPopular(cards: readonly TemplateCard[], count = POPULAR_COUNT): TemplateCard[] {
  return cards
    .filter(c => c.starts_90d > 0)
    .sort((a, b) => b.starts_90d - a.starts_90d || byName(a, b))
    .slice(0, count)
}

/** Favorites (if any), Most popular (if any), then one section per purpose group in gallery order, each only when
 *  it holds a card. `purpose` narrows to that one group's section. */
export function gallerySections(cards: readonly TemplateCard[], purpose: string | null = null): GallerySection[] {
  const groups = PURPOSES
    .filter(p => purpose === null || p.value === purpose)
    .map(p => ({ key: p.value, title: p.label, cards: cards.filter(c => c.purpose === p.value).sort(byName) }))
  if (purpose !== null) return groups.filter(s => s.cards.length > 0)
  const favorites = cards.filter(c => c.favorite).sort(byName)
  const popular = mostPopular(cards)
  return [
    { key: 'favorites', title: 'Favorites', cards: favorites },
    { key: 'popular', title: 'Most popular', cards: popular },
    ...groups,
  ].filter(s => s.cards.length > 0)
}
