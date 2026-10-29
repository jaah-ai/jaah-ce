import type { ProviderCapabilities, ProviderChoice } from '../types'
import { providerOn } from './providerGrid'

/* ── QUESTIONS ABOUT THE PROVIDER CATALOGUE, ANSWERED FROM THE LISTING ──────────────────────────────────────
 * `GET /api/accounts/providers` is the web's one source of which providers exist, their order and what each can
 * do (`apps/jaahd/agent_providers.py`). Nothing else names a provider — the one exception is the default provider
 * below, mirroring `apps/jaahd/agent_providers.py::DEFAULT_PROVIDER`, which a pre-provider row implies — so a new
 * catalogue row reaches every picker without a web change. */

/** The default provider's catalogue value (`agent_providers.py::DEFAULT_PROVIDER`): what a row naming none is. */
export const DEFAULT_PROVIDER = 'claude'

/** What a provider grid stored nowhere is (`app/provider_auth_policy.py::SEED`): the default provider with API key,
 *  mirrored so `(default)` storing none shows what its orgs get. */
export const PROVIDERS_SEED: readonly (readonly [string, string])[] = [[DEFAULT_PROVIDER, 'api_key']]

/** Is this login the default provider's? Read by copy that names the default provider's token and quota. How a
 *  login FINISHES is never keyed on the name — every provider declares it (`declaredPasteMode`).
 *  It takes the value, not the resolved catalogue row: keyed on the row, an unresolvable row (failed
 *  `GET /api/accounts/providers`, unknown value) got the shared-volume warning. A re-login and an enrolment read
 *  the value off the account's record. No value at all ⇒ the default (the pre-provider page). For the default
 *  provider's own copy (its token, its BAA), never to filter a provider list. */
export function isDefaultProvider(value?: string): boolean {
  return !value || value === DEFAULT_PROVIDER
}

/** The HIPAA non-BAA surface the default provider's subscription is (`app/hipaa.py::SURFACES`). */
export const DEFAULT_SUBSCRIPTION_SURFACE = 'claude_subscription'

/** The `session_engine` (`apps/api/app/routes/tasks/activity.py`) of a session whose transcript lives on the
 *  vendor's own site — the default provider's legacy CLI run. */
export const LEGACY_CLI_SESSION_ENGINE = 'claude_code'

/** A row's capability groups; `{}` from an older server that serves none. */
export function capabilitiesOf(row: ProviderChoice): ProviderCapabilities {
  return row.capabilities ?? {}
}

/** The provider an account with none chosen gets: the catalogue's first row (`PROVIDERS` is default-first).
 *  `''` before the listing lands — a writer then omits `provider` and the server applies its own default. */
export function defaultProviderOf(rows: readonly ProviderChoice[]): string {
  return rows[0]?.value ?? ''
}

/** The rows the org's provider grid offers (`lib/providerGrid`: any cell of the row on). Presentation only: the
 *  API refuses an off provider on its own. */
export function offeredOnPlan(rows: readonly ProviderChoice[], grid: Parameters<typeof providerOn>[0]): ProviderChoice[] {
  return rows.filter(row => providerOn(grid, row.value))
}

/** The rows New account hides because the plan leaves out every auth kind they have: the API's `allowed_auth_kinds`
 *  already folds the grid, so an empty list is the "hidden" fact. */
export function hiddenProviders(rows: readonly ProviderChoice[]): ProviderChoice[] {
  return rows.filter(row => row.allowed_auth_kinds !== undefined && row.allowed_auth_kinds.length === 0)
}
