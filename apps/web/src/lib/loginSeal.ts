/* The login seal, browser half — the mirror of `apps/jaahd/login_seal.py`, which is the construction's
 * single home. Change one, change both:
 *   • keys: RSA-2048, on the wire as base64 SPKI DER (`peer_key` is ours, `seal_key` the node's);
 *   • a sealed value: UTF-8 plaintext in ≤190-byte chunks, each RSA-OAEP (SHA-256, MGF1-SHA256, no
 *     label) → 256 bytes, concatenated in order, base64 (`sealed_url` from the node, `sealed_code` to it).
 * The private key is a non-extractable WebCrypto key that lives only in this page's memory: a reload
 * loses it, and the login is simply started again. Nothing here touches the DOM or storage.
 */

import { base64ToBytes, bytesToBase64 } from './base64.ts' // the extension is load-bearing: an interop test runs this file under node's type stripping, which resolves no extensionless import

export const CHUNK_PLAINTEXT_BYTES = 190
export const CHUNK_CIPHERTEXT_BYTES = 256

const ALGORITHM = { name: 'RSA-OAEP', hash: 'SHA-256' } as const
const KEY_GENERATION = {
  ...ALGORITHM,
  modulusLength: 2048,
  publicExponent: new Uint8Array([1, 0, 1]),
} as const

/** A fresh key pair for one login. The private half is non-extractable by construction. */
export function createSealKeyPair(): Promise<CryptoKeyPair> {
  return crypto.subtle.generateKey(KEY_GENERATION, false, ['encrypt', 'decrypt'])
}

/** The wire form of our public key: what phase 1 sends as `peer_key`. */
export async function exportPeerKey(pair: CryptoKeyPair): Promise<string> {
  return bytesToBase64(new Uint8Array(await crypto.subtle.exportKey('spki', pair.publicKey)))
}

/** `text` sealed to the node's wire key `sealKey` (its `seal_key`): the phase-2 `sealed_code`. */
export async function seal(sealKey: string, text: string): Promise<string> {
  const key = await crypto.subtle.importKey('spki', base64ToBytes(sealKey), ALGORITHM, false, ['encrypt'])
  const data = new TextEncoder().encode(text)
  const chunks: Uint8Array[] = []
  for (let start = 0; start < data.length; start += CHUNK_PLAINTEXT_BYTES) {
    const piece = data.subarray(start, start + CHUNK_PLAINTEXT_BYTES)
    chunks.push(new Uint8Array(await crypto.subtle.encrypt({ name: 'RSA-OAEP' }, key, piece)))
  }
  const out = new Uint8Array(chunks.length * CHUNK_CIPHERTEXT_BYTES)
  chunks.forEach((c, i) => out.set(c, i * CHUNK_CIPHERTEXT_BYTES))
  return bytesToBase64(out)
}

/** A `sealed_url` the node sealed to our `peer_key`, back to text. Rejects with a `DOMException` (or an
 *  `Error` on a malformed blob) when the blob was not sealed to `pair` — a login started from another
 *  page, or a tampered relay. */
export async function unseal(pair: CryptoKeyPair, sealed: string): Promise<string> {
  const raw = base64ToBytes(sealed)
  if (raw.length === 0 || raw.length % CHUNK_CIPHERTEXT_BYTES !== 0) {
    throw new Error('sealed value is not whole chunks')
  }
  const pieces: Uint8Array[] = []
  for (let start = 0; start < raw.length; start += CHUNK_CIPHERTEXT_BYTES) {
    const chunk = raw.subarray(start, start + CHUNK_CIPHERTEXT_BYTES)
    pieces.push(new Uint8Array(await crypto.subtle.decrypt({ name: 'RSA-OAEP' }, pair.privateKey, chunk)))
  }
  const total = pieces.reduce((n, p) => n + p.length, 0)
  const joined = new Uint8Array(total)
  let offset = 0
  for (const p of pieces) {
    joined.set(p, offset)
    offset += p.length
  }
  // `fatal`: a chunk decoded to non-UTF-8 is a defect, not a URL to render.
  return new TextDecoder('utf-8', { fatal: true }).decode(joined)
}
