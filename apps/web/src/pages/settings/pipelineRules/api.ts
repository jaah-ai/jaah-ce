// The org's pipeline rules (`GET`/`PUT /api/org/pipeline-guardrails`, rules in `apps/api/app/releases/guardrails.py`):
// three built-in checks on the step into production, each Off, Warn or Block. Any member reads them; an Org
// Owner/Admin sets them.

import { get, put } from '../../../api'

export type GuardrailRule = 'production_approval' | 'no_auto_production' | 'production_maintainers'
export type GuardrailMode = 'off' | 'warn' | 'block'

export type Guardrail = {
  rule: GuardrailRule
  title: string
  mode: GuardrailMode
  /** `production_approval` only: the approvals the step into production needs (1–5). */
  approvals_min: number | null
  /** The projects you can see whose saved pipeline breaks the rule today, the rule taken as on. */
  breaking: { project_id: number; name: string }[]
  updated_by: string | null
  updated_at: string | null
}

export const GUARDRAILS_KEY = ['pipeline-guardrails'] as const
export const getGuardrails = () => get<{ rules: Guardrail[] }>('/api/org/pipeline-guardrails')
export const putGuardrail = (rule: GuardrailRule, body: { mode: GuardrailMode; approvals_min?: number }) =>
  put<{ rules: Guardrail[] }>(`/api/org/pipeline-guardrails/${rule}`, body)
