// The web app's own version, embedded at build time by Vite (see vite.config.ts `define`).
// THE repo version — the same string the api and jaahd report, not a web-only number.
// Resolution order (single home: the repo's version script): APP_VERSION env → the ROOT VERSION file → "0.0.0".
declare const __APP_VERSION__: string

export const APP_VERSION: string = __APP_VERSION__
