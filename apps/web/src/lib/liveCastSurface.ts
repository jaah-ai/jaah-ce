/* LIVE CAST'S CONSOLE SURFACES, loaded where present: the project tab, the task page's override control and the create
 * page's. The community edition's tree leaves Live Cast out (the `live_cast` row of packages/editions/features.toml),
 * and `import.meta.glob` yields `{}` for an absent file, so the tree compiles with no stub. Without the feature each
 * reader gets a stand-in that asks and draws nothing. */
import type { ComponentType, ReactNode } from 'react'
import type { OrgRef, PipelineTask, Project } from '../types'
import { isEnabled } from './editionFeatures'

type CreateLiveCast = { control: ReactNode; onFiled: (row: PipelineTask) => Promise<string> }
type UseShown = (projectId: number | null, offered: boolean, org: OrgRef | null) => boolean
type TabView = ComponentType<{ projectId: number; canEdit: boolean; org?: OrgRef | null }>
type ControlView = ComponentType<{ taskId: number; asked: boolean; org: OrgRef | null }>
type UseCreate = (projectId: number | null, projects: Project[]) => CreateLiveCast

const on = isEnabled('live_cast')

const lib = on
  ? import.meta.glob<UseShown>(
    './liveCast.ts', { eager: true, import: 'useProjectLiveCastShown' })
  : {}
const tab = on
  ? import.meta.glob<TabView>(
    '../components/projects/ProjectLiveCastTab.tsx', { eager: true, import: 'default' })
  : {}
const control = on
  ? import.meta.glob<ControlView>(
    '../components/tasks/LiveCastItemControl.tsx', { eager: true, import: 'TaskLiveCastControl' })
  : {}
const create = on
  ? import.meta.glob<UseCreate>(
    '../pages/taskDetail/useCreateLiveCast.tsx', { eager: true, import: 'useCreateLiveCast' })
  : {}

const first = <T>(found: Record<string, T>): T | undefined => Object.values(found)[0]

const NO_CREATE: CreateLiveCast = { control: null, onFiled: async () => '' }

/** Does the project page show its Live Cast tab (`lib/liveCast.ts`)? Never without the feature. */
export const useProjectLiveCastShown: UseShown = first(lib) ?? ((): boolean => false)

/** The project page's Live Cast tab, or null without the feature. */
export const ProjectLiveCastTab: TabView | null = first(tab) ?? null

/** The task page header's Live Cast control (`components/tasks/LiveCastItemControl.tsx`). */
export const TaskLiveCastControl: ControlView = first(control) ?? ((): null => null)

/** The create page's Live Cast control and the write it makes once the row is filed. */
export const useCreateLiveCast: UseCreate = first(create) ?? ((): CreateLiveCast => NO_CREATE)
