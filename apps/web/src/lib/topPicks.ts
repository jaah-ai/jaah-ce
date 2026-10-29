/* What a create wizard pre-picks: the first TOP_PICKS eligible rows of a list the server returned
   busiest first (`?order=activity`, `apps/api/app/activity_rank.py`). The operator removes any before Create. */
export const TOP_PICKS = 5

export function topPicks<T>(rows: readonly T[], eligible: (row: T) => boolean): T[] {
  return rows.filter(eligible).slice(0, TOP_PICKS)
}
