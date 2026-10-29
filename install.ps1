# install.ps1 — install or upgrade Jaah Community Edition on Windows (Docker Desktop). Linux and macOS: install.sh.
#
#   irm https://raw.githubusercontent.com/jaah-ai/jaah-ce/main/install.ps1 | iex
#   & ([scriptblock]::Create((irm https://raw.githubusercontent.com/jaah-ai/jaah-ce/main/install.ps1))) -Upgrade
#
# Checks Docker, fetches the latest release's compose.yml into ~\.jaah-ce (checked against the release's
# SHA256SUMS), starts the stack with the node as a container (the `node` profile), and prints the console's address.
# Nothing to edit: the installer writes the one setting the stack reads, JAAH_VERSION, to ~\.jaah-ce\.env.
param([switch]$Upgrade)
# Not 'Stop': Windows PowerShell 5.1 turns a native command's stderr into an error record, so a docker warning would
# abort a healthy install. Each cmdlet below that must stop says so; each docker call is judged by its exit code.

$repo = if ($env:JAAH_CE_REPO) { $env:JAAH_CE_REPO } else { 'jaah-ai/jaah-ce' }
# Tests point these at a local server; unset, they are GitHub.
$releaseBase = if ($env:JAAH_CE_RELEASE_BASE) { $env:JAAH_CE_RELEASE_BASE } else { 'https://github.com' }
$apiBase = if ($env:JAAH_CE_API_BASE) { $env:JAAH_CE_API_BASE } else { 'https://api.github.com' }
$dir = Join-Path $HOME '.jaah-ce'
$console = 'http://localhost:3050'
$health = 'http://127.0.0.1:8000/api/health'

function Say($msg) { Write-Host "jaah-ce: $msg" }
# `throw`, never `exit`: under `irm | iex` the script runs in the caller's session, which `exit` would close.
function Die($msg) { throw "jaah-ce: $msg" }

# ── Docker ────────────────────────────────────────────────────────────────────────────────────────────────────
if (-not (Get-Command docker -ErrorAction SilentlyContinue)) {
  Die 'Docker Desktop is not installed — get it from https://docs.docker.com/desktop/setup/install/windows-install/ and run this again'
}
docker compose version 2>&1 | Out-Null
if ($LASTEXITCODE -ne 0) { Die 'Docker Compose v2 is missing — update Docker Desktop and run this again' }
docker info 2>&1 | Out-Null
if ($LASTEXITCODE -ne 0) { Die 'Docker Desktop is installed but not running — start it and run this again' }

# ── the latest release ────────────────────────────────────────────────────────────────────────────────────────
# A GitHub Release, made only once every image of its tag is published (.github/workflows/release.yml).
$tag = (Invoke-RestMethod -Uri "$apiBase/repos/$repo/releases/latest" -UseBasicParsing -ErrorAction Stop).tag_name
if ($tag -notmatch '^v\d+\.\d+\.\d+$') { Die "could not find a release of $repo on GitHub" }
$version = $tag.Substring(1)

$compose = Join-Path $dir 'compose.yml'
if ($Upgrade -and -not (Test-Path $compose)) { Die "no install at $dir — run install.ps1 without -Upgrade" }

# ── the release's files, checked before anything in $dir changes ──────────────────────────────────────────────
# The release's compose.yml pins every image by its signed digest; SHA256SUMS covers every file fetched here.
$tmp = Join-Path ([IO.Path]::GetTempPath()) ([IO.Path]::GetRandomFileName())
New-Item -ItemType Directory -Force -Path $tmp -ErrorAction Stop | Out-Null
try {
  $assets = "$releaseBase/$repo/releases/download/$tag"
  function Fetch($file) { Invoke-WebRequest -UseBasicParsing -MaximumRedirection 5 -Uri "$assets/$file" -OutFile (Join-Path $tmp $file) -ErrorAction Stop }
  try { Fetch 'SHA256SUMS' } catch { Die "SHA256SUMS: missing from release $tag — refusing to install files it cannot check" }
  $sums = @{}
  foreach ($line in Get-Content (Join-Path $tmp 'SHA256SUMS')) {
    if ($line -match '^([0-9a-f]{64}) [ *](.+)$') { $sums[$Matches[2]] = $Matches[1] }
  }
  foreach ($file in @('compose.yml')) {
    try { Fetch $file } catch { Die "${file}: could not download it from release $tag" }
    if (-not $sums.ContainsKey($file)) { Die "${file}: not listed in the SHA256SUMS of release $tag — not installing it" }
    if ((Get-FileHash -Algorithm SHA256 (Join-Path $tmp $file)).Hash -ne $sums[$file].ToUpperInvariant()) {
      Die "${file}: checksum mismatch against the SHA256SUMS of release $tag — not installing it"
    }
  }
  New-Item -ItemType Directory -Force -Path $dir -ErrorAction Stop | Out-Null
  Copy-Item -Force (Join-Path $tmp 'compose.yml') "$compose.new" -ErrorAction Stop
  Move-Item -Force "$compose.new" $compose -ErrorAction Stop
} finally { Remove-Item -Recurse -Force $tmp -ErrorAction SilentlyContinue }
Set-Content -Path (Join-Path $dir '.env') -Value "JAAH_VERSION=$version" -Encoding ascii -ErrorAction Stop

function Compose { docker compose --project-directory $dir -f $compose @args; if ($LASTEXITCODE -ne 0) { Die "docker compose $args failed" } }

# ── the control plane and the node ────────────────────────────────────────────────────────────────────────────
Say "starting Jaah Community Edition $version"
Compose --profile node pull --quiet
Compose up -d
$deadline = (Get-Date).AddMinutes(3)
while ($true) {
  try { Invoke-WebRequest -UseBasicParsing -Uri $health -TimeoutSec 2 | Out-Null; break } catch {}
  if ((Get-Date) -gt $deadline) { Die "the control plane did not come up in 3 minutes — see: docker compose -f $compose logs api" }
  Start-Sleep -Seconds 1
}
Compose --profile node up -d node

Say "ready — open $console"
