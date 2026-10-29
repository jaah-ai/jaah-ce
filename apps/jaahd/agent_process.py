"""agent_process — launching an SDK agent on this node (architecture §3.4/§3.6/§4.4).

An SDK agent is a second launch kind beside the merge lane's `script` (DD2): the lease intent's `launch` is
`{"kind": "agent", ...}` and names either a content-addressed bundle of the agent's repo at one commit (a
manifest 2 agent, `command` set) or an ACP registry pin (`registry` set) instead of a prompt, an account or a
provider. Either runs as an ACP engine under `jaah-acp-host`. This module owns the node's half of that contract:

* `AgentLaunch.from_payload` — the §3.4 payload, checked field by field. A malformed one, or one carrying
  neither `command` nor `registry`, is refused before anything is built (`AgentLaunchRefused` →
  `refused_precondition`).
* `stage` — what the `lease_slot` executor does BEFORE it claims launch permission: (a) make sure the verified
  bundle is in `<cache>/agent-bundles/<bundle_sha256>/` (fetched from `GET /api/agent/tasks/{id}/agent-bundle`,
  size and sha256 checked, unpacked to a temp dir and renamed into place), (b) materialise the task's
  required inputs and write `inputs.json`, (c) copy the bundle into this run's writable `agent/` dir.
* `acp_launch` — the `AcpLaunch` whose engine is the manifest's command, run from the agent root under
  `jaah-acp-host`. Its credentials come through the extension's `credential.get`, never its environment. A bundle
  carries no third-party code: the host first installs what it declares beside its script (`requirements.txt`,
  `package-lock.json`) into that script's folder, as the session user (`acp_runtime_extras`' `deps`).
* An ACP registry install (`AgentLaunch.registry` set) is an ACP agent with no bundle: `stage` leaves its `agent/`
  dir empty and the host installs the pin there as the session user (`acp_runtime_extras`) before it starts the
  engine.
* `acp_runtime_extras` — every run's dir rides `launch.json`'s `runtime`, and the host removes it when its
  attempt ends.

A BUNDLE that cannot be staged does not fail the intent. A failed lease ack spends no attempt and is shown
nowhere on the task: the CP re-serves it into the same slot until a refusal budget parks it
(`routes/agent/slot_intents.py::_account_for_refusal`), which for a corrupt bundle would loop unseen. So a staging failure
is recorded (`StagedAgent.failure`, `[bundle_unavailable]`/`[bundle_corrupt]`) and the run launches anyway:
its pane only reports that sentence through `task.fail` (`staging_failure_lines`), which ends the attempt exactly
like any runner failure, names it on the task, and never wedges the slot (AC-15). Required INPUTS keep the
existing fail-loud contract of `fleet_dispatch.materialize_prerequisite_artifacts`, as for every runner.

Every write lands as the session user (`session_user`): the bundle cache and the run dir are read and
written by the pane's processes.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import secrets
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import account_auth
import env_helpers
import file_io
import session_user

log = logging.getLogger("jaahd.agent_process")

#: The launch kinds this node can enact, reported in the observation's `platform` block (§3.4): the CP
#: places an agent run only on a node that lists "agent", and a manifest 2 (ACP) agent run only on one that
#: also lists "agent_acp".
LAUNCH_KIND_AGENT = "agent"
LAUNCH_KIND_AGENT_ACP = "agent_acp"
#: The host installs an ACP registry agent's pin (a launch carrying `registry`); the CP places one only here.
LAUNCH_KIND_AGENT_ACP_REGISTRY = "agent_acp_registry"
LAUNCH_KINDS: tuple[str, ...] = (LAUNCH_KIND_AGENT, LAUNCH_KIND_AGENT_ACP, LAUNCH_KIND_AGENT_ACP_REGISTRY)

BUNDLE_ROUTE = "/api/agent/tasks/{task_id}/agent-bundle"
#: The languages a bundled agent may declare (`app/agents.py::LANGUAGES`).
LANGUAGES = ("python", "typescript")

BUNDLE_UNAVAILABLE = "bundle_unavailable"
BUNDLE_CORRUPT = "bundle_corrupt"
#: A manifest 2 agent whose own variables carry a secret-shaped name (`acp.launch.validate_secrets`).
AGENT_ENV_REFUSED = "agent_env_refused"
#: The refusal of a launch that names no ACP engine: neither a manifest 2 `command` nor a registry pin. The node
#: does not ship `tools/manifest_check.py`, so this is a copy of its `V1_RETIRED`, pinned equal by
#: `tests/test_microvm_agent_launch_contract.py`.
NO_ENGINE_REFUSAL = "SDK v1 agents are no longer supported — re-register on SDK v2"

_SHA1 = re.compile(r"^[0-9a-f]{40}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_STAGE_TIMEOUT_S = 300


class AgentLaunchRefused(ValueError):
    """A `kind: "agent"` launch this node will not run: the payload is malformed or names no ACP engine."""


class StagingFailed(Exception):
    """The bundle could not be staged; `sentence` is the named failure the run reports."""

    def __init__(self, code: str, sentence: str) -> None:
        super().__init__(sentence)
        self.code = code
        self.sentence = f"[{code}] {sentence}"


@dataclass(frozen=True)
class AgentLaunch:
    """The §3.4 launch payload, as this node reads it."""

    agent_id: str
    display_name: str
    version: str
    commit: str
    language: str
    subpath: str
    bundle_sha256: str
    bundle_bytes: int
    input_artifacts: tuple = ()
    input_artifacts_dir: str = ""
    #: The running task's org: a registry install's install folder key.
    org_id: str = ""
    #: The agent's own variables (`agents.env`), set on its engine's process. Absent is none.
    env: dict = field(default_factory=dict)
    #: A manifest 2 agent's command, run from the agent root as the engine of `jaah-acp-host`; empty for a
    #: registry install.
    command: tuple = ()
    #: The run's first prompt (the task).
    prompt: str = ""
    #: An ACP registry install: `{"slug", "pin", "agent"}`, the pin the host installs (no bundle); empty otherwise.
    registry: dict = field(default_factory=dict)
    #: A registry install's conformance profile and key route (`apps/api/app/acp_registry.py`).
    profile: str = ""
    key_delivery: str = ""
    #: A registry install's bound API-key account slug (`acp_registry.launch_fields`), never its key: the pane is
    #: leased it, and the host fetches the key from the broker for ACP `authenticate`. "" when none is bound.
    key_account: str = ""

    @classmethod
    def from_payload(cls, launch: object) -> "AgentLaunch":
        if not isinstance(launch, dict) or launch.get("kind") != LAUNCH_KIND_AGENT:
            raise AgentLaunchRefused("not an agent launch")
        if not launch.get("command") and not launch.get("registry"):
            raise AgentLaunchRefused(NO_ENGINE_REFUSAL)
        problems = []

        def text(key: str, *, required: bool = True) -> str:
            value = launch.get(key)
            if value is None and not required:
                return ""
            if not isinstance(value, str) or (required and not value.strip()):
                problems.append(f"`{key}` must be non-empty text")
                return ""
            return value.strip()

        registry = launch.get("registry") or {}
        if not isinstance(registry, dict) or (registry and not (
                isinstance(registry.get("slug"), str) and registry["slug"]
                and isinstance(registry.get("pin"), dict) and isinstance(registry.get("agent", {}), dict))):
            problems.append("`registry` must carry a slug and a pin")
            registry = {}
        if registry and not (isinstance(launch.get("org_id"), str) and launch["org_id"].strip()):
            problems.append("`org_id` must be non-empty text for a registry install (its install folder's key)")
        # A registry install has no bundle: no commit, no archive, and its `language` is its distribution kind.
        bundled = not registry
        commit = text("commit", required=bundled)
        if commit and not _SHA1.fullmatch(commit):
            problems.append("`commit` must be a 40-hex commit")
        sha = text("bundle_sha256", required=bundled).lower()
        if sha and not _SHA256.fullmatch(sha):
            problems.append("`bundle_sha256` must be 64 hex characters")
        language = text("language")
        if bundled and language and language not in LANGUAGES:
            problems.append(f"`language` must be one of {', '.join(LANGUAGES)}")
        command = launch.get("command") or []
        if not isinstance(command, list) or not all(isinstance(a, str) and a and "\x00" not in a for a in command):
            problems.append("`command` must be a list of words")
            command = []
        size = launch.get("bundle_bytes")
        if not bundled and size is None:
            size = 0
        elif not isinstance(size, int) or isinstance(size, bool) or size <= 0:
            problems.append("`bundle_bytes` must be a positive integer")
        artifacts = launch.get("input_artifacts") or []
        if not isinstance(artifacts, list) or not all(isinstance(a, dict) for a in artifacts):
            problems.append("`input_artifacts` must be a list of objects")
        env = launch.get("env") or {}
        if not isinstance(env, dict) or not all(isinstance(k, str) and isinstance(v, str) for k, v in env.items()):
            problems.append("`env` must map a variable name to text")
        values = dict(agent_id=text("agent_id"), display_name=text("display_name"), version=text("version"),
                      subpath=text("subpath", required=False),
                      input_artifacts_dir=text("input_artifacts_dir", required=False),
                      org_id=text("org_id", required=False),
                      profile=text("profile", required=False), key_delivery=text("key_delivery", required=False),
                      key_account=text("key_account", required=False))
        if values["key_account"] and not (registry and account_auth.slug_from_node_credential_name(
                account_auth.NODE_CREDENTIAL_PREFIX + values["key_account"])):
            problems.append("`key_account` must be an account slug, on a registry install only")
        prompt = launch.get("prompt") or ""
        if not isinstance(prompt, str):
            problems.append("`prompt` must be text")
            prompt = ""
        if problems:
            raise AgentLaunchRefused("; ".join(problems))
        return cls(commit=commit, bundle_sha256=sha, language=language, bundle_bytes=int(size),
                   input_artifacts=tuple(artifacts), env=dict(env), command=tuple(command), prompt=prompt,
                   registry=dict(registry), **values)

    def unavailable(self, cause: str) -> StagingFailed:
        return StagingFailed(BUNDLE_UNAVAILABLE, f"Couldn't download {self.display_name} {self.version} (commit "
                                                 f"{self.commit[:7]}) to this node: {cause.rstrip('.')}.")

    def corrupt(self) -> StagingFailed:
        return StagingFailed(BUNDLE_CORRUPT, f"{self.display_name}'s code on this node doesn't match the commit "
                                             f"the control plane recorded.")


@dataclass
class StagedAgent:
    """What `stage` prepared for one run. `failure` set means the bundle is not there and the run will only
    report it."""

    launch: AgentLaunch
    run_dir: str
    agent_dir: str
    inputs_path: str
    failure: str = ""
    inputs: list = field(default_factory=list)


# ── paths ────────────────────────────────────────────────────────────────────────────────────────────────
def launch_kinds() -> tuple[str, ...]:
    """What this node reports it can enact. Staging an agent needs a POSIX shell and the session-user split,
    which a Windows node does not have, so it reports none there."""
    import host_platform  # noqa: PLC0415 — deferred: a leaf read only on the observation path

    return () if host_platform.is_windows() else LAUNCH_KINDS


def bundle_cache_dir(bundle_sha256: str) -> Path:
    """Keyed by the bundle's sha256, never by commit: two agents in one repo never collide."""
    return env_helpers.jaah_dir("cache", "agent-bundles", bundle_sha256)


def new_run_dir(task_id: object) -> Path:
    """A fresh directory per ATTEMPT (`task-<id>-<random>`): a re-served intent or a resumed attempt never
    wipes, and an ending attempt never cleans up, another attempt's copy. The host removes it
    (`acp_runtime_extras`), or the pane does when it only reports a failure (`staging_failure_lines`)."""
    return env_helpers.jaah_dir("run", "agent-runs", f"task-{int(str(task_id))}-{secrets.token_hex(6)}")


# ── staging ──────────────────────────────────────────────────────────────────────────────────────────────
#: The unpack, run as the session user (static) or the guest (MicroVM) — never as root. `tarfile`'s `data`
#: filter refuses absolute names, `..`, links leaving the tree, devices and set-id bits; only regular files
#: and directories are taken (the CP's re-pack holds nothing else) and the total is capped, so a bundle that
#: decompresses into a bomb fails with a sentence. Paths ride the environment, never the script.
UNPACK_SCRIPT = (
    "import os, sys, tarfile\n"
    "cap, total, keep = int(os.environ['JAAH_UNPACK_CAP']), 0, []\n"
    "with tarfile.open(os.environ['JAAH_UNPACK_ARCHIVE'], 'r:gz') as tar:\n"
    "    for member in tar:\n"
    "        if not (member.isfile() or member.isdir()):\n"
    "            continue\n"
    "        total += member.size\n"
    "        if total > cap:\n"
    "            sys.exit(f'it unpacks to more than {cap} bytes')\n"
    "        keep.append(member)\n"
    "    os.makedirs(os.environ['JAAH_UNPACK_DEST'], exist_ok=True)\n"
    "    tar.extractall(os.environ['JAAH_UNPACK_DEST'], members=keep, filter='data')\n"
)
#: The most a bundle may unpack to (the CP's own `agent_bundles.MAX_UNPACKED_BYTES`).
UNPACK_CAP_BYTES = 400 * 1024 * 1024
#: The verified archive's name inside its cache directory.
ARCHIVE_NAME = "bundle.tar.gz"


def _sh(script: str, env: dict, *, timeout: float = _STAGE_TIMEOUT_S) -> tuple[int, str]:
    """Run a fixed shell script as the session user (paths ride the env, never the script)."""
    return _as_session_user(["/bin/sh", "-c", script], env, timeout=timeout)


def _as_session_user(argv: list[str], env: dict, *, timeout: float = _STAGE_TIMEOUT_S) -> tuple[int, str]:
    if session_user.switch_needed():
        proc = session_user.run_as_session_user(argv, env=env, timeout=timeout)
    elif os.geteuid() == 0:
        # A root daemon with no session user to switch to: tenant bytes are never handled as root.
        return 1, "this node has no session user to stage an agent as (refusing to do it as root)"
    else:
        proc = subprocess.run(argv, env={**os.environ, **env}, capture_output=True, text=True, timeout=timeout)
    return proc.returncode, (proc.stderr or "").strip()


class _Hashing:
    def __init__(self, handle, digest) -> None:
        self._handle = handle
        self._digest = digest
        self.total = 0

    def write(self, data: bytes) -> int:
        self._digest.update(data)
        self.total += len(data)
        return self._handle.write(data)


def fetch_bundle(launch: AgentLaunch, task_id: object, handle) -> None:
    """Stream the task's bundle into `handle`, raising `StagingFailed` on a fetch error, a size mismatch or a
    sha256 mismatch. The route serves the task's OWN recorded commit only (`routes/agent/task_reads.py`)."""
    import control_plane_api  # noqa: PLC0415 — deferred: network sibling, needed only here

    digest = hashlib.sha256()
    sink = _Hashing(handle, digest)
    try:
        control_plane_api.agent_api_stream_bytes(BUNDLE_ROUTE.format(task_id=int(str(task_id))), sink,
                                                 cap=launch.bundle_bytes + 1)
    except Exception as exc:  # noqa: BLE001 — every fetch failure is one named sentence
        if sink.total > launch.bundle_bytes:
            # More bytes than recorded tripped the stream cap: not an outage, the wrong bytes.
            raise launch.corrupt() from exc
        raise launch.unavailable(str(exc) or type(exc).__name__) from exc
    if sink.total != launch.bundle_bytes or digest.hexdigest() != launch.bundle_sha256:
        log.warning("agent bundle for task %s: got %d bytes / sha256 %s, the CP recorded %d / %s", task_id,
                    sink.total, digest.hexdigest(), launch.bundle_bytes, launch.bundle_sha256)
        raise launch.corrupt()


def _verified(path: Path, launch: AgentLaunch) -> bool:
    """Whether `path` is a regular file (never followed through a link) holding exactly the recorded bytes."""
    fd = file_io.open_regular(str(path))
    if fd is None:
        return False
    digest, total = hashlib.sha256(), 0
    try:
        while chunk := os.read(fd, 1 << 20):
            digest.update(chunk)
            total += len(chunk)
            if total > launch.bundle_bytes:
                return False
    finally:
        os.close(fd)
    return total == launch.bundle_bytes and digest.hexdigest() == launch.bundle_sha256


def ensure_bundle(launch: AgentLaunch, task_id: object) -> Path:
    """`<cache>/agent-bundles/<bundle_sha256>/bundle.tar.gz`, fetched if absent — and RE-HASHED on every use.
    The cache is the session user's to write like everything a runner touches, so a cached archive is
    trusted only while its bytes still match the recorded sha256; one that does not is fetched again. What
    is cached is the verified archive, never an unpacked tree a run could quietly edit for the next one."""
    root = bundle_cache_dir(launch.bundle_sha256)
    archive = root / ARCHIVE_NAME
    if _verified(archive, launch):
        return archive
    # Group-readable (the one gid the daemon and the session user share, `session_user.session_gid`): the archive
    # is the session user's to write, but the DAEMON re-hashes it below and a MicroVM host reads it into the
    # launch share — owner-only modes fail both with EACCES (`[bundle_unavailable] … could not keep it`).
    err = session_user.ensure_dir_as_session_user(str(root), mode=0o750)
    if err:
        raise launch.unavailable(f"could not create {root}: {err}")
    staged = root / f".{secrets.token_hex(6)}.tar.gz"
    failure: list[StagingFailed] = []

    def produce(handle) -> None:
        try:
            fetch_bundle(launch, task_id, handle)
        except StagingFailed as exc:
            failure.append(exc)
            raise

    try:
        err = session_user.stream_file_as_session_user(str(staged), produce, mode=0o640, timeout=_STAGE_TIMEOUT_S)
    except StagingFailed:
        err = ""
    except Exception as exc:  # noqa: BLE001 — the spool or the delegation broke
        err = str(exc) or type(exc).__name__
    if failure or err:
        _sh('rm -f -- "$A"', {"A": str(staged)})
        if failure:
            raise failure[0]
        raise launch.unavailable(f"could not write it to {root}: {err}")
    # A rename over the name: a concurrent stager's archive is the same bytes, so the last rename wins safely.
    rc, out = _sh('mv -f -- "$S" "$A"', {"S": str(staged), "A": str(archive)})
    if rc != 0 or not _verified(archive, launch):
        _sh('rm -f -- "$S"', {"S": str(staged)})
        raise launch.unavailable(f"could not keep it in {root} ({out or f'exit {rc}'})")
    return archive


def _inputs_document(launch: AgentLaunch, inputs_dir: str) -> list[dict]:
    """`[{kind, name, path}]` of the materialised inputs — what the SDK's `task().inputs` reads."""
    rows = []
    for art in launch.input_artifacts:
        name = Path(str(art.get("filename") or "")).name
        if name:
            rows.append({"kind": str(art.get("kind") or ""), "name": name, "path": str(Path(inputs_dir) / name)})
    return rows


def stage(launch: AgentLaunch, task_id: object) -> StagedAgent:
    """The three pre-claim steps (§4.4): the verified bundle in the cache, the required inputs and
    `inputs.json`, and the bundle unpacked into this attempt's own writable `agent/` dir. Raises only when the
    run dir or the required inputs cannot be staged (the existing fail-loud contract); a bundle failure is
    RECORDED, never raised (module docstring)."""
    import fleet_dispatch  # noqa: PLC0415 — deferred: `fleet_dispatch` imports this module

    run_dir = new_run_dir(task_id)
    agent_dir = run_dir / "agent"
    rc, out = _sh('mkdir -p -- "$R/agent" && chmod 0700 -- "$R"', {"R": str(run_dir)})
    if rc != 0:
        raise RuntimeError(f"could not prepare the agent run dir {run_dir}: {out or f'exit {rc}'}")
    failure = ""
    try:
        # A registry install has no bundle: the host installs its pin into the empty `agent/` dir.
        if not launch.registry:
            archive = ensure_bundle(launch, task_id)
            rc, out = _as_session_user(
                [fleet_dispatch.PYTHON, "-I", "-c", UNPACK_SCRIPT],
                {"JAAH_UNPACK_ARCHIVE": str(archive), "JAAH_UNPACK_DEST": str(agent_dir),
                 "JAAH_UNPACK_CAP": str(UNPACK_CAP_BYTES)})
            if rc != 0:
                raise launch.unavailable(
                    f"could not unpack it into {agent_dir} ({(out or f'exit {rc}').splitlines()[-1]})")
    except StagingFailed as exc:
        failure = exc.sentence
        log.warning("agent launch for task %s: %s", task_id, failure)
    staged = StagedAgent(launch=launch, run_dir=str(run_dir), agent_dir=str(agent_dir),
                         inputs_path=str(run_dir / "inputs.json"), failure=failure)
    try:
        # Required inputs, fail-loud, exactly as a model runner's.
        if launch.input_artifacts_dir or launch.input_artifacts:
            fleet_dispatch.materialize_prerequisite_artifacts(list(launch.input_artifacts),
                                                              launch.input_artifacts_dir)
        staged.inputs = _inputs_document(launch, launch.input_artifacts_dir)
        err = session_user.write_file_as_session_user(staged.inputs_path, json.dumps(staged.inputs), mode=0o600)
        if err:
            raise RuntimeError(f"could not write {staged.inputs_path}: {err}")
    except BaseException:
        discard(staged)
        raise
    return staged


def discard(staged: StagedAgent) -> None:
    """Remove a staged run that will not launch (a refused claim, a failed input). Best effort."""
    rc, out = _sh('rm -rf -- "$R"', {"R": staged.run_dir})
    if rc != 0:
        log.warning("could not remove the unlaunched agent run dir %s: %s", staged.run_dir, out)


# ── an ACP agent under `jaah-acp-host` ───────────────────────────────────────────────────────────────────
#: The first prompt's file in the run dir (static) or the launch share (MicroVM): `launch.json`'s `prompt_path`.
PROMPT_FILE = "prompt.md"


def acp_argv(launch: AgentLaunch, agent_dir: str) -> tuple[str, ...]:
    """The engine argv: the manifest's command, run from the agent root (`env -C`) because the host starts every
    engine in the worktree, which is the session's `cwd`."""
    return ("env", "-C", agent_dir, "--", *launch.command)


def acp_launch(staged: StagedAgent) -> "agents.AcpLaunch":
    """A manifest 2 agent's `AcpLaunch`: its command as the engine, its own variables as the env (a secret-shaped
    one is refused, since no secret may sit in an agent's environment: a credential reaches it through
    `credential.get`), and the control MCP server every ACP launch carries. `_meta["jaah.ai"]` tells it which
    agent it is and where its required inputs are. A bundle's declared dependencies are installed by the host before
    it starts the command (`acp_runtime_extras`' `deps`); a registry install names no engine here: the host installs
    it first (`acp_runtime_extras`) and runs what that returns."""
    import agents  # noqa: PLC0415 — deferred: the adapter package is heavy and this kind is rare
    from agents import agent_layer, base  # noqa: PLC0415

    launch = staged.launch
    command, *args = agent_layer.jaah_mcp_command()
    return agents.AcpLaunch(
        argv=() if launch.registry else acp_argv(launch, staged.agent_dir),
        env=dict(launch.env),
        session_meta={"agent_id": launch.agent_id, "agent_version": launch.version, "agent_commit": launch.commit,
                      "inputs_path": staged.inputs_path},
        mcp_servers=(base.control_mcp_server(command, args, {}),),
    )


def acp_runtime_extras(staged: StagedAgent) -> dict:
    """`launch.json`'s `runtime` fields for an agent run, beside `kind`: the attempt's run dir, which the host
    removes when the attempt ends (`acp.host.remove_run_dir`), and for a registry install what the host installs
    (`acp_registry_install.install` into the empty `agent/` dir, as the pane's session user), its profile, its
    key route and the slug of the account whose key the host sends in `authenticate` (`key_account`); for a bundle,
    the dependencies the host installs beside its script first (`deps`,
    `acp_registry_install.install_bundle_dependencies`)."""
    launch = staged.launch
    extras: dict = {"run_dir": staged.run_dir}
    if not launch.registry:
        extras["deps"] = {"dest": staged.agent_dir, "command": list(launch.command), "name": launch.display_name}
    else:
        extras.update(profile=launch.profile, key_delivery=launch.key_delivery,
                      **({"key_account": launch.key_account} if launch.key_account else {}), install={
            "slug": launch.registry["slug"], "org_id": launch.org_id, "pin": launch.registry["pin"],
            "agent": launch.registry.get("agent") or {}, "dest": staged.agent_dir,
        })
    return extras


def acp_launch_or_failure(staged: StagedAgent) -> "tuple[agents.AcpLaunch | None, str]":
    """`(acp_launch(staged), "")`, or `(None, sentence)` when the run must only report its failure: a bundle that
    was not staged, or agent variables `acp_launch` refuses (named, never valued) — never a launch that raises
    and leaves the lease to be re-served."""
    if staged.failure:
        return None, staged.failure
    try:
        return acp_launch(staged), ""
    except ValueError as exc:
        return None, f"[{AGENT_ENV_REFUSED}] {staged.launch.display_name}'s variables: {exc}"


#: How long the failure lines wait for the launcher's claim, polled every half second: a `task.fail` sent while
#: the task is still `ready`, or before the daemon knows the pane, is refused and the named reason lost.
CLAIM_POLLS = 120


def staging_failure_lines(sentence: str, session_id: str, run_dir: str = "") -> tuple[str, ...]:
    """A run that must only report its failure: the pane waits until the task shows claimed, removes the
    attempt's `run_dir` (no host runs to remove it), then reports the named sentence as this attempt's
    `task.fail` (`jaah-call`, under the pane's runner identity, fenced by `session_id`)."""
    import shlex  # noqa: PLC0415

    claimed = "jaah-call task.get 2>/dev/null | grep -Eq '\"state\": *\"(active|awaiting_response)\"'"
    return (
        f"for _ in $(seq {CLAIM_POLLS}); do {claimed} && break; sleep 0.5; done",
        *((f"rm -rf -- {shlex.quote(run_dir)}",) if run_dir else ()),
        f"exec jaah-call task.fail {shlex.quote(json.dumps({'reason': sentence, 'session_id': session_id}))}",
    )

