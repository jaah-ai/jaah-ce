/* ── A node's operating system, as the console speaks it ─────────────────────────────────────────────
 * The vocabulary is the API's `nodes.os` (`apps/api/app/node_platform.py`, declared at create, never
 * changed). This module is the console's ONE home for what each OS is CALLED and what an operator must
 * know before running its install line (Architecture §5's copy). The declared `ubuntu` reads "Linux"; the
 * distribution the machine runs is the server's `distro_label`. It decides NOTHING about capabilities: what
 * a node may do is the server's `unsupported` map, read off the node — never an OS branch here. */

export type NodeOs = 'ubuntu' | 'macos' | 'windows'

/** The self-hosted create dialog's choices, Linux first (the default). */
export const NODE_OS_CHOICES: readonly { value: NodeOs; label: string }[] = [
  { value: 'ubuntu', label: 'Linux' },
  { value: 'macos', label: 'macOS (Apple Silicon)' },
  { value: 'windows', label: 'Windows (x64)' },
]

/** A launched machine's choices (Your Cloud, Jaah Cloud): the cloud's own images, so no macOS — and Ubuntu, the
 *  image it launches, not Linux. */
export const CLOUD_OS_CHOICES: readonly { value: NodeOs; label: string }[] = [
  { value: 'ubuntu', label: 'Ubuntu' },
  { value: 'windows', label: 'Windows Server' },
]

const LABEL: Record<NodeOs, string> = { ubuntu: 'Linux', macos: 'macOS', windows: 'Windows' }

/** The short name (`macOS`). An absent `os` is a pre-0326 row or a presence-only host: the API's own
 *  default, Linux. A value newer than this console reads as unknown, never blank. */
export function nodeOsLabel(os: NodeOs | undefined): string {
  return LABEL[os ?? 'ubuntu'] ?? 'Unknown'
}

const ARCH: Record<string, string> = { arm64: 'Arm', x64: 'x86' }

/** `Ubuntu 26.04 · Arm`: the server's `distro_label` (a Linux node's reported distribution) or the OS name, then
 *  the architecture once the machine has reported it (`arm64` → Arm, `x64` → x86, anything else as sent). */
export function nodeOsArchLabel(os: NodeOs | undefined, arch: string | null | undefined,
  distroLabel?: string | null): string {
  const name = distroLabel || nodeOsLabel(os)
  return arch ? `${name} · ${ARCH[arch] ?? arch}` : name
}

/** `No Windows bundle for 1.2.4746` when the release this node is judged against has no bundle for its OS
 *  (`NodeOut.release_missing_bundle`, `apps/api/app/platform_release.py`) — a publish gap, not a node fault;
 *  null otherwise. */
export function missingBundleText(os: NodeOs | undefined, missing: string | null | undefined): string | null {
  return missing ? `No ${nodeOsLabel(os)} bundle for ${missing}` : null
}

/** What a self-hosted install of this OS needs, said once before the operator registers and again beside
 *  the command. `null` for Ubuntu, whose flow is unchanged. `tailscale` marks the word the dialog links to
 *  the Tailscale variants page (macOS: the line installs its own boot-time `tailscaled`, and refuses a Mac
 *  that runs the Tailscale app instead). */
export type NodeOsGuide = {
  /** One line: minimum version, prerequisites, where to run it. Split around the linked word. */
  before: string
  tailscale: string
  /** Where the linked word points, and its hover sentence; null = plain text. */
  tailscaleLink: { href: string; title: string } | null
  after: string
  /** A requirement the machine itself must meet, said as a warning under the line: a few words, a hover with why,
   *  and the reader-docs page (a path on the docs base) with the steps. */
  warning?: { text: string; help: string; docsPath: string }
  /** A line to read once the command has run, or null. */
  afterRun: string | null
  /** The machine's own uninstall command. */
  uninstall: string
  /** The shell that keeps what was pasted (the command holds the token), and where it keeps it. */
  history: { shell: string; file: string }
  /** macOS: the one binary to grant Full Disk Access once the line has run (NodeLinks need it), and the API route
   *  serving the same grant as an MDM profile (`apps/api/app/macos_pppc.py`). */
  fullDiskAccess?: { path: string; profilePath: string }
}

const GUIDES: Record<NodeOs, NodeOsGuide | null> = {
  ubuntu: null,
  macos: {
    before: 'macOS 14+ on Apple Silicon. Installs ',
    tailscale: 'Tailscale',
    // The variant page: the line installs the standalone tailscaled and refuses a Mac running the app.
    tailscaleLink: {
      href: 'https://tailscale.com/kb/1065/macos-variants',
      title: 'Its own tailscaled, which runs at boot. Quit and remove the Tailscale app first, if installed.',
    },
    after: ' and the rest itself. Run in Terminal as an admin.',
    afterRun: 'Close this Terminal window after it finishes.',
    uninstall: 'sudo /Library/Jaah/current/bin/jaah-uninstall',
    history: { shell: 'zsh', file: '~/.zsh_history' },
    fullDiskAccess: {
      path: '/Library/Jaah/bin/jaah-anchor',
      profilePath: '/api/nodes/macos/full-disk-access.mobileconfig',
    },
  },
  windows: {
    before: 'Windows 11 or 10 22H2, x64. Installs Git for Windows, Node.js, WSL 2 and ',
    tailscale: 'Tailscale',
    tailscaleLink: null,
    after: ' if missing. Run in PowerShell as Administrator.',
    afterRun: null,
    uninstall: "& 'C:\\Program Files\\Jaah\\current\\bin\\jaah-uninstall.ps1'",
    history: { shell: 'PowerShell', file: '(Get-PSReadLineOption).HistorySavePath' },
    warning: {
      text: 'Nested virtualization must be on',
      help: 'Docker and NodeLink run in WSL 2, which starts a virtual machine. Without it the node runs tasks, '
        + 'but no containers or NodeLink. EC2: a C8i, M8i or R8i (or .metal) instance with nested virtualization '
        + 'turned on. A PC: VT-x or AMD-V on in the firmware.',
      docsPath: '/using-jaah/nodes/add-a-node#turn-on-nested-virtualization',
    },
  },
}

export function nodeOsGuide(os: NodeOs | undefined): NodeOsGuide | null {
  return GUIDES[os ?? 'ubuntu']
}
