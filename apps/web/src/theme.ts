/* The console's ONE MUI theme — the app's single theme-factory call, and the only place `styleOverrides`
   may ever appear. Every other file styles an instance with `sx`. Palette, density contract
   and tap-target floor are pinned once here so every route inherits one answer. */
import { createTheme, getOverlayAlpha, type Theme } from '@mui/material/styles'
import { LOGO_THEME, type LogoTheme } from './components/logoTheme'

/* ── The paper's own ground, as ONE colour ──────────────────────────────────────────────────────────
   A dark scheme paints an elevated Paper as `background.paper` UNDER a white `--Paper-overlay` gradient, so a
   sticky bar filled plain `background.paper` inside a drawer, dialog or menu reads as a darker band. The
   overlay is a gradient, which a box-shadow cover cannot carry; `MuiPaper` below also sets `PAPER_GROUND`
   to the same colour flattened (`color-mix` of white at the overlay's alpha), inherited by everything inside.
   Outside any Paper the fallback is the plain paper. A plain `Box` filled `background.paper` inside an elevated
   Paper still inherits the lighter ground: a surface that hosts a `paperGround` sticky is a `Paper`, or resets it. */
export const PAPER_GROUND = '--paper-ground'

/** The fill for anything painted to sit flush on its paper: a sticky bar, a pinned cell, a sticky header. */
export const paperGround = (theme: Theme): string =>
  `var(${PAPER_GROUND}, ${(theme.vars ?? theme).palette.background.paper})`

/* ── The literal hex map, for every surface that cannot read CSS ──────────────────────────────────
   xterm, WebGL, canvas and `<meta name="theme-color">` parse colour STRINGS; under `cssVariables`
   `theme.palette.*` is `var(--mui-…)`, which they cannot resolve (silently black terminal). They read RAW. */
export const RAW = {
  light: {
    primary: '#1d4ed8',
    // Light's `primary` already carries white text at 6.7:1, so a filled surface keeps it (dark's differs).
    primaryFill: '#1d4ed8',
    primaryFillHover: '#1e40af',
    error: '#dc2626',
    success: '#16a34a',
    warning: '#ca8a04',
    info: '#7c3aed',
    backgroundDefault: '#f1f5f9',
    backgroundPaper: '#ffffff',
    textPrimary: '#111827',
    textSecondary: '#374151',
    divider: '#e5e7eb',
    // No `sidebarBg`: the rail reads CSS, and its ground is single-homed in `SIDEBAR_LIGHT`/`SIDEBAR_DARK`.
    // The terminal canvas is dark in BOTH schemes (xterm's contrast assumes a dark ground).
    terminalBg: '#0b0e14',
  },
  dark: {
    primary: '#3b82f6',
    /* The ground of a FILLED primary surface (contained button, filled chip) under dark: `primary` carries
       white text at only 3.67:1, under WCAG AA's 4.5:1; this one is 5.17:1. `primary` stays the accent
       for text and outlines on dark paper, where the lighter blue is what reads. */
    primaryFill: '#2563eb',
    primaryFillHover: '#1d4ed8',
    error: '#f87171',
    success: '#22c55e',
    warning: '#eab308',
    info: '#7c3aed',
    backgroundDefault: '#0f172a',
    backgroundPaper: '#1e293b',
    textPrimary: '#f1f5f9',
    // Slate-400: reads as muted beside `textPrimary`, still 5.7:1 on `backgroundPaper` (WCAG AA).
    textSecondary: '#94a3b8',
    divider: '#334155',
    terminalBg: '#0b0e14',
  },
} as const

/* LITERAL font stacks, not `var(--font-sans)` (dies with `styles.css`). MUI's default is Roboto, which is
   not a dependency, so stock would silently become Arial with wider metrics.
   SYMBOL_FALLBACK: the status glyphs (✓ ✕ ■) live in Dingbats/Geometric Shapes, which a
   bare Linux `system-ui` (and some Windows UI faces) do not carry, so they rendered as tofu boxes. These
   faces are tried for a missing glyph only, ahead of the generic family, whose own fallback is not
   guaranteed to reach a symbol font. */
const SYMBOL_FALLBACK = "'Segoe UI Symbol', 'Apple Symbols', 'Noto Sans Symbols 2', 'DejaVu Sans'"
const SANS_STACK = `system-ui, -apple-system, BlinkMacSystemFont, 'Segoe UI', ${SYMBOL_FALLBACK}, sans-serif`
const MONO_STACK = `ui-monospace, 'SFMono-Regular', Consolas, 'Liberation Mono', ${SYMBOL_FALLBACK}, monospace`

/* The wordmark's colours per logo theme and rail scheme: `mark` is its three gradient stops, `markCore` the lit
   tile (the dot of the j). The active theme is `logoTheme.ts`'s switch. */
type MarkColors = { mark: readonly [string, string, string]; markCore: string }
const LOGO_COLORS: Record<LogoTheme, { light: MarkColors; dark: MarkColors }> = {
  standard: {
    light: {
      // Darkened stops: the dark gradient contrasts under 2:1 on slate-200.
      mark: ['#0369a1', '#1d4ed8', '#7c3aed'],
      // The lit tile is jaah.ai's signal teal, off the gradient's hues so the dot of the j reads as lit.
      markCore: '#0f766e',
    },
    dark: { mark: ['#38bdf8', '#3b82f6', '#a855f7'], markCore: '#5eead4' },
  },
  // Pumpkin orange, the j's dot a green stem. Light runs darker for the same slate-200 contrast.
  halloween: {
    light: { mark: ['#ea580c', '#c2410c', '#9a3412'], markCore: '#15803d' },
    dark: { mark: ['#fdba74', '#f97316', '#ea580c'], markCore: '#4ade80' },
  },
}

/* The sidebar chrome FOLLOWS the colour scheme, with its own scale (it is CHROME, distinct from
   `palette.background`). Split light/dark because `theme.custom` sits outside the colour-scheme system:
   consumers write the light value and reach dark via `theme.applyStyles('dark', …)` — never a ternary on
   `palette.mode`, which is frozen under `cssVariables`. `ui/listStyles.ts`'s `schemed()` is the ONLY way
   in. Every rail colour comes from here; a hardcoded literal in a rail-painting file is a light-theme bug. */
const SIDEBAR_LIGHT = {
  /* Slate-200, one step BELOW `background.default` (#f1f5f9): at #f1f5f9 the rail merged with the page.
     Gives light the same chrome/page/paper stack as dark. */
  bg: '#e2e8f0',
  // Slate-300, not `palette.divider` (invisible on slate).
  border: '#cbd5e1',
  text: '#475569',
  textActive: '#ffffff',
  activeBg: '#2563eb',
  /* Darkens away from the ground, as the dark rail's hover does, so a hovered row lifts in both schemes. */
  hoverBg: '#cbd5e1',
  textStrong: '#0f172a',
  accent: '#1d4ed8',
  overlayHover: 'rgba(15, 23, 42, 0.06)',
  bannerBg: 'rgba(29, 78, 216, 0.10)',
  /* OPAQUE, darker than `activeBg`: a translucent wash over the solid blue Reload pill drops its white label
     to 1.94:1. The dark half stays a wash (it darkens there). */
  bannerHoverBg: '#1e40af',
  /* Profile / Sign out have NO background; this border is held to the 3:1 non-text floor (#cbd5e1 was 1.20:1). */
  buttonBorder: '#64748b',
  buttonHoverBorder: '#475569',
  /* Rail-tuned connection-dot states: `palette.success/warning.main` land at 2.67:1 / 2.38:1 on the light
     rail. The dark half keeps the palette's value. */
  statusOk: '#15803d',
  statusWarn: '#a16207',
  ...LOGO_COLORS[LOGO_THEME].light,
} as const

const SIDEBAR_DARK = {
  bg: '#0b1220',
  border: '#1e293b',
  text: '#94a3b8',
  textActive: '#ffffff',
  activeBg: '#2563eb',
  hoverBg: '#172033',
  textStrong: '#e2e8f0',
  accent: '#60a5fa',
  overlayHover: 'rgba(255, 255, 255, 0.08)',
  bannerBg: 'rgba(96, 165, 250, 0.12)',
  bannerHoverBg: 'rgba(96, 165, 250, 0.28)',
  buttonBorder: '#475569',
  buttonHoverBorder: '#64748b',
  statusOk: '#22c55e',
  statusWarn: '#eab308',
  ...LOGO_COLORS[LOGO_THEME].dark,
} as const

/* A divider inside a dark menu: the menu's elevated paper lifts to about `RAW.dark.divider` itself, so every
   separator on it (`Divider`, a `borderColor: 'divider'` rule) vanished. Slate-500: about 2:1 on that paper. */
export const MENU_DIVIDER_DARK = '#64748b'

// Tooltip grounds: opaque, so nothing under a tooltip shows through (read by `MuiTooltip` below).
export const TOOLTIP = {
  light: { bg: '#1f2937', text: '#ffffff' },
  dark: { bg: '#334155', text: '#f1f5f9', border: '#475569' },
} as const

export interface SidebarColors {
  bg: string
  border: string
  text: string
  textActive: string
  activeBg: string
  hoverBg: string
  /** Hover and chrome-caption emphasis — a step FURTHER from the rail's ground than `text`. */
  textStrong: string
  /** The rail's accent (selection bar, focus ring, …). NOT `palette.primary.main`, which is tuned for paper. */
  accent: string
  /** Hover wash for the bare icon affordances (the collapse toggle) that carry no row background. */
  overlayHover: string
  /** The update banner's tinted ground, and its Reload button's hover. */
  bannerBg: string
  bannerHoverBg: string
  /** The outlined Profile / Sign out buttons in the ≤480px "More" sheet. */
  buttonBorder: string
  buttonHoverBorder: string
  /** The connection dot, overriding `palette.success/warning.main` for the rail's ground only. */
  statusOk: string
  statusWarn: string
  /** The Jaah mark's and wordmark's three SVG gradient stops, set as `--sk-mark-*` custom properties by
   *  the rail and the login card. */
  mark: readonly [string, string, string]
  /** The wordmark's lit tile (the dot of the j) — the one shape painted flat rather than from the gradient. */
  markCore: string
}

export interface ConsoleCustomTokens {
  sidebar: {
    /** Expanded rail width in px — `--sidebar-width`. */
    width: number
    /** Expanded rail width in px below `compactBelowPx` (a 1024px laptop). */
    widthCompact: number
    /** The viewport width below which the expanded rail is `widthCompact`. */
    compactBelowPx: number
    /** Collapsed rail width in px — `--sidebar-width-collapsed`. */
    widthCollapsed: number
    light: SidebarColors
    dark: SidebarColors
  }
  /** The truncation floor: a box narrower than this many characters is too short to identify. */
  minReadableCh: number
  /** The touch floor the e2e suite already enforces at ≤480px. */
  tapTargetPx: number
}

declare module '@mui/material/styles' {
  // Enables the typing half of `cssVariables` (`theme.vars`, per-scheme `colorSchemes`).
  interface CssThemeVariables {
    enabled: true
  }
  interface Theme {
    custom: ConsoleCustomTokens
  }
  interface ThemeOptions {
    custom?: ConsoleCustomTokens
  }
  // MUI ships no mono variant; data columns (ids, durations, timestamps) need one to align.
  interface TypographyVariants {
    mono: React.CSSProperties
  }
  interface TypographyVariantsOptions {
    mono?: React.CSSProperties
  }
}

declare module '@mui/material/Typography' {
  interface TypographyPropsVariantOverrides {
    mono: true
  }
}

export const theme = createTheme({
  /* The palette is emitted as CSS variables under `[data-theme="light"|"dark"]` — the attribute `auth.tsx`
     writes and stays the SOLE writer of: MUI's init-color-scheme script is not mounted and its color-scheme
     hook is never called. Portalled surfaces still sit under `<html data-theme>` and inherit (AC8). */
  cssVariables: { colorSchemeSelector: '[data-theme="%s"]' },
  defaultColorScheme: 'light',
  colorSchemes: {
    light: {
      palette: {
        primary: { main: RAW.light.primary },
        // MUI's stock `secondary` is magenta, a defect per AC4(e); seeded from `--purple` like `info`.
        secondary: { main: '#7c3aed' },
        error: { main: RAW.light.error },
        success: { main: RAW.light.success },
        warning: { main: RAW.light.warning },
        info: { main: RAW.light.info },
        background: { default: RAW.light.backgroundDefault, paper: RAW.light.backgroundPaper },
        text: { primary: RAW.light.textPrimary, secondary: RAW.light.textSecondary },
        divider: RAW.light.divider,
      },
    },
    dark: {
      palette: {
        primary: { main: RAW.dark.primary },
        secondary: { main: '#a78bfa' },
        error: { main: RAW.dark.error },
        success: { main: RAW.dark.success },
        warning: { main: RAW.dark.warning },
        info: { main: RAW.dark.info },
        background: { default: RAW.dark.backgroundDefault, paper: RAW.dark.backgroundPaper },
        text: { primary: RAW.dark.textPrimary, secondary: RAW.dark.textSecondary },
        divider: RAW.dark.divider,
      },
    },
  },

  /* NO `fontSize`: MUI's 14 stays. Density is bought with SPACE, never type size — 13 breaks the 15px
     `body1` floor `e2e/tasks-tree-measure.spec.ts:128` asserts. */
  typography: {
    fontFamily: SANS_STACK,
    mono: { fontFamily: MONO_STACK },
  },

  /* Density contract (MUI has no density API) — pinned once here. */
  spacing: 6, // MUI default is 8.

  /* MUI's 225ms Grow/Collapse re-pointed at the app's 120–220ms scale; specs measure right after a click. */
  transitions: {
    duration: {
      shortest: 120,
      shorter: 160,
      short: 180,
      standard: 180,
      complex: 220,
      enteringScreen: 180,
      leavingScreen: 120,
    },
  },

  custom: {
    /* 212 = the operator's 20% cut from 264, so the board gets the width back. The task tree
       (`tasks/rail/`) keeps ~19 characters of a deepest-level feature's name beside its Stopped pill; 180,
       the header row's floor, shredded names to ~10. Breakpoints in `ui/listStyles.ts`
       were derived from the wider rail, so a narrower one only leaves them
       conservative. Below 1280px it is 192 (240 less the same 20%). */
    sidebar: {
      width: 212,
      widthCompact: 192,
      compactBelowPx: 1280,
      widthCollapsed: 52,
      light: SIDEBAR_LIGHT,
      dark: SIDEBAR_DARK,
    },
    minReadableCh: 12,
    tapTargetPx: 44,
  },

  components: {
    /* ── Button-label casing, single-homed ────────────────────────────────────────────────────────
       `typography.button` bakes in `uppercase`; labels must render as authored ("Sign In", AC4(e)). Scoped to
       exactly `MuiButton`/`MuiTab`/`MuiToggleButton` (the ones observed uppercased), not a blanket
       `typography.button` override, so it cannot reach an unaudited component. */
    MuiButton: {
      defaultProps: { size: 'small', disableElevation: true },
      styleOverrides: {
        root: ({ theme }) => ({
          textTransform: 'none',
          // Dark's filled primary ground, for AA contrast under white text (RAW.dark.primaryFill).
          '&.MuiButton-contained.MuiButton-colorPrimary:not(.Mui-disabled)': theme.applyStyles('dark', {
            backgroundColor: RAW.dark.primaryFill,
            '&:hover, &.Mui-focusVisible': { backgroundColor: RAW.dark.primaryFillHover },
          }),
        }),
      },
    },
    MuiPaper: {
      styleOverrides: {
        // `PAPER_GROUND` above: plain for every Paper, flattened with the overlay for an elevated one in dark.
        root: ({ ownerState, theme }) => {
          const paper = (theme.vars ?? theme).palette.background.paper
          const elevation = ownerState.elevation ?? 1
          return {
            [PAPER_GROUND]: paper,
            ...(ownerState.variant === 'outlined' || elevation === 0 ? {} : theme.applyStyles('dark', {
              [PAPER_GROUND]: `color-mix(in srgb, #fff ${Math.round(getOverlayAlpha(elevation) * 1000) / 10}%, ${paper})`,
            })),
          }
        },
      },
    },
    MuiChip: {
      defaultProps: { size: 'small' },
      styleOverrides: {
        root: ({ theme }) => ({
          // The override outranks MUI's own clickable hover/focus, so it carries them too.
          '&.MuiChip-filled.MuiChip-colorPrimary': theme.applyStyles('dark', {
            backgroundColor: RAW.dark.primaryFill,
            '&.MuiChip-clickable:hover, &.MuiChip-clickable.Mui-focusVisible, &.MuiChip-deletable.Mui-focusVisible': {
              backgroundColor: RAW.dark.primaryFillHover,
            },
          }),
        }),
      },
    },
    MuiFab: { defaultProps: { size: 'small' } },
    MuiToggleButton: {
      defaultProps: { size: 'small' },
      styleOverrides: {
        root: ({ theme }) => ({
          textTransform: 'none',
          /* A `primary` picker's chosen segment is FILLED: MUI's blue text on an ~8% tint read dimmer than the
             unselected grey under dark, so the toggle looked inverted. Grounds as `MuiButton`'s. */
          '&.Mui-selected.MuiToggleButton-primary:not(.Mui-disabled)': {
            backgroundColor: RAW.light.primaryFill,
            color: theme.palette.common.white,
            '&:hover, &.Mui-focusVisible': { backgroundColor: RAW.light.primaryFillHover },
            ...theme.applyStyles('dark', {
              backgroundColor: RAW.dark.primaryFill,
              '&:hover, &.Mui-focusVisible': { backgroundColor: RAW.dark.primaryFillHover },
            }),
          },
        }),
      },
    },
    MuiTextField: { defaultProps: { size: 'small', margin: 'dense' } },
    MuiFormControl: { defaultProps: { size: 'small', margin: 'dense' } },
    MuiSelect: { defaultProps: { size: 'small' } },
    MuiAutocomplete: { defaultProps: { size: 'small' } },
    MuiTable: { defaultProps: { size: 'small' } },

    /* ── The column heading, single-homed ─────────────────────────────────────────────────────────
       Table headings are a CAPTION; one treatment for every table, here because a rule reaching every table
       without opt-in is what `styleOverrides` is for.
       0.6875rem (11px) is a FLOOR at EVERY width — never shrink type to buy space. No surviving spec asserts
       a heading's computed size (no test pins computed type sizes), so a per-page shrink reddens nothing.
       `theme.vars.palette…`, not `theme.palette…` (a static light literal) — the `RAW` trap, in a callback. */
    MuiTableCell: {
      styleOverrides: {
        root: { paddingTop: 4, paddingBottom: 4 },
        head: ({ theme }) => ({
          fontSize: '0.6875rem',
          fontWeight: 600,
          letterSpacing: '0.04em',
          textTransform: 'uppercase',
          lineHeight: 1.4,
          color: theme.vars.palette.text.secondary,
        }),
      },
    },

    /* ── The row hover tint, single-homed ─────────────────────────────────────────────────────────
       A real slate tint instead of `palette.action.hover` (a wash on dark paper). Keyed on
       `.MuiTableRow-hover`, so only rows that opted into hover change. */
    MuiTableRow: {
      styleOverrides: {
        root: ({ theme }) => ({
          '&.MuiTableRow-hover:hover': {
            backgroundColor: '#f8fafc',
            ...theme.applyStyles('dark', { backgroundColor: '#243247' }),
          },
        }),
      },
    },

    MuiToolbar: { defaultProps: { variant: 'dense' } },
    MuiList: { defaultProps: { dense: true } },

    /* ── The tap-target floor: breakpoint-aware, and a floor that only ever RAISES ────────────────
       `max(44px, <component default>)`, never a bare 44 (which would LOWER a 48px default while a
       `>= tapTargetPx` oracle stays green). `down('sm')` only, so desktop keeps the dense sizes. */
    MuiIconButton: {
      defaultProps: { size: 'small' },
      styleOverrides: {
        // IconButton has no default dimension, so both are pure raises (square: 44x28 is not 44px).
        root: ({ theme }) => ({
          [theme.breakpoints.down('sm')]: { minHeight: 44, minWidth: 44 },
        }),
      },
    },
    MuiTab: {
      styleOverrides: {
        root: ({ theme }) => ({
          textTransform: 'none',
          [theme.breakpoints.down('sm')]: { minHeight: 'max(44px, 48px)' },
        }),
      },
    },
    MuiMenuItem: {
      defaultProps: { dense: true },
      styleOverrides: {
        // No `minWidth`: menu items (and list buttons below) are full-width by construction.
        root: ({ theme }) => ({
          [theme.breakpoints.down('sm')]: { minHeight: 'max(44px, 48px)' },
        }),
      },
    },
    MuiListItemButton: {
      defaultProps: { dense: true },
      styleOverrides: {
        root: ({ theme }) => ({
          [theme.breakpoints.down('sm')]: { minHeight: 'max(44px, 48px)' },
        }),
      },
    },

    MuiTypography: { defaultProps: { variantMapping: { mono: 'span' } } },

    /* Every popover (a `Menu` is one) re-points the divider token for what it holds, under dark only
       (`MENU_DIVIDER_DARK`); light's divider already reads on white. */
    MuiPopover: {
      styleOverrides: {
        paper: ({ theme }) => theme.applyStyles('dark', { '--mui-palette-divider': MENU_DIVIDER_DARK }),
      },
    },

    /* ── The tooltip ground, single-homed ────────────────────────────────────────────────────────
       MUI's stock tooltip is translucent grey, so list text under it bled through the copy.
       OPAQUE hex in both schemes; dark lifts one slate step above `background.paper` and adds a border so
       the bubble still separates from the page. */
    MuiTooltip: {
      styleOverrides: {
        tooltip: ({ theme }) => ({
          backgroundColor: TOOLTIP.light.bg,
          color: TOOLTIP.light.text,
          ...theme.applyStyles('dark', {
            backgroundColor: TOOLTIP.dark.bg,
            color: TOOLTIP.dark.text,
            border: `1px solid ${TOOLTIP.dark.border}`,
          }),
        }),
        arrow: ({ theme }) => ({
          color: TOOLTIP.light.bg,
          ...theme.applyStyles('dark', { color: TOOLTIP.dark.bg }),
        }),
      },
    },

    /* ── AC11's focus ring, single-homed ─────────────────────────────────────────────────────────
       MUI's stock `.Mui-focusVisible` is a TINT, not a ring — migrated pages had no visible focus indicator.
       Hence `MuiButtonBase` (every button/icon-button/tab/list-item-button/menu-item) AND `MuiLink` (its own
       focus-visible detection). Local copies should defer to this; the Sidebar rail is the one exception
       (rings in its `custom.sidebar` accent).
       `theme.vars.palette.primary.main`, NOT `theme.palette…`: the static literal stays `#1d4ed8` under dark
       (verified in `theme.test.tsx`). */
    MuiButtonBase: {
      styleOverrides: {
        root: ({ theme }) => ({
          '&.Mui-focusVisible': {
            outline: `2px solid ${theme.vars.palette.primary.main}`,
            outlineOffset: '1px',
          },
        }),
      },
    },
    MuiLink: {
      styleOverrides: {
        root: ({ theme }) => ({
          '&.Mui-focusVisible': {
            outline: `2px solid ${theme.vars.palette.primary.main}`,
            outlineOffset: '1px',
          },
        }),
      },
    },
  },
})
