/* THE FIRST THING THE CONSOLE RUNS (imported first by `main.tsx`). A redirect-based payment method sends the
 * browser back with Stripe's `setup_intent_client_secret` in the address; left there, every request the app
 * makes carries it as the Referer, and the address bar and history keep it. It is removed before any request
 * is made. Nothing reads it: the Billing page needs only `setup_intent` and `redirect_status`. The document
 * request itself is redacted in nginx's access log (`nginx.conf.template`, `console_redacted`). */
const SECRET_PARAM = 'setup_intent_client_secret'

export function scrubRedirectSecret(location: Location = window.location, history: History = window.history): void {
  const url = new URL(location.href)
  if (!url.searchParams.has(SECRET_PARAM)) return
  url.searchParams.delete(SECRET_PARAM)
  history.replaceState(history.state, '', `${url.pathname}${url.search}${url.hash}`)
}

scrubRedirectSecret()
