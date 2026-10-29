"""acp_registry_install — putting an ACP registry agent on this node at its pinned version, and a bundled agent's
declared dependencies beside its script.

An ACP registry agent is an `agents` row whose `manifest.kind` is `acp_registry`; its `manifest.pin` names
one version and one Linux distribution chosen at install (`{"kind": "binary"|"npx"|"uvx", ...}`, the shapes
in the task contract). `install` builds that distribution into a fresh folder and returns how to launch it
(`Installed.argv` / `.env`). It never resolves a version: what the pin names is what lands.

Caller: the ACP host child (`jaah-acp-host`, architecture C8) wires this into dispatch (C5) — it runs as the
pane's session user, so every byte here is written in that identity; as root it refuses (tenant bytes are
never handled as root, as in `agent_process`).

Trust: the agent cache is writable by every session user on the node (runners share that uid), so nothing
already in it is trusted. An extracted tree is never reused — every `install` builds a FRESH one — and the
one cached artefact, a binary archive, is re-hashed against the pin's sha256 on every use. Layout, under
the agent cache root (`env_helpers.jaah_dir("cache")`, beside `agent_process`'s `agent-bundles/`):

    <cache>/acp-registry/archives/<sha256>                     a downloaded binary archive, named by its hash
    <cache>/acp-registry/runs/<org>/<slug>/<version>-<hex>/    one install's tree (0700), when no `dest` is given
        binary: the unpacked archive (`cmd` inside)   npx: node_modules/ (npm --prefix)   uvx: venv/
    <cache>/acp-registry/npm-cache/, uv-cache/                 npm's and uv's package caches (they verify integrity)

An archive is downloaded over https only (redirects too), must carry a sha256 in the pin, and is unpacked
with traversal, link-escape and size guards. A package spec must be a plain registry name at an exact
version. npm and uv run with a minimal env, never this process's. What npm and uv still do on their own:
npm runs the package's lifecycle scripts, and only the top-level package is pinned — its transitive
dependencies follow the package's own ranges. Every failure is an `InstallFailed` whose sentence names the
agent and the cause.

A manifest 2 bundle carries no third-party code: beside the script its `command` runs it declares it instead —
`requirements.txt` (every line hash-pinned) or `package.json` + `package-lock.json`. `install_bundle_dependencies`
installs those INTO the script's folder, in the same identity and minimal env: pip with `--target` (Python puts the
script's folder first on sys.path) and `--require-hashes --only-binary=:all:` (no sdist build runs code), `npm ci
--ignore-scripts` (node_modules beside the script, no lifecycle scripts).
"""
from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import secrets
import shutil
import stat
import subprocess
import tarfile
import tempfile
import time
import urllib.request
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Callable, Optional, Union

import env_helpers
import runner_env  # python_cmd(): the node's own interpreter, never a PATH lookup

REGISTRY_DIR = "acp-registry"
#: The most an archive may download to, and unpack to.
ARCHIVE_CAP_BYTES = 1024 * 1024 * 1024
UNPACK_CAP_BYTES = 2 * 1024 * 1024 * 1024
_FETCH_TIMEOUT_S = 120
#: The whole download, however slowly the bytes trickle in under the per-read timeout.
_FETCH_DEADLINE_S = 600
_RUN_TIMEOUT_S = 900
_TAR_SUFFIXES = (".tar.gz", ".tgz", ".tar.bz2", ".tbz2", ".tar.xz", ".txz")
_SEGMENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]*$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_EXACT = re.compile(r"[0-9][0-9A-Za-z.+-]*")
_NPM_NAME = re.compile(r"^(@[a-z0-9][a-z0-9._-]*/)?[a-z0-9][a-z0-9._-]*$")
_PY_SPEC = re.compile(r"^([A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?)"
                      r"(\[[A-Za-z0-9][A-Za-z0-9._-]*(?:,[A-Za-z0-9][A-Za-z0-9._-]*)*\])?$")
#: Launch env names a pin may not set — they reroute what the agent loads or runs. The CP filters too;
#: this is the node's own fence.
_DANGEROUS_ENV = frozenset({"PATH", "HOME", "SHELL", "BASH_ENV", "ENV", "NODE_OPTIONS", "NODE_PATH",
                            "PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP"})
_DANGEROUS_ENV_PREFIXES = ("LD_", "DYLD_", "JAAH_")
#: The script suffixes a bundle's `command` may run (`tools/manifest_check.py::v2_sdk_errors`).
_SCRIPT_SUFFIXES = (".py", ".js", ".mjs", ".cjs")
#: The only names of this process's env an installer subprocess inherits.
_INSTALLER_ENV = ("PATH", "HOME", "LANG", "LC_ALL", "TMPDIR")

Fetch = Callable[[str, Path], None]
Run = Callable[[list, dict], "tuple[int, str]"]


class InstallFailed(Exception):
    """The agent could not be installed; `sentence` names the agent and the cause."""

    def __init__(self, sentence: str) -> None:
        super().__init__(sentence)
        self.sentence = sentence


@dataclass(frozen=True)
class Installed:
    dir: Path
    argv: list = field(default_factory=list)
    env: dict = field(default_factory=dict)


def default_cache_root() -> Path:
    return env_helpers.jaah_dir("cache")


def _arch(machine: str) -> str:
    m = machine.lower()
    return {"amd64": "x86_64", "x64": "x86_64", "arm64": "aarch64"}.get(m, m)


def _plain(segment: str) -> bool:
    return bool(_SEGMENT.fullmatch(segment)) and ".." not in segment


# ── download ─────────────────────────────────────────────────────────────────────────────────────────────
class HttpsOnlyRedirect(urllib.request.HTTPRedirectHandler):
    """Follows a redirect only to another https URL."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not str(newurl).startswith("https://"):
            raise ValueError(f"its archive redirects to a non-https URL ({newurl})")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def fetch_https(url: str, dest: Path) -> None:
    """Stream `url` into `dest`: https only (redirects too), capped at `ARCHIVE_CAP_BYTES` and
    `_FETCH_DEADLINE_S` overall."""
    if not url.startswith("https://"):
        raise ValueError(f"its archive is not an https URL ({url})")
    opener = urllib.request.build_opener(HttpsOnlyRedirect)
    deadline = time.monotonic() + _FETCH_DEADLINE_S
    total = 0
    with opener.open(url, timeout=_FETCH_TIMEOUT_S) as resp, open(dest, "wb") as out:  # noqa: S310
        while chunk := resp.read(1024 * 1024):
            total += len(chunk)
            if total > ARCHIVE_CAP_BYTES:
                raise ValueError(f"its archive is larger than {ARCHIVE_CAP_BYTES} bytes")
            if time.monotonic() > deadline:
                raise ValueError(f"its archive took longer than {_FETCH_DEADLINE_S}s to download")
            out.write(chunk)


def run_command(argv: list, env: dict) -> tuple[int, str]:
    """Run an installer with exactly `env` — never this process's own."""
    proc = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=_RUN_TIMEOUT_S)
    return proc.returncode, (proc.stderr or proc.stdout or "").strip()


def _installer_env(registry: Path) -> dict:
    env = {k: os.environ[k] for k in _INSTALLER_ENV if os.environ.get(k)}
    env.setdefault("PATH", "/usr/local/bin:/usr/bin:/bin")
    env["npm_config_cache"] = str(registry / "npm-cache")
    env["UV_CACHE_DIR"] = str(registry / "uv-cache")
    # The session user's own npm/uv config (shared uid, writable by any runner) never steers the index.
    env["npm_config_userconfig"] = os.devnull
    env["npm_config_globalconfig"] = os.devnull
    env["UV_NO_CONFIG"] = "1"
    return env


def _launch_env(raw: object) -> dict:
    if not isinstance(raw, dict):
        return {}
    return {str(k): str(v) for k, v in raw.items()
            if str(k) not in _DANGEROUS_ENV and not str(k).startswith(_DANGEROUS_ENV_PREFIXES)}


# ── package specs ────────────────────────────────────────────────────────────────────────────────────────
def _npm_spec(spec: str) -> tuple[str, str]:
    """`name@1.2.3` / `@scope/name@1.2.3` → (name, version); ("", "") for anything else."""
    at = spec.rfind("@")
    if at <= 0:
        return "", ""
    name, version = spec[:at], spec[at + 1:]
    if not _NPM_NAME.fullmatch(name) or not _EXACT.fullmatch(version):
        return "", ""
    return name, version


def _py_spec(spec: str) -> tuple[str, str, str]:
    """`name[extras]==1.2.3` or `name[extras]@1.2.3` (uvx's spelling) → (name, extras, version);
    ("", "", "") for anything else."""
    head, sep, version = spec.partition("==")
    if not sep:
        head, sep, version = spec.partition("@")
    m = _PY_SPEC.fullmatch(head) if sep else None
    if not m or not _EXACT.fullmatch(version):
        return "", "", ""
    return m.group(1), m.group(2) or "", version


# ── archive ──────────────────────────────────────────────────────────────────────────────────────────────
def _inside(root: Path, name: str) -> bool:
    if name.startswith("/") or "\\" in name:
        return False
    target = (root / name).resolve()
    return target == root or root in target.parents


def _private_copy(src: Path) -> tuple[IO[bytes], str]:
    """Copy `src` into an anonymous temp file while hashing it: what is hashed is what gets unpacked, and
    no other process can reach the copy by path."""
    out = tempfile.TemporaryFile()
    digest, total = hashlib.sha256(), 0
    with open(src, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            total += len(chunk)
            if total > ARCHIVE_CAP_BYTES:
                out.close()
                raise ValueError(f"its archive is larger than {ARCHIVE_CAP_BYTES} bytes")
            digest.update(chunk)
            out.write(chunk)
    out.seek(0)
    return out, digest.hexdigest()


def _verified_archive(registry: Path, sha: str, url: str, fetch: Fetch) -> IO[bytes]:
    """The archive whose bytes hash to `sha`: the cached one when it still does, else a fresh download."""
    archives = registry / "archives"
    archives.mkdir(parents=True, exist_ok=True)
    cached = archives / sha
    if cached.is_file():
        fh, got = _private_copy(cached)
        if got == sha:
            return fh
        fh.close()
        cached.unlink(missing_ok=True)  # tampered or truncated: never used, fetched again
    part = archives / f".{sha}.part-{secrets.token_hex(6)}"
    try:
        fetch(url, part)
        fh, got = _private_copy(part)
        if got != sha:
            fh.close()
            raise ValueError(f"its archive's sha256 is {got}, not the pinned {sha}")
        os.replace(part, cached)
        return fh
    finally:
        part.unlink(missing_ok=True)


def _unpack(archive: IO[bytes], dest: Path, url: str) -> None:
    root = dest.resolve()
    lower = url.lower().split("?", 1)[0]
    total = 0
    if lower.endswith(".zip"):
        with zipfile.ZipFile(archive) as zf:
            for info in zf.infolist():
                if not _inside(root, info.filename):
                    raise ValueError(f"its archive has an entry outside its folder ({info.filename})")
                if stat.S_ISLNK(info.external_attr >> 16):
                    raise ValueError(f"its archive has a link entry ({info.filename})")
                total += info.file_size
                if total > UNPACK_CAP_BYTES:
                    raise ValueError(f"it unpacks to more than {UNPACK_CAP_BYTES} bytes")
            for info in zf.infolist():
                out = Path(zf.extract(info, root))
                mode = (info.external_attr >> 16) & 0o777
                if mode and not info.is_dir():
                    out.chmod(mode & 0o755)
        return
    if not lower.endswith(_TAR_SUFFIXES):
        raise ValueError(f"its archive is not a .tar.gz, .tar.bz2, .tar.xz or .zip ({url})")
    with tarfile.open(fileobj=archive, mode="r:*") as tar:
        keep = []
        for member in tar:
            if not _inside(root, member.name):
                raise ValueError(f"its archive has an entry outside its folder ({member.name})")
            if not (member.isfile() or member.isdir() or member.issym() or member.islnk()):
                continue
            if member.issym() and not _inside(root, str(Path(member.name).parent / member.linkname)):
                raise ValueError(f"its archive has a link leaving its folder ({member.name})")
            if member.islnk() and not _inside(root, member.linkname):  # a hardlink names an archive path
                raise ValueError(f"its archive has a link leaving its folder ({member.name})")
            total += member.size
            if total > UNPACK_CAP_BYTES:
                raise ValueError(f"it unpacks to more than {UNPACK_CAP_BYTES} bytes")
            keep.append(member)
        # `data` refuses absolute names, `..`, links leaving the tree, devices and set-id bits — a second wall.
        tar.extractall(root, members=keep, filter="data")


# ── the three distributions ──────────────────────────────────────────────────────────────────────────────
def _target(dist: dict, arch: str) -> Optional[dict]:
    target = (dist.get("targets") or {}).get(f"linux-{arch}")
    return target if isinstance(target, dict) else None


def _binary(dist: dict, tree: Path, arch: str, registry: Path, fetch: Fetch) -> list:
    target = _target(dist, arch)
    if target is None:
        raise ValueError(f"there is no linux-{arch} build in its pin")
    url, cmd = str(target.get("archive") or ""), str(target.get("cmd") or "")
    if not url or not cmd:
        raise ValueError(f"its linux-{arch} build names no archive or no cmd")
    sha = str(target.get("sha256") or "").lower()
    if not _SHA256.fullmatch(sha):
        raise ValueError("its Linux build has no checksum")
    with _verified_archive(registry, sha, url, fetch) as archive:
        _unpack(archive, tree, url)
    root = tree.resolve()
    exe = (root / cmd).resolve()
    if not _inside(root, cmd) or not exe.is_file():
        raise ValueError(f"its cmd {cmd} is not in the unpacked archive")
    exe.chmod(exe.stat().st_mode | 0o111)
    return [str(exe), *[str(a) for a in target.get("args") or []]]


def _npm_bin(tree: Path, name: str) -> str:
    """The command `name` installs, from its own package.json `bin`."""
    base = name.rsplit("/", 1)[-1]
    try:
        bins = json.loads((tree / "node_modules" / name / "package.json").read_text()).get("bin")
    except (OSError, ValueError, AttributeError) as exc:
        raise ValueError(f"its npm package {name} has no readable package.json") from exc
    if isinstance(bins, str) and bins:
        return base
    if isinstance(bins, dict) and len(bins) == 1:
        return str(next(iter(bins)))
    if isinstance(bins, dict) and base in bins:
        return base
    raise ValueError(f"its npm package {name} names no single command to launch")


def _npx(dist: dict, tree: Path, env: dict, run: Run) -> list:
    package = str(dist.get("package") or "")
    name, version = _npm_spec(package)
    if not name:
        raise ValueError(f"its npm package {package!r} names no exact version")
    spec = f"{name}@{version}"
    rc, err = run(["npm", "install", "--prefix", str(tree), "--no-audit", "--no-fund", "--", spec], env)
    if rc != 0:
        raise ValueError(f"npm install {spec} failed: {err or f'exit {rc}'}")
    bin_name = _npm_bin(tree, name)
    root = tree.resolve()
    exe = tree / "node_modules" / ".bin" / bin_name
    if not _plain(bin_name) or not _inside(root, f"node_modules/.bin/{bin_name}") or not exe.is_file():
        raise ValueError(f"its npm package {name} installed no {bin_name} command")
    return [str(exe), *[str(a) for a in dist.get("args") or []]]


def _uvx(dist: dict, tree: Path, env: dict, run: Run) -> list:
    package = str(dist.get("package") or "")
    name, extras, version = _py_spec(package)
    if not name:
        raise ValueError(f"its Python package {package!r} names no exact version")
    venv = tree / "venv"
    for argv in (["uv", "venv", str(venv)],
                 ["uv", "pip", "install", "--python", str(venv / "bin" / "python"), "--",
                  f"{name}{extras}=={version}"]):
        rc, err = run(argv, env)
        if rc != 0:
            raise ValueError(f"{' '.join(argv[:3])} for {package} failed: {err or f'exit {rc}'}")
    exe = venv / "bin" / name
    if not exe.is_file():
        raise ValueError(f"its venv has no {name} command")
    return [str(exe), *[str(a) for a in dist.get("args") or []]]


def install(manifest_or_pin: dict, *, slug: str, org_id: Union[int, str], dest: Optional[Path] = None,
            cache_root: Optional[Path] = None, arch: Optional[str] = None,
            fetch: Fetch = fetch_https, run: Run = run_command) -> Installed:
    """Build the pinned distribution into a fresh tree and return how to launch it. The tree is `dest`
    (created, or an existing empty folder) or, when None, a new
    `<cache_root>/acp-registry/runs/<org_id>/<slug>/<version>-<hex>/` (0700). Accepts the row's whole
    manifest or its `pin`."""
    pin = manifest_or_pin.get("pin", manifest_or_pin)
    agent = manifest_or_pin.get("agent") or {}
    name = str(agent.get("name") or slug)
    version = str(pin.get("version") or "")
    org = str(org_id)
    dist = pin.get("distribution") or {}
    kind = str(dist.get("kind") or "")
    arch = _arch(arch or platform.machine())

    def failed(cause: str) -> InstallFailed:
        return InstallFailed(f"Couldn't install {name} {version or '(no version)'} on this node: {cause}.")

    if not (_plain(slug) and _plain(version) and _plain(org)):
        raise failed("its org, slug or pinned version is not a plain name")
    if kind not in ("binary", "npx", "uvx"):
        raise failed(f"its pin names no binary, npx or uvx distribution ({kind or 'none'})")
    if os.name == "posix" and os.geteuid() == 0:
        raise failed("this would run as root (refusing to handle an agent's files as root)")

    registry = (Path(cache_root) if cache_root is not None else default_cache_root()) / REGISTRY_DIR
    target = _target(dist, arch) if kind == "binary" else dist
    env = _launch_env((target or {}).get("env"))

    created = dest is None or not Path(dest).exists()
    try:
        if dest is None:
            tree = registry / "runs" / org / slug / f"{version}-{secrets.token_hex(8)}"
            tree.parent.mkdir(parents=True, exist_ok=True)
            tree.mkdir(mode=0o700)
        else:
            tree = Path(dest)
            if created:
                tree.mkdir(mode=0o700, parents=True)
            elif not tree.is_dir() or any(tree.iterdir()):
                raise ValueError(f"its install folder {tree} is not empty")
    except (OSError, ValueError) as exc:
        raise failed(str(exc).rstrip(".")) from exc
    try:
        if kind == "binary":
            argv = _binary(dist, tree, arch, registry, fetch)
        elif kind == "npx":
            argv = _npx(dist, tree, _installer_env(registry), run)
        else:
            argv = _uvx(dist, tree, _installer_env(registry), run)
    except (OSError, ValueError, tarfile.TarError, zipfile.BadZipFile, subprocess.SubprocessError) as exc:
        if created:
            shutil.rmtree(tree, ignore_errors=True)
        else:
            for child in tree.iterdir():
                if child.is_dir() and not child.is_symlink():
                    shutil.rmtree(child, ignore_errors=True)
                else:
                    child.unlink(missing_ok=True)
        raise failed(str(exc).rstrip(".")) from exc
    return Installed(dir=tree, argv=argv, env=env)


# ── a bundle's declared dependencies ─────────────────────────────────────────────────────────────────────
def _bundle_script(root: Path, command: list) -> Optional[str]:
    """The script `command` runs: its first word naming a `_SCRIPT_SUFFIXES` file in the bundle (`v2_sdk_errors`'
    rule). None when no word does; a script word leaving the bundle raises."""
    for word in command:
        script = os.path.normpath(str(word))
        if not script.endswith(_SCRIPT_SUFFIXES):
            continue
        if not _inside(root, script):
            raise ValueError(f"its command runs {word}, outside its bundle")
        if (root / script).is_file():
            return script
    return None


def install_bundle_dependencies(dest: Path, command: list, *, name: str, cache_root: Optional[Path] = None,
                                run: Run = run_command) -> list[str]:
    """Install what a bundled agent declares beside the script its `command` runs, into that script's folder:
    `requirements.txt` with pip, `package-lock.json` with `npm ci`. Returns the installers it ran (`["pip"]`,
    `["npm"]`, `[]` when the bundle declares nothing)."""

    def failed(cause: str) -> InstallFailed:
        return InstallFailed(f"Couldn't install {name}'s dependencies on this node: {cause}.")

    if os.name == "posix" and os.geteuid() == 0:
        raise failed("this would run as root (refusing to handle an agent's files as root)")
    root = Path(dest).resolve()
    try:
        script = _bundle_script(root, list(command))
    except ValueError as exc:
        raise failed(str(exc)) from exc
    if script is None:
        return []
    here = (root / script).parent
    registry = (Path(cache_root) if cache_root is not None else default_cache_root()) / REGISTRY_DIR
    steps: list[tuple[str, list, dict]] = []
    if (here / "requirements.txt").is_file():
        first = str(command[0]) if command else ""
        python = first if Path(first).name.startswith("python") else runner_env.python_cmd()
        if "/" in python and not os.path.isabs(python):
            python = str(root / python)  # the engine runs from the agent root (`agent_process.acp_argv`)
        env = {**_installer_env(registry), "PIP_CACHE_DIR": str(registry / "pip-cache"),
               "PIP_CONFIG_FILE": os.devnull, "PIP_NO_INPUT": "1"}
        steps.append(("pip", [python, "-m", "pip", "install", "--quiet", "--disable-pip-version-check", "--no-input",
                              "--require-hashes", "--only-binary=:all:", "--target", str(here),
                              "-r", str(here / "requirements.txt")], env))
    if (here / "package-lock.json").is_file():
        steps.append(("npm", ["npm", "ci", "--prefix", str(here), "--omit=dev", "--ignore-scripts", "--no-audit",
                              "--no-fund"], _installer_env(registry)))
    for installer, argv, env in steps:
        try:
            rc, err = run(argv, env)
        except (OSError, subprocess.SubprocessError) as exc:
            raise failed(f"{installer} could not run ({type(exc).__name__}: {exc})") from exc
        if rc != 0:
            verb = "pip install" if installer == "pip" else "npm ci"
            raise failed(f"{verb} failed: {err or f'exit {rc}'}".rstrip("."))
    return [installer for installer, _argv, _env in steps]
