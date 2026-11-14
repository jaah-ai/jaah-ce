import { useCallback, useRef, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import Link from '@mui/material/Link'
import Typography from '@mui/material/Typography'
import { get } from '../../api'
import { listProjects } from '../../hooks/useRosters'
import { actAsOnce } from '../../lib/actAs'
import { credentialHomeLabel } from '../../lib/credentialRow'
import { sealSecret } from '../../lib/credentialSeal'
import type { OrgRef, Project } from '../../types'
import FormTextField from '../../ui/FormTextField'
import { urlShapeWarning } from '../../lib/urlShape'
import { fmtTimestamp } from '../../lib/relativeTime'
import { ShowHideAdornment } from '../../ui/ShowHideAdornment'
import { getCredentialSealKey, type CredentialSealKey, type FieldSpec, type SealedSecretEnvelope } from './api'
import Tip from '../../ui/Tip'

// Registry-driven form pieces of `CredentialEditPage` (sealing: one envelope per secret, the row's id in the AAD,
// plaintext never on the wire).

/** The seal key, fetched once on first ask and held as a promise for the form's life. */
export function useSealKey(): () => Promise<CredentialSealKey> {
  const ref = useRef<Promise<CredentialSealKey> | null>(null)
  return useCallback(() => {
    if (ref.current === null) ref.current = getCredentialSealKey()
    return ref.current
  }, [])
}

/** A server 400 names the offending KEY first; re-home it under that field's label. */
export function inlineFieldError(fields: FieldSpec[], detail: string): Record<string, string> | null {
  const key = detail.split(' ')[0]
  if (key === 'name') return { name: `Name ${detail.slice(key.length).trim()}` }
  const field = fields.find(candidate => candidate.key === key)
  if (!field) return null
  return { [key]: `${field.label} ${detail.slice(key.length).trim()}` }
}

/** The config to send: a secret is SEALED under the key `sealKey` resolves (blank = omitted, kept); a
 *  blank non-secret is `null` (deleted). The key is asked for only when there is a secret to seal: a frozen
 *  plan refuses the NodeVault key, yet a NodeVault row's other fields stay editable. */
export async function buildSealedConfig({ fields, values, credentialId, destination, sealKey }: {
  fields: FieldSpec[]
  values: Record<string, string>
  credentialId: string
  destination: string
  sealKey: () => Promise<CredentialSealKey>
}): Promise<Record<string, string | SealedSecretEnvelope | null>> {
  const config: Record<string, string | SealedSecretEnvelope | null> = {}
  let key: CredentialSealKey | null = null
  for (const field of fields) {
    const value = values[field.key] ?? ''
    if (field.kind === 'secret') {
      if (value.trim()) {
        key ??= await sealKey()
        config[field.key] = await sealSecret(key, { credentialId, field: field.key, destination }, value)
      }
      continue
    }
    config[field.key] = value.trim() ? value : null
  }
  return config
}

/** One registry field as an input. */
const isIsoStamp = (value: string): boolean =>
  /^\d{4}-\d{2}-\d{2}T/.test(value) && !Number.isNaN(new Date(value).getTime())

export function RegistryFieldInput({ field, value, error, onChange, disabled, required, id, hint, placeholder, freeFormUrl }: {
  field: FieldSpec
  value: string
  error?: string
  onChange: (value: string) => void
  disabled?: boolean
  required?: boolean
  id: string
  hint?: string
  /** Replaces the registry's example (a stored secret says it is kept). */
  placeholder?: string
  /** The type's `free_form_url`: a url field warns on an odd shape (`lib/urlShape.ts`), never refuses. */
  freeFormUrl?: boolean
}) {
  const isSecret = field.kind === 'secret'
  const [shown, setShown] = useState(false)
  /* Sentence case, as `NewPasswordFields`' "Show password". */
  const what = field.label.toLowerCase()
  const toggle = isSecret ? <ShowHideAdornment shown={shown} onToggle={() => setShown(s => !s)} what={what} /> : undefined
  /* A stamp the system writes (`token_expires_at`, `grant_dead_at`: the registry's `*_at` keys) reads as a date,
     like every other date in the console, never raw ISO; shown, not typed, so the stored value saves unchanged. */
  /* Decided from the value the form LOADED (never per keystroke: `new Date('2')` parses), and only an ISO stamp.
     Clear drops it to an empty, editable field, the one edit an operator makes to a stamp. */
  const [stamp, setStamp] = useState(() => !isSecret && field.key.endsWith('_at') && isIsoStamp(value))
  if (stamp && isIsoStamp(value)) {
    return (
      <FormTextField label={field.label} help={field.help ?? undefined} id={id} readOnly value={fmtTimestamp(value)} onChange={() => {}}
        hint={disabled ? undefined : (
          <Link component="button" type="button" onClick={() => { setStamp(false); onChange('') }} data-testid={`${id}-clear`}>Clear</Link>
        )} />
    )
  }
  return (
    <FormTextField
      label={field.label}
      help={field.help ?? undefined}
      hint={hint}
      error={error}
      warning={freeFormUrl && field.kind === 'url' ? urlShapeWarning(value) : undefined}
      id={id}
      required={required}
      disabled={disabled}
      // Write-only: a secret renders as a password (Show reveals only what was typed here), never populated
      // from the server, never read back.
      type={isSecret && !shown ? 'password' : field.kind === 'email' ? 'email' : 'text'}
      autoComplete={isSecret ? 'new-password' : 'off'}
      value={value}
      placeholder={placeholder ?? field.placeholder ?? undefined}
      onChange={onChange}
      slotProps={{
        input: toggle ? { endAdornment: toggle } : undefined,
        // Shrink the label when a placeholder exists: MUI hides it under an unshrunk label, and for
        // GitHub/GitLab `base_url` the placeholder is the only guidance (apps/api/app/credentials/registry.py).
        inputLabel: field.placeholder ? { shrink: true } : undefined,
        // `role="alert"` on the message only while there is an error — otherwise a failed submit is
        // silent (`aria-describedby` is read only on focus).
        formHelperText: error ? { role: 'alert' } : undefined,
      }}
    />
  )
}

/** A project an org credential may be homed on. */
export type HomeProject = { id: number; name: string }

/** The projects of `orgId` an org credential may be homed on (`GET /api/projects`, grant-scoped; the operator's
 *  roster spans orgs, so it is narrowed to the row's own). `actAs`: another org's row (an operator) — read
 *  as that org (`lib/actAs.ts::actAsOnce`), keyed by its id so the acted-as and operator answers never share a cache
 *  entry. `[]` while loading or refused: the org itself stays a home, so a failed read costs only the project choices. */
export function useHomeProjects(
  orgId: string | null | undefined, enabled: boolean, actAs: OrgRef | null = null,
): HomeProject[] {
  const headers = actAsOnce(actAs)
  const query = useQuery({
    queryKey: actAs ? ['projects', { actAs: actAs.id }] : ['projects'],
    queryFn: () => (headers ? get<Project[]>('/api/projects', { headers }) : listProjects()),
    enabled,
    retry: false,
  })
  const rows = Array.isArray(query.data) ? query.data : []
  return rows
    .filter(project => !orgId || !project.org || project.org.id === orgId)
    .map(project => ({ id: project.id, name: project.name }))
}

/** "Home" for an org credential: the org itself, or one of its projects. A native select (the console's select
 *  idiom). A stored project the roster lacks keeps an option under its served name, so the box never paints a
 *  value other than the row's. */
export function HomeProjectField({ orgLabel, projects, value, storedName, onChange, disabled }: {
  orgLabel: string
  projects: HomeProject[]
  value: number | null
  /** The row's own home project name (`home_project`), for when the roster does not carry it. */
  storedName?: string | null
  onChange: (next: number | null) => void
  disabled?: boolean
}) {
  const missing = value !== null && !projects.some(project => project.id === value)
  const options = [
    { value: '', label: orgLabel },
    ...(missing ? [{ value: String(value), label: storedName ?? `Project ${value}` }] : []),
    ...projects.map(project => ({ value: String(project.id), label: project.name })),
  ]
  return (
    <FormTextField
      label="Home"
      id="credential-home-project"
      help="In a project, its tasks use it as if attached."
      value={value === null ? '' : String(value)}
      disabled={disabled}
      options={options}
      onChange={next => onChange(next === '' ? null : Number(next))}
    />
  )
}

/** The home the name lands in (`Acme › homejab ›`), on its own line above the name box so the box holds only what
 *  is typed; never sent. One line, ellipsised past the width, the whole home on hover (`title`). */
export function CredentialHomeLine({ path }: { path: readonly string[] }) {
  const label = credentialHomeLabel(path)
  if (!label) return null
  return (
    <Tip title={label}><Typography variant="body2" noWrap data-testid="credential-name-home" sx={{ color: 'text.secondary', mb: 1 }}>
      {label}
    </Typography></Tip>
  )
}
