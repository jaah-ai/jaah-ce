#!/usr/bin/env python3
"""runner_cli_pins.py — the one reader of the runner vendor-CLI pins in `packaging/platform-pins.toml`.

Every installer of a runner CLI resolves its version here, so a node installs the pin and never `@latest`:
the package's postinst (`converge_runner_npm_cli`, `converge_runner_agy_cli`), `jaah-runner-clis-vn`
and the OMX refresh (`omx_layer.refresh_argv`). The table, its fields and how to bump it are documented in
the pins file itself.

The shell installers call the CLI form, each verb printing one line on stdout:

    runner_cli_pins.py install [--unpinned] NAME...
                                                  (dry run) each NAME as the `name@version` the
                                                  `--prefix` form installs — exit 2,
                                                  nothing printed, when a NAME has no pin or the pins cannot
                                                  be read. `--unpinned` lets the ONE name after it through
                                                  unchanged (`--unpinned typescript@5`, a language server)
    runner_cli_pins.py stale --prefix P NAME...   the pinned NAMEs whose installed version under the npm
                                                  prefix P is not the pin (absent counts) — empty = current
    runner_cli_pins.py install --prefix P [--no-wait] NAME...
                                                  installs each NAME under the npm prefix P, holding
                                                  `P/.jaah-npm.lock` across the whole call — a pinned NAME
                                                  verified and locked (below), an `--unpinned` one through a
                                                  plain `npm install -g`; exit 2 on a refused pin (or an
                                                  unmarked unpinned NAME), 1 on a
                                                  failed install, 75 when `--no-wait` found the lock held
    runner_cli_pins.py agy-shell                  the Antigravity pins as `AGY_*=value` shell assignments,
                                                  every value validated against a strict pattern first
    runner_cli_pins.py agy-acp-stale --home H     `agy-acp-server` when the `[agy-acp-server]` pin is not
                                                  what home H has installed, `current` when it is
    runner_cli_pins.py agy-acp-install --home H   installs that pin under home H (`agy_acp_install`), a
                                                  no-op when current; exit 1 on a failed or refused install
    runner_cli_pins.py lock NAME...               (maintainer) regenerate each pinned NAME's dependency lock

A pinned install is ENFORCED, not just named: `npm pack name@version` into a stage under the prefix, the
tarball's sha512 SRI compared to the pin's `integrity` (a mismatch refuses before anything is unpacked),
then the pin's `patches` (repo paths of unified diffs, e.g. `packages/acp-forks/`) applied to the unpacked
tree exactly — no fuzz, no offset; one that does not apply refuses the install — then `npm ci` against the
committed lock `packaging/runner-cli-locks/<name>.json`, so every transitive dependency is the locked
version at the locked digest (`npm ci` checks each one). The finished tree is renamed into
`P/lib/node_modules/<name>` and its bins linked into `P/bin` the way npm links them (on Windows, where a
symlink needs a privilege the runner lacks, a `<bin>.cmd` shim instead); a `.jaah-lock-sha256`
stamp in it records which lock and patches built it (`build_digest`), so a changed patch reinstalls. Why not `npm install -g`: it checks a tarball
against the digest the registry serves, not the pin, and ignores a shrinkwrap inside a local tarball.

`stale` and `install` run AS the runner user (they read and write files that user owns); `stale` parses
`package.json` and never executes an installed CLI. Stdlib only, run with `python3 -I` from the packaged tree.
"""
from __future__ import annotations

import base64
import contextlib
import hashlib
import hmac
import io
import json
import os
import re
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import zipfile
from dataclasses import dataclass
from pathlib import Path

# The bundle root (`/usr/lib/jaahd` on a node, the checkout in a worktree): apps/jaahd/ is two levels down.
BUNDLE_ROOT = Path(__file__).resolve().parents[2]
PINS_PATH = BUNDLE_ROOT / "packaging" / "platform-pins.toml"
# One lock per `[[npm]]` pin, named by `lock_file_name`; shipped beside the pins in the bundle tree.
LOCKS_DIR = PINS_PATH.parent / "runner-cli-locks"
# The prefix-wide install lock (an exclusive flock): every installer of a runner prefix takes it, so the
# postinst arms, the vnode floor and the OMX refresh never write one `node_modules` at once.
NPM_LOCK_FILE = ".jaah-npm.lock"
# Inside an installed pinned package: the sha256 of the lock and patches that built it (`build_digest`).
LOCK_STAMP = ".jaah-lock-sha256"
_NPM_TIMEOUT_S = 900.0
# One pinned package's worst case in `install`: `npm pack`, `npm ci`, and the uninstall of the package it replaces.
PINNED_INSTALL_BUDGET_S = 3 * _NPM_TIMEOUT_S
# `install --no-wait` found the prefix lock held (EX_TEMPFAIL).
EXIT_BUSY = 75

_VERSION = re.compile(r"\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?")
_NAME = re.compile(r"(?:@[a-z0-9][a-z0-9._-]*/)?[a-z0-9][a-z0-9._-]*")
_INTEGRITY = re.compile(r"sha512-[A-Za-z0-9+/]+={0,2}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_REGISTRY = "https://registry.npmjs.org/"
_AGY_ARCHES = ("amd64", "arm64")
# A Windows node: `npm` is `npm.cmd`, which CreateProcess never finds by the bare name, and a bin is
# a `.cmd` shim, not a symlink (`_shim`). Read at call time through this name so a test can flip it.
_WINDOWS = os.name == "nt"
# The shim's fixed head; `_owned_link` recognises our own shim by it and the package path that follows.
_SHIM_HEAD = "@echo off\r\nrem Installed by runner_cli_pins.py; replaced on every pin bump.\r\n"


class PinsError(RuntimeError):
    """The pins file or a lock is missing or malformed, or a tarball is not the pinned one; the message names
    the entry and the field."""


class InstallError(RuntimeError):
    """npm failed; the message carries its exit status and the tail of its stderr."""


@dataclass(frozen=True)
class NpmPin:
    name: str
    bin: str
    version: str
    integrity: str
    #: The package this one renamed; "" when none.
    replaces: str = ""
    #: Unified diffs applied to the verified tarball's tree, as bundle-root-relative paths, in order.
    patches: tuple[str, ...] = ()

    @property
    def spec(self) -> str:
        return f"{self.name}@{self.version}"


def load_npm(path: Path | None = None) -> dict[str, NpmPin]:
    """Every `[[npm]]` entry, keyed by package name. Raises `PinsError` on any malformed entry."""
    path = path or PINS_PATH
    pins: dict[str, NpmPin] = {}
    for raw in _load(path).get("npm", []):
        name = str(raw.get("name", ""))
        if not _NAME.fullmatch(name):
            raise PinsError(f"{path}: [[npm]] {name!r}: name is not an npm package name")
        if name in pins:
            raise PinsError(f"{path}: [[npm]] {name!r} is pinned twice")
        patches = raw.get("patches", [])
        if not isinstance(patches, list) or not all(isinstance(p, str) for p in patches):
            raise PinsError(f"{path}: [[npm]] {name!r}: patches must be a list of paths")
        pin = NpmPin(name=name, bin=str(raw.get("bin", "")), version=str(raw.get("version", "")),
                     integrity=str(raw.get("integrity", "")), replaces=str(raw.get("replaces", "")),
                     patches=tuple(patches))
        for patch in pin.patches:
            rel = Path(patch)
            if rel.is_absolute() or ".." in rel.parts or not patch.endswith(".patch"):
                raise PinsError(f"{path}: [[npm]] {name!r}: patch {patch!r} is not a relative .patch path")
        if pin.replaces and not _NAME.fullmatch(pin.replaces):
            raise PinsError(f"{path}: [[npm]] {name!r}: replaces {pin.replaces!r} is not an npm package name")
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", pin.bin):
            raise PinsError(f"{path}: [[npm]] {name!r}: bin {pin.bin!r} is not a binary name")
        if not _VERSION.fullmatch(pin.version):
            raise PinsError(f"{path}: [[npm]] {name!r}: version {pin.version!r} is not an exact version")
        if not _INTEGRITY.fullmatch(pin.integrity):
            raise PinsError(f"{path}: [[npm]] {name!r}: integrity is not a sha512 SRI")
        pins[name] = pin
    return pins


def spec(name: str, path: Path | None = None) -> str:
    """`name@version` for a pinned package; raises `PinsError` for an unpinned one or an unreadable file."""
    path = path or PINS_PATH
    pin = load_npm(path).get(name)
    if pin is None:
        raise PinsError(f"{path}: {name!r} has no [[npm]] pin")
    return pin.spec


def install_specs(args: list[str], path: Path | None = None) -> list[str]:
    """The specs npm installs for `args`: each pinned name as `name@version`. Every name must be pinned unless
    `--unpinned` stands right before it, which passes that one name through unchanged; an unmarked unpinned
    name raises `PinsError`, so a deleted or misspelled pin never falls back to an unverified latest."""
    path = path or PINS_PATH
    pins = load_npm(path)
    specs: list[str] = []
    unpinned = False
    for arg in args:
        if arg == "--unpinned":
            unpinned = True
            continue
        if arg in pins:
            specs.append(pins[arg].spec)
        elif unpinned:
            specs.append(arg)
        else:
            raise PinsError(f"{path}: {arg!r} has no [[npm]] pin (an unpinned install needs --unpinned before it)")
        unpinned = False
    if unpinned or not specs:
        raise PinsError("install: --unpinned must name a package after it")
    return specs


def installed_version(prefix: Path, name: str) -> str:
    """The version npm recorded for `name` under a `-g --prefix` install; "" when absent or unreadable."""
    try:
        data = json.loads((prefix / "lib" / "node_modules" / name / "package.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    version = data.get("version") if isinstance(data, dict) else None
    return version if isinstance(version, str) else ""


def stale(prefix: Path, names: list[str], path: Path | None = None, locks_dir: Path | None = None) -> list[str]:
    """The pinned `names` whose install under `prefix` is not the pin: another version, or not built from
    the current lock (a pre-lock install counts). Unpinned names are skipped."""
    pins = load_npm(path)
    return [n for n in names if n in pins and not _current(prefix, pins[n], locks_dir)]


def _current(prefix: Path, pin: NpmPin, locks_dir: Path | None) -> bool:
    if installed_version(prefix, pin.name) != pin.version:
        return False
    try:
        stamp = (prefix / "lib" / "node_modules" / pin.name / LOCK_STAMP).read_text(encoding="utf-8").strip()
    except OSError:
        return False
    return hmac.compare_digest(stamp, build_digest(read_lock(pin, locks_dir), read_patches(pin)))


# ── the enforced install ─────────────────────────────────────────────────────────────────────────────

def sri(data: bytes) -> str:
    """The sha512 SRI npm records as `dist.integrity`."""
    return "sha512-" + base64.b64encode(hashlib.sha512(data).digest()).decode("ascii")


def verify_tarball(pin: NpmPin, data: bytes) -> None:
    """Raises `PinsError` unless the tarball's bytes are the ones the pin's `integrity` names."""
    actual = sri(data)
    if not hmac.compare_digest(actual, pin.integrity):
        raise PinsError(f"{pin.spec}: tarball integrity {actual} is not the pinned {pin.integrity} — refusing")


def lock_file_name(name: str) -> str:
    """`@scope/pkg` -> `scope__pkg.json`, `pkg` -> `pkg.json`."""
    return name.removeprefix("@").replace("/", "__") + ".json"


def read_lock(pin: NpmPin, locks_dir: Path | None = None) -> bytes:
    """The committed lock's bytes. Raises `PinsError` when it is missing, not an npm v2+ lockfile, or was not
    generated for exactly this pin (its root names another package or version)."""
    path = (locks_dir or LOCKS_DIR) / lock_file_name(pin.name)
    try:
        raw = path.read_bytes()
        data = json.loads(raw)
    except (OSError, ValueError) as exc:
        raise PinsError(f"{path}: unreadable lock for {pin.spec}: {exc}") from exc
    packages = data.get("packages") if isinstance(data, dict) else None
    root = packages.get("", {}) if isinstance(packages, dict) else {}
    if data.get("lockfileVersion", 0) < 2 or (root.get("name"), root.get("version")) != (pin.name, pin.version):
        raise PinsError(f"{path}: not the lock of {pin.spec} — regenerate it with `runner_cli_pins.py lock "
                        f"{pin.name}`")
    # `npm ci` checks an entry against its digest only when it has one, so every entry must: a registry
    # tarball with a sha512. A bundled dependency is covered by its parent's tarball digest.
    for key, entry in packages.items():
        if key == "" or entry.get("inBundle"):
            continue
        if (entry.get("link") or not str(entry.get("resolved", "")).startswith(_REGISTRY)
                or not _INTEGRITY.fullmatch(str(entry.get("integrity", "")))):
            raise PinsError(f"{path}: {key} is not a registry tarball with a sha512 integrity — refusing")
    return raw


def read_patches(pin: NpmPin, root: Path | None = None) -> list[bytes]:
    """Each of the pin's patches' bytes, in order, from the bundle root (not beside a pins file passed in:
    on a node both are the bundle tree). Raises `PinsError` when one is missing."""
    root = root or BUNDLE_ROOT
    try:
        return [(root / patch).read_bytes() for patch in pin.patches]
    except OSError as exc:
        raise PinsError(f"{pin.spec}: unreadable patch: {exc}") from exc


def build_digest(lock: bytes, patches: list[bytes]) -> str:
    """The install stamp: the lock's sha256 alone when unpatched (so an unpatched install stays current),
    else over the lock and every patch, each length-prefixed."""
    if not patches:
        return hashlib.sha256(lock).hexdigest()
    digest = hashlib.sha256()
    for part in (lock, *patches):
        digest.update(len(part).to_bytes(8, "big") + part)
    return digest.hexdigest()


_HUNK = re.compile(r"@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def apply_patch(package: Path, patch: bytes, label: str = "patch") -> None:
    """Apply a unified diff (`a/<path>` -> `b/<path>`, paths relative to the package directory) exactly:
    each hunk read by the line counts its header states, at its stated line, its context and removed lines
    matching byte for byte. Edits only existing files inside `package`. Raises `PinsError` naming the file
    and hunk on any mismatch; files are written only once every hunk of the patch has applied."""
    lines = _lines(patch.decode("utf-8"))
    root = package.resolve()
    results: dict[Path, list[str]] = {}
    i = 0
    while i < len(lines):
        if not lines[i].startswith("--- "):
            i += 1
            continue
        if i + 1 >= len(lines) or not lines[i + 1].startswith("+++ "):
            raise PinsError(f"{label}: '---' without '+++' at line {i + 1}")
        old_name = lines[i][4:].split("\t")[0].strip()
        new_name = lines[i + 1][4:].split("\t")[0].strip()
        if not (old_name.startswith("a/") and new_name.startswith("b/")) or old_name[2:] != new_name[2:]:
            raise PinsError(f"{label}: only edits of an existing file are applied ({old_name} -> {new_name})")
        target = (package / old_name[2:]).resolve()
        if root not in target.parents or not target.is_file():
            raise PinsError(f"{label}: {old_name[2:]} is not a file in the package")
        if target not in results:
            results[target] = _lines(target.read_bytes().decode("utf-8"))
        text = results[target]
        i += 2
        shift = 0
        while i < len(lines) and lines[i].startswith("@@"):
            m = _HUNK.match(lines[i])
            if not m:
                raise PinsError(f"{label}: malformed hunk header {lines[i].strip()!r}")
            old_count, new_count = int(m.group(2) or 1), int(m.group(4) or 1)
            start = int(m.group(1)) - (1 if old_count else 0) + shift
            old, new = [], []
            i += 1
            while len(old) < old_count or len(new) < new_count:
                tag, body = (lines[i][:1], lines[i][1:]) if i < len(lines) else ("", "")
                if tag not in (" ", "-", "+"):
                    raise PinsError(f"{label}: hunk {m.group(0)} line counts do not match its body")
                if tag in (" ", "-"):
                    old.append(body)
                if tag in (" ", "+"):
                    new.append(body)
                i += 1
            if (len(old), len(new)) != (old_count, new_count):
                raise PinsError(f"{label}: hunk {m.group(0)} line counts do not match its body")
            if i < len(lines) and lines[i].startswith("\\"):
                raise PinsError(f"{label}: a missing final newline is not supported")
            if text[start:start + len(old)] != old:
                raise PinsError(f"{label}: hunk {m.group(0)} does not apply to {old_name[2:]} — refusing")
            text[start:start + len(old)] = new
            shift += len(new) - len(old)
    if not results:
        raise PinsError(f"{label}: no file in the patch")
    for target, text in results.items():
        target.write_bytes("".join(text).encode("utf-8"))


def _lines(text: str) -> list[str]:
    """Split on `\n` only, keeping it (`splitlines` would also break on `\r`, `\f`, `\u2028`...)."""
    return [line for line in re.split(r"(?<=\n)", text) if line]


class PrefixBusy(RuntimeError):
    """`install(wait=False)` found another installer holding the prefix lock."""


def install(prefix: Path, names: list[str], *, npm: str = "npm", path: Path | None = None,
            locks_dir: Path | None = None, wait: bool = True) -> list[str]:
    """Install `names` under `prefix` holding the prefix lock; returns what was (re)installed. `names` follow
    `install_specs`' grammar: an unpinned name needs `--unpinned` right before it. A pinned name
    already current is left alone. The pins and locks are read once the lock is HELD, so an install queued
    behind a package upgrade installs the new pins. `wait=False` raises `PrefixBusy` instead of queueing.
    Raises `PinsError` (nothing of that pin installed) or `InstallError`."""
    install_specs(names, path)  # an unreadable table, or an unpinned name not marked `--unpinned`, refuses
    prefix.mkdir(parents=True, exist_ok=True)
    done: list[str] = []
    with _prefix_lock(prefix, wait=wait):
        _recover_stages(prefix / "lib" / "node_modules")
        install_specs(names, path)  # re-read under the lock (a package upgrade may have landed meanwhile)
        pins = load_npm(path)
        pinned = [pins[n] for n in names if n in pins]
        unpinned = [n for n in names if n not in pins and n != "--unpinned"]
        locks = {pin.name: read_lock(pin, locks_dir) for pin in pinned}  # all readable before the first write
        for pin in pinned:
            read_patches(pin)
        for pin in pinned:
            if _current(prefix, pin, locks_dir) and _bin_path(prefix / "bin", pin.bin).exists():
                continue
            _install_pinned(prefix, pin, npm, locks[pin.name])
            done.append(pin.spec)
        if unpinned:
            _npm([npm, "install", "-g", "--ignore-scripts", "--no-fund", "--no-audit", "--prefix", str(prefix),
                  "--", *unpinned])
            done.extend(unpinned)
    return done


@contextlib.contextmanager
def _prefix_lock(prefix: Path, *, wait: bool = True):
    """`prefix/.jaah-npm.lock`, LOCK_EX. A prefix that cannot be locked (no flock on this platform or
    filesystem, a symlinked lock file) installs unlocked and says so: npm's own install lock is the fallback."""
    fd = None
    try:
        import fcntl

        # No O_NOFOLLOW where the platform has none (Windows): the open must not die of an AttributeError.
        fd = os.open(prefix / NPM_LOCK_FILE, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX if wait else fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            os.close(fd)
            raise PrefixBusy(f"{prefix}: another installer holds {NPM_LOCK_FILE}") from None
    except (ImportError, OSError) as exc:
        if fd is not None:
            os.close(fd)
        fd = None
        print(f"runner_cli_pins: installing without the prefix lock ({exc})", file=sys.stderr)
    try:
        yield
    finally:
        if fd is not None:
            os.close(fd)  # releases the flock


def _npm(argv: list[str], cwd: Path | None = None) -> str:
    if _WINDOWS:
        # `npm` is `npm.cmd` there: resolved through PATHEXT, since CreateProcess finds only an `.exe` by name.
        argv = [shutil.which(argv[0]) or argv[0], *argv[1:]]
    try:
        res = subprocess.run(argv, cwd=cwd, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                             timeout=_NPM_TIMEOUT_S)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise InstallError(f"{' '.join(argv[:3])}: {exc}") from exc
    if res.returncode != 0:
        raise InstallError(f"{' '.join(argv[:3])} exited {res.returncode}: {res.stderr.strip()[-400:]}")
    return res.stdout


def _fetch_verified(pin: NpmPin, npm: str, stage: Path, patches: list[bytes] | None = None) -> Path:
    """`npm pack` the pin into `stage`, verify it, unpack it, apply its patches (`patches`: their bytes, read
    here when not given); the package directory."""
    packs = stage / "pack"
    packs.mkdir()
    _npm([npm, "pack", "--ignore-scripts", "--pack-destination", str(packs), "--", pin.spec], cwd=stage)
    tarballs = list(packs.glob("*.tgz"))
    if len(tarballs) != 1:
        raise InstallError(f"npm pack {pin.spec} left {len(tarballs)} tarballs, not one")
    # Verified and unpacked from the SAME bytes: nothing can swap the file between the two.
    data = tarballs[0].read_bytes()
    verify_tarball(pin, data)
    unpacked = stage / "unpacked"
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        tar.extractall(unpacked, filter="data")
    tops = [p for p in unpacked.iterdir() if p.is_dir()]
    if len(tops) != 1 or not (tops[0] / "package.json").is_file():
        raise InstallError(f"{pin.spec}: the tarball holds no single package directory")
    package = tops[0]
    for name, patch in zip(pin.patches, read_patches(pin) if patches is None else patches):
        apply_patch(package, patch, f"{pin.spec}: {name}")
    # The lock covers what a dependency install resolves, so devDependencies leave package.json too: `npm
    # ci` then finds the two in sync. A vendor's own shrinkwrap would outrank the lock, so it goes.
    manifest = json.loads((package / "package.json").read_text(encoding="utf-8"))
    manifest.pop("devDependencies", None)
    (package / "package.json").write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    (package / "npm-shrinkwrap.json").unlink(missing_ok=True)
    return package


_STAGE_PREFIX = ".jaah-stage-"
_STAGE_OWNER = "name"  # inside a stage: the package name it installs, so a recovery knows the target


def _recover_stages(modules: Path) -> None:
    """Under the prefix lock no stage is live, so every one left is a killed install's: put back a previous
    tree the kill caught between its two renames, then delete the stage (a full dependency tree)."""
    if not modules.is_dir():
        return
    for stage in modules.glob(f"{_STAGE_PREFIX}*"):
        if stage.is_symlink() or not stage.is_dir():
            continue
        with contextlib.suppress(OSError, ValueError):
            name = (stage / _STAGE_OWNER).read_text(encoding="utf-8").strip()
            target = modules / name
            if _NAME.fullmatch(name) and (stage / "previous").is_dir() and not (
                    target.exists() or target.is_symlink()):
                os.rename(stage / "previous", target)
        shutil.rmtree(stage, ignore_errors=True)


def _install_pinned(prefix: Path, pin: NpmPin, npm: str, lock: bytes) -> None:
    modules = prefix / "lib" / "node_modules"
    modules.mkdir(parents=True, exist_ok=True)
    # Staged INSIDE node_modules so the final step is a same-filesystem rename; the dot name keeps npm's
    # own global listing from reading it as a package.
    stage = Path(tempfile.mkdtemp(prefix=_STAGE_PREFIX, dir=modules))
    try:
        (stage / _STAGE_OWNER).write_text(pin.name, encoding="utf-8")
        patches = read_patches(pin)  # read once: the bytes applied are the bytes stamped
        package = _fetch_verified(pin, npm, stage, patches)
        (package / "package-lock.json").write_bytes(lock)
        _npm([npm, "ci", "--ignore-scripts", "--no-fund", "--no-audit"], cwd=package)
        (package / LOCK_STAMP).write_text(build_digest(lock, patches) + "\n", encoding="utf-8")
        if pin.replaces and installed_version(prefix, pin.replaces):
            # The package this pin renamed still owns the bin it links: uninstalled once the new tree is built.
            _npm([npm, "uninstall", "-g", "--ignore-scripts", "--no-fund", "--no-audit", "--prefix", str(prefix),
                  "--", pin.replaces])
        bins = _bins(prefix, pin, package)  # every check before the installed tree is touched
        target = modules / pin.name
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists() or target.is_symlink():
            os.rename(target, stage / "previous")
        os.rename(package, target)
        _link_bins(prefix, pin, bins)
    finally:
        shutil.rmtree(stage, ignore_errors=True)


def _bins(prefix: Path, pin: NpmPin, package: Path) -> dict[str, str]:
    """The package's bins as {name: path in the package}, validated: it links `pin.bin`, no name or path
    leaves its directory, and none would replace anything in `prefix/bin` but this package's own link (npm's
    EEXIST) — one CLI never takes over another's bin."""
    bins = json.loads((package / "package.json").read_text(encoding="utf-8")).get("bin") or {}
    if isinstance(bins, str):
        bins = {pin.name.rsplit("/", 1)[-1]: bins}
    if pin.bin not in bins:
        raise InstallError(f"{pin.spec} links no `{pin.bin}` (its bins: {sorted(bins)})")
    root = package.resolve()
    for name, rel in bins.items():
        target = (package / str(rel)).resolve()
        if not re.fullmatch(r"[A-Za-z0-9._-]+", name) or name.startswith(".") or root not in target.parents:
            raise InstallError(f"{pin.spec}: bin {name!r} -> {rel!r} leaves its package")
        if _WINDOWS and re.search(r'["%\r\n]', str(rel)):
            # cmd.exe would read these inside the shim's quoted path (`_shim`): refused, never escaped.
            raise InstallError(f"{pin.spec}: bin {name!r} -> {rel!r} cannot be written into a .cmd shim")
        existing = _bin_path(prefix / "bin", name)
        if (existing.exists() or existing.is_symlink()) and not _owned_link(existing, pin):
            raise InstallError(f"{pin.spec}: bin {name!r} would replace {existing}, which is not its own link")
        target.chmod(target.stat().st_mode | 0o111)
    return {name: str(rel) for name, rel in bins.items()}


def _bin_path(bin_dir: Path, name: str) -> Path:
    """Where bin `name` lives in `bin_dir`: the link itself, or on Windows its `.cmd` shim."""
    return bin_dir / (f"{name}.cmd" if _WINDOWS else name)


def _bin_name(path: Path) -> str:
    """The bin a `bin_dir` entry is (`_bin_path`'s inverse); "" for a file on Windows that is no `.cmd`."""
    if not _WINDOWS:
        return path.name
    return path.name[:-4] if path.name.lower().endswith(".cmd") else ""


def _node_exe() -> str:
    """The Node a Windows shim runs, by absolute path (`windows.NODEJS_DIR`): cmd.exe searches the current directory
    — the worktree — before PATH, so a bare `node` would run a repo's committed `node.bat`. In-function:
    the `.deb`'s postinst runs this module on Linux, where no shim is written."""
    from host_platform import windows  # noqa: PLC0415

    return windows.NODEJS_DIR + "\\node.exe"


def _shim(pin: NpmPin, rel: str) -> str:
    """A Windows bin: Node (`_node_exe`) running the package's script by its path relative to the shim, the way
    npm's own `.cmd` shims find it."""
    parts = [p for p in str(rel).replace("\\", "/").split("/") if p not in ("", ".")]
    script = "\\".join(("..", "lib", "node_modules", *pin.name.split("/"), *parts))
    return f'{_SHIM_HEAD}"{_node_exe()}" "%~dp0\\{script}" %*\r\n'


def _owned_link(path: Path, pin: NpmPin) -> bool:
    if _WINDOWS:
        # Ours only when it is exactly a shim of this package: head, this package's path, nothing else.
        package = "\\".join(("..", "lib", "node_modules", *pin.name.split("/"))) + "\\"
        own = re.escape(f'{_SHIM_HEAD}"{_node_exe()}" "%~dp0\\{package}') + r'[^"%\r\n]+" %\*\r\n'
        try:
            return (not path.is_symlink() and path.is_file()
                    and re.fullmatch(own, path.read_bytes().decode("utf-8")) is not None)
        except (OSError, UnicodeDecodeError):
            return False
    return path.is_symlink() and os.readlink(path).startswith(f"../lib/node_modules/{pin.name}/")


def _link_bins(prefix: Path, pin: NpmPin, bins: dict[str, str]) -> None:
    """npm's own bin links, `prefix/bin/<bin>` -> `../lib/node_modules/<name>/<path>`, each swapped in
    atomically; a link of this package's that the new version no longer declares is removed, as npm does."""
    bin_dir = prefix / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    package = prefix / "lib" / "node_modules" / pin.name
    for name, rel in sorted(bins.items()):
        staged = bin_dir / f".{name}.jaah-new"
        staged.unlink(missing_ok=True)
        if _WINDOWS:
            staged.write_bytes(_shim(pin, rel).encode("utf-8"))
        else:
            staged.symlink_to(os.path.relpath(package / rel, bin_dir))
        os.replace(staged, _bin_path(bin_dir, name))
    for link in bin_dir.iterdir():
        name = _bin_name(link)
        if name and name not in bins and not link.name.startswith(".") and _owned_link(link, pin):
            link.unlink()


def write_lock(pin: NpmPin, *, npm: str = "npm", locks_dir: Path | None = None) -> Path:
    """(maintainer) Resolve the pin's dependency tree once and commit it: the verified tarball's own
    package.json (devDependencies dropped, as at install), the vendor's shrinkwrap as the starting point when
    it ships one."""
    with tempfile.TemporaryDirectory() as tmp:
        stage = Path(tmp)
        package = _fetch_verified(pin, npm, stage)
        with tarfile.open(next((stage / "pack").glob("*.tgz")), "r:gz") as tar:
            shipped = next((m for m in tar.getmembers() if m.name.endswith("/npm-shrinkwrap.json")
                            and m.name.count("/") == 1), None)
            if shipped is not None:
                (package / "package-lock.json").write_bytes(tar.extractfile(shipped).read())
        _npm([npm, "install", "--package-lock-only", "--ignore-scripts", "--no-fund", "--no-audit"], cwd=package)
        out = (locks_dir or LOCKS_DIR) / lock_file_name(pin.name)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes((package / "package-lock.json").read_bytes())
    read_lock(pin, locks_dir)
    return out


def agy_assignments(path: Path | None = None) -> list[str]:
    """The `[agy]` pins as `AGY_*=value` lines, safe to `eval`: every value matches a strict pattern."""
    path = path or PINS_PATH
    agy = _load(path).get("agy")
    if not isinstance(agy, dict):
        raise PinsError(f"{path}: no [agy] table")
    version, release = str(agy.get("version", "")), str(agy.get("release", ""))
    if not _VERSION.fullmatch(version):
        raise PinsError(f"{path}: [agy] version {version!r} is not an exact version")
    if not re.fullmatch(r"https://[A-Za-z0-9./_-]+", release) or f"/{version}-" not in release:
        raise PinsError(f"{path}: [agy] release must be an https URL carrying version {version}")
    lines = [f"AGY_VERSION={version}", f"AGY_RELEASE={release}"]
    for key, var in (("sha256", "AGY_SHA256"), ("bin_sha256", "AGY_BIN_SHA256")):
        for arch in _AGY_ARCHES:
            digest = str((agy.get(key) or {}).get(arch, ""))
            if not _SHA256.fullmatch(digest):
                raise PinsError(f"{path}: [agy] {key}.{arch} is not a sha256")
            lines.append(f"{var}_{arch.upper()}={digest}")
    return lines


# The `[agy-acp-server]` install, under the runner's home: the zip's members in their own directory and a wrapper
# script on the runner's PATH. A symlink will not do: the server finds `localharness_external` beside its own path,
# and fails `session/new` when started through one (measured, 1.4.1).
AGY_ACP_MEMBERS = ("agy_acp_server.par", "localharness_external")
AGY_ACP_LIB = Path(".local") / "lib" / "agy-acp-server"
AGY_ACP_WRAPPER = Path(".local") / "bin" / "agy-acp-server"
# Free space the install needs where the home lives: the zip, its unpacked stage and the previous build, kept for a
# server still running from it (sizes: the pins file's [agy-acp-server] comment), rounded up. Short of it the
# install refuses.
AGY_ACP_MIN_FREE = 3 * 1024 ** 3
# Held across the headroom check and the install, so two installs that see the same `/run/lock` take turns: two
# checks that each saw 3 GiB free must not then fill the disk together. A vnode's runner-CLI unit mounts its own
# `/run/lock`, so vnodes do not share it (concurrent vnode first installs are not serialised). `/run/lock` is
# world-writable and sticky, so the file is opened read-only (flock needs no write) and created only when absent.
AGY_ACP_HOST_LOCK = Path("/run/lock/jaah-agy-acp-install.lock")
# What `agy-acp-stale` prints for a home already at the pin.
AGY_ACP_CURRENT = "current"
_MACHINE_ARCH = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64"}


@dataclass(frozen=True)
class AgyAcpPin:
    """One arch's `[agy-acp-server]` pin."""
    version: str
    release: str
    sha256: str
    bin_sha256: str


def agy_acp_pin(arch: str, path: Path | None = None) -> AgyAcpPin:
    """The `[agy-acp-server]` pin for `arch` (`amd64`/`arm64`), every value pattern-checked."""
    path = path or PINS_PATH
    table = _load(path).get("agy-acp-server")
    if not isinstance(table, dict):
        raise PinsError(f"{path}: no [agy-acp-server] table")
    version = str(table.get("version", ""))
    if not _VERSION.fullmatch(version):
        raise PinsError(f"{path}: [agy-acp-server] version {version!r} is not an exact version")
    if arch not in _AGY_ARCHES:
        raise PinsError(f"{path}: [agy-acp-server] has no pin for arch {arch!r}")
    release = str((table.get("release") or {}).get(arch, ""))
    if not re.fullmatch(r"https://[A-Za-z0-9./_-]+\.zip", release) or f"-{version}-" not in release:
        raise PinsError(f"{path}: [agy-acp-server] release.{arch} must be an https zip URL carrying version {version}")
    digests = []
    for key in ("sha256", "bin_sha256"):
        digest = str((table.get(key) or {}).get(arch, ""))
        if not _SHA256.fullmatch(digest):
            raise PinsError(f"{path}: [agy-acp-server] {key}.{arch} is not a sha256")
        digests.append(digest)
    return AgyAcpPin(version, release, *digests)


def host_arch() -> str:
    """This host's arch as the pins spell it."""
    machine = os.uname().machine
    if machine not in _MACHINE_ARCH:
        raise PinsError(f"no agy-acp-server build for machine {machine!r}")
    return _MACHINE_ARCH[machine]


def _file_sha256(path: Path) -> str:
    """`path`'s sha256, "" when it is not a regular file this process can read (never follows a FIFO open)."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError:
        return ""
    with os.fdopen(fd, "rb") as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            return ""
        digest = hashlib.sha256()
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _agy_acp_build(home: Path, pin: AgyAcpPin) -> Path:
    """The pin's own build directory under `AGY_ACP_LIB`: a bump installs beside the old build, never over it, so a
    server still running from it keeps the harness it finds beside its real path."""
    return home / AGY_ACP_LIB / f"{pin.version}-{pin.bin_sha256[:12]}"


def _agy_acp_wrapper(home: Path, pin: AgyAcpPin) -> str:
    """The wrapper's text: `exec` of the pin's installed server by its real path."""
    target = str(_agy_acp_build(home, pin) / AGY_ACP_MEMBERS[0])
    return ("#!/bin/sh\n# Installed by runner_cli_pins.py agy-acp-install; replaced on every pin bump.\n"
            f'exec {shlex.quote(target)} "$@"\n')


def agy_acp_stale(home: Path, pin: AgyAcpPin) -> bool:
    """Whether `home` lacks the pinned server: the wrapper, the other member's presence, or the installed `.par`'s
    hash differ. `localharness_external` is not hashed here: the zip's `sha256`, checked at install, covers it."""
    build = _agy_acp_build(home, pin)
    try:
        wrapper_ok = (home / AGY_ACP_WRAPPER).read_text(encoding="utf-8") == _agy_acp_wrapper(home, pin)
    except (OSError, UnicodeDecodeError):
        wrapper_ok = False
    return not (wrapper_ok and (build / AGY_ACP_MEMBERS[1]).is_file()
                and _file_sha256(build / AGY_ACP_MEMBERS[0]) == pin.bin_sha256)


@contextlib.contextmanager
def _host_lock(path: Path):
    """`path`, LOCK_EX, waited for. One that cannot be taken (no `/run/lock`, a symlink planted there) installs
    unlocked and says so: the headroom check still runs, only the cross-user race is unguarded."""
    fd = None
    try:
        import fcntl

        try:
            fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        except FileNotFoundError:
            fd = os.open(path, os.O_RDONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o644)
        fcntl.flock(fd, fcntl.LOCK_EX)
    except (ImportError, OSError) as exc:
        if fd is not None:
            os.close(fd)
        fd = None
        print(f"runner_cli_pins: installing without the host install lock ({exc})", file=sys.stderr)
    try:
        yield
    finally:
        if fd is not None:
            os.close(fd)


def agy_acp_install(home: Path, pin: AgyAcpPin, *, curl: str = "curl") -> bool:
    """Install `pin` under `home`; False when it is already there. Raises InstallError naming what failed.

    Fetched over https only (redirects included), the zip checked against `sha256` before it is opened, only the
    two expected members extracted, the `.par` checked against `bin_sha256`; then the unpacked build renamed into
    its own directory and the wrapper replaced atomically, so a killed install never leaves a half server under the
    name the PATH search finds. Builds other than this one and the newest before it are removed. Everything runs
    under `AGY_ACP_HOST_LOCK`, so concurrent installs sharing it take turns: the stages a killed install left are
    swept first, then the install refuses without `AGY_ACP_MIN_FREE` free under `home`."""
    if not agy_acp_stale(home, pin):
        return False
    with _host_lock(AGY_ACP_HOST_LOCK):
        # Again under the lock: an install that waited behind another for this home finds it done.
        return agy_acp_stale(home, pin) and _agy_acp_install_locked(home, pin, curl=curl)


def _agy_acp_install_locked(home: Path, pin: AgyAcpPin, *, curl: str) -> bool:
    """`agy_acp_install`'s body, under the host lock."""
    lib = home / AGY_ACP_LIB
    lib.mkdir(parents=True, exist_ok=True)
    for leftover in lib.parent.glob(".agy-acp-server.*"):
        if leftover.is_dir() and not leftover.is_symlink():
            shutil.rmtree(leftover, ignore_errors=True)
    free = shutil.disk_usage(lib.parent).free
    if free < AGY_ACP_MIN_FREE:
        raise InstallError(f"agy-acp-server {pin.version} needs {AGY_ACP_MIN_FREE >> 20} MiB free under {lib.parent}, "
                           f"which has {free >> 20} MiB: not installed, so Antigravity API-key runs stay on the "
                           "terminal runtime — free disk space and re-run the install")
    build = _agy_acp_build(home, pin)
    with tempfile.TemporaryDirectory(prefix=".agy-acp-server.", dir=lib.parent) as tmp:
        stage = Path(tmp)
        archive = stage / "agy-acp-server.zip"
        subprocess.run([curl, "-fsSL", "--proto", "=https", "--proto-redir", "=https", "-o", str(archive),
                        pin.release], check=True, stdin=subprocess.DEVNULL, timeout=1800)
        if _file_sha256(archive) != pin.sha256:
            raise InstallError(f"{pin.release}: sha256 does not match the pin")
        unpacked = stage / "build"
        unpacked.mkdir()
        with zipfile.ZipFile(archive) as zf:
            names = sorted(zf.namelist())
            if names != sorted(AGY_ACP_MEMBERS):
                raise InstallError(f"{pin.release}: unexpected members {names}")
            for name in AGY_ACP_MEMBERS:
                with zf.open(name) as src, open(unpacked / name, "wb") as dst:
                    shutil.copyfileobj(src, dst, 1 << 20)
                os.chmod(unpacked / name, 0o755)
        archive.unlink()
        if _file_sha256(unpacked / AGY_ACP_MEMBERS[0]) != pin.bin_sha256:
            raise InstallError(f"{pin.release}: {AGY_ACP_MEMBERS[0]} sha256 does not match the pin")
        if build.exists() or build.is_symlink():
            os.replace(build, stage / "damaged")  # this pin's own build, failing the check: replaced whole
        os.replace(unpacked, build)
        wrapper = home / AGY_ACP_WRAPPER
        wrapper.parent.mkdir(parents=True, exist_ok=True)
        staged = wrapper.with_name(f".{wrapper.name}.new")
        staged.write_text(_agy_acp_wrapper(home, pin), encoding="utf-8")
        os.chmod(staged, 0o755)
        os.replace(staged, wrapper)
    others = sorted((d for d in lib.iterdir() if d != build and d.is_dir() and not d.is_symlink()),
                    key=lambda d: d.stat().st_mtime, reverse=True)
    for old in others[1:]:
        shutil.rmtree(old, ignore_errors=True)
    return True


def _load(path: Path) -> dict:
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise PinsError(f"{path}: unreadable: {exc}") from exc


def main(argv: list[str]) -> int:
    verb, args = (argv[0], argv[1:]) if argv else ("", [])
    try:
        if verb == "install" and len(args) >= 3 and args[0] == "--prefix":
            wait = args[2] != "--no-wait"
            names = [a for a in args[2 if wait else 3:] if a != "--"]
            # A unit's SIGTERM (its runtime cap) unwinds through `finally`, so the stage is removed; a
            # SIGKILL's leftover is recovered by the next install.
            signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
            for spec_ in install(Path(args[1]), names, wait=wait):
                print(f"runner_cli_pins: installed {spec_}", file=sys.stderr)
            return 0
        if verb == "install" and args:
            print(" ".join(install_specs(args)))
            return 0
        if verb == "stale" and len(args) >= 3 and args[0] == "--prefix":
            # `--unpinned` markers ride along from the installer's package list; `stale` skips unpinned names.
            print(" ".join(stale(Path(args[1]), [a for a in args[2:] if a not in ("--", "--unpinned")])))
            return 0
        if verb == "lock" and args:
            pins = load_npm()
            for name in args:
                if name not in pins:
                    raise PinsError(f"{name!r} has no [[npm]] pin")
                print(write_lock(pins[name]))
            return 0
        if verb == "agy-shell" and not args:
            print("\n".join(agy_assignments()))
            return 0
        if verb in ("agy-acp-stale", "agy-acp-install") and len(args) == 2 and args[0] == "--home":
            home, pin = Path(args[1]), agy_acp_pin(host_arch())
            if verb == "agy-acp-stale":
                # A word either way: a caller reads anything else (a timeout, a crash) as "install".
                print("agy-acp-server" if agy_acp_stale(home, pin) else AGY_ACP_CURRENT)
                return 0
            # As `install`: a unit's SIGTERM (its runtime cap) unwinds through `finally`, so the stage is removed.
            signal.signal(signal.SIGTERM, lambda *_: sys.exit(143))
            if agy_acp_install(home, pin):
                print(f"runner_cli_pins: installed agy-acp-server {pin.version}", file=sys.stderr)
            return 0
    except PinsError as exc:
        print(f"runner_cli_pins: {exc}", file=sys.stderr)
        return 2
    except PrefixBusy as exc:
        print(f"runner_cli_pins: {exc}", file=sys.stderr)
        return EXIT_BUSY
    except (InstallError, OSError, tarfile.TarError, ValueError, subprocess.SubprocessError) as exc:
        print(f"runner_cli_pins: {exc}", file=sys.stderr)
        return 1
    print(__doc__.split("\n\n")[2], file=sys.stderr)
    return 64


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
