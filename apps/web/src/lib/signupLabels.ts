import type { OrgSignup } from '../types'

/* A self-service sign-up's enumerated values, in the words every surface shows them with (the org approval card, a
 * `user_registered` automation source's toggles and review). One home; do not re-add a local copy. */
export const SIGNUP_PROVIDER_LABELS: Record<OrgSignup['provider'], string> = {
  google: 'Google',
  github: 'GitHub',
  microsoft: 'Microsoft',
  email: 'Email',
}

export const SIGNUP_ACCOUNT_TYPE_LABELS: Record<OrgSignup['account_type'], string> = {
  individual: 'Individual',
  organization: 'Organization',
}
