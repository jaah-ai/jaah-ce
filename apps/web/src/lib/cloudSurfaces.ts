/* THE CLOUD-ONLY SURFACES, loaded where present. `routesCloud.tsx` holds every route and rail row the community edition
 * does not ship (billing, sign-up, users and invites, nodes, customers, support tickets); the mirror build leaves it
 * out of the public tree (tools/community_edition/manifest.toml `exclude`). `import.meta.glob` yields `{}` for an absent
 * file, so the public tree compiles with no stub. `App.tsx` and `Sidebar.tsx` read `CLOUD` instead of importing it;
 * Settings' cloud-only panels are the same mechanism in their own file (`pages/settingsCloud.tsx`, read by
 * `pages/Settings.tsx`), so they stay in its chunk. `IS_CE` drops the module too: a test that mocks the edition
 * (`lib/edition.ts`) renders the console the public tree builds. */
import type { ComponentType, ReactNode } from 'react'
import type { Destination } from '../components/Sidebar'
import { IS_CE } from './edition'

export type CloudSurfaces = {
  /** Routes, by where `App.tsx` mounts them: outside the shell (sign-up, invitations), inside it, and under the
   *  Directory strip. Functions, as `App.tsx`'s `shellRoutes`: fresh elements per render. */
  cloudRoutes: { public: () => ReactNode; shell: () => ReactNode; directory: () => ReactNode }
  /** The rail's cloud-only destinations; `Sidebar.tsx`'s `NAV_ORDER` places them. */
  cloudNavItems: readonly Destination[]
  /** The rail's cloud-only chrome, `Sidebar.tsx`'s: the org switcher (`components/account/OrgSwitcherList.tsx`) and
   *  a per-agent plan's trial meter (`components/TrialAgentsMeter.tsx`). */
  OrgSwitcherList: ComponentType<{ muiSkipListHighlight?: boolean }>
  TrialAgentsMeter: ComponentType<{ shape: 'rail' | 'collapsed' | 'sheet' }>
}

const found = import.meta.glob<CloudSurfaces>('../routesCloud.tsx', { eager: true })

export const CLOUD: CloudSurfaces | null = IS_CE ? null : (found['../routesCloud.tsx'] ?? null)
