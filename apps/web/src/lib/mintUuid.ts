/* A v4 UUID minted in the browser, for a row or request named before the server sees it. One home for
 * `lib/billingOrgs.ts::mintRequestId` (a refund or charge) and `pages/credentials/ownership.ts::
 * mintCredentialId` (a credential row); each decides what its caller gets when none can be made. */

/** `crypto.randomUUID` exists only in a SECURE context; over plain http on a LAN address
 *  `getRandomValues` still does, so the id is built from it by hand (RFC 4122 §4.4: version nibble 4,
 *  variant bits 10). `undefined` when even that throws — no `crypto` at all. */
export function mintUuid(): string | undefined {
  try {
    if (typeof crypto.randomUUID === 'function') return crypto.randomUUID()
    const bytes = crypto.getRandomValues(new Uint8Array(16))
    bytes[6] = (bytes[6] & 0x0f) | 0x40
    bytes[8] = (bytes[8] & 0x3f) | 0x80
    const hex = Array.from(bytes, b => b.toString(16).padStart(2, '0')).join('')
    return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`
  } catch {
    return undefined
  }
}
