/* An API key's facts in words — status, expiry, last use — shared by the list and the detail page. The status is
 * the server's (`app/api_keys.py::status`); this only names it. */

import type { ConfirmOptions } from '../../../components/ConfirmDialog'
import { personOf } from '../../../lib/authorBadge'
import { countOf } from '../../../lib/humanize'
import { NO_VALUE, relativeAge } from '../../../lib/relativeTime'
import type { Tone } from '../../../ui/StatusChip'
import type { ApiKey, ApiKeyStatus, RotateOverlap } from './api'

export const STATUS_LABEL: Record<ApiKeyStatus, string> = {
  active: 'Active',
  expiring: 'Expiring soon',
  expired: 'Expired',
  revoked: 'Revoked',
  suspended: 'Owner suspended',
}

export const STATUS_TONE: Record<ApiKeyStatus, Tone> = {
  active: 'success',
  expiring: 'warning',
  expired: 'neutral',
  revoked: 'error',
  suspended: 'warning',
}

export const STATUS_GLYPH: Record<ApiKeyStatus, string> = {
  active: '●',
  expiring: '!',
  expired: '○',
  revoked: '✕',
  suspended: '‖',
}

/** The owner as the list shows them: the person alone (an acting operator's ", acting as <org>" goes, `personOf`),
 *  and the name alone when the label also carries the address (a name typed with the address in it,
 *  "Org B orgb-admin@…", cut the address mid-word), else the label. The full label is the slot's hover;
 *  `lib/users.ts::userOption` makes the same call for a picker. */
export function ownerName(label: string): string {
  const words = personOf(label).trim().split(/\s+/)
  const name = words.filter(word => !word.includes('@'))
  return name.length > 0 && name.length < words.length ? name.join(' ') : words.join(' ')
}

const MINUTE = 60_000
const HOUR = 60 * MINUTE
const DAY = 24 * HOUR

/** "Never", "Expires in 3 days", "Expired" — or a dash once revoked (its expiry no longer matters). */
export function expiryText(key: Pick<ApiKey, 'expires_at' | 'status'>, now: Date = new Date()): string {
  if (key.status === 'revoked') return NO_VALUE
  if (key.expires_at === null) return 'Never'
  const left = Date.parse(key.expires_at) - now.getTime()
  if (Number.isNaN(left)) return NO_VALUE
  if (left <= 0) return 'Expired'
  const days = Math.round(left / DAY)
  if (days >= 1) return `Expires in ${countOf(days, 'day')}`
  const hours = Math.round(left / HOUR)
  if (hours >= 1) return `Expires in ${countOf(hours, 'hour')}`
  return `Expires in ${countOf(Math.max(1, Math.round(left / MINUTE)), 'minute')}`
}

/** "4m ago · 203.0.113.7", or "Never used". */
export function lastUsedText(key: Pick<ApiKey, 'last_used_at' | 'last_used_ip'>, now: Date = new Date()): string {
  if (!key.last_used_at) return 'Never used'
  const age = relativeAge(key.last_used_at, now)
  return key.last_used_ip ? `${age} · ${key.last_used_ip}` : age
}

/** A key still in force (neither revoked nor expired). */
export const isLive = (key: Pick<ApiKey, 'status'>) => key.status !== 'revoked' && key.status !== 'expired'

/** The revoke confirm — it names what is lost and that it cannot be undone. */
export function revokeConfirm(key: Pick<ApiKey, 'name' | 'owner'>): ConfirmOptions {
  return {
    title: `Revoke ${key.name}?`,
    message: `Every call with this key is refused from now on. This can't be undone; ${key.owner.label || 'its owner'} would need a new key.`,
    confirmLabel: 'Revoke',
    destructive: true,
  }
}

export const OVERLAP_OPTIONS: readonly { value: RotateOverlap; label: string }[] = [
  { value: 'none', label: 'None — the old secret stops now' },
  { value: '1h', label: '1 hour' },
  { value: '24h', label: '24 hours' },
  { value: '7d', label: '7 days' },
]

export const DEFAULT_OVERLAP: RotateOverlap = '24h'
