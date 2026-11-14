import type { ReactNode } from 'react'
import Box from '@mui/material/Box'
import type { SxProps, Theme } from '@mui/material/styles'

/* THE CONTROL ICONS, drawn as inline `<svg>`: `⋯ ⟳ ⤢ ⤡ ✕` live in symbol/math fonts, and a stack without one
 * paints an empty box. No icon package here (`@mui/icons-material` is not a dependency). Each icon is
 * `currentColor` (the host's states colour it), `1em` (tracks the host's type size), `aria-hidden`
 * (every host control is already named) and `vertical-align: middle` like the glyph it replaces. */
export interface IconProps {
  /** Width and height; `1em` by default, so the icon follows the host's `font-size`. */
  size?: number | string
  sx?: SxProps<Theme>
  /** `data-testid` on the `<svg>`, for a host whose test asserts the mark itself (the Attach paperclip). */
  testId?: string
  /** An accessible name: the mark is then an `img` named by its `<title>`, not hidden (a mark that IS the fact). */
  title?: string
}

/** THE ONE `<svg>` wrapper for the console: every inline mark renders through it (`viewBox` for a 24-grid path). */
export function Icon({ viewBox = '0 0 16 16', size = '1em', sx, testId, title, children }: IconProps & { viewBox?: string; children: ReactNode }) {
  return (
    <Box
      component="svg"
      viewBox={viewBox}
      role={title ? 'img' : undefined}
      aria-hidden={title ? undefined : 'true'}
      focusable="false"
      data-testid={testId}
      sx={[
        { width: size, height: size, display: 'inline-block', verticalAlign: 'middle', flexShrink: 0 },
        ...(Array.isArray(sx) ? sx : [sx]),
      ]}
    >
      {title && <title>{title}</title>}
      {children}
    </Box>
  )
}

const STROKE = {
  fill: 'none',
  stroke: 'currentColor',
  strokeWidth: 1.5,
  strokeLinecap: 'round',
  strokeLinejoin: 'round',
} as const

/** An icon component over `Icon`: the mark's children, drawn through the shared wrapper on every render. Named
 *  after its export so DevTools and error stacks read `RefreshIcon`, not `Anonymous`. */
const mark = (name: string, children: ReactNode, viewBox?: string) =>
  Object.assign((props: IconProps) => <Icon viewBox={viewBox} {...props}>{children}</Icon>, { displayName: name })

/** A filled Material mark: one 24-unit path. */
const material = (name: string, d: string) => mark(name, <path d={d} fill="currentColor" />, '0 0 24 24')

/** A stroked mark on the 24-unit grid: the large pictures on a choice card (`components/addNode/ChoiceCard.tsx`). */
const line24 = (name: string, children: ReactNode) => mark(name, <g {...STROKE} strokeWidth={1.9}>{children}</g>, '0 0 24 24')

/** A browser window with code in it — an app or website. */
export const AppWindowIcon = line24('AppWindowIcon', <><rect x="3" y="4" width="18" height="16" rx="2.5" /><path d="M3 9h18M6.5 6.5h.01M9.5 6.5h.01M10 13l-2 2 2 2M14 13l2 2-2 2" /></>)

/** A megaphone — a business, sales or marketing initiative. */
export const MegaphoneIcon = line24('MegaphoneIcon', <path d="M3 11v2a1 1 0 0 0 1 1h2l5 4V6L6 10H4a1 1 0 0 0-1 1zM15.5 8.5a5 5 0 0 1 0 7M18.5 6a8.5 8.5 0 0 1 0 12" />)

/** Two sparkles — something new. */
export const SparkleIcon = line24('SparkleIcon', <path d="M12 3l1.9 4.6 4.6 1.9-4.6 1.9L12 16l-1.9-4.6-4.6-1.9 4.6-1.9zM18.5 15.5l.8 1.9 1.9.8-1.9.8-.8 1.9-.8-1.9-1.9-.8 1.9-.8z" />)

/** A branch off a line of commits — an existing repository. */
export const RepoBranchIcon = line24('RepoBranchIcon', <><circle cx="6" cy="5.5" r="2.3" /><circle cx="6" cy="18.5" r="2.3" /><circle cx="18" cy="8.5" r="2.3" /><path d="M6 7.8v8.4M18 10.8c0 3.2-3 4.2-6 4.2H9" /></>)

/** A globe — a web app. */
export const GlobeIcon = line24('GlobeIcon', <><circle cx="12" cy="12" r="9" /><path d="M3 12h18M12 3c2.6 2.5 3.8 5.5 3.8 9s-1.2 6.5-3.8 9c-2.6-2.5-3.8-5.5-3.8-9S9.4 5.5 12 3z" /></>)

/** A square, a circle and a triangle — any other kind of software. */
export const ShapesIcon = line24('ShapesIcon', <><rect x="3.5" y="3.5" width="7" height="7" rx="1.5" /><rect x="13.5" y="13.5" width="7" height="7" rx="1.5" /><circle cx="17" cy="7" r="3.5" /><path d="M7 13.5l3.5 7h-7z" /></>)

/** A headset agent — Support (the operator's rail row). */
export const SupportIcon = material('SupportIcon', 'M21 12.22C21 6.73 16.74 3 12 3c-4.69 0-9 3.65-9 9.28-.6.34-1 .98-1 1.72v2c0 1.1.9 2 2 2h1v-6.1c0-3.87 3.13-7 7-7s7 3.13 7 7V19h-8v2h8c1.1 0 2-.9 2-2v-1.22c.59-.31 1-.92 1-1.56v-2.3c0-.7-.41-1.31-1-1.62M9 11a1 1 0 1 1 0 2 1 1 0 0 1 0-2m6 0a1 1 0 1 1 0 2 1 1 0 0 1 0-2m3 .03C17.52 8.18 15.04 6 12.05 6c-3.03 0-6.29 2.51-6.03 6.45 2.47-1.01 4.33-3.21 4.86-5.89 1.31 2.63 4 4.44 7.12 4.47')

/** `⋯` — the overflow ("more actions") mark. */
export const MoreHorizIcon = mark('MoreHorizIcon', <><circle cx="3" cy="8" r="1.4" fill="currentColor" /><circle cx="8" cy="8" r="1.4" fill="currentColor" /><circle cx="13" cy="8" r="1.4" fill="currentColor" /></>)

/** `⟳` — reload / refresh. */
export const RefreshIcon = mark('RefreshIcon', <><path d="M13 8a5 5 0 1 1-1.46-3.54" {...STROKE} /><path d="M12 1.75v3h-3" {...STROKE} /></>)

/** `⤢` — maximize: arrows out to two corners. */
export const MaximizeIcon = mark('MaximizeIcon', <path d="M9.5 2.5h4v4M13.5 2.5 9 7M6.5 13.5h-4v-4M2.5 13.5 7 9" {...STROKE} />)

/** `⤡` — restore: arrows in from two corners. */
export const RestoreIcon = mark('RestoreIcon', <path d="M13.5 6.5h-4v-4M9.5 6.5l4-4M2.5 9.5h4v4M6.5 9.5l-4 4" {...STROKE} />)

/** `✕` — close. */
export const CloseIcon = mark('CloseIcon', <path d="M3.5 3.5l9 9M12.5 3.5l-9 9" {...STROKE} />)

/** `‹` — previous (the attachment viewer's step back). */
export const ChevronLeftIcon = mark('ChevronLeftIcon', <path d="M10 3.5 5.5 8l4.5 4.5" {...STROKE} />)

/** `›` — next (the attachment viewer's step forward). */
export const ChevronRightIcon = mark('ChevronRightIcon', <path d="M6 3.5 10.5 8 6 12.5" {...STROKE} />)

/** `−` — one fewer (a count stepper). */
export const MinusIcon = mark('MinusIcon', <path d="M3.5 8h9" {...STROKE} />)

/** `+` — one more (a count stepper). */
export const PlusIcon = mark('PlusIcon', <path d="M8 3.5v9M3.5 8h9" {...STROKE} />)

/** An open eye — shown (a project's or feature's Hide/Show, `lib/taskMute.ts`). */
export const EyeIcon = mark('EyeIcon', <><path d="M1.5 8S4 3.5 8 3.5 14.5 8 14.5 8 12 12.5 8 12.5 1.5 8 1.5 8z" {...STROKE} /><circle cx="8" cy="8" r="2" {...STROKE} /></>)

/** A struck-through eye — hidden. */
export const EyeOffIcon = mark('EyeOffIcon', <><path d="M1.5 8S4 3.5 8 3.5 14.5 8 14.5 8 12 12.5 8 12.5 1.5 8 1.5 8z" {...STROKE} /><circle cx="8" cy="8" r="2" {...STROKE} /><path d="M2.5 13.5l11-11" {...STROKE} /></>)

/** A video camera — Live Cast published (`components/tasks/LiveCastItemControl.tsx`). */
export const VideoCameraIcon = mark('VideoCameraIcon', <path d="M1.75 4.5h8.5v7h-8.5zM10.25 7l4-2.25v6.5l-4-2.25" {...STROKE} />)

/** A struck-through video camera — Live Cast hidden. */
export const VideoCameraOffIcon = mark('VideoCameraOffIcon', <><path d="M1.75 4.5h8.5v7h-8.5zM10.25 7l4-2.25v6.5l-4-2.25" {...STROKE} /><path d="M1.5 14l13-12" {...STROKE} /></>)

/** A paperclip — Attach, on both the task form and the answer form: `⬆` read as "upload/send". */
export const PaperclipIcon = mark('PaperclipIcon', <path d="M13.5 7.5 8.1 12.9a3 3 0 0 1-4.24-4.24l5.66-5.66a2 2 0 0 1 2.83 2.83L6.7 11.5a1 1 0 0 1-1.42-1.42l5.3-5.3" {...STROKE} />)

/** A window with a prompt — the runner's web console (the task page's header door). */
export const ConsoleIcon = mark('ConsoleIcon', <><rect x="1.75" y="2.75" width="12.5" height="10.5" rx="1.5" {...STROKE} /><path d="M4.5 6.5 6.5 8l-2 1.5M8 10h3" {...STROKE} /></>)

/** A half-filled circle — the Theme (light / dark) control (the Live Cast viewer's rail, `components/Sidebar.tsx`). */
export const ContrastIcon = mark('ContrastIcon', <><circle cx="8" cy="8" r="6" {...STROKE} /><path d="M8 2a6 6 0 0 1 0 12z" fill="currentColor" /></>)

/** A head and shoulders — an Account control (the viewer's collapsed rail). */
export const PersonIcon = mark('PersonIcon', <><circle cx="8" cy="5.25" r="2.75" {...STROKE} /><path d="M2.75 14.25a5.25 5.25 0 0 1 10.5 0" {...STROKE} /></>)

/* THE PHONE BAR'S AREA ICONS (`components/Sidebar.tsx`), one per destination: the bar's slots are too narrow
   for a word above their label, and a two-letter monogram read as a code, not a place. */

/** A checked box beside two lines — Tasks. */
export const TasksIcon = mark('TasksIcon', <path d="M2.5 3.75l1.25 1.25 2.25-2.5M2.5 10.75l1.25 1.25 2.25-2.5M8.5 4h5M8.5 11h5" {...STROKE} />)

/** Four tiles — Dashboard. */
export const DashboardIcon = mark('DashboardIcon', <path d="M2.5 2.5h4.5v6h-4.5zM9 2.5h4.5v3.5H9zM9 8h4.5v5.5H9zM2.5 10.5h4.5v3h-4.5z" {...STROKE} />)

/** A bar chart: ranked figures (the operator's Agent index). */
export const ChartIcon = mark('ChartIcon', <path d="M2.5 13.5h11M4 13.5V8.5M7 13.5V3.5M10 13.5V6.5M13 13.5V10" {...STROKE} />)

/** A folder — Directory. */
export const FolderIcon = mark('FolderIcon', <path d="M1.75 4.25v8.5h12.5v-7h-6.5l-1.5-1.5h-4.5z" {...STROKE} />)

/** A lightning bolt — Automation. */
export const BoltIcon = mark('BoltIcon', <path d="M9 1.75 3.5 9h4l-1 5.25L12.5 7h-4z" {...STROKE} />)

/** A shield — Compliance. */
export const ShieldIcon = mark('ShieldIcon', <path d="M8 1.75 2.75 3.75v4c0 3 2.25 5.25 5.25 6.5 3-1.25 5.25-3.5 5.25-6.5v-4z" {...STROKE} />)

/** The shield, filled: a rule is on (a repository's Branch policy). */
export const ShieldFilledIcon = mark('ShieldFilledIcon', <path d="M8 1.75 2.75 3.75v4c0 3 2.25 5.25 5.25 6.5 3-1.25 5.25-3.5 5.25-6.5v-4z" fill="currentColor" stroke="currentColor" strokeWidth="1.5" strokeLinejoin="round" />)

/** A padlock, filled: a read-only repository (no pushes at all). */
export const LockFilledIcon = mark('LockFilledIcon', <>
  <path d="M5.25 7V5a2.75 2.75 0 0 1 5.5 0v2" {...STROKE} />
  <rect x="3.25" y="7" width="9.5" height="7" rx="1.25" fill="currentColor" stroke="currentColor" strokeWidth="1.5" strokeLinejoin="round" />
</>)

/** A padlock, open: a field a linked project may change (a pipeline template's lock mark). */
export const LockOpenIcon = mark('LockOpenIcon', <>
  <path d="M5.25 7V5a2.75 2.75 0 0 1 5.4-.75" {...STROKE} />
  <rect x="3.25" y="7" width="9.5" height="7" rx="1.25" {...STROKE} />
</>)

/** A page of log lines — Audit. */
export const LogIcon = mark('LogIcon', <path d="M3.25 1.75h7l2.5 2.5v10h-9.5zM5.75 6.5h4.5M5.75 9h4.5M5.75 11.5h3" {...STROKE} />)

/** Two people — Customers. */
export const PeopleIcon = mark('PeopleIcon', <><circle cx="6" cy="5.25" r="2.25" {...STROKE} /><path d="M1.75 13.5a4.25 4.25 0 0 1 8.5 0M10.5 3.25a2.25 2.25 0 0 1 0 4M12 9.5a4.25 4.25 0 0 1 2.25 4" {...STROKE} /></>)

/** A box — Products. */
export const BoxIcon = mark('BoxIcon', <path d="M8 1.75 13.75 4.5v7L8 14.25 2.25 11.5v-7zM2.25 4.5 8 7.25l5.75-2.75M8 7.25v7" {...STROKE} />)

/** A cog — Settings. */
export const CogIcon = mark('CogIcon', <><circle cx="8" cy="8" r="2" {...STROKE} /><circle cx="8" cy="8" r="4.5" {...STROKE} /><path d="M8 1.5v2M8 12.5v2M1.5 8h2M12.5 8h2M3.4 3.4l1.4 1.4M11.2 11.2l1.4 1.4M3.4 12.6l1.4-1.4M11.2 4.8l1.4-1.4" {...STROKE} /></>)

/** Something wrong in production: a warning triangle. */
export const AlertIcon = mark('AlertIcon', <path d="M8 2 14.5 13.5h-13zM8 6.5v3M8 11.5h.01" {...STROKE} />)

/** An `i` in a circle — a notice to read, nothing wrong now. */
export const InfoIcon = mark('InfoIcon', <><circle cx="8" cy="8" r="6.25" {...STROKE} /><path d="M8 7.25v4M8 4.75h.01" {...STROKE} /></>)

/** A cross in a circle — something is failing now. */
export const ErrorCircleIcon = mark('ErrorCircleIcon', <><circle cx="8" cy="8" r="6.25" {...STROKE} /><path d="M5.75 5.75l4.5 4.5M10.25 5.75l-4.5 4.5" {...STROKE} /></>)

/** A filled square — Stop. */
export const StopIcon = mark('StopIcon', <rect x="3.5" y="3.5" width="9" height="9" rx="1.5" fill="currentColor" />)

export const StarIcon = mark('StarIcon', <path d="M8 1.9l1.85 3.85 4.2.55-3.07 2.92.77 4.18L8 11.4l-3.75 2 .77-4.18L1.95 6.3l4.2-.55z" {...STROKE} />)

export const StarFilledIcon = mark('StarFilledIcon', <path d="M8 1.9l1.85 3.85 4.2.55-3.07 2.92.77 4.18L8 11.4l-3.75 2 .77-4.18L1.95 6.3l4.2-.55z" fill="currentColor" stroke="currentColor" strokeWidth="1.25" strokeLinejoin="round" />)

export const CardIcon =mark('CardIcon', <path d="M1.75 3.75h12.5v8.5H1.75zM1.75 6.5h12.5M4 10h2.5" {...STROKE} />)

/** A server — compute (a node). */
export const ServerIcon = mark('ServerIcon', <path d="M2.25 2.5h11.5v4.5H2.25zM2.25 9h11.5v4.5H2.25zM4.5 4.75h.01M4.5 11.25h.01" {...STROKE} />)

/** A magnifier — a search field's leading mark (`ui/SearchBox`, the Settings search). */
export const SearchIcon = mark('SearchIcon', <><circle cx="7" cy="7" r="4.75" {...STROKE} /><path d="M10.5 10.5 14 14" {...STROKE} /></>)

/** A filled tick — the current choice in a list (the org switcher). */
export const CheckIcon = mark('CheckIcon', <path d="M13.78 4.22a.75.75 0 0 1 0 1.06l-7.25 7.25a.75.75 0 0 1-1.06 0L2.22 9.28a.75.75 0 0 1 1.06-1.06L6 10.94l6.72-6.72a.75.75 0 0 1 1.06 0Z" fill="currentColor" />)

/** Three sliders — a toolbar's options menu (`ui/ToolbarMenu`). Knobs at different positions read as settings, not a
 *  list; sliders, not a funnel, because the menu also changes how a board is drawn. */
export const SlidersIcon = mark('SlidersIcon', <><path d="M2 4h12M2 8h12M2 12h12" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" /><circle cx="5.5" cy="4" r="2" fill="currentColor" /><circle cx="10.5" cy="8" r="2" fill="currentColor" /><circle cx="6.5" cy="12" r="2" fill="currentColor" /></>)

/* FILLED MATERIAL MARKS (24-unit paths): `@mui/icons-material` is not a dependency. */

/** A paper plane — Send (the comment composer). */
export const SendIcon = material('SendIcon', 'M2.01 21 23 12 2.01 3 2 10l15 2-15 2z')

/** `↑` — send, as an arrow (the support chat). */
export const ArrowUpIcon = material('ArrowUpIcon', 'm4 12 1.41 1.41L11 7.83V20h2V7.83l5.58 5.59L20 12l-8-8z')

/** A tick in a filled circle — done. */
export const CheckCircleIcon = material('CheckCircleIcon', 'M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2m-2 15-5-5 1.41-1.41L10 14.17l7.59-7.59L19 8z')

/** Two stacked units — a node row (an account's per-node rows). */
export const NodeIcon = material('NodeIcon', 'M20 13H4c-.55 0-1 .45-1 1v6c0 .55.45 1 1 1h16c.55 0 1-.45 1-1v-6c0-.55-.45-1-1-1M7 19c-1.1 0-2-.9-2-2s.9-2 2-2 2 .9 2 2-.9 2-2 2M20 3H4c-.55 0-1 .45-1 1v6c0 .55.45 1 1 1h16c.55 0 1-.45 1-1V4c0-.55-.45-1-1-1M7 9c-1.1 0-2-.9-2-2s.9-2 2-2 2 .9 2 2-.9 2-2 2')

/** A folder with a person — a NodeLink (a shared folder). */
export const NodeLinkIcon = material('NodeLinkIcon', 'M20 6h-8l-2-2H4c-1.1 0-1.99.9-1.99 2L2 18c0 1.1.9 2 2 2h16c1.1 0 2-.9 2-2V8c0-1.1-.9-2-2-2m-5 3c1.1 0 2 .9 2 2s-.9 2-2 2-2-.9-2-2 .9-2 2-2m4 8h-8v-1c0-1.33 2.67-2 4-2s4 .67 4 2v1z')

/* THE PIPELINE STEPS' MARKS (`components/pipeline/StageStrip.tsx`): the step between two stages, its word on hover.
   Line marks on the 16-grid, like the console's other controls: a filled glyph read heavier than the stage names. */

/** `▶` — Start, filled. */
export const PlayIcon = mark('PlayIcon', <path d="M5 3.25v9.5L12.75 8z" fill="currentColor" stroke="currentColor" strokeWidth={1.5} strokeLinejoin="round" />)

/** A branch joining the trunk — Merge. */
export const MergeIcon = mark('MergeIcon', <>
  <circle cx="4.5" cy="3.25" r="1.75" {...STROKE} /><circle cx="4.5" cy="12.75" r="1.75" {...STROKE} />
  <circle cx="11.75" cy="9.5" r="1.75" {...STROKE} /><path d="M4.5 5v6M4.5 5c0 2.75 2 4.5 5.5 4.5" {...STROKE} />
</>)

/** A delivery truck carrying the work on — Release. */
export const ReleaseIcon = mark('ReleaseIcon', <>
  <path d="M1.5 3.75h8v6.5h-8zM9.5 6.25h2.75l2.25 2.5v1.5h-5" {...STROKE} />
  <circle cx="4.5" cy="11.75" r="1.5" fill="currentColor" /><circle cx="11.75" cy="11.75" r="1.5" fill="currentColor" />
</>)

/** A clock face — when a release goes (the Release step's When). */
export const ClockIcon = material('ClockIcon', 'M11.99 2C6.47 2 2 6.48 2 12s4.47 10 9.99 10C17.52 22 22 17.52 22 12S17.52 2 11.99 2M12 20c-4.42 0-8-3.58-8-8s3.58-8 8-8 8 3.58 8 8-3.58 8-8 8m.5-13H11v6l5.25 3.15.75-1.23-4.5-2.67z')

/** `→` — Move. */
export const ArrowRightIcon = mark('ArrowRightIcon', <path d="M2.5 8h11M9 3.5 13.5 8 9 12.5" {...STROKE} />)

/* Material 24-unit paths (`viewBox="0 0 24 24"`) for hosts that draw their own `<svg>`/`SvgIcon`. */
export const CHECK_PATH = 'M9 16.17 4.83 12l-1.42 1.41L9 19 21 7l-1.41-1.41z'
export const CLOSE_PATH = 'M19 6.41 17.59 5 12 10.59 6.41 5 5 6.41 10.59 12 5 17.59 6.41 19 12 13.41 17.59 19 19 17.59 13.41 12z'
export const EXPAND_MORE_PATH = 'M16.59 8.59 12 13.17 7.41 8.59 6 10l6 6 6-6z'
export const EXPAND_LESS_PATH = 'm12 8-6 6 1.41 1.41L12 10.83l4.59 4.58L18 14z'
export const BACK_PATH = 'M20 11H7.83l5.59-5.59L12 4l-8 8 8 8 1.41-1.41L7.83 13H20z'
/** A clock with a back-turning arrow — history. */
export const HISTORY_PATH = 'M13 3a9 9 0 0 0-9 9H1l3.89 3.89.07.14L9 12H6c0-3.87 3.13-7 7-7s7 3.13 7 7-3.13 7-7 7c-1.93 0-3.68-.79-4.94-2.06l-1.42 1.42A8.954 8.954 0 0 0 13 21a9 9 0 0 0 0-18m-1 5v5l4.28 2.54.72-1.21-3.5-2.08V8z'
/** A filled plus — a new chat. */
export const NEW_CHAT_PATH = 'M19 13h-6v6h-2v-6H5v-2h6V5h2v6h6z'
