/* THE HELP SURFACE, loaded where present: the Help panel's provider (`components/support/SupportChat.tsx`) and the
 * Sidebar's Help button. The community edition has no Help at all (`support_assistant`,
 * packages/editions/features.toml): its tree leaves `components/support/` out, and `import.meta.glob` yields `{}` for
 * an absent file, so the tree compiles with no stub. Without the feature the provider only passes its children
 * through and there is no button. */
import { Fragment, type ComponentType, type ReactNode } from 'react'
import { isEnabled } from './editionFeatures'

const on = isEnabled('support_assistant')

const provider = on
  ? import.meta.glob<ComponentType<{ children: ReactNode }>>(
    '../components/support/SupportChat.tsx', { eager: true, import: 'SupportChatProvider' })
  : {}
const button = on
  ? import.meta.glob<ComponentType>('../components/support/HelpButton.tsx', { eager: true, import: 'default' })
  : {}

/** Mounted once on the shell (`App.tsx`), so a chat survives navigation. */
export const SupportChatProvider: ComponentType<{ children: ReactNode }> = Object.values(provider)[0] ?? Fragment

/** The Sidebar's Help button, or null without the feature. */
export const HelpButton: ComponentType | null = Object.values(button)[0] ?? null
