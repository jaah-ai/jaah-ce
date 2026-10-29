import { type ReactNode } from 'react'
import { useQuery } from '@tanstack/react-query'
import Box from '@mui/material/Box'
import type { WizardReviewRow } from '../../components/wizard/WizardReview'
import { useApiRead } from '../../hooks/useApiRead'
import { countOf } from '../../lib/humanize'
import { monoSx } from '../../ui/listStyles'
import { useOrgsByIds } from '../../ui/OrgSelect'
import { nodesAdapter, projectsAdapter, summarizeTarget, type TargetRule } from '../../ui/TargetSelector'
import { previewSchedule } from '../cron/api'
import { getOrgEmail, ORG_EMAIL_KEY } from '../settings/email/api'
import { intervalUnits, SCHEDULE_KIND_CRON, scheduleFields, type ScheduleDraft } from '../cron/schedule'
import { retentionLabel } from '../cron/vocabulary'
import type { ActionContext, ActionEntry, ActionReviewRow } from './actions/registry'
import type { ActionType } from './api'
import type { Draft } from './AutomationSections'
import {
  ANY_REPOSITORY,
  inboxOptions,
  PREFLIGHT_LABELS,
  USER_ACCOUNT_TYPE_LABELS,
  USER_MOMENT_LABELS,
  USER_PROVIDER_LABELS,
  USER_SCOPE_LABELS,
  WEBHOOK_AUTH_LABELS,
  type EventDraft,
} from './EventSourceFields'
import { WHEN_CHOICE_LABELS } from './vocabulary'

/* THE NEW-AUTOMATION WIZARD'S REVIEW (`AutomationCreateWizard.tsx`): every value the draft will create with, read
 * back in words — the schedule's sentence, target NAMES, the action's own fields — never an id or a JSON blob.
 * Rows carry the step that set them, so `WizardReview` draws one Edit link per step. A name that must be read
 * from the server is a small component that reads it, showing the stored value until it lands. */

export type AutomationStepId = 'about' | 'when' | 'target' | 'action' | 'review'

/** Every chosen value as Review rows, grouped About → When → Target (when shown) → Action. */
export function automationReviewRows({
  draft, showOrg, showsTarget, descriptor, entry, context,
}: {
  draft: Draft
  showOrg: boolean
  showsTarget: boolean
  descriptor: ActionType | undefined
  entry: ActionEntry<unknown> | null
  context: ActionContext
}): WizardReviewRow[] {
  const rows: WizardReviewRow[] = []
  const about = (label: string, value: ReactNode) => rows.push({ stepId: 'about', label, value })
  const when = (label: string, value: ReactNode) => rows.push({ stepId: 'when', label, value })
  about('Name', draft.name.trim())
  about('Description', draft.description.trim() === '' ? 'None' : <Wrapped>{draft.description.trim()}</Wrapped>)
  if (showOrg) about('Organization', <OrgName id={draft.orgId} />)

  when('When', WHEN_CHOICE_LABELS[draft.when])
  if (draft.when === 'schedule') {
    when('Schedule', <ScheduleSentence draft={draft.schedule} />)
    when('Keep logs', retentionLabel(draft.retentionMode))
  } else {
    for (const [label, value] of eventRows({ ...draft.event, sourceKind: draft.when }, draft.orgId)) when(label, value)
  }

  if (showsTarget) {
    rows.push({ stepId: 'target', label: draft.targetKind === 'nodes' ? 'Run on nodes' : 'Run in projects', value: <TargetSentence rule={draft.target} orgId={draft.orgId} /> })
  }

  const actionRows: ActionReviewRow[] = [{ label: 'Action', value: descriptor?.label ?? draft.actionType }]
  if (entry && draft.actionValue !== null) {
    if (entry.reviewRows) actionRows.push(...entry.reviewRows(draft.actionValue, context))
    else actionRows.push(...paramRows(entry.toParams(draft.actionValue, context)))
  }
  for (const row of actionRows) rows.push({ stepId: 'action', ...row })
  return rows
}

function Wrapped({ children }: { children: ReactNode }) {
  return <Box component="span" sx={{ whiteSpace: 'pre-wrap', overflowWrap: 'anywhere' }}>{children}</Box>
}

const listOr = (raw: string, empty: string): string => {
  const items = raw.split(',').map(v => v.trim()).filter(Boolean)
  return items.length > 0 ? items.join(', ') : empty
}

/** An event source's settings, in the labels its fields carry (`EventSourceFields.tsx`). */
function eventRows(event: EventDraft, orgId: string): [string, ReactNode][] {
  const rows: [string, ReactNode][] = []
  switch (event.sourceKind) {
    case 'webhook':
      rows.push(['Authentication', WEBHOOK_AUTH_LABELS[event.webhookAuth] ?? event.webhookAuth])
      break
    case 'github':
      rows.push(['Events', listOr(event.githubEvents, 'None')])
      rows.push(['Actions', listOr(event.githubActions, 'Every action')])
      rows.push(['Repository', event.githubRepository === ANY_REPOSITORY ? 'Any repository' : event.githubRepository.trim() || 'The project’s primary repository'])
      break
    case 'gmail':
      rows.push(['Receives at', <InboxName choice={event.gmailInbox} orgId={orgId} />])
      rows.push(['To contains', event.gmailTo.trim() || 'Any'])
      rows.push(['From', event.gmailFrom.trim() || 'Any'])
      rows.push(['Subject contains', event.gmailSubject.trim() || 'Any'])
      break
    case 'slack':
      rows.push(['Channel IDs', listOr(event.slackChannels, 'None')])
      break
    case 'user_registered': {
      const platform = event.userScope === 'platform'
      // Values are shown in their labels, never their codes; "When" is the section's own row, so the moment
      // keeps the form's label (a duplicate row key would collide in the list).
      const named = (raw: string, labels: Record<string, string>) =>
        listOr(raw.split(',').map(v => v.trim()).filter(Boolean).map(v => labels[v] ?? v).join(', '), 'Any')
      rows.push(['Who', USER_SCOPE_LABELS[event.userScope] ?? event.userScope])
      if (platform) rows.push(['Fires on', USER_MOMENT_LABELS[event.userMoment] ?? event.userMoment])
      rows.push(['Providers', named(event.userProviders, USER_PROVIDER_LABELS)])
      if (platform) rows.push(['Account type', named(event.userAccountTypes, USER_ACCOUNT_TYPE_LABELS)])
      rows.push(['Email domains', listOr(event.userDomains, 'Any')])
      break
    }
  }
  rows.push(['Preflight', PREFLIGHT_LABELS[event.preflightKind] ?? event.preflightKind])
  if (event.preflightKind === 'script') rows.push(['Script path', event.preflightScriptPath.trim()])
  if (event.preflightKind === 'model') {
    rows.push(['Preflight prompt', <Wrapped>{event.preflightPrompt.trim()}</Wrapped>])
    rows.push(['Anthropic API key', event.preflightCredentialId ? 'Selected' : 'None'])
  }
  if (event.preflightKind !== 'none') rows.push(['Timeout', `${event.preflightTimeout.trim()} s`])
  rows.push(['Concurrency cap', event.concurrencyCap.trim()])
  rows.push(['Queue depth', event.queueDepth.trim()])
  rows.push(['Rate limit per hour', event.rateLimit.trim()])
  rows.push(['Budget', event.budget.trim() === '' ? 'No limit' : `$${event.budget.trim()}`])
  rows.push(['Pause after failures', event.autoPauseFailuresOn ? `${event.autoPauseFailures.trim()} in a row` : 'Off'])
  rows.push(['Pause when idle', event.autoPauseIdleOn ? `${event.autoPauseIdleDays.trim()} days` : 'Off'])
  return rows
}

const words = (key: string): string => key.charAt(0).toUpperCase() + key.slice(1).replace(/_/g, ' ')

/** One param value in words: a nested object as its own "key: value" pairs, never JSON. */
function paramWords(value: unknown): string {
  if (value === null || value === undefined || value === '') return 'None'
  if (typeof value === 'boolean') return value ? 'Yes' : 'No'
  if (Array.isArray(value)) return value.length === 0 ? 'None' : value.map(paramWords).join(', ')
  if (typeof value === 'object') {
    const pairs = Object.entries(value as Record<string, unknown>)
    return pairs.length === 0 ? 'None' : pairs.map(([k, v]) => `${words(k)}: ${paramWords(v)}`).join('; ')
  }
  return String(value)
}

/** A type with no review of its own: its wire params, one row each, keys in words. */
function paramRows(params: Record<string, unknown>): ActionReviewRow[] {
  return Object.entries(params).map(([key, value]) => ({ label: words(key), value: paramWords(value) }))
}

const UNIT_WORDS: Record<number, string> = { 1: 'second', 60: 'minute', 3600: 'hour' }

/** An interval as its count and unit ("every 2 hours"), the split the When step's field uses. */
function intervalWords(seconds: number | null): string {
  const { count, unit } = intervalUnits(seconds)
  if (count === null) return 'Not set'
  return `Every ${countOf(count, UNIT_WORDS[unit])}`
}

/** The schedule in words: the server's own sentence (`POST /api/crons/preview`) beside the expression. */
function ScheduleSentence({ draft }: { draft: ScheduleDraft }) {
  const key = JSON.stringify(scheduleFields(draft))
  const preview = useApiRead(['automation-schedule-preview', key], () => previewSchedule(JSON.parse(key)).then(p => p.schedule_human), 'failed')
  const human = preview.loading || preview.error ? null : preview.data
  const raw = draft.kind === SCHEDULE_KIND_CRON ? draft.expression.trim() : intervalWords(draft.intervalSeconds)
  return (
    <span data-testid="automation-review-schedule">
      {human ?? raw}
      {human !== null && draft.kind === SCHEDULE_KIND_CRON && (
        <Box component="span" sx={[monoSx, { color: 'text.secondary', ml: 1 }]}>{raw}</Box>
      )}
    </span>
  )
}

/** The target rule in the selector's own words ("2 projects: api, web"), every name in full. */
function TargetSentence({ rule, orgId }: { rule: TargetRule; orgId: string }) {
  const adapter = rule.kind === 'nodes' ? nodesAdapter : projectsAdapter
  const roster = adapter.useRoster(orgId === '' ? null : orgId)
  // Before the roster lands a ticked id has no name, and would read as "#3 (removed)".
  const words = roster.state === 'loading' ? '…' : summarizeTarget(rule, roster.items, adapter.noun).full
  return <span data-testid="automation-review-target">{words}</span>
}

/** An Email source's "Receives at" in the select's own words, read from the org's email. */
function InboxName({ choice, orgId }: { choice: string; orgId: string }) {
  const { data, isPending } = useQuery({ queryKey: [...ORG_EMAIL_KEY, orgId], queryFn: () => getOrgEmail(orgId), enabled: orgId !== '' })
  if (choice === '') return <>Not set</>
  if (isPending) return <>…</>
  const label = data ? inboxOptions(data, false, null, choice).find(option => option.value === choice)?.label : undefined
  return <span data-testid="automation-review-inbox">{label ?? 'Removed'}</span>
}

/** The draft's org by name: one `?ids=` read, the id itself when it cannot be named. */
function OrgName({ id }: { id: string }) {
  const { data, isPending } = useOrgsByIds([id])
  if (isPending) return <>…</>
  return <>{data?.find(o => o.id === id)?.name ?? id}</>
}
