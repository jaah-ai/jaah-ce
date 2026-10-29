"""agents/antigravity.py — the Google Antigravity `LoginAdapter` (`agents/login.py`) for `agy`.

Literals measured against agy 1.2.11 (linux amd64) in scratch HOMEs. The CLI has no login verb
and no non-TTY sign-in that lasts (`-p` gives up after 60s), so the argv is the pty driver beside this file
(`antigravity_login_pty.py`, whose header records why each of its steps exists); `cli_binary` tells the
machine the CLI it drives is `agy`. The store is pinned by `antigravity_keyring.account_env`, shared with
the runner adapter so both hand the CLI the same credential.

Registered in `LOGIN_ADAPTERS` in the same change as the runner adapter (`agents/antigravity_runner.py`,
Architecture A2): an unbound task may pick any healthy account, so no `antigravity` account may exist
before a runner can launch on it.

Imports only stdlib, `agent_providers` and its sibling store module (`agent_layer` in-function, at `finalize`,
through `base.LoginAdapterBase`).
"""
from __future__ import annotations

import re
from pathlib import Path

import agent_providers

from . import antigravity_keyring
from .antigravity_login_pty import STATUS_REJECTED
from .base import LoginAdapterBase, credential_file_present

# The phishing anchor: Google's authorize endpoint, pinned through this CLI's public OAuth client id, the
# same prefix the control plane holds (this provider's `Capabilities.authorize_url_anchor`). Not the `v2/auth`
# path — measured on every agy 1.2.11 sign-in.
_AUTH_URL_RE = re.compile(
    r"https://accounts\.google\.com/o/oauth2/auth\?access_type=offline&client_id="
    r"1071006060591-tmhssin2h21lcre235vtolojh4g403ep\.apps\.googleusercontent\.com&\S+")

# The SHAPE of the one-time code the consent page shows (`antigravity.google/oauth-callback`), for grepping
# logs and transcripts to prove no code landed there — never used to read one. Google's authorization-code
# form: `4/0A` then ~70 URL-safe base64 characters (measured on one real code).
AUTH_CODE_SHAPE_RE = re.compile(r"4/0A[0-9A-Za-z_-]{40,}")

# The driver, run by the session user's interpreter. Beside this file in the package (root-owned, 0644).
_DRIVER = str(Path(__file__).resolve().with_name("antigravity_login_pty.py"))

# The TUI renders nothing on a terminal it cannot classify (measured with `TERM=dumb`).
_TERM = "xterm-256color"

# Read bound for `credential_present`: the token file is small; the bound stops a planted file from
# making the daemon allocate at will (read one byte over).
_MAX_CREDENTIAL_BYTES = 256 * 1024


class AntigravityLoginAdapter(LoginAdapterBase):
    """Google Antigravity's answers to the seam's questions, in the Protocol's declared order. Stateless.
    `completion` (`paste_code`), `credential_path` (the file store's token), `refusal` (None: no sign-in refusal a
    wait cannot fix was measured — an ineligible identity signs in and is refused at its first request, which is
    the runner's to report) and `finalize` (the provider agent layer under the login's overlay, so `agy mcp add`
    writes this account's `$HOME/.gemini/config/mcp_config.json`) are the row's (`base.LoginAdapterBase`)."""

    row = agent_providers.ANTIGRAVITY

    # Optional attributes (`agents/login.py`): the CLI the driver runs, and how to install it.
    cli_binary = agent_providers.ANTIGRAVITY.binary
    install_hint = ("The runner-CLI floor installs the pinned, checksum-verified `agy` into that user's "
                    "~/.local/bin on its own (packaging/postinst).")

    def login_argv(self) -> tuple[str, ...]:
        """`<node interpreter> -I <driver>`: the pty driver runs `agy` itself. A tuple, no shell. `-I` because `HOME` is
        the group-writable account dir, whose user site-packages would otherwise load into the process that
        receives the code."""
        import runner_env  # noqa: PLC0415 — in-function, like every daemon import an adapter makes

        return (runner_env.python_cmd(), "-I", _DRIVER)

    def login_env(self, *, config_dir: str) -> dict:
        """The store overlay (`antigravity_keyring.account_env`) plus what the TUI needs to draw.

        Paths and public literals only; never logged."""
        return {**antigravity_keyring.account_env(config_dir=config_dir), "TERM": _TERM, "BROWSER": "true"}

    def finalize_env(self, *, config_dir: str) -> dict:
        """The store overlay alone: `agy`'s MCP and skills steps draw nothing."""
        return antigravity_keyring.account_env(config_dir=config_dir)

    def auth_url_re(self) -> re.Pattern:
        """`_AUTH_URL_RE`, returned, never copied. First match is right: the URL is printed once per run."""
        return _AUTH_URL_RE

    def credential_present(self, *, config_dir: str) -> bool:
        """True iff that path is a regular, non-empty, single-name file within the read bound.

        Shape only: the token's format is a secret nobody here parses (it was not measured). Read through
        `agents.base.credential_file_present` (the dir is group-writable: a planted FIFO or symlink);
        `single_link` because a hardlink may be another account's file, linked in. Every error is "not yet"."""
        return credential_file_present(self.credential_path(config_dir=config_dir),
                                       max_bytes=_MAX_CREDENTIAL_BYTES,
                                       validate=lambda raw: bool(raw.strip()), single_link=True)

    # ── optional attributes the machine asks for by `getattr` (agents/login.py) ──────────────────────

    def login_rejected_in_log(self, text: str) -> bool:
        """The driver's own fixed rejection line — the CLI's output is withheld from the log once the code
        prompt appears, so its wording never reaches here. `text` is escape-stripped and lowercased."""
        return STATUS_REJECTED.lower() in text
