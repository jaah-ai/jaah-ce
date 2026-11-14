/* The viewer's reduced-motion preference, read at the moment it matters (a vote, a jump), not subscribed.
 * The one JS read of the query; CSS keeps its own `@media (prefers-reduced-motion: reduce)` rules, and this
 * read stops JS holding a state open for an animation that will not run (the rating's stars, 600ms). False
 * where there is no window or `matchMedia`; `test/setup.ts` stubs `matchMedia` at `matches: false`, so
 * tests take the animating path. */

export function prefersReducedMotion(): boolean {
  if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') return false
  return window.matchMedia('(prefers-reduced-motion: reduce)').matches === true
}
