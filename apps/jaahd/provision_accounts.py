#!/usr/bin/env python3
"""provision_accounts.py — the single entry point that converges every credential account on THIS node.

Idempotent; five layers in load-bearing order: (1) settings — the fleet-harness reconcile;
(2) consent + managed block — `plugin_repair.py --offline` (consent stops the first-launch approval
prompt; an unresolvable hash is reported loudly); (3) plugins — `plugin_repair.py` online, skipped by
`--offline`; (4) account layer — OMC, HUD, notification policy, MCP (`provision_account`); (5) the
provider's own account layer, via the adapter seam (`converge_provider_layer`: one package refresh per
provider per online run, then `account_layer_converge` per home). 1 before 2: both write
`settings.json`. 3 before 4: `_hud_setup` copies from the OMC plugin cache.

THE SPLIT, by the adapter's `account_layer()` — the same question `fleet_dispatch.launch_runner`
asks at dispatch, one control: "omc" → layers 1-4 (the Claude layer); otherwise layer 1's provider lane
(the shipped skills, no `settings.json`; its native agents are the dispatch-time catalogue's,
`agents/subagents.py`) and layer 5. Layers 2-4 are Claude-only and STOP for a provider home: the adapter
refuses them because their plugin manifests break its own CLI. The provider comes from the roster row,
else is guessed on disk (`provider_of`). A Claude artifact a provider home received from an earlier
provider-blind sweep is LEFT IN PLACE — this sweep adds, never deletes. No vendor is named here: every vendor fact is the adapter's (`agents/`).

Runs on deploy (postinst), on (re)provision (`node_reprovision.py`), and at launch. Accounts: the DB
roster filtered to dirs on disk (a missing dir whose token is in the store is created), else on-disk discovery.
Stdlib only: `node_reprovision.py` installs the deps.

  apps/jaahd/provision_accounts.py                    # converge every account on this node
  apps/jaahd/provision_accounts.py --offline          # everything except the network plugin pass
  apps/jaahd/provision_accounts.py --dry-run          # report presence only, write nothing
  apps/jaahd/provision_accounts.py --config-dir DIR   # one account (repeatable)
"""
from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import sys
from collections.abc import Sequence
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

# Stdlib-only, so importing it keeps this module's stdlib-only contract.
import plugin_repair  # noqa: E402 — after the sys.path insert above, by necessity

# Stdlib-only leaf; the home of env var names and outward-surface defaults.
import environment  # noqa: E402 — after the sys.path insert above, by necessity

# Stdlib-only (plus `environment`); `registry_norm` is the roster's config_dir join key.
import env_helpers  # noqa: E402 — after the sys.path insert above, by necessity

# Stdlib-only leaf: the provider catalogue (each provider's credential file, the default provider).
import agent_providers  # noqa: E402 — after the sys.path insert above, by necessity

# The one home of the capped `{exit_code, stdout, stderr}` reply (`run_capped`).
import session_user  # noqa: E402 — after the sys.path insert above, by necessity

# Generous but finite: marketplace add clones a repo. Not bound by the 170s bus budget — this runs off
# the bus.
PLUGIN_STEP_TIMEOUT_SECONDS = 300.0


def _run(*argv, cwd=None, timeout: float | None = None, env=None) -> dict:
    """The `run` contract `accounts.py`'s helpers expect: `session_user.run_capped`'s shape, caps and
    never-raises arms, with this script's own default timeout."""
    return session_user.run_capped(
        list(argv), timeout=PLUGIN_STEP_TIMEOUT_SECONDS if timeout is None else timeout, cwd=cwd, env=env)


def provision_account(accounts, config_dir: str, *, install: bool, run=_run) -> dict:
    """The four account-layer steps for ONE account, in `_finalize_account_add`'s order. `accounts` is
    passed in so `main` owns the one import that can fail.

    OMC is the only hard step; without it `_hud_setup` has nothing to copy, so this returns early rather
    than writing a dangling statusLine. Never touches the registry's `provisioning` block (the wizard's)."""
    omc = accounts._omc_check(config_dir, install=install, run=run)
    result = {"config_dir": config_dir, "omc": bool(omc["present"]),
              "installed": bool(omc.get("installed")), "hud": None, "notify": None, "mcp": None,
              "error": None}
    if not install:
        return result
    if not omc["present"]:
        result["error"] = (omc.get("stderr") or "").strip() or "omc_install_failed"
        return result
    result["hud"] = accounts._hud_setup(config_dir)
    result["notify"] = accounts._notification_setup(config_dir)
    result["mcp"] = accounts._mcp_plugins_setup(config_dir, run)
    return result


HERE_ROOT = HERE.parent.parent          # the package/repo root: <root>/apps/jaahd/this_file
PLUGIN_REPAIR = HERE / "plugin_repair.py"
HARNESS_RECONCILE = HERE_ROOT / "infra" / "host" / "fleet-harness" / "reconcile.py"


def _jaah_home() -> str:
    """`$JAAH_HOME`, or "" (unset is a skip here, so the non-raising accessor). Passed explicitly:
    the harness reconcile refuses to guess."""
    return environment.pool_root_or_empty()


# How long a detached converge is assumed still running, per account and process, so repeating
# schedulers do not re-clone every tick. Matches postinst's `RuntimeMaxSec=900`.
CONVERGE_COOLDOWN_SECONDS = 900.0


def needs_converge(config_dir: object, *, absent_only: bool = False) -> str:
    """WHY this account is not converged on this node, or "" when it is. Read-only, no fork, no network.

    The scheduling predicate for repeating callers (daemon probe, launch path); checks cheapest first
    and returns the first reason. Repeating schedulers must pass `absent_only`: a MISSING managed key
    breaks a runner, but a DIFFERENT value is usually a live session's write-back (`/model`), and chasing
    values would re-converge forever and clobber it. Values are the deploy's business."""
    if not isinstance(config_dir, str) or not config_dir:
        return ""
    cfg = Path(config_dir).expanduser()
    if not cfg.is_dir():
        # The daemon's `claude agents` probe creates a roster account's dir as a side effect (a bare 48-byte
        # settings.json), after which the account is dispatchable — hookless.
        return "the config dir does not exist on this node yet"
    try:
        settings = json.loads((cfg / "settings.json").read_text())
    except Exception:
        return "settings.json is missing or unreadable"
    if not isinstance(settings, dict):
        return "settings.json is not an object"
    try:
        managed = plugin_repair.load_managed_settings()
    except Exception:
        managed = {}
    if absent_only:
        drifted = [key for key in managed if key not in settings]
        label = "managed settings absent"
    else:
        drifted = [key for key, value in managed.items() if settings.get(key) != value]
        label = "managed settings drift"
    if drifted:
        return f"{label}: {', '.join(sorted(drifted))}"
    try:
        installed = plugin_repair.installed_plugin_keys(cfg)
    except Exception:
        installed = set()
    missing = [name for name in plugin_repair.WANTED_PLUGINS if name not in installed]
    if missing:
        return f"plugins not installed: {', '.join(missing)}"
    # A payload a `claude plugin update` extracted without `node_modules` breaks the HUD (`Cannot find
    # package 'zod'`); only the full converge installs them. Not without `npm`: it could never clear.
    try:
        undeps = plugin_repair.omc_payloads_missing_deps(cfg, fixable_only=True) if shutil.which("npm") else []
    except Exception:
        undeps = []
    if undeps:
        return f"OMC runtime dependencies not installed in {', '.join(map(str, undeps))}"
    # A `statusLine` whose script is absent fails silently every prompt.
    status_line = settings.get("statusLine")
    if isinstance(status_line, dict) and status_line.get("command") \
            and not (cfg / "hud" / "omc-hud.mjs").is_file():
        return "statusLine points at a HUD wrapper that is not there"
    if not (cfg / ".omc-config.json").is_file():
        # No notification policy means `ask-user-question` pages nobody.
        return "no .omc-config.json (so no ask-user-question paging)"
    return ""


def converge_argv(config_dir: str | None, *, offline: bool) -> list[str]:
    """The argv that converges ONE account through this script — every account on the node when
    `config_dir` is None — the single spelling of it."""
    argv = [sys.executable, str(Path(__file__).resolve())]
    if config_dir is not None:
        argv += ["--config-dir", config_dir]
    return argv + ["--offline"] if offline else argv


def detached_command(config_dir: str | None, *, offline: bool = False) -> list[str]:
    """A shell command that starts the converge for one account (every account when `config_dir` is None)
    and returns immediately — the FULL one by
    default, `--offline` (no network plugin pass) when `offline`.

    `nohup … &` inside one `sh -c`, so the converge outlives the caller's request."""
    inner = " ".join(shlex.quote(a) for a in converge_argv(config_dir, offline=offline))
    return ["/bin/sh", "-c", f"nohup {inner} >/dev/null 2>&1 &"]


def converge_settings_layer(run=_run, *, provider_dirs: Sequence[tuple[str, str]] = (),
                            only: Sequence[str] = ()) -> dict:
    """Layer 1 — the canonical `settings.json` over every account, via the fleet-harness reconcile.

    Its stdout is the report. A missing script or unset `$JAAH_HOME` is a SKIP, not a failure.
    `provider_dirs` (`(provider, config_dir)` pairs) ride along as repeated `--provider-dir p=dir`:
    reconcile enumerates the whole auth root itself, so it must be told which homes take a provider's
    lane (skills only, no `settings.json`). `only` (config dirs) rides along
    as repeated `--only dir` and RESTRICTS that enumeration to those dirs — without it a run that names
    one provider home leaves every other provider home on disk on the Claude lane."""
    if not HARNESS_RECONCILE.is_file():
        return {"layer": "settings", "status": "skipped", "detail": f"{HARNESS_RECONCILE} absent"}
    home = _jaah_home()
    if not home:
        return {"layer": "settings", "status": "skipped", "detail": "JAAH_HOME is not set"}
    # `{**os.environ, K: v}` so the var name comes from its single home.
    env = {**os.environ, environment.POOL_ROOT_ENV: home}
    flags: list[str] = []
    for provider, config_dir in provider_dirs:
        flags += ["--provider-dir", f"{provider}={config_dir}"]
    for config_dir in only:
        flags += ["--only", config_dir]
    done = run(sys.executable, str(HARNESS_RECONCILE), *flags,
               timeout=PLUGIN_STEP_TIMEOUT_SECONDS, env=env)
    return {"layer": "settings", "status": "ok" if done["exit_code"] == 0 else "failed",
            "detail": (done["stdout"] or done["stderr"] or "").strip()[-2000:]}


def _account_in(line: str) -> str:
    """The config dir named in one `plugin_repair` log line, else the whole line — never empty (it feeds
    an operator-facing warning)."""
    body = line.split("plugin-repair:", 1)[-1].strip()
    account, _, _rest = body.partition(": ")
    return account.strip() or line.strip()


def converge_plugin_layer(config_dirs: list[str], *, offline: bool, run=_run) -> list[dict]:
    """Layers 2 and 3 — the managed settings block + registries + CONSENT, then the online plugin pass.

    Two invocations: phase 1 is local and safe to wait for; phase 2 hits the network, so callers under a
    lock or on a dispatch path pass `offline`. Phase 2 is not gated on phase 1."""
    if not PLUGIN_REPAIR.is_file():
        return [{"layer": "plugins", "status": "skipped", "detail": f"{PLUGIN_REPAIR} absent"}]
    targeted: list[str] = []
    for config_dir in config_dirs:
        targeted += ["--config-dir", config_dir]
    results = []
    phases = [("consent+block", ["--offline"])] + ([] if offline else [("plugins", [])])
    for name, flags in phases:
        done = run(sys.executable, str(PLUGIN_REPAIR), *targeted, *flags,
                   timeout=PLUGIN_STEP_TIMEOUT_SECONDS)
        detail = (done["stderr"] or done["stdout"] or "").strip()
        results.append({"layer": name, "status": "ok" if done["exit_code"] == 0 else "failed",
                        "detail": detail[-4000:],
                        # Named per account: this state wedges a new account at the approval prompt.
                        "unknown_consent": [_account_in(line) for line in detail.splitlines()
                                            if "unknown-hash" in line]})
    return results


def _roster_dirs(accounts) -> tuple[list[str], dict[str, str]]:
    """`(config_dirs, {config_dir: provider})` from the DB roster, both keyed by `registry_norm` (the
    same join key `reconcile.accounts_probe._auth_dirs_from_roster` uses), deduped preserving order.

    Read through `accounts.account_roster()` (rows carry `provider`); an `accounts` without it (an older
    tree) still yields dirs through `_fleet_auth_dirs`, provider-less, so the on-disk guess decides."""
    roster_fn = getattr(accounts, "account_roster", None)
    if roster_fn is None:
        return [str(d) for d in accounts._fleet_auth_dirs()], {}
    rows, _ok = roster_fn()
    dirs: list[str] = []
    providers: dict[str, str] = {}
    for rec in rows:
        # An API-key account's home is derived here, not served: it takes the Claude layer like any other.
        cd = env_helpers.roster_config_dir(rec)
        if not cd:
            continue
        norm = env_helpers.registry_norm(str(cd))
        if not norm:
            # `registry_norm("/")` is "" — and `--config-dir ''` would make `plugin_repair` discover
            # every dir on disk (as `reconcile.accounts_probe._agent_config_dirs` drops it).
            continue
        if norm not in providers:
            dirs.append(norm)
        providers[norm] = str(rec.get("provider") or "")
    return dirs, providers


def _roster_or_disk(accounts) -> tuple[list[str], dict[str, str]]:
    """The accounts to converge and their providers: the DB roster, or the filesystem (provider-less)
    when the roster cannot be read.

    The fallback is what makes this usable as `jaah` (every packaged caller): the node credential in
    `/etc/jaah/node.env` is unreadable to `jaah`, so the roster 401s with zero rows. The roster is
    preferred when it answers: it alone knows accounts whose dir is not yet on this node."""
    try:
        roster, providers = _roster_dirs(accounts)
    except Exception as exc:
        roster, providers = [], {}
        print(f"  ⚠ [provision-accounts] the DB account roster could not be read ({exc}) — falling back "
              f"to on-disk discovery", file=sys.stderr)
    if roster:
        return roster, providers
    try:
        disk = [str(d) for d in plugin_repair.discover_config_dirs(
            environment.pool_root_or_empty() or None)]
    except Exception as exc:
        print(f"  ⚠ [provision-accounts] on-disk account discovery failed too ({exc})", file=sys.stderr)
        return [], {}
    if disk:
        print(f"  ⚠ [provision-accounts] the DB roster returned no accounts (unreadable node credential, "
              f"or this node is not enrolled) — converging the {len(disk)} account dir(s) found on disk "
              f"instead; an account the roster knows but this node lacks is NOT covered by that",
              file=sys.stderr)
    return disk, {}


def provider_of(config_dir: str, providers: dict[str, str]) -> str:
    """Which provider owns `config_dir`: the roster's `provider`, else a CATALOGUE-driven guess from the
    disk, else the catalogue default.

    The guess is the first `agent_providers.PROVIDERS` entry whose `credential_file` is present under
    the dir — `lexists`, so a linked account's credential symlink still names its provider while the
    store is unreachable (a dangling link is still that provider's home; `.exists()` would follow it to
    nothing and fall to the default). The credential, not `settings.json`, is the discriminator: a
    provider home an earlier provider-blind sweep polluted with Claude files still lacks a Claude
    credential, while its own credential is what made it an account at all. An explicit `--config-dir`
    run reads no roster, so it always guesses."""
    known = providers.get(env_helpers.registry_norm(config_dir)) or providers.get(config_dir)
    if known:
        return known
    home = Path(config_dir).expanduser()
    for provider in agent_providers.PROVIDERS:
        # An API-key-only provider has no credential file; `home / ""` is the dir itself, always present.
        if provider.credential_file and os.path.lexists(home / provider.credential_file):
            return provider.value
    return agent_providers.DEFAULT_PROVIDER


def resolve_config_dirs(accounts, explicit: list[str]) -> tuple[list[str], list[str], dict[str, str]]:
    """Returns (present, absent, providers): the accounts to provision, the roster entries whose
    config_dir does not exist on this node, and `{config_dir: provider}` for the roster's rows (empty on
    an `explicit` run, which overrides the roster entirely — a targeted/manual run — and on the disk
    fallback; `provider_of` then guesses).

    Split, not silently filtered, so a missing local dir never reads as a clean sweep.

    The missing per-node dir of an account whose token is in this node's token store is CREATED (nothing
    else would on a node the operator never logged in from, and it would silently never dispatch there).
    The discriminator is `accounts._account_securestorage_dir` (`shared_storage.securestorage_dir`: the
    store holds this account's token). An `explicit` run is exempt: never create a mistyped dir."""
    candidates, providers = (list(explicit), {}) if explicit else _roster_or_disk(accounts)
    present = [d for d in candidates if Path(d).expanduser().is_dir()]
    if not explicit:
        for d in candidates:
            if d in present or not accounts._account_securestorage_dir(d):
                continue
            try:
                accounts.ensure_account_dir(str(Path(d).expanduser()))
            except OSError as exc:
                # Reported, never raised; it stays in `absent`.
                print(f"  ⚠ [provision-accounts] {d}: token in the store, but its config dir could not be "
                      f"created ({exc}) — it will not dispatch here", file=sys.stderr)
                continue
            print(f"  + [provision-accounts] {d}: created the per-node config dir for an account whose token "
                  f"is in the token store (this dir holds its transcripts and plugins)")
        # Re-derived, not appended, to keep candidate order.
        present = [d for d in candidates if Path(d).expanduser().is_dir()]
    absent = [d for d in candidates if d not in present]
    return present, absent, providers


def split_by_account_layer(present: list[str], providers: dict[str, str], get_adapter) \
        -> tuple[list[str], list[tuple[str, str]], list[tuple[str, str]]]:
    """`(claude_dirs, provider_dirs, unknown)`: which lane each present dir takes, asked of its adapter
    (`account_layer()`), never of a vendor name. `provider_dirs` and `unknown` are
    `(provider, config_dir)` pairs; `unknown` is a provider with no adapter on this node — fail closed:
    neither lane may plant its files into a home it cannot identify."""
    claude_dirs: list[str] = []
    provider_dirs: list[tuple[str, str]] = []
    unknown: list[tuple[str, str]] = []
    for config_dir in present:
        provider = provider_of(config_dir, providers)
        try:
            adapter = get_adapter(provider)
        except Exception:
            unknown.append((provider, config_dir))
            continue
        if adapter.account_layer() == "omc":
            claude_dirs.append(config_dir)
        else:
            provider_dirs.append((provider, config_dir))
    return claude_dirs, provider_dirs, unknown


def converge_provider_layer(entries: list[tuple[str, str]], *, offline: bool, dry_run: bool,
                            run=_run, euid: int | None = None) -> list[dict]:
    """Layer 5 — each provider's own account layer over its homes, through the adapter seam.

    Per PROVIDER among `entries` (first-seen order), once per online non-dry run,
    `account_layer_refresh` into the session user's `~/.local` (its skip is `skipped`, its failure
    `degraded` — REPORTED as a layer line, never `failed`: a node still has last run's packages, and the
    per-account converge decides); then per home `account_layer_converge`, and the verdict is
    `account_layer_missing` re-asked. A `--dry-run` asks only the latter. `run` is adapted to the seam's
    `run(argv, *, env, timeout, cwd)` contract. Returns the refresh lines (`layer`) first, then one
    `config_dir` result per home, `_describe_provider`'d (a converge answering `skipped` — a vendor with
    no layer of its own — carries it through).

    NEVER CONVERGES A HOME AS ROOT (`euid`, the real one unless a test injects it): `node_reprovision.py`
    runs this script as root, and a file root writes under the runner home (the vendor layer's tree, its
    MCP registration's config edit, the policy file) can never be repaired by the runner user. Each home is
    reported with `error: "skipped as root"` and `present` as it stands; postinst runs this as `jaah`."""
    if not entries:
        return []
    import agents  # noqa: PLC0415 — deferred: the adapter registry is a daemon package, not stdlib

    def _adapted(argv, *, env=None, timeout=None, cwd=None):
        return run(*argv, env=env, timeout=timeout, cwd=cwd)

    if euid is None:
        euid = os.geteuid()
    as_root = euid == 0 and not dry_run
    if as_root:
        print(f"  ⚠ [provision-accounts] refusing to converge {len(entries)} provider home(s) as root — "
              f"files root writes under the runner home can never be repaired by the runner user; "
              f"postinst runs this as `jaah`", file=sys.stderr)
    results: list[dict] = []
    if not offline and not dry_run:
        # The script runs as `jaah`, whose `~/.local` is the prefix; an adapter refuses euid 0 itself.
        prefix = str(Path.home() / ".local")
        for provider in dict.fromkeys(p for p, _d in entries):
            refreshed = agents.get_adapter(provider).account_layer_refresh(_adapted, prefix=prefix)
            results.append({"layer": f"{provider} refresh",
                            "status": "ok" if refreshed.get("applied") else
                            ("skipped" if refreshed.get("skipped") else "degraded"),
                            "detail": refreshed.get("skipped") or refreshed.get("error") or ""})
    for provider, config_dir in entries:
        adapter = agents.get_adapter(provider)
        error = ""
        skipped = ""
        if as_root:
            error = "skipped as root"
        elif not dry_run:
            done = adapter.account_layer_converge(config_dir=config_dir, run=_adapted, offline=offline)
            error = str(done.get("error") or "")
            skipped = str(done.get("skipped") or "")
        missing = adapter.account_layer_missing(config_dir=config_dir)
        results.append({"config_dir": config_dir, "provider": provider, "present": not missing,
                        "error": missing or error, "skipped": skipped})
    return results


def _describe_provider(result: dict) -> str:
    """One line per provider home, in `_describe`'s shape, that provider's layer in place of OMC. A
    `skipped` converge (the adapter has no account layer of its own) says so rather than "present"."""
    label = f"{result['provider']} account layer"
    if not result["present"]:
        reason = f" — {result['error']}" if result["error"] else ""
        return f"  ✗ {result['config_dir']}: {label} absent{reason}"
    if result.get("skipped"):
        return f"  ✓ {result['config_dir']}: {result['provider']} has no account layer of its own"
    return f"  ✓ {result['config_dir']}: {label} present"


def _describe(result: dict) -> str:
    """One line per account: the OMC verdict plus whichever best-effort steps did not apply.

    Branches on `omc` BEFORE `error`: a --dry-run miss has no error but must still read as absent."""
    if not result["omc"]:
        reason = f" — {result['error']}" if result["error"] else " (--dry-run: nothing installed)"
        return f"  ✗ {result['config_dir']}: OMC absent{reason}"
    notes = ["OMC installed" if result["installed"] else "OMC present"]
    for label, res in (("hud", result["hud"]), ("notify", result["notify"]), ("mcp", result["mcp"])):
        if isinstance(res, dict) and not res.get("applied"):
            notes.append(f"{label} not applied ({res.get('error') or 'unknown'})")
    return f"  ✓ {result['config_dir']}: " + ", ".join(notes)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--dry-run", action="store_true",
                        help="report OMC presence per account and install nothing")
    parser.add_argument("--offline", action="store_true",
                        help="skip every step that touches the network (the online plugin pass), so a "
                             "caller under the package-manager lock or on a dispatch path can wait for the rest")
    parser.add_argument("--config-dir", action="append", default=[], metavar="DIR",
                        help="provision only this account config_dir (repeatable); overrides the roster")
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)

    try:
        import accounts
        import agents  # the adapter registry: which lane a home takes is its adapter's answer
    except Exception as exc:
        # Reported, not raised: node_reprovision must still render and reload its unit.
        print(f"  ⚠ [provision-accounts] cannot import accounts ({exc}) — skipping account layer",
              file=sys.stderr)
        return 1

    present, absent, providers = resolve_config_dirs(accounts, args.config_dir)
    for missing in absent:
        print(f"  ⊘ [provision-accounts] {missing}: in the roster but not on this node — skipped")
    if not present:
        # Zero is both a legitimate steady state and the cold-boot "CP unreachable" case; say so.
        print("==> [provision-accounts] no accounts on this node (empty roster, or the control plane was "
              "unreachable) — nothing to provision; a re-run of node_reprovision.py retries")
        return 0

    claude_dirs, provider_dirs, unknown = split_by_account_layer(present, providers, agents.get_adapter)
    for provider, config_dir in unknown:
        print(f"  ⚠ [provision-accounts] {config_dir}: provider {provider!r} has no adapter on this node "
              f"— left alone (neither lane may write into a home it cannot identify)", file=sys.stderr)

    verb = "checking" if args.dry_run else "converging"
    print(f"==> [provision-accounts] {verb} {len(present)} account(s) ({len(claude_dirs)} on the Claude "
          f"layer, {len(provider_dirs)} on a provider's own): settings → consent+block → plugins → "
          f"OMC/HUD/notify/MCP → provider layer")

    # Layers 1-3 run once over all dirs, not per account: `plugin_repair` resolves the consent hash over
    # every dir given. A --dry-run writes nothing, so skips them. Layer 1 always runs (reconcile walks the
    # auth root itself and is TOLD the provider homes); layers 2-3 only with a Claude dir to name — given
    # none, `plugin_repair` would discover every dir on disk, provider homes included.
    # An EXPLICIT run (`--config-dir`: dispatch, a login finalize) restricts reconcile to the dirs named
    # (`only`): it has split only those, so any other provider home on disk would take the Claude lane
    # (settings.json + agents/*.md) in that pass. A roster run passes no `only` — it has split every
    # dir it knows, and must still reach the ones the roster knows.
    layer_results = []
    if not args.dry_run:
        layer_results.append(converge_settings_layer(
            provider_dirs=provider_dirs, only=present if args.config_dir else ()))
        if claude_dirs:
            layer_results += converge_plugin_layer(claude_dirs, offline=args.offline)
    provider_results = converge_provider_layer(provider_dirs, offline=args.offline, dry_run=args.dry_run)
    layer_results += [r for r in provider_results if "layer" in r]
    provider_results = [r for r in provider_results if "layer" not in r]
    for res in layer_results:
        mark = {"ok": "✓", "skipped": "⊘", "failed": "✗", "degraded": "⚠"}.get(res["status"], "?")
        print(f"  {mark} [{res['layer']}] {res['status']}"
              + (f" — {res['detail'].splitlines()[-1]}" if res.get("detail") else ""))
        # An unresolved consent silently wedges the next launch, so it goes to stderr.
        for account in res.get("unknown_consent") or []:
            print(f"  ⚠ [provision-accounts] {account}: no managed-settings consent could be resolved "
                  f"— it will meet the approval prompt on launch until one account on this node "
                  f"consents, or JAAH_REMOTE_SETTINGS_CONSENT_HASH is set", file=sys.stderr)

    # KNOWN HOLE: the Claude lane below still runs as whatever uid this is, root included (pre-existing;
    # the provider lane refuses root in `converge_provider_layer`).
    results = [provision_account(accounts, d, install=not args.dry_run) for d in claude_dirs]
    for result in results:
        print(_describe(result))
    for result in provider_results:
        print(_describe_provider(result))
    failed_layers = [r["layer"] for r in layer_results if r["status"] == "failed"]
    if failed_layers:
        sys.stdout.flush()
        print(f"  ✗ [provision-accounts] layer(s) failed: {', '.join(failed_layers)} — the account layer "
              f"below still ran; re-run this script once the cause is fixed", file=sys.stderr)

    without_omc = [r["config_dir"] for r in results if not r["omc"]]
    without_layer = [r["config_dir"] for r in provider_results if not r["present"]]
    for label, missing in (("OMC", without_omc), ("the provider's account layer", without_layer)):
        if missing:
            # stdout is block-buffered under a pipe; flush so stderr does not overtake it.
            sys.stdout.flush()
            # rc=1 marks the layer incomplete; callers treat it as best-effort (degraded, not broken).
            print(f"  ✗ [provision-accounts] {label} absent on {len(missing)} account(s): "
                  f"{', '.join(missing)}", file=sys.stderr)
    if without_omc or without_layer or failed_layers:
        return 1
    print(f"==> [provision-accounts] done — every layer converged on {len(present)} account(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
