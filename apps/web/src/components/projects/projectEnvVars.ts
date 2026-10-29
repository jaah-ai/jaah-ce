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

/** Mirror of `routes/project_env.py::EnvVarOut`. No field can carry a resolved value, at any layer. */
export type ProjectEnvVar = {
  id: string
  name: string
  /** The TIER; `null` is the project default. */
  user_id: string | null
  scope: 'project' | 'user'
  source: 'literal' | 'credential'
  literal: string | null
  credential: EnvVarCredentialRef | null
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
  source: 'literal' | 'credential'
  literal: string | null
  credential_id: string | null
  credential_field: string | null
  description: string
}

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
 *  comment naming credential and field. A winner that does not resolve is NOT WRITTEN, so it is commented
 *  out and followed by `unresolvedReason`'s sentence (error tone); a node-owned name keeps its line and
 *  gains the server's `warning` (warning tone). Statuses are rendered, never recomputed. */
export function envFileLines(rows: ProjectEnvVar[], viewerId: string | null): EnvFileLine[] {
  const lines: EnvFileLine[] = []
  for (const row of rows) {
    const value = row.source === 'literal'
      ? row.literal ?? ''
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
