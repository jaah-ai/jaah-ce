import { useAuth } from '../auth'

/* ── A pending self-service org (awaiting operator approval) ──
The single home for how the console reads `Admin.org.lifecycle` and what it says about it. The API
refuses every run- or capacity-bearing write while pending (403, same words as `AWAITING_APPROVAL`);
the console disables those controls with this reason rather than hiding them. Never render the raw
lifecycle value. */

/** Why a control is disabled while the viewer's org is pending. */
export const AWAITING_APPROVAL = 'Available after approval'

/** The shell's persistent notice while pending (`components/PendingOrgBanner.tsx`). It names only what the guided
    setup lets a pending org do: Connect Git and Add a project wait behind Add node there (`components/onboarding`). */
export const PENDING_ORG_BANNER =
  'Your organization is awaiting approval — you can choose a plan and invite your team meanwhile.'

/** True while the viewer's own org awaits approval. Absent org or lifecycle reads as not pending. */
export function useOrgPending(): boolean {
  return useAuth().admin?.org?.lifecycle === 'pending'
}

/** `AWAITING_APPROVAL` while pending, else undefined — the shape a `title=`/`disabledReason` wants. */
export function useAwaitingApprovalReason(): string | undefined {
  return useOrgPending() ? AWAITING_APPROVAL : undefined
}
