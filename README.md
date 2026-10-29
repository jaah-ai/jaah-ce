# Jaah Community Edition

Jaah on your own machine: the console and the AI coding agents it runs, for one person, on one computer. Free to
use under the [Sustainable Use License](LICENSE), with up to 3 AI provider accounts and 3 agents working at once.

> [!WARNING]
> **Jaah Community Edition is for evaluation and development only, not production.**
> Do not use it with production systems or with sensitive or regulated data: personal, health or financial data,
> or production credentials and secrets. It comes as is, without warranty of any kind ([LICENSE](LICENSE), "No
> Liability"). For production, use [Jaah Cloud](https://jaah.ai).

## Quick start

You need [Docker](https://docs.docker.com/get-docker/) running and at least 8 GB of memory: each agent working at
once takes 3 GB (Settings › Runners › Runner memory).

Linux or macOS:

```bash
curl -fsSL https://raw.githubusercontent.com/jaah-ai/jaah-ce/main/install.sh | sh
```

Windows (PowerShell, with Docker Desktop):

```powershell
irm https://raw.githubusercontent.com/jaah-ai/jaah-ce/main/install.ps1 | iex
```

Then open http://localhost:3050 and create your account.

## Upgrade

```bash
curl -fsSL https://raw.githubusercontent.com/jaah-ai/jaah-ce/main/install.sh | sh -s -- --upgrade
```

On Windows, run the install line again with `-Upgrade`:

```powershell
& ([scriptblock]::Create((irm https://raw.githubusercontent.com/jaah-ai/jaah-ce/main/install.ps1))) -Upgrade
```

Your data stays in Docker volumes across upgrades.

### S3 bucket and KMS key settings are gone

The community edition keeps attachments and sessions on its own volumes and wraps credentials under the keypair it
generates. It no longer reads `SESSION_TRANSCRIPT_BUCKET`, `CREDENTIAL_KMS_KEY_ID` or `CREDENTIAL_KMS_PREVIOUS_KEY_IDS`:
an install that still sets one refuses to start, naming the setting to unset. If your install sets any of them, do
this before you upgrade:

1. **A bucket.** Copy its objects into the volumes, keeping each key: `task-attachments/…` into the
   `jaah-attachments` volume, `transcripts/…` and `artifacts/…` into `jaah-sessions`. For example, with the stack
   stopped: `aws s3 sync s3://<bucket>/task-attachments <attachments volume>/task-attachments` (and the same for the
   other two prefixes into the sessions volume). Then unset `SESSION_TRANSCRIPT_BUCKET`. The bucket stays yours;
   nothing deletes it.
2. **A KMS key.** Credentials sealed under it cannot be opened after the upgrade. Unset both KMS settings, start the
   new release and enter each credential again, or stay on the previous release.

### AWS settings are gone

The community edition ships no AWS client. It ignores the SES settings (`SES_MAIL_DOMAIN`, `SES_CONFIGURATION_SET`), so
it sends no system email, and `NODE_LOG_GROUP`. It refuses to start with `DB_SECRET_ARN`: put the database
credential in `DATABASE_URL` instead.

## Run it with Docker Compose yourself

```bash
git clone https://github.com/jaah-ai/jaah-ce.git && cd jaah-ce
git checkout "$(git tag -l 'v*' --sort=-v:refname | head -n 1)"
echo "JAAH_VERSION=$(git describe --tags | sed 's/^v//')" > .env
docker compose --profile node up -d
```

## Build from source

The same checkout builds every image instead of pulling the published ones:

```bash
docker compose --profile node build
docker compose --profile node up -d
```

## Stop, start, remove

```bash
docker compose -f ~/.jaah-ce/compose.yml --profile node stop
docker compose -f ~/.jaah-ce/compose.yml --profile node start
docker compose -f ~/.jaah-ce/compose.yml --profile node down --volumes   # deletes your data
```

## What it is not

Jaah Community Edition listens on `127.0.0.1` only, runs on a single machine, and sends nothing home: no telemetry, no
update check, no licence server. Teams, more agents, and machines in the cloud are on
[Jaah Cloud](https://jaah.ai).

## Contributing

This repository is a read-only mirror of the Jaah source, published on every release. Issues are welcome; pull
requests from forks are closed automatically.

## Doctrine

`doctrine/` holds the edition's own runner charter and context-architecture policy.
