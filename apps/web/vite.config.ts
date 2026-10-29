import { existsSync, readFileSync, writeFileSync } from 'node:fs'
import { defineConfig, type Plugin } from 'vite'
import react from '@vitejs/plugin-react'
import { obfuscatePlugin } from './obfuscate'
import { consoleCspPlugin, isLiveHost } from './devCsp'

// Dev-server fallbacks track this repo's port grid (block 2 → API 8020 / FRONTEND 3020).
// In a worktree, override via VITE_API_PROXY_TARGET / VITE_HMR_CLIENT_PORT (the pool-banded ports).
const apiTarget = process.env.VITE_API_PROXY_TARGET ?? `http://localhost:${process.env.API_PORT ?? 8020}`
const hmrClientPort = Number(process.env.VITE_HMR_CLIENT_PORT ?? 3020)
// The console relay (apps/api/app/console_relay/) is its own process; the compose stack names it via
// VITE_RELAY_PROXY_TARGET. The fallback is the relay's own listen port for one run on the host.
const relayTarget = process.env.VITE_RELAY_PROXY_TARGET ?? `http://localhost:${process.env.CONSOLE_RELAY_PORT ?? 8000}`

// THE repo version — the same string the api and jaahd report, not a web-only number.
// CI/deploy inject it via APP_VERSION (computed by the repo's version script); without
// injection we fall back to the MAJOR.MINOR milestone in the ROOT VERSION file. Single home for the
// formula: that version script.
//
// Two probe paths, because this one config runs under two layouts and there is no per-app VERSION to
// anchor it anymore: './VERSION' is the FLATTENED in-image layout (Dockerfile.frontend copies the root
// file next to the config at /app), '../../VERSION' is the repo layout (apps/web/ → repo root). First
// readable NON-EMPTY file wins; '0.0.0' only when both are absent, which useVersionCheck.ts reads as
// "not a real build".
//
// The injected value is tested for EMPTINESS, not for null (`??` would be wrong here, measured
// 2026-08-11): Dockerfile.frontend's `ARG APP_VERSION` + `ENV APP_VERSION=${APP_VERSION}` defines the
// variable as the EMPTY STRING when the build passes no --build-arg, so `??` never fell through and an
// un-injected image shipped `{"version":""}` — the file fallback below was dead code inside the image
// it exists for. app/version.py's `or`-chain has always had this right. A CI workflow check pins it.
const VERSION_PROBES = ['./VERSION', '../../VERSION']
const appVersion =
  process.env.APP_VERSION?.trim() ||
  (() => {
    for (const probe of VERSION_PROBES) {
      try {
        const text = readFileSync(new URL(probe, import.meta.url), 'utf8').trim()
        if (text) return text
      } catch {
        // Unreadable here just means "wrong layout" — try the next probe, never fail the build.
      }
    }
    return '0.0.0'
  })()

// Static build manifest: the deployed web version needs to be readable by a running
// client WITHOUT going through the api container, which deploys independently and has no reliable
// knowledge of the web bundle's version. closeBundle (not a Dockerfile step) so this also works for a
// plain local `npm run build`. nginx serves dist/version.json as a real file under `location /`
// (no-cache — see nginx.conf.template), so a stale index.html/version.json is never served across a
// redeploy the way an `/assets/` (immutable-cached) file would be.
function versionManifestPlugin(version: string): Plugin {
  return {
    name: 'version-manifest',
    // BUILD ONLY, and `closeBundle` is exactly why it has to be said: vite's DEV `PluginContainer.close()`
    // fires `buildEnd` + `closeBundle` too (vite 5.4 dist, `class PluginContainer`), so an unqualified
    // plugin rewrites `dist/version.json` every time a dev server SHUTS DOWN. In the frontend container
    // that write is root-owned inside the bind-mounted worktree, which leaves the slot
    // unerasable (see the api service's `PYTHONPYCACHEPREFIX` note); on a host checkout it silently
    // back-dates a real `npm run build`'s manifest to whatever the dev server's version string was.
    apply: 'build',
    closeBundle() {
      // A failed build writes no `dist/`; writing into it then would replace the build's own error with an ENOENT.
      if (!existsSync(new URL('./dist/', import.meta.url))) return
      writeFileSync(new URL('./dist/version.json', import.meta.url), JSON.stringify({ version }))
    },
  }
}

// Route path → the page module that path renders. Only the entries worth pre-warming.
const ROUTE_ENTRIES: Record<string, string> = {
  '/tasks': '/src/pages/Tasks.tsx',
  '/nodes': '/src/pages/Nodes.tsx',
  // `/nodes/:nodeId` is not listed and cannot be: these keys are literal paths. The node DETAIL page is
  // where the worktree pool now lives (`/worktrees` was retired, and the module it named
  // here, which this table went on asking Vite to warm on every dev boot until it was deleted).
  '/nodes/local': '/src/pages/NodeDetailPage.tsx',
  '/projects': '/src/pages/Projects.tsx',
}

const APP_ENTRY = '/src/main.tsx'

// Whether this tree carries the Live Cast viewer: the community edition's leaves it out (`live_cast`,
// packages/editions/features.toml), and then the build is the console alone and the dev server answers no live host.
const LIVE_VIEWER = existsSync(new URL('./live.html', import.meta.url))

// THE LIVE CAST VIEWER ON THE DEV SERVER. Production serves `live.html` for every client route of its own host (the
// 3053 listener in nginx.conf.template); the dev server answers that host too, as `live.localhost:<port>` (Chromium
// resolves any `*.localhost` to loopback, and Vite admits it), so the viewer and its e2e specs run against the real
// dev stack exactly as the console does. A page navigation on that host is rewritten to `live.html`; modules,
// assets and `/api` pass through. Dev-only — the build already emits `live.html` beside `index.html`.
function liveDevHostPlugin(): Plugin {
  return {
    name: 'live-dev-host',
    apply: 'serve',
    configureServer(server) {
      server.middlewares.use((req, _res, next) => {
        const path = (req.url ?? '/').split('?')[0]
        const page = req.method === 'GET' && String(req.headers.accept ?? '').includes('text/html')
          && !path.startsWith('/api/') && !path.startsWith('/@') && !/\.[a-z0-9]+$/i.test(path)
        if (page && isLiveHost(req.headers.host)) req.url = '/live.html'
        next()
      })
    },
  }
}

// THE LIVE VIEWER'S CHUNK RECOVERY (src/live/chunkRecovery.ts says what and why). Its own build entry, so it shares
// no chunk with the viewer, and a CLASSIC `<script>` first in `live.html`'s head: it must already be listening when
// the viewer's module graph 404s, which is the failure in which no module code runs. NOT content-hashed: under the
// very build split it answers, a hashed name from one build 404s on the other, so every build carries it at ONE
// path (`LIVE_RECOVERY_FILE`), which the live host serves `no-cache` (nginx.conf.template, 3053). Build-only.
const LIVE_RECOVERY_ENTRY = 'liveChunkRecovery'
const LIVE_RECOVERY_FILE = 'assets/live-recovery.js'

function liveChunkRecoveryPlugin(): Plugin {
  return {
    name: 'live-chunk-recovery',
    apply: 'build',
    // A classic script's top-level names are page globals, shared with gtag's: scoped to itself. An
    // `import` in it would be a syntax error that drops the recovery silently, so a shared chunk fails the build.
    renderChunk(code, chunk) {
      if (chunk.name !== LIVE_RECOVERY_ENTRY) return null
      if (chunk.imports.length || chunk.dynamicImports.length || chunk.exports.length) {
        throw new Error(`${LIVE_RECOVERY_ENTRY} must be self-contained: it loads as a classic script`)
      }
      return `(()=>{${code}})();`
    },
    transformIndexHtml: {
      order: 'post',
      handler(_html, ctx) {
        if (!ctx.filename.endsWith('live.html')) return
        if (!ctx.bundle?.[LIVE_RECOVERY_FILE]) throw new Error(`live.html: ${LIVE_RECOVERY_FILE} is missing from the bundle`)
        return [{ tag: 'script', attrs: { src: `/${LIVE_RECOVERY_FILE}` }, injectTo: 'head-prepend' }]
      },
    },
  }
}

export default defineConfig({
  define: { __APP_VERSION__: JSON.stringify(appVersion) },
  plugins: [
    react(),
    versionManifestPlugin(appVersion),
    ...(LIVE_VIEWER ? [liveDevHostPlugin(), liveChunkRecoveryPlugin()] : []),
    // The production Content-Security-Policy on every dev page, plus the violation collector the
    // Playwright run reads (devCsp.ts).
    consoleCspPlugin(),
    // Opt-in, build-only; the deploy workflow sets it for the `main` image alone (obfuscate.ts).
    obfuscatePlugin(process.env.WEB_OBFUSCATE === '1'),
  ],
  // Two pages: the console (`index.html`) and, where the tree carries it, the Live Cast viewer (`live.html`,
  // `src/live/main.tsx`), which nginx serves on the live host alone. Shared modules split into common chunks, so the
  // console's boot path is unchanged.
  build: {
    rollupOptions: {
      input: {
        main: 'index.html',
        ...(LIVE_VIEWER ? { live: 'live.html', [LIVE_RECOVERY_ENTRY]: 'src/live/chunkRecoveryEntry.ts' } : {}),
      },
      // Vite's own default for every other entry.
      output: { entryFileNames: c => (c.name === LIVE_RECOVERY_ENTRY ? LIVE_RECOVERY_FILE : 'assets/[name]-[hash].js') },
    },
  },
  server: {
    host: '0.0.0.0',
    port: 3050,
    strictPort: true,
    // Vite ≥5.4.12 blocks non-localhost Host headers by default. The console is reached over
    // Tailscale from other tailnet devices, so allow any
    // MagicDNS name (*.ts.net) — the perimeter is the tailnet itself, not the Host header. Also
    // allow the bare MagicDNS short name (no .ts.net suffix — Tailscale resolves it directly
    // within the tailnet, the JAAH_REMOTE_HOST of the host's remote-access env file): the Host header some clients send
    // is the literal short name typed in the URL bar, which doesn't match the suffix-only pattern above.
    // Named by the host's `.env` (VITE_DEV_SHORT_HOST, passed by the local compose file), never here: the
    // config ships in the community edition's public tree.
    allowedHosts: ['.ts.net', ...(process.env.VITE_DEV_SHORT_HOST ? [process.env.VITE_DEV_SHORT_HOST] : [])],
    watch: { usePolling: true, interval: 120 },
    hmr: { clientPort: hmrClientPort },
    // Transform the main routes at boot instead of on first visit: the first /tasks load after a
    // dev-server restart stops paying esbuild transform time (measured 310ms cold vs 101ms warm).
    // FILE GLOBS relative to the root, not the browser URLs above: a leading `/` is read as an absolute
    // filesystem path (`/src/main.tsx`), which failed every boot and every config restart with "Pre-transform
    // error: Failed to load url /@fs/src/…" and warmed nothing.
    warmup: {
      clientFiles: [APP_ENTRY, ...Object.values(ROUTE_ENTRIES)].map(url => `.${url}`),
    },
    proxy: {
      // The console's byte path goes to the RELAY, not the API — as the ALB routes it on ECS. Listed
      // BEFORE '/api': the first matching prefix wins. ws: true — without it the dev proxy never upgrades
      // the socket; the handshake just hangs until the browser gives up. '/api/console-stream' is the
      // node leg (a node dials it directly, never via this proxy); listed so the routing reads whole.
      '/api/console-relay': { target: relayTarget, changeOrigin: true, ws: true },
      '/api/console-stream': { target: relayTarget, changeOrigin: true, ws: true },
      // ws: true kept for the API's remaining WebSocket (/api/node-bus).
      '/api': { target: apiTarget, changeOrigin: true, ws: true },
      /* THE PRESIGNED-UPLOAD PATH IN LOCAL DEV, and every clause here is
         load-bearing — do not "simplify" this entry away.
         MinIO publishes no host port in the local stack, so there is no absolute URL the browser could
         be handed: the api mints the presigned URL and rewrites it to a SAME-ORIGIN `/s3/...`, which
         lands here and is proxied into the compose network.
         `changeOrigin: true` IS THE WHOLE TRICK. The signature was computed over the `host` header
         `minio:9000`; without this the proxy forwards the browser's own `Host` (localhost:3050) and
         MinIO recomputes a different signature, so EVERY local upload 403s. `rewrite` strips the `/s3`
         prefix because the signed path is the bucket path, which does not contain it. No `ws`: this
         carries PUTs and GETs, never an upgrade.
         SCOPED TO THE ATTACHMENT KEY PATH, AND THAT IS THE ONLY REASON THE KEY IS A REGEXP. A bare
         `/s3` prefix forwarded ANY path to MinIO — `/s3/minio/admin/v3/…`, `PUT /s3/a-new-bucket`, a
         bucket listing — over a proxy with no authentication of its own, to a server whose root
         credentials ship as a documented default (`.env.example`). That is harmless only while the dev
         server is on loopback, and it is not always: `FRONTEND_BIND` + the tailnet is a documented
         access mode — the `allowedHosts` entry above exists for precisely that — so anyone who could
         reach the frontend port could act as MinIO ROOT over this worktree's objects. The pattern admits exactly what the browser is ever
         handed and nothing else — a path-style `/{bucket}/task-attachments/{task}/{id}` URL, built by
         `attachment_storage.object_key` + `presign.s3_object_url` and reachable only with a live
         signature. ONE PATH SEGMENT STANDS IN FOR THE BUCKET rather than its name: the frontend
         container is not given `SESSION_TRANSCRIPT_BUCKET`, and reading a copy of it here would be a
         second answer to a question the api already answers, wrong on any stack that renamed the
         bucket. Vite reads a `^`-prefixed key as a RegExp tested against the whole request URL
         (`doesProxyContextMatchUrl`), so the signature's query string is unaffected and everything
         outside the prefix now falls through to the SPA instead of to MinIO. */
      '^/s3/[^/]+/task-attachments/': {
        target: 'http://minio:9000',
        changeOrigin: true,
        rewrite: (path: string) => path.replace(/^\/s3/, ''),
      },
    },
  },
})
