"""manifest_check.py — the one home of the `jaah-agent.toml` contract.

The control plane validates at registration (`apps/api/app/agent_bundles.py`) through `apps/api/app/agents.py`,
which LOADS this file by path and re-exports it (`app/shared_module.py`). STDLIB ONLY, and no sibling imports,
for that loader.

`validate_manifest(text, *, tree=None, read=None)` returns a `Manifest` or the list of
`ManifestError`s; `read_agent_root(root)` gathers `tree` from an unpacked bundle.

One manifest version is accepted: `[agent] manifest = 2`, an SDK v2 agent, which is just an ACP agent. It names a
`command` the node runs as the engine of `jaah-acp-host` from the agent root. The bundle carries no third-party
file: beside the script `command` runs it names its dependencies — `requirements.txt` (hash-pinned) or
`package.json` with `package-lock.json` — and the node installs them there before it starts the engine
(`apps/jaahd/acp_registry_install.py::install_bundle_dependencies`). `entrypoint`, `sdk` and `[runtime] install`
are SDK v1 fields and refused, and so is a `command` script that imports the SDK from a bundle that neither carries
it nor names what installs it. A manifest with no `manifest` key or `manifest = 1` is an SDK v1 agent, refused with
`V1_RETIRED`.
"""
from __future__ import annotations

import json
import os
import posixpath
import re
import shlex
import tomllib
from dataclasses import asdict, dataclass
from typing import Any, Callable, Collection, Mapping, Optional, Union


#: The model personas an agent may serve. A task whose persona is not here never runs an agent.
SDK_PERSONAS: tuple[str, ...] = ("coder", "business_analysis", "architecture", "plan", "finish", "researcher")

AGENT_TYPES: tuple[str, ...] = ("coding", "non_coding", "cli_wrapper")
LANGUAGES: tuple[str, ...] = ("python", "typescript")

MANIFEST_FILENAME = "jaah-agent.toml"

#: `[agent] manifest`: 2 is an ACP agent under `jaah-acp-host`. 1 (or absent) is an SDK v1 agent, refused.
MANIFEST_V1 = 1
MANIFEST_V2 = 2
#: Why an SDK v1 agent is refused at registration and cannot run (`apps/api/app/agent_resolution.py`): the one
#: sentence the registration error, the dispatch refusal and the console's Agents list all show.
V1_RETIRED = "SDK v1 agents are no longer supported — re-register on SDK v2"
#: The SDK v1 keys a manifest may not carry: `[agent]` fields, then `[runtime]`'s.
V1_ONLY_FIELDS: tuple[str, ...] = ("entrypoint", "sdk")

#: Manifest 2: what a bundle whose `command` script imports the SDK must carry beside that script, where the node
#: installs its dependencies (Python's `--target` is the script's folder, `sys.path[0]`; npm's `node_modules` sits
#: beside the lockfile). Python: the dependency list, and the SDK itself unless that list names it
#: (`V2_SDK_PYPI`). Node: the lockfile `npm ci` installs the SDK from. A bundled script (`node agent.mjs` built with
#: the SDK inlined) imports neither.
V2_SDK_IMPORTS: dict[str, re.Pattern[str]] = {
    "python": re.compile(r"^[ \t]*(?:from|import)[ \t]+jaah_agent\b", re.M),
    "node": re.compile(r"""(?:\bfrom\s*|\bimport\s*\(?\s*|\brequire\s*\(\s*)["']@jaah/agent-sdk(?:/[^"']*)?["']"""),
}
V2_SDK_CARRIED: dict[str, tuple[str, ...]] = {
    "python": ("requirements.txt", "jaah_agent/__init__.py"),
    "node": ("package-lock.json",),
}
#: The Python SDK's distribution name: a `requirements.txt` naming it installs the SDK, so `jaah_agent` need not ride.
V2_SDK_PYPI = re.compile(r"^[ \t]*jaah[-_.]agent[-_.]sdk\b", re.I | re.M)
_V2_SCRIPT_SUFFIXES: dict[str, str] = {
    ".py": "python", ".js": "node", ".mjs": "node", ".cjs": "node", ".ts": "node", ".mts": "node", ".cts": "node",
}

_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{1,39}$")
_SEMVER_RE = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)
_MAINTAINER_RE = re.compile(r"^(@[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})|[^<>@]+ <[^<>@\s]+@[^<>@\s]+>)$")
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")

DISPLAY_NAME_MAX = 60
DESCRIPTION_MAX = 280


@dataclass(frozen=True)
class ManifestError:
    """One rule the manifest broke: the field it is about and the sentence an author reads."""

    field: str
    message: str

    def __str__(self) -> str:
        return self.message


@dataclass(frozen=True)
class Manifest:
    """A valid `jaah-agent.toml`. `to_json()` is what `agents.manifest` stores."""

    name: str
    display_name: str
    description: str
    type: str
    language: str
    entrypoint: str
    version: str
    maintainer: str
    personas: tuple[str, ...]
    sdk: str
    install: bool
    #: `[agent] manifest`. `entrypoint` is the `command` as one shell-quoted line, for display.
    manifest: int = MANIFEST_V2
    command: tuple[str, ...] = ()

    def to_json(self) -> dict[str, Any]:
        data = asdict(self)
        data["personas"] = list(self.personas)
        data["command"] = list(self.command)
        return data


def is_acp_manifest(manifest: Optional[Mapping[str, Any]]) -> bool:
    """Whether a stored manifest (`Manifest.to_json()`) is version 2: an ACP agent run under `jaah-acp-host`."""
    return isinstance(manifest, Mapping) and manifest.get("manifest") == MANIFEST_V2


#: `manifest.kind` of an agent installed from the ACP registry (`apps/api/app/acp_registry.py`): no repo, no
#: bundle — its `pin` is what the node installs.
REGISTRY_KIND = "acp_registry"


def is_registry_manifest(manifest: Optional[Mapping[str, Any]]) -> bool:
    """Whether a stored manifest is an ACP registry install's (`REGISTRY_KIND`)."""
    return isinstance(manifest, Mapping) and manifest.get("kind") == REGISTRY_KIND


#: A variable name shaped like a secret. A manifest 2 agent's own variables (`agents.env`) become its ACP launch
#: env, where the node refuses these names (`apps/jaahd/acp/launch.py::SECRET_NAME`, the same pattern: the host's
#: fence is stdlib-only and loads before anything else, so it keeps its own copy, and a test pins the two equal).
SECRET_ENV_NAME = re.compile(r"(_TOKEN|_API_KEY|_SECRET|PASSWORD)$", re.I)


def secret_env_refusals(manifest: Optional[Mapping[str, Any]], env: Optional[Mapping[str, str]]) -> list[str]:
    """One sentence per variable a manifest 2 agent may not carry: a secret-shaped name with a value (the node's
    launch would refuse it). Names the variable, never the value. Empty for any other manifest."""
    if not is_acp_manifest(manifest):
        return []
    return [
        f"{name} is named like a secret, and a manifest 2 agent's variables cannot carry one; store it as a "
        f"credential and have the agent read it with credential.get"
        for name in sorted(env or {}) if (env or {})[name] and SECRET_ENV_NAME.search(name)
    ]


def is_commit_sha(ref: str) -> bool:
    """Whether `ref` is a full 40-hex commit, which never moves (the refresh sweep skips it)."""
    return bool(_SHA_RE.fullmatch(ref or ""))


def short_sha(commit: Optional[str]) -> str:
    return (commit or "")[:7]


def _shown(value: str) -> str:
    """A value as an author typed it, or "nothing" for an empty one."""
    return value if value else "nothing"


def _quoted(value: Any) -> str:
    # `default=str`: TOML has dates and times, which JSON does not.
    return value if isinstance(value, str) else json.dumps(value, default=str)


def _one_of(values: Collection[str]) -> str:
    items = list(values)
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " or " + items[-1]


def v2_sdk_errors(
    command: Collection[str], tree: Collection[str], read: Callable[[str], Optional[str]]
) -> list[ManifestError]:
    """Manifest 2: the node provides no SDK and installs only what a bundle names, so a `command` whose script
    imports the SDK must run from a bundle that carries what `V2_SDK_CARRIED` lists beside it — refused here at
    registration rather than dying at launch. The script is the first word of `command` naming a source file in
    `tree`; `read` returns its text."""
    for word in command:
        script = posixpath.normpath(word)
        kind = _V2_SCRIPT_SUFFIXES.get(posixpath.splitext(script)[1])
        if kind is not None and script in tree:
            break
    else:
        return []
    text = read(script)
    if text is None or not V2_SDK_IMPORTS[kind].search(text):
        return []
    here = posixpath.dirname(script)

    def beside(name: str) -> str:
        return posixpath.normpath(posixpath.join(here, name))

    needed = list(V2_SDK_CARRIED[kind])
    if kind == "python" and beside("requirements.txt") in tree and V2_SDK_PYPI.search(read(beside("requirements.txt")) or ""):
        needed.remove("jaah_agent/__init__.py")
    missing = [_package(m) for m in needed if beside(m) not in tree]
    if not missing:
        return []
    how = ("carry the SDK (jaah_agent) and list its dependencies, hash-pinned, in requirements.txt beside it"
           if kind == "python" else "list the SDK in package.json with a package-lock.json beside it, or bundle it "
                                    "into the script")
    return [ManifestError(
        "command",
        f"`command` runs {script}, which imports the Jaah SDK, but the bundle does not carry "
        f"{_one_of(missing)} beside it: the node installs only the dependencies a bundle names, so {how}",
    )]


def _package(carried: str) -> str:
    """`jaah_agent/__init__.py` → `jaah_agent`; a file name stays as it is."""
    return posixpath.dirname(carried) or carried


def validate_manifest(
    text: str,
    *,
    tree: Optional[Collection[str]] = None,
    read: Optional[Callable[[str], Optional[str]]] = None,
) -> Union[Manifest, list[ManifestError]]:
    """The `jaah-agent.toml` in `text`, or every rule it breaks — one sentence per field, never "invalid". An SDK
    v1 manifest is the one sentence `V1_RETIRED` alone: none of its other rules apply any more.

    `tree`, when given, is every file path under the agent root (relative to it), and `read` returns a root
    file's text, for the check that a bundle carries the SDK its `command` imports (`v2_sdk_errors`)."""
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        return [ManifestError("jaah-agent.toml", f"{MANIFEST_FILENAME} is not valid TOML: {exc}")]
    agent = data.get("agent")
    if not isinstance(agent, dict):
        return [ManifestError("agent", f"{MANIFEST_FILENAME} has no [agent] table")]
    version_key = agent.get("manifest", MANIFEST_V1)
    if type(version_key) is int and version_key == MANIFEST_V1:
        return [ManifestError("manifest", V1_RETIRED)]
    errors: list[ManifestError] = []

    def string(field: str) -> Optional[str]:
        value = agent.get(field)
        if value is None:
            errors.append(ManifestError(field, f"`{field}` is missing from [agent]"))
            return None
        if not isinstance(value, str):
            errors.append(ManifestError(field, f"`{field}` must be text, got {_quoted(value)}"))
            return None
        return value.strip()

    name = string("name")
    if name is not None and not _NAME_RE.fullmatch(name):
        errors.append(ManifestError(
            "name",
            f"`name` must be 2 to 40 lowercase letters, digits or hyphens starting with a letter, got {_shown(name)}",
        ))
    display_name = string("display_name")
    if display_name is not None and not 1 <= len(display_name) <= DISPLAY_NAME_MAX:
        errors.append(ManifestError(
            "display_name", f"`display_name` must be 1 to {DISPLAY_NAME_MAX} characters, got {len(display_name)}"
        ))
    description = agent.get("description", "")
    if not isinstance(description, str):
        errors.append(ManifestError("description", f"`description` must be text, got {_quoted(description)}"))
        description = ""
    elif len(description.strip()) > DESCRIPTION_MAX:
        errors.append(ManifestError(
            "description", f"`description` must be at most {DESCRIPTION_MAX} characters, got {len(description.strip())}"
        ))
    agent_type = string("type")
    if agent_type is not None and agent_type not in AGENT_TYPES:
        errors.append(ManifestError("type", f"`type` must be {_one_of(AGENT_TYPES)}, got {_shown(agent_type)}"))
    language = string("language")
    if language is not None and language not in LANGUAGES:
        errors.append(ManifestError("language", f"`language` must be {_one_of(LANGUAGES)}, got {_shown(language)}"))
    if type(version_key) is not int or version_key != MANIFEST_V2:
        errors.append(ManifestError("manifest", f"`manifest` must be {MANIFEST_V2}, got {_quoted(version_key)}"))
    for field in V1_ONLY_FIELDS:
        if field in agent:
            errors.append(ManifestError(field, f"`{field}` is an SDK v1 field; an SDK v2 agent names `command` instead"))
    command = _command(agent.get("command"), errors)
    entrypoint = shlex.join(command)
    version = string("version")
    if version is not None and not _SEMVER_RE.fullmatch(version):
        errors.append(ManifestError("version", f"`version` must be a semantic version such as 1.2.0, got {_shown(version)}"))
    maintainer = string("maintainer")
    if maintainer is not None and not _MAINTAINER_RE.fullmatch(maintainer):
        errors.append(ManifestError(
            "maintainer",
            f"`maintainer` must be a GitHub handle such as @octocat or \"Name <email>\", got {_shown(maintainer)}",
        ))
    personas = agent.get("personas")
    personas_ok: tuple[str, ...] = ()
    if personas is None:
        errors.append(ManifestError("personas", "`personas` is missing from [agent]"))
    elif not isinstance(personas, list) or not all(isinstance(p, str) for p in personas):
        errors.append(ManifestError("personas", f"`personas` must be a list of persona names, got {_quoted(personas)}"))
    elif not personas:
        errors.append(ManifestError("personas", f"`personas` must list at least one of {', '.join(SDK_PERSONAS)}"))
    else:
        unknown = [p for p in personas if p not in SDK_PERSONAS]
        if unknown:
            errors.append(ManifestError(
                "personas",
                f"`personas` names {', '.join(unknown)}, which an agent cannot serve — use one or more of "
                f"{', '.join(SDK_PERSONAS)}",
            ))
        personas_ok = tuple(dict.fromkeys(personas))
    runtime = data.get("runtime", {})
    if not isinstance(runtime, dict):
        errors.append(ManifestError("runtime", f"[runtime] must be a table, got {_quoted(runtime)}"))
    elif "install" in runtime:
        errors.append(ManifestError(
            "install", "`install` in [runtime] is an SDK v1 field: an SDK v2 agent lists its dependencies in "
                       "requirements.txt or package.json beside its script, and the node installs them"
        ))
    if command and tree is not None and read is not None:
        errors.extend(v2_sdk_errors(command, tree, read))
    if errors:
        return errors
    return Manifest(
        name=name or "", display_name=display_name or "", description=description.strip(), type=agent_type or "",
        language=language or "", entrypoint=entrypoint, version=version or "", maintainer=maintainer or "",
        personas=personas_ok, sdk="", install=False, manifest=MANIFEST_V2, command=command,
    )


def _command(raw: Any, errors: list[ManifestError]) -> tuple[str, ...]:
    """A manifest 2 `command`: a non-empty list of non-empty text, run from the agent root."""
    if raw is None:
        errors.append(ManifestError("command", "`command` is missing from [agent]"))
        return ()
    if not isinstance(raw, list) or not raw or not all(isinstance(a, str) and a.strip() for a in raw):
        errors.append(ManifestError(
            "command", f"`command` must be a list of words such as [\"python3\", \"agent.py\"], got {_quoted(raw)}"
        ))
        return ()
    if any("\x00" in a for a in raw):
        errors.append(ManifestError("command", "`command` must not contain a NUL character"))
        return ()
    return tuple(raw)


def read_agent_root(root: str) -> tuple[Optional[str], set[str]]:
    """`(manifest text or None, every file path relative to root)` of an unpacked agent root — the inputs
    `validate_manifest` takes, read off disk. `node_modules` is skipped: it is an install's output, never part of
    the bundle."""
    tree: set[str] = set()
    for here, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d != "node_modules"]
        rel = os.path.relpath(here, root)
        for name in files:
            tree.add(posixpath.normpath(name if rel == "." else posixpath.join(rel.replace(os.sep, "/"), name)))
    manifest_path = os.path.join(root, MANIFEST_FILENAME)
    text = None
    if os.path.isfile(manifest_path):
        with open(manifest_path, encoding="utf-8", errors="replace") as handle:
            text = handle.read()
    return text, tree
