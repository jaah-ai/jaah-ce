import { useRef, type JSX, type ReactNode } from 'react'
import { Link as RouterLink } from 'react-router-dom'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import Link from '@mui/material/Link'
import Stack from '@mui/material/Stack'
import Typography from '@mui/material/Typography'
import {
  DEFAULT_NOTIFICATION_EVENT,
  NOTIFICATION_EVENTS,
  NOTIFICATION_METHODS,
  NOTIFICATION_METHOD_TYPES,
} from '../lib/notificationPrefs'
import type {
  NotificationEventDetail,
  NotificationMethodType,
  UserNotificationsPreference,
} from '../types'
import { useAuth } from '../auth'
import { planSurfaceShown } from '../lib/planSwitches'
import { STICKY_TABS_HEIGHT_VAR } from './Tabs'
import Banner from '../ui/Banner'
import FormTextField from '../ui/FormTextField'
// 44px phone floors: `tapTargetSx` (switches, buttons).
import { ROW_ACTION_SX, tapTargetSx } from '../ui/listStyles'
import SegmentedControl, { type SegmentedOption } from '../ui/SegmentedControl'
import SettingCheckbox from '../ui/SettingCheckbox'
import { CHANNEL_NAME_SX, CHANNEL_NOTE_SX, CHANNEL_ROW_SX, CHANNEL_WHO_SX, channelWhoId, NO_ID } from './channelRow'

/* The notifications panel, shared by `/profile` and `/users/:id` (both edit `preferences.notifications`;
 * precedent: `components/UserSettingsFields.tsx`). Presentation only — no state, fetch or knowledge of
 * its host; the non-React half is `lib/notificationPrefs.ts`.
 * No credential picker: one deployment-wide credential per channel (Slack, Gmail);
 * `methodsAvailable` says which exist. Email has no id field: it delivers to the login email (`email`);
 * nor has Telegram: it delivers to the person's linked chat (`telegramConnect` links it).
 * Two lists, no prose (facts, not explanations). Channels: one line each — name, who it delivers to,
 * then Connect/Disconnect (Disconnect removes it); `components/channelRow.ts`. Events: one Off/Brief/Full switch
 * each, in two columns where they fit, plus one for all at once. With no channel the switches dim and lose their
 * fill (still editable), and the heading says nothing is sent, as a link to the Channels list.
 * With an email channel, each event also gets an Email box (`email`; a one-click unsubscribe clears it too): off,
 * that event is not emailed, while the other channels still send it.
 * Slack is a plan switch (`slack_enabled`): while the org's plan leaves it out (and the viewer is no operator,
 * `planSurfaceShown`), nothing of Slack shows — no add button, no channel row (a stored one stays in the value,
 * unlisted), no notice — and Slack cannot be added. */

/* One choice per event: Off, or which detail it sends. Stored as `enabled` + `detail` (`lib/notificationPrefs.ts`);
 * Off keeps the stored detail, so turning it back on restores it. */
type EventLevel = 'off' | NotificationEventDetail
const LEVEL_OPTIONS: SegmentedOption<EventLevel>[] = [
  { value: 'off', label: 'Off' },
  { value: 'brief', label: 'Brief' },
  { value: 'full', label: 'Full' },
]
const levelOf = (pref: { enabled: boolean; detail: NotificationEventDetail }): EventLevel => (pref.enabled ? pref.detail : 'off')

/* Two columns where 18rem each fit (the 48rem Alerts page), one on a phone; read down, then across. */
const EVENTS_LIST_SX = { columns: '18rem 2', columnGap: 3 } as const

/* An event's line. Its rule runs to the edge; the switch ends inset by the gutter (see `EVENTS_TABLE_GUTTER`).
 * Wraps: a long label beside the Email box and the switch outruns a phone's row, so the controls drop under it,
 * still on the right edge. */
const EVENT_ROW_SX = {
  display: 'flex',
  flexWrap: 'wrap',
  alignItems: 'center',
  justifyContent: 'space-between',
  columnGap: 1,
  minHeight: '2.5rem',
  borderTop: 1,
  borderColor: 'divider',
  breakInside: 'avoid',
} as const

/* The right gutter of every channel and event row, inside it so the rules still run to the edge. Exported: a
 * host's Save ends on the same edge (`pages/ProfilePage.tsx`). */
export const EVENTS_TABLE_GUTTER = 2

/* Muted (no channel), the switches dim and lose the live fill: nothing is sent, so none may look on. The chosen
 * one keeps full-strength bold text so the stored choice stays legible. */
const MUTED_LEVEL_SX = [tapTargetSx, { opacity: 0.6, '&.Mui-selected:not(.Mui-disabled)': { color: 'text.primary', fontWeight: 600 } }]

/** Where an operator sets a channel up: a link, kept on one line. */
const IntegrationsLink = (): JSX.Element => (
  <Link component={RouterLink} to="/settings?area=integrations" data-testid="notif-integrations-link" sx={{ whiteSpace: 'nowrap' }}>
    Settings › Integrations
  </Link>
)

/** Unconfigured-channel notice: a stored method that cannot deliver (a saved id looks working). Take the
 *  channel's label, not "Slack". Name "an operator", never "you".
 *  Only the operator (`is_operator`, never the superadmin flag) is told "Settings" — the phrase is
 *  on `app/tenancy/copy.py`'s never-served list. */
const setsIt = (type: NotificationMethodType, label: string, isOperator: boolean): ReactNode => {
  // Telegram's fix is a bot the operator picks, not a credential the reader would look for.
  const fix = type === 'telegram' ? 'An operator picks a bot' : 'An operator sets the credential'
  return isOperator ? <>{fix} in <IntegrationsLink /> › {label}.</> : `${fix}.`
}
const unavailableWithMethod = (type: NotificationMethodType, label: string, isOperator: boolean): ReactNode =>
  <>{label} delivery is unconfigured, so this channel delivers nothing. {setsIt(type, label, isOperator)}</>

/** Names the consequence of an empty list: a mute account. */
const NO_METHODS = 'No channel — nothing is delivered.'

/** `NO_METHODS`'s stand-in when the deployment offers no channel and nothing above says so: why there is no Add. */
const NO_CHANNELS = 'No channel is set up — nothing is delivered.'

/** Beside the Events heading while there is no channel: the choices below are kept, not sent. */
const EVENTS_MUTED = 'Not sent until a channel is added.'

/** `EVENTS_MUTED`'s stand-in when nothing can be added: names who sets a channel up. Only the operator is told
 *  where (see `setsIt`). */
const eventsNoChannel = (isOperator: boolean): ReactNode =>
  isOperator
    ? <>Not sent until an operator sets up a channel in <IntegrationsLink />.</>
    : 'Not sent until an operator sets up a channel.'


/** A Telegram row's who where the panel cannot read the linked name (no `telegramConnect`). */
const TELEGRAM_CHAT = 'Chat'

/** Email delivers only to an SSO-verified address; names the one fix and where. No link: `/login` sends a
 *  signed-in person home, and Google's button lives only there. */
const EMAIL_UNVERIFIED = 'Not verified — on the sign-in page, use Google, Microsoft or GitHub once.'

// Default export only: one import spelling for one component.
function NotificationsPanel({
  value,
  onChange,
  methodsAvailable,
  events,
  disabled = false,
  idPrefix,
  email,
  emailVerified,
  slackConnect,
  telegramConnect,
  telegramEnable,
  telegramEnableOffered = false,
}: {
  /** The complete document (every event), hydrated by `lib/notificationPrefs.readNotifications`. */
  value: UserNotificationsPreference
  /** Called with the whole next document. */
  onChange: (next: UserNotificationsPreference) => void
  /**
   * Channels this deployment can deliver to (`notification_methods_available`,
   * `app/notify_targets.available_notification_methods`). Gates the add affordance only: an existing
   * method on an unavailable channel still renders and can be edited or removed. A list, intersected
   * with `NOTIFICATION_METHOD_TYPES`, so a newer API cannot paint a nameless row.
   */
  methodsAvailable: readonly string[]
  /**
   * The events this user may see and set (`notification_events`, `app/preferences.subscribable_events`); the rest
   * are not drawn, and the bulk switch leaves them alone. Absent (an API that predates the field) draws every one.
   */
  events?: readonly string[]
  /**
   * Locks every control while the host saves. A silent-loss guard: both hosts re-seed the draft on save,
   * which would destroy keystrokes typed in between.
   */
  disabled?: boolean
  /** Namespaces control ids per surface (`'profile'`, `'user'`). */
  idPrefix: string
  /** The recipient's login email — where an email method delivers. Absent → "Login email". */
  email?: string | null
  /** Whether `email` is SSO-verified. Only `false` shows the caption; absent says nothing. */
  emailVerified?: boolean
  /** `/profile`'s `components/SlackConnect` (self-service only). The Slack id is read-only unless the viewer
   *  is the operator on a surface without Connect: only the operator may type one
   *  (`app/preferences.validate_notifications_preferences`); a user proves theirs by Connect. */
  slackConnect?: ReactNode
  /** `/profile`'s `components/TelegramConnect` (self-service only): the Telegram row's who and buttons. */
  telegramConnect?: ReactNode
  /** `/profile`'s `TelegramEnableHere`, shown while this membership has no Telegram method (the person's chat
   *  may be linked from another org). */
  telegramEnable?: ReactNode
  /** `telegramEnable` is showing its Turn on: Add Telegram is withheld, so one chat has one way on. */
  telegramEnableOffered?: boolean
}): JSX.Element {
  const { admin } = useAuth()
  const methodsRef = useRef<HTMLElement>(null)
  const isOperator = admin?.is_operator === true
  /* Off the plan, Slack's add button, its unconfigured banner and its row are not drawn. */
  const offPlan = (type: NotificationMethodType) => type === 'slack' && !planSurfaceShown(admin, 'slack_enabled')
  /* A tenant's Slack is its org's workspace: `methodsAvailable` leaves Slack out until an org admin
   * connects one, yet Profile still offers the row, where `slackConnect` says who connects it. */
  const slackPerOrg = !!slackConnect && !isOperator && admin?.tenant !== false
  const available = (type: NotificationMethodType) => methodsAvailable.includes(type) || (type === 'slack' && slackPerOrg)
  /* Available, renderable, not already present, on the plan, and not already offered by Turn on. Empty → the
   * add affordance renders nothing. */
  const addable = NOTIFICATION_METHOD_TYPES.filter(
    type => available(type) && !value.methods.some(method => method.type === type) && !offPlan(type)
      && !(type === 'telegram' && telegramEnableOffered),
  )

  /* Stored methods on a channel this deployment cannot deliver to; one warning each. A channel nobody chose is
   * news to nobody here, the operator included: Settings › Integrations is where channels are set up. */
  const usesMethod = (type: NotificationMethodType) => value.methods.some(method => method.type === type)
  const unavailable = NOTIFICATION_METHOD_TYPES.filter(
    type => !available(type) && !offPlan(type) && usesMethod(type),
  )
  /* The rows drawn: a stored method the plan leaves out stays in `value`, unlisted. */
  const shownMethods = value.methods.filter(method => !offPlan(method.type))
  /* Slack's id is read-only unless the operator types one: a user proves theirs by Connect, on the row. */
  const slackReadOnly = !!slackConnect || !isOperator
  /* Telegram's row is `telegramConnect`'s (who it is linked as, Connect, Disconnect) wherever it can link. */
  const telegramShown = !!telegramConnect && methodsAvailable.includes('telegram')
  /* No method: the events below deliver nothing, so they must not look switched on. */
  const muted = shownMethods.length === 0
  /* Each event's Email box shows only while email is a channel: without one there is nothing to stop. */
  const emailShown = usesMethod('email')
  /* No add button, notice or banner says why the list is empty: the empty state names it. */
  const nothingOffered = addable.length === 0 && unavailable.length === 0
    && !telegramEnableOffered

  const addMethod = (type: NotificationMethodType) =>
    onChange({ ...value, methods: [...value.methods, { type, user_id: '' }] })

  const removeMethod = (type: NotificationMethodType) =>
    onChange({ ...value, methods: value.methods.filter(method => method.type !== type) })

  /* Edited by type, not index: at most one method per type (server dedups/refuses). */
  const setMethodId = (type: NotificationMethodType, userId: string) =>
    onChange({
      ...value,
      /* A typed id drops the stored name: it named the old id, and the server refuses it on a new one. */
      methods: value.methods.map(method => (
        method.type === type ? { ...method, user_id: userId, display_name: undefined } : method
      )),
    })

  /* Spread all events, including keys this build does not render (`readNotifications` keeps them so a
     save cannot delete a newer tab's choice). */
  const patchEvent = (event: string, changes: { enabled?: boolean; detail?: NotificationEventDetail; email?: boolean }) => {
    const current = value.events[event] ?? DEFAULT_NOTIFICATION_EVENT
    onChange({ ...value, events: { ...value.events, [event]: { ...current, ...changes } } })
  }

  /* The Events caption's link: the Channels list scrolls in and its first Add takes focus (the heading when
     there is none), so the next key press adds one. */
  const goToChannels = () => {
    const section = methodsRef.current
    if (!section) return
    section.scrollIntoView({ behavior: 'smooth', block: 'start' })
    const target = section.querySelector<HTMLElement>('button:not(:disabled)') ?? section.querySelector<HTMLElement>('h2')
    if (target === null) return
    if (target.tagName === 'H2') target.tabIndex = -1
    target.focus({ preventScroll: true })
  }

  const setLevel = (event: string, level: EventLevel) =>
    patchEvent(event, level === 'off' ? { enabled: false } : { enabled: true, detail: level })

  /* The rows this user may see (`events`); the others keep their stored choice untouched. */
  const shown = events === undefined ? NOTIFICATION_EVENTS : NOTIFICATION_EVENTS.filter(({ event }) => events.includes(event))
  /* The heading's bulk switch: the level every shown event shares, or none chosen while they differ. */
  const levels = new Set(shown.map(({ event }) => levelOf(value.events[event] ?? DEFAULT_NOTIFICATION_EVENT)))
  const allLevel: EventLevel | 'mixed' = levels.size === 1 ? [...levels][0] : 'mixed'
  const setAllEvents = (level: EventLevel) => {
    const next = { ...value.events }
    for (const { event } of shown) {
      const current = next[event] ?? DEFAULT_NOTIFICATION_EVENT
      next[event] = level === 'off' ? { ...current, enabled: false } : { ...current, enabled: true, detail: level }
    }
    onChange({ ...value, events: next })
  }

  return (
    <>
      {/* Its scroll margin clears the host's sticky tab strip (`STICKY_TABS_HEIGHT_VAR`, the strip's measured height) when
          the Events caption scrolls it in, with a breath of space so the Add buttons' tops show. */}
      <Box ref={methodsRef} data-testid="notif-methods" sx={{ scrollMarginTop: `calc(var(${STICKY_TABS_HEIGHT_VAR}, 0px) + 0.75rem)` }}>
        {/* Add sits on the heading row so it never moves. One button per addable channel (one click, no
            menu); hidden when nothing is addable. */}
        <Stack
          direction="row"
          sx={{ alignItems: 'center', justifyContent: 'space-between', gap: 1, mb: 0.5 }}
        >
          <Typography variant="subtitle2" component="h2" sx={{ fontWeight: 600 }}>Channels</Typography>
          {/* Wraps at phone width: three channels' buttons outrun a 360px row. */}
          {addable.length > 0 && (
            <Stack direction="row" spacing={0.5} useFlexGap sx={{ flexWrap: 'wrap', justifyContent: 'flex-end' }}>
              {addable.map(type => (
                <Button
                  key={type}
                  id={`${idPrefix}-notif-add-${type}`}
                  variant="outlined"
                  disabled={disabled}
                  sx={ROW_ACTION_SX}
                  onClick={() => addMethod(type)}
                >
                  Add {NOTIFICATION_METHODS[type].label}
                </Button>
              ))}
            </Stack>
          )}
        </Stack>

        {/* Above the list: it also answers "why can I not add one". */}
        {unavailable.map(type => (
          <Banner key={type} severity="warning">
            {unavailableWithMethod(type, NOTIFICATION_METHODS[type].label, isOperator)}
          </Banner>
        ))}
        {methodsAvailable.includes('telegram') && !usesMethod('telegram') && telegramEnable}

        {muted ? (
          /* `mt: 1`: the channel list's own offset, so it never sits flush under a Turn on banner. */
          <Typography variant="body2" sx={{ color: 'text.secondary', mt: 1 }}>
            {nothingOffered ? NO_CHANNELS : NO_METHODS}
          </Typography>
        ) : (
          <Box sx={{ mt: 1 }}>
            {shownMethods.map(method => {
              const meta = NOTIFICATION_METHODS[method.type]
              const whoId = channelWhoId(idPrefix, method.type)
              /* Disconnect removes the channel. A linked Telegram's is `telegramConnect`'s own: it also unlinks. */
              const disconnect = (
                <Button
                  size="small"
                  variant="outlined"
                  color="error"
                  sx={tapTargetSx}
                  aria-label={`Disconnect ${meta.label}`}
                  disabled={disabled}
                  onClick={() => removeMethod(method.type)}
                >
                  Disconnect
                </Button>
              )
              /* Telegram's who and buttons are `telegramConnect`'s: only it knows the linked name. */
              if (method.type === 'telegram' && telegramShown) {
                return (
                  <Box key={method.type} sx={{ ...CHANNEL_ROW_SX, pr: EVENTS_TABLE_GUTTER }} data-testid={`notif-channel-${method.type}`}>
                    <Typography variant="body2" sx={CHANNEL_NAME_SX}>{meta.label}</Typography>
                    {telegramConnect}
                  </Box>
                )
              }
              return (
                /* One line: name, who it delivers to, buttons; a note wraps under them (`channelRow.ts`). */
                <Box key={method.type} sx={{ ...CHANNEL_ROW_SX, pr: EVENTS_TABLE_GUTTER }} data-testid={`notif-channel-${method.type}`}>
                  <Typography variant="body2" sx={CHANNEL_NAME_SX}>{meta.label}</Typography>
                  {method.type === 'slack' && slackReadOnly ? (
                    /* Read-only: the bot stores the proven id and its display name (`app/slack_link.py`); a link
                       made before names were read shows the id. `title` keeps the id a hover away. */
                    /* `aria-live`: Connect fills it in, the one sign the link worked. */
                    <Typography
                      id={whoId}
                      variant="body2"
                      aria-live="polite"
                      title={method.display_name ? method.user_id : undefined}
                      sx={[CHANNEL_WHO_SX, { color: method.user_id ? 'text.primary' : 'text.disabled' }]}
                    >
                      {method.display_name ? `@${method.display_name}` : method.user_id || NO_ID}
                    </Typography>
                  ) : meta.field ? (
                    /* No client-side format check: the id shape (`^[UW][A-Z0-9]{2,32}$`) is the server's, and its
                       422 names the field. The hint names whom a Connect-linked id reaches; typing a new id drops
                       it (`setMethodId`). `maxWidth` keeps a 9-13 character id's field short. */
                    <Box sx={{ ...CHANNEL_WHO_SX, flexBasis: '10rem', overflow: 'visible' }}>
                      <Box sx={{ maxWidth: '17rem' }}>
                        <FormTextField
                          id={whoId}
                          label={meta.field.label}
                          type="text"
                          disabled={disabled}
                          placeholder={meta.field.placeholder}
                          hint={method.display_name ? `@${method.display_name}` : undefined}
                          value={method.user_id}
                          onChange={v => setMethodId(method.type, v)}
                        />
                      </Box>
                    </Box>
                  ) : method.type === 'telegram' ? (
                    /* No Connect here (another person's row, or Telegram unconfigured): the bot adds the method on
                       linking, so a stored one is a linked chat, whose name only that person's Connect reads. */
                    <Typography id={whoId} variant="body2" sx={CHANNEL_WHO_SX}>
                      {TELEGRAM_CHAT}
                    </Typography>
                  ) : (
                    /* The address it delivers to; `title` shows it whole where the row cuts it short. */
                    <Typography id={whoId} variant="body2" title={email || undefined} sx={CHANNEL_WHO_SX}>
                      {email || 'Login email'}
                    </Typography>
                  )}
                  {/* Unconfigured, Connect could link nothing that delivers. */}
                  {method.type === 'slack' && !unavailable.includes('slack') && slackConnect}
                  {disconnect}
                  {method.type === 'email' && emailVerified === false && (
                    <Typography variant="caption" component="div" sx={[CHANNEL_NOTE_SX, { color: 'warning.main' }]}>
                      {EMAIL_UNVERIFIED}
                    </Typography>
                  )}
                </Box>
              )
            })}
            {/* The last row's rule, so the list ends on a line as the events below begin on one. */}
            <Box sx={{ borderTop: 1, borderColor: 'divider' }} />
          </Box>
        )}
      </Box>

      <Box>
        {/* Muted, the heading row says why: a dimmed switch still reads as on. */}
        <Stack
          direction="row"
          sx={{ alignItems: 'center', gap: 1, mb: 0.75, flexWrap: 'wrap', pr: EVENTS_TABLE_GUTTER }}
        >
          <Typography variant="subtitle2" component="h2" sx={{ fontWeight: 600 }}>Events</Typography>
          {/* With nothing to add, a link to Channels would lead nowhere: the caption names who can fix it. */}
          {muted && (nothingOffered ? (
            <Typography variant="caption" sx={{ color: 'text.secondary' }}>{eventsNoChannel(isOperator)}</Typography>
          ) : (
            <Link component="button" type="button" variant="caption" underline="always" onClick={goToChannels}>
              {EVENTS_MUTED}
            </Link>
          ))}
          {/* Every event at once; none chosen while they differ. */}
          <Stack direction="row" sx={{ alignItems: 'center', gap: 1, ml: 'auto' }}>
            <Typography variant="caption" sx={{ color: 'text.secondary' }}>All</Typography>
            <SegmentedControl<EventLevel | 'mixed'>
              value={allLevel}
              options={LEVEL_OPTIONS}
              ariaLabel="All events"
              testId="notif-all-events"
              disabled={disabled}
              color={muted ? undefined : 'primary'}
              buttonSx={muted ? MUTED_LEVEL_SX : tapTargetSx}
              onChange={next => { if (next !== 'mixed') setAllEvents(next) }}
            />
          </Stack>
        </Stack>
        <Box role="group" aria-label="Events" data-muted={muted ? 'true' : undefined} sx={EVENTS_LIST_SX}>
          {shown.map(({ event, label }) => {
            /* `??` for a hand-built host object; same default as `readNotifications`. */
            const pref = value.events[event] ?? DEFAULT_NOTIFICATION_EVENT
            const level = levelOf(pref)
            return (
              <Box key={event} sx={{ ...EVENT_ROW_SX, pr: EVENTS_TABLE_GUTTER }} data-testid={`notif-event-${event}`}>
                <Typography
                  variant="body2"
                  component="div"
                  sx={{ color: disabled || level === 'off' ? 'text.secondary' : 'text.primary' }}
                >
                  {label}
                </Typography>
                {/* The controls stay together, so the switches line up down the column whether or not a row wraps. */}
                <Stack direction="row" sx={{ alignItems: 'center', gap: 0.5, ml: 'auto' }}>
                  {emailShown && (
                    /* Off, this event is not emailed; Off on the switch already stops it everywhere. */
                    <Stack direction="row" sx={{ alignItems: 'center' }}>
                      <Typography
                        component="label"
                        htmlFor={`${idPrefix}-notif-email-${event}`}
                        variant="caption"
                        sx={{ color: 'text.secondary' }}
                      >
                        Email
                      </Typography>
                      <SettingCheckbox
                        id={`${idPrefix}-notif-email-${event}`}
                        name={`notif-email-${event}`}
                        label={`Email: ${label}`}
                        labelHidden
                        testId={`notif-event-email-${event}`}
                        checked={pref.email}
                        disabled={disabled || level === 'off'}
                        onChange={checked => patchEvent(event, { email: checked })}
                      />
                    </Stack>
                  )}
                  {/* `primary` (filled, `theme.ts`): MUI's ~8% grey selected tint is unreadable down a column. */}
                  <SegmentedControl
                    value={level}
                    options={LEVEL_OPTIONS}
                    ariaLabel={label}
                    disabled={disabled}
                    color={muted ? undefined : 'primary'}
                    buttonSx={muted ? MUTED_LEVEL_SX : tapTargetSx}
                    onChange={next => setLevel(event, next)}
                  />
                </Stack>
              </Box>
            )
          })}
        </Box>
      </Box>
    </>
  )
}

export default NotificationsPanel
