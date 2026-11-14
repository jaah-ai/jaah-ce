/* A legal document's link (Terms, Privacy), opened in a new tab; its bare label without an address. Shared by sign-up
 * (`pages/signup/orgApplication.tsx` re-exports it) and the Terms gate, which ships where sign-up does not. */
import ExternalLink from './ExternalLink'

export const legalLink = (href: string | null, label: string) => (
  <ExternalLink href={href} underline="always" aria-label={`${label} (opens in a new tab)`}>{label}</ExternalLink>
)
