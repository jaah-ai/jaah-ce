"""login.py — the login seam every agent-CLI vendor is driven through.

A Protocol rather than `if provider == ...` arms in `accounts.py`, so each vendor's brand anchor, env var
and refusal wording stay apart and no guard silently widens. It declares only: no adapter is imported
here and the registry below is filled by `agents/__init__.py`.

Structural, not an ABC: nothing is shared to inherit. Not `@runtime_checkable`: `isinstance` checks method
names only, which reads like a guard and is not one; the real guards are
`tests/test_account_login_provider_mode.py` (member set and order) and each adapter's tests.

Import ceiling: stdlib + `agent_providers`, asserted from this file's AST, relative imports
included — which is why the registry is filled from `agents/__init__.py`."""
from __future__ import annotations

import re
from typing import Protocol


class LoginAdapter(Protocol):
    """One agent CLI's login, as the questions `accounts.py` asks to run one.

    The method set and its order are a contract pinned by a test. Add a method only by adding it to every
    adapter in the same commit — a missing one is an `AttributeError` mid-login."""

    # The catalogue `value` this adapter is for (`agent_providers.AgentProvider.value`); the registry key.
    provider: str

    def login_argv(self) -> tuple[str, ...]:
        """The argv that starts an interactive login, binary included, for `subprocess.Popen`.

        A tuple, spawned with no shell: a shell in this path is an injection surface. May carry a flag
        stopping the CLI from opening a browser this node has no display for."""

    def login_env(self, *, config_dir: str) -> dict:
        """The env overlay pinning this login's credentials to `config_dir` — the isolation contract.

        An overlay the caller merges, not a whole environment (the login still needs PATH and the user's
        home). Returning `{}` does not fail; it writes the second account's credentials over the first's.

        Paths and public literals only — never a secret. On a uid-split node
        `accounts.py::_spawn_login_as_session_user` renders each pair as `export KEY='VALUE'` text into a
        `0750` request script in the `2750 jaahd:jaah` spool, readable by every runner. A vendor that
        needs a secret needs a new seam (a `0600` fd or file the child reads), not a wider return here.

        Never logged, echoed, or put in a telemetry payload — not even the keys: the paths name a specific
        account's credential store. Log variable names only if at all."""

    def auth_url_re(self) -> re.Pattern:
        """This vendor's authorize-URL pattern — the phishing anchor on the link an operator is told to trust.

        Brand-anchored per vendor; never a widening of `accounts.py::_OAUTH_URL_RE`. The brand domain must
        be the registrable host followed by a delimiter or end-of-token, and there is exactly one pattern
        per vendor."""

    def completion(self) -> str:
        """How this login finishes: `agent_providers.PASTE_CODE` or `agent_providers.AWAIT_CREDENTIALS`.

        Declared, never inferred from the URL: a device-auth login is non-loopback yet completes on disk,
        and inference would hang it until `CODE_TIMEOUT_SECONDS`. Return the catalogue row's
        `login_completion` (Claude Code's: `paste_code`)."""

    def refusal(self, log_tail: str) -> str | None:
        """A refusal this vendor's login prints instead of failing — a stable reason code, or None.

        Distinguishes "can never succeed here" (e.g. a workspace-gated device login) from a timeout. Match
        narrowly on the vendor's own wording.

        `log_tail` is raw CLI output, where tokens land: return only a code from a closed set of module
        constants, never anything interpolated from `log_tail` — this value is reported to the control
        plane and stored. None means "nothing conclusive", never "the login is fine"."""

    def credential_path(self, *, config_dir: str) -> str:
        """The one file under `config_dir` whose (re)appearance means this vendor's login succeeded.

        The machine needs the path for two things `credential_present` cannot do: a freshness baseline (a
        re-auth's dir already holds the old credential, so success requires this file's mtime to move) and a
        delegated shape check (on a uid-split node the file is 0600 the session user's).

        Must name the real file the CLI writes. Pure path arithmetic: called before the dir exists, so it
        must not stat, create or raise. No value is read."""

    def credential_present(self, *, config_dir: str) -> bool:
        """True iff a usable credential now exists under `config_dir` — the await-mode completion signal.

        Shape only, no value returned or logged (as `accounts.py::_creds_have_token`); "file exists" is not
        "has a token" (a cancelled login leaves a file). Polled: cheap, and must not raise on a missing
        file."""

    def finalize(self, *, config_dir: str) -> None:
        """Everything this vendor needs done after its credential lands (the agent layer: MCP, config
        seeding).

        Separate so it is re-runnable without a browser round trip: idempotent, must not raise on a partial
        dir, returns nothing (a failure degrades the agent layer, never strands a good credential).

        Mode floor: anything carrying or revealing a credential is 0600 (file) / 0700 (dir), set on the
        file descriptor (`os.open(..., O_CREAT | O_EXCL, 0o600)` or `os.fchmod`), never on the path — an
        open-then- chmod window leaks on the group-shared `2775 jaah:jaah` auth dir, and a path chmod
        follows symlinks. Widening beyond the floor is a degradation to publish in `agent_providers.py`,
        not a fixup."""


# ───────────────────────────────────────────────────────────────────────────────────────────────────
# THE OPTIONAL ATTRIBUTES THE MACHINE ASKS FOR BY `getattr`, AND NONE IS A PROTOCOL MEMBER
# ───────────────────────────────────────────────────────────────────────────────────────────────────
#
# Single home of this surface (a test checks it against `accounts.py`'s getattr reads). Absent is legal.
#
#   `cli_binary: str` — the vendor CLI's executable name when `login_argv()[0]` is a driver around it
#       (a pty driver run by `python3`); what `accounts.py::_login_cli_refusal` looks for on the login's
#       PATH and `_scrape_failure_error` names. Absent: `login_argv()[0]`.
#   `install_hint: str` — one sentence on installing the CLI, appended by
#       `accounts.py::_login_cli_refusal` when the binary is missing.
#   `url_banner_seen(text: str) -> bool` — a first-match gate on stdout, for a log that can show a
#       brand-host URL that is not the authorize URL before the real one. Absent: `auth_url_re` alone.
#   `user_code_re() -> re.Pattern` — group 1 is an RFC 8628 **user** code shown beside the link (never the
#       `device_code`, the polling secret); the introducing line belongs inside the pattern, since a bare
#       code's shape is too weak alone. Never logged. Absent: link only.
#   `login_rejected_in_log(text: str) -> bool` — the rejected-code wording in the (escape-stripped,
#       lowercased) stdout log. The code FIFO never EOFs, so a rejecting CLI re-prompts instead of
#       exiting. Match narrowly. Absent: only a process exit reports a rejection.
#   `machine_finished: bool` — True when the login machine's OWN steps finish this login: its freshness reads
#       the account token store (`accounts.py::_login_creds_fresh`) and its agent layer is
#       `accounts.py::_finalize_account_add`, so `credential_path`/`credential_present`/`finalize` are never
#       called for it (`accounts.py::_declared_login_adapter`). Claude Code's row. Absent: the adapter's own
#       members finish the login.
#
# ADD THE NEXT ONE ONLY BY ADDING IT HERE TOO. The count is deliberately not spelled.


# THE REGISTRY — the one answer to "can this node drive a login for this provider?"
# ───────────────────────────────────────────────────────────────────────────────────────────────────
#
# A provider with no row gets a terminal refusal from `accounts.py::_login_provider_refusal`; without it
# the request would run Claude's login and mint a Claude token under another vendor's permanent
# `accounts.provider`. A registry, not a `!= "claude"` test: a new adapter lands by adding one row. Claude
# Code has a row like every vendor (`agents/claude_login.py`), finished by the machine (`machine_finished`).
#
# Distinct from the runner registry in `agents/__init__.py` (`get_adapter`, which raises): this one
# answers None, because "no login adapter" is a refusal shown to an operator. Declared here, populated
# in `agents/__init__.py`, because this seam may not import a sibling adapter.
LOGIN_ADAPTERS: dict[str, LoginAdapter] = {}


def login_adapter_for(value: str) -> LoginAdapter | None:
    """The login adapter for a wire/DB `provider` value, or None when this node has none for it.

    None, never a fallback: another vendor's adapter would run the wrong CLI against this config dir."""
    return LOGIN_ADAPTERS.get(value)
