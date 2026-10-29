"""The AI quality review's and the weekly digest's constants, import-free so `app/models/` can read them.

The review's writer is `review.py`, its reader `quality.py`; the digest's writer and reader are `digest.py`.
"""
from __future__ import annotations

OUTCOME_REVIEWS_TABLE = "outcome_reviews"

#: The weekly system cron that writes the reviews (`app/cron_system_activities.py`).
OUTCOME_REVIEW_ACTIVITY = "outcome-review"
#: Mondays 05:00. A `cron` row is evaluated in the control plane's `cron_timezone`; set that to UTC
#: for 05:00 UTC. The sample window is the 7 days before the firing whenever it lands.
OUTCOME_REVIEW_CRON_EXPRESSION = "0 0 5 * * 1"

#: The scores and the reason bound (`ck_outcome_reviews_score`, `ck_outcome_reviews_reason`).
SCORES: tuple[int, ...] = (1, 2, 3, 4, 5)
REASON_MAX_CHARS = 200

INSIGHT_DIGESTS_TABLE = "insight_digests"
#: The weekly system cron that writes the digest (`app/cron_system_activities.py`).
INSIGHT_DIGEST_ACTIVITY = "insight-digest"
#: Mondays 06:00 (the CP's `cron_timezone`, as above), an hour after the review it reads.
INSIGHT_DIGEST_CRON_EXPRESSION = "0 0 6 * * 1"
#: A digest's statuses (`ck_insight_digests_status`) and bounds.
DIGEST_STATUSES: tuple[str, ...] = ("ok", "failed")
DIGEST_MAX_BULLETS = 10
DIGEST_BULLET_MAX_CHARS = 160
