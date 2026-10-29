import type { ObfuscatorOptions } from 'javascript-obfuscator'
import type { Plugin } from 'vite'

// PROD-ONLY OBFUSCATION OF OUR OWN CODE. Opted into by `WEB_OBFUSCATE=1` at build time,
// which the deploy workflow sets for the `main` image ALONE (the production console); dev images and local builds
// stay readable for debugging. Owner decisions: a real obfuscator TUNED for zero
// UX cost, applied to `src/` only (vendor libraries are public anyway and are where size/speed would go),
// and NO anti-debug features — production errors stay diagnosable.
//
// Per MODULE, before bundling, rather than per chunk: Vite's chunks mix vendor and app code, so a
// module-level transform is the only cut that touches exactly `src/`. `enforce: 'post'` puts it after
// the TS/JSX transform, so it sees plain ESM; esbuild's minifier still runs over the bundled result.
//
// The options are the "low cost" tier. Deliberately OFF: controlFlowFlattening and deadCodeInjection
// (2-3x size and measurable runtime slowdown), selfDefending/debugProtection/disableConsoleOutput (the
// owner declined anti-debug), renameGlobals/renameProperties (break ES-module exports and the API's JSON
// shapes), and stringArrayWrappers' extra call layers beyond one.
const OPTIONS: ObfuscatorOptions = {
  sourceType: 'module',
  // A FIXED seed: unseeded, rotate/shuffle differ per run, so every app chunk got a new content hash on
  // every release and returning users re-downloaded all of it. Seeded, an unchanged module obfuscates
  // byte-identically and its chunk stays cached.
  seed: 4620,
  target: 'browser',
  compact: true,
  identifierNamesGenerator: 'mangled',
  renameGlobals: false,
  simplify: true,
  numbersToExpressions: false,
  splitStrings: false,
  stringArray: true,
  // Module specifiers MUST stay literals: Rollup code-splits on `import('./pages/X')` and resolves it at
  // build time, so a specifier moved into the string array silently folds every lazy route into one
  // chunk and leaves a runtime import of an unbuilt path. Every runtime dynamic import in `src/` is
  // relative, so path-shaped strings are the whole set.
  reservedStrings: ['^\\.{1,2}/'],
  stringArrayThreshold: 1,
  stringArrayEncoding: ['base64'],
  stringArrayRotate: true,
  stringArrayShuffle: true,
  stringArrayIndexShift: true,
  stringArrayWrappersCount: 0,
  stringArrayWrappersType: 'variable',
  transformObjectKeys: false,
  unicodeEscapeSequence: false,
  controlFlowFlattening: false,
  deadCodeInjection: false,
  selfDefending: false,
  debugProtection: false,
  disableConsoleOutput: false,
  sourceMap: false,
}

// THIS app's `src/` only — anchored to the config's own directory, so no other `/src/` path qualifies.
const APP_SOURCE_DIR = decodeURIComponent(new URL('./src/', import.meta.url).pathname)
const SCRIPT = /\.[jt]sx?$/

// The obfuscator is IMPORTED only when a build opts in, so a dev server or a dev-image build never loads
// a module of its (sizeable, build-time-only) dependency tree at all — the flag scopes where the package
// RUNS, not merely what it transforms.
export function obfuscatePlugin(enabled: boolean): Plugin {
  let obfuscate: ((code: string) => string) | undefined
  return {
    name: 'obfuscate-app-source',
    apply: 'build',
    enforce: 'post',
    async buildStart() {
      if (!enabled) return
      const { default: JavaScriptObfuscator } = await import('javascript-obfuscator')
      obfuscate = code => JavaScriptObfuscator.obfuscate(code, OPTIONS).getObfuscatedCode()
    },
    transform(code, id) {
      if (!obfuscate) return null
      const path = id.split('?')[0]
      if (!path.startsWith(APP_SOURCE_DIR) || !SCRIPT.test(path)) return null
      return { code: obfuscate(code), map: null }
    },
  }
}
