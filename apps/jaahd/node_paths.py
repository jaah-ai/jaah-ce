"""node_paths.py — the `jaah` package's on-node directories and unit names.

/etc/jaah, /run/jaah and /usr/lib/jaahd are the node's config, runtime and package roots (the daemon's
environment file is `jaahd.env`). Executed code spells these physical paths, never a link to one: an atomic
write (temp file + rename) onto a file-level link replaces the link with a file, and a systemd namespace
directive (`InaccessiblePaths=`, `TemporaryFileSystem=`, `BindPaths=`) must not depend on one.
"""

from __future__ import annotations

from host_platform import linux as _linux

# ── directories ─────────────────────────────────────────────────────────────────────────────────────────
# They ARE the Linux layout `host_platform` single-homes.
ETC = _linux.paths().etc_dir
RUN = _linux.paths().run_dir
LIB = _linux.paths().lib_root
DAEMON_ENV = f"{ETC}/jaahd.env"

# ── units: kind -> name; `{}` is the template instance ──────────────────────────────────────────────────
_UNITS: dict[str, str] = {
    "daemon": "jaahd.service",
    "update": "jaahd-update.service",
    "runner": "jaah-runner@{}.service",
    "shared_accounts": "jaah-shared-accounts.service",
    "gha": "jaah-gha.service",
    "vnodehelper": "jaah-vnodehelper.service",
    "vnode_rebuild": "jaah-vnode-rebuild.service",
}


def unit_name(kind: str, instance: str | None = None) -> str:
    """The unit name of `kind`, with `instance` filled into a template.

    Raises KeyError for an unknown kind and ValueError when a template gets no instance (or a plain unit
    gets one) — a unit name built wrong addresses nothing, silently."""
    name = _UNITS[kind]
    if ("{}" in name) != (instance is not None):
        raise ValueError(f"unit kind {kind!r} {'needs' if '{}' in name else 'takes no'} instance")
    return name if instance is None else name.format(instance)
