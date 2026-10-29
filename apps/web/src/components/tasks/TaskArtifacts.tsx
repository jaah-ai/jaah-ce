import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import { useParams } from 'react-router-dom'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import CircularProgress from '@mui/material/CircularProgress'
import IconButton from '@mui/material/IconButton'
import Typography from '@mui/material/Typography'
import type { KeyboardEvent, ReactNode } from 'react'
import { apiFetch, apiHref, getText, noticeRefusal, useApiRewriterVersion } from '../../api'
import type { Artifact } from '../../types'
import { Banner } from '../../ui/Banner'
import { DownloadGlyph } from '../../ui/DownloadGlyph'
import { formatBytes } from '../../lib/formatBytes'
import { ARTIFACT_PARAM, parseArtifactParam } from '../../lib/taskArtifactLink'
import FeedList from '../../ui/feed/FeedList'
import type { FeedRowSpec } from '../../ui/feed/types'
import { StatusChip } from '../../ui/StatusChip'
import { ABOVE_PHONE, ARTIFACTS_LIST_CONTAINER_SX, PHONE, monoSx, tapTargetSx } from '../../ui/listStyles'
import { absoluteTime, compactAge, relativeAge } from '../../lib/relativeTime'
import RichTextRenderer from '../richtext/RichTextRenderer'
import { type ArtifactRenderMode, artifactKindLabel, artifactRenderMode, isDownloadArtifactKind } from './artifactKinds'
import { CodeBlock } from '../../ui/CodeBlock'
import SplitDivider from '../SplitDivider'
import { usePersistedState } from '../../hooks/usePersistedState'
import { useApiRead } from '../../hooks/useApiRead'
import { useUrlFilters } from '../../hooks/useUrlFilters'
import { readJson, writeJson } from '../../lib/safeStorage'
import { ROW_H } from '../../ui/feed/geometry'

/* ── THE ARTIFACTS TAB'S VIEWER ───────────────────────────────────────────────────────────────────
 *
 * Lists a runner's DELIVERABLES: `report` (HTML), `prd` and `architecture` (markdown), and `file`
 * (anything handed back).
 *   1. A row's identity includes its KIND, spelled via `artifactKindLabel`, never the raw slug (AC11.3).
 *   2. A row selects a RENDERER. `artifactKinds.ts::artifactRenderMode` owns the routing (a `file` is
 *      routed by inspection; the other kinds never are). THIS FILE OWNS THE FIVE RENDERERS, not the choice.
 *   3. This component FETCHES markdown/text bodies (the list rides the host's load), through `useApiRead`,
 *      and only under `artifactKinds.ts::TEXT_PREVIEW_MAX_BYTES` — a 100MB archive must never be
 *      pulled into memory as a string.
 *
 * THE LIST ARRIVES NEWEST FIRST (`created_at DESC, id DESC`): "newest" is `artifacts[0]`, never
 * `length - 1` — silent when wrong.
 */

/* ── THE CHOOSER IS A FEED LIST ABOVE THE VIEWER, AND THE 300px SIDE RAIL IS GONE ─────────────────
 * DO NOT PUT A RAIL BACK: the id gate is a VIEWPORT query, so inside a 300px track the filename is left little
 * even with the Kind and currency off (they follow the list's own width, `ARTIFACTS_NO_*`). The cost — truncated filenames — is
 * covered by the row `title` and the identity strip's unwrapped name.
 *
 * THE LIST IS CAPPED AND SCROLLS ON ITS OWN, with a draggable divider under it: uncapped, a run
 * that uploaded twenty reports pushed the viewer below the fold. The cap is a `maxHeight`, so a short list
 * never leaves a gap; the dragged height is a per-browser preference (`localStorage`), as the panes'. */
const LIST_HEIGHT_KEY = 'jaah.taskArtifacts.listHeight'
/** About eight desktop rows: the newest few stay in view and the document starts above the fold. */
const LIST_DEFAULT_MAX_PX = ROW_H * 8
/** Two rows: a drag can make the list small, never gone. */
const LIST_MIN_PX = ROW_H * 2

function loadListHeight(): number | null {
  const stored = readJson('local', LIST_HEIGHT_KEY)
  return typeof stored === 'number' && Number.isFinite(stored) && stored >= LIST_MIN_PX ? stored : null
}

function saveListHeight(height: number | null): void {
  writeJson('local', LIST_HEIGHT_KEY, height)
}

/* ── A LONG LIST LEADS WITH WHAT IS LIVE ──────────────────────────────────────────────
 * A run that re-uploads its report leaves one current row and many superseded ones of the same name.
 *   1. Superseded rows of a ranked kind hang, COLLAPSED, under that kind's current row ("16 earlier
 *      versions"). The group opens itself when the pick lands inside it, and stays open until closed.
 *   2. Every row in a chain of two or more carries its VERSION (`v17`, oldest = v1): same-named rows are
 *      told apart without hovering. A chain is the kind for ranked kinds (the supersession chain), the
 *      filename for `file` (each file is its own deliverable; only a re-upload under one name is a version).
 *   3. Rows below the cap's fold are SAID to exist: a fade and a "+N more" count, since an overlay
 *      scrollbar draws nothing at rest.
 *   4. ↑/↓ on a focused row moves the pick to the next visible row. */
function versionChain(artifact: Artifact): string {
  return isDownloadArtifactKind(artifact.kind) ? `file:${artifact.filename}` : artifact.kind
}

/** Each artifact's version number within its chain, for chains of two or more. The list is newest first. */
function artifactVersions(artifacts: Artifact[]): Map<number, number> {
  const sizes = new Map<string, number>()
  for (const artifact of artifacts) sizes.set(versionChain(artifact), (sizes.get(versionChain(artifact)) ?? 0) + 1)
  const seen = new Map<string, number>()
  const versions = new Map<number, number>()
  for (const artifact of artifacts) {
    const chain = versionChain(artifact)
    const size = sizes.get(chain) ?? 0
    const index = seen.get(chain) ?? 0
    seen.set(chain, index + 1)
    if (size > 1) versions.set(artifact.id, size - index)
  }
  return versions
}

/** The rows below the list's visible bottom (more than half hidden), measured from the DOM. */
function countRowsBelow(list: HTMLElement): number {
  const bottom = list.getBoundingClientRect().bottom
  let hidden = 0
  for (const row of list.querySelectorAll<HTMLElement>('[data-artifact]')) {
    const box = row.getBoundingClientRect()
    if (box.height > 0 && box.top + box.height / 2 > bottom) hidden++
  }
  return hidden
}

/* The version badge (`v2`), in the row and the viewer strip: set like the row's size · age metadata (11px mono),
   with a clear gap before the filename it qualifies. Dim, never faint: `text.secondary`, not the metadata's
   `text.disabled` — the number is how same-named rows differ, so it keeps readable contrast. */
const VERSION_SX = [monoSx, { color: 'text.secondary', fontSize: '0.6875rem', mr: 1 }]
/* The selected disc's 3px ring (`FeedList`'s `MARKED_NODE_SX`) ended 1px short of the name in this lead-less row:
   one half-step (3px) more puts the name ~7px off the disc, clear of the ring in both bands. */
const TITLE_INSET_SX = { ml: 0.5 } as const
const earlierLabel = (n: number) => `${n} earlier ${n === 1 ? 'version' : 'versions'}`

/* Fades the list's last 28px. A MASK, not a gradient over it: it fades into whatever ground the tab sits on. */
const LIST_FADE_SX = {
  maskImage: 'linear-gradient(to bottom, #000 calc(100% - 28px), transparent)',
  WebkitMaskImage: 'linear-gradient(to bottom, #000 calc(100% - 28px), transparent)',
} as const

const MORE_PILL_SX = {
  position: 'absolute',
  bottom: 2,
  left: '50%',
  transform: 'translateX(-50%)',
  px: 1.25,
  py: 0.25,
  border: '1px solid',
  borderColor: 'divider',
  borderRadius: 999,
  bgcolor: 'background.paper',
  color: 'text.secondary',
  fontFamily: 'inherit',
  fontSize: '0.75rem',
  lineHeight: 1.4,
  cursor: 'pointer',
  boxShadow: 1,
  '&:hover': { color: 'text.primary' },
} as const

/* The kind as a SHAPE on the row's disc (`ui/feed/types.ts`); no `badge-*` token exists for it. The
 * fallback covers a newer server's kind. */
const KIND_GLYPH: Record<string, string> = {
  report: '▤',
  prd: '◆',
  architecture: '△',
  file: '■',
}

/* The row's download slot, gated at `id` (not `compact`): the verb is also in the ⋯, and at phone width
   `theme.ts`'s 44px `MuiIconButton` floor would otherwise set the row height. */
const ROW_DOWNLOAD_SLOT = 30

/* Geometry both iframes share — ONLY geometry: `sandbox` is a security decision and stays per-branch.
   Shared so switching report ↔ PDF does not jump. */
const ARTIFACT_FRAME_SX = {
  width: '100%',
  // `display: block` kills the baseline gap; `minWidth: 0` keeps a wide document from scrolling the page.
  display: 'block',
  minWidth: 0,
  border: 0,
  borderRadius: 1,
  flex: '1 1 auto',
  // Floor it: a short laptop tab would squeeze the document unreadable; the panel scrolls instead.
  minHeight: 480,
} as const

export default function TaskArtifacts({ taskId, artifacts }: { taskId: number; artifacts: Artifact[] }) {
  // Its hrefs are rewritten on the live host (`apiHref`): re-render when the rewriter is installed.
  useApiRewriterVersion()
  /* The pick lives in the URL (`lib/taskArtifactLink.ts`), so a link, a reload and a shared address all
     show the same artifact. No param = "follow the newest", which stays the default as artifacts arrive; a
     pick wins over newer ones. An ID, not an index: the host's list grows and shrinks. */
  const [searchParams, setFilters] = useUrlFilters()
  const rawPick = searchParams.get(ARTIFACT_PARAM)
  const pickedId = parseArtifactParam(rawPick)
  /* `replace` (`hooks/useUrlFilters`), as `hooks/useUrlTab.ts`: picking is browsing within the tab, not a page
     Back should replay. Other keys (`tab`, …) are kept. */
  const setPickedId = useCallback(
    (id: number | null) => setFilters({ [ARTIFACT_PARAM]: id === null ? '' : String(id) }),
    [setFilters],
  )

  // Newest first: index 0.
  const picked = artifacts.find(a => a.id === pickedId)
  const selected = picked ?? artifacts[0]

  /* An unknown, no-longer-visible or malformed pick shows the newest (above) AND leaves the address, so the
     URL never names an artifact the viewer is not showing. Only once a list is in hand, and only a list FOR
     THE ROUTED TASK: on `/tasks/A → /tasks/B` the host renders once more with A's list under B's URL, and
     dropping there would throw away B's pick before B's artifacts arrive. */
  const { id: routedTaskId } = useParams<{ id?: string }>()
  const listIsForRoute = routedTaskId === undefined || routedTaskId === String(taskId)
  const stalePick = rawPick !== null && picked === undefined && artifacts.length > 0 && listIsForRoute
  useEffect(() => {
    if (stalePick) setPickedId(null)
  }, [stalePick, setPickedId])

  /* Retry token: re-selecting the same row bails out of the render, and a single artifact has no
     clickable row, so only a counter in the deps can re-run the fetch. */
  const [retryToken, setRetryToken] = useState(0)

  /* The image branch's runtime escape hatch: a `.png` that fails to decode demotes to `download` (the
     table's own fallback). Held here so the identity strip's suppression reads it too. The artifact's
     ID, not a boolean: a flag would need clearing in the effect whose deps read it, looping forever. */
  const [imageFailedId, setImageFailedId] = useState<number | null>(null)

  /* A frame cannot report its own load: a failed read is framed as the error page, which the API's default
     `frame-ancestors 'none'` blanks to a white box. So the frame branch PROBES the same URL and swaps in the
     markdown branch's banner on a refusal. Headers only: the body is aborted once the status is in,
     so a report's bytes cross the wire once (the frame's). The probe is a read on the audit trail too. */
  const [frameFailedId, setFrameFailedId] = useState<number | null>(null)

  /* `null` = the default cap (also what a reset stores). */
  const [listHeight, setListHeight] = usePersistedState(loadListHeight, saveListHeight)
  const listRef = useRef<HTMLDivElement>(null)
  const listMaxPx = listHeight ?? LIST_DEFAULT_MAX_PX
  /* The divider's pointer (or arrow step) at `position` asks for a list that ends there; clamped to the
     floor and to the list's own content, so the strip never drifts below the last row. */
  const onDividerMove = useCallback((position: number, grabOffset: number) => {
    const list = listRef.current
    if (!list) return
    const desired = position - grabOffset - list.getBoundingClientRect().top
    const content = list.scrollHeight
    // A list already shorter than its cap ends at its last row: growing it is a no-op, never a shrink.
    if (desired >= content && content <= listMaxPx) return
    setListHeight(Math.round(Math.min(Math.max(desired, LIST_MIN_PX), Math.max(content, LIST_MIN_PX))))
  }, [listMaxPx, setListHeight])

  const selectedId = selected?.id ?? null

  /* Each ranked kind's live row, and the superseded rows that hang under it. A superseded row whose kind has
     no live row (never expected) stays a root rather than vanishing. */
  const versions = useMemo(() => artifactVersions(artifacts), [artifacts])
  const liveByKind = useMemo(() => {
    const live = new Map<string, Artifact>()
    for (const artifact of artifacts) {
      if (!isDownloadArtifactKind(artifact.kind) && artifact.current && !live.has(artifact.kind)) live.set(artifact.kind, artifact)
    }
    return live
  }, [artifacts])
  const isGroupedUnderLive = (artifact: Artifact) =>
    !isDownloadArtifactKind(artifact.kind) && !artifact.current && liveByKind.has(artifact.kind)

  /* Per kind: absent = shut. Every pick that lands inside a group opens it (latched, so a step out never
     folds it away under the keyboard); only the operator's own press closes it. */
  const [groupOpen, setGroupOpen] = useState<Record<string, boolean>>({})
  const selectedGroup = selected && isGroupedUnderLive(selected) ? selected.kind : null
  useEffect(() => {
    if (selectedGroup !== null) setGroupOpen(prev => (prev[selectedGroup] ? prev : { ...prev, [selectedGroup]: true }))
  }, [selectedId, selectedGroup])
  const isGroupOpen = (kind: string) => groupOpen[kind] ?? kind === selectedGroup

  /* Rows below the fold, re-measured whenever the list's content or cap can have changed. */
  const [hiddenBelow, setHiddenBelow] = useState(0)
  const measureHidden = useCallback(() => {
    if (listRef.current) setHiddenBelow(countRowsBelow(listRef.current))
  }, [])
  const openKinds = Object.keys(groupOpen).filter(kind => groupOpen[kind]).join(',')
  useLayoutEffect(() => measureHidden(), [measureHidden, artifacts, openKinds, selectedGroup, listMaxPx])
  useEffect(() => {
    const list = listRef.current
    if (!list || typeof ResizeObserver === 'undefined') return
    const observer = new ResizeObserver(measureHidden)
    observer.observe(list)
    return () => observer.disconnect()
  }, [measureHidden])

  /* ↑/↓ from a focused row: pick the neighbouring VISIBLE row (DOM order is display order) and follow it with
     focus. Only from the row itself — a key inside its ⋯ menu bubbles here through the portal. */
  const onListKeyDown = useCallback((event: KeyboardEvent<HTMLElement>) => {
    if (event.key !== 'ArrowDown' && event.key !== 'ArrowUp') return
    const list = listRef.current
    const from = event.target as HTMLElement
    if (!list || from.dataset.artifact === undefined) return
    const rows = Array.from(list.querySelectorAll<HTMLElement>('[data-artifact]'))
    // Held at the ends too: the key belongs to the list, not the scroll box.
    event.preventDefault()
    const next = rows[rows.indexOf(from) + (event.key === 'ArrowDown' ? 1 : -1)]
    if (!next) return
    setPickedId(Number(next.dataset.artifact))
    next.focus()
    next.scrollIntoView?.({ block: 'nearest' })
  }, [setPickedId])

  /* The frame's address, fixed per artifact (`apiHref`, rewritten on the live host): a re-render must not reload a
     frame the visitor is reading. */
  const frameSrc = useMemo(
    () => (selectedId === null ? '' : artifactContentUrl(taskId, selectedId, false)),
    [taskId, selectedId],
  )
  /* ONE VALUE DRIVES EVERY BRANCH — viewer, strip suppression and fetch allow-list — so no third condition
     can disagree with the other two. */
  const routedMode: ArtifactRenderMode = selected ? artifactRenderMode(selected) : 'download'
  const mode: ArtifactRenderMode =
    routedMode === 'image' && imageFailedId === selectedId ? 'download' : routedMode
  // The two byte-pulling branches, as a positive set (see the effect).
  const needsBody = mode === 'markdown' || mode === 'text'

  /* The body, keyed on the artifact (and the Retry token): a slow PRD body must not paint into the Architecture
     view. `enabled` is an ALLOW-LIST: a fetch in any other branch would decode e.g. a 100MB `file` into a
     string held in memory. A new mode must arrive silent. The size ceiling lives upstream in
     `artifactRenderMode`. */
  const body = useApiRead(
    ['task-artifact-body', taskId, selectedId, retryToken],
    () => getText(artifactContentUrl(taskId, selectedId as number, false)),
    'Failed to load the artifact',
    { enabled: selectedId !== null && needsBody },
  )
  /* Three states, not two: a failed read must never paint an EMPTY DOCUMENT ("the agent wrote nothing").
     `null` = no body in hand — while the next artifact's body loads too, so the previous one never shows under it. */
  const markdown = body.loading || body.error ? null : body.data
  const loadFailed = body.error !== ''
  const { loading } = body

  const isFrame = mode === 'frame'
  useEffect(() => {
    if (selectedId === null || !isFrame) return undefined
    setFrameFailedId(null)
    const controller = new AbortController()
    void (async () => {
      try {
        const resp = await apiFetch(artifactContentUrl(taskId, selectedId, false), { credentials: 'same-origin', signal: controller.signal })
        if (!resp.ok) {
          setFrameFailedId(selectedId)
          await noticeRefusal(resp)
        }
      } catch {
        if (!controller.signal.aborted) setFrameFailedId(selectedId)
      } finally {
        controller.abort()
      }
    })()
    return () => controller.abort()
  }, [taskId, selectedId, isFrame, retryToken])

  /* Unreachable under the host's `hasArtifacts` guard; handled so no host frames `…/undefined/content`. */
  if (!selected) return null

  // One artifact is listed but not pressable (see `onRowClick`).
  const selectable = artifacts.length > 1

  /* The kind is the disc's SHAPE, the currency its TONE; both also in words. */
  const earlierByKind = new Map<string, Artifact[]>()
  for (const artifact of artifacts) {
    if (isGroupedUnderLive(artifact)) earlierByKind.set(artifact.kind, [...(earlierByKind.get(artifact.kind) ?? []), artifact])
  }

  const toRow = (artifact: Artifact, depth: number): FeedRowSpec => {
    /* Supersession is false for `file`: `current` is latest-wins per kind
       (`app/task_artifacts.py::current_artifact`), but each file is its own deliverable. */
    const ranked = !isDownloadArtifactKind(artifact.kind)
    const superseded = ranked && !artifact.current
    const kindLabel = artifactKindLabel(artifact.kind)
    const chosen = artifact.id === selected.id
    const version = versions.get(artifact.id)
    const earlier = depth === 0 && ranked && artifact.current ? (earlierByKind.get(artifact.kind) ?? []) : []
    const open = isGroupOpen(artifact.kind)
    /* Size and a COMPACT age in one slot: at 11px mono (~6.6px/char) the widest is 16 chars
       (`1023.9 KB · 364d`); the verbose age fit at no realistic size. */
    const meta = `${formatBytes(artifact.size_bytes)} · ${compactAge(artifact.created_at)}`
    return {
      key: String(artifact.id),
      /* `data-` handle (the testid is the feed's `feed-row`); `selected` beside it since the row contract
         has no `aria-current`. */
      data: { artifact: artifact.id, selected: chosen },
      indicator: {
        // Superseded = neutral grey, live = info. Never success/error: an artifact is not an outcome.
        tone: superseded ? 'neutral' : 'info',
        glyph: KIND_GLYPH[artifact.kind] ?? '●',
        label: `${kindLabel}${ranked ? (artifact.current ? ' — current' : ' — superseded') : ''}`,
      },
      /* The filename is the row (`prd-v2.md` vs `prd-v2-final.md`); full value on hover. The version leads,
         so a narrow row ellipsises the name, never the number. */
      title: (
        <Box component="span" sx={TITLE_INSET_SX}>
          {version !== undefined && <Box component="span" sx={VERSION_SX}>v{version}</Box>}
          <span>{artifact.filename}</span>
        </Box>
      ),
      titleText: artifact.filename,
      /* The earlier versions fold under the live row: the count is the row's hover note, the toggle a ⋯ item,
        and opened they are its children. */
      ...(earlier.length > 0
        ? {
            notes: [{ key: 'earlier', tone: 'muted' as const, text: earlierLabel(earlier.length) }],
            children: open ? earlier.map(child => toRow(child, 1)) : undefined,
          }
        : {}),
      /* One-sentence name (makes the disc decorative) carrying "shown in the viewer" and the exact instant. */
      ariaLabel: [
        kindLabel,
        artifact.filename,
        version !== undefined ? `version ${version}` : null,
        earlier.length > 0 ? earlierLabel(earlier.length) : null,
        formatBytes(artifact.size_bytes),
        absoluteTime(artifact.created_at),
        ranked ? (artifact.current ? 'current' : 'superseded') : null,
        chosen ? 'shown in the viewer' : null,
      ].filter(Boolean).join(' — '),
      /* Pressing selects — a view, never a write. One artifact is listed but not pressable. */
      onRowClick: selectable ? () => setPickedId(artifact.id) : undefined,
      rowClickLabel: `Show ${artifact.filename}`,
      /* `current`, not `deepLinked`: `current` spells `aria-current` for "the row the surface shows"
         (`ui/feed/types.ts`). */
      current: chosen,
      slots: [
        {
          key: 'kind',
          width: 124,
          gate: 'artifactKind',
          align: 'right',
          dim: true,
          testId: `task-artifact-kind-${artifact.id}`,
          // Human-readable kind, never `artifact.kind`.
          node: kindLabel,
        },
        {
          key: 'currency',
          width: 78,
          gate: 'artifactCurrency',
          align: 'right',
          dim: true,
          /* Marked additively; superseded rows are NOT dimmed (operators open them deliberately). */
          node: !ranked ? null : artifact.current
            ? <StatusChip label="Current" tone="info" />
            : 'superseded',
        },
        {
          key: 'meta',
          /* 106px = the widest composed string, 16 chars at ~6.6px. */
          width: 106,
          align: 'right',
          mono: true,
          dim: true,
          /* `title` keeps the absolute timestamp, so compacting never loses it (`lib/relativeTime`). */
          title: absoluteTime(artifact.created_at),
          node: meta,
        },
        {
          key: 'download',
          width: ROW_DOWNLOAD_SLOT,
          gate: 'id',
          align: 'center',
          node: <ArtifactRowDownload taskId={taskId} artifact={artifact} />,
        },
      ],
      actions: [
        {
          /* The glyph's second home (lets the slot gate off phones). `download` attribute not needed: the
             route sends `Content-Disposition: attachment` (`app/routes/attachments.py::artifact_content`). */
          key: 'download',
          icon: '⤓',
          label: 'Download',
          // A superset of the visible "Download" (WCAG 2.5.3), naming the file.
          ariaLabel: `Download ${artifact.filename}`,
          href: artifactContentUrl(taskId, artifact.id, true),
        },
        ...(earlier.length > 0
          ? [{
              key: 'earlier',
              icon: open ? '▾' : '▸',
              label: open ? 'Hide earlier versions' : 'Show earlier versions',
              ariaLabel: `${open ? 'Hide' : 'Show'} ${earlierLabel(earlier.length)} of ${artifact.filename}`,
              testId: `task-artifact-earlier-${artifact.kind}`,
              onClick: () => setGroupOpen(prev => ({ ...prev, [artifact.kind]: !open })),
            }]
          : []),
      ],
    }
  }
  const artifactRows = artifacts.filter(artifact => !isGroupedUnderLive(artifact)).map(artifact => toRow(artifact, 0))

  return (
    <Box
      sx={{
        display: 'flex',
        flexDirection: 'column',
        gap: 1,
        flex: '1 1 auto',
        minHeight: 0,
        minWidth: 0,
      }}
    >
      {/* No gap inside: the strip's top IS the list's bottom, which the drag arithmetic relies on. */}
      <Box sx={{ display: 'flex', flexDirection: 'column', flex: '0 0 auto', minWidth: 0 }}>
        <Box sx={{ position: 'relative', minWidth: 0 }}>
          <Box
            ref={listRef}
            data-testid="task-artifacts-list-scroll"
            onScroll={measureHidden}
            onKeyDown={onListKeyDown}
            /* `scrollPaddingBottom`: a row ↓ scrolls into view stops above the fade and the pill, not under them. */
            sx={[{ maxHeight: listMaxPx, overflowY: 'auto', minWidth: 0, scrollPaddingBottom: 28 }, ARTIFACTS_LIST_CONTAINER_SX, hiddenBelow > 0 ? LIST_FADE_SX : null]}
          >
            <FeedList ariaLabel="Artifacts" testId="task-artifacts-list" sections={[{ key: 'artifacts', rows: artifactRows }]} />
          </Box>
          {/* Outside the faded box, so it is drawn at full strength. Pages the list down. */}
          {hiddenBelow > 0 && (
            <Box
              component="button"
              type="button"
              data-testid="task-artifacts-more"
              aria-label={`Scroll to ${hiddenBelow} more ${hiddenBelow === 1 ? 'artifact' : 'artifacts'}`}
              onClick={() => listRef.current?.scrollBy({ top: listRef.current.clientHeight - ROW_H, behavior: 'smooth' })}
              sx={MORE_PILL_SX}
            >
              +{hiddenBelow} more
            </Box>
          )}
        </Box>
        {/* One artifact is a one-row list: nothing to resize. */}
        {selectable && (
          <SplitDivider
            orientation="horizontal"
            testId="task-artifacts-divider"
            ariaLabel="Resize artifact list"
            /* Read at render, so it can lag a list that grew since; announced only, never acted on. */
            valueNow={Math.min(100, (listMaxPx / Math.max(listRef.current?.scrollHeight ?? listMaxPx, 1)) * 100)}
            edge={() => listRef.current?.getBoundingClientRect().bottom ?? 0}
            onMove={onDividerMove}
            onReset={() => setListHeight(null)}
          />
        )}
      </Box>

      <Box sx={{ display: 'flex', flexDirection: 'column', gap: 1, flex: '1 1 auto', minWidth: 0, minHeight: 0 }}>
        {/* The selected artifact named outside its body (the list scrolls away), and the only unwrapped
            full filename. */}
        {/* Not drawn over the download card, which already states these facts. Keyed on what is ON
            SCREEN (the card), not the kind: a previewable `file` still needs this strip. */}
        {mode !== 'download' && (
          /* On a phone one line: the name wraps within itself and Download is an icon beside it. */
          <Box sx={{ display: 'flex', flexWrap: 'wrap', alignItems: 'baseline', gap: 1, minWidth: 0, [PHONE]: { flexWrap: 'nowrap' } }}>
            <Typography variant="caption" sx={{ color: 'text.secondary', letterSpacing: '0.04em', textTransform: 'uppercase', flexShrink: 0 }}>
              {artifactKindLabel(selected.kind)}
            </Typography>
            {/* Never truncated: the differing tail (`prd-v2.md` vs `prd-v2-final.md`) is the identity. */}
            <Typography variant="body2" sx={{ fontWeight: 600, minWidth: 0, overflowWrap: 'anywhere' }}>
              {selected.filename}
            </Typography>
            {/* Which of the same-named versions is on screen, as its row spells it. */}
            {versions.has(selected.id) && (
              <Typography variant="caption" data-testid="task-artifact-version" sx={[...VERSION_SX, { mr: 0, flexShrink: 0 }]}>
                v{versions.get(selected.id)}
              </Typography>
            )}
            <Typography variant="caption" sx={{ color: 'text.secondary', flexShrink: 0 }}>
              {formatBytes(selected.size_bytes)}
            </Typography>
            {/* Where the bytes are stored. Off the phone's one line, where it would squeeze the name: the
                line below carries it there. */}
            {selected.region_label && (
              <Typography variant="caption" data-testid="task-artifact-region" sx={{ color: 'text.secondary', flexShrink: 0, [PHONE]: { display: 'none' } }}>
                Region: {selected.region_label}
              </Typography>
            )}
            {/* Download for rendered kinds: a sandboxed report frame blocks the browser's Save/Print.
                On the strip (which names the subject), absent beside the download card. */}
            <ArtifactDownloadButton taskId={taskId} artifact={selected} />
          </Box>
        )}
        {mode !== 'download' && selected.region_label && (
          <Typography variant="caption" sx={{ color: 'text.secondary', [ABOVE_PHONE]: { display: 'none' } }}>
            Region: {selected.region_label}
          </Typography>
        )}

        {mode === 'download' ? (
          <FileDownloadCard taskId={taskId} artifact={selected} />
        ) : mode === 'markdown' ? (
          <MarkdownView
            markdown={markdown}
            loading={loading}
            failed={loadFailed}
            onRetry={() => setRetryToken(t => t + 1)}
          />
        ) : mode === 'text' ? (
          /* Same bytes and states as markdown, but NEVER through `RichTextRenderer` (a `#` log line would
             become a heading). Monospace, preserved whitespace, no parse. */
          <PlainTextView
            text={markdown}
            loading={loading}
            failed={loadFailed}
            onRetry={() => setRetryToken(t => t + 1)}
          />
        ) : mode === 'image' ? (
          <ArtifactImageView
            taskId={taskId}
            artifact={selected}
            onDecodeFailure={() => setImageFailedId(selected.id)}
          />
        ) : mode === 'pdf' ? (
          /* ── THE ONE FRAME WITHOUT `sandbox`, AND THE MEASUREMENT THAT PUT IT HERE ───────────────
              Measured (2026-08-28): plain iframe → renders; `sandbox=""` → grey box, even with NO CSP and
              with `allow-scripts`. Chrome's PDF viewer will not start in a SANDBOXED BROWSING CONTEXT; no
              server change helps, and weakening the route's CSP would be for nothing. The response CSP
              does NOT sandbox the rendered PDF either (line 1 would fail otherwise): no sandbox ships.
              Containment is the format: `application/pdf` + `nosniff`
              (`app/routes/attachments.py::artifact_content`) means the bytes are never HTML, and PDFium's
              JS subset has no DOM, `window` or cookies. Accepted residual: a link annotation can navigate
              or pop up on one click; a form annotation can beacon. Phishing/telemetry, never the session.
              SPECIFIC TO A FORMAT THE HTML ENGINE NEVER PARSES — never reuse it for another
              `INLINE_FILE_CONTENT_TYPES` member; the `.html` branch keeps its `sandbox`. */
          <Box
            component="iframe"
            key={`artifact-pdf-${selected.id}`}
            data-testid="task-artifact-pdf"
            src={frameSrc}
            title={`${artifactKindLabel(selected.kind)}: ${selected.filename}`}
            sx={{ ...ARTIFACT_FRAME_SX, backgroundColor: 'action.hover' }}
          />
        ) : frameFailedId === selected.id ? (
          <FetchFailedBanner onRetry={() => setRetryToken(t => t + 1)} />
        ) : (
          /* ── The sandbox is the feature's security core, not a hardening nicety ──────────────────
              `sandbox="allow-scripts allow-forms"` and NOTHING else — above all never `allow-same-origin`:
              agent-authored HTML served from the console's own origin to an admin session would otherwise be
              stored XSS. Scripts run so a mockup's click handlers work, on an opaque origin with
              no cookie, storage or API; `allow-forms` so a wizard's `<form>` reaches its `submit` handler
              while the CSP's `form-action 'none'` posts nothing; no popups or top navigation.
              The route's `sandbox allow-scripts allow-forms` CSP
              (plus `default-src 'none'`, so no subresource fetch or beacon) is the second layer.
              Markdown kinds never become markup (`RichTextRenderer`, fixed TipTap schema, `html: false`; no
              raw-HTML path in this file) but lack the frame's `default-src 'none'`: an agent-written remote
              image would load and beacon. Known follow-up (a SPA CSP or an `AttachmentImage` opt-out);
              unreachable until `prd`/`architecture` uploads exist. The banned APIs are named only in
              `TaskArtifacts.test.tsx`, which greps comment-stripped source.
              `key` = artifact id so switching REPLACES the frame (a `src` swap keeps scroll and history). */
          <Box
            component="iframe"
            // The retry token too, so Retry reloads the frame along with its probe.
            key={`artifact-${selected.id}-${retryToken}`}
            data-testid="task-artifact-frame"
            src={frameSrc}
            sandbox="allow-scripts allow-forms"
            // The accessible name distinguishes frames.
            title={`${artifactKindLabel(selected.kind)}: ${selected.filename}`}
            sx={{
              ...ARTIFACT_FRAME_SX,
              // Explicit paper: a report may declare no background, and transparent on the dark surface
              // is unreadable. (PDF and markdown branches need none.)
              backgroundColor: '#ffffff',
            }}
          />
        )}
      </Box>
    </Box>
  )
}

/** The two states every fetched body owes (failure, loading), or `null` when the caller renders the
 *  body. A helper, not a wrapper: order matters, and a blank document over a failed read would claim
 *  "the agent wrote nothing". */
function fetchedBodyState({
  body,
  loading,
  failed,
  onRetry,
}: {
  body: string | null
  loading: boolean
  failed: boolean
  onRetry: () => void
}): ReactNode | null {
  if (failed) return <FetchFailedBanner onRetry={onRetry} />
  if (loading || body === null) {
    // A spinner, not a skeleton of a document we may never get. No `aria-live`: the operator clicked.
    return (
      <Box sx={{ display: 'flex', alignItems: 'center', justifyContent: 'center', py: 4, minHeight: 120 }}>
        <CircularProgress size={24} aria-label="Loading artifact" />
      </Box>
    )
  }
  return null
}

/** A read that failed, for every branch that reads the bytes (the fetched bodies, the report frame's probe). */
function FetchFailedBanner({ onRetry }: { onRetry: () => void }) {
  return (
    /* A real Retry: re-clicking the selected row is a no-op (see `retryToken`). */
    <Banner severity="error" testId="task-artifact-error">
      <Box sx={{ display: 'flex', alignItems: 'center', flexWrap: 'wrap', gap: 1 }}>
        <span>This artifact could not be loaded. Its content is still stored on the task.</span>
        <Button size="small" variant="outlined" color="inherit" onClick={onRetry} sx={tapTargetSx}>
          Retry
        </Button>
      </Box>
    </Banner>
  )
}

/* The fetched bodies' surface: console DOM, theme-aware, grows with content. `overflowX: 'auto'` keeps a
   wide code fence or table scrolling inside the box, not `.app-main`. */
const BODY_SURFACE_SX = {
  flex: '1 1 auto',
  minWidth: 0,
  overflowX: 'auto',
  border: '1px solid',
  borderColor: 'divider',
  borderRadius: 1,
  px: 2,
  py: 1.5,
  [PHONE]: { px: 1.25 },
} as const

/* The markdown viewer, for markdown kinds and a free-form `.md` alike — one set of typographic decisions. */
function MarkdownView({
  markdown,
  loading,
  failed,
  onRetry,
}: {
  markdown: string | null
  loading: boolean
  failed: boolean
  onRetry: () => void
}) {
  const pending = fetchedBodyState({ body: markdown, loading, failed, onRetry })
  if (pending) return pending
  // Narrowed by `fetchedBodyState`, which TypeScript cannot see through.
  const body = markdown as string
  return (
    <Box
      data-testid="task-artifact-markdown"
      sx={{
        ...BODY_SURFACE_SX,
        // An 80ch measure on the PROSE, not the box, so the border still frames the panel.
        '& .rte-content': { maxWidth: '80ch', overflowWrap: 'anywhere' },
      }}
    >
      {body.trim() === '' ? (
        /* Known-empty, distinct from a failed read. */
        <Typography variant="body2" sx={{ color: 'text.secondary', fontStyle: 'italic' }}>
          This artifact is empty.
        </Typography>
      ) : (
        <RichTextRenderer content={body} />
      )}
    </Box>
  )
}

/* ── THE PLAIN-TEXT VIEWER — a `.log`, a `.json`, a `.csv`, a `.txt` ──────────────────────────────
 * Byte-for-byte what the runner wrote: `pre-wrap` preserves whitespace yet wraps (`pre` would scroll every
 * long line), `anywhere` handles unbroken tokens. Scrolls in its own box, capped (the frame's 480 floor
 * read as a max) so a 2MB log cannot own the page. The bytes are React children, never markup: a
 * `<script>` renders as its characters. */
function PlainTextView({
  text,
  loading,
  failed,
  onRetry,
}: {
  text: string | null
  loading: boolean
  failed: boolean
  onRetry: () => void
}) {
  const pending = fetchedBodyState({ body: text, loading, failed, onRetry })
  if (pending) return pending
  const body = text as string
  return (
    <Box
      data-testid="task-artifact-text"
      sx={{
        ...BODY_SURFACE_SX,
        maxHeight: 640,
        overflowY: 'auto',
        // Respelled: this key replaces the surface's phone inset rather than merging.
        [PHONE]: { px: 1.25, maxHeight: 440 },
      }}
    >
      {body.trim() === '' ? (
        /* Known-empty, as in the markdown branch. */
        <Typography variant="body2" sx={{ color: 'text.secondary', fontStyle: 'italic' }}>
          This artifact is empty.
        </Typography>
      ) : (
        // `text.primary`: this is the document, and a dimmed log is unreadable.
        <CodeBlock tone="plain" size="medium" sx={{ color: 'text.primary' }}>{body}</CodeBlock>
      )}
    </Box>
  )
}

/* ── THE IMAGE VIEWER — a `.png`, `.jpg`, `.gif`, `.webp` or `.svg` a persona attached ────────────
 * NEVER UPSCALED: sizing is a ceiling in both axes, never a floor (a 320px screenshot must not blur).
 * On an `action.hover` backdrop so transparent PNGs stay visible and have an edge, in either theme.
 * `alt` is the filename — the only fact available. */
function ArtifactImageView({
  taskId,
  artifact,
  onDecodeFailure,
}: {
  taskId: number
  artifact: Artifact
  onDecodeFailure: () => void
}) {
  return (
    <Box
      data-testid="task-artifact-image"
      sx={{
        /* Hugs the image rather than filling the panel (a mostly-empty box reads as a failed load):
           `alignSelf: 'flex-start'` for width, no `flex-grow` for height. */
        alignSelf: 'flex-start',
        maxWidth: '100%',
        minWidth: 0,
        display: 'flex',
        alignItems: 'flex-start',
        justifyContent: 'center',
        overflow: 'auto',
        border: '1px solid',
        borderColor: 'divider',
        borderRadius: 1,
        backgroundColor: 'action.hover',
        p: 2,
        [PHONE]: { p: 1 },
      }}
    >
      <Box
        component="img"
        // `key` so switching REPLACES the element (else the previous image shows under the new name).
        key={`artifact-image-${artifact.id}`}
        src={artifactContentUrl(taskId, artifact.id, false)}
        alt={artifact.filename}
        /* The routing guessed "image" from the NAME; a failed decode hands the row to the download card. */
        onError={onDecodeFailure}
        sx={{
          display: 'block',
          // A ceiling in both axes; `height: 'auto'` keeps the aspect ratio.
          maxWidth: '100%',
          maxHeight: 640,
          height: 'auto',
          [PHONE]: { maxHeight: 420 },
          borderRadius: 0.5,
        }}
      />
    </Box>
  )
}

/* ── THE FALLBACK CARD — the branch that renders no bytes at all, and is reached LAST ─────────────
 * For formats with no viewer (a spreadsheet, an archive). A frame is not a fallback: an unrenderable
 * frame paints an empty box. The card carries, once: name, kind, size, when, and one action. Not a
 * warning tone (nothing failed); same border vocabulary as `MarkdownView`. Capped at 480px and hugging
 * its content, or it reads as a banner or a failed load.
 */
const DOWNLOAD_CARD_SX = {
  width: '100%',
  maxWidth: 480,
  minWidth: 0,
  border: '1px solid',
  borderColor: 'divider',
  borderRadius: 1,
  px: 2,
  py: 1.75,
  [PHONE]: { px: 1.25 },
} as const

/* The type cue is the filename's EXTENSION, not an icon set (no icon package under `src`; see
   `TaskToolbar`) — true for every format. Theme tokens for ground and ring. */
const EXTENSION_PLATE_SX = {
  flexShrink: 0,
  width: 44,
  height: 44,
  display: 'flex',
  alignItems: 'center',
  justifyContent: 'center',
  border: '1px solid',
  borderColor: 'divider',
  borderRadius: 1,
  backgroundColor: 'action.hover',
  color: 'text.primary',
  fontSize: '0.625rem',
  fontWeight: 700,
  letterSpacing: '0.04em',
  overflow: 'hidden',
} as const

/* A small table of formats runs actually hand back; everything else prints the raw type (ugly and
 * correct). Not in `artifactKinds.ts`, whose test pins it as a mirror of a server map; this is
 * presentation for this card alone. */
const CONTENT_TYPE_NAMES: Readonly<Record<string, string>> = {
  'application/pdf': 'PDF document',
  'application/zip': 'ZIP archive',
  'application/x-tar': 'TAR archive',
  'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet': 'Excel spreadsheet',
  'application/vnd.ms-excel': 'Excel spreadsheet',
  'application/vnd.openxmlformats-officedocument.wordprocessingml.document': 'Word document',
  'application/msword': 'Word document',
  'application/vnd.openxmlformats-officedocument.presentationml.presentation': 'PowerPoint deck',
  'text/csv': 'CSV spreadsheet',
  'application/json': 'JSON data',
}

/** The file's type as a human reads it. Missing and `application/octet-stream` are the same don't-know
 *  (the server falls back to octet-stream), so both read "File". */
function contentTypeName(contentType: string | null): string {
  // Strip parameters (`text/csv; charset=utf-8`).
  const essence = (contentType ?? '').split(';')[0].trim().toLowerCase()
  if (!essence || essence === 'application/octet-stream') return 'File'
  const known = CONTENT_TYPE_NAMES[essence]
  if (known) return known
  // Top-level families where that alone says something true.
  const [family] = essence.split('/')
  if (family === 'image') return 'Image'
  if (family === 'video') return 'Video'
  if (family === 'audio') return 'Audio'
  if (family === 'text') return 'Text file'
  return essence
}

/** The plate's word: short alphanumeric extensions only, else "FILE" (a truncated "SQLI" would be wrong). */
function fileExtensionTag(filename: string): string {
  const dot = filename.lastIndexOf('.')
  if (dot <= 0) return 'FILE'
  const ext = filename.slice(dot + 1)
  return /^[a-z0-9]{1,5}$/i.test(ext) ? ext.toUpperCase() : 'FILE'
}

/** The URL for an artifact's bytes — the one place `?download=1` is spelled (on the live host, its viewer URL:
 *  `apiHref`). The flag only flips the
 *  disposition header (`app/routes/attachments.py::artifact_content`), same row, same guard. Every
 *  download control passes `true`: the route answers `inline` for renderable free-form types. */
function artifactContentUrl(taskId: number, artifactId: number, download: boolean): string {
  return apiHref(`/api/tasks/${taskId}/artifacts/${artifactId}/content${download ? '?download=1' : ''}`)
}

/** The identity strip's Download — the only route to the bytes for kinds rendered in place. `?download=1`
 *  makes the server send `attachment` (the attribute alone would not, and the flagless URL is the frame's
 *  inline one). `outlined`, since it must not compete with the document below. */
function ArtifactDownloadButton({ taskId, artifact }: { taskId: number; artifact: Artifact }) {
  return (
    <Button
      component="a"
      href={artifactContentUrl(taskId, artifact.id, true)}
      download={artifact.filename}
      // `noopener`: the house default on hand-built links.
      rel="noopener"
      /* Shares `FileDownloadCard`'s testid, safe only because this strip is suppressed beside the card; lift
         that and they need separate handles. */
      data-testid="task-artifact-download"
      variant="outlined"
      size="small"
      startIcon={<DownloadGlyph />}
      /* "Download <filename>" is a superset of the visible word (WCAG 2.5.3). */
      aria-label={`Download ${artifact.filename}`}
      /* `ml: 'auto'` pushes it trailing; `alignSelf: 'center'` opts out of the strip's baseline alignment. On a
         phone it is the glyph alone (the name stays its `aria-label`), so it fits beside the filename. */
      sx={[
        { ml: 'auto', alignSelf: 'center', flexShrink: 0 },
        tapTargetSx,
        { [PHONE]: { minWidth: 44, px: 0, '& .MuiButton-startIcon': { m: 0 } } },
      ]}
    >
      <Box component="span" sx={{ [PHONE]: { display: 'none' } }}>Download</Box>
    </Button>
  )
}

/** The per-row download glyph, beside the strip's button: collects several deliverables without selecting
 *  each. `?download=1` is mandatory here — a `report` or `.html` would navigate the console away. Its own
 *  testid per row. No `stopPropagation` (`FeedList` ignores presses inside controls by selector) and no
 *  tap floor (`gate: 'id'` keeps it off phones). */
function ArtifactRowDownload({ taskId, artifact }: { taskId: number; artifact: Artifact }) {
  return (
    <IconButton
      component="a"
      href={artifactContentUrl(taskId, artifact.id, true)}
      download={artifact.filename}
      // `noopener`: the house default on hand-built links.
      rel="noopener"
      data-testid={`task-artifact-row-download-${artifact.id}`}
      /* Icon-only, so the filename is its only name. */
      aria-label={`Download ${artifact.filename}`}
      size="small"
      sx={{
        // Recedes at rest (never below `text.secondary`), full strength on hover and focus.
        color: 'text.secondary',
        p: 0,
        width: 22,
        height: 22,
        '&:hover': { color: 'text.primary', backgroundColor: 'action.hover' },
        '&:focus-visible': { color: 'text.primary' },
      }}
    >
      <DownloadGlyph />
    </IconButton>
  )
}

function FileDownloadCard({ taskId, artifact }: { taskId: number; artifact: Artifact }) {
  /* Same helper URL (same-origin, so the cookie rides it). `download` and the route's
     `Content-Disposition: attachment` agree on the name. NOT THE SECURITY BOUNDARY: an `evil.html` stays
     inert through two server-side headers (`app/task_manifest.py::FREEFORM_KINDS`); `download` governs
     only this click. A link, not a `fetch` + Blob (as `pages/audit/SessionsTab.tsx` argues): the browser
     streams to disk natively. */
  const contentUrl = artifactContentUrl(taskId, artifact.id, true)
  return (
    <Box data-testid="task-artifact-download-card" sx={DOWNLOAD_CARD_SX}>
      <Box sx={{ display: 'flex', alignItems: 'flex-start', gap: 1.5, minWidth: 0 }}>
        <Box aria-hidden="true" sx={[EXTENSION_PLATE_SX, monoSx]}>
          {fileExtensionTag(artifact.filename)}
        </Box>
        <Box sx={{ minWidth: 0 }}>
          {/* Never ellipsised here (the list row truncates): the differing tail is the identity. */}
          <Typography
            variant="subtitle2"
            component="div"
            sx={{ fontWeight: 700, lineHeight: 1.35, minWidth: 0, overflowWrap: 'anywhere' }}
          >
            {artifact.filename}
          </Typography>
          {/* What · how big · when; `title` keeps the absolute timestamp (`lib/relativeTime`). */}
          <Typography
            variant="caption"
            component="div"
            sx={{ color: 'text.secondary', mt: 0.25 }}
            title={absoluteTime(artifact.created_at)}
          >
            {contentTypeName(artifact.content_type)} · {formatBytes(artifact.size_bytes)} ·{' '}
            {relativeAge(artifact.created_at)}
          </Typography>
          {artifact.region_label && (
            <Typography variant="caption" component="div" data-testid="task-artifact-region" sx={{ color: 'text.secondary' }}>
              Region: {artifact.region_label}
            </Typography>
          )}
        </Box>
      </Box>

      {/* One sentence, no apology (nothing failed), about THIS FORMAT — some files are previewed now. */}
      <Typography variant="body2" sx={{ color: 'text.secondary', mt: 1.5 }}>
        No preview — download to open.
      </Typography>

      <Button
        component="a"
        href={contentUrl}
        download={artifact.filename}
        // `noopener`: the house default on hand-built links.
        rel="noopener"
        data-testid="task-artifact-download"
        variant="contained"
        startIcon={<DownloadGlyph />}
        /* "Download <filename>" is a superset of the visible word (WCAG 2.5.3). */
        aria-label={`Download ${artifact.filename}`}
        sx={[{ mt: 2 }, tapTargetSx]}
      >
        Download
      </Button>
    </Box>
  )
}
