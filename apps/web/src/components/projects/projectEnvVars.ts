import type { Credential } from '../../pages/credentials/api'

/* THE ENVIRONMENT TAB'S WIRE SHAPE AND ITS ONE DERIVED SENTENCE, kept out of the component for unit tests.
 * `status`/`winning` are the server's (`app/project_env.py` holds R4 precedence and resolvability) and are
 * rendered, never recomputed; `unresolvedReason` only TRANSLATES a status, totally over the vocabulary.
 * The sentence is built here, not read off `status_detail`, because `RowStatus.detail` never names the
 * credential (it rides the slot-lease path) while the tab may (R10: NAME and FIELD, never the value);
 * `status_detail` is the fallback. A generic "unresolved" badge would merge three different remedies.
 */

/** All but `field` are `null` on a TOMBSTONE (credential deleted, reference nulled) or a reference the
 *  reader's org cannot reach. */
export type EnvVarCredentialRef = {
  id: string | null
  name: string | null
  type_label: string | null
  field: string
}

/** Where a variable's value comes from. A `port` is assigned by the node on each lease. */
export type EnvVarSource = 'literal' | 'credential' | 'port'

/** Mirror of `routes/project_env.py::EnvVarOut`. No field can carry a resolved value, at any layer. */
export type ProjectEnvVar = {
  id: string
  name: string
  /** The TIER; `null` is the project default. A port row is always a project default. */
  user_id: string | null
  scope: 'project' | 'user'
  source: EnvVarSource
  literal: string | null
  credential: EnvVarCredentialRef | null
  /** A port row's preferred port; `null` = auto-assigned per slot. Always `null` off a port row. */
  port_preferred: number | null
  /** `ok`, one of three omission reasons, or `unknown` (unresolvable). Rendered, never recomputed. */
  status: string
  status_detail: string | null
  /** Whether THIS row is what the viewer's slot receives (R4). */
  winning: boolean
  /** Server-computed: what on the node writes this name INSTEAD; `null` otherwise. */
  warning: string | null
  description: string
}

/** Mirror of `EnvVarWriteIn`. `user_id`: `null` for a project default, the caller's id for their override. */
export type ProjectEnvVarWrite = {
  name: string
  user_id: string | null
  source: EnvVarSource
  literal: string | null
  credential_id: string | null
  credential_field: string | null
  /** Port rows only (1024..65535); `null` = auto-assigned per slot. */
  port_preferred?: number | null
  description: string
}

/** The lowest and highest port a Port row may prefer (the server's `EnvVarWriteIn` bounds). */
export const PORT_MIN = 1024
export const PORT_MAX = 65535

/** A port row's value in words: `auto`, or the port it prefers. */
export const portValueLabel = (preferred: number | null): string =>
  preferred === null ? 'auto' : `prefers ${preferred}`

/** A port row's value in words, as the preview shows it: `Port · auto`, `Port · prefers 5173`. */
export const portLabel = (row: Pick<ProjectEnvVar, 'port_preferred'>): string =>
  `Port · ${portValueLabel(row.port_preferred)}`

export const STATUS_OK = 'ok'

/** `null` for a DELETED credential, so the sentence says so rather than printing empty quotes. */
const credentialLabel = (row: ProjectEnvVar): string | null => row.credential?.name ?? null

/** THE ONE SENTENCE A WARNED ROW SHOWS, or `null` when resolved. Every branch names what and which, and
 *  ends with the consequence (R9: the variable is left OUT of the slot's `.env`, never present-and-empty).
 *  `viewerOwnsRow` changes only the PERSON in `not_granted`: a project default resolves under its last
 *  updater, not the reader.
 */
export function unresolvedReason(row: ProjectEnvVar, viewerOwnsRow: boolean): string | null {
  if (row.status === STATUS_OK) return null
  const tail = ' It is left out of the slot’s .env entirely.'
  const name = credentialLabel(row)
  const field = row.credential?.field ?? ''
  switch (row.status) {
    case 'credential_missing':
      return (
        'Not resolved — the credential this variable referenced was deleted'
        + (field ? `, so nothing supplies its ${field} field` : '')
        + '. Point it at another credential, or replace it with a literal.'
        + tail
      )
    case 'not_granted':
      return (
        'Not resolved — '
        + (viewerOwnsRow
          ? `you have no grant on credential ${name ?? 'it references'}`
          : `the identity this variable resolves under has no grant on credential ${name ?? 'it references'}`)
        + '.'
        + tail
      )
    case 'field_absent':
      return name !== null
        ? `Not resolved — field ${field} is not set on credential ${name}.${tail}`
        : `Not resolved — ${row.status_detail ?? 'this variable has no usable value'}.${tail}`
    case 'nodevault':
      /* AC-5.4: the field is sealed under the org's NodeVault key, which only its nodes hold. */
      return `Not resolved — NodeVault: Jaah's servers can't open ${name !== null ? `credential ${name}` : 'this credential'}.${tail}`
    default:
      /* `unknown`: falling through to `null` would render it as RESOLVED. */
      return `Not resolved — ${row.status_detail ?? 'this variable could not be resolved'}.${tail}`
  }
}

/** The tier in the operator's words; the viewer's own override is named as theirs. Names no person (the
 *  owner string would truncate in a chip) — the owner renders on its own line. */
export function scopeLabel(row: ProjectEnvVar, viewerId: string | null): string {
  if (row.user_id === null) return 'Project default'
  if (viewerId !== null && row.user_id === viewerId) return 'Your override'
  return 'Another user’s override'
}

/** A row's SOURCE in words, over any source the server sends: the two known ones named, a new one (a later
 *  source type) title-cased rather than dropped, so the table shows it before this file learns its word. */
export function sourceLabel(source: string): string {
  if (source === 'credential') return 'Credential'
  if (source === 'literal') return 'Literal'
  return source.charAt(0).toUpperCase() + source.slice(1).replace(/_/g, ' ')
}

/** Fields from the server registry, never a TS table. `[]` for a retired type (picker says "no fields declared"). */
export function fieldsOfType(
  types: { slug: string; fields: { key: string; label: string; kind: string }[] }[],
  slug: string | null,
): { key: string; label: string; kind: string }[] {
  if (slug === null) return []
  return types.find(t => t.slug === slug)?.fields ?? []
}

/** The hint is the TYPE LABEL — `name` is not unique. */
export const toCredentialOption = (row: Credential) => ({
  id: row.id,
  name: row.name,
  hint: row.type_label,
})

/** What a credential-sourced line shows in place of its value. The wire never carries one (secret floor),
 *  so this is not a redaction of something present — it marks where the slot's real value goes. */
export const MASK = '********'

/** One line of the preview's `.env` text; `tone` colours it, the text alone is what Copy takes. */
export type EnvFileLine = { text: string; tone: 'plain' | 'error' | 'warning'; name: string }

/** THE PREVIEW AS A `.env` FILE: `NAME=value`, literals in clear, credentials masked with a trailing
 *  comment naming credential and field, ports as `…` (the node picks the number on each lease). A winner that does not resolve is NOT WRITTEN, so it is commented
 *  out and followed by `unresolvedReason`'s sentence (error tone); a node-owned name keeps its line and
 *  gains the server's `warning` (warning tone). Statuses are rendered, never recomputed. */
export function envFileLines(rows: ProjectEnvVar[], viewerId: string | null): EnvFileLine[] {
  const lines: EnvFileLine[] = []
  for (const row of rows) {
    const value = row.source === 'literal'
      ? row.literal ?? ''
      : row.source === 'port'
        ? `…  # ${portLabel(row)}, set on each lease`
        : `${MASK}  # credential ${row.credential?.name ?? '(deleted)'}`
        + (row.credential?.field ? ` · ${row.credential.field}` : '')
    const reason = unresolvedReason(row, viewerId !== null && row.user_id === viewerId)
    if (reason !== null) {
      lines.push({ text: `# ${row.name}=${value}`, tone: 'error', name: row.name })
      lines.push({ text: `#   ⚠ ${reason}`, tone: 'error', name: row.name })
    } else {
      lines.push({ text: `${row.name}=${value}`, tone: 'plain', name: row.name })
    }
    if (row.warning !== null) {
      lines.push({ text: `#   ⚑ ${row.warning}`, tone: 'warning', name: row.name })
    }
  }
  return lines
}

/** The project's Environment tab: the list a variable's page sits beside (`lib/listDetail.ts`). */
export const envListHref = (projectId: number | string): string => `/projects/${projectId}?tab=environment`

/** One variable's page (`pages/projects/ProjectEnvVarPage.tsx`), by row id — a name has two rows (Everyone and an
 *  Only me override) — or `new` for the same page in create mode. */
export const envVarHref = (projectId: number | string, varId: string): string =>
  `/projects/${projectId}/env/${encodeURIComponent(varId)}`

/** The catalogue for one project, keyed by project: the tab's list and the variable page's row read share it. */
export const envVarsKey = (projectId: number) => ['project-env-vars', projectId] as const
/** The preview is a second read (winners only, for one identity): its own key, fetched only while open. */
export const envPreviewKey = (projectId: number) => ['project-env-preview', projectId] as const

/** May the viewer write THIS row (to avoid offering a control that 403s)? Defaults need `projects:edit`; an
 *  override belongs to its user — `projects:edit` does not lift that (D20). */
export const mayWriteEnvVar = (row: ProjectEnvVar, canEdit: boolean, viewerId: string | null): boolean =>
  row.user_id === null ? canEdit : viewerId !== null && row.user_id === viewerId

export const DELETE_FALLBACK = 'Could not delete the environment variable.'

/** The Delete confirm, the list's and the variable page's. */
export const deleteEnvVarConfirm = (name: string) => ({
  title: 'Delete environment variable',
  identity: name,
  message: 'Delete this variable? Slots claimed after this stop receiving it. This cannot be undone.',
  confirmLabel: 'Delete',
  destructive: true,
})
