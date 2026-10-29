/* Jaah API keys: the console's client for `apps/api/app/routes/api_keys.py`. Every rule is the server's
 * (`app/api_keys.py`); these mirror its wire shapes field for field. The secret is answered once, by create and
 * rotate, and never kept past the reveal dialog. */

import { get, post, patch } from '../../../api'

export type PersonRef = { id: string; label: string }

export type ApiKeyStatus = 'active' | 'expiring' | 'expired' | 'revoked' | 'suspended'

/** `ApiKeyOut`. `display` is `jaah_…<last4>`, never the secret. */
export type ApiKey = {
  id: string
  name: string
  description: string
  owner: PersonRef
  display: string
  grants_all: boolean
  permissions: string[]
  org_admin: boolean
  ip_allowlist: string[]
  expires_at: string | null
  created_at: string
  created_by: PersonRef
  updated_at: string
  last_used_at: string | null
  last_used_ip: string | null
  status: ApiKeyStatus
  revoked_at: string | null
  revoked_by: PersonRef | null
  revoke_reason: string | null
  previous_secret_valid_until: string | null
  can_edit: boolean
  can_revoke: boolean
}

export type ApiKeyWithSecret = { key: ApiKey; secret: string }

/** What a key may do: the permission grid's value, as the create and edit bodies carry it. */
export type KeyGrants = { grants_all: boolean; permissions: string[]; org_admin: boolean }

export type ApiKeyCreateBody = KeyGrants & {
  name: string
  description: string
  ip_allowlist: string[]
  expires_at: string | null
}

/** No `expires_at`: a key's expiry is fixed at create. */
export type ApiKeyUpdateBody = Partial<KeyGrants & { name: string; description: string; ip_allowlist: string[] }>

export type RotateOverlap = 'none' | '1h' | '24h' | '7d'

export const API_KEYS_KEY = ['api-keys'] as const
export const API_KEYS_URL = '/settings/api-keys'
export const NEW_API_KEY_URL = `${API_KEYS_URL}/new`
export const apiKeyUrl = (id: string) => `${API_KEYS_URL}/${encodeURIComponent(id)}`

const base = '/api/api-keys'
const one = (id: string) => `${base}/${encodeURIComponent(id)}`

export const listApiKeys = () => get<ApiKey[]>(base)
export const getApiKey = (id: string) => get<ApiKey>(one(id))
export const createApiKey = (body: ApiKeyCreateBody) => post<ApiKeyWithSecret>(base, body)
export const updateApiKey = (id: string, body: ApiKeyUpdateBody) => patch<ApiKey>(one(id), body)
export const rotateApiKey = (id: string, overlap: RotateOverlap) => post<ApiKeyWithSecret>(`${one(id)}/rotate`, { overlap })
export const revokeApiKey = (id: string) => post<ApiKey>(`${one(id)}/revoke`, {})
