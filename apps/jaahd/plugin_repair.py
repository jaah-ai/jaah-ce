#!/usr/bin/python3
"""plugin_repair.py — converge every Claude account on this node onto the agent config the fleet needs:
the plugins (OMC, context7, playwright, jaah) and the managed `settings.json` block (hooks, model, flags).

Run by the package's postinst on every install and upgrade; directly runnable:

    python3 /usr/lib/jaahd/apps/jaahd/plugin_repair.py --all            # repair + network converge
    python3 /usr/lib/jaahd/apps/jaahd/plugin_repair.py --all --offline  # path repair only

  1. REPAIR (offline, sub-second; postinst runs it synchronously). The CLI's plugin registries record
     absolute paths and never re-derive them, so a moved tree makes every plugin "failed to load:
     cache-miss". Each path is re-rooted at the config dir that contains it; entries whose payload is
     gone are dropped so phase 2 reinstalls them. The managed settings block converges here too.
  2. CONVERGE (network, best-effort; postinst detaches it, since `apt-get install jaah=<good>` is the
     emergency recovery path and must not depend on a registry). Failures are reported, never raised.
     First, node level: ONE PLUGIN ROOT PER NODE TREE (`share_plugin_root`) — every account's `plugins` is
     a link to `<JAAH_HOME>/plugins`, seeded by renaming the newest account copy, so the cache and both
     registries exist once per node (a vnode's inside its own tree) and the registries record the shared
     root's paths (`plugins_owner`). A real copy that reappears is re-linked next run; each swap that freed
     disk sends one `plugin_dedup` disk report.

Never raises past `main`; per-account failures are isolated. Stdlib only (runs from the packaged tree).
"""
from __future__ import annotations

import argparse
import contextlib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from pathlib import Path

# Stdlib-only sibling. Its directory is appended (never inserted, so it cannot shadow a stdlib module)
# because a test execs this file with nothing of apps/jaahd on
# the path.
_HERE = str(Path(__file__).resolve().parent)
if _HERE not in sys.path:
    sys.path.append(_HERE)
import disk_events  # noqa: E402 — same
import env_helpers  # noqa: E402 — same
import file_io  # noqa: E402 — same
import environment  # noqa: E402 — after the sys.path append above, by necessity
import host_platform  # noqa: E402 — same
import runner_env  # noqa: E402 — same
from jaah_env import env_get  # noqa: E402 — renamed env vars

# ── what a fleet account is supposed to have ────────────────────────────────────────────────────────
# The single home for the fleet's plugin set (`accounts.py` imports it). Sources are what
# `claude plugin marketplace add` takes; `jaah` is an absolute local path the package installs (no
# network, always this node's version). That path is byte-pinned against
# the fleet-harness template's `extraKnownMarketplaces.jaah.source.path` by
# a test, so this stays a dict of literals; `marketplaces()` is
# what this module reads.
MARKETPLACES: dict[str, str] = {
    "claude-plugins-official": "anthropics/claude-plugins-official",
    "omc": "https://github.com/Yeachan-Heo/oh-my-claudecode.git",
    "jaah": "/usr/lib/jaahd/apps/jaahd/mcp",
}
OMC_MARKETPLACE_URL = MARKETPLACES["omc"]
# `<plugin>@<marketplace>`. Split because the account-add wizard requires OMC and treats the MCP
# pair as best-effort.
OMC_PLUGIN = "oh-my-claudecode@omc"
MCP_PLUGINS: tuple[str, ...] = (
    "context7@claude-plugins-official",
    "playwright@claude-plugins-official",
    "jaah@jaah",
)
WANTED_PLUGINS: tuple[str, ...] = (OMC_PLUGIN, *MCP_PLUGINS)
# Every runner starts in a freshly-leased worktree, so anything narrower than `user` misses it.
_FLEET_SCOPE = "user"

# Only these subtrees are re-rootable; an unrecognised shape is left exactly as found.
_REHOMEABLE_SUBTREES = ("cache", "marketplaces")

# Generous (`marketplace add` clones) but bounded: the caller is a package script.
_CLI_TIMEOUT_S = 180.0

# The settings a fleet account must carry, as a JSON template shipped in the bundle — config an
# operator can diff against a live `settings.json`, not hook commands buried in string escapes.
MANAGED_SETTINGS_PATH = Path(__file__).resolve().parent / "fleet_account_settings.json"


def marketplaces() -> dict[str, str]:
    """The marketplaces a repair pass may actually add ON THIS NODE.

    The remote sources come back untouched. The local `jaah` path is relocated by
    `$JAAH_MARKETPLACE_ROOT`, and dropped on a sandbox that names no root. The dict stays the pinned
    literal (see `MARKETPLACES`), which is why `environment.marketplace_roots` takes it as an argument.
    Called, never bound at import: the root is an env var. Where the bundle is not at the package path
    (Windows, macOS, a community-edition node: `runner_env.lib_root_relocated`) the local source is re-rooted at the
    bundle first (`runner_env.rehome_lib_paths`): the pinned path is the Linux package's, which does not exist
    there, and an absent marketplace would leave every runner without its control tools."""
    return environment.marketplace_roots({name: rehomed(source) for name, source in MARKETPLACES.items()})


def rehomed(source: str) -> str:
    """`source` re-rooted at this node's bundle when it names the package path and the bundle is elsewhere
    (`runner_env.lib_root_relocated`); unchanged otherwise, a remote source included."""
    if runner_env.lib_root_relocated() and source.startswith(runner_env.PACKAGED_LIB_PREFIX):
        return runner_env.rehome_lib_paths(source, host_platform.paths().lib_root)
    return source


def _log(msg: str) -> None:
    print(f"plugin-repair: {msg}", file=sys.stderr)


# ── JSON helpers ────────────────────────────────────────────────────────────────────────────────────

# $JAAH_HOME is shared by `jaahd` and `jaah` via group `jaah` + setgid dirs + UMask=0002
# (the package's postinst, D9); a registry rewritten 0644 by one could never be rewritten by the other. An
# existing file keeps its own mode; only a created file takes this.
SHARED_FILE_MODE = 0o664


def _write_json(path: Path, data) -> None:
    """Atomic replace (runners read these concurrently); an existing file's mode is restated, since the
    helper's `mkstemp` temp file starts 0600 and the replace would otherwise narrow it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        mode = path.stat().st_mode & 0o777
    except OSError:
        mode = SHARED_FILE_MODE
    env_helpers.atomic_write_json(path, data, mode=mode, indent=2)


# ── phase 1: offline path repair ────────────────────────────────────────────────────────────────────

def rehome_path(recorded: str | None, config_dir: Path) -> str | None:
    """`recorded` re-rooted at `config_dir`, or None when it is not a path this function may touch.

    `config_dir` is the dir whose `plugins/` holds the payload (`plugins_owner`: the account's own dir, or
    the node tree's `$JAAH_HOME` once its `plugins` is the shared root). Keep everything from the last
    `/plugins/` segment (`rfind`: a config dir may itself sit under a `plugins` dir) and prefix
    `config_dir`. Unchanged when already under `<config_dir>/plugins/`; None when empty or its tail is not
    one of `_REHOMEABLE_SUBTREES`.
    """
    if not recorded or not isinstance(recorded, str):
        return None
    base = str(config_dir).rstrip("/")
    if recorded.startswith(base + "/plugins/"):
        return recorded
    marker = "/plugins/"
    idx = recorded.rfind(marker)
    if idx < 0:
        return None
    tail = recorded[idx + len(marker):].strip("/")
    if not tail or tail.split("/", 1)[0] not in _REHOMEABLE_SUBTREES:
        return None
    return f"{base}/plugins/{tail}"


def plugins_owner(config_dir: Path) -> Path:
    """The dir registry paths are re-rooted at: `config_dir`, or — when its `plugins` is a link (the node
    tree's shared root) — the dir holding the link's target.

    The CANONICAL root, so every account's repair writes the same paths into the one shared registry and
    no two accounts' converges flip-flop it between their own spellings."""
    link = config_dir / "plugins"
    try:
        target = Path(os.readlink(link))
    except OSError:
        return config_dir
    target = target if target.is_absolute() else config_dir / target
    # Only this tree's own root (`<JAAH_HOME>/plugins`, JAAH_HOME being `<config_dir>/../..`): a link pointing
    # anywhere else never moves the registry there.
    if target.name != "plugins" or os.path.realpath(target.parent) != os.path.realpath(config_dir.parent.parent):
        return config_dir
    return target.parent


def repair_marketplaces(config_dir: Path) -> dict:
    """Re-root every `installLocation` in `<config_dir>/plugins/known_marketplaces.json`.

    Nothing is ever deleted: an entry's `source` is the only record of where it came from. A missing
    directory is phase 2's job (`marketplace add` re-fetches in place)."""
    path = config_dir / "plugins" / "known_marketplaces.json"
    data = file_io.read_json_or(path, None)
    if not isinstance(data, dict):
        return {"rehomed": [], "error": None if not path.exists() else "unreadable"}
    rehomed = []
    owner = plugins_owner(config_dir)
    for name, rec in data.items():
        if not isinstance(rec, dict):
            continue
        fixed = rehome_path(rec.get("installLocation"), owner)
        if fixed and fixed != rec.get("installLocation"):
            rec["installLocation"] = fixed
            rehomed.append(name)
    if rehomed:
        _write_json(path, data)
    return {"rehomed": rehomed, "error": None}


def repair_installed_plugins(config_dir: Path) -> dict:
    """Re-root every `installPath` in `<config_dir>/plugins/installed_plugins.json`, and DROP the
    entries whose payload is not there after re-rooting.

    The drop is what lets phase 2 fix a missing plugin: `claude plugin install` is a no-op for one the
    registry already claims. A key with no surviving entries is removed. An entry is kept whenever its
    re-rooted directory exists, whatever its scope."""
    path = config_dir / "plugins" / "installed_plugins.json"
    data = file_io.read_json_or(path, None)
    if not isinstance(data, dict):
        return {"rehomed": [], "dropped": [], "error": None if not path.exists() else "unreadable"}
    plugins = data.get("plugins")
    if not isinstance(plugins, dict):
        return {"rehomed": [], "dropped": [], "error": "no plugins map"}

    rehomed: list[str] = []
    dropped: list[str] = []
    dirty = False
    owner = plugins_owner(config_dir)
    for key in list(plugins):
        entries = plugins[key]
        if not isinstance(entries, list):
            continue
        kept = []
        for entry in entries:
            if not isinstance(entry, dict):
                kept.append(entry)
                continue
            recorded = entry.get("installPath")
            fixed = rehome_path(recorded, owner) or recorded
            if not fixed or not os.path.isdir(fixed):
                dropped.append(key)
                dirty = True
                continue
            if fixed != recorded:
                entry["installPath"] = fixed
                rehomed.append(key)
                dirty = True
            kept.append(entry)
        if kept:
            plugins[key] = kept
        else:
            del plugins[key]
    if dirty:
        _write_json(path, data)
    return {"rehomed": sorted(set(rehomed)), "dropped": sorted(set(dropped)), "error": None}


def installed_plugin_keys(config_dir: Path, scope: str | None = None) -> set[str]:
    """Plugin ids recorded in this config dir's `installed_plugins.json`. Empty on any
    missing/malformed file — the same tolerant read `accounts._installed_plugin_keys` does.

    `scope` narrows to ids with at least one entry at that scope. The fleet cares about `user`: a
    plugin recorded only at `project` scope applies to those paths alone, so a runner in a fresh
    worktree gets none of it while a scope-blind check reports it installed."""
    data = file_io.read_json_or(config_dir / "plugins" / "installed_plugins.json", None)
    plugins = data.get("plugins") if isinstance(data, dict) else None
    if not isinstance(plugins, dict):
        return set()
    if scope is None:
        return {str(k) for k in plugins}
    return {str(k) for k, entries in plugins.items()
            if isinstance(entries, list)
            and any(isinstance(e, dict) and e.get("scope") == scope for e in entries)}


def known_marketplaces(config_dir: Path) -> dict:
    """`known_marketplaces.json` as a dict, `{}` on any missing/malformed file."""
    data = file_io.read_json_or(config_dir / "plugins" / "known_marketplaces.json", None)
    return data if isinstance(data, dict) else {}


def enable_plugins(config_dir: Path, plugins) -> list[str]:
    """Set `enabledPlugins[<id>] = true` in `<config_dir>/settings.json` for each id given, and
    return the ones newly turned on.

    Only ever called with plugins confirmed present in the registry: enabling one the CLI cannot load
    turns a missing plugin into a startup error. Other settings are preserved key-for-key."""
    path = config_dir / "settings.json"
    settings = file_io.read_json_or(path, None)
    if not isinstance(settings, dict):
        settings = {}
    enabled = settings.get("enabledPlugins")
    if not isinstance(enabled, dict):
        enabled = {}
    newly = [p for p in plugins if enabled.get(p) is not True]
    if not newly:
        return []
    for plugin in newly:
        enabled[plugin] = True
    settings["enabledPlugins"] = enabled
    _write_json(path, settings)
    return newly


def load_managed_settings(path: Path | None = None) -> dict:
    """The managed settings template as a dict, `{}` when it is missing or malformed.

    Tolerant: an unparseable template degrades to "manage nothing" rather than taking the repair down."""
    path = path or MANAGED_SETTINGS_PATH
    try:
        text = path.read_text(encoding="utf-8")
        if runner_env.lib_root_relocated():
            # The template's hook commands name the package path; a Windows, macOS or community-edition node's
            # standard lives in the bundle (`runner_env.rehome_lib_paths`), and the fleet-harness reconcile
            # re-roots the same way, so the two writers of an account's settings.json agree.
            text = runner_env.rehome_lib_paths(text, host_platform.paths().lib_root)
        # Its bare `python3` commands run on the node's interpreter, as the fleet-harness reconcile rehomes them.
        data = json.loads(runner_env.rehome_python(text, runner_env.python_cmd()))
    except (OSError, ValueError):
        data = None
    return data if isinstance(data, dict) else {}


def converge_settings(config_dir: Path, managed: dict | None = None) -> list[str]:
    """Write every key of the managed template into `<config_dir>/settings.json`, and return the names
    of the keys that actually CHANGED (empty = already converged).

    Whole-key replacement, not a deep merge: `hooks` is a list of groups of shell text, and merging
    would either duplicate a hook every converge or keep a stale one forever. Keys outside the template
    are preserved — `enable_plugins` owns `enabledPlugins` and `accounts._hud_setup` owns `statusLine`,
    so neither is in the template. Returns changed key names so the summary says which drifted."""
    managed = load_managed_settings() if managed is None else managed
    if not managed:
        return []
    path = config_dir / "settings.json"
    settings = file_io.read_json_or(path, None)
    if not isinstance(settings, dict):
        # Includes a config dir with no settings.json at all; `_write_json` creates it.
        settings = {}
    changed = [key for key, value in managed.items() if settings.get(key) != value]
    if not changed:
        return []
    settings.update(managed)
    _write_json(path, settings)
    return changed


# ── the managed-settings consent ────────────────────────────────────────────────────────────────────
#
# The second prompt a fleet account must never see (`skipDangerousModePermissionPrompt` answers the
# first): "Managed settings require approval". A runner launched into it registers no session and claims
# no Task, so the CP relaunches it into the same prompt — measured on prod 2026-09-01, one task relaunched
# 13 times in 14 minutes, marching to `system_failed` for a reason not its own.
#
# The consent is keyed on the Claude `accountUuid` (re-pointing a dir at another account invalidates it)
# and `dangerousSettingsHash`, which is identical fleet-wide, so a managed-settings edit invalidates every
# account at once. We cannot compute that hash (16 reconstructions failed), so it is CARRIED: from a
# consenting peer on this node, else `JAAH_REMOTE_SETTINGS_CONSENT_HASH` in `/etc/jaah/jaahd.env`
# (a fresh node); with neither, "unknown" and the account is left alone.
CONSENT_FILENAME = "remote-settings-consent.json"
CONSENT_HASH_ENV = "JAAH_REMOTE_SETTINGS_CONSENT_HASH"
# Validated: a malformed carried value written into every account would be a silent fleet-wide wedge.
_CONSENT_HASH_RE = re.compile(r"\A[0-9a-f]{64}\Z")

# The CP's copy of the hash, off every pool tick (`control_plane_api.agent_api_pool_tick`). Daemon-process
# state: the launch path hands it to the account converge in its env (`fleet_dispatch._delegated_account_env`),
# so a node whose env was never seeded (self-hosted, a vnode) still gets it. Like the env seed it ranks
# BELOW a consenting peer (`consent_hash`), and once written into an account it is a peer record that
# `main.resolve_remote_settings_consent` publishes — an echo of the CP's own current value, a no-op there.
_carried_lock = threading.Lock()
_carried_hash: str | None = None


def is_consent_hash(value: object) -> bool:
    """Is VALUE a well-formed `dangerousSettingsHash` (64 lowercase hex)?"""
    return isinstance(value, str) and _CONSENT_HASH_RE.fullmatch(value) is not None


def take_consent_hash_from_tick(body: object) -> None:
    """Keep the pool tick's `remote_settings_consent_hash`. Never raises.

    Absent, `None` or a non-dict body (an older CP, an unreachable CP's `{}`) keeps the last value; a
    malformed one is logged and ignored, never carried into an account."""
    if not isinstance(body, dict):
        return
    received = body.get("remote_settings_consent_hash")
    if received is None:
        return
    if not isinstance(received, str) or not _CONSENT_HASH_RE.fullmatch(received):
        logging.getLogger("jaahd.plugin_repair").warning(
            "ignoring an unusable remote_settings_consent_hash from the pool tick (%s); keeping the last one",
            type(received).__name__)
        return
    global _carried_hash
    with _carried_lock:
        changed, _carried_hash = _carried_hash != received, received
    if changed:
        logging.getLogger("jaahd.plugin_repair").info(
            "managed-settings consent hash received from the control plane; launches carry it from now on")


def carried_consent_hash() -> str | None:
    """The last hash a pool tick delivered, or None before the first."""
    with _carried_lock:
        return _carried_hash


def account_uuid(config_dir: Path) -> str | None:
    """This account's Claude `accountUuid`, or None when the dir holds no logged-in account.

    Read from `.claude.json`'s `oauthAccount`. None is a real answer: a never-logged-in dir has no
    account to consent for."""
    data = file_io.read_json_or(config_dir / ".claude.json", None)
    oauth = data.get("oauthAccount") if isinstance(data, dict) else None
    uuid = oauth.get("accountUuid") if isinstance(oauth, dict) else None
    return uuid if isinstance(uuid, str) and uuid else None


def _consent_records(config_dir: Path) -> dict:
    """`{record_id: record}` from this account's consent file, `{}` when absent or malformed."""
    data = file_io.read_json_or(config_dir / CONSENT_FILENAME, None)
    records = data.get("records") if isinstance(data, dict) else None
    return records if isinstance(records, dict) else {}


def newest_peer_consent(config_dirs) -> tuple[int, str] | None:
    """`(updatedAt, dangerousSettingsHash)` of the newest well-formed consent record across `config_dirs`,
    or None. `updatedAt` is 0 when the record carries none. Newest wins by `updatedAt`: after a settings
    change some accounts still hold the previous hash."""
    newest: tuple[int, str] | None = None
    for config_dir in config_dirs:
        for record in _consent_records(Path(config_dir)).values():
            if not isinstance(record, dict):
                continue
            digest = record.get("dangerousSettingsHash")
            if not isinstance(digest, str) or not _CONSENT_HASH_RE.match(digest):
                continue
            stamp = record.get("updatedAt")
            stamp = stamp if isinstance(stamp, int) else 0
            if newest is None or stamp > newest[0]:
                newest = (stamp, digest)
    return newest


def consent_hash(config_dirs, *, env: dict | None = None) -> str | None:
    """The `dangerousSettingsHash` to consent to, carried rather than computed (see the block above).

    The newest consenting peer (`newest_peer_consent`) wins; the env fallback is for a node with no
    consenting peer at all."""
    newest = newest_peer_consent(config_dirs)
    if newest is not None:
        return newest[1]
    fallback = (env_get(CONSENT_HASH_ENV, "", env if env is not None else os.environ) or "").strip()
    return fallback if _CONSENT_HASH_RE.match(fallback) else None


def account_org_uuid(config_dir: Path) -> str | None:
    """This account's Claude `organizationUuid` (`.claude.json`'s `oauthAccount`), or None when absent.

    The CLI keys its consent records BY THIS ID: it reads `records[organizationUuid]` and honours the record
    only when its `accountUuid` is the live account's (Claude Code 2.1.289, `remote-settings-consent.json`).
    A record under any other key is never found, and the runner sits at "Managed settings require approval"."""
    data = file_io.read_json_or(config_dir / ".claude.json", None)
    oauth = data.get("oauthAccount") if isinstance(data, dict) else None
    org = oauth.get("organizationUuid") if isinstance(oauth, dict) else None
    return org if isinstance(org, str) and org else None


def converge_remote_settings_consent(config_dir: Path, *, settings_hash: str | None) -> str:
    """Record this account's consent to the managed settings. Returns what happened, for the summary:
    `"written"`, `"converged"` (already correct), `"no-account"` (no `accountUuid` or no `organizationUuid`
    yet — both arrive with the CLI's first run), or `"unknown-hash"`.

    The record lives at the key the CLI reads — the account's `organizationUuid` (`account_org_uuid`) —
    holding this `accountUuid` and the hash. Idempotent on that (org, uuid, hash) triple, not on the file's
    existence. This account's records under any OTHER key are dropped: they are the CLI-invisible shape an
    earlier version of this function wrote (a random id), and a stale copy would also be read back as a peer
    by `consent_hash`. Other accounts' records are left untouched."""
    if settings_hash is None:
        return "unknown-hash"
    uuid = account_uuid(config_dir)
    org = account_org_uuid(config_dir)
    if uuid is None or org is None:
        return "no-account"
    document = consent_document(_consent_records(config_dir), org=org, uuid=uuid, settings_hash=settings_hash)
    if document is None:
        return "converged"
    _write_json(config_dir / CONSENT_FILENAME, document)
    return "written"


def consent_document(records: dict, *, org: str, uuid: str, settings_hash: str) -> dict | None:
    """The consent file RECORDS should become for account UUID consenting to SETTINGS_HASH under ORG, or None
    when they already say so (`converge_remote_settings_consent`'s rules: this account's misfiled records
    dropped, other accounts' kept, the record stamped with this host's clock)."""
    records = dict(records)
    current = records.get(org)
    misfiled = [rid for rid, r in records.items()
                if rid != org and isinstance(r, dict) and r.get("accountUuid") == uuid]
    if (isinstance(current, dict) and current.get("accountUuid") == uuid
            and current.get("dangerousSettingsHash") == settings_hash and not misfiled):
        return None
    for rid in misfiled:
        del records[rid]
    records[org] = {
        "accountUuid": uuid,
        "dangerousSettingsHash": settings_hash,
        "updatedAt": int(time.time() * 1000),
    }
    return {"version": 1, "records": records}


def consent_gap(config_dir: Path) -> str:
    """Why the CLI would show "Managed settings require approval" in CONFIG_DIR, or "" when it holds the record
    the CLI reads (`account_org_uuid`). The hash is not checked: only the CLI can tell a stale one.

    A MicroVM guest's own approval lands in its VM's copy, which no peer scan reads (`discover_config_dirs`), so
    an org whose accounts run only in VMs never learns a hash and its host layers stay at "unknown-hash"."""
    uuid, org = account_uuid(config_dir), account_org_uuid(config_dir)
    if uuid is None or org is None:
        return "its .claude.json names no oauthAccount accountUuid and organizationUuid"
    record = _consent_records(config_dir).get(org)
    if not isinstance(record, dict) or record.get("accountUuid") != uuid:
        return "no consent record for its organization and account"
    return ""


def repair(config_dir: Path, *, settings_hash: str | None = None) -> dict:
    """Phase 1 whole: both registries re-rooted, then the wanted plugins that SURVIVED enabled.

    Enabling here too makes `--offline` a complete repair. `settings_hash` is resolved once by the
    caller over every config dir, so two accounts in one sweep cannot consent to different hashes."""
    marketplaces = repair_marketplaces(config_dir)
    installed = repair_installed_plugins(config_dir)
    present = installed_plugin_keys(config_dir)
    enabled = enable_plugins(config_dir, [p for p in WANTED_PLUGINS if p in present])
    # After `enable_plugins`: this re-reads `settings.json` and would otherwise drop what it just added.
    settings = converge_settings(config_dir)
    return {
        "marketplaces_rehomed": marketplaces["rehomed"],
        "plugins_rehomed": installed["rehomed"],
        "plugins_dropped": installed["dropped"],
        "enabled": enabled,
        "settings": settings,
        "consent": converge_remote_settings_consent(config_dir, settings_hash=settings_hash),
        "errors": [e for e in (marketplaces["error"], installed["error"]) if e],
    }


# ── phase 2: network converge ───────────────────────────────────────────────────────────────────────

def claude_bin() -> str | None:
    """The `claude` executable, or None when this node has none.

    With a `~/.local/bin` fallback: a package script's PATH is dpkg's, so `shutil.which` alone finds
    nothing under `systemd-run`/`runuser`."""
    found = shutil.which("claude")
    if found:
        return found
    fallback = Path.home() / ".local" / "bin" / "claude"
    return str(fallback) if os.access(fallback, os.X_OK) else None


def _run(argv: list[str], env: dict, timeout: float) -> dict:
    """One bounded CLI call → `{exit_code, stdout, stderr}`; a timeout or missing binary is a non-zero
    result, never a raise."""
    try:
        proc = subprocess.run(argv, env=env, capture_output=True, text=True, timeout=timeout,
                              stdin=subprocess.DEVNULL)
        return {"exit_code": proc.returncode, "stdout": proc.stdout, "stderr": proc.stderr}
    except subprocess.TimeoutExpired:
        return {"exit_code": 124, "stdout": "", "stderr": f"timed out after {timeout:g}s"}
    except OSError as exc:
        return {"exit_code": 127, "stdout": "", "stderr": str(exc)}


def converge(config_dir: Path, *, binary: str, timeout: float = _CLI_TIMEOUT_S,
             run=_run) -> dict:
    """Phase 2: add missing marketplaces, install missing plugins, update the rest, enable what
    landed.

    Marketplaces first: `claude plugin install` refuses a plugin from an unknown marketplace with a
    misleading "local copy may be out of date" message. Presence is re-read from the registry after
    every install rather than trusted from the exit code, and only confirmed plugins are enabled."""
    # Open obligation, one of the three, and the widest: no store pin at all, so the caller's store var
    # rides in whole. `accounts.py::_claude_cli` carries the note.
    env = {**os.environ, "CLAUDE_CONFIG_DIR": str(config_dir)}
    errors: list[str] = []

    def _cli(*args: str) -> dict:
        res = run([binary, "plugin", *args], env, timeout)
        if res["exit_code"] != 0:
            errors.append(f"{' '.join(args)}: {(res['stderr'] or res['stdout']).strip()[:400]}")
        return res

    known = known_marketplaces(config_dir)
    added: list[str] = []
    # `marketplaces()`, not the raw dict: on a sandbox the local `jaah` entry is contained or absent.
    for name, source in marketplaces().items():
        location = (known.get(name) or {}).get("installLocation") if isinstance(
            known.get(name), dict) else None
        # A missing directory too: the `cache-miss` phase 1 cannot fix by re-rooting; `add` is idempotent.
        if name not in known or not location or not os.path.isdir(location):
            if _cli("marketplace", "add", source)["exit_code"] == 0:
                added.append(name)
    # Refresh each catalog, then update each plugin; best-effort per item.
    for name in marketplaces():
        _cli("marketplace", "update", name)

    # User scope throughout, presence included (see `installed_plugin_keys`).
    present = installed_plugin_keys(config_dir, scope=_FLEET_SCOPE)
    installed: list[str] = []
    updated: list[str] = []
    for plugin in WANTED_PLUGINS:
        if plugin in present:
            if _cli("update", plugin, "--scope", _FLEET_SCOPE, "--yes")["exit_code"] == 0:
                updated.append(plugin)
        elif _cli("install", plugin, "--scope", _FLEET_SCOPE, "--yes")["exit_code"] == 0:
            installed.append(plugin)

    # Normally a no-op; catches entries whose payload only arrived with this pass's installs.
    repaired = repair(config_dir)
    deps = install_omc_runtime_deps(config_dir, timeout=timeout, run=run)
    present = installed_plugin_keys(config_dir, scope=_FLEET_SCOPE)
    confirmed = [p for p in WANTED_PLUGINS if p in present]
    return {
        "marketplaces_added": added,
        "installed": [p for p in installed if p in present],
        "updated": updated,
        "confirmed": confirmed,
        "missing": [p for p in WANTED_PLUGINS if p not in present],
        "enabled": repaired["enabled"],
        "settings": repaired["settings"],
        "omc_deps_installed": deps["installed"],
        "errors": errors + repaired["errors"] + deps["errors"],
    }


# The marker npm writes into `node_modules` once an install completes; absent means none or a killed one.
_NPM_DONE_MARKER = Path("node_modules") / ".package-lock.json"


def omc_install_paths(config_dir: Path) -> list[Path]:
    """Every distinct OMC payload dir the registry records, any scope — the ones a session may load."""
    data = file_io.read_json_or(config_dir / "plugins" / "installed_plugins.json", None)
    plugins = data.get("plugins") if isinstance(data, dict) else None
    entries = plugins.get(OMC_PLUGIN) if isinstance(plugins, dict) else None
    paths = {e.get("installPath") for e in entries or [] if isinstance(e, dict)}
    return sorted(Path(p) for p in paths if isinstance(p, str) and os.path.isdir(p))


# Written into a payload dir whose `npm install` failed; removed by a later success.
_NPM_FAILED_MARKER = ".jaah-npm.failed"


def omc_payloads_missing_deps(config_dir: Path, *, fixable_only: bool = False) -> list[Path]:
    """The recorded OMC payload dirs whose runtime dependencies are not installed. Read-only, stat calls only:
    `provision_accounts.needs_converge` asks it every probe tick, with `fixable_only` — leaving out the dirs
    a converge refuses (a `.npmrc`) or already failed in, which would otherwise re-converge forever. A new
    payload version is a new dir, and the deploy-time converge retries a failed one."""
    return [p for p in omc_install_paths(config_dir)
            if (p / "package.json").is_file() and not (p / _NPM_DONE_MARKER).is_file()
            and not (fixable_only and (os.path.lexists(p / ".npmrc") or (p / _NPM_FAILED_MARKER).exists()))]


def install_omc_runtime_deps(config_dir: Path, *, timeout: float = _CLI_TIMEOUT_S, run=_run) -> dict:
    """Install OMC's production dependencies into each recorded payload dir that lacks them.

    `claude plugin install/update` extracts the plugin WITHOUT `node_modules`; OMC's own fix
    (`scripts/plugin-setup.mjs`) runs only from its setup skill, which no fleet account runs. Without it
    every import of a dependency fails — from 5.7.5 the HUD's `zod` (`[OMC HUD] Plugin HUD load failed
    (Cannot find package 'zod' …)`). The same `npm install --omit=dev --ignore-scripts` OMC runs (its
    `better-sqlite3` native rebuild is skipped: OMC falls back to file locks). Idempotent (skipped once
    npm's completion marker is there); best-effort.

    The payload dir is group-writable by session users and this runs as the daemon, so a `.npmrc` there
    is refused and the registry and git binary are pinned in the env, which outranks project config:
    `--ignore-scripts` alone does not stop a planted `git=` from running. One install per dir at a time
    (`flock`): two sweeps racing on one prefix can leave a marked-done broken tree."""
    result: dict = {"installed": [], "errors": []}
    paths = omc_payloads_missing_deps(config_dir)
    if not paths:
        return result
    npm = shutil.which("npm")
    if not npm:
        result["errors"].append(f"no `npm` on PATH — OMC runtime deps missing in {', '.join(map(str, paths))}")
        return result
    env = {**os.environ, "CLAUDE_CONFIG_DIR": str(config_dir),
           "npm_config_registry": "https://registry.npmjs.org/", "npm_config_git": "git"}
    for path in paths:
        if os.path.lexists(path / ".npmrc"):
            result["errors"].append(f"{path}/.npmrc present — refusing to run npm there")
            continue
        try:
            with open(path / ".jaah-npm.lock", "a") as lock:
                host_platform.lock(lock)
                if (path / _NPM_DONE_MARKER).is_file():   # a peer sweep finished it while we waited
                    continue
                res = run([npm, "install", "--prefix", str(path), "--omit=dev", "--ignore-scripts",
                           "--no-audit", "--no-fund"], env, timeout)
        except OSError as exc:
            result["errors"].append(f"npm install in {path}: {exc}")
            continue
        try:
            if res["exit_code"] == 0:
                (path / _NPM_FAILED_MARKER).unlink(missing_ok=True)
            else:
                (path / _NPM_FAILED_MARKER).write_text(f"{int(time.time())}\n")
        except OSError:
            pass  # the marker only throttles the re-converge; the result below is what reports
        if res["exit_code"] == 0:
            result["installed"].append(str(path))
        else:
            result["errors"].append(f"npm install in {path}: {(res['stderr'] or res['stdout']).strip()[:400]}")
    return result


# ── account discovery ───────────────────────────────────────────────────────────────────────────────

# Any one marks a Claude config dir. `.claude.json` is the first file the CLI writes, so it catches an
# account seeded but never logged in (without it such a dir was never discovered, silently).
_CONFIG_DIR_MARKERS = ("plugins", "settings.json", ".credentials.json", ".claude.json")


def discover_config_dirs(jaah_home: str | None, *, home: Path | None = None) -> list[Path]:
    """Every Claude config dir this node owns: the fleet accounts under `<jaah_home>/auth/*`, and
    nothing else.

    Filesystem discovery, not the DB roster: this runs under `dpkg --configure`, where the CP may be
    unreachable, and the on-disk state being repaired is authoritative anyway.

    The invoking user's own `~/.claude` is never discovered — postinst runs on every upgrade, and the
    operator's account is not fleet state. `home=` is accepted and ignored (signature kept). So the
    operator's account is never a `consent_hash` source; a fleet account consenting once is the fix."""
    out: list[Path] = []
    seen: set[str] = set()

    def _add(path: Path) -> None:
        resolved = str(path).rstrip("/")
        if resolved not in seen:
            seen.add(resolved)
            out.append(Path(resolved))

    if jaah_home:
        auth = Path(jaah_home).expanduser() / "auth"
        try:
            children = sorted(auth.iterdir())
        except OSError:
            children = []
        for child in children:
            # A dot-dir is never an account (`auth/.shared` is the accounts NodeLink's mount).
            if (not child.name.startswith(".") and child.is_dir()
                    and any((child / m).exists() for m in _CONFIG_DIR_MARKERS)):
                _add(child)

    return out


def resolve_jaah_home(explicit: str | None) -> str | None:
    """`--jaah-home`, else `$JAAH_HOME`, else None.

    No `/etc/jaah/jaahd.env` parse and no guess: `env_helpers.jaah_home()` owns that resolution,
    and postinst passes the value in. The raw `environment.pool_root_raw()` accessor, so the
    normalisation and the `explicit or …` precedence stay on this line."""
    value = (explicit or environment.pool_root_raw() or "").strip().rstrip("/")
    return value or None


# ── one plugin root per node tree ───────────────────────────────────────────────────────────────────
#
# Every account's `plugins` is a link to `<JAAH_HOME>/plugins`: one copy of the cache and of both registries
# per node tree, where it used to be one ~580 MB copy per account. Per-account state — `settings.json`
# (`enabledPlugins` included), credentials, transcripts, `projects/` — stays in the account dir. A link, not
# `CLAUDE_CODE_PLUGIN_CACHE_DIR` (every launch site would need the var, and one that missed it would
# silently load no plugins) and not hardlinks. Every `claude` call runs as the one session user, so the shared
# tree has one owner. A co-located vnode's `JAAH_HOME` is inside its own tree, so it gets its own root.

# A real `plugins` dir is renamed to this (plus the pid) before its link goes in, then deleted. One left
# behind by a killed run is swept by the next.
_REPLACED_PREFIX = ".plugins.replaced-"


def shared_plugin_root(jaah_home: str | Path) -> Path:
    """The node tree's one plugin root."""
    return Path(jaah_home) / "plugins"


def _in_node_tree(config_dir: Path, jaah_home: Path) -> bool:
    """Whether `config_dir` is an account dir directly under `<jaah_home>/auth` — the only dirs this tree's
    root may be linked into, so no link ever crosses into another node's (or vnode's) tree."""
    return Path(os.path.realpath(config_dir)).parent == Path(os.path.realpath(jaah_home / "auth"))


def _is_real_dir(path: Path) -> bool:
    return path.is_dir() and not path.is_symlink()


def _links_to(link: Path, root: Path) -> bool:
    """Whether `link` is a link resolving to `root` — by resolved path, so two spellings of one JAAH_HOME
    never re-point every link on every run."""
    return link.is_symlink() and os.path.realpath(link) == os.path.realpath(root)


def _registry_mtime(config_dir: Path) -> float:
    try:
        return (config_dir / "plugins" / "installed_plugins.json").stat().st_mtime
    except OSError:
        return -1.0


def _replace_with_link(link: Path, root: Path) -> None:
    """Make `link` a link to `root` atomically (a temp link renamed over it), so it never goes missing."""
    tmp = link.with_name(f".plugins.link-{os.getpid()}")
    with contextlib.suppress(FileNotFoundError):
        tmp.unlink()
    os.symlink(str(root), tmp)
    os.replace(tmp, link)


def _merge_registry(account_plugins: Path, root: Path, name: str, owner: Path) -> None:
    """Add to the shared registry `name` the entries only this account's copy records, re-rooted at the
    shared root, so a plugin or marketplace one account added alone still resolves after its copy goes."""
    mine = file_io.read_json_or(account_plugins / name, None)
    shared_path = root / name
    shared = file_io.read_json_or(shared_path, None)
    if not isinstance(mine, dict) or not isinstance(shared, dict):
        return
    if name == "installed_plugins.json":
        mine, theirs, field = mine.get("plugins"), shared.setdefault("plugins", {}), "installPath"
    else:
        theirs, field = shared, "installLocation"
    if not isinstance(mine, dict) or not isinstance(theirs, dict):
        return
    added = False
    for key, value in mine.items():
        if key in theirs:
            continue
        for rec in value if isinstance(value, list) else [value]:
            if isinstance(rec, dict):
                rec[field] = rehome_path(rec.get(field), owner) or rec.get(field)
        theirs[key] = value
        added = True
    if added:
        _write_json(shared_path, shared)


def _keep_unique(account_plugins: Path, root: Path) -> None:
    """Move into the shared root what only this account's copy holds — each `cache/<mkt>/<plugin>/<version>`,
    `data/<plugin>` and `marketplaces/<name>` the root lacks (same filesystem: a rename, never a copy) — and
    merge the registry entries the root lacks. A live session still resolves the version it loaded through
    the new link, and a plugin's own persistent data survives."""
    for pattern in ("cache/*/*/*", "data/*", "marketplaces/*"):
        for item in sorted(account_plugins.glob(pattern)):
            target = root / item.relative_to(account_plugins)
            if not _is_real_dir(item) or os.path.lexists(target):
                continue
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                os.rename(item, target)
            except OSError as exc:
                _log(f"{item}: not kept in the shared root ({exc})")
    for name in ("installed_plugins.json", "known_marketplaces.json"):
        _merge_registry(account_plugins, root, name, root.parent)


def link_plugin_root(config_dir: Path, root: Path) -> int | None:
    """Point `<config_dir>/plugins` at `root`, returning the bytes a replaced real copy held (0 when there was
    none to replace), or None when it already pointed there. Raises OSError, for the caller to report; a
    copy whose link could not go in is put back."""
    link = config_dir / "plugins"
    if _links_to(link, root):
        return None
    if not _is_real_dir(link):
        _replace_with_link(link, root)
        return 0
    _keep_unique(link, root)
    replaced = config_dir / f"{_REPLACED_PREFIX}{os.getpid()}"
    os.rename(link, replaced)
    try:
        os.symlink(str(root), link)
    except OSError:
        with contextlib.suppress(OSError):
            os.rename(replaced, link)
        raise
    freed = disk_events.tree_bytes(replaced)
    shutil.rmtree(replaced, ignore_errors=True)
    return freed


def link_new_account(config_dir: Path, jaah_home: str | None) -> bool:
    """Link a NEW account's absent `plugins` at its tree's shared root, so the add flow installs into the one
    copy instead of downloading another. Replaces nothing (the converge owns that) and needs the root to
    exist already (the converge seeds it, as the session user). Returns whether it linked; never raises."""
    if not jaah_home or host_platform.is_windows():
        return False
    home = Path(jaah_home)
    root = shared_plugin_root(home)
    link = config_dir / "plugins"
    try:
        if os.path.lexists(link) or not _is_real_dir(root) or not _in_node_tree(config_dir, home):
            return False
        # Relative to the account dir opened WITHOUT following a link: the add flow runs in the daemon, and
        # a dir swapped for a link after the check above must not get a link created through it.
        dir_fd = os.open(config_dir, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.symlink(str(root), "plugins", dir_fd=dir_fd)
        finally:
            os.close(dir_fd)
        return True
    except OSError:
        return False


def _used_pct(path: Path) -> float:
    try:
        return disk_events.used_pct(str(path))
    except OSError:
        return 0.0


def _post_via_daemon(report: dict) -> bool:
    """The upload leg for `disk_events.post_disk_report`. This runs as the session user, who holds no node
    credential, so the node's daemon makes the call (`disk.report`, `agent_vocabulary`)."""
    import agent_socket  # noqa: PLC0415 — only a swap that freed something reports
    try:
        agent_socket.call("disk.report", report)
    except Exception as exc:  # noqa: BLE001 — a lost report is logged, never a failed converge
        _log(f"plugin_dedup disk report not delivered ({type(exc).__name__}: {exc})")
        return False
    return True


def share_plugin_root(jaah_home: str | Path, config_dirs, *, report=None) -> dict:
    """Converge `config_dirs` onto the node tree's one plugin root: seed the root if absent, link every
    account's `plugins` at it, and report what the swap freed. Idempotent; never raises.

    SEED: an absent root is the account `plugins` dir with the newest `installed_plugins.json`, RENAMED into
    place (no copy); with no account dir to seed from, an empty root. Dirs outside `<jaah_home>/auth` are
    skipped. One `converge` disk report (`disk_events.post_disk_report`, or `report`) per run that replaced a
    real copy, covering every account it swapped: `plugin_dedup` freed, `plugin_root` remaining."""
    home = Path(jaah_home)
    root = shared_plugin_root(home)
    result: dict = {"root": str(root), "seeded_from": None, "linked": [], "freed_bytes": 0, "errors": []}
    try:
        lock = open(home / ".plugins.lock", "a")  # noqa: SIM115 — held for the whole share
    except OSError as exc:
        result["errors"].append(f"{root}: {exc} — plugins stay per account")
        return result
    with lock:
        # Two converges (postinst's detached one, a provision run) must not swap the same dirs at once.
        host_platform.lock(lock)
        return _share_locked(home, root, config_dirs, result, report)


def _share_locked(home: Path, root: Path, config_dirs, result: dict, report) -> dict:
    started, used_before = disk_events.utc_now(), _used_pct(home)
    try:
        dirs = [Path(d) for d in config_dirs if _in_node_tree(Path(d), home)]
        if root.is_symlink() or (os.path.lexists(root) and not root.is_dir()):
            result["errors"].append(f"{root} is not a directory — plugins stay per account")
            return result
        if not root.exists():
            seeds = [d for d in dirs if _is_real_dir(d / "plugins")]
            if seeds:
                seed = max(seeds, key=_registry_mtime)
                os.rename(seed / "plugins", root)
                # At once, not when the loop reaches it: until then a session there would load no plugins.
                os.symlink(str(root), seed / "plugins")
                result["seeded_from"] = str(seed)
            else:
                root.mkdir()
        for config_dir in dirs:
            # A killed run's leftover copy: keep what only it holds, then drop it.
            for leftover in config_dir.glob(f"{_REPLACED_PREFIX}*"):
                if _is_real_dir(leftover):
                    _keep_unique(leftover, root)
                    shutil.rmtree(leftover, ignore_errors=True)
                else:
                    leftover.unlink()
    except OSError as exc:
        result["errors"].append(f"{root}: {exc} — plugins stay per account")
        return result
    for config_dir in dirs:
        try:
            freed = link_plugin_root(config_dir, root)
        except OSError as exc:
            result["errors"].append(f"{config_dir}: {exc}")
            continue
        if freed is not None or str(config_dir) == result["seeded_from"]:
            result["linked"].append(str(config_dir))
            result["freed_bytes"] += freed or 0
    if result["freed_bytes"]:
        disk_report = {
            "reclaim_id": str(uuid.uuid4()),
            "started_at": started,
            "finished_at": disk_events.utc_now(),
            "trigger": "converge",
            "used_pct_before": used_before,
            "used_pct_after": _used_pct(home),
            "freed_bytes": {"plugin_dedup": result["freed_bytes"]},
            "remaining_bytes": {"plugin_root": disk_events.tree_bytes(root)},
            # Per-account errors stay in this run's log: the socket verb carries no free text (`disk.report`).
            "errors": [],
        }
        if report is not None:
            report(disk_report)
        else:
            disk_events.post_disk_report(disk_report, post=_post_via_daemon)
    return result


# ── entry point ─────────────────────────────────────────────────────────────────────────────────────

def process(config_dir: Path, *, offline: bool, binary: str | None, timeout: float,
            settings_hash: str | None = None) -> dict:
    """One account, both phases, fully isolated: any exception is captured into this account's own
    result so the sweep continues to the next one."""
    # Full shape seeded before anything can fail, so the summary reads the same keys either way.
    result: dict = {"config_dir": str(config_dir), "marketplaces_rehomed": [],
                    "plugins_rehomed": [], "plugins_dropped": [], "enabled": [], "settings": [],
                    "consent": "unknown-hash", "errors": []}
    try:
        result.update(repair(config_dir, settings_hash=settings_hash))
        if not offline and binary:
            result["converge"] = converge(config_dir, binary=binary, timeout=timeout)
            # Re-attempted: `.claude.json` (where `accountUuid` lives) is written by the CLI forks of
            # the plugin phase, so a fresh dir's first `repair` can only answer `no-account` — and without
            # a consent the next launch wedges at the approval prompt. `unknown-hash` is node-level and
            # cannot change here.
            if result.get("consent") == "no-account":
                result["consent"] = converge_remote_settings_consent(
                    config_dir, settings_hash=settings_hash)
    except Exception as exc:  # noqa: BLE001 — a per-account failure is data, not a crash
        result["errors"] = [*result["errors"], f"{type(exc).__name__}: {exc}"]
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="plugin_repair",
        description="Repair and converge the fleet's Claude accounts — plugins (OMC, context7, "
                    "playwright, jaah) plus the managed settings block (hooks, model).")
    parser.add_argument("--all", action="store_true",
                        help="every fleet account under $JAAH_HOME/auth (and nothing else — the "
                             "invoking user's own ~/.claude is never discovered)")
    parser.add_argument("--config-dir", action="append", default=[], metavar="DIR",
                        help="a specific CLAUDE_CONFIG_DIR (repeatable); implies not --all")
    parser.add_argument("--jaah-home", dest="jaah_home", default=None,
                        help="override $JAAH_HOME for account discovery")
    parser.add_argument("--offline", action="store_true",
                        help="phase 1 only — re-root the registries and converge the managed "
                             "settings block, touch no network")
    parser.add_argument("--timeout", type=float, default=_CLI_TIMEOUT_S, metavar="SECONDS",
                        help=f"per `claude plugin` call wall clock (default {_CLI_TIMEOUT_S:g})")
    parser.add_argument("--json", action="store_true", help="emit the full result as JSON on stdout")
    args = parser.parse_args(argv)

    # The daemon unit's UMask=0002 (the package's postinst, D9): files this process or its `claude plugin`
    # children create must stay group-rewritable, and a maintainer script's umask is dpkg's 022.
    os.umask(0o002)

    if args.config_dir:
        dirs = [Path(d).expanduser() for d in args.config_dir]
    elif args.all:
        dirs = discover_config_dirs(resolve_jaah_home(args.jaah_home))
    else:
        parser.error("one of --all or --config-dir is required")

    binary = None if args.offline else claude_bin()
    if not args.offline and not binary:
        # Not an error: registries still get re-rooted.
        _log("no `claude` on PATH — phase 1 (path repair) only")

    # Once, over every dir on this node, not just the requested ones: a targeted `--config-dir` run
    # (the launch path, the first-sight pass) holds no consenting peer in its own set, so the hash would
    # always be None and a new account would wedge at the approval prompt. Requested dirs come first.
    jaah_home = resolve_jaah_home(args.jaah_home)
    peers = [d for d in discover_config_dirs(jaah_home) if d not in dirs]
    settings_hash = consent_hash(list(dirs) + peers)
    # Node level, BEFORE the per-account loop, so each account's repair re-roots onto the shared registry
    # and only the first account's install/update does any work. Online only: the first swap deletes a
    # copy per account, which is no work for postinst's synchronous phase or a launch's offline phase. A
    # peer joins only with a real plugin tree of its own (never a provider's home).
    if not args.offline and jaah_home and not host_platform.is_windows():
        shared = share_plugin_root(jaah_home, list(dirs) + [
            p for p in peers if _is_real_dir(p / "plugins")
            and (p / "plugins" / "installed_plugins.json").is_file()])
        if shared["seeded_from"] or shared["linked"] or shared["errors"]:
            _log(f"shared plugin root {shared['root']}: "
                 + (f"seeded from {shared['seeded_from']}, " if shared["seeded_from"] else "")
                 + f"{len(shared['linked'])} account(s) linked, {shared['freed_bytes']} bytes freed"
                 + (f", {len(shared['errors'])} error(s)" if shared["errors"] else ""))
            for err in shared["errors"]:
                _log(f"  {err}")
    if settings_hash is None:
        _log(f"no managed-settings consent hash on this node and no {CONSENT_HASH_ENV} — accounts will "
             "meet the approval prompt on first launch until one account consents (then the next sweep "
             "carries it to the rest)")
    results = [process(d, offline=args.offline, binary=binary, timeout=args.timeout,
                       settings_hash=settings_hash) for d in dirs]
    if args.json:
        print(json.dumps({"results": results}, indent=2))
    for res in results:
        conv = res.get("converge") or {}
        # Union: phase 2 re-runs phase 1 after phase 1 already wrote the settings, so its own list is
        # empty on exactly the run that changed them.
        settings = sorted(set(res["settings"]) | set(conv.get("settings", [])))
        changed = (res["marketplaces_rehomed"] + res["plugins_rehomed"] + res["plugins_dropped"]
                   + res["enabled"] + conv.get("marketplaces_added", [])
                   + conv.get("installed", []) + conv.get("updated", [])
                   + conv.get("omc_deps_installed", []) + settings)
        errors = res.get("errors", []) + conv.get("errors", [])
        consent = res.get("consent")
        _log(f"{res['config_dir']}: "
             + (f"{len(changed)} change(s)" if changed else "already converged")
             # Named: which key drifted is the whole finding.
             + (f" [settings: {', '.join(settings)}]" if settings else "")
             # Named whenever not `converged`: each other value is a different wedge story.
             + (f" [consent: {consent}]" if consent and consent != "converged" else "")
             + (f", {len(errors)} error(s)" if errors else ""))
        for err in errors:
            _log(f"  {res['config_dir']}: {err}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
