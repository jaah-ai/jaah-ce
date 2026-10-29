// THE NOTIFICATIONS BLOCK'S VOCABULARY AND ITS ONE HYDRATE RULE.
//
// `preferences.notifications` is edited from `/profile` and a superadmin's `/users/:id`, through ONE
// panel (`components/NotificationsPanel.tsx`); this is its non-React half (names, hydrate, equality),
// the split `lib/userSettings.ts` + `components/UserSettingsFields.tsx` keep.
//
// NOT PART OF `UserSettingsDraft`: `/users/:id` saves notifications with its own button and PATCH, so
// folding it in would make that page's Settings save re-assert a block the operator never opened.
import type {
  NotificationEventDetail,
  NotificationEventPreference,
  NotificationMethod,
  NotificationMethodType,
  UserNotificationsPreference,
} from '../types'

/**
 * The events the panel lists, IN THIS ORDER, with the word an operator reads for each.
 *
 * MIRRORS `app/preferences.py::NOTIFICATION_EVENTS` (`("done", "question", "mention", "reaction", "support", "billing",
 * "budget", "triggers", "api_key_expiry", "automation_failures")` then
 * `alert_relay.ALERT_KINDS` minus `done`); no route serves it. A key missing here is simply not offered
 * (its stored choice rides along, see `readNotifications`), never a row labelled with the raw key.
 *
 * LABELS ARE NOUNS OR SHORT PHRASES: one word where one word does. "Question waiting" and
 * "Assigned to you" need their words: `question` is derived from a `blocked` transition carrying a
 * question card, and `assigned` is about the READER, not a task state.
 */
export const NOTIFICATION_EVENTS: readonly { readonly event: string; readonly label: string }[] = [
  { event: 'done', label: 'Finished' },
  { event: 'question', label: 'Question waiting' },
  { event: 'mention', label: 'Mentioned' },
  // Someone reacted to the reader's comment.
  { event: 'reaction', label: 'Reactions' },
  { event: 'support', label: 'Support' },
  // An Owner/Admin's: a failed payment, a refund, a card about to expire.
  { event: 'billing', label: 'Billing' },
  // An Owner/Admin's: a project or group budget reached 80% or 100%.
  { event: 'budget', label: 'Budgets' },
  // A trigger owner's: one of their triggers stopped, and the daily digest.
  { event: 'triggers', label: 'Triggers' },
  // An Owner/Admin's: one of their Jaah API keys expires in 7 days or tomorrow.
  { event: 'api_key_expiry', label: 'API key expiry' },
  // An Owner/Admin's: one of the org's automations is failing or stalling. Email is sent whatever this says.
  { event: 'automation_failures', label: 'Automation failures' },
  { event: 'blocked', label: 'Blocked' },
  { event: 'error', label: 'Error' },
  { event: 'failed', label: 'Failed' },
  { event: 'self-heal', label: 'Self-heal' },
  // A runner account's Claude login nears its hard expiry; only a re-login renews it.
  { event: 'login-expiring', label: 'Login expiring' },
  // A node's disk went critical, so its runner and CI dispatch paused until usage drops. Plain words, not the
  // host's: Owners and Admins read it, not only those who run nodes. This and the two node events beside it are
  // listed to Owners and Admins only (`notification_events`).
  { event: 'node-disk-paused', label: 'Node disk full' },
  // A node's /tmp inode use crossed 90%: every mktemp there fails at 100%. Plain words, as above.
  { event: 'node-tmp-inodes', label: 'Node temp folder full' },
  // An SDK agent's informational post about its task.
  { event: 'agent-message', label: 'Agent messages' },
  { event: 'assigned', label: 'Assigned to you' },
]

/**
 * Per channel, IN DISPLAY ORDER: the row's title and its id field. `field.label` NAMES THE VALUE, so no
 * help text is needed; Slack's `placeholder` is an EXAMPLE matching the server's `^[UW][A-Z0-9]{2,32}$`
 * and its 422's `U0123ABCD`. `field: null` — no id to enter: email delivers to the login email, telegram
 * to the person's linked chat, and the server refuses a non-empty `user_id` for either.
 */
export const NOTIFICATION_METHODS: Record<
  NotificationMethodType,
  { readonly label: string; readonly field: { readonly label: string; readonly placeholder: string } | null }
> = {
  slack: { label: 'Slack', field: { label: 'Slack member id', placeholder: 'U0123ABCD' } },
  email: { label: 'Email', field: null },
  telegram: { label: 'Telegram', field: null },
}

/** Every renderable channel, as `NOTIFICATION_METHODS`' keys, so the add affordance and the guard agree. */
export const NOTIFICATION_METHOD_TYPES = Object.keys(NOTIFICATION_METHODS) as NotificationMethodType[]

/** Whether a wire string is a channel this build renders; unknown types are dropped here alone. */
export function isNotificationMethodType(value: unknown): value is NotificationMethodType {
  return typeof value === 'string' && Object.prototype.hasOwnProperty.call(NOTIFICATION_METHODS, value)
}

/** The default for an event with nothing stored: delivered, in full, emailed too. MUST MATCH THE SERVER
 *  (`app/preferences.py::_complete_events`): defaulting to silence would store "muted" over every
 *  existing recipient on their first save. */
export const DEFAULT_NOTIFICATION_EVENT: NotificationEventPreference = { enabled: true, detail: 'full', email: true }

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
}

/** One stored event row, coerced; every field degrades to the default, never a guess. */
function readEvent(raw: unknown): NotificationEventPreference {
  if (!isRecord(raw)) return { ...DEFAULT_NOTIFICATION_EVENT }
  const detail: NotificationEventDetail = raw.detail === 'brief' ? 'brief' : 'full'
  return {
    enabled: typeof raw.enabled === 'boolean' ? raw.enabled : DEFAULT_NOTIFICATION_EVENT.enabled,
    detail,
    email: typeof raw.email === 'boolean' ? raw.email : DEFAULT_NOTIFICATION_EVENT.email,
  }
}

/**
 * The methods a stored block carries, cleaned, with the pre-3176 `{channel: 'slack', user_id}` document
 * adopted. THE LEGACY ARM IS LOAD-BEARING ON `/profile`: it seeds from `authStore`'s persisted snapshot,
 * which can predate the field, and `ProfilePage` hydrates once per account (`hydratedKeyRef`) — "no
 * methods" would then be saved over a live channel. (The API does the same in `_tolerate`.)
 * FIRST ENTRY OF A TYPE WINS (the server's dedup): one row per type.
 */
function readMethods(block: Record<string, unknown>): NotificationMethod[] {
  const raw = block.methods
  if (Array.isArray(raw)) {
    const methods: NotificationMethod[] = []
    for (const entry of raw) {
      if (!isRecord(entry) || !isNotificationMethodType(entry.type)) continue
      if (methods.some(m => m.type === entry.type)) continue
      methods.push({
        type: entry.type,
        // `''` for a missing id: a fixable empty field, which the server refuses on write. Always `''` for a
        // channel with no id field (email, telegram), which the server refuses otherwise.
        user_id: NOTIFICATION_METHODS[entry.type].field && typeof entry.user_id === 'string' ? entry.user_id : '',
        // Slack's name, for its line only: `methodsToSave` leaves it out of a save.
        ...(entry.type === 'slack' && typeof entry.display_name === 'string' && entry.display_name
          ? { display_name: entry.display_name }
          : {}),
      })
    }
    return methods
  }
  const legacyId = block.user_id
  return block.channel === 'slack' && typeof legacyId === 'string' && legacyId !== ''
    ? [{ type: 'slack', user_id: legacyId }]
    : []
}

/**
 * A draft's methods as a save sends them: without Slack's `display_name`, which only Connect writes. The server
 * carries the stored one forward while the member id is unchanged (`preferences_to_store`), so a tab loaded before
 * a re-link cannot send back a name the server no longer holds.
 */
export function methodsToSave(methods: NotificationMethod[]): NotificationMethod[] {
  return methods.map(({ display_name: _name, ...method }) => method)
}

/**
 * A stored preferences document as the panel's draft — the ONE hydrate rule both surfaces use.
 *
 * ALWAYS COMPLETE for every listed event (also from `null` and from pre-feature snapshots), so nothing
 * downstream needs a default. A STORED KEY THIS BUILD DOES NOT LIST SURVIVES: both saves PUT THE WHOLE
 * DOCUMENT, so dropping it would delete a choice made on a newer tab.
 */
export function readNotifications(
  preferences: Record<string, unknown> | null | undefined,
): UserNotificationsPreference {
  const stored = preferences?.notifications
  const block = isRecord(stored) ? stored : {}
  const events: Record<string, NotificationEventPreference> = {}
  if (isRecord(block.events)) {
    for (const [event, raw] of Object.entries(block.events)) events[event] = readEvent(raw)
  }
  for (const { event } of NOTIFICATION_EVENTS) {
    if (!(event in events)) events[event] = { ...DEFAULT_NOTIFICATION_EVENT }
  }
  return { methods: readMethods(block), events }
}

/**
 * Whether two drafts say the same thing — field-wise, never through `JSON.stringify`: `events` order
 * varies with what was stored, and a false positive would make `/profile`'s minimal body
 * (`ProfilePage.buildBody`) rewrite an unopened block.
 */
export function notificationsEqual(a: UserNotificationsPreference, b: UserNotificationsPreference): boolean {
  if (a.methods.length !== b.methods.length) return false
  if (a.methods.some((method, i) => method.type !== b.methods[i].type || method.user_id !== b.methods[i].user_id)) {
    return false
  }
  const keys = Object.keys(a.events)
  if (keys.length !== Object.keys(b.events).length) return false
  return keys.every(key => {
    const other = b.events[key]
    const mine = a.events[key]
    return other !== undefined && other.enabled === mine.enabled && other.detail === mine.detail && other.email === mine.email
  })
}
