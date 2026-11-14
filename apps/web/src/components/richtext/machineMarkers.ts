/* Machine markers: the HTML comments a system writer stamps into a markdown body for its own matching —
 * `<!-- sysdedup:KEY -->` (older system tasks carry it; nothing stamps it now) and
 * the triage tool's `<!-- triage:task key=… -->`. The editor escapes raw HTML (`html: false`,
 * extensions.ts), so one would show as literal text; they are split off before the editor or renderer sees the
 * body and put back on every edit, so the stored body keeps them and dedup still matches. Only these
 * namespaces: a comment someone wrote by hand (or inside a code sample) stays as written. */
const MARKER = /[ \t]*<!-- (?:sysdedup|triage):[^\n]*?-->[ \t]*/g

export interface SplitBody {
  /** The body without its markers: what a person reads and edits. */
  visible: string
  /** The markers, in body order (empty when there are none). */
  markers: string[]
}

export function splitMachineMarkers(body: string): SplitBody {
  const markers = (body.match(MARKER) ?? []).map(m => m.trim())
  if (markers.length === 0) return { visible: body, markers }
  return { visible: body.replace(MARKER, '').replace(/\s+$/, ''), markers }
}

/** The stored body for an edited `visible` text: the markers ride at the end, each on its own line. */
export function joinMachineMarkers(visible: string, markers: readonly string[]): string {
  if (markers.length === 0) return visible
  return `${visible.replace(/\s+$/, '')}\n${markers.join('\n')}`
}
