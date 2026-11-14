import { absoluteTime, compactAge } from '../../lib/relativeTime'
import {
  CONFIDENCE,
  INCIDENT_STATE,
  INCIDENT_STATES,
  INCIDENT_TYPE,
  INCIDENT_TYPES,
  SEVERITIES,
  SEVERITY,
  listIncidents,
  prName,
  wordOf,
  type Incident,
} from '../../lib/productionQa'
import type { FeedRowSpec } from '../../ui/feed/types'
import { PHONE } from '../../ui/listStyles'
import { grid, SIZE } from '../../theme'
import StatusChip from '../../ui/StatusChip'
import { AGE_COLUMN, ProductionQaList } from './shared'

/* THE ORG'S INCIDENTS — every recorded incident across the projects the viewer may see, newest first
 * (`GET /api/incidents`). A list (ui-style-guide): rows open `/incidents/:id`. A row: the title (ellipsised, whole
 * on hover; two lines at phone width), the state as disc and word, the severity as a word in its colour, and the
 * last time it was seen; wider screens add type, project, count and the top suspect PR with its confidence as a
 * word. Filters (project, severity, state, type) live in the URL. Polls every 15 s while visible. */

const SEVERITY_OPTIONS = SEVERITIES.map(s => ({ value: s, label: SEVERITY[s].label }))
const STATE_OPTIONS = INCIDENT_STATES.map(s => ({ value: s, label: INCIDENT_STATE[s].label }))
const TYPE_OPTIONS = INCIDENT_TYPES.map(t => ({ value: t, label: INCIDENT_TYPE[t] }))

/** The top suspect in words: `#7 · High`, never a colour alone (AC-12). */
export function suspectText(i: Pick<Incident, 'top_suspect'>): string | null {
  const s = i.top_suspect
  return s ? `${prName(s, false)} · ${wordOf(CONFIDENCE, s.confidence).label}` : null
}

function row(i: Incident): FeedRowSpec {
  const state = wordOf(INCIDENT_STATE, i.state)
  const severity = wordOf(SEVERITY, i.severity)
  const type = INCIDENT_TYPE[i.type] ?? i.type
  const suspect = suspectText(i)
  return {
    key: String(i.id),
    indicator: { tone: state.tone, glyph: state.glyph, label: state.label, data: { state: i.state } },
    title: i.title,
    titleText: i.title,
    titleWrapAt: PHONE,
    href: `/incidents/${i.id}`,
    notes: [
      { key: 'where', text: `${type} · ${i.project.name}`, tone: 'muted' as const },
      ...(suspect ? [{ key: 'suspect', text: `Suspect ${suspect}`, tone: 'muted' as const }] : []),
    ],
    slots: [
      { key: 'type', width: grid(16), gate: 'meta', align: 'left', title: type, node: type, testId: 'incident-type' },
      { key: 'project', width: grid(18.5), gate: 'meta', align: 'left', title: i.project.name, node: i.project.name, testId: 'incident-project' },
      { key: 'suspect', width: grid(17), gate: 'id', align: 'left', title: suspect ? `Top suspect ${suspect}` : 'No suspect', node: suspect ?? '—', testId: 'incident-suspect' },
      { key: 'count', width: grid(7), gate: 'id', align: 'right', mono: true, dim: true, title: `${i.count} occurrences`, node: i.count, testId: 'incident-count' },
      { key: 'severity', width: grid(10), align: 'left', node: <StatusChip label={severity.label} tone={severity.tone} data-status={i.severity} testId="incident-severity" /> },
      { key: 'state', width: grid(11), align: 'left', node: <StatusChip label={state.label} tone={state.tone} data-status={i.state} testId="incident-state" /> },
      { key: 'seen', width: AGE_COLUMN, align: 'right', mono: true, dim: true, title: `Last seen ${absoluteTime(i.last_seen)}`, node: compactAge(i.last_seen), testId: 'incident-seen' },
    ],
    data: { state: i.state, severity: i.severity, id: i.id },
  }
}

export default function IncidentsPage() {
  return (
    <ProductionQaList
      noun="Incidents"
      fetch={listIncidents}
      filters={[
        { param: 'severity', label: 'Severity', allLabel: 'Any severity', options: SEVERITY_OPTIONS, minWidth: SIZE.fieldSm },
        { param: 'state', label: 'State', allLabel: 'Any state', options: STATE_OPTIONS, minWidth: SIZE.fieldSm },
        { param: 'type', label: 'Type', allLabel: 'Any type', options: TYPE_OPTIONS, minWidth: SIZE.fieldSm },
      ]}
      row={row}
      title="Incident"
      slots={[
        { key: 'type', label: 'Type' },
        { key: 'project', label: 'Project' },
        { key: 'suspect', label: 'Suspect' },
        { key: 'count', label: 'Count' },
        { key: 'severity', label: 'Severity' },
        { key: 'state', label: 'State' },
        { key: 'seen', label: 'Seen', ariaLabel: 'Last seen' },
      ]}
    />
  )
}
