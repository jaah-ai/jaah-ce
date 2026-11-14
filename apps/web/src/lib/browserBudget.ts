/* Gives this browser its OWN signed-in rate budget at the WAF (imported early by `main.tsx`, before any request).
 * The WAF's signed-in rate rule keys on the network address PLUS this cookie (the rule's home is the edge ACL's
 * terraform), so the people and agent browsers behind one office address stop spending one another's budget.
 *
 * NOT A CREDENTIAL: a random id that names nothing and grants nothing. A rate key's value is written to the WAF
 * log, which is why the key is this cookie and never the session one. A client that forges or rotates it gains a
 * fresh per-browser budget only, under the per-network ceiling. */
export const BROWSER_COOKIE = 'jaah_browser'

const ONE_YEAR_S = 365 * 24 * 60 * 60
const VALID = /^[A-Za-z0-9-]{8,64}$/

function readCookie(cookies: string, name: string): string | null {
  for (const part of cookies.split(';')) {
    const [key, ...rest] = part.trim().split('=')
    if (key === name) return rest.join('=')
  }
  return null
}

export function ensureBrowserBudget(doc: Document = document, newId: () => string = () => crypto.randomUUID()): void {
  const current = readCookie(doc.cookie, BROWSER_COOKIE)
  if (current && VALID.test(current)) return
  const secure = doc.location?.protocol === 'https:' ? '; Secure' : ''
  doc.cookie = `${BROWSER_COOKIE}=${newId()}; Path=/; Max-Age=${ONE_YEAR_S}; SameSite=Lax${secure}`
}

try {
  ensureBrowserBudget()
} catch {
  // No cookie store (a sandboxed frame) or no crypto: the browser falls back to the per-network ceiling alone.
}
