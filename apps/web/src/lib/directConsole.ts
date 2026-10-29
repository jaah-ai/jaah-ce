// The browser half of the DIRECT console transport: ask the CP for a grant, then dial the node itself.
// The wire contract with apps/jaahd/console_direct.py; the state machine lives in ConsolePane.
//
// THE CLIENT ASSEMBLES NO URL: `direct_url` arrives complete (apps/api/app/console_direct.py's
// `compose_direct_url` is the ONE home of the path template) and is dialled verbatim.
//
// THE TOKEN NEVER TOUCHES THE URL: it rides the handshake as a `tok.<token>` subprotocol, because a query
// string lands in access logs, history and `Referer`. Nothing here logs, stores or interpolates it.
import { get, ApiError, apiDetail } from '../api'
import { sessionHeaders } from './activeOrg'
import { readString, writeString } from './safeStorage'

/** The subprotocol that names this wire version: the node echoes it back, never the credential. */
export const DIRECT_SUBPROTOCOLS = ['jaah.console.v1']
const TOKEN_SUBPROTOCOL_PREFIX = 'tok.'

/** A redeemable direct-connect grant. `expiresAt` is an ISO-8601 instant; the node enforces it. */
export type DirectGrant = {
  directUrl: string
  token: string
  expiresAt: string
}

/**
 * The outcome of asking for a grant. `ok: false` is a NORMAL, displayable answer (HTTP 200 from the CP)
 * so it cannot collapse into a silent fallback. `disabled` marks the CP's standing "direct console is
 * not configured for this node" verdict — the one answer the pane relays on instead of retrying.
 */
export type DirectGrantResult =
  | { ok: true; grant: DirectGrant }
  | { ok: false; reason: string; disabled?: boolean }

// The CP's raw union; callers branch on `DirectGrantResult`.
type DirectIssueResponse =
  | { status: 'ok'; direct_url: string; token: string; expires_at: string }
  | { status: 'unavailable'; reason: string }
  | { status: 'disabled'; reason: string }

/**
 * The `Sec-WebSocket-Protocol` values: the versions, then the credential. ORDER MATTERS — the node picks
 * the first value it recognises as the version and reads the token from the `tok.` one. The CP mints
 * `tchar`-only tokens; anything else makes the header unparseable (an opaque 1006).
 */
export function directSubprotocols(token: string): string[] {
  return [...DIRECT_SUBPROTOCOLS, `${TOKEN_SUBPROTOCOL_PREFIX}${token}`]
}

/**
 * The C-7 hello, as the TEXT frame the node expects first, within 5s of open. MUST BE TEXT (a binary
 * hello closes 4400): after the ack the wire is the `[type:1][len:2][payload]` envelope, where bytes
 * would parse silently as frame type 123 (`{`).
 *
 * `view` is `@<window id>` for a TILE of a designed layout, null for the single-pane console; a tile
 * sending null would show the slot's own window, so every tile would render the SAME terminal. The node
 * validates it and refuses with a close code rather than attaching elsewhere.
 */
export function directHelloFrame(
  cols: number,
  rows: number,
  attachOnly: boolean,
  view?: string,
  layout?: string,
): string {
  // `layout`: a designed grid the node builds as ONE tmux window. Never both — the node refuses a hello
  // carrying `view` and `layout`.
  return JSON.stringify({
    cols, rows, attach_only: attachOnly, view: view ?? null, layout: layout ?? null,
  })
}

/**
 * Whether `data` is the node's `{"ok":true}` ack. Parsed as JSON, never string-compared (`json.dumps`
 * spacing is the Python side's detail). Until it arrives no input may be sent.
 */
export function isDirectAck(data: unknown): boolean {
  if (typeof data !== 'string') return false
  try {
    const parsed: unknown = JSON.parse(data)
    return typeof parsed === 'object' && parsed !== null && (parsed as { ok?: unknown }).ok === true
  } catch {
    return false
  }
}

/**
 * Ask the CP for a grant on this slot. Never throws: every failure becomes `ok: false` with a showable
 * reason — the caller retries indefinitely and the reason is the only thing on screen.
 */
export async function fetchDirectGrant(
  projectId: number,
  number: number,
  kind: 'clauded' | 'term',
  nodeId?: string,
  provider?: string,
): Promise<DirectGrantResult> {
  const node = nodeId ? `&node=${encodeURIComponent(nodeId)}` : ''
  // Absent = Any (the CP's Auto-assign pick); only a non-empty catalogue value is sent.
  const prov = provider ? `&provider=${encodeURIComponent(provider)}` : ''
  const path = `/api/console/${projectId}/${number}/direct?kind=${kind}${node}${prov}`
  let body: DirectIssueResponse
  try {
    body = await get<DirectIssueResponse>(path)
  } catch (err) {
    // A 401 already cleared the auth snapshot in `api()`. `apiDetail` keeps the CP's own wording.
    if (err instanceof ApiError) return { ok: false, reason: apiDetail(err, `the console answered ${err.status}`) }
    return { ok: false, reason: 'the console could not be reached' }
  }
  if (body?.status === 'ok' && body.direct_url && body.token) {
    return { ok: true, grant: { directUrl: body.direct_url, token: body.token, expiresAt: body.expires_at } }
  }
  if (body?.status === 'unavailable') {
    return { ok: false, reason: body.reason || 'this node cannot serve a direct connection' }
  }
  if (body?.status === 'disabled') {
    return { ok: false, disabled: true, reason: body.reason || 'direct console is not configured for this node' }
  }
  return { ok: false, reason: 'the console returned an unrecognised direct-connection answer' }
}

/**
 * Hand a grant back before it expires. WITHOUT THIS, EVERY ABANDONED GRANT STAYS LIVE FOR ITS FULL 24
 * HOURS (closing the overlay, replacing a stale grant, chevron-hopping), each good for an interactive
 * shell. The CP keeps no grant store, so only the client knows.
 *
 * Best-effort and unawaited: a lost revoke costs the TTL, a blocking one would stop the console closing.
 * `keepalive` outlives the unload. The token goes in the BODY (a query string reaches the access log).
 */
export function revokeDirectGrant(
  projectId: number,
  number: number,
  kind: 'clauded' | 'term',
  token: string,
  nodeId?: string,
): void {
  try {
    void fetch(`/api/console/${projectId}/${number}/direct/revoke`, {
      method: 'POST',
      credentials: 'same-origin',
      keepalive: true,
      headers: sessionHeaders({ 'Content-Type': 'application/json' }, 'POST'),
      body: JSON.stringify({ kind, node: nodeId ?? null, token }),
    }).catch(() => {
      /* the grant's own TTL is the backstop — there is nothing to tell the user */
    })
  } catch {
    /* fetch itself unavailable (teardown during unload); same backstop */
  }
}

// ── The `direct_url` override ──
//
// A real setting, not a test seam: the only way to reach the UNREACHABLE-NODE state deliberately, and a
// fix for a stale published address. `localStorage`, per slot, no API: a per-browser diagnostic that must
// not follow the user to another device.

/**
 * The key an override is stored under: per kind (Web Console and Web Term are different sessions), per
 * slot, and per NODE (`(project, number)` names one slot PER NODE). Every caller passes `nodeId`.
 */
export function directUrlOverrideKey(
  project: string,
  number: number,
  kind: 'clauded' | 'term',
  nodeId?: string,
): string {
  return `console-direct-url:${project}/${number}/${kind}@${nodeId ?? ''}`
}

/** The stored override, or '' when there is none or storage cannot be read (private-mode Safari throws
 *  on ACCESS). */
export function loadDirectUrlOverride(key: string): string {
  return readString('local', key) ?? ''
}

/** Persist an override, or remove it when blank. Returns whether the write landed; never block applying
 *  it on that. */
export function saveDirectUrlOverride(key: string, value: string): boolean {
  return writeString('local', key, value.trim() || null)
}

// Loopback is the one place plaintext is tolerable. Mirrors the CP's own origin-validation rule.
const LOOPBACK_HOSTS = new Set(['127.0.0.1', '::1', '[::1]', 'localhost'])

/**
 * The address a direct dial should use: a usable override, else the CP-issued URL verbatim.
 *
 * THE OVERRIDE MAY MOVE THE PORT AND THE PATH. IT MAY NOT MOVE THE MACHINE: a different host is ignored
 * entirely, not dialled without a credential — a hostile peer that is dialled at all answers
 * `{"ok":true}` and receives every keystroke and owns what the terminal renders (a fake sudo prompt).
 *
 * `wss:` only, except to loopback (the CP's rule for a node's published origin). Unparseable or blank
 * is ignored (blank is how the setting is turned off).
 */
export function directDialUrl(grantUrl: string, override?: string): string {
  const candidate = override?.trim()
  if (!candidate) return grantUrl
  try {
    const parsed = new URL(candidate)
    const granted = new URL(grantUrl)
    const schemeOk =
      parsed.protocol === 'wss:' || (parsed.protocol === 'ws:' && LOOPBACK_HOSTS.has(parsed.hostname))
    if (!schemeOk) return grantUrl
    // Hostname, not host: the port may move, the machine may not. Both from one parser, so already
    // canonicalised.
    if (parsed.hostname.toLowerCase() !== granted.hostname.toLowerCase()) return grantUrl
    return candidate
  } catch {
    return grantUrl
  }
}

/**
 * Whether a dial address is the machine the CP named, i.e. whether the grant may be presented to it.
 * A SECOND GATE OVER THE SAME RULE, ON PURPOSE: today it can only answer true, so loosening
 * `directDialUrl` can never silently hand the credential to a stranger.
 */
export function isGrantedHost(grantUrl: string, dialUrl: string): boolean {
  if (dialUrl === grantUrl) return true
  try {
    return new URL(dialUrl).hostname.toLowerCase() === new URL(grantUrl).hostname.toLowerCase()
  } catch {
    return false
  }
}

/**
 * A node-supplied `reason` made safe to write into an xterm buffer: C0/C1 controls stripped so an ESC
 * cannot repaint the screen. React covers the DOM side.
 */
export function sanitizeDirectReason(reason: string): string {
  // eslint-disable-next-line no-control-regex
  return reason.replace(/[\u0000-\u001f\u007f-\u009f]/g, ' ').trim()
}
