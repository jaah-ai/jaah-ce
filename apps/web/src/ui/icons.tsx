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
}

/** THE ONE `<svg>` wrapper for the console: every inline mark renders through it (`viewBox` for a 24-grid path). */
export function Icon({ viewBox = '0 0 16 16', size = '1em', sx, testId, children }: IconProps & { viewBox?: string; children: ReactNode }) {
  return (
    <Box
      component="svg"
      viewBox={viewBox}
      aria-hidden="true"
      focusable="false"
      data-testid={testId}
      sx={[
        { width: size, height: size, display: 'inline-block', verticalAlign: 'middle', flexShrink: 0 },
        ...(Array.isArray(sx) ? sx : [sx]),
      ]}
    >
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
const mark = (name: string, children: ReactNode) =>
  Object.assign((props: IconProps) => <Icon {...props}>{children}</Icon>, { displayName: name })

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

/** A folder — Directory. */
export const FolderIcon = mark('FolderIcon', <path d="M1.75 4.25v8.5h12.5v-7h-6.5l-1.5-1.5h-4.5z" {...STROKE} />)

/** A lightning bolt — Automation. */
export const BoltIcon = mark('BoltIcon', <path d="M9 1.75 3.5 9h4l-1 5.25L12.5 7h-4z" {...STROKE} />)

/** A shield — Compliance. */
export const ShieldIcon = mark('ShieldIcon', <path d="M8 1.75 2.75 3.75v4c0 3 2.25 5.25 5.25 6.5 3-1.25 5.25-3.5 5.25-6.5v-4z" {...STROKE} />)

/** A page of log lines — Audit. */
export const LogIcon = mark('LogIcon', <path d="M3.25 1.75h7l2.5 2.5v10h-9.5zM5.75 6.5h4.5M5.75 9h4.5M5.75 11.5h3" {...STROKE} />)

/** Two people — Customers. */
export const PeopleIcon = mark('PeopleIcon', <><circle cx="6" cy="5.25" r="2.25" {...STROKE} /><path d="M1.75 13.5a4.25 4.25 0 0 1 8.5 0M10.5 3.25a2.25 2.25 0 0 1 0 4M12 9.5a4.25 4.25 0 0 1 2.25 4" {...STROKE} /></>)

/** A box — Products. */
export const BoxIcon = mark('BoxIcon', <path d="M8 1.75 13.75 4.5v7L8 14.25 2.25 11.5v-7zM2.25 4.5 8 7.25l5.75-2.75M8 7.25v7" {...STROKE} />)

/** A cog — Settings. */
export const CogIcon = mark('CogIcon', <><circle cx="8" cy="8" r="2" {...STROKE} /><circle cx="8" cy="8" r="4.5" {...STROKE} /><path d="M8 1.5v2M8 12.5v2M1.5 8h2M12.5 8h2M3.4 3.4l1.4 1.4M11.2 11.2l1.4 1.4M3.4 12.6l1.4-1.4M11.2 4.8l1.4-1.4" {...STROKE} /></>)

/** A card — Billing. */
export const TemplatesIcon = mark('TemplatesIcon', <path d="M2 2.5h5v5H2zM9 2.5h5v5H9zM2 9.5h5v4H2zM9 9.5h5v4H9z" {...STROKE} />)

export const StarIcon = mark('StarIcon', <path d="M8 1.9l1.85 3.85 4.2.55-3.07 2.92.77 4.18L8 11.4l-3.75 2 .77-4.18L1.95 6.3l4.2-.55z" {...STROKE} />)

export const StarFilledIcon = mark('StarFilledIcon', <path d="M8 1.9l1.85 3.85 4.2.55-3.07 2.92.77 4.18L8 11.4l-3.75 2 .77-4.18L1.95 6.3l4.2-.55z" fill="currentColor" stroke="currentColor" strokeWidth="1.25" strokeLinejoin="round" />)

export const CardIcon =mark('CardIcon', <path d="M1.75 3.75h12.5v8.5H1.75zM1.75 6.5h12.5M4 10h2.5" {...STROKE} />)

/** A server — compute (a node). */
export const ServerIcon = mark('ServerIcon', <path d="M2.25 2.5h11.5v4.5H2.25zM2.25 9h11.5v4.5H2.25zM4.5 4.75h.01M4.5 11.25h.01" {...STROKE} />)

/** A git branch — a forge connection. */
export const BranchIcon = mark('BranchIcon', <><circle cx="4.5" cy="3.5" r="1.5" {...STROKE} /><circle cx="4.5" cy="12.5" r="1.5" {...STROKE} /><circle cx="11.5" cy="5" r="1.5" {...STROKE} /><path d="M4.5 5v6M11.5 6.5c0 3-7 2.5-7 4.5" {...STROKE} /></>)

/* Material 24-unit paths (`viewBox="0 0 24 24"`) for hosts that draw their own `<svg>`/`SvgIcon`. */
export const CHECK_PATH = 'M9 16.17 4.83 12l-1.42 1.41L9 19 21 7l-1.41-1.41z'
export const CLOSE_PATH = 'M19 6.41 17.59 5 12 10.59 6.41 5 5 6.41 10.59 12 5 17.59 6.41 19 12 13.41 17.59 19 19 17.59 13.41 12z'
export const EXPAND_MORE_PATH = 'M16.59 8.59 12 13.17 7.41 8.59 6 10l6 6 6-6z'
export const EXPAND_LESS_PATH = 'm12 8-6 6 1.41 1.41L12 10.83l4.59 4.58L18 14z'
