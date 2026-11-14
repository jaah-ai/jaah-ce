import { posix } from 'node:path'
import type { Plugin } from 'vite'

/* THE BUILD SIDE OF `src/lib/chunkImport.ts`: every `import("./X.js")` of a sibling chunk becomes a call to the
 * page's `__jaahImport`, which can fetch a stuck chunk again under a one-off address. A page that has not installed it
 * yet falls back to the chunk's own `import()`, resolved against the chunk as before. A chunk that any other chunk
 * imports STATICALLY keeps its plain `import()`: a second copy loaded under `?fresh=N` would hold module state apart
 * from the copy its static importers get. Runs in
 * `generateBundle` AFTER Vite's own (`vite:build-import-analysis`), which still has to find the plain `import()`
 * to fill each `__vitePreload` dependency list. `build.sourcemap` is off; were it on, columns after a rewritten
 * import would be off by the added text on that line. */
const SIBLING_CHUNK_IMPORT = /(?<![\w$.])import\(\s*(["'])(\.{1,2}\/[^"'\\\s]+?\.js)\1\s*\)/g

/** `code` with every dynamic import of a relative `.js` literal that `route` accepts (all, by default) routed through
 *  `__jaahImport`. */
export function rewriteChunkImports(code: string, route: (spec: string) => boolean = () => true): string {
  return code.replace(SIBLING_CHUNK_IMPORT, (match, _quote, spec: string) =>
    route(spec) ? `(globalThis.__jaahImport||(s=>import(s)))(${JSON.stringify(spec)},import.meta.url)` : match)
}

/** Build-only; `skipChunk` names an entry that is not a module (a classic script). */
export function chunkImportPlugin(skipChunk?: string): Plugin {
  return {
    name: 'chunk-import',
    apply: 'build',
    generateBundle: {
      order: 'post',
      handler(_options, bundle) {
        const chunks = Object.values(bundle).filter(file => file.type === 'chunk')
        const staticTargets = new Set(chunks.flatMap(chunk => chunk.imports))
        for (const chunk of chunks) {
          if (chunk.name === skipChunk) continue
          const dir = posix.dirname(chunk.fileName)
          chunk.code = rewriteChunkImports(chunk.code, spec => !staticTargets.has(posix.join(dir, spec)))
        }
      },
    },
  }
}
