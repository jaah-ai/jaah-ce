/* The bell — the in-app half of the caller's notifications; Slack's cards are the other half.
 * One feed, `GET /api/inbox` (`apps/api/app/routes/inbox.py`), eleven kinds:
 * `mention` (named in a comment), `reaction` (someone reacted to the caller's comment — the same row, its
 * excerpt led by the glyph), `access_request` (a request the caller may decide), `access_decision` (the
 * caller's own request, decided), `support_ticket` (news on a support ticket), `support_task_done` (an
 * operator's: a task linked to a ticket finished), `billing` (an Owner/Admin's: a failed payment, a
 * refund, a card about to expire — its sentence the server's), the two trigger kinds, `trigger_failed`
 * (the system stopped one of the caller's triggers) and `trigger_digest` (their daily digest), and
 * `automation_alert` (an Owner/Admin's: one of the org's automations is failing or stalling), each the
 * server's sentence too, and `api_key_expiring` (the caller's API key is about to expire — the server's sentence,
 * name and expiry). What each says and where it opens: `components/inbox/inboxItem.ts`; the feeds, the polling and the
 * writes: `components/inbox/useInbox.ts`.
 * An Unread | All switch heads the tray, Unread by default — the badge counts unread whichever is shown. All adds the
 * read items of the kinds that keep a read stamp, so a comment read in passing can be found again; each row's dot
 * marks it read, or (read) unread again — the "answer it later" queue. View all opens the same feeds as a page
 * (`pages/NotificationsPage.tsx`).
 * Mounted for every signed-in user: a requester holding no key at all must still hear the answer, and
 * the mention arm is narrowed server-side to tasks the caller reads. */
import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import Badge from '@mui/material/Badge'
import Box from '@mui/material/Box'
import IconButton from '@mui/material/IconButton'
import MenuItem from '@mui/material/MenuItem'
import MenuList from '@mui/material/MenuList'
import Popover from '@mui/material/Popover'
import Typography from '@mui/material/Typography'
import { INBOX_LIMIT, type InboxItem } from '../lib/accessRequests'
import { relativeAge } from '../lib/relativeTime'
import { Icon } from '../ui/icons'
import { PHONE } from '../ui/listStyles'
import SegmentedControl from '../ui/SegmentedControl'
import { canMarkRead, canMarkUnread, describeItem, isUnread } from './inbox/inboxItem'
import { hasMarkable, useInboxActions, useInboxFeed, type InboxView } from './inbox/useInbox'
import AuthorBadge from './tasks/AuthorBadge'

/* The API `limit` (`INBOX_LIMIT`): a 50-row popover is already long. The badge caps at `50+`. */
const LIMIT = INBOX_LIMIT

export const VIEW_OPTIONS = [
  { value: 'unread', label: 'Unread', testId: 'inbox-view-unread' },
  { value: 'all', label: 'All', testId: 'inbox-view-all' },
] as const

/* Filled when something waits, outline otherwise: a colour-independent second channel for the badge. */
const BELL_FILLED =
  'M12 22c1.1 0 2-.9 2-2h-4c0 1.1.89 2 2 2m6-6v-5c0-3.07-1.64-5.64-4.5-6.32V4c0-.83-.67-1.5-1.5-1.5s-1.5.67-1.5 1.5v.68C7.63 5.36 6 7.92 6 11v5l-2 2v1h16v-1z'
const BELL_OUTLINE =
  'M12 22c1.1 0 2-.9 2-2h-4c0 1.1.9 2 2 2m6-6v-5c0-3.07-1.63-5.64-4.5-6.32V4c0-.83-.67-1.5-1.5-1.5s-1.5.67-1.5 1.5v.68C7.64 5.36 6 7.92 6 11v5l-2 2v1h16v-1zm-2 1H8v-6c0-2.48 1.51-4.5 4-4.5s4 2.02 4 4.5z'

/* Popover width: MUI's `calc(100% - 32px)` alone lets a long title stretch the menu on desktop. */
const MENU_PAPER_SX = { maxWidth: 'min(360px, calc(100% - 32px))', minWidth: 'min(300px, calc(100% - 32px))' } as const

/* One line per field: titles/excerpts are unbounded and `MenuItem` is `nowrap` with no ellipsis. Rows
   are pointers to the task, so truncation is fine (unlike `ProjectMenu`, whose rows are the choice). */
const ONE_LINE_SX = { overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' } as const
/* Up to two lines, then an ellipsis — for a detail whose words are the point. */
const TWO_LINES_SX = {
  overflow: 'hidden', whiteSpace: 'normal', overflowWrap: 'anywhere', display: '-webkit-box',
  WebkitLineClamp: 2, WebkitBoxOrient: 'vertical',
} as const

/* Badge geometry is the host's (`AuthorBadge` contract); 18px fits the title line. */
const AUTHOR_SX = {
  flex: '0 0 auto',
  width: 18,
  fontSize: '0.5625rem',
  fontWeight: 700,
  lineHeight: '18px',
  textAlign: 'center',
} as const

export default function MentionsBell(): JSX.Element {
  const navigate = useNavigate()
  const [anchorEl, setAnchorEl] = useState<HTMLElement | null>(null)
  const [view, setView] = useState<InboxView>('unread')
  const unreadFeed = useInboxFeed('unread', LIMIT)
  // Read only while the tray shows it: the badge needs the unread feed alone.
  const allFeed = useInboxFeed('all', LIMIT, anchorEl !== null && view === 'all')
  const { open, markRead, markUnread, markAllRead } = useInboxActions()
  const shown = view === 'unread' ? unreadFeed : allFeed
  const entries = shown.entries

  const close = () => setAnchorEl(null)
  const unread = unreadFeed.entries.length
  const markable = hasMarkable(entries)
  return (
    <>
      <IconButton
        data-testid="mentions-bell"
        onClick={e => setAnchorEl(e.currentTarget)}
        title="Notifications"
        /* The count is in the accessible name; the badge alone announces a bare digit, or nothing. */
        aria-label={unread > 0 ? `Notifications, ${unread} unread` : 'Notifications'}
        sx={{ color: 'inherit', padding: '2px 5px', borderRadius: '3px' }}
      >
        <Badge
          data-testid="mentions-badge"
          badgeContent={unread}
          max={LIMIT}
          color="error"
          /* `showZero` false (MUI default) is a requirement: `0` reads as a broken control. */
          sx={{ '& .MuiBadge-badge': { fontSize: '0.625rem', height: 16, minWidth: 16 } }}
        >
          <Icon viewBox="0 0 24 24" size="1.125rem" sx={{ display: 'block', fill: 'currentColor' }}>
            <path d={unread > 0 ? BELL_FILLED : BELL_OUTLINE} />
          </Icon>
        </Badge>
      </IconButton>
      {/* A Popover around a MenuList, not a `Menu`: the switch above the rows is not a menu item. */}
      <Popover
        anchorEl={anchorEl}
        open={anchorEl !== null}
        onClose={close}
        data-testid="mentions-menu"
        anchorOrigin={{ vertical: 'bottom', horizontal: 'left' }}
        slotProps={{ paper: { sx: MENU_PAPER_SX } }}
      >
        <Box sx={{ display: 'flex', alignItems: 'center', px: 2, pt: 1, pb: 0.5 }}>
          <SegmentedControl
            value={view}
            options={VIEW_OPTIONS}
            onChange={setView}
            ariaLabel="Show"
            testId="inbox-view"
          />
        </Box>
        {/* Focus lands in the list, as a `Menu`'s does: ArrowDown works at once; Shift+Tab reaches the switch. */}
        <MenuList dense aria-label="Notifications" autoFocusItem>
          {/* No action: nothing to do here. `disabled` keeps it out of the tab order. */}
          {entries.length === 0 && (
            <MenuItem disabled data-testid="mentions-empty" sx={{ opacity: 1 }}>
              <Typography variant="body2" sx={{ color: 'text.secondary' }}>
                {shown.query.data === undefined && shown.query.isFetching
                  ? 'Loading…'
                  : view === 'unread' ? 'Nothing new' : 'No notifications'}
              </Typography>
            </MenuItem>
          )}
          {entries.map(entry => (
            <InboxMenuItem
              key={`${entry.kind}-${entry.id}`}
              entry={entry}
              onOpen={() => { close(); open(entry) }}
              onToggle={isUnread(entry) ? () => markRead([entry]) : () => markUnread([entry])}
            />
          ))}
          {markable && (
            /* Not "Clear": it reads as delete. */
            <MenuItem
              data-testid="mentions-mark-all"
              onClick={() => { close(); markAllRead(entries) }}
              sx={{ [PHONE]: { minHeight: 44 } }}
            >
              <Typography variant="body2" sx={{ fontWeight: 600 }}>
                Mark all read
              </Typography>
            </MenuItem>
          )}
          <MenuItem
            data-testid="mentions-view-all"
            onClick={() => { close(); navigate(`/notifications${view === 'all' ? '?tab=all' : ''}`) }}
            sx={{ [PHONE]: { minHeight: 44 } }}
          >
            <Typography variant="body2" sx={{ color: 'primary.main', fontWeight: 600 }}>
              View all
            </Typography>
          </MenuItem>
        </MenuList>
      </Popover>
    </>
  )
}

/* One item: who or what (author badge, or the kind's glyph) and where above, what happened below, the age and the
   read dot at the end. testids keep the kind as prefix: a comment item's is `<kind>-<id>`, the rest `inbox-<kind>-<id>`.
   An unread item is bold; a read one (All only) is not. */
function InboxMenuItem({ entry, onOpen, onToggle }: { entry: InboxItem; onOpen: () => void; onToggle: () => void }) {
  const text = describeItem(entry)
  const unread = isUnread(entry)
  const comment = text.glyph === undefined
  return (
    <MenuItem
      data-testid={comment ? `${entry.kind}-${entry.id}` : `inbox-${entry.kind}-${entry.id}`}
      data-read={unread ? undefined : 'true'}
      onClick={onOpen}
      sx={{ display: 'block', py: 0.5, pr: 1, [PHONE]: { minHeight: 44 } }}
    >
      <Box sx={{ display: 'flex', alignItems: 'center', gap: 0.75, minWidth: 0 }}>
        {comment ? (
          <AuthorBadge name={entry.author_name ?? ''} sx={AUTHOR_SX} testId={`${entry.kind}-author-${entry.id}`} />
        ) : (
          <Icon viewBox="0 0 24 24" size={18} sx={{ flex: '0 0 auto', fill: 'currentColor', color: 'text.secondary' }}>
            <path d={text.glyph} />
          </Icon>
        )}
        <Typography
          variant="body2"
          sx={{
            ...(text.wrap ? { whiteSpace: 'normal', overflowWrap: 'anywhere' } : ONE_LINE_SX),
            flex: 1,
            minWidth: 0,
            fontWeight: unread ? 600 : 400,
          }}
        >
          {text.title}
        </Typography>
        {/* `relativeAge`, not `compactAge` — see `lib/relativeTime.ts`. */}
        <Typography
          variant="caption"
          sx={{ flex: '0 0 auto', color: 'text.secondary', fontVariantNumeric: 'tabular-nums' }}
        >
          {relativeAge(entry.created_at)}
        </Typography>
        <ReadDot entry={entry} onToggle={onToggle} />
      </Box>
      <Typography
        variant="caption"
        component="div"
        sx={{ ...(text.wrapDetail ? TWO_LINES_SX : ONE_LINE_SX), color: 'text.secondary', pl: '1.5rem', pr: '26px' }}
      >
        {text.detail}
      </Typography>
    </MenuItem>
  )
}

/* The read toggle: a filled dot on an unread item (press: mark read), a ring on a read one (press: mark unread) —
   the ring only on hover or focus where there is hover, always on touch. A pending request has nothing to mark and
   a read item of a kind with no read stamp is never listed; either keeps the slot, so ages line up. */
const DOT_SLOT = 22

function ReadDot({ entry, onToggle }: { entry: InboxItem; onToggle: () => void }) {
  const unread = isUnread(entry)
  if (!canMarkRead(entry) && !canMarkUnread(entry)) return <Box sx={{ flex: '0 0 auto', width: DOT_SLOT }} />
  return (
    <IconButton
      size="small"
      data-testid={`inbox-toggle-${entry.kind}-${entry.id}`}
      aria-label={unread ? 'Mark as read' : 'Mark as unread'}
      title={unread ? 'Mark as read' : 'Mark as unread'}
      onClick={event => { event.stopPropagation(); onToggle() }}
      /* Out of the tab order: a button inside a menu item is invisible to the menu's arrow keys, and 50 extra tab
         stops bury Mark all read. Keyboard and screen-reader users mark read or unread on the page (View all). */
      tabIndex={-1}
      sx={{
        flex: '0 0 auto', width: DOT_SLOT, height: DOT_SLOT, p: 0,
        '@media (hover: hover)': unread ? {} : { opacity: 0, '.MuiMenuItem-root:hover &, .MuiMenuItem-root.Mui-focusVisible &': { opacity: 1 } },
        [PHONE]: { width: 44, height: 44, mr: -1 },
      }}
    >
      <Box
        component="span"
        sx={{
          width: 10, height: 10, borderRadius: '50%', boxSizing: 'border-box',
          ...(unread ? { bgcolor: 'primary.main' } : { border: '1.5px solid', borderColor: 'text.secondary' }),
        }}
      />
    </IconButton>
  )
}
