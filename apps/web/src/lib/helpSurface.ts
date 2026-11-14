/* THE HELP SURFACE, loaded where present (`editionModule.ts`): the Help panel's provider
 * (`components/support/SupportChat.tsx`) and the Sidebar's Help button. The community edition has no Help at all
 * (`support_assistant`, packages/editions/features.toml): its tree leaves `components/support/` out. Without the
 * feature the provider only passes its children through and there is no button. */
import { Fragment, type ComponentType, type ReactNode } from 'react'
import { isEnabled } from './editionFeatures'
import { editionModule } from './editionModule'

const on = isEnabled('support_assistant')

const provider = editionModule(on, () => import.meta.glob<ComponentType<{ children: ReactNode }>>(
  '../components/support/SupportChat.tsx', { eager: true, import: 'SupportChatProvider' }))
const button = editionModule(on,
  () => import.meta.glob<ComponentType>('../components/support/HelpButton.tsx', { eager: true, import: 'default' }))

/** Mounted once on the shell (`App.tsx`), so a chat survives navigation. */
export const SupportChatProvider: ComponentType<{ children: ReactNode }> = provider ?? Fragment

/** The Sidebar's Help button, or null without the feature. */
export const HelpButton: ComponentType | null = button ?? null
