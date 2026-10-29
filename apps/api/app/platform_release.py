"""The release a macOS or Windows node can actually install: the CP's own version when its bundle is
published, else the newest one that is.

Linux nodes take the CP's version from apt, which publishes with the CP. The macOS and Windows bundles
(`dist/<platform>/jaah-<ver>-<platform>.{tar.gz,zip}` + `.sha256`) come from the deploy workflow's
`node-publish-platforms` job, decoupled from the Linux publish, so a release can ship with no bundle
for a platform. Told that release anyway, a node's updater waits for a sidecar that never comes, every pass.

So a platform node is told the CP's version only when its `.sha256` sidecar exists (a HEAD), else the
version named by `dist/<platform>/LATEST` — the pointer the publish job writes after the sidecar, never
backwards — when that one is older and published, else nothing (`version: null`, "nothing to converge
onto"). `missing` names the CP version with no bundle, for the console. Without `s3:ListBucket` (the CP
never enumerates this bucket) S3 answers a missing key 403, as it answers a signing or permission fault, so
a 403 counts as absent only once the same signer has read `LATEST`. An unconfigured bucket, an S3 that
cannot be read, or no pointer to prove the read with answers the CP's version as before: no reading is not
proof of absence.

Per-process cache: a present bundle never un-publishes, so it is kept; an absent one is re-checked every
`ABSENT_TTL_SECONDS`, which is how a re-run of the publish job reaches the nodes.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Optional

import httpx

from app import edition_features, http_bounded, node_platform
from app.config import get_settings
from app.node_liveness import parse_version
from app.operator_settings import reads as settings_reads

if TYPE_CHECKING:
    from app.aws.container_credentials import AwsCredentials

log = logging.getLogger("platform-release")

#: The bundle platform each node OS installs; an OS absent here (Ubuntu) takes the CP's version.
DIST_PLATFORM_BY_OS = {node_platform.OS_MACOS: "darwin-arm64", node_platform.OS_WINDOWS: "windows-x64"}
_ARCHIVE_EXT = {"darwin-arm64": "tar.gz", "windows-x64": "zip"}
LATEST_POINTER = "LATEST"

ABSENT_TTL_SECONDS = 300.0
UNKNOWN_TTL_SECONDS = 60.0
_TIMEOUT_SECONDS = 5.0
_POINTER_MAX_BYTES = 64

_PRESENT, _ABSENT, _UNKNOWN = "present", "absent", "unknown"


@dataclass(frozen=True)
class PlatformRelease:
    """What a node of one OS should run (`None` = nothing it can install), and the CP version whose bundle
    for that OS is missing (`None` = none missing, or not known to be)."""

    version: Optional[str]
    missing: Optional[str] = None


_cache: dict[tuple[str, str], tuple[PlatformRelease, float]] = {}
_lock = threading.Lock()
# Single flight: on a cold cache (every deploy) only one caller pays the S3 round trips; the rest wait for it.
_resolve_lock = threading.Lock()


def for_os(os: Optional[str], desired: Optional[str]) -> PlatformRelease:
    """The release a node of `os` should converge onto, given the CP's `desired` one."""
    platform = DIST_PLATFORM_BY_OS.get(os or "")
    if platform is None or desired is None:
        return PlatformRelease(desired)
    key = (platform, desired)
    cached = _cached(key)
    if cached is not None:
        return cached
    with _resolve_lock:
        cached = _cached(key)
        if cached is not None:
            return cached
        target = _target()
        if target is None:
            return PlatformRelease(desired)
        release, ttl = _resolve(target, platform, desired)
        with _lock:
            _cache[key] = (release, time.monotonic() + ttl)
        return release


def _cached(key: tuple[str, str]) -> Optional[PlatformRelease]:
    with _lock:
        cached = _cache.get(key)
    return cached[0] if cached is not None and cached[1] > time.monotonic() else None


def _resolve(target: "_Target", platform: str, desired: str) -> tuple[PlatformRelease, float]:
    """`(release, how long to keep it)`. Every inconclusive read answers `desired` for `UNKNOWN_TTL_SECONDS`."""
    unknown = (PlatformRelease(desired), UNKNOWN_TTL_SECONDS)
    state = _sidecar_state(target, platform, desired)
    if state == _PRESENT:
        return PlatformRelease(desired), float("inf")
    if state == _UNKNOWN:
        return unknown
    pointer = _send(target, "GET", f"dist/{platform}/{LATEST_POINTER}")
    if pointer is None or pointer[0] != 200:
        # No pointer to prove this signer reads `dist/` (a 403 above may be a credential fault), or no answer.
        return unknown
    latest = pointer[1].decode("ascii", "replace").strip()
    if parse_version(latest) is not None and _older(latest, desired):
        latest_state = _sidecar_state(target, platform, latest)
        if latest_state == _PRESENT:
            return PlatformRelease(latest, missing=desired), ABSENT_TTL_SECONDS
        if latest_state == _UNKNOWN:
            return unknown
    return PlatformRelease(None, missing=desired), ABSENT_TTL_SECONDS


def _older(candidate: str, desired: str) -> bool:
    a, b = parse_version(candidate), parse_version(desired)
    if a is None or b is None:
        return False
    width = max(len(a), len(b))
    return a + (0,) * (width - len(a)) < b + (0,) * (width - len(b))


@dataclass(frozen=True)
class _Target:
    bucket: str
    region: str
    credentials: AwsCredentials


def _target() -> Optional[_Target]:
    """The artifacts bucket as `agent_apt.presigned_redirect` resolves it, or None when unconfigured."""
    if not edition_features.enabled("apt_self_upgrade"):
        # No artifacts bucket in this edition: every node converges onto the desired release.
        return None
    from app.aws import container_credentials
    from app.routes.agent_apt import APT_BUCKET_SETTING  # deferred: keeps this module importable without FastAPI

    bucket = settings_reads.get_str(APT_BUCKET_SETTING)
    region = get_settings().aws_region
    credentials = container_credentials.resolve_credentials() if bucket and region else None
    if not bucket or not region or credentials is None:
        return None
    return _Target(bucket=bucket, region=region, credentials=credentials)


def _send(target: _Target, method: str, key: str) -> Optional[tuple[int, bytes]]:
    """One signed request for `key`: its status and at most `_POINTER_MAX_BYTES` of body, or None on a
    transport or signing failure. The test seam."""
    if not edition_features.enabled("apt_self_upgrade"):
        return None
    from app.aws import presign

    try:
        url = presign.presigned_url(
            method=method,
            url=presign.s3_object_url(bucket=target.bucket, key=key, region=target.region),
            region=target.region,
            service="s3",
            credentials=target.credentials,
            amz_date=datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ"),
            expires_seconds=60,
        )
        with http_bounded.client(_TIMEOUT_SECONDS) as client:
            with client.stream(method, url) as response:
                return response.status_code, http_bounded.read_capped(response, _POINTER_MAX_BYTES)[0]
    except (httpx.HTTPError, ValueError) as exc:
        # Never the URL: it is a bearer credential.
        log.warning("platform-release: %s %s failed (%s)", method, key, type(exc).__name__)
        return None


def _sidecar_state(target: _Target, platform: str, version: str) -> str:
    key = f"dist/{platform}/jaah-{version}-{platform}.{_ARCHIVE_EXT[platform]}.sha256"
    answer = _send(target, "HEAD", key)
    if answer is None:
        return _UNKNOWN
    if answer[0] == 200:
        return _PRESENT
    if answer[0] in (403, 404):
        return _ABSENT
    log.warning("platform-release: HEAD %s answered HTTP %s", key, answer[0])
    return _UNKNOWN


def _reset_for_tests() -> None:
    with _lock:
        _cache.clear()
