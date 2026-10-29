"""Which operating system a node runs, and what that lets it do — the capability table.

A node's OS is DECLARED when it is created (`nodes.os`, never changed afterwards) and its capabilities are
looked up HERE. This module is the only place a platform decision is made: every consumer (placement, the
GitHub Actions runner pool, Multi-Node Hosts, the shared credential store, the account picker) asks
`supports`/`unsupported` and never branches on an OS value itself. The node's own report
(`POST /api/agent/fleet/observe`'s `platform` block) adds `arch`/`os_version` and can contradict the
declaration — `platform_blocker` says so — but never re-labels it.

A Linux node is declared `ubuntu` whatever its distribution (labelled "Linux"); what it reports — its release
(`distro_id`/`distro_version`), architecture and probed host features (`host_features`) — can withdraw a
capability through the host-fact rules (`node_reason`). An absent fact restricts nothing. Otherwise Linux supports
everything. macOS runs the runner CLIs the provider catalogue marks `macos_runner` (its
installer ships them), over ACP, and mounts NodeLinks once its Developer ID-signed anchor holds Full Disk Access
(macOS privacy controls, TCC, keep every launchd-started process off a network file system until then).
Windows runs the runner CLIs the catalogue
marks `windows_runner`, over ACP, and mounts NodeLinks (through its WSL2 distro), and the AWS door launches it;
everything else is denied there.

Every OS may be marked MicroVM (`CAP_MICROVM`; whether THIS node qualifies is `app/microvm_mode.py`'s). A marked
node runs each session in a Linux guest, so the session-level capabilities (`runner_cli:<p>`, `acp_runtime`) are
asked of `session_platform(node)` — Ubuntu on the guest's architecture — through `session_reason`, never of the host
OS: a marked macOS node runs a provider its host cannot, unless the provider has no Linux arm64 build
(`linux_arm64`). Node-level capabilities (`gha_runners`, `vnode_host`, `shared_accounts`, `sdk_agent`,
`cloud_launch`) stay the host's, and an unmarked node is judged by the table below unchanged.
"""
from __future__ import annotations

import functools
import logging
import tomllib
from typing import Any, Optional

from app import agent_providers, shared_module

log = logging.getLogger(__name__)

OS_UBUNTU = "ubuntu"
OS_MACOS = "macos"
OS_WINDOWS = "windows"
#: The `nodes.os` vocabulary, pinned by the column's CHECK (migration 0326).
OS_VALUES: tuple[str, ...] = (OS_UBUNTU, OS_MACOS, OS_WINDOWS)

#: The OS family a node reports (`platform.os`), and the one each declared OS must report. Ubuntu
#: reports the kernel family, `linux`.
REPORTED_OS_VALUES: tuple[str, ...] = ("linux", "macos", "windows")
_EXPECTED_FAMILY = {OS_UBUNTU: "linux", OS_MACOS: "macos", OS_WINDOWS: "windows"}
_FAMILY_LABEL = {"linux": "Linux", "macos": "macOS", "windows": "Windows"}
#: The architectures a node may report; anything else is dropped at ingest.
ARCH_VALUES: tuple[str, ...] = ("x64", "arm64")
#: A MicroVM guest's architecture by host OS where it is not the host's own (`session_platform`).
_GUEST_ARCH = {OS_MACOS: "arm64", OS_WINDOWS: "x64"}
#: The hypervisor a node reports (`platform.hypervisor`, `nodes.hypervisor`); anything else is dropped at ingest.
HYPERVISOR_VALUES: tuple[str, ...] = ("kvm", "kvm-wsl2", "vz")

CAP_GHA_RUNNERS = "gha_runners"
CAP_VNODE_HOST = "vnode_host"
CAP_SHARED_ACCOUNTS = "shared_accounts"
# Display only: the cloud door validates its own `os` (Ubuntu, or Windows on a `windows_capable` type).
CAP_CLOUD_LAUNCH = "cloud_launch"
#: Running a registered SDK agent (`app/agents.py`): the supervisor and its runtimes ship for Ubuntu only.
CAP_SDK_AGENT = "sdk_agent"
#: Running a runner over the Agent Client Protocol (`app/acp_runtime.py`): every OS has it — its engine adapters are
#: npm installs the .deb, the macOS installer and the Windows runner service converge. A node that has
#: not converged a provider's adapter yet runs that provider legacy on its own (`apps/jaahd/acp_runtime.py`).
CAP_ACP_RUNTIME = "acp_runtime"
#: Being marked MicroVM: every OS (Linux KVM, Windows KVM under WSL2, macOS Virtualization.framework); the node's own
#: prerequisites are `microvm_mode.blocker`'s.
CAP_MICROVM = "microvm"
#: A family, one member per `agent_providers.PROVIDER_VALUES` entry (`runner_cli(provider)`).
CAP_RUNNER_CLI_PREFIX = "runner_cli:"
CAPABILITIES: tuple[str, ...] = (
    CAP_GHA_RUNNERS,
    CAP_VNODE_HOST,
    CAP_SHARED_ACCOUNTS,
    CAP_CLOUD_LAUNCH,
    CAP_SDK_AGENT,
    CAP_ACP_RUNTIME,
    CAP_MICROVM,
    f"{CAP_RUNNER_CLI_PREFIX}<provider>",
)

_LABEL = {OS_UBUNTU: "Linux", OS_MACOS: "macOS", OS_WINDOWS: "Windows"}
# The subject of each refusal sentence: `<subject> not available on <OS> nodes.`
_SUBJECT = {
    CAP_GHA_RUNNERS: "GitHub Actions runners are",
    CAP_VNODE_HOST: "Running several nodes on one machine is",
    CAP_SHARED_ACCOUNTS: "NodeLink is",
    CAP_CLOUD_LAUNCH: "Cloud launch is",
    CAP_SDK_AGENT: "SDK agents are",
    CAP_ACP_RUNTIME: "The Agent Client Protocol runtime is",
    CAP_MICROVM: "MicroVM nodes are",
}
# A capability whose refusal is a sentence of its own rather than `<subject> not available on <OS> nodes.`
_SENTENCE = {
    CAP_SDK_AGENT: "SDK agents run on Ubuntu nodes only for now.",
}
# What each non-Ubuntu OS may do; everything else is denied there. Ubuntu supports everything.
_SUPPORTED: dict[str, frozenset[str]] = {
    OS_MACOS: frozenset(
        {f"{CAP_RUNNER_CLI_PREFIX}{p.value}" for p in agent_providers.PROVIDERS if p.macos_runner}
        | {CAP_ACP_RUNTIME, CAP_SHARED_ACCOUNTS}  # ACP adapters; NodeLink needs FDA (module docstring)
        | {CAP_MICROVM}  # Virtualization.framework guests
    ),
    OS_WINDOWS: frozenset(
        {f"{CAP_RUNNER_CLI_PREFIX}{p.value}" for p in agent_providers.PROVIDERS if p.windows_runner}
        | {CAP_SHARED_ACCOUNTS, CAP_CLOUD_LAUNCH}  # AWS launches Windows
        | {CAP_ACP_RUNTIME}  # the runner service converges the pinned adapter
        | {CAP_MICROVM}  # KVM under WSL2
    ),
}


#: The host features a Linux node may report (`platform.features`, `nodes.host_features`), each a bool; a key absent
#: from the report is "not probed" and restricts nothing. Anything else is dropped at ingest.
FEATURE_ROOTLESS_DOCKER = "rootless_docker"
FEATURE_MOUNT_EFS = "mount_efs"
FEATURE_VALUES: tuple[str, ...] = (FEATURE_ROOTLESS_DOCKER, FEATURE_MOUNT_EFS)

MICROVM_ARM_LINUX = "MicroVM isn't available on Arm Linux nodes yet."
MICROVM_NEEDS_CELL = "MicroVM needs an x86_64 Ubuntu 26.04 node."
VNODE_HOST_NO_ROOTLESS = (
    "Running several nodes on one machine needs rootless Docker, which this node doesn't have yet."
)
NODELINK_NO_EFS_HELPER = "NodeLink needs the EFS mount helper, which this node doesn't have yet."
# The one Linux cell MicroVM runs on (architecture D9): `(distro_id, distro_version, arch)`.
_MICROVM_CELL = ("ubuntu", "26.04", "x64")
#: A feature-gated capability and the sentence when its feature is reported false.
_FEATURE_RULES = {
    CAP_VNODE_HOST: (FEATURE_ROOTLESS_DOCKER, VNODE_HOST_NO_ROOTLESS),
    CAP_SHARED_ACCOUNTS: (FEATURE_MOUNT_EFS, NODELINK_NO_EFS_HELPER),
}


def runner_cli(provider: str) -> str:
    """The capability that lets a node run `provider`'s accounts."""
    return f"{CAP_RUNNER_CLI_PREFIX}{provider}"


def label(os: str) -> str:
    """The operator-facing name of a declared OS (`macOS`, `Windows`, `Ubuntu`)."""
    return _LABEL.get(os, os)


def _reason(os: str, capability: str) -> str:
    if capability in _SENTENCE:
        return _SENTENCE[capability]
    if capability.startswith(CAP_RUNNER_CLI_PREFIX):
        provider = capability[len(CAP_RUNNER_CLI_PREFIX):]
        record = agent_providers.by_value(provider)
        what = f"{record.label if record is not None else provider} accounts are"
    else:
        what = _SUBJECT.get(capability, f"{capability} is")
    return f"{what} not available on {label(os)} nodes."


def unsupported(os: str) -> dict[str, str]:
    """`{capability: one-line operator reason}` for everything a node of `os` cannot do; `{}` for Ubuntu.

    The runner-CLI family is expanded per known provider, so the console can name each one."""
    if os == OS_UBUNTU:
        return {}
    every = [c for c in CAPABILITIES if not c.startswith(CAP_RUNNER_CLI_PREFIX)]
    every += [runner_cli(p) for p in agent_providers.PROVIDER_VALUES]
    allowed = _SUPPORTED.get(os, frozenset())
    return {capability: _reason(os, capability) for capability in every if capability not in allowed}


@functools.cache
def linux_support() -> dict[str, Any]:
    """`packaging/linux-support.toml` as read, the one home of the supported releases and arches (its `release`
    rows and `arches`), cached: callers read it, never change it. A missing file is a deployment defect, logged once; it then reads as supporting nothing,
    so labels read as reported and the bootstrap warns and tries, rather than failing a page or an install."""
    path = next((p for p in shared_module.candidates("linux-support.toml", "packaging") if p.is_file()), None)
    if path is None:
        log.error("linux-support.toml is missing — the api image must COPY it; "
                  "node releases read as reported")
        return {"release": [], "arches": []}
    with path.open("rb") as fh:
        return tomllib.load(fh)


def _releases() -> dict[tuple[str, str], str]:
    """`{(ID, VERSION_ID): label}` from `linux_support()`."""
    return {(r["id"], r["version_id"]): r["label"] for r in linux_support()["release"]}


def distro_label(node: Any) -> Optional[str]:
    """What a Linux node runs, as the console names it: the supported release's label (`Ubuntu 22.04`), an unlisted
    one as reported (`debian 12`), `Linux` before the first report. None off Linux (its OS label stands)."""
    if node.os != OS_UBUNTU:
        return None
    distro, version = getattr(node, "distro_id", None), getattr(node, "distro_version", None)
    if not distro:
        return label(OS_UBUNTU)
    return _releases().get((distro, version or "")) or " ".join(v for v in (distro, version) if v)


def _host_fact_reason(node: Any, capability: str) -> Optional[str]:
    """The host-fact rule refusing `capability` on a Linux node, from what it reported; None when it has not
    reported the fact (architecture D6: an absent fact is no restriction, decided per fact)."""
    if node.os != OS_UBUNTU:
        return None
    if capability == CAP_MICROVM:
        distro, version, arch = (getattr(node, k, None) for k in ("distro_id", "distro_version", "arch"))
        # Each of the three is a fact of its own: any one unreported restricts nothing (D6).
        if not distro or not version or not arch or (distro, version, arch) == _MICROVM_CELL:
            return None
        return MICROVM_ARM_LINUX if arch == "arm64" else MICROVM_NEEDS_CELL
    rule = _FEATURE_RULES.get(capability)
    if rule is None:
        return None
    feature, sentence = rule
    return sentence if (getattr(node, "host_features", None) or {}).get(feature) is False else None


def node_reason(node: Any, capability: str) -> Optional[str]:
    """`reason` for a node-level capability on one node: its declared OS's refusal first, then what the machine
    reported (`_host_fact_reason`). The API refuses with this sentence what the console hides."""
    return reason(node.os or OS_UBUNTU, capability) or _host_fact_reason(node, capability)


def node_unsupported(node: Any) -> dict[str, str]:
    """`unsupported` for one node: its OS's row plus its host-fact refusals (`node_reason`), its session-level
    entries (`runner_cli:<p>`, `acp_runtime`) asked of `session_platform` (`session_reason`): what the console serves
    for a node, so a MicroVM node offers what its guest runs."""
    table = unsupported(node.os)
    for capability in (CAP_MICROVM, *_FEATURE_RULES):
        why = _host_fact_reason(node, capability)
        if why is not None and capability not in table:
            table[capability] = why
    if not node.microvm:
        return table
    session_level = [runner_cli(p) for p in agent_providers.PROVIDER_VALUES] + [CAP_ACP_RUNTIME]
    kept = {capability: why for capability, why in table.items() if capability not in session_level}
    for capability in session_level:
        why = session_reason(node, capability)
        if why is not None:
            kept[capability] = why
    return kept


def _require_known(capability: str) -> None:
    """A misspelled capability would read as supported on Ubuntu and pass every test there: refuse it."""
    if capability.startswith(CAP_RUNNER_CLI_PREFIX) and len(capability) > len(CAP_RUNNER_CLI_PREFIX):
        return
    if capability not in _SUBJECT:
        raise ValueError(f"unknown node capability {capability!r}")


def supports(os: str, capability: str) -> bool:
    """Whether a node of `os` may do `capability`. An unknown provider is unsupported off Ubuntu."""
    _require_known(capability)
    return os == OS_UBUNTU or capability in _SUPPORTED.get(os, frozenset())


def reason(os: str, capability: str) -> Optional[str]:
    """The refusal sentence for `capability` on `os`, or None when it is supported."""
    return None if supports(os, capability) else _reason(os, capability)


def session_platform(node: Any) -> tuple[str, Optional[str]]:
    """The `(os, arch)` a session on `node` runs on: a MicroVM node's Linux guest — Ubuntu, arm64 on a macOS host (no
    Rosetta), x64 on Windows, the host's own on Ubuntu (KVM guests match their host) — else the host's own. `node` is
    anything with `os`, `arch` and `microvm` (a `Node` or a selected row); None is a nodeless caller, Ubuntu."""
    if node is None:
        return OS_UBUNTU, None
    os = node.os or OS_UBUNTU
    if not node.microvm:
        return os, node.arch
    return OS_UBUNTU, _GUEST_ARCH.get(os, node.arch)


def session_reason(node: Any, capability: str) -> Optional[str]:
    """`reason` for a session-level capability (`runner_cli:<p>`, `acp_runtime`) on `node`, asked of
    `session_platform`. A provider without a Linux arm64 build (`linux_arm64`) is refused in a macOS node's MicroVM."""
    os, arch = session_platform(node)
    if (node is not None and node.microvm and node.os == OS_MACOS and arch == "arm64"
            and capability.startswith(CAP_RUNNER_CLI_PREFIX)):
        record = agent_providers.by_value(capability[len(CAP_RUNNER_CLI_PREFIX):])
        if record is not None and not record.linux_arm64:
            return f"{record.label} accounts can't run in MicroVMs on {label(OS_MACOS)} nodes."
    return reason(os, capability) or (
        None if node is None or node.microvm else _host_fact_reason(node, capability)
    )


def platform_blocker(os: str, reported_os: Optional[str]) -> Optional[str]:
    """The node blocker when what the machine reports contradicts what it was declared as, else None.

    The declaration stands (it is the row's identity and what placement trusts); the operator recreates
    the node with the right OS. Absent report = unknown, never a contradiction."""
    if reported_os is None or _EXPECTED_FAMILY.get(os) == reported_os:
        return None
    return (
        f"This node was created as {label(os)} but reports {_FAMILY_LABEL.get(reported_os, reported_os)}. "
        f"Delete it and create it again with the right OS."
    )
