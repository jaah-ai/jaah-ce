// Email-templates API client (mirrors `settings/modelPrices/api.ts`). The record shapes live in
// `src/types.ts`; this module owns the REQUEST shapes and the refusal routing.
//
// PUT WITH A PARTIAL BODY: the route reads only the keys sent, and `type` is immutable — so the edit
// sends name/description/fields, never `type`. Every route is operator-only (`require_operator`).

import { del, get, post, put } from '../../../api'
import type { EmailTemplate, EmailTemplateType } from '../../../types'

export type EmailTemplateCreateBody = {
  type: string
  name: string
  description: string
  fields: Record<string, string>
}

export type EmailTemplateUpdateBody = Partial<Omit<EmailTemplateCreateBody, 'type'>>

export type EmailPreview = { subject: string; text: string; html: string }

/** The closed set of types, each with its field set, placeholders and active row (code-owned). */
export const listEmailTemplateTypes = () => get<EmailTemplateType[]>('/api/email-templates/types')

/** Ordered by type, then name, server-side — no client sort. */
export const listEmailTemplates = () => get<EmailTemplate[]>('/api/email-templates')

export const createEmailTemplate = (body: EmailTemplateCreateBody) =>
  post<EmailTemplate>('/api/email-templates', body)

export const updateEmailTemplate = (id: string, body: EmailTemplateUpdateBody) =>
  put<EmailTemplate>(`/api/email-templates/${id}`, body)

/** Deleting the active row drops its type back to the built-in defaults. */
export const deleteEmailTemplate = (id: string) => del(`/api/email-templates/${id}`)

/** Deactivates the type's other rows, then activates this one (one active per type). */
export const activateEmailTemplate = (id: string) =>
  post<EmailTemplate>(`/api/email-templates/${id}/activate`)

/** Deactivates every row of the type: it sends its built-in defaults again. */
export const selectBuiltInEmailTemplate = (type: string) =>
  post<void>(`/api/email-templates/types/${type}/use-built-in`)

/** The unsaved draft rendered with sample values; a field the type refuses is a 422. */
export const previewEmailTemplate = (type: string, fields: Record<string, string>) =>
  post<EmailPreview>('/api/email-templates/preview', { type, fields })

/** SES's mailbox simulator addresses the test send may target instead of the caller (`routes/email_templates.py`). */
export const SIMULATOR_OPTIONS = [
  { value: 'success', label: 'Simulator: delivered' },
  { value: 'bounce', label: 'Simulator: bounce' },
  { value: 'complaint', label: 'Simulator: complaint' },
  { value: 'suppressionlist', label: 'Simulator: suppressed' },
] as const

export type Simulator = (typeof SIMULATOR_OPTIONS)[number]['value']

/** Sends the rendered sample to the CALLER's own address, or to one SES simulator address. 409 when email is not
 *  configured, 502 when SES refused it. */
export const sendTestEmailTemplate = (type: string, fields: Record<string, string>, simulator?: Simulator) =>
  post<{ sent_to: string }>('/api/email-templates/test', simulator ? { type, fields, simulator } : { type, fields })

/* Which form field a refusal belongs to — same contract as `settings/tags/api.ts::fieldForDetail`
 * (`null` → the caller renders it whole). A FIELD refusal leads with the field's LABEL
 * (`app/email_templates.py::validate_fields`: "Subject cannot be blank", "Message uses unknown
 * placeholder(s) …"), so the type's labels are matched as a prefix, longest first so "Preview text" is
 * never read as a shorter label that prefixes it. Name/description refusals lead with the column; the
 * 409 names the collision. */
export function fieldForDetail(
  detail: string,
  fields: readonly { key: string; label: string }[],
): string | null {
  const byLength = [...fields].sort((a, b) => b.label.length - a.label.length)
  const hit = byLength.find(f => detail.startsWith(`${f.label} `))
  if (hit) return hit.key
  if (/already exists/i.test(detail)) return 'name'
  if (/^name\b/i.test(detail)) return 'name'
  if (/^description\b/i.test(detail)) return 'description'
  return null
}
