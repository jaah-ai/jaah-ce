/* THE API's CAP ON `q`: 200 characters, a 422 above it (`app/routes/audit.py` and the command log share
 * the bound). The audit tabs' Search fields carry it as `maxLength`, and that `maxLength` IS THE CONTRACT,
 * not a nicety — a 422 surfaces as "the request failed" over stale rows, so a pasted log line would
 * silently stop the operator's search from ever running. One home, because two tabs read it. */
export const Q_MAX_LEN = 200
/* Activity's floor on `q` (`_Q_MIN_LEN` in `app/routes/audit.py`, a 422 below it): shorter text cannot use the
 * trigram index. Its box offers no search under it, and a URL's shorter `q` is dropped. */
export const Q_MIN_LEN = 3
