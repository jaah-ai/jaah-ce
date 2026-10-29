/* A legal document's link (Terms, Privacy), opened in a new tab; its bare label without an address. Shared by sign-up
 * (`pages/signup/orgApplication.tsx` re-exports it) and the Terms gate, which ships where sign-up does not. */
import Link from '@mui/material/Link'

export const legalLink = (href: string | null, label: string) =>
  href ? (
    <Link href={href} target="_blank" rel="noopener noreferrer" underline="always" aria-label={`${label} (opens in a new tab)`}>
      {label}
    </Link>
  ) : (
    label
  )
