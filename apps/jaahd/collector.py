"""The node's account/quota probe. SECURITY-SENSITIVE — this module performs live OAuth token
refresh + atomic credential rewrites.

Writes no file of its own. Stdlib only at import apart from jaahd-local helpers. The account set,
`enabled` and `slug` come from the DB SSOT via `control_plane_api.agent_api_accounts()`.
"""
from __future__ import annotations

import json
import logging
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import agent_providers
from agents import claude as default_cli  # the default provider's adapter: its legacy-runtime facts
import environment
from jaah_env import env_get  # renamed env vars
import host_platform
import ce_caps  # the community edition's account cap (a no-op on a cloud node)
import provider_health
import runner_credentials
import session_user
import shared_storage
import env_helpers
from env_helpers import _norm_dir

log = logging.getLogger("jaahd.collector")


# ── Live OAuth quota chain ──────────────────────────────────────────────────────

# The default provider's endpoints and ids (the adapter's notes say what is measured and what is not),
# re-bound here so every function reads them as module globals at call time.
_OAUTH_API = default_cli.OAUTH_API
_OAUTH_BETA_HEADER = default_cli.OAUTH_BETA_HEADER
_OAUTH_BETA = default_cli.OAUTH_BETA
_OAUTH_TIMEOUT = 8.0
# Refresh-token grant: if it changes a refresh fails and we fall back to re-login.
_OAUTH_TOKEN_URL = default_cli.OAUTH_TOKEN_URL
_OAUTH_CLIENT_ID = default_cli.OAUTH_CLIENT_ID
# Every OAuth request sends this: the token host's edge rejects urllib's default agent (`OAUTH_TOKEN_URL`'s
# note). An honest own-name agent — never impersonate the default CLI. The API probes send it too.
_OAUTH_USER_AGENT = "jaahd-collector/1.0"
# The token file's OAuth block, and the account home's cached-identity file and block.
_CREDENTIAL_BLOCK = default_cli.CREDENTIAL_BLOCK
_ACCOUNT_CONFIG_FILE = default_cli.ACCOUNT_CONFIG_FILE
_ACCOUNT_CONFIG_BLOCK = default_cli.ACCOUNT_CONFIG_BLOCK


def _now_iso() -> str:
    """This node's clock as ISO-8601 UTC — the named single home for a quota block's `observedAt`.

    Only a block that ASSERTS a percentage is dated: it REPLACES the CP's stored reading, and an undated
    replacement cannot be aged. A verdict block MERGES and has nothing to date; verdicts expire on
    `account_fold.ACCOUNT_VERDICT_TTL_SECONDS` / `accounts.quota_status_until`, a clock the node must not
    fake. One caller (`_live_quota`), named anyway so a new numeric block does not forget it.

    Not `env_helpers.now_iso`: that is second-precision `Z`, while this `isoformat()` (microseconds,
    `+00:00`) is the format on the wire, and `reconcile` is too heavy to import here."""
    return datetime.now(timezone.utc).isoformat()


def _tier_label(tier_raw: str | None, seat: str | None = None) -> str | None:
    """A `rate_limit_tier` ending `max_5x` → 'Max 5×'; else seat tier or the raw value."""
    m = re.search(r"max_(\d+)x", tier_raw or "")
    return f"Max {m.group(1)}×" if m else (seat or tier_raw or None)


def _oauth_get(token: str, path: str) -> dict:
    """GET an OAuth-scoped endpoint of the default provider's API with an account's access token."""
    req = urllib.request.Request(
        _OAUTH_API + path,
        headers={
            "Authorization": f"Bearer {token}",
            _OAUTH_BETA_HEADER: _OAUTH_BETA,
            "User-Agent": _OAUTH_USER_AGENT,
        },
    )
    return json.loads(urllib.request.urlopen(req, timeout=_OAUTH_TIMEOUT).read())


def _live_account(token: str) -> dict:
    """Authoritative identity from the live token — never the cache-prone account config file."""
    prof = _oauth_get(token, "/api/oauth/profile")
    a = prof.get("account", {}) or {}
    o = prof.get("organization", {}) or {}
    return {
        "email": a.get("email"),
        "display_name": a.get("display_name") or a.get("full_name"),
        "org": o.get("name"),
        "tier": _tier_label(o.get("rate_limit_tier"), o.get("seat_tier")),
        "status": "connected",
    }


def _live_quota(token: str) -> dict:
    """Per-account rate-limit utilization + reset times from the live token.

    `overQuotaSpendPercent` is a different axis: the org's extra usage-based spend cap (`spend.percent`),
    which applies only after the plan's 5h/weekly quotas and can read 100 while those read healthy. No
    `resets_at` (it rolls with the billing cycle). Trusted only when `spend.enabled`: the uncapped shape
    is unverified, and a stray percent there would exclude every account in the org fleet-wide."""
    u = _oauth_get(token, "/api/oauth/usage")

    def pct(d: object) -> int | None:
        return round(d["utilization"]) if isinstance(d, dict) and d.get("utilization") is not None else None

    def at(d: object) -> str | None:
        return d.get("resets_at") if isinstance(d, dict) else None

    fh, sd, ss = u.get("five_hour"), u.get("seven_day"), u.get("seven_day_sonnet")
    spend = u.get("spend")
    over_quota_spend_pct = spend.get("percent") if isinstance(spend, dict) and spend.get("enabled") else None
    return {
        # When these percentages were READ. Required here only: this block asserts numbers, so it
        # REPLACES what the CP stored and must be ageable. The verdict sentinels carry no date (`_now_iso`).
        "observedAt": _now_iso(),
        "fiveHourPercent": pct(fh), "fiveHourResetsAt": at(fh),
        "weeklyPercent": pct(sd), "weeklyResetsAt": at(sd),
        "sonnetWeeklyPercent": pct(ss), "sonnetWeeklyResetsAt": at(ss),
        "overQuotaSpendPercent": round(over_quota_spend_pct) if isinstance(over_quota_spend_pct, (int, float)) else None,
    }


def _cached_account(auth_dir: Path) -> dict | None:
    """Fallback identity from the cached `_ACCOUNT_CONFIG_BLOCK` (can lag a re-login)."""
    config_json = auth_dir / _ACCOUNT_CONFIG_FILE
    if not config_json.exists():
        return None
    try:
        oauth = (json.loads(config_json.read_text()).get(_ACCOUNT_CONFIG_BLOCK) or {})
        if not oauth:
            return None
        return {
            "email": oauth.get("emailAddress"),
            "display_name": oauth.get("displayName"),
            "org": oauth.get("organizationName"),
            "tier": _tier_label(oauth.get("userRateLimitTier"), oauth.get("seatTier")),
            "status": "connected",
        }
    except Exception as exc:
        log.warning("resources: %s read failed for %s: %s", _ACCOUNT_CONFIG_FILE, auth_dir, exc)
        return None


# Sentinel quota for a confirmed-rejected account: NULL percents, not all-100 (indistinguishable from real
# exhaustion). Consumers exclude auth_rejected explicitly (selector, self-heal, "re-login" in the UI).
#
# The explicit nulls are load-bearing: this block MERGES (`fleet_observe.account_upsert_statement`), and
# naming every percentage null is what COLLAPSES stale pre-rejection numbers — the opposite of
# `_refresh_blocked_quota`, which omits them so they survive. Undated (no numbers). Build it through
# `_auth_rejected_quota`, which copies it.
_AUTH_REJECTED_QUOTA: dict = {
    "status": "auth_rejected",
    "fiveHourPercent": None, "fiveHourResetsAt": None,
    "weeklyPercent": None, "weeklyResetsAt": None,
    "sonnetWeeklyPercent": None, "sonnetWeeklyResetsAt": None,
    "overQuotaSpendPercent": None,
}

# Sentinel for a refresh STRUCTURALLY blocked (403 edge refusal, 404 moved endpoint) as opposed to a
# 5xx/429 worth the next cycle. Not auth_rejected: the refresh token is not proven dead. Its own state so
# the refusal is visible rather than retried silently. No percent keys: it MERGES, so the previous DATED
# percentages survive beneath the flag server-side. Undated — a stamp would overwrite the reading's own.
def _refresh_blocked_quota() -> dict:
    """A fresh `refresh_blocked` block: the verdict alone, undated, with no percent keys.

    A function so no use site can forget to copy."""
    return {"status": "refresh_blocked"}

# WHY the token was rejected — three causes, three fixes, so the cause must survive into the badge. A
# semantic word, not the HTTP code (a 401 from /profile and /usage mean the same). `status` stays
# `auth_rejected` for existing consumers.
_REJECTED_TOKEN = "token"      # revoked / expired / dead refresh token → a re-login DOES fix it
_REJECTED_BILLING = "billing"  # 402 payment required → paying does; re-login does not
_REJECTED_SEAT = "seat"        # 403 forbidden, suspended seat → an admin does; re-login does not
_REJECTED_REASON_BY_CODE = {401: _REJECTED_TOKEN, 402: _REJECTED_BILLING, 403: _REJECTED_SEAT}
# Not a rejection at all: this node found no credential it could read, so nothing was ever sent to the provider.
# Kept an `auth_rejected` block so every consumer still excludes the account; the console says it cannot read
# the credential, and where it looked (`CREDENTIAL_PATH_KEY`), never "token rejected".
_REJECTED_UNREADABLE = "unreadable"
CREDENTIAL_PATH_KEY = "credentialPath"


def _auth_rejected_quota(reason: str) -> dict:
    """A fresh `auth_rejected` block tagged with why; copied per call, undated, every percent key an
    explicit null (see the constant's comment)."""
    return dict(_AUTH_REJECTED_QUOTA, authRejectedReason=reason)

# Token-endpoint statuses no retry can clear. 400 is handled first as the dead refresh token; every other
# code stays "transient".
_REFRESH_BLOCKED_CODES = (403, 404)


def _unreadable_credential_quota(cred: Path) -> dict:
    """The `auth_rejected` block for an account whose credential this node could not read at all, at CRED."""
    return dict(_auth_rejected_quota(_REJECTED_UNREADABLE), **{CREDENTIAL_PATH_KEY: str(cred)})


def _access_token_expired(creds: dict) -> bool:
    """True when the stored access token is not usable for a probe — `expiresAt` missing/non-numeric,
    or within a 60s clock-skew guard of now. Missing counts as expired: the refresh ladder is safe either way."""
    expires_at = creds.get("expiresAt")
    return not isinstance(expires_at, (int, float)) or expires_at <= int(time.time() * 1000) + 60_000


def _cred_path(auth_dir: Path, securestorage_dir: str = "") -> Path:
    """This account's token file — the token store's when the store holds one, `<auth_dir>/.credentials.json`
    otherwise (`shared_storage.token_dir`, the single home of that redirection).

    `securestorage_dir` is the store `_collect_resources` resolved for this pass
    (`shared_storage.securestorage_dir`), `token_dir`'s rung 1, so the read, the locked re-read and the
    rewrite all ask the one answer taken up front.

    One helper, because the read, the locked re-read and the rewrite must agree — else a rotated pair is
    dropped (the adapter's `CLI_REFRESH_LOCK` note). Only the token moves: the account config and the collector's
    flock stay per-node (the CLI's refresh lock sits beside the token, `_take_cli_refresh_lock`).
    Ownership is not checked: through the access point every writer is uid 3000."""
    return Path(shared_storage.token_dir(str(auth_dir), securestorage_dir)) / ".credentials.json"


def _read_oauth_block(auth_dir: Path, securestorage_dir: str = "") -> dict:
    """The `_CREDENTIAL_BLOCK` as it is ON DISK RIGHT NOW, or `{}` when unreadable.

    A snapshot's refresh token may have been rotated by a CLI before we took its lock,
    so it may already be consumed and must never be sent. `{}` is safe: callers treat no refresh token as nothing to
    do. `securestorage_dir` must match the caller's, or a rewrite reads one file and writes another."""
    try:
        full = json.loads(_cred_path(auth_dir, securestorage_dir).read_text())
        block = full.get(_CREDENTIAL_BLOCK) if isinstance(full, dict) else None
        return block if isinstance(block, dict) else {}
    except Exception:
        return {}


# The default CLI's OWN refresh locks — the current one and its legacy sibling, as measured (the adapter's
# `CLI_REFRESH_LOCK` note). They sit beside the token, so on the EFS share every node's CLI takes the SAME
# locks — and a refresh that skips them is the one that replays a spent refresh token. `mkdir` is atomic
# on NFSv4.
_CLI_REFRESH_LOCK = default_cli.CLI_REFRESH_LOCK
# The CLI's stale window: past it the CLI takes a lock over, so it is no longer ours to remove — and a
# lock that old is one the collector takes over too (`_mkdir_or_take_over_stale`).
_CLI_REFRESH_LOCK_STALE_SECONDS = default_cli.CLI_REFRESH_LOCK_STALE_SECONDS
# Takeover waits longer than the CLI would: a lock's mtime is the HOLDER's clock, so the margin keeps a node
# whose clock runs ahead from reading a live, heartbeated lock as abandoned. Nothing is lost by waiting.
_CLI_REFRESH_LOCK_TAKEOVER_SECONDS = _CLI_REFRESH_LOCK_STALE_SECONDS + 30.0
# After a takeover, how long to wait before checking the lock is still ours: a second taker that judged the
# same lock stale removes and re-creates it inside this window (the remove-then-mkdir race), so a changed
# identity means two takers — back off rather than both refreshing one single-use token.
_CLI_REFRESH_LOCK_SETTLE_SECONDS = 1.0
# Our lock carries no heartbeat, so it must not be relied on near the stale window: past this age (a hung
# EFS read) the grant is not sent at all, since another node may be about to take the lock over.
_CLI_REFRESH_LOCK_SEND_DEADLINE_SECONDS = _CLI_REFRESH_LOCK_STALE_SECONDS / 2


def _cli_refresh_lock_paths(token_dir: Path) -> tuple[Path, Path]:
    """The CLI's current and legacy refresh lock for the token in `token_dir`, in the CLI's own order."""
    return token_dir / _CLI_REFRESH_LOCK, token_dir.parent / f"{token_dir.name}.lock"


def _take_cli_refresh_lock(token_dir: Path) -> list[tuple[Path, tuple[int, int], float]] | None:
    """Take the CLI's refresh locks for `token_dir` — `[(lock, identity, taken_at), ...]` for
    `_release_cli_refresh_lock` — or None when either is held live. Any other `OSError` (permissions, a
    stale NFS handle) is RAISED, after releasing what was taken: it is a fault, not a busy lock.

    A live lock means skip this cycle. A STALE one — mtime older than `_CLI_REFRESH_LOCK_TAKEOVER_SECONDS`,
    where a live CLI holder heartbeats every 5s — is taken over as proper-lockfile does: remove it, then one
    `mkdir` that steals nothing further, then a settle and an identity re-check so two takers of the same
    stale lock cannot both proceed. Without that, a refresher killed while holding the lock (once,
    a probe timed out after a dead-token 400, leaving `<store>.lock` on the share) wedges the account on
    every node until a CLI happens to refresh it — which never comes for a dead token, so its rejected
    verdict hid behind `no reading` for a day. Held for one HTTP round trip (`_OAUTH_TIMEOUT`, 8s),
    normally well inside the stale window, so it carries no mtime heartbeat — the release copes with the
    abnormal case instead."""
    held: list[tuple[Path, tuple[int, int], float]] = []
    took_over = False
    for lock in _cli_refresh_lock_paths(token_dir):
        try:
            took_over = _mkdir_or_take_over_stale(lock) or took_over
            held.append((lock, _lock_identity(lock), time.monotonic()))
        except FileExistsError:
            _release_cli_refresh_lock(held)
            return None
        except OSError:
            _release_cli_refresh_lock(held)
            raise
    if took_over:
        time.sleep(_CLI_REFRESH_LOCK_SETTLE_SECONDS)
        if not _still_ours(held):
            log.warning("refresh lock for %s was re-taken during a takeover — deferring", token_dir)
            _release_cli_refresh_lock(held)
            return None
    return held


def _mkdir_or_take_over_stale(lock: Path) -> bool:
    """`mkdir` the lock, taking over an abandoned one (proper-lockfile's stale rule). True when it took one
    over. Raises `FileExistsError` when the lock is held live (or a concurrent taker beat this one to the
    re-create), any other `OSError` when it cannot be taken at all."""
    try:
        lock.mkdir()
        return False
    except FileExistsError:
        try:
            age = time.time() - lock.stat().st_mtime
        except FileNotFoundError:
            age = None  # released between the mkdir and the stat: just try once more
        if age is not None and age < _CLI_REFRESH_LOCK_TAKEOVER_SECONDS:
            raise
    if age is not None:
        log.warning("refresh lock %s is %ds old, past the stale window — taking it over", lock, int(age))
        try:
            lock.rmdir()
        except FileNotFoundError:
            pass  # another taker removed it first; the mkdir below decides which of us has it
    lock.mkdir()
    return age is not None


def _still_ours(held: list[tuple[Path, tuple[int, int], float]]) -> bool:
    """Is every lock in `held` still the directory we created (same inode and mtime)?"""
    try:
        return all(_lock_identity(lock) == identity for lock, identity, _taken in held)
    except OSError:
        return False


def _lock_identity(lock: Path) -> tuple[int, int]:
    """(inode, mtime_ns) of a lock dir. Inode alone is not identity — a filesystem may hand a re-created
    dir the inode just freed (measured in the gate's container) — but a CLI's takeover re-creates the dir
    and then heartbeats its mtime every 5s, while nothing ever touches the mtime of ours."""
    st = lock.stat()
    return st.st_ino, st.st_mtime_ns


def _release_cli_refresh_lock(held: list[tuple[Path, tuple[int, int], float]]) -> None:
    """Remove each lock only while it is still OURS (same identity, inside the stale window). Past the CLI's stale window (a hung EFS write, a slow
    DNS lookup) a CLI may already have taken it over under the same name, and removing that would let a
    third refresher in; a lock we no longer own is left for its holder. Never raises."""
    for lock, identity, taken_at in held:
        if time.monotonic() - taken_at >= _CLI_REFRESH_LOCK_STALE_SECONDS:
            continue
        try:
            if _lock_identity(lock) == identity:
                lock.rmdir()
        except OSError:
            pass  # already gone — nothing of ours left to release


def _refresh_oauth_token(auth_dir: Path, creds: dict, securestorage_dir: str = "") -> tuple[str | None, str]:
    """Exchange the stored refresh token for a fresh access token via the OAuth refresh grant, then
    ATOMICALLY rewrite .credentials.json with the rotated pair. Returns (access_token, status):

      "ok"        — refreshed; the returned access token is already persisted, ready to probe with.
      "dead"      — HTTP 400 / invalid_grant: the refresh token itself is dead → caller flags
                    auth_rejected (true re-login).
      "blocked"   — a refusal no retry can clear: HTTP 403/404 (`_REFRESH_BLOCKED_CODES` — refused at the
                    edge before reaching the API, or a moved endpoint) or a 200 whose body is not JSON
                    (contract break). Caller keeps the last-known quota AND flags `refresh_blocked` so
                    the condition is visible instead of retried in silence forever.
      "locked"    — another refresher holds a lock (this node's flock, or a live CLI lock on any node):
                    skip this cycle, nothing sent.
      "transient" — a failure the next cycle may well clear (5xx, 429, 401, network,
                    timeout, or a 200 missing/invalid access_token or expires_in): caller keeps the
                    last-known quota, no flag, retries next cycle.

    Recomputed every cycle — no status is sticky. NEVER logs, raises, or surfaces a token value.
    `creds` is the full `_CREDENTIAL_BLOCK`; untouched fields are preserved.

    Refresh tokens are single-use, so every refresher of one token must be serialized: two refreshes race,
    the loser replays a spent token, and the next CLI to refresh is answered `invalid_grant` and wipes the
    token (`expiresAt: 0`) — every runner on that account then dies `Login expired`. Two locks, both
    non-blocking (held → skip this cycle): the node-local flock serializes this node's collector threads,
    and the CLI's own lock (`_take_cli_refresh_lock`) serializes the collector with every CLI — on every
    node, for a token in a mounted store, since that lock sits beside the token."""
    if not creds.get("refreshToken"):
        return None, "transient"

    slug = auth_dir.name
    # Per-account non-blocking flock: serializes this node's collector refreshes. Held → skip this cycle.
    locks_dir = env_helpers.fleet_locks_dir()
    locks_dir.mkdir(parents=True, exist_ok=True)
    lock_fh = open(locks_dir / f"token-refresh-{slug}.lock", "a+")
    cli_lock = None
    try:
        try:
            host_platform.lock(lock_fh, blocking=False)
        except OSError:
            return None, "locked"  # another refresh in flight → skip this cycle
        try:
            cli_lock = _take_cli_refresh_lock(_cred_path(auth_dir, securestorage_dir).parent)
        except OSError as exc:
            log.warning("refresh lock for %s could not be taken (%s) — retrying next cycle",
                        auth_dir, type(exc).__name__)
            return None, "transient"
        if cli_lock is None:
            return None, "locked"  # a CLI (on any node) is refreshing → skip this cycle

        # LOCK, THEN READ — never send a token we did not just read off disk. `creds` is a snapshot, and
        # the CLI may have rotated since; replaying a spent refresh token revokes the whole family. Unlike
        # the CLI we may DECLINE: if someone already refreshed, use their access token and send nothing.
        disk = _read_oauth_block(auth_dir, securestorage_dir)
        refresh_token = disk.get("refreshToken")
        if not refresh_token:
            # Rotated away and not replaced (or the file went unreadable) — nothing safe to send.
            return None, "transient"
        if refresh_token != creds.get("refreshToken"):
            fresh_access = disk.get("accessToken")
            if fresh_access and not _access_token_expired(disk):
                log.debug("oauth refresh skipped for %s — already refreshed by another process", auth_dir)
                return fresh_access, "ok"
            # Rotated but not yet usable: refresh from the current (freshly read) token.

        if time.monotonic() - min(taken for _lock, _identity, taken in cli_lock) > \
                _CLI_REFRESH_LOCK_SEND_DEADLINE_SECONDS:
            log.warning("refresh lock for %s held too long before the grant — not sending it", auth_dir)
            return None, "transient"

        body = urllib.parse.urlencode({
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": _OAUTH_CLIENT_ID,
        }).encode()
        req = urllib.request.Request(
            _OAUTH_TOKEN_URL, data=body,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent": _OAUTH_USER_AGENT,
            },
            method="POST",
        )
        try:
            raw = urllib.request.urlopen(req, timeout=_OAUTH_TIMEOUT).read()
        except urllib.error.HTTPError as exc:
            if exc.code == 400:
                # invalid_grant — the refresh token is dead. Log the CODE only, never the token.
                log.warning("oauth refresh dead (HTTP 400) for %s", auth_dir)
                return None, "dead"
            if exc.code in _REFRESH_BLOCKED_CODES:
                # Retrying cannot clear an edge block or moved endpoint: its own state.
                log.warning(
                    "oauth refresh BLOCKED (HTTP %s) for %s — no retry can clear this; the request is "
                    "being refused before it reaches the API (check the endpoint URL and User-Agent). "
                    "Quota for this account will not refresh until it is fixed.",
                    exc.code, auth_dir,
                )
                return None, "blocked"
            log.warning(
                "oauth refresh failed (HTTP %s) for %s — retrying next cycle; quota stays stale meanwhile",
                exc.code, auth_dir,
            )
            return None, "transient"
        except Exception as exc:
            log.warning(
                "oauth refresh failed (%s) for %s — quota stays stale until this clears",
                type(exc).__name__, auth_dir,
            )
            return None, "transient"

        try:
            resp = json.loads(raw)
            new_access = resp.get("access_token")
            expires_in = resp.get("expires_in")
        except Exception:
            # A non-JSON 200 is a contract break, not a blip.
            log.warning(
                "oauth refresh returned a non-JSON 200 for %s — the token endpoint contract changed; "
                "quota will not refresh until this is fixed", auth_dir,
            )
            return None, "blocked"
        if not new_access or not isinstance(expires_in, (int, float)) or expires_in <= 0:
            # A non-positive expires_in would write an already-expired token.
            log.warning(
                "oauth refresh response missing/invalid access_token/expires_in for %s — quota stays stale",
                auth_dir,
            )
            return None, "transient"

        # Preserve every existing field; refresh fields only when the response carried them. Merge onto
        # the file as it is NOW, not the snapshot: a CLI refresh inside the HTTP round trip wrote a newer
        # pair that building on `creds` would clobber.
        now_ms = int(time.time() * 1000)
        new_block = dict(_read_oauth_block(auth_dir, securestorage_dir) or creds)
        new_block["accessToken"] = new_access
        new_block["expiresAt"] = now_ms + int(expires_in * 1000)
        new_refresh = resp.get("refresh_token")
        if new_refresh:
            new_block["refreshToken"] = new_refresh
        rt_expires_in = resp.get("refresh_token_expires_in")
        if isinstance(rt_expires_in, (int, float)):
            new_block["refreshTokenExpiresAt"] = now_ms + int(rt_expires_in * 1000)

        # Persist the rotated pair before returning, preserving other top-level keys.
        cred_path = _cred_path(auth_dir, securestorage_dir)
        try:
            full = json.loads(cred_path.read_text())
            if not isinstance(full, dict):
                full = {}
        except Exception:
            full = {}
        full[_CREDENTIAL_BLOCK] = new_block
        try:
            # 0600 as the CLI itself writes it (widening is no fix), and the replace hands the file the
            # writer's uid, so only one uid may ever write here — `probe_account` runs this path as the
            # session user. `mkstemp`, never a pid-suffixed name: on the shared store two nodes' pid spaces
            # collide, and an interleaved write leaves an unparseable credential fleet-wide.
            env_helpers.atomic_write_json(cred_path, full, mode=0o600)
        except Exception as exc:
            log.debug("oauth refresh persist failed for %s: %s", auth_dir, type(exc).__name__)
            return None, "transient"
        return new_access, "ok"
    finally:
        if cli_lock is not None:
            _release_cli_refresh_lock(cli_lock)
        lock_fh.close()  # releases the flock


def _read_account_quota(
    auth_dir: Path, securestorage_dir: str = "", provider: str = "",
) -> tuple[dict | None, dict | None]:
    """Return (account, quota) for a pool auth dir. The live OAuth token is authoritative for both; the
    cached oauthAccount block is the fallback.

    A 401/402/403 on EITHER `/profile` or `/usage` marks `auth_rejected` (a revoked token can 401 on
    /profile while /usage 429s); a rejected profile skips the usage call. `securestorage_dir` as in
    `_cred_path`.

    One reader for every provider, `provider_health.observe`: a default-CLI home (`Capabilities.account_layer`
    "omc") comes back here through this module's OAuth ladder, every other through its own probe. Absent
    reads as the default. A community-edition node past its account cap probes nothing (`ce_caps`): `(None, None)`
    leaves the stored row alone."""
    if why := ce_caps.admit_account(config_dir=str(auth_dir), action="probe"):
        log.warning("account probe skipped: %s", why)
        return None, None
    return provider_health.observe(provider or agent_providers.DEFAULT_PROVIDER, auth_dir, securestorage_dir)


def claude_code_account_quota(auth_dir: Path, securestorage_dir: str = "") -> tuple[dict | None, dict | None]:
    """`(account, quota)` for a default-CLI home — `provider_health.observe`'s reader for the "omc" account
    layer: the provider's OAuth ladder, then the refresh token's own expiry."""
    account, quota = _claude_account_quota(auth_dir, securestorage_dir)
    if account is not None:
        # Read AFTER the ladder: a refresh this pass may have rotated the pair and moved the expiry.
        account["refresh_expires_at"] = _refresh_expires_iso(_read_oauth_block(auth_dir, securestorage_dir))
    return account, quota


def _refresh_expires_iso(creds: dict) -> str | None:
    """When the refresh token itself dies (`refreshTokenExpiresAt`, ms epoch) as ISO-8601 UTC — a hard
    lifetime from login that no refresh extends; only a re-login renews it. The CP warns before it passes.

    A timestamp, never a token. `None` for no numeric expiry, stated rather than omitted: the CP merges
    `extra` with `||`, so an absent key would keep a stale date across a re-login."""
    rt_expires_at = creds.get("refreshTokenExpiresAt")
    if not isinstance(rt_expires_at, (int, float)) or isinstance(rt_expires_at, bool) or rt_expires_at <= 0:
        return None
    try:
        return datetime.fromtimestamp(rt_expires_at / 1000, timezone.utc).isoformat()
    except (OverflowError, OSError, ValueError):
        return None


# Why a pass produced no quota, published as `account.no_reading_reason` so the page can say which
# `no reading` it is. ALWAYS published (None alongside a quota or a verdict): `extra` merges
# with jsonb `||`, so an omitted key would keep a stale reason forever. Accepted limit: a pass that
# publishes no record at all (no identity, a failed delegated probe) leaves the stored reason as it was.
NO_READING_REASON_KEY = "no_reading_reason"
NO_READING_REFRESH_LOCKED = "refresh_locked"  # another refresher holds the lock — retried next cycle
NO_READING_REFRESH_FAILED = "refresh_failed"  # token refresh failed transiently (5xx/429/network/...)
NO_READING_USAGE_FAILED = "usage_failed"  # token fine, the profile/usage calls failed transiently


def _claude_account_quota(auth_dir: Path, securestorage_dir: str = "") -> tuple[dict | None, dict | None]:
    """The OAuth ladder behind `_read_account_quota`: token read, refresh, live profile + usage."""
    account, quota, reason = _claude_account_quota_ladder(auth_dir, securestorage_dir)
    if account is not None:
        account[NO_READING_REASON_KEY] = reason if quota is None else None
    return account, quota


def _claude_account_quota_ladder(
    auth_dir: Path, securestorage_dir: str = "",
) -> tuple[dict | None, dict | None, str | None]:
    """The OAuth ladder itself, plus why it produced no quota (a `NO_READING_*`, else None) — the wrapper
    above publishes that reason."""
    token = None
    creds: dict = {}
    # Did this pass read a credential document at all? Without one nothing reached the provider, so no verdict
    # below may call the token rejected.
    read = False
    cred = _cred_path(auth_dir, securestorage_dir)
    if cred.exists():
        try:
            creds = json.loads(cred.read_text()).get(_CREDENTIAL_BLOCK, {}) or {}
            token = creds.get("accessToken")
            read = True
        except Exception as exc:
            log.debug("credentials read failed for %s: %s", auth_dir, type(exc).__name__)
    elif securestorage_dir:
        # The store held a token when this pass resolved it: say whether it is mounted, since a share that
        # dropped between the two reads is the usual cause.
        log.warning("account probe: %s has no credential at %s (token store %s)", auth_dir.name, cred,
                    "mounted" if shared_storage.share_mounted() else "not mounted")

    account: dict | None = None
    quota: dict | None = None

    # An expired or MISSING access token takes the refresh ladder before the probe: a lapsed token is not
    # rejected while its refresh token lives, and a token-less dir must reach a verdict rather than
    # `quota=None` forever (which let the CP keep whatever it last stored).
    if not token or _access_token_expired(creds):
        now_ms = int(time.time() * 1000)
        refresh_token = creds.get("refreshToken")
        rt_expires_at = creds.get("refreshTokenExpiresAt")
        # Dead only when refreshToken is falsy or its NUMERIC expiry has passed; a missing expiry is no
        # proof — the token endpoint decides.
        rt_dead = not refresh_token or (
            isinstance(rt_expires_at, (int, float)) and rt_expires_at <= now_ms
        )
        if rt_dead:
            # No refresh can succeed: auth_rejected, identity from the cached account config — and when no
            # credential was read at all, the reason says so, with where this node looked.
            verdict = _auth_rejected_quota(_REJECTED_TOKEN) if read else _unreadable_credential_quota(cred)
            return _cached_account(auth_dir), verdict, None
        new_token, status = _refresh_oauth_token(auth_dir, creds, securestorage_dir)
        if status == "dead":
            # 400 invalid_grant — the refresh token itself, so a re-login is exactly the fix.
            return _cached_account(auth_dir), _auth_rejected_quota(_REJECTED_TOKEN), None
        if status == "blocked":
            # Not dead — unreachable. No percentages (none read); the block merges, so the last dated
            # numbers survive server-side.
            return _cached_account(auth_dir), _refresh_blocked_quota(), None
        if status != "ok" or not new_token:
            # Transient: `quota=None` = "no first-hand reading this pass"; the CP's COALESCE keeps its
            # row. Preservation by absence, never "fine".
            reason = NO_READING_REFRESH_LOCKED if status == "locked" else NO_READING_REFRESH_FAILED
            return _cached_account(auth_dir), None, reason
        token = new_token  # refreshed OK → probe with the fresh token below (NOT auth_rejected)

    if token:
        # 401/402/403 is a confirmed rejection; any other HTTPError or error is transient.
        profile_reason = None
        try:
            account = _live_account(token)
        except urllib.error.HTTPError as exc:
            if exc.code in _REJECTED_REASON_BY_CODE:
                log.warning("live profile confirmed-rejected for %s: %s", auth_dir, exc)
                profile_reason = _REJECTED_REASON_BY_CODE[exc.code]
            else:
                log.debug("live profile transient HTTPError for %s: %s", auth_dir, exc)
        except Exception as exc:
            log.debug("live profile failed for %s: %s", auth_dir, exc)

        if profile_reason:
            # Dead token — skip the usage call entirely (can't be valid; a 429 there would mislead).
            quota = _auth_rejected_quota(profile_reason)
        else:
            try:
                quota = _live_quota(token)
            except urllib.error.HTTPError as exc:
                # Only 401/402/403 mean "rejected"; urlopen raises HTTPError for 5xx too, so a blanket
                # catch would read an upstream outage as every account dead.
                if exc.code in _REJECTED_REASON_BY_CODE:
                    log.warning("live usage confirmed-rejected for %s: %s", auth_dir, exc)
                    quota = _auth_rejected_quota(_REJECTED_REASON_BY_CODE[exc.code])
                else:
                    log.debug("live usage transient HTTPError for %s: %s", auth_dir, exc)
            except Exception as exc:
                log.debug("live usage failed for %s: %s", auth_dir, exc)

    if account is None:
        account = _cached_account(auth_dir)
    return account, quota, NO_READING_USAGE_FAILED if token and quota is None else None


# ── Which uid opens the account dir (the credential split, and its fix) ──────────
#
# Everything above opens and rewrites the account's token file; run as `jaahd` that split credentials by
# uid (the token write hands the file the writer's uid). So on a node with a session user, `probe_account`
# re-executes this module as `jaah` and reads back the finished records. The boundary is the WHOLE probe,
# not the refresh: only the non-secret identity/quota dicts cross the result spool, never a token. The
# ladder runs verbatim on the far side (the lock dir is 2775 `jaah:jaah`, so both uids serialize on it).

# Three 8s HTTP calls plus a refresh plus a `python3` cold start; half the `node_account_probe_interval_secs` floor.
_DELEGATED_PROBE_TIMEOUT = 60.0

# The provider crosses the delegation in the ENVIRONMENT, never argv, and never a secret.
#
# Exported unconditionally, default included — a security property: on this seam an unexported variable
# is whatever the fleet tmux server's global environment holds, which any runner can set, so a planted
# value would probe every default-provider account with the wrong adapter and take them out of rotation.
#
# Not argv: both positionals are CP-supplied strings, and a `securestorage_dir` literally equal to
# `--provider=<x>` would be parsed as the flag. The environment needs no parse, and leaves an ordinary
# account's argv structurally unchanged.
_PROVIDER_ENV = "JAAH_PROBE_PROVIDER"


def _delegated_probe_env(provider: str = "") -> dict:
    """The environment the delegated probe runs with — spelled out, NEVER `os.environ`.

    The daemon's environment carries `NODE_TOKEN`, which user `jaah` must never see. `JAAH_HOME`
    (for the refresh lock) is all this path needs; HOME/PATH are the seam's defaults. `provider` is always
    stated (see `_PROVIDER_ENV`); empty is spelled as the catalogue default."""
    # `pool_root_raw`: forward the value the environment actually carries.
    home = environment.pool_root_raw()
    env = {environment.POOL_ROOT_ENV: home} if home else {}
    env[_PROVIDER_ENV] = provider or agent_providers.DEFAULT_PROVIDER
    return env


def _own_this_dir(auth_dir: Path) -> bool:
    """Does THIS process own the account's credential dir, so it may read it and exec a vendor CLI against
    it without crossing a uid boundary? Absent is True (nothing to read); any other stat error is False —
    unproven ownership must not license the exec."""
    try:
        return os.stat(auth_dir).st_uid == os.geteuid()
    except (FileNotFoundError, NotADirectoryError):
        return True
    except OSError as exc:
        log.warning("account probe: %s could not be stat'ed (%s) — treating it as not ours",
                    auth_dir, type(exc).__name__)
        return False


def probe_account(
    auth_dir: Path, securestorage_dir: str = "", provider: str = "",
) -> tuple[dict | None, dict | None]:
    """`_read_account_quota` for one account, run AS THE SESSION USER when this host has one.

    Same `(account, quota)` contract as `_read_account_quota`. Without a usable switch (dev host, CI) it
    runs in-process — there is no second uid.

    Fails soft: a delegation that could not run or came back unparseable yields `(None, None)`, so
    `_collect_resources` skips the account and the CP's COALESCE leaves the stored row alone — it states
    nothing, since it cannot tell a broken spool from a dead credential. Logged at warning, and never an
    in-process fallback, which is the write this exists to stop.

    `securestorage_dir` crosses as a positional only when non-empty (an ordinary account's argv is
    unchanged); it is a path, never a secret. `provider` crosses in the environment (`_PROVIDER_ENV`)."""
    if not session_user.can_run_as_session_user():
        # A provider adapter must not run as the daemon uid: on a broken packaged node the fallback would
        # exec a vendor binary against a dir every runner can write, as `jaahd`. The test is OWNERSHIP of
        # the directory, not `switch_needed()`, which reads an unresolvable session user as "dev host".
        # An absent dir is allowed (adapters answer `no_credentials` without executing). Scoped to
        # providers whose runners are not the default CLI: the default provider's OAuth ladder executes nothing.
        if provider and not provider_health.has_account_reader(provider) and not _own_this_dir(auth_dir):
            log.warning(
                "account probe: refusing to run the %s adapter as uid %s against %s — the session-user "
                "switch is unavailable; check jaah-runner@<node>.service. Unobserved this pass.",
                provider, os.geteuid(), auth_dir)
            return None, None
        if runner_credentials.read_through_runner(_cred_path(auth_dir, securestorage_dir)):
            # A Windows NodeLink store is the runner account's alone: read here, a live token reads as none.
            log.warning("account probe: %s's credential is on this node's NodeLink, which only %r can read, "
                        "and the switch to it is unavailable — unobserved this pass.",
                        auth_dir.name, session_user.session_user())
            return None, None
        return _read_account_quota(auth_dir, securestorage_dir, provider)
    argv = [sys.executable, str(Path(__file__).resolve()), "--account-probe", str(auth_dir)]
    if securestorage_dir:
        argv.append(securestorage_dir)
    try:
        proc = session_user.run_as_session_user(
            argv, env=_delegated_probe_env(provider), timeout=_DELEGATED_PROBE_TIMEOUT)
    except Exception as exc:  # noqa: BLE001 — RuntimeError (switch gone), TimeoutExpired, OSError
        log.warning("account probe as %s failed to run for %s (%s) — quota stays stale this cycle",
                    session_user.session_user(), auth_dir, type(exc).__name__)
        return None, None
    if proc.returncode != 0:
        # stderr never carries a token; truncated against a runaway traceback.
        log.warning("account probe as %s exited %s for %s: %s", session_user.session_user(),
                    proc.returncode, auth_dir, (proc.stderr or "").strip()[-500:] or "(no stderr)")
        return None, None
    try:
        payload = json.loads(proc.stdout)
        account, quota = payload.get("account"), payload.get("quota")
    except Exception as exc:  # noqa: BLE001 — a non-JSON stdout is a contract break, not a probe result
        log.warning("account probe as %s returned unparseable output for %s: %s",
                    session_user.session_user(), auth_dir, type(exc).__name__)
        return None, None
    return (account if isinstance(account, dict) else None,
            quota if isinstance(quota, dict) else None)


def _collect_resources() -> dict:
    """Resolve the DB account set (live quota API calls, fanned out) → `{"accounts": [...]}`. Runs in the
    collector and, for the account family, in the daemon (`main.NodeStatePublisher.refresh_account_probe`).

    A node publishes only a quota it observed itself this pass — no floor, cache or republish; `quota`
    absent means "no first-hand reading from me", never "fine" or "clear". The one rule, single-homed
    here: a block asserting a non-null percentage REPLACES the stored reading and MUST carry `observedAt`;
    a block asserting none MERGES and needs no date (`_AUTH_REJECTED_QUOTA` nulls every percentage to
    collapse them; `_refresh_blocked_quota` omits them so they survive). Both arms live in
    `fleet_observe.account_upsert_statement`; verdicts expire on the CP's clock.

    The incident (2026-08-18), single-homed here: a persistent 429 on acct1's `/usage` returned
    `quota=None`, and a node-side cache then re-published the CP's own frozen, undated `auth_rejected`
    blob as a fresh observation for three weeks. The cache was laundering; it is gone.

    Session attribution is computed by the CP from its own `worktrees` table and must not come back here."""
    from control_plane_api import agent_api_accounts
    from fleet_dispatch import local_account_dir
    # DB SSOT account set, disabled included (the page shows them). Per account:
    #   `literal_norm` — the registry's string, REPORTED: a fleet-wide folded column other consumers compare
    #     against the CP-derived dir, so a node-local path would break them.
    #   `local_dir` — what is PROBED: the node's `<jaah_home>/auth/<slug>`, so a stale dir at the literal
    #     path never shadows fresh credentials.
    #   `securestorage_dir` — the token store when the token is there (`shared_storage.securestorage_dir`,
    #     resolved on this node; the row's own field is an older CP's view, unread), threaded to `_cred_path`.
    #   `provider` — which health signal is taken; absent reads as the catalogue default.
    db_by_norm: dict[str, dict] = {}
    literal_norms: list[str] = []
    auth_dirs: list[Path] = []
    securestorage_dirs: list[str] = []
    providers: list[str] = []
    token_sources: list[str] = []
    # One mount answer for the pass, not one per account (`shared_storage.token_source`).
    mounted = shared_storage.share_mounted()
    for rec in agent_api_accounts(include_disabled=True):
        cd = rec.get("config_dir") if isinstance(rec, dict) else None
        if not cd:
            continue
        literal_norm = _norm_dir(cd)
        if literal_norm in db_by_norm:
            continue
        # The row's own provider: the resolve ranks candidates by which holds a live token.
        local_dir = local_account_dir(cd, provider=rec.get("provider"))
        if not local_dir:
            continue
        securestorage_dir = shared_storage.securestorage_dir(local_dir)
        db_by_norm[literal_norm] = rec
        literal_norms.append(literal_norm)
        # A `shared_token_link` provider's credential in the store is probed THERE (the per-node file is a
        # link the adapters refuse to read).
        auth_dirs.append(Path(shared_storage.credential_home(local_dir, rec.get("provider"))))
        securestorage_dirs.append(securestorage_dir)
        providers.append(str(rec.get("provider") or ""))
        token_sources.append(shared_storage.token_source(local_dir, mounted=mounted))

    # NO session-derived field is published (see the docstring).

    # The wizard's provisioning state is the CP's own durable workflow, not projected here.
    accounts: list[dict] = []
    if auth_dirs:
        # `probe_account` runs as the uid that owns the dir; threads wait on the spool as on the HTTP.
        with ThreadPoolExecutor(max_workers=min(4, len(auth_dirs))) as pool:
            results = list(pool.map(probe_account, auth_dirs, securestorage_dirs, providers))
        # zip preserves order: literal_norms[i]/auth_dirs[i] is the account behind results[i].
        for literal_norm, source, (acct, quota) in zip(literal_norms, token_sources, results):
            if not acct:
                continue
            # Shared or local, never a path; rides `extra` to the Brains page (`account_fold.account_node_records`).
            acct["token_source"] = source
            # Verbatim, None included — nothing substituted. `refresh_blocked`'s numbers survive via the
            # server-side merge arm, which is what makes having no local merge lossless.
            acct["quota"] = quota
            # The registry's literal path (see above), identical on every node.
            acct["config_dir"] = literal_norm  # absolute path shown in the worktree model-hover (#1)
            rec = db_by_norm.get(literal_norm) or {}
            # enabled + slug come from the DB account row (migration PA); absent ⇒ enabled:true.
            acct["enabled"] = rec.get("enabled", True) is not False
            if rec.get("slug"):
                acct["slug"] = rec["slug"]
            accounts.append(acct)

    return {"accounts": accounts}


def cli_account_probe(auth_dir: str, securestorage_dir: str = "") -> int:
    """`python3 collector.py --account-probe <auth_dir> [<securestorage_dir>]` — the SESSION-USER side of
    `probe_account`.

    The provider comes from the environment (`_PROVIDER_ENV`); absent reads as the default. Prints
    `{"account": …, "quota": …}` on stdout — the whole protocol — and never a token (logs go to stderr).
    Exits 0 whenever it produced a record, `(None, None)` included; non-zero means it could not run."""
    account, quota = _read_account_quota(
        Path(auth_dir), securestorage_dir, env_get(_PROVIDER_ENV, ""))
    print(json.dumps({"account": account, "quota": quota}))
    return 0


if __name__ == "__main__":
    # One verb: the daemon's session-user re-exec (`probe_account`).
    # Three or four argv — the token store is appended only when the token is there; the provider arrives
    # in the environment.
    if len(sys.argv) in (3, 4) and sys.argv[1] == "--account-probe":
        raise SystemExit(cli_account_probe(sys.argv[2], sys.argv[3] if len(sys.argv) == 4 else ""))
    print("usage: collector.py --account-probe <auth_dir> [<securestorage_dir>]", file=sys.stderr)
    raise SystemExit(2)
