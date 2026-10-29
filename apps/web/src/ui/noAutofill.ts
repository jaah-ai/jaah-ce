/* THE SEARCH-BOX OPT-OUT FROM PASSWORD MANAGERS. Spread into a search/filter/lookup input's `htmlInput`
 * slot — every Autocomplete picker's `renderInput` included, after `params.slotProps.htmlInput` — so a
 * password manager never takes it for a login field. One input without it is enough for LastPass to fill
 * (the opt-out once reached only the shared search components, not the pickers). LastPass filled
 * the saved email into the Credentials page's Search box: a bare text input on a page named "credentials" reads as a
 * username field to its heuristics, and `autocomplete="off"` alone does not stop it — each manager honours
 * only its own marker, so every one is spelled here:
 *   data-lpignore   — LastPass
 *   data-1p-ignore  — 1Password
 *   data-bwignore   — Bitwarden
 *   data-form-type  — Dashlane ("other" = not a login/identity form)
 * `autoComplete: 'off'` keeps the browser's own autofill out too. Search boxes only: a real credential
 * field (a password, a token) keeps `new-password`, which is what a manager SHOULD see. */
export const NO_AUTOFILL_INPUT = {
  autoComplete: 'off',
  'data-lpignore': 'true',
  'data-1p-ignore': 'true',
  'data-bwignore': 'true',
  'data-form-type': 'other',
} as const
