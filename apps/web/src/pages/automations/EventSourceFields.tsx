import { type ReactNode, useEffect, useRef, useState } from 'react'
import { Link as RouterLink } from 'react-router-dom'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import Link from '@mui/material/Link'
import Stack from '@mui/material/Stack'
import TextField from '@mui/material/TextField'
import Typography from '@mui/material/Typography'
import { apiDetail, ApiError } from '../../api'
import type { useConfirm } from '../../components/ConfirmDialog'
import { useSubmit } from '../../hooks/useSubmit'
import Banner from '../../ui/Banner'
import { CopyableValue } from '../../ui/CopyableValue'
import CredentialSelect from '../../ui/CredentialSelect'
import { credentialMark } from '../../ui/credentialMarks'
import { DetailSection } from '../../ui/detail/DetailPage'
import { Mono } from '../../ui/DetailFields'
import FormTextField from '../../ui/FormTextField'
import { fieldTapTargetSx, monoSx, tapTargetSx } from '../../ui/listStyles'
import SettingCheckbox from '../../ui/SettingCheckbox'
import { loadCredentialOptions } from '../credentials/api'
import { API_KEYS_URL } from '../settings/apiKeys/api'
import { EMAIL_ADDRESSES_URL, getOrgEmail, ORG_EMAIL_KEY, type OrgEmail } from '../settings/email/api'
import NewAddressDialog from '../settings/email/NewAddressDialog'
import { githubEventLabel, HTTP_SOURCE_KINDS } from '../triggers/vocabulary'
import SignatureCheckDialog from './SignatureCheck'
import {
  connectedGithubRepos,
  revealAutomationSecret,
  rotateAutomationSecret,
  runAutomation,
  testRenderAutomation,
  type Automation,
  type EventWhen,
} from './api'
import { CodeBlock } from '../../ui/CodeBlock'
import RichTextRenderer from '../../components/richtext/RichTextRenderer'

/* An event Automation's When — the source's settings, its preflight, limits and auto-pause (lifted from the
 * retired trigger editor), plus the Setup (where a sender posts, the one-time secret) and Test panels.
 *
 * THE DRAFT IS STRINGS: every number field holds what was typed, and `eventWhenOf` turns the draft into the wire
 * `when` or refuses with a sentence — a cleared number never reaches the server as `NaN`/`null` by accident.
 * The source kind is picked by the editor's When control; this form only draws that kind's fields. */

/** Each kind's template namespaces (`app/trigger_template.py`): a pasted sample whose top-level keys all
 *  sit inside these is sent as-is; anything else is wrapped in the kind's own namespace. */
const SAMPLE_NAMESPACES: Record<string, { wrap: string; keys: readonly string[] }> = {
  webhook: { wrap: 'body', keys: ['body', 'headers', 'query', 'item'] },
  github: { wrap: 'body', keys: ['body', 'headers', 'query', 'item'] },
  gmail: { wrap: 'email', keys: ['email', 'item'] },
  slack: { wrap: 'slack', keys: ['slack', 'item'] },
  user_registered: { wrap: 'user', keys: ['user', 'item'] },
}

const SAMPLE_PLACEHOLDER: Record<string, string> = {
  webhook: '{"issue": {"title": "Login broken"}}',
  github: '{"action": "opened", "issue": {"title": "Login broken"}}',
  gmail: 'Paste {"from": "…", "subject": "…", "body": "…"}',
  slack: 'Paste {"text": "…", "user": "…", "channel": "…", "permalink": "…"}',
  user_registered: 'Paste {"email": "…", "name": "…", "org_name": "…", "provider": "…"}',
}

const PLACEHOLDERS: Record<string, string> = {
  webhook: '{{body.…}}, {{headers.…}}, {{query.…}}',
  github: '{{body.…}}, {{headers.…}}, {{query.…}}',
  gmail: '{{email.from}}, {{email.subject}}, {{email.body}}',
  slack: '{{slack.text}}, {{slack.user}}, {{slack.channel}}, {{slack.permalink}}',
  user_registered: '{{user.email}}, {{user.name}}, {{user.org_name}}, {{user.provider}}, {{user.url}}',
}

/** The hint under a body template, per source kind. */
export const EVENT_TEMPLATE_HINT: Record<string, string> = Object.fromEntries(
  Object.entries(PLACEHOLDERS).map(([kind, names]) => [kind, `Placeholders: ${names}; with a preflight, {{item.id}}, {{item.…}}.`]),
)

export const PREFLIGHT_LABELS: Record<string, string> = { none: 'None', script: 'Script', model: 'Model' }

/** A `user_registered` source's enumerated values, in the words the form and the review show them with. */
export const USER_PROVIDER_LABELS: Record<string, string> = { google: 'Google', github: 'GitHub', microsoft: 'Microsoft', email: 'Email' }
export const USER_ACCOUNT_TYPE_LABELS: Record<string, string> = { individual: 'Individual', organization: 'Organization' }
export const USER_SCOPE_LABELS: Record<string, string> = { org: 'Your members', platform: 'Sign-ups on Jaah' }
export const USER_MOMENT_LABELS: Record<string, string> = { applied: 'Application submitted', approved: 'Organization approved' }

/** A webhook source's `auth`. Only `api_key` is offered: `hmac`/`bearer` are legacy secrets a saved source keeps until
 *  its owner switches it, and the server refuses a switch to either. */
export const WEBHOOK_AUTH_LABELS: Record<string, string> = { api_key: 'API key', hmac: 'HMAC signature', bearer: 'Bearer token' }
const isLegacyWebhookAuth = (auth: string) => auth === 'hmac' || auth === 'bearer'
/** A saved webhook source's `auth` (`trigger_secrets.webhook_auth`): one saved before `auth` existed is HMAC-signed.
 *  Empty for any other kind. */
const savedWebhookAuth = (when: EventWhen) => (when.source_kind === 'webhook' ? str(when.source_config?.auth) || 'hmac' : '')

/** A GitHub source's "Any repository", in the draft. Never a valid `owner/repo`, so it cannot collide with one; sent
 *  as `""`, which the server keeps as any. An untouched blank (`''`) is sent as null instead, which a create fills
 *  with the project's primary (`_default_github_repository`) — so a wizard that reaches Target after When still
 *  defaults. */
export const ANY_REPOSITORY = '*'

const MIN_TIMEOUT = 5
const MAX_TIMEOUT = 165

export type EventDraft = {
  sourceKind: string
  webhookAuth: string
  /** The saved webhook source's `auth`; '' for a new one. A legacy value keeps the Authentication choice drawn. */
  webhookSavedAuth: string
  githubEvents: string
  githubActions: string
  githubRepository: string
  /** Where an Email source receives (`GMAIL_SYSTEM`, `address:<id>`, `connection:<id>`); '' = not picked. */
  gmailInbox: string
  gmailTo: string
  gmailFrom: string
  gmailSubject: string
  slackChannels: string
  userScope: string
  userMoment: string
  userProviders: string
  userAccountTypes: string
  userDomains: string
  preflightKind: string
  preflightScriptPath: string
  preflightPrompt: string
  preflightCredentialId: string | null
  preflightTimeout: string
  concurrencyCap: string
  queueDepth: string
  rateLimit: string
  budget: string
  autoPauseFailuresOn: boolean
  autoPauseFailures: string
  autoPauseIdleOn: boolean
  autoPauseIdleDays: string
}

export function newEventDraft(sourceKind: string): EventDraft {
  return {
    sourceKind,
    webhookAuth: 'api_key',
    webhookSavedAuth: '',
    githubEvents: '',
    githubActions: '',
    githubRepository: '',
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
    preflightKind: 'none',
    preflightScriptPath: '',
    preflightPrompt: '',
    preflightCredentialId: null,
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

const str = (value: unknown): string => (typeof value === 'string' ? value : '')
const list = (value: unknown): string => (Array.isArray(value) ? value.filter(v => typeof v === 'string').join(', ') : '')
const splitList = (raw: string): string[] => raw.split(',').map(v => v.trim()).filter(Boolean)
const orNull = (raw: string): string | null => (raw.trim() === '' ? null : raw.trim())

/* The server names the refused field by its wire key ("when: rate_limit_per_hour: must be …",
 * "action.params.body: …"). The page says it with the field's own label instead — no internal code reaches
 * the user. */
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
  const label = match ? FIELD_LABELS[match[1]] : undefined
  return match && label ? `${label}: ${match[2]}` : detail
}

export function eventDraftOf(when: EventWhen): EventDraft {
  const config = when.source_config ?? {}
  // A webhook saved before `auth` existed is HMAC-signed; any other kind turned into a webhook here is a new one.
  const savedAuth = savedWebhookAuth(when)
  return {
    sourceKind: when.source_kind,
    webhookAuth: savedAuth || 'api_key',
    webhookSavedAuth: savedAuth,
    githubEvents: list(config.events),
    githubActions: list(config.actions),
    // A saved source with no repository listens to any.
    githubRepository: str(config.repository) || ANY_REPOSITORY,
    gmailInbox: when.source_kind === 'gmail' ? inboxChoiceOf(config) : '',
    gmailTo: str(config.to),
    gmailFrom: str(config.from),
    gmailSubject: str(config.subject),
    slackChannels: list(config.channel_ids),
    userScope: str(config.scope) || 'org',
    userMoment: str(config.moment) || 'applied',
    userProviders: list(config.providers),
    userAccountTypes: list(config.account_types),
    userDomains: list(config.email_domains),
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

/** An Email source's "Receives at" values: the deployment's own mail domain (operator org only), one of the org's
 *  hosted addresses, or one of its service-account mailboxes. */
export const GMAIL_SYSTEM = 'system'
/** How the server reads a From rule (`trigger_intake.from_rule_matches`). */
export const FROM_RULE_HINT = 'An address matches only itself; a domain (@acme.com) also matches its subdomains; other text matches anywhere.'
const ADDRESS_PREFIX = 'address:'
const CONNECTION_PREFIX = 'connection:'
/** "Receives at"'s last option: opens New address and selects what it makes; never a stored value. */
const NEW_ADDRESS = 'new-address'

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

/** The kind's `source_config`, in the shape `app/trigger_config.py` validates. */
function sourceConfigOf(draft: EventDraft): Record<string, unknown> {
  switch (draft.sourceKind) {
    case 'webhook':
      return { auth: draft.webhookAuth }
    case 'github':
      return {
        events: splitList(draft.githubEvents),
        actions: splitList(draft.githubActions),
        repository: draft.githubRepository === ANY_REPOSITORY ? '' : orNull(draft.githubRepository),
      }
    case 'gmail':
      return {
        to: orNull(draft.gmailTo), from: orNull(draft.gmailFrom), subject: orNull(draft.gmailSubject),
        ...inboxConfigOf(draft.gmailInbox),
      }
    case 'slack':
      return { channel_ids: splitList(draft.slackChannels) }
    case 'user_registered': {
      // Moment and account type are a platform sign-up's; the server refuses them on an org scope.
      const platform = draft.userScope === 'platform'
      return {
        scope: draft.userScope,
        moment: platform ? draft.userMoment : 'applied',
        providers: splitList(draft.userProviders),
        account_types: platform ? splitList(draft.userAccountTypes) : [],
        email_domains: splitList(draft.userDomains),
      }
    }
    default:
      return {}
  }
}

/** A whole number from a typed field, or the sentence that says why not. */
function wholeNumber(raw: string, label: string): number | string {
  const value = Number(raw.trim())
  if (raw.trim() === '' || !Number.isInteger(value)) return `${label} must be a whole number.`
  return value
}

/** The draft as the wire `when` (`automation_service.EVENT_FIELDS`), or the first sentence that stops it. */
export function eventWhenOf(draft: EventDraft): ({ kind: 'event' } & Record<string, unknown>) | string {
  const numbers = {
    preflight_timeout_seconds: wholeNumber(draft.preflightTimeout, 'Preflight timeout'),
    concurrency_cap: wholeNumber(draft.concurrencyCap, 'Concurrency cap'),
    queue_depth: wholeNumber(draft.queueDepth, 'Queue depth'),
    rate_limit_per_hour: wholeNumber(draft.rateLimit, 'Rate limit'),
    auto_pause_failures: draft.autoPauseFailuresOn ? wholeNumber(draft.autoPauseFailures, 'Failures before pause') : null,
    auto_pause_idle_days: draft.autoPauseIdleOn ? wholeNumber(draft.autoPauseIdleDays, 'Idle days before pause') : null,
  }
  for (const value of Object.values(numbers)) if (typeof value === 'string') return value
  if (draft.sourceKind === 'gmail') {
    if (draft.gmailInbox === '') return 'Pick where it receives.'
    const anyRule = [draft.gmailTo, draft.gmailFrom, draft.gmailSubject].some(v => v.trim() !== '')
    if (draft.gmailInbox === GMAIL_SYSTEM && !anyRule) return 'The system mail domain needs a To, From or Subject.'
  }
  let budget: number | null = null
  if (draft.budget.trim() !== '') {
    budget = Number(draft.budget.trim())
    if (!Number.isFinite(budget)) return 'Budget must be a number.'
  }
  return {
    kind: 'event',
    source_kind: draft.sourceKind,
    source_config: sourceConfigOf(draft),
    preflight_kind: draft.preflightKind,
    preflight_script_path: orNull(draft.preflightScriptPath),
    preflight_prompt: orNull(draft.preflightPrompt),
    preflight_credential_id: draft.preflightCredentialId,
    ...numbers,
    budget_usd: budget,
  }
}

/** The pasted sample as the event the template reads. Throws on invalid JSON. */
function sampleEvent(kind: string, raw: string): Record<string, unknown> {
  const parsed: unknown = JSON.parse(raw)
  const spaces = SAMPLE_NAMESPACES[kind] ?? SAMPLE_NAMESPACES.webhook
  if (parsed !== null && typeof parsed === 'object' && !Array.isArray(parsed)) {
    const keys = Object.keys(parsed)
    if (keys.length > 0 && keys.every(key => spaces.keys.includes(key))) return parsed as Record<string, unknown>
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
  draft,
  onChange,
  disabled,
  creating,
  operator = false,
  projectId = null,
  orgId = '',
  operatorOrg = false,
  automationId = null,
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
  const edit = <K extends keyof EventDraft>(key: K, value: EventDraft[K]) => onChange({ ...draft, [key]: value })
  return (
    <>
      <SourceConfigFields draft={draft} edit={edit} disabled={disabled} creating={creating} operator={operator} projectId={projectId}
        orgId={orgId} operatorOrg={operatorOrg} automationId={automationId} />

      <SubHeading>Preflight</SubHeading>
      <FormTextField
        label="Preflight"
        help="May split one event into several items."
        value={draft.preflightKind}
        disabled={disabled}
        testId="automation-preflight-kind"
        onChange={v => edit('preflightKind', v)}
        options={Object.entries(PREFLIGHT_LABELS).map(([value, label]) => ({ value, label }))}
      />
      {draft.preflightKind === 'script' && (
        <FormTextField
          label="Script path"
          required
          hint="Repo-relative, e.g. scripts/triage.py"
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
            required
            value={draft.preflightPrompt}
            disabled={disabled}
            multiline
            minRows={3}
            onChange={v => edit('preflightPrompt', v)}
            testId="automation-preflight-prompt"
          />
          <CredentialSelect
            id="automation-preflight-credential"
            label="Anthropic API key"
            value={draft.preflightCredentialId}
            onChange={next => edit('preflightCredentialId', next)}
            fetchOptions={fetchAnthropicKeys}
            disabled={disabled}
            helperText="Blank uses the platform key, if one is set."
            fullWidth
            size="small"
            testId="automation-preflight-credential"
          />
        </>
      )}
      {draft.preflightKind !== 'none' && (
        <FormTextField
          label="Timeout (seconds)"
          hint={`${MIN_TIMEOUT}–${MAX_TIMEOUT}.`}
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
        <FormTextField label="Concurrency cap" type="number" value={draft.concurrencyCap} disabled={disabled}
          slotProps={{ htmlInput: { min: 1, max: 20, step: '1' } }}
          testId="automation-concurrency" onChange={v => edit('concurrencyCap', v)} />
        <FormTextField label="Queue depth" type="number" value={draft.queueDepth} disabled={disabled}
          slotProps={{ htmlInput: { min: 0, max: 100, step: '1' } }}
          testId="automation-queue-depth" onChange={v => edit('queueDepth', v)} />
        <FormTextField label="Rate limit per hour" type="number" value={draft.rateLimit} disabled={disabled}
          slotProps={{ htmlInput: { min: 1, max: 1000, step: '1' } }} testId="automation-rate-limit" onChange={v => edit('rateLimit', v)} />
        <FormTextField label="Budget (USD)" type="number" value={draft.budget} disabled={disabled}
          slotProps={{ htmlInput: { min: 0, step: '0.01' } }} testId="automation-budget" onChange={v => edit('budget', v)}
          help="Advisory: a Task over budget is flagged, never stopped." />
      </FieldGrid>

      <SubHeading>Auto-pause</SubHeading>
      <ToggledNumber
        name="auto_pause_failures"
        label="Pause after consecutive failures"
        on={draft.autoPauseFailuresOn}
        value={draft.autoPauseFailures}
        disabled={disabled}
        testId="automation-auto-pause-failures"
        onToggle={on => edit('autoPauseFailuresOn', on)}
        onChange={value => edit('autoPauseFailures', value)}
      />
      <ToggledNumber
        name="auto_pause_idle_days"
        label="Pause after idle days"
        on={draft.autoPauseIdleOn}
        value={draft.autoPauseIdleDays}
        disabled={disabled}
        testId="automation-auto-pause-idle"
        onToggle={on => edit('autoPauseIdleOn', on)}
        onChange={value => edit('autoPauseIdleDays', value)}
      />
    </>
  )
}

function SourceConfigFields({
  draft,
  edit,
  disabled,
  creating,
  operator,
  projectId,
  orgId,
  operatorOrg,
  automationId,
}: {
  draft: EventDraft
  edit: <K extends keyof EventDraft>(key: K, value: EventDraft[K]) => void
  disabled: boolean
  creating: boolean
  operator: boolean
  projectId: number | null
  orgId: string
  operatorOrg: boolean
  automationId: string | null
}) {
  const text = (key: keyof EventDraft, label: string, testId: string, props: { hint?: string; required?: boolean; placeholder?: string } = {}) => (
    <FormTextField
      label={label}
      {...props}
      value={draft[key] as string}
      disabled={disabled}
      onChange={v => edit(key, v as EventDraft[typeof key])}
      testId={testId}
    />
  )
  switch (draft.sourceKind) {
    case 'webhook':
      // An API key is the only scheme: the choice is drawn only for a legacy secret, to switch it.
      if (!isLegacyWebhookAuth(draft.webhookSavedAuth)) return null
      return (
        <>
          <Banner severity="warning" testId="automation-webhook-legacy">Legacy secret. Switch to an API key.</Banner>
          <FormTextField
            label="Authentication"
            value={draft.webhookAuth}
            disabled={disabled}
            testId="automation-webhook-auth"
            onChange={v => edit('webhookAuth', v)}
            options={[draft.webhookSavedAuth, 'api_key'].map(value => ({ value, label: WEBHOOK_AUTH_LABELS[value] }))}
            hint={draft.webhookAuth === 'api_key' ? 'Saving drops the secret.' : undefined}
          />
        </>
      )
    case 'github':
      return (
        <>
          {text('githubEvents', 'Events', 'automation-github-events', { required: true, placeholder: 'issues, pull_request', hint: 'Comma-separated.' })}
          {text('githubActions', 'Actions', 'automation-github-actions', { placeholder: 'opened, labeled', hint: 'Comma-separated. Empty: every action.' })}
          {projectId === null
            ? (
              <FormTextField
                label="Repository"
                placeholder="owner/repo"
                hint={creating ? 'Empty: the project’s primary repository.' : 'Empty: any repository.'}
                value={draft.githubRepository === ANY_REPOSITORY ? '' : draft.githubRepository}
                disabled={disabled}
                onChange={v => edit('githubRepository', v.trim() === '' && !creating ? ANY_REPOSITORY : v)}
                testId="automation-github-repository"
              />
            )
            : (
              <GithubRepositorySelect
                projectId={projectId}
                value={draft.githubRepository}
                onChange={v => edit('githubRepository', v)}
                disabled={disabled}
                creating={creating}
              />
            )}
        </>
      )
    case 'gmail': {
      // An address or a mailbox of the org's own takes every message there; the shared system domain needs a rule.
      const hint = draft.gmailInbox !== '' && draft.gmailInbox !== GMAIL_SYSTEM ? 'Optional.' : undefined
      return (
        <>
          <GmailInboxSelect orgId={orgId} operatorOrg={operatorOrg} automationId={automationId} value={draft.gmailInbox}
            onChange={v => edit('gmailInbox', v)} disabled={disabled} />
          {text('gmailTo', 'To contains', 'automation-gmail-to', { hint })}
          {text('gmailFrom', 'From', 'automation-gmail-from', { hint: [hint, FROM_RULE_HINT].filter(Boolean).join(' ') })}
          {text('gmailSubject', 'Subject contains', 'automation-gmail-subject', { hint })}
        </>
      )
    }
    case 'slack':
      return text('slackChannels', 'Channel IDs', 'automation-slack-channels', {
        required: true,
        placeholder: 'C0123ABCD',
        hint: 'Comma-separated.',
      })
    case 'user_registered': {
      const platform = draft.userScope === 'platform'
      // One box per value, kept in the draft's comma-joined list; the boxes share one wrapping row, and a row
      // with nothing ticked is an any-of over everyone — said once under it, since bare boxes cannot say so.
      const toggles = (key: 'userProviders' | 'userAccountTypes', choices: [string, string][], testId: string) => {
        const picked = splitList(draft[key])
        return (
          <Box>
            <Box sx={{ display: 'flex', flexWrap: 'wrap', columnGap: 2 }}>
              {choices.map(([value, label]) => (
                <SettingCheckbox
                  key={value}
                  name={`${testId}-${value}`}
                  label={label}
                  checked={picked.includes(value)}
                  disabled={disabled}
                  onChange={on => edit(key, (on ? [...picked, value] : picked.filter(v => v !== value)).join(', '))}
                  testId={`${testId}-${value}`}
                />
              ))}
            </Box>
            <Typography variant="caption" sx={{ color: 'text.secondary' }}>None ticked: any.</Typography>
          </Box>
        )
      }
      return (
        <>
          <FormTextField
            label="Who"
            value={draft.userScope}
            disabled={disabled}
            testId="automation-user-scope"
            onChange={v => edit('userScope', v)}
            options={[
              { value: 'org', label: USER_SCOPE_LABELS.org },
              ...(operator || platform ? [{ value: 'platform', label: USER_SCOPE_LABELS.platform }] : []),
            ]}
          />
          {platform && (
            <FormTextField
              label="Fires on"
              value={draft.userMoment}
              disabled={disabled}
              testId="automation-user-moment"
              onChange={v => edit('userMoment', v)}
              options={Object.entries(USER_MOMENT_LABELS).map(([value, label]) => ({ value, label }))}
            />
          )}
          <SubHeading>Sign-in providers</SubHeading>
          {toggles('userProviders', Object.entries(USER_PROVIDER_LABELS), 'automation-user-provider')}
          {platform && (
            <>
              <SubHeading>Account type</SubHeading>
              {toggles('userAccountTypes', Object.entries(USER_ACCOUNT_TYPE_LABELS), 'automation-user-account')}
            </>
          )}
          {text('userDomains', 'Email domains', 'automation-user-domains', { placeholder: 'example.com', hint: 'Exact domains, comma-separated. Empty: any.' })}
        </>
      )
    }
    default:
      return null
  }
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
 *  (on the plan, on a deployment that receives mail), "New address…" makes one here and selects it, so the editor is
 *  never left for Settings. A tenant org with nothing to receive at and nothing to make gets the way to Settings. */
function GmailInboxSelect({ orgId, operatorOrg, automationId, value, onChange, disabled }: {
  orgId: string
  operatorOrg: boolean
  automationId: string | null
  value: string
  onChange: (value: string) => void
  disabled: boolean
}) {
  const queryClient = useQueryClient()
  const [creating, setCreating] = useState(false)
  const key = [...ORG_EMAIL_KEY, orgId]
  const email = useQuery({ queryKey: key, queryFn: () => getOrgEmail(orgId), enabled: orgId !== '' })
  const options = email.data ? inboxOptions(email.data, operatorOrg, automationId, value) : []
  const canCreate = !!email.data && email.data.on_plan && email.data.mail_domain !== ''
  const known = value === '' || !email.data || options.some(option => option.value === value)
  if (email.data && options.length === 0 && !canCreate) {
    return (
      <Typography variant="body2" sx={{ color: 'text.secondary' }} data-testid="automation-gmail-inbox-none">
        No address to receive at. <Link component={RouterLink} to={EMAIL_ADDRESSES_URL}>Add one</Link>
      </Typography>
    )
  }
  return (
    <>
      <FormTextField
        label="Receives at"
        required
        value={value}
        disabled={disabled || email.isLoading}
        testId="automation-gmail-inbox"
        onChange={choice => (choice === NEW_ADDRESS ? setCreating(true) : onChange(choice))}
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
      {creating && (
        <NewAddressDialog orgId={orgId} onClose={() => setCreating(false)} onCreated={row => {
          setCreating(false)
          // In the list before it is selected, so the select never reads it as Removed.
          queryClient.setQueryData<OrgEmail>(key, old => (old ? { ...old, addresses: [...old.addresses, row] } : old))
          void queryClient.invalidateQueries({ queryKey: ORG_EMAIL_KEY })
          onChange(`${ADDRESS_PREFIX}${row.id}`)
        }} />
      )}
    </>
  )
}

/** A GitHub source's Repository: one of the project's connected repos (D9), or any. A new source starts on the
 *  primary, and again whenever the Target moves to a project that does not connect the picked repo. A stored value
 *  the project no longer connects stays listed, marked, so a reopen never silently changes it. */
function GithubRepositorySelect({ projectId, value, onChange, disabled, creating }: {
  projectId: number
  value: string
  onChange: (value: string) => void
  disabled: boolean
  creating: boolean
}) {
  const repos = useQuery({ queryKey: ['automation-github-repos', projectId], queryFn: () => connectedGithubRepos(projectId) })
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
  return (
    <FormTextField
      label="Repository"
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

function ToggledNumber({
  name,
  label,
  on,
  value,
  disabled,
  testId,
  onToggle,
  onChange,
}: {
  name: string
  label: string
  on: boolean
  value: string
  disabled: boolean
  testId: string
  onToggle: (on: boolean) => void
  onChange: (value: string) => void
}) {
  return (
    <Box sx={{ display: 'flex', alignItems: 'center', gap: 1, flexWrap: 'wrap' }}>
      <Box sx={{ flex: '1 1 14rem' }}>
        <SettingCheckbox name={name} label={label} checked={on} disabled={disabled} onChange={onToggle} testId={`${testId}-on`} />
      </Box>
      {on && (
        <Box sx={{ flex: '0 1 8rem' }}>
          <TextField
            type="number"
            size="small"
            fullWidth
            value={value}
            disabled={disabled}
            onChange={e => onChange(e.target.value)}
            sx={fieldTapTargetSx}
            slotProps={{ htmlInput: { 'data-testid': testId, min: 1, 'aria-label': label } }}
          />
        </Box>
      )}
    </Box>
  )
}

function FieldGrid({ children }: { children: ReactNode }) {
  return (
    <Box sx={{ display: 'grid', gap: 2, gridTemplateColumns: 'repeat(auto-fit, minmax(11rem, 1fr))' }}>
      {children}
    </Box>
  )
}

function SubHeading({ children }: { children: ReactNode }) {
  return (
    <Typography variant="body2" component="h3" sx={{ fontWeight: 600, pt: 0.5 }}>
      {children}
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
  const [checking, setChecking] = useState(false)
  const when = automation.when
  if (when.kind !== 'event' || !HTTP_SOURCE_KINDS.has(when.source_kind)) return null
  const auth = savedWebhookAuth(when) || null
  const bearer = auth === 'bearer'
  // An API-key webhook holds no secret: nothing to reveal, rotate or check.
  const apiKey = auth === 'api_key'
  const revealed = { ...automation, when: { ...when, secret_state: 'revealed' } }
  // HMAC-signed sources only: a bearer sender sends the secret itself, so there is no signature to check.
  const check = bearer || apiKey ? null : (
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
    const ok = await confirm({
      title: 'Rotate secret',
      identity: automation.name,
      message: 'The old secret stops working at once. Update every sender with the new one.',
      confirmLabel: 'Rotate secret',
      destructive: true,
    })
    if (ok) await run(() => rotateAutomationSecret(automation.id), 'Couldn’t rotate the secret.')
  }

  return (
    <DetailSection title="Endpoint" testId="automation-section-setup">
      {when.source_kind === 'github' && when.ingress_url ? (
        <GithubSetup when={when} url={when.ingress_url} />
      ) : when.ingress_url ? (
        <CopyableValue value={when.ingress_url} copyLabel="Copy URL" sx={{ overflowWrap: 'anywhere' }} />
      ) : (
        <Typography variant="body2">
          No public hooks address — this automation can’t receive requests yet.
        </Typography>
      )}
      {when.source_kind === 'webhook' && (
        <Typography variant="body2" sx={{ color: 'text.secondary', overflowWrap: 'anywhere' }} data-testid="automation-auth-hint">
          {apiKey ? (
            <>
              Send <Mono>Authorization: Bearer &lt;API key&gt;</Mono> — a key with Tasks: create.{' '}
              <Link component={RouterLink} to={API_KEYS_URL}>API keys</Link>
            </>
          ) : bearer ? (
            <>Send <Mono>Authorization: Bearer &lt;secret&gt;</Mono>.</>
          ) : (
            <>Sign each request: <Mono>X-Jaah-Signature-256: sha256=&lt;hex&gt;</Mono>, the HMAC-SHA256 of the raw body keyed with the secret.</>
          )}
        </Typography>
      )}
      {error && <Banner severity="error">{error}</Banner>}
      {apiKey ? null : when.secret_state === 'revealed' ? (
        <Box sx={{ display: 'flex', alignItems: 'center', gap: 1.5, flexWrap: 'wrap' }}>
          <Typography variant="body2">Secret hidden — rotate to get a new one</Typography>
          <Button variant="outlined" disabled={busy} onClick={() => void rotate()} sx={tapTargetSx} data-testid="automation-rotate-secret">
            Rotate secret
          </Button>
          {check}
        </Box>
      ) : when.secret_state === 'owed' ? (
        <Box sx={{ display: 'flex', alignItems: 'center', gap: 1.5, flexWrap: 'wrap' }}>
          <Button
            variant="outlined"
            disabled={busy}
            onClick={() => void run(() => revealAutomationSecret(automation.id), 'Couldn’t reveal the secret.')}
            sx={tapTargetSx}
            data-testid="automation-reveal-secret"
          >
            Reveal secret (one time)
          </Button>
          {check}
        </Box>
      ) : null}
      {/* Mounted only while open: closing drops every typed field. */}
      {checking && <SignatureCheckDialog onClose={() => setChecking(false)} />}
    </DetailSection>
  )
}

/* The repo-webhook form GitHub shows, field by field, in its own order: what to paste into each box and which
 * events to tick. The secret is the one revealed below — never shown here. */
function GithubSetup({ when, url }: { when: EventWhen; url: string }) {
  const events = Array.isArray(when.source_config.events) ? (when.source_config.events as string[]) : []
  const repository = typeof when.source_config.repository === 'string' ? when.source_config.repository : ''
  const where = repository ? <Mono>{repository}</Mono> : 'the repository'
  return (
    <Stack spacing={1.5} data-testid="automation-github-setup">
      <Typography variant="body2">
        In GitHub, open {where} → Settings → Webhooks → Add webhook, and fill it in:
      </Typography>
      <SetupRow label="Payload URL">
        <CopyableValue value={url} copyLabel="Copy URL" sx={{ overflowWrap: 'anywhere' }} />
      </SetupRow>
      <SetupRow label="Content type">
        <CopyableValue value="application/json" copyLabel="Copy" />
      </SetupRow>
      <SetupRow label="Secret">
        <Typography variant="body2">This automation’s secret (below).</Typography>
      </SetupRow>
      <SetupRow label="Events">
        <Typography variant="body2" sx={{ mb: 0.5 }}>
          Choose “Let me select individual events” and tick:
        </Typography>
        <Box component="ul" sx={{ m: 0, pl: 2.5 }} data-testid="automation-github-events-to-tick">
          {events.map(event => (
            <Box component="li" key={event} sx={{ py: 0.25 }}>
              <Box sx={{ display: 'flex', alignItems: 'center', gap: 1, flexWrap: 'wrap' }}>
                <Typography variant="body2" sx={{ fontWeight: 500 }}>{githubEventLabel(event)}</Typography>
                <CopyableValue value={event} copyLabel="Copy" />
              </Box>
            </Box>
          ))}
        </Box>
      </SetupRow>
    </Stack>
  )
}

function SetupRow({ label, children }: { label: string; children: ReactNode }) {
  return (
    <Box
      sx={{
        display: 'grid',
        gridTemplateColumns: { xs: '1fr', sm: '9rem 1fr' },
        columnGap: 2,
        rowGap: 0.5,
        alignItems: 'baseline',
      }}
    >
      <Typography variant="body2" sx={{ color: 'text.secondary', fontWeight: 500 }}>{label}</Typography>
      <Box sx={{ minWidth: 0 }}>{children}</Box>
    </Box>
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

  const runTest = async () => {
    const event = parsedSample()
    if (event === null) return
    setRunning('test')
    try {
      setResult(await testRenderAutomation(automation.id, event, { prompt_template: bodyTemplate, title_template: titleTemplate }))
    } catch (err) {
      setResult(null)
      setError(readableError(err, 'Couldn’t render the template.'))
    } finally {
      setRunning(null)
    }
  }

  const runNow = async () => {
    const event = parsedSample()
    if (event === null) return
    setRunning('run')
    try {
      const fired = await runAutomation(automation.id, event)
      onRan(fired.detail)
    } catch (err) {
      setError(apiDetail(err, 'Couldn’t run the automation.'))
    } finally {
      setRunning(null)
    }
  }

  return (
    <Stack spacing={1.5} sx={{ maxWidth: '40rem' }}>
      <FormTextField
        label="Sample payload"
        error={jsonError || undefined}
        value={sample}
        multiline
        minRows={4}
        placeholder={SAMPLE_PLACEHOLDER[kind] ?? SAMPLE_PLACEHOLDER.webhook}
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
                border: '1px solid', borderColor: 'divider', borderRadius: 2, bgcolor: 'background.paper', p: '0.75rem',
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
