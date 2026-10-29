"""Direct console — this daemon's OWN inbound TLS WebSocket listener, bypassing the control plane.

The CP path (`console.py`) is a rendezvous where every PTY byte crosses the control plane; here the
browser connects to the NODE over TLS and the CP leaves the data path, with the PTY unchanged
(`ConsoleBridge.attach_direct_viewer`). TLS terminates here because an HTTPS page refuses `ws://`.
It binds the node's own Tailscale addresses, NEVER a wildcard, on a port disjoint from the worktree pool's port grid.

The credential rides a SUBPROTOCOL, not a query parameter, and the CP mints the grant out of band over
the node bus. NOTHING IS EVER REJECTED DURING THE UPGRADE: a pre-handshake refusal collapses to HTTP
403/400, which a browser reports as retryable 1006, so every valid handshake is accepted and every
refusal is a close code in the BROWSER's vocabulary. The hello frame is TEXT — a binary `{` would parse
as frame type 123 in the `[type:1][len:2][payload]` envelope that follows it.

PYTHON 3.9 on fleet nodes: no `match`, no runtime `X | Y`, no `asyncio.timeout`/`TaskGroup`, and never
pass `ssl_shutdown_timeout` to `serve()` — absent on 3.9's `loop.create_server`."""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import ssl
import subprocess
import time
import traceback
import urllib.parse
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable, Dict, List, Optional, Sequence, Tuple

import control_plane_api
import console as console_mod
import edition
import environment
from jaah_env import env_get
import node_frame_auth   # single home for "is this CP->node frame authentic" (the cp_frame_proof check)
from worktree_mechanics import ports

log = logging.getLogger("jaahd.console_direct")

# Pinned at INFO as a credential control: `websockets` logs the whole handshake, credential header included,
# at DEBUG.
_ws_log = logging.getLogger("jaahd.console_direct.ws")
_ws_log.setLevel(logging.INFO)

# The credential as it appears inside a library log record: the offered subprotocol, verbatim.
_TOKEN_IN_TEXT = re.compile(r"tok\.[^\s,;\"']+")


class _RedactDirectToken(logging.Filter):
    """Scrub the direct credential out of anything the library logs, at any level.

    The level pin is not enough: an invalid `Sec-WebSocket-Protocol` header makes the vendored parser raise
    with the raw header in its message, at ERROR — and `=` is not a `tchar`, so a padded base64 token would
    leak on every connection."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            text = record.getMessage()
        except Exception:   # noqa: BLE001 — a record we cannot render is one we cannot scrub
            return True
        if "tok." in text:
            record.msg = _TOKEN_IN_TEXT.sub("tok.<redacted>", text)
            record.args = ()
        if record.exc_info is not None:
            # The traceback text is where the header rides; scrubbed before the handler formats it.
            rendered = "".join(traceback.format_exception(*record.exc_info))
            record.exc_info = None
            record.exc_text = _TOKEN_IN_TEXT.sub("tok.<redacted>", rendered)
        return True


_ws_log.addFilter(_RedactDirectToken())

# ── Flags, port, and the pool-grid disjointness assertion ────────────────────────────────────────────

# No node-side enable flag, by design: the capability gates itself on having a tailnet identity to bind and
# certify.
DIRECT_CONSOLE_PORT_VAR = "JAAH_DIRECT_CONSOLE_PORT"

# Not 8787, which sits in API_PORT's 8000-8999 grid window. A default, not an assertion: `port_grid_conflict`
# re-checks it against the live, operator-editable grid at bind time.
DEFAULT_DIRECT_CONSOLE_PORT = 7787


def resolve_direct_console_port() -> int:
    """The listener's port: `JAAH_DIRECT_CONSOLE_PORT`, else `DEFAULT_DIRECT_CONSOLE_PORT`.

    An unparseable or out-of-range value is refused loudly, never a silent fallback."""
    raw = (env_get(DIRECT_CONSOLE_PORT_VAR) or "").strip()
    if not raw:
        return DEFAULT_DIRECT_CONSOLE_PORT
    try:
        port = int(raw)
    except ValueError:
        raise ValueError(f"{DIRECT_CONSOLE_PORT_VAR}={raw!r} is not an integer")
    if not 1 <= port <= 65535:
        raise ValueError(f"{DIRECT_CONSOLE_PORT_VAR}={port} is outside 1..65535")
    return port


# How long a failed grid read is remembered: uncached, a retried bind loop is one HTTP round trip per attempt
# while the CP is down; forever, one blip kills direct mode until restart.
_GRID_UNAVAILABLE_TTL_SECONDS = 60.0
_grid_cache: Optional[dict] = None
_grid_unavailable_until: float = 0.0


def _pool_grid() -> dict:
    """The pool port grid's inputs from the control plane, cached for this process.

    A CP unreachable at daemon start refuses the direct console — accepted, because a built-in fallback
    grid would be a second copy of the operator's table and a stale one squats a lease's port. A success is
    cached for the process (the bound port is fixed at bind time); a failure for
    `_GRID_UNAVAILABLE_TTL_SECONDS`. Raises on an unreadable grid; the caller turns that into "do not
    bind", never an empty grid."""
    global _grid_cache, _grid_unavailable_until
    if _grid_cache is not None:
        return _grid_cache
    now = time.monotonic()
    if now < _grid_unavailable_until:
        raise RuntimeError(
            "the pool port grid was unreadable {:.0f}s ago and the cached refusal still stands "
            "(retrying in {:.0f}s) — the direct console cannot prove its port is disjoint from the pool's"
            .format(_GRID_UNAVAILABLE_TTL_SECONDS - (_grid_unavailable_until - now),
                    _grid_unavailable_until - now)
        )
    try:
        grid = control_plane_api.agent_api_port_grid()
    except Exception as exc:   # noqa: BLE001 — every transport failure is one answer: "we do not know"
        _grid_unavailable_until = now + _GRID_UNAVAILABLE_TTL_SECONDS
        raise RuntimeError(
            "could not read the pool port grid from the control plane ({}: {}) — the direct console "
            "cannot prove its port is disjoint from the pool's".format(type(exc).__name__, exc)
        ) from exc
    _grid_cache = grid
    return grid


def pool_grid_windows() -> List[Tuple[str, int, int]]:
    """Every port window the pool port grid manages, as `(var, first, last_inclusive)`, read from the CP.

    Fail-closed both ways: an unreachable CP raises (`_pool_grid`), and an unusable window raises rather
    than being dropped (a dropped window is one this node would bind inside). No range starts at all is
    legal — every port var is tier 2, which the tier-2 check covers."""
    grid = _pool_grid()
    starts = grid.get("range_start_by_var")
    if not isinstance(starts, dict) or not starts:
        return []
    window = grid.get("window")
    if isinstance(window, bool) or not isinstance(window, int) or window < 1:
        raise RuntimeError(
            "the control plane's pool grid has {} range starts but no usable window size ({!r}) — refusing "
            "to bind against windows this node cannot compute".format(len(starts), window)
        )
    windows: List[Tuple[str, int, int]] = []
    for var, start in starts.items():
        if isinstance(start, bool) or not isinstance(start, int):
            raise RuntimeError(
                "the control plane's pool grid gives {!r} a non-integer range start ({!r}) — refusing to "
                "bind against a window this node cannot compute".format(var, start)
            )
        windows.append((str(var), start, start + window - 1))
    return sorted(windows, key=lambda w: w[1])


def pool_auto_port_range() -> Tuple[int, int]:
    """The pool's tier-2 range `(floor, ceiling)`, via the allocator's own read
    (`worktree_mechanics.ports.auto_port_range_from_grid`), so the refusal and the allocator cannot disagree — a
    divergence would lose the console's port to a lease and crash-loop on EADDRINUSE."""
    return ports.auto_port_range_from_grid(_pool_grid())


def port_grid_conflict(port: int) -> Optional[str]:
    """The reason `port` may not host the direct listener, or None when it is disjoint from everything the
    worktree pool hands out.

    Checked against the grid's windows (not current allocations, which go stale) and the tier-2 range
    (allocated by live bind test, so a console that is merely down would lose its port and crash-loop on
    EADDRINUSE). Raises when the grid is unreadable: None means "checked, and disjoint", never "could not
    check"."""
    for var, first, last in pool_grid_windows():
        if first <= port <= last:
            return (
                f"port {port} is inside the pool port grid's {var} window ({first}-{last}) — the "
                f"direct console port is a node-wide singleton and must be disjoint from every "
                f"project's band (the grid is the console's port-grid settings, Settings)"
            )
    floor, ceiling = pool_auto_port_range()
    if floor <= port <= ceiling:
        return (
            f"port {port} is inside the worktree pool's tier-2 auto range ({floor}-{ceiling}) — a lease "
            f"can allocate it to a project stack, and the direct console port is a node-wide singleton "
            f"(see worktree_mechanics.ports.apply_auto_ports)"
        )
    return None


# ── The node's own tailnet identity (C-6) ────────────────────────────────────────────────────────────

# A local API round trip through the CLI; bounded so a wedged tailscaled cannot hold up startup.
_TAILSCALE_STATUS_TIMEOUT = 10.0

# The tailnet's base DNS domain (e.g. `tn.example.com`), from `jaahd.env` like the port above. It is the
# node-side direct-console ON switch: unset or empty ⇒ no identity ⇒ no listener, no certificate, no
# `direct_console_url`, so the CP answers `disabled` and every console relays. It does not gate the
# node's plain tailnet name (`resolve_tailnet_dns_name`), which is published either way. The name itself is read from tailscaled, never
# derived from `node_id` (a `NODE_ID` need not match its `DNSName`); this only says which domain counts.
TAILNET_DNS_SUFFIX_VAR = "JAAH_TAILNET_DNS_SUFFIX"


def resolve_tailnet_dns_suffix() -> str:
    """`JAAH_TAILNET_DNS_SUFFIX`, normalised (`.strip().strip(".").lower()`, as the CP's own
    `tailnet_suffix`), or "" when unset — "" means direct console is off on this node."""
    return (env_get(TAILNET_DNS_SUFFIX_VAR) or "").strip().strip(".").lower()


@dataclass(frozen=True)
class TailnetIdentity:
    """What this node is on the tailnet: the certifiable DNS name, and every address it answers on — both
    families, since binding only v4 refuses a browser that picks the AAAA record."""

    dns_name: str
    addresses: Tuple[str, ...]


# A failed `tailscale status` is asked again after this long rather than cached for the process life: a
# tailscaled not yet up at boot would otherwise cost the node its slot-link host until a restart.
_TAILSCALE_RETRY_SECONDS = 60.0
# `(answer, monotonic time asked)`; None = never asked. A positive answer is kept for the process life.
_tailscale_self_cache: Optional[Tuple[Optional[TailnetIdentity], float]] = None


def _tailscale_self() -> Optional[TailnetIdentity]:
    """Ask this machine's `tailscaled` what this node is on the tailnet.

    Any name counts here; which domain direct console accepts is `_probe_tailnet_identity`'s filter.
    Cached because `build_config_snapshot` asks every idle tick and a node's tailnet name does not change
    under a running daemon; a None is retried after `_TAILSCALE_RETRY_SECONDS`. Every cached answer is
    one tailscaled gave — which is why the sandbox refusal lives in the callers. Tests clear it via
    `resolve_tailnet_identity.cache_clear()`."""
    global _tailscale_self_cache
    cached = _tailscale_self_cache
    if cached is not None and (cached[0] is not None or time.monotonic() - cached[1] < _TAILSCALE_RETRY_SECONDS):
        return cached[0]
    answer = _ask_tailscale_self()
    _tailscale_self_cache = (answer, time.monotonic())
    return answer


def _clear_tailscale_self_cache() -> None:
    global _tailscale_self_cache
    _tailscale_self_cache = None


def _ask_tailscale_self() -> Optional[TailnetIdentity]:
    """One uncached `tailscale status --json`, reduced to this node's name and addresses. Never asked in the local
    edition, which joins no tailnet."""
    if edition.IS_CE or not environment.tailscale_askable():
        return None
    try:
        proc = subprocess.run(
            environment.tailscale_command("status", "--json"),
            capture_output=True, text=True, timeout=_TAILSCALE_STATUS_TIMEOUT,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log.info("tailnet identity: tailscale status unavailable (%s)", exc)
        return None
    if proc.returncode != 0:
        log.info("tailnet identity: tailscale status failed (rc=%s)", proc.returncode)
        return None
    try:
        status = json.loads(proc.stdout or "{}")
        self_node = status.get("Self") or {}
    except ValueError as exc:
        log.warning("tailnet identity: tailscale status returned unparseable JSON (%s)", exc)
        return None
    # The trailing dot is how tailscaled reports a FQDN; it is not part of a URL host.
    dns_name = str(self_node.get("DNSName") or "").strip().rstrip(".")
    if not dns_name:
        return None
    addresses = [str(ip) for ip in (self_node.get("TailscaleIPs") or []) if str(ip).strip()]
    return TailnetIdentity(dns_name=dns_name, addresses=tuple(addresses))


def _probe_tailnet_identity(suffix: str) -> Optional[TailnetIdentity]:
    """`_tailscale_self`, narrowed to a name under `.<suffix>` that has an address to bind."""
    identity = _tailscale_self()
    if identity is None or not identity.dns_name.lower().endswith(f".{suffix}"):
        log.info(
            "direct console: this node has no .%s name (DNSName=%r) — direct mode needs one for a "
            "browser-trusted certificate", suffix, identity.dns_name if identity else "",
        )
        return None
    if not identity.addresses:
        log.warning("direct console: tailscale reports no addresses for this node — nothing to bind")
        return None
    return identity


def resolve_tailnet_dns_name() -> Optional[str]:
    """This node's tailnet DNS name (MagicDNS FQDN, no trailing dot), or None when it has none.

    Independent of `JAAH_TAILNET_DNS_SUFFIX`: that switch decides direct console, while this name is how a
    remote browser addresses the node at all — the CP hosts slot stack URLs on it
    (`apps/api/app/slot_stack.py`). None in sandbox posture without asking, as `resolve_tailnet_identity`."""
    if environment.is_sandbox():
        return None
    identity = _tailscale_self()
    return identity.dns_name if identity is not None else None


def resolve_tailnet_identity() -> Optional[TailnetIdentity]:
    """This node's tailnet name + addresses, or None when it has none. The one entry point.

    None in sandbox posture without asking: a non-None identity is what constructs `DirectConsoleCert`,
    whose `tailscale cert` provisions a real certificate, so this one guard closes both. Every consumer
    treats None as an absence. The check sits outside the cache because posture is filesystem-sensed
    (`node.env`): a provisioned node whose file was briefly absent would otherwise cache the refusal for
    the process's life, and nothing in production calls `cache_clear()`."""
    if environment.is_sandbox():
        log.info("direct console: sandbox posture - not asking tailscaled for a tailnet identity, so "
                 "no direct listener and no certificate are provisioned")
        return None
    suffix = resolve_tailnet_dns_suffix()
    if not suffix:
        # Not an error: the configured OFF state (unset in jaahd.env). tailscaled is never asked.
        log.info("direct console: %s is unset — direct console is off on this node", TAILNET_DNS_SUFFIX_VAR)
        return None
    return _probe_tailnet_identity(suffix)


# The test seam stays on the public name; exposing the private probe would let a caller bypass the posture
# guard.
resolve_tailnet_identity.cache_clear = _clear_tailscale_self_cache   # type: ignore[attr-defined]


# ── Certificates: acquisition, renewal, and the crash-loop gate ──────────────────────────────────────

# `tailscale cert` yields a 90-day certificate with no auto-renewal, so this timer is a deliverable. 12h
# against a 30-day margin is ~60 chances.
_CERT_CHECK_INTERVAL_SECONDS = 12 * 3600
# `--min-validity`: tailscaled returns its cached cert when valid this long. The first crash-loop guard.
_CERT_MIN_VALIDITY = "720h"   # 30 days
# The second guard: an attempt stamp on disk, written before the request. LE allows 5 duplicate certificates
# per 168h, so 36h apart cannot exceed it.
_CERT_ATTEMPT_MIN_INTERVAL_SECONDS = 36 * 3600

# After a failed order, 1h: the LE limit counts certificates issued, and a failed order issued none. A crashed
# (`pending`) attempt keeps the long bound.
_CERT_FAILED_ATTEMPT_MIN_INTERVAL_SECONDS = 1 * 3600

_ATTEMPT_PENDING = "pending"
_ATTEMPT_FAILED = "failed"
_ATTEMPT_SUCCEEDED = "succeeded"
# Bounds one `tailscale cert` call. A first issuance talks to the CA, so this is generous.
_CERT_REQUEST_TIMEOUT = 120.0


class CertUnavailable(RuntimeError):
    """No usable certificate, and so no listener: the browser refuses `ws://` from an HTTPS page."""


def _restrict(path: Path) -> None:
    """Make `path` owner-only, tolerating a filesystem that will not say so."""
    try:
        os.chmod(str(path), 0o700)
    except OSError as exc:
        log.warning("direct console: could not restrict %s to owner-only: %s", path, exc)


class DirectConsoleCert:
    """The listener's `SSLContext`, kept current for the process lifetime.

    Hot swap, not re-bind: a renewal calls `load_cert_chain` on the same context `serve()` holds, so only
    new connections see the new chain. An explicit `cert_file`/`key_file` pair skips `tailscale cert` (dev
    seam). "cert access denied" means the package install's `TS_PERMIT_CERT_UID=jaahd` drop-in is
    missing."""

    def __init__(
        self,
        dns_name: str,
        cert_dir: Path,
        *,
        cert_file: Optional[str] = None,
        key_file: Optional[str] = None,
        min_validity: str = _CERT_MIN_VALIDITY,
        attempt_min_interval: float = _CERT_ATTEMPT_MIN_INTERVAL_SECONDS,
        failed_attempt_min_interval: float = _CERT_FAILED_ATTEMPT_MIN_INTERVAL_SECONDS,
    ) -> None:
        self._dns_name = dns_name
        self._cert_dir = Path(cert_dir)
        self._explicit = cert_file is not None and key_file is not None
        self._cert_path = Path(cert_file) if cert_file else self._cert_dir / (dns_name + ".crt")
        self._key_path = Path(key_file) if key_file else self._cert_dir / (dns_name + ".key")
        self._attempt_path = self._cert_dir / (dns_name + ".last-attempt")
        self._min_validity = min_validity
        self._attempt_min_interval = attempt_min_interval
        self._failed_attempt_min_interval = failed_attempt_min_interval
        self._context: Optional[ssl.SSLContext] = None

    def context(self) -> ssl.SSLContext:
        """The live context, built once and reused: `serve()` holds this object."""
        if self._context is None:
            context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            # Explicit, so a distro policy change cannot silently widen it.
            context.minimum_version = ssl.TLSVersion.TLSv1_2
            self._context = context
        return self._context

    def _load(self) -> None:
        # The directory too: a group/other-writable parent leaves even a 0600 key replaceable.
        parent = self._key_path.parent
        parent_mode = parent.stat().st_mode & 0o777
        if parent_mode & 0o022:
            raise CertUnavailable(
                "the directory holding the private key ({}) is mode {:o}; direct console will not serve "
                "a key whose parent is writable outside its owner".format(parent, parent_mode)
            )
        mode = self._key_path.stat().st_mode & 0o777
        if mode & 0o077:
            # Refuse, not warn: a key readable beyond its owner is a node-impersonation credential.
            raise CertUnavailable(
                "the private key at {} is mode {:o}; direct console will not serve a key readable "
                "outside its owner".format(self._key_path, mode)
            )
        self.context().load_cert_chain(str(self._cert_path), str(self._key_path))

    def _attempt_age(self) -> Optional[float]:
        """Seconds since the last issuance attempt, or None. Read from disk so a restart inherits the
        throttle."""
        try:
            return max(0.0, time.time() - self._attempt_path.stat().st_mtime)
        except OSError:
            return None

    def _attempt_outcome(self) -> str:
        """How the last attempt ended: `succeeded`, `failed`, or `pending` (also for unknown — the long
        interval)."""
        try:
            recorded = self._attempt_path.read_text().split()
        except OSError:
            return _ATTEMPT_PENDING
        outcome = recorded[1] if len(recorded) > 1 else _ATTEMPT_PENDING
        return outcome if outcome in (_ATTEMPT_FAILED, _ATTEMPT_SUCCEEDED) else _ATTEMPT_PENDING

    def _min_interval_for_last_attempt(self) -> float:
        """The throttle this node has actually earned.

        The question is "was a certificate issued", and the disk is the stronger answer: no cert file, none
        was issued. A crash between write and outcome leaves `pending` and a certificate, i.e. 36h."""
        issued = self._cert_path.exists()
        if self._attempt_outcome() == _ATTEMPT_FAILED or not issued:
            return min(self._failed_attempt_min_interval, self._attempt_min_interval)
        return self._attempt_min_interval

    def _record_attempt_outcome(self, outcome: str) -> None:
        """Rewrite the stamp with how the attempt ended, keeping its timestamp. Best-effort: a failed write
        leaves `pending`, the long interval — the safe direction."""
        try:
            stamped = self._attempt_path.read_text().split()[0]
            self._attempt_path.write_text("{} {}".format(stamped, outcome))
        except (OSError, IndexError):
            pass

    def _stamp_attempt(self) -> None:
        try:
            self._cert_dir.mkdir(parents=True, exist_ok=True)
            # 0700 explicitly (mkdir's mode is umask-masked and a no-op on an existing dir): this key
            # impersonates the node.
            _restrict(self._cert_dir)
            self._attempt_path.write_text("{} {}".format(int(time.time()), _ATTEMPT_PENDING))
        except OSError as exc:
            # No stamp, no throttle: abandon the attempt rather than make it unthrottled.
            raise CertUnavailable(
                "cannot record a certificate-request attempt at {} ({}) — refusing to request without a "
                "crash-loop throttle".format(self._attempt_path, exc)
            )

    def request(self) -> None:
        """Ask tailscaled for a certificate, subject to both crash-loop guards.

        Raises `CertUnavailable` when throttled or the CLI fails; the caller decides fatal vs
        retry-next-tick."""
        if self._explicit:
            raise CertUnavailable("an explicit cert/key pair is configured; nothing to request")
        age = self._attempt_age()
        min_interval = self._min_interval_for_last_attempt()
        if age is not None and age < min_interval:
            raise CertUnavailable(
                "the last certificate request for {} was {:.1f}h ago ({}) and the minimum interval is "
                "{:.1f}h (Let's Encrypt allows 5 duplicate certificates per 168h for one name, and 5 "
                "failed validations per hour; this throttle is what stops a crash-loop burning "
                "them)".format(
                    self._dns_name, age / 3600.0, self._attempt_outcome(), min_interval / 3600.0,
                )
            )
        self._stamp_attempt()
        try:
            self._cert_dir.mkdir(parents=True, exist_ok=True)
            # 0700 explicitly, as above.
            _restrict(self._cert_dir)
            proc = subprocess.run(
                environment.tailscale_command(
                    "cert",
                    "--min-validity", self._min_validity,
                    "--cert-file", str(self._cert_path),
                    "--key-file", str(self._key_path),
                    self._dns_name,
                ),
                capture_output=True, text=True, timeout=_CERT_REQUEST_TIMEOUT,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            self._record_attempt_outcome(_ATTEMPT_FAILED)
            raise CertUnavailable("tailscale cert could not run ({})".format(exc))
        if proc.returncode != 0:
            # The CLI's stderr is the actionable half ("HTTPS must be enabled in the admin console").
            self._record_attempt_outcome(_ATTEMPT_FAILED)
            raise CertUnavailable(
                "tailscale cert failed for {} (rc={}): {}".format(
                    self._dns_name, proc.returncode, (proc.stderr or "").strip()[:400],
                )
            )
        self._record_attempt_outcome(_ATTEMPT_SUCCEEDED)
        log.info("direct console: certificate for %s written to %s", self._dns_name, self._cert_path)

    def ensure_loaded(self) -> None:
        """Make the context serve a usable certificate, requesting one if none is on disk. Called once
        before binding; raises if none can be had, and the listener does not start."""
        if self._cert_path.exists() and self._key_path.exists():
            try:
                self._load()
                return
            except (OSError, ssl.SSLError) as exc:
                if self._explicit:
                    raise CertUnavailable(
                        "the configured cert/key pair at {} is unusable ({})".format(self._cert_path, exc)
                    )
                log.warning("direct console: existing certificate at %s is unusable (%s) — re-requesting",
                            self._cert_path, exc)
        self.request()
        try:
            self._load()
        except (OSError, ssl.SSLError) as exc:
            raise CertUnavailable("the certificate just written to {} is unusable ({})".format(
                self._cert_path, exc))

    def renew_once(self) -> bool:
        """One renewal pass: request (tailscaled decides whether renewal is due via `--min-validity`, so no
        private expiry parser) and hot-swap on success. Returns whether the context was reloaded."""
        if self._explicit:
            return False
        try:
            self.request()
        except CertUnavailable as exc:
            # A throttle refusal is by design and a CLI failure retries next tick; the current context keeps
            # serving.
            log.info("direct console: certificate renewal skipped (%s)", exc)
            return False
        try:
            self._load()
        except (OSError, ssl.SSLError) as exc:
            log.warning("direct console: renewed certificate at %s is unusable (%s) — keeping the old one",
                        self._cert_path, exc)
            return False
        log.info("direct console: certificate for %s renewed and hot-swapped into the live listener",
                 self._dns_name)
        return True

    async def renewal_loop(self, interval: float = _CERT_CHECK_INTERVAL_SECONDS) -> None:
        """The renewal timer, for the daemon's life; every failure is contained so it cannot die silently."""
        while True:
            await asyncio.sleep(interval)
            try:
                await asyncio.get_event_loop().run_in_executor(None, self.renew_once)
            except Exception:   # noqa: BLE001 — a renewal tick may never kill the timer
                log.exception("direct console: certificate renewal tick failed")


# ── Grants: what the CP authorized, and what a revoke drops ──────────────────────────────────────────

# A resource bound (~200 bytes each, one per console open): caps what a compromised CP token could grow.
_MAX_GRANTS = 256
# The contract is 24h plus an hour of skew. Refused rather than clamped, so the bug is not hidden.
_MAX_GRANT_TTL_SECONDS = 25 * 3600
# RFC 7230 `token` characters, all a `Sec-WebSocket-Protocol` value may hold (no `=`, so no base64 padding).
# `fullmatch`, never `^…$`: `$` also matches before a trailing \n.
_TCHAR_ONLY = re.compile(r"[!#$%&'*+\-.^_`|~0-9A-Za-z]+")
# 128 bits at hex, far above network brute force, well under what the CP mints.
_MIN_TOKEN_CHARS = 32


@dataclass
class DirectGrant:
    """One CP authorization: this token may attach to this slot, as this admin session, until then.

    The raw `token` is kept (the CP's audit channel keys on it) but never logged (`fingerprint` is); the
    store indexes by its SHA-256. `read_only`, `attach_only`, `config_dir` and `securestorage_dir` live on
    the grant, which arrived over the proof-authenticated bus, never on the browser-composed hello — so a
    token holder cannot flip a tier or point a session at another account's credentials. `attach_only` is
    ORed with the hello's (a CP requirement only narrows); the per-dial reconnect half rides the hello
    because a grant lives up to 24h. `config_dir` is the CP-chosen account a create runs under;
    `securestorage_dir` only relocates that same account's token lookup (`shared_storage`). Absent fields
    parse as the narrow default."""

    token: str
    project: str
    number: str
    kind: str
    session_id: str
    expires_at: float
    tmux_session: str = ""
    config_dir: Optional[str] = None
    securestorage_dir: Optional[str] = None
    # Whose CLI a create opens, and an API-key account's slug standing in for `config_dir`; as the relay.
    provider: Optional[str] = None
    api_key_account: Optional[str] = None
    # The Jaah-account block (`console.console_bedrock`), standing in for both accounts above.
    bedrock: Optional[str] = None
    # The Architectural Guard stanza's project (`console.console_guard`), only where it is available; None otherwise.
    guard_project: Optional[int] = None
    read_only: bool = False
    attach_only: bool = False
    # Whether this grant may name a window: `view`/`layout` resolve against the shared `fleet` session, a slot
    # claim the grant's (project, number) does not cover. Defaults False, also for an older CP.
    view_allowed: bool = False

    @property
    def key(self) -> Tuple[str, str, str]:
        return (self.project, self.number, self.kind)

    @property
    def fingerprint(self) -> str:
        """A short, non-reversible log handle: eight hex characters of SHA-256."""
        return _token_digest(self.token)[:8]


def _token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _finite(value: float) -> Optional[float]:
    """`value`, or None when it is NaN or an infinity.

    `json.loads` accepts `NaN` and every comparison against it is False, so it would defeat both expiry
    guards: an immortal bearer credential."""
    if value != value or value in (float("inf"), float("-inf")):
        return None
    return value


def _parse_expiry(raw: object) -> Optional[float]:
    """A grant's expiry as a POSIX timestamp, or None when it cannot be read.

    Accepts ISO-8601 (what the CP sends) and a numeric epoch. A naive datetime is UTC, matching the CP.
    Pinned by `tests/test_console_direct.py`'s cross-app contract test."""
    if isinstance(raw, bool):
        return None                    # `True` is an int to `float()`, and never a timestamp
    if isinstance(raw, (int, float)):
        return _finite(float(raw))
    if isinstance(raw, str) and raw.strip():
        text = raw.strip()
        try:
            # 3.9's `fromisoformat` rejects a trailing `Z`.
            parsed = datetime.fromisoformat(text[:-1] + "+00:00" if text.endswith("Z") else text)
        except ValueError:
            try:
                return _finite(float(text))     # a stringified epoch
            except ValueError:
                return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    return None


class DirectGrantStore:
    """The node's authoritative record of which direct tokens exist.

    The node, not the CP, is the authority: during a deploy the replica handling a logout may never have
    minted the token. So a revoke is keyed on `session_id` + slot and drops every match (`token` only
    narrows). In memory only; "I restarted" and "wrong token" both close 4401."""

    def __init__(self, node_id: str) -> None:
        self._node_id = node_id
        self._grants: Dict[str, DirectGrant] = {}
        # The sockets each grant is serving; without it revocation would only stop new connections.
        self._live: Dict[str, List[object]] = {}
        # In-flight revocation closes, held so the loop cannot collect them; each discards itself on
        # completion.
        self._closing = set()

    def __len__(self) -> int:
        return len(self._grants)

    def prune(self, now: Optional[float] = None) -> int:
        """Drop expired grants. Returns how many went."""
        cutoff = time.time() if now is None else now
        stale = [digest for digest, grant in self._grants.items() if grant.expires_at <= cutoff]
        for digest in stale:
            self._grants.pop(digest, None)
        return len(stale)

    def grant(self, frame: dict) -> dict:
        """Record one `console_direct_grant`, returning the reply body the bus sends back.

        The reply is load-bearing: the CP hands the token to the browser only after this acks, and it
        always carries `read_only`, so a pre-tier node's bare `{"ok": true}` makes the CP decline.
        `cp_frame_proof` authenticates the frame; `token` is the 24h direct credential."""
        if not node_frame_auth.frame_auth_ok(frame, expected_token=console_mod._expected_token):
            # Two booleans, never the values: distinguishes nothing-sent, nothing-configured, and a mismatch.
            log.warning(
                "direct console: grant rejected — invalid/missing cp_frame_proof (provided=%s, configured=%s)",
                bool(frame.get("cp_frame_proof")), bool(console_mod._expected_token()),
            )
            return {"error": "unauthorized"}
        node_id = frame.get("node_id")
        # Mandatory: an optional check fails open, honoured by whichever node receives it.
        if not isinstance(node_id, str) or not node_id:
            return {"error": "grant is missing node_id"}
        if node_id != self._node_id:
            log.warning("direct console: grant rejected — bound to node %r, this is %r",
                        node_id, self._node_id)
            return {"error": "grant is bound to another node"}
        token = frame.get("token")
        if not isinstance(token, str) or not token:
            return {"error": "missing direct token"}
        if not _TCHAR_ONLY.fullmatch(token):
            # Must survive a `Sec-WebSocket-Protocol` header; anything else is unofferable and makes the
            # parser raise with the raw header (the leak `_RedactDirectToken` catches).
            return {"error": "direct token is not a valid HTTP token"}
        if len(token) < _MIN_TOKEN_CHARS:
            # The node's own floor for a 24h credential, not an assumption about what the CP mints.
            return {"error": "direct token is shorter than this node's minimum"}
        kind = frame.get("kind")
        if kind != "clauded":
            return {"error": "unknown console kind"}
        # `is None`, never `or ""`: slot 0 (the main clone) is falsy.
        project = "" if frame.get("project") is None else str(frame.get("project"))
        number = "" if frame.get("number") is None else str(frame.get("number"))
        if not project or not number:
            return {"error": "invalid console target"}
        session_id = frame.get("session_id")
        if not isinstance(session_id, str) or not session_id:
            return {"error": "missing session_id"}
        # Optional. Shape-checked here only: `_serve_session` → `attach_direct_viewer` → `_resolve_session`
        # pass it straight to a tmux target.
        tmux_session_raw = frame.get("session")
        tmux_session = ""
        if tmux_session_raw:
            # Falsy means "none named" (`""` is the ordinary case; the validator refuses it).
            validated, session_error = console_mod._validate_session_name(tmux_session_raw)
            if session_error:
                return {"error": session_error}
            tmux_session = validated or ""
        # `console.py`'s own reader, so both transports agree on what read-only means.
        read_only, read_only_error = console_mod._validate_optional_bool(
            frame.get("read_only"), "read_only",
        )
        if read_only_error:
            return {"error": read_only_error}
        # Absence is "no requirement": older CPs omit it, and refusing would break them.
        attach_only, attach_only_error = console_mod._validate_optional_bool(
            frame.get("attach_only"), "attach_only",
        )
        if attach_only_error:
            return {"error": attach_only_error}
        expires_at = _parse_expiry(frame.get("expires_at"))
        if expires_at is None:
            return {"error": "missing or unparseable expires_at"}
        now = time.time()
        if expires_at <= now:
            return {"error": "grant is already expired"}
        if expires_at - now > _MAX_GRANT_TTL_SECONDS:
            return {"error": "grant lifetime exceeds the 24h contract"}

        self.prune(now)
        digest = _token_digest(token)
        if digest not in self._grants and len(self._grants) >= _MAX_GRANTS:
            log.warning("direct console: grant refused — this node already holds %d live grants",
                        len(self._grants))
            return {"error": "too many live grants on this node"}
        # Since per-slot editability a `view` names a window of the shared `fleet` session, which
        # `console.py::_fleet_window_socket` resolves without checking the grant's slot — ungated, one slot's
        # holder would reach every runner on the host. `is True`, so a truthy value cannot widen it.
        view_allowed = frame.get("view_allowed") is True
        # The relay's own readers. No `securestorage_dir` (older CP) is exactly a non-shared account.
        config_dir = console_mod.console_config_dir(frame.get("config_dir"))
        # And the store that account's token lives in when it is shared — same discipline, its own
        # one-reading function. A CP too old to send it leaves `None`, which is exactly a non-shared
        # account and makes the launch this grant authorizes byte-identical to before.
        securestorage_dir = console_mod.console_securestorage_dir(frame.get("securestorage_dir"))
        # The same two readers the relay frame goes through, so both transports create the same tab.
        provider = console_mod.console_provider(frame.get("provider"))
        api_key_account = console_mod.console_api_key_account(frame.get("api_key_account"))
        bedrock = console_mod.console_bedrock(frame.get("bedrock"))
        guard_project = console_mod.console_guard(frame.get("guard"))
        grant = DirectGrant(
            token=token, project=project, number=number, kind=kind,
            session_id=session_id, expires_at=expires_at, tmux_session=tmux_session,
            config_dir=config_dir, securestorage_dir=securestorage_dir,
            provider=provider, api_key_account=api_key_account, bedrock=bedrock, guard_project=guard_project,
            read_only=read_only, attach_only=attach_only,
            view_allowed=view_allowed,
        )
        self._grants[digest] = grant
        # The account is logged so a "no credential account available" refusal is diagnosable. A path, never a
        # key.
        log.info(
            "direct console: granted %s/%s/%s to admin session %s (token %s, %.1fh, account %s, %s)",
            project, number, kind, session_id, grant.fingerprint, (expires_at - now) / 3600.0,
            config_dir or api_key_account or ("Jaah account" if bedrock else "-"), "read-only" if read_only else "read-write",
        )
        # Always sent: its presence tells the CP this node understands the tier.
        return {"ok": True, "read_only": read_only}

    def revoke(self, frame: dict) -> int:
        """Drop every grant a `console_direct_revoke` matches. Returns how many went.

        Fire-and-forget; keyed on `session_id` + slot, `token` narrowing. Accepted residual: a revoke lost
        to a bus disconnect leaves the grant live until expiry — a validity poll would put the CP back in
        the path."""
        if not node_frame_auth.frame_auth_ok(frame, expected_token=console_mod._expected_token):
            log.warning("direct console: revoke ignored — invalid/missing cp_frame_proof")
            return 0
        session_id = frame.get("session_id")
        # `all`: every grant on this node — its org changed (`app/tenancy/transfer_node.py`), so no grant the old
        # org's viewers hold may outlive the move.
        revoke_all = frame.get("all") is True
        if not revoke_all and (not isinstance(session_id, str) or not session_id):
            log.warning("direct console: revoke ignored — no session_id to key on")
            return 0
        project = "" if frame.get("project") is None else str(frame.get("project"))
        number = "" if frame.get("number") is None else str(frame.get("number"))
        kind = frame.get("kind")
        token = frame.get("token")
        narrowed = _token_digest(token) if isinstance(token, str) and token else None

        dropped = []
        for digest, grant in list(self._grants.items()):
            if revoke_all:
                self._grants.pop(digest, None)
                dropped.append(grant)
                continue
            if grant.session_id != session_id:
                continue
            if project and grant.project != project:
                continue
            if number and grant.number != number:
                continue
            if kind and grant.kind != kind:
                continue
            if narrowed is not None and digest != narrowed:
                continue
            self._grants.pop(digest, None)
            dropped.append(grant)
        for grant in dropped:
            # Per grant: all are already popped, so one raise would leave the rest's sockets live — a silent
            # partial revoke.
            try:
                closed = self._disconnect(_token_digest(grant.token))
            except Exception as exc:   # noqa: BLE001 — a revoke must drop every match it can
                log.warning("direct console: could not close live sessions for grant %s: %s",
                            grant.fingerprint, exc)
                closed = 0
            log.info("direct console: revoked %s/%s/%s for admin session %s (token %s), "
                     "closing %d live session(s)",
                     grant.project, grant.number, grant.kind, grant.session_id, grant.fingerprint,
                     closed)
        if not dropped:
            log.info("direct console: revoke for admin session %s matched no live grant", session_id)
        return len(dropped)

    def attach(self, grant: DirectGrant, ws: object) -> bool:
        """Record that `ws` is being served under `grant`. False if the grant is no longer live.

        Closes a race: a revoke can land in the await window between `redeem` and here. Check and
        registration happen with no await between them."""
        digest = _token_digest(grant.token)
        if digest not in self._grants:
            return False
        self._live.setdefault(digest, []).append(ws)
        return True

    def detach(self, grant: DirectGrant, ws: object) -> None:
        """Forget a finished socket. Idempotent (the `finally` and a revoke's close can both land here)."""
        digest = _token_digest(grant.token)
        sockets = self._live.get(digest)
        if sockets is None:
            return
        if ws in sockets:
            sockets.remove(ws)
        if not sockets:
            self._live.pop(digest, None)

    def _disconnect(self, digest: str) -> int:
        """Close every socket currently served by `digest`. Returns how many were closed.

        Fire-and-forget: `revoke` runs synchronously on the bus's read loop. Code 4401, not 1000 — the
        authorization ended, not the session."""
        sockets = self._live.pop(digest, [])
        for ws in sockets:
            # A strong reference until the close completes: asyncio holds only a weak one to a task.
            task = asyncio.ensure_future(console_mod._close_ws(ws, CLOSE_UNAUTHORIZED))
            self._closing.add(task)
            task.add_done_callback(self._closing.discard)
        return len(sockets)

    def redeem(self, token: str, key: Tuple[str, str, str]) -> Optional[DirectGrant]:
        """The grant authorizing `token` for `key`, or None.

        Every rejection collapses to None and one close code (4401), so there is no slot-enumeration
        oracle. Lookup is by digest, so cost does not depend on how much of the token is correct."""
        self.prune()
        grant = self._grants.get(_token_digest(token))
        if grant is None:
            return None
        if grant.key != key:
            return None
        return grant


# ── The listener ─────────────────────────────────────────────────────────────────────────────────────

# The subprotocol the server speaks, and the ONLY one it ever echoes back.
CONSOLE_SUBPROTOCOL = "jaah.console.v1"
# The credential-carrying subprotocol prefix. A header that does not tokenise is rejected by the library's
# parser — the one permitted upgrade-time refusal.
_TOKEN_SUBPROTOCOL_PREFIX = "tok."
# The slot path a direct connection names: /console/{project}/{number}/{kind}.
_PATH_PREFIX = "/console/"

# Browser-facing close codes: no relay translates here, and a raw 4000 reads as retryable to a browser.
CLOSE_MALFORMED_HELLO = 4400        # missing, late, binary or unparseable hello frame
CLOSE_UNAUTHORIZED = 4401           # token missing / invalid / expired / foreign
CLOSE_UNKNOWN_SLOT = 4404           # the path names no console slot this node can resolve
CLOSE_NO_LIVE_SESSION = 4502        # attach_only and there is nothing attached here
# Taken from `console` (owner of `_viewer_close_code`) so the two cannot drift.
CLOSE_RETRYABLE = console_mod._BROWSER_CLOSE_CODE_RECOVERABLE      # 4503 — transient: try again

# How long the client has to send its hello after the socket opens.
HELLO_TIMEOUT_SECONDS = 5.0
# Keepalive: a silent peer is dropped after roughly the sum, detaching a vanished browser's viewer.
PING_INTERVAL_SECONDS = 20.0
PING_TIMEOUT_SECONDS = 20.0
# Also bounds the TLS handshake (`ssl_handshake_timeout` derives from it); never pass `ssl_shutdown_timeout`
# (absent on 3.9).
OPEN_TIMEOUT_SECONDS = 15.0
CLOSE_TIMEOUT_SECONDS = 10.0


@dataclass(frozen=True)
class _Hello:
    cols: int
    rows: int
    attach_only: bool
    # What this console shows (`console_key_target`), resolved in `parse_hello` so a bad value closes with a
    # code the browser renders, the same verdict on both transports.
    target: str = ""


class _HelloError(Exception):
    """A hello that cannot be honoured, carrying the close code to deliver over the accepted socket."""

    def __init__(self, code: int, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


def parse_offered_token(subprotocols: Sequence[str]) -> Optional[str]:
    """The direct token out of the client's offered subprotocols, or None when it offered none.

    Read-only: also called during subprotocol selection, which may never refuse (module docstring)."""
    for offered in subprotocols:
        text = str(offered)
        if text.startswith(_TOKEN_SUBPROTOCOL_PREFIX):
            token = text[len(_TOKEN_SUBPROTOCOL_PREFIX):]
            return token or None
    return None


def parse_slot_path(path: str) -> Optional[Tuple[str, str, str]]:
    """`(project, number, kind)` out of `/console/{project}/{number}/{kind}`, or None when the path names
    no slot. The query string is ignored — nothing rides it."""
    just_path = urllib.parse.urlsplit(path).path
    if not just_path.startswith(_PATH_PREFIX):
        return None
    parts = [urllib.parse.unquote(part) for part in just_path[len(_PATH_PREFIX):].split("/")]
    if len(parts) != 3:
        return None
    project, number, kind = (part.strip() for part in parts)
    if not project or not number or kind != "clauded":
        return None
    return (project, number, kind)


def parse_hello(raw: object) -> _Hello:
    """The hello frame's contents, or `_HelloError` with the code to close on.

    Text only: a binary `{` would be frame type 123 in the envelope that follows. Dimensions go through the
    CP path's `_clamp_dim`, so both transports behave identically."""
    if not isinstance(raw, str):
        raise _HelloError(CLOSE_MALFORMED_HELLO, "the hello frame was binary, not text")
    try:
        hello = json.loads(raw)
    except ValueError as exc:
        raise _HelloError(CLOSE_MALFORMED_HELLO, "the hello frame is not JSON ({})".format(exc))
    if not isinstance(hello, dict):
        raise _HelloError(CLOSE_MALFORMED_HELLO, "the hello frame is not a JSON object")
    # `view` names one window of the shared `fleet` session instead of the slot's derived one.

    # The CP-frame path's checks, in the same order, so no transport is a laxer door: shape (`@<digits>`)
    # before the value reaches a key or argv; existence at resolve time.
    for field_name in ("view", "layout"):
        value = hello.get(field_name)
        if value is not None and not isinstance(value, str):
            raise _HelloError(CLOSE_MALFORMED_HELLO, f"the hello's `{field_name}` is not a string")
    # One resolver shared with the CP-frame path (refuses both fields at once). Every verdict is about shape,
    # hence `CLOSE_UNKNOWN_SLOT`.
    target, error = console_mod.console_key_target(hello.get("view"), hello.get("layout"))
    if error is not None:
        raise _HelloError(CLOSE_UNKNOWN_SLOT, error[:200])
    return _Hello(
        cols=console_mod._clamp_dim(hello.get("cols"), default=80),
        rows=console_mod._clamp_dim(hello.get("rows"), default=24),
        attach_only=bool(hello.get("attach_only")),
        target=target,
    )


class DirectConsoleListener:
    """One inbound TLS WebSocket listener on this node's tailnet addresses. No PTY logic of its own:
    sessions go through `ConsoleBridge.attach_direct_viewer` and the bridge's teardown paths."""

    def __init__(
        self,
        bridge: "console_mod.ConsoleBridge",
        grants: DirectGrantStore,
        cert: DirectConsoleCert,
        *,
        node_id: str,
        dns_name: str,
        addresses: Sequence[str],
        port: int,
        report_use: Optional[Callable[[str, str], Awaitable[None]]] = None,
    ) -> None:
        self._bridge = bridge
        self._grants = grants
        self._cert = cert
        self._node_id = node_id
        self._dns_name = dns_name
        self._addresses = tuple(addresses)
        self._port = port
        # Wired later by `main` (the bus is built after this): `NodeBusClient.send_direct_used`, or None.
        # Audit logging never depends on it.
        self.report_use = report_use
        self._server = None
        self._renewal_task: Optional[asyncio.Task] = None

    @property
    def grants(self) -> DirectGrantStore:
        return self._grants

    @property
    def addresses(self) -> Tuple[str, ...]:
        return self._addresses

    @property
    def port(self) -> int:
        return self._port

    def published_url(self) -> Optional[str]:
        """The origin to publish — `wss://<tailnet name>:<port>`, no path — or None while nothing is bound.

        What is bound, not what could be: a URL for a down listener is a closed port a browser cannot tell
        from a fault."""
        if self._server is None:
            return None
        return "wss://{}:{}".format(self._dns_name, self._port)

    def _select_subprotocol(self, connection, subprotocols) -> Optional[str]:
        """Echo back only the version (`jaah.console.v1`), never the credential.

        May not refuse: the library's default aborts with HTTP 400 when the client offers none, which a
        browser sees as 1006 and retries. A token-less connection is accepted and closed 4401 in the
        handler."""
        return CONSOLE_SUBPROTOCOL if CONSOLE_SUBPROTOCOL in [str(item) for item in subprotocols] else None

    async def start(self) -> None:
        """Load the certificate, bind, and arm the renewal timer. Raises on any of the three — no
        half-listener."""
        # Lazy: `websockets` is a pinned dependency of the node's venv (infra/jaah/requirements.txt);
        # tests import this module directly.
        from websockets.asyncio.server import serve

        # Raises when the grid is unreadable ("could not check" is deliberately not caught). A conflict
        # refuses the bind: a singleton squatting a grid port breaks a worktree's stack with a far-away
        # EADDRINUSE.
        conflict = port_grid_conflict(self._port)
        if conflict is not None:
            raise ValueError(conflict)
        if not self._addresses:
            raise ValueError("direct console has no tailnet address to bind — refusing a wildcard bind")
        # Off the loop: `tailscale cert` can take tens of seconds (120s bound) and would block the presence
        # heartbeat, which the CP flips OFFLINE after 30s.
        await asyncio.get_event_loop().run_in_executor(None, self._cert.ensure_loaded)
        self._server = await serve(
            self._handle,
            # Concrete tailnet addresses, never a wildcard or None ("every interface"). AC8.6 is this line.
            list(self._addresses),
            self._port,
            ssl=self._cert.context(),
            # No permessage-deflate: no daemon hop negotiates it (node_bus.py and console.py dial with
            # compression=None too), and PTY bytes barely compress anyway. Spelled, never defaulted.
            compression=None,
            select_subprotocol=self._select_subprotocol,
            open_timeout=OPEN_TIMEOUT_SECONDS,
            close_timeout=CLOSE_TIMEOUT_SECONDS,
            ping_interval=PING_INTERVAL_SECONDS,
            ping_timeout=PING_TIMEOUT_SECONDS,
            # Never DEBUG: the library's debug output contains the credential (`_ws_log`).
            logger=_ws_log,
            # No `ssl_shutdown_timeout`: TypeError on 3.9 nodes (module docstring).
        )
        self._renewal_task = asyncio.ensure_future(self._cert.renewal_loop())
        log.info("direct console listening on %s (wss, tailnet-only)",
                 ", ".join("{}:{}".format(addr, self._port) for addr in self._addresses))

    async def stop(self) -> None:
        if self._renewal_task is not None:
            # Awaited, not just cancelled, or asyncio warns "Task was destroyed but it is pending" at GC.
            self._renewal_task.cancel()
            await asyncio.gather(self._renewal_task, return_exceptions=True)
            self._renewal_task = None
        if self._server is not None:
            self._server.close()
            try:
                await self._server.wait_closed()
            except Exception as exc:   # noqa: BLE001 — shutdown must not raise past here
                log.warning("direct console: listener close failed (%s)", exc)
            self._server = None

    async def _handle(self, ws) -> None:
        """Serve one accepted direct connection, from handshake to teardown.

        Every refusal is on an accepted socket with an application close code. Auth before hello: no slot
        information and no PTY before the grant is checked."""
        peer = _peer_name(ws)
        try:
            key = parse_slot_path(getattr(ws.request, "path", "") or "")
            if key is None:
                await self._refuse(ws, CLOSE_UNKNOWN_SLOT, peer,
                                   "path {!r} names no console slot".format(
                                       getattr(ws.request, "path", "")[:120]))
                return

            token = parse_offered_token(_offered_subprotocols(ws))
            if token is None:
                await self._refuse(ws, CLOSE_UNAUTHORIZED, peer, "no direct token was offered")
                return
            grant = self._grants.redeem(token, key)
            if grant is None:
                # One code for every rejection class; the log names the class, never the token.
                await self._refuse(ws, CLOSE_UNAUTHORIZED, peer,
                                   "no live grant authorizes token %s… for %s" % (
                                       _token_digest(token)[:8], "/".join(key)))
                return

            try:
                hello = await self._read_hello(ws)
            except _HelloError as exc:
                # Explained: the caller is already authenticated, so the detail reveals nothing.
                await self._refuse(ws, exc.code, peer, exc.detail, explain=True)
                return

            await self._serve_session(ws, key, grant, hello, peer)
        except asyncio.CancelledError:
            raise
        except Exception:
            # Our bug still leaves the browser an actionable code; retryable, since it is no verdict on the
            # session.
            log.exception("direct console: handler failed for %s", peer)
            await console_mod._close_ws(ws, CLOSE_RETRYABLE)

    async def _read_hello(self, ws) -> _Hello:
        """The first frame, bounded. A silent client closes 4400 like a malformed one."""
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=HELLO_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            raise _HelloError(CLOSE_MALFORMED_HELLO, "no hello frame within {:.0f}s".format(
                HELLO_TIMEOUT_SECONDS))
        except Exception as exc:
            raise _HelloError(CLOSE_MALFORMED_HELLO, "the socket ended before a hello ({})".format(exc))
        return parse_hello(raw)

    async def _serve_session(self, ws, key, grant: DirectGrant, hello: _Hello, peer: str) -> None:
        """Attach this connection to the slot's PTY and hold the handler open until it closes.

        `websockets` closes the connection when the handler returns, so the handler must outlive the
        session."""
        opened_at = time.monotonic()
        # The CP's requirement ORed with the browser's: a CP requirement only narrows (`_open_worktree_pty`
        # refuses `read_only and not attach_only`). Never the reverse: only the browser knows a first open
        # from an auto-reconnect.

        # NOT the other direction: the browser's own `attach_only` distinguishes a first open from an
        # auto-reconnect, which only it can know, so a grant saying `false` must not switch it off.
        attach_only = grant.attach_only or hello.attach_only

        # A named window needs the grant's permission: `hello.target` is browser-composed and
        # `_fleet_window_socket` resolves it against the whole shared session. The CP decides
        # (`routes/console.py::_may_name_window`); enforced here because the hello never passes the CP.
        # Unknown-slot code, so no window oracle.
        if hello.target and not grant.view_allowed:
            log.warning(
                "direct console: refusing view %s on %s-%s/%s — this grant may not name a window",
                hello.target, grant.project, grant.number, grant.kind,
            )
            raise _HelloError(CLOSE_UNKNOWN_SLOT, "console view is not available")

        async def ack() -> None:
            """`{"ok":true}`, sent between resolving the session and attaching the viewer: once attached
            the pump may write PTY bytes at once, and the ack must precede them."""
            await ws.send(json.dumps({"ok": True}))

        try:
            # The key gains the view member; `attach_direct_viewer` normalises "no view" to `""` exactly as
            # the CP path does (two spellings = two tmux clients on one window). `config_dir` comes from the
            # grant, never the hello: a client naming it would pick who a created shell authenticates as.

            # NAMING A WINDOW IS GATED ABOVE, on the grant's `view_allowed`: it is a SECOND slot claim,
            # not a restatement of the one the grant carries. `_resolve_session` re-checks the id.

            # `config_dir` COMES FROM THE GRANT, NEVER FROM THE HELLO. It is the account a CREATE runs
            # under, and a client that could name it would pick who the created shell authenticates as —
            # which is the whole reason the hello is not trusted for it.
            vid = await self._bridge.attach_direct_viewer(
                key + (hello.target,), ws, rows=hello.rows, cols=hello.cols,
                attach_only=attach_only,
                # The grant's own session claim; the browser-composed hello carries none.
                session_name=grant.tmux_session or None,
                before_attach=ack,
                # The account, its shared credential store and the tier all come from the grant, never the
                # hello: with no CP in the data path, the minted `read_only` is the only thing between a view
                # principal and a writable shell. `cols`/`rows` are discarded in `_resolve_session`.
                config_dir=grant.config_dir,
                securestorage_dir=grant.securestorage_dir,
                provider=grant.provider, api_key_account=grant.api_key_account, bedrock=grant.bedrock,
                **console_mod._guard_kw(grant.guard_project),
                read_only=grant.read_only,
            )
        except console_mod._ConsoleOpenError as exc:
            # The effective value: under a CP-forced attach-only a definitive failure must read as 4502.
            code = self._establish_close_code(exc, attach_only=attach_only)
            await self._refuse(
                ws, code, peer, "could not establish: {}".format(exc.error), explain=True,
            )
            return

        # Audit: admin session and slot, never the token; `view` distinguishes a slot attach from another
        # fleet window.
        log.info(
            "direct console OPEN %s/%s/%s shows=%s vid=%s admin_session=%s token=%s peer=%s",
            key[0], key[1], key[2], hello.target or "-", vid, grant.session_id, grant.fingerprint, peer,
        )
        await self._report_use(grant, "open")
        # Revocable from exactly here: earlier exposes a not-yet-serving socket, later leaves a revoked shell
        # live.
        if not self._grants.attach(grant, ws):
            # Revoked while establishing (see `attach`); closing the socket detaches the viewer.
            log.info("direct console: grant %s was revoked while %s/%s/%s was establishing — closing",
                     grant.fingerprint, key[0], key[1], key[2])
            await self._refuse(ws, CLOSE_UNAUTHORIZED, peer, "revoked during establishment")
            await self._report_use(grant, "close")
            return
        try:
            await self._hold_until_closed_or_expired(ws, grant, key)
        finally:
            self._grants.detach(grant, ws)
            log.info(
                "direct console CLOSE %s/%s/%s vid=%s admin_session=%s token=%s code=%s after %.1fs",
                key[0], key[1], key[2], vid, grant.session_id, grant.fingerprint,
                getattr(ws, "close_code", None), time.monotonic() - opened_at,
            )
            await self._report_use(grant, "close")

    async def _hold_until_closed_or_expired(self, ws, grant: DirectGrant, key) -> None:
        """Hold the handler open for the session's life, but never past the grant's own expiry.

        `redeem` checks expiry once, and neither ping/pong nor the PTY idle sweep (refreshed by output)
        bounds it. `asyncio.wait` with a timeout, not `wait_for`, which would cancel — the socket must
        survive to be closed."""
        waiter = asyncio.ensure_future(ws.wait_closed())
        try:
            remaining = grant.expires_at - time.time()
            if remaining <= 0:
                remaining = 0
            done, _pending = await asyncio.wait([waiter], timeout=remaining)
            if not done:
                log.info(
                    "direct console: grant %s expired under a live session %s/%s/%s — closing",
                    grant.fingerprint, key[0], key[1], key[2],
                )
                await console_mod._close_ws(ws, CLOSE_UNAUTHORIZED)
                await waiter
        finally:
            if not waiter.done():
                waiter.cancel()

    def _establish_close_code(self, exc, *, attach_only: bool) -> int:
        """Map an establishment failure onto the browser's vocabulary.

        `retryable` is load-bearing: an indeterminate read must not report "nothing here". Definitive: 4502
        under `attach_only`, else 4404."""
        if exc.retryable:
            return CLOSE_RETRYABLE
        return CLOSE_NO_LIVE_SESSION if attach_only else CLOSE_UNKNOWN_SLOT

    async def _refuse(self, ws, code: int, peer: str, detail: str, *, explain: bool = False) -> None:
        """Close this connection with an application code, and log why in full.

        `explain` also puts the detail on the wire; off by default, because most refusals are
        authentication verdicts and the detail would be a slot-enumeration oracle. Establishment refusals
        explain (the caller redeemed a valid grant); the text is collapsed and capped at 123 bytes by
        `_close_ws`."""
        log.warning("direct console REFUSED %s: %s (close %d)", peer, detail, code)
        # One line: the byte cap would cut a multi-line error mid-message.
        await console_mod._close_ws(ws, code, " ".join(detail.split()) if explain else "")

    async def _report_use(self, grant: DirectGrant, event: str) -> None:
        """Tell the CP a grant was used. Best-effort: an undeliverable report never stops serving (logged
        locally)."""
        report = self.report_use
        if report is None:
            return
        try:
            await report(grant.token, event)
        except Exception as exc:   # noqa: BLE001 — audit reporting may never break a session
            log.warning("direct console: could not report %s use to the control plane: %s", event, exc)


def _offered_subprotocols(ws) -> List[str]:
    """Every offered subprotocol, across however many headers. Read from the request, not from selection."""
    try:
        headers = ws.request.headers
    except AttributeError:
        return []
    offered: List[str] = []
    for value in headers.get_all("Sec-WebSocket-Protocol"):
        offered.extend(item.strip() for item in str(value).split(",") if item.strip())
    return offered


def _peer_name(ws) -> str:
    """A log-safe peer label. `remote_address` is a tuple on TCP and absent on a stand-in."""
    remote = getattr(ws, "remote_address", None)
    if isinstance(remote, tuple) and remote:
        return "{}:{}".format(remote[0], remote[1] if len(remote) > 1 else "?")
    return str(remote or "?")


async def build_listener(
    bridge: "console_mod.ConsoleBridge",
    *,
    node_id: str,
    cert_dir: Path,
) -> Optional[DirectConsoleListener]:
    """Build and start the direct listener for this node, or return None when it cannot serve one.

    No enable flag; the precondition is a tailnet identity. No identity returns None (an ordinary permanent
    state, not an error on every boot); anything failing after it — a grid conflict, no certificate —
    raises."""
    # Off the loop (see `start`): `tailscale status` has a 10s bound.
    identity = await asyncio.get_event_loop().run_in_executor(None, resolve_tailnet_identity)
    if identity is None:
        # Says what the browser will do: publishing no address makes the CP answer `disabled`, and a
        # browser with the direct toggle on then uses the relay rather than retrying this node.
        log.info(
            "direct console: this node has no tailnet identity under %s=%r, so it serves no direct "
            "consoles — browsers use the control plane relay", TAILNET_DNS_SUFFIX_VAR,
            resolve_tailnet_dns_suffix(),
        )
        return None
    port = resolve_direct_console_port()
    cert = DirectConsoleCert(
        identity.dns_name,
        cert_dir,
        cert_file=env_get("JAAH_DIRECT_CONSOLE_CERT_FILE") or None,
        key_file=env_get("JAAH_DIRECT_CONSOLE_KEY_FILE") or None,
    )
    listener = DirectConsoleListener(
        bridge, DirectGrantStore(node_id), cert,
        node_id=node_id, dns_name=identity.dns_name, addresses=identity.addresses, port=port,
    )
    await listener.start()
    return listener
