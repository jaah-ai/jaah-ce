/* What one inbox item says and where it goes — the non-React half shared by the bell's tray
 * (`components/MentionsBell.tsx`) and the Notifications page (`pages/NotificationsPage.tsx`). The kinds and their
 * rules are `apps/api/app/routes/inbox.py`'s docstring; this file only words and routes them. */
import { scopeHref, scopeLabel, type InboxItem, type InboxKind, type TriggerNotice } from '../../lib/accessRequests'
import { noticeAction } from '../../lib/billing'
import { BILLING_PAGE } from '../../lib/companySetup'
import { reactionGlyph } from '../../lib/reactions'
import { STATUS_LABEL, type TicketStatus } from '../../lib/ticketStatus'

/* A glyph where a comment item's author badge sits — a key for access, a speech bubble for ticket news, a check
   for a linked task done, a card for billing. Inlined MUI paths: `@mui/icons-material` is not a dependency
   (`components/tasks/RatingBadge.tsx`). */
export const KEY_GLYPH =
  'M12.65 10C11.83 7.67 9.61 6 7 6c-3.31 0-6 2.69-6 6s2.69 6 6 6c2.61 0 4.83-1.67 5.65-4H17v4h4v-4h2v-4zM7 14c-1.1 0-2-.9-2-2s.9-2 2-2 2 .9 2 2-.9 2-2 2'
const TICKET_GLYPH = 'M20 2H4c-1.1 0-2 .9-2 2v18l4-4h14c1.1 0 2-.9 2-2V4c0-1.1-.9-2-2-2m0 14H6l-2 2V4h16z'
const DONE_GLYPH =
  'M16.59 7.58 10 14.17l-3.59-3.58L5 12l5 5 8-8zM12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2m0 18c-4.42 0-8-3.58-8-8s3.58-8 8-8 8 3.58 8 8-3.58 8-8 8'
const CARD_GLYPH =
  'M20 4H4c-1.11 0-1.99.89-1.99 2L2 18c0 1.11.89 2 2 2h16c1.11 0 2-.89 2-2V6c0-1.11-.89-2-2-2m0 14H4v-6h16zm0-10H4V6h16z'

/* A trigger the system stopped (a pause sign), the daily digest (a list), an automation failing or stalling
   (a warning sign). */
const STOPPED_GLYPH =
  'M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2m-1 14H9V8h2zm4 0h-2V8h2z'
const DIGEST_GLYPH = 'M3 13h2v-2H3zm0 4h2v-2H3zm0-8h2V7H3zm4 4h14v-2H7zm0 4h14v-2H7zM7 7v2h14V7z'
const ALERT_GLYPH = 'M1 21h22L12 2zm12-3h-2v-2h2zm0-4h-2v-4h2z'
const TRIGGER_GLYPHS: Record<TriggerNotice['kind'], string> = {
  trigger_failed: STOPPED_GLYPH,
  trigger_digest: DIGEST_GLYPH,
  automation_alert: ALERT_GLYPH,
}
const TRIGGER_FALLBACKS: Record<TriggerNotice['kind'], string> = {
  trigger_failed: 'Trigger stopped',
  trigger_digest: 'Trigger digest',
  automation_alert: 'Automation failing',
}

export function isTriggerKind(kind: InboxKind): kind is TriggerNotice['kind'] {
  return kind in TRIGGER_GLYPHS
}

export const isCommentKind = (kind: InboxKind) => kind === 'mention' || kind === 'reaction'

/** The kinds that keep a read stamp, so a read one stays listed under All and can be marked unread again. The
 *  rest — a pending request, both support kinds, a key reminder — are listed while unread only. */
export function keepsReadStamp(kind: InboxKind): boolean {
  return isCommentKind(kind) || kind === 'access_decision' || kind === 'billing' || isTriggerKind(kind)
}

export const isUnread = (item: InboxItem) => item.read_at === null

/** A pending request has nothing to mark: it leaves every approver's inbox when someone decides it. */
export const canMarkRead = (item: InboxItem) => isUnread(item) && item.kind !== 'access_request'

export const canMarkUnread = (item: InboxItem) => !isUnread(item) && keepsReadStamp(item.kind)

/** How one item reads, both lines. `glyph` absent: a comment item, whose author badge leads instead. */
export type ItemText = {
  glyph?: string
  title: string
  detail: string
  /** The title wraps instead of truncating: a billing sentence's card digits and date are the point. */
  wrap?: boolean
  /** The detail wraps to two lines before it is cut: a stopped trigger's reason is why it stopped. */
  wrapDetail?: boolean
}

export function describeItem(item: InboxItem): ItemText {
  if (isCommentKind(item.kind)) {
    // A reaction leads the excerpt with its glyph; the badge is the reactor.
    const glyph = item.kind === 'reaction' ? reactionGlyph(item.emoji ?? '') : ''
    return {
      title: (item.ticket_id != null ? item.ticket_title : item.task_title) ?? '',
      detail: `${glyph ? `${glyph} ` : ''}${item.excerpt ?? ''}`,
    }
  }
  if (item.kind === 'support_ticket' || item.kind === 'support_task_done') {
    return { glyph: item.kind === 'support_ticket' ? TICKET_GLYPH : DONE_GLYPH, ...supportLine(item) }
  }
  if (isTriggerKind(item.kind)) {
    return { glyph: TRIGGER_GLYPHS[item.kind], ...triggerLine(item), wrap: true, wrapDetail: true }
  }
  if (item.kind === 'api_key_expiring') return { glyph: KEY_GLYPH, ...apiKeyLine(item), wrap: true }
  if (item.kind === 'billing') return { glyph: CARD_GLYPH, ...billingLine(item), wrap: true }
  return { glyph: KEY_GLYPH, ...accessLine(item) }
}

/** Where opening an item goes: both support kinds the ticket's page, a billing item its notice's action, a
 *  trigger item the trigger (or the Triggers page), an automation alert the automation's runs, a key reminder
 *  the key. `external`: a new tab (a billing portal). */
export function itemTarget(item: InboxItem): { href: string; external?: boolean } {
  if (isCommentKind(item.kind)) {
    // A ticket comment opens the ticket; its thread is the page, so no tab.
    return {
      href: item.ticket_id != null
        ? `/support/tickets/${item.ticket_id}#comment-${item.comment_id}`
        : `/tasks/${item.task_id}?tab=comments#comment-${item.comment_id}`,
    }
  }
  if (item.kind === 'access_request') return { href: '/support?tab=requests&box=pending' }
  if (item.kind === 'support_ticket' || item.kind === 'support_task_done') return { href: `/support/tickets/${item.ticket_id}` }
  if (isTriggerKind(item.kind)) return { href: item.trigger_notice?.href ?? '/triggers' }
  if (item.kind === 'api_key_expiring') return { href: item.api_key_notice?.href ?? '/settings/api-keys' }
  if (item.kind === 'billing') {
    // No usable action still has a home: the Billing page lists what the notice is about.
    const action = noticeAction(item.notice?.action ?? null)
    return action ? { href: action.href, external: action.external } : { href: BILLING_PAGE }
  }
  const href = item.request?.state === 'approved' && item.request ? scopeHref(item.request.scope) : null
  return { href: href ?? '/support?tab=requests&box=mine' }
}

/** What an access item says, in two lines like a mention's: the short, identifying part bold (who asks /
 *  what was decided), the role and scope below. A long name truncates alone, never the role. */
function accessLine(item: InboxItem): { title: string; detail: string } {
  const r = item.request
  if (!r) return { title: 'Access request', detail: '' }
  if (item.kind === 'access_request') {
    return { title: r.requester.name || 'Someone', detail: `asks for ${r.role.name} on ${scopeLabel(r.scope)}` }
  }
  return {
    title: r.state === 'approved' ? 'Request approved' : 'Request denied',
    detail: `${r.role.name} on ${scopeLabel(r.scope)}`,
  }
}

/** A trigger item's two lines, both the server's: what happened (wraps — a trigger name is the point), then
 *  the reason or the window. */
function triggerLine(item: InboxItem): { title: string; detail: string } {
  const notice = item.trigger_notice
  const fallback = TRIGGER_FALLBACKS[item.kind as TriggerNotice['kind']]
  return { title: notice?.text || fallback, detail: notice?.detail || 'Triggers' }
}

/** An API key reminder's two lines, both the server's: the key's name and when it expires (wraps — both are the
 *  point), then where it lives. */
function apiKeyLine(item: InboxItem): { title: string; detail: string } {
  const notice = item.api_key_notice
  return { title: notice?.text || 'API key expiring', detail: notice?.detail || 'API keys' }
}

/** A billing item's two lines: the server's sentence, then where opening it goes. */
function billingLine(item: InboxItem): { title: string; detail: string } {
  const action = noticeAction(item.notice?.action ?? null)
  return { title: item.notice?.text ?? 'Billing notice', detail: action ? `Billing · ${action.label}` : 'Billing' }
}

/** A support item's two lines: the ticket's title bold (what it is about), then its number and what
 *  happened — the status for ticket news, "Linked task done" for the other. */
function supportLine(item: InboxItem): { title: string; detail: string } {
  const ticket = `Ticket #${item.ticket_id}`
  if (item.kind === 'support_task_done') return { title: item.ticket_title ?? ticket, detail: `${ticket} · Linked task done` }
  const status = STATUS_LABEL[item.ticket_status as TicketStatus]
  return { title: item.ticket_title ?? ticket, detail: status ? `${ticket} · ${status}` : ticket }
}
