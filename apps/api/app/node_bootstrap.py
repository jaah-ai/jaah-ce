"""The one home of the shell a control-plane-created node runs to become a node.

Two entry points, one body: `render_cloud_init` (EC2 user-data) and `render_self_hosted_script` (served
by `routes/agent_bootstrap.py`, fetched by `render_fetch_command`'s one-liner); the only bootstrap
for a node the control plane creates or a self-hosted one. Order is load-bearing: base packages; the tailnet
with `--accept-routes`/`--ssh` when a key was minted; the apt channel (`/api/agent/apt`, over the node token);
`/etc/jaah/*.env` before the package (postinst starts the daemon); `apt-get install jaah`;
`jaah auth login` (sole writer of `NODE_TOKEN`).

The script carries the node token in cleartext (IMDS-readable, hence IMDSv2 in `app/aws/ec2.py`):
never log or echo it or any input. Every value is `shlex.quote`d; heredoc/hostname values are validated.
"""
from __future__ import annotations

import ipaddress
import re
import shlex
import socket
from dataclasses import dataclass
from typing import NamedTuple
from urllib.parse import urlsplit

from app import node_platform, version as app_version

# EC2 refuses user-data over this many bytes, measured BEFORE base64 ("User data is limited to 16384 bytes"). Its home
# is here, not in the EC2 client (`app/aws/ec2.py`, which imports it): that client does not ship in every edition.
USER_DATA_LIMIT = 16384

# The CP's `host[:port]` as it may appear in a heredoc, where `shlex.quote` protects nothing and the
# sources heredoc stays unquoted (for `$(dpkg --print-architecture)`), so `$` or a backtick would be
# evaluated. Letters, digits, dots, hyphens and an optional port only.
_CP_NETLOC_RE = re.compile(r"^[A-Za-z0-9.-]+(?::[0-9]{1,5})?$")

# A node's name is its tailnet hostname, so DNS label rules apply. Checked, not sanitized: a rewritten
# name would register under one the console cannot find.
_NODE_NAME_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$", re.IGNORECASE)

# The node's wire identity (`jaah auth login` takes it; `routes/nodes/` uses `str(row.id)`).
_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.IGNORECASE
)

# The token's shape (`node_credentials.TOKEN_PREFIX` + urlsafe base64), so a wrong string fails at
# render rather than booting a node into a 401 loop.
_NODE_TOKEN_RE = re.compile(r"^snt_[A-Za-z0-9_-]{16,}$")


# Mirrors `apps/jaahd/plugin_repair.py::_CONSENT_HASH_RE` (two deployables that cannot import each
# other). That copy decides use; this one refuses provisioning a malformed hash.
_CONSENT_HASH_RE = re.compile(r"\A[0-9a-f]{64}\Z")

# Where apt points, relative to the CP base; the route and the sources file must agree.
APT_PATH = "/api/agent/apt"

# Where the self-hosted bootstrap is served; single home for `render_fetch_command` and
# `routes/agent_bootstrap.py`. Deliberately not under `/api/agent/`: that prefix is refused by the
# public ALB, and a bare box cannot reach the machine surface before this script joins it
# to the tailnet. An accepted residual: the one `Access.INFRA` route reachable off the tailnet, an
# exception to the machine surface never leaving it; do not move it. Both deployed CPs are internal ALBs, so off-VPC the
# fetch itself still needs the
# tailnet first (stated in `apps/web/src/components/NodeOnboardingDialog.tsx`).
BOOTSTRAP_PATH = "/api/node-bootstrap"
# The query that turns the same door into "does this node still exist?" (`present`/`absent`), asked by a
# box whose node.env names another node: a deleted one is re-enrolled, a live one refused.
FORMER_NODE_PARAM = "former_node"
# The `nodes.infra` key holding when this door last served the node its script (ISO time): a machine that never
# fetched it never ran the install command (`routes/agent_bootstrap.py` writes it; `hosts._order_out` reads it).
BOOTSTRAP_FETCHED_KEY = "bootstrap_fetched_at"

# Where a provisioned node keeps its jaah tree: written as `JAAH_HOME` and read by
# `routes/nodes/register.py::_registered_node` to seed `nodes.jaah_home`. One spelling, because an empty row
# home drops every project from the node's pool tick and the node then reports no metrics.
PROVISIONED_JAAH_HOME = "/home/jaah/.jaah"

# The repo signing key, served at the same place and under the same auth as the packages.
APT_PUBKEY_PATH = f"{APT_PATH}/apt-pubkey.asc"

# Where dnf points (`routes/agent_rpm.py`), and the node's repo file and the signing key dnf checks with: the apt
# channel's own key (`APT_PUBKEY_PATH`), imported for rpm. The repo file carries the token as its password, so it
# is 0600 like apt's auth file (`jaah auth rotate` rewrites it).
RPM_PATH = "/api/agent/rpm"
DNF_REPO_FILE = "/etc/yum.repos.d/jaah.repo"
RPM_GPG_KEY_FILE = "/etc/pki/rpm-gpg/RPM-GPG-KEY-jaah"

# OS PATCHING (SOC 2 G-35), CLOUD NODES ONLY: a self-hosted machine is the user's, and its OS (its kernel
# above all) is never ours to change. unattended-upgrades applies security updates daily and NOTHING EVER
# RESTARTS OR REBOOTS ON ITS OWN, because either kills every runner on the box. Ubuntu's stock
# `50unattended-upgrades` already limits Allowed-Origins to the frozen release pocket plus the security
# pockets (never the jaah repo — `jaahd-update` owns that package); these files only switch the timer
# on and pin the two restart knobs off. A pending reboot surfaces as `reboot_required` in the host-metrics report
# (`apps/jaahd/host_metrics.py`), and an operator drains the node before rebooting it.
# Mirrored by the GitHub Actions runner host installer's `UNATTENDED_UPGRADES_FILES` (a separate
# deployable); its test pins the two equal. Quoted heredocs, so `$nrconf` reaches the file verbatim.
UNATTENDED_UPGRADES_FILES = {
    "/etc/apt/apt.conf.d/20auto-upgrades": (
        'APT::Periodic::Update-Package-Lists "1";\n'
        'APT::Periodic::Unattended-Upgrade "1";\n'
    ),
    "/etc/apt/apt.conf.d/52jaah-unattended-upgrades": 'Unattended-Upgrade::Automatic-Reboot "false";\n',
    # needrestart (Ubuntu 24.04+) restarts services on outdated libraries after an upgrade — runner units
    # included. `l` = list only.
    "/etc/needrestart/conf.d/50jaah.conf": "$nrconf{restart} = 'l';\n",
}
UNATTENDED_UPGRADES_UNITS = ("unattended-upgrades.service", "apt-daily.timer", "apt-daily-upgrade.timer")


#: Ubuntu's own apt sources on a cloud image: deb822 since 24.04, the one-line list before it.
UBUNTU_SOURCES = ("/etc/apt/sources.list.d/ubuntu.sources", "/etc/apt/sources.list")


def _universe_lines() -> list[str]:
    """Add `universe` to the image's Ubuntu sources where they leave it out. On 26.04 `python3-pip` and `python3-venv`
    live there, and an image without it fails the base install ("has no installation candidate") under `set -e`, so
    the machine never reports in (an EC2 26.04 launch did). Cloud path only: a self-hosted machine's
    sources are its owner's."""
    deb822, one_line = UBUNTU_SOURCES
    return [
        f"if [ -f {deb822} ]; then",
        "  sed -i -E '/^Components:/{/[[:space:]]universe([[:space:]]|$)/!s/$/ universe/}' " + deb822,
        "fi",
        f"if [ -f {one_line} ]; then",
        "  sed -i -E '/^deb(-src)?[[:space:]]/{/[[:space:]]universe([[:space:]]|$)/!s/[[:space:]]main([[:space:]]|$)/ main universe\\1/}' "
        + one_line,
        "fi",
    ]


def _unattended_upgrades_lines() -> list[str]:
    """Patching is a hardening, not the node: a package or unit this image refuses warns and the install goes on
    (no optional step leaves a box neither clean nor a node)."""
    lines = [
        'echo "==> [jaah] enabling unattended security upgrades (no automatic reboot)"',
        "if ! apt-get install -y unattended-upgrades; then",
        '  echo "==> [jaah] warning: unattended-upgrades did not install; this node gets no automatic security updates" >&2',
        "fi",
        "install -d -m 0755 /etc/needrestart/conf.d",
    ]
    for path, body in UNATTENDED_UPGRADES_FILES.items():
        lines += [f"cat > {path} <<'EOF'", *body.splitlines(), "EOF", f"chmod 0644 {path}"]
    return [
        *lines,
        f"if ! systemctl enable --now {' '.join(UNATTENDED_UPGRADES_UNITS)}; then",
        '  echo "==> [jaah] warning: the automatic security update timers did not start" >&2',
        "fi",
        # One run NOW, while no runner exists to be disturbed: however old the image, the node takes work
        # patched rather than waiting for the first daily timer. A kernel update leaves `reboot_required`
        # set from the first report; nothing runs here yet, so that reboot is free. Non-fatal — a refused upgrade is the
        # timer's to retry, not a reason to leave the box neither clean nor a node.
        'echo "==> [jaah] applying pending security updates"',
        # The stamp is what `apps/jaahd/host_metrics.py` reports as the last run; only apt's daily job
        # touches it, so a direct run stamps itself.
        "if unattended-upgrade; then touch /var/lib/apt/periodic/upgrade-stamp; else",
        '  echo "==> [jaah] warning: unattended-upgrade failed; the daily timer retries" >&2',
        "fi",
        "",
    ]


@dataclass(frozen=True)
class BootstrapInputs:
    """Everything one created node needs to become one. All of it is secret-adjacent (see the module
    docstring)."""

    node_id: str
    """The node's uuid — its wire identity, what `jaah auth login` is given."""

    node_name: str
    """The operator-facing name, which is also the tailnet hostname."""

    node_token: str
    """The plaintext node token. Shown once, stored only as a sha256 digest control-plane side."""

    cp_base_url: str
    """`https://host[:port]` — the control plane this node dials, and the apt channel's origin.
    HTTPS is required, not merely conventional: the token becomes apt's Basic-auth password. Only a
    loopback host may be plain `http`, for the local `./dc` stack — see `cp_host`."""

    tailscale_auth_key: str | None = None
    """A single-use, pre-authorized tailnet key, or None to skip the join entirely."""

    tailscale_login_server: str | None = None
    """The coordination server the key is presented to (`--login-server`), or None for Tailscale's own —
    the legacy `tailscale` credential, whose script is byte-identical to before this field existed. Only
    with a key, and https only: it is the server the node's whole tailnet identity is enrolled with."""

    remote_settings_consent_hash: str = ""
    """`JAAH_REMOTE_SETTINGS_CONSENT_HASH` — the `dangerousSettingsHash` this node's accounts
    consent to, carried from a node that already has one. Resolving WHICH hash that is belongs to the
    control plane and not to this module; here it is an opaque string that is either well-formed or
    refused.

    CARRIED, NEVER COMPUTED, and the argument for that is single-homed in
    `apps/jaahd/plugin_repair.py`'s `# ── the managed-settings consent ──` block: the value is the
    Claude CLI's own internal digest and no reconstruction of it reproduces it. So the only way a node
    can hold one is for something to hand it one.

    EMPTY IS ORDINARY. A control plane that has never had a node report a hash has nothing to seed,
    and a node provisioned without one behaves exactly as the whole fleet did before this line
    existed: `plugin_repair.consent_hash` falls back to a consenting peer account on the node, and
    prompts only when there is no peer either. What the line buys is the FIRST runner on a FRESH
    node, which by definition has no peer — the case where the prompt blocks forever, registers no
    session, and leaves the task `ready` while the console says "Not running"."""

    tailnet_reenrol: bool = False
    """Whether an operator re-enrolled this node (`POST /api/nodes/{node_id}/tailnet-reenrol`), so the
    already-provisioned arm re-joins the tailnet with `--force-reauth` (`_reenrol_lines`). Only
    `routes/agent_bootstrap.py` sets it, for an armed node that has reported in; a key alone never does —
    a first-enrolment key re-served to a provisioned box must not force a re-join. False renders nothing."""

    vnode_host: bool = False
    """Whether this node is a MULTI-NODE HOST (`app/vnodes.py`). True writes one root-owned, empty marker
    file, `VNODE_HOST_MARKER`, before the package installs — the activation seam the host-only systemd
    units key on (`ConditionPathExists=`), so they stay inert on every node that is not a host. False
    renders NOTHING extra: every other node's bootstrap is byte-identical to what it was before this
    field existed (`tests/test_node_bootstrap.py` pins that with a golden digest)."""

    public_base_url: str | None = None
    """The PUBLIC console origin this bootstrap was fetched from, or None. Only a Mac's body reads it
    (`render_macos_body`): off the tailnet, `cp_base_url`'s machine routes are out of reach, so the installer
    fetches its bundle back through the one public path (`BOOTSTRAP_PATH`, `?dist=`) before it joins. None
    (cloud-init, every other OS) keeps the tailnet-only fetch; Ubuntu and Windows bodies never read it."""

    def __repr__(self) -> str:  # pragma: no cover - trivial, but load-bearing
        """Never render the secrets (tracebacks, logs, 500 bodies)."""
        return f"BootstrapInputs(node_name={self.node_name!r}, secrets=<redacted>)"


class BootstrapInputError(ValueError):
    """One of the inputs cannot be rendered into a script safely. Names the field."""


def _require(condition: bool, field: str, rule: str) -> None:
    if not condition:
        raise BootstrapInputError(f"{field} {rule}")


def _host_address(host: str) -> ipaddress._BaseAddress | None:
    """`host` as an IP address, including the legacy spellings `ipaddress` alone refuses.

    `127.1`, `2130706433` and `0x7f.0.0.1` all dial loopback; `inet_aton` is the C library's parser, so
    it agrees with what the box will really dial.
    """
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        pass
    try:
        return ipaddress.IPv4Address(socket.inet_aton(host))
    except (OSError, ipaddress.AddressValueError):
        return None


def is_loopback(base_url: str) -> bool:
    """Whether `base_url`'s host is one only the machine it runs on can reach.

    Name-based (`localhost`, `localhost.`, `*.localhost`) and address-based. An unparseable host is not
    loopback, so it falls through to the https requirement. Single home; `cloud_node_launch` imports it.
    """
    host = (urlsplit(base_url).hostname or "").rstrip(".")
    if host == "localhost" or host.endswith(".localhost"):
        return True
    address = _host_address(host)
    return address is not None and address.is_loopback


def is_undialable(base_url: str) -> bool:
    """Whether a node handed `base_url` could never reach the control plane with it.

    A superset of `is_loopback`: also the unspecified address (`0.0.0.0`, `::`), meaningless to a
    client and a plausible derived `Host`. The derived-origin guard asks this; the plain-http exemption
    asks the narrower `is_loopback`.
    """
    if is_loopback(base_url):
        return True
    address = _host_address((urlsplit(base_url).hostname or "").rstrip("."))
    return address is not None and address.is_unspecified


def cp_host(cp_base_url: str) -> str:
    """The `host[:port]` apt's `auth.conf` must name, derived from the same base URL the sources
    file uses so the two cannot disagree (a mismatch fails as an unexplained 401 in `apt-get update`).
    """
    parts = urlsplit(cp_base_url)
    _require(parts.scheme in {"http", "https"}, "cp_base_url", "must be an http(s) URL")
    _require(bool(parts.netloc), "cp_base_url", "must name a host")
    # Userinfo would put a credential in apt's config; refused with its own message.
    _require("@" not in parts.netloc, "cp_base_url", "must not carry userinfo")
    _require(
        bool(_CP_NETLOC_RE.fullmatch(parts.netloc)),
        "cp_base_url",
        "host must be letters, digits, dots and hyphens with an optional port",
    )
    # HTTPS or a loopback host only: the node token becomes apt's Basic-auth password, replayed on every
    # `apt-get update` for the machine's life. Loopback is exempt for the local `./dc` stack.
    _require(
        parts.scheme == "https" or is_loopback(cp_base_url),
        "cp_base_url",
        "must be https — apt replays the node token as a Basic-auth password on every update, so "
        "http would send it in cleartext; only a loopback host is exempt",
    )
    return parts.netloc


def require_bare_origin(cp_base_url: str) -> None:
    """Every rule a control-plane base URL must satisfy before it is put anywhere, in one place.

    One function so the script and the one-liner cannot diverge. Refuses: a path (a channel
    nothing serves); a query or fragment (invisible to the path/netloc gates, and was a root shell
    injection via the unquoted heredoc); tab/CR/LF (stripped by `urlsplit` before parsing, and a newline
    can close the heredoc). `_body` also rebuilds the origin from parsed parts.
    """
    parts = urlsplit(cp_base_url)
    _require(parts.path in {"", "/"}, "cp_base_url", "must be a bare origin with no path")
    _require(
        not parts.query and not parts.fragment,
        "cp_base_url",
        "must be a bare origin with no query string or fragment",
    )
    _require(
        not any(character in cp_base_url for character in "\t\r\n"),
        "cp_base_url",
        "must not contain a tab, carriage return or newline",
    )
    cp_host(cp_base_url)  # raises with its own field name


def _validate(inputs: BootstrapInputs) -> None:
    """Everything that must be true before a single byte is interpolated."""
    _require(bool(_UUID_RE.fullmatch(inputs.node_id)), "node_id", "must be a uuid")
    _require(
        bool(_NODE_NAME_RE.fullmatch(inputs.node_name)),
        "node_name",
        "must be a DNS label: letters, digits and inner hyphens, at most 63 characters",
    )
    _require(
        bool(_NODE_TOKEN_RE.fullmatch(inputs.node_token)),
        "node_token",
        "must be a minted node token",
    )
    require_bare_origin(inputs.cp_base_url)
    if inputs.public_base_url is not None:
        try:
            require_bare_origin(inputs.public_base_url)
        except BootstrapInputError as exc:
            raise BootstrapInputError(str(exc).replace("cp_base_url", "public_base_url", 1)) from None
    if inputs.tailscale_auth_key is not None:
        _require(
            bool(inputs.tailscale_auth_key.strip()),
            "tailscale_auth_key",
            "must not be blank — pass None to skip the tailnet join",
        )
        # A key is never multi-line; a newline would escape the quoted assignment.
        _require(
            "\n" not in inputs.tailscale_auth_key,
            "tailscale_auth_key",
            "must be a single line",
        )
    if inputs.tailscale_auth_key is not None and inputs.tailscale_auth_key.startswith("hskey-"):
        # Presented to Tailscale's own server, a headscale key is a live key disclosed to a third party.
        _require(
            inputs.tailscale_login_server is not None,
            "tailscale_login_server",
            "is required with a headscale key",
        )
    if inputs.tailscale_login_server is not None:
        _require(
            inputs.tailscale_auth_key is not None,
            "tailscale_login_server",
            "must come with a tailscale_auth_key — a login server alone joins nothing",
        )
        validate_login_server(inputs.tailscale_login_server)
    if inputs.remote_settings_consent_hash:
        # Not a shell guard (quoted heredoc): a malformed hash would reach the node, be discarded by
        # `plugin_repair.consent_hash`, and the first runner would prompt anyway. Refuse at render.
        _require(
            bool(_CONSENT_HASH_RE.fullmatch(inputs.remote_settings_consent_hash)),
            "remote_settings_consent_hash",
            "must be a hex sha256: exactly 64 lowercase hex characters — the node's own validator "
            "discards anything else and the node then prompts as if it had been given nothing",
        )


def validate_login_server(login_server: str) -> None:
    """A login server is a bare https origin. Quoted on render, so this is not the injection guard; it
    refuses the typo that would otherwise boot a node that never joins, far from the operator. Public so
    `cloud_node_launch.mint_tailnet_join` can refuse BEFORE a single-use key is spent."""
    parts = urlsplit(login_server)
    _require(
        parts.scheme == "https",
        "tailscale_login_server",
        "must be https — the node enrols its tailnet identity with this server",
    )
    _require(
        bool(_CP_NETLOC_RE.fullmatch(parts.netloc))
        and parts.path in {"", "/"}
        and not parts.query
        and not parts.fragment
        and not any(character in login_server for character in " \t\r\n"),
        "tailscale_login_server",
        "must be a bare https origin: a host and optional port, no path, query, userinfo or whitespace",
    )


# How long a join may wait for tailscaled to reach Running. A healthy join takes seconds; without a bound,
# a login the coordination server keeps refusing (429s behind a shared NAT) hangs the run forever.
TAILNET_JOIN_TIMEOUT = "300s"


def tailnet_join_command(
    q_name: str, *, login_server: bool, force_reauth: bool = False, reset: bool = False, ssh: bool = True,
    cli: str = "tailscale", key_file: bool = False,
) -> str:
    """THE JOIN LINE — single home, so every arm that joins renders the same command. `q_name` is the
    already-quoted hostname; the key (and the login server, when `login_server`) are read from the
    `JAAH_TS_AUTHKEY`/`JAAH_TS_LOGIN_SERVER` shell variables the caller assigns. Without a login
    server it is exactly the line the fleet has always run. `force_reauth` is the re-enrol arm's: the
    machine is already logged in (possibly to another server) and must re-authenticate with the new key.
    `reset` is the tailnet switch's: prefs set for ANOTHER tailnet (its exit node, advertised routes) would
    make `tailscale up` refuse to change settings it was not told to keep; and run over Tailscale SSH it
    refuses ("aborted, no changes made") unless the dropped session is accepted, `--accept-risk=lose-ssh`.
    `ssh=False` is the Mac's: often someone's own machine, with a console in reach. `cli` is the command that
    runs (`"$ts_cli"` on a Mac, whose installer calls its Tailscale CLI by absolute path). `key_file` is the
    Mac's too: the key is read from `$work/ts.key` (`--auth-key=file:`, `_macos_key_file_lines`), never put on
    argv, since a Mac is often a shared machine whose other users can `ps` a single-use key and race it.
    Every join is bounded by `--timeout` (`TAILNET_JOIN_TIMEOUT`): past it `up` exits non-zero, so the run fails
    at its named step instead of hanging."""
    server = '--login-server "$JAAH_TS_LOGIN_SERVER" ' if login_server else ""
    reauth = " --force-reauth" if force_reauth else ""
    reauth += " --reset --accept-risk=lose-ssh" if reset else ""
    ssh_flag = " --ssh" if ssh else ""
    key = '--auth-key="file:$work/ts.key"' if key_file else '--authkey "$JAAH_TS_AUTHKEY"'
    return f"{cli} up {server}{key} --hostname {q_name}{ssh_flag} --accept-routes --timeout={TAILNET_JOIN_TIMEOUT}{reauth}"


def _macos_key_file_lines(indent: str) -> list[str]:
    """The Mac's tailnet key, out of the shell variable into `$work/ts.key` (`tailnet_join_command(key_file=True)`):
    0600 in the installer's 0700 scratch dir, made here when no bundle was fetched, which every exit path's trap
    removes; `printf` is a builtin, so the key is never on an argv. Each join deletes it as soon as `up` returns."""
    return [
        f'{indent}[ -n "$work" ] || {{ work="$(mktemp -d /tmp/jaah-install.XXXXXX)"; trap \'rm -rf -- "$work"\' EXIT; }}',
        f"{indent}( umask 077; printf '%s\\n' \"$JAAH_TS_AUTHKEY\" > \"$work/ts.key\" )",
        f"{indent}unset JAAH_TS_AUTHKEY",
    ]


def _tailnet_key_lines(inputs: BootstrapInputs, indent: str) -> list[str]:
    """The shell assignments the join line reads: the key, and the login server when one is set."""
    lines = [f"{indent}JAAH_TS_AUTHKEY={shlex.quote(inputs.tailscale_auth_key or '')}"]
    if inputs.tailscale_login_server is not None:
        # Headscale: the key is presented to this server, never to Tailscale's own.
        lines.append(f"{indent}JAAH_TS_LOGIN_SERVER={shlex.quote(inputs.tailscale_login_server)}")
    return lines


def _other_tailnet_probe_lines(inputs: BootstrapInputs, *, macos: bool = False) -> list[str]:
    """Sets `jaah_other_tailnet` to the control server this box is logged into when it is NOT this control
    plane's login server — a box on another environment's tailnet. Empty when it is ours, when
    the box is not logged in, when no login server is configured (Tailscale's own: nothing to compare), or
    when the server cannot be read: each of those keeps the guard's old meaning. `macos` lower-cases with
    `tr` (the Mac's /bin/bash is 3.2, which has no `${x,,}`) and runs its CLI by absolute path, `"$ts_cli"`."""
    if inputs.tailscale_login_server is None:
        return []
    cli = '"$ts_cli"' if macos else "tailscale"
    # Compared as `scheme://host[:port]`, lower-cased, without the default port: a spelling difference must
    # not read as another tailnet, or the switch below re-registers a box already enrolled here.
    ours = shlex.quote(_server_key(inputs.tailscale_login_server))
    # WHICH TAILNET? `tailscale status` only says the box is logged in somewhere. A box on ANOTHER
    # environment's tailnet (prod's, handed dev's command) has no route to this control plane, so it is
    # moved below. `tailscale debug prefs` is the only CLI that names the server; unreadable reads as ours.
    # A Python comment, not a rendered one: the script rides EC2 user-data, whose cap
    # (`test_the_largest_real_bootstrap_fits_ec2s_cap`) these three lines pushed past its headroom.
    return [
        "jaah_other_tailnet=''",
        f"if {cli} status >/dev/null 2>&1; then",
        f"  jaah_ts_server=\"$({cli} debug prefs 2>/dev/null"
        " | awk -F'\"' '/\"ControlURL\"/{print $4; exit}' || true)\"",
        '  jaah_ts_server="${jaah_ts_server%/}"',
        (
            '  jaah_ts_server="$(printf \'%s\' "$jaah_ts_server" | tr \'[:upper:]\' \'[:lower:]\')"'
            if macos
            else '  jaah_ts_server="${jaah_ts_server,,}"'
        ),
        '  jaah_ts_server="${jaah_ts_server%:443}"',
        f'  if [ -n "$jaah_ts_server" ] && [ "$jaah_ts_server" != {ours} ]; then',
        '    jaah_other_tailnet="$jaah_ts_server"',
        "  fi",
        "fi",
    ]


def _server_key(url: str) -> str:
    """A control server URL as the probe above compares it: no trailing slash, lower-cased, no `:443`."""
    key = url.rstrip("/").lower()
    return key[: -len(":443")] if key.endswith(":443") else key


def _tailnet_switch_lines(inputs: BootstrapInputs, q_name: str, *, ask: bool, macos: bool = False) -> list[str]:
    """The guard's middle arm: leave the other tailnet for this one. `--force-reauth` because the box is
    logged in elsewhere, `--reset` because that tailnet's prefs are not this one's. The key is this run's
    single-use one, so the device enrols exactly as a fresh join would. Empty without a login server.

    `ask` (the fetched script only; cloud-init has no terminal and EC2 user-data a size cap): an operator
    at a terminal is ASKED first, reading `/dev/tty` since stdin is the piped script; with no
    terminal there is nobody to ask, so it moves. The SSH session it was run from usually rides the tailnet
    being left and drops mid-run, so the rest of the output goes to `JOIN_LOG` — created afresh 0600, never
    a planted symlink followed — whether or not it asked: a write to the dead session would fail and
    `set -e` stop the run halfway. HUP is ignored, so the run finishes on its own, and a terminal that
    survived is told how it ended. The tailnet left behind takes its NodeLinks with it: `_nodelink_detach_lines`.

    `macos` is the Mac installer's: no `--ssh` on the join (`tailnet_join_command`), which runs `"$ts_cli"`; its
    NodeLinks detached by `jaah-nodelink down` (no `/proc` there); the exit trap also removes the installer's
    scratch `$work`, whose own trap this one replaces; and with no terminal it REFUSES rather than moves — a
    Mac is often someone's own machine, and is moved off a tailnet only on its owner's word."""
    if inputs.tailscale_login_server is None:
        return []
    if macos:
        trap = (
            "  trap 'jaah_rc=$?; [ -z \"${work:-}\" ] || rm -rf -- \"$work\"; "
            f"echo \"==> [jaah] bootstrap exited $jaah_rc (log: {JOIN_LOG})\" 2>/dev/null >/dev/tty || true' EXIT"
        )
    else:
        # It replaces the prelude's trap (`_platform_lines`), so it names the failed step too: into the log, and
        # on the terminal that survived.
        trap = (
            "  trap 'jaah_rc=$?; jaah_on_exit $jaah_rc; "
            f"echo \"==> [jaah] bootstrap exited $jaah_rc at step: $JAAH_STEP (log: {JOIN_LOG})\" 2>/dev/null >/dev/tty"
            " || true' EXIT"
        )
    detach = [
        "  if (: </dev/tty) 2>/dev/null; then",
        '    echo "==> [jaah] an SSH session reaching it over that tailnet will disconnect;"',
        '    echo "==> [jaah] the rest of this run then carries on by itself and the node comes online in the console"',
        "    printf '==> [jaah] move it now? [y/N] ' >/dev/tty",
        "    read -r jaah_move </dev/tty || jaah_move=''",
        '    case "$jaah_move" in',
        "      [Yy]*) ;;",
        '      *) echo "==> [jaah] not moved; nothing on the tailnet changed. Run the command again and answer y to move it." >&2',
        "         exit 1 ;;",
        "    esac",
        *(
            [
                "  else",
                '    cant "a Mac on this tailnet (it is on another one, $jaah_other_tailnet)" "run the command in'
                ' Terminal on this Mac, to confirm moving it"',
                "    refuse_if_any",
            ]
            if macos
            else []
        ),
        "  fi",
        f'  echo "==> [jaah] moving; the rest of this run logs to {JOIN_LOG} (tail -f it once reconnected)"',
        f"  install -m 600 /dev/null {JOIN_LOG}",
        f"  exec >>{JOIN_LOG} 2>&1",
        trap,
        *(_macos_nodelink_detach_lines() if macos else _nodelink_detach_lines()),
    ]
    return [
        'elif [ -n "$jaah_other_tailnet" ]; then',
        '  echo "==> [jaah] this machine is on another tailnet ($jaah_other_tailnet); it must join this one"',
        *(detach if ask else []),
        "  trap '' HUP",
        # A Mac's installer assigns the key once, before its whole guard (`_macos_tailnet_slots`).
        *([] if macos else _tailnet_key_lines(inputs, "  ")),
        "  " + tailnet_join_command(
            q_name, login_server=True, force_reauth=True, reset=True, ssh=not macos,
            cli='"$ts_cli"' if macos else "tailscale", key_file=macos,
        ),
        '  rm -f -- "$work/ts.key"' if macos else "  unset JAAH_TS_AUTHKEY",
    ]


def _macos_nodelink_detach_lines() -> list[str]:
    """`_nodelink_detach_lines` on a Mac: the installed node's own helper unmounts every NodeLink
    (`jaah-nodelink down`, from the macOS bundle), bounded, since a `hard` mount whose server is gone blocks."""
    return [
        '  if [ -x "$JAAH_ROOT/current/bin/jaah-nodelink" ]; then',
        "    echo \"==> [jaah] detaching that tailnet's NodeLinks\"",
        '    "$JAAH_ROOT/current/bin/timeout" 60 "$JAAH_ROOT/current/bin/jaah-nodelink" down || true',
        "  fi",
    ]


#: Where the legacy accounts share and every NodeLink's staging mount live on a node (the package's shared-mount
#: helper and the daemon's NodeLink mount `STAGING_ROOT`).
_LEGACY_SHARE_MOUNT = "/mnt/jaah-shared/accounts"
_NODELINK_STAGING = "/run/jaah-nodelinks/"


def _nodelink_detach_lines() -> list[str]:
    """Lazily detach the environment being left's NodeLink mounts. They are its EFS, which this tailnet cannot
    reach, and a `hard` NFS mount whose server is gone blocks every `stat` on it forever — jaahd's event loop
    included, so the node never placed its first vnode. Read from mountinfo, which never asks the
    server: the devices of the legacy share and the staging mounts, then every mount on those devices, so each
    NodeLink's bind goes too — NFS ONLY, so a local directory bound at either path never takes its disk's other
    mounts (`/`) with it. `-l`, as everywhere a NodeLink is unmounted; `-i --no-canonicalize` and a timeout,
    so the unmount itself never stats the dead server. Only the fetched script: a fresh cloud instance has no
    mounts to leave behind, and EC2 user-data has no room."""
    return [
        "  for jaah_m in $(awk -v legacy=" + shlex.quote(_LEGACY_SHARE_MOUNT) + " -v staging="
        + shlex.quote(_NODELINK_STAGING)
        + " '{fs = \"\"; for (j = 7; j < NF; j++) if ($j == \"-\") {fs = $(j + 1); break}}"
        " fs !~ /^nfs4?$/ {next} $5 == legacy || index($5, staging) == 1 {d[$3] = 1} {dev[NR] = $3; mp[NR] = $5}"
        " END {for (i = NR; i > 0; i--) if (dev[i] in d) print mp[i]}' /proc/self/mountinfo); do",
        '    echo "==> [jaah] detaching $jaah_m (that tailnet\'s NodeLink)"',
        '    timeout 15 umount -l -i --no-canonicalize "$jaah_m" || true',
        "  done",
    ]


#: Where a bootstrap that moved tailnets under an operator's terminal writes the rest of its output.
JOIN_LOG = "/var/log/jaah-bootstrap.log"


def _rotate_lines(cli: str, indent: str) -> list[str]:
    """The Ubuntu body's last step: trade the token this script carries (and the user-data holds, IMDS-readable)
    for one only node.env holds (`jaah auth rotate`, which also restarts jaahd and moves the apt channel's auth).

    Last, so a run that failed earlier can be re-run as is: until here the token in the script still works. A
    failed rotation is a warning, not a failed install — the node works, on the token it was given."""
    return [
        f'{indent}echo "==> [jaah] rotating the credential"',
        f'{indent}{cli} auth rotate || echo "==> [jaah] warning: not rotated; retry: sudo jaah auth rotate" >&2',
    ]


def _reenrol_lines(inputs: BootstrapInputs, q_name: str, *, macos: bool = False) -> list[str]:
    """The already-provisioned arm's tailnet re-join: only for a re-enrolled node (`tailnet_reenrol`) whose
    key was actually minted. Otherwise nothing, so every other script stays byte-identical.

    `macos` is the Mac guard's "already on ours" arm (`_macos_tailnet_slots`): the key is already in
    `$work/ts.key` (assigned once before the guard), the join is the Mac's (`tailnet_join_command`), and a
    refused `up` is a `cant` line rather than a silent `set -e` exit."""
    if not inputs.tailnet_reenrol or inputs.tailscale_auth_key is None:
        return []
    join = tailnet_join_command(
        q_name, login_server=inputs.tailscale_login_server is not None, force_reauth=True, reset=macos,
        ssh=not macos, cli='"$ts_cli"' if macos else "tailscale", key_file=macos,
    )
    lines = [
        "  # RE-ENROLMENT: an operator re-opened this node's enrolment, so this run carries one tailnet key.",
        "  # `--force-reauth` because the machine is already logged in, possibly to a different server.",
        *(["  # `--reset` because a Mac's stale prefs would make `up` refuse the re-auth."] if macos else []),
        '  echo "==> [jaah] re-joining the tailnet"',
    ]
    if not macos:
        return [*lines, *_tailnet_key_lines(inputs, "  "), f"  {join}", "  unset JAAH_TS_AUTHKEY"]
    return [
        *lines,
        f"  if ! {join}; then",
        '    rm -f -- "$work/ts.key"',
        '    cant "a tailnet re-join (tailscale up refused, above)" "fix what it says, then run this command'
        ' again"',
        "    refuse_if_any",
        "  fi",
        '  rm -f -- "$work/ts.key"',
        "  joined_tailnet=1",
    ]


def validate_inputs(inputs: BootstrapInputs) -> None:
    """`_validate` as a public entry point, for a caller that must be refused before it spends something.

    `cloud_node_launch.with_tailnet_join` validates before minting a single-use tailnet key, so a
    persistently bad `cp_base_url` does not burn one key per fetch.
    """
    _validate(inputs)


#: The marker a Multi-Node Host's bootstrap writes; host-only units carry `ConditionPathExists=` on
#: it. Root-owned, 0644, empty.
VNODE_HOST_MARKER = "/etc/jaah/vnode-host"


def _vnode_host_lines(inputs: BootstrapInputs, apt_install: str, *, self_hosted: bool) -> list[str]:
    """The marker for a Multi-Node Host, and nothing for any other node (byte-identical scripts).
    Written before the package installs, whose postinst starts units conditioned on it."""
    if not inputs.vnode_host:
        return []
    growpart_warning = "|| echo \"==> [jaah] warning: {} did not install; a root disk grow will need it\" >&2"
    return [
        "# This machine is a MULTI-NODE HOST: it carries co-located nodes and runs no work itself.",
        "# The host-only units are conditioned on this marker, so they stay inert everywhere else.",
        f"install -m 0644 /dev/null {VNODE_HOST_MARKER}",
        "# growpart, which the helper's live root grow (`host.grow_root`) runs after a storage increase.",
        "# Ubuntu's cloud images carry it already; installed here so no image can leave a resize stuck.",
        # Optional: only a later storage increase needs it, so a refusal warns and the node still installs.
        *_pkg_branch(
            [f"dnf -y install cloud-utils-growpart {growpart_warning.format('cloud-utils-growpart')}"],
            [f"{apt_install} cloud-guest-utils {growpart_warning.format('cloud-guest-utils')}"],
            self_hosted=self_hosted,
        ),
        "",
    ]


# ── the platform: install-time detection, the fetched script's only ──────────────────────────────────
# The cloud path boots one image (Ubuntu 26.04, `app/aws/ec2.py`) under EC2's user-data cap, so it carries none
# of this: no prelude, no step labels, no dnf arm (`_pkg_branch`).

#: The dnf arm's opening line and the branch's closing one (`_pkg_branch`); the comment names what closes.
_DNF_IF = 'if [ "$JAAH_PKG" = dnf ]; then'
_PKG_FI = "fi  # $JAAH_PKG"

#: The first line of the platform prelude (`_platform_lines`), and its last.
_PLATFORM_BEGIN = "# --- the platform: which OS, package manager and architecture this is ------------------"
_PLATFORM_END = "jaah_detect_platform"


def _pkg_branch(dnf: list[str], apt: list[str], *, self_hosted: bool, indent: str = "") -> list[str]:
    """`apt` alone on the cloud path; on the fetched script, `dnf` and `apt` behind the prelude's `$JAAH_PKG`. The
    apt lines go in exactly as they were, so the Ubuntu body inside its arm reads byte for byte as before: the arms
    are deliberately NOT indented (`test_the_fetched_ubuntu_body_is_byte_identical_apart_from_the_platform`)."""
    if not self_hosted:
        return apt
    return [f"{indent}{_DNF_IF}", *dnf, f"{indent}else", *apt, f"{indent}{_PKG_FI}"]


def _dnf_repo_write_lines(raw_cp: str, indent: str) -> list[str]:
    """Write `DNF_REPO_FILE` (architecture §3.4), 0600 before the token lands in it, by `printf`: a builtin, so the
    token is on no argv. Single-quoted format, so dnf's own `$basearch` reaches the file verbatim; `raw_cp` is a
    scheme plus `_CP_NETLOC_RE` text, which carries no quote or `%`. `includepkgs`: `rpm --import` trusts the key
    machine-wide (apt's `Signed-By` has no rpm twin), so this repo may offer only Jaah's own packages, never one
    that would replace the user's."""
    body =(f"[jaah]\\nname=Jaah\\nbaseurl={raw_cp}{RPM_PATH}/$basearch\\nusername=node\\npassword=%s\\n"
            f"gpgcheck=1\\nrepo_gpgcheck=1\\ngpgkey=file://{RPM_GPG_KEY_FILE}\\nincludepkgs=jaah jaah-*\\nenabled=1\\n")
    return [
        f"{indent}install -m 600 /dev/null {DNF_REPO_FILE}",
        f"{indent}printf '{body}' \"$JAAH_NODE_TOKEN\" > {DNF_REPO_FILE}",
    ]


#: Tailscale's own repository for Amazon Linux 2023, the dnf arm's fallback where its installer names the release
#: unsupported (AL2027 preview, R4); its packages are static Go builds, so the 2023 ones run there.
TAILSCALE_AL2023_REPO = "https://pkgs.tailscale.com/stable/amazon-linux/2023/tailscale.repo"


def _dnf_base_lines() -> list[str]:
    """The dnf arm's base packages. A box that was a node before still carries that node's repo file, whose old
    token the control plane refuses: every dnf run 401s on it, so it goes first (the channel step writes it anew).
    Amazon Linux ships `curl-minimal`, which conflicts with the `curl` package, so curl is installed only when the
    box has none. The apt arm's python3-pip/venv have no twin: the jaah rpm brings its own Python (`jaah-python`).

    `jaah_dnf_wait` is the apt arm's lock wait (`jaah_apt_update`) for rpm: a fresh Amazon Linux box runs other
    package installs in its first minutes (GuardDuty's runtime agent arrives by SSM as a `yum install`), and
    `rpm --import` in Tailscale's installer fails at once on a held rpm lock rather than waiting. Up to 600s, as
    apt's; matched on the process name, which is the script's for dnf/yum."""
    return [
        f"rm -f {DNF_REPO_FILE} {RPM_GPG_KEY_FILE}",
        "jaah_dnf_wait() {",
        "  local tries=0",
        "  command -v pgrep >/dev/null 2>&1 || return 0",
        "  while pgrep -x 'dnf|dnf-3|dnf5|yum|rpm|dnf-automatic' >/dev/null; do",
        '    [ "$tries" -ge 60 ] && return 0',
        "    tries=$((tries + 1))",
        '    echo "==> [jaah] another package install is running; waiting for it" >&2',
        "    sleep 10",
        "  done",
        "}",
        "jaah_dnf_wait",
        'echo "==> [jaah] installing base packages"',
        'jaah_dnf_base="ca-certificates git tmux"',
        'command -v curl >/dev/null 2>&1 || jaah_dnf_base="$jaah_dnf_base curl"',
        "dnf -y install $jaah_dnf_base",
    ]


def _dnf_tailscale_lines(indent: str) -> list[str]:
    """Tailscale on the dnf arm: its installer, else, on Amazon Linux only (a release the installer does not list
    yet), its Amazon Linux 2023 repo. Any other dnf distro fails at this step, as the installer did."""
    return [
        f"{indent}jaah_dnf_wait",
        f"{indent}if ! curl -fsSL https://tailscale.com/install.sh | sh; then",
        f'{indent}  [ "$JAAH_OS_ID" = amzn ] || exit 1',
        f'{indent}  echo "==> [jaah] warning: Tailscale\'s installer failed on $JAAH_OS_LABEL;'
        ' trying its Amazon Linux 2023 packages" >&2',
        f"{indent}  jaah_dnf_wait",
        f"{indent}  curl -fsSL {TAILSCALE_AL2023_REPO} -o /etc/yum.repos.d/tailscale.repo",
        f"{indent}  dnf -y install tailscale",
        f"{indent}  systemctl enable --now tailscaled",
        f"{indent}fi",
    ]


def _dnf_channel_lines(q_pubkey_url: str, raw_cp: str) -> list[str]:
    """The dnf arm's package channel: the apt channel's signing key, fetched as there (token on stdin, never argv)
    and imported into rpm, then the repo file (`_dnf_repo_write_lines`)."""
    return [
        'echo "==> [jaah] fetching the package signing key"',
        "install -d -m 0755 /etc/pki/rpm-gpg",
        f"printf 'header = \"X-Agent-Token: %s\"\\n' \"$JAAH_NODE_TOKEN\" "
        f"| curl -K - -fsSL {q_pubkey_url} -o {RPM_GPG_KEY_FILE}",
        f"chmod 0644 {RPM_GPG_KEY_FILE}",
        # rpm takes its lock at once rather than waiting, and an install that starts between the wait and the
        # import (GuardDuty's agent did, on dev EC2) fails it: waited for, then retried five times; a sixth try's
        # failure stands.
        "jaah_dnf_wait",
        "jaah_import_tries=0",
        f"until rpm --import {RPM_GPG_KEY_FILE}; do",
        "  jaah_import_tries=$((jaah_import_tries + 1))",
        f'  [ "$jaah_import_tries" -lt 5 ] || {{ rpm --import {RPM_GPG_KEY_FILE}; break; }}',
        '  echo "==> [jaah] rpm is busy; importing the signing key again" >&2',
        "  jaah_dnf_wait",
        "  sleep 5",
        "done",
        f'echo "==> [jaah] writing {DNF_REPO_FILE}"',
        *_dnf_repo_write_lines(raw_cp, ""),
        "",
    ]


def _platform_lines() -> list[str]:
    """The single home of install-time detection: `jaah_detect_platform` sets `JAAH_OS_ID`, `JAAH_OS_VERSION`,
    `JAAH_OS_LABEL`, `JAAH_PKG` (apt|dnf) and `JAAH_ARCH` (`uname -m`), and `jaah_step` names the step a failure
    is reported at (D17). The supported list is rendered from `packaging/linux-support.toml`, never restated.

    A release outside it is WARNED about first and tried (C-8): its package manager is the one present, apt
    before dnf (D16); a machine with neither, or an architecture outside the list, fails at that step."""
    support = node_platform.linux_support()
    releases, arches = support["release"], support["arches"]
    listed = ", ".join(r["label"] + (" (preview)" if r.get("preview") else "") for r in releases)
    supported = shlex.quote(f"Supported: {listed or 'none listed'} on {' or '.join(arches) or 'no architecture'}.")
    arms = []
    for r in releases:
        if r["pkg"] not in ("apt", "dnf"):
            raise ValueError(f"linux-support.toml: {r['label']} names pkg {r['pkg']!r}, not apt or dnf")
        arms.append(f"    {shlex.quote(r['id'] + '/' + r['version_id'])}) "
                    f"JAAH_OS_LABEL={shlex.quote(r['label'])}; JAAH_PKG={r['pkg']} ;;")
    # No list (a missing toml) refuses no architecture: warn and try, as for an unlisted release.
    arch_pattern = "|".join(shlex.quote(a) for a in arches) or "*"
    return [
        _PLATFORM_BEGIN,
        "# A failure anywhere below names the step it happened in; a deliberate refusal names its own.",
        "JAAH_STEP=start",
        'jaah_step() { JAAH_STEP="$1"; }',
        'jaah_on_exit() { [ "$1" -eq 0 ] || echo "==> [jaah] FAILED at step: $JAAH_STEP" >&2; }',
        "trap 'jaah_on_exit $?' EXIT",
        "# One field of /etc/os-release, its quotes stripped; read, never sourced.",
        "jaah_os_field() {",
        "  awk -F= -v k=\"$1\" '$1 == k {v = substr($0, length(k) + 2); gsub(/^[\"\\047]|[\"\\047]$/, \"\", v); "
        "print v; exit}' /etc/os-release 2>/dev/null || true",
        "}",
        "jaah_detect_platform() {",
        '  jaah_step "detect operating system"',
        '  JAAH_OS_ID="$(jaah_os_field ID)"',
        '  JAAH_OS_VERSION="$(jaah_os_field VERSION_ID)"',
        "  JAAH_OS_LABEL=''",
        "  JAAH_PKG=''",
        '  case "$JAAH_OS_ID/$JAAH_OS_VERSION" in',
        *arms,
        "  esac",
        '  if [ -z "$JAAH_OS_LABEL" ]; then',
        '    JAAH_OS_LABEL="$(jaah_os_field NAME)"',
        '    JAAH_OS_LABEL="${JAAH_OS_LABEL% GNU/Linux}"',
        '    JAAH_OS_LABEL="${JAAH_OS_LABEL:-${JAAH_OS_ID:-an unknown Linux}}${JAAH_OS_VERSION:+ $JAAH_OS_VERSION}"',
        f'    echo "==> [jaah] warning: $JAAH_OS_LABEL is not a supported release; trying anyway. "{supported} >&2',
        "  fi",
        '  jaah_step "detect package manager"',
        '  if [ -z "$JAAH_PKG" ]; then',
        "    if command -v apt-get >/dev/null 2>&1; then JAAH_PKG=apt",
        "    elif command -v dnf >/dev/null 2>&1; then JAAH_PKG=dnf",
        "    else",
        '      echo "==> [jaah] this machine has neither apt nor dnf, and Jaah installs with one of them" >&2',
        "      exit 1",
        "    fi",
        "  fi",
        '  jaah_step "detect architecture"',
        '  JAAH_ARCH="$(uname -m)"',
        '  case "$JAAH_ARCH" in',
        f"    {arch_pattern}) ;;",
        "    *)",
        f'      echo "==> [jaah] this machine\'s architecture, $JAAH_ARCH, is not supported. "{supported} >&2',
        "      exit 1 ;;",
        "  esac",
        "}",
        _PLATFORM_END,
        "",
    ]


def _jaah_home_choice_lines() -> list[str]:
    """Shell that sets `$jaah_home` ON THE BOX: an explicit `JAAH_HOME` already in jaahd.env wins, else
    `PROVISIONED_JAAH_HOME`. A box still on a pre-rename home keeps it here, and the package's rename wave R3
    (its rename script) moves it and the env file onto `PROVISIONED_JAAH_HOME`; the row's seed follows
    the node's own report (`app/node_config.py`)."""
    return [
        "# The home: a JAAH_HOME this box was already given wins, else the new ~/.jaah.",
        "jaah_home=''",
        "if [ -f /etc/jaah/jaahd.env ]; then",
        # Last line wins, as the package's postinst reads it (`tail -n 1`). One awk, always exit 0.
        # One pair of surrounding quotes is stripped, as systemd's `EnvironmentFile=` does.
        "  jaah_home=\"$(awk '/^JAAH_HOME=/{"
        "sub(/^JAAH_HOME=/, \"\"); sub(/\\r$/, \"\"); "
        "if (match($0, /^\"[^\"]*\"$/) || match($0, /^\\047[^\\047]*\\047$/)) $0 = substr($0, 2, length($0) - 2); "
        "j=$0} END{print j}' /etc/jaah/jaahd.env)\"",
        "fi",
        f'[ -n "$jaah_home" ] || jaah_home={PROVISIONED_JAAH_HOME}',
    ]


def _ask_another_cp_lines() -> list[str]:
    """The guard's question to ANOTHER control plane: is the box's former node still there? The fetched
    script's only — an operator moves a box between environments by hand, while a cloud box boots a stock
    AMI (and EC2 user-data has a size cap), so cloud-init refuses as before, naming that CP.

    Its OWN 401 (`routes/agent/auth.py::_authenticate_agent`, not a proxy's) is the answer "gone": a deleted
    node's row takes its token's hash with it. A known limit: a token re-issued but never run on this box
    reads the same, and leaves that row behind with no machine. Only an https URL of a scheme and host is
    dialled, never a redirect; the box's own token rides a --config header on stdin, never argv."""
    return [
        "      former_token=\"$(awk '/^NODE_TOKEN=/{sub(/^NODE_TOKEN=/, \"\"); "
        "sub(/\\r$/, \"\"); print; exit}' /etc/jaah/node.env)\"",
        "      if [[ \"$former_cp\" =~ ^https://[A-Za-z0-9.:-]+$ ]] && [ -n \"$former_token\" ]; then",
        "        former_reply=\"$(",
        "          printf 'header = \"X-Agent-Token: %s\"\\n' \"$former_token\" "
        f"| curl -K - -sS --max-time 30 -w ' %{{http_code}}' "
        f"\"$former_cp\"{shlex.quote(f'{BOOTSTRAP_PATH}?{FORMER_NODE_PARAM}=')}\"$existing_node_id\" "
        "2>/dev/null || true",
        "        )\"",
        "        case \"$former_reply\" in",
        "          'present 200') former_node=present ;;",
        "          'absent 200'|*'\"invalid agent token\"} 401') former_node=absent ;;",
        "        esac",
        "      fi",
        "      unset former_token",
    ]


def _body(inputs: BootstrapInputs, *, self_hosted: bool = False) -> str:
    """The bootstrap itself, identical on both paths but for `self_hosted` (the fetched script's). That
    script may run at an operator's terminal (`_tailnet_switch_lines` asks there), and on a machine that
    is the user's own, so it leaves the OS alone: no upgrade of a package already there, no patching
    policy, never a kernel (`_unattended_upgrades_lines` is the cloud path's only). Runs as root, `set -euo pipefail`."""
    # Rebuilt from the validated parts, never the raw `inputs.cp_base_url` (a query string once carried
    # `$(id)` into the unquoted heredoc). `base` holds only a scheme plus `_CP_NETLOC_RE` text.
    _parts = urlsplit(inputs.cp_base_url)
    base = f"{_parts.scheme}://{_parts.netloc}"
    q_name = shlex.quote(inputs.node_name)
    q_id = shlex.quote(inputs.node_id)
    q_token = shlex.quote(inputs.node_token)
    q_pubkey_url = shlex.quote(f"{base}{APT_PUBKEY_PATH}")
    q_host = shlex.quote(cp_host(inputs.cp_base_url))
    q_former_url = shlex.quote(f"{base}{BOOTSTRAP_PATH}?{FORMER_NODE_PARAM}=")
    # apt's HTTP Basic credential for the CP apt channel: the first run writes it, a rotation rewrites it.
    def apt_auth_write(target: str) -> str:
        return (f"printf 'machine %s{APT_PATH} login node password %s\\n' {q_host} "
                f'"$JAAH_NODE_TOKEN" > {target}')
    # Raw, not quoted: these land in heredocs; `_CP_NETLOC_RE` already excludes `$`, backticks, quotes
    # and newlines.
    raw_apt_uri = f"{base}{APT_PATH}"
    raw_cp = base
    # A missing tool is installed; on the user's own machine one already there is never upgraded. What the
    # `jaah` package itself depends on can still pull an upgrade — that is the cost of installing it.
    apt_install = "apt-get install -y --no-upgrade" if self_hosted else "apt-get install -y"

    def step(name: str, indent: str = "") -> list[str]:
        """The label a failure from here on is reported at (`_platform_lines`); the fetched script's only."""
        return [f'{indent}jaah_step "{name}"'] if self_hosted else []

    lines: list[str] = [
        "set -euo pipefail",
        "",
        "# cloud-init runs this with no $HOME at all, and anything below that consults a dotfile",
        "# (git, apt helpers) dies with \"$HOME not set\" when it is missing. Set unconditionally",
        "# rather than only on the cloud path, so the two bootstraps stay one body — and it is",
        "# correct on the fetched path too, where the shell is a `sudo bash` whose $HOME is not root's.",
        "export HOME=/root",
        "export DEBIAN_FRONTEND=noninteractive",
        "",
        *(_platform_lines() if self_hosted else []),
        "# The token this node authenticates with, and the only place it appears. Kept in a shell",
        "# variable so no later command puts it on a process's argv, where `ps` would show it.",
        f"JAAH_NODE_TOKEN={q_token}",
        "",
        *step("check for an existing node"),
        "# --- ALREADY A NODE? THEN THIS IS A CREDENTIAL ROTATION, NOT A PROVISIONING ----------",
        "# ONE COMMAND, TWO JOBS, and this branch is what makes that safe. The same line is handed to an",
        "# operator provisioning a bare box AND to one re-issuing the token of a machine that is already",
        "# running — and everything below this block is destructive on the second: it reinstalls the",
        "# package (restarting jaahd and killing live runners), rewrites /etc/jaah/jaahd.env,",
        "# truncates node.env, overwrites the apt sources of a node that may read its packages straight",
        "# from S3 under an IAM profile, and re-joins a tailnet the machine is already on. So a machine",
        "# that has already been enrolled takes the credential write and NOTHING ELSE.",
        "#",
        "# THE TEST IS node.env's OWN NODE_ID, because it is written by this script BEFORE the package",
        "# is installed. A run that died halfway therefore leaves the id with no `jaah` binary beside",
        "# it — and falls through to a full provisioning, which is exactly what that box still needs.",
        # Source-only, so it costs no user-data bytes (EC2's cap: `test_the_largest_real_bootstrap_fits_ec2s_cap`).
        # IT IS IN THE SHARED BODY, SO IT IS ON THE CLOUD PATH TOO, and there it is inert on a stock
        # AMI (no /etc/jaah, so `existing_node_id` is empty and the whole block falls through). The
        # one case where it is not inert is an AMI baked FROM a running node, which carries that node's
        # node.env: every instance launched from it now refuses with `exit 1` inside cloud-init instead
        # of silently re-pointing a second machine at the first one's identity. That is the better
        # outcome and it is still a trap, because on the cloud path nobody is reading the console — an
        # operator who pins such an AMI sees capacity that never registers. Bake AMIs from a machine
        # that was never enrolled, or strip /etc/jaah from the image.
        "existing_node_id=''",
        "if [ -f /etc/jaah/node.env ]; then",
        # One `awk`, no pipe, always exit 0, under `set -euo pipefail`: a pipe can SIGPIPE-abort
        # silently, `grep -m1` exits 1 on no match, and `sed …;q` stops at the first line, not the
        # first match (`NODE_ID` is not always line 1).
        "  existing_node_id=\"$(awk '/^NODE_ID=/{sub(/^NODE_ID=/, \"\"); "
        "sub(/\\r$/, \"\"); print; exit}' /etc/jaah/node.env)\"",
        "fi",
        "# A DIFFERENT LIVE NODE IS A REFUSAL, NEVER A REPOINT. Running one node's command on another's",
        "# box would hand this row's identity to a machine the fleet already knows by another name, and",
        "# the operator would have two rows pointing at one host with no way to tell which is live.",
        "# A DELETED one is not: an operator who deleted a node and re-added the same machine must not",
        "# have to clean files up by hand. So the control plane is asked whether that node",
        f"# still exists (`?{FORMER_NODE_PARAM}=`, `routes/agent_bootstrap.py`); only an explicit",
        "# `absent` forgets it and falls through to a full provisioning — the old node is gone, so",
        "# nothing below can harm it. Any other answer (present, unreachable, malformed id) refuses.",
        "former_node_deleted=''",
        f"if [ -n \"$existing_node_id\" ] && [ \"$existing_node_id\" != {q_id} ]; then",
        # Only the CP that issued an id can say it is gone: an id another CP issued is absent here whether
        # or not it lives there (a dev node handed a prod line). So a box enrolled elsewhere is asked about
        # THERE, with its own credential — this run's token is this CP's and means nothing to that one.
        "  former_cp=''",
        "  if [ -f /etc/jaah/jaahd.env ]; then",
        "    former_cp=\"$(awk '/^JAAH_CP_BASE_URL=/ && !h{"
        "sub(/^JAAH_CP_BASE_URL=/, \"\"); sub(/\\r$/, \"\"); j=$0; h=1} "
        "END{print j}' /etc/jaah/jaahd.env)\"",
        "  fi",
        "  former_cp=\"${former_cp%/}\"",
        "  former_at=''",
        "  former_console='the console'",
        f"  if [ -n \"$former_cp\" ] && [ \"$former_cp\" != {shlex.quote(raw_cp)} ]; then",
        "    former_at=\" of the control plane at $former_cp\"",
        "    former_console=\"the console at $former_cp\"",
        "  fi",
        "  former_node=''",
        # The id goes into a URL, so only a uuid's characters reach curl; anything else is never asked.
        "  if [[ \"$existing_node_id\" =~ ^[0-9A-Fa-f-]{36}$ ]]; then",
        "    if [ -n \"$former_at\" ]; then",
        *(_ask_another_cp_lines() if self_hosted else ["      :"]),
        "    else",
        # The token as a --config header on stdin, never on argv (the apt-key fetch below says why).
        "      former_node=\"$(",
        "        printf 'header = \"X-Agent-Token: %s\"\\n' \"$JAAH_NODE_TOKEN\" "
        f"| curl -K - -fsS --max-time 30 {q_former_url}\"$existing_node_id\" 2>/dev/null || true",
        "      )\"",
        "    fi",
        "  fi",
        '  if [ "$former_node" = absent ]; then',
        '    echo "==> [jaah] this machine was node $existing_node_id$former_at, which has been deleted; enrolling it as the new node"',
        "    existing_node_id=''",
        "    former_node_deleted=1",
        # The deleted node's Multi-Node Host marker would start host-only units on a plain node; a new
        # host's provisioning writes it back below (`_vnode_host_lines`).
        f"    rm -f {VNODE_HOST_MARKER}",
        '  elif [ "$former_node" = present ]; then',
        '    echo "this machine is already jaah node $existing_node_id$former_at — refusing to re-point it" >&2',
        '    echo "that node still exists: delete it in $former_console (Nodes), then run the command again." >&2',
        "    exit 1",
        "  else",
        '    echo "this machine is already jaah node $existing_node_id$former_at — refusing to re-point it" >&2',
        '    echo "the control plane could not confirm that node was deleted. If it is gone and this box" >&2',
        '    echo "should become the one you just registered, remove /etc/jaah/node.env and run the command again." >&2',
        "    exit 1",
        "  fi",
        "fi",
        "# The operator CLI.",
        'node_cli="$(command -v jaah || true)"',
        'if [ -n "$existing_node_id" ] && [ -n "$node_cli" ]; then',
        *step("write the new credential", "  "),
        '  echo "==> [jaah] already provisioned; writing the new credential only"',
        f'  printf \'%s\\n\' "$JAAH_NODE_TOKEN" | "$node_cli" auth login {q_id} --with-token',
        "  # The CP apt channel authenticates with this same token (written below on a first run). Left",
        "  # behind, apt 401s and the node never leaves its current release. Only where that channel is",
        "  # in use: a node reading its packages from S3 under an IAM profile has no such file.",
        "  if [ -f /etc/apt/auth.conf.d/jaah.conf ]; then",
        "    " + apt_auth_write("/etc/apt/auth.conf.d/jaah.conf"),
        "  fi",
        # dnf's twin: the repo file carries the token as its password (`_dnf_repo_write_lines`).
        *([f"  if [ -f {DNF_REPO_FILE} ]; then", *_dnf_repo_write_lines(raw_cp, "    "), "  fi"]
          if self_hosted else []),
        "  unset JAAH_NODE_TOKEN",
        *_reenrol_lines(inputs, q_name),
        *step("rotate the credential", "  "),
        *_rotate_lines('"$node_cli"', "  "),
        '  echo "==> [jaah] done"',
        "  exit 0",
        "fi",
        "",
        *step("install base packages"),
        *_pkg_branch(_dnf_base_lines(), [
        "# Ubuntu boots apt-daily/unattended-upgrades on a timer and habitually holds the dpkg lock",
        "# for the first minutes of a fresh box — exactly when this script wants it. Losing that",
        "# race is fatal under `set -e`, and it is a 50/50 first boot. Written as apt CONFIG rather",
        "# than a flag, because Tailscale's vendored installer runs its own apt-get and that is the",
        "# one that actually failed on the fleet (aws2, 2026-08-13).",
        "install -d -m 0755 /etc/apt/apt.conf.d",
        "printf 'DPkg::Lock::Timeout \"600\";\\n' > /etc/apt/apt.conf.d/99jaah-lock-timeout",
        "",
        # That timeout covers dpkg's lock, NOT the package-lists lock `apt-get update` takes, and the
        # same first-boot apt run holds that one too: unwaited, the very first update aborts the run
        # ("Could not get lock /var/lib/apt/lists/lock"). Waited out here, up to the same
        # 600s; any other update failure stays fatal at once. `LC_ALL=C`: the match is on apt's English text.
        # A Python comment, not a rendered one: EC2 user-data has a size cap (`test_aws_ec2.py`).
        "jaah_apt_update() {",
        "  local err tries=0",
        "  err=\"$(mktemp)\"",
        "  until LC_ALL=C apt-get update 2>\"$err\"; do",
        "    cat \"$err\" >&2",
        "    if ! grep -q 'Could not get lock' \"$err\" || [ \"$tries\" -ge 60 ]; then rm -f \"$err\"; return 1; fi",
        "    tries=$((tries + 1))",
        "    echo \"==> [jaah] another apt run holds the package lists; waiting for it\" >&2",
        "    sleep 10",
        "  done",
        "  cat \"$err\" >&2",
        "  rm -f \"$err\"",
        "}",
        "",
        # A box that was a node before (its node deleted, or its /etc/jaah removed) still carries that
        # node's apt channel: the source, its keyring and an auth file holding the OLD token, which the
        # control plane now refuses. Left in place, the very next `apt-get update` — this script's, or
        # Tailscale's installer's — 401s and aborts the run before the fresh files below are written.
        # Removed, not patched: the apt channel section below writes all three anew with this token.
        # Python comments, unrendered: EC2 user-data is size-capped.
        "rm -f /etc/apt/sources.list.d/jaah.sources /etc/apt/keyrings/jaah.asc /etc/apt/auth.conf.d/jaah.conf",
        "",
        *([] if self_hosted else _universe_lines()),
        'echo "==> [jaah] installing base packages"',
        "jaah_apt_update",
        # `python3-venv` too: Ubuntu splits out `ensurepip`, GHA runner workflows create venvs, and the jaah
        # package's postinst builds the daemon's own venv from PyPI — so beyond the apt channel the node needs
        # pypi.org + files.pythonhosted.org at install and at an upgrade that changes the pins
        # (docs/policies/jaah-package-distribution.md § The daemon's Python dependencies).
        # Unversioned metapackage, so it keeps matching after python3 moves.
        f"{apt_install} ca-certificates curl gnupg git tmux python3-pip python3-venv",
        ], self_hosted=self_hosted),
        "",
        *([] if self_hosted else _unattended_upgrades_lines()),
    ]

    if inputs.tailscale_auth_key is not None:
        lines += [
            "# THE TAILNET. This node has no route to the control plane without it: the machine",
            "# surface lives only on the internal ALB and is refused on the public one.",
            "# The key is single-use and pre-authorized, minted by the control plane for this node",
            "# alone, so it enrols exactly one machine and cannot be replayed.",
            "# LEAVE `--authkey` ON THE ARGV. It has the same `ps`-readable shape the apt fetch above",
            "# goes out of its way to avoid, and it stays because it is VERBATIM the form the fleet's",
            "# retired terraform user-data used and the form Tailscale's own docs give; inventing a",
            "# second spelling of one join buys nothing for a window measured in seconds, on a key",
            "# that is single-use and spent by the time `tailscale up` returns.",
            "# GUARDED ON `tailscale status`, which fails when the binary is absent AND when it is",
            "# present but not logged in — both of which mean this box still needs the join. A box",
            "# already on the tailnet skips it: `tailscale up` with a fresh key on an enrolled machine",
            "# re-registers it, which on a hostname clash lands a duplicate device and drops the ACL",
            "# tags the original carried. The route above already declines to mint a key for a node the",
            "# control plane has seen, so this guards the remaining case — a machine joined by hand, or",
            "# by a run that got this far and then failed.",
            *step("join the tailnet"),
            *_other_tailnet_probe_lines(inputs),
            "if tailscale status >/dev/null 2>&1"
            + (' && [ -z "$jaah_other_tailnet" ]' if inputs.tailscale_login_server is not None else "")
            + "; then",
            '  echo "==> [jaah] already on the tailnet; skipping the join"',
            # A box whose node was deleted keeps the deleted node's device (`delete_node` does not remove
            # it), so it is renamed in place — `tailscale set`, never a re-registering `tailscale up`.
            # Not fatal: the hostname is a label, and the prefs below are what reaching home needs.
            '  if [ -n "${former_node_deleted:-}" ]; then',
            f"    tailscale set --hostname={q_name} || echo \"==> [jaah] could not rename this tailnet device\" >&2",
            "  fi",
            *_tailnet_switch_lines(inputs, q_name, ask=self_hosted),
            "else",
            '  echo "==> [jaah] installing Tailscale"',
            *_pkg_branch(_dnf_tailscale_lines("    "), [
            "  curl -fsSL https://tailscale.com/install.sh | sh",
            ], self_hosted=self_hosted, indent="  "),
            *_tailnet_key_lines(inputs, "  "),
            '  echo "==> [jaah] joining the tailnet"',
            # Bounded (`--timeout`): a join that never comes up exits with tailscaled's own state on
            # stderr, and the EXIT trap names this step.
            "  if ! " + tailnet_join_command(q_name, login_server=inputs.tailscale_login_server is not None)
            + "; then",
            f'    echo "==> [jaah] the tailnet join did not come up within {TAILNET_JOIN_TIMEOUT}; tailscaled says:" >&2',
            "    timeout 15 tailscale status >&2 || true",
            # A logged-out `status` says only that; the refusal (a 429, a bad key) is in tailscaled's log.
            "    timeout 15 journalctl -u tailscaled -n 20 --no-pager >&2 || true",
            "    exit 1",
            "  fi",
            "  unset JAAH_TS_AUTHKEY",
            "fi",
            "",
            "# THE TWO PREFS, CONVERGED ON BOTH PATHS — the join above sets them, and the skip above",
            "# sets nothing at all. That asymmetry is the whole defect this closes: the CP hostnames",
            "# are INTERNAL ALBs (docs/architecture.md §Environments), so a box reaches the control",
            "# plane only over the bastion subnet route `--accept-routes` accepts. An operator who",
            "# installed Tailscale themselves (which a one-liner fetched from an internal URL once",
            "# required) took the skip branch and got neither pref, leaving a",
            "# node that cannot dial home and cannot be SSH'd into to find out why. Measured on h3",
            "# 2026-09-02: `tailscale set --accept-routes` and `tailscale set --ssh` both run by hand.",
            "#",
            "# `tailscale set`, NEVER a second `tailscale up`: it edits prefs in place and does not",
            "# re-register the device, so it carries none of the duplicate-device/ACL-tag risk the",
            "# guard above exists to avoid. Idempotent, so the join path re-asserting them costs a",
            "# no-op.",
            "#",
            "# TWO CALLS, NOT ONE, BECAUSE ONLY ONE OF THEM IS A PREREQUISITE. `tailscale set` applies",
            "# its flags as a single prefs edit, so `--accept-routes --ssh` together means a refused",
            "# `--ssh` also discards the route. Tailscale SSH is unavailable under",
            "# userspace-networking (a container, an LXC guest, an unprivileged host — exactly the bare",
            "# box in someone's office this path exists for) and can be off by tailnet policy, so",
            "# pairing them would refuse enrolment to a whole class of node over a diagnostic",
            "# convenience — and push its operator to loosen the tailnet ACL to get provisioned.",
            *step("converge the tailnet prefs"),
            'echo "==> [jaah] converging the tailnet prefs this node needs"',
            "",
            "# FATAL, with the remediation named. Without the route every later step — the apt key",
            "# fetch, `apt-get install jaah`, `jaah auth login` — fails against an address this",
            "# box has no path to, and reports it as a connection error a long way from this cause.",
            "if ! tailscale set --accept-routes; then",
            '  echo "==> [jaah] could not accept subnet routes on this machine\'s tailnet prefs." >&2',
            "  echo \"Without them this node has no route to the control plane's internal ALB\" >&2",
            '  echo "and nothing below this line can reach it." >&2',
            "  exit 1",
            "fi",
            "",
            "# BEST-EFFORT: this is how an operator gets a shell on a box whose enrolment went wrong,",
            "# which is worth having and is not worth refusing a working node over.",
            "if ! tailscale set --ssh; then",
            '  echo "==> [jaah] warning: Tailscale SSH is not enabled here (userspace networking, or" >&2',
            '  echo "tailnet policy). It provisions fine; diagnose it from its own console." >&2',
            "fi",
            "",
            "# IS THIS DEVICE THE ONE THE CONTROL PLANE MINTED A KEY FOR? A node enrolled by the key",
            "# above carries the fleet's tailnet tags, and the tailnet ACL grants access BY TAG",
            "# (`app/headscale_keys.py` refuses to mint an untagged key for exactly that reason). A box",
            "# an operator joined by hand instead — which out here they must, to reach this control",
            "# plane at all — enrols under their OWN identity, never redeems the minted key, and sits",
            "# outside every tag-based grant the fleet's ACL makes. It still works; it is simply not",
            "# governed the way the roster implies, and nothing else would ever say so.",
            "#",
            "# `--peers=false` so the match is about THIS device: the full status document carries every",
            "# peer's tags too, and a substring search over that reports any tagged tailnet as tagged.",
            "# Assigned rather than piped — `grep -q` closes the pipe early, and under `pipefail` the",
            "# resulting SIGPIPE on the left-hand side reads as a failure on a device that is fine.",
            "# `Tags` is omitted entirely when a node carries none, so its presence IS the answer, and",
            "# an unreadable status says nothing rather than guessing.",
            "ts_self=\"$(tailscale status --json --peers=false 2>/dev/null || true)\"",
            'case "$ts_self" in',
            "  *'\"Tags\"'*|'') : ;;",
            "  *)",
            '    echo "==> [jaah] note: this machine joined the tailnet under an operator identity," >&2',
            '    echo "not the fleet tag this node was registered with, so the tailnet ACL rules that" >&2',
            '    echo "grant access by tag do not cover it. To enrol it as a tagged fleet device," >&2',
            '    echo "remove it from the tailnet and re-run this command on a machine that is not" >&2',
            '    echo "yet joined." >&2',
            "    ;;",
            "esac",
            "",
        ]
    else:
        lines += [
            "# NO TAILNET JOIN. The control plane named no tailnet credential when this script was",
            "# rendered (Settings > Nodes > Cloud), which is correct only when this node sits inside",
            "# the fleet's own VPC and reaches the internal control-plane ALB by plain VPC routing.",
            "# Anywhere else it will come up unable to dial home.",
            "",
        ]

    lines += [
        *step("configure the package channel"),
        *_pkg_branch(_dnf_channel_lines(q_pubkey_url, raw_cp), [
        "# --- the apt channel -------------------------------------------------------------------",
        "# The control plane re-serves the fleet's signed apt repository over this node's own token,",
        "# so no AWS credential is handed out and the S3 bucket behind it stays private. The",
        "# packages are the same GPG-signed artifacts and are verified by the keyring below, so",
        "# authenticity does not rest on the transport.",
        'echo "==> [jaah] fetching the apt repo signing key"',
        "install -d -m 0755 /etc/apt/keyrings",
        "# The header reaches curl as a --config file ON STDIN, never as an argument. curl is an",
        '# external binary, so `-H \"X-Agent-Token: $JAAH_NODE_TOKEN\"` would leave the EXPANDED',
        "# token in /proc/<pid>/cmdline for the length of the fetch, where any local user's `ps`",
        "# reads it — the one thing the JAAH_NODE_TOKEN comment above promises does not happen.",
        "# `printf` is a shell builtin and forks nothing, so no process here ever carries the token.",
        "# The config-file value is double-quoted, whose only escapes are \\\\ and \\\" — neither can",
        "# occur in a token, `_NODE_TOKEN_RE` having already pinned it to snt_ + urlsafe base64.",
        "# `set -o pipefail` is what keeps a failed fetch fatal now that curl is not the last command.",
        f"printf 'header = \"X-Agent-Token: %s\"\\n' \"$JAAH_NODE_TOKEN\" "
        f"| curl -K - -fsSL {q_pubkey_url} -o /etc/apt/keyrings/jaah.asc",
        "chmod 0644 /etc/apt/keyrings/jaah.asc",
        "",
        "# apt cannot send a custom header, so it authenticates with HTTP Basic out of auth.conf.d;",
        "# the control plane accepts either. 0600 and root-owned: this file holds the token, and apt",
        "# reads auth.conf.d as root before dropping to _apt.",
        "install -d -m 0755 /etc/apt/auth.conf.d",
        "install -m 600 /dev/null /etc/apt/auth.conf.d/jaah.conf",
        apt_auth_write("/etc/apt/auth.conf.d/jaah.conf"),
        "",
        "# deb822. Architectures is DERIVED: the package is arch-independent but a sources file",
        "# naming an architecture this machine is not makes `apt-get install jaah` report the",
        "# package uninstallable.",
        'echo "==> [jaah] writing /etc/apt/sources.list.d/jaah.sources"',
        # Unquoted heredoc, on purpose: `Architectures` needs `$(dpkg --print-architecture)` to
        # expand. Nothing else interpolated here can carry a `$` — see `_CP_NETLOC_RE`.
        "cat > /etc/apt/sources.list.d/jaah.sources <<EOF",
        "Types: deb",
        f"URIs: {raw_apt_uri}",
        "Suites: stable",
        "Components: main",
        "Architectures: $(dpkg --print-architecture)",
        "Signed-By: /etc/apt/keyrings/jaah.asc",
        "EOF",
        "chmod 0644 /etc/apt/sources.list.d/jaah.sources",
        "",
        ], self_hosted=self_hosted),
        "# --- /etc/jaah, written BEFORE the package ------------------------------------------",
        "# The package's postinst enables AND STARTS jaahd in the same transaction, so a daemon",
        "# that starts before this exists starts wrong. That ordering is load-bearing.",
        *step("write /etc/jaah"),
        "mkdir -p /etc/jaah",
        *_jaah_home_choice_lines(),
        'echo "==> [jaah] writing /etc/jaah/jaahd.env"',
        # Quoted heredoc: nothing here expands, as a property of the script, not the values.
        "cat > /etc/jaah/jaahd.env <<'EOF'",
        f"JAAH_CP_BASE_URL={raw_cp}",
        # No line at all when no hash was carried (§absence-is-not-a-state). Raw, not quoted (quoted
        # heredoc); `_validate` pinned it to 64 hex characters.
        *(
            [f"JAAH_REMOTE_SETTINGS_CONSENT_HASH={inputs.remote_settings_consent_hash}"]
            if inputs.remote_settings_consent_hash
            else []
        ),
        "EOF",
        # The one value chosen on the box, so outside the quoted heredoc; `%s` expands nothing in it.
        "printf 'JAAH_HOME=%s\\n' \"$jaah_home\" >> /etc/jaah/jaahd.env",
        "chmod 0644 /etc/jaah/jaahd.env",
        "",
        *_vnode_host_lines(inputs, apt_install, self_hosted=self_hosted),
        "# node.env carries identity now and the credential in a moment. Created 0600 before any",
        "# write — between create and chmod the token would be world-readable — and handed to the",
        "# daemon's group by `jaah auth login` once the package has created that group.",
        "install -m 600 /dev/null /etc/jaah/node.env",
        f"printf 'NODE_ID=%s\\n' {q_id} >> /etc/jaah/node.env",
        "",
        "# --- the software ----------------------------------------------------------------------",
        *step("install jaah"),
        'echo "==> [jaah] installing jaah"',
        *_pkg_branch(["jaah_dnf_wait", "dnf -y install jaah"], [
        "jaah_apt_update",
        "apt-get install -y jaah",
        ], self_hosted=self_hosted),
        "",
        "# The one writer of NODE_TOKEN. The token reaches it on STDIN, so it never appears in a",
        "# process list, and the verb verifies it against the control plane before writing anything.",
        *step("register with the control plane"),
        'echo "==> [jaah] registering this node with the control plane"',
        f'printf \'%s\\n\' "$JAAH_NODE_TOKEN" | jaah auth login {q_id} --with-token',
        "unset JAAH_NODE_TOKEN",
        *step("rotate the credential"),
        *_rotate_lines("jaah", ""),
        "",
        'echo "==> [jaah] done"',
    ]
    return "\n".join(lines)


def render_cloud_init(inputs: BootstrapInputs) -> str:
    """The EC2 user-data for a control-plane-launched cloud node.

    cloud-init runs it once as root on first boot; IMDS-readable, hence IMDSv2.
    """
    _validate(inputs)
    return "#!/bin/bash\n" + _body(inputs) + "\n"


def render_self_hosted_script(inputs: BootstrapInputs) -> str:
    """The script `routes/agent_bootstrap.py` serves to a bare Linux machine that asked for it.

    Identical to the cloud path but for the preamble, which refuses non-root up front rather than
    failing halfway and leaving a box that is neither clean nor a node, and for leaving the user's OS
    unpatched (`_body`): only the `jaah` package is ours to install and update there.
    """
    _validate(inputs)
    preamble = "\n".join(
        [
            "#!/bin/bash",
            "# Fetched from the control plane over this node's own token; see",
            "# app/node_bootstrap.py. It holds that credential in cleartext.",
            'if [ "$(id -u)" -ne 0 ]; then',
            '  echo "this bootstrap must run as root — pipe it into: sudo bash" >&2',
            "  exit 1",
            "fi",
        ]
    )
    return preamble + "\n" + _body(inputs, self_hosted=True) + "\n"


# ── macOS (Apple Silicon): the native installer ──────────────────────────────────────────────────────
# The layout is spelled three times, once per deployable that cannot import another: here (the installer),
# the bundle's `libexec/jaah-common.sh` (the scripts it ships) and
# `apps/jaahd/host_platform/macos.py::_PATHS` (the daemon). A test pins them.
MACOS_ROOT = "/Library/Jaah"
#: `JAAH_HOME` on a macOS node, and the runner user's home.
MACOS_JAAH_HOME = "/var/jaah"
MACOS_DAEMON_USER = "_jaahd"
MACOS_RUNNER_USER = "_jaah"
#: The LaunchDaemons the installer loads, in load order; their plists ship in the bundle's `launchd/`.
#: `ai.jaah.microvmd` is loaded on every node and runs only while the MicroVM marker exists (its plist says why).
MACOS_LABELS = ("ai.jaah.jaahd", "ai.jaah.runner", "ai.jaah.update", "ai.jaah.engine", "ai.jaah.microvmd",
                "ai.jaah.nodelink")
#: The node-managed container engine's socket (`host_platform/macos.py`'s `docker_host`): Colima, run as the
#: runner user by `ai.jaah.engine`, behind a link `jaahd-launch` makes at every boot.
MACOS_DOCKER_HOST = "unix:///var/run/jaah/docker.sock"
MACOS_MIN_MAJOR = 14
MACOS_PLATFORM = "darwin-arm64"
#: The daemon's upgrade request (`JAAH_UPGRADE_MARKER`), in a directory that persists across boots so
#: `ai.jaah.update`'s `QueueDirectories` has something to watch from the moment launchd loads it.
MACOS_UPGRADE_MARKER = f"{MACOS_ROOT}/state/requests/upgrade"
#: The static pool's git mirrors (`JAAH_POOL_MIRRORS`, `apps/jaahd/git_mirror.py`), `2750 _jaahd:_jaah`: the runner
#: user clones from them, and it cannot traverse the daemon's own `state` (0750 in the DAEMON's group, because
#: on macOS a new file takes its directory's group, so a runner-group `state` would leak the daemon's files).
MACOS_POOL_MIRRORS = f"{MACOS_ROOT}/pool-mirrors"

#: Where the CP serves the macOS and Windows bundles (`routes/agent_dist.py`, which imports it).
DIST_PATH = "/api/agent/dist"

def provisioned_jaah_home(os: str) -> str:
    """The `JAAH_HOME` a node of `os` is provisioned with — the seed for its row's `jaah_home` (`routes/nodes/register.py`);
    Ubuntu's is the default."""
    return {"macos": MACOS_JAAH_HOME, "windows": WINDOWS_JAAH_HOME}.get(os, PROVISIONED_JAAH_HOME)


# The script, as one template with `@@NAME@@` slots: bash's own `$`/`{}` make an f-string unreadable. Every
# slot is filled by `render_macos_body` from a validated (or shlex-quoted) value. Bash 3.2 (macOS's
# /bin/bash): no associative arrays, no `${x,,}`, and no `"${array[@]}"` on a possibly-empty array under `set -u`.
_MACOS_TEMPLATE = r"""#!/bin/bash
# The Jaah node installer for macOS (Apple Silicon), fetched from the control plane over this node's own
# token (apps/api/app/node_bootstrap.py). It holds that credential in cleartext: never log or echo it.
#
# PHASES, IN A LOAD-BEARING ORDER:
#   1. preflight — CHANGES NOTHING. Every failure is one `Can't install: <thing> — <fix>` line, and any
#      failure stops here, so a refused Mac is left as it was and the node stays never-seen.
#   2. the tailnet. Fetched from the public console, this run first downloads the bundle back through that
#      same public path, sha256-verified, into a scratch dir (nothing installed yet); then installs
#      Tailscale's open-source system daemon from it when the Mac has none; then joins this run's tailnet
#      (single-use key), moves a Mac off ANOTHER tailnet (asking first, at a terminal), or keeps the one it is
#      on — the control plane's machine surface is reachable only over the tailnet
#   3. the hidden service users: _jaahd (the daemon) and _jaah (runners; non-admin; home @@HOME@@)
#   4. the tailnet prefs this node needs
#   5. the bundle, to @@ROOT@@/versions/<ver> + `current`, and its TCC anchor to @@ROOT@@/bin/jaah-anchor
#   6. the runner CLIs, as @@RUNNER_USER@@ (which ones: the bundle's bin/jaah-converge-clis)
#   7. @@ROOT@@/etc: jaahd.env (the knobs) and node.env (identity; the token only ever lands here)
#   8. the LaunchDaemons ai.jaah.jaahd, ai.jaah.runner, ai.jaah.update, ai.jaah.engine (the container
#      engine: Colima as @@RUNNER_USER@@) and ai.jaah.nodelink — they start at boot, logged in or not
#   9. `jaah auth login`, then a self-check that fails the install if anything is not running
#  10. `jaah auth rotate`: the token this script carries stops working; then the Full Disk Access grant,
#      guided (System Settings, or an MDM profile)
# Uninstall: sudo @@ROOT@@/current/bin/jaah-uninstall
set -euo pipefail
export HOME=/var/root
# The system's dirs only: root runs nothing an admin could have swapped (/usr/local/bin, Homebrew's bin). The
# bundle's tools, the Tailscale CLI among them (`$ts_cli`), are run by absolute path.
export PATH="/usr/bin:/bin:/usr/sbin:/sbin"

# The token, held in a shell variable so no command ever carries it on its argv (`ps`).
JAAH_NODE_TOKEN=@@TOKEN@@
JAAH_CP=@@CP@@
JAAH_PUBLIC_CP=@@PUBLIC_CP@@
JAAH_NODE_ID=@@NODE_ID@@
JAAH_ROOT=@@ROOT@@
JAAH_PLATFORM=@@PLATFORM@@
JAAH_DAEMON_USER=@@DAEMON_USER@@
JAAH_RUNNER_USER=@@RUNNER_USER@@
JAAH_LABELS=@@LABELS@@

# Everything below runs only once the WHOLE script has arrived: bash executes a piped script line by line,
# so a download cut mid-way would otherwise run the phases it did receive.
main() {

# ── 1. preflight: changes nothing ─────────────────────────────────────────────────────────────────────
echo "==> [jaah] checking this Mac"
problems=''
cant() { problems="${problems}Can't install: $1 — $2"$'\n'; }
refuse_if_any() {
  if [ -n "$problems" ]; then
    printf '%s' "$problems" >&2
    if [ -n "${joined_tailnet:-}" ]; then
      echo "This Mac joined the tailnet; nothing else was changed. Fix the above and run the same command again." >&2
    elif [ -n "${ts_installed:-}" ]; then
      echo "This Mac got Tailscale's system daemon (tailscaled); nothing else was changed. Fix the above and run the same command again." >&2
    else
      echo "Nothing was changed on this Mac. Fix the above and run the same command again." >&2
    fi
    exit 1
  fi
}

if [ "$(uname -s)" != Darwin ]; then
  cant "macOS (this machine runs $(uname -s))" "this command is for a Mac — create a node for this machine's OS in the console"
  refuse_if_any
fi
if [ "$(id -u)" -ne 0 ]; then
  cant "administrator rights" "run the command exactly as the console shows it, with sudo, from an admin account"
fi
# `uname -m` reads x86_64 under Rosetta on an Apple Silicon Mac; the hardware flag does not.
if [ "$(uname -m)" != arm64 ] && [ "$(sysctl -n hw.optional.arm64 2>/dev/null || echo 0)" != 1 ]; then
  cant "an Apple Silicon Mac (this one is $(uname -m))" "Intel Macs are not supported — use a Mac with an M-series chip"
fi
os_version="$(sw_vers -productVersion 2>/dev/null || echo unknown)"
os_major="${os_version%%.*}"
case "$os_major" in ''|*[!0-9]*) os_major=0 ;; esac
if [ "$os_major" -lt @@MIN_MAJOR@@ ]; then
  cant "macOS @@MIN_MAJOR@@ or later (this Mac runs $os_version)" "update it in System Settings > General > Software Update"
fi
# The node must be on the tailnet before anyone logs in, so tailscaled has to be a system LaunchDaemon.
# The App Store and Standalone apps run only inside a login session: after a reboot to the login screen, a
# node on one of them never comes back. A loaded open-source tailscaled (a plist naming it, loaded in the
# system domain) is kept as it is.
ts_join=''
ts_daemon=''
ts_cli=''
for plist in /Library/LaunchDaemons/*.plist; do
  [ -f "$plist" ] || continue
  if grep -q tailscaled "$plist" 2>/dev/null; then
    ts_label="$(plutil -extract Label raw -o - "$plist" 2>/dev/null || true)"
    if [ -n "$ts_label" ] && launchctl print "system/$ts_label" >/dev/null 2>&1; then
      ts_daemon="$plist"
      break
    fi
  fi
done
@@TAILSCALE_PREFLIGHT@@
# A DIFFERENT NODE IS A REFUSAL, NEVER A REPOINT (the Ubuntu bootstrap's rule and reasons).
existing_node_id=''
if [ -f "$JAAH_ROOT/etc/node.env" ]; then
  existing_node_id="$(awk '/^NODE_ID=/{sub(/^NODE_ID=/, ""); print; exit}' "$JAAH_ROOT/etc/node.env" 2>/dev/null || true)"
fi
if [ -n "$existing_node_id" ] && [ "$existing_node_id" != "$JAAH_NODE_ID" ]; then
  cant "a Mac that is not already a Jaah node (this one is node $existing_node_id)" "remove it first: sudo $JAAH_ROOT/current/bin/jaah-uninstall"
fi
# An account that already exists is reused, so it must not be an administrator (a runner with sudo).
for account in "$JAAH_DAEMON_USER" "$JAAH_RUNNER_USER"; do
  if dscl . -read "/Users/$account" >/dev/null 2>&1 \
     && dseditgroup -o checkmember -m "$account" admin >/dev/null 2>&1; then
    cant "a non-admin $account account (this Mac's $account is an administrator)" "remove it from the admin group, or delete it, then run the command again"
  fi
done
# ALREADY THIS NODE: a re-issued token, or a run that died after the services were installed (decided below).
# Every service must be installed AND loaded; anything less is a half-finished run, which reinstalls.
already=''
services_loaded=1
for label in $JAAH_LABELS; do
  if [ ! -f "/Library/LaunchDaemons/$label.plist" ] || ! launchctl print "system/$label" >/dev/null 2>&1; then
    services_loaded=0
  fi
done
if [ "$existing_node_id" = "$JAAH_NODE_ID" ] && [ -x "$JAAH_ROOT/current/bin/jaah" ] \
   && [ "$services_loaded" = 1 ]; then
  already=1
fi
refuse_if_any

# A dist object (`$1` its URL on the control plane) to a file: the route answers a 307 to a short-lived
# presigned URL, followed in two steps rather than `-L`, so the token goes to the control plane only; the
# URL is a bearer credential too, so it goes by --config. The header reaches curl as a --config file on
# STDIN, never as an argument (`ps` would show the token).
dist_get() {
  local location
  location="$(
    printf 'header = "X-Agent-Token: %s"\n' "$JAAH_NODE_TOKEN" \
      | curl -K - --proto =https -sS --max-time 30 -o /dev/null -w '%{redirect_url}' "$1" 2>/dev/null || true
  )"
  [ -n "$location" ] || return 1
  printf 'url = "%s"\n' "$location" | curl -K - --proto =https -fsS --max-time "$2" -o "$3"
}
version=''
archive=''
expected=''
work=''

# ── 2. the tailnet ────────────────────────────────────────────────────────────────────────────────────
@@TAILNET@@

# ── 9. registration + self-check, defined here because a credential rotation runs only this ──────────
register_and_check() {
  echo "==> [jaah] registering this node with the control plane"
  # The one writer of NODE_TOKEN. The token reaches it on STDIN, and it verifies before it writes.
  printf '%s\n' "$JAAH_NODE_TOKEN" | "$JAAH_ROOT/current/bin/jaah" auth login "$JAAH_NODE_ID" --with-token
  unset JAAH_NODE_TOKEN

  # The runner keeper's session is this node's NAME, as the daemon resolves it (NODE_NAME, else NODE_ID).
  # `_jaah` cannot read node.env, so root copies the (non-secret) name into jaahd.env and restarts it.
  session="$(awk '/^NODE_NAME=/{sub(/^NODE_NAME=/, ""); n=$0} /^NODE_ID=/{sub(/^NODE_ID=/, ""); i=$0} END {print (n != "" ? n : i)}' "$JAAH_ROOT/etc/node.env")"
  awk -v s="$session" '/^JAAH_RUNNER_SESSION=/ {next} {print} END {print "JAAH_RUNNER_SESSION=" s}' \
    "$JAAH_ROOT/etc/jaahd.env" > "$JAAH_ROOT/etc/jaahd.env.new"
  chmod 0644 "$JAAH_ROOT/etc/jaahd.env.new"
  mv -f "$JAAH_ROOT/etc/jaahd.env.new" "$JAAH_ROOT/etc/jaahd.env"
  launchctl kickstart -k "system/ai.jaah.runner" || true

  # A socket left by the token-less daemon phase 8 started must not pass for the new one's.
  rm -f /var/run/jaah/creds/sock
  launchctl kickstart -k "system/ai.jaah.jaahd"

  echo "==> [jaah] checking the node is running"
  failed=''
  up=0
  for _ in $(seq 1 60); do
    pid="$(launchctl print system/ai.jaah.jaahd 2>/dev/null | awk '$1 == "pid" {print $3}' || true)"
    if [ -n "$pid" ] && [ -S /var/run/jaah/creds/sock ]; then
      # Up = the same pid still running 5s later: a crash loop is not "running".
      sleep 5
      again="$(launchctl print system/ai.jaah.jaahd 2>/dev/null | awk '$1 == "pid" {print $3}' || true)"
      if [ "$again" = "$pid" ]; then up=1; break; fi
    fi
    sleep 1
  done
  if [ "$up" != 1 ]; then
    failed="${failed}Self-check failed: the jaahd daemon and its socket /var/run/jaah/creds/sock — see $JAAH_ROOT/log/jaahd.log"$'\n'
  fi
  if ! "$JAAH_ROOT/current/bin/tmux" -V >/dev/null 2>&1; then
    failed="${failed}Self-check failed: the bundled tmux ($JAAH_ROOT/current/bin/tmux) does not run on this Mac"$'\n'
  fi
  # git and the runner CLIs as the runner user, through the runners' own environment (the bundled git, never
  # /usr/bin/git: that is the Xcode tools' stub, which opens an install dialog nobody is there to answer).
  # A bundle older than the converge has none to ask.
  if [ -x "$JAAH_ROOT/current/bin/jaah-converge-clis" ] && ! "$JAAH_ROOT/current/bin/jaah-converge-clis" --check; then
    failed="${failed}Self-check failed: the runner CLIs above, as $JAAH_RUNNER_USER — retry with: sudo $JAAH_ROOT/current/bin/jaah-converge-clis"$'\n'
  fi

  # The runners' tmux server: ai.jaah.runner up on one pid that runs as the runner user, and the fleet
  # socket it seeds present and owned by that user — a server owned by anyone else forks every runner pane
  # as the wrong account. Only the FILE is looked at, never `tmux -S` from here: root is exempt from the
  # server's access list, so a root probe would pass where the daemon is refused.
  echo "==> [jaah] checking the runner tmux server runs as $JAAH_RUNNER_USER"
  runner_uid="$(id -u "$JAAH_RUNNER_USER" 2>/dev/null || true)"
  runner_socket="/var/run/jaah/tmux/tmux-$runner_uid/default"
  # Longer than the launcher's own 300s fleet-harness bound, which runs before the server binds its socket.
  up=0
  for _ in $(seq 1 360); do
    pid="$(launchctl print system/ai.jaah.runner 2>/dev/null | awk '$1 == "pid" {print $3}' || true)"
    if [ -n "$pid" ] && [ -S "$runner_socket" ] \
       && [ "$(ps -o user= -p "$pid" 2>/dev/null | tr -d '[:space:]')" = "$JAAH_RUNNER_USER" ] \
       && [ "$(stat -f %Su "$runner_socket" 2>/dev/null || true)" = "$JAAH_RUNNER_USER" ]; then
      sleep 5
      again="$(launchctl print system/ai.jaah.runner 2>/dev/null | awk '$1 == "pid" {print $3}' || true)"
      if [ "$again" = "$pid" ]; then up=1; break; fi
    fi
    sleep 1
  done
  if [ "$up" != 1 ]; then
    failed="${failed}Self-check failed: the runner tmux server (ai.jaah.runner as $JAAH_RUNNER_USER, socket $runner_socket) — see $JAAH_ROOT/log/runner.log"$'\n'
  fi

  # The container engine, as the runners reach it: `docker info` as the runner user over the node's socket.
  # Its first start downloads the VM image, hence the long bound.
  echo "==> [jaah] checking the container engine (ai.jaah.engine) answers $JAAH_RUNNER_USER — the first start downloads its VM image"
  up=0
  for _ in $(seq 1 900); do
    # Bounded: a socket that accepts while dockerd behind it is stalled must not hang the install.
    if sudo -u "$JAAH_RUNNER_USER" env HOME=@@HOME@@ DOCKER_HOST=@@DOCKER_HOST@@ \
         "$JAAH_ROOT/current/bin/timeout" 10 "$JAAH_ROOT/current/bin/docker" info >/dev/null 2>&1; then
      up=1
      break
    fi
    sleep 1
  done
  if [ "$up" != 1 ]; then
    failed="${failed}Self-check failed: the container engine (ai.jaah.engine, docker info as $JAAH_RUNNER_USER on @@DOCKER_HOST@@) — see $JAAH_ROOT/log/engine.log"$'\n'
  fi
  if [ -n "$failed" ]; then
    printf '%s' "$failed" >&2
    exit 1
  fi
  echo "==> [jaah] done — this Mac shows online on the Nodes page within a minute"
}

# THE TCC ANCHOR, out of `current` to the one path a Full Disk Access grant names and no update changes,
# atomically (as `jaah_install_anchor` does). root:<runner group> 0750: only root (ai.jaah.jaahd,
# ai.jaah.nodelink) and the runner account (ai.jaah.runner) can execute it, so it needs the runner account.
install_anchor() {
  [ -f "$JAAH_ROOT/current/libexec/jaah-anchor" ] || return 0
  runner_gid="$(id -g "$JAAH_RUNNER_USER")"
  install -d -o root -g wheel -m 0755 "$JAAH_ROOT/bin"
  install -m 0750 -o root -g "$runner_gid" "$JAAH_ROOT/current/libexec/jaah-anchor" "$JAAH_ROOT/bin/.jaah-anchor.new"
  mv -f "$JAAH_ROOT/bin/.jaah-anchor.new" "$JAAH_ROOT/bin/jaah-anchor"
}

# FULL DISK ACCESS, the one grant no installer can make: NodeLinks read through it. Guided, never checked, and
# the Settings pane opened for whoever is at the console — best effort: nobody may be logged in.
full_disk_access_guide() {
  echo "==> [jaah] NodeLinks need Full Disk Access for $JAAH_ROOT/bin/jaah-anchor:"
  echo "==> [jaah]   System Settings → Privacy & Security → Full Disk Access → + → ⌘⇧G → $JAAH_ROOT/bin/jaah-anchor → on"
  echo "==> [jaah]   MDM-managed Mac: install the profile from the console's Add node dialog instead"
  console_uid="$(stat -f %u /dev/console 2>/dev/null || true)"
  [ -n "$console_uid" ] && [ "$console_uid" != 0 ] \
    && launchctl asuser "$console_uid" open 'x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles' >/dev/null 2>&1 \
    || true
}

# ── 10. the credential rotation: the token this script carries stops working once the node is up ─────────
# Last, so a run that failed earlier can be re-run as is. A failed rotation is a warning: the node works.
rotate_credential() {
  echo "==> [jaah] rotating the credential, so the one in this script stops working"
  if "$JAAH_ROOT/current/bin/jaah" auth rotate; then
    # The daemon reads node.env only at start. A failed restart only warns: the script's token is already dead.
    launchctl kickstart -k "system/ai.jaah.jaahd" \
      || echo "==> [jaah] warning: restart ai.jaah.jaahd to use the new credential" >&2
  else
    echo "==> [jaah] warning: the credential did not rotate - retry with: sudo $JAAH_ROOT/current/bin/jaah auth rotate" >&2
  fi
}

# ALREADY THIS NODE (preflight decided): only the credential and the check — never a reinstall under live runners.
if [ -n "$already" ]; then
  echo "==> [jaah] already installed; writing the new credential only"
  install_anchor
  register_and_check
  rotate_credential
  # A release from before the anchor has none to grant.
  if [ -f "$JAAH_ROOT/bin/jaah-anchor" ]; then full_disk_access_guide; fi
  exit 0
fi
@@RELEASE@@

# ── 3. the service users ──────────────────────────────────────────────────────────────────────────────
echo "==> [jaah] creating the service users $JAAH_DAEMON_USER and $JAAH_RUNNER_USER"
# The lowest id in 400–499 that no user AND no group holds: below 500 is what keeps them off the login window.
free_id() {
  { dscl . -list /Users UniqueID; dscl . -list /Groups PrimaryGroupID; } \
    | awk '{used[$2] = 1} END {for (i = 400; i < 500; i++) if (!(i in used)) {print i; exit 0}; exit 1}'
}
ensure_group() {
  local gid
  dscl . -read "/Groups/$1" >/dev/null 2>&1 && return 0
  gid="$(free_id)"
  dscl . -create "/Groups/$1"
  dscl . -create "/Groups/$1" PrimaryGroupID "$gid"
  dscl . -create "/Groups/$1" RealName "$2"
  dscl . -create "/Groups/$1" Password '*'
}
ensure_user() {
  local uid gid
  dscl . -read "/Users/$1" >/dev/null 2>&1 && return 0
  uid="$(free_id)"
  gid="$(dscl . -read "/Groups/$1" PrimaryGroupID | awk '{print $2}')"
  dscl . -create "/Users/$1"
  dscl . -create "/Users/$1" UniqueID "$uid"
  dscl . -create "/Users/$1" PrimaryGroupID "$gid"
  dscl . -create "/Users/$1" RealName "$2"
  dscl . -create "/Users/$1" NFSHomeDirectory "$3"
  dscl . -create "/Users/$1" UserShell "$4"
  dscl . -create "/Users/$1" Password '*'
  dscl . -create "/Users/$1" IsHidden 1
}
ensure_group "$JAAH_DAEMON_USER" "Jaah node daemon"
ensure_group "$JAAH_RUNNER_USER" "Jaah runners"
ensure_user "$JAAH_DAEMON_USER" "Jaah node daemon" "$JAAH_ROOT/state" /usr/bin/false
# A real shell: tmux falls back to the passwd shell, and every runner pane is one.
ensure_user "$JAAH_RUNNER_USER" "Jaah runners" @@HOME@@ /bin/bash
# The daemon joins the runners' group — the sharing mechanism for the tmux socket dir and the spools.
dseditgroup -o checkmember -m "$JAAH_DAEMON_USER" "$JAAH_RUNNER_USER" >/dev/null 2>&1 \
  || dseditgroup -o edit -a "$JAAH_DAEMON_USER" -t user "$JAAH_RUNNER_USER"
install -d -o "$JAAH_RUNNER_USER" -g "$JAAH_RUNNER_USER" -m 2770 @@HOME@@
install -d -o root -g wheel -m 0755 "$JAAH_ROOT"
install -d -o "$JAAH_DAEMON_USER" -g "$JAAH_DAEMON_USER" -m 0750 "$JAAH_ROOT/state"
# The upgrade drop-box: persistent (unlike /var/run), so ai.jaah.update can watch it from boot.
install -d -o "$JAAH_DAEMON_USER" -g "$JAAH_DAEMON_USER" -m 0750 "$JAAH_ROOT/state/requests"
# The pool mirrors the runner user clones from, outside `state` (MACOS_POOL_MIRRORS): readable, never writable.
install -d -o "$JAAH_DAEMON_USER" -g "$JAAH_RUNNER_USER" -m 2750 "@@POOL_MIRRORS@@"

# ── 4. the tailnet prefs ──────────────────────────────────────────────────────────────────────────────
# Joined by now (phase 2). Converge the one pref the node needs: never a second `tailscale up`, which can
# re-register the device. Routes are what reach the control plane's internal ALB. Unlike Ubuntu, no `--ssh`:
# this is often someone's own Mac, with a console in reach.
echo "==> [jaah] converging the tailnet prefs this node needs"
"$ts_cli" set --accept-routes || echo "==> [jaah] warning: could not set --accept-routes; continuing" >&2

# ── 5. the bundle ─────────────────────────────────────────────────────────────────────────────────────
@@DOWNLOAD@@
install -d -o root -g wheel -m 0755 "$JAAH_ROOT/versions"
rm -rf -- "$JAAH_ROOT/versions/$version"
mv "$work/x/versions/$version" "$JAAH_ROOT/versions/$version"
chown -R root:wheel "$JAAH_ROOT/versions/$version"
chmod -R u-s,g-s,go-w "$JAAH_ROOT/versions/$version"
ln -sfh "versions/$version" "$JAAH_ROOT/current.new"
mv -fh "$JAAH_ROOT/current.new" "$JAAH_ROOT/current"
# THE TCC ANCHOR, before any LaunchDaemon runs through it (the service users exist since phase 3).
install_anchor

# ── 6. the runner CLIs ────────────────────────────────────────────────────────────────────────────────
# Before the LaunchDaemons, so the runner keeper finds its CLIs installed rather than installing one
# itself. Bounded, and not fatal here: the self-check (phase 9) names whatever is still missing.
echo "==> [jaah] installing the runner CLIs for $JAAH_RUNNER_USER"
"$JAAH_ROOT/current/bin/jaah-converge-clis" || echo "==> [jaah] warning: the runner CLIs did not all install; the self-check below says which" >&2

# ── 7. config ─────────────────────────────────────────────────────────────────────────────────────────
echo "==> [jaah] writing $JAAH_ROOT/etc"
install -d -o root -g wheel -m 0755 "$JAAH_ROOT/etc"
cat > "$JAAH_ROOT/etc/jaahd.env" <<'EOF'
@@ENV_LINES@@
EOF
chmod 0644 "$JAAH_ROOT/etc/jaahd.env"
chown root:wheel "$JAAH_ROOT/etc/jaahd.env"
# Identity now, the credential at step 9. Created 0640 root:_jaahd before any write.
install -m 0640 -o root -g "$JAAH_DAEMON_USER" /dev/null "$JAAH_ROOT/etc/node.env"
printf 'NODE_ID=%s\n' "$JAAH_NODE_ID" >> "$JAAH_ROOT/etc/node.env"

# ── 8. the LaunchDaemons ──────────────────────────────────────────────────────────────────────────────
echo "==> [jaah] installing the LaunchDaemons"
install -d -o root -g wheel -m 0755 "$JAAH_ROOT/log"
# 0640: daemon output names tasks, accounts and repos, and the runner user runs untrusted agent code. Group: the
# daemon's own, which ships these lines (`apps/jaahd/node_logs.py`) — never the runner's.
for log in jaahd update nodelink; do
  install -m 0640 -o root -g "$JAAH_DAEMON_USER" /dev/null "$JAAH_ROOT/log/$log.log"
done
for log in runner engine; do
  install -m 0640 -o "$JAAH_RUNNER_USER" -g "$JAAH_RUNNER_USER" /dev/null "$JAAH_ROOT/log/$log.log"
done
install -m 0640 -o "$JAAH_DAEMON_USER" -g "$JAAH_DAEMON_USER" /dev/null "$JAAH_ROOT/log/microvmd.log"
for label in $JAAH_LABELS; do
  install -m 0644 -o root -g wheel "$JAAH_ROOT/current/launchd/$label.plist" "/Library/LaunchDaemons/$label.plist"
  launchctl bootout "system/$label" 2>/dev/null || true
  launchctl enable "system/$label"
  # A bootout still settling makes bootstrap fail with an I/O error; it clears within seconds.
  loaded=0
  for _ in 1 2 3 4 5; do
    if launchctl bootstrap system "/Library/LaunchDaemons/$label.plist" 2>/dev/null; then loaded=1; break; fi
    sleep 1
  done
  [ "$loaded" = 1 ] || { echo "==> [jaah] could not load $label — see: sudo launchctl print system/$label" >&2; exit 1; }
done

# ── 9. ────────────────────────────────────────────────────────────────────────────────────────────────
register_and_check
rotate_credential
full_disk_access_guide
}

# Bare: under `set -u`, bash 3.2 (macOS /bin/bash) calls an empty "$@" unbound.
main
"""

# ── the tailnet-only arm (no public origin: cloud-init, tests) — the Mac must already run tailscaled ───────

#: Preflight's Tailscale checks without a public origin: nothing can be downloaded before the tailnet, so the
#: Mac must bring its own tailscaled, loaded as a system daemon.
_MACOS_TS_PREFLIGHT_TAILNET = r"""# This arm's premise is the Mac's OWN Tailscale (nothing can be downloaded first), so its CLI runs.
for ts_candidate in /usr/local/bin/tailscale /opt/homebrew/bin/tailscale; do
  if [ -x "$ts_candidate" ]; then ts_cli="$ts_candidate"; break; fi
done
if [ -z "$ts_cli" ]; then
  cant "the tailscale command" "install the open-source tailscaled (brew install tailscale), not the App Store app: https://tailscale.com/kb/1065/macos-variants"
elif [ -z "$ts_daemon" ]; then
  cant "the tailscaled system daemon" "the Tailscale app only runs while someone is logged in — install the open-source tailscaled (brew install tailscale) and run: sudo tailscaled install-system-daemon"
elif ! "$ts_cli" status >/dev/null 2>&1; then
@@TAILNET_ABSENT@@
fi"""

#: What to install without a public origin: the release the control plane names over the tailnet, and only
#: the small checksum (it proves the bundle is published without downloading it yet).
_MACOS_RELEASE_TAILNET = r"""
# What to install: the release this control plane runs, and that release's macOS bundle must be published.
cp_get() {
  printf 'header = "X-Agent-Token: %s"\n' "$JAAH_NODE_TOKEN" \
    | curl -K - -sS --max-time 30 -w '\n%{http_code}' "$JAAH_CP$1" 2>/dev/null || true
}
release="$(cp_get /api/agent/release-desired)"
release_code="${release##*$'\n'}"
release_body="${release%$'\n'*}"
if [ "$release_code" != 200 ]; then
  cant "a reachable control plane ($JAAH_CP answered ${release_code:-nothing})" "check this Mac's tailnet reaches the control plane (a Mac logged in to another tailnet must leave it first: sudo tailscale logout), then run the command again"
else
  version="$(printf '%s' "$release_body" | sed -n 's/.*"version"[[:space:]]*:[[:space:]]*"\([^"]*\)".*/\1/p')"
  case "$version" in
    [0-9]*) case "$version" in *[!A-Za-z0-9.+-]*) version='' ;; esac ;;
    *) version='' ;;
  esac
  [ -n "$version" ] || cant "a published release" "the control plane has no node release to install yet — try again after its next deploy"
fi
if [ -n "$version" ]; then
  archive="jaah-$version-$JAAH_PLATFORM.tar.gz"
  # The tailnet join may have made it already, for its key file.
  [ -n "$work" ] || { work="$(mktemp -d /tmp/jaah-install.XXXXXX)"; trap 'rm -rf -- "$work"' EXIT; }
  if dist_get "$JAAH_CP@@DIST@@/$JAAH_PLATFORM/$archive.sha256" 30 "$work/$archive.sha256" 2>/dev/null; then
    expected="$(awk 'NR == 1 {print $1}' "$work/$archive.sha256")"
  fi
  case "$expected" in
    *[!0-9a-f]*) expected='' ;;
  esac
  if [ "${#expected}" -ne 64 ]; then
    cant "the macOS bundle for release $version" "the control plane has not published $archive yet — try again in a few minutes"
  fi
fi
refuse_if_any"""

#: The download itself, over the tailnet, once every refusal is decided.
_MACOS_DOWNLOAD_TAILNET = r"""echo "==> [jaah] downloading $archive"
dist_get "$JAAH_CP@@DIST@@/$JAAH_PLATFORM/$archive" 900 "$work/$archive"
actual="$(shasum -a 256 "$work/$archive" | awk '{print $1}')"
if [ "$actual" != "$expected" ]; then
  echo "==> [jaah] $archive does not match its published sha256 — refusing it" >&2
  exit 1
fi
mkdir "$work/x"
tar -xzf "$work/$archive" -C "$work/x"
[ -x "$work/x/versions/$version/runtime/python/bin/python3" ] || { echo "==> [jaah] $archive is not a node bundle" >&2; exit 1; }
[ -x "$work/x/versions/$version/libexec/jaah-anchor" ] || { echo "==> [jaah] $archive has no libexec/jaah-anchor — tell the control plane's operator" >&2; exit 1; }"""

# ── the public arm (fetched from the console): bundle first, then tailscaled from it, then the tailnet ─────

#: Preflight's Tailscale check with a public origin: a Mac with no open-source daemon gets the bundle's, so
#: the only refusal is the Tailscale APP, whose own tunnel would fight the daemon this installs.
_MACOS_TS_PREFLIGHT_PUBLIC = r"""if [ -z "$ts_daemon" ] && [ -d /Applications/Tailscale.app ]; then
  cant "a Mac without the Tailscale app (it runs only while someone is logged in, and two Tailscales on one Mac fight over its network)" "quit the Tailscale app and move it to the Trash — this installer sets up Tailscale's system daemon in its place — then run the command again"
fi
# Tailscale's daemon installs itself to /usr/local/bin and runs from there as root, so that directory (and the
# one above it) must be root's alone: anyone else who can write it could swap the binary.
if [ -z "$ts_daemon" ]; then
  for ts_dir in /usr/local /usr/local/bin; do
    if [ -e "$ts_dir" ] || [ -L "$ts_dir" ]; then
      if [ -z "$(find "$ts_dir" -maxdepth 0 -type d -user root ! -perm -020 ! -perm -002 2>/dev/null)" ]; then
        cant "a $ts_dir only root can write (Tailscale's daemon runs from it as root)" "sudo chown root:wheel $ts_dir && sudo chmod go-w $ts_dir — then run the command again"
      fi
    fi
  done
fi"""

#: Phase 2 with a public origin, up to the tailnet guard `_macos_tailnet_guard` renders.
_MACOS_FETCH_PUBLIC = r"""
# THE BUNDLE, FROM THE PUBLIC CONSOLE. Off the tailnet, the control plane's machine routes are out of reach;
# its one public path serves this node's bundle (`?dist=`) over the same token. The release is the one this
# control plane runs, rendered into this script. Into a scratch dir only: nothing is installed yet. Skipped
# for an installed node that keeps its own tailscaled: it needs nothing from the bundle.
version=@@VERSION@@
if [ -z "$already" ] || [ -z "$ts_daemon" ]; then
  if [ -z "$version" ]; then
    cant "a published release" "the control plane has no node release to install yet — try again after its next deploy"
  else
    archive="jaah-$version-$JAAH_PLATFORM.tar.gz"
    work="$(mktemp -d /tmp/jaah-install.XXXXXX)"
    trap 'rm -rf -- "$work"' EXIT
    echo "==> [jaah] downloading $archive"
    if dist_get "$JAAH_PUBLIC_CP@@BOOTSTRAP@@?dist=$archive.sha256" 30 "$work/$archive.sha256" 2>/dev/null; then
      expected="$(awk 'NR == 1 {print $1}' "$work/$archive.sha256")"
    fi
    case "$expected" in
      *[!0-9a-f]*) expected='' ;;
    esac
    if [ "${#expected}" -ne 64 ]; then
      cant "the macOS bundle for release $version" "the control plane has not published $archive yet — try again in a few minutes"
    elif ! dist_get "$JAAH_PUBLIC_CP@@BOOTSTRAP@@?dist=$archive" 900 "$work/$archive"; then
      cant "a download of $archive" "check this Mac reaches $JAAH_PUBLIC_CP, then run the command again"
    elif [ "$(shasum -a 256 "$work/$archive" | awk '{print $1}')" != "$expected" ]; then
      cant "an intact $archive (it does not match its published sha256)" "run the command again"
    elif ! { mkdir "$work/x" && tar -xzf "$work/$archive" -C "$work/x"; } \
         || [ ! -x "$work/x/versions/$version/runtime/python/bin/python3" ] \
         || [ ! -x "$work/x/versions/$version/libexec/jaah-anchor" ]; then
      cant "a node bundle in $archive" "the control plane published a broken bundle — tell its operator"
    fi
    rm -f -- "$work/$archive"
  fi
  refuse_if_any
  ts_cli="$work/x/versions/$version/bin/tailscale"
fi
# The Tailscale CLI this run calls, by absolute path: the bundle's — fetched above, else the installed node's.
[ -n "$ts_cli" ] || ts_cli="$JAAH_ROOT/current/bin/tailscale"

# TAILSCALE'S SYSTEM DAEMON, from the bundle, where preflight found none: `install-system-daemon` copies
# itself to /usr/local/bin/tailscaled, writes /Library/LaunchDaemons/com.tailscale.tailscaled.plist and loads
# it, so it runs with nobody logged in. Preflight refused a /usr/local/bin anyone but root can write. Its CLI
# goes beside it, a root-owned copy, for whoever runs `tailscale` on this Mac; this run and the node call the
# bundle's own by absolute path. The open-source daemon leaves DNS alone, and nothing here needs MagicDNS: the
# control plane's names resolve in public DNS, and the tailnet only carries the route.
if [ -z "$ts_daemon" ]; then
  echo "==> [jaah] installing Tailscale's system daemon (tailscaled)"
  [ -d /usr/local ] || install -d -o root -g wheel -m 0755 /usr/local
  [ -d /usr/local/bin ] || install -d -o root -g wheel -m 0755 /usr/local/bin
  if ! "$work/x/versions/$version/bin/tailscaled" install-system-daemon; then
    cant "Tailscale's system daemon (tailscaled install-system-daemon failed, above)" "run the command again; if it fails again, see: sudo launchctl print system/com.tailscale.tailscaled"
    refuse_if_any
  fi
  ts_installed=1
  install -m 0755 -o root -g wheel "$ts_cli" /usr/local/bin/tailscale
  for _ in $(seq 1 30); do
    [ -S /var/run/tailscaled.socket ] && break
    sleep 1
  done
fi"""


_RELEASE_RE = re.compile(r"[0-9][A-Za-z0-9.+-]*")


def _macos_release() -> str:
    """The release a Mac installs when its installer cannot ask: what `GET /api/agent/release-desired`
    (`routes/agent/releases.py`) answers — `app.version.stamped_release` — held to the version grammar the shell side holds it to
    (`jaah_valid_version`), since it becomes a path and a filename. Empty = refused in preflight."""
    release = app_version.stamped_release()
    if release is None or not _RELEASE_RE.fullmatch(release):
        return ""
    return release


def _macos_tailnet_slots(inputs: BootstrapInputs) -> dict[str, str]:
    """The macOS body's tailnet slots: preflight's Tailscale checks, phase 2, the release check and the download.

    WITH a public origin (`public_base_url`, the console's fetch): the bundle comes first, over the public path;
    tailscaled from it when the Mac has none; then the guard — already ours (re-joined with `--force-reauth` when
    an operator re-enrolled it: `_reenrol_lines`), ANOTHER tailnet (moved, asking at a terminal:
    `_tailnet_switch_lines`), or the join with this run's key; with no key a Mac off the tailnet is refused.
    WITHOUT one: the Mac must bring a loaded tailscaled, a keyed one joins once every LOCAL check has
    passed, and the release, checksum and bundle come over the tailnet. The key rides the argv for the reason
    the Ubuntu join gives (`_body`): single-use, and spent by the time `tailscale up` returns."""
    q_name = shlex.quote(inputs.node_name)
    # `reset`: a logged-out tailscaled keeps the last tailnet's prefs (its exit node, routes), and `up` refuses
    # to drop settings it was not told to keep — so a Mac joins from ANY prior state, not only a fresh one.
    join = tailnet_join_command(
        q_name, login_server=inputs.tailscale_login_server is not None, reset=True, ssh=False, cli='"$ts_cli"',
        key_file=True,
    )
    keyed_join = [
        '  echo "==> [jaah] joining the tailnet"',
        *(
            [*_tailnet_key_lines(inputs, "  "), *_macos_key_file_lines("  ")]
            if inputs.public_base_url is None
            else []
        ),
        f"  if ! {join}; then",
        '    rm -f -- "$work/ts.key"',
        '    cant "a tailnet join (tailscale up refused, above)" "fix what it says, then run this command'
        ' again"',
        "    refuse_if_any",
        "  fi",
        '  rm -f -- "$work/ts.key"',
        "  joined_tailnet=1",
    ]
    # The route to the control plane arrives with the netmap, a moment after `tailscale up` returns. Any HTTP
    # answer will do: it proves the route, and what follows judges the rest.
    route_wait = [
        'if [ -n "${joined_tailnet:-}" ]; then',
        "  for _ in $(seq 1 30); do",
        '    curl -s -o /dev/null --max-time 5 "$JAAH_CP/api/health" && break',
        "    sleep 2",
        "  done",
        "fi",
    ]
    if inputs.public_base_url is None:
        absent = (
            '  cant "a connected tailnet" "run: sudo tailscale up — then run this command again"'
            if inputs.tailscale_auth_key is None
            else "  ts_join=1"
        )
        return {
            "TAILSCALE_PREFLIGHT": _MACOS_TS_PREFLIGHT_TAILNET.replace("@@TAILNET_ABSENT@@", absent),
            "TAILNET": "\n".join([
                "# Preflight found this Mac on the tailnet, or marked it for the join below.",
                *(
                    [
                        "# THE TAILNET JOIN, with the single-use key the control plane minted for this node alone.",
                        'if [ -n "$ts_join" ]; then',
                        *keyed_join,
                        "fi",
                        *route_wait,
                    ]
                    if inputs.tailscale_auth_key is not None
                    else []
                ),
            ]),
            "RELEASE": _MACOS_RELEASE_TAILNET.replace("@@DIST@@", DIST_PATH),
            "DOWNLOAD": _MACOS_DOWNLOAD_TAILNET.replace("@@DIST@@", DIST_PATH),
        }
    on_ours = '"$ts_cli" status >/dev/null 2>&1' + (
        ' && [ -z "$jaah_other_tailnet" ]' if inputs.tailscale_login_server is not None else ""
    )
    not_on = (
        keyed_join
        if inputs.tailscale_auth_key is not None
        else [
            '  cant "a connected tailnet" "run: sudo tailscale up — then run this command again"',
            "  refuse_if_any",
        ]
    )
    switch = _tailnet_switch_lines(inputs, q_name, ask=True, macos=True)
    guard = [
        "# THE TAILNET: already ours, another one (moved), or joined with this run's single-use key — assigned once",
        "# here for both arms that spend it, unset whichever runs.",
        *(
            [*_tailnet_key_lines(inputs, ""), *_macos_key_file_lines("")]
            if inputs.tailscale_auth_key is not None
            else []
        ),
        *_other_tailnet_probe_lines(inputs, macos=True),
        f"if {on_ours}; then",
        '  echo "==> [jaah] already on the tailnet"',
        *_reenrol_lines(inputs, q_name, macos=True),
        *switch,
        *(["  joined_tailnet=1"] if switch else []),
        "else",
        *not_on,
        "fi",
        # Unspent (already on this tailnet): gone now, not at exit.
        *(['rm -f -- "$work/ts.key"'] if inputs.tailscale_auth_key is not None else []),
        # WHATEVER THE ARM, the control plane must answer before anything is installed: a device joined but its
        # subnet route not approved, or a Mac on someone else's tailnet that no login server names, reaches
        # nothing. Any HTTP answer will do (it proves the route; registration judges the rest). Bounded, since
        # the route arrives with the netmap a moment after `tailscale up` returns.
        "cp_up=''",
        "for _ in $(seq 1 30); do",
        '  if curl -s -o /dev/null --max-time 5 "$JAAH_CP/api/health"; then cp_up=1; break; fi',
        "  sleep 2",
        "done",
        'if [ -z "$cp_up" ]; then',
        '  cant "a control plane reachable over the tailnet ($JAAH_CP did not answer)" "in the tailnet admin, check'
        " this Mac's device is approved and the control plane's subnet route is enabled — then run the command"
        ' again"',
        "  refuse_if_any",
        "fi",
    ]
    fetch = _MACOS_FETCH_PUBLIC.replace("@@BOOTSTRAP@@", BOOTSTRAP_PATH)
    return {
        "TAILSCALE_PREFLIGHT": _MACOS_TS_PREFLIGHT_PUBLIC,
        "TAILNET": fetch.replace("@@VERSION@@", shlex.quote(_macos_release())) + "\n" + "\n".join(guard),
        "RELEASE": "",
        "DOWNLOAD": "",
    }


def render_macos_body(inputs: BootstrapInputs) -> str:
    """The installer served to a macOS node that fetched its bootstrap (`| sudo bash`); see the template.

    The one-liner is fetched over the public console, but `cp_base_url` is the tailnet-only machine surface.
    With `public_base_url` the installer brings everything itself — the bundle back over the public path, and
    tailscaled from it — then joins with the key `routes/agent_bootstrap.py` minted for this fetch. Without a
    key (none configured, or the node has reported before) a Mac off the tailnet is refused."""
    _validate(inputs)
    parts = urlsplit(inputs.cp_base_url)
    base = f"{parts.scheme}://{parts.netloc}"  # rebuilt from validated parts, as `_body` does
    public = ""
    if inputs.public_base_url is not None:
        public_parts = urlsplit(inputs.public_base_url)
        public = f"{public_parts.scheme}://{public_parts.netloc}"
    env_lines = [
        # Raw, not quoted: a quoted heredoc. `_validate` pinned the base and the name to `$`-free grammars.
        f"JAAH_HOME={MACOS_JAAH_HOME}",
        f"JAAH_CP_BASE_URL={base}",
        f"JAAH_DAEMON_USER={MACOS_DAEMON_USER}",
        f"JAAH_SESSION_USER={MACOS_RUNNER_USER}",
        f"JAAH_UPGRADE_MARKER={MACOS_UPGRADE_MARKER}",
        f"JAAH_POOL_MIRRORS={MACOS_POOL_MIRRORS}",
        # The Ubuntu body's rule: no line at all when no hash was carried.
        *(
            [f"JAAH_REMOTE_SETTINGS_CONSENT_HASH={inputs.remote_settings_consent_hash}"]
            if inputs.remote_settings_consent_hash
            else []
        ),
        f"JAAH_RUNNER_SESSION={inputs.node_name}",
    ]
    slots = {
        "TOKEN": shlex.quote(inputs.node_token),
        "CP": shlex.quote(base),
        "PUBLIC_CP": shlex.quote(public),
        "NODE_ID": shlex.quote(inputs.node_id),
        "ROOT": MACOS_ROOT,
        "HOME": MACOS_JAAH_HOME,
        "PLATFORM": MACOS_PLATFORM,
        "DAEMON_USER": MACOS_DAEMON_USER,
        "RUNNER_USER": MACOS_RUNNER_USER,
        "LABELS": shlex.quote(" ".join(MACOS_LABELS)),
        "DOCKER_HOST": MACOS_DOCKER_HOST,
        "MIN_MAJOR": str(MACOS_MIN_MAJOR),
        "POOL_MIRRORS": MACOS_POOL_MIRRORS,
        "ENV_LINES": "\n".join(env_lines),
        # Last, so nothing a key carries is read as a slot.
        **_macos_tailnet_slots(inputs),
    }
    script = _MACOS_TEMPLATE
    for name, value in slots.items():
        script = script.replace(f"@@{name}@@", value)
    return script


class InstallerPin(NamedTuple):
    """One vendor installer, pinned: what a Windows node downloads and the bytes and signer it must carry."""

    url: str
    sha256: str
    thumbprint: str


# ── Windows x64: the native installer ─────────────────────────────────────────────────────────────────
# The layout is spelled four times, once per deployable that cannot import another: here (the installer),
# the bundle's `libexec/jaah-common.ps1` (the scripts it ships), `JaahService.cs` (the
# service host) and `apps/jaahd/host_platform/windows.py::_PATHS` (the daemon).
# A test pins them.
WINDOWS_ROOT = "C:\\Program Files\\Jaah"
WINDOWS_DATA = "C:\\ProgramData\\Jaah"
#: `JAAH_HOME` on a Windows node, and the runner account's home folder.
WINDOWS_JAAH_HOME = f"{WINDOWS_DATA}\\home"
WINDOWS_RUNNER_USER = "jaah"
#: The services the installer registers, in start order. The engine's (the `jaah-engine` WSL2 distro and
#: its docker pipe) and the runner's run as `.\\jaah`, the daemon's as its own virtual account
#: (`NT SERVICE\\jaahd`), the updater's as LocalSystem.
WINDOWS_SERVICES = ("jaah-engine", "jaahd", "jaah-runner", "jaah-update")
#: Node.js, which every Claude Code hook runs: phase 1b installs it here, the runner service puts it on PATH.
WINDOWS_NODEJS = "C:\\Program Files\\nodejs"
#: The vendor installers a Windows node's first install runs, pinned: one release's URL, the file's
#: sha256, and the SHA-1 thumbprint of the leaf certificate that signed it (`Get-AuthenticodeSignature`'s
#: `SignerCertificate.Thumbprint`). A setup matching all three is run; anything else refuses the install. Bump the
#: three together, read off the new release: `sha256sum`, and the signer certificate's SHA-1 fingerprint.
WINDOWS_GIT_SETUP = InstallerPin(
    url="https://github.com/git-for-windows/git/releases/download/v2.56.0.windows.2/Git-2.56.0.2-64-bit.exe",
    sha256="52188F917B378F00C70EC136BCF090005F30D44FBC4EBA0BCE759CC6592D60F6",
    thumbprint="C4BA1DE1C1B6049CF0E3FB393640685052199AEB",
)
WINDOWS_TAILSCALE_SETUP = InstallerPin(
    url="https://pkgs.tailscale.com/stable/tailscale-setup-1.104.2-amd64.msi",
    sha256="7EFF4ABDCBCF592AC9BCE28F2B9F0FD51F8FC09194C955FBE8253B67B531E7F5",
    thumbprint="108F172FDE945B21A5C0696731D6220D67D1C39E",
)
#: Windows 10 22H2; every Windows 11 build is above it.
WINDOWS_MIN_BUILD = 19045
#: Free space C: needs for a first install: Git, WSL, the bundle, the engine's distro and its images, worktrees.
WINDOWS_MIN_FREE_GB = 20
WINDOWS_PLATFORM = "windows-x64"
#: The daemon's upgrade request (`JAAH_UPGRADE_MARKER`), polled by the `jaah-update` service.
WINDOWS_UPGRADE_MARKER = f"{WINDOWS_DATA}\\state\\requests\\upgrade"

# The script, as one template with `@@NAME@@` slots, filled by `render_windows_body` from validated (or
# PowerShell-quoted) values. Raw: PowerShell's own `$` and backslashes stay literal.
_WINDOWS_TEMPLATE = r"""# The Jaah node installer for Windows x64, fetched from the control plane over this node's own token
# (apps/api/app/node_bootstrap.py) and run by `irm ... | iex` in an elevated PowerShell. It holds that
# credential in cleartext: never log or print it.
#
# PHASES, IN A LOAD-BEARING ORDER:
#   1. preflight - CHANGES NOTHING. Every failure is one `Can't install: <thing> - <fix>` line, and any
#      failure stops here, so a refused PC is left exactly as it was and the node stays never-seen. Only what
#      needs the tailnet (the control plane's release) is checked after 1b, and its refusal names what 1b
#      installed.
#  1b. the prerequisites it is missing, each vendor-signed: Git for Windows, Node.js LTS, Tailscale and the
#      tailnet join (with the single-use key minted for this fetch), WSL 2. Turning WSL 2 on can need a restart; the run
#      then stops, and the same command run after the restart picks up here.
#   2. the accounts: the local runner user jaah (non-admin; its password is held only by the Service
#      Control Manager) and the daemon's virtual account NT SERVICE\jaahd; the data directories
#   3. the tailnet: unattended mode, so it stays connected with nobody signed in
#   4. the bundle, from the CP's dist channel, sha256-verified, to @@ROOT@@\versions\<ver> + `current`
#   5. @@DATA@@\etc: jaahd.env (the knobs) and node.env (identity; the token only ever lands here)
#   6. the services jaah-engine, jaahd, jaah-runner, jaah-update - they start at boot, signed in or not
#   7. `jaah auth login`, then a self-check that fails the install if the daemon, the runners' tmux server
#      or the container engine (`docker info` as jaah) is not serving
#   8. `jaah auth rotate`: the token this command carries stops working
#   FINAL, whether the install succeeded or not: the PowerShell history keeps no line holding the token.
# Uninstall: & '@@ROOT@@\current\bin\jaah-uninstall.cmd'
#
# Windows PowerShell 5.1 compatible (what a fresh PC has) and ASCII only. Every word here is chosen so
# PowerShell's own script-block logging never flags this text as suspicious: a flagged block is written
# to the event log whole, token and all, even where no logging policy is set
# (`tools/tests/test_node_bootstrap_windows.py` holds the list).
& {
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
# Windows' default policy, Restricted, refuses even the module files behind cmdlets used here
# (Get-FileHash, Get-LocalUser). For THIS PowerShell process only, and put back at the end. A policy set by
# Group Policy outranks it, and preflight then names it.
$policySaved = Get-ExecutionPolicy -Scope Process
try { Set-ExecutionPolicy -Scope Process -ExecutionPolicy RemoteSigned -Force } catch { }
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$JaahNodeToken = @@TOKEN@@
$JaahCp = @@CP@@
$JaahNodeId = @@NODE_ID@@
$JaahRoot = '@@ROOT@@'
$JaahData = '@@DATA@@'
$JaahHome = '@@HOME@@'
$JaahPlatform = '@@PLATFORM@@'
$JaahRunnerUser = '@@RUNNER_USER@@'
$JaahEngineService = '@@ENGINE_SERVICE@@'
$JaahDaemonService = '@@DAEMON_SERVICE@@'
$JaahRunnerService = '@@RUNNER_SERVICE@@'
$JaahUpdateService = '@@UPDATE_SERVICE@@'
$JaahMinBuild = @@MIN_BUILD@@
$JaahMinFreeGb = @@MIN_FREE_GB@@
$JaahNodeName = '@@NODE_NAME@@'
# The single-use tailnet key minted for this fetch ('' = none), the server it is presented to ('' =
# Tailscale's own), and that server as preflight compares it.
$JaahTsKey = @@TS_KEY@@
$JaahTsLoginServer = @@TS_LOGIN_SERVER@@
$JaahTsServer = @@TS_SERVER@@
$JaahEtc = Join-Path $JaahData 'etc'
$JaahNodeEnv = Join-Path $JaahEtc 'node.env'
$JaahEnvFile = Join-Path $JaahEtc 'jaahd.env'
$JaahLog = Join-Path $JaahData 'log'
$JaahPipe = '\\.\pipe\jaah-creds'
$System32 = Join-Path $env:SystemRoot 'System32'
$SidSystem = 'S-1-5-18'
$SidAdmins = 'S-1-5-32-544'
$SidUsers = 'S-1-5-32-545'
$work = $null
$envNames = @('JAAH_NODE_ENV_FILE', 'JAAH_JAAHD_ENV_FILE', 'WSL_UTF8')
$envSaved = @{}
foreach ($name in $envNames) { $envSaved[$name] = [Environment]::GetEnvironmentVariable($name, 'Process') }

function Write-Step([string]$Text) { Write-Host "==> [jaah] $Text" }

$problems = New-Object System.Collections.Generic.List[string]
function Add-Problem([string]$Thing, [string]$Fix) { $problems.Add("Can't install: $Thing - $Fix") }
# What phase 1b installed, so a later refusal does not claim the PC is untouched.
$installed = New-Object System.Collections.Generic.List[string]
function Assert-Preflight {
    if ($problems.Count -gt 0) {
        foreach ($line in $problems) { Write-Host $line -ForegroundColor Red }
        if ($installed.Count -gt 0) {
            Write-Host "This run installed $($installed -join ', ') and changed nothing else. Fix the above and run the same command again." -ForegroundColor Red
        } else {
            Write-Host 'Nothing was changed on this PC. Fix the above and run the same command again.' -ForegroundColor Red
        }
        throw 'jaah-refused'
    }
}
function Stop-Install([string]$Text) { throw ('jaah-stop: ' + $Text) }
# What WSL 2's VM needs, said once for the preflight refusal and the engine check.
$VirtualizationFix = 'on a physical PC turn on Intel VT-x or AMD-V in the firmware; on AWS EC2 use a C8i, M8i or R8i instance with nested virtualization turned on (stop it, then Actions > Instance settings > Change CPU options), or a .metal instance - then run the same command again'

# One authenticated GET. The token rides a request header in this process, never a command line, and no
# redirect is followed: the dist route answers 307 to a presigned URL, and following it here would hand the
# token to the bucket too. Returns @{ Code; Body; Location }; Code 0 = nothing answered.
function Invoke-Cp([string]$Path) {
    $result = @{ Code = 0; Body = ''; Location = '' }
    try {
        $request = [Net.WebRequest]::Create($JaahCp + $Path)
        $request.Method = 'GET'
        $request.AllowAutoRedirect = $false
        $request.Timeout = 30000
        $request.Headers.Add('X-Agent-Token', $JaahNodeToken)
        $response = $request.GetResponse()
    } catch [Net.WebException] {
        $response = $_.Exception.Response
        if ($null -eq $response) { return $result }
    } catch {
        return $result
    }
    try {
        $result.Code = [int]$response.StatusCode
        $result.Location = [string]$response.Headers['Location']
        $reader = New-Object IO.StreamReader($response.GetResponseStream())
        $result.Body = $reader.ReadToEnd()
        $reader.Close()
    } finally {
        $response.Close()
    }
    return $result
}
# One dist file: the presigned location from the CP (token), then THAT, with no token. The location is a
# bearer credential too; it stays in this process.
function Save-Dist([string]$Name, [string]$OutFile, [int]$TimeoutSec) {
    $answer = Invoke-Cp "@@DIST@@/$JaahPlatform/$Name"
    if ($answer.Code -lt 300 -or $answer.Code -ge 400 -or -not $answer.Location.StartsWith('https://')) { return $false }
    try {
        Invoke-WebRequest -UseBasicParsing -Uri $answer.Location -OutFile $OutFile -TimeoutSec $TimeoutSec -MaximumRedirection 0
        return $true
    } catch {
        return $false
    }
}
function Get-EnvLine([string]$Path, [string]$Key) {
    if (-not (Test-Path -LiteralPath $Path)) { return '' }
    $value = ''
    foreach ($line in [IO.File]::ReadAllLines($Path)) {
        if ($line.StartsWith("$Key=", [StringComparison]::Ordinal)) { $value = $line.Substring($Key.Length + 1).TrimEnd("`r") }
    }
    return $value
}
function Write-Ascii([string]$Path, [string[]]$Lines) {
    [IO.File]::WriteAllText($Path, (($Lines -join "`n") + "`n"), [Text.Encoding]::ASCII)
}
# Native commands. 'Continue' inside: under 'Stop', Windows PowerShell 5.1 turns a native command's first
# stderr line into a terminating error once it is redirected. Its exit code is what is judged.
function Get-NativeResult([scriptblock]$Command) {
    $ErrorActionPreference = 'Continue'
    $text = & $Command 2>&1 | Out-String
    return @{ Code = $LASTEXITCODE; Text = $text.Trim() }
}
function Invoke-Native([string]$What, [scriptblock]$Command) {
    $result = Get-NativeResult $Command
    if ($result.Code -ne 0) { Stop-Install ("$What failed (exit $($result.Code)): " + $result.Text) }
}
# A protected access list: inheritance from the parent cut, then exactly these grants (SIDs, never names,
# so a localized Windows reads them the same).
function Set-Access([string]$Path, [string[]]$Grants) {
    # Owner first: whoever owns a folder can rewrite its list, and a user could have made it between
    # preflight and here.
    Invoke-Native "taking ownership of $Path" { & (Join-Path $System32 'icacls.exe') $Path /setowner "*$SidAdmins" /q }
    $arguments = @($Path, '/inheritance:r', '/grant:r') + $Grants + @('/q')
    Invoke-Native "setting the access list on $Path" { & (Join-Path $System32 'icacls.exe') $arguments }
}
function Get-ServiceSid([string]$Account) {
    return (New-Object Security.Principal.NTAccount($Account)).Translate([Security.Principal.SecurityIdentifier]).Value
}

try {
# -- 1. preflight: changes nothing --------------------------------------------------------------------
Write-Step 'checking this PC'
# Only Group Policy can still say Restricted or AllSigned here, and it would stop the updater too.
if (@('Restricted', 'AllSigned') -contains [string](Get-ExecutionPolicy)) {
    Add-Problem "a PowerShell execution policy that allows local scripts (Group Policy sets it to $(Get-ExecutionPolicy) here)" 'ask your administrator to allow RemoteSigned, then run the command again'
}
$principal = New-Object Security.Principal.WindowsPrincipal([Security.Principal.WindowsIdentity]::GetCurrent())
$elevated = $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $elevated) {
    Add-Problem 'an elevated PowerShell' 'right-click Windows PowerShell, choose "Run as administrator", and paste the command there'
}
# The machine's own architecture, not this process's: x64 PowerShell under emulation on an ARM PC reads AMD64.
$nativeArch = [string](Get-ItemProperty 'HKLM:\SYSTEM\CurrentControlSet\Control\Session Manager\Environment').PROCESSOR_ARCHITECTURE
if ($nativeArch -ne 'AMD64') {
    Add-Problem "an x64 PC (this one is $nativeArch)" 'Windows on ARM is not supported - use an Intel or AMD PC'
} elseif ($env:PROCESSOR_ARCHITECTURE -ne 'AMD64') {
    Add-Problem '64-bit PowerShell' 'open "Windows PowerShell" as administrator, not "Windows PowerShell (x86)"'
}
$build = 0
[void][int]::TryParse([string](Get-ItemProperty 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion').CurrentBuildNumber, [ref]$build)
if ($build -lt $JaahMinBuild) {
    Add-Problem "Windows 11 or Windows 10 22H2 (build $JaahMinBuild or later; this PC is build $build)" 'update it in Settings > Windows Update'
}
# Git for Windows, Node.js, WSL 2 and Tailscale are not refusals: phase 1b installs whichever is missing, once
# every refusal below has passed. Here they are only looked for.
# Machine-wide only: the runner account must be able to run it, and it cannot read another user's profile.
function Find-GitBash {
    $roots = @()
    $key = Get-ItemProperty 'HKLM:\SOFTWARE\GitForWindows' -ErrorAction SilentlyContinue
    if ($key -and $key.InstallPath) { $roots += [string]$key.InstallPath }
    $roots += (Join-Path $env:ProgramFiles 'Git')
    foreach ($root in $roots) {
        if (Test-Path -LiteralPath (Join-Path $root 'bin\bash.exe')) { return (Join-Path $root 'bin\bash.exe') }
    }
    return $null
}
$gitBash = Find-GitBash
# Node.js: what every Claude Code hook runs (`node ...` under Git Bash). Only the machine-wide install, at the
# one folder the runner service puts on PATH (JaahService.cs, `Layout.NodeJs`), where the MSI is told to go.
$JaahNodeJs = '@@NODEJS@@'
$nodeReady = Test-Path -LiteralPath (Join-Path $JaahNodeJs 'node.exe')
# WSL 2 = the Virtual Machine Platform turned on AND a wsl.exe that answers `--status`.
$wslExe = Join-Path $System32 'wsl.exe'
function Test-Wsl {
    $platformOn = $false
    try { $platformOn = (Get-WindowsOptionalFeature -Online -FeatureName VirtualMachinePlatform -ErrorAction Stop).State -eq 'Enabled' } catch { }
    if (-not $platformOn -or -not (Test-Path -LiteralPath $wslExe)) { return $false }
    $env:WSL_UTF8 = '1'
    # `--version` answers on the Store WSL even before any distro exists; `--status` on the inbox one.
    return ((Get-NativeResult { & $wslExe --version }).Code -eq 0) -or ((Get-NativeResult { & $wslExe --status }).Code -eq 0)
}
$wslReady = $elevated -and (Test-Wsl)
$csc = Join-Path $env:SystemRoot 'Microsoft.NET\Framework64\v4.0.30319\csc.exe'
if (-not (Test-Path -LiteralPath $csc)) {
    Add-Problem 'the .NET Framework 4 compiler (csc.exe)' 'turn on ".NET Framework 4.8 Advanced Services" in Settings > Optional features > More Windows features'
}
if (-not (Test-Path -LiteralPath (Join-Path $System32 'tar.exe'))) {
    Add-Problem 'tar.exe' 'it ships with Windows 10 1803 and later - update Windows'
}
function Find-Tailscale {
    $path = Join-Path $env:ProgramFiles 'Tailscale\tailscale.exe'
    if (Test-Path -LiteralPath $path) { return $path }
    $found = Get-Command tailscale.exe -ErrorAction SilentlyContinue
    if ($found) { return $found.Source }
    return $null
}
$tailscale = Find-Tailscale
# The tailnet: joined by phase 1b with the single-use key this control plane minted for this fetch, or,
# without one (none configured, or this node has reported before), required already.
$tsJoin = $false
if (-not $tailscale) {
    if ($JaahTsKey) { $tsJoin = $true } else { Add-Problem 'Tailscale' 'install it from https://tailscale.com/download/windows and sign in, then run the command again' }
} else {
    $tsService = Get-Service -Name Tailscale -ErrorAction SilentlyContinue
    if (-not $tsService -or @('Automatic', 'AutomaticDelayedStart') -notcontains [string]$tsService.StartType) {
        Add-Problem 'the Tailscale service starting with Windows' 'reinstall Tailscale from https://tailscale.com/download/windows (it installs a service that starts at boot)'
    } else {
        # WHICH tailnet, connected or stopped? A PC on another one (its owner's own) has no route to this control
        # plane, and moving it off that tailnet is its owner's call. `debug prefs` is the one CLI that names the
        # server; read by pattern, since a warning on stderr would break a JSON parse. Unreadable reads as ours.
        $control = ''
        if ($JaahTsServer -and (Get-NativeResult { & $tailscale debug prefs }).Text -match '"ControlURL"\s*:\s*"([^"]+)"') { $control = $Matches[1] }
        $controlKey = $control.TrimEnd('/').ToLowerInvariant()
        if ($controlKey.EndsWith(':443')) { $controlKey = $controlKey.Substring(0, $controlKey.Length - 4) }
        $status = Get-NativeResult { & $tailscale status }
        $signedIn = $status.Code -eq 0
        # Stopped = signed in, switched off. Logged out (a never-used install reads Tailscale's own server) is
        # nobody's tailnet, so the join takes it.
        if ($controlKey -and $controlKey -ne $JaahTsServer -and ($signedIn -or $status.Text -match 'stopped')) {
            Add-Problem "a PC that is not signed in to another tailnet (this one uses $control)" 'run: tailscale logout - then run this command again'
        } elseif (-not $signedIn) {
            if ($JaahTsKey) { $tsJoin = $true } else { Add-Problem 'a connected tailnet' 'open Tailscale and sign in, then run this command again' }
        }
    }
}
# A DIFFERENT NODE IS A REFUSAL, NEVER A REPOINT (the Ubuntu bootstrap's rule and reasons).
$existingNodeId = ''
if ($elevated) { $existingNodeId = Get-EnvLine $JaahNodeEnv 'NODE_ID' }
if ($existingNodeId -and $existingNodeId -ne $JaahNodeId) {
    Add-Problem "a PC that is not already a Jaah node (this one is node $existingNodeId)" "remove it first: & '$JaahRoot\current\bin\jaah-uninstall.cmd'"
}
# Room for a first install (a re-run on this node installs nothing new).
if ($existingNodeId -ne $JaahNodeId) {
    $freeGb = [math]::Floor((Get-PSDrive -Name ($env:SystemDrive.TrimEnd(':'))).Free / 1GB)
    if ($freeGb -lt $JaahMinFreeGb) {
        Add-Problem "$JaahMinFreeGb GB free on $env:SystemDrive (this PC has $freeGb GB)" 'free up space or grow the disk, then run the command again'
    }
}
# An account that already exists is reused, so it must hold no privileged group: Administrators would own
# the PC, and Backup Operators (and the others listed) could read node.env past its access list.
$runner = Get-LocalUser -Name $JaahRunnerUser -ErrorAction SilentlyContinue
if ($runner -and $elevated) {
    $privileged = @()
    foreach ($groupSid in @($SidAdmins, 'S-1-5-32-551', 'S-1-5-32-547', 'S-1-5-32-548', 'S-1-5-32-549', 'S-1-5-32-550', 'S-1-5-32-578')) {
        $group = Get-LocalGroup -SID $groupSid -ErrorAction SilentlyContinue
        if (-not $group) { continue }
        $member = $false
        try {
            $member = @(Get-LocalGroupMember -SID $groupSid -ErrorAction Stop | Where-Object { $_.SID.Value -eq $runner.SID.Value }).Count -gt 0
        } catch {
            # Get-LocalGroupMember fails outright on a group holding an orphaned SID; ask net.exe instead.
            foreach ($line in ((Get-NativeResult { & (Join-Path $System32 'net.exe') localgroup $group.Name }).Text -split "`r?`n")) {
                if (([string]$line).Trim() -ieq $JaahRunnerUser -or ([string]$line).Trim() -ilike "*\$JaahRunnerUser") { $member = $true }
            }
        }
        if ($member) { $privileged += $group.Name }
    }
    if ($privileged.Count -gt 0) {
        Add-Problem "an unprivileged $JaahRunnerUser account (this PC's $JaahRunnerUser is in $($privileged -join ', '))" 'remove it from those groups, or delete it, then run the command again'
    }
}
# Any user may create folders in C:\ProgramData, and whoever OWNS a folder can rewrite its access list
# whatever the installer sets. So everything already there must be owned by SYSTEM or Administrators and be
# no link: else a user who made C:\ProgramData\Jaah\etc first could swap jaahd.env for one that sends the
# updater, and the token, elsewhere.
if ($elevated -and (Test-Path -LiteralPath $JaahData)) {
    $suspect = @()
    $items = @(Get-Item -LiteralPath $JaahData -Force)
    foreach ($child in @('etc', 'state', 'run', 'home', 'log', 'engine')) {
        $path = Join-Path $JaahData $child
        if (Test-Path -LiteralPath $path) { $items += Get-Item -LiteralPath $path -Force }
    }
    if (Test-Path -LiteralPath $JaahEtc) { $items += @(Get-ChildItem -LiteralPath $JaahEtc -Force) }
    foreach ($item in $items) {
        $owner = (Get-Acl -LiteralPath $item.FullName).GetOwner([Security.Principal.SecurityIdentifier]).Value
        $linked = [bool]($item.Attributes -band [IO.FileAttributes]::ReparsePoint)
        if ($linked -or @($SidSystem, $SidAdmins) -notcontains $owner) { $suspect += $item.FullName }
    }
    if ($suspect.Count -gt 0) {
        Add-Problem "a $JaahData that only SYSTEM and Administrators own ($($suspect -join ', ') is owned by another account, or is a link)" "delete $JaahData, then run the command again"
    }
}
Assert-Preflight
$work = Join-Path $env:TEMP ('jaah-install-' + [guid]::NewGuid().ToString('N'))
New-Item -ItemType Directory -Path $work | Out-Null

# -- 1b. the prerequisites: Git for Windows, Node.js, Tailscale (and the join), WSL 2 ------------------
# Each from its vendor's own download, and run only once Windows has verified the vendor's signature on it.
# A pinned setup (`-Sha256`, `-Thumbprint`) must also be exactly that release, signed by exactly that certificate.
function Save-Signed([string]$Uri, [string]$OutFile, [string]$Signer, [string]$Sha256 = '', [string]$Thumbprint = '') {
    try {
        Invoke-WebRequest -UseBasicParsing -Uri $Uri -OutFile $OutFile -TimeoutSec 900
    } catch {
        Stop-Install "could not download $Uri ($($_.Exception.Message))"
    }
    $signature = Get-AuthenticodeSignature -LiteralPath $OutFile
    if ($signature.Status -ne 'Valid' -or -not $signature.SignerCertificate -or $signature.SignerCertificate.Subject -notlike "*CN=$Signer*") {
        Stop-Install "$Uri is not signed by $Signer - refusing it"
    }
    if ($Thumbprint -and $signature.SignerCertificate.Thumbprint -ne $Thumbprint) {
        Stop-Install "$Uri is signed by a certificate other than the pinned $Thumbprint - refusing it"
    }
    if ($Sha256 -and (Get-FileHash -LiteralPath $OutFile -Algorithm SHA256).Hash -ne $Sha256) {
        Stop-Install "$Uri is not the pinned release (sha256 $Sha256) - refusing it"
    }
}
function Invoke-Setup([string]$What, [string]$File, [string[]]$Arguments) {
    # Not -Wait, which also waits for every process the setup leaves running (a tray app).
    $process = Start-Process -FilePath $File -ArgumentList $Arguments -PassThru
    $process.WaitForExit()
    # 3010: done, a restart finishes it.
    if (@(0, 3010) -notcontains $process.ExitCode) { Stop-Install "the $What setup failed (exit $($process.ExitCode))" }
}
if (-not $gitBash) {
    Write-Step 'installing Git for Windows'
    $setup = Join-Path $work 'git-setup.exe'
    Save-Signed '@@GIT_URL@@' $setup 'Johannes Schindelin' '@@GIT_SHA256@@' '@@GIT_THUMBPRINT@@'
    Invoke-Setup 'Git for Windows' $setup @('/VERYSILENT', '/NORESTART', '/NOCANCEL', '/SP-', '/SUPPRESSMSGBOXES', '/ALLUSERS')
    $gitBash = Find-GitBash
    if (-not $gitBash) { Stop-Install 'Git for Windows installed, but its bash.exe is not under Program Files' }
    $installed.Add('Git for Windows')
}
if (-not $nodeReady) {
    Write-Step 'installing Node.js LTS'
    # nodejs.org's release index, newest first: the first LTS line with a Windows x64 MSI.
    $nodeVersion = ''
    try {
        $releases = Invoke-RestMethod -UseBasicParsing -Uri 'https://nodejs.org/dist/index.json' -TimeoutSec 60
        $lts = @($releases | Where-Object { $_.lts -and @($_.files) -contains 'win-x64-msi' }) | Select-Object -First 1
        if ($lts -and [string]$lts.version -cmatch '^v[0-9]+[.][0-9]+[.][0-9]+\z') { $nodeVersion = [string]$lts.version }
    } catch { $nodeVersion = '' }
    if (-not $nodeVersion) { Stop-Install 'could not find the latest Node.js LTS release on nodejs.org' }
    $setup = Join-Path $work 'node-setup.msi'
    Save-Signed "https://nodejs.org/dist/$nodeVersion/node-$nodeVersion-x64.msi" $setup 'OpenJS Foundation'
    Invoke-Setup 'Node.js' (Join-Path $System32 'msiexec.exe') @('/i', ('"' + $setup + '"'), '/quiet', '/norestart', 'ALLUSERS=1', ('INSTALLDIR="' + $JaahNodeJs + '"'))
    if (-not (Test-Path -LiteralPath (Join-Path $JaahNodeJs 'node.exe'))) { Stop-Install "Node.js installed, but its node.exe is not in $JaahNodeJs" }
    $installed.Add('Node.js')
}
if (-not $tailscale) {
    Write-Step 'installing Tailscale'
    $setup = Join-Path $work 'tailscale-setup.msi'
    Save-Signed '@@TS_URL@@' $setup 'Tailscale Inc.' '@@TS_SHA256@@' '@@TS_THUMBPRINT@@'
    # TS_NOLAUNCH: no "Connect to your tailnet devices" window over the operator's PowerShell.
    Invoke-Setup 'Tailscale' (Join-Path $System32 'msiexec.exe') @('/i', ('"' + $setup + '"'), '/quiet', '/norestart', 'TS_NOLAUNCH=true')
    $tailscale = Find-Tailscale
    if (-not $tailscale) { Stop-Install 'Tailscale installed, but its tailscale.exe is not under Program Files' }
    $installed.Add('Tailscale')
}
if ($tsJoin) {
    Write-Step 'joining the tailnet'
    # The service answers a moment after its setup returns.
    for ($i = 0; $i -lt 30; $i++) {
        $service = Get-Service -Name Tailscale -ErrorAction SilentlyContinue
        if ($service -and $service.Status -ne 'Running') { try { Start-Service -InputObject $service -ErrorAction Stop } catch { } }
        if ((Get-NativeResult { & $tailscale status }).Text -notmatch 'failed to connect|is Tailscale running') { break }
        Start-Sleep -Seconds 2
    }
    # The key rides the argv, as on Ubuntu and macOS: single-use, and spent by the time `up` returns.
    # Unattended: connected after a reboot with nobody signed in.
    $upArgs = @('up', '--authkey', $JaahTsKey, '--hostname', $JaahNodeName, '--accept-routes', '--unattended', '--timeout', '120s')
    if ($JaahTsLoginServer) { $upArgs += @('--login-server', $JaahTsLoginServer) }
    $joined = Get-NativeResult { & $tailscale $upArgs }
    $upArgs = $null
    if ($joined.Code -ne 0) { Stop-Install ('tailscale up failed: ' + $joined.Text) }
    $installed.Add('the tailnet join')
    # The route to the control plane arrives with the netmap, a moment after `up` returns. Any answer proves it;
    # the release check below names a route that never came.
    $deadline = (Get-Date).AddMinutes(2)
    while ((Get-Date) -lt $deadline -and (Invoke-Cp '/api/health').Code -eq 0) { Start-Sleep -Seconds 2 }
}
$JaahTsKey = $null
$restart = $false
# Why WSL 2 cannot run its VM on this machine, found before the engine is waited for (`Register-AndCheck`).
$noVirtualization = ''
if (-not $wslReady) {
    Write-Step 'turning on WSL 2'
    $enabled = $false
    foreach ($feature in @('Microsoft-Windows-Subsystem-Linux', 'VirtualMachinePlatform')) {
        if ((Get-WindowsOptionalFeature -Online -FeatureName $feature).State -ne 'Enabled') {
            $enabled = $true
            if ((Enable-WindowsOptionalFeature -Online -FeatureName $feature -All -NoRestart -WarningAction SilentlyContinue).RestartNeeded) { $restart = $true }
        }
    }
    # WSL itself, with no distro: the engine imports its own (jaah-engine). Its kernel arrives here too.
    $wslInstall = ''
    if (Test-Path -LiteralPath $wslExe) { $wslInstall = (Get-NativeResult { & $wslExe --install --no-distribution }).Text }
    $installed.Add('WSL 2')
    if (-not $restart -and -not (Test-Wsl)) {
        # A restart finishes only what this run turned on: with nothing to finish, another restart would loop. With
        # everything on and no VT-x (a GCE Windows VM: Google runs no Hyper-V nested), the node installs without its
        # engine, as for WSL refusing its VM below.
        if ($enabled) { $restart = $true } else { $noVirtualization = 'WSL 2 did not start'; Write-Host "    wsl said: $wslInstall" }
    }
}
if ($restart) { throw 'jaah-restart' }

$python = Join-Path $JaahRoot 'current\runtime\python\python.exe'
$cli = Join-Path $JaahRoot 'current\apps\jaahd\jaah_cli.py'

# -- 7. registration + self-check, defined here because a credential rotation runs only this ----------
function Register-AndCheck {
    Write-Step 'registering this node with the control plane'
    # The one writer of NODE_TOKEN. The token reaches it on STDIN, and it verifies before it writes.
    $env:JAAH_NODE_ENV_FILE = $JaahNodeEnv
    $env:JAAH_JAAHD_ENV_FILE = $JaahEnvFile
    $login = Get-NativeResult { $JaahNodeToken | & $python $cli auth login $JaahNodeId --with-token }
    if ($login.Code -ne 0) { Stop-Install ('jaah auth login failed: ' + $login.Text) }
    Write-Host $login.Text

    # The runner's session is this node's NAME, as the daemon resolves it (NODE_NAME, else NODE_ID).
    # `jaah` cannot read node.env, so the (non-secret) name is copied into jaahd.env. Replace, not a
    # rewrite: it keeps jaahd.env's own access list.
    $session = Get-EnvLine $JaahNodeEnv 'NODE_NAME'
    if (-not $session) { $session = $JaahNodeId }
    $kept = @([IO.File]::ReadAllLines($JaahEnvFile) | Where-Object { -not $_.StartsWith('JAAH_RUNNER_SESSION=', [StringComparison]::Ordinal) })
    Write-Ascii "$JaahEnvFile.new" ($kept + @("JAAH_RUNNER_SESSION=$session"))
    # [NullString]::Value, not $null: PowerShell 5.1 passes $null to a .NET string as "", and Replace
    # throws "The path is not of a legal form" on an empty backup path.
    [IO.File]::Replace("$JaahEnvFile.new", $JaahEnvFile, [NullString]::Value)
    if ((Get-Service -Name $JaahRunnerService).Status -eq 'Running') { Restart-Service -Name $JaahRunnerService -Force }

    # The daemon reads node.env only at start.
    if ((Get-Service -Name $JaahDaemonService).Status -eq 'Running') {
        Restart-Service -Name $JaahDaemonService -Force
    } else {
        Start-Service -Name $JaahDaemonService
    }

    Write-Step 'checking the node is running'
    $up = $false
    for ($i = 0; $i -lt 60 -and -not $up; $i++) {
        $service = Get-CimInstance Win32_Service -Filter "Name='$JaahDaemonService'"
        $pipes = @()
        try { $pipes = [IO.Directory]::GetFiles('\\.\pipe\') } catch { $pipes = @() }
        $serving = ($pipes -contains $JaahPipe) -or (Test-Path -LiteralPath $JaahPipe)
        if ($service.State -eq 'Running' -and $service.ProcessId -and $serving) {
            # Up = the same process still running 5 s later: a crash loop is not "running".
            $first = $service.ProcessId
            Start-Sleep -Seconds 5
            $again = Get-CimInstance Win32_Service -Filter "Name='$JaahDaemonService'"
            if ($again.State -eq 'Running' -and $again.ProcessId -eq $first) { $up = $true; break }
        }
        Start-Sleep -Seconds 1
    }
    if (-not $up) {
        Stop-Install "Self-check failed: the $JaahDaemonService service and its pipe $JaahPipe - see $JaahLog\jaahd.log"
    }

    # The runners' tmux server: its service up on one process, and the fleet socket it seeds present. Only
    # the FILE is looked at: running tmux from here would be an administrator, whom the server's access
    # list does not admit. The server comes up before the service installs Claude Code, so a few minutes
    # covers a first start; the deadline is time, not a count, so a slow poll cannot shorten it.
    Write-Step 'checking the runner tmux server'
    $socket = Join-Path $JaahData 'run\tmux\jaah\default'
    # Phase 6 started it before jaahd.env named its session, so it may be between the SCM's restarts.
    $runner = Get-Service -Name $JaahRunnerService
    if ($runner.Status -ne 'Running') {
        try { Start-Service -InputObject $runner -ErrorAction Stop } catch { }
    }
    $up = $false
    $deadline = (Get-Date).AddMinutes(10)
    while (-not $up -and (Get-Date) -lt $deadline) {
        $service = Get-CimInstance Win32_Service -Filter "Name='$JaahRunnerService'"
        if ($service.State -eq 'Running' -and $service.ProcessId -and (Test-Path -LiteralPath $socket)) {
            $first = $service.ProcessId
            Start-Sleep -Seconds 5
            $again = Get-CimInstance Win32_Service -Filter "Name='$JaahRunnerService'"
            if ($again.State -eq 'Running' -and $again.ProcessId -eq $first) { $up = $true; break }
        }
        Start-Sleep -Seconds 1
    }
    if (-not $up) {
        Stop-Install "Self-check failed: the $JaahRunnerService service and its tmux server $socket - see $JaahLog\runner.log"
    }

    # The container engine: the jaah-engine service runs `docker info` through its pipe, as jaah, every
    # minute and records a success in run\engine-ready - fresh means it answered just now. Only the file is
    # looked at, as above: the pipe's access list does not admit an administrator. A first start imports
    # the distro and downloads dockerd into it, hence the long deadline.
    Write-Step 'checking the container engine'
    $ready = Join-Path $JaahData 'run\engine-ready'
    $engine = Get-Service -Name $JaahEngineService
    if ($engine.Status -ne 'Running') {
        try { Start-Service -InputObject $engine -ErrorAction Stop } catch { }
    }
    # engine.log is followed while waiting: each new line is shown, and WSL refusing to create its VM (HCS_E_*,
    # which no retry fixes) ends the wait at once. That is a warning, not a stop: the node runs every task that
    # needs no container, and the engine service keeps retrying, so turning virtualization on later is enough.
    $engineLog = Join-Path $JaahLog 'engine.log'
    $seen = 0
    $partial = ''
    $item = Get-Item -LiteralPath $engineLog -ErrorAction SilentlyContinue
    if ($item) { $seen = $item.Length }
    $up = $false
    $noVm = $noVirtualization
    $deadline = (Get-Date).AddMinutes(20)
    while (-not $up -and -not $noVm -and (Get-Date) -lt $deadline) {
        $item = Get-Item -LiteralPath $ready -ErrorAction SilentlyContinue
        if ($item -and $item.LastWriteTime -gt (Get-Date).AddMinutes(-3)) {
            $up = ([IO.File]::ReadAllText($ready)).StartsWith('ok ', [StringComparison]::Ordinal)
        }
        # Whole lines only: a line the service is still writing waits for the next read, so a code is never split.
        $new = ''
        $stream = $null
        try {
            $stream = New-Object IO.FileStream($engineLog, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::ReadWrite)
            if ($stream.Length -gt $seen) {
                [void]$stream.Seek($seen, [IO.SeekOrigin]::Begin)
                $seen = $stream.Length
                $text = $partial + (New-Object IO.StreamReader($stream)).ReadToEnd()
                $end = $text.LastIndexOf("`n")
                $new = $text.Substring(0, $end + 1)
                $partial = $text.Substring($end + 1)
            }
        } catch {
            $new = ''
        } finally {
            if ($stream) { $stream.Dispose() }
        }
        foreach ($line in ($new -split "`r?`n")) { if ($line.Trim()) { Write-Host "    $($line.Trim())" } }
        if ($new -match '(HCS_E_[A-Z_]+)') { $noVm = $Matches[1]; break }
        if (-not $up) { Start-Sleep -Seconds 5 }
    }
    if ($noVm) {
        Write-Host "==> [jaah] warning: nested virtualization is off ($noVm), so this node runs no Docker and mounts no NodeLink." -ForegroundColor Yellow
        Write-Host "==> [jaah] to turn it on: $VirtualizationFix. The engine starts by itself once it can." -ForegroundColor Yellow
    } elseif (-not $up) {
        Stop-Install "Self-check failed: the $JaahEngineService service and docker info as $JaahRunnerUser - see $JaahLog\engine.log"
    }
    Write-Step 'done - this PC shows online on the Nodes page within a minute'
}

# -- 8. the credential rotation: the token this command carries stops working once the node is up -----
# Last, so a run that failed earlier can be re-run as is. A failed rotation is a warning: the node works.
function Invoke-CredentialRotation {
    Write-Step 'rotating the credential, so the one in this command stops working'
    $rotate = Get-NativeResult { & $python $cli auth rotate }
    if ($rotate.Code -ne 0) {
        Write-Host ('==> [jaah] warning: the credential did not rotate - retry with: jaah auth rotate. ' + $rotate.Text) -ForegroundColor Yellow
        return
    }
    Write-Host $rotate.Text
    # The daemon reads node.env only at start. A failed restart is a warning too: the bootstrap token is already
    # dead, so a run marked failed would have a cloud node retry its fetch with it at every boot.
    try { Restart-Service -Name $JaahDaemonService -Force -ErrorAction Stop } catch {
        Write-Host ('==> [jaah] warning: restart the ' + $JaahDaemonService + ' service to use the new credential. ' + $_.Exception.Message) -ForegroundColor Yellow
    }
}

# ALREADY THIS NODE: a re-issued token, or a run that died after the services were installed. Only the
# credential and the check - never a reinstall under live runners.
# Installed = phase 6 FINISHED (its marker, in the administrators-only etc), not merely begun.
$servicesInstalled = (Test-Path -LiteralPath (Join-Path $JaahRoot 'current\bin\jaah-service.exe')) -and (Test-Path -LiteralPath (Join-Path $JaahEtc 'installed'))
foreach ($name in @($JaahEngineService, $JaahDaemonService, $JaahRunnerService, $JaahUpdateService)) {
    if (-not (Get-Service -Name $name -ErrorAction SilentlyContinue)) { $servicesInstalled = $false }
}
if ($existingNodeId -eq $JaahNodeId -and $servicesInstalled -and (Test-Path -LiteralPath $python)) {
    Write-Step 'already installed; writing the new credential only'
    Register-AndCheck
    Invoke-CredentialRotation
    $global:LASTEXITCODE = 0
    return
}

# What to install: the release this control plane runs, and that release's Windows bundle must be published.
$release = Invoke-Cp '/api/agent/release-desired'
$version = ''
if ($release.Code -ne 200) {
    Add-Problem "a reachable control plane ($JaahCp answered $($release.Code))" "check this PC's tailnet reaches the control plane, then run the command again"
} else {
    try { $version = [string](ConvertFrom-Json $release.Body).version } catch { $version = '' }
    if ($version -cnotmatch '^[0-9][A-Za-z0-9.+-]*$') { $version = '' }
    if (-not $version) { Add-Problem 'a published release' 'the control plane has no node release to install yet - try again after its next deploy' }
}
$archive = ''
$expected = ''
if ($version) {
    $archive = "jaah-$version-$JaahPlatform.zip"
    # Only the small checksum here: it proves the bundle is published without downloading it yet.
    if (Save-Dist "$archive.sha256" (Join-Path $work "$archive.sha256") 30) {
        $expected = ([IO.File]::ReadAllText((Join-Path $work "$archive.sha256")).Trim() -split '\s+')[0]
    }
    if ($expected -cnotmatch '^[0-9a-f]{64}$') {
        Add-Problem "the Windows bundle for release $version" "the control plane has not published $archive yet - try again in a few minutes"
    }
}
Assert-Preflight

# -- 2. the accounts ----------------------------------------------------------------------------------
Remove-Item -LiteralPath (Join-Path $JaahEtc 'installed') -Force -ErrorAction SilentlyContinue
Write-Step "creating the runner account $JaahRunnerUser and the daemon account NT SERVICE\$JaahDaemonService"
# A fresh random password on every install: nobody signs in as jaah, and only the Service Control Manager
# keeps it. GUIDs are drawn from the system's secure random source. Never the account name inside it: the
# complexity policy refuses a password containing it (measured on Windows Server 2025).
$password = 'Svc-' + [guid]::NewGuid().ToString('N') + [guid]::NewGuid().ToString('N').ToUpperInvariant() + '!'
$secret = ConvertTo-SecureString $password -AsPlainText -Force
$password = $null
if ($runner) {
    Enable-LocalUser -Name $JaahRunnerUser
    Set-LocalUser -Name $JaahRunnerUser -Password $secret -PasswordNeverExpires $true -UserMayChangePassword $false -AccountNeverExpires
} else {
    New-LocalUser -Name $JaahRunnerUser -Password $secret -Description 'Jaah runners (service account, no sign-in)' -PasswordNeverExpires -UserMayNotChangePassword -AccountNeverExpires | Out-Null
}
$runnerSid = (Get-LocalUser -Name $JaahRunnerUser).SID.Value
# Users: what lets it read C:\Program Files. Never Administrators (preflight refused that).
Add-LocalGroupMember -SID $SidUsers -Member $JaahRunnerUser -ErrorAction SilentlyContinue
Invoke-Native 'setting the home folder of the runner account' { & (Join-Path $System32 'net.exe') user $JaahRunnerUser "/homedir:$JaahHome" }
# Off the sign-in screen.
$userList = 'HKLM:\SOFTWARE\Microsoft\Windows NT\CurrentVersion\Winlogon\SpecialAccounts\UserList'
if (-not (Test-Path -LiteralPath $userList)) { New-Item -Path $userList -Force | Out-Null }
New-ItemProperty -Path $userList -Name $JaahRunnerUser -Value 0 -PropertyType DWord -Force | Out-Null
# "Log on as a service", which the Service Control Manager needs to start jaah-runner as it. Added to the
# current holders, never replacing them.
$rights = Join-Path $work 'rights'
New-Item -ItemType Directory -Path $rights | Out-Null
Invoke-Native 'reading the local user rights' { & (Join-Path $System32 'secedit.exe') /export /cfg (Join-Path $rights 'export.inf') /areas USER_RIGHTS }
$holderLine = @(Get-Content -LiteralPath (Join-Path $rights 'export.inf') | Where-Object { $_ -match '^SeServiceLogonRight\s*=' }) | Select-Object -First 1
$holders = @()
if ($holderLine) { $holders = @(($holderLine -split '=', 2)[1].Split(',') | ForEach-Object { $_.Trim() } | Where-Object { $_ }) }
if ($holders -notcontains "*$runnerSid") {
    $policy = @('[Unicode]', 'Unicode=yes', '[Version]', 'signature="$CHICAGO$"', 'Revision=1', '[Privilege Rights]', ('SeServiceLogonRight = ' + (($holders + @("*$runnerSid")) -join ',')))
    Set-Content -LiteralPath (Join-Path $rights 'rights.inf') -Value $policy -Encoding Unicode
    Invoke-Native 'granting the runner account "log on as a service"' { & (Join-Path $System32 'secedit.exe') /configure /db (Join-Path $rights 'rights.sdb') /cfg (Join-Path $rights 'rights.inf') /areas USER_RIGHTS /quiet }
}

# The services are registered now (configured at 6) - registering jaahd is what makes its virtual account,
# NT SERVICE\jaahd, exist for the access lists below. Each runs the service host in `current`.
$hostImage = '"' + (Join-Path $JaahRoot 'current\bin\jaah-service.exe') + '"'
function Register-JaahService([string]$Name, [string]$Display, [string]$Description) {
    $existing = Get-CimInstance Win32_Service -Filter "Name='$Name'"
    if ($existing) {
        Stop-Service -Name $Name -Force -ErrorAction SilentlyContinue
        $changed = Invoke-CimMethod -InputObject $existing -MethodName Change -Arguments @{ PathName = "$hostImage $Name"; DisplayName = $Display }
        if ($changed.ReturnValue -ne 0) { Stop-Install "could not update the service $Name (error $($changed.ReturnValue))" }
    } else {
        New-Service -Name $Name -BinaryPathName "$hostImage $Name" -DisplayName $Display -Description $Description -StartupType Manual | Out-Null
    }
}
Register-JaahService $JaahDaemonService 'Jaah node daemon' 'Connects this PC to the Jaah control plane and runs its tasks.'
Invoke-Native "running $JaahDaemonService as its own virtual account" { & (Join-Path $System32 'sc.exe') config $JaahDaemonService obj= "NT SERVICE\$JaahDaemonService" }
# Its service SID in its token: what the broker pipe's access list names (host_platform/windows.py).
Invoke-Native "giving $JaahDaemonService its service SID" { & (Join-Path $System32 'sc.exe') sidtype $JaahDaemonService unrestricted }
$daemonSid = Get-ServiceSid "NT SERVICE\$JaahDaemonService"
# Git's MSYS runtime cannot name a service SID: inside the daemon's token it is uid 4294967295
# ("Unknown+User"), while the runner's tmux server resolves the granted `NT SERVICE+jaahd` to another uid,
# so the server refused every daemon client with "access not allowed" (measured on a real PC).
# One line in Git's own passwd (nsswitch reads `files` first) gives both sides the same uid: MSYS's
# natural mapping of a service SID, 0x50000 plus the low 16 bits of its last RID. Rewritten on every
# install run; jaah-uninstall removes it.
$msysPasswd = Join-Path (Split-Path (Split-Path $gitBash -Parent) -Parent) 'etc\passwd'
$daemonUid = 0x50000 + ([uint32]($daemonSid.Split('-')[-1]) -band 0xFFFF)
$daemonMsysName = "NT SERVICE+$JaahDaemonService"
$passwdKept = @()
if (Test-Path -LiteralPath $msysPasswd) {
    $passwdKept = @([IO.File]::ReadAllLines($msysPasswd) | Where-Object { $_ -and -not $_.StartsWith("${daemonMsysName}:", [StringComparison]::Ordinal) })
}
$passwdLine = "${daemonMsysName}:*:${daemonUid}:${daemonUid}:U-NT SERVICE\$JaahDaemonService,${daemonSid}:/:/bin/sh"
[IO.File]::WriteAllText($msysPasswd, ((@($passwdKept) + $passwdLine) -join "`n") + "`n", [Text.Encoding]::ASCII)

New-Item -ItemType Directory -Path $JaahRoot -Force | Out-Null
New-Item -ItemType Directory -Path (Join-Path $JaahRoot 'versions') -Force | Out-Null
$JaahEngineDir = Join-Path $JaahData 'engine'
foreach ($dir in @($JaahData, $JaahHome, (Join-Path $JaahData 'state'), (Join-Path $JaahData 'state\requests'), (Join-Path $JaahData 'run'), $JaahEtc, $JaahLog, $JaahEngineDir)) {
    New-Item -ItemType Directory -Path $dir -Force | Out-Null
}
$full = @("*${SidSystem}:(OI)(CI)F", "*${SidAdmins}:(OI)(CI)F")
# The root: traverse only, for the two accounts.
Set-Access $JaahData ($full + @("*${daemonSid}:(RX)", "*${runnerSid}:(RX)"))
Set-Access $JaahHome ($full + @("*${runnerSid}:(OI)(CI)M", "*${daemonSid}:(OI)(CI)M"))
Set-Access (Join-Path $JaahData 'state') ($full + @("*${daemonSid}:(OI)(CI)M"))
Set-Access (Join-Path $JaahData 'run') ($full + @("*${daemonSid}:(OI)(CI)M", "*${runnerSid}:(OI)(CI)M"))
# etc: node.env inherits this list, so it is readable by SYSTEM, Administrators and the daemon only - and
# so is every file `jaah auth login` swaps into place here.
Set-Access $JaahEtc ($full + @("*${daemonSid}:(OI)(CI)RX", "*${runnerSid}:(RX)"))
Set-Access $JaahLog ($full + @("*${daemonSid}:(RX)", "*${runnerSid}:(RX)"))
# engine: the jaah-engine distro's disk, the runner account's alone (the daemon reaches the engine through
# its pipe, never its files).
Set-Access $JaahEngineDir ($full + @("*${runnerSid}:(OI)(CI)M"))

# -- 3. the tailnet -----------------------------------------------------------------------------------
# Already joined (preflight proved it, or phase 1b joined it). Unattended mode keeps it connected
# after a reboot with nobody signed in; routes are what reach the control plane's internal ALB. `set`,
# never a second `up`, which can re-register the device.
Write-Step 'turning on Tailscale unattended mode'
Invoke-Native 'turning on Tailscale unattended mode (run: tailscale up --unattended)' { & $tailscale set --unattended --accept-routes }

# -- 4. the bundle ------------------------------------------------------------------------------------
# A rerun after a run that stopped part-way: nothing may still run from the release about to be replaced.
foreach ($name in @($JaahRunnerService, $JaahUpdateService, $JaahDaemonService, $JaahEngineService)) {
    if (Get-Service -Name $name -ErrorAction SilentlyContinue) { Stop-Service -Name $name -Force -ErrorAction SilentlyContinue }
}
Write-Step "downloading $archive"
$zip = Join-Path $work $archive
if (-not (Save-Dist $archive $zip 900)) { Stop-Install "could not download $archive" }
$actual = (Get-FileHash -LiteralPath $zip -Algorithm SHA256).Hash.ToLowerInvariant()
if ($actual -ne $expected) { Stop-Install "$archive does not match its published sha256 - refusing it" }
# Extracted beside versions\ (same volume, so the move is a rename), never into the live tree.
$incoming = Join-Path $JaahRoot ".incoming-$version"
if (Test-Path -LiteralPath $incoming) { Remove-Item -LiteralPath $incoming -Recurse -Force }
New-Item -ItemType Directory -Path $incoming | Out-Null
Invoke-Native "extracting $archive" { & (Join-Path $System32 'tar.exe') -xf $zip -C $incoming }
$staged = Join-Path $incoming "versions\$version"
if (-not (Test-Path -LiteralPath (Join-Path $staged 'runtime\python\python.exe'))) { Stop-Install "$archive is not a node bundle" }
# The service host: the bundle is built on Linux and carries its source; this PC's compiler builds it.
$hostExe = Join-Path $staged 'bin\jaah-service.exe'
Invoke-Native 'compiling the service host' { & $csc /nologo /optimize+ /target:exe "/out:$hostExe" /reference:System.ServiceProcess.dll (Join-Path $staged 'libexec\JaahService.cs') }
$target = Join-Path $JaahRoot "versions\$version"
if (Test-Path -LiteralPath $target) { Remove-Item -LiteralPath $target -Recurse -Force }
Move-Item -LiteralPath $staged -Destination $target
Remove-Item -LiteralPath $incoming -Recurse -Force
# A junction, removed as a link: Windows PowerShell's recursive delete would follow it into the release.
$current = Join-Path $JaahRoot 'current'
if (Test-Path -LiteralPath $current) { [IO.Directory]::Delete($current, $false) }
New-Item -ItemType Junction -Path $current -Target $target | Out-Null

# -- 5. config ----------------------------------------------------------------------------------------
Write-Step "writing $JaahEtc"
Write-Ascii $JaahEnvFile @(
@@ENV_LINES@@
)
# jaahd.env is the knobs, never a credential: the runner account may read it.
Invoke-Native 'letting the runner account read jaahd.env' { & (Join-Path $System32 'icacls.exe') $JaahEnvFile /grant "*${runnerSid}:(R)" /q }
# Identity now, the credential at step 7. Inherits etc's access list.
Write-Ascii $JaahNodeEnv @("NODE_ID=$JaahNodeId")

# -- 6. the services ----------------------------------------------------------------------------------
Write-Step 'installing the services'
# Daemon output names tasks, accounts and repos, and the runner account runs untrusted agent code: each
# log is writable by its own service only.
foreach ($log in @('jaahd.log', 'runner.log', 'update.log', 'engine.log')) {
    $path = Join-Path $JaahLog $log
    if (-not (Test-Path -LiteralPath $path)) { [IO.File]::WriteAllText($path, '') }
}
Invoke-Native 'opening jaahd.log to the daemon' { & (Join-Path $System32 'icacls.exe') (Join-Path $JaahLog 'jaahd.log') /grant "*${daemonSid}:(M)" /q }
Invoke-Native 'opening runner.log to the runner account' { & (Join-Path $System32 'icacls.exe') (Join-Path $JaahLog 'runner.log') /grant "*${runnerSid}:(M)" /q }
Invoke-Native 'opening engine.log to the runner account' { & (Join-Path $System32 'icacls.exe') (Join-Path $JaahLog 'engine.log') /grant "*${runnerSid}:(M)" /q }
# The daemon ships the updater's and the runner's logs to the control plane (`apps/jaahd/node_logs.py`): read only.
foreach ($log in @('update.log', 'runner.log')) {
    Invoke-Native "letting the daemon read $log" { & (Join-Path $System32 'icacls.exe') (Join-Path $JaahLog $log) /grant "*${daemonSid}:(R)" /q }
}

Register-JaahService $JaahRunnerService 'Jaah runners' 'Runs the Jaah agent sessions on this PC, as the jaah account.'
$runnerService = Get-CimInstance Win32_Service -Filter "Name='$JaahRunnerService'"
Register-JaahService $JaahEngineService 'Jaah container engine' 'Runs the jaah-engine WSL 2 distro (dockerd) for the Jaah runners, as the jaah account.'
$engineService = Get-CimInstance Win32_Service -Filter "Name='$JaahEngineService'"
$plain = (New-Object Management.Automation.PSCredential('x', $secret)).GetNetworkCredential().Password
$changed = Invoke-CimMethod -InputObject $runnerService -MethodName Change -Arguments @{ StartName = ".\$JaahRunnerUser"; StartPassword = $plain }
$changedEngine = Invoke-CimMethod -InputObject $engineService -MethodName Change -Arguments @{ StartName = ".\$JaahRunnerUser"; StartPassword = $plain }
$plain = $null
if ($changed.ReturnValue -ne 0) { Stop-Install "could not set the account of $JaahRunnerService (error $($changed.ReturnValue))" }
if ($changedEngine.ReturnValue -ne 0) { Stop-Install "could not set the account of $JaahEngineService (error $($changedEngine.ReturnValue))" }
Register-JaahService $JaahUpdateService 'Jaah node updater' 'Installs new Jaah node releases when autoupdate is on.'
$updateService = Get-CimInstance Win32_Service -Filter "Name='$JaahUpdateService'"
$changed = Invoke-CimMethod -InputObject $updateService -MethodName Change -Arguments @{ StartName = 'LocalSystem'; StartPassword = '' }
if ($changed.ReturnValue -ne 0) { Stop-Install "could not set the account of $JaahUpdateService (error $($changed.ReturnValue))" }

foreach ($name in @($JaahEngineService, $JaahDaemonService, $JaahRunnerService, $JaahUpdateService)) {
    # Started at boot with nobody signed in; delayed, so the network and Tailscale are up first.
    Invoke-Native "starting $name at boot" { & (Join-Path $System32 'sc.exe') config $name start= delayed-auto }
    # Restarted after a failure. failureflag 1: a stop with a nonzero exit code counts as one; the daemon's
    # exit 0 on a revoked token is reported as a clean stop, which these never restart.
    Invoke-Native "restarting $name after a failure" { & (Join-Path $System32 'sc.exe') failure $name reset= 86400 actions= restart/5000/restart/10000/restart/60000 }
    Invoke-Native "counting a nonzero exit of $name as a failure" { & (Join-Path $System32 'sc.exe') failureflag $name 1 }
}
# jaahd starts at 7, once it has a credential. The engine's readiness is a fresh run\engine-ready.
Remove-Item -LiteralPath (Join-Path $JaahData 'run\engine-ready') -Force -ErrorAction SilentlyContinue
Start-Service -Name $JaahEngineService
Start-Service -Name $JaahRunnerService
Start-Service -Name $JaahUpdateService
Write-Ascii (Join-Path $JaahEtc 'installed') @($version)

# -- 7. -----------------------------------------------------------------------------------------------
Register-AndCheck
# -- 8. -----------------------------------------------------------------------------------------------
Invoke-CredentialRotation
$global:LASTEXITCODE = 0
} catch {
    $global:LASTEXITCODE = 1
    $reason = [string]$_.Exception.Message
    if ($reason -eq 'jaah-restart') {
        Write-Host '==> [jaah] WSL 2 is on, and Windows needs a restart to finish it. Restart this PC, then run the same command again (copy it from the console).' -ForegroundColor Yellow
    } elseif ($reason -ne 'jaah-refused') {
        if ($reason.StartsWith('jaah-stop: ')) { $reason = $reason.Substring(11) }
        Write-Host "==> [jaah] the install stopped: $reason" -ForegroundColor Red
        Write-Host '==> [jaah] fix that and run the same command again - it picks up where this run stopped.' -ForegroundColor Red
    }
} finally {
# -- FINAL: the PowerShell history keeps no line holding the token -----------------------------------
    # PSReadLine wrote the pasted command (it holds the token) to its history file before this ran, and
    # PSReadLine 2.0 (the version Windows ships) has no filter for sensitive lines. Run whatever happened.
    $history = $null
    try { $history = (Get-PSReadLineOption).HistorySavePath } catch { $history = $null }
    if ($history -and (Test-Path -LiteralPath $history)) {
        try {
            $lines = [IO.File]::ReadAllLines($history)
            $clean = @($lines | Where-Object { $_.IndexOf($JaahNodeToken, [StringComparison]::Ordinal) -lt 0 })
            if ($clean.Count -ne $lines.Count) { [IO.File]::WriteAllLines($history, [string[]]$clean) }
        } catch {
            Write-Host "==> [jaah] warning: could not clean $history - delete the line holding the command yourself" -ForegroundColor Yellow
        }
    }
    foreach ($name in $envNames) { [Environment]::SetEnvironmentVariable($name, $envSaved[$name], 'Process') }
    Set-ExecutionPolicy -Scope Process -ExecutionPolicy $policySaved -Force -ErrorAction SilentlyContinue
    # The pasted line left its header table, token inside, in the session (`render_fetch_command`).
    $pasted = Get-Variable -Name h -Scope Global -ValueOnly -ErrorAction SilentlyContinue
    if ($pasted -is [hashtable] -and $pasted['X-Agent-Token'] -ceq $JaahNodeToken) { Remove-Variable -Name h -Scope Global -Force }
    # PSReadLine's in-memory list too, so the arrow keys cannot bring the pasted line back.
    try { [Microsoft.PowerShell.PSConsoleReadLine]::ClearHistory() } catch { }
    if ($work) { Remove-Item -LiteralPath $work -Recurse -Force -ErrorAction SilentlyContinue }
    $JaahNodeToken = $null
}
}
"""


def render_windows_body(inputs: BootstrapInputs) -> str:
    """The installer served to a Windows node that fetched its bootstrap (`irm … | iex`); see the template.

    The one-liner is fetched over the public console, but `cp_base_url` is the tailnet-only machine surface,
    so a PC on no tailnet joins in phase 1b with the key `routes/agent_bootstrap.py` minted for this fetch
    (as `render_macos_body`). Without a key a PC off the tailnet is refused in preflight."""
    _validate(inputs)
    parts = urlsplit(inputs.cp_base_url)
    base = f"{parts.scheme}://{parts.netloc}"  # rebuilt from validated parts, as `_body` does
    engine, daemon, runner, update = WINDOWS_SERVICES
    env_lines = [
        f"JAAH_HOME={WINDOWS_JAAH_HOME}",
        f"JAAH_CP_BASE_URL={base}",
        # The MSYS spelling: its only reader is tmux's `server-access`, which knows no `NT SERVICE\\` name
        # (`host_platform.windows.DAEMON_MSYS_USER`; measured "unknown user" on a real PC).
        f"JAAH_DAEMON_USER=NT SERVICE+{daemon}",
        f"JAAH_SESSION_USER={WINDOWS_RUNNER_USER}",
        f"JAAH_UPGRADE_MARKER={WINDOWS_UPGRADE_MARKER}",
        # The Ubuntu body's rule: no line at all when no hash was carried.
        *(
            [f"JAAH_REMOTE_SETTINGS_CONSENT_HASH={inputs.remote_settings_consent_hash}"]
            if inputs.remote_settings_consent_hash
            else []
        ),
        f"JAAH_RUNNER_SESSION={inputs.node_name}",
    ]
    slots = {
        "TOKEN": _powershell_quote(inputs.node_token),
        "CP": _powershell_quote(base),
        "NODE_ID": _powershell_quote(inputs.node_id),
        "ROOT": WINDOWS_ROOT,
        "DATA": WINDOWS_DATA,
        "HOME": WINDOWS_JAAH_HOME,
        "PLATFORM": WINDOWS_PLATFORM,
        "RUNNER_USER": WINDOWS_RUNNER_USER,
        "NODEJS": WINDOWS_NODEJS,
        "ENGINE_SERVICE": engine,
        "DAEMON_SERVICE": daemon,
        "RUNNER_SERVICE": runner,
        "UPDATE_SERVICE": update,
        "MIN_BUILD": str(WINDOWS_MIN_BUILD),
        "MIN_FREE_GB": str(WINDOWS_MIN_FREE_GB),
        "NODE_NAME": inputs.node_name,
        "TS_KEY": _powershell_quote(inputs.tailscale_auth_key or ""),
        "TS_LOGIN_SERVER": _powershell_quote(inputs.tailscale_login_server or ""),
        "TS_SERVER": _powershell_quote(_server_key(inputs.tailscale_login_server or "")),
        "DIST": DIST_PATH,
        "ENV_LINES": ",\n".join(f"    {_powershell_quote(line)}" for line in env_lines),
        **_installer_slots(),
    }
    script = _WINDOWS_TEMPLATE
    for name, value in slots.items():
        script = script.replace(f"@@{name}@@", value)
    return script


def _installer_slots() -> dict[str, str]:
    """The pinned vendor installers' slots, shared by the installer and the cloud prep."""
    return {
        "GIT_URL": WINDOWS_GIT_SETUP.url, "GIT_SHA256": WINDOWS_GIT_SETUP.sha256,
        "GIT_THUMBPRINT": WINDOWS_GIT_SETUP.thumbprint,
        "TS_URL": WINDOWS_TAILSCALE_SETUP.url, "TS_SHA256": WINDOWS_TAILSCALE_SETUP.sha256,
        "TS_THUMBPRINT": WINDOWS_TAILSCALE_SETUP.thumbprint,
    }


def _powershell_quote(value: str) -> str:
    """One PowerShell single-quoted literal: nothing inside expands, and a quote is escaped by doubling —
    every character PowerShell reads as one, `'` and the typographic U+2018–U+201B alike."""
    return "'" + _POWERSHELL_QUOTES.sub(lambda match: match.group(0) * 2, value) + "'"


#: What ends a PowerShell single-quoted string: the ASCII quote and four Unicode ones (`_powershell_quote`).
_POWERSHELL_QUOTES = re.compile("['\u2018\u2019\u201a\u201b]")


#: The bootstrap body for each OS that is not served by `cloud_node_launch.self_hosted_bootstrap` (the
#: Ubuntu script). `routes/agent_bootstrap.py` dispatches on the row's declared `os` through this.
NATIVE_BODIES = {"macos": render_macos_body, "windows": render_windows_body}

#: Which shell each OS's one-liner is pasted into, served beside it (`onboarding_shell`).
FETCH_SHELLS = {"ubuntu": "bash", "macos": "bash", "windows": "powershell"}


# The one-liner's fetch bounds, in seconds (`render_fetch_command`).
FETCH_CONNECT_TIMEOUT = 20
FETCH_MAX_TIME = 300


def render_fetch_command(*, node_token: str, cp_base_url: str, os: str = "ubuntu") -> str:
    """The whole of what an operator sees — one line that turns a bare machine of `os` into this node.

    The single home of all three one-liners. Each fetches the script over the node's own token and
    pipes it into the platform's shell. The token is both argument and identity, and must ride the
    request: the CP stores only its sha256, so the script's token line can only be filled from what the
    node presents.

    Ubuntu and macOS: `curl … | sudo bash`, the same line (macOS ships both). The leading space keeps it
    out of bash history under `ignorespace` (zsh ignores `HISTCONTROL`: partial mitigation). Windows:
    `irm … | iex` in PowerShell, the header in a hashtable so the token is one single-quoted literal.

    `require_bare_origin` runs here for every OS: quoting makes words, not safe destinations, and the base
    URL is a free-form operator knob piped into a privileged shell. No redirect is ever
    followed — curl has no `-L` (it re-sends custom headers across redirects, cf.
    `cloud_node_launch._probe_client`) and `irm` gets `-MaximumRedirection 0`. Errors are bare status
    codes (`-f`; `irm` throws), since piping an error body into a shell is worse. Accepted cost: the
    token is briefly on argv / in the session's history.

    BOUNDED, so an address this machine cannot route to fails in seconds with curl's own "Failed to
    connect" instead of hanging silently on the kernel's TCP timeout — the shape of a box on another
    tailnet handed an internal address. `FETCH_CONNECT_TIMEOUT` bounds the connect alone: a
    total cap on `curl | sudo bash` could cut the script mid-line while bash runs a long step and the pipe
    is full. `irm` downloads in full before `iex` runs, so it takes `FETCH_MAX_TIME`, the only bound it has.
    """
    if os not in FETCH_SHELLS:
        raise BootstrapInputError(f"os: unknown node OS {os!r}")
    require_bare_origin(cp_base_url)
    url = f"{cp_base_url.rstrip('/')}{BOOTSTRAP_PATH}"
    if FETCH_SHELLS[os] == "powershell":
        return (
            f"$h=@{{'X-Agent-Token'={_powershell_quote(node_token)}}}; "
            f"irm -UseBasicParsing -MaximumRedirection 0 -TimeoutSec {FETCH_MAX_TIME} -Headers $h "
            f"{_powershell_quote(url)} | iex"
        )
    header = shlex.quote(f"X-Agent-Token: {node_token}")
    return (
        f" curl -fsS --connect-timeout {FETCH_CONNECT_TIMEOUT} -H {header} "
        f"{shlex.quote(url)} | sudo bash"
    )


# ── Windows on EC2: the first-boot prep ───────────────────────────────────────────────────────────────
# A Windows cloud node boots Amazon's bare Windows Server image (`aws.ec2.latest_windows_image`), which has
# none of the installer's prerequisites. This user-data brings them (Tailscale joined with the minted key,
# Git for Windows, WSL 2) and then runs the very one-liner an operator would paste, so the installer
# itself stays the one Windows install path.
#
# EC2Launch v2 runs `<powershell>` as LocalSystem; `<persist>true</persist>` re-runs it on every boot, which
# is what carries the run across WSL's reboot. Every step is therefore idempotent, and a `done` marker
# ends every boot after the installer succeeded. EC2Launch reads user-data raw only, and `ec2._user_data_bytes`
# gzips it past `ec2.USER_DATA_LIMIT` (16 KiB): hence the terse in-script comments, and a size test. The text
# holds the node token and the tailnet key, so — as for the installer — no PowerShell suspicious-content
# signature may appear in it (a test holds the list), and the log names steps only.
# `Invoke-Quiet`: under PowerShell 5.1 with `Stop`, a native command's redirected stderr would throw. msiexec's
# 3010 is "installed, restart pending" — a success under `/norestart`.
_WINDOWS_CLOUD_TEMPLATE = r"""<powershell>
# Jaah cloud node first-boot prep (apps/api/app/node_bootstrap.py). Holds secrets: never log or print them.
& {
$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
$prep = '@@PREP@@'
$log = Join-Path $prep 'prep.log'
$done = Join-Path $prep 'done'
New-Item -ItemType Directory -Force -Path $prep | Out-Null
# SYSTEM and Administrators only, before anything is written (ProgramData lets Users read and create).
& (Join-Path $env:SystemRoot 'System32\icacls.exe') $prep /inheritance:r /grant:r '*S-1-5-18:(OI)(CI)F' '*S-1-5-32-544:(OI)(CI)F' /q | Out-Null
if (Test-Path -LiteralPath $done) { return }
# Steps only, never secrets: the console line is how a boot watch reads progress. Where the cloud's runner does not
# copy output to serial port 1, the line goes to COM1 itself, briefly opened: a moment's clash with another writer
# is tried again, a port nobody may open skips the line.
$serial = $@@SERIAL@@
function Write-Serial([string]$Line) {
    for ($i = 0; $i -lt 5; $i++) {
        $port = $null
        try { $port = New-Object IO.Ports.SerialPort 'COM1', 115200; $port.WriteTimeout = 1000; $port.Open(); $port.Write("$Line`r`n"); return }
        catch { Start-Sleep -Milliseconds 200 }
        finally { if ($port) { $port.Dispose() } }
    }
}
function Write-Log([string]$Text) {
    Add-Content -LiteralPath $log -Value ((Get-Date -Format s) + ' ' + $Text)
    Write-Host "==> [jaah] $Text"
    if ($serial) { Write-Serial "==> [jaah] $Text" }
}
function Invoke-Quiet([scriptblock]$Command) {
    $ErrorActionPreference = 'Continue'
    & $Command *> $null
    return $LASTEXITCODE
}
function Get-Signed([string]$Url, [string]$Name, [string]$Signer, [string]$Sha256, [string]$Thumbprint) {
    $file = Join-Path $env:TEMP $Name
    Invoke-WebRequest -UseBasicParsing -Uri $Url -OutFile $file
    $sig = Get-AuthenticodeSignature -FilePath $file
    if ($sig.Status -ne 'Valid' -or $sig.SignerCertificate.GetNameInfo('SimpleName', $false) -cne $Signer) { throw "$Name is not signed by $Signer" }
    if ($sig.SignerCertificate.Thumbprint -ne $Thumbprint) { throw "$Name is not signed by the pinned certificate" }
    if ((Get-FileHash -LiteralPath $file -Algorithm SHA256).Hash -ne $Sha256) { throw "$Name is not the pinned release" }
    return $file
}
$step = 'start'
try {
    $step = 'tailscale'
    $ts = Join-Path $env:ProgramFiles 'Tailscale\tailscale.exe'
    if (-not (Test-Path -LiteralPath $ts)) {
        Write-Log $step
        $msi = Get-Signed '@@TS_URL@@' 'tailscale.msi' 'Tailscale Inc.' '@@TS_SHA256@@' '@@TS_THUMBPRINT@@'
        $p = Start-Process msiexec.exe -ArgumentList @('/i', "`"$msi`"", '/quiet', '/norestart', 'TS_UNATTENDEDMODE=always') -Wait -PassThru
        if (@(0, 3010) -notcontains $p.ExitCode) { throw "msiexec exit $($p.ExitCode)" }
    }
    $step = 'tailnet join'
    for ($i = 0; $i -lt 30 -and (Get-Service Tailscale -ErrorAction SilentlyContinue).Status -ne 'Running'; $i++) { Start-Sleep 2 }
    # After WSL's reboot the backend reconnects a moment after its service runs: never re-join on that moment.
    $joined = $false
    for ($i = 0; $i -lt 30 -and -not $joined; $i++) {
        $joined = (Invoke-Quiet { & $ts status }) -eq 0
        if (-not $joined -and (Test-Path -LiteralPath (Join-Path $prep 'joined'))) { Start-Sleep 2 } else { break }
    }
    if (-not $joined) {
        Write-Log $step
@@KEY_LINES@@
        if ((Invoke-Quiet { @@JOIN@@ }) -ne 0) { throw 'tailscale up failed' }
        Set-Content -LiteralPath (Join-Path $prep 'joined') -Value 'joined'
    }
    $step = 'git'
    $gitKey = Get-ItemProperty 'HKLM:\SOFTWARE\GitForWindows' -ErrorAction SilentlyContinue
    $gitBash = @((Join-Path $env:ProgramFiles 'Git\bin\bash.exe'))
    if ($gitKey -and $gitKey.InstallPath) { $gitBash += Join-Path $gitKey.InstallPath 'bin\bash.exe' }
    if (-not @($gitBash | Where-Object { Test-Path -LiteralPath $_ })) {
        Write-Log $step
        $exe = Get-Signed '@@GIT_URL@@' 'git-setup.exe' 'Johannes Schindelin' '@@GIT_SHA256@@' '@@GIT_THUMBPRINT@@'
        $p = Start-Process $exe -ArgumentList '/VERYSILENT /NORESTART /NOCANCEL /SP- /SUPPRESSMSGBOXES /ALLUSERS' -Wait -PassThru
        if ($p.ExitCode -ne 0) { throw "Git setup exit $($p.ExitCode)" }
    }
    $step = 'wsl'
    $wsl = Join-Path $env:SystemRoot 'System32\wsl.exe'
    $on = (Get-WindowsOptionalFeature -Online -FeatureName VirtualMachinePlatform).State -eq 'Enabled'
    $answers = (Test-Path -LiteralPath $wsl) -and (((Invoke-Quiet { & $wsl --version }) -eq 0) -or ((Invoke-Quiet { & $wsl --status }) -eq 0))
    if (-not ($on -and $answers)) {
        $countFile = Join-Path $prep 'reboots'
        $reboots = 0
        if (Test-Path -LiteralPath $countFile) { $reboots = [int](Get-Content -LiteralPath $countFile -Raw) }
        if ($reboots -lt @@MAX_REBOOTS@@) {
            Write-Log "$step, reboot $($reboots + 1)"
            Enable-WindowsOptionalFeature -Online -FeatureName VirtualMachinePlatform, Microsoft-Windows-Subsystem-Linux -All -NoRestart | Out-Null
            if (Test-Path -LiteralPath $wsl) { Invoke-Quiet { & $wsl --install --no-distribution } | Out-Null }
            Set-Content -LiteralPath $countFile -Value ($reboots + 1)
            Restart-Computer -Force
            return
        }
        Write-Log "$step not ready after $reboots reboots; the installer names what is missing"
    }
    $step = 'imds'
    # Runner code shares this host's network (no vnode egress rules on Windows), so the instance metadata
    # service (the role's credentials, and this user-data with the node token) is closed to every account,
    # and to the WSL VM. EC2Launch has read the user-data already; a later boot's re-read failing is harmless.
    if (-not (Get-NetFirewallRule -Name 'jaah-no-imds' -ErrorAction SilentlyContinue)) {
        Write-Log $step
        New-NetFirewallRule -Name 'jaah-no-imds' -DisplayName 'Jaah: no instance metadata' -Direction Outbound -Action Block -RemoteAddress @('169.254.169.254', 'fd00:ec2::254') -Profile Any | Out-Null
    }
    if ((Get-Command New-NetFirewallHyperVRule -ErrorAction SilentlyContinue) -and -not (Get-NetFirewallHyperVRule -Name 'jaah-no-imds-wsl' -ErrorAction SilentlyContinue)) {
        New-NetFirewallHyperVRule -Name 'jaah-no-imds-wsl' -DisplayName 'Jaah: WSL no instance metadata' -Direction Outbound -Action Block -VMCreatorId '{40E0AC32-46A5-438A-A0B2-2B479E8F2E90}' -RemoteAddresses '169.254.169.254' | Out-Null
    }
    $step = 'installer'
    for ($try = 1; $try -le @@FETCH_TRIES@@; $try++) {
        Write-Log "$step, try $try"
        $global:LASTEXITCODE = 1
        try {
            @@FETCH@@
        } catch {
            Write-Log "$step fetch retry"
        }
        if ($LASTEXITCODE -eq 0) { break }
        if ($try -lt @@FETCH_TRIES@@) { Start-Sleep 30 }
    }
    if ($LASTEXITCODE -ne 0) { throw 'the installer stopped' }
    Set-Content -LiteralPath $done -Value 'done'
    Write-Log 'done'
} catch {
    Write-Log "$step failed"
}
}
</powershell>
<persist>true</persist>
"""

#: WSL's reboots the prep may ask for; past them it runs the installer anyway, whose preflight then names
#: what is still missing instead of the machine rebooting forever.
WINDOWS_CLOUD_MAX_REBOOTS = 2
#: Installer runs the prep tries, 30 s apart: the route to the control plane arrives with the netmap a moment
#: after `tailscale up` returns, and no later boot comes to retry a fetch that met no route. The installer
#: resumes where a stopped run left off, so a retry is safe.
WINDOWS_CLOUD_FETCH_TRIES = 5
#: Where the prep keeps its markers (`done`, the reboot count) and its step log.
WINDOWS_CLOUD_PREP = f"{WINDOWS_DATA}\\cloud-prep"
_WINDOWS_TAILNET_KEY_RE = re.compile(r"[A-Za-z0-9_-]{8,512}")


def windows_tailnet_join_command(inputs: BootstrapInputs) -> str:
    """`tailnet_join_command`'s line as PowerShell, so the join flags keep one home. The bash line reads
    the key and server from `"$JAAH_TS_AUTHKEY"`/`"$JAAH_TS_LOGIN_SERVER"`, which PowerShell expands the
    same way inside double quotes once `_windows_tailnet_key_lines` has assigned them; only the program
    differs (`& $ts`, the full path — the MSI's PATH change is not in this process). Plus `--unattended`
    (Windows only: stay connected with nobody signed in), and no `--ssh`: Tailscale SSH has no Windows
    server."""
    join = tailnet_join_command(
        _powershell_quote(inputs.node_name), login_server=inputs.tailscale_login_server is not None, ssh=False
    )
    return "& $ts " + join.removeprefix("tailscale ") + " --unattended"


def _windows_tailnet_key_lines(inputs: BootstrapInputs, indent: str) -> list[str]:
    """`_tailnet_key_lines` as PowerShell single-quoted literals."""
    lines = [f"{indent}$JAAH_TS_AUTHKEY = {_powershell_quote(inputs.tailscale_auth_key or '')}"]
    if inputs.tailscale_login_server is not None:
        lines.append(f"{indent}$JAAH_TS_LOGIN_SERVER = {_powershell_quote(inputs.tailscale_login_server)}")
    return lines


def render_windows_cloud_user_data(inputs: BootstrapInputs, *, serial: bool = True) -> str:
    """The EC2 user-data for a control-plane-launched Windows node: the first-boot prep above, ending in
    exactly `render_fetch_command(os="windows")`'s line, so the installer is fetched and run as an
    operator's paste would.

    `serial` writes each step line to COM1 too, for a cloud whose script runner keeps output off the serial
    console (EC2Launch v2, Azure's start-up task); False where the runner already copies it there (GCE), so
    `cloud_boot_watch` reads every step once (`CloudProvider.runner_writes_serial`).

    The tailnet key is required, not optional as on Ubuntu: the installer's preflight refuses a PC with no
    connected tailnet, and a cloud machine has nobody to sign it in."""
    _validate(inputs)
    _require(inputs.tailscale_auth_key is not None, "tailscale_auth_key",
             "is required for a Windows cloud node — the installer needs a connected tailnet")
    # Only key characters reach the PowerShell literal: the key is minted by our own tailnet, but it is the one
    # free-form input of this script.
    _require(bool(_WINDOWS_TAILNET_KEY_RE.fullmatch(inputs.tailscale_auth_key or "")), "tailscale_auth_key",
             "must be a pre-auth key (letters, digits, '-' and '_')")
    slots = {
        "PREP": WINDOWS_CLOUD_PREP,
        "KEY_LINES": "\n".join(_windows_tailnet_key_lines(inputs, "        ")),
        "JOIN": windows_tailnet_join_command(inputs),
        "SERIAL": "true" if serial else "false",
        "MAX_REBOOTS": str(WINDOWS_CLOUD_MAX_REBOOTS),
        "FETCH_TRIES": str(WINDOWS_CLOUD_FETCH_TRIES),
        "FETCH": render_fetch_command(node_token=inputs.node_token, cp_base_url=inputs.cp_base_url, os="windows"),
        **_installer_slots(),
    }
    script = _WINDOWS_CLOUD_TEMPLATE
    for name, value in slots.items():
        script = script.replace(f"@@{name}@@", value)
    # EC2Launch reads user-data raw, and `ec2._user_data_bytes` gzips anything past this: refuse it here.
    _require(len(script.encode()) <= USER_DATA_LIMIT, "node_name", "makes the Windows user-data too long")
    return script
