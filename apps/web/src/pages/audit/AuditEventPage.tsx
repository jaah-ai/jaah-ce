import { useMemo, useState, type JSX, type ReactNode } from 'react'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import Collapse from '@mui/material/Collapse'
import Link from '@mui/material/Link'
import Stack from '@mui/material/Stack'
import Typography from '@mui/material/Typography'
import { useQuery } from '@tanstack/react-query'
import { Link as RouterLink, useLocation, useParams } from 'react-router-dom'
import { apiDetail } from '../../api'
import { useAuth } from '../../auth'
import Breadcrumb from '../../components/Breadcrumb'
import { TabPanel, Tabs, type TabDef } from '../../components/Tabs'
import { useUrlTab } from '../../hooks/useUrlTab'
import Banner from '../../ui/Banner'
import { CopyButton } from '../../ui/CopyButton'
import LoadingState from '../../ui/LoadingState'
import StatusChip from '../../ui/StatusChip'
import { Mono } from '../../ui/DetailFields'
import DetailPage, { DetailField } from '../../ui/detail/DetailPage'
import { PHONE, monoSx } from '../../ui/listStyles'
import { customerUrl } from '../../lib/customers'
import { NO_VALUE } from '../../lib/relativeTime'
import { formatZoned, viewerZone } from '../../lib/zonedTime'
import { getAuditEvent } from './auditApi'
import { UNATTRIBUTED_LABEL, actionText, actorText, isUnattributed, namesSubject, rowSentence, subjectText } from './rows'
import { statusTone } from './status'
import { isSessionValue } from './activityFilters'
import type { AuditRow } from './types'
import { CodeBlock } from '../../ui/CodeBlock'

/* ONE ACTIVITY ROW, IN FULL (`/audit/events/:eventId`) — an editor in the list/detail pane beside Audit's
 * activity feed (`lib/listDetail.ts`), a full page below the dock width. The feed keeps what is scanned DOWN a
 * column; this keeps what is read ACROSS one event, including the redacted request/response bodies.
 * SEEDED FROM THE ROW THAT OPENED IT (`location.state.row`, `pages/audit/UsersTab.tsx`) and never refetched:
 * an audit row is immutable, so a second read could only disagree with the row clicked (AC7.3). A cold load
 * (a pasted link, a reload) reads it once by id.
 * Tabs by priority — Event · Request · Subject · Payload — and a tab with nothing to show is not offered.
 * Every JSON block stays DEFAULT-COLLAPSED: bodies are capped at 8 KB each.
 */

/** The event's address, as the feed opens it. */
export function auditEventPath(eventId: number): string {
  return `/audit/events/${eventId}`
}

type EventTab = 'event' | 'request' | 'subject' | 'payload'

const EVENT_TABS: readonly TabDef<EventTab>[] = [
  { id: 'event', label: 'Event' },
  { id: 'request', label: 'Request' },
  { id: 'subject', label: 'Subject' },
  { id: 'payload', label: 'Payload' },
]

/** A tab panel's fields: the `<dl>` `DetailField` pairs sit in, without a heading — the tab names it. Spaced as a
 *  `fields` `DetailSection`'s (`ui/detail/DetailPage.tsx`), so one pair never runs into the next.
 *  Every value sets at `body2`, and an inline value (`Mono`) takes that line height top-aligned: its own smaller
 *  mono baseline otherwise grows the line 2px past a `Typography` value's. On a phone
 *  the label column is one fixed width a long label ("Client-declared hops") wraps inside, and the value takes the
 *  rest, so every value starts in one column instead of after its own label. */
const FIELDS_SX = {
  m: 0,
  pt: 1.5,
  display: 'flex',
  flexDirection: 'column',
  gap: 1.75,
  minWidth: 0,
  '& dd': { typography: 'body2', '& > span.MuiTypography-body2': { lineHeight: 'inherit', verticalAlign: 'top' } },
  [PHONE]: {
    '& dt': { flex: '0 0 6.5rem', minWidth: 0 },
    '& dd': { flex: '1 1 0' },
  },
} as const

function Fields({ children }: { children: ReactNode }): JSX.Element {
  return <Box component="dl" sx={FIELDS_SX}>{children}</Box>
}

/** A secondary line under a value: the raw id behind a name, on ONE line — ellipsised where it does not fit (a
 *  UUID on a phone), whole in its tooltip and on the clipboard. */
function SecondaryId({ id, what }: { id: string; what: string }): JSX.Element {
  return (
    <Stack direction="row" sx={{ alignItems: 'center', gap: 0.5, minWidth: 0 }}>
      <Typography
        component="div"
        variant="caption"
        title={id}
        sx={[monoSx, { color: 'text.secondary', minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }]}
      >
        {id}
      </Typography>
      <CopyButton variant="icon" text={() => id} label={`Copy ${what}`} />
    </Stack>
  )
}

/** Characters of a nested value printed inline before stopping; the full text is one disclosure away. */
const INLINE_VALUE_MAX = 120

/** Payload keys with their own named disclosure, in order: the two captured bodies. */
const BODY_KEYS = ['request', 'response'] as const

/** A labelled, default-collapsed block: a plain button whose state a spec reads off `aria-expanded` (not
 *  MUI's `Accordion`). */
function Disclosure({
  label,
  testId,
  children,
}: {
  label: string
  testId: string
  children: ReactNode
}): JSX.Element {
  const [open, setOpen] = useState(false)
  return (
    <Box sx={{ mt: 1 }}>
      <Button
        size="small"
        variant="text"
        data-testid={testId}
        aria-expanded={open}
        onClick={() => setOpen(v => !v)}
        sx={{ px: 0.75, textTransform: 'none' }}
      >
        {open ? '▾' : '▸'} {label}
      </Button>
      <Collapse in={open} unmountOnExit>
        <Box data-testid={`${testId}-body`} sx={{ pt: 0.5 }}>
          {children}
        </Box>
      </Collapse>
    </Box>
  )
}

/** One payload entry inline: scalars as themselves, structures as compact JSON up to `INLINE_VALUE_MAX`
 *  (the full object is in the payload disclosure below). */
function payloadValue(value: unknown): { text: string; truncated: boolean } {
  if (value === null || value === undefined) return { text: NO_VALUE, truncated: false }
  const text = typeof value === 'string' ? value : JSON.stringify(value)
  const full = text ?? String(value)
  if (full.length <= INLINE_VALUE_MAX) return { text: full, truncated: false }
  return { text: `${full.slice(0, INLINE_VALUE_MAX)}…`, truncated: true }
}

/** The `X-Forwarded-For` hops the client wrote, or null. The right-most hop is the ALB's own, shown as Address
 *  (`client_ip`, `apps/api/app/request_caller.py`); a row without one (older, or no ALB) declares its whole chain. */
function declaredHops(data: AuditRow['data']): string | null {
  const chain = data.forwarded_for
  if (typeof chain !== 'string') return null
  const hops = typeof data.client_ip === 'string' ? chain.split(',').slice(0, -1).join(',') : chain
  return hops.trim() || null
}

/** Only for a request row: "—" in method/path/status would suggest a lost capture. */
function hasRequestFields(row: AuditRow): boolean {
  return row.path !== null || row.method !== null || row.status !== null
}

function EventView({ row, zone }: { row: AuditRow; zone: string }): JSX.Element {
  /* Payload keys other than the two bodies (which have their own disclosures). */
  const otherPayloadKeys = Object.keys(row.data)
    .filter(key => !(BODY_KEYS as readonly string[]).includes(key))
    .sort()
  const tabs = EVENT_TABS.filter(t =>
    (t.id !== 'request' || hasRequestFields(row))
    && (t.id !== 'subject' || row.subject !== null)
    && (t.id !== 'payload' || Object.keys(row.data).length > 0))
  const [tab, selectTab] = useUrlTab(tabs, 'event')
  /* The browser sign-in that made this write; the feed narrows to it by `?session=`. */
  const session = typeof row.data.console_session === 'string' && isSessionValue(row.data.console_session)
    ? row.data.console_session
    : null
  const declared = declaredHops(row.data)
  /* The operator is served every row's org, so none means the record belongs to no org; a tenant is served none. */
  const operator = useAuth().admin?.is_operator === true

  return (
    <>
      {tabs.length > 1 && <Tabs tabs={tabs} tab={tab} onSelect={selectTab} label="Event sections" idPrefix="audit-event" />}

      <TabPanel id="event" current={tab} idPrefix="audit-event" label={tabs.length > 1 ? undefined : 'Event'}>
        <Fields>
          <DetailField label="When">
            <Typography variant="body2">{formatZoned(row.created_at, zone, { seconds: true })}</Typography>
          </DetailField>
          {/* The person by name; their uuid is secondary — on hover and the copy button, never a line of its own. */}
          <DetailField label="Actor">
            <Stack direction="row" sx={{ alignItems: 'center', gap: 0.5, minWidth: 0 }}>
              <Typography
                variant="body2"
                data-testid="audit-event-actor"
                title={row.user_id ?? undefined}
                /* No one to name (an unknown address's refusal): muted, as the feed's actor slot is. */
                data-unattributed={isUnattributed(row) || undefined}
                sx={{ minWidth: 0, overflowWrap: 'anywhere', ...(isUnattributed(row) ? { color: 'text.disabled' } : null) }}
              >
                {actorText(row)}
              </Typography>
              {row.user_id !== null && <CopyButton variant="icon" text={() => row.user_id ?? ''} label="Copy user id" />}
            </Stack>
          </DetailField>
          {/* The kind (machine name, for grepping) under its label, as a raw id sits under its name. */}
          <DetailField label="Action">
            <Typography variant="body2">{actionText(row)}</Typography>
            <SecondaryId id={row.kind} what="kind" />
          </DetailField>
          <DetailField label="Type">
            <Typography variant="body2">{row.action_type}</Typography>
          </DetailField>
          {/* How the request ended, so a short event (a sign-in) reads on this tab alone; the Request tab keeps the
              method and path beside it. */}
          {row.status !== null && (
            <DetailField label="Result">
              <Box data-testid="audit-event-result" component="span">
                <StatusChip label={String(row.status)} tone={statusTone(row.status)} />
              </Box>
            </DetailField>
          )}
          {/* The org, named as other detail pages name one; served to the operator only (`types.ts::AuditRow.org`).
              Its uuid is secondary, as the Actor's is: on hover and the copy button. A row served no
              org reads as Unattributed, as the feed's org slot says. */}
          {operator && !row.org && (
            <DetailField label="Account">
              <Typography variant="body2" sx={{ color: 'text.secondary' }} data-testid="audit-event-account">
                {UNATTRIBUTED_LABEL}
              </Typography>
            </DetailField>
          )}
          {row.org && (
            <DetailField label="Account">
              <Stack direction="row" sx={{ alignItems: 'center', gap: 0.5, minWidth: 0 }}>
                <Link
                  component={RouterLink}
                  to={customerUrl(row.org.id)}
                  underline="hover"
                  variant="body2"
                  data-testid="audit-event-account"
                  title={row.org.id}
                  sx={{ minWidth: 0, overflowWrap: 'anywhere' }}
                >
                  {row.org.name}
                </Link>
                <CopyButton variant="icon" text={() => row.org?.id ?? ''} label="Copy account id" />
              </Stack>
            </DetailField>
          )}
          {session && (
            <DetailField label="Session">
              <Link
                component={RouterLink}
                to={`/audit?session=${session}`}
                underline="hover"
                variant="body2"
                data-testid="audit-event-session"
                sx={{ display: 'block' }}
              >
                All writes in this session
              </Link>
              <SecondaryId id={session} what="session id" />
            </DetailField>
          )}
          {typeof row.data.agent_session === 'string' && (
            <DetailField label="Agent session">
              <Mono>{row.data.agent_session}</Mono>
            </DetailField>
          )}
          {typeof row.data.user_agent === 'string' && (
            <DetailField label="Browser">
              <Typography variant="body2" sx={{ overflowWrap: 'anywhere' }}>{row.data.user_agent}</Typography>
            </DetailField>
          )}
          {typeof row.data.client_ip === 'string' && (
            <DetailField label="Address">
              <Mono>{row.data.client_ip}</Mono>
            </DetailField>
          )}
          {declared && (
            <DetailField label="Client-declared hops">
              <Mono>{declared}</Mono>
            </DetailField>
          )}
        </Fields>
      </TabPanel>

      {hasRequestFields(row) && (
        <TabPanel id="request" current={tab} idPrefix="audit-event">
          <Fields>
            <DetailField label="Method">
              <Typography variant="body2">{row.method ?? NO_VALUE}</Typography>
            </DetailField>
            <DetailField label="Path">
              {row.path ? <Mono>{row.path}</Mono> : <Typography variant="body2">{NO_VALUE}</Typography>}
            </DetailField>
            <DetailField label="Status">
              {row.status === null ? (
                <Typography variant="body2" data-testid="audit-event-status">
                  {NO_VALUE}
                </Typography>
              ) : (
                <Box data-testid="audit-event-status" component="span">
                  <StatusChip label={String(row.status)} tone={statusTone(row.status)} />
                </Box>
              )}
            </DetailField>
          </Fields>
        </TabPanel>
      )}

      {row.subject && (
        <TabPanel id="subject" current={tab} idPrefix="audit-event">
          <Fields>
            <DetailField label="Type">
              <Typography variant="body2">{row.subject.type}</Typography>
            </DetailField>
            <DetailField label="Id">
              <Mono>{row.subject.id}</Mono>
            </DetailField>
            <DetailField label="Name">
              {/* An unresolved label is stated as unresolved, never blank: "this object is gone" is an answer. */}
              <Typography variant="body2">
                {row.subject.label ?? 'not resolvable (deleted, or not visible to you)'}
              </Typography>
            </DetailField>
          </Fields>
        </TabPanel>
      )}

      {Object.keys(row.data).length > 0 && (
        <TabPanel id="payload" current={tab} idPrefix="audit-event">
          {otherPayloadKeys.length > 0 && (
            <Fields>
              {otherPayloadKeys.map(key => {
                const { text, truncated } = payloadValue(row.data[key])
                return (
                  <DetailField key={key} label={key}>
                    <Mono>{text}</Mono>
                    {truncated && (
                      <Typography component="span" variant="caption" sx={{ color: 'text.secondary', ml: 1 }}>
                        (truncated — see the full payload below)
                      </Typography>
                    )}
                  </DetailField>
                )
              })}
            </Fields>
          )}
          {/* The three JSON blocks, collapsed: `request`/`response` first, the whole payload last. */}
          {BODY_KEYS.map(key =>
            key in row.data ? (
              <Disclosure key={key} label={`${key} body`} testId={`audit-event-${key}`}>
                <CodeBlock maxHeight={320}>{JSON.stringify(row.data[key], null, 2)}</CodeBlock>
              </Disclosure>
            ) : null,
          )}
          <Disclosure label="Full payload (JSON)" testId="audit-event-payload">
            <CodeBlock maxHeight={320}>{JSON.stringify(row.data, null, 2)}</CodeBlock>
          </Disclosure>
        </TabPanel>
      )}
    </>
  )
}

export default function AuditEventPage(): JSX.Element {
  const params = useParams()
  const eventId = Number(params.eventId)
  const validId = Number.isInteger(eventId) && eventId > 0
  /* The row the feed was showing — only when it IS this event (a stale state on a moved URL is ignored). */
  const seeded = (useLocation().state as { row?: AuditRow } | null)?.row
  const initial = seeded && seeded.id === eventId ? seeded : undefined
  const zone = useMemo(viewerZone, [])
  const event = useQuery({
    queryKey: ['audit-event', eventId],
    queryFn: () => getAuditEvent(eventId),
    enabled: validId,
    initialData: initial,
    staleTime: Infinity,
    retry: false,
  })
  const row = event.data

  const breadcrumb = <Breadcrumb items={[{ label: 'Audit', to: '/audit' }, { label: `Event ${params.eventId ?? ''}` }]} />
  return (
    <DetailPage
      testId="audit-event"
      sx={{ minWidth: 0 }}
      breadcrumb={breadcrumb}
      /* Leads with the sentence, then its subject unless the sentence already names it (as the feed,
         `UsersTab.tsx`). The kind repeats the sentence in machine form, so it is the Action field's, not the
         header's. The event id is the breadcrumb's. */
      title={row && <Box component="span" data-testid="audit-event-sentence" sx={{ overflowWrap: 'anywhere' }}>{rowSentence(row)}</Box>}
      subtitle={row?.subject && !namesSubject(rowSentence(row), row.subject) && (
        <Typography component="span" variant="body2" data-testid="audit-event-subject" sx={{ display: 'block', overflowWrap: 'anywhere' }}>
          {subjectText(row.subject)}
        </Typography>
      )}
    >
      {!validId && <Banner severity="error">This event link is not valid.</Banner>}
      {validId && event.isPending && <LoadingState rows={4} label="Loading event" />}
      {event.isError && <Banner severity="error">{apiDetail(event.error, 'Could not load this event')}</Banner>}
      {/* Keyed: moving to another event starts its disclosures collapsed again. */}
      {row && <EventView key={row.id} row={row} zone={zone} />}
    </DetailPage>
  )
}
