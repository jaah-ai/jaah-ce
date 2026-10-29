import { createElement } from 'react'
import ActivityParamsForm from '../../cron/ActivityParamsForm'
import { registerAction, registerSystemFallback, type ActionEntry, type ActionFormProps } from './registry'
import { emailAction } from './emailAction'
import { taskAction } from './taskAction'

/* The entries the console ships (architecture §7.3): Task, Send email, the `noop` reference type, and the read-only
 * form every system function shares. Imported once for its side effect by the editor. */

type Params = Record<string, unknown>
const asParams = (params: Params): Params => ({ ...params })

function NoopForm() {
  return null
}

const noopAction: ActionEntry<Params> = {
  type: 'noop',
  Form: NoopForm,
  initial: () => ({}),
  fromParams: asParams,
  toParams: asParams,
  summary: (_params, label) => label ?? 'No-op',
}

/* A system function's params are the server's schema, read-only (control-plane machinery: what it does when it
 * runs is not edited here, D5). */
function SystemForm({ value, context }: ActionFormProps<Params>) {
  const descriptor = context.descriptor
  const type = context.automation?.action.type ?? descriptor?.type ?? ''
  return createElement(ActivityParamsForm, {
    activity: descriptor?.params_schema
      ? {
          type: descriptor.type,
          label: descriptor.label,
          description: descriptor.description,
          is_system: true,
          creatable: true,
          params: descriptor.params_schema,
        }
      : null,
    catalogueLoaded: true,
    activityType: type,
    params: value,
  })
}

const systemAction: ActionEntry<Params> = {
  type: 'system',
  Form: SystemForm,
  initial: () => ({}),
  fromParams: asParams,
  toParams: asParams,
  summary: (_params, label) => label ?? 'System function',
}

registerAction(taskAction)
registerAction(emailAction)
registerAction(noopAction)
registerSystemFallback(systemAction)
