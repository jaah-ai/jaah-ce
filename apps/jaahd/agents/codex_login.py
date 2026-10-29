"""codex_login.py — the `LoginAdapter` for OpenAI Codex.

Answers `agents/login.py`'s questions for `codex`; every value is MEASURED against codex-cli 0.151.0
(2026-08-31/09-01) unless a line says otherwise. `--device-auth`, not plain `codex login`: the plain verb
opens a loopback listener no headless node can be reached on; device auth opens none, prints a URL and a
one-time code, and polls the vendor. Its URL is non-loopback yet it completes on disk, so `completion()`
is DECLARED from the catalogue, never inferred from the URL.

Unmeasured: a successful login against the real issuer (needs a Codex seat); the success banner was
measured against a fake issuer. Imports `provider_health` for `CODEX_HOME_ENV` only, so the login and the
health probe cannot spell the config-home variable differently. `agents.agent_layer` is imported inside
`finalize_steps` (under `base.LoginAdapterBase.finalize`'s `try`), so a broken agent layer cannot make the login
registry uncollectable.
"""
from __future__ import annotations

import re

import agent_providers
import host_platform
import provider_health

from .base import LoginAdapterBase, credential_file_present

# ── The closed set of refusal codes (`LoginAdapter.refusal`'s return vocabulary) ─────────────────────
# A CLOSED SET, never interpolated from the log tail (stored by the CP; a tail is where a token sits).
# `accounts.py` discards any reason not matching `[a-z0-9][a-z0-9_]{0,63}\Z`.
REFUSAL_DEVICE_AUTH_DISABLED = "codex_device_auth_disabled"
REFUSAL_CODES: frozenset[str] = frozenset({REFUSAL_DEVICE_AUTH_DISABLED})

# `credential_present`'s size bound: `auth.json` is a few KB; a document past this is no credential.
MAX_CREDENTIAL_BYTES = 1024 * 1024

# Device-code login switched off on a Team/Enterprise workspace (HTTP 404). Measured stderr, exit 1:
#     Error logging in with device code: device code login is not enabled for this Codex server.
# Matched narrowly on the clause, tolerating only "device code"/"device-code": a FALSE refusal sends an
# operator to change a workspace setting that is fine; a missed one degrades to the scrape diagnosis.
_DEVICE_AUTH_DISABLED_RE = re.compile(r"device[\s-]code login is not enabled", re.IGNORECASE)

# The phishing anchor on the link an operator is told to trust — a literal, not a wildcard: the CLI
# CONSTRUCTS `<issuer>/codex/device` itself (measured: it ignored a fake issuer's `verification_uri`).
#   * Path pinned: a host-only anchor scraped the CLI's own transport-error text
#     (`…/api/accounts/deviceauth/usercode`) as the authorize link.
#   * No trailing tail: the log is appendable by every runner on a delegated node, and a planted
#     `…/codex/device?user_code=<attacker's code>` is a one-click authorization of someone else's flow.
#   * Right lookahead refuses `auth.openai.com.evil.example`; `(?<!\S)` on the left (not a class of
#     delimiters, which missed `?to=`) stops an embedded match returning early and opening the
#     code-scrape attack `accounts._scrape_user_code` documents.
# A pattern that stops matching fails loudly as the scrape-failure diagnosis — the right direction.
_AUTH_URL_RE = re.compile(r"(?<!\S)https://auth\.openai\.com/codex/device(?=[/?#\s]|$)")

# The one-time code. The banner ("Enter this one-time code …") is IN the pattern: the code's shape alone
# would match request ids or SHA fragments. Measured: code on the next line, 3-space indent, 4-5 chars;
# bounds widened to 4-8 (server-side choice). Optional newline for same-line renderings; horizontal
# whitespace only, so a blank line cannot bind the banner to a distant token.
# SPANS BOUNDED against a measured DoS: unbounded, the pattern is super-linear in a whitespace run an
# attacker can plant (42s at 100k); 80 (a terminal width over the 24-char parenthetical) and 16 (four
# indents) make it ≤81x17 steps per banner on the capped tail (`accounts._LOG_SCRAPE_TAIL_BYTES`).
_USER_CODE_RE = re.compile(
    r"one-time code[^\n]{0,80}?\n?[^\S\n]{0,16}([A-Z0-9]{4,8}-[A-Z0-9]{4,8})(?=\s|$)")


class CodexLoginAdapter(LoginAdapterBase):
    """A `LoginAdapter` for `codex` (structurally — no import of the Protocol, which is the point of it).

    Stateless. The nine Protocol members come first in the Protocol's declared order (pinned by
    `tests/test_account_login_provider_mode.py::_EXPECTED_MEMBERS`); `completion` (`await_credentials`: the URL
    says paste, the flow says disk — module header) and `credential_path` (`{CODEX_HOME}/auth.json`, the ONE file
    a completed login writes 0600 without re-moding the home, so the machine stats it — REQUIRED, as
    `accounts._login_creds_fresh` calls it unconditionally) are the row's (`base.LoginAdapterBase`).
    `install_hint` and `user_code_re` are the only optional attributes, read by `getattr`.
    """

    # From the catalogue, never a literal.
    row = agent_providers.CODEX

    # Appended to `login_cli_missing`. Names the convergence, not `npm install -g`: a hand-installed copy
    # in another prefix is invisible on the daemon's PATH.
    install_hint = ("Install it with the node's runner-CLI convergence (the `@openai/codex` npm arm in "
                    "the jaah package's postinst), so it lands on the session user's own PATH.")

    def login_argv(self) -> tuple[str, ...]:
        """`codex login --device-auth` — the argv that starts an interactive login on a headless node.

        A tuple, no shell (spawned as the session user). `--device-auth` is load-bearing: without it the
        login waits on a loopback listener nothing can reach and can only time out. On macOS the root
        `-c` pinning the credential store to `auth.json` precedes the verb (`codex.credential_store_args`)."""
        from .codex import credential_store_args  # local, as `finalize`: the registry must stay collectable

        return (agent_providers.CODEX.binary, *credential_store_args(), "login", "--device-auth")

    def login_env(self, *, config_dir: str) -> dict:
        """`{"CODEX_HOME": config_dir}` — a WHOLE-HOME relocation, and the isolation contract for Codex.

        The name is `provider_health.CODEX_HOME_ENV`'s, so login and health probe read one directory. An
        OVERLAY merged over PATH/HOME (codex is an npm shim that re-execs `node`). This is the whole of the
        per-node isolation behind `CODEX.max_accounts_per_node == 0`: `{}` would silently write a second
        account over the first. Secret-bearing: never logged, echoed or put in telemetry, keys included."""
        return {provider_health.CODEX_HOME_ENV: config_dir}

    def auth_url_re(self) -> re.Pattern:
        """Codex's authorize-URL pattern — a phishing guard, including against a prefilled verification
        URI (see `_AUTH_URL_RE`). Returned, never copied."""
        return _AUTH_URL_RE

    def refusal(self, log_tail: str) -> str | None:
        """`codex_device_auth_disabled` when this workspace has device-code login switched OFF, else None.

        Without it the refusal looks like a scrape failure ("the CLI's output changed") when the remedy is
        an admin-console switch. Returns a closed-set code, nothing from `log_tail`. Case-insensitive: the
        caller does not lowercase this tail. None means "nothing conclusive", never "the login is fine"."""
        return REFUSAL_DEVICE_AUTH_DISABLED if _DEVICE_AUTH_DISABLED_RE.search(log_tail) else None

    def credential_present(self, *, config_dir: str) -> bool:
        """True iff this `CODEX_HOME` now holds a credential file — a STAT of `credential_path`, and
        deliberately NOT the vendor's `codex login status` verb.

        The status verb cannot be the oracle (measured): it reads the local file and asks the server
        nothing, so it says "logged in" for a fake key; and run by the daemon against the session user's
        0600 file it exits 1 — a definite "no" that would time out every real login.

        A stat needs only search permission on the daemon-owned `config_dir`. Contents are not checked;
        freshness is `_login_creds_fresh`'s — the machine requires both halves. NON-EMPTY, because a 100ms
        poll can land between create and write; and non-empty is enough — measured, the CLI writes the whole
        document in one `write(2)` (if it ever streams, use a stable-size-across-two-polls test).

        `credential_file_present` with no `validate`, so an `lstat`, not `stat`: `config_dir` is 0o2770, so
        any runner at the session uid can plant an `auth.json` symlink and forge completion (and freshness).
        This closes the cheap forgery, not the same-uid trust boundary. Every not-yet state answers False."""
        return credential_file_present(self.credential_path(config_dir=config_dir),
                                       max_bytes=MAX_CREDENTIAL_BYTES)

    def finalize_steps(self, *, config_dir: str) -> None:
        """The shared PROVIDER agent layer (`super()`, the single home of the provider tail, handed the SAME env
        overlay the login ran under), then the CODEX-ONLY half: START this account's OMX converge — no `claude`
        verb. The OMX layer (`omx_layer.py`, driven by `provision_accounts.py`) is spawned DETACHED —
        `provision_accounts.detached_command` is a `sh -c "nohup … &"` argv that runs the full converge for THIS
        account and returns at once — through `agent_layer.run_provider_cli`, so it lands as the account's uid
        without spending the finalize sequence's pinned budget
        (`test_the_finalize_sequence_fits_the_budget_its_caller_has`).

        Raises freely (`base.LoginAdapterBase.finalize` contains it, the imports inside its `try`): a broken agent
        layer or converge script must not make this file, the registry, and `accounts.py` uncollectable
        (`provision_accounts` is a top-level module; the agents package imports siblings lazily, as here). Silent:
        the layers already log, and a failed spawn is a `run_provider_cli` result, never a raise."""
        super().finalize_steps(config_dir=config_dir)
        # Never on Windows: no OMX layer ships there (`agents/codex.py` `_OMX_WINDOWS_SKIP`).
        if host_platform.is_windows():
            return
        # The OMX converge, detached: `run_provider_cli` returns when the `sh -c "nohup … &"` does.
        import provision_accounts

        from . import agent_layer

        agent_layer.run_provider_cli(
            provision_accounts.detached_command(config_dir), env=self.login_env(config_dir=config_dir))

    # ── The optional attribute (`agents/login.py` documents the full optional surface) ───────────────

    def user_code_re(self) -> re.Pattern:
        """The pattern that finds the DEVICE CODE in this login's stdout — group 1 is the code itself.

        The link alone is useless (a constant page asking for the code), so the code is scraped beside the
        URL and travels the same hop. It is the RFC 8628 USER code, meant to be shown — the `device_code`
        never leaves the CLI — but short-lived and never logged. See `_USER_CODE_RE` for the banner."""
        return _USER_CODE_RE
