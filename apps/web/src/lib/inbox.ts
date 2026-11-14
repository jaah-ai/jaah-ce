// The bell's wire (`apps/api/app/routes/inbox.py`): the item shapes and the read/seen calls. The rules
// are the API's; this module owns only what the web reads and sends.

import { get, post } from '../api'
import type { BillingNotice } from './billing'

export type InboxKind =
  | 'mention' | 'reaction' | 'support_ticket' | 'support_task_done' | 'billing'
  | 'trigger_failed' | 'trigger_digest' | 'automation_alert' | 'system_alert' | 'api_key_expiring'
  | ReleaseKind

/** A release notice's kind (`app/releases/notify.py`): "Approve release", "Live", "Release failed", and the approval
 * rules' reminder, timeout and rejection. */
export type ReleaseKind =
  | 'release_approval' | 'release_live' | 'release_failed' | 'release_rolled_back'
  | 'release_approval_reminder' | 'release_approval_timeout' | 'release_rejected'

/** A deploy notice (`app/releases/notify.py::wire`): the server's sentence and the project's pipeline page. */
export type ReleaseNotice = {
  text: string
  href: string
}

/** A trigger owner's notice, or an Owner's/Admin's automation or system alert (`app/trigger_notices.py::wire`): the
 *  server's sentence, a second line, and the
 *  console path it opens. */
export type TriggerNotice = {
  kind: 'trigger_failed' | 'trigger_digest' | 'automation_alert' | 'system_alert'
  trigger_id: string | null
  text: string
  detail: string
  href: string
}

/** An API key owner's expiry reminder (`app/api_keys.py::reminder_wire`): the server's sentence, a second line, and
 *  the key's console path. */
export type ApiKeyNotice = {
  text: string
  detail: string
  href: string
}

/** One bell entry (`routes/inbox.py::InboxItem`); `kind` decides which optional fields are set. */
export type InboxItem = {
  kind: InboxKind
  id: string
  created_at: string | null
  read_at: string | null
  task_id?: number | null
  task_title?: string | null
  /** Set instead of `task_id` for a mention (or reaction) in a support-ticket comment. */
  ticket_id?: number | null
  ticket_title?: string | null
  /** A support item's ticket status (`support_ticket`, `support_task_done`). */
  ticket_status?: string | null
  comment_id?: number | null
  /** A mention's commenter; a reaction's REACTOR. */
  author_name?: string | null
  /** The comment's opening words — for a reaction, the comment reacted to. */
  excerpt?: string | null
  /** A reaction's `lib/reactions.ts` key. */
  emoji?: string | null
  /** A `billing` item's notice; `id` is the notice's. */
  notice?: BillingNotice | null
  /** A `trigger_failed` / `trigger_digest` / `automation_alert` / `system_alert` item's notice; `id` is the notice's. */
  trigger_notice?: TriggerNotice | null
  /** An `api_key_expiring` item's reminder; `id` is the key's. */
  api_key_notice?: ApiKeyNotice | null
  /** A `release_*` item's notice; `id` is the notice's. */
  release_notice?: ReleaseNotice | null
}

export const INBOX_LIMIT = 50

/** The most `GET /api/inbox` returns (`comment_mentions.MAX_READ_IDS`): the Notifications page's last page. */
export const INBOX_MAX = 200

/** `unreadOnly` false is the Unread | All switch's All: read items of the kinds that keep a read stamp too
 *  (`routes/inbox.py`'s docstring). */
export const getInbox = (unreadOnly = true, limit = INBOX_LIMIT) =>
  get<InboxItem[]>(`/api/inbox?unread_only=${unreadOnly}&limit=${limit}`)

/** `all.mentions` / `all.reactions` mark EVERY unread one of that kind read server-side — not only the ids
 *  on screen, which may be capped at `INBOX_LIMIT` or stale. A flag is sent only when set. */
export const markSeen = (
  items: { kind: InboxKind; id: string }[],
  all: { mentions?: boolean; reactions?: boolean } = {},
) =>
  post<unknown>('/api/inbox/seen', {
    items,
    ...(all.mentions ? { all_mentions: true } : {}),
    ...(all.reactions ? { all_reactions: true } : {}),
  })

/** "Mark as unread": clears the read stamp of the listed items; a kind with no stamp is skipped server-side. */
export const markUnseen = (items: { kind: InboxKind; id: string }[]) => post<unknown>('/api/inbox/unseen', { items })
