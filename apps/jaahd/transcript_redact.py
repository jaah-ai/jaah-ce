"""Best-effort secret redaction over a session transcript, BEFORE it is gzipped and uploaded (D8).

Runs on a COPY: pure text→text; nothing here opens an on-disk transcript for write (it is the only
record of what a session did). A fixed list of shapes with no failure state and no assurance — a shape
not on the list reaches the bucket (D8), so `report()` always stamps `confidence: "best_effort"` and
`hits` says nothing about what was missed.

Under-redaction ships a live credential to a one-way bucket kept forever; over-redaction silently
corrupts the artifact. So every shape is either self-identifying (`AKIA…`, PEM, `ghp_…`) or bound to a
NAMED key (`aws_secret_access_key=`, `Authorization:`, `*_TOKEN=`) — never a bare high-entropy run,
which also spells digests, JWT fragments and git ids.
Stdlib only, nothing that can raise at import; must parse on the oldest interpreter the package
admits.
"""
from __future__ import annotations

import re
from typing import Dict, Iterable, Mapping, Tuple

# Records WHICH list ran. Bumped when a pattern or its replacement changes — never for a comment.
PATTERNS_VERSION = 11

# `<`/`>` appear in no pattern's value, so a second pass matches nothing (idempotent); JSON-safe — it
# introduces no quote or backslash, so a `.jsonl` line stays parseable.
REDACTED = "<redacted>"

# The only value: this pass has no failure state and no completeness it could claim.
CONFIDENCE = "best_effort"

# A value's character class for every KEY-BOUND pattern, spelled once so `_VALUE` and
# `_LOOKS_LIKE_A_CREDENTIAL` agree where a value ENDS. Stops at quotes, backslash and JSON/shell
# separators so a `.jsonl` record's closing `\"` survives. `<`/`>` excluded so `REDACTED` never re-matches
# (else `hits` climbs every pass). Parens/brackets excluded: `Credential(` or `Optional[list[int]]` is not
# a value.
_VALUE_EXCLUDED = r"""\s"'\\,;&|()[\]{}<>"""
_VALUE = r"[^" + _VALUE_EXCLUDED + r"]"

# Named-key/value separator: bare (`Authorization: Bearer x`), `=` (`GITHUB_TOKEN=x`), or a JSON member,
# possibly escaped (`\"Authorization\": \"Bearer x\"`). `\\{0,4}` because nested JSON in a JSONL string is
# double-escaped; bounded so it cannot backtrack. The LEADING optional quote is load-bearing: without it
# the JSON-member form (what `aws sts assume-role --output json` emits) matches nothing and the
# `SecretAccessKey` beside a redacted `AccessKeyId` ships in plaintext.
_ASSIGN = r"""(?:\s*(?:\\{0,4})?["']?\s*[:=]\s*(?:\\{0,4})?["']?\s*)"""

# A word boundary that also holds inside a JSON string (where `\b` fails for `export `/`&&`/escaped
# newlines). Captured and re-emitted verbatim.
_LEAD = r"(^|[^A-Za-z0-9_])"

# The key names that make an otherwise-unremarkable value a secret. Separators are `[_-]?` so
# screaming-snake, camelCase (`SecretAccessKey`) and CLI-flag (`--api-key=`) spellings all match.
#
# `CLIENT_?ID` because `apps/api/app/credentials/registry.py` declares Xero's `client_id` a secret;
# `test_secretish_keys_cover_every_registry_secret_field` keeps the two in step. It does NOT eat
# `access_key_id`/`access-key-id` (public without its secret half): `_ASSIGN` demands its separator
# IMMEDIATELY after the key, and `_` or `-` is none. Pinned by
# `test_a_hyphenated_flag_that_is_not_a_secret_still_survives`.
# `MCP_SERVER_URL`/`WEBHOOK_URL` for the same parity reason (Zapier's): each URL embeds the capability itself.
_SECRETISH_KEY = (
    r"(?:SECRET[_-]?ACCESS[_-]?KEY|SECRET[_-]?KEY|SESSION[_-]?TOKEN|REFRESH[_-]?TOKEN|BEARER[_-]?TOKEN|SECRET|TOKEN"
    r"|PASS(?:WD|WORD)|API[_-]?KEY|PRIVATE[_-]?KEY|CREDENTIALS?|ACCESS[_-]?KEY|AUTH[_-]?KEY|SERVICE[_-]?KEY"
    r"|CLIENT[_-]?ID|MCP[_-]?SERVER[_-]?URL|WEBHOOK[_-]?URL|SERVICE[_-]?ACCOUNT[_-]?JSON)"
)

# A key must END at the secretish word (refusing `--max-tokens=8192`, `SSH_PRIVATE_KEY_PATH=/tmp/k`).
# Defence in depth, NOT the mechanism: every separator arm already begins outside `[A-Za-z0-9_-]`, so
# this cannot change an outcome today. Kept so a future separator loosened to admit a word character
# cannot silently turn a key-prefixed path back into a redaction.
_KEY_TAIL = r"(?![A-Za-z0-9_-])"

# BOUNDED, as a DoS fix: `_LEAD` and this class both admit `-`, so in a long `[A-Za-z0-9_-]` run every
# hyphen restarts an unbounded scan — quadratic (32 KB single line took 24 s; real transcripts carry
# ~30 KB runs, on a sweep whose tick dies at 1800 s and retries forever). 32 is far above any real key
# name (`AWS_SECRET_ACCESS_KEY` is 21).
_KEY_PREFIX = r"[A-Za-z0-9_-]{0,32}"

# ── The separator, SPLIT BY FORM, because one permissive rule cannot serve both directions ────────
#   * `=` unspaced (`KEY=value`, env/shell/kwarg) takes a bare or quoted value — `POSTGRES_PASSWORD=x`
#         is a real credential that looks like an identifier. SPACED (`key = value`, source code)
#         requires a QUOTED value, so `bot_token = destination` survives.
#   * `":` JSON member — BOTH quotes required, which separates `"SecretAccessKey":"wJalr…"` from
#         `class AwsCredentials: access_key_id`.
#   * ` :` bare colon (YAML, `kubectl config view`) — only with the key spelled EXACTLY, never prefixed.
_ASSIGN_EQUALS = (
    r"""(?:=\s*(?:\\{0,4})?["']?\s*"""      # `KEY=value` / `KEY="value"` — env, shell, kwarg
    r"""|\s+=\s*(?:\\{0,4})?["']\s*)"""    # `key = "value"` — a quoted literal in source
)
_ASSIGN_JSON = r"""(?:\s*(?:\\{0,4})?["']\s*:\s*(?:\\{0,4})?["']\s*)"""
_ASSIGN_BARE_COLON = r"""(?:\s*:[ \t]+)"""

# The bare-colon arm's extra fence, since a bare colon is also English (`the refresh token: rotated…`):
# a digit anywhere or 16+ chars. Length-or-digit, not case: the whole pattern is `(?i)` and `(?-i:…)`
# is 3.11+.
_LOOKS_LIKE_A_CREDENTIAL = r"(?=" + _VALUE + r"*\d|" + _VALUE + r"{16,})"

# A value that is CODE, not a credential, refused as a lookahead: a call (`os.environ.get(`) or a dotted
# identifier chain (`process.env.E2E_ADMIN_PASSWORD`). A real credential is neither; a JWT, which has
# dots, is caught by its own pattern first.
_CODE_EXPRESSION = (
    r"""(?![A-Za-z_][A-Za-z0-9_]{0,63}(?:\.[A-Za-z_][A-Za-z0-9_]{0,63}){0,8}\s*\()"""
    r"""(?![A-Za-z_][A-Za-z0-9_]{0,63}(?:\.[A-Za-z_][A-Za-z0-9_]{0,63}){1,8}(?:[\s"',;&|)}\]]|$))"""
)
# Also never a credential: `{token}` (an f-string placeholder), a `` `code span` ``, a boolean/nil, a
# path, a `$VAR`, a plain integer. And a value that is itself a credential NAME is a reference
# (`api_key=DEEPSEEK_API_KEY`, `"credential": "node_token"`, `bot_token=bot_token`). The trade, stated:
# a real secret whose literal ends in a secretish word (`MY_TOKEN=someservicetoken`) is MISSED — narrow,
# since generated values don't end in English words, against broad corruption otherwise (D8).
_A_KEY_NAME_NOT_A_VALUE = r"(?!" + _KEY_PREFIX + _SECRETISH_KEY + r"(?![A-Za-z0-9_-]))"

_NOT_A_SECRET_VALUE = (
    # `#` — a CSS colour (`credential: #fb923c`, measured), a comment, a fragment. Never a secret.
    r"""(?![$/~{`#])"""
    + _A_KEY_NAME_NOT_A_VALUE
    + r"""(?!\d+(?:[\s"',;&|)}\]]|$))"""
    # No `(?i:…)` scoped flag (3.11+); both users carry `(?i)` at position 0. `:` is in this boundary
    # but not in `_VALUE` so `require_credential=False:` is recognised up to its colon.
    r"""(?!(?:true|false|none|null|nil|undefined)(?:[\s"',;:&|)}\]]|$))"""
) + _CODE_EXPRESSION

# ── The list ──────────────────────────────────────────────────────────────────────────────────────
# `(name, compiled, replacement)`. A `\1`… replacement re-emits the key, separator and opening quote and
# replaces only the value, keeping `.jsonl` and `.env` lines syntactically intact.
_PATTERNS: Tuple[Tuple[str, "re.Pattern", str], ...] = (
    # A PEM block. `[\s\S]` spans a real newline and an escaped `\n` in a `.jsonl` record.
    (
        "pem_private_key",
        # `[A-Z ]{0,32}` both sides of `PRIVATE KEY` for GPG's `PGP PRIVATE KEY BLOCK`. The body is a
        # bounded base64 class, as a DoS fix: an unbounded lazy scan per unmatched `BEGIN` is O(n^2).
        re.compile(
            r"-----BEGIN[A-Z ]{0,32}PRIVATE KEY[A-Z ]{0,32}-----"
            r"[A-Za-z0-9+/=\s\\n]{0,65536}?"
            r"-----END[A-Z ]{0,32}PRIVATE KEY[A-Z ]{0,32}-----"
        ),
        REDACTED,
    ),
    (
        # Self-identifying, and the most likely credential on a Claude-agent fleet.
        "anthropic_api_key",
        re.compile(r"\bsk-ant-(?:api|oat|ort|sid)[0-9]{2}-[A-Za-z0-9_-]{20,}"),
        REDACTED,
    ),
    (
        # OpenAI's prefixed keys (`sk-proj-`/`sk-svcacct-`/`sk-admin-`), then any bare `sk-` + 32 or more
        # alphanumerics: a legacy OpenAI key, and DeepSeek's. `sk-ant-` is above; its hyphen fails the run.
        "openai_api_key",
        re.compile(r"\bsk-(?:(?:proj|svcacct|admin)-[A-Za-z0-9_-]{20,}|[A-Za-z0-9]{32,})"),
        REDACTED,
    ),
    (
        # Stripe's secret and restricted keys (the deployment's billing credential, `app/billing/`):
        # self-identifying by prefix and mode, so a bare one in `curl -u sk_live_…:` is caught too.
        "stripe_secret_key",
        re.compile(r"\b(?:sk|rk)_(?:live|test)_[A-Za-z0-9]{20,}"),
        REDACTED,
    ),
    (
        # Google's API key (what both Google-model CLIs take): `AIza` and exactly 35 more of its alphabet.
        "google_api_key",
        re.compile(r"\bAIza[0-9A-Za-z_-]{35}"),
        REDACTED,
    ),
    (
        # Self-identifying: the middle segment always begins `eyJ`. A bearer token outside a header.
        "jwt",
        re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}"),
        REDACTED,
    ),
# Self-identifying: a four-letter type prefix and exactly 16 upper/digit characters.
    (
        "aws_access_key_id",
        re.compile(r"\b(?:AKIA|ASIA|ABIA|ACCA|AGPA|AIDA|AIPA|ANPA|ANVA|AROA|APKA)[0-9A-Z]{16}\b"),
        REDACTED,
    ),
# No distinguishing shape (40 base64 chars), so redacted ONLY beside its own key name.
    (
        "aws_secret_access_key",
        re.compile(
            r"(?i)\b(aws_secret_access_key|aws_secret_key|aws_session_token)\b(" + _ASSIGN + r")"
            + _VALUE + r"{16,}"
        ),
        r"\1\2" + REDACTED,
    ),
    # GitHub's own prefixed shapes — `ghp_`/`gho_`/`ghu_`/`ghs_`/`ghr_` plus the fine-grained PAT.
    (
        "github_token",
        re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{36,255}|github_pat_[A-Za-z0-9_]{22,255})\b"),
        REDACTED,
    ),
# Jaah's own node token (`apps/api/app/node_credentials.py::TOKEN_PREFIX`) — the credential jaahd itself
# holds, so the likeliest one in a shipped node journal — a tailnet key, and GitLab's prefixed tokens.
    (
        "jaah_node_token",
        re.compile(r"\bsnt_[A-Za-z0-9_-]{20,}"),
        REDACTED,
    ),
    (
        # Tailscale/headscale auth and API keys — what joins a node to the tailnet.
        "tailscale_key",
        re.compile(r"\btskey-[A-Za-z0-9-]{20,}"),
        REDACTED,
    ),
    (
        "gitlab_token",
        re.compile(r"\bgl(?:pat|oas|dt|rt|ptt|cbt|imt|ft|soat|agent)-[A-Za-z0-9_-]{20,}"),
        REDACTED,
    ),
# A password in a URL's userinfo (`https://oauth2:<tok>@host`, `postgres://u:<pw>@db`): the user and `@`
# stay, only the password goes. Six or more characters, so `a:b@` prose is left alone.
    (
        "url_userinfo_password",
        re.compile(r"(?i)\b([a-z][a-z0-9+.-]{1,15}://[^/\s:@\"'<>]{1,64}:)[^@\s/\"'<>]{6,}(@)"),
        r"\1" + REDACTED + r"\2",
    ),
# Slack tokens, and the incoming-webhook URL, which is itself a credential. No trailing `\b`: the last
# character may be `-`.
    (
        # `xapp-` is what Socket Mode authenticates with; caught bare here, not only as `SLACK_APP_TOKEN=`.
        "slack_token",
        re.compile(r"\b(?:xox[abposre]-[A-Za-z0-9-]{10,}|xapp-[0-9]-[A-Za-z0-9-]{10,})"),
        REDACTED,
    ),
    (
        "slack_webhook_url",
        re.compile(r"https://hooks\.slack\.com/services/[A-Za-z0-9/_+-]{8,}"),
        REDACTED,
    ),
# Zapier's catch-hook and MCP server URLs, each a capability by itself (the registry's `webhook_url` and
# `mcp_server_url`), caught bare as well as key-bound. Scheme optional, host case-blind. An MCP server URL lives
# under `/api/` (a docs link such as `/mcp/servers` is no capability) and may carry a dotted key as a query.
    (
        "zapier_webhook_url",
        re.compile(r"(?:https?://)?\b(?i:hooks\.zapier\.com)/hooks/catch/[A-Za-z0-9/_+-]{6,}"),
        REDACTED,
    ),
    (
        "zapier_mcp_url",
        re.compile(r"(?:https?://)?\b(?i:mcp\.zapier\.com)/api/[A-Za-z0-9/_+=?&%.~:-]{8,}"),
        REDACTED,
    ),
# Atlassian's prefixed API token, plus the key-bound form for older unprefixed tokens.
    (
        "jira_token",
        re.compile(r"\bATATT[A-Za-z0-9_=\-]{20,}"),
        REDACTED,
    ),
    (
        "jira_token_assignment",
        re.compile(
            r"(?i)\b((?:jira|atlassian|confluence)[_-]?(?:api[_-]?)?token)\b(" + _ASSIGN + r")"
            + _VALUE + r"{8,}"
        ),
        r"\1\2" + REDACTED,
    ),
# The scheme is preserved; `\3` is empty when the scheme group did not participate.
    (
        "authorization_header",
        re.compile(
            r"(?i)\b(authorization)(" + _ASSIGN + r")(?:((?:bearer|basic|token|digest)\s+)"
            + _VALUE + r"{8,}|()" + _VALUE + r"{20,})"
        ),
        r"\1\2\3" + REDACTED,
    ),
    (
        # An assignment whose KEY reads as a secret: `.env` line, `export` in a shell command, or JSON
        # member. `_LEAD` makes it fire mid-string; `_KEY_TAIL` anchors the key's end.
        "secret_key_assignment",
        re.compile(
            r"(?i)" + _LEAD + r"(" + _KEY_PREFIX + _SECRETISH_KEY + r")" + _KEY_TAIL
            + r"((?:" + _ASSIGN_EQUALS + r")|(?:" + _ASSIGN_JSON + r"))"
            + _NOT_A_SECRET_VALUE + _VALUE + r"{6,}"
        ),
        r"\1\2\3" + REDACTED,
    ),
    (
        # Bare colon, much narrower: the key spelled EXACTLY, as a YAML key is (`token: abc…`), which
        # neither a type name nor a sentence does.
        "secret_key_bare_colon",
        re.compile(
            r"(?i)" + _LEAD + r"(" + _SECRETISH_KEY + r")" + _KEY_TAIL
            + r"(" + _ASSIGN_BARE_COLON + r")" + _NOT_A_SECRET_VALUE
            + _LOOKS_LIKE_A_CREDENTIAL + _VALUE + r"{6,}"
        ),
        r"\1\2\3" + REDACTED,
    ),
)


def pattern_names() -> Tuple[str, ...]:
    """Every shape this pass covers, in list order. The COVERAGE claim a manifest records."""
    return tuple(name for name, _pattern, _replacement in _PATTERNS)


def redact_text(text: str) -> Tuple[str, Dict[str, int]]:
    """`(redacted, {pattern_name: matches})` — only the patterns that actually fired appear in the map.

    Applied in list order; `pem_private_key` runs FIRST so a key body is consumed whole before a
    key-bound pattern bites a fragment out of it.
    """
    counts: Dict[str, int] = {}
    for name, pattern, replacement in _PATTERNS:
        text, hits = pattern.subn(replacement, text)
        if hits:
            counts[name] = counts.get(name, 0) + hits
    return text, counts


def redact_bytes(data: bytes) -> Tuple[bytes, Dict[str, int]]:
    """`redact_text` over raw bytes, LOSSLESS for anything that is not valid UTF-8.

    `surrogateescape` both ways: raw command stdout need not be UTF-8, and dropping bytes would
    corrupt the artifact.
    """
    text, counts = redact_text(data.decode("utf-8", "surrogateescape"))
    return text.encode("utf-8", "surrogateescape"), counts


def merge(into: Dict[str, int], counts: Mapping[str, int]) -> Dict[str, int]:
    """Fold one member's counts into a session-wide tally. Returns `into`, mutated."""
    for name, hits in counts.items():
        into[name] = into.get(name, 0) + hits
    return into


def report(counts: Mapping[str, int], patterns: Iterable[str] = ()) -> Dict[str, object]:
    """The `redaction` block a node sends with its upload report, and the CP copies into the manifest.

    `patterns` is the COVERAGE SET (everything looked for, not what matched), so a clean transcript does
    not read as an exhaustive one. Neither field is assurance: `confidence` is pinned to `best_effort`.
    """
    names = tuple(patterns) or pattern_names()
    return {
        "patterns_version": PATTERNS_VERSION,
        "patterns": list(names),
        "hits": sum(int(value) for value in counts.values()),
        "by_pattern": {name: int(counts[name]) for name in sorted(counts)},
        "confidence": CONFIDENCE,
    }
