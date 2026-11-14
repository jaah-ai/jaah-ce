/* The console's ONE explanation shape: what a setting's `help` prop carries (`ui/HelpLabel` renders it).
 * A plain string is a tooltip alone; an `Explain` adds a pop-up ("More") with a short paragraph, one line per
 * choice (passed separately as `ChoiceHelp`) and a link to its reader-docs page. Copy budgets: the UI copy
 * standard (its explanation pop-up row). */

export type Explain = {
  /** The tooltip: what the setting does or changes, at most `TIP_MAX_WORDS` words. */
  tip: string
  /** The pop-up's paragraph (its budget: the UI copy standard's pop-up row). */
  more?: string
  /** A reader-docs route path, no leading/trailing slash, never ending `/index` (e.g. `using-jaah/projects`).
   *  It resolves to that page's markdown in the reader-docs content tree (`<path>.md`, `.mdx` or `<path>/index.md`). */
  docs?: string
}

export type HelpProp = string | Explain

/** One line of the pop-up: a choice of the setting and what picking it does (at most `TIP_MAX_WORDS` words). */
export type ChoiceHelp = { label: string; help: string }

export const TIP_MAX_WORDS = 10

export function toExplain(h: HelpProp | undefined): Explain | undefined {
  if (!h) return undefined
  return typeof h === 'string' ? { tip: h } : h
}

/** Whether the explanation has a pop-up to open: prose, choice lines or a docs page. */
export function hasMore(explain: Explain | undefined, choices?: readonly ChoiceHelp[]): boolean {
  return Boolean(explain && (explain.more || explain.docs || choices?.length))
}
