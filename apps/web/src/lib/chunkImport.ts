/* EVERY DYNAMIC `import()` OF A BUILT CHUNK GOES THROUGH HERE (imported by `main.tsx` before anything else that
 * loads). The build rewrites each `import("./X.js")` in its chunks into `__jaahImport("./X.js", import.meta.url)`
 * (`chunkImportPlugin.ts`). A browser that holds a dead connection keeps a chunk's first request on it for tens of
 * seconds, and a second `import()` of the same address waits on that same request instead of starting a new one.
 * So `refreshStuckChunks` (called by `lazyRetry.ts` when a load times out) gives a stuck address a one-off
 * `?fresh=N` suffix: the next import of it is a new download. Once an address has loaded, every later import reuses
 * the address it loaded under, so a module is never instantiated twice after that.
 *
 * Every loader that times out calls `refreshStuckChunks` (a route, its pane and the idle warm-up can wait on the same
 * chunk at once), and a fresh address rides the same dead connection until the browser gives that connection up
 * (~20 s). So an address counts as stuck only once it has itself been in flight `STUCK_AFTER_MS`, doubling with each
 * move of that chunk up to `MAX_STUCK_MS`: one copy per chunk at a time, not one per caller (on one customer's network
 * a page sent ~25 copies of two chunks within a minute, each spending the connection's request budget). */
type Importer = (url: string) => Promise<unknown>

declare global {
  var __jaahImport: ((spec: string, base: string) => Promise<unknown>) | undefined
}

const nativeImport: Importer = url => import(/* @vite-ignore */ url)
let importer: Importer = nativeImport
const loaded = new Map<string, string>()
const inFlight = new Map<string, number>()
const freshTokens = new Map<string, number>()
/** When the current address of a chunk was first requested, and how many times that chunk has been moved. */
const addressSince = new Map<string, number>()
const refreshes = new Map<string, number>()
let lastToken = 0

/** How long a chunk's first address may be in flight before it is moved; each later move waits twice as long, up to
 *  `MAX_STUCK_MS`. A loader's attempt timeout equals it (`lazyRetry.ts`), so the check allows `STUCK_SLACK_MS`: the
 *  address is stamped a moment after that loader's timer is armed. */
export const STUCK_AFTER_MS = 8_000
const MAX_STUCK_MS = 32_000
const STUCK_SLACK_MS = 100

/** Import `spec` resolved against `base`, under a fresh address when it is marked stuck, the loaded one once loaded. */
export function chunkImport(spec: string, base: string): Promise<unknown> {
  const url = new URL(spec, base).href
  const done = loaded.get(url)
  if (done !== undefined) return importer(done)
  const token = freshTokens.get(url)
  const actual = token === undefined ? url : `${url}${url.includes('?') ? '&' : '?'}fresh=${token}`
  inFlight.set(url, (inFlight.get(url) ?? 0) + 1)
  if (!addressSince.has(url)) addressSince.set(url, Date.now())
  const settle = () => {
    const left = (inFlight.get(url) ?? 1) - 1
    if (left > 0) inFlight.set(url, left)
    else {
      inFlight.delete(url)
      // A later import of it (after a failure) is a new request, its clock new too.
      addressSince.delete(url)
    }
  }
  return importer(actual).then(
    module => {
      settle()
      if (!loaded.has(url)) loaded.set(url, actual)
      return module
    },
    (error: unknown) => {
      settle()
      throw error
    },
  )
}

/** Give every chunk whose current address has been in flight long enough a new one-off suffix, so its next import
 * starts a new download; an address minted recently is left to the request already on its way. */
export function refreshStuckChunks(now: number = Date.now()): void {
  for (const url of inFlight.keys()) {
    const since = addressSince.get(url)
    const moved = refreshes.get(url) ?? 0
    const stuckAfter = Math.min(STUCK_AFTER_MS * 2 ** moved, MAX_STUCK_MS) - STUCK_SLACK_MS
    if (since === undefined || now - since < stuckAfter) continue
    freshTokens.set(url, ++lastToken)
    refreshes.set(url, moved + 1)
    addressSince.delete(url)
  }
}

export function installChunkImport(win: typeof globalThis = globalThis): void {
  win.__jaahImport = chunkImport
}

/** Test seam: the importer behind `chunkImport` (the browser's `import()` when omitted), with all state cleared. */
export function resetChunkImport(fn: Importer = nativeImport): void {
  importer = fn
  loaded.clear()
  inFlight.clear()
  freshTokens.clear()
  addressSince.clear()
  refreshes.clear()
  lastToken = 0
}

installChunkImport()
