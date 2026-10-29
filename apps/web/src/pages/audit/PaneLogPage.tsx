import { useEffect, useLayoutEffect, useMemo, useRef, useState, type JSX } from 'react'
import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import MuiLink from '@mui/material/Link'
import Typography from '@mui/material/Typography'
import useMediaQuery from '@mui/material/useMediaQuery'
import { useQuery } from '@tanstack/react-query'
import { Link as RouterLink, useParams } from 'react-router-dom'
import { ApiError, apiDetail, get } from '../../api'
import Breadcrumb from '../../components/Breadcrumb'
import { NO_VALUE } from '../../lib/relativeTime'
import { formatZoned, viewerZone } from '../../lib/zonedTime'
import Banner from '../../ui/Banner'
import { CodeBlock } from '../../ui/CodeBlock'
import { CopyButton } from '../../ui/CopyButton'
import LoadingState from '../../ui/LoadingState'
import { PHONE, PHONE_QUERY, tapTargetSx } from '../../ui/listStyles'
import DetailPage, { DetailField } from '../../ui/detail/DetailPage'

/* A DEAD RUN'S PANE TAIL (`/sessions/:sessionId/pane-log`): the last bytes its node's tmux pane wrote,
 * sent once by the node after an unexplained end. `GET /api/sessions/{id}/pane-log` asks `sessions:edit` (the
 * transcript download's key) and serves the text already cleaned, redacted and capped, NEWEST LAST — so the
 * block opens scrolled to its end. */

/** The viewer's address, as the Sessions list, a node's Health tab and a task's Activity link it. */
export function paneLogPath(sessionId: string): string {
  return `/sessions/${encodeURIComponent(sessionId)}/pane-log`
}

/** `routes/sessions.py`'s pane-log body. */
export type PaneLog = {
  id: string
  name: string
  node: string | null
  node_id: string | null
  task_id: number | null
  task_title: string | null
  end_reason: string | null
  /** `end_reason` in words ("Session vanished"). Optional for rolling deploys. */
  end_reason_label?: string | null
  started_at: string | null
  ended_at: string | null
  captured_at: string | null
  /** The node file's mtime: when the pane last wrote. */
  written_at: string | null
  /** `written_at` is well after the end: every run of a task appends to one file, so a later run may show. */
  written_after_end?: boolean
  redactions: number
  redaction_confidence: string
  text: string
}

/* The facts above the log: one wrapping row, each fact its own width, so five short values cost one line on a
 * desktop and two or three at 390px — never a column of five stacked rows pushing the log below the fold. The
 * phone's label floor (`DetailField`'s 4.5rem, which aligns a COLUMN of values) is dropped: in a row it only parted
 * "Node" from its value. */
const FACTS_SX = {
  display: 'flex', flexWrap: 'wrap', columnGap: 3, rowGap: 1, m: 0, pt: 1.5, pb: 0.5,
  '& > *': { flex: '0 1 auto' },
  [PHONE]: { '& dt': { minWidth: 0 } },
} as const

/** The fold mark `pane_logs.tidy` leaves on a retry countdown it folded to its last frame. */
const FOLD_MARK = /\[×\d+\]/

/** The text's own scrollport: both axes, so a 390px page never scrolls sideways. */
const TEXT_MAX_HEIGHT = '60vh'

export default function PaneLogPage(): JSX.Element {
  const { sessionId = '' } = useParams<{ sessionId: string }>()
  const zone = useMemo(viewerZone, [])
  const log = useQuery({
    queryKey: ['session-pane-log', sessionId],
    queryFn: () => get<PaneLog>(`/api/sessions/${encodeURIComponent(sessionId)}/pane-log`),
    enabled: sessionId !== '',
    staleTime: Infinity,
    retry: false,
  })
  const data = log.data
  /* Columns matter on a desktop (tables, progress bars), so lines stay whole and scroll; on a phone a cause line
   * (a provider's refusal) would scroll off the box, so it wraps. Wrap flips either default. */
  const phone = useMediaQuery(PHONE_QUERY, { noSsr: true })
  const [wrapChoice, setWrapChoice] = useState<boolean | null>(null)
  const wrap = wrapChoice ?? phone
  const status = log.error instanceof ApiError ? log.error.status : null

  /* Newest lines are at the end: open on them, and again after Wrap reflows the text. `CodeBlock` takes no ref, so its
   * `<pre>` is found from a wrapper. */
  const wrapRef = useRef<HTMLDivElement>(null)
  useEffect(() => {
    const pre = wrapRef.current?.querySelector('pre')
    if (pre) pre.scrollTop = pre.scrollHeight
  }, [data?.text, wrap])

  /* Unwrapped, a long line runs past the box's right edge and nothing says so: a cause read there looked whole
   * while its end was hidden. Measured, so the note shows only when a line is really cut. */
  const [cut, setCut] = useState(false)
  useLayoutEffect(() => {
    const pre = wrapRef.current?.querySelector('pre')
    if (!pre || wrap) { setCut(false); return }
    const measure = () => setCut(pre.scrollWidth > pre.clientWidth)
    measure()
    if (typeof ResizeObserver === 'undefined') return
    const observer = new ResizeObserver(measure)
    observer.observe(pre)
    return () => observer.disconnect()
  }, [data?.text, wrap])

  const at = (iso: string | null) => (iso ? formatZoned(iso, zone, { seconds: true }) : NO_VALUE)
  /* The pane usually last writes in the run's final second, so Ended and Last written read the same: one fact then,
   * two only when they differ. */
  const ended = data ? at(data.ended_at) : NO_VALUE
  const written = data ? at(data.written_at) : NO_VALUE

  return (
    <DetailPage
      testId="pane-log-page"
      sx={{ minWidth: 0 }}
      // Back to this run's own row (`?q=` matches a row id exactly, `routes/sessions.py::_list_filters`).
      breadcrumb={<Breadcrumb items={[{ label: 'Sessions', to: `/sessions?q=${encodeURIComponent(sessionId)}` }, { label: 'Pane log' }]} />}
      title={data && <Box component="span" data-testid="pane-log-name" sx={{ overflowWrap: 'anywhere' }}>{data.name}</Box>}
      actions={data && (
        <>
          <Button
            size="small"
            variant="outlined"
            color="inherit"
            aria-pressed={wrap}
            data-testid="pane-log-wrap"
            onClick={() => setWrapChoice(!wrap)}
            sx={[{ textTransform: 'none', whiteSpace: 'nowrap', ...(wrap && { bgcolor: 'action.selected' }) }, tapTargetSx]}
          >
            Wrap
          </Button>
          <CopyButton text={() => data.text} label="Copy" testId="pane-log-copy" />
        </>
      )}
    >
      {log.isPending && sessionId !== '' && <LoadingState rows={6} label="Loading pane log" />}
      {status === 403 && <Banner severity="warning" testId="pane-log-forbidden">Needs sessions edit access.</Banner>}
      {status === 404 && <Banner severity="info" testId="pane-log-missing">No pane log for this session.</Banner>}
      {log.isError && status !== 403 && status !== 404 && (
        <Banner severity="error">{apiDetail(log.error, 'Could not load the pane log')}</Banner>
      )}
      {data && (
        <>
          <Box component="dl" data-testid="pane-log-facts" sx={FACTS_SX}>
            <DetailField label="Node">
              {data.node_id ? (
                <MuiLink component={RouterLink} to={`/nodes/${encodeURIComponent(data.node_id)}`} data-testid="pane-log-node">
                  {data.node ?? data.node_id}
                </MuiLink>
              ) : <Typography variant="body2">{data.node ?? NO_VALUE}</Typography>}
            </DetailField>
            {/* A run with no task has no Task fact: a lone dash only spends a slot. */}
            {data.task_id != null && (
              <DetailField label="Task">
                <MuiLink component={RouterLink} to={`/tasks/${data.task_id}`} data-testid="pane-log-task">
                  #{data.task_id}{data.task_title ? ` · ${data.task_title}` : ''}
                </MuiLink>
              </DetailField>
            )}
            <DetailField label="End reason">
              <Typography variant="body2" data-testid="pane-log-end-reason" title={data.end_reason ?? undefined}>
                {data.end_reason_label ?? data.end_reason ?? NO_VALUE}
              </Typography>
            </DetailField>
            {ended === written ? (
              <DetailField label="Ended · last written">
                <Typography variant="body2" data-testid="pane-log-written">{ended}</Typography>
              </DetailField>
            ) : (
              <>
                <DetailField label="Ended">
                  <Typography variant="body2" data-testid="pane-log-ended">{ended}</Typography>
                </DetailField>
                <DetailField label="Last written">
                  <Typography variant="body2" data-testid="pane-log-written">{written}</Typography>
                </DetailField>
              </>
            )}
          </Box>
          {data.written_after_end && (
            <Banner severity="warning" testId="pane-log-later-run">Written after this run ended; may show a later run.</Banner>
          )}
          {/* The redaction caveat before the bytes, as the transcript download's: it counts what it caught only. */}
          <Typography variant="caption" component="p" data-testid="pane-log-caveat" sx={{ color: 'text.secondary', my: 1 }}>
            Secrets redacted best effort ({data.redactions} caught); treat as sensitive.
            {FOLD_MARK.test(data.text) && (
              <Box component="span" data-testid="pane-log-fold-note" sx={{ display: 'block' }}>
                [×N]: N repeated retry frames folded into the last one; “8…5” is a folded countdown.
              </Box>
            )}
            {cut && (
              <Box component="span" data-testid="pane-log-cut-note" sx={{ display: 'block' }}>
                Long lines cut at the edge — scroll sideways or{' '}
                <MuiLink component="button" type="button" variant="caption" onClick={() => setWrapChoice(true)}
                  sx={{ verticalAlign: 'baseline' }}>
                  Wrap
                </MuiLink>.
              </Box>
            )}
          </Typography>
          <Box ref={wrapRef} sx={{ minWidth: 0 }}>
            <CodeBlock testId="pane-log-text" wrap={wrap} maxHeight={TEXT_MAX_HEIGHT} bordered>
              {data.text}
            </CodeBlock>
          </Box>
        </>
      )}
    </DetailPage>
  )
}
