/* The few Sentry exports `lib/errorTracking.ts` uses, imported by name so the lazy chunk carries only them (a
 * namespace import of `@sentry/browser` keeps replay and feedback in it). Loaded only when a DSN is set. */
export {
  close,
  dedupeIntegration,
  eventFiltersIntegration,
  flush,
  functionToStringIntegration,
  globalHandlersIntegration,
  init,
  linkedErrorsIntegration,
} from '@sentry/browser'
