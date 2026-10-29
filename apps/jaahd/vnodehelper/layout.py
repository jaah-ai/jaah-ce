"""Every NAME, NUMBER and PATH of a virtual node, derived from its identity — the single home of the layout.

A caller names a vnode by its opaque `id` and nothing else (no path, uid, username or command); that
holds only if every such value is computed here from the `Vnode` (slot + tag) in the helper's root-only
store. Add no second derivation.

A vnode's TOKEN is its random `tag` (`TAG_PATTERN`) or, for a vnode built before tags (tag None), its slot
as a decimal. EVERY name — user, group, tree, unit, netns, veth — derives from the token, so a NEW node's
names say nothing about how many nodes the host has held; every NUMBER — uid span, subid block, link /31 —
derives from the slot, which the control plane picks at random from the host's never-used ones. A legacy
vnode's values are byte-identical to the pre-tag scheme (`tests/test_vnodehelper_layout.py` pins it).
Per-vnode layout for token `t`, slot `k`:

  users/groups   `vn<t>-jaahd` (daemon), `vn<t>-jaah` (session), `vn<t>-gha<N>` (GHA runners), each
                 with a private primary group of its own name. The daemon and every gha user hold the
                 SUPPLEMENTARY group `vn<t>-jaah` — never `jaah`, `docker`, `jaahd` or a sibling's.
  uids/gids      `600000 + 100*k + {0 daemon, 1 session, 10..19 gha}` — disjoint from `useradd`'s own
                 range below 100000.
  subuid/subgid  `100_000_000 + k*1_048_576`, one 65536-wide span per member — above the 100000-origin
                 region `useradd` and the host's gha pool allocate from.
  durable disk   `/var/lib/jaah-vn/<t>/` and its `daemon/`, `home/`, `shared/accounts`, `tailscale/`.
  tmpfs          `/run/jaah-vn/<t>/` — the vnode's own copy of the host's `/run/jaah` subtree.
  config         `/etc/jaah-vn/<t>/{node.env,jaahd.env,resolv.conf}`.
  network        netns `vn<t>`; veth `vnh<t>` (host) <-> `vnp<t>` (ns) on a /31 of 198.51.100.0/24.

The unit files spell the same paths as text (`%i` = the token); a units test
pins them against the functions below. Stdlib only, no import-time side effects (the boot rebuild imports
it first).
"""
from __future__ import annotations

import ipaddress
import os
import re
from dataclasses import dataclass

# ── the slot ─────────────────────────────────────────────────────────────────────────────────────────
# 1..99 — the CP's `vnodes.MAX_SLOT`: the uid stride gives 100 ids per slot and the /31 links fit 128 in
# a /24, so 99 binds. Allocated once per host and never reused, so every value below is pure in it.
SLOT_MIN = 1
SLOT_MAX = 99

# ── the control plane's id of a vnode ────────────────────────────────────────────────────────────────
# A canonical lower-case UUID: never a path or name, but the store's key, so validated.
VNODE_ID_PATTERN = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\Z")

# ── the helper's own state (durable, root-only) ──────────────────────────────────────────────────────
# Its schema is `store.py`'s.
STATE_ROOT = "/var/lib/jaah/vnodehelper"
STORE_PATH = f"{STATE_ROOT}/store.json"
NODELINK_STAGING_ROOT = f"{STATE_ROOT}/nodelinks"

# ── the host side ────────────────────────────────────────────────────────────────────────────────────
#
# The host jaahd's helper socket, 0660 root:jaahd (no vnode user is in that group). Spelled only
# here: `tests/test_environment_vnode.py`'s literal scan allowlists this module alone.
SOCKET_PATH = "/run/jaah/vnodehelper.sock"
SOCKET_GROUP = "jaahd"
# tmpfs, root 0700: where a tailnet auth key sits for the seconds `tailscale up` takes (`ops._tailscale_up`).
AUTHKEY_DIR = "/run/jaah-vnodehelper"

# Written by the host bootstrap (`apps/api/app/node_bootstrap.py::VNODE_HOST_MARKER`); every vnode unit
# carries `ConditionPathExists=` on it and postinst enables them only when it exists.
HOST_MARKER = "/etc/jaah/vnode-host"

# The host's own daemon config, read ONLY for the control-plane endpoint keys a vnode inherits.
HOST_JAAHD_ENV = "/etc/jaah/jaahd.env"

# ── the per-vnode trees ──────────────────────────────────────────────────────────────────────────────
VN_ROOT = "/var/lib/jaah-vn"
VN_RUN_ROOT = "/run/jaah-vn"
VN_ETC_ROOT = "/etc/jaah-vn"
NETNS_ROOT = "/run/netns"
NETNS_ETC_ROOT = "/etc/netns"
SYSTEMD_ETC = "/etc/systemd/system"

# ── ids ──────────────────────────────────────────────────────────────────────────────────────────────
UID_BASE = 600_000
UID_STRIDE = 100
DAEMON_OFFSET = 0
SESSION_OFFSET = 1
GHA_OFFSETS = range(10, 20)

SUBID_BASE = 100_000_000
SUBID_STRIDE = 1_048_576     # 16 spans of 65536 per vnode
SUBID_SPAN = 65_536

# ── the link ─────────────────────────────────────────────────────────────────────────────────────────
# TEST-NET-2, which `rootlib/nft_ruleset.NON_ROUTABLE_DESTINATIONS` already denies, so no vnode can
# reach any vnode's link address.
LINK_CIDR = "198.51.100.0/24"

# The tailnet device name inside each vnode's namespace (`jaah-tailscaled-vn@.service --tun=`).
TAILSCALE_IF = "tailscale0"
# Host paths no vnode unit may see: the host's runtime tree (sockets, tmux), its tailscaled LocalAPI
# (the host's netmap), its config and its shared mount. `/home`/`/root` are hidden separately (see the
# units).
UNIT_INACCESSIBLE = ("/mnt/jaah-shared", "/etc/jaah", "/run/jaah", "/run/tailscale", "/var/run/tailscale")

# cron and at run a job in the HOST's namespaces, outside every unit fence (a security review finding), so each
# vnode principal is listed in both deny files (each tool reads its `.deny` when no `.allow` exists) and
# its spooled crontab and at jobs are removed (`ops.close_schedules`).
SCHEDULE_DENY_FILES = ("/etc/cron.deny", "/etc/at.deny")
# Each tool reads its `.allow` INSTEAD when one exists: a principal listed there would be let back in.
SCHEDULE_ALLOW_FILES = ("/etc/cron.allow", "/etc/at.allow")
CRONTAB_SPOOL = "/var/spool/cron/crontabs"
ATJOB_SPOOL = "/var/spool/cron/atjobs"
# Where a job already started runs: the daemons' own cgroups (their PAM stacks open no logind session).
SCHEDULER_CGROUPS = ("/sys/fs/cgroup/system.slice/cron.service", "/sys/fs/cgroup/system.slice/atd.service")

# The one port a vnode accepts over its tailnet: the direct console, which refuses without a CP grant.
CONSOLE_DIRECT_PORT = 7787
# Every tailnet address (headscale's 100.88.0.0/16 and quad100 lie inside it): a vnode opens no connection
# to any of them (`nft.render_namespace`).
TAILNET_CGNAT = "100.64.0.0/10"


def validate_slot(slot: object) -> int:
    """The slot, or ValueError. `bool` is refused: it is an `int` subclass and `True` is not slot 1."""
    if not isinstance(slot, int) or isinstance(slot, bool) or not SLOT_MIN <= slot <= SLOT_MAX:
        raise ValueError(f"slot must be an integer in {SLOT_MIN}..{SLOT_MAX}")
    return slot


# A NEW vnode's random tag: 8 characters, a letter first so a token never reads as a slot. A veth name is
# `vnh` + token = 11 characters, inside IFNAMSIZ's 15.
TAG_PATTERN = re.compile(r"^[a-z][a-z0-9]{7}\Z")


def validate_tag(tag: object) -> str | None:
    """The tag, None (a legacy vnode), or ValueError."""
    if tag is None:
        return None
    if not isinstance(tag, str) or not TAG_PATTERN.fullmatch(tag):
        raise ValueError("tag must be a letter then 7 lower-case letters or digits")
    return tag


@dataclass(frozen=True)
class Vnode:
    """A vnode's layout identity: the SLOT its numbers derive from and the TAG its names derive from
    (None for a vnode built before tags). Validated on construction, so every function below can trust it."""
    slot: int
    tag: str | None = None

    def __post_init__(self) -> None:
        validate_slot(self.slot)
        validate_tag(self.tag)

    @property
    def token(self) -> str:
        """What every name, path, unit, netns and veth carries — and a template unit's `%i`."""
        return self.tag if self.tag is not None else str(self.slot)


def parse_token(token: object) -> str:
    """A token as it arrives from outside (a unit's `%i`, a directory name): a canonical slot (`1`..`99`,
    no leading zero) or a tag — else ValueError. Says nothing about whether such a vnode exists."""
    if isinstance(token, str):
        if TAG_PATTERN.fullmatch(token):
            return token
        if token.isascii() and token.isdigit() and str(int(token)) == token:
            validate_slot(int(token))
            return token
    raise ValueError("a vnode token is a slot 1..99 or an 8-character tag")


def _token(v: Vnode) -> str:
    # A bare slot is refused rather than read as a legacy vnode: a caller still passing one would name a
    # TAGGED vnode's paths wrongly, and silently.
    if not isinstance(v, Vnode):
        raise TypeError(f"expected a layout.Vnode, got {type(v).__name__}")
    return v.token


def _slot(v: Vnode) -> int:
    if not isinstance(v, Vnode):
        raise TypeError(f"expected a layout.Vnode, got {type(v).__name__}")
    return v.slot


def validate_vnode_id(vnode_id: object) -> str:
    if not isinstance(vnode_id, str) or not VNODE_ID_PATTERN.fullmatch(vnode_id):
        raise ValueError("id must be a canonical lower-case UUID")
    return vnode_id


# ── names ────────────────────────────────────────────────────────────────────────────────────────────

def daemon_user(v: Vnode) -> str:
    return f"vn{_token(v)}-jaahd"


def session_user(v: Vnode) -> str:
    return f"vn{_token(v)}-jaah"


def gha_user(v: Vnode, n: int) -> str:
    if not 0 <= n < len(GHA_OFFSETS):
        raise ValueError(f"gha index must be in 0..{len(GHA_OFFSETS) - 1}")
    return f"vn{_token(v)}-gha{n}"


def daemon_uid(v: Vnode) -> int:
    return UID_BASE + UID_STRIDE * _slot(v) + DAEMON_OFFSET


def session_uid(v: Vnode) -> int:
    return UID_BASE + UID_STRIDE * _slot(v) + SESSION_OFFSET


def gha_uid(v: Vnode, n: int) -> int:
    gha_user(v, n)   # validates n
    return UID_BASE + UID_STRIDE * _slot(v) + GHA_OFFSETS[n]


def uid_span(v: Vnode) -> range:
    """Every uid (and gid — each principal's private group shares its number) vnode `v` may ever use."""
    first = UID_BASE + UID_STRIDE * _slot(v)
    return range(first, first + UID_STRIDE)


def subid_start(v: Vnode, member: int = 0) -> int:
    """The subordinate id range of member `member` of vnode `v`: 0 is the session user, 1+N gha N."""
    if not 0 <= member < SUBID_STRIDE // SUBID_SPAN:
        raise ValueError("member must index one of the vnode's 16 spans")
    return SUBID_BASE + SUBID_STRIDE * _slot(v) + SUBID_SPAN * member


def subid_block(v: Vnode) -> range:
    """The whole subordinate-id block reserved for vnode `v` — what no other owner may overlap."""
    first = SUBID_BASE + SUBID_STRIDE * _slot(v)
    return range(first, first + SUBID_STRIDE)


# ── durable paths ────────────────────────────────────────────────────────────────────────────────────

def vnode_root(v: Vnode) -> str:
    """`0750 root:vn<t>-jaah` — traversable by the vnode's own two principals and nobody else."""
    return f"{VN_ROOT}/{_token(v)}"


def daemon_home(v: Vnode) -> str:
    """The vnode daemon's HOME and WorkingDirectory: `0700 vn<t>-jaahd`."""
    return f"{vnode_root(v)}/daemon"


def session_home(v: Vnode) -> str:
    """The session user's HOME: `0750 vn<t>-jaah:vn<t>-jaah`."""
    return f"{vnode_root(v)}/home"


def jaah_home(v: Vnode) -> str:
    """`JAAH_HOME` — the control plane's `vnodes.jaah_home_for(v)`; a test pins the two."""
    return f"{session_home(v)}/.jaah"


def mirrors_dir(v: Vnode) -> str:
    """The vnode daemon's git mirrors (`git_mirror`, `JAAH_POOL_MIRRORS`): `2750 vn<t>-jaahd:vn<t>-jaah` — the
    only writer is the daemon, which holds the credential; its session user reads it to fetch locally. Not
    under `daemon_home`, which is `0700`."""
    return f"{vnode_root(v)}/mirrors"


def shared_root(v: Vnode) -> str:
    return f"{vnode_root(v)}/shared"


def shared_accounts(v: Vnode) -> str:
    """Where the org's EFS access point is mounted — the control plane's `vnodes.shared_root_for(v)`."""
    return f"{shared_root(v)}/accounts"


def tmp_dir(v: Vnode) -> str:
    """The vnode's `/tmp`: `1770 root:vn<t>-jaah`, bind-mounted over `/tmp` in its daemon, runner and
    user-manager units so all three share ONE /tmp (the daemon stages report and task-input directories
    there for the runner's panes) that no other vnode can reach and that survives a unit restart."""
    return f"{vnode_root(v)}/tmp"


def var_tmp_dir(v: Vnode) -> str:
    """The vnode's `/var/tmp`, as `tmp_dir` is its `/tmp` and for its reasons: `1770 root:vn<t>-jaah`, bound
    over `/var/tmp` in the same units, so a pane's `docker run -v /var/tmp/…` resolves where its dockerd
    looks and no other vnode reads what its tools leave there."""
    return f"{vnode_root(v)}/var-tmp"


def shared_config(v: Vnode) -> str:
    """The vnode's validated EFS access-point triple (non-secret, root 0600), so a boot or a `start` can
    re-mount what `create` mounted — the store's shape is pinned and carries no mount parameters."""
    return f"{etc_dir(v)}/shared.json"


def gha_home(v: Vnode, n: int) -> str:
    """GHA runner `vn<t>-gha<N>`'s HOME (`0750`, its own), under the vnode root it traverses as a member
    of `vn<t>-jaah` — so `vnode.destroy`'s removal of that root takes every runner tree with it."""
    gha_user(v, n)   # validates n
    return f"{vnode_root(v)}/gha{n}"


def gha_tmp(v: Vnode, n: int) -> str:
    """GHA runner `n`'s own /tmp (`0700`, its own) — bound over /tmp in its runner unit AND its user
    manager, so a job's `docker run -v /tmp/…` resolves where its rootless dockerd looks, and never the
    vnode's shared `tmp_dir`."""
    return f"{gha_home(v, n)}/.tmp"


def gha_var_tmp(v: Vnode, n: int) -> str:
    """GHA runner `n`'s own /var/tmp (`0700`, its own), for `gha_tmp`'s reasons."""
    return f"{gha_home(v, n)}/.var-tmp"


def gha_hidden(v: Vnode) -> tuple[str, ...]:
    """What a GHA runner user must not reach although it holds `vn<t>-jaah`: the vnode's session home
    (its JAAH_HOME, worktrees and accounts), its shared-accounts mount, its runtime tree (tmux, creds,
    exec) and its shared /tmp and /var/tmp. Hidden in the runner unit and its user manager."""
    return session_home(v), shared_root(v), run_root(v), tmp_dir(v), var_tmp_dir(v)


def tailscale_state_dir(v: Vnode) -> str:
    return f"{vnode_root(v)}/tailscale"


# ── tmpfs paths ──────────────────────────────────────────────────────────────────────────────────────

def run_root(v: Vnode) -> str:
    """The vnode's `JAAH_RUN_ROOT` — its own copy of the host's `/run/jaah` subtree."""
    return f"{VN_RUN_ROOT}/{_token(v)}"


def tailscale_run_dir(v: Vnode) -> str:
    """`0750 root:vn<t>-jaahd` — the daemon reaches its tailscaled; the session user cannot."""
    return f"{run_root(v)}/tailscale"


def tailscale_socket(v: Vnode) -> str:
    return f"{tailscale_run_dir(v)}/tailscaled.sock"


def upgrade_marker(v: Vnode) -> str:
    """Named so the unit relocates it; a vnode never writes it (D7: the package is host-managed)."""
    return f"{run_root(v)}/requests/upgrade"


# ── config ───────────────────────────────────────────────────────────────────────────────────────────

def etc_dir(v: Vnode) -> str:
    """`0750 root:vn<t>-jaahd` — the vnode daemon's own config, unreadable by its session user."""
    return f"{VN_ETC_ROOT}/{_token(v)}"


def node_env(v: Vnode) -> str:
    return f"{etc_dir(v)}/node.env"


def jaahd_env(v: Vnode) -> str:
    return f"{etc_dir(v)}/jaahd.env"


def nodelinks_cache(v: Vnode) -> str:
    """The vnode's NodeLink cache (architecture 4.1b): `0640 root:vn<t>-jaahd`, written by the host helper
    (`nodelinks.converge`), read by the vnode's own daemon (`shared_storage.nodelinks_cache_path()`)."""
    return f"{etc_dir(v)}/nodelinks.json"


def nodelink_staging_root(v: Vnode) -> str:
    """`0700 root:root` — where the helper mounts each of the vnode's NodeLink access points before binding
    it into the vnode tree (`nodelinks.py`); no vnode principal can even traverse to it."""
    return f"{NODELINK_STAGING_ROOT}/{_token(v)}"


def nodelink_staging(v: Vnode, nodelink_id: str) -> str:
    return f"{nodelink_staging_root(v)}/{nodelink_id}"


# ── network ──────────────────────────────────────────────────────────────────────────────────────────

def netns_name(v: Vnode) -> str:
    return f"vn{_token(v)}"


def netns_path(v: Vnode) -> str:
    return f"{NETNS_ROOT}/{netns_name(v)}"


def netns_etc_dir(v: Vnode) -> str:
    """iproute2's per-netns `/etc` overrides — where a vnode's resolv.conf lived before it moved to
    `resolv_conf`. No longer created; a leftover is removed at start and rebuild, so `/etc/netns` lists
    no vnode and can be hidden from vnode units."""
    return f"{NETNS_ETC_ROOT}/{netns_name(v)}"


def resolv_conf(v: Vnode) -> str:
    """`0644 root` in the vnode's `0750` etc dir: its units bind it over `/etc/resolv.conf` (the mode
    travels with the inode), and nothing else reaches it."""
    return f"{etc_dir(v)}/resolv.conf"


def veth_names(v: Vnode) -> tuple[str, str]:
    """(host side, namespace side)."""
    k = _token(v)
    return f"vnh{k}", f"vnp{k}"


def link_addresses(v: Vnode) -> tuple[str, str]:
    """(host end, namespace end) of vnode `v`'s /31 — slot 1 is .0/.1, slot 99 is .196/.197."""
    net = ipaddress.ip_network(LINK_CIDR)
    base = int(net.network_address) + 2 * (_slot(v) - 1)
    return str(ipaddress.IPv4Address(base)), str(ipaddress.IPv4Address(base + 1))


NFT_TABLE = "jaah_vnodes"
# The `ip rule` priority that routes everything sourced from a vnode link by the MAIN table. Below
# tailscaled's own rules (5210-5270), so the host's tailnet table 52 — which carries every subnet route a
# `--accept-routes` host takes, the VPC's own CIDR included — never picks the wire for vnode traffic.
LINK_RULE_PRIORITY = 5200
# The nft wildcard every host-side veth matches — `vnh<token>` and nothing else on a host.
HOST_IF_GLOB = "vnh*"
# The host table's dynamic set counting each vnode link's tracked connections (`nft.render_host`).
NFT_CONN_SET = "vnode_conns"
# NEW connections one vnode may hold tracked at once, past which its new ones are dropped — so one tenant
# cannot fill the host's conntrack table (and so stall every neighbour's). The CP's `vnode_conntrack_limit`
# setting overrides it (`vnode.limits`); the bounds are what that operation accepts.
DEFAULT_CONNTRACK_LIMIT = 16_384
CONNTRACK_LIMIT_MIN = 256
CONNTRACK_LIMIT_MAX = 1_000_000


# ── the MicroVM grants (R11/N1) ──────────────────────────────────────────────────────────────────
# The device a co-located node's DAEMON reaches beyond its own trees: its OWN kvm device node
# (`kvm_node`, kvm's major:minor, 0600, owned by its DAEMON uid — the uid its `jaah-microvmd@` runs as),
# bound over /dev/kvm in that unit alone (its session user gets a SEPARATE node, and only by opt-in:
# `session_kvm_node`). Never the `kvm` group, which every sibling could join the same way,
# and never an ACL entry on /dev/kvm: on dev the entries vanished between the grant and the next VM start,
# and udev and logind both rewrite that device's ACL — neither touches a node it did not make.
# With it, the shipped virtiofsd: `0750 root:jaahd` by postinst (its AppArmor `userns` grant must not be
# borrowable), so the vnode's supervisor is let execute it by a per-uid ACL entry on that regular file.
# Absent files are skipped: a host without KVM is limited by its hardware only (I6).
KVM_DEVICE = "/dev/kvm"
# `microvmd.virtiofsd_path()` on a packaged node (the microvm pins' `install_to`, physical);
# a units test pins the two spellings together.
MICROVM_VIRTIOFSD = "/usr/lib/jaahd/microvm/virtiofsd"


def kvm_dir() -> str:
    """Where every vnode's kvm node lives: `0700 root`, so no uid but root has a path to any of them (the
    owning daemon reaches its own only as /dev/kvm inside its supervisor) and none can list its neighbours'
    tokens. Beside /dev/kvm, on devtmpfs: gone at reboot, made again by the boot rebuild."""
    return f"{os.path.dirname(KVM_DEVICE)}/jaah-kvm"


def kvm_node(v: Vnode) -> str:
    """Vnode `v`'s own kvm device node. The `jaah-microvmd@` unit binds it over /dev/kvm by the same
    spelling."""
    return f"{kvm_dir()}/vn{_token(v)}"


def session_kvm_node(v: Vnode) -> str:
    """Vnode `v`'s SESSION kvm node — "KVM for sessions" (`ops.ensure_session_kvm_node`). The runner units bind it
    over /dev/kvm by the same spelling."""
    return f"{kvm_node(v)}-session"


def microvm_grants(v: Vnode) -> tuple[tuple[str, str], ...]:
    """(path, setfacl entry) of each MicroVM ACL grant of vnode `v` — its daemon uid only, never a sibling's
    and never its session user's (`ops.ensure_microvm_grants`). /dev/kvm is reached by `kvm_node` instead."""
    return ((MICROVM_VIRTIOFSD, f"u:{daemon_uid(v)}:rx"),)


# ── units ────────────────────────────────────────────────────────────────────────────────────────────

def tailscaled_unit(v: Vnode) -> str:
    return f"jaah-tailscaled-vn@{_token(v)}.service"


def daemon_unit(v: Vnode) -> str:
    return f"jaahd@{_token(v)}.service"


def runner_unit(v: Vnode) -> str:
    return f"jaah-runner-vn@{_token(v)}.service"


def gha_path_unit(v: Vnode) -> str:
    """Watches `<run_root(v)>/requests/gha-runners` (the vnode daemon's GHA converge request,
    `gha_request.marker_path` under `JAAH_RUN_ROOT`) and starts `gha_service_unit(v)` — the vnode twin of
    `jaah-gha.path`."""
    return f"jaah-gha-vn@{_token(v)}.path"


def gha_service_unit(v: Vnode) -> str:
    """The vnode's root GHA runner-pool converge (`jaah-gha-converge --vnode <token>`)."""
    return f"jaah-gha-vn@{_token(v)}.service"


def runner_cli_unit(v: Vnode) -> str:
    """The session user's runner CLI installs (`jaah-runner-cli-vn@.service`)."""
    return f"jaah-runner-cli-vn@{_token(v)}.service"


def microvm_path_unit(v: Vnode) -> str:
    """Watches `<daemon_home(v)>/microvm.enabled` (`microvm_mark.marker_path` under the vnode daemon's HOME)
    and starts `microvm_converge_unit(v)` — the vnode twin of `jaah-microvm.path` (the MicroVM design's D0,
    per co-located node)."""
    return f"jaah-microvm@{_token(v)}.path"


def microvm_converge_unit(v: Vnode) -> str:
    """The vnode's root MicroVM converge: `microvm_net.py converge` inside its netns (tap pool + table)."""
    return f"jaah-microvm-converge@{_token(v)}.service"


def microvmd_unit(v: Vnode) -> str:
    """The vnode's MicroVM supervisor (`microvmd.py serve`), as its daemon user in its netns."""
    return f"jaah-microvmd@{_token(v)}.service"


def units(v: Vnode) -> tuple[str, ...]:
    """Every unit of the vnode, in START order; stopped in reverse. The MicroVM watch BEFORE the daemon:
    `PathChanged=` sees only changes made after the watch exists, and the daemon's first tick rewrites the
    marker. Inert on an unmarked vnode (no marker is ever written), like the host's own watch."""
    return tailscaled_unit(v), microvm_path_unit(v), daemon_unit(v), runner_unit(v), gha_path_unit(v)


def hostname_units(v: Vnode) -> tuple[str, ...]:
    """Every vnode SYSTEM unit whose UTS namespace a per-instance drop-in names after the vnode
    (`hostname.instance_dropin_body`)."""
    return tailscaled_unit(v), daemon_unit(v), runner_unit(v), runner_cli_unit(v), microvmd_unit(v)


def instance_dropin_dir(unit: str) -> str:
    return f"{SYSTEMD_ETC}/{unit}.d"


def instance_dropin(unit: str) -> str:
    """The helper-written drop-in that names `unit`'s UTS namespace — `unit` one of `hostname_units`."""
    return f"{instance_dropin_dir(unit)}/jaah-vnode-hostname.conf"


# Every helper-written drop-in: root-only. systemd reads it as root; its body names the vnode.
DROPIN_MODE = 0o600


def user_dropin_dir(uid: int) -> str:
    return f"{SYSTEMD_ETC}/user@{int(uid)}.service.d"


def user_dropin(uid: int) -> str:
    """The helper-written drop-in that puts a vnode uid's user manager (and so its rootless dockerd and
    every `./dc` port) inside the vnode's netns."""
    return f"{user_dropin_dir(uid)}/jaah-vnode.conf"
