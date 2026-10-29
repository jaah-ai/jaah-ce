#!/usr/bin/python3
"""agents/subagents.py — THE PROVIDER SUBAGENT CATALOGUE: the Claude runner's subagents, translated into
the native agent definitions of a non-Claude CLI so its runner can delegate the same lanes.

A Claude runner reaches two rosters: the OMC plugin's agents (installed per Claude account under
`<config_dir>/plugins/cache/omc/oh-my-claudecode/<version>/agents/`) and the hub's shipped definitions
(today `recon-explore`). On a node without that plugin, the
globally installed oh-my-codex package supplies its prompts instead. This file translates the available
roster without keeping a copy in this repo, in one of two dialects:

  * `codex` — Codex 0.151.0 reads `$CODEX_HOME/agents/<name>.toml` (`name`, `description`,
    `developer_instructions`, `sandbox_mode`), discovered without a config.toml entry and listed as an
    `agent_type` of the `spawn_agent` tool (measured against a stand-in endpoint).
  * `antigravity` — agy 1.2.11 reads `$HOME/.gemini/config/agents/<name>.md`: Markdown under a YAML frontmatter
    whose `name` and `description` are required (either missing: silently skipped), the body the agent's system
    prompt, its name the frontmatter's (measured with `agy agents` in a scratch home). Spawned by
    `invoke_subagent` with `TypeName` = that name. Written: `model: inherit` (a tier name is also accepted),
    `mainAgent: false` (measured to keep it off `agy agents`, the `--agent` list) and `subagent: true`. NOT
    written: `tools` — agy checks no tool name (`plugin validate` passes any), so a mistyped allowlist would
    strand an agent with no tool and say nothing; read-only is enforced by the hook bridge instead
    (from `READ_ONLY_MANIFEST`). Not yet seen spawned on a live run.

  * `claude` — a community-edition node only (`agent_layer.place_subagents`): the bundle's own definitions copied
    verbatim into `<CLAUDE_CONFIG_DIR>/agents/<name>.md`, under their bare names. The fleet node's Claude runners
    get them from the fleet harness instead.

Read-only agents get a Codex `read-only` marker (the runtime enforces via its PreToolUse guard, since
Codex 0.157.1 does not apply that sandbox setting to children); in both dialects they are listed in
`READ_ONLY_MANIFEST`, which each runtime's PreToolUse fence reads. No model is pinned
(operator decision): a subagent inherits the runner's model, so a plan lacking a named model
never fails a spawn. Only names this file wrote are ever touched (`MANIFEST`); a stale one is removed.

THE CODEX DIRECTORY IS SHARED. `omx setup` (`omx_layer.py`) writes its own Codex-native agents into the same
`$CODEX_HOME/agents/`, and many of its names are OMC's too (`executor`, `planner`, `explore`, …). A
Codex-native definition wins that clash: the manifest records a content hash per file this file wrote,
and a name whose file now carries other content was replaced by another writer — it is yielded (left
alone, dropped from the manifest, never pruned) rather than flapped back to the translation on every
dispatch. The catalogue fills only the names no other writer took (`recon-explore` among them).

Run AS THE ACCOUNT'S OWN UID (`agent_layer.place_subagents`), as a script: stdlib only, no daemon imports.

    python3 subagents.py <codex|antigravity|claude> <dest dir> [--omc-root <config dir>]... [--omx-prompts-root <dir>]
        [--agent-file <md>]...

Prints one verdict line: `subagents=<n> source=<omc version|omx|none> removed=<n> skipped=<names|->`.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import sys
from pathlib import Path

if __package__:
    from . import staged_write
else:  # run by path at the account's uid: this file's own directory is put on `sys.path`
    sys.path.append(os.path.dirname(os.path.abspath(__file__)))   # last: shadowing nothing
    import staged_write  # type: ignore[no-redef]

USAGE = ("usage: subagents.py <codex|antigravity|claude> <dest dir> [--omc-root <config dir>]... "
         "[--omx-prompts-root <dir>] [--agent-file <md>]...")

CODEX = "codex"
ANTIGRAVITY = "antigravity"
CLAUDE = "claude"
DIALECTS = (CODEX, ANTIGRAVITY, CLAUDE)

# Where the OMC plugin lands inside a Claude account config dir (the same path `accounts._hud_setup` reads).
OMC_CACHE = ("plugins", "cache", "omc", "oh-my-claudecode")

# `{name: sha256 of the content this file wrote}`, beside the files. Dot-prefixed and neither `.toml` nor `.md`,
# so no CLI reads it as an agent (Codex reads `*.toml`; agy reads `*.md` and refuses a `.json` agent).
# A legacy manifest is a bare list of names: read as `{name: None}` (ours, hash unknown) and
# rewritten as the object on the next run.
MANIFEST = ".jaah-subagents.json"
READ_ONLY_MANIFEST = ".jaah-readonly-agents.json"

# OMX prompts have no Claude `tools` frontmatter. These roles investigate or review and may not write;
# the other OMX roles keep their normal tools. When OMC is available its own frontmatter is authoritative.
_OMX_READ_ONLY = frozenset({
    "analyst", "api-reviewer", "architect", "code-reviewer", "critic", "dependency-expert",
    "explore", "explore-harness", "information-architect", "performance-reviewer",
    "product-analyst", "quality-reviewer", "quality-strategist", "researcher",
    "security-reviewer", "style-reviewer", "ux-researcher", "verifier", "vision",
})

# The agent-name slug Codex accepts. `\Z`, not `$`.
_NAME_RE = re.compile(r"^[a-z0-9-_]+\Z")
_MANAGED_FILE_RE = re.compile(r"^[a-z0-9-_]+\.(?:toml|md)\Z")

# The Claude tools whose absence makes an agent read-only.
_WRITE_TOOLS = frozenset({"Write", "Edit", "MultiEdit", "NotebookEdit"})

# What an agent with no `tools:` line holds, minus what it `disallowedTools`.
_DEFAULT_TOOLS = ("Read", "Grep", "Glob", "Bash", "Edit", "Write", "WebFetch", "WebSearch")

# How each vendor names the tools a Claude-written prompt mentions — prepended to every prompt, because
# the OMC bodies say `Read`, `Grep`, `Bash` throughout.
_VENDOR_NOTE = {
    CODEX: (
        "You are running as a sub-agent inside the OpenAI Codex CLI. The instructions below were written "
        "for Claude Code: read `Read`/`Grep`/`Glob` as shell reads and searches through `exec_command` "
        "(`cat`, `rg --no-config`, `ls`), `Bash` as `exec_command`, and `Edit`/`Write` as `apply_patch`. "
        "Tools they name that you do not hold (LSP lookups, `Agent`/`Task`, MCP servers) do not exist "
        "here: do that part with the tools you have. You cannot spawn further agents.{scope}{readonly}"
    ),
    ANTIGRAVITY: (
        "You are running as a sub-agent inside Google Antigravity (`agy`). The instructions below were written "
        "for Claude Code: read `Read` as `view_file`, `Grep`/`Glob` as your search and find tools, `Bash` as "
        "`run_command`, and `Edit`/`Write` as your file-editing tools. Tools they name that you do not hold "
        "(LSP lookups, `Agent`/`Task`) do not exist here: do that part with the tools you have. Do not spawn "
        "further agents.{scope}{readonly}"
    ),
}
# A Codex sub-agent inherits the runner's AGENTS.override.md charter and its MCP servers, so it must be told
# it is not the runner: only the runner settles, commits and pushes.
_SCOPE_NOTE = (" You are NOT the fleet runner, whatever any charter you can see says: never call a "
               "control-plane tool (`task_complete`, `task_fail`, …), never commit, push or open a PR — "
               "return your result to the agent that spawned you.")
_READONLY_NOTE = " You are READ-ONLY: never modify a file, and say what you found instead."


# ── reading a Claude agent definition ──────────────────────────────────────────────────────────────

def parse_agent(text: str) -> dict | None:
    """`{name, description, tools, disallowed, body}` from a Claude Code agent file, or None.

    The frontmatter is the flat `key: value` subset every shipped definition uses — no YAML parser,
    which the stdlib does not have. `tools`/`disallowedTools` are comma lists; absent is None."""
    match = re.match(r"\A---\r?\n(.*?)\r?\n---\r?\n?(.*)\Z", text, re.S)
    if not match:
        return None
    meta: dict[str, str] = {}
    for line in match.group(1).splitlines():
        key, sep, value = line.partition(":")
        if sep and key.strip() and not key.startswith((" ", "\t")):
            meta[key.strip()] = value.strip().strip("'\"")
    name, description = meta.get("name", ""), meta.get("description", "")
    if not _NAME_RE.match(name) or not description:
        return None

    def names(key: str) -> tuple[str, ...] | None:
        raw = meta.get(key)
        return None if raw is None else tuple(t.strip() for t in raw.split(",") if t.strip())

    # FAIL CLOSED on a YAML list spelling this flat reader does not parse (`[Write, Edit]`, or an empty
    # value followed by `- Write` lines): read as "no denials" it would hand a reader write tools.
    for key in ("tools", "disallowedTools"):
        if key in meta and (meta[key] == "" or meta[key].startswith("[")):
            return None
    return {"name": name, "description": description, "tools": names("tools"),
            "disallowed": names("disallowedTools") or (), "body": match.group(2).strip(), "text": text}


def effective_tools(agent: dict) -> tuple[str, ...]:
    """The Claude tools this agent actually holds: its allowlist, else the default set, minus denials."""
    held = agent["tools"] if agent["tools"] is not None else _DEFAULT_TOOLS
    return tuple(t for t in held if t not in set(agent["disallowed"]))


def is_read_only(agent: dict) -> bool:
    """True when the agent holds no Claude file-edit tool (the OMC `disallowedTools: Write, Edit` agents
    and `recon-explore`). `Bash` is not an edit tool here, as on Claude."""
    return not (_WRITE_TOOLS & set(effective_tools(agent)))


# ── rendering one vendor's definition ──────────────────────────────────────────────────────────────

def _prompt(agent: dict, dialect: str) -> str:
    note = _VENDOR_NOTE[dialect].format(scope=_SCOPE_NOTE,
                                        readonly=_READONLY_NOTE if is_read_only(agent) else "")
    return f"{note}\n\n{agent['body']}\n"


def _toml_str(value: str) -> str:
    """A TOML basic string. `json.dumps` escapes are a subset of TOML's (`\\n`, `\\"`, `\\uXXXX`); it
    leaves DEL raw, which TOML forbids, so that one is escaped here."""
    return json.dumps(value, ensure_ascii=False).replace("\x7f", "\\u007f")


def render(agent: dict, dialect: str) -> tuple[str, str]:
    """`(file name, content)` for AGENT in DIALECT."""
    if dialect == CLAUDE:
        return f"{agent['name']}.md", agent["text"]
    if dialect == ANTIGRAVITY:
        # A JSON string is a YAML double-quoted scalar, so any description survives the frontmatter whole.
        front = [f"name: {agent['name']}", f"description: {json.dumps(agent['description'], ensure_ascii=False)}",
                 "model: inherit", "mainAgent: false", "subagent: true"]
        # The body opens on an H1, the layout every agent agy was measured to load has.
        return (f"{agent['name']}.md",
                "---\n" + "\n".join(front) + f"\n---\n# {agent['name']}\n\n" + _prompt(agent, dialect))
    lines = [f"name = {_toml_str(agent['name'])}",
             f"description = {_toml_str(agent['description'])}"]
    if is_read_only(agent):
        lines.append('sandbox_mode = "read-only"')
    lines.append(f"developer_instructions = {_toml_str(_prompt(agent, dialect))}")
    return f"{agent['name']}.toml", "\n".join(lines) + "\n"


# ── finding the sources ────────────────────────────────────────────────────────────────────────────

def _version_key(path: Path) -> list:
    # Segments tagged (0, int) / (1, str) so a stray non-numeric segment cannot raise TypeError.
    return [(0, int(t)) if t.isdigit() else (1, t) for t in re.split(r"(\d+)", path.name)]


def newest_omc_agents(roots: list[str]) -> Path | None:
    """The `agents/` dir of the newest OMC version installed under any of ROOTS, or None.

    Newest across ALL roots: accounts upgrade at different times, and the newest is what a freshly
    converged Claude runner on this node spawns. Unreadable roots are skipped, never fatal."""
    found: list[Path] = []
    for root in roots:
        cache = Path(root).joinpath(*OMC_CACHE)
        try:
            # Released versions only: a `5.5.0-beta` would otherwise sort above `5.5.0`.
            found += [p for p in cache.iterdir()
                      if re.match(r"^\d+(\.\d+)*\Z", p.name) and (p / "agents").is_dir()]
        except OSError:
            continue
    return max(found, key=_version_key) / "agents" if found else None


def load_catalogue(omc_agents: Path | None, agent_files: list[str],
                   omx_prompts: Path | None = None) -> tuple[list[dict], list[str]]:
    """`(agents, skipped)`: the OMC agents first, then the hub's files (which win a name clash — they are
    the fleet's own, e.g. `recon-explore`). `skipped` names files that did not parse."""
    paths = sorted(omc_agents.glob("*.md")) if omc_agents else []
    if omc_agents is None and omx_prompts is not None:
        paths = sorted(omx_prompts.glob("*.md"))
    paths += [Path(p) for p in agent_files]
    by_name: dict[str, dict] = {}
    skipped: list[str] = []
    for path in paths:
        try:
            source = path.read_text(encoding="utf-8")
            if omc_agents is None and omx_prompts is not None and path.parent == omx_prompts:
                # OMX frontmatter has a description but no name or tool permissions. Translate its
                # package-owned filename and the known read-only roles into the Claude-shaped input.
                readonly = "disallowedTools: Write, Edit\n" if path.stem in _OMX_READ_ONLY else ""
                source = source.replace("---\n", f"---\nname: {path.stem}\n{readonly}", 1)
            agent = parse_agent(source)
        except (OSError, UnicodeDecodeError):
            agent = None
        if agent is None:
            skipped.append(path.name)
            continue
        by_name[agent["name"]] = agent
    return list(by_name.values()), skipped


# ── writing ────────────────────────────────────────────────────────────────────────────────────────

def _write_if_changed(path: Path, content: str) -> None:
    try:
        if path.read_text(encoding="utf-8") == content and not path.is_symlink():
            return
    except OSError:
        pass
    # Atomic, and replaces a symlink rather than writing through it.
    staged_write.replace_file(str(path), content, mode=0o644)


def _digest(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def _read_manifest(manifest: Path) -> dict[str, str | None]:
    """`{name: recorded hash or None}`. Only a bare `<slug>.<ext>` this file could have written: an entry
    can neither walk out of DEST nor name a directory, and a corrupt one is dropped rather than wedging
    every later run. A legacy list maps every name to None."""
    try:
        loaded = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if isinstance(loaded, list):
        items = [(e, None) for e in loaded]
    elif isinstance(loaded, dict):
        items = list(loaded.items())
    else:
        return {}
    return {name: (digest if isinstance(digest, str) else None) for name, digest in items
            if isinstance(name, str) and _MANAGED_FILE_RE.match(name)}


def install(dialect: str, dest: str, agents: list[dict], *, prune: bool) -> tuple[int, list[str]]:
    """Converge DEST onto AGENTS rendered in DIALECT. Returns `(stale files removed, names left alone)`.

    PRUNE only when the full source was read: a run that lost the OMC source for a moment (the account
    roster unreachable, the newest install unreadable) must not delete the roster a live runner on this
    account is using. A file already at a name this file never wrote is an operator's and is left alone.
    A name this file wrote whose file another writer has since replaced (`omx setup`'s Codex-native
    agents) is yielded the same way: left alone, dropped from the manifest, never pruned."""
    root = Path(dest)
    if root.is_symlink():
        raise OSError(f"{root} is a symlink; refusing to write through it")
    root.mkdir(parents=True, exist_ok=True)
    manifest = root / MANIFEST
    previous = _read_manifest(manifest)
    yielded: set[str] = set()
    for name, digest in list(previous.items()):
        if digest is None:  # legacy entry: hash unknown, still ours
            continue
        try:
            current = hashlib.sha256((root / name).read_bytes()).hexdigest()
        except OSError:
            continue  # missing: still ours, rewritten below if the catalogue still holds it
        if current != digest:
            yielded.add(name)
            del previous[name]
    written: dict[str, str] = {}
    foreign: list[str] = []
    for agent in agents:
        name, content = render(agent, dialect)
        target = root / name
        if name in yielded:
            foreign.append(name)
            continue
        if name not in previous and (target.exists() or target.is_symlink()):
            try:
                ours = target.read_text(encoding="utf-8") == content
            except (OSError, UnicodeDecodeError):
                ours = False
            if not ours:
                foreign.append(name)
                continue
        _write_if_changed(target, content)
        written[name] = _digest(content)
    removed = 0
    for stale in sorted(set(previous) - set(written)) if prune else ():
        try:
            (root / stale).unlink()
            removed += 1
        except FileNotFoundError:
            pass
        except OSError as exc:  # a directory or an unwritable file at that name: leave it, say so
            print(f"subagents.py: could not remove stale {stale}: {exc}", file=sys.stderr)
    kept = written if prune else {**previous, **written}
    _write_if_changed(manifest, json.dumps(dict(sorted(kept.items()))) + "\n")
    if dialect == CLAUDE:
        return removed, foreign  # Claude enforces each agent's own `tools`: no fence reads a sidecar
    readonly = {agent["name"] for agent in agents if is_read_only(agent)}
    sidecar = root / READ_ONLY_MANIFEST
    if not prune:
        try:
            old = json.loads(sidecar.read_text(encoding="utf-8"))
            if isinstance(old, list):
                readonly.update(name for name in old if isinstance(name, str) and _NAME_RE.match(name))
        except (OSError, ValueError):
            pass
    _write_if_changed(sidecar, json.dumps(sorted(readonly)) + "\n")
    return removed, foreign


def main(argv: list[str]) -> int:
    if len(argv) < 2 or argv[0] not in DIALECTS:
        print(USAGE, file=sys.stderr)
        return 2
    dialect, dest, rest = argv[0], argv[1], argv[2:]
    roots: list[str] = []
    files: list[str] = []
    omx_root: Path | None = None
    while rest:
        flag, value, rest = rest[0], (rest[1] if len(rest) > 1 else ""), rest[2:]
        if flag == "--omc-root" and value:
            roots.append(value)
        elif flag == "--agent-file" and value:
            files.append(value)
        elif flag == "--omx-prompts-root" and value:
            omx_root = Path(value)
        else:
            print(f"subagents.py: unexpected argument {flag!r}", file=sys.stderr)
            return 2
    omc = newest_omc_agents(roots)
    omx = omx_root if omc is None and omx_root is not None and omx_root.is_dir() else None
    agents, skipped = load_catalogue(omc, files, omx)
    # Claude's one source is the bundle's own files, read whole on every run.
    removed, foreign = install(dialect, dest, agents, prune=omc is not None or omx is not None or dialect == CLAUDE)
    source = omc.parent.name if omc else "omx" if omx is not None else "none"
    print(f"subagents={len(agents) - len(foreign)} source={source} removed={removed} "
          f"skipped={','.join(skipped + foreign) or '-'}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
