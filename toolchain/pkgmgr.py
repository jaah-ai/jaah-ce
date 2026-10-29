"""The node's ONE Python home for the package manager: apt on Ubuntu, dnf on Amazon Linux.

Its shell twin is the package's `jaah-pkg` script (the same verbs for the maintainer scripts and root units); every
other Python file the package ships reaches apt/dpkg/dnf/rpm through here, and the package-manager gate test holds
the tree to that. Two copies, one per language — within the context-architecture policy's extraction ceiling
(rule 1).

Package NAMES differ between the two archives (`docker.io` vs `docker`), so a caller that needs a per-manager
name passes both: `install(apt=("docker.io",), dnf=("docker",))`. A caller whose name is the same on both
passes it positionally.
"""
from __future__ import annotations

import shlex
import shutil
import subprocess
from collections.abc import Sequence

# WAIT FOR ANOTHER MANAGER'S LOCK, don't fail on it: callers are CONCURRENT with a package transaction by
# construction (postinst's detached converge starts inside `apt-get install jaah`). apt's default fails
# instantly; dnf waits on its own. Longer than the package's `jaahd-update.sh`'s: a first-boot transaction is longer
# than one upgrade.
LOCK_TIMEOUT_S = 600
APT_LOCK_OPT = f"-o DPkg::Lock::Timeout={LOCK_TIMEOUT_S}"

APT = "apt"
DNF = "dnf"
# dpkg's installed-state letters (`${db:Status-Abbrev}`, second column) whose files are on disk: installed,
# unpacked, half-configured, triggers-awaited, triggers-pending. Not `n`ot-installed, `c`onfig-files, `H`alf-installed.
_INSTALLED_STATES = frozenset("iUFWt")


def kind() -> str | None:
    """`apt` or `dnf`, or None on a host with neither (a dev box, macOS)."""
    if shutil.which("apt-get"):
        return APT
    if shutil.which("dnf"):
        return DNF
    return None


def _names(packages: Sequence[str], apt: Sequence[str] | None, dnf: Sequence[str] | None,
           manager: str) -> list[str]:
    per_manager = {APT: apt, DNF: dnf}[manager]
    return list(packages) + list(per_manager or ())


def install_command(*packages: str, apt: Sequence[str] | None = None, dnf: Sequence[str] | None = None,
                    refresh: bool = True, manager: str | None = None) -> str:
    """The shell text that installs PACKAGES (plus this manager's own names), refreshing the index first unless
    `refresh` is False. Raises RuntimeError on a host with no package manager.

    The refresh is NOT optional by default: a node needing repair is exactly one whose index may be stale, and
    installing against it 404s or lands a version still under the floor."""
    manager = manager or kind()
    if manager is None:
        raise RuntimeError("pkgmgr: no apt-get or dnf on this host")
    names = " ".join(shlex.quote(n) for n in _names(packages, apt, dnf, manager))
    update = f"{refresh_command(manager)}; " if refresh else ""
    if manager == APT:
        return f"{update}DEBIAN_FRONTEND=noninteractive apt-get {APT_LOCK_OPT} install -y {names}"
    return f"{update}dnf -y -q install {names}"


def refresh_command(manager: str | None = None) -> str:
    """The shell text that refreshes this host's package index. Raises RuntimeError with no package manager."""
    manager = manager or kind()
    if manager == APT:
        return f"apt-get {APT_LOCK_OPT} update -qq"
    if manager == DNF:
        return "dnf -q makecache"
    raise RuntimeError("pkgmgr: no apt-get or dnf on this host")


def has_candidate(package: str, manager: str | None = None) -> bool:
    """Does this host's package index offer PACKAGE? On dnf a package that PROVIDES the name counts (`ruby` is
    `ruby3.2` on Amazon Linux 2023), exactly as `dnf install` resolves it."""
    manager = manager or kind()
    if manager == APT:
        # `apt-cache policy` exits 0 on unknown names; `Candidate: (none)` means virtual or obsolete.
        argv = ["apt-cache", "policy", package]
    elif manager == DNF:
        argv = ["dnf", "-q", "repoquery", "--whatprovides", package, "--qf", "%{name}"]
    else:
        return False
    try:
        out = subprocess.run(argv, capture_output=True, text=True, check=False, timeout=120).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    if manager == DNF:
        return bool(out.strip())
    for line in out.splitlines():
        if line.strip().startswith("Candidate:"):
            return line.split(":", 1)[1].strip() not in ("", "(none)")
    return False


def install(*packages: str, apt: Sequence[str] | None = None, dnf: Sequence[str] | None = None,
            refresh: bool = True, check: bool = True) -> subprocess.CompletedProcess:
    """Install PACKAGES (see `install_command`); raises CalledProcessError on failure when `check`."""
    return subprocess.run(["bash", "-c", "set -e; " + install_command(*packages, apt=apt, dnf=dnf,
                                                                       refresh=refresh)], check=check)


def install_file_command(path: str, manager: str | None = None) -> str:
    """The shell text that installs a downloaded package FILE (`./x.deb` / `./x.rpm`) with its dependencies."""
    manager = manager or kind()
    if manager == APT:
        return f"apt-get {APT_LOCK_OPT} install -y {shlex.quote(path)}"
    if manager == DNF:
        return f"dnf -y -q install {shlex.quote(path)}"
    raise RuntimeError("pkgmgr: no apt-get or dnf on this host")


def installed_version(package: str) -> str | None:
    """The INSTALLED version of PACKAGE, or None (not installed, or no package database on this host). For `jaah`
    this is the upstream version the control plane hands out: rpm's Release is never part of it."""
    manager = kind()
    if manager == APT:
        argv = ["dpkg-query", "-W", "-f", "${db:Status-Abbrev} ${Version}", package]
    elif manager == DNF:
        argv = ["rpm", "-q", "--qf", "ii %{VERSION}", package]
    else:
        return None
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, check=False, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    status, _, version = proc.stdout.strip().partition(" ")
    # The SECOND letter is the installed state; the first is only the wanted one, so a HELD package (`hi`) is
    # installed too — reading it as absent would e.g. pick the wrong docker flavour's rootless package. Unpacked
    # and mid-configure (`U`, `F`) and trigger states count as installed: postinst restarts the daemon while dpkg
    # still reads `iF`, and that daemon asks for its own version first thing.
    if proc.returncode != 0 or status.strip()[1:2] not in _INSTALLED_STATES or not version.strip():
        return None
    return version.strip()


def is_installed(package: str) -> bool:
    return installed_version(package) is not None


def reinstall_hint(package: str = "jaah") -> str:
    """The operator command that re-runs PACKAGE's maintainer scripts on this host, for a message."""
    if kind() == DNF:
        return f"dnf reinstall -y {package}"
    return f"apt-get install --reinstall {package}"
