// The lightning toggle's state machine and per-user memory — the pure half of the console header's
// direct-connection control. The wire contract lives in lib/directConsole.ts; nothing here touches a socket.
//
// State is DERIVED from (user's persisted choice, per-pane reported status), never stored: a stored enum
// would go stale the instant a socket dropped (AC7.2). There is no fall-back-on-failure state: a failing
// direct pane retries the node (ConsolePane's `retryDirect`). `transport: 'cp'` under an enabled toggle
// means only one thing — the CP answered `disabled` (direct console is not configured for that node) —
// and it reads as `relayed`, never as `direct`.

import { readString, writeString } from './safeStorage'

/** A structural subset of ConsolePane's `ConsoleStatus`, declared locally to stay component-free. */
export type TransportStatus = {
  state: 'connecting' | 'connected' | 'closed'
  transport?: 'cp' | 'direct'
}

/**
 * The states AC7.2 requires be told apart:
 *   off        — direct mode not asked for; relayed.
 *   connecting — asked for, being established. Transient.
 *   direct     — asked for, bytes go straight to the node.
 *   closed     — asked for, not connected (node unreachable and retrying, or session ended).
 *   relayed    — asked for, but the CP says direct console is not configured for this node; relayed.
 * `closed` exists so a dead console never shows a perpetual "connecting…".
 */
export type DirectToggleState = 'off' | 'connecting' | 'direct' | 'closed' | 'relayed'

/**
 * What the control should say. `statuses` is every rendering pane (one per visible layout tile, AC8.10).
 * Priority, worst-first: not asked -> off; ANY pane relayed (CP said `disabled`) -> relayed; ANY pane
 * closed -> closed (one dead tile must not hide);
 * ALL connected -> direct; else connecting (includes mount with no statuses yet).
 */
export function directToggleState(enabled: boolean, statuses: readonly TransportStatus[]): DirectToggleState {
  if (!enabled) return 'off'
  // Direct console is switched off for this node: the pane is on the relay by design, not by failure.
  if (statuses.some((s) => s.transport === 'cp')) return 'relayed'
  if (statuses.some((s) => s.state === 'closed')) return 'closed'
  if (statuses.length === 0) return 'connecting'
  if (statuses.every((s) => s.state === 'connected')) return 'direct'
  return 'connecting'
}

/**
 * The ⚙ popup's pre-toggle explainer. Asserted by IDENTITY in the suite, not by keywords: a keyword
 * guard passed a rewrite that appended a false relay-fallback promise. Any edit must be deliberate:
 * direct mode never falls back to the relay.
 */
export const DIRECT_MODE_EXPLAINER =
  'The ⚡ in the console header turns this on. It skips the relay and keeps the connection '
  + 'on the node: if the node cannot be reached, this console says so and keeps retrying it rather than '
  + 'quietly relaying. Turn it off to use the relay.'

/** How one state is presented. `badge` is a GLYPH, not a colour — see `DIRECT_TOGGLE_VIEW`. */
export type DirectToggleView = {
  /** The control's accessible name. DISTINCT per state — this is what a screen reader announces. */
  label: string
  /** The tooltip. Says what the state means and, where it is not obvious, what to do about it. */
  title: string
  /** A shape rendered beside the bolt, so the states differ without hovering and without colour. */
  badge: string
}

/**
 * The presentations. No state is conveyed by colour alone: each has a distinct accessible name,
 * tooltip and badge glyph (nothing / … / ✓ / × / –). `off`'s empty badge is its own shape.
 */
export const DIRECT_TOGGLE_VIEW: Record<DirectToggleState, DirectToggleView> = {
  off: {
    label: 'Direct connection: off',
    title:
      'Direct connection off — this console is relayed. Turn it on to connect straight to the node.',
    badge: '',
  },
  connecting: {
    label: 'Direct connection: connecting',
    title: 'Connecting straight to the node…',
    badge: '…',
  },
  direct: {
    label: 'Direct connection: on, connected straight to the node',
    title: 'Connected straight to the node — the relay is out of the path.',
    badge: '✓',
  },
  // No retry interval in this string: its single home is ConsolePane's `DIRECT_RETRY_DELAY_MS`, which this
  // module cannot import; the notice bar shown in this state renders the real interval.
  closed: {
    label: 'Direct connection: on, but this console is not connected',
    title:
      'Direct mode is on and this console is not connected — it keeps retrying the node and will never fall back to the relay. Reconnect to try immediately.',
    badge: '×',
  },
  relayed: {
    label: 'Direct connection: not available for this node, relayed',
    title: 'Direct connection is not set up for this node, so this console uses the relay.',
    badge: '–',
  },
}

// ── Per-user memory (settled D11) ───────────────────────────────────────────────────────────────────
// OFF on first use, then the user's last choice. One key, not per slot: it is about this person's network.
// `localStorage` (per browser); no server-side preference store exists. A remembered ON can open a
// non-working console on a node without direct support — accepted, since that failure is loud and one
// click from its remedy.

/** The key the preference is stored under. Exported so a test names the same string the code does. */
export const DIRECT_ENABLED_KEY = 'console-direct-enabled'

/**
 * The remembered preference. Anything but the exact `'1'`, including a storage that throws on read
 * (private-mode Safari), reads as OFF — direct mode is opt-in.
 */
export function loadDirectEnabled(): boolean {
  return readString('local', DIRECT_ENABLED_KEY) === '1'
}

/**
 * Remember the user's choice; returns whether the write landed. A failed write never blocks honouring
 * the choice for this session.
 */
export function saveDirectEnabled(enabled: boolean): boolean {
  return writeString('local', DIRECT_ENABLED_KEY, enabled ? '1' : null)
}
