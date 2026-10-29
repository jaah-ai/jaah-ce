import type { ComponentType, ReactNode } from 'react'
import type { TargetKind, TargetRule } from '../../../ui/TargetSelector'
import type { ActionType, Automation } from '../api'

/* THE WEB ACTION REGISTRY — the browser half of `app/automation_actions.py`.
 * The editor shell and the list know NO action type by name: they ask this table for the type's form and its
 * one-line summary. Adding an action type is one server `register(...)` plus one `registerAction(...)` here with
 * its form — nothing in the shell changes. A type with no entry renders "No editor for <type>".
 *
 * An entry's form edits its OWN value shape (`V`); `fromParams` / `toParams` cross to and from the wire's
 * `action.params`, so a form may hold state the wire cannot (the Task form's axes) and the shell only ever sends
 * what `toParams` returns. */

/** What the editor knows about the automation around the action form. */
export type ActionContext = {
  /** The org the automation files into; null before the operator picks one. */
  orgId: string | null
  /** The live target rule (null when the type takes none). */
  target: TargetRule | null
  whenKind: 'schedule' | 'event'
  /** An event source's template placeholders, shown with the body; undefined on a schedule. */
  templateHint?: string
  /** The server's descriptor for this type, when the catalogue offers it. */
  descriptor: ActionType | undefined
  /** The saved automation, null on create. */
  automation: Automation | null
  disabled: boolean
  /** Files picked on an unsaved draft; the shell uploads them right after the first create. */
  stagedFiles: File[]
  onStagedFilesChange: (files: File[]) => void
  /** The save's refusal for a params field, keyed by wire name. */
  errors?: Record<string, string>
}

export type ActionFormProps<V = unknown> = {
  value: V
  onChange: (next: V) => void
  context: ActionContext
}

export type ActionEntry<V = unknown> = {
  type: string
  Form: ComponentType<ActionFormProps<V>>
  /** A new automation's value for this type. */
  initial: (context: ActionContext) => V
  /** The wire params a stored automation carries, as this form's value. */
  fromParams: (params: Record<string, unknown>, context: ActionContext) => V
  /** This form's value as the wire `action.params`, for the When the automation is saved with. */
  toParams: (value: V, context: ActionContext) => Record<string, unknown>
  /** One line for a list or a heading ("Task · Coder"); `label` is the server's word for the type, if known. */
  summary: (params: Record<string, unknown>, label?: string) => string
  /** The list column's shorter word ("Coder"), where the summary's lead adds nothing a column needs. Absent: the
   *  summary. */
  brief?: (params: Record<string, unknown>, label?: string) => string
  /** The New-automation wizard's Review rows for this value, readable (names, not ids). Absent: the shell lists
   *  the wire params, one row each (`AutomationReview.tsx`). */
  reviewRows?: (value: V, context: ActionContext) => ActionReviewRow[]
}

/** One read-only line of an action's settings on the wizard's Review step. */
export type ActionReviewRow = { label: string; value: ReactNode }

const entries = new Map<string, ActionEntry<never>>()
let fallback: ActionEntry<never> | null = null

/** Register (or replace) one action type's entry. */
export function registerAction<V>(entry: ActionEntry<V>): void {
  entries.set(entry.type, entry as unknown as ActionEntry<never>)
}

/** Forget one entry (tests). */
export function unregisterAction(type: string): void {
  entries.delete(type)
}

/** The entry every system function shares (its params are read-only): used when a type has no entry of its own
 *  and the catalogue says it is an operator-only system function. */
export function registerSystemFallback<V>(entry: ActionEntry<V>): void {
  fallback = entry as unknown as ActionEntry<never>
}

/** The entry for `type`, or null when nothing can edit it. */
export function actionEntryFor(type: string, descriptor?: ActionType, isSystem = false): ActionEntry<unknown> | null {
  const own = entries.get(type)
  if (own) return own as unknown as ActionEntry<unknown>
  if (fallback && (isSystem || descriptor?.params_schema)) return fallback as unknown as ActionEntry<unknown>
  return null
}

/** One line naming an automation's action, for a type with or without an entry. */
export function actionSummary(type: string, params: Record<string, unknown>, label?: string, isSystem = false): string {
  const entry = actionEntryFor(type, undefined, isSystem)
  return entry ? entry.summary(params, label) : (label ?? type)
}

/** The list column's word for an automation's action: the entry's `brief`, else its summary. */
export function actionBrief(type: string, params: Record<string, unknown>, label?: string, isSystem = false): string {
  const entry = actionEntryFor(type, undefined, isSystem)
  if (!entry) return label ?? type
  return entry.brief ? entry.brief(params, label) : entry.summary(params, label)
}

export type { TargetKind }
