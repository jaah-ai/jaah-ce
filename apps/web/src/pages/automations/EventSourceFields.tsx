import { createContext, type ReactNode, useContext, useEffect, useRef, useState } from 'react'
import { Link as RouterLink } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import Link from '@mui/material/Link'
import Stack from '@mui/material/Stack'
import TextField from '@mui/material/TextField'
import Typography from '@mui/material/Typography'
import { apiDetail, ApiError } from '../../api'
import { danger, type useConfirm } from '../../components/ConfirmDialog'
import { useSubmit } from '../../hooks/useSubmit'
import Banner from '../../ui/Banner'
import FormBox from '../../ui/FormBox'
import { CopyButton } from '../../ui/CopyButton'
import { CopyableValue } from '../../ui/CopyableValue'
import CredentialSelect from '../../ui/CredentialSelect'
import { credentialMark } from '../../ui/credentialMarks'
import { DetailSection } from '../../ui/detail/DetailPage'
import { Mono } from '../../ui/DetailFields'
import FormField from '../../ui/FormField'
import FormTextField, { type FormOption } from '../../ui/FormTextField'
import type { Explain, HelpProp } from '../../ui/explain'
import { HelpLabel } from '../../ui/HelpLabel'
import { KeyValue, KeyValueList } from '../../ui/KeyValueList'
import { fieldTapTargetSx, monoSx, tapTargetSx } from '../../ui/listStyles'
import SavedNote from '../../ui/SavedNote'
import SettingCheckbox from '../../ui/SettingCheckbox'
import { loadCredentialOptions } from '../credentials/api'
import { API_KEYS_URL } from '../settings/apiKeys/api'
import { EMAIL_ADDRESSES_URL, getOrgEmail, ORG_EMAIL_KEY, type OrgEmail } from '../settings/email/api'
import { capitalize } from '../../lib/humanize'
import { SEVERITIES } from '../../lib/productionQa'
import { isRecord } from '../../lib/records'
import { SIGNUP_ACCOUNT_TYPE_LABELS, SIGNUP_PROVIDER_LABELS } from '../../lib/signupLabels'
import { splitList, strOrNull } from '../../lib/strings'
import { wholeNumber } from '../../lib/wholeNumber'
import type { Forge } from '../../types'
import { githubEventLabel, gitlabEventLabel, HTTP_SOURCE_KINDS } from '../triggers/vocabulary'
import { str } from './vocabulary'
import SignatureCheckDialog from './SignatureCheck'
import {
  connectedGithubRepos,
  revealAutomationSecret,
  rotateAutomationSecret,
  runAutomation,
  setAutomationSecret,
  testRenderAutomation,
  type Automation,
  type EventWhen,
} from './api'
import { CodeBlock } from '../../ui/CodeBlock'
import RichTextRenderer from '../../components/richtext/RichTextRenderer'
import { FONT_WEIGHT, RADIUS, SIZE, grid } from '../../theme'

/* An event Automation's When — the source's settings, its preflight, limits and auto-pause (lifted from the
 * retired trigger editor), plus the Setup (where a sender posts, the one-time secret) and Test panels.
 *
 * THE DRAFT IS STRINGS: every number field holds what was typed, and `eventWhenOf` turns the draft into the wire
 * `when` or refuses with a sentence — a cleared number never reaches the server as `NaN`/`null` by accident.
 * The source kind is picked by the editor's When control; this form only draws that kind's fields. Everything one
 * kind owns — its config read and written, its template namespaces, its fields — is its `SOURCE_KINDS` entry. */

export const PREFLIGHT_LABELS: Record<string, string> = { none: 'None', script: 'Script', model: 'Model' }

/** A `user_registered` source's remaining enumerated values (provider and account type: `lib/signupLabels`). */
export const USER_SCOPE_LABELS: Record<string, string> = { org: 'Your members', platform: 'Sign-ups on Jaah' }
export const USER_MOMENT_LABELS: Record<string, string> = { applied: 'Application submitted', approved: 'Organization approved' }

export const SEVERITY_LABELS: Record<string, string> = {
  debug: 'Debug', info: 'Info', warning: 'Warning', error: 'Error', critical: 'Critical',
}
/** An `incident` source's `types`; none ticked is every type. */
export const INCIDENT_TYPE_LABELS: Record<string, string> = {
  monitor: 'Monitor alert', regression: 'Regression', lane_slowdown: 'Merge-lane slowdown',
}
const QUIET_START = '22:00'
const QUIET_END = '08:00'
const HH_MM = /^([01]\d|2[0-3]):[0-5]\d$/
const localTimeZone = () => Intl.DateTimeFormat().resolvedOptions().timeZone

/** A webhook source's `auth`. Only `api_key` is offered: `hmac`/`bearer` are legacy secrets a saved source keeps until
 *  its owner switches it, and the server refuses a switch to either. */
export const WEBHOOK_AUTH_LABELS: Record<string, string> = { api_key: 'API key', hmac: 'HMAC signature', bearer: 'Bearer token' }
const isLegacyWebhookAuth = (auth: string) => auth === 'hmac' || auth === 'bearer'
/** A saved webhook source's `auth` (`trigger_secrets.webhook_auth`): one saved before `auth` existed is HMAC-signed.
 *  Empty for any other kind. */
const savedWebhookAuth = (when: EventWhen) => (when.source_kind === 'webhook' ? str(when.source_config?.auth) || 'hmac' : '')

/** A webhook's `shape` (`app/trigger_config.py`): `event` files a task; `incident` only records the generic incident
 *  JSON under the target project's Incidents. */
export const WEBHOOK_SHAPE_LABELS: Record<string, string> = { event: 'Any JSON', incident: 'Incident' }
/** The keys an incident-shaped body carries. */
const INCIDENT_KEYS = 'fingerprint, title, severity, environment, count, release, commit, stack_frames, action'

/** The monitor sources (`app/triggers.py::MONITOR_SOURCE_KINDS`): each only records incidents. */
export const MONITOR_SOURCE_KINDS: ReadonlySet<string> = new Set(['sentry', 'datadog', 'pagerduty', 'cloudwatch'])
/** Monitors whose vendor mints the signing secret, pasted here (`app/triggers.py::VENDOR_SECRET_KINDS`). */
const VENDOR_SECRET_LABELS: Record<string, string> = { sentry: 'Sentry Client Secret', pagerduty: 'PagerDuty signing secret' }
/** The Datadog webhook payload to paste (`app/production_qa/adapters/datadog.py` reads exactly these keys). */
export const DATADOG_PAYLOAD = `{
  "id": "$ID",
  "aggreg_key": "$AGGREG_KEY",
  "transition": "$ALERT_TRANSITION",
  "priority": "$ALERT_PRIORITY",
  "title": "$EVENT_TITLE",
  "date": "$DATE",
  "link": "$LINK",
  "tags": "$TAGS",
  "alert_id": "$ALERT_ID",
  "scope": "$ALERT_SCOPE",
  "hostname": "$HOSTNAME"
}`
/** The PagerDuty event types the adapter reads. */
const PAGERDUTY_EVENTS = ['incident.triggered', 'incident.resolved', 'incident.reopened']

/** A source that only records incidents, filing no task: a monitor, or a webhook shaped as an incident. */
export const recordsOnly = (draft: EventDraft) =>
  MONITOR_SOURCE_KINDS.has(draft.sourceKind) || (draft.sourceKind === 'webhook' && draft.webhookShape === 'incident')

/** A GitHub source's "Any repository", in the draft. Never a valid `owner/repo`, so it cannot collide with one; sent
 *  as `""`, which the server keeps as any. An untouched blank (`''`) is sent as null instead, which a create fills
 *  with the project's primary (`_default_github_repository`) — so a wizard that reaches Target after When still
 *  defaults. */
export const ANY_REPOSITORY = '*'

const MIN_TIMEOUT = 5
const MAX_TIMEOUT = 165

/* ── Every field's explanation (`ui/explain.ts`): the hover on its label, a pop-up where there is more to say. A
 *    one-line explanation used once sits on its field. ── */
export const DOCS_HOW = 'using-jaah/automations/how-automations-work'
export const DOCS_REFERENCE = 'console-reference/automation/automations'
const PREFLIGHT_HELP: Explain = {
  tip: 'Decides whether to file, or splits one event into items.',
  more: 'It runs before each event files and answers run or skip, or returns items, each filed as its own task. It '
    + 'may also rewrite the State document. A failed preflight counts toward Auto-pause.',
  docs: DOCS_REFERENCE,
}
const PREFLIGHT_CHOICE_HELP: Record<string, string> = {
  none: 'Every event files as it arrives.',
  script: 'Runs a script from the project’s repository on a node.',
  model: 'Asks a model with your prompt and Anthropic key.',
}
const SCRIPT_PATH_HELP: Explain = {
  tip: 'The repository script that judges each event.',
  more: 'It runs on an online node the project runs on, in a throwaway clone of the integration branch made with '
    + 'the owner’s git login. It reads the event and the state as JSON and prints its answer as JSON.',
}
const CONCURRENCY_HELP: Explain = {
  tip: 'Most open tasks per target; further events wait queued.',
  more: 'While a target already has this many open tasks from this automation, the next event waits as Queued and '
    + 'files when one closes.',
  docs: DOCS_HOW,
}
const QUEUE_DEPTH_HELP: Explain = {
  tip: 'How many events may wait; later ones are skipped.',
  docs: DOCS_HOW,
}
const BUDGET_HELP: Explain = {
  tip: 'Cost cap per task; over it flags, never stops.',
  more: 'A filed task that costs more marks the automation Degraded with the task named. Blank: no budget.',
}
const AUTO_PAUSE_FAILURES_HELP: Explain = {
  tip: 'Stops it after this many failed runs in a row.',
  more: 'Failed preflights, template renders and filings count, and so does a filed task that fails. It shows as '
    + 'Failing; Owners and Admins are told, and Resume starts it again.',
  docs: DOCS_HOW,
}
const WEBHOOK_SHAPE_CHOICE_HELP: Record<string, string> = {
  event: 'Each call files a task per target.',
  incident: 'Records an incident in the target project; files no task.',
}
const WEBHOOK_AUTH_CHOICE_HELP: Record<string, string> = {
  api_key: 'Callers send an API key that may create tasks.',
  hmac: 'Legacy: callers sign the body with the stored secret.',
  bearer: 'Legacy: callers send the stored secret as a token.',
}
const SNS_TOPICS_HELP: Explain = {
  tip: 'Only alarms from these SNS topics are recorded.',
  more: 'Topic ARNs, comma-separated. A message from any other topic is refused.',
}
const GITHUB_EVENTS_HELP: Explain = {
  tip: 'GitHub event types that run it, comma-separated.',
  more: 'Each name is a GitHub webhook event, such as issues or pull_request. Tick the same events on the GitHub webhook.',
  docs: DOCS_REFERENCE,
}
const GITHUB_REPOSITORY_HELP = 'Only deliveries from this repository run it.'
const RECEIVES_AT_HELP: Explain = {
  tip: 'The address or mailbox whose incoming mail runs it.',
  more: 'One of your organization’s Jaah addresses or a service-account Gmail mailbox. The system mail domain needs a '
    + 'To, From or Subject rule.',
  docs: 'administration/security-and-audit/gmail',
}
const USER_SCOPE_CHOICE_HELP: Record<string, string> = {
  org: 'Each new member of this organization.',
  platform: 'Anyone signing up to Jaah; operators only.',
}
const USER_MOMENT_CHOICE_HELP: Record<string, string> = {
  applied: 'Runs when someone submits a sign-up application.',
  approved: 'Runs when their organization is approved.',
}
const USER_PROVIDER_HELP: Record<string, string> = {
  google: 'Counts sign-ups through a Google account.',
  github: 'Counts sign-ups through a GitHub account.',
  microsoft: 'Counts sign-ups through a Microsoft account.',
  email: 'Counts email sign-ups and members an admin creates.',
}
const USER_ACCOUNT_TYPE_HELP: Record<string, string> = {
  individual: 'Counts sign-ups for a personal account.',
  organization: 'Counts sign-ups made for an organization.',
}
const INCIDENT_TYPE_HELP: Record<string, string> = {
  monitor: 'Counts alerts raised by a connected monitor.',
  regression: 'Counts errors that rose after a release.',
  lane_slowdown: 'Counts the merge lane running slower than usual.',
}
const VENDOR_SECRET_HELP = 'Checks each delivery’s signature; paste it from the vendor.'
const SAMPLE_PAYLOAD_HELP: Explain = {
  tip: 'An example event to try the templates on.',
  more: 'Test shows the Title and Body it would file, without filing. Run now with this sample files it for real.',
  docs: DOCS_REFERENCE,
}
/** A ticked-boxes row's heading: what ticking narrows to, and that none ticked takes everyone. */
const narrowsTo = (what: string) => `Runs only for these ${what}; none ticked: any.`
/** A labels record as select options, each with its own help when given. */
const optionsOf = (labels: Record<string, string>, help?: Record<string, string>): FormOption[] =>
  Object.entries(labels).map(([value, label]) => ({ value, label, ...(help ? { help: help[value] } : {}) }))

/** A new source's draft, whose shape is `EventDraft`. */
export function newEventDraft(sourceKind: string) {
  return {
    sourceKind,
    webhookAuth: 'api_key',
    /** The saved webhook source's `auth`; '' for a new one. A legacy value keeps the Authentication choice drawn. */
    webhookSavedAuth: '',
    /** `event` | `incident` (`WEBHOOK_SHAPE_LABELS`). */
    webhookShape: 'event',
    githubEvents: '',
    githubActions: '',
    githubRepository: '',
    sentryEnvironments: '',
    /** A saved monitor source's `metrics` (Sentry, Datadog, CloudWatch), never edited here: sent back as it was read. */
    monitorMetrics: null as Record<string, unknown> | null,
    /** A CloudWatch source's SNS topic ARNs, comma-separated. */
    cloudwatchTopicArns: '',
    cloudwatchSeverity: 'error',
    /** Where an Email source receives (`GMAIL_SYSTEM`, `address:<id>`, `connection:<id>`); '' = not picked. */
    gmailInbox: '',
    gmailTo: '',
    gmailFrom: '',
    gmailSubject: '',
    slackChannels: '',
    userScope: 'org',
    userMoment: 'applied',
    userProviders: '',
    userAccountTypes: '',
    userDomains: '',
    incidentSeverity: 'error',
    incidentTypes: '',
    /** Blank sends null: production only (a deploy failure: every environment). */
    incidentEnvironments: '',
    quietOn: false,
    quietStart: QUIET_START,
    quietEnd: QUIET_END,
    quietTz: localTimeZone(),
    preflightKind: 'none',
    preflightScriptPath: '',
    preflightPrompt: '',
    preflightCredentialId: null as string | null,
    preflightTimeout: '120',
    concurrencyCap: '1',
    queueDepth: '10',
    rateLimit: '10',
    budget: '',
    autoPauseFailuresOn: true,
    autoPauseFailures: '5',
    autoPauseIdleOn: false,
    autoPauseIdleDays: '7',
  }
}
export type EventDraft = ReturnType<typeof newEventDraft>

const list = (value: unknown): string => (Array.isArray(value) ? value.filter(v => typeof v === 'string').join(', ') : '')

/* The server names the refused field by its wire key ("when: rate_limit_per_hour: must be …",
 * "action.params.body: …"). The page says it with the field's own label instead — no internal code reaches
 * the user. A refusal written as a whole sentence ("Add a description or pick a template.") is said as is. */
const FIELD_LABELS: Record<string, string> = {
  name: 'Name',
  description: 'Description',
  target: 'Target',
  'action.params.body': 'Instructions',
  'action.params.persona': 'Persona',
  source_kind: 'Source',
  source_config: 'Source settings',
  preflight_kind: 'Preflight',
  preflight_script_path: 'Script path',
  preflight_prompt: 'Preflight prompt',
  preflight_credential_id: 'Anthropic API key',
  preflight_timeout_seconds: 'Timeout',
  concurrency_cap: 'Concurrency cap',
  queue_depth: 'Queue depth',
  rate_limit_per_hour: 'Rate limit per hour',
  budget_usd: 'Budget',
  auto_pause_failures: 'Pause after consecutive failures',
  auto_pause_idle_days: 'Pause after idle days',
}

export function readableError(err: unknown, fallback: string): string {
  const detail = apiDetail(err, fallback).replace(/^when: /, '')
  const match = /^([a-z_.]+): ([\s\S]+)$/.exec(detail)
  if (match && /^[A-Z].*\.$/.test(match[2])) return match[2]
  const label = match ? FIELD_LABELS[match[1]] : undefined
  return match && label ? `${label}: ${match[2]}` : detail
}

/** An Email source's "Receives at" values: the deployment's own mail domain (operator org only), one of the org's
 *  hosted addresses, or one of its service-account mailboxes. */
export const GMAIL_SYSTEM = 'system'
/** How the server reads a From rule (`trigger_intake.from_rule_matches`): the field's pop-up. */
const FROM_RULE_HELP: Explain = {
  tip: 'Runs only on mail from this sender or domain.',
  more: 'An address matches only itself; a domain (@acme.com) also matches its subdomains; other text matches anywhere.',
}
const ADDRESS_PREFIX = 'address:'
const CONNECTION_PREFIX = 'connection:'
/** "Receives at"'s last option: opens New address and selects what it makes; never a stored value. */
const NEW_ADDRESS = 'new-address'
/** The nested create's pick (`hooks/useNestedCreate.ts`): the New address page's id fills "Receives at". */
export const ADDRESS_PICK = 'address_id'

/** Opens the New address page for "Receives at" (a nested create): the automation editor provides it, since it owns
 *  the draft that page comes back to. Absent, no "New address…" is offered. */
export const NewAddressContext = createContext<(() => void) | null>(null)

/** The draft with a just-made hosted address picked as "Receives at". */
export function withAddress(draft: EventDraft, addressId: string): EventDraft {
  return { ...draft, gmailInbox: `${ADDRESS_PREFIX}${addressId}` }
}

/** A saved Email source's `inbox`/`address_id` as the select's value; a source saved before either existed is the
 *  system inbox (the server's default). */
function inboxChoiceOf(config: Record<string, unknown>): string {
  if (typeof config.address_id === 'string' && config.address_id !== '') return `${ADDRESS_PREFIX}${config.address_id}`
  if (typeof config.inbox === 'string' && config.inbox !== GMAIL_SYSTEM && config.inbox !== '') return `${CONNECTION_PREFIX}${config.inbox}`
  return GMAIL_SYSTEM
}

/** The select's value as the wire `inbox`/`address_id` (`app/trigger_config.py`): an address rides the system inbox. */
function inboxConfigOf(choice: string): { inbox: string; address_id: string | null } {
  if (choice.startsWith(ADDRESS_PREFIX)) return { inbox: GMAIL_SYSTEM, address_id: choice.slice(ADDRESS_PREFIX.length) }
  if (choice.startsWith(CONNECTION_PREFIX)) return { inbox: choice.slice(CONNECTION_PREFIX.length), address_id: null }
  return { inbox: GMAIL_SYSTEM, address_id: null }
}

/* ── ONE entry per source kind ─────────────────────────────────────────────────────────────────────── */

/** The draft fields that hold text. */
type TextKey = { [K in keyof EventDraft]: EventDraft[K] extends string ? K : never }[keyof EventDraft]
/** One labelled field: a text box, or a select when `options` is given. */
type FieldSpec = {
  key: TextKey
  label: string
  testId: string
  help: HelpProp
  required?: boolean
  placeholder?: string
  hint?: string
  type?: 'time'
  options?: FormOption[]
}
/** What a kind's bespoke fields draw with: the form's props, one field's edit, a `FieldSpec`'s field, a ticked-boxes row. */
type Kit = {
  draft: EventDraft
  disabled: boolean
  creating: boolean
  operator: boolean
  projectId: number | null
  orgId: string
  operatorOrg: boolean
  automationId: string | null
  edit: <K extends keyof EventDraft>(key: K, value: EventDraft[K]) => void
  field: (spec: FieldSpec) => ReactNode
  toggles: (key: TextKey, labels: Record<string, string>, testId: string, help: Record<string, string>) => ReactNode
}
/** A kind's template namespaces (`app/trigger_template.py`): a pasted sample whose top-level keys all sit inside
 *  `keys` is sent as-is; anything else is wrapped in `wrap`. `names` are the body hint's placeholders. */
type Template = { wrap: string; keys: readonly string[]; names: string; sample: string }
type SourceKind = {
  template?: Template
  /** The saved `source_config` as this kind's draft fields; every other field keeps `newEventDraft`'s. */
  parse?: (config: Record<string, unknown>) => Partial<EventDraft>
  /** The kind's `source_config`, in the shape `app/trigger_config.py` validates; none: `{}`. */
  wire?: (draft: EventDraft) => Record<string, unknown>
  /** The sentence that stops a save, beyond the shared number checks. */
  check?: (draft: EventDraft) => string | null
  /** Its fields: plain ones as descriptors, the rest drawn by `render` after them. */
  fields?: FieldSpec[]
  render?: (kit: Kit) => ReactNode
}

const httpTemplate = (sample: string): Template => ({
  wrap: 'body', keys: ['body', 'headers', 'query', 'item'], names: '{{body.…}}, {{headers.…}}, {{query.…}}', sample,
})
const WEBHOOK_TEMPLATE = httpTemplate('{"issue": {"title": "Login broken"}}')
/** A monitor's saved `metrics`, and the config that sends them back as they were read. */
const parseMetrics = (config: Record<string, unknown>) => ({ monitorMetrics: isRecord(config.metrics) ? config.metrics : null })
const metricsOf = (draft: EventDraft) => (draft.monitorMetrics ? { metrics: draft.monitorMetrics } : {})

/** `incident`, `deploy_failed` and `health_breach`: only `incident` (`typed`) narrows by incident type. */
function incidentKind(typed: boolean): SourceKind {
  return {
    template: {
      wrap: 'incident',
      keys: ['incident', 'suspects', 'deployment', 'item'],
      names: '{{incident.brief}}, {{incident.title}}, {{incident.severity}}, {{incident.url}}, {{deployment.run_url}}',
      sample: 'Paste {"incident": {"title": "…", "severity": "…", "environment": "…"}}',
    },
    parse: config => {
      const quiet: Record<string, unknown> = isRecord(config.quiet_hours) ? config.quiet_hours : {}
      return {
        incidentSeverity: str(config.min_severity) || 'error',
        incidentTypes: typed ? list(config.types) : '',
        incidentEnvironments: list(config.environments),
        quietOn: isRecord(config.quiet_hours),
        quietStart: str(quiet.start) || QUIET_START,
        quietEnd: str(quiet.end) || QUIET_END,
        quietTz: str(quiet.tz) || localTimeZone(),
      }
    },
    wire: draft => {
      const environments = splitList(draft.incidentEnvironments)
      return {
        min_severity: draft.incidentSeverity,
        types: typed ? splitList(draft.incidentTypes) : [],
        environments: environments.length > 0 ? environments : null,
        quiet_hours: draft.quietOn ? { start: draft.quietStart.trim(), end: draft.quietEnd.trim(), tz: draft.quietTz.trim() } : null,
      }
    },
    check: draft => {
      if (!draft.quietOn) return null
      if (!HH_MM.test(draft.quietStart.trim())) return 'Quiet hours start must be HH:MM.'
      if (!HH_MM.test(draft.quietEnd.trim())) return 'Quiet hours end must be HH:MM.'
      return draft.quietTz.trim() === '' ? 'Quiet hours need a time zone.' : null
    },
    render: ({ draft, edit, field, toggles, disabled }) => (
      <>
        <Banner severity="info" testId="automation-incident-target">Files in the incident’s project: pick exactly one project as the Target.</Banner>
        {field({ key: 'incidentSeverity', label: 'Minimum severity', testId: 'automation-incident-severity', options: optionsOf(SEVERITY_LABELS),
          help: 'Incidents below this severity are ignored.' })}
        {typed && (
          <>
            <SubHeading help={narrowsTo('incident types')}>Types</SubHeading>
            {toggles('incidentTypes', INCIDENT_TYPE_LABELS, 'automation-incident-type', INCIDENT_TYPE_HELP)}
          </>
        )}
        {field({
          key: 'incidentEnvironments', label: 'Environments', testId: 'automation-incident-environments', placeholder: 'production',
          help: draft.sourceKind === 'deploy_failed'
            ? 'Runs only for these environments, comma-separated; blank: all.'
            : 'Runs only for these environments, comma-separated; blank: production.',
        })}
        <SettingCheckbox name="quiet_hours" label="Quiet hours" help="Holds tasks in a daily window; they file after it." checked={draft.quietOn}
          disabled={disabled} onChange={on => edit('quietOn', on)} testId="automation-quiet-hours-on" />
        {draft.quietOn && (
          <FieldGrid>
            {field({ key: 'quietStart', label: 'Start', testId: 'automation-quiet-start', type: 'time', help: 'From this time each day, new tasks are held.' })}
            {field({ key: 'quietEnd', label: 'End', testId: 'automation-quiet-end', type: 'time', help: 'Held tasks file at this time each day.' })}
            {field({ key: 'quietTz', label: 'Time zone', testId: 'automation-quiet-tz', placeholder: 'Europe/London', help: 'Start and End are local times in this zone.' })}
          </FieldGrid>
        )}
      </>
    ),
  }
}

const SOURCE_KINDS: Partial<Record<string, SourceKind>> = {
  webhook: {
    template: WEBHOOK_TEMPLATE,
    // A webhook saved before `auth` existed is HMAC-signed.
    parse: config => {
      const auth = str(config.auth) || 'hmac'
      return { webhookAuth: auth, webhookSavedAuth: auth, webhookShape: config.shape === 'incident' ? 'incident' : 'event' }
    },
    // `shape` is sent only for an incident: an event webhook keeps the plain `{auth}` it always sent.
    wire: draft => ({ auth: draft.webhookAuth, ...(draft.webhookShape === 'incident' ? { shape: 'incident' } : {}) }),
    render: ({ draft, field }) => (
      <>
        {field({
          key: 'webhookShape', label: 'Payload', testId: 'automation-webhook-shape', help: 'Whether each call files a task or records an incident.',
          options: optionsOf(WEBHOOK_SHAPE_LABELS, WEBHOOK_SHAPE_CHOICE_HELP),
          hint: draft.webhookShape === 'incident' ? `Keys: ${INCIDENT_KEYS}.` : undefined,
        })}
        {/* An API key is the only scheme: the choice is drawn only for a legacy secret, to switch it. */}
        {isLegacyWebhookAuth(draft.webhookSavedAuth) && (
          <>
            <Banner severity="warning" testId="automation-webhook-legacy">Legacy secret. Switch to an API key.</Banner>
            {field({
              key: 'webhookAuth', label: 'Authentication', testId: 'automation-webhook-auth', help: 'How a caller proves the call is genuine.',
              options: [draft.webhookSavedAuth, 'api_key'].map(value => ({ value, label: WEBHOOK_AUTH_LABELS[value], help: WEBHOOK_AUTH_CHOICE_HELP[value] })),
              hint: draft.webhookAuth === 'api_key' ? 'Saving drops the secret.' : undefined,
            })}
          </>
        )}
      </>
    ),
  },
  github: {
    template: httpTemplate('{"action": "opened", "issue": {"title": "Login broken"}}'),
    // A saved source with no repository listens to any.
    parse: config => ({ githubEvents: list(config.events), githubActions: list(config.actions), githubRepository: str(config.repository) || ANY_REPOSITORY }),
    wire: draft => ({
      events: splitList(draft.githubEvents),
      actions: splitList(draft.githubActions),
      repository: draft.githubRepository === ANY_REPOSITORY ? '' : strOrNull(draft.githubRepository),
    }),
    fields: [
      { key: 'githubEvents', label: 'Events', testId: 'automation-github-events', required: true, placeholder: 'issues, pull_request', help: GITHUB_EVENTS_HELP },
      { key: 'githubActions', label: 'Actions', testId: 'automation-github-actions', placeholder: 'opened, labeled',
        help: 'Narrows events to these actions; empty takes every action.' },
    ],
    // Keyed so a Target move between no project and a project remounts it: the select then defaults afresh.
    render: ({ draft, edit, projectId, disabled, creating }) => (
      <GithubRepositorySelect key={projectId === null ? 'free' : 'connected'} projectId={projectId} value={draft.githubRepository}
        onChange={v => edit('githubRepository', v)} disabled={disabled} creating={creating} />
    ),
  },
  sentry: {
    template: httpTemplate('{"action": "created", "data": {"issue": {"title": "TypeError"}}}'),
    parse: config => ({ sentryEnvironments: list(config.environments), ...parseMetrics(config) }),
    wire: draft => ({ environments: splitList(draft.sentryEnvironments), ...metricsOf(draft) }),
    fields: [{ key: 'sentryEnvironments', label: 'Environments', testId: 'automation-sentry-environments', placeholder: 'production',
      help: 'Records only these environments, comma-separated; blank: all.' }],
  },
  datadog: { parse: parseMetrics, wire: metricsOf },
  pagerduty: { parse: parseMetrics },
  cloudwatch: {
    parse: config => ({ cloudwatchTopicArns: list(config.topic_arns), cloudwatchSeverity: str(config.severity) || 'error', ...parseMetrics(config) }),
    wire: draft => ({ topic_arns: splitList(draft.cloudwatchTopicArns), severity: draft.cloudwatchSeverity, ...metricsOf(draft) }),
    fields: [
      { key: 'cloudwatchTopicArns', label: 'SNS topics', testId: 'automation-cloudwatch-topics', required: true,
        placeholder: 'arn:aws:sns:us-east-1:<account>:alarms', help: SNS_TOPICS_HELP },
      { key: 'cloudwatchSeverity', label: 'Severity', testId: 'automation-cloudwatch-severity',
        options: SEVERITIES.map(value => ({ value, label: capitalize(value) })), help: 'Severity every alarm is recorded with; CloudWatch sends none.' },
    ],
  },
  gmail: {
    template: { wrap: 'email', keys: ['email', 'item'], names: '{{email.from}}, {{email.subject}}, {{email.body}}',
      sample: 'Paste {"from": "…", "subject": "…", "body": "…"}' },
    parse: config => ({ gmailInbox: inboxChoiceOf(config), gmailTo: str(config.to), gmailFrom: str(config.from), gmailSubject: str(config.subject) }),
    wire: draft => ({
      to: strOrNull(draft.gmailTo), from: strOrNull(draft.gmailFrom), subject: strOrNull(draft.gmailSubject),
      ...inboxConfigOf(draft.gmailInbox),
    }),
    check: draft => {
      if (draft.gmailInbox === '') return 'Pick where it receives.'
      const anyRule = [draft.gmailTo, draft.gmailFrom, draft.gmailSubject].some(v => v.trim() !== '')
      return draft.gmailInbox === GMAIL_SYSTEM && !anyRule ? 'The system mail domain needs a To, From or Subject.' : null
    },
    render: ({ draft, edit, field, disabled, orgId, operatorOrg, automationId }) => (
      <>
        <GmailInboxSelect orgId={orgId} operatorOrg={operatorOrg} automationId={automationId} value={draft.gmailInbox}
          onChange={v => edit('gmailInbox', v)} disabled={disabled} />
        {field({ key: 'gmailTo', label: 'To contains', testId: 'automation-gmail-to', help: 'Runs only on mail whose To contains this text.' })}
        {field({ key: 'gmailFrom', label: 'From', testId: 'automation-gmail-from', help: FROM_RULE_HELP })}
        {field({ key: 'gmailSubject', label: 'Subject contains', testId: 'automation-gmail-subject', help: 'Runs only on mail whose subject contains this text.' })}
      </>
    ),
  },
  slack: {
    template: { wrap: 'slack', keys: ['slack', 'item'], names: '{{slack.text}}, {{slack.user}}, {{slack.channel}}, {{slack.permalink}}',
      sample: 'Paste {"text": "…", "user": "…", "channel": "…", "permalink": "…"}' },
    parse: config => ({ slackChannels: list(config.channel_ids) }),
    wire: draft => ({ channel_ids: splitList(draft.slackChannels) }),
    fields: [{ key: 'slackChannels', label: 'Channel IDs', testId: 'automation-slack-channels', required: true, placeholder: 'C0123ABCD',
      help: 'Mentions of Jaah in these channels run it; comma-separated.' }],
  },
  user_registered: {
    template: { wrap: 'user', keys: ['user', 'item'], names: '{{user.email}}, {{user.name}}, {{user.org_name}}, {{user.provider}}, {{user.url}}',
      sample: 'Paste {"email": "…", "name": "…", "org_name": "…", "provider": "…"}' },
    parse: config => ({
      userScope: str(config.scope) || 'org', userMoment: str(config.moment) || 'applied', userProviders: list(config.providers),
      userAccountTypes: list(config.account_types), userDomains: list(config.email_domains),
    }),
    wire: draft => {
      // Moment and account type are a platform sign-up's; the server refuses them on an org scope.
      const platform = draft.userScope === 'platform'
      return {
        scope: draft.userScope,
        moment: platform ? draft.userMoment : 'applied',
        providers: splitList(draft.userProviders),
        account_types: platform ? splitList(draft.userAccountTypes) : [],
        email_domains: splitList(draft.userDomains),
      }
    },
    render: ({ draft, field, toggles, operator }) => {
      const platform = draft.userScope === 'platform'
      return (
        <>
          {field({ key: 'userScope', label: 'Who', testId: 'automation-user-scope', help: 'Whose sign-ups run it.', options: [
            { value: 'org', label: USER_SCOPE_LABELS.org, help: USER_SCOPE_CHOICE_HELP.org },
            ...(operator || platform ? [{ value: 'platform', label: USER_SCOPE_LABELS.platform, help: USER_SCOPE_CHOICE_HELP.platform }] : []),
          ] })}
          {platform && field({ key: 'userMoment', label: 'Fires on', testId: 'automation-user-moment', help: 'Which step of a platform sign-up runs it.',
            options: optionsOf(USER_MOMENT_LABELS, USER_MOMENT_CHOICE_HELP) })}
          <SubHeading help={narrowsTo('sign-in methods')}>Sign-in providers</SubHeading>
          {toggles('userProviders', SIGNUP_PROVIDER_LABELS, 'automation-user-provider', USER_PROVIDER_HELP)}
          {platform && (
            <>
              <SubHeading help={narrowsTo('account types')}>Account type</SubHeading>
              {toggles('userAccountTypes', SIGNUP_ACCOUNT_TYPE_LABELS, 'automation-user-account', USER_ACCOUNT_TYPE_HELP)}
            </>
          )}
          {field({ key: 'userDomains', label: 'Email domains', testId: 'automation-user-domains', placeholder: 'example.com',
            help: 'Runs only for emails at these exact domains; empty: any.' })}
        </>
      )
    },
  },
  incident: incidentKind(true),
  deploy_failed: incidentKind(false),
  health_breach: incidentKind(false),
}

/** A kind's template namespaces; a kind without its own reads as a webhook's. */
const templateOf = (kind: string) => SOURCE_KINDS[kind]?.template ?? WEBHOOK_TEMPLATE

/** The hint under a body template, per source kind. */
export const EVENT_TEMPLATE_HINT: Record<string, string> = Object.fromEntries(
  Object.entries(SOURCE_KINDS).flatMap(([kind, spec]) =>
    spec?.template ? [[kind, `Placeholders: ${spec.template.names}; with a preflight, {{item.id}}, {{item.…}}.`]] : []),
)

export function eventDraftOf(when: EventWhen): EventDraft {
  return {
    ...newEventDraft(when.source_kind),
    ...SOURCE_KINDS[when.source_kind]?.parse?.(when.source_config ?? {}),
    preflightKind: when.preflight_kind,
    preflightScriptPath: when.preflight_script_path ?? '',
    preflightPrompt: when.preflight_prompt ?? '',
    preflightCredentialId: when.preflight_credential_id,
    preflightTimeout: String(when.preflight_timeout_seconds),
    concurrencyCap: String(when.concurrency_cap),
    queueDepth: String(when.queue_depth),
    rateLimit: String(when.rate_limit_per_hour),
    budget: when.budget_usd === null ? '' : String(when.budget_usd),
    autoPauseFailuresOn: when.auto_pause_failures !== null,
    autoPauseFailures: String(when.auto_pause_failures ?? 5),
    autoPauseIdleOn: when.auto_pause_idle_days !== null,
    autoPauseIdleDays: String(when.auto_pause_idle_days ?? 7),
  }
}

/** A whole number from a typed field, or the sentence that says why not. */
const whole = (raw: string, label: string): number | string => wholeNumber(raw) ?? `${label} must be a whole number.`

/** The draft as the wire `when` (`automation_service.EVENT_FIELDS`), or the first sentence that stops it. */
export function eventWhenOf(draft: EventDraft): ({ kind: 'event' } & Record<string, unknown>) | string {
  const numbers = {
    preflight_timeout_seconds: whole(draft.preflightTimeout, 'Preflight timeout'),
    concurrency_cap: whole(draft.concurrencyCap, 'Concurrency cap'),
    queue_depth: whole(draft.queueDepth, 'Queue depth'),
    rate_limit_per_hour: whole(draft.rateLimit, 'Rate limit'),
    auto_pause_failures: draft.autoPauseFailuresOn ? whole(draft.autoPauseFailures, 'Failures before pause') : null,
    auto_pause_idle_days: draft.autoPauseIdleOn ? whole(draft.autoPauseIdleDays, 'Idle days before pause') : null,
  }
  for (const value of Object.values(numbers)) if (typeof value === 'string') return value
  const kind = SOURCE_KINDS[draft.sourceKind]
  const refused = kind?.check?.(draft)
  if (refused) return refused
  let budget: number | null = null
  if (draft.budget.trim() !== '') {
    budget = Number(draft.budget.trim())
    if (!Number.isFinite(budget)) return 'Budget must be a number.'
  }
  return {
    kind: 'event',
    source_kind: draft.sourceKind,
    source_config: kind?.wire?.(draft) ?? {},
    preflight_kind: draft.preflightKind,
    preflight_script_path: strOrNull(draft.preflightScriptPath),
    preflight_prompt: strOrNull(draft.preflightPrompt),
    preflight_credential_id: draft.preflightCredentialId,
    ...numbers,
    budget_usd: budget,
  }
}

/** The pasted sample as the event the template reads. Throws on invalid JSON. */
function sampleEvent(kind: string, raw: string): Record<string, unknown> {
  const parsed: unknown = JSON.parse(raw)
  const spaces = templateOf(kind)
  if (isRecord(parsed)) {
    const keys = Object.keys(parsed)
    if (keys.length > 0 && keys.every(key => spaces.keys.includes(key))) return parsed
  }
  return { [spaces.wrap]: parsed }
}

const fetchAnthropicKeys = (query: string, signal: AbortSignal) =>
  loadCredentialOptions(query, signal, row => ({
    id: row.id,
    name: row.name,
    hint: row.description?.trim() || row.type_label,
    icon: credentialMark(row.type, row.type_label),
  }), { types: ['anthropic_api_key'] })

/* ── The When's fields ─────────────────────────────────────────────────────────────────────────────── */

export default function EventSourceFields({
  draft, onChange, disabled, creating, operator = false, projectId = null, orgId = '', operatorOrg = false, automationId = null,
}: {
  draft: EventDraft
  onChange: (next: EventDraft) => void
  disabled: boolean
  creating: boolean
  /** A Jaah operator may also listen to sign-ups on Jaah (`user_registered`'s platform scope). */
  operator?: boolean
  /** The one project the event child lives in (the Target's first specific project), whose connected repos a
   *  GitHub source picks from; null when the Target names none. */
  projectId?: number | null
  /** The automation's org, whose addresses and mailboxes an Email source receives at. */
  orgId?: string
  /** That org is the operator plane: the system mail domain is offered too. */
  operatorOrg?: boolean
  /** The saved automation's id: the address its own trigger holds stays offered. */
  automationId?: string | null
}) {
  const edit = fieldEditor(draft, onChange)
  const field = ({ key, ...props }: FieldSpec) => (
    <FormTextField key={key} {...props} value={draft[key]} disabled={disabled} onChange={v => edit(key, v)} />
  )
  // One box per value, kept in the draft's comma-joined list; the boxes share one wrapping row, and a row
  // with nothing ticked is an any-of over everyone — said once under it, since bare boxes cannot say so.
  const toggles: Kit['toggles'] = (key, labels, testId, help) => {
    const picked = splitList(draft[key])
    return (
      <Box>
        <Box sx={{ display: 'flex', flexWrap: 'wrap', columnGap: 2 }}>
          {Object.entries(labels).map(([value, label]) => (
            <SettingCheckbox
              key={value}
              name={`${testId}-${value}`}
              label={label}
              help={help[value]}
              checked={picked.includes(value)}
              disabled={disabled}
              onChange={on => edit(key, (on ? [...picked, value] : picked.filter(v => v !== value)).join(', '))}
              testId={`${testId}-${value}`}
            />
          ))}
        </Box>
      </Box>
    )
  }
  const kind = SOURCE_KINDS[draft.sourceKind]
  return (
    <>
      {recordsOnly(draft) && (
        <Banner severity="info" testId="automation-records-only">Records incidents in the target project. Files no task.</Banner>
      )}
      {kind?.fields?.map(field)}
      {kind?.render?.({ draft, edit, field, toggles, disabled, creating, operator, projectId, orgId, operatorOrg, automationId })}
    </>
  )
}

/** One field's change as the whole next draft, which both halves of the event form hand their fields. */
function fieldEditor(draft: EventDraft, onChange: (next: EventDraft) => void) {
  return <K extends keyof EventDraft>(key: K, value: EventDraft[K]) => onChange({ ...draft, [key]: value })
}

/** The Limits row's number fields. */
const LIMIT_FIELDS: { key: TextKey; label: string; help: HelpProp; testId: string; htmlInput: { min: number; max?: number; step: string } }[] = [
  { key: 'concurrencyCap', label: 'Concurrency cap', help: CONCURRENCY_HELP, testId: 'automation-concurrency', htmlInput: { min: 1, max: 20, step: '1' } },
  { key: 'queueDepth', label: 'Queue depth', help: QUEUE_DEPTH_HELP, testId: 'automation-queue-depth', htmlInput: { min: 0, max: 100, step: '1' } },
  { key: 'rateLimit', label: 'Rate limit per hour', help: 'Most filings per hour; events past it are skipped.', testId: 'automation-rate-limit',
    htmlInput: { min: 1, max: 1000, step: '1' } },
  { key: 'budget', label: 'Budget (USD)', help: BUDGET_HELP, testId: 'automation-budget', htmlInput: { min: 0, step: '0.01' } },
]
/** Auto-pause's switched numbers: a box turns each on, and its number shows only while on. */
const AUTO_PAUSE_FIELDS = [
  { name: 'auto_pause_failures', label: 'Pause after consecutive failures', help: AUTO_PAUSE_FAILURES_HELP, on: 'autoPauseFailuresOn',
    value: 'autoPauseFailures', testId: 'automation-auto-pause-failures' },
  { name: 'auto_pause_idle_days', label: 'Pause after idle days', help: 'Stops it after this many days without a successful run.', on: 'autoPauseIdleOn',
    value: 'autoPauseIdleDays', testId: 'automation-auto-pause-idle' },
] as const

/** An event's Settings-tab half: Preflight, Limits and Auto-pause. A new automation keeps `newEventDraft`'s values. */
export function EventExtrasFields({ draft, onChange, disabled }: {
  draft: EventDraft
  onChange: (next: EventDraft) => void
  disabled: boolean
}) {
  const edit = fieldEditor(draft, onChange)
  return (
    <>
      <FormTextField
        label="Preflight"
        help={PREFLIGHT_HELP}
        value={draft.preflightKind}
        disabled={disabled}
        testId="automation-preflight-kind"
        onChange={v => edit('preflightKind', v)}
        options={optionsOf(PREFLIGHT_LABELS, PREFLIGHT_CHOICE_HELP)}
      />
      {draft.preflightKind === 'script' && (
        <FormTextField
          label="Script path"
          help={SCRIPT_PATH_HELP}
          required
          placeholder="scripts/triage.py"
          value={draft.preflightScriptPath}
          disabled={disabled}
          onChange={v => edit('preflightScriptPath', v)}
          testId="automation-preflight-script"
          slotProps={{ htmlInput: { maxLength: 500 } }}
        />
      )}
      {draft.preflightKind === 'model' && (
        <>
          <FormTextField
            label="Preflight prompt"
            help="Instructions the model follows to judge each event."
            required
            value={draft.preflightPrompt}
            disabled={disabled}
            multiline
            minRows={3}
            onChange={v => edit('preflightPrompt', v)}
            testId="automation-preflight-prompt"
          />
          <FormField label="Anthropic API key" help="The Anthropic key the preflight model is billed to.">
            <CredentialSelect
              id="automation-preflight-credential"
              value={draft.preflightCredentialId}
              onChange={next => edit('preflightCredentialId', next)}
              fetchOptions={fetchAnthropicKeys}
              disabled={disabled}
              fullWidth
              size="small"
              testId="automation-preflight-credential"
            />
          </FormField>
        </>
      )}
      {draft.preflightKind !== 'none' && (
        <FormTextField
          label="Timeout (seconds)"
          help={`The preflight fails after this many seconds (${MIN_TIMEOUT}–${MAX_TIMEOUT}).`}
          type="number"
          value={draft.preflightTimeout}
          disabled={disabled}
          slotProps={{ htmlInput: { min: MIN_TIMEOUT, max: MAX_TIMEOUT, step: '1' } }}
          testId="automation-preflight-timeout"
          onChange={v => edit('preflightTimeout', v)}
        />
      )}

      <SubHeading>Limits</SubHeading>
      <FieldGrid>
        {LIMIT_FIELDS.map(({ key, htmlInput, ...props }) => (
          <FormTextField key={key} {...props} type="number" value={draft[key]} disabled={disabled} slotProps={{ htmlInput }}
            onChange={v => edit(key, v)} />
        ))}
      </FieldGrid>

      <SubHeading>Auto-pause</SubHeading>
      {AUTO_PAUSE_FIELDS.map(({ name, label, help, on, value, testId }) => (
        <Box key={name} sx={{ display: 'flex', alignItems: 'center', gap: 1, flexWrap: 'wrap' }}>
          <Box sx={{ flex: `1 1 ${grid(37)}` }}>
            <SettingCheckbox name={name} label={label} help={help} checked={draft[on]} disabled={disabled} onChange={next => edit(on, next)}
              testId={`${testId}-on`} />
          </Box>
          {draft[on] && (
            <Box sx={{ flex: `0 1 ${grid(21)}` }}>
              <TextField
                type="number"
                size="small"
                fullWidth
                value={draft[value]}
                disabled={disabled}
                onChange={e => edit(value, e.target.value)}
                sx={fieldTapTargetSx}
                slotProps={{ htmlInput: { 'data-testid': testId, min: 1, 'aria-label': label } }}
              />
            </Box>
          )}
        </Box>
      ))}
    </>
  )
}

/** "Receives at"'s options for one org: the system domain (operator org, or a source already saved on it); each
 *  hosted address no other automation's trigger holds; each service-account mailbox that can receive. Never a Google
 *  sign-in connection: it only sends. */
export function inboxOptions(email: OrgEmail, operatorOrg: boolean, automationId: string | null, value: string): { value: string; label: string }[] {
  const options: { value: string; label: string }[] = []
  if (operatorOrg || value === GMAIL_SYSTEM) options.push({ value: GMAIL_SYSTEM, label: 'System mail domain' })
  for (const row of email.addresses) {
    const choice = `${ADDRESS_PREFIX}${row.id}`
    const free = row.trigger_id === null || (automationId !== null && row.automation_id === automationId) || value === choice
    if (!free) continue
    const labelled = row.name.trim() !== '' && row.name !== row.address
    options.push({ value: choice, label: labelled ? `${row.address} (${row.name})` : row.address })
  }
  for (const row of email.connections) {
    if (row.kind !== 'service_account' || !row.can_receive) continue
    options.push({ value: `${CONNECTION_PREFIX}${row.id}`, label: `Gmail · ${row.address}` })
  }
  return options
}

/** An Email source's required "Receives at", read from the automation's org. Where the org can make a hosted address
 *  (on the plan, on a deployment that receives mail), "New address…" opens the New address page, which comes back
 *  to the editor with its input kept and the new address picked (`NewAddressContext`). A tenant org with nothing to
 *  receive at and nothing to make gets the way to Settings. */
function GmailInboxSelect({ orgId, operatorOrg, automationId, value, onChange, disabled }: {
  orgId: string
  operatorOrg: boolean
  automationId: string | null
  value: string
  onChange: (value: string) => void
  disabled: boolean
}) {
  const openNewAddress = useContext(NewAddressContext)
  const email = useQuery({ queryKey: [...ORG_EMAIL_KEY, orgId], queryFn: () => getOrgEmail(orgId), enabled: orgId !== '' })
  const options = email.data ? inboxOptions(email.data, operatorOrg, automationId, value) : []
  const canCreate = openNewAddress !== null && !!email.data && email.data.on_plan && email.data.mail_domain !== ''
  const known = value === '' || !email.data || options.some(option => option.value === value)
  if (email.data && options.length === 0 && !canCreate) {
    return (
      <Typography variant="body2" sx={{ color: 'text.secondary' }} data-testid="automation-gmail-inbox-none">
        No address to receive at. <Link component={RouterLink} to={EMAIL_ADDRESSES_URL}>Add one</Link>
      </Typography>
    )
  }
  return (
    <FormTextField
      label="Receives at"
      help={RECEIVES_AT_HELP}
      required
      value={value}
      disabled={disabled || email.isLoading}
      testId="automation-gmail-inbox"
      onChange={choice => (choice === NEW_ADDRESS ? openNewAddress?.() : onChange(choice))}
      error={email.isError ? apiDetail(email.error, 'Couldn’t load the addresses.') : undefined}
      options={[
        ...options,
        ...(known ? [] : [{ value, label: 'Removed' }]),
        ...(canCreate ? [{ value: NEW_ADDRESS, label: 'New address…' }] : []),
      ]}
    >
      {/* Nothing picked yet: a native select would show its first option as if chosen, and picking "New address…"
          when it is the only one would fire no change. */}
      {value === '' && <option value="" disabled>Choose…</option>}
    </FormTextField>
  )
}

/** A GitHub source's Repository: one of the project's connected repos (D9), or any. A new source starts on the
 *  primary, and again whenever the Target moves to a project that does not connect the picked repo. A stored value
 *  the project no longer connects stays listed, marked, so a reopen never silently changes it. With no project
 *  there is nothing to list: free text, where blank means any repository (on create, the project's primary). */
function GithubRepositorySelect({ projectId, value, onChange, disabled, creating }: {
  projectId: number | null
  value: string
  onChange: (value: string) => void
  disabled: boolean
  creating: boolean
}) {
  const repos = useQuery({
    queryKey: ['automation-github-repos', projectId],
    queryFn: () => connectedGithubRepos(projectId!),
    enabled: projectId !== null,
  })
  const connected = repos.data ?? []
  const picked = value.trim()
  // Matched case-insensitively, as intake matches a delivery; the select shows the connected spelling.
  const match = connected.find(repo => repo.name.toLowerCase() === picked.toLowerCase())?.name
  const known = picked === '' || picked === ANY_REPOSITORY || match !== undefined || !repos.data
  const defaultedFor = useRef<number | null>(null)
  useEffect(() => {
    if (!creating || !repos.data || defaultedFor.current === projectId) return
    const first = defaultedFor.current === null
    defaultedFor.current = projectId
    // "Any repository" picked by hand stays picked across a Target change.
    if (picked === ANY_REPOSITORY && !first) return
    if (!repos.data.some(repo => repo.name.toLowerCase() === picked.toLowerCase())) onChange(repos.data[0]?.name ?? ANY_REPOSITORY)
  }, [creating, repos.data, projectId, picked, onChange])
  if (projectId === null) {
    return (
      <FormTextField
        label="Repository"
        placeholder="owner/repo"
        help={creating ? 'Only this repository runs it; empty: the project’s primary.' : 'Only this repository runs it; empty: any repository.'}
        value={value === ANY_REPOSITORY ? '' : value}
        disabled={disabled}
        onChange={v => onChange(v.trim() === '' && !creating ? ANY_REPOSITORY : v)}
        testId="automation-github-repository"
      />
    )
  }
  return (
    <FormTextField
      label="Repository"
      help={GITHUB_REPOSITORY_HELP}
      value={match ?? (picked === '' ? ANY_REPOSITORY : picked)}
      disabled={disabled || repos.isPending}
      testId="automation-github-repository"
      onChange={onChange}
      error={repos.isError ? 'Couldn’t load the project’s repositories.' : undefined}
      hint={!known ? 'Not connected to this project: pick a connected one or Any repository.' : undefined}
      options={[
        { value: ANY_REPOSITORY, label: 'Any repository' },
        ...connected.map(repo => ({ value: repo.name, label: repo.is_primary ? `${repo.name} (primary)` : repo.name })),
        ...(known ? [] : [{ value: picked, label: `${picked} (not connected)` }]),
      ]}
    />
  )
}

function FieldGrid({ children }: { children: ReactNode }) {
  return (
    <Box sx={{ display: 'grid', gap: 2, gridTemplateColumns: `repeat(auto-fit, minmax(${grid(29)}, 1fr))` }}>
      {children}
    </Box>
  )
}

function SubHeading({ children, help }: { children: string; help?: string }) {
  return (
    <Typography variant="body2" component="h3" sx={{ fontWeight: FONT_WEIGHT.semibold, pt: 0.5 }}>
      <HelpLabel label={children} help={help} />
    </Typography>
  )
}

/* ── Endpoint: where a sender posts, and the secret ──────────────────────────────────────────────────── */

export function EventSetupSection({
  automation,
  onAutomation,
  onSecret,
  confirm,
}: {
  automation: Automation
  onAutomation: (next: Automation) => void
  onSecret: (secret: string) => void
  confirm: ReturnType<typeof useConfirm>[0]
}) {
  const { busy, formError: error, run: submit } = useSubmit()
  const pasted = useSubmit()
  const [clientSecret, setClientSecret] = useState('')
  const [checking, setChecking] = useState(false)
  const when = automation.when
  if (when.kind !== 'event' || !HTTP_SOURCE_KINDS.has(when.source_kind)) return null
  const auth = savedWebhookAuth(when) || null
  const bearer = auth === 'bearer'
  // An API-key webhook holds no secret: nothing to reveal, rotate or check.
  const apiKey = auth === 'api_key'
  // Sentry and PagerDuty sign with their own secret, pasted below: rotating would overwrite it.
  const vendorSecretLabel = VENDOR_SECRET_LABELS[when.source_kind] ?? null
  const revealed = { ...automation, when: { ...when, secret_state: 'revealed' } }
  // Jaah's own HMAC scheme only: a bearer or Datadog sender sends the secret itself, and a monitor signs its own way.
  const check = bearer || apiKey || MONITOR_SOURCE_KINDS.has(when.source_kind) ? null : (
    <Button variant="outlined" onClick={() => setChecking(true)} sx={tapTargetSx} data-testid="automation-check-signature">
      Check signature
    </Button>
  )

  const run = (call: () => Promise<{ secret: string }>, fallback: string) => submit(fallback, async () => {
    try {
      const { secret } = await call()
      onAutomation(revealed)
      onSecret(secret)
    } catch (err) {
      // 409: the one reveal already happened (another tab, another admin) — stop offering it.
      if (err instanceof ApiError && err.status === 409) onAutomation(revealed)
      throw err
    }
  })

  const rotate = async () => {
    const ok = await confirm(danger('Rotate secret', {
      title: 'Rotate secret',
      identity: automation.name,
      message: 'The old secret stops working at once. Update every sender with the new one.',
    }))
    if (ok) await run(() => rotateAutomationSecret(automation.id), 'Couldn’t rotate the secret.')
  }

  const saveClientSecret = () => pasted.run('Couldn’t save the secret.', async () => {
    await setAutomationSecret(automation.id, clientSecret.trim())
    setClientSecret('')
    onAutomation(revealed)
  })

  return (
    <DetailSection title="Endpoint" testId="automation-section-setup">
      {when.source_kind === 'github' && when.ingress_url ? (
        <ForgeSetup
          forge="github"
          events={Array.isArray(when.source_config.events) ? (when.source_config.events as string[]) : []}
          repository={typeof when.source_config.repository === 'string' ? when.source_config.repository : ''}
          url={when.ingress_url}
        />
      ) : isVendorKind(when.source_kind) && when.ingress_url ? (
        <VendorSetup kind={when.source_kind} url={when.ingress_url} />
      ) : when.ingress_url ? (
        <CopyableValue value={when.ingress_url} copyLabel="Copy URL" sx={{ overflowWrap: 'anywhere' }} />
      ) : (
        <Banner severity="warning" testId="automation-no-ingress">No public address.</Banner>
      )}
      {when.source_kind === 'webhook' && (
        <Box data-testid="automation-auth-hint" sx={{ overflowWrap: 'anywhere' }}>
          <KeyValueList labelWidth={grid(24)}>
            <KeyValue label={<HelpLabel label="Header" help={apiKey ? 'A key with Tasks: create.' : bearer ? undefined : 'HMAC-SHA256 of the raw body, keyed with the secret.'} />}>
              <Box sx={{ display: 'flex', alignItems: 'center', gap: 1, flexWrap: 'wrap' }}>
                <CopyableValue
                  value={apiKey ? 'Authorization: Bearer <API key>' : bearer ? 'Authorization: Bearer <secret>' : 'X-Jaah-Signature-256: sha256=<hex>'}
                  copyLabel="Copy"
                />
                {apiKey && <Link component={RouterLink} to={API_KEYS_URL} variant="body2">API keys</Link>}
              </Box>
            </KeyValue>
          </KeyValueList>
        </Box>
      )}
      {error && <Banner severity="error">{error}</Banner>}
      {!apiKey && (when.secret_state === 'owed' || (when.secret_state === 'revealed' && !vendorSecretLabel)) && (
        <Box sx={{ display: 'flex', alignItems: 'center', gap: 1.5, flexWrap: 'wrap' }}>
          {when.secret_state === 'revealed' ? (
            <Button variant="outlined" disabled={busy} onClick={() => void rotate()} sx={tapTargetSx} data-testid="automation-rotate-secret">
              Rotate secret
            </Button>
          ) : (
            <Button variant="outlined" disabled={busy} sx={tapTargetSx} data-testid="automation-reveal-secret"
              onClick={() => void run(() => revealAutomationSecret(automation.id), 'Couldn’t reveal the secret.')}>
              Reveal secret (one time)
            </Button>
          )}
          {check}
        </Box>
      )}
      {vendorSecretLabel && (
        <FormBox onSubmit={saveClientSecret}
          sx={{ display: 'flex', alignItems: 'flex-start', gap: 1.5, flexWrap: 'wrap' }}>
          <Box sx={{ flex: `1 1 ${grid(42.5)}`, minWidth: 0 }}>
            <FormTextField
              label={vendorSecretLabel}
              help={VENDOR_SECRET_HELP}
              type="password"
              autoComplete="new-password"
              value={clientSecret}
              onChange={v => { setClientSecret(v); pasted.clearSaved() }}
              disabled={pasted.busy}
              testId="automation-vendor-secret"
            />
          </Box>
          <Button type="submit" variant="outlined" disabled={pasted.busy || clientSecret.trim() === ''} sx={tapTargetSx}
            data-testid="automation-vendor-secret-save">
            Save
          </Button>
          <SavedNote show={pasted.saved} sx={{ alignSelf: 'center' }} />
        </FormBox>
      )}
      {pasted.formError && <Banner severity="error">{pasted.formError}</Banner>}
      {/* Mounted only while open: closing drops every typed field. */}
      {checking && <SignatureCheckDialog onClose={() => setChecking(false)} />}
    </DetailSection>
  )
}

const AZURE_DEVOPS_EVENT_LABELS: Record<string, string> = {
  'git.push': 'Code pushed',
  'ms.vss-pipelines.run-state-changed-event': 'Run state changed',
  'ms.vss-pipelines.stage-state-changed-event': 'Run stage state changed',
  'ms.azure-devops-release.deployment-completed-event': 'Release deployment completed',
}

const BITBUCKET_TRIGGER_LABELS: Record<string, string> = {
  'repo:push': 'Push',
  'repo:commit_status_created': 'Build status created',
  'repo:commit_status_updated': 'Build status updated',
}

/** An Azure DevOps `organization/project/repo` as people read it: a stored repo URL keeps `My%20Project`. */
export function azureRepositoryLabel(repository: string): string {
  try {
    return decodeURIComponent(repository)
  } catch {
    return repository
  }
}

/* Each forge's own webhook form, field by field in its own order: what to paste into each box and which events to
 * tick. `secretPending` is the secret row's text while the caller does not hold the secret. */
const FORGE_SETUPS: Record<Forge, {
  testId: string
  repositoryLabel: string
  repository?: (repository: string) => string
  urlLabel: string
  fixedRows?: { label: string; value: string }[]
  secretLabel: string
  secretPending: (secret: string | null | undefined) => string
  eventsLabel: string
  eventsHelp: string
  eventsTestId: (testId: string) => string
  eventLabel: (event: string) => string
  copyEvent?: boolean
}> = {
  /* GitHub's repo-webhook form. The secret: `null` when it was shown at creation, else revealed once below. Also the
   * Production QA tab's Connect releases answer (`ProductionQaConnectDeploys.tsx`). */
  github: {
    testId: 'automation-github-setup',
    repositoryLabel: 'Repository',
    urlLabel: 'Payload URL',
    fixedRows: [{ label: 'Content type', value: 'application/json' }],
    secretLabel: 'Secret',
    secretPending: secret => (secret === null ? 'Shown once, when created' : 'Below'),
    eventsLabel: 'Events',
    eventsHelp: 'Let me select individual events',
    eventsTestId: () => 'automation-github-events-to-tick',
    eventLabel: githubEventLabel,
    copyEvent: true,
  },
  /* GitLab's Add webhook form (Settings › Webhooks): the URL, the Secret token (sent back in `X-Gitlab-Token`), and
   * the Trigger boxes to tick. */
  gitlab: {
    testId: 'automation-gitlab-setup',
    repositoryLabel: 'Project',
    urlLabel: 'URL',
    secretLabel: 'Secret token',
    secretPending: secret => (secret === null ? 'Shown once, when created' : 'Below'),
    eventsLabel: 'Trigger',
    eventsHelp: 'Tick only these',
    eventsTestId: testId => `${testId}-events`,
    eventLabel: gitlabEventLabel,
  },
  /* Azure DevOps service hooks (Web Hooks consumer), one subscription per event: Azure DevOps does not sign, so the
   * secret is the basic-auth password (`app/production_qa/azure_devops.py`). */
  azure_devops: {
    testId: 'automation-azure-devops-setup',
    repositoryLabel: 'Repository',
    repository: azureRepositoryLabel,
    urlLabel: 'URL',
    fixedRows: [{ label: 'Username', value: 'jaah' }],
    secretLabel: 'Password',
    secretPending: () => 'Shown once, when created',
    eventsLabel: 'Events',
    eventsHelp: 'One subscription each',
    eventsTestId: testId => `${testId}-events`,
    eventLabel: event => AZURE_DEVOPS_EVENT_LABELS[event] ?? event,
  },
  /* Bitbucket Cloud's Add webhook form (Repository settings › Webhooks): its triggers are the push and the two
   * build-status events deploy ingestion reads (`app/production_qa/bitbucket.py`). */
  bitbucket: {
    testId: 'automation-bitbucket-setup',
    repositoryLabel: 'Repository',
    urlLabel: 'URL',
    secretLabel: 'Secret',
    secretPending: () => 'Shown once, when created',
    eventsLabel: 'Triggers',
    eventsHelp: 'Choose from a full list of triggers',
    eventsTestId: testId => `${testId}-triggers`,
    eventLabel: event => BITBUCKET_TRIGGER_LABELS[event] ?? event,
  },
}

/* The secret: `secret` when the caller holds it, else the forge's pending text. An unknown `forge` reads as GitHub. */
export function ForgeSetup({ forge, events, repository, url, secret, testId }: {
  forge: Forge
  events: string[]
  repository: string
  url: string
  secret?: string | null
  testId?: string
}) {
  const spec = FORGE_SETUPS[forge] ?? FORGE_SETUPS.github
  const id = testId ?? spec.testId
  return (
    <KeyValueList labelWidth={grid(24)} testId={id}>
      {repository && <KeyValue label={spec.repositoryLabel}><Mono>{spec.repository?.(repository) ?? repository}</Mono></KeyValue>}
      <KeyValue label={spec.urlLabel}>
        <CopyableValue value={url} copyLabel="Copy URL" sx={{ overflowWrap: 'anywhere' }} />
      </KeyValue>
      {spec.fixedRows?.map(row => (
        <KeyValue key={row.label} label={row.label}>
          <CopyableValue value={row.value} copyLabel="Copy" />
        </KeyValue>
      ))}
      <KeyValue label={spec.secretLabel}>
        {secret
          ? <CopyableValue value={secret} copyLabel="Copy secret" testId={`${id}-secret`} sx={{ overflowWrap: 'anywhere' }} />
          : <Typography variant="body2">{spec.secretPending(secret)}</Typography>}
      </KeyValue>
      <KeyValue label={<HelpLabel label={spec.eventsLabel} help={spec.eventsHelp} />}>
        <Box component="ul" sx={{ m: 0, pl: 2.5 }} data-testid={spec.eventsTestId(id)}>
          {events.map(event => {
            const label = <Typography variant="body2" sx={{ fontWeight: FONT_WEIGHT.medium }}>{spec.eventLabel(event)}</Typography>
            return (
              <Box component="li" key={event}>
                {spec.copyEvent
                  ? <Box sx={{ display: 'flex', alignItems: 'center', gap: 1, flexWrap: 'wrap' }}>{label}<CopyableValue value={event} copyLabel="Copy" /></Box>
                  : label}
              </Box>
            )
          })}
        </Box>
      </KeyValue>
    </KeyValueList>
  )
}

/* ONE panel per monitor vendor: where the URL goes in the vendor's own form (`urlLabel`), its other rows, and the
 * hint naming the vendor's screen. The vendor mints the signing secret where the hint says to paste it here. */
type VendorKind = 'sentry' | 'datadog' | 'pagerduty' | 'cloudwatch'
const VENDOR_SETUP: Record<VendorKind, { urlLabel: string; rows?: ReactNode; hint?: string }> = {
  // Sentry's Custom Integration form; its Client Secret is the signing secret.
  sentry: {
    urlLabel: 'Webhook URL',
    hint: 'In Sentry: Settings → Custom Integrations → Webhook URL = this URL; turn on Alert Rule Action and the Issue '
      + 'webhook; paste its Client Secret here.',
  },
  // Datadog's Webhooks integration: the custom header carries the secret (Datadog does not sign), and the payload
  // is what the adapter reads.
  datadog: {
    urlLabel: 'URL',
    rows: (
      <>
        <KeyValue label={<HelpLabel label="Custom headers" help="Keep the secret in a hidden custom variable." />}>
          <CopyableValue value='{"X-Jaah-Token": "<secret>"}' copyLabel="Copy" />
        </KeyValue>
        <KeyValue label="Payload">
          <Stack spacing={0.5} sx={{ alignItems: 'flex-start' }}>
            <CodeBlock testId="automation-datadog-payload" bordered>{DATADOG_PAYLOAD}</CodeBlock>
            <CopyButton text={() => DATADOG_PAYLOAD} label="Copy payload" testId="automation-datadog-payload-copy" />
          </Stack>
        </KeyValue>
      </>
    ),
  },
  // PagerDuty's Generic Webhook (v3) subscription: the event types the adapter reads.
  pagerduty: {
    urlLabel: 'Webhook URL',
    rows: (
      <KeyValue label="Event subscription">
        <Box sx={{ display: 'flex', gap: 1, flexWrap: 'wrap' }}>
          {PAGERDUTY_EVENTS.map(event => <CopyableValue key={event} value={event} copyLabel="Copy" />)}
        </Box>
      </KeyValue>
    ),
    hint: 'In PagerDuty: Integrations → Generic Webhooks (v3) → New Webhook; paste its signing secret here.',
  },
  // An HTTPS subscription to the SNS topic the alarms publish to: AWS signs every message, Jaah confirms the
  // subscription itself.
  cloudwatch: {
    urlLabel: 'Endpoint',
    rows: <KeyValue label="Protocol"><Mono>HTTPS</Mono></KeyValue>,
    hint: "In SNS: the alarms' topic → Create subscription. Jaah confirms it while active.",
  },
}

function isVendorKind(kind: string): kind is VendorKind {
  return kind in VENDOR_SETUP
}

function VendorSetup({ kind, url }: { kind: VendorKind; url: string }) {
  const { urlLabel, rows, hint } = VENDOR_SETUP[kind]
  return (
    <Stack spacing={1.5} data-testid={`automation-${kind}-setup`}>
      <KeyValueList labelWidth={grid(24)}>
        <KeyValue label={urlLabel}>
          <CopyableValue value={url} copyLabel="Copy URL" sx={{ overflowWrap: 'anywhere' }} />
        </KeyValue>
        {rows}
      </KeyValueList>
      {hint && <Typography variant="body2" sx={{ color: 'text.secondary' }}>{hint}</Typography>}
    </Stack>
  )
}

/* ── Test: render the UNSAVED templates over a pasted sample, or fire it for real ─────────────────── */

type TestRender = { title: string; body: string; missing_paths: string[] }

export function EventTestPanel({
  automation,
  bodyTemplate,
  titleTemplate,
  onRan,
}: {
  automation: Automation
  bodyTemplate: string
  titleTemplate: string
  onRan: (detail: string) => void
}) {
  const kind = automation.when.kind === 'event' ? automation.when.source_kind : 'webhook'
  const [sample, setSample] = useState('')
  const [jsonError, setJsonError] = useState('')
  const [error, setError] = useState('')
  const [result, setResult] = useState<TestRender | null>(null)
  const [running, setRunning] = useState<'test' | 'run' | null>(null)
  /** The body as stored (markdown source) rather than as the task page draws it. */
  const [raw, setRaw] = useState(false)

  /** The parsed sample, or null (with the sentence shown) when it is not JSON. */
  const parsedSample = (): Record<string, unknown> | null => {
    setJsonError('')
    setError('')
    try {
      return sampleEvent(kind, sample.trim() === '' ? '{}' : sample)
    } catch (err) {
      setJsonError(`Not valid JSON: ${err instanceof Error ? err.message : String(err)}`)
      setResult(null)
      return null
    }
  }

  /** Hands the parsed sample to `call` while `mode` runs; a failure goes to `refused`. */
  const fire = async (mode: 'test' | 'run', call: (event: Record<string, unknown>) => Promise<void>, refused: (err: unknown) => void) => {
    const event = parsedSample()
    if (event === null) return
    setRunning(mode)
    try {
      await call(event)
    } catch (err) {
      refused(err)
    } finally {
      setRunning(null)
    }
  }
  const runTest = () => fire('test',
    async event => setResult(await testRenderAutomation(automation.id, event, { prompt_template: bodyTemplate, title_template: titleTemplate })),
    err => { setResult(null); setError(readableError(err, 'Couldn’t render the template.')) })
  const runNow = () => fire('run',
    async event => onRan((await runAutomation(automation.id, event)).detail),
    err => setError(apiDetail(err, 'Couldn’t run the automation.')))

  return (
    <Stack spacing={1.5} sx={{ maxWidth: SIZE.measure }}>
      <FormTextField
        label="Sample payload"
        help={SAMPLE_PAYLOAD_HELP}
        error={jsonError || undefined}
        value={sample}
        multiline
        minRows={4}
        placeholder={templateOf(kind).sample}
        onChange={setSample}
        sx={{ '& textarea': monoSx }}
        testId="automation-test-sample"
      />
      <Box sx={{ display: 'flex', gap: 1, flexWrap: 'wrap' }}>
        <Button variant="outlined" disabled={running !== null} onClick={() => void runTest()} sx={tapTargetSx} data-testid="automation-test-run">
          {running === 'test' ? 'Testing…' : 'Test'}
        </Button>
        <Button
          variant="outlined"
          disabled={running !== null}
          onClick={() => void runNow()}
          sx={tapTargetSx}
          data-testid="automation-run-sample"
        >
          {running === 'run' ? 'Running…' : 'Run now with this sample'}
        </Button>
      </Box>
      {error && <Banner severity="error">{error}</Banner>}
      {result && (
        <Stack spacing={1}>
          <Typography variant="caption" sx={{ color: 'text.secondary' }}>Title</Typography>
          <Typography variant="body2" data-testid="automation-test-title" sx={{ overflowWrap: 'anywhere' }}>
            {result.title || 'Taken from the body.'}
          </Typography>
          <Box sx={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 1 }}>
            <Typography variant="caption" sx={{ color: 'text.secondary' }}>Body</Typography>
            <Button size="small" onClick={() => setRaw(r => !r)} sx={tapTargetSx} data-testid="automation-test-body-view">
              {raw ? 'Show rendered' : 'Show raw'}
            </Button>
          </Box>
          {/* Rendered as the task page draws a description (`TaskSpecFields`' read-only arm), so a hard break is a
              line, not the markdown's `\`; the stored text is one click away. */}
          {raw ? (
            <CodeBlock testId="automation-test-body" size="medium" bordered>{result.body}</CodeBlock>
          ) : (
            <Box
              data-testid="automation-test-body"
              sx={{
                border: '1px solid', borderColor: 'divider', borderRadius: RADIUS.md, bgcolor: 'background.paper', p: 2,
                overflowWrap: 'anywhere',
                '& > .rte-content': { p: 0 },
                '& .ProseMirror > :first-child': { mt: 0 },
                '& .ProseMirror > :last-child': { mb: 0 },
              }}
            >
              <RichTextRenderer content={result.body} />
            </Box>
          )}
          <Typography variant="body2" data-testid="automation-test-missing" sx={{ overflowWrap: 'anywhere' }}>
            {result.missing_paths.length > 0
              ? `Missing paths: ${result.missing_paths.join(', ')}`
              : 'Every placeholder had a value.'}
          </Typography>
        </Stack>
      )}
    </Stack>
  )
}
