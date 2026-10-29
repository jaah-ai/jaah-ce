import { LEGACY_CLI_SESSION_ENGINE } from './providerCatalogue'

/* Where a session id links to. Only a legacy-CLI run of the default provider has a claude.ai session (viewable by
 * the account that ran it); every other engine — and a row that names none — opens the console's own Audit › Sessions ledger,
 * filtered to that id. Never send a run claude.ai cannot show to claude.ai. */
export function sessionHref(sessionId: string, engine?: string | null): { href: string; external: boolean } {
  if (engine === LEGACY_CLI_SESSION_ENGINE) {
    return { href: `https://claude.ai/code/${encodeURIComponent(sessionId)}`, external: true }
  }
  return { href: `/sessions?q=${encodeURIComponent(sessionId)}`, external: false }
}
