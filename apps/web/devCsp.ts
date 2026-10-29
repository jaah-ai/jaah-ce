import { createHash } from 'node:crypto'
import { existsSync, readFileSync } from 'node:fs'
import type { IncomingMessage, ServerResponse } from 'node:http'
import type { Plugin } from 'vite'

// THE CONSOLE'S CSP ON THE DEV SERVER. nginx sends the policy in production
// (nginx-console-csp.conf, its single home); this plugin reads that SAME file and sends it on every page
// the Vite dev server serves — so local development and the whole Playwright suite (which runs against
// this dev server) are held to the production policy, and a change that would break under it fails there
// instead of in production.
//
// The dev server needs three things production does not, each added here and nowhere else:
//  * the hash of each INLINE script in the page — Vite's React plugin injects one (the Fast Refresh
//    preamble); production pages have none, so its policy allows no inline script at all;
//  * `ws://<host>` — the HMR socket and the api's WebSockets over plain-http dev (production is `wss:`);
//  * `report-uri` — the browser posts every violation to the collector below, which the Playwright run
//    reads at teardown (e2e/global-teardown.ts) and fails on: AC-11.9's zero-violation suite.
//
// THE COLLECTOR: `POST /__csp-report` stores a report, `GET /__csp-violations` lists them, `DELETE` clears
// them (global-setup). A report whose blocked URL names `CSP_CANARY_HOST` is a deliberate probe
// (e2e/console-csp.spec.ts) and is listed apart, so the suite can prove the reporting works without
// failing itself.

export const CSP_CANARY_HOST = 'csp-canary.invalid'
const MAX_REPORTS = 500

const POLICY_FILE = new URL('./nginx-console-csp.conf', import.meta.url)
// The Live Cast viewer's own policy (nginx-live-csp.conf), sent instead on a `live.` host (`isLiveHost`) — where the
// tree carries the viewer (the community edition's does not).
const LIVE_POLICY_FILE = new URL('./nginx-live-csp.conf', import.meta.url)

/** The production policy: the quoted value of the snippet's one `add_header Content-Security-Policy`. */
export function productionPolicy(text: string = readFileSync(POLICY_FILE, 'utf8'), file: URL = POLICY_FILE): string {
  const found = text.match(/^add_header Content-Security-Policy "([^"]+)" always;$/m)
  if (!found) throw new Error(`${file.pathname} carries no Content-Security-Policy line`)
  return found[1]
}

/** Is `host` the dev server reached as the Live Cast viewer (`live.localhost:<port>`, vite.config.ts)? Production's
 *  live host is its own nginx listener; in dev the same server answers both, told apart by the name. */
export const isLiveHost = (host: string | undefined): boolean => (host ?? '').toLowerCase().startsWith('live.')

const pagePolicy = (host: string | undefined): string =>
  isLiveHost(host) && existsSync(LIVE_POLICY_FILE)
    ? productionPolicy(readFileSync(LIVE_POLICY_FILE, 'utf8'), LIVE_POLICY_FILE)
    : productionPolicy()

/** `policy` with `extra` sources appended to `directive` (the directive is added if it is missing). */
function widen(policy: string, directive: string, extra: string[]): string {
  if (extra.length === 0) return policy
  const parts = policy.split(';').map(p => p.trim()).filter(Boolean)
  const at = parts.findIndex(p => p.split(/\s+/)[0] === directive)
  if (at === -1) parts.push([directive, ...extra].join(' '))
  else parts[at] = [parts[at], ...extra].join(' ')
  return parts.join('; ')
}

/** The sha256 source of every inline `<script>` body in `html`. */
export function inlineScriptHashes(html: string): string[] {
  const hashes = new Set<string>()
  for (const m of html.matchAll(/<script\b(?![^>]*\bsrc=)[^>]*>([\s\S]*?)<\/script>/gi)) {
    hashes.add(`'sha256-${createHash('sha256').update(m[1], 'utf8').digest('base64')}'`)
  }
  return [...hashes]
}

/** The policy one dev page is served under. */
export function devPolicy(base: string, html: string, host: string | undefined): string {
  let policy = widen(base, 'script-src', inlineScriptHashes(html))
  if (host) policy = widen(policy, 'connect-src', [`ws://${host}`])
  return widen(policy, 'report-uri', ['/__csp-report'])
}

type Report = { blocked: string; directive: string; document: string; sample: string }

function readBody(req: IncomingMessage): Promise<string> {
  return new Promise(resolve => {
    let body = ''
    req.setEncoding('utf8')
    req.on('data', (chunk: string) => {
      if (body.length < 64_000) body += chunk
    })
    req.on('end', () => resolve(body))
    req.on('error', () => resolve(body))
  })
}

function toReport(raw: string): Report | null {
  try {
    const parsed = JSON.parse(raw) as { 'csp-report'?: Record<string, unknown> }
    const r = parsed['csp-report']
    if (!r) return null
    const text = (key: string) => String(r[key] ?? '').slice(0, 300)
    return {
      blocked: text('blocked-uri'),
      directive: text('effective-directive') || text('violated-directive'),
      document: text('document-uri'),
      sample: text('script-sample'),
    }
  } catch {
    return null
  }
}

export function consoleCspPlugin(): Plugin {
  const violations: Report[] = []
  const canaries: Report[] = []
  return {
    name: 'console-csp',
    apply: 'serve',
    configureServer(server) {
      server.middlewares.use((req: IncomingMessage, res: ServerResponse, next: () => void) => {
        const path = (req.url ?? '').split('?')[0]
        if (path === '/__csp-report' && req.method === 'POST') {
          void readBody(req).then(raw => {
            const report = toReport(raw)
            if (report) {
              const bucket = report.blocked.includes(CSP_CANARY_HOST) ? canaries : violations
              if (bucket.length < MAX_REPORTS) bucket.push(report)
            }
            res.statusCode = 204
            res.end()
          })
          return
        }
        if (path === '/__csp-violations') {
          if (req.method === 'DELETE') {
            violations.length = 0
            canaries.length = 0
            res.statusCode = 204
            res.end()
            return
          }
          res.setHeader('Content-Type', 'application/json')
          res.end(JSON.stringify({ violations, canaries }))
          return
        }
        // Every HTML page Vite serves gets the policy, set as its body is written (Vite sets the content
        // type first and ends with the transformed page, so the inline scripts are known by then).
        const end = res.end.bind(res) as (...args: unknown[]) => ServerResponse
        res.end = ((...args: unknown[]) => {
          const [chunk] = args
          const type = String(res.getHeader('Content-Type') ?? '')
          if (!res.headersSent && type.startsWith('text/html') && (typeof chunk === 'string' || Buffer.isBuffer(chunk))) {
            const html = typeof chunk === 'string' ? chunk : chunk.toString('utf8')
            res.setHeader('Content-Security-Policy', devPolicy(pagePolicy(req.headers.host), html, req.headers.host))
          }
          return end(...args)
        }) as typeof res.end
        next()
      })
    },
  }
}
