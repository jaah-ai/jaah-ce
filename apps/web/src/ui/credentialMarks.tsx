import type { JSX, ReactNode } from 'react'
import Box from '@mui/material/Box'
import { firstCodepoints } from '../lib/strings'
import { Icon } from './icons'

/* The vendor mark for a credential type — the one slug→mark table for every surface.
 * 1. Bundled inline `<svg>`, never fetched: no URL, no icon package, renders offline.
 * 2. Monochrome from `currentColor`: theme-correct without branches; no brand colours at 18px.
 * 3. Total: an unknown slug (the registry is the server's) gets the monogram.
 * 4. `aria-hidden`; every consumer renders the type's label beside it.
 * Drawn: `github`, `github_app`, `gitlab`, `slack`, `jira`, `aws`, `google`, `anthropic_api_key`. The
 * rest monogram by design (no vendor, or marks that do not survive one colour at 18px). The eight are a
 * floor (`DRAWN_MARK_SLUGS`): a monogram-only build would pass every constraint above.
 * Licensing: hand-authored simplifications, nominative use beside the vendor's name; no brand-kit files.
 * New mark: one `MARKS` entry, `0 0 24 24` viewBox, `currentColor` only; if it fails at 18px, omit it. */

/** Mark edge in px, shared by drawn glyphs and the monogram so the column aligns. */
export const CREDENTIAL_MARK_SIZE = 18

/* Shared `<svg>` shell: `aria-hidden` (constraint 4), `flexShrink: 0`, `display: block` (no baseline
   gap). The two stroked marks say so on their own elements. */
function Glyph({ children }: { children: ReactNode }): JSX.Element {
  return (
    <Icon viewBox="0 0 24 24" size={CREDENTIAL_MARK_SIZE} sx={{ display: 'block', fill: 'currentColor' }}>
      {children}
    </Icon>
  )
}

/* Octocat, shared by `github` and `github_app`. */
const OCTOCAT_D =
  'M12 .3C5.37.3 0 5.67 0 12.3c0 5.3 3.44 9.8 8.2 11.39.6.11.83-.26.83-.58 0-.28-.01-1.04-.02-2.04-3.34.73-4.04-1.6-4.04-1.6-.55-1.39-1.34-1.76-1.34-1.76-1.08-.75.09-.73.09-.73 1.2.08 1.84 1.23 1.84 1.23 1.07 1.84 2.8 1.31 3.49 1 .11-.78.42-1.31.76-1.61-2.67-.3-5.47-1.33-5.47-5.93 0-1.31.47-2.38 1.24-3.22-.13-.3-.54-1.52.11-3.18 0 0 1-.32 3.3 1.23a11.5 11.5 0 0 1 6 0c2.29-1.55 3.3-1.23 3.3-1.23.64 1.66.24 2.88.12 3.18.77.84 1.23 1.91 1.23 3.22 0 4.61-2.81 5.63-5.48 5.92.43.37.81 1.1.81 2.22 0 1.6-.01 2.9-.01 3.29 0 .31.22.69.83.57C20.57 22.09 24 17.59 24 12.3 24 5.67 18.63.3 12 .3Z'

/* One Slack arm rotated four times to keep the symmetry exact; the off-centre bar opens the middle hole. */
function SlackArm(): JSX.Element {
  return (
    <>
      <rect x="13.2" y="1.6" width="4.4" height="11.6" rx="2.2" />
      <rect x="8.6" y="1.6" width="4.6" height="4.4" rx="2.2" />
    </>
  )
}

/* slug → the drawn mark. Eight entries; everything else monograms (see `credentialMark`). */
const MARKS: Readonly<Record<string, () => JSX.Element>> = {
  github: () => (
    <Glyph>
      <path d={OCTOCAT_D} />
    </Glyph>
  ),

  /* Octocat in a tile: distinguishes the App from a personal account in adjacent rows. */
  github_app: () => (
    <Glyph>
      <rect
        x="1"
        y="1"
        width="22"
        height="22"
        rx="5.5"
        fill="none"
        stroke="currentColor"
        strokeWidth="1.9"
      />
      <path d={OCTOCAT_D} transform="translate(5.4 5.4) scale(0.55)" />
    </Glyph>
  ),

  /* Tanuki reduced: three flames over a wedge. */
  gitlab: () => (
    <Glyph>
      <path d="M12 1.6 16.4 10.4 7.6 10.4Z" />
      <path d="M4.1 4.4 7.6 10.4 0.6 10.4Z" />
      <path d="M19.9 4.4 23.4 10.4 16.4 10.4Z" />
      <path d="M0.6 10.4 23.4 10.4 12 22.6Z" />
    </Glyph>
  ),

  slack: () => (
    <Glyph>
      <SlackArm />
      <g transform="rotate(90 12 12)">
        <SlackArm />
      </g>
      <g transform="rotate(180 12 12)">
        <SlackArm />
      </g>
      <g transform="rotate(270 12 12)">
        <SlackArm />
      </g>
    </Glyph>
  ),

  /* Atlassian's nested chevrons. */
  jira: () => (
    <Glyph>
      <path d="M12 1.4 23 12.4 19 16.4 12 9.4 5 16.4 1 12.4Z" />
      <path d="M12 12.8 18.2 19 15 22.2 12 19.2 9 22.2 5.8 19Z" />
    </Glyph>
  ),

  /* The lower-case "aws" over the smile: the smile alone reads as a spinner at 18px. Strokes, not
     filled outlines, so the letters keep their weight at this scale; the arrowhead is the one fill. */
  aws: () => (
    <Glyph>
      <g fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
        <path d="M1.8 5.2C2.8 3.9 5.8 3.9 5.8 6.4V11.6M5.8 8.2C3.4 7.8 1.4 8.5 1.4 10.1C1.4 11.9 4.4 12.2 5.8 10.4" />
        <path d="M7.6 4.6 9.5 11.6 11.8 6.2 14.1 11.6 16 4.6" />
        <path d="M22.1 5.3C21 4 17.7 4.2 17.8 6.1C17.9 7.9 22.4 7.5 22.4 9.7C22.4 12.1 18.7 12.2 17.5 10.8" />
        <path d="M2.2 15.8C7.6 19.8 14.6 20.1 19.6 16.9" strokeWidth="1.9" />
      </g>
      <path d="M23 15.2 18.2 15.4 21.2 19.4Z" />
    </Glyph>
  ),

  /* The Google "G". */
  google: () => (
    <Glyph>
      <path d="M12.48 10.92v3.28h7.84c-.24 1.84-.853 3.187-1.787 4.133-1.147 1.147-2.933 2.4-6.053 2.4-4.827 0-8.6-3.893-8.6-8.72s3.773-8.72 8.6-8.72c2.6 0 4.507 1.027 5.907 2.347l2.307-2.307C18.747 1.44 16.133 0 12.48 0 5.867 0 .307 5.387.307 12s5.56 12 12.173 12c3.573 0 6.267-1.173 8.373-3.36 2.16-2.16 2.84-5.213 2.84-7.667 0-.76-.053-1.467-.173-2.053H12.48Z" />
    </Glyph>
  ),

  /* Anthropic's wedge "A" — two strokes meeting at the apex, the left one carrying the crossbar. */
  anthropic_api_key: () => (
    <Glyph>
      <path d="M13.83 3.52h3.6L24 20.48h-3.6L13.83 3.52ZM6.57 3.52h3.77l6.57 16.96H13.24L11.9 17.02H5.02l-1.34 3.46H0L6.57 3.52Zm.44 10.34h4.98L8.68 6.68 7.01 13.86Z" />
    </Glyph>
  ),
}

/**
 * A type's label → one or two upper-case monogram letters. From the label, never the slug (the UI shows
 * `type_label`). Two+ words: first and last initials; one word: first two letters. Capped after
 * upper-casing (a ligature can upper-case into two). `''` for a blank label.
 */
export function credentialMonogram(label: string): string {
  const words = label.trim().split(/\s+/).filter(word => word !== '')
  if (words.length === 0) return ''
  const letters =
    words.length === 1
      ? firstCodepoints(words[0], 2)
      : `${firstCodepoints(words[0], 1)}${firstCodepoints(words[words.length - 1], 1)}`
  return firstCodepoints(letters.toUpperCase(), 2)
}

/* Fallback tile: outlined square holding the letters. No `lib/authorBadge.ts` hues — slugs are distinct,
   so colour would carry no fact. */
function MonogramTile({ label }: { label: string }): JSX.Element {
  const letters = credentialMonogram(label)
  return (
    <Box
      aria-hidden="true"
      sx={{
        width: CREDENTIAL_MARK_SIZE,
        height: CREDENTIAL_MARK_SIZE,
        flexShrink: 0,
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        borderRadius: '4px',
        border: '1px solid currentColor',
        boxSizing: 'border-box',
        /* Two letters fit inside the rule; negative tracking keeps the pair centred. */
        fontSize: letters.length > 1 ? '9px' : '11px',
        fontWeight: 700,
        letterSpacing: '-0.03em',
        lineHeight: 1,
        userSelect: 'none',
      }}
    >
      {letters}
    </Box>
  )
}

/**
 * A credential type's mark. Total: an unknown slug monograms. `label` is used only for the monogram;
 * pass the server's `type_label`.
 */
export function credentialMark(slug: string, label: string): ReactNode {
  const drawn = Object.prototype.hasOwnProperty.call(MARKS, slug) ? MARKS[slug] : undefined
  return drawn ? drawn() : <MonogramTile label={label} />
}

/** Slugs with a drawn mark, pinned by a test so one cannot drop silently. */
export const DRAWN_MARK_SLUGS: readonly string[] = Object.keys(MARKS)
