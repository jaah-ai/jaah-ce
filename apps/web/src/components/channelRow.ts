/* One alert channel's line (`components/NotificationsPanel`): a wrapping flex row of name, who it delivers to,
 * then its buttons, with any note on a line of its own under them. `components/SlackConnect` and
 * `components/TelegramConnect` render flex ITEMS of that row (fragments), so their notes take this sx too. */

/** The row: buttons wrap under the name at phone width; a note takes a line of its own. */
export const CHANNEL_ROW_SX = {
  display: 'flex',
  alignItems: 'center',
  flexWrap: 'wrap',
  columnGap: 1,
  rowGap: 0.5,
  minHeight: '2.75rem',
  py: 0.5,
  borderTop: 1,
  borderColor: 'divider',
} as const

/** Who it delivers to: takes the slack and gives it up first (ellipsis), so the buttons stay on the row's end
 *  at phone width. */
export const CHANNEL_WHO_SX = {
  flex: '1 1 0',
  minWidth: 0,
  overflow: 'hidden',
  textOverflow: 'ellipsis',
  whiteSpace: 'nowrap',
} as const

/** A channel's name: one width on every row, so the who and the buttons line up down the list. */
export const CHANNEL_NAME_SX = { width: { xs: '4.5rem', sm: '5.5rem' }, flexShrink: 0, color: 'text.secondary' } as const

/** A note under the row (a warning, a code to send, an error): after every button, on its own line. */
export const CHANNEL_NOTE_SX = { flexBasis: '100%', order: 1 } as const

/** The who cell's id on a channel row: a host's Connect widget that renders the who names it the same way. */
export const channelWhoId = (idPrefix: string, type: string): string => `${idPrefix}-notif-${type}-id`

/** A row's who while nothing is linked yet: its Connect says the rest. */
export const NO_ID = '—'
