/**
 * The console's active logo theme: `standard` (the Jaah mark as jaah.ai ships it) or a seasonal one. Every
 * themed surface reads this — `BrandMark`'s glyph, the `--sk-mark-*` colours in `theme.ts` (which paint
 * `BrandWordmark`), and the browser-tab favicon — so switching theme is this one line. The docs portal has
 * its own `LOGO_THEME` switch in its site config: flip both together.
 */
export type LogoTheme = 'standard' | 'halloween'

export const LOGO_THEME: LogoTheme = 'halloween'

/* The tab icon per theme; `null` keeps index.html's own (the standard mark). Each mirrors its theme's
   `BrandMark` glyph, flattened to literal colours. */
const FAVICON: Record<LogoTheme, string | null> = {
  standard: null,
  halloween:
    "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'><defs>" +
    "<linearGradient id='g' x1='0' y1='0' x2='32' y2='32' gradientUnits='userSpaceOnUse'>" +
    "<stop offset='0' stop-color='#fdba74'/><stop offset='0.55' stop-color='#f97316'/><stop offset='1' stop-color='#c2410c'/>" +
    "</linearGradient></defs>" +
    "<path d='M15 4.5C15 2.5 16 1 18.5.5l.7 1.3C17.6 2.4 17 3.3 17 4.5Z' fill='#15803d'/>" +
    "<rect y='3.5' width='32' height='28.5' rx='8' fill='url(#g)'/>" +
    "<g fill='#431407'><rect x='17.25' y='6.5' width='7' height='6.5' rx='1.5'/>" +
    "<rect x='17.25' y='15' width='7' height='6.5' rx='1.5'/><rect x='17.25' y='23.5' width='7' height='6.5' rx='1.5'/></g>" +
    "<rect x='7.75' y='23.5' width='7' height='6.5' rx='1.5' fill='#fde047'/></svg>",
}

/** Points the tab icon at the active theme's favicon; a no-op under `standard`. Called once at boot. */
export function applyLogoFavicon(doc: Document = document): void {
  const svg = FAVICON[LOGO_THEME]
  const link = doc.querySelector<HTMLLinkElement>('link[rel="icon"]')
  if (svg && link) link.href = `data:image/svg+xml,${encodeURIComponent(svg)}`
}
