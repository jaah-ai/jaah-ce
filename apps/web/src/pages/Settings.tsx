import { Fragment, useEffect, useMemo, useState, type ComponentType, type KeyboardEvent, type ReactNode } from 'react'
import { Navigate, useParams, useSearchParams } from 'react-router-dom'
import UnknownRoute from '../components/UnknownRoute'
import Box from '@mui/material/Box'
import Typography from '@mui/material/Typography'
import Button from '@mui/material/Button'
import TextField from '@mui/material/TextField'
import InputAdornment from '@mui/material/InputAdornment'
import List from '@mui/material/List'
import ListItemButton from '@mui/material/ListItemButton'
import ListItemText from '@mui/material/ListItemText'
import ListSubheader from '@mui/material/ListSubheader'
import MenuItem from '@mui/material/MenuItem'
import useMediaQuery from '@mui/material/useMediaQuery'
import { useTheme } from '@mui/material/styles'
import { useAuth } from '../auth'
import { exitActAs, useActingAs } from '../lib/actAs'
import { Banner } from '../ui/Banner'
import { isOrgAdminSeat, ORG_ACCESS_URL, ORG_SETTINGS_TAB_PARAM, orgSectionOfTab } from '../lib/orgAdmin'
import { keepInPane } from '../lib/scrollWithinPane'
import { slugFrom } from '../lib/slug'
import PortGridSection from '../components/PortGridSection'
import EmptyState from '../ui/EmptyState'
import { Icon } from '../ui/icons'
import SectionCard, { SECTION_CARD_TEXT_INSET } from '../ui/SectionCard'
import { ABOVE_PHONE, PHONE, PHONE_QUERY, SHELL_FILL_ATTR } from '../ui/listStyles'
import { LAUNCHER_CLEARANCE_PX } from '../ui/launcherSeat'
import { NODELINK_STORE_URL } from './nodes/nodeLinkStore/api'
import { BILLING_PAGE } from '../lib/companySetup'
import { ownOrgPanelsShown, type OwnOrgPanel } from './settings/ownOrg/OwnOrgPanels'
import ApiKeysSection from './settings/apiKeys/ApiKeysSection'
import { OwnCronMinPeriodSection } from './orgs/CronMinPeriodBlock'
import ProvidersSection from './settings/providers/ProvidersSection'
import { planSurfaceShown, planSwitchOn } from '../lib/planSwitches'
import NodeVaultSection from './settings/nodeVault/NodeVaultSection'
import TagsSection from './settings/tags/TagsSection'
import UnroutedNotificationsNotice from './settings/notifications/UnroutedNotificationsNotice'
import RetentionSection from './settings/retention/RetentionSection'
import { controlSx, KnobAlerts, KnobSaveBar, KnobSection, TELEGRAM_BOT_KEY, touchTargetSx, useKnobEditor, useLitJump } from './settings/KnobSection'
import type { OperatorSetting } from '../types'
import { NO_AUTOFILL_INPUT } from '../ui/noAutofill'
import LoadingState from '../ui/LoadingState'
import { IS_CE } from '../lib/edition'
import { isEnabled, type FeatureName } from '../lib/editionFeatures'
import ThisMachineSection from './ce/ThisMachineSection'


// Settings — the live operator knobs, resolved from the DB on each use server-side
// (app/operator_settings/), so an edit takes effect without an API restart; a knob earns a row only if
// its consumer re-reads it.
// One page, two renders, split by the API: `GET /api/settings` is `@feature("system", "view")` and answers
// PER ORG (serving a non-superadmin only `tier === 'org'` knobs — `app/routes/settings.py::_settings_out`);
// `PUT` is `@feature("system", "edit")` plus an in-handler `is_operator` gate (`routes/settings.py`). So any
// `system:view` holder reads (App.tsx guards on that key alone) and an operator saves; a tenant Org Admin
// saves only its `tenant_editable` knobs (`org_editable`). `rowEditable` decides each row; a read-only row is
// a reading surface (value as words), and the save bar shows only when some row is writable.
// SHAPE: a category list on the left (an AREA is a knob's `group`), the chosen category's
// SECTIONS as cards on the right (a knob's `section`), one row per knob with its help on hover of the
// label. On a phone the list is the page and a category opens as its own page. A search box above both
// finds a knob or a panel by name, help or key and jumps to it. Everything is derived from the payload —
// adding an area is a catalogue edit in `app/operator_settings/` only.
// Knob rows, their cards, the Save bar and the save itself are `settings/KnobSection.tsx`'s (shared with
// Products, `pages/Products.tsx`, which now holds what defines WHAT IS SOLD — `products/sections.ts`).

/* ── The non-knob panels ───────────────────────────────────────────────────────────────────────────
 * Surfaces with their own API each, placed by a KNOB KEY (never by an area's display name, prose the
 * catalogue may reword): the panel lands right after the section holding its anchor. `own` gives the
 * panel a section of its own (Tags, the rate card, NodeVault); the rest sit under the anchor's
 * section. When no served knob anchors a panel — the tenant superadmin served no Cloud knob, or a failed
 * knob fetch — the panel is SYNTHESIZED into the area and section it belongs to, so a gated surface
 * never vanishes with the knobs and never lands in the wrong place. */
export type PanelKind = OwnOrgPanel | 'thisMachine' | 'apiKeys' | 'ports' | 'tags' | 'pricing' | 'headscale' | 'billingStatus' | 'bedrockStatus' | 'cronMinPeriod' | 'emailTemplates' | 'emailDeliveryLog' | 'emailSuppressions' | 'dmarcReports' | 'sesAccount' | 'providers' | 'nodeVault' | 'complianceSigning' | 'legalStatus' | 'nodelinkLocations' | 'telegramWebhook' | 'retention' | 'residency' | 'dataRegions'
export type PanelDef = {
  kind: PanelKind
  /** The name search shows and (`own`) the card title. */
  label: string
  anchorKey: string
  area: string
  section: string
  own: boolean
  /** The card takes the section label as its title: the panel draws no heading of its own. */
  titled?: boolean
  /** A card title of its own, for a panel that sits under its section's knob card and would read as a stray card
   *  untitled (Telegram's webhook under the Bot knob). */
  title?: string
  /** The panel draws its own card (title bar and actions), so Settings does not frame it a second time. */
  framed?: boolean
  /** No knob ever anchors it, so its home is known before the knobs load: it renders while they are in flight. */
  fixed?: boolean
  /** The panel's main controls and what it shows: search finds a panel by what it does, not its title alone. */
  terms: string
}
/* The operator's model rate card (`settings/modelPrices/`): Model pricing's (`model_prices`), loaded only with it — the
 * community edition's tree leaves it out, and `import.meta.glob` yields `{}` for an absent file. */
const ModelPricesSection: ComponentType | undefined = Object.values(isEnabled('model_prices')
  ? import.meta.glob<ComponentType>('./settings/modelPrices/ModelPricesSection.tsx', { eager: true, import: 'default' })
  : {})[0]

const CORE_PANELS: readonly PanelDef[] = [
  { kind: 'tags', label: 'Tags', anchorKey: 'task_default_persona', area: 'Tasks', section: 'Tags', own: true, titled: true, terms: 'Add tag, edit tag, delete tag, tag group' },
  // `port_block_size` is pinned by the wire, the writer's bounds and `test_operator_settings.py`.
  { kind: 'ports', label: 'Range starts', anchorKey: 'port_block_size', area: 'Infrastructure', section: 'Ports', own: false, framed: true, terms: 'Add range start, delete range start, env var' },
  ...(ModelPricesSection
    ? [{ kind: 'pricing', label: 'Model pricing', anchorKey: 'quota_high_pct', area: 'Accounts', section: 'Pricing', own: true, terms: 'Add a model, add rates, save rates, delete rates, model id' } as const]
    : []),
  // No knob anchor (its own route): synthesized into Providers, the one home of per-provider policy:
  // each provider's runtime card.
  { kind: 'providers', label: 'Providers', anchorKey: 'acp_runtime', area: 'Providers', section: 'Providers', own: true, fixed: true, terms: 'Runtime switch, ACP, legacy, terminal only, capabilities, gaps, shared gaps, gap count, runtime modes, not enforced natively' },
  // The org's API keys (`settings/apiKeys`): a category of its own, every Org Owner and Admin. `/settings/api-keys`.
  { kind: 'apiKeys', label: 'API keys', anchorKey: 'own_org_api_keys', area: 'API keys', section: 'API keys', own: true, fixed: true, terms: 'Create key, revoke key, rotate secret, expiry, permissions, bearer, token, IP allowlist' },
  // The viewer's own org's raise-only floor for its schedules (beside the task knobs, not the org's cards).
  { kind: 'cronMinPeriod', label: 'Cron minimum period', anchorKey: 'own_org_cron_min_period', area: 'Tasks', section: 'Schedules', own: true, fixed: true, terms: 'Cron, schedule, minimum period, edit your minimum, clear your minimum' },
  // No knob anchors it: always synthesized into a category of its own.
  { kind: 'nodeVault', label: 'NodeVault', anchorKey: 'nodevault', area: 'NodeVault', section: 'NodeVault', own: true, fixed: true, terms: 'Set up, rotate key, current key, previous keys' },
  // No knob anchors it: synthesized into Retention (the old Data category merged in), after the operator's own
  // retention knobs. The viewer's own org's windows (`settings/retention`).
  { kind: 'retention', label: 'Your data', anchorKey: 'org_retention', area: 'Retention', section: 'Your data', own: true, titled: true, fixed: true, terms: 'Session transcripts, activity events, task artifacts, days, delete older data' },
]

/** A knob's SAVED value by key (pending edits excluded): what a panel that follows a knob reads. */
type SavedKnob = (key: string) => unknown
/** How many times this page saved a knob: a panel keyed on it re-reads after a save that kept the value. */
type SaveCount = (key: string) => number
export type PanelBody = (saved: SavedKnob, saves: SaveCount) => ReactNode
/** A panel the cloud console alone draws (`settingsCloud.tsx`), its body beside it. */
export type CloudPanel = PanelDef & { body: PanelBody }

/* The cloud console's own panels (billing, organizations, system mail, Telegram, headscale, NodeLink, data regions
 * and residency, legal pages, compliance manifest signing), read the way `lib/cloudSurfaces.ts` reads `routesCloud.tsx`: absent from the local
 * edition's public tree, dropped by `IS_CE` here. Appended, so each area keeps its panels in their old order. */
const cloudFound = import.meta.glob<{ cloudSettingsPanels: readonly CloudPanel[]; cloudProducts: CloudProducts }>(
  './settingsCloud.tsx', { eager: true })
const CLOUD_PANELS: readonly CloudPanel[] = IS_CE ? [] : (cloudFound['./settingsCloud.tsx']?.cloudSettingsPanels ?? [])

/** One search result for what moved from Settings to Products: its words, raw (`searchIndex` normalizes them). */
export type ProductsHit = { label: string; sectionLabel: string; id: string; terms: readonly string[]; to: string }
/* WHAT MOVED TO PRODUCTS (`products/sections.ts`, the cloud console's, `settingsCloud.tsx`): the operator's billing
 * knobs, rendered there and still found here. Billing's (`billing`): without it nothing moved, and search finds no
 * Products entry. */
export type CloudProducts = {
  isProductsKnob: (s: OperatorSetting) => boolean
  /** Where an old Settings address of a moved section lands. */
  productsMoved: (area: string | null, section: string | null) => string | undefined
  /** The search entries for the moved knobs and, with `panels`, Products' own panels and `(default)`. */
  productsHits: (moved: readonly OperatorSetting[], panels: boolean) => ProductsHit[]
}
const PRODUCTS: CloudProducts | null = !isEnabled('billing') ? null : (cloudFound['./settingsCloud.tsx']?.cloudProducts ?? null)
const isProductsKnob = (s: OperatorSetting): boolean => PRODUCTS?.isProductsKnob(s) ?? false

/* The community edition's one machine (`local/ThisMachineSection.tsx`): its only node UI, in a category of its own. */
const CE_PANELS: readonly PanelDef[] = IS_CE
  ? [{ kind: 'thisMachine', label: 'This machine', anchorKey: 'local_machine', area: 'This machine', section: 'This machine', own: true, titled: true, fixed: true, terms: 'Node, online, offline, last seen, agents running, queued' }]
  : []

const PANELS: readonly PanelDef[] = [...CE_PANELS, ...CORE_PANELS, ...CLOUD_PANELS]

const PANEL_BODY: Partial<Record<PanelKind, PanelBody>> = {
  ...Object.fromEntries(CLOUD_PANELS.map(p => [p.kind, p.body])),
  thisMachine: () => <ThisMachineSection />,
  tags: () => <TagsSection />,
  ports: () => <PortGridSection />,
  ...(ModelPricesSection && { pricing: () => <ModelPricesSection /> }),
  apiKeys: () => <ApiKeysSection />,
  cronMinPeriod: () => <OwnCronMinPeriodSection />,
  providers: () => <ProvidersSection />,
  nodeVault: () => <NodeVaultSection />,
  retention: () => <OwnRetentionSection />,
}

// One SECTION (a card) inside one AREA (a category). `label` is server display text, `id` the element-id
// slug. A section holds knob rows and/or panels; a synthetic one (`own` panel, or a synthesized home) holds
// no knob.
type Section = { id: string; label: string; entries: OperatorSetting[]; panels: PanelKind[] }
type Area = { id: string; label: string; sections: Section[] }

/* The category list's order: related areas sit together under a heading, as Products orders its tabs.
 * By area label (a knob's `group`, or a synthesized panel's `area`). An area not listed — a group the catalogue
 * adds later — leads the list under "Other", in catalogue order, so it is seen and placed rather than buried. */
const CATEGORY_GROUPS: readonly { heading: string; areas: readonly string[] }[] = [
  { heading: 'Console', areas: ['General', 'Dashboard', 'Support'] },
  { heading: 'Work', areas: ['Tasks', 'Projects', 'Runners'] },
  { heading: 'Platform', areas: [...(IS_CE ? ['This machine'] : []), 'Infrastructure', 'Accounts', 'Providers', 'NodeVault'] },
  { heading: 'Access', areas: ['Organizations', 'API keys', 'Integrations'] },
  { heading: 'Records', areas: ['Retention', 'Compliance'] },
  { heading: 'Business', areas: ['Billing', 'Legal'] },
]
const CATEGORY_ORDER = CATEGORY_GROUPS.flatMap(g => g.areas)

/* Each cloud surface's knobs belong to its feature (PRD R18: billing, orgs, SSO, nodes beyond this one, Jarvis, the
 * Help assistant's knowledge base, project memory, Slack, Telegram, system mail, tailnet). A control plane without the
 * feature serves none of them; they are dropped here too, so a stray one never draws a dead card. By `group`,
 * `group/section`, or a knob's `key` when its section stays. */
const KNOB_FEATURE: Readonly<Record<string, FeatureName>> = {
  Dashboard: 'customers', Support: 'support_assistant', Billing: 'billing', Legal: 'legal_acceptance',
  Organizations: 'multi_org_tenancy',
  'Runners/Project memory': 'project_memory', project_memory_default: 'project_memory', runner_project_memory: 'project_memory',
  'Infrastructure/Cloud': 'cloud_nodes', 'Infrastructure/Orders': 'cloud_nodes', 'Infrastructure/Data regions': 'data_residency',
  'Infrastructure/NodeLink storage': 'nodelink', 'Infrastructure/MicroVM': 'microvm', 'Infrastructure/Hosts': 'multi_node_hosts',
  'Integrations/Slack': 'slack', 'Integrations/Telegram': 'telegram', 'Integrations/Email': 'system_mail',
  'Integrations/Tenant Gmail': 'tenant_mail', 'Integrations/Site enquiries': 'site_chat', 'Integrations/Google Analytics': 'site_chat',
  'Integrations/Support agent': 'support_assistant',
  // No event trigger runs without it (`routes/triggers.py::NO_EVENTS_LOCALLY`), so no preflight either.
  'Integrations/Trigger preflight': 'event_triggers',
  // Forge sign-in apps: without them every forge takes a pasted token (D22).
  'Integrations/GitHub': 'forge_connect', 'Integrations/GitLab': 'forge_connect', 'Integrations/Bitbucket': 'forge_connect',
  'Integrations/Microsoft': 'forge_connect',
  'Retention/Organizations': 'multi_org_tenancy',
  // It logs to CloudWatch, which no control plane without the feature has.
  cpu_profiling: 'cpu_profiling',
}
const knobShown = (s: OperatorSetting) =>
  [s.group, `${s.group}/${s.section ?? ''}`, s.key].every(k => !(k in KNOB_FEATURE) || isEnabled(KNOB_FEATURE[k]))
const OTHER_CATEGORIES = 'Other'

function categoryHeading(area: Area): string {
  return CATEGORY_GROUPS.find(g => g.areas.includes(area.label))?.heading ?? OTHER_CATEGORIES
}

/** `areas` in `CATEGORY_GROUPS` order; the unlisted keep catalogue order ahead of them (the sort is stable). */
function inCategoryOrder(areas: Area[]): Area[] {
  const rank = (area: Area) => {
    const i = CATEGORY_ORDER.indexOf(area.label)
    return i < 0 ? -1 : i
  }
  return [...areas].sort((a, b) => rank(a) - rank(b))
}

/** True where `areas[i]` opens a heading group: the list draws the heading above it. */
function opensGroup(areas: Area[], i: number): boolean {
  return i === 0 || categoryHeading(areas[i - 1]) !== categoryHeading(areas[i])
}

// The bucket for knobs with no section: a sentinel (it becomes half an element id), double-underscored so
// no real section slug collides.
const UNSECTIONED = '__all'


const slug = (text: string) => slugFrom(text)

const loose = (text: string) => text.toLowerCase().replace(/[^a-z0-9]/g, '')

/** The section a `?section=` deep link names, and its area: by the section's slug or label, a knob key in it, or
 *  a panel it holds (`nodelink_locations` → the NodeLink storage panel), in `?area=`'s area first. Null when
 *  nothing matches. */
function sectionHome(areas: Area[], areaParam: string, sectionParam: string): { area: Area; section: Section } | null {
  const want = loose(sectionParam)
  if (!want) return null
  const ordered = [...areas.filter(a => a.id === areaParam), ...areas.filter(a => a.id !== areaParam)]
  // A section's own name outranks a knob or panel that happens to spell another section's.
  const passes: ((section: Section) => string[])[] = [
    section => [section.id, section.label],
    section => [...section.entries.map(e => e.key), ...section.panels],
  ]
  for (const names of passes) {
    for (const area of ordered) {
      const section = area.sections.find(sec => names(sec).some(name => loose(name) === want))
      if (section) return { area, section }
    }
  }
  return null
}

// Element ids must be unique, so slug collisions get a counter — and it LOOPS, since a suffixed candidate
// can itself be taken (`A!`, `A 2`, `A?` → `a`, `a-2`, then `a-2` again).
function uniqueId(base: string, taken: { id: string }[]): string {
  const root = slug(base) || UNSECTIONED
  let id = root
  let n = 2
  while (taken.some(t => t.id === id)) id = `${root}-${n++}`
  return id
}

/* ── Search ────────────────────────────────────────────────────────────────────────────────────────
 * One flat index over every knob and panel this console shows, each carrying its path. A query is
 * whitespace-split into terms and every term must appear in the entry's text (label, help, key with
 * its underscores spaced, section, area) — so "retag attempts" and "attempts retag" both find the row.
 * Label hits rank first, then section/area hits, then help/key hits. A hyphenated query also matches as
 * one word (its joined spelling, "re-register" → "reregister"), and those hits rank above all others:
 * split, "re" alone matches nearly every entry. */
type SearchEntry = {
  id: string
  label: string
  areaId: string
  areaLabel: string
  sectionId: string
  sectionLabel: string
  haystack: string
  labelText: string
  /** The label with its hyphens dropped ("Re-tag on done" → "retag on done"), for a joined query. */
  labelJoined: string
  pathText: string
  /** Set for a knob or panel that moved to Products: the result navigates there. */
  to?: string
}

function norm(text: string): string {
  return text.toLowerCase().replace(/[_-]+/g, ' ')
}

/** Hyphens and underscores dropped, so "Auto-save" reads "autosave" and "re-register" "reregister". */
function joinedSpelling(text: string): string {
  return text.toLowerCase().replace(/[_-]+/g, '')
}

function searchIndex(areas: Area[], moved: OperatorSetting[], movedPanels: boolean): SearchEntry[] {
  const out: SearchEntry[] = []
  for (const area of areas) {
    for (const section of area.sections) {
      for (const s of section.entries) {
        const labelText = norm(s.label)
        const pathText = norm(`${area.label} ${section.label}`)
        // The label's joined spelling too, so "autosave" finds "Auto-save".
        const labelJoined = joinedSpelling(s.label)
        out.push({
          id: `setting-row-${s.key}`,
          label: s.label,
          areaId: area.id,
          areaLabel: area.label,
          sectionId: section.id,
          sectionLabel: section.label,
          labelText,
          labelJoined,
          pathText,
          haystack: `${labelText} ${labelJoined} ${pathText} ${norm(s.help)} ${norm(s.key)}`,
        })
      }
      for (const kind of section.panels) {
        const def = PANELS.find(p => p.kind === kind)!
        const labelText = norm(def.label)
        const labelJoined = joinedSpelling(def.label)
        const pathText = norm(`${area.label} ${section.label}`)
        out.push({
          id: `settings-panel-${kind}`,
          label: def.label,
          areaId: area.id,
          areaLabel: area.label,
          sectionId: section.id,
          sectionLabel: section.label,
          labelText,
          labelJoined,
          pathText,
          haystack: `${labelText} ${labelJoined} ${pathText} ${norm(def.terms)} ${joinedSpelling(def.terms)}`,
        })
      }
    }
  }
  /* What moved to Products stays findable here (AC1.3): the result names its new home and goes there. */
  for (const hit of PRODUCTS?.productsHits(moved, movedPanels) ?? []) {
    const labelText = norm(hit.label)
    const labelJoined = joinedSpelling(hit.label)
    const pathText = norm(`Products ${hit.sectionLabel}`)
    out.push({
      id: hit.id,
      label: hit.label,
      areaId: '',
      areaLabel: 'Products',
      sectionId: '',
      sectionLabel: hit.sectionLabel,
      labelText,
      labelJoined,
      pathText,
      haystack: `${labelText} ${labelJoined} ${pathText} ${hit.terms.map(norm).join(' ')}`,
      to: hit.to,
    })
  }
  return out
}

const SEARCH_CAP = 30
/** Search ranks, best first. `WORD_*` is a hyphenated query matched as one word (its joined spelling), in the
 *  label or anywhere. A `HELP` hit (found only by help text or key) is listed under "Other matches". */
const RANK = { WORD_LABEL: 0, WORD: 1, LABEL: 2, PATH: 3, HELP: 4 } as const

type SearchHit = { entry: SearchEntry; loose: boolean }
/* A rule and a label in the flow of the list, not a sticky subheader: it marks one break, not a section. */
const OTHER_MATCHES_SX = {
  mt: 1,
  borderTop: 1,
  borderColor: 'divider',
  lineHeight: '32px',
  fontSize: '0.75rem',
  fontWeight: 600,
  color: 'text.secondary',
  bgcolor: 'transparent',
  position: 'static',
} as const

/* A category group's heading (`CATEGORY_GROUPS`): small and muted, in the flow of the list, so it never reads as an
 * item and never covers one while the rail scrolls. */
const CATEGORY_HEADING_SX = {
  lineHeight: '28px',
  mt: 1,
  px: 2,
  fontSize: '0.75rem',
  fontWeight: 600,
  color: 'text.secondary',
  bgcolor: 'transparent',
  position: 'static',
  '&:first-of-type': { mt: 0 },
} as const

function searchMatches(index: SearchEntry[], query: string): SearchHit[] {
  const terms = norm(query).split(/\s+/).filter(Boolean)
  if (terms.length === 0) return []
  // Only a query with a hyphen or underscore has a joined spelling worth trying.
  const joined = /[_-]/.test(query) ? query.split(/\s+/).map(joinedSpelling).filter(Boolean) : []
  const ranked: { entry: SearchEntry; rank: number }[] = []
  for (const entry of index) {
    const asOneWord = joined.length > 0 && joined.every(t => entry.haystack.includes(t))
    if (!asOneWord && !terms.every(t => entry.haystack.includes(t))) continue
    const rank = asOneWord ? (joined.every(t => entry.labelJoined.includes(t)) ? RANK.WORD_LABEL : RANK.WORD)
      : terms.every(t => entry.labelText.includes(t)) ? RANK.LABEL
        : terms.every(t => entry.labelText.includes(t) || entry.pathText.includes(t)) ? RANK.PATH
          : RANK.HELP
    ranked.push({ entry, rank })
  }
  // A stable sort: ties keep catalogue order.
  return ranked.sort((a, b) => a.rank - b.rank).slice(0, SEARCH_CAP)
    .map(r => ({ entry: r.entry, loose: r.rank === RANK.HELP }))
}

/** Material's `search` path, inlined: `@mui/icons-material` is not a dependency (`ui/InfoTip`). */
function SearchGlyph() {
  return (
    <Icon viewBox="0 0 24 24" size={20} sx={{ fill: 'currentColor', color: 'text.secondary' }}>
      <path d="M15.5 14h-.79l-.28-.27A6.47 6.47 0 0 0 16 9.5 6.5 6.5 0 1 0 9.5 16c1.61 0 3.09-.59 4.23-1.57l.27.28v.79l5 4.99L20.49 19l-4.99-5zm-6 0C7.01 14 5 11.99 5 9.5S7.01 5 9.5 5 14 7.01 14 9.5 11.99 14 9.5 14z" />
    </Icon>
  )
}

/** Material's `chevron_right` path, the phone list's "opens a page" affordance. */
function ChevronGlyph() {
  return (
    <Icon viewBox="0 0 24 24" size={20} sx={{ fill: 'currentColor', color: 'text.secondary' }}>
      <path d="M10 6 8.59 7.41 13.17 12l-4.58 4.59L10 18l6-6z" />
    </Icon>
  )
}

/* THE PAGE CAP: the rail plus one settings column, so the Save bar sits over the settings rather than a
 * screen away from the row it saves. 64rem for the column (the research band is 40–48rem for a single
 * form; this column carries a fixed label + control pair with room for a wrapping default beside them),
 * `12rem` matching the rail's own width below, plus the 12px between them (`gap: 2` at the theme's 6px
 * unit). */
const PAGE_MAX_WIDTH = { sm: 'calc(64rem + 12rem + 12px)' }

/* THE PINNED CHROME. Above a phone the page is exactly the shell's scrollport tall (`100dvh`
 * less `.app-main`'s 1.75rem top and bottom padding — App.tsx's shell layout contract), so the Save
 * bar, the search box and the category rail stay put and only the chosen category's cards scroll.
 * A shell banner above the page (`PendingOrgBanner`, `PlanEndedBanner`) adds its own height to the
 * scrollport, which then scrolls by exactly that much: the banner scrolls away and the page sits whole.
 * On a phone the list is the page and scrolls as one; only the Save bar sticks to the top. */
const PINNED_PAGE_SX = { [ABOVE_PHONE]: { height: 'calc(100dvh - 3.5rem)' } }
/* The Help launcher's room under a pinned scroller's last row (`.app-main`'s own clearance, less the 1.75rem it
 * keeps under this fill page): a column's last card scrolls clear of the launcher. */
const LAUNCHER_TAIL = `calc(${LAUNCHER_CLEARANCE_PX}px - 1.75rem)`
/** What scrolls when the chrome is pinned: fills the page's leftover height and owns its own scrollbar. */
const PINNED_SCROLL_SX = { [ABOVE_PHONE]: { flex: '1 1 0', minHeight: 0, overflowY: 'auto', pb: LAUNCHER_TAIL } }

/* Below this the 12rem rail left the cards ~180px at 700px: a category picker beside the search takes its place. */
const COMPACT_QUERY = '(max-width:899.95px)'
/* The compact search's floor: the icon plus its whole "Search" placeholder. With the expanded sidebar beside it
 * (760–900px) a 6rem floor read "Searc", and "Search settings" whole left no room for the Category picker and Save. */
const COMPACT_SEARCH_FLOOR = '8rem'
/* The cards column's right padding, inside its scrollbar gutter; the search row above reserves the same. */
const AREA_COLUMN_PR = '4px'
/* On a phone the Save bar sticks over the page while anything is unsaved: a card scrolled to stops under it. */
const SAVE_BAR_CLEARANCE = '4.75rem'

/* The viewer's own org's retention windows (`panelShown.retention` guarantees the id). */
function OwnRetentionSection() {
  const { admin } = useAuth()
  return admin?.org?.id ? <RetentionSection orgId={admin.org.id} /> : null
}

/* Old addresses of what moved to Machines (`pages/Nodes.tsx`): a deep link lands where the thing now lives.
 * `area` and `section` are the slugs the Settings page gave them. */
const MOVED: readonly { area: string; section: string; to: string }[] = [
  { area: 'accounts', section: 'nodelink-storage', to: NODELINK_STORE_URL },
]

/* Renamed areas (Nodes is the node list, so its Settings area is Infrastructure; Data merged into
 * Retention): an old link keeps its section and every other param. Read per render, not on arrival — it lands on this same route. */
const RENAMED_AREAS: ReadonlyMap<string, string> = new Map([['nodes', 'infrastructure'], ['data', 'retention']])

/* Sections that moved to another area, by the slugs Settings gave them: an old link keeps its section and lands in
 * the new area (an old Accounts › `default-providers` link lands on Providers). */
const MOVED_SECTIONS: readonly { area: string; section: string; to: string }[] = [
  { area: 'accounts', section: 'default-providers', to: 'providers' },
]

/* Checked on ARRIVAL only. Infrastructure > Cloud is not here: it still holds the Headscale and instance-type knobs.
 * What moved to Products (`products/sections.ts`) moved for the OPERATOR only — a tenant sees no knob it cannot
 * edit, so it has nothing there to be sent to. */
export default function Settings() {
  const [searchParams] = useSearchParams()
  const { admin } = useAuth()
  const [moved] = useState(() => {
    const area = searchParams.get('area')
    const section = searchParams.get('section')
    return MOVED.find(m => area === m.area && section === m.section)?.to
      ?? (admin?.is_operator === true ? PRODUCTS?.productsMoved(area, section)
        // A tenant has no Billing category (no knob it may edit): an Org Owner/Admin's billing is the Billing page.
        : area === 'billing' && isOrgAdminSeat(admin) && isEnabled('billing') ? BILLING_PAGE : undefined)
  })
  if (moved) return <Navigate to={moved} replace />
  /* The retired org panel's `?orgtab=` (its tabs are Organizations' cards now, Access is Directory's): the
     tab's card, or Access in Directory. Old links, the `/orgs/:id` redirect and bookmarks still land. */
  const orgTab = searchParams.get(ORG_SETTINGS_TAB_PARAM)
  if (orgTab !== null) {
    if (orgTab === 'access') return <Navigate to={ORG_ACCESS_URL} replace />
    const next = new URLSearchParams(searchParams)
    next.delete(ORG_SETTINGS_TAB_PARAM)
    next.set('area', 'organizations')
    if (orgTab) next.set('section', orgSectionOfTab(orgTab))
    return <Navigate to={{ search: `?${next}` }} replace />
  }
  const renamed = RENAMED_AREAS.get(searchParams.get('area') ?? '')
    ?? MOVED_SECTIONS.find(m => searchParams.get('area') === m.area && searchParams.get('section') === m.section)?.to
  if (renamed) {
    const next = new URLSearchParams(searchParams)
    next.set('area', renamed)
    return <Navigate to={{ search: `?${next}` }} replace />
  }
  return <SettingsPage />
}


/* `/settings/<area>` (a typed `/settings/providers` must not 404): a known area's path lands on its `?area=`, keeping
 * every other param, and on the area's own-name section when no `?section=` is given. Known means a category this page orders (`CATEGORY_GROUPS`) or an old name it renames; anything
 * else stays "Page not found", never a silent landing on an unrelated area. */
export function SettingsAreaPath() {
  const { area = '' } = useParams()
  const [searchParams] = useSearchParams()
  if (!CATEGORY_ORDER.some(label => slug(label) === area) && !RENAMED_AREAS.has(area)) return <UnknownRoute />
  const next = new URLSearchParams(searchParams)
  next.set('area', area)
  // An area with a section of its own name (Providers › Providers) opens on that section.
  if (!next.has('section') && PANELS.some(p => slug(p.area) === area && slug(p.section) === area)) next.set('section', area)
  return <Navigate to={`/settings?${next}`} replace />
}


/* A link to an area this organization is not shown: said in place of the area, with Exit act-as for an
 * operator acting as an org. No offer to switch orgs: it would advertise which areas are the operator's.
 * Not "your role": the area is the organization's, not the person's. */
function DeniedArea({ area }: { area: string }) {
  const actingAs = useActingAs()
  if (actingAs) {
    return (
      /* Exit keeps this URL: the tab re-reads its identity as the operator, and the area opens. */
      <Banner
        severity="info"
        testId="settings-area-denied"
        action={<Button color="inherit" size="small" data-testid="settings-area-denied-exit" onClick={exitActAs} sx={{ whiteSpace: 'nowrap', flexShrink: 0 }}>Exit act-as</Button>}
      >
        {area} is operator-only. You're acting as {actingAs.name}.
      </Banner>
    )
  }
  return (
    <Banner severity="info" testId="settings-area-denied">
      {area} isn't available here.
    </Banner>
  )
}

function SettingsPage() {
  const knobs = useKnobEditor()
  const { settings, loadError, rowEditable, unsavedIn } = knobs
  const [query, setQuery] = useState('')
  /** The element a search jump lit; cleared on a timer. */
  const [lit, setLit] = useState<string | null>(null)

  const theme = useTheme()
  const phone = useMediaQuery(PHONE_QUERY)
  const compact = useMediaQuery(COMPACT_QUERY) && !phone

  /* ── The gated panels ────────────────────────────────────────────────────────────────────────
   * The port grid's range table: an org admin (`isOrgAdminSeat`; `routes/port_grid.py`'s `org_admin_writer`,
   * scoped to the caller's org). So is NodeVault (`routes/nodevault.py`'s `@org_admin`, AC-2.1). So is the cron minimum period (`routes/orgs.py::get_org_cron_min_period`, own org). The rate card, the headscale switch and the email templates: the operator alone
   * (`routes/model_prices.py` and `routes/headscale_allowlist.py` take `Depends(require_operator)`) — never a tenant
   * superadmin, who holds `system:view` too.
   * Operational data (support telemetry) is not a setting and lives on the Support page instead. Tags:
   * every reader. Cloud connections and the NodeLink store live in Machines (`pages/Nodes.tsx`).
   * Presentation only; the API is the boundary. */
  const { admin } = useAuth()
  const isOperator = admin?.is_operator === true
  const isOrgAdmin = isOrgAdminSeat(admin)
  const actingAs = useActingAs()
  const savedKnob: SavedKnob = key => settings?.find(s => s.key === key)?.value
  const telegramBotSaved = Boolean(savedKnob(TELEGRAM_BOT_KEY))
  const nodeVaultOn = planSwitchOn(admin, 'nodevault_enabled')
  const retentionShown = planSurfaceShown(admin, 'org_retention_enabled')
  const residencyShown = planSurfaceShown(admin, 'data_residency_enabled')
  const ownOrg = ownOrgPanelsShown(admin)
  // What `areas` keys on for the own-org cards: their plan switches as one value.
  const ownOrgKey = JSON.stringify(ownOrg)
  const panelShown: Record<PanelKind, boolean> = {
    thisMachine: IS_CE,
    tags: true,
    ports: isOrgAdmin,
    pricing: isOperator,
    headscale: isOperator,
    billingStatus: isOperator,
    bedrockStatus: isOperator,
    // Their subject is the viewer's own org: with none bound each would be an empty card.
    ...ownOrg,
    // An Org Owner or Admin of a tenant, or the operator acting as one (list and revoke); never the operator's own plane.
    apiKeys: isOrgAdmin && Boolean(admin?.org?.id) && (!isOperator || actingAs !== null),
    cronMinPeriod: isOrgAdmin && Boolean(admin?.org?.id),
    emailTemplates: isOperator,
    emailDeliveryLog: isOperator,
    emailSuppressions: isOperator,
    dmarcReports: isOperator,
    sesAccount: isOperator,
    // `/api/settings/acp-runtime` takes `_operator_actor`.
    providers: isOperator,
    // Off the plan, no category at all (`planSwitches.tsx`): nothing unavailable is shown to a tenant.
    nodeVault: isOrgAdmin && nodeVaultOn,
    // The API's `require_operator` refuses everyone else.
    complianceSigning: isOperator,
    legalStatus: isOperator,
    // `/api/operator/nodelink-locations` is the operator's alone.
    nodelinkLocations: isOperator,
    // Only while a bot is saved: with none there is no webhook to read or re-register.
    telegramWebhook: isOperator && telegramBotSaved,
    // Its subject is the viewer's own org, as `organization`.
    retention: isOrgAdmin && Boolean(admin?.org?.id) && retentionShown,
    residency: isOrgAdmin && Boolean(admin?.org?.id) && residencyShown,
    // `/api/operator/data-regions` is the operator's alone.
    dataRegions: isOperator,
  }

  /* ── AND WHICH KNOBS IT OFFERS ────────────────────────────────────────────────────────────────────
   * The editor's visible set (`useKnobEditor`: a non-operator renders org-tier knobs only, fail-closed) less,
   * for the operator, what moved to Products (`products/sections.ts`) — which this page still FINDS (search)
   * but no longer renders — and, for anyone else, every knob they cannot edit: a tenant is shown what
   * it can change, never the operator's values to read (Billing's hosting switches, the dispatch caps). Anchors are
   * evaluated on this list, so a panel whose anchor is filtered is synthesized into its home. An area losing every
   * knob loses its entry (`areas` derives from this). */
  const movedKnobs = useMemo(
    () => (isOperator ? knobs.visibleKnobs.filter(isProductsKnob).filter(knobShown) : []),
    [knobs.visibleKnobs, isOperator],
  )
  const visibleKnobs = useMemo(
    () => (isOperator
      ? knobs.visibleKnobs.filter(s => !isProductsKnob(s))
      // `rowEditable`'s verdict, from the flags it reads (the function itself is new every render).
      : knobs.visibleKnobs.filter(s => knobs.editable || (knobs.orgEditable && s.tenant_editable === true))
    ).filter(knobShown),
    [knobs.visibleKnobs, isOperator, knobs.editable, knobs.orgEditable],
  )

  // The knob fetch has SETTLED (a payload or a failure): what gates every synthesized panel home, or a
  // panel would mount while `settings` is null, fetch, unmount and fetch again in place.
  const knobsSettled = settings !== null || loadError !== ''

  // Sections render in server order (the catalogue is the single home); areas in `CATEGORY_GROUPS` order. Derived
  // from `visibleKnobs`, so an area/section exists only if this console has a knob for it — or a panel.
  const areas = useMemo<Area[]>(() => {
    const out: Area[] = []
    const areaFor = (label: string): Area => {
      let area = out.find(a => a.label === label)
      if (!area) {
        area = { id: uniqueId(label, out), label, sections: [] }
        out.push(area)
      }
      return area
    }
    const sectionFor = (area: Area, label: string): Section => {
      let section = area.sections.find(sec => sec.label === label)
      if (!section) {
        section = { id: label ? uniqueId(label, area.sections) : UNSECTIONED, label, entries: [], panels: [] }
        area.sections.push(section)
      }
      return section
    }
    for (const s of visibleKnobs) {
      const area = areaFor(s.group)
      // Unsectioned knobs share one bucket so such an area renders one card (a one-card title is chrome).
      sectionFor(area, s.section ?? '').entries.push(s)
    }
    for (const def of PANELS) {
      // A panel placed by a knob waits for the knobs; a fixed one renders now (a slow knob read must not leave the
      // whole page a spinner). Rebuilt from scratch on settle, so a fixed panel keeps its area and key, never remounts.
      if (!panelShown[def.kind] || !(knobsSettled || def.fixed)) continue
      // The anchor's home, when a served knob claims it; else the panel's declared home, synthesized.
      let home: { area: Area; section: Section } | null = null
      for (const area of out) {
        const section = area.sections.find(sec => sec.entries.some(e => e.key === def.anchorKey))
        if (section) home = { area, section }
      }
      // A panel with a section of its own closes its area (Tags after Chats, the rate card after Quotas); one
      // without sits in its anchor's section (the range table under the Ports card).
      const target = home === null
        ? sectionFor(areaFor(def.area), def.section)
        : def.own ? sectionFor(home.area, def.section) : home.section
      target.panels.push(def.kind)
    }
    return inCategoryOrder(out)
    // `panelShown` is derived from the booleans and the org id the memo lists.
  }, [visibleKnobs, knobsSettled, isOperator, isOrgAdmin, admin?.org?.id, telegramBotSaved, nodeVaultOn, retentionShown, residencyShown, ownOrgKey, actingAs])

  // The active category is DERIVED from `?area=<id>` with a fallback to the first area: a save can change
  // the areas, and a vanished one would leave the page pointing at nothing. In the URL, not local state,
  // so a link can open one, a reload keeps the place, and the phone's back
  // button returns from a category page to the list. `?section=<id>` scrolls that card into view.
  const [searchParams, setSearchParams] = useSearchParams()
  const areaParam = searchParams.get('area') ?? ''
  const sectionParam = searchParams.get('section') ?? ''
  // A `?section=` names its area too (`sectionHome`): a link carrying no `?area=` still opens it.
  const linked = sectionHome(areas, areaParam, sectionParam)
  const chosenArea = areas.find(a => a.id === areaParam) ?? linked?.area ?? null
  /* A link to an area this viewer is not shown (e.g. an operator acting as an org opening `?area=providers`)
     says so in place of the fallback, rather than silently landing on another area. Known areas only: an
     unknown slug is not a place this console has. */
  // On a loaded catalogue only: a failed load drops the knob areas, and would call the viewer's own area denied.
  const deniedArea = settings !== null && areaParam !== '' && !chosenArea
    ? CATEGORY_ORDER.find(label => slug(label) === areaParam) ?? null
    : null
  // On a phone the list is a page of its own: no category chosen means the list. On a desktop the first
  // category stands in, except for a denied one: its notice and the list stand there instead.
  // Not before the knobs settle: the first area then is a fixed panel's, and the page would jump off it on arrival.
  const activeArea = chosenArea ?? (phone || !knobsSettled || deniedArea ? null : areas[0] ?? null)
  const sectionId = linked && linked.area.id === activeArea?.id ? linked.section.id : sectionParam

  function openArea(areaId: string, sectionId?: string) {
    const next = new URLSearchParams(searchParams)
    next.set('area', areaId)
    if (sectionId) next.set('section', sectionId)
    else next.delete('section')
    setSearchParams(next)
  }

  function closeArea() {
    const next = new URLSearchParams(searchParams)
    next.delete('area')
    next.delete('section')
    setSearchParams(next)
  }

  // A `?section=` in the URL (a deep link) scrolls its card into view once the area is on screen.
  // Keyed on the area's ID, not the object: a save rebuilds `areas`, and the scroll must not re-fire then.
  const activeAreaId = activeArea?.id ?? null
  const unsaved = knobs.dirtyKeys.length > 0 || knobs.saving
  // A refusal keeps it too: on a phone the bar is where the refusal is said.
  const showSave = unsaved || Boolean(knobs.saveError) || Boolean(activeArea?.sections.some(sec => sec.entries.some(rowEditable)))
  // The column's tail is padded so even its last card can head the pane, and kept so as the cards below it grow
  // or shrink (a section draws once its read lands). Only where the column is the scroller (above a phone).
  useEffect(() => {
    if (!activeAreaId || !sectionId) return
    const card = document.getElementById(`settings-section-${activeAreaId}-${sectionId}`)
    if (!card) return
    const column = card.closest<HTMLElement>('[data-testid="settings-area"]')
    const scrolls = column !== null && getComputedStyle(column).overflowY === 'auto'
    const fit = () => {
      if (!column) return
      const padded = parseFloat(column.style.paddingBottom) || 0
      const top = card.getBoundingClientRect().top - column.getBoundingClientRect().top + column.scrollTop
      column.style.paddingBottom = `${Math.max(0, column.clientHeight - (column.scrollHeight - padded - top))}px`
    }
    if (scrolls) fit()
    // Only the pane moves (`scrollIntoView` also slid the overflow-hidden shell), held while the cards above load.
    const release = keepInPane(card, 'start')
    if (!scrolls || typeof ResizeObserver === 'undefined') return release
    const observer = new ResizeObserver(fit)
    for (const child of Array.from(column.children)) observer.observe(child)
    // Its border box only: the padding `fit` writes changes the content box, which would re-fire it.
    observer.observe(column, { box: 'border-box' })
    return () => {
      release()
      observer.disconnect()
      column.style.paddingBottom = ''
    }
    // `knobsSettled`: a fixed panel opens its category before the knob cards a link names exist.
  }, [activeAreaId, sectionId, knobsSettled])

  /* A category opened from the rail takes keyboard focus to its column, so arrow and page keys scroll it
   * without Tabbing past the rail first. A fresh object per click: the same category clicked again refocuses. */
  const [focusArea, setFocusArea] = useState<{ id: string } | null>(null)
  useEffect(() => {
    if (!focusArea || focusArea.id !== activeAreaId) return
    setFocusArea(null)
    document.getElementById(`settings-area-${focusArea.id}`)?.focus({ preventScroll: true })
  }, [focusArea, activeAreaId])

  // A search jump: open the home, then light the row once it is in the DOM.
  function jumpTo(entry: SearchEntry) {
    if (entry.to) {
      // A hit that moved to Products: that page lights it (`Products.tsx`, router state `lit`). The query stays until it
      // leaves: staying (the unsaved-changes dialog's Cancel) keeps the search typed.
      knobs.leaveTo(entry.to, { lit: entry.id })
      return
    }
    setQuery('')
    openArea(entry.areaId, entry.sectionId)
    setLit(entry.id)
  }

  /* Also keyed on the refusal count, so the same row refused again is scrolled to again (its `lit` id is unchanged). */
  const refusalSeq = knobs.refusal?.seq ?? 0
  useLitJump(lit, setLit, `${activeAreaId}:${refusalSeq}`)

  /* A refused save that names one knob opens its category and lights its row, where the reason is said: the alert
   * over the page sits a screen above it on a phone. Once per refusal (`seq`), never on a re-render. */
  useEffect(() => {
    const key = knobs.refusal?.key
    if (!key) return
    for (const area of areas) {
      const section = area.sections.find(sec => sec.entries.some(e => e.key === key))
      if (!section) continue
      setQuery('')
      if (area.id !== activeAreaId) openArea(area.id, section.id)
      setLit(`setting-row-${key}`)
      return
    }
    // Keyed on the refusal alone: a refusal is acted on once, whatever the areas do after.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [refusalSeq])

  const index = useMemo(() => searchIndex(areas, movedKnobs, isOperator && knobsSettled), [areas, movedKnobs, isOperator, knobsSettled])
  const results = useMemo(() => searchMatches(index, query), [index, query])
  const searching = query.trim() !== ''

  function onSearchKey(e: KeyboardEvent<HTMLInputElement>) {
    if (e.key === 'Enter' && results[0]) {
      e.preventDefault()
      jumpTo(results[0].entry)
    } else if (e.key === 'Escape') {
      setQuery('')
    }
  }

  // The reader's note for one category, or null when this viewer may edit every knob row in it. A category that
  // also holds panels (Infrastructure: the Ports knobs beside Range starts, whose vars an org admin adds) names
  // the knob cards the note covers, so a panel's own Add button never reads as contradicting it.
  function readOnlyNote(area: Area): string | null {
    const covered = area.sections.filter(sec => sec.entries.some(e => !rowEditable(e)))
    if (covered.length === 0) return null
    const hasPanels = area.sections.some(sec => sec.panels.length > 0)
    if (!hasPanels || covered.some(sec => sec.label === '')) return 'Set by an operator. Ask them for a change.'
    return `${covered.map(sec => sec.label).join(', ')}: set by an operator. Ask them for a change.`
  }

  // One section: a card of its knob rows, then each panel it holds as a card of its own. A panel with a
  // heading of its own gets no card title; the Tags panel has none, so its section's label titles it.
  function sectionCards(area: Area, section: Section) {
    const cards: ReactNode[] = []
    const anchorId = `settings-section-${area.id}-${section.id}`
    if (section.entries.length) {
      cards.push(
        <KnobSection
          key={`knobs-${section.id}`}
          knobs={knobs}
          entries={section.entries}
          title={section.label}
          id={anchorId}
          lit={lit}
        />,
      )
    }
    section.panels.forEach((kind, i) => {
      const def = PANELS.find(p => p.kind === kind)!
      const panelId = `settings-panel-${kind}`
      cards.push(
        <Box
          // Prefixed: a section slug (`ports`) and a panel kind (`ports`) share this array.
          key={`panel-${kind}`}
          id={panelId}
          data-testid="settings-section"
          data-highlight={lit === panelId ? 'true' : undefined}
          sx={lit === panelId ? { outline: `2px solid ${theme.palette.warning.main}`, borderRadius: '4px' } : undefined}
        >
          <Box id={section.entries.length === 0 && i === 0 ? anchorId : undefined}>
            {/* A section with knobs already carries its label (Billing > Stripe: the Stripe credential knob), so
                the panel beneath it does not repeat it. */}
            {def.framed ? PANEL_BODY[kind]?.(savedKnob, knobs.saveCount) : (
              <SectionCard title={def.title ?? (def.titled && section.entries.length === 0 ? section.label : undefined)}>
                {PANEL_BODY[kind]?.(savedKnob, knobs.saveCount)}
              </SectionCard>
            )}
          </Box>
        </Box>,
      )
    })
    return cards
  }

  const categoryList = (
    <List
      component="nav"
      aria-label="Settings categories"
      data-testid="settings-categories"
      dense={!phone}
      sx={{
        py: 0,
        // A desktop rail; on a phone the list IS the page.
        // Keyed on the console's phone width, not MUI's `sm`: between the two the full-width list squeezed the
        // cards to nothing.
        width: '100%',
        flexShrink: 0,
        // Pinned: the rail runs the page's height and scrolls on its own if the categories outgrow it.
        [ABOVE_PHONE]: {
          width: '12rem',
          alignSelf: 'stretch',
          overflowY: 'auto',
          borderRight: '1px solid',
          borderColor: 'divider',
          pr: 1,
        },
      }}
    >
      {areas.map((area, i) => {
        const current = activeArea?.id === area.id
        const n = unsavedIn(area.sections.flatMap(sec => sec.entries))
        return (
          <Fragment key={area.id}>
          {opensGroup(areas, i) && (
            <ListSubheader component="div" data-testid="settings-category-heading" sx={CATEGORY_HEADING_SX}>
              {categoryHeading(area)}
            </ListSubheader>
          )}
          <ListItemButton
            selected={current}
            aria-current={current ? 'page' : undefined}
            onClick={() => {
              setFocusArea({ id: area.id })
              openArea(area.id)
            }}
            data-testid="settings-category"
            sx={{
              borderRadius: '4px',
              minHeight: { xs: '48px', sm: '36px' },
              gap: 1,
              '&.Mui-selected': { fontWeight: 600 },
            }}
          >
            <ListItemText
              primary={area.label}
              secondary={n ? `${n} unsaved` : undefined}
              slotProps={{
                primary: { sx: { fontWeight: current ? 600 : 500 } },
                secondary: { sx: { color: 'primary.main', fontWeight: 600 } },
              }}
            />
            {phone && <ChevronGlyph />}
          </ListItemButton>
          </Fragment>
        )
      })}
    </List>
  )

  const categoryArea = compact && !searching ? activeArea : null
  const showCluster = !phone && unsaved
  return (
    <Box
      data-testid="settings-page"
      {...{ [SHELL_FILL_ATTR]: '' }}
      sx={{
        display: 'flex',
        flexDirection: 'column',
        gap: '0.75rem',
        maxWidth: PAGE_MAX_WIDTH,
        ...PINNED_PAGE_SX,
        ...(unsaved && {
          [PHONE]: { '& [id^="settings-section-"]': { scrollMarginTop: SAVE_BAR_CLEARANCE } },
        }),
      }}
    >
      {/* No page title: the sidebar names the page. The phone's bar is the Save cluster alone, and ABSENT, not
      disabled, for a viewer who cannot save: `PUT /api/settings` refuses every non-operator, and a dead Save is
      chrome with no reason. It also stays away from a category with no row this viewer may edit — unless
      something is still unsaved elsewhere, which must stay saveable (Products' tab strip does the same). Above a
      phone the cluster rides the search row instead, and only while something is unsaved: a band of two dead
      buttons headed every category, and appearing in the search row moves nothing under it (the pinned rail). */}
      {phone && showSave && <KnobSaveBar knobs={knobs} />}
      <KnobAlerts knobs={knobs} />
      <UnroutedNotificationsNotice />
      {/* On a phone the list is the page: the notice heads it. Above a phone it fills the area column, beside the rail. */}
      {deniedArea && phone && <DeniedArea area={deniedArea} />}

      {/* Knobs in flight: here while nothing has loaded, or on a phone (its list is a column); else beside the rail. */}
      {!knobsSettled && !activeArea && (areas.length === 0 || phone) && <LoadingState />}

      {areas.length > 0 && (
        /* Discard/Save joining the row while something is unsaved must not wrap it: the compact search and the
           Category + Discard/Save unit both start from their floors and share what is left, so the row breaks only
           where even the floors do not fit. When it must, the search takes the first line and the unit the second,
           Save pinned to its right end.
           Above a phone the row reserves the cards column's scrollbar gutter and right padding, so Save ends flush
           with the cards; `overflow: hidden` is what lets it hold a gutter, the paddings (taken back by the margins)
           keep the Category label's notch and Save's focus ring inside it, and `flexShrink: 0` stops the pinned page
           squeezing it once it no longer has a content-sized minimum. */
        <Box
          sx={{
            display: 'flex',
            flexWrap: 'wrap',
            gap: 1.5,
            alignItems: 'center',
            [ABOVE_PHONE]: { flexShrink: 0, overflow: 'hidden', scrollbarGutter: 'stable', pr: AREA_COLUMN_PR, pt: '10px', mt: '-10px', pb: '4px', mb: '-4px' },
          }}
        >
          <TextField
            size="small"
            type="search"
            value={query}
            onChange={e => setQuery(e.target.value)}
            onKeyDown={onSearchKey}
            placeholder={compact ? 'Search' : 'Search settings'}
            slotProps={{
              htmlInput: { ...NO_AUTOFILL_INPUT, 'aria-label': 'Search settings', 'data-testid': 'settings-search' },
              input: { startAdornment: <InputAdornment position="start"><SearchGlyph /></InputAdornment> },
            }}
            sx={compact ? { ...controlSx, flex: `1 1 ${COMPACT_SEARCH_FLOOR}`, minWidth: COMPACT_SEARCH_FLOOR } : phone ? { ...controlSx, width: '100%' } : { ...controlSx, flex: '1 1 16rem', maxWidth: '28rem' }}
          />
          {(categoryArea || showCluster) && (
            <Box data-testid="settings-band-actions" sx={{ display: 'flex', gap: 1.5, alignItems: 'center', flex: '1 1 0', ml: 'auto' }}>
              {/* Not while searching: the results replace the page, so a category picked then would show nothing. */}
              {categoryArea && (
                <TextField
                  select
                  size="small"
                  label="Category"
                  value={categoryArea.id}
                  onChange={e => openArea(e.target.value)}
                  // The field names the area alone: the Save button already counts what is unsaved; the menu says where.
                  slotProps={{
                    htmlInput: { 'data-testid': 'settings-category-select' },
                    select: { renderValue: id => areas.find(a => a.id === id)?.label ?? '' },
                  }}
                  sx={{ ...controlSx, flex: '0 1 12rem', minWidth: '8rem' }}
                >
                  {/* Flat children, not a Fragment per area: the Select reads its options off its direct children. */}
                  {areas.flatMap((area, i) => {
                    const n = unsavedIn(area.sections.flatMap(sec => sec.entries))
                    const item = <MenuItem key={area.id} value={area.id}>{n ? `${area.label} (${n} unsaved)` : area.label}</MenuItem>
                    return opensGroup(areas, i)
                      ? [<ListSubheader key={`heading-${area.id}`} sx={CATEGORY_HEADING_SX}>{categoryHeading(area)}</ListSubheader>, item]
                      : [item]
                  })}
                </TextField>
              )}
              {/* `nowrap`: a line breaks on min-content, and a wrappable "Save (1)" let the unit shrink past Save's right end. */}
              {showCluster && <Box sx={{ ml: 'auto', flexShrink: 0, whiteSpace: 'nowrap' }}><KnobSaveBar knobs={knobs} bar={false} /></Box>}
            </Box>
          )}
        </Box>
      )}

      {/* The results REPLACE the page while a query is typed: a flat list, each row naming its home, opened on
      click or Enter. The page underneath stays MOUNTED and `hidden` — as every category does — so a panel fetches
      once per load and in-progress typing survives a search or a category switch. */}
      {searching && (
        results.length === 0 ? (
          <EmptyState title="No matching settings" />
        ) : (
          <List aria-label="Search results" data-testid="settings-search-results" sx={{ py: 0, ...PINNED_SCROLL_SX }}>
            {results.map(({ entry, loose }, i) => (
              <Fragment key={entry.id}>
                {/* Help-text-only hits sit apart from the name hits above them; a list of only those
                    needs no heading. */}
                {loose && i > 0 && !results[i - 1].loose && (
                  <ListSubheader component="div" data-testid="settings-search-other" sx={OTHER_MATCHES_SX}>
                    Other matches
                  </ListSubheader>
                )}
                <ListItemButton
                  onClick={() => jumpTo(entry)}
                  data-testid="settings-search-result"
                  sx={{ borderRadius: '4px', minHeight: { xs: '48px', sm: '40px' } }}
                >
                  <ListItemText
                    primary={entry.label}
                    secondary={entry.sectionLabel ? `${entry.areaLabel} › ${entry.sectionLabel}` : entry.areaLabel}
                  />
                </ListItemButton>
              </Fragment>
            ))}
          </List>
        )
      )}

      <Box
        hidden={searching}
        sx={{
          display: searching ? 'none' : 'flex',
          gap: { xs: 0, sm: 2 },
          alignItems: 'flex-start',
          [ABOVE_PHONE]: { flex: '1 1 0', minHeight: 0, alignItems: 'stretch' },
        }}
      >
        {/* The rail: always on a desktop; on a phone only while no category is open. */}
        {/* Below 900px the picker beside the search stands in for it, while it has a category to name. */}
        {(phone ? !activeArea : !compact || (deniedArea !== null && !activeArea)) && categoryList}
        {deniedArea && !phone && !activeArea && (
          <Box data-testid="settings-area-denied-column" sx={{ flex: '1 1 0', minWidth: 0, [ABOVE_PHONE]: { pl: 0.5 } }}>
            <DeniedArea area={deniedArea} />
          </Box>
        )}
        {/* The knobs are still in flight: the rail already offers the areas that have loaded. */}
        {!activeArea && !knobsSettled && !phone && areas.length > 0 && <Box sx={{ flex: '1 1 0', minWidth: 0 }}><LoadingState /></Box>}

        {areas.map(area => {
          const current = activeArea?.id === area.id
          const note = readOnlyNote(area)
          return (
            <Box
              key={area.id}
              component="section"
              id={`settings-area-${area.id}`}
              aria-label={area.label}
              data-testid="settings-area"
              hidden={!current}
              // A tab stop, so the arrow and page keys scroll the column as the wheel does. On a phone the page
              // scrolls instead: focusable only from script, so an opened category still takes focus (`focusArea`).
              tabIndex={phone ? -1 : 0}
              sx={{
                flex: '1 1 0',
                minWidth: 0,
                display: current ? 'flex' : 'none',
                flexDirection: 'column',
                gap: '0.75rem',
                // The one scrolling column. A card (it clips) would otherwise shrink to fit it; the
                // right padding keeps the scrollbar off the cards' border. The gutter is reserved whether or not
                // the category overflows, so the cards' right edge (and the search row's, above) never moves.
                [ABOVE_PHONE]: {
                  overflowY: 'auto',
                  scrollbarGutter: 'stable',
                  '&:focus-visible': { outline: `2px solid ${theme.palette.primary.main}`, outlineOffset: '-2px' },
                  '&:focus:not(:focus-visible)': { outline: 'none' },
                  pr: AREA_COLUMN_PR,
                  '& > *': { flexShrink: 0 },
                  // A spacer, not padding: the deep-link fit writes this column's padding. Less the column's gap.
                  '&::after': { content: '""', flexShrink: 0, height: `calc(${LAUNCHER_TAIL} - 0.75rem)` },
                },
              }}
            >
              {/* The category's name only on a phone, where the rail is gone: on a desktop the selected rail item
              names it, and a heading would repeat it. */}
              {phone && (
                <Box sx={{ display: 'flex', alignItems: 'center', gap: 1 }}>
                  <Button type="button" variant="text" size="small" onClick={closeArea} sx={{ ...touchTargetSx, px: 1 }}>
                    ‹ Back
                  </Button>
                  <Typography component="h3" variant="subtitle1" sx={{ fontWeight: 600, fontSize: '1rem' }}>
                    {area.label}
                  </Typography>
                </Box>
              )}
              {/* A reader who cannot save is told whose values these are, once per category and only where the
              category has knob rows: a panel (range starts, cloud connections, tags) has its own writer and its own
              affordances, so the note never sits over one (ui-copy-standards: no page subtitle for the editor). It
              promises no org-marked values — an org's overrides are the operator's, set on Orgs > <org> > Settings
              (`routes/orgs.py::put_org_settings`). No "read-only", "permission" or "superadmin". */}
              {note && (
                // Inset as the cards' text is, not flush with their edge.
                <Typography variant="body2" data-testid="settings-read-only-note" sx={{ color: 'text.secondary', maxWidth: '72ch', px: SECTION_CARD_TEXT_INSET }}>
                  {note}
                </Typography>
              )}
              {area.sections.flatMap(section => sectionCards(area, section))}
            </Box>
          )
        })}
      </Box>
    </Box>
  )
}
