import type { ReactNode } from 'react'
import { useAuth } from '../../auth'
import { useApiRead } from '../../hooks/useApiRead'
import { ChoiceField } from '../../ui/ChoiceField'
import FormTextField, { type FormOption } from '../../ui/FormTextField'
import { listApiKeys, type ApiKeyStatus } from '../settings/apiKeys/api'

/* The `jaah` secret's key pair (`integrations/vendors/jaah.toml`): `api_key` (the secret) and `key_id` (which Jaah API
 * key it is), drawn as one "Key: New | Existing" choice instead of two registry fields. New asks nothing more:
 * `CredentialEditPage`'s Save makes the key (`POST /api/api-keys`) and stores its secret and id. Existing picks one
 * of the viewer's own live keys and takes its pasted secret. */

export const JAAH_TYPE = 'jaah'
/** The registry fields this component draws in place of `RegistryFieldInput`. */
export const JAAH_KEY_FIELDS: ReadonlySet<string> = new Set(['api_key', 'key_id'])

export type JaahKeyMode = 'new' | 'existing'

/** A key name's longest (`routes/api_keys.py::_NAME_MAX`): New names the key after the secret, cut to fit. */
export const API_KEY_NAME_MAX = 100

/** A key that still authenticates (`ApiKeyOut.status`). */
const LIVE: ReadonlySet<ApiKeyStatus> = new Set(['active', 'expiring'])

/** Whether `url` is this console's origin: the only control plane the browser can make a key on. */
export function isThisConsole(url: string): boolean {
  try {
    return new URL(url.trim()).origin === window.location.origin
  } catch {
    return false
  }
}

export default function JaahKeyField({ mode, newOffered, onMode, keyId, onKeyId, secret, secretInput, error, disabled }: {
  mode: JaahKeyMode
  /** New is offered only on create, and only when the URL is this console (`isThisConsole`). */
  newOffered: boolean
  onMode: (next: JaahKeyMode) => void
  keyId: string
  onKeyId: (next: string) => void
  /** The pasted secret, read only to compare its last four characters with the chosen key's. */
  secret: string
  /** The `api_key` field's own input (`RegistryFieldInput`), drawn under the key. */
  secretInput: ReactNode
  error?: string
  disabled?: boolean
}) {
  const { admin } = useAuth()
  const keys = useApiRead(['credential-jaah-keys'], listApiKeys, 'Failed to load API keys', { enabled: mode === 'existing' })
  const chosen = keys.data?.find(key => key.id === keyId)
  const options: FormOption[] = (keys.data ?? [])
    .filter(key => key.owner.id === admin?.id && LIVE.has(key.status))
    .map(key => ({ value: key.id, label: `${key.name} · ${key.display}` }))
  /* A saved row's key that is no longer offered (revoked, someone else's) still shows what it is. */
  if (keyId && !options.some(option => option.value === keyId)) {
    options.unshift({ value: keyId, label: chosen ? `${chosen.name} · ${chosen.display}` : keyId })
  }
  const tail = secret.trim().slice(-4)
  const mismatch = chosen && tail.length === 4 && !chosen.display.endsWith(tail) ? 'Not this key\'s secret.' : undefined
  return (
    <>
      {newOffered && (
        <ChoiceField
          label="Key"
          row
          value={mode}
          onChange={onMode}
          disabled={disabled}
          testId="jaah-key-mode"
          options={[
            { value: 'new', label: 'New', help: null, testId: 'jaah-key-new' },
            { value: 'existing', label: 'Existing', help: null, testId: 'jaah-key-existing' },
          ]}
        />
      )}
      {mode === 'existing' && (
        <>
          <FormTextField
            label={newOffered ? 'Existing key' : 'Key'}
            id="credential-field-key_id"
            testId="jaah-key-select"
            value={keyId}
            onChange={onKeyId}
            options={options}
            error={error || keys.error || undefined}
            warning={mismatch}
            disabled={disabled}
          >
            <option value="" disabled>Choose a key</option>
          </FormTextField>
          {secretInput}
        </>
      )}
    </>
  )
}
