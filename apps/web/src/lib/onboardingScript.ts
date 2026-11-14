/* ── The node install command builders ───────────────────────────────────────────────────────────────
 * The one line a machine runs to become a node, as the console shapes it: alone, wrapped in the tailnet
 * join where the console is out of the machine's reach, or as an EC2 user-data script. Shown by
 * `components/NodeOnboardingDialog` and the Add node flow (`components/addNode`). */

/* The self-hosted script on a console the internet cannot reach: join the tailnet, then fetch the bootstrap
 * from the address nodes dial, an internal ALB reached via the bastion subnet route — a bare box cannot fetch
 * anything until it is on the tailnet accepting routes. A public console serves the bootstrap to any machine,
 * and the bootstrap joins the tailnet itself, so its line runs alone (`onboardingScript`).
 *   * A `{ … }` group, so bash reads every line before the first `sudo` prompts — pasted lines would
 *     otherwise be swallowed as the password.
 *   * The join only when `tailscale status` fails (as `node_bootstrap.py` does): on a box already on a
 *     tailnet, `install.sh` could restart `tailscaled` and cut the SSH session running this. No
 *     `tailscale set` either: on a box logged into some OTHER tailnet it would turn on Tailscale SSH and
 *     accept that tailnet's routes.
 *   * The group opens with a space so bash `ignorespace` keeps the whole entry (token included) out of
 *     history; zsh ignores it (`HIST_IGNORE_SPACE` is off by default) — a partial mitigation.
 *   * `--ssh` so a failed enrolment still leaves a shell. The login server is the CP's configured
 *     headscale (`tailnet_login_server`, a validated bare https origin, so unquoted); without one the flag
 *     is omitted and the dialog says so.
 *   * `--authkey` when the server minted one (`tailnet_auth_key`): the join then needs no headscale admin and
 *     runs unattended, so the line works as user-data too. Single-quoted; the key is single-use. */
function selfHostedScript(loginServer: string | null, fetchCommand: string, authKey: string | null): string {
  const server = loginServer ? `--login-server ${loginServer} ` : ''
  // Never a key without its server: presented to Tailscale's own, a headscale key is disclosed.
  const key = authKey && loginServer ? `--authkey '${authKey}' ` : ''
  return [
    ' {',
    '  if ! tailscale status >/dev/null 2>&1; then',
    '    curl -fsSL https://tailscale.com/install.sh | sh',
    `    sudo tailscale up ${server}${key}--accept-routes --ssh`,
    '  fi',
    `  ${fetchCommand.trim()}`,
    '}',
  ].join('\n')
}

/** The one command a self-hosted machine runs: the line alone, or wrapped in the join where the server says the
 *  console is out of the machine's reach (`onboarding_joins_tailnet`). Verbatim: a leading space keeps the
 *  line out of the shell's history. */
export function onboardingScript(
  command: string, joinsTailnet: boolean | undefined, loginServer: string | null, authKey: string | null = null,
): string {
  return joinsTailnet ? selfHostedScript(loginServer, command, authKey) : command
}

/* The same install as a script for EC2 user-data / cloud-init: cloud-init runs user-data only when its first line
 * is a `#!`, and skips a bare command without a word ("Unhandled non-multipart userdata"). Not offered for a join
 * with no key: as user-data nobody could answer its `tailscale up` login. */
export function userDataScript(line: string): string {
  return ['#!/bin/bash', line.trim()].join('\n')
}

export type RunAs = 'command' | 'script'

/** Command or Script: shown wherever a bare Ubuntu box gets its install command. */
export const RUN_AS = [
  { value: 'command', label: 'Command', help: 'Paste in a shell', testId: 'run-as-command' },
  { value: 'script', label: 'Script', help: 'EC2 user-data, cloud-init', testId: 'run-as-script' },
] as const
