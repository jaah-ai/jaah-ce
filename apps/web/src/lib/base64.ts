/* Standard base64 (RFC 4648 §4, `+`/`/`, padded) over raw bytes — the one wire spelling every seal
 * shares (`loginSeal.ts`, `credentialSeal.ts`, and the test-only `test/sealedSecrets.ts`). `btoa`/`atob`
 * take and give latin-1 strings, so bytes map one char each; nothing here is UTF-8 aware. */

export function bytesToBase64(bytes: Uint8Array): string {
  let binary = ''
  for (const b of bytes) binary += String.fromCharCode(b)
  return btoa(binary)
}

/** A fresh `ArrayBuffer`-backed view, so WebCrypto accepts it where a `SharedArrayBuffer` view would not. */
export function base64ToBytes(text: string): Uint8Array<ArrayBuffer> {
  const binary = atob(text)
  const out = new Uint8Array(new ArrayBuffer(binary.length))
  for (let i = 0; i < binary.length; i += 1) out[i] = binary.charCodeAt(i)
  return out
}
