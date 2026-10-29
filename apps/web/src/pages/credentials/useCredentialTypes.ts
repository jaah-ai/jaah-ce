import { useApiRead } from '../../hooks/useApiRead'
import { listCredentialTypes, type CredentialTypeSpec } from './api'

// Type/field schema from `GET /api/credentials/types` — the form's only field source, so the TS side
// never duplicates the server registry. `kind: "secret"` is the same classification the server strips
// responses by.

export type CredentialTypesState = {
  types: CredentialTypeSpec[]
  loading: boolean
  /** A load failure — the form cannot render any fields without the schema, so it must be surfaced. */
  error: string
}

const NO_TYPES: CredentialTypeSpec[] = []

export function useCredentialTypes(): CredentialTypesState {
  const { data, loading, error } = useApiRead(['credential-types'], () => listCredentialTypes(), 'Failed to load secret types')
  return { types: data ?? NO_TYPES, loading, error }
}
