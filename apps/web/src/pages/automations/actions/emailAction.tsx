import { useQuery } from '@tanstack/react-query'
import Box from '@mui/material/Box'
import Link from '@mui/material/Link'
import { Link as RouterLink } from 'react-router-dom'
import { FormTextField } from '../../../ui/FormTextField'
import { EMAIL_SETTINGS_URL, getOrgEmail, ORG_EMAIL_KEY } from '../../settings/email/api'
import type { ActionContext, ActionEntry, ActionFormProps, ActionReviewRow } from './registry'

/* SEND EMAIL: one message from the org's connected Gmail each time the automation runs. Every rule is
 * the server's (`app/automation_email.py`): placeholders, recipient parsing, the connection check. This entry is the
 * form and its crossing to `action.params`. */

export type EmailActionValue = { connection_id: string | null; to: string; cc: string; subject: string; body: string }

/** The fields every run fills (`automation_email.OWN_FIELDS`). */
const OWN_PLACEHOLDERS = '{{automation.name}}, {{fired_at}}, {{org.name}}'

const str = (value: unknown): string => (typeof value === 'string' ? value : '')

export function emailValueOf(params: Record<string, unknown>): EmailActionValue {
  return {
    connection_id: typeof params.connection_id === 'string' && params.connection_id !== '' ? params.connection_id : null,
    to: str(params.to),
    cc: str(params.cc),
    subject: str(params.subject),
    body: str(params.body),
  }
}

/** On an event, an address a placeholder fills is chosen by whoever sends the event: say so where it is typed. */
function eventAddressed(recipients: string, context: ActionContext): string | undefined {
  return context.whenKind === 'event' && /\{\{/.test(recipients)
    ? 'An event fills this address: whoever sends the event picks the recipient.'
    : undefined
}

function placeholderHint(context: ActionContext): string {
  const own = `Placeholders: ${OWN_PLACEHOLDERS}.`
  return context.templateHint ? `${own} ${context.templateHint}` : own
}

function EmailActionForm({ value, onChange, context }: ActionFormProps<EmailActionValue>) {
  const orgId = context.orgId
  const query = useQuery({
    queryKey: [...ORG_EMAIL_KEY, orgId],
    queryFn: () => getOrgEmail(orgId as string),
    enabled: orgId !== null,
  })
  const connections = query.data?.connections ?? []
  const errors = context.errors ?? {}
  const set = (patch: Partial<EmailActionValue>) => onChange({ ...value, ...patch })
  const hint = placeholderHint(context)
  const noConnection = query.isSuccess && connections.length === 0
  const stored = value.connection_id !== null && !connections.some(c => c.id === value.connection_id)

  return (
    <Box sx={{ display: 'flex', flexDirection: 'column', gap: 2 }}>
      {noConnection ? (
        <Box data-testid="automation-email-no-connection" sx={{ color: 'text.secondary', fontSize: '0.875rem' }}>
          No Gmail connected.{' '}
          <Link component={RouterLink} to={EMAIL_SETTINGS_URL}>Connect Gmail</Link>
        </Box>
      ) : (
        <FormTextField
          label="From"
          id="automation-email-from"
          testId="automation-email-from"
          value={value.connection_id ?? ''}
          onChange={v => set({ connection_id: v === '' ? null : v })}
          disabled={context.disabled}
          error={errors.connection_id}
          hint={stored ? 'This mailbox was disconnected. Pick another.' : undefined}
          slotProps={{ inputLabel: { shrink: true } }}
          fullWidth
          // One connection: it is the empty value's own label, so the stored params stay "the org's only one".
          options={connections.length === 1
            ? [{ value: value.connection_id === connections[0].id ? connections[0].id : '', label: connections[0].address }]
            : connections.map(c => ({ value: c.id, label: c.address }))}
        >
          {connections.length !== 1 && <option value="">Pick a mailbox</option>}
          {stored && <option value={value.connection_id ?? ''}>Disconnected mailbox</option>}
        </FormTextField>
      )}
      <FormTextField
        label="To"
        id="automation-email-to"
        testId="automation-email-to"
        required
        value={value.to}
        onChange={to => set({ to })}
        disabled={context.disabled}
        error={errors.to}
        hint={eventAddressed(value.to, context) ?? 'Separate addresses with commas.'}
        placeholder="ops@example.com"
        fullWidth
      />
      <FormTextField
        label="Cc"
        id="automation-email-cc"
        testId="automation-email-cc"
        value={value.cc}
        onChange={cc => set({ cc })}
        disabled={context.disabled}
        error={errors.cc}
        hint={eventAddressed(value.cc, context)}
        fullWidth
      />
      <FormTextField
        label="Subject"
        id="automation-email-subject"
        testId="automation-email-subject"
        required
        value={value.subject}
        onChange={subject => set({ subject })}
        disabled={context.disabled}
        error={errors.subject}
        placeholder="{{automation.name}} ran"
        fullWidth
      />
      <FormTextField
        label="Body"
        id="automation-email-body"
        testId="automation-email-body"
        required
        multiline
        minRows={5}
        value={value.body}
        onChange={body => set({ body })}
        disabled={context.disabled}
        error={errors.body}
        hint={hint}
        fullWidth
      />
    </Box>
  )
}

function reviewRows(value: EmailActionValue): ActionReviewRow[] {
  const rows: ActionReviewRow[] = [{ label: 'To', value: value.to }]
  if (value.cc.trim()) rows.push({ label: 'Cc', value: value.cc })
  rows.push({ label: 'Subject', value: value.subject }, { label: 'Body', value: value.body })
  return rows
}

export const emailAction: ActionEntry<EmailActionValue> = {
  type: 'email.send',
  Form: EmailActionForm,
  initial: () => ({ connection_id: null, to: '', cc: '', subject: '', body: '' }),
  fromParams: params => emailValueOf(params),
  toParams: value => ({ ...value }),
  summary: (params, label) => {
    const to = str(params.to)
    return to ? `${label ?? 'Send email'} · ${to}` : (label ?? 'Send email')
  },
  brief: params => str(params.to) || 'Send email',
  reviewRows,
}
