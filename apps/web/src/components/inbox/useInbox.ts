/* The inbox's reads and writes, shared by the bell's tray (`components/MentionsBell.tsx`) and the Notifications page
 * (`pages/NotificationsPage.tsx`). Two feeds behind one switch, Unread | All (`GET /api/inbox?unread_only=`), each
 * cached per page size under the one `['inbox']` root, so every write patches every copy at once and anything that
 * invalidates `['inbox']` (`pages/Requests.tsx`) refreshes both.
 * Polls every 60s, not `useEventStream`: neither comments nor requests publish, so a subscription would refresh only
 * on unrelated task events. When the server publishes, drop the interval, keep the query.
 * A failed fetch keeps the last good answer — emptying the list reads as someone having read them. Writes are
 * optimistic; a failed POST refetches, which puts the server's truth back. */
import { useNavigate } from 'react-router-dom'
import { keepPreviousData, useQuery, useQueryClient } from '@tanstack/react-query'
import { getInbox, markSeen, markUnseen, type InboxItem } from '../../lib/accessRequests'
import { canMarkRead, isCommentKind, itemTarget, keepsReadStamp } from './inboxItem'

/** Delete when the server publishes (see header). */
const REFRESH_MS = 60_000

const INBOX_ROOT = ['inbox'] as const

export type InboxView = 'unread' | 'all'

const inboxKey = (view: InboxView, limit: number) => [...INBOX_ROOT, view, limit] as const

/** One feed. `Array.isArray`: the bell is mounted by `Sidebar` on every route, so a non-list would crash the app
 *  chrome. */
export function useInboxFeed(view: InboxView, limit: number, enabled = true) {
  const query = useQuery({
    queryKey: inboxKey(view, limit),
    queryFn: () => getInbox(view === 'unread', limit),
    refetchInterval: REFRESH_MS,
    enabled,
    // Load more keeps the rows on screen while the wider page loads.
    placeholderData: keepPreviousData,
  })
  const entries: InboxItem[] = Array.isArray(query.data) ? query.data : []
  return { query, entries }
}

const same = (a: InboxItem, b: InboxItem) => a.kind === b.kind && a.id === b.id
const newestFirst = (a: InboxItem, b: InboxItem) => (b.created_at ?? '').localeCompare(a.created_at ?? '')

export function useInboxActions() {
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const refetch = () => void queryClient.invalidateQueries({ queryKey: INBOX_ROOT })

  /** Rewrite every cached feed; `unread` says which kind of feed `list` is. Each stays within its page size. */
  function patch(rewrite: (list: InboxItem[], unread: boolean) => InboxItem[]): void {
    // A poll already in flight would otherwise put the old rows back.
    void queryClient.cancelQueries({ queryKey: INBOX_ROOT })
    for (const [key, data] of queryClient.getQueriesData<InboxItem[]>({ queryKey: INBOX_ROOT })) {
      if (Array.isArray(data)) queryClient.setQueryData(key, rewrite(data, key[1] === 'unread').slice(0, Number(key[2])))
    }
  }

  /** Every unread item any cached Unread feed holds: All's window can miss one the Unread feed lists. */
  function cachedUnread(): InboxItem[] {
    return queryClient.getQueriesData<InboxItem[]>({ queryKey: [...INBOX_ROOT, 'unread'] }).flatMap(([, data]) => data ?? [])
  }

  /** Read: out of Unread; under All it stays, read — or goes, for a kind that keeps no read stamp. */
  function readLocally(matches: (e: InboxItem) => boolean): void {
    const at = new Date().toISOString()
    patch((list, unread) => list.flatMap(e => {
      if (!matches(e)) return [e]
      if (unread || !keepsReadStamp(e.kind)) return []
      return [{ ...e, read_at: at }]
    }))
  }

  function markRead(items: InboxItem[]): void {
    const marked = items.filter(canMarkRead)
    if (marked.length === 0) return
    readLocally(e => marked.some(m => same(m, e)))
    void markSeen(marked.map(({ kind, id }) => ({ kind, id }))).catch(refetch)
  }

  /** Back to unread: flagged under All, and back in Unread in date order. */
  function markUnread(items: InboxItem[]): void {
    if (items.length === 0) return
    const back = items.map(e => ({ ...e, read_at: null }))
    patch((list, unread) => {
      const flipped = list.map(e => back.find(b => same(b, e)) ?? e)
      return unread ? [...flipped, ...back.filter(b => !list.some(e => same(b, e)))].sort(newestFirst) : flipped
    })
    void markUnseen(items.map(({ kind, id }) => ({ kind, id }))).catch(refetch)
  }

  /** One request. Mentions and reactions: `all_mentions`/`all_reactions` = the API's "all unread", not the ids on
   *  screen, which may be capped or stale. The other kinds: the ones listed here or in any cached Unread feed.
   *  Pending requests stay — they are work, not news. */
  function markAllRead(entries: InboxItem[]): void {
    const unread = [...entries, ...cachedUnread()].filter(canMarkRead)
      .filter((e, i, all) => all.findIndex(o => same(o, e)) === i)
    const listed = unread.filter(e => !isCommentKind(e.kind)).map(({ kind, id }) => ({ kind, id }))
    const mentions = unread.some(e => e.kind === 'mention')
    const reactions = unread.some(e => e.kind === 'reaction')
    readLocally(e => canMarkRead(e) && (isCommentKind(e.kind) || listed.some(l => l.kind === e.kind && l.id === e.id)))
    if (mentions || reactions || listed.length > 0) {
      void markSeen(listed, { mentions, reactions }).catch(refetch)
    }
  }

  /** Navigate first, mark read after. */
  function open(entry: InboxItem): void {
    const target = itemTarget(entry)
    if (target.external) window.open(target.href, '_blank', 'noopener,noreferrer')
    else navigate(target.href)
    markRead([entry])
  }

  return { open, markRead, markUnread, markAllRead }
}

/** Something Mark all read would change. */
export const hasMarkable = (entries: InboxItem[]) => entries.some(canMarkRead)
