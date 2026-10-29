// Shared quota threshold + helpers (R4/R5 NFR Consistency): the ≥80% "high" boundary lives here once
// (Accounts cell colour, Worktrees warning badge, Launch-Console picker). It MIRRORS the CP gate's
// DEFAULT_HIGH_PCT in apps/api/app/jaah_scheduler.py — an intentional two-file mirror (different
// runtime layer, same number), not duplication to collapse.
export const QUOTA_HIGH_PCT = 80

// A confirmed-rejected OAuth token (agent main.py _read_account_quota: status=auth_rejected, NULL percents).
// Not high usage: it needs re-login. A distinct "re-login" badge (Accounts' QuotaCell), never a
// red bar or a quota-high warning (Worktrees).
export const isAuthRejected = (
  q?: { status?: string | null } | null,
) => q?.status === 'auth_rejected'

// The collector cannot refresh this token in a way retrying fixes (edge 403, moved endpoint). Not a
// re-login problem — our reader is broken. Distinct badge; last-known percents still render.
export const isRefreshBlocked = (
  q?: { status?: string | null } | null,
) => q?.status === 'refresh_blocked'

// Older than this reads as stale: several collector cycles, so one missed tick stays quiet. Mirrors
// the node daemon's stale-reading cutoff (an intentional two-file mirror, like QUOTA_HIGH_PCT).
export const QUOTA_STALE_MS = 30 * 60 * 1000

// Age of the reading in ms, or null without a usable timestamp (pre-`observedAt` record, or never read).
export const quotaAgeMs = (
  q?: { observedAt?: string | null } | null,
  now: number = Date.now(),
): number | null => {
  if (!q?.observedAt) return null
  const stamped = Date.parse(q.observedAt)
  if (Number.isNaN(stamped)) return null
  return Math.max(0, now - stamped)
}

// null pct (incl. auth_rejected's NULL percents) yields '' — a neutral cell, never the quota-high class;
// the auth badge is the cell's.
export const quotaClass = (pct: number | null | undefined) =>
  pct == null ? '' : pct >= QUOTA_HIGH_PCT ? 'quota-high' : pct >= 50 ? 'quota-mid' : 'quota-low'

export const isQuotaHigh = (
  q?: { fiveHourPercent: number | null; weeklyPercent: number | null; overQuotaSpendPercent?: number | null; status?: string | null } | null,
) =>
  // Exclude auth_rejected first: a rejected account CAN carry real percentages (siblings' measurements are
  // kept), and "quota high" would name the wrong fix.
  !!q && !isAuthRejected(q) && (
    (q.fiveHourPercent ?? 0) >= QUOTA_HIGH_PCT
    || (q.weeklyPercent ?? 0) >= QUOTA_HIGH_PCT
    || (q.overQuotaSpendPercent ?? 0) >= QUOTA_HIGH_PCT
  )
