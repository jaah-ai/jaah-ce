/* The operator's "Change org…" client — the API's org-transfer routes, field for field. Nothing is recomputed
 * here: what a move would do (the manifest) is the server's. The dialog is `components/ChangeOrgDialog.tsx`,
 * which also carries `useChangeOrgAction`, the one hook every surface wires. */

import { useQuery } from '@tanstack/react-query'
import { get, post } from '../api'
import { isEnabled } from './editionFeatures'
import type { OrgRef } from '../types'

/** The engine's kinds (`tenancy/transfer*.py::register`). A schedule and a trigger move with their automation,
 *  so the Automations surface (which lists all three) moves through `automation`. */
export type TransferKind =
  | 'project' | 'node' | 'host' | 'credential' | 'cloud_connection' | 'account' | 'node_group'
  | 'cron' | 'automation' | 'trigger' | 'playbook'

/** What a surface hands the action: the entity as the server addresses it. `org` is its current org when
 *  the row carries one — the picker then leaves it out; without it the first plan names it. */
export type TransferSubject = {
  id: number | string
  name: string
  org?: OrgRef | null
  /** Where the entity's page is, for the result's link. */
  href?: string
}

export type TransferOrg = { id: string; name: string; slug: string }

export type Manifest = {
  kind: string
  id: number | string
  name: string
  from_org: TransferOrg
  to_org: TransferOrg
  moves: Array<{ type: string; label: string; count: number; names: string[] }>
  released: Array<{ node: string; slot: string; task_id: number | null; task_title: string | null; uncommitted: boolean | null }>
  left_behind: Array<{ type: string; name: string; still_used_by: string[] }>
  dropped: Array<{ type: string; name: string; reason: string }>
  reassigned: Array<{ type: string; name: string; from_person: string; to_person: string }>
  notes: string[]
  fingerprint: string
  /** What the move would break or leave behind — shown, never stopping it: nothing blocks the operator. */
  warnings: Array<{ code: string; message: string; link: string | null }>
}

/** A refused plan or move: `reason` is the server's code, `detail` its sentence; a 409 carries the fresh manifest. */
export type TransferRefusal = { reason: string; detail: string; manifest?: Manifest }

const BASE = '/api/operator/transfers'

export const TRANSFER_KINDS_KEY = ['operator', 'transfers', 'kinds'] as const

export const listTransferKinds = () => get<{ kinds: string[] }>(`${BASE}/kinds`)

export const planTransfer = (kind: TransferKind, id: number | string, targetOrgId: string) =>
  post<Manifest>(`${BASE}/plan`, { kind, id, target_org_id: targetOrgId })

export const executeTransfer = (
  kind: TransferKind, id: number | string, targetOrgId: string, fingerprint: string, confirmName: string,
) => post<{ transfer_id: string; manifest: Manifest }>(BASE, {
  kind, id, target_org_id: targetOrgId, fingerprint, confirm_name: confirmName,
})

/** The refusal in a thrown `ApiError`'s body, when the server sent one. */
export function refusalOf(body: unknown): TransferRefusal | null {
  if (!body || typeof body !== 'object') return null
  const { reason, detail, manifest } = body as Partial<TransferRefusal>
  if (typeof reason !== 'string') return null
  return { reason, detail: typeof detail === 'string' ? detail : reason, manifest }
}

/** The kinds a move exists for — fetched only for the operator (the route 403s everyone else), and never without Org
 *  transfer: there is no transfer route, so "Change org…" is offered nowhere. */
export function useTransferKinds(isOperator: boolean): ReadonlySet<string> {
  const asked = isOperator && isEnabled('org_transfer')
  const { data } = useQuery({
    queryKey: TRANSFER_KINDS_KEY,
    queryFn: listTransferKinds,
    enabled: asked,
    staleTime: Infinity,
    // A 403 (an operator seat that is not a superadmin) is an answer: nothing to offer.
    retry: false,
  })
  return new Set(asked ? data?.kinds ?? [] : [])
}

/** Group-by-group count changes between two plans of one move, in words ("Moves: 3 → 4"): what a re-plan
 *  after `plan_changed` tells the operator. Empty when only the fingerprint moved. */
export function manifestChanges(before: Manifest, after: Manifest): string[] {
  const groups: Array<[string, (m: Manifest) => number]> = [
    ['Moves', m => m.moves.reduce((n, g) => n + g.count, 0)],
    ['Released', m => m.released.length],
    ['Left behind', m => m.left_behind.length],
    ['Dropped', m => m.dropped.length],
    ['Reassigned', m => m.reassigned.length],
    ['Warnings', m => m.warnings.length],
  ]
  return groups
    .map(([label, count]) => [label, count(before), count(after)] as const)
    .filter(([, a, b]) => a !== b)
    .map(([label, a, b]) => `${label}: ${a} → ${b}`)
}
