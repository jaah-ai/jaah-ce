/* The credential seal, browser half: a secret is encrypted HERE, under the deployment's
 * public wrapping key, and only the envelope crosses the wire — the plaintext never reaches the API.
 * The construction's single home is `apps/api/app/credential_crypto.py` (`seal_secret`,
 * `tier2_aad`, `_secret_envelope_parts`); the destination rules mirror
 * `apps/api/app/credentials/registry.py` (`canonical_destination`, `sealing_destination`). Change one,
 * change both:
 *   • the wrapping key: RSA, on the wire as base64 SPKI DER (`GET /api/credentials/seal-key`);
 *   • one envelope per secret field: a random AES-256-GCM data key wraps under RSA-OAEP (SHA-256,
 *     MGF1-SHA256, no label) → `ek`; a 12-byte nonce → `n`; the UTF-8 value under the data key with the
 *     tier-2 AAD → `ct` (WebCrypto appends the 16-byte tag, which is what Python's AESGCM expects);
 *   • the AAD binds the envelope to (credential id, field, destination), so it cannot be moved to
 *     another row or field, or opened after the row's URL is pointed elsewhere.
 * Standard base64 throughout (`lib/base64.ts`, shared with `loginSeal.ts`). Nothing here touches the
 * DOM or storage, and the plaintext is not retained past the encrypt call.
 */

import { get } from '../api'
import { base64ToBytes, bytesToBase64 } from './base64.ts'

/** `GET /api/credentials/seal-key` (`routes/credentials/credential_wire.py::SealKeyOut`). `key_id` is stamped on every
 *  envelope, so the server can refuse one sealed under a key it does not hold. */
export type CredentialSealKey = {
  key_id: string
  algorithm: 'RSA-OAEP-256'
  /** Base64 DER SubjectPublicKeyInfo — WebCrypto's `spki` import format. */
  public_key: string
}

/** The tier-2 envelope, EXACTLY the key set `_secret_envelope_parts` admits (no more, no fewer). */
export type SealedSecretEnvelope = {
  v: 2
  k: string
  alg: 'RSA-OAEP-256'
  ek: string
  n: string
  ct: string
}

/** What a secret is bound to: the parts of the AAD besides the fixed prefix. */
export type SealingScope = {
  /** The row's hyphenated UUID — on create, one the browser minted before sealing. */
  credentialId: string
  field: string
  destination: string
}

/** The two inputs of `sealingDestination`, as `CredentialTypeSpec` carries them. */
export type SealingSpec = {
  host_field: string | null | undefined
  default_base_url: string | null | undefined
}

export const STORED_VERSION = 2
export const WRAP_ALGORITHM = 'RSA-OAEP-256'
const AAD_PREFIX = 'credentials.secret|v2|'
const NONCE_BYTES = 12
const DATA_KEY_BITS = 256

const RSA_IMPORT = { name: 'RSA-OAEP', hash: 'SHA-256' } as const

/** The deployment's public wrapping key. Rejects outside a secure context (no `crypto.subtle`), so a
 *  page can say "cannot encrypt here" before it ever offers Save. */
export async function fetchSealKey(): Promise<CredentialSealKey> {
  if (typeof crypto === 'undefined' || !crypto.subtle) {
    throw new Error('WebCrypto is unavailable: secrets cannot be encrypted in this browser session')
  }
  return get<CredentialSealKey>('/api/credentials/seal-key')
}

/** One spelling of a destination URL for the AAD: trimmed, no trailing slash, else `''`.
 *  MIRROR of `registry.canonical_destination` — change one, change both. Deliberately no host
 *  lowercasing or scheme normalisation: the fewer rules, the fewer ways the two halves disagree. */
export function canonicalDestination(value: string | null | undefined): string {
  if (value === null || value === undefined) return ''
  return value.trim().replace(/\/+$/, '')
}

/** The destination a row of this type under `values` binds its secrets to: the operator's host field
 *  when filled, else the type's `default_base_url`, canonicalised. MIRROR of
 *  `registry.sealing_destination` — change one, change both. `values` must be the SAME values the
 *  request carries: the server strips url values, and `canonicalDestination` trims to match. */
export function sealingDestination(spec: SealingSpec, values: Record<string, string>): string {
  const key = spec.host_field ?? null
  const raw = key === null ? undefined : values[key]
  if (raw === undefined || !raw.trim()) return canonicalDestination(spec.default_base_url)
  return canonicalDestination(raw)
}

/** `credentials.secret|v2|<id>|<field>|<destination>` as UTF-8 — MIRROR of `credential_crypto.tier2_aad`.
 *  The id is lower-cased (Python canonicalises through `UUID`); the destination is last so it may
 *  contain `|`, and a field never does. */
export function tier2Aad(credentialId: string, field: string, destination: string): Uint8Array<ArrayBuffer> {
  if (!field || field.includes('|')) throw new Error("a secret field name is a non-empty string without '|'")
  return new TextEncoder().encode(`${AAD_PREFIX}${credentialId.toLowerCase()}|${field}|${destination}`)
}

/** `value` as a fresh envelope for `scope`, under `sealKey`. The data key is generated extractable only
 *  so it can be wrapped; neither it nor the plaintext outlives this call. */
export async function sealSecret(
  sealKey: CredentialSealKey,
  scope: SealingScope,
  value: string,
): Promise<SealedSecretEnvelope> {
  const rsaKey = await crypto.subtle.importKey(
    'spki', base64ToBytes(sealKey.public_key), RSA_IMPORT, false, ['encrypt'],
  )
  const aesKey = await crypto.subtle.generateKey(
    { name: 'AES-GCM', length: DATA_KEY_BITS }, true, ['encrypt'],
  )
  const rawAesKey = await crypto.subtle.exportKey('raw', aesKey)
  const ek = await crypto.subtle.encrypt({ name: 'RSA-OAEP' }, rsaKey, rawAesKey)
  const iv = crypto.getRandomValues(new Uint8Array(NONCE_BYTES))
  const ct = await crypto.subtle.encrypt(
    {
      name: 'AES-GCM',
      iv,
      additionalData: tier2Aad(scope.credentialId, scope.field, scope.destination),
      tagLength: 128,
    },
    aesKey,
    new TextEncoder().encode(value),
  )
  return {
    v: STORED_VERSION,
    k: sealKey.key_id,
    alg: WRAP_ALGORITHM,
    ek: bytesToBase64(new Uint8Array(ek)),
    n: bytesToBase64(iv),
    ct: bytesToBase64(new Uint8Array(ct)),
  }
}

/** Whether `x` has the envelope's exact shape (the server's own admission rule: exact key set,
 *  version 2, the one wrap algorithm, string fields). */
export function isSealedSecretEnvelope(x: unknown): x is SealedSecretEnvelope {
  if (typeof x !== 'object' || x === null) return false
  const keys = Object.keys(x).sort()
  if (keys.join(',') !== 'alg,ct,ek,k,n,v') return false
  const e = x as Record<string, unknown>
  return e.v === STORED_VERSION
    && e.alg === WRAP_ALGORITHM
    && typeof e.k === 'string' && e.k !== ''
    && typeof e.ek === 'string' && e.ek !== ''
    && typeof e.n === 'string' && e.n !== ''
    && typeof e.ct === 'string' && e.ct !== ''
}
