#!/usr/bin/env python3
"""check_commit_msg_closing_keyword.py — commit-msg guard: refuse a message where a GitHub
closing keyword sits immediately before an issue/PR reference, unless opted in explicitly.

Mechanism (this docstring is the single home): GitHub's reference parser does not read English. A
subject like `B5T closed #1827's AC0 gap` (possessive prose) closed held-draft PR #1827 unmerged when
merged to the default branch. A verb guard on PR-closing commands cannot see this; only the message
content can.

Scope: local, offline, string-level. It never inspects PR bodies and cannot police commits authored
outside this repo's hooks (e.g. the GitHub web editor).

Detected reference forms, each immediately after a keyword on the SAME line:
  `#123`, `GH-123`, `owner/repo#123`, and a full `https://github.com/owner/repo/(issues|pull)/123`.
Keywords (case-insensitive, all nine GitHub honours): close/closes/closed, fix/fixes/fixed,
resolve/resolves/resolved. An optional single `:` between keyword and reference is honoured because
GitHub honours it.

Escape hatch — when you genuinely DO want to close something, one greppable opt-in:
  a trailer line `Closes-Intentionally: #123` in the message, or `ALLOW_CLOSING_KEYWORD=1` in env.

Usage (wired in the repo's `commit-msg` hook):
  python3 tools/check_commit_msg_closing_keyword.py <path-to-COMMIT_EDITMSG>
  ... | python3 tools/check_commit_msg_closing_keyword.py -        # message on stdin
Sweep real history for false positives:
  python3 tools/check_commit_msg_closing_keyword.py --scan-log 400
Selftest:
  python3 tools/check_commit_msg_closing_keyword.py --selftest
"""

from __future__ import annotations

import os
import re
import subprocess
import sys

KEYWORDS = (
    "close", "closes", "closed",
    "fix", "fixes", "fixed",
    "resolve", "resolves", "resolved",
)

OPT_IN_TRAILER = "Closes-Intentionally:"
OPT_IN_ENV = "ALLOW_CLOSING_KEYWORD"

# One line only (GitHub does not link across a newline); URL form first so it wins over `#\d+`.
_REFERENCE = (
    r"(?:https?://github\.com/[\w.-]+/[\w.-]+/(?:issues|pull)/\d+"
    r"|[\w.-]+/[\w.-]+#\d+"
    r"|GH-\d+"
    r"|#\d+)"
)
HAZARD_RE = re.compile(
    r"\b(" + "|".join(KEYWORDS) + r")\b[ \t]*:?[ \t]+(" + _REFERENCE + r")",
    re.IGNORECASE,
)


def strip_git_comments(raw: str) -> str:
    """Drop the `#`-comment lines git appends to COMMIT_EDITMSG (and everything after a
    `--verbose` scissors line), so the guard only ever sees the real message."""
    lines: list[str] = []
    for line in raw.splitlines():
        if line.startswith("# ------------------------ >8"):
            break
        if line.startswith("#"):
            continue
        lines.append(line)
    return "\n".join(lines)


def has_opt_in(message: str, env: dict | None = None) -> bool:
    env = os.environ if env is None else env
    if str(env.get(OPT_IN_ENV, "")).strip() not in ("", "0", "false", "no"):
        return True
    return any(
        line.strip().lower().startswith(OPT_IN_TRAILER.lower())
        for line in message.splitlines()
    )


def find_hits(message: str) -> list[str]:
    """Every `<keyword> <reference>` hazard in `message`, as the matched substrings."""
    if re.match(r"^(Merge|Revert|Squash)\b", message.lstrip()):
        return []
    return [m.group(0) for m in HAZARD_RE.finditer(message)]


def refusal(hits: list[str]) -> str:
    return "\n".join([
        "❌ Commit message contains a GitHub CLOSING KEYWORD next to an issue/PR reference:",
        *(f"     {h!r}" for h in hits),
        "",
        "Merging this to the default branch will CLOSE that issue/PR — GitHub's parser reads the",
        "keyword, not your English. This exact shape closed PR #1827 unmerged twice",
        "(see this module's docstring for the mechanism).",
        "",
        "If you meant it as prose, rewrite it — any of these are safe:",
        "  • drop the `#`:            `PR 1827` / `issue 1827`",
        "  • move the keyword away:   `the AC0 gap on #1827 is closed`",
        "  • use a non-keyword verb:  `addresses #1827`, `re #1827`, `see #1827`",
        "",
        "If you REALLY want to close it, opt in explicitly with a trailer line:",
        f"  {OPT_IN_TRAILER} #1827",
        f"(or set {OPT_IN_ENV}=1 for this one commit).",
    ])


def check_message(raw: str, env: dict | None = None) -> tuple[int, str]:
    """(exit_code, output) for one raw commit message."""
    message = strip_git_comments(raw)
    hits = find_hits(message)
    if not hits:
        return 0, "✅ No stray closing keyword."
    if has_opt_in(message, env):
        return 0, f"✅ Closing keyword present but opted in via {OPT_IN_TRAILER} — allowed."
    return 1, refusal(hits)


# ── history sweep (false-positive audit) ────────────────────────────────────────

def scan_log(count: int = 400, ref: str = "HEAD") -> list[tuple[str, list[str]]]:
    """Run the guard over the last `count` commit messages. Returns [(sha, hits), ...]."""
    # `%x00` is a git format placeholder for NUL — a literal NUL in argv is not passable to exec().
    out = subprocess.run(
        ["git", "log", f"-n{count}", "--format=%x00%H%n%s%n%b", ref],
        capture_output=True, text=True, check=True,
    ).stdout
    found: list[tuple[str, list[str]]] = []
    for chunk in out.split("\x00"):
        if not chunk.strip():
            continue
        sha, _, body = chunk.partition("\n")
        hits = find_hits(body)
        if hits:
            found.append((sha.strip(), hits))
    return found


# ── selftest ────────────────────────────────────────────────────────────────────

HISTORICAL_SUBJECT = (
    "docs(orchestration): B5T closed #1827's AC0 gap — and the CI pause turns out to be TREE-LOCAL"
)


def selftest() -> int:
    env: dict = {}
    # RED: the exact historical string that closed #1827.
    code, out = check_message(HISTORICAL_SUBJECT, env)
    assert code == 1, "the historical #1827 subject MUST be rejected"
    assert "closed #1827" in out, out
    # The refusal must teach every escape it claims to.
    for needle in (OPT_IN_TRAILER, "drop the `#`", "addresses #1827", "is closed"):
        assert needle in out, f"refusal must mention {needle!r}"

    # GREEN control arms.
    for ok in (
        "docs: the six lanes delivered; #1827 stays held as draft",
        "docs(orchestration): the AC0 gap on #1827 is closed",
        "fix(hooks): prevent false positive in secret detection",
        "chore: bump to 1.2.3",
        "docs: see #1827 and PR 1827 for context",
        "docs: addresses #1827",
        "fix: closes the door",                      # keyword, no reference
        "docs: closed 12 tasks",                     # keyword + bare number, no `#`
        "Merge pull request #1838 from acme/x\n\ncloses #1827",  # merge commits exempt
    ):
        code, out = check_message(ok, env)
        assert code == 0, f"false positive on {ok!r}:\n{out}"

    # Every keyword form, every reference form, must be caught.
    for kw in KEYWORDS:
        for ref in ("#1827", "GH-1827", "acme/hub#1827",
                    "https://github.com/acme/hub/pull/1827"):
            for sep in (" ", ": ", "  "):
                msg = f"docs: {kw}{sep}{ref} in passing"
                assert check_message(msg, env)[0] == 1, f"missed {msg!r}"
        assert check_message(f"docs: {kw.upper()} #1827", env)[0] == 1, "must be case-insensitive"

    # Opt-in arms.
    code, out = check_message("fix(api): real fix\n\nCloses-Intentionally: #1827", env)
    assert code == 0, f"explicit opt-in must be allowed:\n{out}"
    code, out = check_message("fix(api): real fix\n\nCloses #1827", {OPT_IN_ENV: "1"})
    assert code == 0, f"env opt-in must be allowed:\n{out}"
    assert check_message("fix(api): real fix\n\nCloses #1827", {OPT_IN_ENV: "0"})[0] == 1

    # A `Closes-Intentionally:` trailer must not itself be read as a hazard.
    assert find_hits("Closes-Intentionally: #1827") == []

    # git's own comment lines are stripped before checking.
    assert check_message("docs: fine\n\n# closes #1827\n", env)[0] == 0
    assert check_message("docs: fine\n\n# ------------------------ >8 ------\ncloses #1827", env)[0] == 0

    # Multiple hazards are all reported.
    hits = find_hits("docs: fixes #1 and resolves #2")
    assert len(hits) == 2, hits

    print("✅ check_commit_msg_closing_keyword selftest passed.")
    return 0


def main(argv: list[str]) -> int:
    if "--selftest" in argv:
        return selftest()
    if "--scan-log" in argv:
        i = argv.index("--scan-log")
        count = int(argv[i + 1]) if len(argv) > i + 1 else 400
        ref = argv[i + 2] if len(argv) > i + 2 else "HEAD"
        found = scan_log(count, ref)
        for sha, hits in found:
            print(f"{sha[:12]} {hits}")
        print(f"{len(found)} hit(s) in the last {count} commits.")
        return 0
    if not argv:
        print("usage: check_commit_msg_closing_keyword.py <COMMIT_EDITMSG|-> | --selftest "
              "| --scan-log N", file=sys.stderr)
        return 2
    raw = sys.stdin.read() if argv[0] == "-" else open(argv[0], encoding="utf-8").read()
    code, out = check_message(raw)
    print(out, file=sys.stderr if code else sys.stdout)
    return code


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
