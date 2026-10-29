import type { ReactNode } from 'react'
import { Navigate, Outlet } from 'react-router-dom'
import { useQueryClient } from '@tanstack/react-query'
import { useAuth, useCan } from '../../auth'
import { useConfirm } from '../../components/ConfirmDialog'
import { DenyListBackground, useListBackground } from '../../components/ListDetail'
import { countOf } from '../../lib/humanize'
import { planSwitchOn } from '../../lib/planSwitches'
import { MANAGE_KEY, deleteTemplate, templateKey, templateUsage } from './api'

/* WHO MANAGES TEMPLATES — the console's twin of `routes/task_templates.py::_Viewer`: the operator edits the global
 * library; an org template editor holds `templates:edit` while the org's "Templates (org)" plan switch is on (AC-13).
 * With the switch off an org admin is offered no manage view and no route. Presentation only — the API decides
 * (`components/RequireFeature.tsx`). */
export function useTemplateEditing(): { global: boolean; org: boolean; any: boolean } {
  const { admin } = useAuth()
  const can = useCan()
  const global = admin?.is_operator === true
  const org = planSwitchOn(admin, 'org_templates_enabled') && can('templates:edit')
  return { global, org, any: global || org }
}

/** The manage list's and the editor's route guard (`RequireFeature`'s holding and refusal rules). */
export function RequireTemplateEditor() {
  const { loading, resolving } = useAuth()
  const { any } = useTemplateEditing()
  const background = useListBackground()
  if (loading) return null
  if (!any) {
    if (resolving) return null
    return background ? <DenyListBackground deny={background} /> : <Navigate to="/no-access" replace />
  }
  return <Outlet />
}

/** DELETE, ONE CONFIRM FOR THE LIST AND THE EDITOR: it names how many automations will stop (AC-4), read from
 *  `GET …/usage` as the dialog opens, and runs the delete while it is open so a refusal shows in place. */
export function useTemplateDelete(): [(row: { id: string; name: string }) => Promise<boolean>, ReactNode] {
  const [confirm, dialog] = useConfirm()
  const queryClient = useQueryClient()
  async function ask(row: { id: string; name: string }): Promise<boolean> {
    const usage = await templateUsage(row.id).catch(() => null)
    const automations = usage?.automations ?? null
    const message = automations === null
      ? 'Automations that use it will stop. This cannot be undone.'
      : automations === 0
        ? 'No automation uses it. This cannot be undone.'
        : `${countOf(automations, 'automation')} will stop. This cannot be undone.`
    const ok = await confirm({
      title: 'Delete template?',
      message,
      identity: row.name,
      confirmLabel: 'Delete',
      destructive: true,
      run: { action: () => deleteTemplate(row.id), failed: 'Could not delete the template.' },
    })
    if (ok) {
      queryClient.removeQueries({ queryKey: templateKey(row.id) })
      void queryClient.invalidateQueries({ queryKey: MANAGE_KEY })
    }
    return ok
  }
  return [ask, dialog]
}
