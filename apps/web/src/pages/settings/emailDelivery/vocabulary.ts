import type { Tone } from '../../../ui/StatusChip'
import type { DeliveryStatus, EmailSuppression } from './api'

/** A delivery status in R5's words, and its tone (`app/email_deliveries.py` owns each value's meaning). */
export const DELIVERY_STATUS: Record<DeliveryStatus, { label: string; tone: Tone }> = {
  queued: { label: 'Queued', tone: 'neutral' },
  sent: { label: 'Sent', tone: 'info' },
  delivered: { label: 'Delivered', tone: 'success' },
  delayed: { label: 'Delayed', tone: 'warning' },
  bounced: { label: 'Bounced', tone: 'error' },
  complained: { label: 'Complained', tone: 'error' },
  suppressed: { label: 'Suppressed', tone: 'warning' },
  failed: { label: 'Failed', tone: 'error' },
  not_configured: { label: 'Not configured', tone: 'neutral' },
}

export const SUPPRESSION_REASON: Record<EmailSuppression['reason'], string> = {
  bounce: 'Bounced',
  complaint: 'Complained',
  manual: 'Added by operator',
}

/** A template key as words: `password_reset` → `Password reset`. */
export function mailTypeLabel(key: string): string {
  const words = key.replace(/_/g, ' ')
  return words.charAt(0).toUpperCase() + words.slice(1)
}

/** What a send refused by SES's SANDBOX says: the account may mail only verified addresses until production access
 *  is granted. */
export const SES_SANDBOX_REASON = 'SES is in sandbox: the recipient is not verified.'

/* SES's sandbox refuses an unverified recipient in two shapes:
   - an AccessDenied on `ses:SendRawEmail` whose resource is the RECIPIENT's address identity
     (`…:identity/someone@example.com`); verbatim it reads as a broken IAM grant;
   - once the grant covers `identity/*`, a MessageRejected: "Email address is not verified. The following identities
     failed the check in region US-EAST-1: someone@example.com, …".
   Only an ADDRESS (an `@`) counts: a refusal naming the sender's domain identity is a real grant or setup problem,
   and keeps its own words. */
const ACCESS_DENIED = /AccessDenied[\s\S]*ses:SendRawEmail[\s\S]*:identity\/([^\s'"`]+@[^\s'"`]+)/
const NOT_VERIFIED = /Email address is not verified\.[\s\S]*?failed the check in region [^:]*:([^\n;]*)/
const ADDRESS = /^[^\s@,'"`]+@[^\s@,'"`]+\.[^\s@,'"`]+$/

/** The unverified recipients a sandbox refusal names, in order; empty for any other reason. */
export function sesSandboxRecipients(detail: string | null | undefined): string[] {
  if (!detail) return []
  const denied = ACCESS_DENIED.exec(detail)
  if (denied) return [denied[1]]
  const rejected = NOT_VERIFIED.exec(detail)
  if (!rejected) return []
  return rejected[1].split(/[,\s]+/).map(token => token.replace(/[.;'"`)]+$/, '')).filter(token => ADDRESS.test(token))
}

/** AWS's production-access review status (`ReviewDetails.Status`: PENDING, GRANTED, DENIED, FAILED) as a word. */
export function sesReviewLabel(status: string): string {
  const word = status.toLowerCase()
  return word.charAt(0).toUpperCase() + word.slice(1)
}
