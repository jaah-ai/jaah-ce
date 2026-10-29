// ONE terminal and its socket: a thin xterm ⇄ WebSocket bridge to the host agent's PTY
// (tmux attach): POST /api/console/{project}/{number}/open sets the session up, then the socket dials the
// console relay at the URL it answered (`apps/api/app/console_relay/`). Renders only the
// `data-testid="console-body"` element; chrome belongs to the mounting overlay, so N panes can share one
// header.
//
// Self-healing: a keepalive frame stops idle reaping and abnormal closes retry. Reconnect is cheap because
// tmux owns the state (re-attach redraws). Closing the socket makes the agent DETACH tmux — never kill.
// Same-origin throughout: the cookie authenticates the open; the relay socket is authorized by the
// single-use ticket in its URL.
//
// `direct` dials the node itself and NEVER falls back to the relay; policy and bounds live in the
// "Direct transport" section below.
import { forwardRef, useCallback, useEffect, useImperativeHandle, useLayoutEffect, useRef } from 'react'
import { Terminal } from '@xterm/xterm'
import {
  DIRECT_SUBPROTOCOLS,
  directDialUrl,
  directHelloFrame,
  directSubprotocols,
  fetchDirectGrant,
  isDirectAck,
  isGrantedHost,
  revokeDirectGrant,
  sanitizeDirectReason,
  type DirectGrant,
} from '../lib/directConsole'
import { sessionHeaders } from '../lib/activeOrg'
import { clipboardFiles } from '../lib/clipboardFiles'
import { ClipboardAddon } from '@xterm/addon-clipboard'
import { FitAddon } from '@xterm/addon-fit'
import { WebglAddon } from '@xterm/addon-webgl'
import '@xterm/xterm/css/xterm.css'
import Box from '@mui/material/Box'
import { RAW } from '../theme'
import { copyText } from '../ui/useCopyFlash'
import { attachControlPlaneBadges } from './consoleLabels'

/**
 * The terminal's colours, from `theme.ts`'s literal `RAW` map, never `theme.palette.*`: under
 * `cssVariables` the palette holds `var(--…)` strings a canvas renderer cannot resolve (it silently paints
 * black). Always `RAW.dark`, in both schemes: the canvas hosts TUIs whose ANSI palettes assume a dark
 * ground. Only surface colours are set; xterm's stock 16-colour ANSI palette is left alone on purpose.
 */
export const XTERM_THEME = {
  background: RAW.dark.terminalBg,
  foreground: RAW.dark.textPrimary,
  cursor: RAW.dark.primary,
  cursorAccent: RAW.dark.terminalBg,
  // 8-digit hex: xterm wants the selection translucent so the glyphs under it stay legible.
  selectionBackground: `${RAW.dark.primary}66`,
} as const

// Probed on a THROWAWAY canvas: an addon whose activate() fails stays registered half-built and throws
// from its own dispose() later. `getContext` throws on some locked-down engines, hence the catch.
function supportsWebgl2(): boolean {
  // Constructor check first: under jsdom `getContext` logs via console.error, which the unit tests assert
  // stays silent.
  if (typeof WebGL2RenderingContext === 'undefined') return false
  try {
    return document.createElement('canvas').getContext('webgl2') !== null
  } catch {
    return false
  }
}

// A throw from dispose() on the cleanup path becomes a React render error that takes the terminal down.
function disposeQuietly(disposable: { dispose: () => void }): void {
  try {
    disposable.dispose()
  } catch {
    /* a renderer that cannot even tear itself down changes nothing for the user */
  }
}

// `attachOnly` is set on every RECONNECT, never a first attach: it forbids the daemon's create path, so a
// retry cannot silently spawn a NEW agent (kind=clauded runs clauded_wt.py against account quota) and
// claim a free slot. A request, not the enforcement: the CP applies its own tier and budget bounds.
function consoleOpenUrl(
  projectId: number, number: number, nodeId: string | undefined,
  kind: string, cols: number, rows: number, attachOnly: boolean,
  view: string | undefined,
  layout: string | undefined,
  provider: string | undefined,
): string {
  const attach = attachOnly ? '&attach_only=1' : ''
  // `node` rides every attempt: dropping it could re-attach to another node's same-numbered slot.
  const node = nodeId ? `&node=${encodeURIComponent(nodeId)}` : ''
  // `view=@<window_id>`: show ONE window of the node's shared `fleet` session. `layout=<spec>`: one window
  // whose panes are the designed tiles. Both omitted when absent so a plain console's URL is unchanged.
  const viewParam = view ? `&view=${encodeURIComponent(view)}` : ''
  const layoutParam = layout ? `&layout=${encodeURIComponent(layout)}` : ''
  // `provider`: the account's provider for a NEW session; absent = Any (the CP's Auto-assign pick).
  const providerParam = provider ? `&provider=${encodeURIComponent(provider)}` : ''
  return `/api/console/${projectId}/${number}/open?kind=${kind}&cols=${cols}&rows=${rows}${node}${attach}${viewParam}${layoutParam}${providerParam}`
}

/** A close as the reconnect decision reads it — a socket's `CloseEvent`, or a refused open's body, whose
 *  `detail` is the API's one line for the pane to print and `throttled` its 429 (the create budget spent). */
type ConsoleClose = { code: number; reason: string; detail?: string; throttled?: boolean }

// A transport failure before any verdict: the code a dropped handshake closes with, so the pane reads it
// exactly as it read the old socket's (recoverable, 'disconnected').
const ABNORMAL_CLOSE: ConsoleClose = { code: 1006, reason: '' }

/**
 * Ask the API to set the session up (`routes/console.py::console_open`). Never throws. Resolves to the
 * relay's same-origin path, or to the close the old socket would have carried: the refusal's own
 * `close_code`; 4401 for a bare 401/403 (an auth middleware's answer); otherwise ABNORMAL_CLOSE — a
 * network error or a proxy's 502 during an API deploy must retry, never end the console.
 * A raw `fetch`, not `api()`: a 401 here is the pane's verdict to render, not a global sign-out.
 */
async function openConsole(path: string): Promise<{ url: string } | { closed: ConsoleClose }> {
  let res: Response
  try {
    res = await fetch(path, { method: 'POST', credentials: 'same-origin', headers: sessionHeaders(undefined, 'POST') })
  } catch {
    return { closed: ABNORMAL_CLOSE }
  }
  let body: unknown = null
  try {
    body = await res.json()
  } catch {
    /* no JSON body — decided by the status alone */
  }
  const fields = (body ?? {}) as { url?: unknown; close_code?: unknown; reason?: unknown; detail?: unknown }
  if (res.ok) {
    // Same-origin relative path only: anything else is not this API's answer.
    const { url } = fields
    if (typeof url === 'string' && url.startsWith('/') && !url.startsWith('//')) return { url }
    return { closed: ABNORMAL_CLOSE }
  }
  if (typeof fields.close_code === 'number') {
    const reason = typeof fields.reason === 'string' ? fields.reason : ''
    // One line, whatever the server sent: the pane composes it into a single terminal row.
    const detail = typeof fields.detail === 'string' ? fields.detail.split('\n')[0].trim() : ''
    return {
      closed: { code: fields.close_code, reason, ...(detail ? { detail } : {}), ...(res.status === 429 ? { throttled: true } : {}) },
    }
  }
  if (res.status === 401 || res.status === 403) return { closed: { code: UNAUTHORIZED_CLOSE_CODE, reason: '' } }
  return { closed: ABNORMAL_CLOSE }
}

// Client→agent frame: 1-byte type + 2-byte big-endian length + payload. 0 = input (verbatim to PTY),
// 1 = resize (cols hi/lo, rows hi/lo → TIOCSWINSZ), 2/3 = pasted image, 4 = copy pane text (empty; the
// text comes back as tmux's OSC 52 on the raw stream). Agent→browser is raw.
// Agent side: apps/jaahd/console.py's ConsoleBridge.
const FRAME_TYPE_INPUT = 0
const FRAME_TYPE_RESIZE = 1
const FRAME_TYPE_PASTE_BEGIN = 2
const FRAME_TYPE_PASTE_DATA = 3
const FRAME_TYPE_COPY_PANE = 4
// A pane copy that has not come back as OSC 52 by then reports failed (no tmux client, or a node too old to
// know the frame) — the button must not wait forever.
const COPY_PANE_TIMEOUT_MS = 8000
const FRAME_MAX_INPUT_PAYLOAD = 60000   // comfortably under the frame length field's 65535 cap

function sendFrame(ws: WebSocket, type: number, payload: Uint8Array): void {
  if (ws.readyState !== WebSocket.OPEN) return
  const frame = new Uint8Array(3 + payload.length)
  frame[0] = type
  frame[1] = (payload.length >> 8) & 0xff
  frame[2] = payload.length & 0xff
  frame.set(payload, 3)
  ws.send(frame)
}

function sendInput(ws: WebSocket, encoder: TextEncoder, data: string): void {
  const bytes = encoder.encode(data)
  for (let offset = 0; offset < bytes.length; offset += FRAME_MAX_INPUT_PAYLOAD) {
    sendFrame(ws, FRAME_TYPE_INPUT, bytes.subarray(offset, offset + FRAME_MAX_INPUT_PAYLOAD))
  }
}

function sendResize(ws: WebSocket, cols: number, rows: number): void {
  sendFrame(ws, FRAME_TYPE_RESIZE, new Uint8Array([(cols >> 8) & 0xff, cols & 0xff, (rows >> 8) & 0xff, rows & 0xff]))
}

const encoder = new TextEncoder()

// ── Pasted images ───────────────────────────────────────────────────────────────────────────────────
// Images travel on their own frame types; the daemon writes a file and injects its path into the prompt.
// Nothing is echoed locally — the PTY's echo is the one writer. Limits are authoritative in
// apps/jaahd/console.py (_PASTE_MAX_BYTES / _PASTE_EXTS); this copy only avoids a doomed upload.
const PASTE_MAX_BYTES = 10 * 1024 * 1024
// The map doubles as the allowlist (`image/jpeg` → `jpg`; svg/tiff are not ours).
const PASTE_EXT_BY_MIME: Record<string, string> = {
  'image/png': 'png',
  'image/jpeg': 'jpg',
  'image/jpg': 'jpg',
  'image/gif': 'gif',
  'image/webp': 'webp',
}

type WireFrame = { type: number; payload: Uint8Array }

export function pasteExtFromMime(mime: string): string | null {
  // A clipboard `type` may carry parameters (`image/png; charset=…`).
  return PASTE_EXT_BY_MIME[mime.split(';')[0].trim().toLowerCase()] ?? null
}

// Header (size + ext) then chunks. No end frame — the declared size IS the terminator, so both are built
// together. `null` = not forwarded (unsupported, empty, or over the cap).
export function buildPasteFrames(bytes: Uint8Array, mime: string): WireFrame[] | null {
  const ext = pasteExtFromMime(mime)
  if (ext === null || bytes.length === 0 || bytes.length > PASTE_MAX_BYTES) return null
  const frames: WireFrame[] = [
    { type: FRAME_TYPE_PASTE_BEGIN, payload: encoder.encode(JSON.stringify({ size: bytes.length, ext })) },
  ]
  for (let offset = 0; offset < bytes.length; offset += FRAME_MAX_INPUT_PAYLOAD) {
    frames.push({ type: FRAME_TYPE_PASTE_DATA, payload: bytes.subarray(offset, offset + FRAME_MAX_INPUT_PAYLOAD) })
  }
  return frames
}

// OSC 52 provider, write-only ON PURPOSE: answering READ requests would let any program in the tmux
// session read the user's clipboard.
export function makeClipboardProvider(write: (text: string) => void) {
  return {
    readText: () => '',
    writeText: (_selection: string, text: string) => {
      write(text)
    },
  }
}

// ── Whose key is Ctrl+V? ────────────────────────────────────────────────────────────────────────────
// A `paste` event only fires if the browser runs its paste command. xterm claims Ctrl+V (a C0 control,
// writes SYN and calls preventDefault), so off-Apple no paste event is ever born; ⌘V is unaffected. So
// off-Apple the chord is handed back to the browser, giving up the literal SYN. Ctrl+Shift+V is included
// (xterm already ignores it) to keep one rule.
const APPLE_PLATFORM = /^(Mac|iPhone|iPad|iPod)/

export function isApplePlatform(platform: string | undefined): boolean {
  return APPLE_PLATFORM.test(platform ?? '')
}

// `keyCode` is what xterm's own dispatch switches on; `code` covers Firefox's keypress, which has none.
export function isPasteChord(
  event: Pick<KeyboardEvent, 'ctrlKey' | 'altKey' | 'metaKey' | 'code' | 'keyCode'>,
  apple: boolean,
): boolean {
  if (apple) return false
  return event.ctrlKey && !event.altKey && !event.metaKey && (event.code === 'KeyV' || event.keyCode === 86)
}

// Intermediaries (an ALB's 60s idle timeout) reap idle sockets well short of jaahd's
// CONSOLE_IDLE_TIMEOUT_SECONDS or nginx's proxy_read_timeout. A zero-length INPUT frame counts as traffic
// end to end and refreshes the daemon's last_activity (not via sendInput(), which sends nothing for "").
// Foreground tabs only: Chrome throttles hidden-tab intervals to ~1/min, so those rely on reconnect.
export const KEEPALIVE_INTERVAL_MS = 25_000

// A DENYLIST on purpose: an unenumerated code must fall on the recoverable side (uvicorn sends 1012 on
// every relay restart). Terminal codes:
//   4401/4404 — unauthorized / no worktree.
//   4502      — node unavailable; terminal on a FIRST attach only. On a reconnect it is handled before
//               this set (SESSION_GONE_CLOSE_CODE / scheduleSessionGoneRetry).
//   1000      — the session itself ended. Recoverable drops arrive as 4503; the verdict is the node's
//               (apps/jaahd/console.py `_CLOSE_REASON_RETRYABLE`; relay close codes:
//               apps/api/app/console_relay/server.py; refused opens: routes/console.py::console_open).
//               This pane cannot verify that table.
//   4400      — direct path: a hello the node could not read (our bug); the relay never emits it.
//   4409      — the org's cap refused the slot claim (routes/console.py::console_open); its reason is the sentence to show.
//   4408      — a HIPAA org's console closed after 15 minutes with no keystroke (relay `BROWSER_CLOSE_IDLE`).
//   4403      — the viewer's address left the org's IP allowlist (relay `BROWSER_CLOSE_NETWORK`).
// 1005 is absent: no peer here emits a bare close frame (timeouts surface as 1006).
const NON_RETRYABLE_CLOSE_CODES = new Set([1000, 4400, 4401, 4403, 4404, 4408, 4409, 4502])
// The session-level verdict: falling back or recreating would announce an end in reverse.
const SESSION_ENDED_CLOSE_CODE = 1000
// `console_open`'s `_WS_UPSTREAM_UNAVAILABLE` refusal: usually "no live session" for an attach_only dial, but a
// CATCH-ALL (NODE_TOKEN mismatch, no account, failed create, no tmux), hence the budgeted response in
// scheduleSessionGoneRetry. Not 1000: treating 4502 as an ending strands a fine slot; treating 1000 as
// recoverable respawns a shell somebody exited.
const SESSION_GONE_CLOSE_CODE = 4502

// Printed for a 4502 only when the opener's row also said no session (`hadLiveSession === false`).
// Neither fact alone means "empty": a wedged runner has a tmux window with nothing sensed
// (`lib/worktree.ts::canOpenConsoleOn` still offers it), and 4502 is a catch-all. Valid only until this
// pane has held a session (`sessionHeldRef`). Never match the node's reason string instead — the node
// ships independently, so a rephrase would break it silently. Claims only the slot, not the task.
// Kept to one short clause: both retry paths compose it into a single terminal row.
const SLOT_EMPTY_REASON = 'this slot has no session running in it'

// Token missing/invalid/expired/foreign/revoked collapse to one code (no slot-enumeration oracle).
const UNAUTHORIZED_CLOSE_CODE = 4401
const RECONNECT_BASE_DELAY_MS = 1000
const RECONNECT_MAX_DELAY_MS = 15_000
// Hold time before the attempt streak is forgiven; resetting on `onopen` let a flapping backend retry
// at 1s forever.
const RECONNECT_STABLE_AFTER_MS = 5000
// Exponential ladder length: 1+2+4+8s then sixteen at the 15s ceiling ≈ 255s, then the flat cadence.
export const RECONNECT_MAX_ATTEMPTS = 20

// Flat cadence past the ladder, for as long as the pane is mounted: the ordinary outage (node off the
// bus, relay 4503) must recover without a click. Flat because waiting longer teaches nothing. Hidden tabs
// are clamped to ~1/min by the browser. Cost is bounded by the overlay's lifetime, like the keepalive;
// a node-off-bus dial is refused at the CP without refreshing the session. No jitter and no
// `connecting` status per dial (it would flicker the header dot).
export const RECONNECT_IDLE_DELAY_MS = 10_000

// Create-capable dials per outage before the cadence reverts to attach-only. One covers the ordinary
// case; two spare cover transient failures. Each clauded create spends quota even if the console is
// unusable (`_post_create_not_found`), so an unattended tab must not keep paying. The cadence keeps
// re-attaching regardless; starting anew goes back to the Reconnect button.
export const SESSION_GONE_CREATE_BUDGET = 3

// Max fraction `jitter` pulls a backoff delay earlier (never past the ceiling). De-synchronises the N
// sockets of a tiled console dropped by one outage. Per pane, default 0 = the exact deterministic
// schedule; only tiles get a non-zero value (`paneJitter` in ConsoleTiles.tsx).
const RECONNECT_JITTER = 0.5

/** Delay before reconnect `attempt`, pulled earlier by `jitter` ∈ [0,1). Exported for tests. */
export function reconnectDelayMs(attempt: number, jitter: number = 0): number {
  const base = Math.min(RECONNECT_BASE_DELAY_MS * 2 ** (attempt - 1), RECONNECT_MAX_DELAY_MS)
  const spread = Math.min(Math.max(jitter, 0), 1)
  return Math.round(base * (1 - RECONNECT_JITTER * spread))
}

// tmux's per-view title `#{window_id} #{window_name}` (OSC 0). `@<digits>` is an unambiguous prefix, so
// one space separates even though names contain spaces and `|`.
const VIEW_TITLE_RE = /^(@\d+) ?(.*)$/

/** `@7 acme-3|task-42 …` → the window it names, or null for any title that is not one of ours. */
export function parseViewTitle(title: string): { id: string; name: string } | null {
  const m = VIEW_TITLE_RE.exec(title)
  return m ? { id: m[1], name: m[2] } : null
}

// 4503 = the relay's own transport broke; the node-side session lives on. `hadLiveSession` reaches only
// the 4502 arm, as `=== false` (see SLOT_EMPTY_REASON); `undefined` reads as `true`.
// A refused open's `detail` is printed only where it says why the OPEN failed: a throttled (429) create, and a
// 4502 that was not a reconnect to a live session. A 4503 keeps its reconnect wording with the detail beside it;
// a reconnect's 4502 keeps the session-ended wording.
function closeReason(evt: ConsoleClose, hadLiveSession?: boolean): string {
  if (evt.code === 4401) return 'not authorized'
  if (evt.throttled && evt.detail) return evt.detail
  if (evt.code === 4404) return 'slot not found'
  if (evt.code === 4403) return evt.reason || 'your network is no longer allowed'
  if (evt.code === 4408) return evt.reason || 'closed after 15 minutes of inactivity'
  if (evt.code === 4409) return evt.reason || 'limit reached'
  if (evt.code === 4502) {
    return hadLiveSession === false ? evt.detail || SLOT_EMPTY_REASON : evt.reason || 'no live session'
  }
  if (evt.code === 4503) return evt.detail ? `relay interrupted (${evt.detail})` : 'relay interrupted'
  return 'disconnected'
}

// ── Direct transport ────────────────────────────────────────────────────────────────────────────────
//
// With `direct` set the pane fetches a grant and dials the NODE (wire contract: lib/directConsole.ts).
// DIRECT MODE NEVER FALLS BACK TO THE CONTROL PLANE: a silent relay would hide whether the node is
// reachable, which is what the user asked to see. Every failure reports a reason and retries the node
// every DIRECT_RETRY_DELAY_MS for as long as the pane is mounted — no budget, no ladder. Flat, as at
// RECONNECT_IDLE_DELAY_MS; it also rides out a ~10s daemon restart.
//
// Terminal verdicts are about the SESSION, reported as on the CP path:
//   * 1000 — the user's shell exited; retrying would respawn it.
//   * 4502 on a NAMED FLEET WINDOW (`view`/`layout`) — tmux window ids are never reused.
// A 4502 on a worktree slot arms one budgeted create (armCreateOnSessionGone) instead.
//
// 5s: half the relay cadence — one handshake to one node, with a user actively waiting. Exported for tests.
export const DIRECT_RETRY_DELAY_MS = 5000

// Rate limit on minting grants while retrying. A grant is a redeemable 24h shell the node holds from
// issuance, so re-minting per attempt would be twelve live credentials a minute. Once a minute stays
// well under the CP's 30/min issuance limit while letting an expired grant or a second restart self-heal.
// The first refusal of an outage (the restart) re-mints at once. See the 4401 branch in `dialDirect`.
export const DIRECT_GRANT_MINT_COOLDOWN_MS = 60_000

// Deadline for one whole dial (grant fetch + connect + ack), disarmed only by the ack. A peer that goes
// QUIET (hung grant request, blackholed address, missing ack) fires no event and would pin the console on
// "connecting…" with no Reconnect button. Past the node's own 5s hello deadline so its verdict wins.
export const DIRECT_ATTEMPT_TIMEOUT_MS = 12_000

// Direct-path close reasons (4401 here is a grant the node refused, not a bad cookie). The node's own
// words win when it sent any (`console_direct.py::_refuse`) — except on 4401, whose collapsed auth
// classes must never be published through us. On 4502 with `hadLiveSession === false` this returns the
// SAME SLOT_EMPTY_REASON as the relay path, so flipping transports gives one account of one slot.
function directCloseReason(evt: CloseEvent, hadLiveSession?: boolean): string {
  const said = sanitizeDirectReason(evt.reason || '')
  if (evt.code === UNAUTHORIZED_CLOSE_CODE) return 'the node would not accept a fresh access grant'
  if (evt.code === 4400) return said || 'the node could not read this client’s handshake'
  if (evt.code === 4404) return said || 'the node does not recognise this console slot'
  if (evt.code === 4502) {
    return hadLiveSession === false ? SLOT_EMPTY_REASON : said || 'the node has no live session to attach to'
  }
  return said || `the direct connection closed (${evt.code})`
}

// Reported upward; the owner renders it. `notice` is separate from `label`: the label is overwritten by
// every reconnect, while the notice is the direct transport's standing condition, which tmux repaints
// away inside the terminal. Set by `retryDirect`, cleared by the ack; '' = nothing to say.
export type ConsoleStatus = {
  state: 'connecting' | 'connected' | 'closed'
  label: string
  /** Transport of the socket this status describes. Read only by the toggle's `relayed` state (the CP's
   *  `disabled` answer); a test asserting it does not prove direct mode never falls back on a FAILURE
   *  (`expectNeverRelayed()` does). */
  transport?: 'cp' | 'direct'
  /** Why direct mode was given up on, if it was. Plain text — never markup, never a token. */
  notice?: string
}

// Reaches the effect-scoped terminal/socket for the key bar and re-fit effects. Every method is inert
// before the terminal exists or after disposal.
export type ConsolePaneHandle = {
  /** Send `data` to the PTY as input frames, then return focus to the terminal. */
  sendKeys: (data: string) => void
  /** True in DECCKM, where an arrow's introducer is SS3 (ESC O) rather than CSI (ESC [). */
  isApplicationCursorKeys: () => boolean
  /** Re-fit the terminal to its container; safe to call before the container is laid out. */
  fit: () => void
  /** Re-anchor the view on the cursor. */
  scrollToBottom: () => void
  /** Move keyboard focus into this pane's terminal. */
  focus: () => void
  /**
   * Send an already-read image as paste frames — for a paste that landed on overlay chrome. False when
   * the bytes are not forwardable or there is no open socket.
   */
  pasteImage: (bytes: Uint8Array, mime: string) => boolean
  /** The MEASURED cell box in CSS px (only a real xterm knows its font metrics); `null` before layout. */
  cellSize: () => { cellW: number; cellH: number } | null
  /** The terminal's current character grid — what the AC3.5 floor is checked against. */
  gridSize: () => { cols: number; rows: number }
  /**
   * Ask the node for the pane's whole history as plain text, put on the clipboard; `onSettled` hears how the
   * clipboard write went. False (and no callback) when there is no open socket or the console is read-only.
   */
  copyPaneText: (onSettled: (state: 'copied' | 'failed') => void) => boolean
}

type Props = {
  // The project's id: every `/api/console/{project_id}/…` path carries it, never the display name.
  projectId: number
  number: number
  // (project, number) names one slot PER NODE; without this the relay could attach another node's slot. The node uuid.
  nodeId?: string
  kind: 'clauded' | 'term'
  // Fresh identity each render is fine: read through a ref, never re-dials.
  onStatusChange: (status: ConsoleStatus) => void
  // Bumping re-runs the connect effect as a FIRST attach (may create) — the manual escape.
  reconnectNonce: number
  // `@<window_id>` of the node's `fleet` session for a layout tile. Part of the connect identity.
  view?: string
  // The designed grid as the node's `layout=` spec (lib/consoleLayout `encodeLayoutSpec`): tmux draws the
  // tiles in one window on this single PTY. Mutually exclusive with `view` (node-enforced). Connect identity.
  layout?: string
  // False for non-active tiles so a routine reconnect cannot steal the caret. Read through a ref.
  autoFocus?: boolean
  // Smaller in multi-tile layouts to clear the 80x20 floor (`tileFontSize` in lib/consoleLayout).
  fontSize?: number
  // The window the PTY reports it is showing (drift signal, `view` panes only).
  onWindowChange?: (win: { id: string; name: string }) => void
  // See RECONNECT_JITTER. Zero by default.
  jitter?: number
  // Dial the node directly instead of via the CP relay. In the connect deps, so a flip tears down the old
  // socket (cleanup runs first) before the new one exists — one PTY never has two live attachments.
  direct?: boolean
  // Diagnostic override for the CP-issued address (lib/directConsole.ts). In the connect deps.
  directUrlOverride?: string
  // ONE-WAY console: no keystrokes, mouse or resize frames (anything short of `worktrees:edit`). The
  // keepalive still ticks and the handshake still carries cols/rows (clamped server-side). NOT the
  // security boundary: the CP relay drops a viewer's frames and the daemon attaches with `tmux -r`.
  // Live-changeable: the capability layer may resolve after first paint.
  readOnly?: boolean
  // What the opener's worktree row said (`lib/worktree.ts::hasLiveSession`) when this console opened.
  // Words only, for 4502 (see SLOT_EMPTY_REASON); no retry decision reads it. Absent is NOT `false` —
  // absent keeps quoting the node. Read through a ref, not a connect dep: re-dialling for a refined
  // snapshot of the past would cost for no gain.
  hadLiveSession?: boolean
  // Catalogue value of the provider whose account runs a NEW session; absent = Any. Fixed per open; a
  // connect dep so a different pick re-dials.
  provider?: string
}

/** The single-pane console's terminal size — the size this component has always rendered at. */
export const DEFAULT_FONT_SIZE = 13

const ConsolePane = forwardRef<ConsolePaneHandle, Props>(function ConsolePane(
  {
    projectId, number, nodeId, kind, onStatusChange, reconnectNonce,
    view, layout, autoFocus = true, fontSize = DEFAULT_FONT_SIZE, onWindowChange, jitter = 0,
    direct = false, directUrlOverride, readOnly = false, hadLiveSession, provider,
  },
  ref,
) {
  const mountRef = useRef<HTMLDivElement | null>(null)
  const fitRef = useRef<FitAddon | null>(null)
  // Disposed BEFORE term.dispose() — see the load site.
  const webglRef = useRef<WebglAddon | null>(null)
  // Re-read on every call, never captured: a reconnect replaces the socket.
  const wsRef = useRef<WebSocket | null>(null)
  // The one "Copy pane text" awaiting its OSC 52; the next clipboard write settles it (`clipboardWrite`).
  const pendingPaneCopyRef = useRef<{ onSettled: (state: 'copied' | 'failed') => void; timer: number } | null>(null)
  const termRef = useRef<Terminal | null>(null)
  // C-7: a direct socket is OPEN before ESTABLISHED; nothing may be written until the ack. A ref so the
  // key bar's sendKeys() (outside the effect) honours it. Always true on the CP path.
  const inputAllowedRef = useRef(true)
  const readOnlyRef = useRef(readOnly)
  /* The gate for every write carrying user intent: transport ready AND principal may write. The
     keepalive is the one exception (see its site). Stable identity (both inputs are refs). */
  const mayWrite = useCallback(() => inputAllowedRef.current && !readOnlyRef.current, [])
  // Applied to the LIVE terminal: `disableStdin` drops keyboard and mouse reports at the source.
  useEffect(() => {
    const loosened = readOnlyRef.current && !readOnly
    readOnlyRef.current = readOnly
    const term = termRef.current
    if (!term) return
    term.options.disableStdin = readOnly
    /* Re-send the size when permission widens under an open socket: the on-open resize was gated, and
       `onResize` may never fire again. `sendFrame` no-ops unless OPEN. */
    const ws = wsRef.current
    if (loosened && ws) sendResize(ws, term.cols, term.rows)
  }, [readOnly])
  // Latched so the connect effect need not depend on them; `autoFocus`/`onWindowChange` change with the
  // active tile, and re-dialling on every tile click would tear down the terminal just left.
  const onStatusChangeRef = useRef(onStatusChange)
  const autoFocusRef = useRef(autoFocus)
  const onWindowChangeRef = useRef(onWindowChange)
  // Read only at close time, so late reads lose nothing; seeded by useRef so the first close is right.
  const hadLiveSessionRef = useRef(hadLiveSession)
  // Retired once this pane has HELD a session (stable-hold timer): the opener's snapshot no longer
  // describes the slot and a later 4502 is the ordinary catch-all. Cleared to `undefined`, not `true`.
  // One-way flag so the every-render layout effect cannot re-seed the stale value. Scoped to the pane,
  // not the slot: a chevron switch after a hold quotes the node (the safe direction).
  const sessionHeldRef = useRef(false)
  useLayoutEffect(() => {
    onStatusChangeRef.current = onStatusChange
    autoFocusRef.current = autoFocus
    onWindowChangeRef.current = onWindowChange
    if (!sessionHeldRef.current) hadLiveSessionRef.current = hadLiveSession
  })

  useImperativeHandle(ref, (): ConsolePaneHandle => ({
    sendKeys: (data: string) => {
      const ws = wsRef.current
      const term = termRef.current
      if (!ws || !term || !mayWrite()) return
      sendInput(ws, encoder, data)
      term.focus()
    },
    isApplicationCursorKeys: () => termRef.current?.modes.applicationCursorKeysMode ?? false,
    fit: () => {
      try {
        fitRef.current?.fit()
      } catch {
        /* container not laid out yet — the caller's next resize event retries */
      }
    },
    scrollToBottom: () => termRef.current?.scrollToBottom(),
    focus: () => termRef.current?.focus(),
    pasteImage: (bytes: Uint8Array, mime: string) => {
      const ws = wsRef.current
      if (!ws || !mayWrite()) return false
      const frames = buildPasteFrames(bytes, mime)
      if (!frames) return false
      for (const frame of frames) sendFrame(ws, frame.type, frame.payload)
      return true
    },
    // `.xterm-screen` is sized to exactly cols x rows cells; an unlaid screen reports null.
    cellSize: () => {
      const term = termRef.current
      const screen = term?.element?.querySelector('.xterm-screen') as HTMLElement | null | undefined
      if (!term || !screen) return null
      const box = screen.getBoundingClientRect()
      if (!(box.width > 0) || !(box.height > 0) || !(term.cols > 0) || !(term.rows > 0)) return null
      return { cellW: box.width / term.cols, cellH: box.height / term.rows }
    },
    gridSize: () => ({ cols: termRef.current?.cols ?? 0, rows: termRef.current?.rows ?? 0 }),
    copyPaneText: (onSettled: (state: 'copied' | 'failed') => void) => {
      const ws = wsRef.current
      if (!ws || ws.readyState !== WebSocket.OPEN || !mayWrite()) return false
      const previous = pendingPaneCopyRef.current
      if (previous) window.clearTimeout(previous.timer)
      const timer = window.setTimeout(() => {
        if (pendingPaneCopyRef.current?.timer !== timer) return
        pendingPaneCopyRef.current = null
        onSettled('failed')
      }, COPY_PANE_TIMEOUT_MS)
      pendingPaneCopyRef.current = { onSettled, timer }
      sendFrame(ws, FRAME_TYPE_COPY_PANE, new Uint8Array(0))
      return true
    },
  }), [mayWrite])

  useEffect(() => {
    const container = mountRef.current
    if (!container) return

    const term = new Terminal({
      convertEol: false,
      cursorBlink: true,
      fontSize,
      // From the ref so capability resolution never re-dials; the effect above applies later changes.
      disableStdin: readOnlyRef.current,
      fontFamily: 'ui-monospace, SFMono-Regular, Menlo, Consolas, monospace',
      theme: { ...XTERM_THEME },
    })
    const fit = new FitAddon()
    fitRef.current = fit
    term.loadAddon(fit)
    // Every console copy funnels here (OSC 52 and copy-on-select). Safari rejects writes outside a user
    // gesture and non-HTTPS has no navigator.clipboard, so the text is also KEPT for the `copy` listener
    // below (⌘C / Edit→Copy). The hint shows once.
    let lastConsoleCopy = ''
    let copyHintShown = false
    const clipboardWrite = (text: string) => {
      lastConsoleCopy = text
      const paneCopy = pendingPaneCopyRef.current
      if (paneCopy) {
        window.clearTimeout(paneCopy.timer)
        pendingPaneCopyRef.current = null
      }
      const showHint = () => {
        if (copyHintShown) return
        copyHintShown = true
        term.write('\r\n\x1b[90m[text captured — this browser needs ⌘C / Edit→Copy to finish the copy]\x1b[0m\r\n')
      }
      copyText(() => text, state => {
        if (state === 'failed') showHint()
        paneCopy?.onSettled(state)
      })
    }
    // OSC 52 → clipboard. Under tmux with mouse on, a plain drag is a tmux copy-mode copy announced as
    // OSC 52 (the host's tmux config sets `set-clipboard on`); stock xterm.js discards it.
    term.loadAddon(new ClipboardAddon(undefined, makeClipboardProvider(clipboardWrite)))
    term.open(container)

    // WebGL renderer: the DOM renderer's per-cell span mutation dominates TUI repaints. Loaded after
    // term.open() (needs a canvas); falls back to DOM on no context or onContextLoss. Probe first: a
    // failed activate leaves the addon registered and its dispose() throws from term.dispose(), escaping
    // cleanup as a render error (deterministic in headless Chromium). Disposed defensively before term.
    if (supportsWebgl2()) {
      try {
        const webgl = new WebglAddon()
        webgl.onContextLoss(() => disposeQuietly(webgl))
        term.loadAddon(webgl)
        webglRef.current = webgl
      } catch {
        /* no WebGL here — xterm stays on its DOM renderer */
        webglRef.current = null
      }
    }

    try {
      // A narrow viewport can hand FitAddon unresolved dimensions (throws at 320px); uncaught it aborts
      // the WebSocket connect below.
      fit.fit()
    } catch {
      /* container not laid out yet — the owner's resize/manual-resize effects retry fit() */
    }

    termRef.current = term
    // Lines the control plane typed into the pane carry its label; badge them apart from the operator's own.
    const cpBadges = attachControlPlaneBadges(term)

    // `ws` is replaced by each reconnect, so handlers read it live. `torndown` is set by cleanup before
    // closing so a switched-away socket's close is never acted on; `socket !== ws` is belt-and-braces.
    let ws: WebSocket | null = null
    let torndown = false
    let attempt = 0
    // Announce-once latch for the session-gone cadence; cleared by a connection that HOLDS. Gates nothing else.
    let sessionGoneAnnounced = false
    // One-shot create permission, armed by a 4502 and spent by the next dial. Latching it would make every
    // cadence dial create-capable, including 4503s from `_post_create_not_found` (agent already launched),
    // relaunching every 10s against quota.
    let mayCreateNextDial = false
    // Total ceiling per outage (one-shot bounds only the rate); reset by a connection that holds.
    let createDialsSpent = 0
    let budgetAnnounced = false
    let retryTimer: number | null = null
    // Wall-clock deadline of the pending retry: a suspended page freezes timers but not Date.now(), so
    // this detects an overdue retry. Read only by retryNowIfOwed.
    let retryDueAt = 0
    let stableTimer: number | null = null
    let keepalive: number | null = null

    // ── Direct-transport state, all scoped to this effect ──────────────────────────────────────────
    // Fixed for the effect's life except in ONE direction: a CP `disabled` answer (direct console is not
    // configured for this node) moves it to 'cp' once, and it stays there. Never on a failure.
    let transport: 'cp' | 'direct' = direct ? 'direct' : 'cp'
    // Held across reconnects: issuance is rate-limited and each mint is an untracked live credential.
    let grant: DirectGrant | null = null
    // When the grant was minted (wall clock); 0 = never. Read only by the 4401 branch.
    let lastGrantMintAt = 0
    // Re-fetch-once latch: a daemon restart drops in-memory grants and closes 4401, same as a bad token.
    // Boolean, not a counter, to prevent a CP↔node loop.
    let grantRefetched = false
    // Whether this pane ever dialled; NEVER cleared. Only the first dial of the effect (the user's own
    // act) or an armed one may create. A resettable counter let every post-drop dial create, which on a
    // pocketed phone meant an agent launched per idle-sweep cycle all night. Recovery still works: the
    // attach-only dial gets 4502, which arms one gated create.
    let dialedOnce = false
    // The in-flight attempt's deadline; effect-scoped because it covers the grant fetch too.
    let dialTimer: number | null = null
    // The in-flight socket's `abandon`, so the deadline closes it rather than orphaning it.
    let abortDial: ((reason: string) => void) | null = null
    // Monotonic attempt id, bumped by every dial and abandonment. Guards the grant fetch's `await`: a
    // deadline-abandoned fetch that answers late must not dial a second socket beside the armed retry.
    let dialEpoch = 0
    // Announce-once for a direct outage; cleared by a connection that HOLDS, not by an ack.
    let directRetryAnnounced = false
    // See ConsoleStatus.notice; cleared by the ack so it never outlives the retry.
    let notice = ''
    inputAllowedRef.current = !direct

    // Only for truly terminal paths (non-retryable close, teardown). Not the attempt cap: the flat cadence
    // will replace the socket and needs the keepalive armed.
    const stopKeepalive = () => {
      if (keepalive === null) return
      window.clearInterval(keepalive)
      keepalive = null
    }

    // Arm the pending reconnect and CLEAR THE HANDLE AS IT FIRES: retryNowIfOwed reads
    // `retryTimer !== null` as "owed", so a spent handle would dial a second socket.
    const armRetry = (dial: () => void, delay: number) => {
      // One pending retry per pane; overwriting a live handle would leak a timer that dials.
      if (retryTimer !== null) window.clearTimeout(retryTimer)
      retryDueAt = Date.now() + delay
      retryTimer = window.setTimeout(() => {
        retryTimer = null
        dial()
      }, delay)
    }

    const scheduleRetry = (reason: string) => {
      attempt += 1
      if (attempt > RECONNECT_MAX_ATTEMPTS) {
        // Past the ladder: flat cadence (see RECONNECT_IDLE_DELAY_MS). `closed` keeps the Reconnect button
        // as "try now". Announce once. Keepalive stays armed: it reads the CURRENT socket and no-ops on a
        // closed one; stopping it would leave a reconnected pane to be reaped ~60s later.
        const every = RECONNECT_IDLE_DELAY_MS / 1000
        onStatusChangeRef.current({
          state: 'closed',
          label: `${reason} — retrying every ${every}s`,
          transport,
          notice,
        })
        if (attempt === RECONNECT_MAX_ATTEMPTS + 1) {
          term.write(`\r\n\x1b[90m[${reason} — still retrying, every ${every}s]\x1b[0m\r\n`)
        }
        armRetry(connect, RECONNECT_IDLE_DELAY_MS)
        return
      }
      // Announced once per outage; the re-attach redraw wipes it anyway.
      if (attempt === 1) term.write(`\r\n\x1b[90m[${reason} — reconnecting…]\x1b[0m\r\n`)
      onStatusChangeRef.current({ state: 'connecting', label: 'reconnecting…', transport, notice })
      armRetry(connect, reconnectDelayMs(attempt, jitter))
    }

    // May the next dial create? Shared by both transports — one place decides when a tab spends quota.
    //   'not-allowed' — read-only viewer or hidden page (a pocketed phone must not create overnight).
    //                   Both still re-attach on the cadence.
    //   'armed'       — one create-capable dial permitted.
    //   'exhausted'   — per-outage budget spent; attach-only from here.
    const armCreateOnSessionGone = (): 'armed' | 'not-allowed' | 'exhausted' => {
      if (readOnlyRef.current || document.visibilityState !== 'visible') {
        mayCreateNextDial = false
        return 'not-allowed'
      }
      // Cleared on every non-armed answer so the post-condition is uniform for all callers.
      if (createDialsSpent >= SESSION_GONE_CREATE_BUDGET) {
        mayCreateNextDial = false
        return 'exhausted'
      }
      mayCreateNextDial = true
      return 'armed'
    }

    // 4502 on a reconnect is the NORMAL end of a node outage (node back without the old tmux window;
    // every reconnect is attach_only), so keep dialling every 10s with budgeted create permission.
    // Creating spends quota and claims a slot — accepted because it is budgeted (SESSION_GONE_CREATE_BUDGET,
    // armed per 4502 answer; 4503 from `_post_create_not_found` means creation can half-succeed
    // expensively), the alternative is a dead console the user is trying to reach, and 1000 stays terminal.
    // Read-only panes take the cadence too (a viewer is the common case) but never create.
    const scheduleSessionGoneRetry = (reason: string) => {
      const every = RECONNECT_IDLE_DELAY_MS / 1000
      if (!sessionGoneAnnounced) {
        term.write(`\r\n\x1b[90m[${reason} — reconnecting, every ${every}s]\x1b[0m\r\n`)
      }
      sessionGoneAnnounced = true
      // Arm one create; announce budget exhaustion once. `readOnlyRef`, not `mayWrite()`: the question is
      // the tier (the CP/node enforce it; this keeps a viewer from wasting the budget). Visibility gate
      // bounds the lifetime cost — the budget resets per held connection — and retryNowIfOwed lands the
      // create when the user returns.
      if (armCreateOnSessionGone() === 'exhausted' && !budgetAnnounced) {
        budgetAnnounced = true
        term.write(
          '\r\n\x1b[90m[still no session after '
          + `${SESSION_GONE_CREATE_BUDGET} attempts to start one — still reconnecting every ${every}s, `
          + 'but use Reconnect to start a new session]\x1b[0m\r\n',
        )
      }
      onStatusChangeRef.current({
        state: 'closed',
        label: `${reason} — retrying every ${every}s`,
        transport,
        notice,
      })
      // Keepalive left armed (see scheduleRetry).
      armRetry(connect, RECONNECT_IDLE_DELAY_MS)
    }

    // Revoke rather than leave live for its TTL (revokeDirectGrant). Retries do NOT release — the grant
    // is held across reconnects; only a create-capable dial and teardown release.
    const releaseGrant = () => {
      if (!grant) return
      revokeDirectGrant(projectId, number, kind, grant.token, nodeId)
      grant = null
    }

    // THE answer to a broken direct connection: say so and redial the node in 5s, forever (see Direct
    // transport). Input is re-gated until the next ack (C-7). `closed` keeps Reconnect as "try now".
    const retryDirect = (reason: string) => {
      if (torndown) return
      if (dialTimer !== null) {
        window.clearTimeout(dialTimer)
        dialTimer = null
      }
      abortDial = null
      // Makes the abandoned attempt stale (see dialEpoch).
      dialEpoch += 1
      inputAllowedRef.current = false
      const detail = sanitizeDirectReason(reason)
      const every = DIRECT_RETRY_DELAY_MS / 1000
      // Names the remedy, since the relay is the user's choice. NO EMOJI: the same string goes to xterm,
      // which renders ⚡ as tofu. An empty slot (4502 = the node answered) gets a different sentence:
      // "connection failed" and "turn direct off" would both mislead, since the relay finds the same slot.
      notice = detail === SLOT_EMPTY_REASON
        ? `Reached the node — ${detail}.`
          + ` Still dialling every ${every}s; the console attaches by itself if a session starts here.`
        : `Direct connection to the node failed${detail ? ` — ${detail}` : ''}.`
          + ` Retrying the node every ${every}s; this console never falls back to the relay.`
          + ' Turn direct mode off in the header to use the relay instead.'
      if (!directRetryAnnounced) {
        directRetryAnnounced = true
        term.write(`\r\n\x1b[90m[${notice}]\x1b[0m\r\n`)
      }
      onStatusChangeRef.current({
        state: 'closed',
        label: `${detail || 'disconnected'} — retrying every ${every}s`,
        transport,
        notice,
      })
      armRetry(() => void dialDirect(), DIRECT_RETRY_DELAY_MS)
    }

    // The CP said direct console is OFF for this node (not failing — not configured). Re-dialling a
    // capability that is off would strand the console forever, so this effect moves to the relay for good
    // and says so once. No node is ever dialled, and no retry cadence is armed.
    // `mayOpen`/`mayCreate` are the abandoned direct dial's own permissions, carried over so the switch
    // cannot mint a create the `dialedOnce` latch would refuse: a `disabled` answering a LATER dial (after
    // a 4401 re-fetch, say a node restarting with its suffix unset) must reach the relay attach-only.
    const useRelayInstead = (reason: string, mayOpen: boolean, mayCreate: boolean) => {
      if (torndown) return
      if (dialTimer !== null) {
        window.clearTimeout(dialTimer)
        dialTimer = null
      }
      abortDial = null
      dialEpoch += 1
      transport = 'cp'
      // Not the first dial: attach-only, and a 4502 enters the session-gone cadence (both read `attempt`).
      if (!mayOpen && attempt === 0) attempt = 1
      // An armed create was cleared before the grant await but never charged — hand it to the relay dial.
      if (mayCreate) mayCreateNextDial = true
      inputAllowedRef.current = true
      const detail = sanitizeDirectReason(reason)
      // NO EMOJI: the same string goes to xterm (see `retryDirect`).
      notice = `Direct connection unavailable${detail ? ` (${detail})` : ''}. Using the relay.`
      term.write(`\r\n\x1b[90m[${notice}]\x1b[0m\r\n`)
      void dialCp()
    }

    const connect = () => {
      if (transport === 'direct') {
        void dialDirect()
        return
      }
      void dialCp()
    }

    // THE decision for a relay-path close — the socket's, or a refused open's (`openConsole`), which
    // carries the code the socket would have closed with.
    const onCpClosed = (closed: ConsoleClose) => {
      if (stableTimer !== null) {
        window.clearTimeout(stableTimer)
        stableTimer = null
      }
      const reason = closeReason(closed, hadLiveSessionRef.current)
      // Before the denylist, since 4502 is in it. Only on a RECONNECT (`attempt > 0`): a first-attach 4502
      // is a refused create, and retrying won't change it. Read-only panes included (cadence, no create).
      // Named fleet windows (`layout`/`view`) excluded: `_open_layout_pty`/`_open_view_pty` never create
      // and window ids are not reused, so the cadence would rebuild a grid every 10s forever.
      if (closed.code === SESSION_GONE_CLOSE_CODE && attempt > 0 && view === undefined && layout === undefined) {
        scheduleSessionGoneRetry(reason)
        return
      }
      if (NON_RETRYABLE_CLOSE_CODES.has(closed.code)) {
        onStatusChangeRef.current({ state: 'closed', label: reason, transport, notice })
        term.write(`\r\n\x1b[90m[${reason}]\x1b[0m\r\n`)
        stopKeepalive()   // nothing is coming back on this socket
        return
      }
      scheduleRetry(reason)
    }

    // open → socket. Async only for the open; callers fire it with `void`.
    const dialCp = async () => {
      dialEpoch += 1
      const epoch = dialEpoch
      // Reconnects are attach_only unless a 4502 armed a create (scheduleSessionGoneRetry). The one-shot
      // is spent by the dial that actually goes out — the open is that dial — and cleared unconditionally.
      const mayCreate = mayCreateNextDial && createDialsSpent < SESSION_GONE_CREATE_BUDGET
      mayCreateNextDial = false
      if (mayCreate) createDialsSpent += 1
      const opened = await openConsole(
        consoleOpenUrl(projectId, number, nodeId, kind, term.cols, term.rows, attempt > 0 && !mayCreate, view, layout, provider),
      )
      // Overtaken by teardown or a newer dial (`epoch`) — acting would open a second socket.
      if (torndown || epoch !== dialEpoch) return
      if ('closed' in opened) {
        onCpClosed(opened.closed)
        return
      }
      let socket: WebSocket
      try {
        const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
        socket = new WebSocket(`${proto}//${window.location.host}${opened.url}`)
      } catch (err) {
        // A throwing constructor (rejected URL, blocked port) would otherwise escape the timer and stall.
        term.write(`\r\n\x1b[90m[reconnect failed: ${String(err)}]\x1b[0m\r\n`)
        scheduleRetry('connection error')
        return
      }
      socket.binaryType = 'arraybuffer'
      ws = socket
      wsRef.current = socket

      socket.onopen = () => {
        onStatusChangeRef.current({ state: 'connected', label: 'connected', transport, notice })
        // Only the active pane focuses, so a reconnect elsewhere cannot steal the caret.
        if (autoFocusRef.current) term.focus()
        // Streak forgiven only once the connection HOLDS (RECONNECT_STABLE_AFTER_MS).
        if (stableTimer !== null) window.clearTimeout(stableTimer)
        stableTimer = window.setTimeout(() => {
          stableTimer = null
          attempt = 0
          // A held connection ends the outage: reset the latch, budget and opener snapshot (see
          // `sessionHeldRef`). Resetting on open would trust a session that has not proved it stays.
          sessionGoneAnnounced = false
          createDialsSpent = 0
          budgetAnnounced = false
          hadLiveSessionRef.current = undefined
          sessionHeldRef.current = true
        }, RECONNECT_STABLE_AFTER_MS)
        // Re-send the size: a resize while CONNECTING was dropped by sendFrame. Gated: a viewer must not
        // resize a SHARED session (tmux sizes to its smallest client).
        if (mayWrite()) sendResize(socket, term.cols, term.rows)
      }
      socket.onmessage = (evt: MessageEvent) => {
        if (typeof evt.data === 'string') {
          term.write(evt.data)
        } else {
          term.write(new Uint8Array(evt.data as ArrayBuffer))
        }
      }
      // No onerror: close always follows error, so onclose is the single decider.
      socket.onclose = (evt: CloseEvent) => {
        if (torndown || socket !== ws) return
        onCpClosed(evt)
      }
    }

    // grant → socket → hello → ack. Async only for the grant fetch; callers fire it with `void`.
    const dialDirect = async () => {
      dialEpoch += 1
      const epoch = dialEpoch

      // One-shot create permission, spent here as in dialCp.
      const mayCreate = mayCreateNextDial && createDialsSpent < SESSION_GONE_CREATE_BUDGET
      mayCreateNextDial = false
      // `mayOpen`: first dial of the effect or an armed one; latched before the dial so a failed first
      // attempt still counts.
      const mayOpen = mayCreate || !dialedOnce
      dialedOnce = true
      // The budget is charged below the await, by the dial that actually goes out.

      // Whole-attempt deadline, armed before any step (DIRECT_ATTEMPT_TIMEOUT_MS).
      if (dialTimer !== null) window.clearTimeout(dialTimer)
      abortDial = null
      dialTimer = window.setTimeout(() => {
        dialTimer = null
        const reason = 'the node did not answer in time'
        // Close the stalled socket if one exists; otherwise straight to the retry cadence.
        if (abortDial) abortDial(reason)
        else retryDirect(reason)
      }, DIRECT_ATTEMPT_TIMEOUT_MS)

      // A create-capable dial must carry a FRESH account choice: the grant snapshots the CP's credential
      // account pick and lives up to 24h, so reusing it would bypass the relay's per-open quota pick.
      if (mayOpen) releaseGrant()
      if (!grant) {
        const result = await fetchDirectGrant(projectId, number, kind, nodeId, provider)
        // Overtaken by teardown or this attempt's own deadline (`epoch`) — acting would orphan a socket.
        if (torndown || epoch !== dialEpoch) {
          // Never stored, so cleanup cannot revoke it — hand it back here.
          if (result.ok) revokeDirectGrant(projectId, number, kind, result.grant.token, nodeId)
          return
        }
        if (!result.ok && result.disabled) {
          useRelayInstead(result.reason, mayOpen, mayCreate)
          return
        }
        if (!result.ok) {
          retryDirect(result.reason)
          return
        }
        grant = result.grant
        lastGrantMintAt = Date.now()
      }

      if (mayCreate) createDialsSpent += 1
      let socket: WebSocket
      try {
        // `directDialUrl` only ever yields the machine the CP named; `isGrantedHost` gates the token too.
        // The token rides the subprotocol list, never the URL. See lib/directConsole.ts.
        const dial = directDialUrl(grant.directUrl, directUrlOverride)
        const protocols = isGrantedHost(grant.directUrl, dial)
          ? directSubprotocols(grant.token)
          : [...DIRECT_SUBPROTOCOLS]
        socket = new WebSocket(dial, protocols)
      } catch {
        // Error text deliberately unreported: some engines quote the subprotocol, which holds the token.
        retryDirect('the direct address could not be dialled')
        return
      }
      socket.binaryType = 'arraybuffer'
      ws = socket
      wsRef.current = socket
      inputAllowedRef.current = false
      // Per-socket: the FIRST terminal decision wins, so onmessage's failure is not re-handled by onclose.
      let handled = false
      let acked = false

      // Handlers are detached BEFORE close(): some engines fire onclose synchronously.
      const abandon = (reason: string) => {
        if (handled || torndown) return
        handled = true
        socket.onmessage = null
        socket.onclose = null
        if (stableTimer !== null) {
          window.clearTimeout(stableTimer)
          stableTimer = null
        }
        try {
          socket.close()
        } catch {
          /* already closing — the retry scheduled below is what matters */
        }
        retryDirect(reason)
      }
      abortDial = abandon

      socket.onopen = () => {
        // C-7: the TEXT hello is the first thing on the wire.
        try {
          /* attach_only on a retry unless a 4502 armed a create (`mayCreate`); ALWAYS for a read-only pane
             — on this transport the browser composes the flag (the node and grant enforce it). `view` rides
             the hello here (the CP path uses a query parameter). */
          socket.send(directHelloFrame(
            term.cols, term.rows, !mayOpen || readOnlyRef.current, view, layout,
          ))
        } catch {
          abandon('the direct connection closed before the handshake could be sent')
        }
      }

      socket.onmessage = (evt: MessageEvent) => {
        // Queued messages run after cleanup; an unacked frame here would arm a retry on a disposed terminal.
        if (torndown || socket !== ws || handled) return
        if (!acked) {
          // Before the ack, only the ack is legal; anything else is not our protocol.
          if (!isDirectAck(evt.data)) {
            abandon('the node did not acknowledge the direct connection')
            return
          }
          acked = true
          inputAllowedRef.current = true
          // Outage over: clear the notice on the ack (it describes right now).
          notice = ''
          // The only thing that disarms the deadline.
          if (dialTimer !== null) {
            window.clearTimeout(dialTimer)
            dialTimer = null
          }
          abortDial = null
          onStatusChangeRef.current({ state: 'connected', label: 'connected', transport, notice })
          // Gated as on the CP path.
          if (autoFocusRef.current) term.focus()
          // Forgive streaks only once the connection HOLDS. `dialedOnce` is never forgiven (see its
          // declaration). `grantRefetched` is: it stops a loop within one episode, not a long-lived tab.
          if (stableTimer !== null) window.clearTimeout(stableTimer)
          stableTimer = window.setTimeout(() => {
            stableTimer = null
            attempt = 0
            grantRefetched = false
            directRetryAnnounced = false
            createDialsSpent = 0
            budgetAnnounced = false
            // Retired on both transports so flipping direct mode gives one account (see `sessionHeldRef`).
            hadLiveSessionRef.current = undefined
            sessionHeldRef.current = true
          }, RECONNECT_STABLE_AFTER_MS)
          // Re-send the size; same self-heal and gate as the CP path.
          if (mayWrite()) sendResize(socket, term.cols, term.rows)
          return
        }
        if (typeof evt.data === 'string') {
          term.write(evt.data)
        } else {
          term.write(new Uint8Array(evt.data as ArrayBuffer))
        }
      }

      socket.onclose = (evt: CloseEvent) => {
        if (torndown || socket !== ws || handled) return
        handled = true
        if (stableTimer !== null) {
          window.clearTimeout(stableTimer)
          stableTimer = null
        }
        // A close ends the attempt, so disarm the deadline here; the next dial arms its own.
        if (dialTimer !== null) {
          window.clearTimeout(dialTimer)
          dialTimer = null
        }
        abortDial = null
        inputAllowedRef.current = false

        // ── 4401, AND WHY IT IS THE ONE CLOSE CODE THIS PANE RATIONS ─────────────────────────────────
        // The node collapses restart, invalid, wrong-slot and expired into 4401 (`console_direct.py::redeem`).
        // First refusal of an outage: re-mint at once (the restart). After that: at most one mint per
        // DIRECT_GRANT_MINT_COOLDOWN_MS, else re-present the held grant. Abandoned grants are revoked via
        // `releaseGrant`: the node admits a grant at issuance and keeps refused ones live, `prune()` only
        // evicts expired entries, and a full `_MAX_GRANTS` store blocks direct console for everyone.
        if (evt.code === UNAUTHORIZED_CLOSE_CODE) {
          const firstRefusal = !grantRefetched
          if (firstRefusal || Date.now() - lastGrantMintAt >= DIRECT_GRANT_MINT_COOLDOWN_MS) {
            releaseGrant()
          }
          if (firstRefusal) {
            grantRefetched = true
            // Same attempt, fresh credential; `dialedOnce` is a latch, so no create permission is re-earned.
            void dialDirect()
            return
          }
        }

        // A session verdict: retrying would respawn an exited shell.
        if (evt.code === SESSION_ENDED_CLOSE_CODE) {
          const reason = closeReason(evt, hadLiveSessionRef.current)
          onStatusChangeRef.current({ state: 'closed', label: reason, transport, notice })
          term.write(`\r\n\x1b[90m[${reason}]\x1b[0m\r\n`)
          stopKeepalive()
          return
        }

        // 4502 on a worktree slot: arm one budgeted, tier- and visibility-gated create. Named fleet windows
        // are excluded (terminal, below). Only when this dial's hello did NOT ask to open (`mayOpen`, not
        // `mayCreate` — the first dial also asks): a 4502 to an open request is the node refusing to create.
        if (evt.code === SESSION_GONE_CLOSE_CODE && !mayOpen && view === undefined && layout === undefined) {
          const budget = armCreateOnSessionGone()
          if (budget === 'exhausted' && !budgetAnnounced) {
            budgetAnnounced = true
            term.write(
              '\r\n\x1b[90m[still no session after '
              + `${SESSION_GONE_CREATE_BUDGET} attempts to start one — still reconnecting to the node, `
              + 'but use Reconnect to start a new session]\x1b[0m\r\n',
            )
          }
          retryDirect(directCloseReason(evt, hadLiveSessionRef.current))
          return
        }
        // Only a named fleet window's 4502 is terminal; a slot's falls through to the cadence.
        if (evt.code === SESSION_GONE_CLOSE_CODE && (view !== undefined || layout !== undefined)) {
          const reason = directCloseReason(evt, hadLiveSessionRef.current)
          onStatusChangeRef.current({ state: 'closed', label: reason, transport, notice })
          term.write(`\r\n\x1b[90m[${reason}]\x1b[0m\r\n`)
          stopKeepalive()
          return
        }

        // Everything else retries on the flat cadence (4400, repeat 4401, 4404, 4503, 1006). Accepted cost:
        // a `layout` pane stuck on 4400/4404 dials every 5s for the overlay's life — refused at the
        // handshake, so cheap. Narrow it only on evidence of the cost.
        retryDirect(directCloseReason(evt, hadLiveSessionRef.current))
      }
    }
    // Each effect run starts fresh: otherwise a chevron switch shows the outgoing session's close and
    // offers a Reconnect (a create-capable first attach) over a healthy connect.
    onStatusChangeRef.current({ state: 'connecting', label: 'connecting…', transport, notice })
    connect()

    // ── Coming back from a suspended tab ───────────────────────────────────────────────────────────
    // Mobile browsers freeze a hidden page's timers, so an armed retry can sit minutes overdue.
    // `visibilitychange`/`online` fire it NOW, changing only WHEN, never `attempt` or create permission.
    // `retryTimer !== null` means a reconnect is owed, which makes repeated events self-limiting.
    const retryNowIfOwed = () => {
      if (torndown || retryTimer === null) return
      if (document.visibilityState === 'hidden') return
      // OVERDUE, not merely pending: firing any pending retry would collapse the backoff and the
      // create-cadence rate limit (flaky cellular bursts `online`). A frozen page's deadline has passed.
      if (Date.now() < retryDueAt) return
      window.clearTimeout(retryTimer)
      retryTimer = null
      connect()
    }
    document.addEventListener('visibilitychange', retryNowIfOwed)
    window.addEventListener('online', retryNowIfOwed)

    /* `inputAllowedRef`, NOT `mayWrite()`: the keepalive deliberately ignores the tier — a read-only
       viewer is the likeliest to sit on a silent socket. The relay drops it for a viewer and does not
       refresh `last_activity`. The transport half still applies (no byte before a direct ack). */
    keepalive = window.setInterval(() => {
      if (ws && inputAllowedRef.current) sendFrame(ws, FRAME_TYPE_INPUT, new Uint8Array(0))
    }, KEEPALIVE_INTERVAL_MS)

    // Keystrokes → PTY stdin (sendFrame no-ops unless OPEN).
    const dataDisposable = term.onData((data: string) => {
      if (ws && mayWrite()) sendInput(ws, encoder, data)
    })
    // Live resizes after attach (the initial size rode the handshake).
    const resizeDisposable = term.onResize(({ cols, rows }: { cols: number; rows: number }) => {
      if (ws && mayWrite()) sendResize(ws, cols, rows)
    })

    // Drift channel: things outside the app move a tile's window pointer (killed window, rename,
    // switch-client). The per-view session sets `set-titles-string '#{window_id} #{window_name}'`
    // (apps/jaahd/console.py `_create_view_session`), surfaced as `onTitleChange` — push, no poll.
    // Titles that are not ours are ignored.
    const titleDisposable = term.onTitleChange((title: string) => {
      const win = parseViewTitle(title)
      if (win) onWindowChangeRef.current?.(win)
    })

    // Copy-on-select (Ctrl+C is interrupt, Ctrl+Shift+C devtools), debounced to the settled selection.
    // Best-effort; an empty selection never blanks the clipboard.
    const COPY_ON_SELECT_SETTLE_MS = 150
    let copyTimer: number | null = null
    const selectionDisposable = term.onSelectionChange(() => {
      if (copyTimer !== null) window.clearTimeout(copyTimer)
      copyTimer = window.setTimeout(() => {
        copyTimer = null
        const text = term.getSelection()
        if (text) clipboardWrite(text)
      }, COPY_ON_SELECT_SETTLE_MS)
    })

    // ⌘C / Edit→Copy with no live xterm selection (a tmux drag-copy cancels copy-mode): serve the kept
    // text via clipboardData, which needs no permission. A live selection wins (xterm's own handler).
    const onCopyCommand = (e: ClipboardEvent) => {
      // Only copies aimed at the terminal; header text keeps its own meaning.
      if (!(e.target instanceof Node) || !term.element?.contains(e.target)) return
      if (term.hasSelection() || !lastConsoleCopy || !e.clipboardData) return
      e.clipboardData.setData('text/plain', lastConsoleCopy)
      e.preventDefault()
    }
    container.addEventListener('copy', onCopyCommand, { capture: true })

    // No window `resize` fit here: the owner's panel ResizeObserver re-fits on every box change, debounced. An
    // undebounced one resized the PTY at every step of a window drag and left the TUI half-repainted.

    // Touch-drag → scroll. xterm's touch scrolling is off while mouse reporting is active (always, under
    // tmux), so the drag is replayed as a `wheel` on the terminal — xterm encodes it for the app or
    // scrolls local scrollback. Capture phase on the container consumes the drag before xterm;
    // preventDefault suppresses compat mouse events. Below the threshold a tap passes through.
    const DRAG_THRESHOLD_PX = 8
    let touchStartY: number | null = null
    let lastY = 0
    let dragging = false

    const onTouchStart = (e: TouchEvent) => {
      if (e.touches.length !== 1) return
      touchStartY = e.touches[0].clientY
      lastY = touchStartY
      dragging = false
    }
    const onTouchMove = (e: TouchEvent) => {
      if (touchStartY === null || e.touches.length !== 1) return
      const y = e.touches[0].clientY
      if (!dragging && Math.abs(y - touchStartY) < DRAG_THRESHOLD_PX) return
      dragging = true
      e.preventDefault()
      e.stopPropagation()
      const deltaY = lastY - y
      lastY = y
      if (deltaY === 0) return
      // clientX/clientY decide which tmux pane scrolls.
      term.element?.dispatchEvent(
        new WheelEvent('wheel', {
          deltaY,
          deltaMode: 0,
          clientX: e.touches[0].clientX,
          clientY: y,
          bubbles: true,
          cancelable: true,
        }),
      )
    }
    const onTouchEnd = (e: TouchEvent) => {
      if (dragging) e.stopPropagation()
      touchStartY = null
      dragging = false
    }
    // Non-passive: preventDefault on touchmove is what stops the page behind from scrolling.
    container.addEventListener('touchstart', onTouchStart, { capture: true, passive: true })
    container.addEventListener('touchmove', onTouchMove, { capture: true, passive: false })
    container.addEventListener('touchend', onTouchEnd, { capture: true, passive: true })

    // Hand the paste chord back to the browser off-Apple (see `isPasteChord`); returning false stops
    // xterm before it writes SYN or calls preventDefault.
    const apple = isApplePlatform(navigator.platform)
    term.attachCustomKeyEventHandler(event => !isPasteChord(event, apple))

    // Screenshot paste → host file → path in the prompt (buildPasteFrames). Capture phase so an image
    // paste is claimed before xterm's textarea inserts the text fallback; text pastes pass untouched.
    const onPaste = (e: ClipboardEvent) => {
      const [file] = clipboardFiles(e.clipboardData)
      if (!file) return
      e.preventDefault()
      e.stopPropagation()
      // `ws` read inside the callback: a reconnect may have replaced it.
      void file
        .arrayBuffer()
        .then(buffer => {
          const frames = buildPasteFrames(new Uint8Array(buffer), file.type)
          if (!frames) {
            term.write(`\r\n\x1b[90m[image paste ignored — png/jpg/gif/webp under ${PASTE_MAX_BYTES / (1024 * 1024)} MB only]\x1b[0m\r\n`)
            return
          }
          // Gate re-read: the handshake or the tier may have changed while the file was read.
          if (!ws || !mayWrite()) return
          for (const frame of frames) sendFrame(ws, frame.type, frame.payload)
        })
        .catch(() => {
          /* the clipboard's bytes were unreadable — there is nothing to send and nothing to say */
        })
    }
    container.addEventListener('paste', onPaste, { capture: true })

    return () => {
      container.removeEventListener('touchstart', onTouchStart, { capture: true })
      container.removeEventListener('touchmove', onTouchMove, { capture: true })
      container.removeEventListener('touchend', onTouchEnd, { capture: true })
      container.removeEventListener('paste', onPaste, { capture: true })
      container.removeEventListener('copy', onCopyCommand, { capture: true })
      document.removeEventListener('visibilitychange', retryNowIfOwed)
      window.removeEventListener('online', retryNowIfOwed)
      dataDisposable.dispose()
      resizeDisposable.dispose()
      titleDisposable.dispose()
      selectionDisposable.dispose()
      cpBadges.dispose()
      if (copyTimer !== null) window.clearTimeout(copyTimer)
      // Set BEFORE closing: some engines fire onclose synchronously.
      torndown = true
      if (retryTimer !== null) window.clearTimeout(retryTimer)
      if (stableTimer !== null) window.clearTimeout(stableTimer)
      if (dialTimer !== null) window.clearTimeout(dialTimer)
      stopKeepalive()
      // The held grant dies with the pane.
      releaseGrant()
      ws?.close()
      // WebGL first and defensively (see the load site); term.dispose() guarded likewise.
      if (webglRef.current) {
        disposeQuietly(webglRef.current)
        webglRef.current = null
      }
      disposeQuietly(term)
      fitRef.current = null
      wsRef.current = null
      termRef.current = null
    }
    // `view`/`fontSize` are connect identity (a different window; a different cell box and PTY size).
    // `direct`/`directUrlOverride`: a flip must tear down the live socket before dialling.
  }, [projectId, number, nodeId, kind, reconnectNonce, view, layout, fontSize, direct, directUrlOverride, mayWrite, provider])

  // Plain `<div>` mount for xterm. `data-testid="console-body"` replaces the retired `.console-body`
  // class (Selector Stability Contract rule 2).
  return (
    <Box
      ref={mountRef}
      data-testid="console-body"
      sx={{
        flex: 1,
        minHeight: 0,
        // Terminal ground behind the padding (see XTERM_THEME). `color` too: `.xterm` inherits text colour,
        // near-black under the light scheme, which the DOM-renderer fallback and a11y tree would use.
        bgcolor: RAW.dark.terminalBg,
        color: RAW.dark.textPrimary,
        px: '0.5rem',
        py: '0.35rem',
        /* NOT what routes terminal touch-drags (the JS handler does; this node is not an ancestor of the
           painted cells). Kept as a defensive guard against page scroll / pull-to-refresh on engines that
           reserve scrollbar width; measured 0px here. */
        '& .xterm-viewport': {
          touchAction: 'pan-y',
          overscrollBehavior: 'contain',
          WebkitOverflowScrolling: 'touch',
        },
      }}
    />
  )
})

export default ConsolePane
