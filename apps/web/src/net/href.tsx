/* THE CHOKE POINT FOR URLS THE BROWSER FETCHES BY ITSELF. An `<img src>`, an `<iframe src>` and a download link reach
 * the console's API without `send` (`send.ts`), so a dead connection used to leave a broken image, a blank frame or a
 * download that never started. Every such URL is built here from an API path (`send.ts::apiHref`, rewritten on the
 * live host) and gets the same rule:
 *   - an image or a frame that errors (a frame: its load got no answer, `frameLoadFailed`), or has not loaded `LOAD_BOUND_MS` after it was on screen, is asked for again
 *     ONCE with a cache-busting `?_r=<n>` (a new request, which the browser sends on a fresh connection by then); a
 *     second failure shows a Retry button, never a broken-image icon;
 *   - a download is fetched as a blob through `send` (its headers bounded, its body by an idle bound, retried once)
 *     and saved, so a stall is retried rather than left to the browser; a file over `BLOB_DOWNLOAD_MAX_BYTES`, a 4xx,
 *     or a download that still fails falls back to the plain navigation it always was, whose download manager shows
 *     the failure and offers its own Retry. */

import Box from '@mui/material/Box'
import Button from '@mui/material/Button'
import {
  forwardRef,
  useCallback,
  useEffect,
  useRef,
  useState,
  type ForwardedRef,
  type IframeHTMLAttributes,
  type ImgHTMLAttributes,
  type MouseEvent,
} from 'react'

import { apiHref, send } from './send'
import { FONT_SIZE } from '../theme'

/** How long an on-screen image or frame may take to load before it is asked for again: a dead connection is the
 *  case it exists for. The retried request gets `RETRY_BOUND_FACTOR` times longer before Retry is shown, so a large
 *  file on a slow but healthy link still lands. */
export const LOAD_BOUND_MS = 10_000
const RETRY_BOUND_FACTOR = 3
/** The cache-busting query parameter a retried request carries. */
export const RETRY_PARAM = '_r'
/** A download's bound per attempt (`send`'s `boundMs`): the answer's headers must arrive within it. */
const DOWNLOAD_BOUND_MS = 60_000
/** The longest a download's body may go without a byte before the attempt counts as stalled. */
const DOWNLOAD_IDLE_MS = 20_000
/** The largest file saved through a blob; a larger one is the browser's own download (it retries by itself). */
export const BLOB_DOWNLOAD_MAX_BYTES = 50 * 1024 * 1024
/** How long a saved blob's object URL lives: the browser has started the save long before. */
const OBJECT_URL_LIFETIME_MS = 60_000

/** `url` with `?_r=<attempt>` (attempt 0: unchanged). */
export function withRetryParam(url: string, attempt: number): string {
  if (attempt === 0 || url === '') return url
  return `${url}${url.includes('?') ? '&' : '?'}${RETRY_PARAM}=${attempt}`
}

/** An API path as the browser fetches it (rewritten on the live host; '' when this host must not fetch it). A local
 *  `blob:`/`data:` URL (a pasted image's preview) is used as it is. */
function browserUrl(src: string): string {
  return src.startsWith('/api/') ? apiHref(src) : src
}

/** A link the USER follows (opens a file in a new tab, plays a media file): the browser's own navigation, whose
 *  error page carries its own Reload. Never for an image, a frame or a download — those are `ApiImg`, `ApiFrame`
 *  and `apiDownloadProps`. */
export const apiLinkHref = (path: string): string => browserUrl(path)

/** Whether `el` is laid out inside the viewport: a lazy image below the fold, or one in a closed tab panel, is not
 *  loading yet and must not count as stalled. */
function onScreen(el: Element): boolean {
  // An image with no size of its own is 0×0 until it loads: its box is the one it sits in.
  let r = el.getBoundingClientRect()
  if ((r.width === 0 || r.height === 0) && el.parentElement) r = el.parentElement.getBoundingClientRect()
  return r.width > 0 && r.height > 0 && r.bottom > 0 && r.right > 0
    && r.top < window.innerHeight && r.left < window.innerWidth
}

/** The retry state of one browser-fetched URL: which attempt is showing, and whether both failed. Only an API path
 *  is retried: a local `blob:` URL has no connection to replace. */
function useRetriedUrl(src: string, onFailed?: () => void) {
  const base = browserUrl(src)
  const retried = src.startsWith('/api/')
  const [state, setState] = useState({ base, attempt: 0, failed: false, loaded: false })
  // A new source starts over (a render-time reset, so the old URL is never shown under the new one).
  const current = state.base === base ? state : { base, attempt: 0, failed: false, loaded: false }
  if (current !== state) setState(current)
  const failedRef = useRef(onFailed)
  failedRef.current = onFailed
  useEffect(() => {
    if (current.failed) failedRef.current?.()
  }, [current.failed])

  const fail = useCallback(() => {
    setState(s => {
      if (s.base !== base || s.failed || s.loaded) return s
      // The automatic retry, once; then the Retry affordance (or the caller's own fallback).
      return retried && s.attempt === 0 ? { ...s, attempt: 1 } : { ...s, failed: true }
    })
  }, [base, retried])
  const loaded = useCallback(() => setState(s => (s.base === base ? { ...s, loaded: true } : s)), [base])
  const retry = useCallback(() => setState(s => ({ ...s, attempt: s.attempt + 1, failed: false, loaded: false })), [])
  return {
    url: retried ? withRetryParam(base, current.attempt) : base,
    boundMs: current.attempt === 0 ? LOAD_BOUND_MS : LOAD_BOUND_MS * RETRY_BOUND_FACTOR,
    retried,
    watch: retried && !current.failed && !current.loaded && base !== '',
    failed: current.failed,
    fail,
    loaded,
    retry,
  }
}

/** Calls `onStalled` once `el` has been on screen for `boundMs` without loading. */
function useStallWatch(
  el: { current: Element | null }, active: boolean, key: string, boundMs: number, onStalled: () => void,
) {
  useEffect(() => {
    if (!active) return
    let seenSince: number | null = null
    const timer = setInterval(() => {
      const node = el.current
      if (!node || (node instanceof HTMLImageElement && node.complete && node.naturalWidth > 0)) return
      if (!onScreen(node)) {
        seenSince = null
        return
      }
      seenSince ??= Date.now()
      if (Date.now() - seenSince < boundMs) return
      clearInterval(timer)
      onStalled()
    }, LOAD_BOUND_MS / 5)
    return () => clearInterval(timer)
  }, [el, active, key, boundMs, onStalled])
}

function assignRef<T>(ref: ForwardedRef<T>, value: T | null): void {
  if (typeof ref === 'function') ref(value)
  else if (ref) ref.current = value
}

/** The Retry a failed image or frame shows in its place. */
function RetryNotice({ what, onRetry, className }: { what: string; onRetry: () => void; className?: string }) {
  return (
    <Box className={className} sx={{ display: 'flex', alignItems: 'center', justifyContent: 'center', gap: 1, p: 1 }}
      data-testid="api-url-retry">
      <Box component="span" sx={{ color: 'text.secondary', fontSize: FONT_SIZE.md }}>{what} did not load</Box>
      <Button size="small" variant="outlined" onClick={onRetry}>Retry</Button>
    </Box>
  )
}

export type ApiImgProps = Omit<ImgHTMLAttributes<HTMLImageElement>, 'src' | 'onError'> & {
  /** An API path (`/api/...`), or a local `blob:`/`data:` URL. */
  src: string
  /** Both attempts failed. Given, the caller shows its own fallback and this renders nothing; absent, a Retry. */
  onError?: () => void
}

/** An `<img>` for an API path (module header). Takes every `<img>` attribute, so MUI's `component={ApiImg}` works. */
export const ApiImg = forwardRef<HTMLImageElement, ApiImgProps>(function ApiImg({ src, onError, onLoad, ...rest }, ref) {
  const own = useRef<HTMLImageElement | null>(null)
  const r = useRetriedUrl(src, onError)
  useStallWatch(own, r.watch, r.url, r.boundMs, r.fail)
  // A local preview that broke has nothing to ask again: it stays the plain `<img>` it was.
  if (r.failed && onError) return null
  if (r.failed && r.retried) return <RetryNotice what="Image" onRetry={r.retry} className={rest.className} />
  return (
    <img
      {...rest}
      ref={node => { own.current = node; assignRef(ref, node) }}
      src={r.url}
      onLoad={e => { r.loaded(); onLoad?.(e) }}
      onError={r.failed ? undefined : r.fail}
    />
  )
})

export type ApiFrameProps = Omit<IframeHTMLAttributes<HTMLIFrameElement>, 'src'> & { src: string }

/** Whether the frame's last load of `url` got no answer (a cut connection: the browser shows its own error page and
 *  still fires `load`) or a gateway's 5xx. Read from the page's Resource Timing entry for that URL; a browser that
 *  records none, or reports no status, counts as loaded — the stall bound still applies to a frame that hangs. */
export function frameLoadFailed(url: string): boolean {
  if (typeof performance === 'undefined' || typeof performance.getEntriesByName !== 'function') return false
  const entries = performance.getEntriesByName(new URL(url, window.location.href).href)
  const last = entries[entries.length - 1] as (PerformanceResourceTiming & { responseStatus?: number }) | undefined
  if (!last) return false
  if (typeof last.responseStatus === 'number' && last.responseStatus > 0) return last.responseStatus >= 500
  return last.responseStart === 0 && last.responseEnd > 0
}

/** An `<iframe>` for an API path (module header). A frame reports no error event: a load that got no answer is told
 *  apart by `frameLoadFailed`, and a frame that hangs by the stall bound. */
export const ApiFrame = forwardRef<HTMLIFrameElement, ApiFrameProps>(function ApiFrame({ src, onLoad, ...rest }, ref) {
  const own = useRef<HTMLIFrameElement | null>(null)
  const r = useRetriedUrl(src)
  useStallWatch(own, r.watch, r.url, r.boundMs, r.fail)
  if (r.failed) return <RetryNotice what="This file" onRetry={r.retry} className={rest.className} />
  return (
    <iframe
      {...rest}
      ref={node => { own.current = node; assignRef(ref, node) }}
      src={r.url}
      onLoad={e => {
        if (frameLoadFailed(r.url)) {
          r.fail()
          return
        }
        r.loaded()
        onLoad?.(e)
      }}
    />
  )
})

/** The name a `Content-Disposition` header gives, if any. */
function dispositionName(header: string | null): string | null {
  if (!header) return null
  const star = /filename\*\s*=\s*(?:UTF-8'')?([^;]+)/i.exec(header)
  if (star) {
    try {
      return decodeURIComponent(star[1].trim().replace(/^"|"$/g, ''))
    } catch { /* fall through to the plain name */ }
  }
  const plain = /filename\s*=\s*"?([^";]+)"?/i.exec(header)
  return plain ? plain[1].trim() : null
}

/** Click a temporary `<a download>`: the one way a page starts a save. `filename` undefined leaves the attribute
 *  empty, so the server's `Content-Disposition` (or the URL's last segment) names the file and any answer is saved;
 *  `null` sets no attribute at all, so only an attachment answer saves and anything else (a refusal) navigates. */
export function clickDownload(href: string, filename?: string | null): void {
  const a = document.createElement('a')
  a.href = href
  if (filename !== null) a.download = filename ?? ''
  a.rel = 'noopener'
  a.style.display = 'none'
  document.body.appendChild(a)
  a.click()
  a.remove()
}

/** The body of `resp`, read with `DOWNLOAD_IDLE_MS` as the most it may go without a byte; `'too-big'` once it passes
 *  `BLOB_DOWNLOAD_MAX_BYTES` (a streamed answer names no length up front). Aborts `ctl` on either. */
async function readBody(resp: Response, ctl: AbortController): Promise<Blob | 'too-big'> {
  if (!resp.body) return resp.blob()
  const reader = resp.body.getReader()
  const chunks: Uint8Array[] = []
  let size = 0
  for (;;) {
    let timer: ReturnType<typeof setTimeout> | undefined
    const idle = new Promise<never>((_, reject) => {
      timer = setTimeout(() => reject(new Error('download stalled')), DOWNLOAD_IDLE_MS)
    })
    try {
      const { done, value } = await Promise.race([reader.read(), idle])
      if (done) break
      size += value.byteLength
      if (size > BLOB_DOWNLOAD_MAX_BYTES) {
        ctl.abort()
        return 'too-big'
      }
      chunks.push(value)
    } catch (err) {
      ctl.abort()
      throw err
    } finally {
      clearTimeout(timer)
    }
  }
  return new Blob(chunks as BlobPart[], { type: resp.headers.get('content-type') ?? '' })
}

/** One attempt's outcome: the file, `'too-big'` / `'refused'` (a 4xx: asking again changes nothing) for the
 *  browser's own download, or `null` for a failure worth one retry. */
type Fetched = { blob: Blob; name: string | null } | 'too-big' | 'refused' | null

async function fetchBlob(path: string, attempt: number): Promise<Fetched> {
  const ctl = new AbortController()
  try {
    const resp = await send(withRetryParam(path, attempt), {
      credentials: 'same-origin', relay: 'none', boundMs: DOWNLOAD_BOUND_MS, signal: ctl.signal,
    })
    if (resp.status >= 400 && resp.status < 500) return 'refused'
    if (!resp.ok) return null
    if (Number(resp.headers.get('content-length') ?? 0) > BLOB_DOWNLOAD_MAX_BYTES) {
      ctl.abort()
      return 'too-big'
    }
    const blob = await readBody(resp, ctl)
    return blob === 'too-big' ? blob : { blob, name: dispositionName(resp.headers.get('content-disposition')) }
  } catch {
    return null
  }
}

/** Save the file at API `path` (module header). `filename` names it when the server's answer does not. */
export async function apiDownload(path: string, filename?: string): Promise<void> {
  for (const attempt of [0, 1]) {
    const got = await fetchBlob(path, attempt)
    if (got === 'too-big' || got === 'refused') break
    if (got === null) continue
    const url = URL.createObjectURL(got.blob)
    // The server's name first, as a plain `<a download>` would take it.
    clickDownload(url, got.name || filename || undefined)
    setTimeout(() => URL.revokeObjectURL(url), OBJECT_URL_LIFETIME_MS)
    return
  }
  // Too big, refused, or two failed attempts: the plain navigation it always was (the browser's manager shows the
  // failure and retries).
  const href = browserUrl(path)
  if (href) clickDownload(href, filename)
}

/** A download anchor's `href`, `onClick` and (when given) `download` for API `path`: it looks and copies like the
 *  plain link it always was, and a plain click saves through `apiDownload`. `download`: the attribute the link had —
 *  a file name, `true` for a bare one, omitted for none. A modified click (new tab, save-as) is the browser's. */
export function apiDownloadProps(path: string, download?: string | true) {
  const filename = typeof download === 'string' ? download : undefined
  return {
    href: browserUrl(path),
    ...(download === undefined ? {} : { download }),
    onClick: (e: MouseEvent<HTMLElement>) => {
      if (e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return
      e.preventDefault()
      void apiDownload(path, filename)
    },
  }
}
