"""claude_login.py — the `LoginAdapter` for Claude Code, the login machine's own vendor.

Answers `agents/login.py`'s questions for `claude`, so the registry is the one answer to "can this node drive
a login for this provider?" for every vendor, Claude included (`accounts.py::_login_provider_refusal` asks
nothing else). `claude auth login --claudeai` under `CLAUDE_CONFIG_DIR`, completed by pasting the code back.

`machine_finished` (an optional attribute, `agents/login.py`): the login machine's own steps finish this
login. Its freshness is the account token store's mtime+shape pair (`accounts.py::_login_creds_fresh`) and
its agent layer is `accounts.py::_finalize_account_add`, both keyed by the account's token store, which the
Protocol's `config_dir`-only members cannot name. So `credential_path`/`credential_present`/`finalize`
below answer the Protocol truthfully for the config dir but the machine never calls them for this row.
"""
from __future__ import annotations

import json
import re

import agent_providers
import host_platform
from env_helpers import jaah_home
from host_platform import windows  # importable everywhere: no native call at import

from .base import LoginAdapterBase, credential_file_present

# The read bound on `.credentials.json` (a few hundred bytes when real), as the sibling login adapters'.
MAX_CREDENTIAL_BYTES = 256 * 1024


def has_oauth_token(raw: bytes) -> bool:
    """Claude's credential shape: a JSON object whose `claudeAiOauth` carries a non-empty `accessToken`.
    Raises on a document that does not parse (`credential_file_present` reads that as False). The ONE
    copy: `agent_config_dirs.creds_state` passes it too."""
    data = json.loads(raw)
    oauth = data.get("claudeAiOauth") if isinstance(data, dict) else None
    return isinstance(oauth, dict) and bool(oauth.get("accessToken"))

# THE PHISHING GUARD ON THE URL THE OPERATOR IS TOLD TO TRUST. The brand domain must be the registrable
# host — a path/query/fragment delimiter or end-of-token right after it — so a look-alike like
# `claude.com.evil.example/authorize` can't match. ONE pattern, returned by `auth_url_re` — never a copy.
OAUTH_URL_RE = re.compile(
    r"https://(?:[a-z0-9-]+\.)*(?:claude\.(?:ai|com)|anthropic\.com)(?=[/?#\s]|$)\S*")


class ClaudeLoginAdapter(LoginAdapterBase):
    """Claude Code's answers to the questions the login machine asks of every login; `completion`, `refusal`
    (None: Claude's login prints no declared "this can never succeed here" refusal — `_scrape_failure_error`
    covers its one cause, a CLI that never ran), `credential_path` (`.credentials.json`) and the no-op
    `finalize` are the row's (`base.LoginAdapterBase`)."""

    row = agent_providers.CLAUDE

    # The machine's own steps finish this login (module docstring).
    machine_finished = True

    # Optional attribute appended by the CLI-missing refusal (`getattr` in `_login_cli_refusal`).
    install_hint = ("Install it as that user (`curl -fsSL https://claude.ai/install.sh | bash`, which "
                    "lands in ~/.local/bin); a package converge does this on its own (packaging/postinst).")

    def login_argv(self) -> tuple[str, ...]:
        """`claude auth login --claudeai`, spawned with no shell. The `timeout(1)` wrapper is the machine's."""
        return ("claude", "auth", "login", "--claudeai")

    def login_env(self, *, config_dir: str) -> dict:
        """The overlay that pins this login's credentials to CONFIG_DIR — Claude's isolation contract.

        One variable (so `node_pinned=False`). The shared-store pin is the account's, added by the machine
        (`_securestorage_env`). An overlay, never a whole environment."""
        return {"CLAUDE_CONFIG_DIR": config_dir}

    def auth_url_re(self) -> re.Pattern:
        """The module's one phishing anchor (`OAUTH_URL_RE`), returned, never copied."""
        return OAUTH_URL_RE

    def credential_present(self, *, config_dir: str) -> bool:
        """True iff `credential_path` is a regular file with a `claudeAiOauth.accessToken`-shaped field.

        Shape only (`has_oauth_token`), no value returned or logged; read by `credential_file_present`, so
        a planted link or FIFO is False. Uncalled by the machine for this row (`machine_finished`), whose
        freshness reads the token store.

        Windows has no `O_NOFOLLOW`: refused before the open when a link leads off the disk or out of
        `<jaah_home>` (`windows.local_path_under`) — an open through one to `\\\\host\\share` authenticates
        the daemon to that host."""
        path = self.credential_path(config_dir=config_dir)
        if host_platform.is_windows() and not windows.local_path_under(path, jaah_home()):
            return False
        return credential_file_present(path, max_bytes=MAX_CREDENTIAL_BYTES, validate=has_oauth_token)

    # ── Optional attributes the machine reads by `getattr` (`agents/login.py`) ─────────────────────────

    def url_banner_seen(self, text: str) -> bool:
        """Claude's FIRST-MATCH DISCIPLINE on the stdout log, and it is Claude's alone.

        The first brand-host URL is the authorize URL only after this banner (else a docs link could win).
        Measured phrasing."""
        return "didn't open" in text or "did not open" in text or "visit:" in text.lower()

    def login_rejected_in_log(self, text: str) -> bool:
        """Claude's rejected-code wording. `text` is stripped and lowercased; the needle is narrow so a
        banner line cannot report a false rejection."""
        return "login failed" in text
