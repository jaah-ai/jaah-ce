/* NodeVault's key constructions, browser half: generating a NodeVault Key
 * (NVK), wrapping it into a key blob for a node or for the rotation chain, and the passphrase backup.
 * The Python half is `apps/jaahd/nodevault_crypto.py`; the two are held byte-compatible by shared test
 * vectors that an interop test runs through both.
 * Change one, change both:
 *   • every key is RSA-4096, e=65537, RSA-OAEP with SHA-256/MGF1-SHA-256, no label; on the wire as
 *     base64 SPKI DER. Symmetric: AES-256-GCM, 12-byte nonce, 128-bit tag. Standard base64 throughout;
 *   • key hash / NIK fingerprint = hex(sha256(SPKI DER)), ALWAYS recomputed here from a public key —
 *     a hash the server sends alongside a key is never trusted;
 *   • key blob `{v:1,k,alg,ek,n,ct}`: `ct` = AES-GCM of the NVK's PKCS#8 DER under
 *     `nodevault.key|v1|<org_id>|<key_hash>|<recipient>`, `ek` = the data key under the recipient's
 *     RSA key. A node gets `k="nik:<fp>"`, recipient `node:<node_id>`; the rotation chain gets
 *     `k="nv:<current hash>"`, recipient `chain:<current hash>`;
 *   • backup file: `ct` = AES-GCM of the PKCS#8 DER under a PBKDF2-SHA256 (600k) passphrase key, AAD
 *     `nodevault.backup|v1|<key_hash>`.
 * Secret envelopes are NOT built here: `credentialSeal.sealSecret` takes the key as a parameter, and
 * `nodeVaultSealKey` is that parameter for an NVK.
 *
 * The private key lives only inside a `NodeVaultKey`, in a `#private` field: nothing enumerates it,
 * `JSON.stringify` cannot see it, and `dispose()` zero-fills it. Everything this module RETURNS for
 * sending (blobs, backup file, seal key, `toJSON`) carries ciphertext and public material only. Errors
 * are `NodeVaultKeyError` with a closed `reason` and a message that never carries key material. */

import { base64ToBytes, bytesToBase64 } from './base64.ts'
// Type-only: `credentialSeal.ts` imports the API client, which the node-run interop test cannot load.
import type { CredentialSealKey } from './credentialSeal.ts'

export const RSA_BITS = 4096
/** `credentialSeal.WRAP_ALGORITHM`, the one wrap every envelope and blob names. */
export const WRAP_ALGORITHM = 'RSA-OAEP-256'
export const KEY_BLOB_VERSION = 1
export const BACKUP_FORMAT = 'jaah-nodevault-backup'
export const BACKUP_VERSION = 1
export const BACKUP_KDF = 'PBKDF2-SHA256'
export const BACKUP_ITERATIONS = 600_000
export const MIN_PASSPHRASE_LENGTH = 12
/** How many leading hex characters of a key hash the UI shows. */
export const SHORT_HASH_LENGTH = 12

const NV_PREFIX = 'nv:'
const NIK_PREFIX = 'nik:'
const KEY_AAD_PREFIX = 'nodevault.key|v1|'
const BACKUP_AAD_PREFIX = 'nodevault.backup|v1|'
const NONCE_BYTES = 12
const SALT_BYTES = 16
const DATA_KEY_BITS = 256
const PUBLIC_EXPONENT = new Uint8Array([1, 0, 1])
const RSA_PARAMS = { name: 'RSA-OAEP', hash: 'SHA-256' } as const
const HASH_RE = /^[0-9a-f]{64}$/
const B64_RE = /^[A-Za-z0-9+/]+={0,2}$/
// Bounds mirrored from `nodevault_crypto.py`, so a hostile file is refused before any arithmetic.
const WRAPPED_BYTES = RSA_BITS / 8
const MAX_KEY_CT_BYTES = 4096
const MAX_SPKI_BYTES = 1024
// A real backup file is ~4 KB; anything far larger is refused before `JSON.parse`.
const MAX_BACKUP_CHARS = 16_384

/** The closed vocabulary of `NodeVaultKeyError.reason` — the Python half's words where one applies.
 *  A wrong backup passphrase and a tampered backup are both `open_failed`: AES-GCM cannot tell them
 *  apart, and the UI offers one message for both. */
export type NodeVaultKeyErrorReason =
  | 'malformed' | 'open_failed' | 'hash_mismatch' | 'wrong_recipient' | 'weak_passphrase' | 'unavailable'

export class NodeVaultKeyError extends Error {
  readonly reason: NodeVaultKeyErrorReason

  constructor(reason: NodeVaultKeyErrorReason, detail = '') {
    super(detail ? `${reason}: ${detail}` : reason)
    this.name = 'NodeVaultKeyError'
    this.reason = reason
  }
}

/** A key blob — an NVK in transit, ciphertext only. */
export type NodeVaultKeyBlob = {
  v: 1
  k: string
  alg: 'RSA-OAEP-256'
  ek: string
  n: string
  ct: string
}

/** The backup file's JSON, exactly the pinned key set. */
export type NodeVaultBackup = {
  format: typeof BACKUP_FORMAT
  v: 1
  org_id: string
  key_hash: string
  public_key: string
  kdf: { alg: typeof BACKUP_KDF, iterations: number, salt: string }
  n: string
  ct: string
}

/** An RSA-4096 private key held for the length of one Setup or Rotation in the page. The PKCS#8 bytes
 *  sit in a `#private` field (unenumerable, invisible to `JSON.stringify` and structured logging);
 *  only this module reads them. Call `dispose()` once the blobs and the backup are made. */
/** The one reader of a key's PKCS#8 — module-private, assigned by the class's static block. */
let pkcs8Of: (key: NodeVaultKey) => Uint8Array<ArrayBuffer>

export class NodeVaultKey {
  readonly keyHash: string
  /** Base64 SPKI DER — what `POST /api/nodevault/setup|rotation` carries as `public_key`. */
  readonly publicKey: string
  #pkcs8: Uint8Array<ArrayBuffer> | null

  /** Not for callers: build one with `generateNodeVaultKey`, `restoreBackup` or `openKeyBlob`. */
  constructor(pkcs8: Uint8Array<ArrayBuffer>, publicKey: string, keyHash: string, token: symbol) {
    if (token !== CONSTRUCT) throw new TypeError('NodeVaultKey is built by nodeVaultKeys.ts only')
    this.#pkcs8 = pkcs8
    this.publicKey = publicKey
    this.keyHash = keyHash
  }

  get shortHash(): string {
    return shortHash(this.keyHash)
  }

  get disposed(): boolean {
    return this.#pkcs8 === null
  }

  /** Zero-fill and drop the private key. Every later use throws. */
  dispose(): void {
    this.#pkcs8?.fill(0)
    this.#pkcs8 = null
  }

  /** The public parts only — so an accidental `JSON.stringify(key)` can never carry the private one. */
  toJSON(): { key_hash: string, public_key: string } {
    return { key_hash: this.keyHash, public_key: this.publicKey }
  }

  static {
    pkcs8Of = (key) => {
      if (key.#pkcs8 === null) throw new NodeVaultKeyError('unavailable', 'the key was disposed')
      return key.#pkcs8
    }
  }
}

const CONSTRUCT = Symbol('NodeVaultKey')

function subtle(): SubtleCrypto {
  if (typeof crypto === 'undefined' || !crypto.subtle) {
    throw new NodeVaultKeyError('unavailable', 'WebCrypto is unavailable in this browser session')
  }
  return crypto.subtle
}

/** The first `SHORT_HASH_LENGTH` characters of a key hash, as the UI shows it. */
export function shortHash(hash: string): string {
  return hash.slice(0, SHORT_HASH_LENGTH)
}

async function sha256Hex(bytes: Uint8Array<ArrayBuffer>): Promise<string> {
  const digest = new Uint8Array(await subtle().digest('SHA-256', bytes))
  return Array.from(digest, (b) => b.toString(16).padStart(2, '0')).join('')
}

function b64(value: unknown, label: string, maxBytes: number, exactBytes?: number): Uint8Array<ArrayBuffer> {
  // Padded only (length % 4), as Python's `b64decode(validate=True)` admits — `atob` alone would not.
  if (typeof value !== 'string' || value === '' || value.length > Math.ceil(maxBytes / 3) * 4 + 4
    || value.length % 4 !== 0 || !B64_RE.test(value)) {
    throw new NodeVaultKeyError('malformed', label)
  }
  let bytes: Uint8Array<ArrayBuffer>
  try {
    bytes = base64ToBytes(value)
  } catch {
    throw new NodeVaultKeyError('malformed', label)
  }
  if (bytes.length === 0 || bytes.length > maxBytes || (exactBytes !== undefined && bytes.length !== exactBytes)) {
    throw new NodeVaultKeyError('malformed', label)
  }
  return bytes
}

function checkHash(value: unknown, label = 'key hash'): string {
  if (typeof value !== 'string' || !HASH_RE.test(value)) throw new NodeVaultKeyError('malformed', label)
  return value
}

function aadPart(value: string, label: string): string {
  if (typeof value !== 'string' || value === '' || value.includes('|')) throw new NodeVaultKeyError('malformed', label)
  return value
}

function isRsa4096(key: CryptoKey): boolean {
  const alg = key.algorithm as RsaHashedKeyAlgorithm
  const e = alg.publicExponent
  return alg.modulusLength === RSA_BITS && e !== undefined
    && Array.from(e).join(',') === Array.from(PUBLIC_EXPONENT).join(',')
}

/** Import a base64 SPKI public key (an NIK or an NVK), refusing anything but RSA-4096 e=65537, and
 *  derive its hash over the RE-EXPORTED SPKI — the canonical DER Python's `key_hash` re-encodes too, so
 *  a non-canonical encoding from the server cannot make the two halves disagree. */
async function importPublic(spkiB64: unknown, label: string): Promise<{ key: CryptoKey, hash: string }> {
  const der = b64(spkiB64, label, MAX_SPKI_BYTES)
  let key: CryptoKey
  let canonical: Uint8Array<ArrayBuffer>
  try {
    key = await subtle().importKey('spki', der, RSA_PARAMS, true, ['encrypt'])
    canonical = new Uint8Array(await subtle().exportKey('spki', key))
  } catch {
    throw new NodeVaultKeyError('malformed', label)
  }
  if (!isRsa4096(key)) throw new NodeVaultKeyError('malformed', `${label} is not RSA-4096 e=65537`)
  return { key, hash: await sha256Hex(canonical) }
}

/** Read one DER TLV at `at`: its tag, and where its content starts and ends. */
function derItem(der: Uint8Array, at: number): { tag: number, start: number, end: number } {
  if (at + 2 > der.length) throw new NodeVaultKeyError('malformed', 'private key')
  const tag = der[at]
  let len = der[at + 1]
  let start = at + 2
  if (len & 0x80) {
    const n = len & 0x7f
    if (n === 0 || n > 3 || start + n > der.length) throw new NodeVaultKeyError('malformed', 'private key')
    len = 0
    for (let i = 0; i < n; i += 1) len = (len << 8) | der[start + i]
    start += n
  }
  if (start + len > der.length) throw new NodeVaultKeyError('malformed', 'private key')
  return { tag, start, end: start + len }
}

/** A DER INTEGER's magnitude as base64url (JWK spelling), its sign-padding byte dropped. */
function derUintB64url(der: Uint8Array, item: { tag: number, start: number, end: number }): string {
  if (item.tag !== 0x02 || item.end <= item.start) throw new NodeVaultKeyError('malformed', 'private key')
  let start = item.start
  while (start < item.end - 1 && der[start] === 0) start += 1
  return bytesToBase64(der.slice(start, item.end)).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '')
}

/** The public half `(n, e)` of an RSA PKCS#8 DER, read from the structure itself
 *  (PrivateKeyInfo → OCTET STRING → RSAPrivateKey → version, n, e) — so rebuilding the public key never
 *  exports the private one as a JWK (whose `d`/`p`/`q` strings could not be wiped). */
function rsaPublicFromPkcs8(der: Uint8Array): { n: string, e: string } {
  const info = derItem(der, 0)
  if (info.tag !== 0x30) throw new NodeVaultKeyError('malformed', 'private key')
  const version = derItem(der, info.start)
  const algorithm = derItem(der, version.end)
  const octets = derItem(der, algorithm.end)
  if (octets.tag !== 0x04) throw new NodeVaultKeyError('malformed', 'private key')
  const rsa = derItem(der, octets.start)
  if (rsa.tag !== 0x30) throw new NodeVaultKeyError('malformed', 'private key')
  const rsaVersion = derItem(der, rsa.start)
  const n = derItem(der, rsaVersion.end)
  const e = derItem(der, n.end)
  return { n: derUintB64url(der, n), e: derUintB64url(der, e) }
}

/** `hex(sha256(SPKI DER))` of a base64 SPKI public key — the NVK key hash and the NIK fingerprint
 *  alike (AC-4.6: equal to `openssl pkey -pubin -outform DER | sha256sum`). */
export async function keyHashOf(spkiB64: string): Promise<string> {
  return (await importPublic(spkiB64, 'public key')).hash
}

/** A `NodeVaultKey` from PKCS#8 DER: refuses anything but RSA-4096 e=65537, and derives the public key
 *  (and so the hash) from the private one. WebCrypto cannot export SPKI from a private key, so the
 *  public half is rebuilt from the DER's own `n`/`e`; the private import is non-extractable and serves
 *  only to prove WebCrypto accepts the key. */
async function keyFromPkcs8(pkcs8: Uint8Array<ArrayBuffer>): Promise<NodeVaultKey> {
  let spki: Uint8Array<ArrayBuffer>
  try {
    const priv = await subtle().importKey('pkcs8', pkcs8, RSA_PARAMS, false, ['decrypt'])
    if (!isRsa4096(priv)) throw new NodeVaultKeyError('malformed', 'private key is not RSA-4096 e=65537')
    const { n, e } = rsaPublicFromPkcs8(pkcs8)
    const pub = await subtle().importKey('jwk', { kty: 'RSA', n, e, alg: 'RSA-OAEP-256', ext: true }, RSA_PARAMS, true, ['encrypt'])
    spki = new Uint8Array(await subtle().exportKey('spki', pub))
  } catch (err) {
    pkcs8.fill(0)
    if (err instanceof NodeVaultKeyError) throw err
    throw new NodeVaultKeyError('malformed', 'private key')
  }
  return new NodeVaultKey(pkcs8, bytesToBase64(spki), await sha256Hex(spki), CONSTRUCT)
}

/** A fresh RSA-4096 NVK. The WebCrypto pair is extractable only long enough to export the PKCS#8 and
 *  SPKI; both `CryptoKey`s are dropped before this returns. */
export async function generateNodeVaultKey(): Promise<NodeVaultKey> {
  let pair: CryptoKeyPair | null = await subtle().generateKey(
    { ...RSA_PARAMS, modulusLength: RSA_BITS, publicExponent: PUBLIC_EXPONENT }, true, ['encrypt', 'decrypt'],
  )
  const pkcs8 = new Uint8Array(await subtle().exportKey('pkcs8', pair.privateKey))
  const spki = new Uint8Array(await subtle().exportKey('spki', pair.publicKey))
  pair = null
  return new NodeVaultKey(pkcs8, bytesToBase64(spki), await sha256Hex(spki), CONSTRUCT)
}

/** `nodevault.key|v1|<org_id>|<key_hash>|<recipient>` as UTF-8 — mirror of `nodevault_crypto.key_blob_aad`. */
export function keyBlobAad(orgId: string, keyHash: string, recipient: string): Uint8Array<ArrayBuffer> {
  const text = `${KEY_AAD_PREFIX}${aadPart(orgId, 'org id')}|${checkHash(keyHash)}|${aadPart(recipient, 'recipient')}`
  return new Uint8Array(new TextEncoder().encode(text))
}

/** `nodevault.backup|v1|<key_hash>` as UTF-8. */
export function backupAad(keyHash: string): Uint8Array<ArrayBuffer> {
  return new Uint8Array(new TextEncoder().encode(`${BACKUP_AAD_PREFIX}${checkHash(keyHash)}`))
}

async function gcmSeal(aesKey: CryptoKey, plaintext: Uint8Array<ArrayBuffer>, aad: Uint8Array<ArrayBuffer>) {
  const iv = crypto.getRandomValues(new Uint8Array(NONCE_BYTES))
  const ct = await subtle().encrypt({ name: 'AES-GCM', iv, additionalData: aad, tagLength: 128 }, aesKey, plaintext)
  return { n: bytesToBase64(iv), ct: bytesToBase64(new Uint8Array(ct)) }
}

async function gcmOpen(
  aesKey: CryptoKey, nonce: Uint8Array<ArrayBuffer>, ct: Uint8Array<ArrayBuffer>, aad: Uint8Array<ArrayBuffer>,
): Promise<Uint8Array<ArrayBuffer>> {
  try {
    return new Uint8Array(await subtle().decrypt(
      { name: 'AES-GCM', iv: nonce, additionalData: aad, tagLength: 128 }, aesKey, ct,
    ))
  } catch {
    throw new NodeVaultKeyError('open_failed', 'ciphertext')
  }
}

async function wrapKeyBlob(
  key: NodeVaultKey, wrapping: CryptoKey, keyId: string, orgId: string, recipient: string,
): Promise<NodeVaultKeyBlob> {
  const aad = keyBlobAad(orgId, key.keyHash, recipient)
  const aesKey = await subtle().generateKey({ name: 'AES-GCM', length: DATA_KEY_BITS }, true, ['encrypt'])
  const rawAesKey = new Uint8Array(await subtle().exportKey('raw', aesKey))
  const ek = new Uint8Array(await subtle().encrypt({ name: 'RSA-OAEP' }, wrapping, rawAesKey))
  rawAesKey.fill(0)
  const { n, ct } = await gcmSeal(aesKey, pkcs8Of(key), aad)
  return { v: KEY_BLOB_VERSION, k: keyId, alg: WRAP_ALGORITHM, ek: bytesToBase64(ek), n, ct }
}

/** `key` as a Setup key blob for one node: wrapped to the node's identity key (NIK), `k="nik:<fp>"`,
 *  recipient `node:<nodeId>`. The fingerprint is recomputed from `identityKey`, never taken from the
 *  server — which stops a lying fingerprint, not a substituted key: the UI shows the recomputed
 *  fingerprint so an admin can compare it with the node's own (D13, rogue-node enrollment is detectable,
 *  not impossible). */
export async function wrapKeyForNode(
  key: NodeVaultKey, target: { orgId: string, nodeId: string, identityKey: string },
): Promise<NodeVaultKeyBlob> {
  const nik = await importPublic(target.identityKey, 'node identity key')
  return wrapKeyBlob(key, nik.key, NIK_PREFIX + nik.hash, target.orgId, `node:${aadPart(target.nodeId, 'node id')}`)
}

/** `key` (the NEW key) as the rotation chain blob: wrapped under the CURRENT NodeVault public key
 *  (AC-6.2), `k="nv:<current hash>"`, recipient `chain:<current hash>`. The current hash is recomputed
 *  from `currentPublicKey`. */
export async function wrapKeyChain(
  key: NodeVaultKey, target: { orgId: string, currentPublicKey: string },
): Promise<NodeVaultKeyBlob> {
  const current = await importPublic(target.currentPublicKey, 'current NodeVault public key')
  return wrapKeyBlob(key, current.key, NV_PREFIX + current.hash, target.orgId, `chain:${current.hash}`)
}

/** The key a blob carries, opened with `unwrappingKey` (a NIK for `node:`, the current NVK for
 *  `chain:`). A blob proves where a key is BOUND, not who made it: anyone holding the recipient's
 *  public key can build one, so `keyHash` must come from a trusted pin, never from the response that
 *  delivered the blob. Mirror of `nodevault_crypto.unwrap_key_blob`: `wrong_recipient` when the blob names
 *  another key, `open_failed` when it does not open under the AAD, `hash_mismatch` unless the carried
 *  key's DERIVED hash is `keyHash`. */
export async function openKeyBlob(
  blob: unknown,
  opts: { unwrappingKey: NodeVaultKey, orgId: string, keyHash: string, recipient: string },
): Promise<NodeVaultKey> {
  const expected = checkHash(opts.keyHash)
  if (typeof blob !== 'object' || blob === null || Object.keys(blob).sort().join(',') !== 'alg,ct,ek,k,n,v') {
    throw new NodeVaultKeyError('malformed', 'key blob')
  }
  const b = blob as Record<string, unknown>
  if (b.v !== KEY_BLOB_VERSION) throw new NodeVaultKeyError('malformed', 'key blob version')
  if (b.alg !== WRAP_ALGORITHM) throw new NodeVaultKeyError('malformed', 'key blob algorithm')
  const recipient = aadPart(opts.recipient, 'recipient')
  const prefix = recipient.startsWith('chain:') ? NV_PREFIX : NIK_PREFIX
  if (b.k !== prefix + opts.unwrappingKey.keyHash) {
    throw new NodeVaultKeyError('wrong_recipient', 'the blob is wrapped to another key')
  }
  const ek = b64(b.ek, 'key blob wrapped key', WRAPPED_BYTES)
  const nonce = b64(b.n, 'key blob nonce', NONCE_BYTES, NONCE_BYTES)
  const ct = b64(b.ct, 'key blob ciphertext', MAX_KEY_CT_BYTES)
  const aad = keyBlobAad(opts.orgId, expected, recipient)
  let rawAesKey: Uint8Array<ArrayBuffer>
  try {
    const priv = await subtle().importKey('pkcs8', pkcs8Of(opts.unwrappingKey), RSA_PARAMS, false, ['decrypt'])
    rawAesKey = new Uint8Array(await subtle().decrypt({ name: 'RSA-OAEP' }, priv, ek))
  } catch (err) {
    if (err instanceof NodeVaultKeyError) throw err
    throw new NodeVaultKeyError('open_failed', 'wrapped key')
  }
  if (rawAesKey.length !== DATA_KEY_BITS / 8) {
    rawAesKey.fill(0)
    throw new NodeVaultKeyError('open_failed', 'wrapped key length')
  }
  const aesKey = await subtle().importKey('raw', rawAesKey, { name: 'AES-GCM' }, false, ['decrypt'])
  rawAesKey.fill(0)
  const carried = await keyFromPkcs8(await gcmOpen(aesKey, nonce, ct, aad))
  if (carried.keyHash !== expected) {
    carried.dispose()
    throw new NodeVaultKeyError('hash_mismatch', 'the unwrapped key is not the declared one')
  }
  return carried
}

/** The passphrase rule: at least `MIN_PASSPHRASE_LENGTH` characters (code points). */
export function passphraseAcceptable(passphrase: string): boolean {
  return typeof passphrase === 'string' && [...passphrase].length >= MIN_PASSPHRASE_LENGTH
}

/** The backup's AES-256-GCM key: PBKDF2-SHA256 over the NFC-normalised UTF-8 passphrase (NFC, so the
 *  same passphrase typed on another OS derives the same key). */
async function passphraseKey(
  passphrase: string, salt: Uint8Array<ArrayBuffer>, usage: 'encrypt' | 'decrypt',
): Promise<CryptoKey> {
  const material = await subtle().importKey(
    'raw', new Uint8Array(new TextEncoder().encode(passphrase.normalize('NFC'))), 'PBKDF2', false, ['deriveKey'],
  )
  return subtle().deriveKey(
    { name: 'PBKDF2', hash: 'SHA-256', salt, iterations: BACKUP_ITERATIONS },
    material, { name: 'AES-GCM', length: DATA_KEY_BITS }, false, [usage],
  )
}

/** The passphrase-encrypted backup of `key` for the admin to save. Refuses `weak_passphrase` below the
 *  minimum length (the UI's "enter it twice" check is the UI's). Without the passphrase the file holds
 *  only public material and ciphertext. */
export async function createBackup(
  key: NodeVaultKey, opts: { orgId: string, passphrase: string },
): Promise<NodeVaultBackup> {
  if (!passphraseAcceptable(opts.passphrase)) {
    throw new NodeVaultKeyError('weak_passphrase', `use at least ${MIN_PASSPHRASE_LENGTH} characters`)
  }
  if (typeof opts.orgId !== 'string' || opts.orgId === '') throw new NodeVaultKeyError('malformed', 'org id')
  subtle()
  const salt = crypto.getRandomValues(new Uint8Array(SALT_BYTES))
  const aesKey = await passphraseKey(opts.passphrase, salt, 'encrypt')
  const { n, ct } = await gcmSeal(aesKey, pkcs8Of(key), backupAad(key.keyHash))
  return {
    format: BACKUP_FORMAT,
    v: BACKUP_VERSION,
    org_id: opts.orgId,
    key_hash: key.keyHash,
    public_key: key.publicKey,
    kdf: { alg: BACKUP_KDF, iterations: BACKUP_ITERATIONS, salt: bytesToBase64(salt) },
    n,
    ct,
  }
}

/** The key a backup holds, from the file (its text or its parsed JSON) and the passphrase. `malformed`
 *  for a file that is not a backup, `open_failed` for a wrong passphrase or a tampered ciphertext,
 *  `hash_mismatch` when the key inside is not the one the file names. Nothing is produced on any of
 *  them. The returned key's `keyHash` is recomputed from the key itself. */
export async function restoreBackup(file: unknown, passphrase: string): Promise<NodeVaultKey> {
  let parsed: unknown = file
  if (typeof file === 'string') {
    if (file.length > MAX_BACKUP_CHARS) throw new NodeVaultKeyError('malformed', 'not a NodeVault backup file')
    try {
      parsed = JSON.parse(file)
    } catch {
      throw new NodeVaultKeyError('malformed', 'not a NodeVault backup file')
    }
  }
  if (typeof parsed !== 'object' || parsed === null
    || Object.keys(parsed).sort().join(',') !== 'ct,format,kdf,key_hash,n,org_id,public_key,v') {
    throw new NodeVaultKeyError('malformed', 'not a NodeVault backup file')
  }
  const f = parsed as Record<string, unknown>
  if (f.format !== BACKUP_FORMAT || f.v !== BACKUP_VERSION) throw new NodeVaultKeyError('malformed', 'backup version')
  if (typeof f.org_id !== 'string' || f.org_id === '') throw new NodeVaultKeyError('malformed', 'backup org id')
  const declared = checkHash(f.key_hash, 'backup key hash')
  const kdf = f.kdf
  if (typeof kdf !== 'object' || kdf === null || Object.keys(kdf).sort().join(',') !== 'alg,iterations,salt') {
    throw new NodeVaultKeyError('malformed', 'backup kdf')
  }
  const k = kdf as Record<string, unknown>
  // The pinned parameters only: a file cannot ask for a weaker (or a page-freezing) derivation.
  if (k.alg !== BACKUP_KDF || k.iterations !== BACKUP_ITERATIONS) throw new NodeVaultKeyError('malformed', 'backup kdf')
  const salt = b64(k.salt, 'backup salt', SALT_BYTES, SALT_BYTES)
  const nonce = b64(f.n, 'backup nonce', NONCE_BYTES, NONCE_BYTES)
  const ct = b64(f.ct, 'backup ciphertext', MAX_KEY_CT_BYTES)
  const pub = await importPublic(f.public_key, 'backup public key')
  if (pub.hash !== declared) throw new NodeVaultKeyError('hash_mismatch', 'the public key is not the one the file names')
  if (typeof passphrase !== 'string' || passphrase === '') throw new NodeVaultKeyError('open_failed', 'passphrase')
  const aesKey = await passphraseKey(passphrase, salt, 'decrypt')
  const key = await keyFromPkcs8(await gcmOpen(aesKey, nonce, ct, backupAad(declared)))
  if (key.keyHash !== declared) {
    key.dispose()
    throw new NodeVaultKeyError('hash_mismatch', 'the key inside is not the one the file names')
  }
  return key
}

/** The `credentialSeal.sealSecret` key parameter for an NVK public key: `key_id` is `nv:<hash>` with
 *  the hash recomputed here, never taken from the server's `key_id`. */
export async function nodeVaultSealKey(publicKey: string): Promise<CredentialSealKey> {
  const { hash } = await importPublic(publicKey, 'NodeVault public key')
  return { key_id: NV_PREFIX + hash, algorithm: WRAP_ALGORITHM, public_key: publicKey }
}

/** Test and interop seam: a `NodeVaultKey` over PKCS#8 DER (a test NIK, a vector key). The page never
 *  holds a raw private key, so it never calls this. */
export async function nodeVaultKeyFromPkcs8(pkcs8: Uint8Array<ArrayBuffer>): Promise<NodeVaultKey> {
  return keyFromPkcs8(new Uint8Array(pkcs8))
}
