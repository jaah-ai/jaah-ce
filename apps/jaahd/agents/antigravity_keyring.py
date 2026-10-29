"""agents/antigravity_keyring.py — where `agy` keeps an account's credential, and how a process is pinned to it.

Architecture §5.2's contract, shared by the login adapter and (Task C2) the runner adapter, so both hand
the CLI the SAME store. Measured against agy 1.2.11 (linux amd64) in scratch HOMEs:

  * Everything the CLI writes hangs off `$HOME/.gemini/` (`antigravity-cli/` for state, logs and the
    credential; `config/` for `mcp_config.json`), so `HOME=<config_dir>` relocates the whole store.
  * Its token storage is "composite": it tries the OS keyring over D-Bus FIRST and falls back to a file
    (cli.log: "Failed to load stored token from keyring, falling back to file"). With no
    `DBUS_SESSION_BUS_ADDRESS` it still dials the libdbus default `/run/user/<uid>/bus` — the session
    user's ONE bus, shared by every account on the node — so the variable is set to a transport that
    cannot connect ("dbus: invalid bus address"), which forces the per-account file store. Hence no
    `gnome-keyring`, no `dbus-run-session`, and the CLI's argv is passed through unchanged.
  * `TMPDIR` is pinned too: the CLI's feature-flag cache (`unleash-repo-schema-v1-...json`) is written to
    `os.TempDir()`, i.e. the node's shared `/tmp`, otherwise. Pointed at the CLI's own `cache/`, which it
    creates before that write (measured on a fresh dir), every write lands inside `config_dir` (strace,
    `trace=%file`: nothing under the real home, nothing in a sibling account's dir, no `connect` to
    `/run/user/<uid>/bus`).
  * `AGY_CLI_DISABLE_AUTO_UPDATE` disables the self-updater only for the literal `true` (`1` and `yes`
    still spawn it — measured); an updater rewriting the pinned binary would undo the package postinst's pin.

Stdlib only; pure functions — nothing here reads or writes the store.
"""
from __future__ import annotations

import os

# The transport libdbus refuses, so the CLI's keyring branch fails at once and the file store is used.
NO_DBUS = "disabled:"


def account_env(*, config_dir: str) -> dict[str, str]:
    """The env overlay pinning one `agy` process to `config_dir`'s store — the isolation contract.

    Paths and public literals only (it is rendered into a spool script readable by every runner, see
    `agents/login.py::LoginAdapter.login_env`). The XDG trio is pinned too: the CLI's own files follow
    `$HOME`, but a library resolving `os.UserCacheDir`/`UserConfigDir` would otherwise reach the real home."""
    return {
        "HOME": config_dir,
        "XDG_CONFIG_HOME": os.path.join(config_dir, ".config"),
        "XDG_DATA_HOME": os.path.join(config_dir, ".local", "share"),
        "XDG_CACHE_HOME": os.path.join(config_dir, ".cache"),
        "XDG_STATE_HOME": os.path.join(config_dir, ".local", "state"),
        "TMPDIR": os.path.join(config_dir, ".gemini", "antigravity-cli", "cache"),
        "DBUS_SESSION_BUS_ADDRESS": NO_DBUS,
        "AGY_CLI_DISABLE_AUTO_UPDATE": "true",
    }
