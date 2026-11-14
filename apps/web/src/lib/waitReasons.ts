import { compactSpan, humanDuration } from './relativeTime'
import { ROLE_LABEL } from './pipeline'
import type { Named, WaitReason } from './pipelineBoard'

/* WHY A STEP WAITS, IN WORDS (B4) — one pure formatter per `WaitReason` kind (`pipelineBoard.ts`, architecture §5.3):
 * a chip label of at most two words and the full lines the list shows. Times read in the viewer's zone, except
 * `hours`, which names its own. Never a kind code, an ISO stamp or an id on screen. */

/** Kinds that never hold a step (`app/pipeline/reasons.py::ADVISORY`): shown beside the blocking ones. */
const ADVISORY: ReadonlySet<WaitReason['kind']> = new Set(['instructions', 'timeout', 'freeze_soon'])

export const isBlocking = (reason: WaitReason): boolean => !ADVISORY.has(reason.kind)

/** A reason the screen shows: every kind but `press` — a manual step's own button already says someone must press it. */
export type Shown = Exclude<WaitReason, { kind: 'press' }>
const shown = (reasons: readonly WaitReason[] | undefined): Shown[] =>
  (reasons ?? []).filter((r): r is Shown => r.kind !== 'press')

/** The step's chip: the first blocking reason shown, in the server's display order. */
export const firstBlocking = (reasons: readonly WaitReason[] | undefined): Shown | null =>
  shown(reasons).find(isBlocking) ?? null

/** A role's words; one this list does not know reads title-cased ("Org Member"), never as its code. */
const roleLabel = (slug: string) => ROLE_LABEL.get(slug) ?? (slug === 'project_viewer' ? 'Viewer'
  : slug.split('_').filter(Boolean).map(w => w[0].toUpperCase() + w.slice(1)).join(' '))

/** Roles then names, as one list: "Org Admin, Ann Lee". */
function whoText(roles: readonly string[], users: readonly Named[]): string {
  return [...roles.map(roleLabel), ...users.map(u => u.name).filter(Boolean)].join(', ')
}

/** Time left until `iso`: "3h 20m", "42m"; under a minute reads "1m". */
function left(iso: string, now: Date): string {
  const seconds = Math.max(60, Math.round((Date.parse(iso) - now.getTime()) / 1000))
  return humanDuration(seconds, true)
}

/** Kinds whose words count down to a moment: while one shows, the view is re-read and the words re-drawn. */
const TIMED: ReadonlySet<WaitReason['kind']> = new Set(['soak', 'timeout', 'hours', 'freeze', 'freeze_soon'])
export const hasDeadline = (reasons: readonly WaitReason[] | undefined): boolean => !!reasons?.some(r => TIMED.has(r.kind))

const WEEK_MS = 7 * 24 * 60 * 60 * 1000

/** A moment as a weekday and clock ("Fri 18:00") within a week of `now`, else with its date ("12 Dec 18:00"); in
 *  `timeZone` when given, else the viewer's. */
export function moment(iso: string, now: Date, timeZone?: string): string {
  const at = new Date(iso)
  const near = Math.abs(at.getTime() - now.getTime()) < WEEK_MS
  const parts = new Intl.DateTimeFormat('en-GB', {
    ...(near ? { weekday: 'short' } : { day: 'numeric', month: 'short' }),
    hour: '2-digit', minute: '2-digit', hourCycle: 'h23', ...(timeZone ? { timeZone } : {}),
  }).formatToParts(at)
  const part = (type: Intl.DateTimeFormatPartTypes) => parts.find(p => p.type === type)?.value ?? ''
  const day = near ? part('weekday') : `${part('day')} ${part('month')}`
  return `${day} ${part('hour')}:${part('minute')}`
}

const releaseName = (version: string | null, id: number) => version ? `release ${version}` : `release #${id}`

/** The chip: one or two words naming the reason ("2/3 approvals", "Frozen", "Soak 42m", "Off hours", "Paused"). */
export function waitChip(reason: Shown, now: Date = new Date()): string {
  switch (reason.kind) {
    case 'freeze_now':
    case 'freeze': return 'Frozen'
    case 'paused': return 'Paused'
    case 'verify': return reason.chip
    case 'approvals': return `${reason.have}/${reason.need} approvals`
    case 'order': return 'In line'
    case 'soak': return `Soak ${compactSpan(Math.max(60_000, Date.parse(reason.until) - now.getTime()))}`
    case 'hours': return 'Off hours'
    case 'sign_off': return 'Sign-off'
    case 'instructions': return 'Instructions'
    case 'timeout': return reason.action === 'cancel' ? 'Cancels soon' : 'Reminder'
    case 'freeze_soon': return 'Freeze soon'
  }
}

/** The full lines of one reason, each short and on its own line. */
export function waitLines(reason: Shown, now: Date = new Date()): string[] {
  switch (reason.kind) {
    case 'freeze_now':
      return [reason.by_name ? `Frozen now by ${reason.by_name}` : 'Frozen now']
    case 'freeze':
      return [`Frozen until ${moment(reason.until, now)}${reason.name ? ` (${reason.name})` : ''}`]
    case 'paused':
      return [`By itself paused after hotfix ${releaseName(reason.version, reason.release_id)}`]
    case 'verify':
      return [reason.text]
    case 'approvals': {
      const lines = [`${reason.have}/${reason.need} approvals`]
      if (reason.nobody) lines.push('Nobody can approve: add an approver in Pipeline settings')
      else {
        const who = whoText(reason.roles, reason.users)
        if (who) lines.push(`Approvers: ${who}`)
      }
      for (const a of reason.approved) {
        lines.push(`Approved by ${a.name}${a.comment ? `: “${a.comment}”` : ''}`)
      }
      if (reason.viewer === 'counts') lines.push('Your approval counts')
      else if (reason.viewer === 'requester') lines.push('You asked for this')
      else if (reason.viewer === 'approved') lines.push('You approved')
      return lines
    }
    case 'order':
      return [`Runs after ${releaseName(reason.version, reason.after_release_id)} finishes`]
    case 'soak':
      return [`Ready in ${left(reason.until, now)}${reason.source ? ` (wait after ${reason.source})` : ''}`]
    case 'hours':
      return [`Next allowed: ${moment(reason.next, now, reason.timezone)} (${reason.timezone})`]
    case 'sign_off':
      return [`${reason.count} ${reason.count === 1 ? 'task needs' : 'tasks need'} sign-off`]
    case 'instructions':
      return [`For approvers: ${reason.text}`]
    case 'timeout':
      return [`${reason.action === 'cancel' ? 'Cancels' : 'Reminder'} in ${left(reason.at, now)}`]
    case 'freeze_soon':
      return [`Freeze starts ${moment(reason.starts, now)}${reason.name ? ` (${reason.name})` : ''}`]
  }
}

/** Every line of a step's reasons, in their order. */
export const allWaitLines = (reasons: readonly WaitReason[], now: Date = new Date()): string[] =>
  shown(reasons).flatMap(r => waitLines(r, now))

/** "Frozen now" while the project's Freeze now is on: who switched it on and when (R7.6); null when off. */
export function frozenNowText(reasons: readonly WaitReason[], now: Date = new Date()): string | null {
  const on = reasons.find((r): r is Extract<WaitReason, { kind: 'freeze_now' }> => r.kind === 'freeze_now')
  if (!on) return null
  const by = on.by_name ? ` by ${on.by_name}` : ''
  const at = on.at ? ` since ${moment(on.at, now)}` : ''
  return `Frozen now${by}${at}`
}
