import { useState } from 'react'
import Box from '@mui/material/Box'
import Typography from '@mui/material/Typography'
import { Banner } from '../ui/Banner'
import { CopyButton } from '../ui/CopyButton'
import FormDialog from '../ui/FormDialog'
import { monoSx } from '../ui/listStyles'
import { fmtTimestamp } from '../lib/relativeTime'
import { type NodeOs, nodeOsGuide } from '../lib/nodeOs'
import type { NodeCredential } from '../types'
import NodeOsRequirements from './NodeOsRequirements'
import { FullDiskAccessStep } from './FullDiskAccessStep'
import { CodeBlock } from '../ui/CodeBlock'
import { ChoiceField } from '../ui/ChoiceField'

/* Onboarding a freshly credentialed node — the one screen that shows a node's token, exactly once: the
 * CP stores only `sha256(token)` (apps/api/app/node_credentials.py); recovery is re-issue.
 *   * ONE thing to copy: the token rides inside the command, so it is never shown on its own.
 *   * Modal, and Backdrop/Escape do not close it: the Nodes list refetches on `node` events.
 *   * The script is selectable text as well as a copy button: `navigator.clipboard` may be absent.
 *   * The fetch line comes from apps/api/app/node_bootstrap.py::render_fetch_command; the machine fetches
 *     the script itself (apps/api/app/routes/agent_bootstrap.py).
 * The token never goes in a `title` or `aria-label` (hover, screenshots and a11y tooling read them). */

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
 *     is omitted and the dialog says so. */
function selfHostedScript(loginServer: string | null, fetchCommand: string): string {
  const server = loginServer ? `--login-server ${loginServer} ` : ''
  return [
    ' {',
    '  if ! tailscale status >/dev/null 2>&1; then',
    '    curl -fsSL https://tailscale.com/install.sh | sh',
    `    sudo tailscale up ${server}--accept-routes --ssh`,
    '  fi',
    `  ${fetchCommand.trim()}`,
    '}',
  ].join('\n')
}

/** The one command a self-hosted machine runs: the line alone, or wrapped in the join where the server says the
 *  console is out of the machine's reach (`onboarding_joins_tailnet`). Verbatim: a leading space keeps the
 *  line out of the shell's history. */
export function onboardingScript(command: string, joinsTailnet: boolean | undefined, loginServer: string | null): string {
  return joinsTailnet ? selfHostedScript(loginServer, command) : command
}

/* The same install as a script for EC2 user-data / cloud-init: cloud-init runs user-data only when its first line
 * is a `#!`, and skips a bare command without a word ("Unhandled non-multipart userdata"). Offered only for the bare
 * line: as user-data nobody could answer the join's `tailscale up` login, and the fetched bootstrap joins itself. */
export function userDataScript(fetchCommand: string): string {
  return ['#!/bin/bash', fetchCommand.trim()].join('\n')
}

export type RunAs = 'command' | 'script'

/** Command or Script: shown wherever a bare Ubuntu box gets its install command. */
export const RUN_AS = [
  { value: 'command', label: 'Command', hint: 'Paste in a shell', testId: 'run-as-command' },
  { value: 'script', label: 'Script', hint: 'EC2 user-data, cloud-init', testId: 'run-as-script' },
] as const

// Flags never wrap at their own hyphens (`--accept-routes -` / `-ssh` reads as a broken flag).
function unbrokenFlags(line: string) {
  return line.split(/(\s+)/).map((part, index) =>
    part.startsWith('-')
      ? <Box component="span" key={index} sx={{ whiteSpace: 'nowrap' }}>{part}</Box>
      : part,
  )
}

// One block per line with a hanging indent, so a wrapped continuation never reads as a new command. Only
// the rendering changes: each line keeps its own newline, so the text — and the copy — is the script
// verbatim.
function scriptLines(script: string) {
  const lines = script.split('\n')
  return lines.map((line, index) => (
    <Box component="span" key={index} sx={{ display: 'block', pl: '8ch', textIndent: '-8ch' }}>
      {unbrokenFlags(line)}
      {index < lines.length - 1 ? '\n' : ''}
    </Box>
  ))
}

// A terminal: dark in both themes, so it reads as "paste this into a shell" at a glance.
const TERMINAL_SX = {
  position: 'relative',
  borderRadius: 1.5,
  bgcolor: 'grey.900',
  color: 'grey.100',
  border: 1,
  borderColor: 'grey.800',
} as const

/* Room on the right (top, at phone width) for the copy button; wraps so a ~120-char line never clips
 * mid-URL; selectable as the fallback where the clipboard API is unavailable. */
const SCRIPT_SX = { p: 2, pr: { xs: 2, sm: 12 }, pt: { xs: 7, sm: 2 }, lineHeight: 1.7, userSelect: 'all' } as const

export function NodeOnboardingDialog({
  credential,
  reissued = false,
  reenrolUntil,
  launched,
  os = 'ubuntu',
  onClose,
}: {
  credential: NodeCredential
  /** The node's declared OS (`NodeRecord.os`): picks the join's shell and the words around the command. */
  os?: NodeOs
  /** A rotation rather than a registration — the same secret, a materially different warning. */
  reissued?: boolean
  /* A re-enrolment (`POST /api/nodes/{name}/tailnet-reenrol`): a rotation that also hands the next
     bootstrap run ONE tailnet key until this instant. Implies `reissued`. The node is already on a
     tailnet, so the join is not prepended — the command itself re-joins it. */
  reenrolUntil?: string
  /* Cloud flow: the bootstrap went as EC2 user-data, so the command is recovery; show the instance and
     region so the operator can find it if it never dials in. */
  launched?: { instanceId: string; region: string }
  onClose: () => void
}) {
  // A launched node and a re-enrolled one are already on the network: never wrapped.
  const [runAs, setRunAs] = useState<RunAs>('command')
  const joinsTailnet = !launched && !reenrolUntil && Boolean(credential.onboarding_joins_tailnet)
  const rotated = reissued || Boolean(reenrolUntil)
  // Ubuntu's flow is unchanged: no guide, the original words.
  const guide = launched ? null : nodeOsGuide(os)
  // A bare Ubuntu box may be a cloud instance whose only input is its user-data.
  const offersScript = !launched && !reenrolUntil && !joinsTailnet && !guide && credential.onboarding_shell !== 'powershell'
  const script = offersScript && runAs === 'script'
    ? userDataScript(credential.onboarding_command)
    : onboardingScript(credential.onboarding_command, joinsTailnet, credential.tailnet_login_server)
  return (
    <FormDialog
      // Neither Escape nor backdrop closes it: the credential cannot be read again.
      modal
      hideCancel
      // A launched instance was handed its bootstrap, so its owner has copied nothing.
      submitLabel={launched ? 'Done' : 'I’ve copied it'}
      submitAutoFocus
      onSubmit={onClose}
      onClose={onClose}
      testId="node-onboarding-dialog"
      submitTestId="node-onboarding-done"
      // The title confirms which of the four actions happened.
      title={reenrolUntil
        ? `Re-enrol ${credential.name}`
        : reissued
          ? `New token for ${credential.name}`
          : launched
            ? `${credential.name} launched`
            : `${credential.name} registered`}
    >
      {launched ? (
        // Cloud flow: nothing to run; prose, not a code block, so the instance id is not pasted.
        <Typography variant="body2" sx={{ color: 'text.secondary' }} data-testid="node-onboarding-launched">
          Instance <Box component="span" sx={monoSx}>{launched.instanceId}</Box> is starting
          in <strong>{launched.region}</strong>. It comes online on its own.
        </Typography>
      ) : guide && !reenrolUntil ? (
        // The guide's line already says where and how to run it.
        <NodeOsRequirements os={os} testId="node-onboarding-requirements" />
      ) : (
        <Typography variant="body2" sx={{ color: 'text.secondary' }}>
          Run this on the machine{reenrolUntil ? ' to re-join the tailnet' : ''}.
        </Typography>
      )}

      {reenrolUntil && (
        <Typography variant="body2" data-testid="node-onboarding-reenrol">
          Run it before {fmtTimestamp(reenrolUntil)}.
        </Typography>
      )}

      {offersScript && (
        <ChoiceField<RunAs> label="Run as" row value={runAs} options={RUN_AS} onChange={setRunAs} testId="node-onboarding-run-as" />
      )}

      <Box>
        {launched && (
          <Typography variant="caption" sx={{ display: 'block', mb: 0.75, color: 'text.secondary' }}>
            Only if it never comes online:
          </Typography>
        )}
        <Box sx={TERMINAL_SX}>
          <CopyButton
            testId="node-onboarding-copy-command"
            label="Copy"
            ariaLabel="Copy command"
            text={() => script}
            sx={{ position: 'absolute', top: 10, right: 10, color: 'grey.100', borderColor: 'grey.700', bgcolor: 'grey.900' }}
          />
          <CodeBlock
            testId="node-onboarding-command"
            data-shell={credential.onboarding_shell ?? 'bash'}
            tone="plain"
            size="medium"
            sx={SCRIPT_SX}
          >
            {scriptLines(script)}
          </CodeBlock>
        </Box>
      </Box>

      {/* `ignorespace` is bash's; zsh keeps the line (token included) unless told otherwise. User-data is no shell's history. */}
      {runAs === 'command' && <Typography variant="caption" sx={{ color: 'text.secondary', mt: -1 }} data-testid="node-onboarding-history">
        {/* Ubuntu's words are the guide-less default; the path never breaks at its own hyphen. */}
        Paste it whole. {guide?.history.shell ?? 'zsh'} saves it to history — delete it
        from <Box component="code" sx={{ whiteSpace: { sm: 'nowrap' }, overflowWrap: 'anywhere' }}>{guide?.history.file ?? '~/.zsh_history'}</Box>.
      </Typography>}

      {guide?.afterRun && (
        <Typography variant="body2" data-testid="node-onboarding-after-run">{guide.afterRun}</Typography>
      )}

      {/* Not on a re-enrol: the node is installed, and its grant outlives a token. */}
      {guide?.fullDiskAccess && !reenrolUntil && <FullDiskAccessStep grant={guide.fullDiskAccess} />}

      {/* Not on a re-enrol: the node is installed and staying. */}
      {guide && !reenrolUntil && (
        <Typography variant="caption" sx={{ color: 'text.secondary' }} data-testid="node-onboarding-uninstall">
          To uninstall:{' '}
          <Box component="code" sx={[monoSx, { overflowWrap: 'anywhere', userSelect: 'all' }]}>{guide.uninstall}</Box>
        </Typography>
      )}

      {joinsTailnet && !credential.tailnet_login_server && (
        <Banner severity="warning" testId="node-onboarding-no-login-server">
          <Typography component="span" variant="body2">
            No tailnet set in Settings &gt; Infrastructure &gt; Cloud — this joins Tailscale’s public one.
          </Typography>
        </Banner>
      )}

      <Banner severity="warning" testId="node-onboarding-warning">
        <Typography component="span" variant="body2">
          {/* A rotation has ALREADY revoked the old token, so the warning differs. */}
          {rotated
            ? 'The old token has stopped working. This command is shown only once.'
            : 'This command holds the node’s token and is shown only once.'}
        </Typography>
      </Banner>
    </FormDialog>
  )
}
