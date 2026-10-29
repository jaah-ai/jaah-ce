"""A slot's port and stack wiring in `.env` — the CP's band, tier-2 auto ports, stack and node identity."""
from __future__ import annotations

import logging
import os
import re
import socket
import threading
from typing import Iterable

import environment
import host_platform

from worktree_mechanics import core, env_file

log = logging.getLogger("jaahd.worktree_mechanics.ports")

# Pool-root reads use `environment.pool_root_raw()` (UNSTRIPPED): both readers normalise the value
# themselves, and a `.strip()` on the way in would change what a whitespace-bearing value resolves to.

# The deterministic port grid is NOT here: the CP ships each slot's band, `apply_port_band` writes it and
# REFUSES an absent one ("write nothing" leaves every slot on the same `.env.example` port). Tier 2: a
# declared `port_vars` entry with no range start gets a flat auto port from `apply_auto_ports` — the one
# port decision the node makes. The grid's audit/`--fix` tool loads this module by
# path for `_as_port`, `_env_embedded_ports` and the regexes (`tools/jaahd_module.py`) — no copy of them there.


def _env_port(wt: str, var: str) -> str:
    """Read VAR from a worktree's .env — first `^\\s*VAR=` line, trailing comment and quotes stripped.
    "" if unset or unreadable."""
    try:
        text = env_file.env_text(os.path.join(wt, ".env"))
    except OSError:
        return ""
    pat = re.compile(r"^\s*" + re.escape(var) + r"=")
    for line in text.splitlines():
        if pat.match(line):
            return env_file._clean_env_value(pat.sub("", line, count=1))
    return ""


def wt_ports_json(wt: str, port_vars: Iterable[str]) -> dict:
    """Ports a worktree is actually configured with, read from its own .env."""
    obj: dict[str, int] = {}
    for var in port_vars:
        if not var:
            continue
        v = _env_port(wt, var)
        if re.fullmatch(r"[0-9]+", v or ""):
            obj[var] = int(v)
    return obj


# `PORT` as an underscore-delimited TOKEN, not a substring — a SECURITY boundary: `env_port_var_names`
# publishes KEY AND VALUE into a runner's environment out of the slot's secrets file, and `PASSPORT`,
# `SUPPORT`, `EXPORT`, `REPORT`, `PORTAL`, `TRANSPORT` all contain `PORT` (`.endswith` is not enough
# either), so a numeric PIN under such a name would clear both this and `wt_ports_json`'s digits filter.
# Costs nothing: every name the grid seeds (`apps/api/app/port_grid.py`) is `*_PORT`. A hypothetical
# `PORTFOO=` goes unpublished — a runner told one port fewer, the direction to be wrong in.
_PORT_VAR_TOKEN_RX = re.compile(r"(?:^|_)PORTS?(?:_|$)", re.IGNORECASE)


def env_port_var_names(wt: str) -> list[str]:
    """Every KEY in this worktree's `.env` that NAMES a port, in file order, deduplicated.

    The half `wt_ports_json` cannot supply: it reads VALUES for names it is handed, and
    `fleet_dispatch.build_auth_lines` holds no roster at launch — only the leased worktree. Matching is on
    the NAME via `_PORT_VAR_TOKEN_RX`, never on a port-looking value; `wt_ports_json`'s all-digits check is
    the second, independent gate. `_ENV_KV_RX` (resolved at call time) defines "assignment": `export FOO=`
    included, `#`-commented lines excluded.

    Known divergences, all inherited from `_env_port`:
      * FIRST-WINS, while compose and the compose env guard (`| tail -1`) are LAST-wins.
      * PHYSICAL LINES, not the quoted-value fold the pool conformance check's `logical_env_lines`
        performs, so a `KEY=1234` inside a multi-line value reads as an assignment.
      * `_env_port` does not accept `export VAR=`, so an export-form port yields a name and no value.
    All three under- or mis-report a PORT; none can publish a value from a key this filter rejects.
    """
    env_path = os.path.join(wt, ".env")
    try:
        text = env_file.env_text(env_path)
    except OSError as exc:
        # LOUD: silence is indistinguishable from "binds no ports", and an unreadable `.env` otherwise
        # produces no diagnostic anywhere.
        log.warning("could not read %s to find its port vars (%s)", env_path, exc)
        return []
    names: list[str] = []
    for line in text.splitlines():
        match = env_file._ENV_KV_RX.match(line)
        if match and _PORT_VAR_TOKEN_RX.search(match.group(1)) and match.group(1) not in names:
            names.append(match.group(1))
    return names


def wt_urls_json(ports: dict) -> dict:
    """localhost URLs derived from the ports: frontend/web/ui→web, api/backend/server→api, else the var.
    Later keys win."""
    urls: dict[str, str] = {}
    for key, value in ports.items():
        lk = str(key).lower()
        if re.search(r"front|web|ui", lk):
            role = "web"
        elif re.search(r"api|back|server", lk):
            role = "api"
        else:
            role = key
        urls[role] = f"http://localhost:{value}"
    return urls


def has_stack(wt: str) -> bool:
    """Does this worktree have a local stack we can bring down (a `dc` wrapper or a compose file)?"""
    dc = os.path.join(wt, "dc")
    if os.path.isfile(dc) and os.access(dc, os.X_OK):
        return True
    for pattern in ("docker-compose*.y*ml", "compose*.y*ml"):
        import glob as _glob

        for match in _glob.glob(os.path.join(wt, pattern)):
            if os.path.exists(match):
                return True
    return False


def apply_port_band(
    wt: str, band: "dict[str, int] | None", *, port_vars: "list[str] | tuple[str, ...]" = (),
    auto_ports: "tuple[int, int] | None" = None,
) -> None:
    """WRITE the CP-computed port band (`{VAR: port}` from `apps/api/app/fleet_decide.py::port_band`) into
    a worktree's .env; nothing here derives a port.

    FAIL-CLOSED and ALL-OR-NOTHING: an absent or malformed band raises `ResetRefused` before a single var
    is written — writing nothing leaves the slot on the `.env.example` seed, identical in every slot.
    `port_vars` alone tells "binds no host ports" from "the CP said nothing"; both arrive as an empty
    mapping. A worktree with NO `.env` is the one non-refusing gap.

    `auto_ports` is the tier-2 `(floor, ceiling)` off the pool tick's `grid` block, threaded here because
    this tail call is the only entry into tier 2. None keeps the module defaults (a bus `claim`, a test)."""
    if band is None:
        raise core.ResetRefused(
            "port-band-absent: the control plane sent no port band for this slot. The node deleted the "
            "deterministic port grid (the CP computes it and ships it on the "
            "pool-tick roster slot and in the lease intent payload) and will not fall back to the .env "
            "seed, whose ports are identical in every slot"
        )
    if not isinstance(band, dict):
        raise core.ResetRefused(
            f"port-band-malformed: expected a {{VAR: port}} mapping from the CP, got "
            f"{type(band).__name__} ({band!r})"
        )
    clean: dict[str, int] = {}
    for var, port in band.items():
        name = str(var or "").strip()
        if not name:
            raise core.ResetRefused(f"port-band-malformed: empty port-var name in the CP's band {band!r}")
        # A band key is written as a `.env` KEY (see `_PORT_VAR_NAME_RX`); checked where it is acted on.
        if not _port_var_name_ok(name):
            raise core.ResetRefused(
                f"port-band-malformed: {name!r} is not a usable port-var name — a band key is written as a "
                f".env KEY, so it must be an env identifier (a name carrying a newline or an '=' would "
                f"inject an arbitrary assignment) and must not be one of "
                f"{sorted(_PORT_VAR_RESERVED_NAMES)} (which would SHADOW this node's own credential or "
                f"identity with a port)"
            )
        # `bool` is an `int` in Python and `True` would band a slot onto port 1. Rejected, not coerced.
        if isinstance(port, bool) or not isinstance(port, int):
            raise core.ResetRefused(
                f"port-band-malformed: {name}={port!r} is not an integer port — the CP decides ports and "
                f"this module has no grid left to coerce or default one from"
            )
        if not 1 <= port <= 65535:
            raise core.ResetRefused(f"port-band-malformed: {name}={port} is not a host port (1-65535)")
        clean[name] = port
    declared = [str(v).strip() for v in (port_vars or []) if str(v).strip()]
    # Tier 2: a declared var the band misses is ASSIGNED a flat port, not refused ("no declared var keeps
    # the shared seed", acted on). `port_vars` with no `port_block` is graded by
    # the pool conformance check; refusing here too would re-block the case tier 2 exists for.
    missing = [v for v in declared if v not in clean]
    if not clean and not missing:
        # No `port_vars`, no band: a repo that binds no host ports — a fact, not a silence.
        return
    env_path = os.path.join(wt, ".env")
    if not os.path.isfile(env_path):
        # LOUD but not a refusal: an absent `.env` is a REPO-contract fault the pool conformance check
        # gates; refusing here would make the project's pool unmaterialisable on every node.
        log.warning(
            "apply_port_band: %s has no .env — the CP's band %s could not be written. The repo must ship "
            "a .env.example for the slot to seed from; until it does, this stack falls back to its compose "
            "defaults, which are the same in every slot",
            wt, sorted(clean.items()),
        )
        return
    env_file._ensure_eol_nl(env_path)
    # Accumulated in the band's own order, written once (`_set_env_vars`).
    assignments: dict[str, str] = {}
    embedded: list[tuple[int, int]] = []
    for var, port in clean.items():
        old = _env_port(wt, var)
        assignments[var] = str(port)
        if old and old.isdigit() and old != str(port):
            embedded.append((int(old), int(port)))
    env_file._set_env_vars(env_path, assignments, embedded)
    # Tier 2 runs LAST and reads the file back, so the ports just written are already avoided. `declared`
    # (not `missing`) on purpose: "which var is tier 2" is defined in the function that acts on it. The
    # range is forwarded, not re-derived — passing nothing would stop an operator's edit one hop short.
    if auto_ports is None:
        apply_auto_ports(wt, port_vars=declared, band=clean)
    else:
        floor, ceiling = auto_ports
        apply_auto_ports(wt, port_vars=declared, band=clean, floor=floor, ceiling=ceiling)


# ── The AUTO tier (tier 2): a declared port the port grid has no window for ────────────────────────────
# ONE FLAT RANGE ABOVE EVERY GRID WINDOW (highest: CDP_BRIDGE_PORT's 18800-19799), so an auto port is
# never mistaken for a banded one and is invisible to the CP's drift comparison by design. Collision
# reporting (the pool port audit) still covers it.
AUTO_PORT_FLOOR = 20800
# Last port below the kernel's ephemeral floor (`net.ipv4.ip_local_port_range` = 32768-60999 here, also
# docker's dynamic-publish source): a service bound inside it is a once-a-week EADDRINUSE. ~12k ports.
AUTO_PORT_CEILING = 32767
# Host-wide reservations, inclusive `(first, last)`. 32768-33279 is carved out via
# `net.ipv4.ip_local_reserved_ports` for docker's dynamic publishing (sysctl-jaah.conf's header). Above
# AUTO_PORT_CEILING today; the check exists so RAISING the ceiling cannot hand out docker's ports.
RESERVED_PORT_RANGES: tuple[tuple[int, int], ...] = ((32768, 33279),)


def auto_port_range_from_grid(grid: object) -> "tuple[int, int]":
    """`(floor, ceiling)` for the tier-2 walk from the CP's grid block (`auto_port_floor`/`auto_port_ceiling`),
    defaulting to the constants above. THE SINGLE HOME for that read: the allocator (`apply_auto_ports`)
    and the direct console's refusal (`console_direct.port_grid_conflict`) must never disagree, or a
    console bound inside the walked range crash-loops on EADDRINUSE.

    Absent or unparseable FALLS BACK (operator knobs, not a CP table copy); `bool` excluded — a stray
    `true` would put the floor at 1. An inverted pair is rejected WHOLE: half-applying it hands
    `_pick_auto_port` an empty interval and every lease reads as `auto-port-exhausted`."""
    if not isinstance(grid, dict):
        return AUTO_PORT_FLOOR, AUTO_PORT_CEILING

    def _knob(key: str, default: int) -> int:
        raw = grid.get(key)
        if isinstance(raw, bool) or not isinstance(raw, int) or not 1 <= raw <= 65535:
            if raw is not None:
                log.warning(
                    "auto_port_range_from_grid: the control plane's %s is %r, not a host port — falling "
                    "back to this node's default (%d)", key, raw, default,
                )
            return default
        return raw

    floor = _knob("auto_port_floor", AUTO_PORT_FLOOR)
    ceiling = _knob("auto_port_ceiling", AUTO_PORT_CEILING)
    if floor > ceiling:
        log.warning(
            "auto_port_range_from_grid: the control plane's tier-2 range is inverted (%d-%d) — falling "
            "back WHOLE to this node's defaults (%d-%d) rather than allocating out of an empty interval",
            floor, ceiling, AUTO_PORT_FLOOR, AUTO_PORT_CEILING,
        )
        return AUTO_PORT_FLOOR, AUTO_PORT_CEILING
    return floor, ceiling
_EMBEDDED_PORT_RX = re.compile(r"(?:localhost|127\.0\.0\.1|0\.0\.0\.0):([0-9]+)")
# A `port_vars` name is operator free text that becomes a `.env` write KEY here, never charset-checked
# upstream. A name carrying a newline or `=` would inject a second assignment (`FOO\nCOMPOSE_PROJECT_NAME=
# shared`), and compose reads the last one — every slot of a project onto shared volumes.
_PORT_VAR_NAME_RX = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
# The charset stops INJECTION, not SHADOWING: a `port_vars` row named `NODE_TOKEN` writes `NODE_TOKEN=<port>`
# into every slot's `.env`, and `./dc`'s compose-env-guard force-reads those keys into each stack — node auth
# breaks from an edit to a table about ports. These three are what `/etc/jaah/node.env` defines
# (`jaah_cli._write_node_env`). The CP's `port_grid.py::VAR_NAME_MUST_CONTAIN` already refuses them at
# declaration, but a row that never passed that door (seeded, older build, restored backup) arrives here
# unrefused. A REFUSAL, narrower than the CP's rule: a merely misdeclared name stays a one-slot warning.
_PORT_VAR_RESERVED_NAMES = frozenset({"NODE_ID", "NODE_NAME", "NODE_TOKEN"})


def _port_var_name_ok(name: str) -> bool:
    """Is `name` usable as a `.env` port-var KEY? Both halves, in one place, for both call sites."""
    return bool(_PORT_VAR_NAME_RX.match(name)) and name.upper() not in _PORT_VAR_RESERVED_NAMES


def _as_port(raw: str) -> "int | None":
    """A `.env` value as a host port, or None — the ONLY way this tier turns text into a number.
    `isdecimal`, never `isdigit`: `"²"` is isdigit-true and `int()`-UNSAFE, and a 5000-digit run trips
    CPython's 4300-digit cap; either would fail the lease of an INNOCENT slot, since the scan reads every
    sibling `.env`."""
    value = raw.strip()
    if not value.isdecimal() or len(value) > 5:
        return None
    port = int(value)
    return port if 1 <= port <= 65535 else None


def _port_reserved(port: int) -> bool:
    """Is PORT inside a host-wide reservation the pool may not allocate into (RESERVED_PORT_RANGES)?"""
    return any(first <= port <= last for first, last in RESERVED_PORT_RANGES)


def _env_assigned_ports(
    text: str, *, floor: int = AUTO_PORT_FLOOR, ceiling: int = AUTO_PORT_CEILING,
) -> set[int]:
    """The ports a `.env`'s TEXT actually ASSIGNS: the value of any key whose name contains `PORT`, plus any
    value already inside the auto range whatever its key is called (the `*_PORT` convention is a
    convention, and a sibling's allocation is its allocation either way).

    `floor`/`ceiling` are the EFFECTIVE range of the pass that is asking, not the module constants, and
    the caller passes its own: a sibling's allocation must be judged against the range it was allocated
    from. Read off the operator's knobs, so widening the range immediately makes the values already sitting
    in the new stretch count as spent instead of handing the same port out twice. The module constants stay
    as the defaults for the readers that hold no grid.

    NARROW ON PURPOSE — no URL-embedded ports. This is the set the KEEP decision consults, where a false
    positive is expensive: it moves a port a warm stack is bound to. A sibling merely *mentioning* another
    slot's port in a URL is not a claim on it."""
    spent: set[int] = set()
    for line in text.splitlines():
        m = env_file._ENV_KV_RX.match(line)
        if not m:
            continue
        port = _as_port(env_file._clean_env_value(m.group(2)))
        if port is None:
            continue
        if "PORT" in m.group(1).upper() or floor <= port <= ceiling:
            spent.add(port)
    return spent


def _env_embedded_ports(text: str) -> set[int]:
    """Ports embedded in `localhost`/`127.0.0.1`/`0.0.0.0` URL values. Added to the ASSIGN avoid-list only,
    where erring wide is free (one skipped candidate out of ~12k) — never to the keep decision."""
    return {port for port in (_as_port(raw) for raw in _EMBEDDED_PORT_RX.findall(text))
            if port is not None}


def _env_keys_assigning(text: str, port: int) -> list[str]:
    """Every key in TEXT assigning exactly PORT — the ambiguity test for the embedded-URL rewrite. With two
    keys on one value there is no way to tell which URL belongs to which, and rewriting them all moves a
    URL that belonged to the OTHER var (the shape a shared `.env.example` seed produces)."""
    keys = []
    for line in text.splitlines():
        m = env_file._ENV_KV_RX.match(line)
        if m and _as_port(env_file._clean_env_value(m.group(2))) == port:
            keys.append(m.group(1))
    return keys


def _child_dirs(path: str) -> list[str]:
    """Immediate subdirectories of PATH, sorted; `[]` for anything unreadable. `scandir` rather than
    `listdir` + `isdir` so a directory with thousands of FILES in it costs one syscall, not thousands."""
    try:
        with os.scandir(path) as entries:
            # NOT following symlinks: the pool layout has no legitimate symlinked slot, and following one
            # would let anything that can write under $JAAH_HOME point this scan at a file outside it.
            return sorted(e.path for e in entries if e.is_dir(follow_symlinks=False))
    except OSError:
        return []


def _sibling_env_paths(wt: str) -> list[str]:
    """Every OTHER pool `.env` on this host that could be spending an auto port, cheapest scope first:

      * this worktree's own project — its sibling slots and the main clone (`<repo dir>/*/.env`);
      * every project on the node (`<home>/repos/<project id>/<slot>/.env`).

    CROSS-PROJECT IS NOT OPTIONAL. The auto range is ONE flat range shared by every project on the node —
    unlike the grid, where each project's `port_block` makes disjointness structural — so a scan stopping
    at the project boundary would hand two projects the same port. Which is why the pool root is DERIVED
    from `wt` when `$JAAH_HOME` is unset rather than skipping the wide pass: an env var missing from one
    daemon's environment must not silently narrow the scan to the one scope that cannot be correct.

    Only `main` and numeric slot dirs are read — the pool's own naming (`slot_path`) — so a `.env` sitting
    under some other three-deep path in the home is not mistaken for a slot's. Unreadable directories are
    skipped: this is a best-effort avoid-list backed by the bind test, never an authority."""
    base = wt.rstrip("/")
    own_env = os.path.abspath(os.path.join(base, ".env"))
    paths: list[str] = []
    seen: set[str] = set()

    def add_slots_of(repo_dir: str) -> None:
        for slot_dir in _child_dirs(repo_dir):
            name = os.path.basename(slot_dir)
            if name != "main" and not name.isdigit():
                continue
            env_path = os.path.abspath(os.path.join(slot_dir, ".env"))
            if env_path == own_env or env_path in seen:
                continue
            if os.path.isfile(env_path):
                seen.add(env_path)
                paths.append(env_path)

    repo_dir = os.path.dirname(base)
    add_slots_of(repo_dir)
    # `<home>/repos/<id>/<slot>`: the home is three levels up from the slot. Preferred over the env var
    # precisely because it cannot be absent.
    home = environment.pool_root_raw().rstrip("/") or os.path.dirname(os.path.dirname(repo_dir))
    if home and home not in ("", "/"):
        for other_repo in _child_dirs(os.path.join(home, "repos")):
            add_slots_of(other_repo)
    return paths


# Winsock's `~SO_REUSEADDR` (-5); `socket` defines the name only on Windows, so a Linux test names it here.
_SO_EXCLUSIVEADDRUSE = getattr(socket, "SO_EXCLUSIVEADDRUSE", ~4)


def _port_bindable(port: int) -> bool:
    """Can this host bind PORT on `0.0.0.0` right now — the live half of the collision check, and the only
    half that can see a listener no `.env` mentions (a host daemon, a hand-published container, another
    tool). Deliberately WITHOUT `SO_REUSEADDR`: the question is "would a container publishing this port
    fail with EADDRINUSE", and that option makes the probe succeed beside a listener that already owns it.
    Any `OSError` answers no — an unbindable port is unbindable whatever the reason.

    ON WINDOWS a plain wildcard bind SUCCEEDS beside a listener on a specific address (Linux refuses it),
    so the probe asks for `SO_EXCLUSIVEADDRUSE`, which fails on any existing binding of the port. A port in
    one of Windows' excluded ranges (Hyper-V/WinNAT) fails the bind with WSAEACCES — an `OSError`, a no."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if host_platform.is_windows():
            sock.setsockopt(socket.SOL_SOCKET, _SO_EXCLUSIVEADDRUSE, 1)
        sock.bind(("0.0.0.0", port))
        return True
    except OSError:
        return False
    finally:
        sock.close()


def _pick_auto_port(taken: "set[int]", floor: int, ceiling: int) -> int:
    """The LOWEST port in `[floor, ceiling]` that is unspent, unreserved and bindable now. Lowest rather
    than random so a node's allocations stay compact and an operator reading two slots' `.env` files can
    see the pattern. Raises `ResetRefused` on exhaustion: silently leaving the var on its seed is the
    shared-port failure this whole tier exists to prevent, so it fails the lease instead."""
    for port in range(floor, ceiling + 1):
        if port in taken or _port_reserved(port):
            continue
        if _port_bindable(port):
            return port
    raise core.ResetRefused(
        f"auto-port-exhausted: no free host port left in the tier-2 range {floor}-{ceiling} on this node "
        f"({len(taken)} already spent by this host's .env files). Either the range needs widening — it is "
        f"an OPERATOR KNOB now, `auto_port_floor`/`auto_port_ceiling` in the console's port-grid settings, "
        f"which this node falls back to worktree_mechanics.ports.AUTO_PORT_FLOOR/AUTO_PORT_CEILING for when the "
        f"control plane sends none — or dead worktrees need retiring"
    )


# Serializes tier-2 picks inside this daemon: `reconcile.execute.execute_intents` claims slots concurrently, and two
# picks reading the sibling `.env` files before either writes would hand both slots the lowest free port.
_AUTO_PORTS_LOCK = threading.Lock()


def apply_auto_ports(
    wt: str,
    *,
    port_vars: "list[str] | tuple[str, ...]" = (),
    band: "dict[str, int] | None" = None,
    floor: int = AUTO_PORT_FLOOR,
    ceiling: int = AUTO_PORT_CEILING,
) -> dict[str, int]:
    """`_apply_auto_ports` under `_AUTO_PORTS_LOCK`, so this daemon's concurrent claims cannot race a pick."""
    with _AUTO_PORTS_LOCK:
        return _apply_auto_ports(wt, port_vars=port_vars, band=band, floor=floor, ceiling=ceiling)


def _apply_auto_ports(
    wt: str,
    *,
    port_vars: "list[str] | tuple[str, ...]" = (),
    band: "dict[str, int] | None" = None,
    floor: int = AUTO_PORT_FLOOR,
    ceiling: int = AUTO_PORT_CEILING,
) -> dict[str, int]:
    """Give every declared `port_vars` entry the CP's `band` does NOT name a flat host port, written into
    this worktree's `.env`. Returns `{VAR: port}` for the tier-2 vars afterwards — assigned and kept alike.

    IDEMPOTENT, WHICH IS NOT "KEEP WHATEVER IS THERE": a value is kept only when it is already an in-range
    auto port nothing else on this host spends, because a re-lease must not move a port a warm stack is
    bound to. Anything else is REassigned, and the case that matters is the `.env.example` SEED — present,
    numeric, and identical in every slot, i.e. the cross-slot collision the port grid exists to prevent.

    COLLISION CHECKED TWICE, since each probe sees what the other cannot: every sibling `.env` on the node
    (a DOWN slot still owns its port) plus a live bind test (a listener no `.env` mentions). Best effort —
    this daemon's own picks are serialized (`apply_auto_ports`), but another process leasing on the same node
    can still pick one port, and the loser fails loudly at `up` time.

    Called from `apply_port_band`, not from each mechanic, so tier 2 lands exactly where tier 1 does: every
    create and every claim, from one place that cannot fall out of step with it."""
    declared = [str(v).strip() for v in (port_vars or []) if str(v).strip()]
    banded = {str(v).strip() for v in (band or {})}
    # DEDUPED, ORDER IRRELEVANT (the loop sorts): `port_vars` is operator free text and CAN repeat a name.
    # Processed twice, the second pass reads back the value the first just wrote, finds it in `claimed`, and
    # reassigns — so the port flipped on EVERY lease, the exact churn idempotency exists to prevent.
    autos = list(dict.fromkeys(v for v in declared if v not in banded))
    if not autos:
        return {}
    malformed = [v for v in autos if not _port_var_name_ok(v)]
    if malformed:
        raise core.ResetRefused(
            f"port-var-name-malformed: {malformed!r} is not a usable port-var name, and a declared port "
            f"var becomes a KEY in this slot's .env — it must be an env identifier (a name carrying a "
            f"newline or an '=' would inject an arbitrary assignment) and must not be one of "
            f"{sorted(_PORT_VAR_RESERVED_NAMES)} (which would SHADOW this node's own credential or "
            f"identity with a port). Fix the project's `port_vars` column"
        )
    if not banded:
        # AMBIGUOUS AND WORTH SAYING SO. From here it is indistinguishable whether this project genuinely
        # declares no grid-managed var or whether its `port_block` is unset — the node holds no grid to
        # tell them apart. The second is a config fault with its own gate, named here so a human reading
        # a node log has the next step rather than a mystery.
        log.warning(
            "apply_auto_ports: %s got NO grid band at all, so all of %s are being auto-assigned. Expected "
            "for a project with no grid-managed var; otherwise its `port_block` is unset — run "
            "the pool conformance check against the project",
            wt, sorted(autos),
        )
    else:
        # A MIXED band names the DEPLOY-SKEW case: if one of these vars does have a range start in a NEWER
        # grid than the control plane is running, it is being auto-assigned instead of banded, and the CP's
        # own drift check cannot see it (its copy is the stale one). The node holds no grid to decide this,
        # so it reports rather than refuses — and the next lease after the CP catches up re-bands it.
        log.info(
            "apply_auto_ports: %s banded %s and is auto-assigning %s. If one of the latter should be on "
            "the grid, this node's grid is ahead of the control plane's copy (policy §Ports, Known gap)",
            wt, sorted(banded), sorted(autos),
        )
    env_path = os.path.join(wt, ".env")
    if not os.path.isfile(env_path):
        # Same asymmetry as `apply_port_band`'s: an absent `.env` is a REPO-contract fault
        # (the pool conformance check gates it), never this node's, so it is loud and not a refusal.
        log.warning(
            "apply_auto_ports: %s has no .env — the tier-2 vars %s could not be assigned a port and this "
            "stack falls back to its compose defaults, which are the same in every slot",
            wt, sorted(autos),
        )
        return {}

    # THE KEEP/ASSIGN SPLIT IS THE POINT — collapsing these breaks idempotency one way or disjointness the
    # other. `sibling_assigned`: ports other slots ASSIGN, the ONLY set the keep test may read (a false
    # positive there moves a port a warm stack is bound to, so a sibling merely naming our port in a URL
    # must not count). `avoid`: everything a new assignment dodges — that, plus every sibling's URL ports,
    # plus this file's own; erring wide is free. `claimed`: kept or assigned in THIS pass, so two vars
    # seeded to one value cannot both keep it.
    sibling_assigned: set[int] = set()
    avoid: set[int] = set()
    for path in _sibling_env_paths(wt):
        try:
            text = env_file.env_text(path)
        except OSError as exc:
            # Logged, not silent: a dropped sibling's ports get allocated over (the bind probe sees only
            # LIVE stacks), and a seam outage would empty `avoid` unnoticed. Absent is ordinary; still a
            # `continue` — one unreadable sibling must not fail the claim.
            if not isinstance(exc, FileNotFoundError):
                log.warning("apply_auto_ports: %s is unreadable (%s) — that slot's ports are NOT being "
                            "avoided by this pass", path, exc)
            continue
        # THIS PASS'S range, not the module default: `floor`/`ceiling` may be the operator's, and a
        # sibling's value only counts as an auto allocation when judged against the range it came from.
        sibling_assigned |= _env_assigned_ports(text, floor=floor, ceiling=ceiling)
        avoid |= _env_embedded_ports(text)
    avoid |= sibling_assigned
    try:
        own_text = env_file.env_text(env_path)
    except OSError as exc:
        log.warning("apply_auto_ports: %s is unreadable (%s) — tier-2 vars left alone", env_path, exc)
        return {}
    avoid |= _env_assigned_ports(own_text, floor=floor, ceiling=ceiling) | _env_embedded_ports(own_text)

    resolved: dict[str, int] = {}
    claimed: set[int] = set()
    # ACCUMULATED across the walk and written ONCE at the end (`_set_env_vars`): at
    # `WORKTREE_ENV_MODE` the daemon's write is a delegated round trip, and a project with several tier-2
    # vars would otherwise pay one per var on every claim. The reads inside the loop are unaffected —
    # each var reads only its OWN key, which no earlier iteration writes.
    assignments: dict[str, str] = {}
    embedded: list[tuple[int, int]] = []
    for var in sorted(autos):     # sorted so a re-run assigns in the same order, not dict order
        current = _env_port(wt, var)
        was = _as_port(current)
        if was is not None:
            in_range = floor <= was <= ceiling and not _port_reserved(was)
            if in_range and was not in sibling_assigned and was not in claimed:
                resolved[var] = was
                claimed.add(was)
                continue
        elif current:
            # A declared port var holding something that is not a port at all — a URL, a name, a comment.
            # It is still assigned (the project DECLARED it as a port), but the old value is deliberately
            # NOT logged: this shape is how a misdeclared `port_vars` entry (`DATABASE_URL`) would print a
            # credential into the node journal. Loud about the overwrite, silent about the value.
            log.warning(
                "apply_auto_ports: %s %s held a NON-PORT value and is being overwritten with a tier-2 "
                "port — if that var is not a host port, remove it from the project's `port_vars`", wt, var,
            )
        port = _pick_auto_port(avoid | claimed, floor, ceiling)
        assignments[var] = str(port)
        if was is not None and was != port:
            # ONLY when this var is the sole owner of the old value. Two keys on one port (a seed copied
            # across vars) make every `localhost:<was>` ambiguous, and rewriting them all hands the OTHER
            # var's URL to this var's new port — worse than leaving a stale one an operator can see.
            owners = _env_keys_assigning(own_text, was)
            if len(owners) <= 1:
                embedded.append((was, port))
            else:
                log.warning(
                    "apply_auto_ports: %s %s moved %d->%d but %s all assign %d, so any localhost:%d URL "
                    "is ambiguous and was left alone — give each var its own port in .env.example",
                    wt, var, was, port, sorted(owners), was, was,
                )
        log.info(
            "apply_auto_ports: %s %s=%d (tier 2 — no port-grid range start for this var%s)",
            wt, var, port, "" if was is None else f", was {was}",
        )
        resolved[var] = port
        claimed.add(port)
    if assignments or embedded:
        # Both AFTER the walk, and only when the walk decided something: a pass that keeps every port
        # writes NOTHING — not even the trailing-newline fixup, which exists solely so an APPENDED
        # assignment cannot glue onto the last line.
        env_file._ensure_eol_nl(env_path)
        env_file._set_env_vars(env_path, assignments, embedded)
    return resolved


def stack_base(wt: str, repo: str) -> str:
    """The COMPOSE_PROJECT_NAME base for the tree at `wt`: `p<project id>` for a tree at the pool layout
    `.../repos/<project id>/{main,N}` — the project's identity, so two projects sharing one repo never
    share a stack — else `repo`, for a tree outside that layout. Mirrored by the pool port audit, or the two rewrite it back and
    forth."""
    parent = os.path.dirname(os.path.normpath(wt))
    project_dir = os.path.basename(parent)
    if os.path.basename(os.path.dirname(parent)) == "repos" and project_dir.isdigit():
        return f"p{project_dir}"
    return repo


def apply_stack_identity(wt: str, repo: str, n: int | str) -> None:
    """Write the deterministic COMPOSE_PROJECT_NAME = `<base>` for the main clone, else `<base>-N`, where
    `<base>` is `stack_base`. No-op with no .env.

    NEVER THE DISPLAY NAME: a stack identity is a PLACEMENT fact naming containers, volumes and networks
    already on disk, so a rename of the operator-editable `projects.name` would ORPHAN a warm stack's —
    invisible to `docker compose down`, still holding the slot's ports. Built from the tree's project plus
    `n`, never from `wt`'s basename, which is a bare `N` shared by every project's slot N. DOES NOT MIGRATE
    AN EXISTING STACK: compose reads the identity at invocation time, so a warm stack keeps its old one
    until an operator cycles it — and its named volumes stay behind under the old name."""
    env_path = os.path.join(wt, ".env")
    if not os.path.isfile(env_path):
        return
    env_file._ensure_eol_nl(env_path)
    base = stack_base(wt, repo)
    name = base if str(n) == "0" else f"{base}-{n}"
    env_file._set_env_var(env_path, "COMPOSE_PROJECT_NAME", name)


def _default_node_id() -> str:
    """Fallback host identity — the short hostname — for a direct/test call to
    `apply_node_identity_env`; the daemon passes its own real `node_id()`. NOT a lease identity, and it is
    never persisted as one (see that function on why writing it into a pool `.env` strands the slot).
    A virtual node answers its `NODE_NAME` instead (`environment.reported_hostname`)."""
    name = environment.reported_hostname()
    return name.split(".", 1)[0] if name else name


def apply_node_identity_env(wt: str, n: int | str, node_id: str | None = None) -> None:
    """Assert-and-correct a POOL worktree's (N>=1) `NODE_ID` — which host this stack's `apps/api` reports
    against. Slot 0 is never rewritten. `node_id` is INJECTED by the daemon.

    A CALLER-SUPPLIED `node_id` IS REQUIRED TO WRITE, because absent is strictly better than wrong. The
    value is not inert: a dispatch into a slot carrying the wrong node resumes under it, is refused by its
    own dispatcher and closes instantly, leaving a leased worktree and an `active` Task behind. And
    `resolve_node_id()` degrades to `_default_node_id()`'s bare short hostname whenever the tailnet query
    is slow, so a value written from that is indistinguishable from a real answer and outlives the blip.
    Nothing reads this key for identity at runtime. Warn, do not fail."""
    if str(n) == "0":
        return
    env_path = os.path.join(wt, ".env")
    if not os.path.isfile(env_path):
        return
    if not node_id:
        log.warning(
            "apply_node_identity_env: no node_id supplied for %s — leaving NODE_ID alone rather than "
            "freezing the short hostname %r into it (a wrong identity there strands the slot's lease "
            "handoff; absent is recoverable)", wt, _default_node_id(),
        )
        return
    env_file._ensure_eol_nl(env_path)
    env_file._set_env_var(env_path, "NODE_ID", node_id)
