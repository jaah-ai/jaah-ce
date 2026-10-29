/* THE MERGE LANE'S PROJECT SURFACES, loaded where present: the project page's Merge lane tab and the lane state that
 * places it. The community edition has no merge lane (`merge_lane`, packages/editions/features.toml): its tree leaves
 * the lane's modules out, and `import.meta.glob` yields `{}` for an absent file, so the tree compiles with no stub.
 * Without the feature there is no tab and no lane read. */
import type { ComponentType } from 'react'
import { isEnabled } from './editionFeatures'

type LaneTab = { hasRows: boolean; live: boolean }
type LaneModule = {
  useMergeLaneTab: (projectId: number | null) => LaneTab
  promoteLink: (project: { forge?: string | null; web_url?: string | null; integration_branch?: string | null }) => string | null
}
type TabView = ComponentType<{ projectId: number; projectName?: string; canStop: boolean; promoteUrl?: string }>

const on = isEnabled('merge_lane')

const lane: LaneModule | undefined = Object.values(on
  ? import.meta.glob<LaneModule>('../components/projects/mergeLane.ts', { eager: true })
  : {})[0]
const tab: TabView | undefined = Object.values(on
  ? import.meta.glob<TabView>('../components/projects/ProjectMergeLaneTab.tsx', { eager: true, import: 'default' })
  : {})[0]

const NO_LANE: LaneTab = { hasRows: false, live: false }

/** The project page's lane state; never read without the feature. */
export const useMergeLaneTab: LaneModule['useMergeLaneTab'] = lane?.useMergeLaneTab ?? ((): LaneTab => NO_LANE)

/** Where a lane project's promotion PR is opened, or null. */
export const promoteLink: LaneModule['promoteLink'] = lane?.promoteLink ?? ((): null => null)

/** The project page's Merge lane tab, or null without the feature. */
export const ProjectMergeLaneTab: TabView | null = tab ?? null
