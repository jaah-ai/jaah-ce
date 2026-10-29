# Release checklist

The manual oracles a release runs before it is announced. The Linux path (install, first account, a coding task
and its pull request on the native node) is exercised on a fresh VM by the publishing pipeline; everything below
needs a person or a host that pipeline does not have. Record each result (pass, fail, skipped and why) in the
release's notes.

## Install, no account anywhere

- [ ] **The public one-line run.** On a machine with no GitHub, Docker Hub or Jaah login, run the README's quick
      start line exactly as written. It finishes without a prompt, a token or a config edit, and prints
      `http://localhost:3050`.
- [ ] **Loopback only.** `ss -ltn` (Linux) or `netstat -an` (macOS, Windows) shows ports 3050 and 8000 bound to
      `127.0.0.1` alone.
- [ ] **Signed images.** For each of `api`, `web` and `node`, with the digest from the release's `compose.yml`:
      `cosign verify ghcr.io/jaah-ai/jaah-ce-<img>@<digest> --certificate-identity-regexp '.*jaah-ce.*release.yml.*' --certificate-oidc-issuer https://token.actions.githubusercontent.com`
      succeeds.
- [ ] **Pinned compose.** The release's assets list `compose.yml` and `SHA256SUMS`; in a directory holding both,
      `sha256sum -c SHA256SUMS` passes, `grep -n "latest" compose.yml` prints nothing, and every `image:` line
      carries `@sha256:`.
- [ ] **Installers between publish and release.** The installers refuse a release without `SHA256SUMS`, and `main`
      carries them as soon as the mirror publishes: run the quick start once the tag's release is out (minutes after the
      publish), and expect the first release that carries `SHA256SUMS` to be the oldest one any installer accepts.
- [ ] **Upgrade.** From the previous release, `install.sh --upgrade` (or `install.ps1 -Upgrade`) moves every image
      to the new tag and keeps the account, projects and tasks.
- [ ] **Removed S3 and KMS settings.** The release notes say the edition no longer reads `SESSION_TRANSCRIPT_BUCKET`,
      `CREDENTIAL_KMS_KEY_ID` or `CREDENTIAL_KMS_PREVIOUS_KEY_IDS` and point to the README's upgrade steps (copy the
      bucket into the volumes, then unset it; under KMS, re-enter credentials or stay on the previous release).
      Starting with one still set refuses with a message naming it.

## Platforms the pipeline does not cover

- [ ] **macOS** (Apple silicon, Docker Desktop): the quick start brings up the console and the container node; a
      coding task runs and opens its pull request.
- [ ] **Windows** (Docker Desktop on WSL 2): the PowerShell quick start does the same.
- [ ] **WSL shell**: `install.sh` run inside a WSL distribution chooses the container node, not the native one.

## Agent CLIs beyond Claude Code

Each with its own account, one coding task to a pull request:

- [ ] **Codex**
- [ ] **Antigravity**
- [ ] **Pi**

## No phone home

- [ ] With a capture running on the host (`tcpdump` or the OS firewall log), start the stack and leave it idle for
      ten minutes: no connection to any Jaah host, and none to a CLI vendor other than the model provider an agent
      is using.
- [ ] **The CLIs with no environment off-switch** — the list closing node/cli_env: Codex CLI and the Antigravity
      CLI. For each, run one task and confirm from the capture which analytics or update calls it makes; record
      them in the release notes, as the off-switch for these lives only in the CLI's own settings.

## Before the public flip

Once, before the CE repository goes public; each is an owner's action, not a release run's.

- [ ] **Legal**: have counsel review LICENSE and the use of the Jaah trademark.
- [ ] **Pricing**: update the pricing doc's "platform repository is private" line.
- [ ] **Site**: add the community edition to the website.
